"""The per-intent cleanup journal: bounded files, single-intent I/O and the one-time v1 migration."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ummanu.dispatch import cleanup
from ummanu.dispatch.cleanup import (
    INTENT_FILE_LIMIT,
    CleanupJournal,
    _compact_heads,
    _intent_key,
    cleanup_record,
)
from ummanu.dispatch.types import HostError
from ummanu.runtime.head import HeadRun, HeadSpec, StopInitiator, TaskRef

STATUSES = ("owned", "pending", "preserved", "completed")
HEAD_IDENTITY = ("run_id", "scope_generation", "spec", "head_runtime", "workspace", "task_ref", "role", "handle",
                 "leaf", "pid_file", "lifecycle", "stopped_by")


def big_policy(run: HeadRun, *, baseline: int, events: int) -> dict:
    """The shape production heads carry: a session baseline of every earlier journal, and events."""
    return {
        "version": 1,
        "state": "unknown",
        "terminal_state": "unknown",
        "reason": "fan-out provider source binding is missing",
        "events": [
            {"type": "collaboration_call", "raw_event_digest": hashlib.sha256(str(n).encode()).hexdigest(),
             "source_sequence": n, "source_location": f"/codex/sessions/2026/10/07/rollout-{run.run_id}.jsonl:{n}",
             "captured_at": "2026-10-07T22:00:00Z", "tool": "spawn_agent", "parent_thread_id": "parent-1"}
            for n in range(events)
        ],
        "provider_source_required": True,
        "provider_source": {
            "version": 1, "kind": "codex_session_event_jsonl", "state": "unbound", "root": "/codex/sessions",
            "run_id": run.run_id, "role": run.role, "workspace": run.workspace,
            "baseline": [f"/home/dev/.codex/sessions/2026/10/{day:02d}/rollout-2026-10-07T00-00-00-{n:032x}.jsonl"
                         for day in range(1, 2) for n in range(baseline)],
        },
        "provider_progress_source": {"kind": "codex_session_event_jsonl", "path": "/codex/x.jsonl", "line": 9},
        "prompt_identity": {"digest": "a" * 64, "path": run.workspace + "/TASK.md"},
    }


def head(ref: str, index: int, *, baseline: int, events: int, exited: bool = False) -> dict:
    run = HeadRun(
        run_id=f"run-{ref}-{index}",
        spec=HeadSpec(profile_id="codex-high", adapter="codex", model="gpt-5.6-terra"),
        workspace=f"/data/workspaces/ummanu/{ref}-worker",
        task_ref=TaskRef.card(ref),
        role="worker" if index % 2 == 0 else "reviewer",
        handle=f"%{index}",
        leaf=f"leaf-{index}",
        pid_file=f"/data/heads/{ref}-{index}.pid",
        scope_generation=f"generation-{index}",
    )
    if exited:
        run = run.finishing(StopInitiator(actor="ummanu-dispatcher", reason="owned residue cleanup")).exited()
    raw = run.to_json()
    raw["fanout_policy"] = big_policy(run, baseline=baseline, events=events)
    return raw


def record(ref: str, *, baseline: int, events: int) -> dict:
    return {
        "attempt_id": f"attempt-{ref}",
        "worker": f"{ref}-worker",
        "workspace": f"/data/workspaces/ummanu/{ref}-worker",
        "handle": "%1",
        "worker_pid_file": f"/data/heads/{ref}-worker.pid",
        "worker_head_run": head(ref, 0, baseline=baseline, events=events),
        "review_head_run": head(ref, 1, baseline=baseline, events=events),
        "launch_intent": {"head_run": head(ref, 2, baseline=baseline // 4, events=2), "prompt": "x" * 2000},
        "gate_attestation": {"evidence": "g" * 4600},
        "po_submission": {"body": "p" * 4000},
        "worker_progress_at": 1.5,
    }


def intent(ref: str, status: str, *, baseline: int = 180, events: int = 40, heads: int = 2) -> dict:
    raw = record(ref, baseline=baseline, events=events)
    # A pending obligation has not verified its heads yet: launch admission refuses its card.
    progress = {"heads_stopped": status in ("preserved", "completed"), "claim_settled": status == "completed"}
    if status == "preserved":
        progress["preservation_verified"] = True
    if status == "completed":
        progress.update(workspace_removed=True, ref_removed=True, removal_started=True)
    reason = {"pending": "cleanup workspace disappeared without removal evidence",
              "preserved": "dirty tracked, untracked or ignored work: ?? notes.txt"}.get(status, "")
    value = {
        "task": {"id": hash(ref) % 10**6, "ref": ref, "project": "ummanu", "sprint": "sprint:1484",
                 "state": "done", "claim": {"worker": f"{ref}-worker"}, "description": "d" * 3000},
        "record": raw,
        "identity": {"repo": "/home/dev/ummanu", "common": "/home/dev/ummanu/.git", "branch": "refs/heads/pipeline/" + ref,
                     "tip": "f" * 40, "workspace": raw["workspace"], "device": 2049, "inode": 1234},
        "heads": [head(ref, n, baseline=baseline, events=events, exited=status != "owned") for n in range(heads)],
        "progress": progress,
        "status": status,
        "reason": reason,
        "disposition": "owned" if status == "owned" else "done",
    }
    if status == "completed":
        value["commit_proof"] = {"tip": "f" * 40, "publication": "remote-tracking", "refs": ["refs/remotes/origin/main"]}
        value["generated_environment"] = {"device": 1, "inode": 2, "owner": "ummanu-dispatcher",
                                          "workspace": raw["workspace"], "schema_version": 1}
    return value


def legacy_journal(count: int = 300, *, largest: int = 3) -> dict:
    """A released v1 document: every status, three ~1 MB intents like production's largest."""
    intents = {}
    for n in range(count):
        ref = f"ummanu-{n}"
        if n < largest:
            value = intent(ref, STATUSES[n % 4], baseline=1900, events=60, heads=5)
        else:
            value = intent(ref, STATUSES[n % 4])
        intents[_intent_key(ref, value["record"]["attempt_id"])] = value
    generated = {f"/data/workspaces/ummanu/ummanu-{n}-worker/TASK.md": hashlib.sha256(str(n).encode()).hexdigest()
                 for n in range(530)}
    return {"version": 1, "intents": intents, "generated": generated, "replay_cursor": sorted(intents)[count // 2]}


def stored_files(journal: CleanupJournal) -> dict[str, tuple[int, int, int]]:
    return {str(path.relative_to(journal.path)): (path.stat().st_ino, path.stat().st_mtime_ns, path.stat().st_size)
            for path in journal.path.rglob("*") if path.is_file()}


class LegacyFixture:
    """One 40+ MB v1 document, serialized once for the module."""

    value: dict | None = None
    body: bytes = b""

    @classmethod
    def load(cls) -> tuple[dict, bytes]:
        if cls.value is None:
            cls.value = legacy_journal()
            cls.body = json.dumps(cls.value, sort_keys=True).encode()
        return json.loads(cls.body), cls.body


class JournalTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.data = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.journal = CleanupJournal(self.data)

    def write_legacy(self, body: bytes) -> None:
        self.journal.legacy.parent.mkdir(parents=True, exist_ok=True)
        self.journal.legacy.write_bytes(body)

    def assert_lossless(self, original: dict, migrated: dict) -> None:
        self.assertEqual(set(migrated["intents"]), set(original["intents"]))
        self.assertEqual(migrated["generated"], original["generated"])
        self.assertEqual(migrated["replay_cursor"], original["replay_cursor"])
        for key, before in original["intents"].items():
            after = migrated["intents"][key]
            for field in set(before) | set(after):
                if field not in ("record", "heads"):
                    self.assertEqual(after.get(field), before.get(field), (key, field))
            # Every field a cleanup path reads survives, and every head keeps its identity and receipt.
            for field in cleanup.__dict__["_RECORD_KEPT"]:
                if field in before["record"] and field not in cleanup.__dict__["_HEAD_RUN_FIELDS"] + ("launch_intent",):
                    self.assertEqual(after["record"][field], before["record"][field], (key, field))
            for field in ("worker_head_run", "review_head_run"):
                self.assertEqual({name: after["record"][field][name] for name in HEAD_IDENTITY if name in before["record"][field]},
                                 {name: before["record"][field][name] for name in HEAD_IDENTITY if name in before["record"][field]})
            self.assertEqual(after["record"]["launch_intent"]["head_run"]["run_id"],
                             before["record"]["launch_intent"]["head_run"]["run_id"])
            expected = [{name: raw[name] for name in HEAD_IDENTITY if name in raw} for raw in _compact_heads(before["heads"])]
            self.assertEqual([{name: raw[name] for name in HEAD_IDENTITY if name in raw} for raw in after["heads"]], expected)
            for raw in after["heads"]:
                read = HeadRun.from_json(raw)
                self.assertEqual(read.to_json()["lifecycle"], raw["lifecycle"])
                self.assertNotIn("provider_source", raw["fanout_policy"])
                self.assertLessEqual(len(raw["fanout_policy"]["events"]), 8)


class PerIntentLayoutTests(JournalTestCase):
    def test_realistic_intents_each_stay_under_the_bound_and_keep_their_proof(self) -> None:
        for n in range(300):
            value = intent(f"ummanu-{n}", "owned", baseline=1900 if n < 3 else 400, events=60, heads=1)
            # The producer path: a dispatcher record with launch telemetry, as save_records hands it over.
            raw = value["record"]
            key = self.journal.remember(value["task"], raw)
            self.assertEqual(key, _intent_key(value["task"]["ref"], raw["attempt_id"]))
        files = list((self.journal.path / "intents").glob("*.json"))
        self.assertEqual(len(files), 300)
        sizes = [file.stat().st_size for file in files]
        self.assertLessEqual(max(sizes), INTENT_FILE_LIMIT)
        stored = self.journal.read()["intents"]
        for value in stored.values():
            self.assertEqual(set(value["record"]) - set(cleanup.__dict__["_RECORD_KEPT"]), set())
            for raw in value["heads"]:
                self.assertTrue(HeadRun.from_json(raw).run_id)
        print(f"300 remembered intents: max {max(sizes)} bytes, total {sum(sizes)} bytes, "
              f"{self.journal.writes['intent']} intent writes, {self.journal.writes['bytes']} bytes written")

    def test_one_mutation_reads_and_replaces_only_its_own_intent(self) -> None:
        original, body = LegacyFixture.load()
        self.write_legacy(body)
        self.assertTrue(self.journal.migrate())
        key = _intent_key("ummanu-4", "attempt-ummanu-4")
        self.assertEqual(original["intents"][key]["status"], "owned")
        before = stored_files(self.journal)
        reads: list[str] = []
        read_bytes = Path.read_bytes

        def tracked(path: Path) -> bytes:
            reads.append(str(path.relative_to(self.journal.path)) if path.is_relative_to(self.journal.path) else str(path))
            return read_bytes(path)

        writes_before = dict(self.journal.writes)
        with mock.patch.object(Path, "read_bytes", tracked), mock.patch.object(
                CleanupJournal, "read", side_effect=AssertionError("whole-journal read on a hot path")):
            fresh = self.journal.intent(key)
            fresh["status"], fresh["reason"] = "pending", "retried"
            self.journal.commit_intent(key, fresh)
            self.journal.defer_intent(key, self.journal.intent(key), "deferred again")
            self.journal.remember(fresh["task"], {**fresh["record"], "worker": "replacement-worker"})
            self.journal.set_replay_cursor(key)
            self.journal.generated(Path("/data/workspaces/ummanu/x/TASK.md"), "body")
        after = stored_files(self.journal)
        changed = {name for name in after if after[name] != before.get(name)}
        own = "intents/" + key + ".json"
        self.assertEqual(changed - {own, "meta.json"}, {next(name for name in changed if name.startswith("generated/"))})
        self.assertIn(own, changed)
        self.assertEqual({name for name in reads if name.startswith("intents/")}, {own})
        written = {kind: self.journal.writes[kind] - writes_before[kind] for kind in self.journal.writes}
        self.assertEqual((written["intent"], written["meta"], written["generated"], written["layout"]), (3, 1, 1, 0))
        self.assertLess(written["bytes"], 3 * INTENT_FILE_LIMIT)
        print(f"one-intent mutations: {written}")

    def test_oversize_is_refused_loudly_and_keeps_the_stored_obligation(self) -> None:
        value = intent("ummanu-big", "owned", heads=1)
        key = self.journal.remember(value["task"], value["record"])
        stored = (self.journal.path / "intents" / (key + ".json")).read_bytes()
        # Unread record bulk is shed rather than refused; nothing a cleanup path reads is dropped.
        bulky = self.journal.intent(key)
        bulky["record"]["retained_evidence"] = "x" * (2 * INTENT_FILE_LIMIT)
        self.journal.commit_intent(key, {**bulky, "record": self.journal.intent(key)["record"]})
        # Ownership evidence beyond the bound: thousands of distinct exact heads.
        flood = self.journal.intent(key)
        for n in range(2500):
            flood["heads"].append(head("ummanu-big", 100 + n, baseline=0, events=0))
        with self.assertRaisesRegex(HostError, "over the 1000000-byte bound; its stored obligation is unchanged"):
            self.journal.commit_intent(key, flood)
        self.assertEqual((self.journal.path / "intents" / (key + ".json")).read_bytes(), stored)
        self.assertEqual(self.journal.intent(key)["status"], "owned")

    def test_record_bulk_beyond_the_bound_is_shed_not_lost_evidence(self) -> None:
        value = intent("ummanu-bulk", "owned", heads=1)
        key = self.journal.remember(value["task"], value["record"])
        stored = self.journal.intent(key)
        bulky = copy.deepcopy(stored)
        bulky["record"]["retained_evidence"] = "x" * (2 * INTENT_FILE_LIMIT)
        self.journal.save({"intents": {key: bulky}})
        kept = self.journal.intent(key)
        self.assertNotIn("retained_evidence", kept["record"])
        self.assertEqual(kept["record"], cleanup_record(stored["record"]))
        self.assertEqual(kept["heads"], stored["heads"])

    def test_compaction_reaches_heads_and_every_record_head_run(self) -> None:
        value = intent("ummanu-3", "pending", baseline=1900, events=60, heads=5)
        legacy = {"version": 1, "intents": {"a" * 64: value}, "generated": {}}
        self.write_legacy(json.dumps(legacy).encode())
        self.journal.migrate()
        stored = self.journal.intent("a" * 64)
        runs = [*stored["heads"], stored["record"]["worker_head_run"], stored["record"]["review_head_run"],
                stored["record"]["launch_intent"]["head_run"]]
        for raw in runs:
            policy = raw["fanout_policy"]
            self.assertFalse({"provider_source", "provider_progress_source", "prompt_identity",
                              "provider_source_required"} & set(policy))
            self.assertEqual(len(policy["events"]), 8 if policy.get("event_count") else len(policy["events"]))
            self.assertEqual(HeadRun.from_json(raw).fanout_policy_state, "unknown")
        self.assertEqual(stored["record"]["launch_intent"], {"head_run": runs[-1]})
        self.assertLess(len(json.dumps(stored)), 60_000)
        self.assertGreater(len(json.dumps(value)), INTENT_FILE_LIMIT)


class MigrationTests(JournalTestCase):
    def test_v1_journal_over_40_mb_migrates_without_loss_and_once(self) -> None:
        original, body = LegacyFixture.load()
        self.assertGreaterEqual(len(body), 40_000_000)
        self.assertEqual({value["status"] for value in original["intents"].values()}, set(STATUSES))
        self.write_legacy(body)
        # Unmigrated, every reader answers from the v1 document and writes nothing.
        summary = self.journal.summary()
        # One ref per status (each answer reads the whole journal).
        refs = [f"ummanu-{n}" for n in range(8)]
        refusals = {ref: self.journal.admission_refusal(ref) for ref in refs}
        self.assertEqual(sorted(set(refusals.values()))[0], "")
        self.assertIn("has not verified", "".join(refusals.values()))
        self.assertFalse(self.journal.path.exists())

        self.assertTrue(self.journal.migrate())
        self.assertFalse(self.journal.legacy.exists())
        self.assertEqual(self.journal.archive.read_bytes(), body)
        migrated = self.journal.read()
        self.assert_lossless(original, migrated)
        sizes = [path.stat().st_size for path in self.journal.path.rglob("*.json")]
        self.assertLessEqual(max(sizes), INTENT_FILE_LIMIT)
        self.assertEqual(self.journal.summary(), summary)
        self.assertEqual({ref: self.journal.admission_refusal(ref) for ref in refusals}, refusals)
        print(f"v1 {len(body)} bytes -> {len(sizes)} files, max {max(sizes)}, total {sum(sizes)} bytes; "
              f"{self.journal.writes}")

        files = stored_files(self.journal)
        self.assertFalse(CleanupJournal(self.data).migrate())
        self.assertEqual(stored_files(self.journal), files)

    def test_crash_while_copying_resumes_from_the_untouched_v1_journal(self) -> None:
        original, body = LegacyFixture.load()
        self.write_legacy(body)
        replace = cleanup._replace_file
        calls = []

        def crash(path, data):
            calls.append(path)
            if len(calls) == 150:
                raise OSError("power lost while copying")
            return replace(path, data)

        with mock.patch.object(cleanup, "_replace_file", side_effect=crash), self.assertRaises(OSError):
            self.journal.migrate()
        self.assertFalse(self.journal.path.exists())
        self.assertEqual(self.journal.legacy.read_bytes(), body)
        self.assertEqual(self.journal.read()["intents"], original["intents"])
        self.assertTrue(CleanupJournal(self.data).migrate())
        self.assert_lossless(original, self.journal.read())
        self.assertFalse(os.path.lexists(self.journal._staging))

    def test_crash_at_publication_resumes(self) -> None:
        original, body = LegacyFixture.load()
        self.write_legacy(body)
        with mock.patch.object(cleanup.os, "rename", side_effect=OSError("crash before rename")), \
                self.assertRaises(OSError):
            self.journal.migrate()
        self.assertTrue(self.journal._staging.is_dir())
        self.assertFalse(self.journal.path.exists())
        self.assertEqual(self.journal.legacy.read_bytes(), body)
        self.assertTrue(CleanupJournal(self.data).migrate())
        self.assert_lossless(original, self.journal.read())
        self.assertEqual(self.journal.archive.read_bytes(), body)

    def test_crash_before_archiving_never_reads_the_v1_journal_again(self) -> None:
        original, body = LegacyFixture.load()
        self.write_legacy(body)
        with mock.patch.object(CleanupJournal, "_archive_legacy", side_effect=OSError("crash before archive")), \
                self.assertRaises(OSError):
            self.journal.migrate()
        self.assertTrue(self.journal.path.is_dir())
        self.assertTrue(self.journal.legacy.exists())
        key = sorted(original["intents"])[0]
        with mock.patch.object(CleanupJournal, "_read_legacy", side_effect=AssertionError("v1 read after publication")):
            # The published layout is authoritative; progress made on it is never rolled back.
            progressed = self.journal.intent(key)
            progressed.update(status="completed", reason="settled after publication")
            self.journal.commit_intent(key, progressed)
            self.assertFalse(self.journal.legacy.exists())
            self.assertEqual(self.journal.archive.read_bytes(), body)
            # A v1 file that reappears later is archived beside the first, unread.
            self.write_legacy(body)
            self.assertFalse(CleanupJournal(self.data).migrate())
            self.assertFalse(self.journal.legacy.exists())
            self.assertTrue(self.journal.archive.with_name("cleanup.v1-archive.1.json").exists())
            self.assertEqual(self.journal.intent(key)["status"], "completed")
            self.assertEqual(len(self.journal.read()["intents"]), len(original["intents"]))

    def test_unreadable_v1_journal_is_never_migrated_or_replaced(self) -> None:
        self.write_legacy(b"{not json")
        with self.assertRaisesRegex(HostError, "unreadable"):
            self.journal.migrate()
        with self.assertRaisesRegex(HostError, "unreadable"):
            self.journal.remember({"ref": "x-1", "id": 1, "project": "p"}, {"attempt_id": "a"})
        self.assertFalse(self.journal.path.exists())
        self.assertEqual(self.journal.legacy.read_bytes(), b"{not json")
        shutil.rmtree(self.journal._staging, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
