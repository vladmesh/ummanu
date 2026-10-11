"""The per-intent cleanup journal: bounded files, single-intent I/O and the one-time v1 migration."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path
from unittest import mock

from tests.support import cleanup_journal as journal_fixture
from tests.support.cleanup_journal import LegacyFixture, head, intent, stored_files
from ummanu.dispatch import cleanup
from ummanu.dispatch.cleanup import INTENT_FILE_LIMIT, CleanupJournal, _intent_key, cleanup_record
from ummanu.dispatch.tick_telemetry import tick_counting
from ummanu.dispatch.types import HostError
from ummanu.runtime.head import HeadRun


def skewed_generated(count: int, *, prefix: str = "0", start: int = 0) -> tuple[dict[str, str], int]:
    """Ordinary TASK.md paths whose hashes all share one prefix, with the next unused index."""
    generated, n = {}, start
    while len(generated) < count:
        name = f"/home/dev/ummanu-data/workspaces/ummanu/ummanu-{n}-worker/TASK.md"
        n += 1
        if hashlib.sha256(name.encode()).hexdigest().startswith(prefix):
            generated[name] = hashlib.sha256(name.encode() + b"body").hexdigest()
    return generated, n


def active_files(journal: CleanupJournal) -> dict[str, int]:
    return {str(path.relative_to(journal.path)): path.stat().st_size for path in journal.path.rglob("*.json")}


class PerIntentLayoutTests(journal_fixture.JournalTestCase):
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
        self.assertEqual((written["intent"], written["meta"], written["generated"]), (3, 1, 1))
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


    def test_compaction_never_upgrades_a_policy_a_head_run_read_refuses(self) -> None:
        raw = head("ummanu-5", 0, baseline=0, events=0)
        attested = {"version": 1, "state": "allowed", "terminal_state": "clean", "events": [], "run_id": raw["run_id"],
                    "role": raw["role"], "model": "gpt-5.6-terra", "binary_path": "/usr/bin/codex",
                    "binary_digest": "b" * 64, "cli_version": "codex 9", "tool_schema_digest": "c" * 64,
                    "provider_schema_verdict": "no_callable_child_spawn_surface"}
        self.assertTrue(HeadRun.from_json({**raw, "fanout_policy": attested}).fanout_clean)
        # Required provider evidence that is missing makes the read unknown; dropping the source must not
        # turn it back into a clean allow.
        missing = {**raw, "fanout_policy": {**attested, "provider_source_required": True}}
        self.assertFalse(HeadRun.from_json(missing).fanout_clean)
        compact = cleanup._compact_run(missing)
        self.assertFalse(HeadRun.from_json(compact).fanout_clean)
        self.assertEqual(compact["fanout_policy"]["state"], "unknown")
        self.assertEqual(cleanup._compact_run(compact), compact)


class GeneratedBoundTests(journal_fixture.JournalTestCase):
    """Generated digests are active journal files too: bounded, never truncated (ummanu-131 review 14)."""

    def seed_generated_at_bound(self) -> tuple[dict[str, str], dict[str, str]]:
        """One acknowledged addition fits; the next crosses the real serialized byte bound."""
        generated, size, after = {}, 0, 0
        while True:
            entry, after = skewed_generated(1, start=after)
            # With the journal's JSON separators, a nonempty map's size is the sum of
            # its singleton sizes. Size each entry once instead of serializing each growing map.
            entry_size = len(cleanup._generated_buckets(entry, 1)["0"])
            if size + entry_size > INTENT_FILE_LIMIT:
                name, digest = generated.popitem()
                more = {name: digest, **entry}
                break
            generated.update(entry)
            size += entry_size
        name = next(iter(more))
        first = {name: more[name]}
        self.assertLessEqual(len(cleanup._generated_buckets({**generated, **first}, 1)["0"]), INTENT_FILE_LIMIT)
        self.assertGreater(len(cleanup._generated_buckets({**generated, **more}, 1)["0"]), INTENT_FILE_LIMIT)
        self.write_legacy(json.dumps({"version": 1, "intents": {}, "generated": generated}).encode())
        self.assertTrue(self.journal.migrate())
        self.assertEqual(self.journal._generated_depth(), 1)
        return generated, more

    def assert_bounded(self) -> None:
        sizes = active_files(self.journal)
        self.assertTrue(sizes)
        self.assertLessEqual(max(sizes.values()), INTENT_FILE_LIMIT, max(sizes.items(), key=lambda item: item[1]))

    def test_skewed_v1_generated_over_a_megabyte_migrates_into_bounded_buckets(self) -> None:
        generated, after = skewed_generated(9000)
        legacy = {"version": 1, "intents": {}, "generated": generated}
        self.assertGreater(len(json.dumps(generated)), INTENT_FILE_LIMIT)
        self.write_legacy(json.dumps(legacy).encode())
        self.assertTrue(self.journal.migrate())
        self.assert_bounded()
        self.assertEqual(self.journal.generated_digests(), generated)
        self.assertEqual(self.journal._generated_depth(), 2)
        self.assertFalse((self.journal.path / "generated").exists())
        # Ordinary producers keep adding to the same prefix; every digest stays readable.
        more, _ = skewed_generated(40, start=after)
        for name, digest in more.items():
            self.journal._record_generated(name, digest)
        self.assert_bounded()
        self.assertEqual(self.journal.generated_digests(), {**generated, **more})

    def test_ordinary_growth_across_the_bound_deepens_without_losing_a_digest(self) -> None:
        generated, more = self.seed_generated_at_bound()
        bucket = self.journal.path / "generated" / "0.json"
        self.assertGreater(bucket.stat().st_size, INTENT_FILE_LIMIT - 30_000)
        replace = cleanup._replace_file
        published = []
        with tick_counting() as counters, mock.patch.object(
                cleanup, "_replace_file", side_effect=lambda path, body: (replace(path, body), published.append(len(body)))):
            for name, digest in more.items():
                self.journal._record_generated(name, digest)
        self.assertEqual(self.journal._generated_depth(), 2)
        self.assertFalse((self.journal.path / "generated").exists())
        self.assert_bounded()
        self.assertEqual(self.journal.generated_digests(), {**generated, **more})
        # Every completed replace (bucket writes, the deeper rebuild and the meta switch) and nothing else.
        self.assertEqual(counters["cleanup_bytes_written"], sum(published))
        self.assertEqual(counters["cleanup_intent_writes"], 0)

    def test_crash_while_deepening_keeps_the_published_depth_complete(self) -> None:
        generated, more = self.seed_generated_at_bound()
        replace = cleanup._replace_file
        writes = []

        def crash(path, body):
            if path.parent.name == "generated-2":
                writes.append(path)
                if len(writes) == 5:
                    raise OSError("power lost while deepening")
            return replace(path, body)

        acknowledged, crossing = more
        self.journal.generated(Path(acknowledged), acknowledged.encode() + b"body")
        written = {acknowledged: more[acknowledged]}
        before = stored_files(self.journal)
        with mock.patch.object(cleanup, "_replace_file", side_effect=crash), \
                self.assertRaisesRegex(OSError, "power lost while deepening"):
            self.journal.generated(Path(crossing), crossing.encode() + b"body")
        self.assertEqual(len(writes), 5)
        self.assertEqual([path.exists() for path in writes], [True, True, True, True, False])
        # The meta still names depth 1, whose files are untouched and complete.
        self.assertEqual(self.journal._generated_depth(), 1)
        self.assertEqual(self.journal.generated_digests(), {**generated, **written})
        after = stored_files(self.journal)
        self.assertEqual({name: after[name] for name in before}, before)
        self.assert_bounded()
        # The retried write finishes the deepening from scratch.
        self.journal.generated(Path(crossing), crossing.encode() + b"body")
        self.assertEqual(self.journal._generated_depth(), 2)
        self.assertEqual(self.journal.generated_digests(), {**generated, **more})
        self.assert_bounded()

    def test_crash_after_the_depth_switch_reads_the_new_depth(self) -> None:
        generated, more = self.seed_generated_at_bound()
        with mock.patch.object(cleanup.shutil, "rmtree", side_effect=OSError("crash before removal")), \
                self.assertRaises(OSError):
            for name, digest in more.items():
                self.journal._record_generated(name, digest)
        self.assertEqual(self.journal._generated_depth(), 2)
        self.assertTrue((self.journal.path / "generated").exists())  # inactive, unread leftover
        expected = self.journal.generated_digests()
        self.assertLessEqual(set(generated), set(expected))
        for name, digest in more.items():
            self.journal._record_generated(name, digest)
        self.assertEqual(self.journal.generated_digests(), {**generated, **more})

    def test_crash_while_staging_a_deep_generated_map_resumes(self) -> None:
        generated, _ = skewed_generated(9000)
        value = intent("ummanu-1", "pending", heads=1)
        legacy = {"version": 1, "intents": {"a" * 64: value}, "generated": generated}
        body = json.dumps(legacy).encode()
        self.write_legacy(body)
        replace = cleanup._replace_file
        calls = []

        def crash(path, data):
            calls.append(path)
            if "generated-2" in str(path) and len([c for c in calls if "generated-2" in str(c)]) == 3:
                raise OSError("power lost while staging generated")
            return replace(path, data)

        with mock.patch.object(cleanup, "_replace_file", side_effect=crash), self.assertRaises(OSError):
            self.journal.migrate()
        self.assertFalse(self.journal.path.exists())
        self.assertEqual(self.journal.legacy.read_bytes(), body)
        self.assertTrue(CleanupJournal(self.data).migrate())
        self.assertEqual(self.journal.generated_digests(), generated)
        self.assertEqual(set(self.journal.read()["intents"]), {"a" * 64})
        self.assert_bounded()
        self.assertEqual(self.journal.archive.read_bytes(), body)

    def test_a_digest_that_cannot_fit_is_refused_before_publication(self) -> None:
        self.journal.generated(Path("/data/workspaces/ummanu/x/TASK.md"), "body")
        before = stored_files(self.journal)
        with self.assertRaisesRegex(HostError, "nothing was published"):
            self.journal.generated(Path("/data/" + "x" * INTENT_FILE_LIMIT), "body")
        self.assertEqual(stored_files(self.journal), before)
        self.assertEqual(list(self.journal.generated_digests()), ["/data/workspaces/ummanu/x/TASK.md"])

    def test_the_publication_seam_checks_the_bound_before_every_replace(self) -> None:
        with mock.patch.object(cleanup, "_replace_file") as replace, \
                self.assertRaisesRegex(HostError, "over the 1000000-byte bound; nothing was published"):
            self.journal._replace(self.journal.path / "intents" / ("a" * 64 + ".json"), b"x" * (INTENT_FILE_LIMIT + 1))
        replace.assert_not_called()
        self.assertEqual(self.journal.writes, {"intent": 0, "meta": 0, "generated": 0, "bytes": 0})


class PublicationCountTests(journal_fixture.JournalTestCase):
    """Tick counters equal the files the journal actually published (ummanu-131 review 14)."""

    def test_migrated_intents_are_counted_as_intent_writes(self) -> None:
        intents = {}
        for n in range(3):
            value = intent(f"ummanu-{n}", "owned", baseline=0, events=0, heads=1)
            intents[_intent_key(value["task"]["ref"], value["record"]["attempt_id"])] = value
        generated, _ = skewed_generated(5)
        self.write_legacy(json.dumps({"version": 1, "intents": intents, "generated": generated}).encode())
        with tick_counting() as counters:
            self.journal.migrate()
        files = active_files(self.journal)
        self.assertEqual(counters["cleanup_intent_writes"], len(list((self.journal.path / "intents").glob("*.json"))))
        self.assertEqual(counters["cleanup_intent_writes"], 3)
        self.assertEqual(counters["cleanup_bytes_written"], sum(files.values()))
        self.assertEqual(self.journal.writes["intent"], 3)

    def test_a_failed_replace_is_not_counted_and_its_retry_is(self) -> None:
        value = intent("ummanu-1", "owned", heads=1)
        key = self.journal.remember(value["task"], value["record"])
        changed = self.journal.intent(key)
        changed["reason"] = "retried"
        replace = cleanup._replace_file
        with tick_counting() as counters:
            with mock.patch.object(cleanup, "_replace_file", side_effect=OSError("disk full")), \
                    self.assertRaises(OSError):
                self.journal.commit_intent(key, copy.deepcopy(changed))
            self.assertEqual(counters["cleanup_intent_writes"], 0)
            self.assertEqual(counters["cleanup_bytes_written"], 0)
            with mock.patch.object(cleanup, "_replace_file", side_effect=replace):
                self.journal.commit_intent(key, copy.deepcopy(changed))
        size = (self.journal.path / "intents" / (key + ".json")).stat().st_size
        self.assertEqual(counters["cleanup_intent_writes"], 1)
        self.assertEqual(counters["cleanup_bytes_written"], size)

    def test_outside_a_tick_publications_count_nothing(self) -> None:
        value = intent("ummanu-1", "owned", heads=1)
        self.journal.remember(value["task"], value["record"])
        with tick_counting() as counters:
            pass
        self.assertEqual(counters["cleanup_intent_writes"], 0)
        self.assertEqual(self.journal.writes["intent"], 1)


if __name__ == "__main__":
    unittest.main()
