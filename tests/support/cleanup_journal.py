"""Shared released-v1 journal fixture and lossless migration assertions."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from ummanu.dispatch import cleanup
from ummanu.dispatch.cleanup import CleanupJournal, _compact_heads, _intent_key
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
