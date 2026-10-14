"""secretary-1703: a card's local-pty heads on the web, read-only, without Orca.

Hermetic: the run directories are written here the way a supervisor writes them, the card's history
is a fake audit owner, and the board is not consulted. What a live supervisor answers is
`test_web_head_view_supervised`'s; what this file holds is everything a run directory alone decides
-- which runs are the card's, what a finished head shows, what is never shown, and that a source in
any state is a page rather than a 500.
"""

from __future__ import annotations

import json
import os
import subprocess
import unittest
from typing import Any
from unittest import mock

from tests.web_head_view_fixtures import (
    FINISHED_EARLIER,
    FOREIGN,
    LEGACY,
    OTHER,
    REF,
    SECRET,
    TOKEN,
    WORKER,
    HeadViewFixture,
    _routing,
)
from ummanu.runtime.head_runtimes import ORCA_LEGACY_RUNTIME
from ummanu.web.app import ROUTES
from ummanu.webproto import head_view as head_reads


class TheCardPageListsItsHeadsTests(HeadViewFixture):
    def test_every_recorded_head_is_a_row_and_only_local_pty_ones_link_to_a_view(self) -> None:
        self.run_dir(WORKER, tail=b"hello\n")
        self.run_dir(FINISHED_EARLIER, tail=b"before\n")
        status, page = self.get(f"/tasks/{REF}")
        self.assertEqual(status, 200)
        self.assertIn(f'href="/tasks/{REF}/heads/{WORKER}"', page)
        self.assertIn(f'href="/tasks/{REF}/heads/{FINISHED_EARLIER}"', page)
        self.assertNotIn(f"/heads/{LEGACY}", page, "a legacy head has no view to link to")
        self.assertIn(LEGACY, page)
        self.assertIn(head_reads.LEGACY_NOTICE, page)
        self.assertNotIn(FOREIGN, page, "another card's run is not this card's head")

    def test_a_row_carries_role_run_id_and_state(self) -> None:
        self.run_dir(WORKER)
        rows = {row["run_id"]: row for row in self.layer().task_snapshot(REF)["heads"]["items"]}
        self.assertEqual(set(rows), {WORKER, LEGACY, FINISHED_EARLIER})
        worker = rows[WORKER]
        self.assertEqual((worker["role"], worker["state"], worker["local_pty"]), ("worker", "finished", True))
        self.assertTrue(worker["current"])
        legacy = rows[LEGACY]
        self.assertEqual((legacy["role"], legacy["runtime"], legacy["local_pty"]), ("reviewer", ORCA_LEGACY_RUNTIME, False))
        self.assertEqual(legacy["reason"], head_reads.LEGACY_NOTICE)
        # Named only by the history and with no run directory: a head no supervisor ever held.
        self.assertEqual(rows[FINISHED_EARLIER]["reason"], head_reads.LEGACY_NOTICE)
        self.assertFalse(rows[FINISHED_EARLIER]["local_pty"])

    def test_a_held_supervisor_lock_reads_as_running(self) -> None:
        import fcntl

        directory = self.run_dir(WORKER)
        fd = os.open(directory / "supervisor.lock", os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        rows = {row["run_id"]: row for row in self.layer().task_snapshot(REF)["heads"]["items"]}
        self.assertEqual(rows[WORKER]["state"], head_reads.RUNNING)


class AFinishedHeadTests(HeadViewFixture):
    def test_a_finished_head_shows_its_journal_without_terminal_output(self) -> None:
        self.run_dir(WORKER, tail=b"printed only to the terminal\n")
        status, page = self.get(f"/tasks/{REF}/heads/{WORKER}")
        self.assertEqual(status, 200)
        self.assertIn("turn.finished", page)
        self.assertIn("input.accepted", page)
        self.assertNotIn("printed only to the terminal", page)
        self.assertNotIn("Terminal output", page)

    def test_the_json_twin_has_the_journal_and_no_transcript(self) -> None:
        self.run_dir(WORKER, tail=b"plain\n")
        response = self.app().handle("GET", f"/api/tasks/{REF}/heads/{WORKER}")
        self.assertEqual(response.status, 200)
        document = json.loads(response.body)
        self.assertEqual(document["kind"], "head_view")
        self.assertNotIn("transcript", document)
        self.assertEqual({key for record in document["journal"]["tail"] for key in record} - set(head_reads.JOURNAL_KEYS), set())

    def test_a_legacy_head_has_no_local_pty_journal(self) -> None:
        status, page = self.get(f"/tasks/{REF}/heads/{LEGACY}")
        self.assertEqual(status, 200)
        self.assertIn(head_reads.LEGACY_NOTICE, page)
        self.assertNotIn("not answering", page)


class NothingSecretReachesThePageTests(HeadViewFixture):
    def test_a_planted_memory_token_is_on_neither_the_page_nor_the_document(self) -> None:
        tail = (
            f"export UMMANU_MEMORY_ACCESS_TOKEN={TOKEN}\r\n"
            f"the token is {TOKEN} ok\r\n"
        ).encode()
        directory = self.run_dir(WORKER, tail=tail, subject=f"deliver {TOKEN}")
        # The plant is really there, in both places, before the page is asked for.
        self.assertIn(TOKEN, (directory / "journal.jsonl").read_text())
        self.assertIn(TOKEN.encode(), (directory / "output.tail").read_bytes())
        for path in (f"/tasks/{REF}/heads/{WORKER}", f"/api/tasks/{REF}/heads/{WORKER}", f"/tasks/{REF}"):
            with self.subTest(path=path):
                status, body = self.get(path)
                self.assertEqual(status, 200)
                self.assertNotIn(TOKEN, body)
                self.assertNotIn(SECRET, body)
                self.assertNotIn("--dangerously", body, "run.started's command is never shown")



class OnlyTheCardsOwnRunsTests(HeadViewFixture):
    def test_another_card_s_run_is_not_found(self) -> None:
        self.run_dir(FOREIGN, ref=OTHER, tail=b"not yours\n")
        for path in (f"/tasks/{REF}/heads/{FOREIGN}", f"/api/tasks/{REF}/heads/{FOREIGN}"):
            with self.subTest(path=path):
                status, body = self.get(path)
                self.assertEqual(status, 404)
                self.assertNotIn("not yours", body)

    def test_a_random_run_id_is_not_found(self) -> None:
        self.assertEqual(self.get(f"/tasks/{REF}/heads/{'f' * 32}")[0], 404)

    def test_path_traversal_in_the_run_id_is_not_found(self) -> None:
        (self.data_dir / "secret.txt").write_text("outside the heads root")
        for run_id in ("..", "..%2F..%2Fsecret.txt", "..%2Fdispatcher", "%2Fetc%2Fpasswd", f"{WORKER}%2F..%2F.."):
            for prefix in ("/tasks", "/api/tasks"):
                with self.subTest(prefix=prefix, run_id=run_id):
                    status, body = self.get(f"{prefix}/{REF}/heads/{run_id}")
                    self.assertEqual(status, 404)
                    self.assertNotIn("outside the heads root", body)

    def test_a_card_the_board_does_not_hold_is_not_found(self) -> None:
        self.assertEqual(self.get(f"/tasks/ummanu-404/heads/{WORKER}")[0], 404)

    def test_a_recorded_run_whose_directory_names_another_card_is_not_read(self) -> None:
        self.run_dir(WORKER, ref=OTHER, tail=b"somebody else's\n")
        status, page = self.get(f"/tasks/{REF}/heads/{WORKER}")
        self.assertEqual(status, 200)
        self.assertNotIn("somebody else", page)
        self.assertIn("belongs to another card", page)

    def test_the_view_is_read_only(self) -> None:
        views = [route for route in ROUTES if route.operation == "reads.head_view"]
        self.assertEqual({(route.method, route.pattern) for route in views}, {
            ("GET", "/tasks/{ref}/heads/{run_id}"),
            ("GET", "/api/tasks/{ref}/heads/{run_id}"),
        })
        self.run_dir(WORKER)
        response = self.app().handle("POST", f"/tasks/{REF}/heads/{WORKER}", headers={})
        self.assertEqual(response.status, 405)


class ASourceInAnyStateIsAPageTests(HeadViewFixture):
    def test_a_damaged_journal_is_said_to_be_degraded(self) -> None:
        directory = self.run_dir(WORKER)
        with open(directory / "journal.jsonl", "ab") as journal:
            journal.write(b"{not json\n\xff\xfe torn")
        status, page = self.get(f"/tasks/{REF}/heads/{WORKER}")
        self.assertEqual(status, 200)
        self.assertIn("the journal answered in part", page)

    def test_a_journal_that_is_not_a_file_and_a_lock_that_is_garbage_are_still_a_page(self) -> None:
        directory = self.root / WORKER
        directory.mkdir()
        (directory / "journal.jsonl").mkdir()
        (directory / "supervisor.lock").write_bytes(b"\xff\xfe")
        for path in (f"/tasks/{REF}", f"/tasks/{REF}/heads/{WORKER}", f"/api/tasks/{REF}/heads/{WORKER}"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path)[0], 200)

    def test_every_reader_raising_is_still_a_page(self) -> None:
        self.run_dir(WORKER)
        for name in ("head_run_first_record", "head_run_supervisor_lease", "head_run_journal_tail"):
            with self.subTest(reader=name), mock.patch.object(head_reads, name, side_effect=RuntimeError("boom")):
                for path in (f"/tasks/{REF}", f"/tasks/{REF}/heads/{WORKER}"):
                    status, _body = self.get(path)
                    self.assertEqual(status, 200, f"{path} with {name} raising")


def _routing_of(run_id: str) -> dict[str, Any]:
    return _routing(REF, (run_id, "worker", "claude-local-pty"))


class HostileJournalValuesTests(HeadViewFixture):
    """Rule B: no value a journal line can hold makes the page or the JSON route fail."""

    HUGE = 10**400
    CASES = (
        ("at", 1e300),
        ("at", -1),
        ("at", 0),
        ("at", True),
        ("at", "x"),
        ("at", []),
        ("at", HUGE),
        ("seq", HUGE),
        # A digit string is read as its int by the substrate's own journal reader, so the hostile
        # string is one that is not a number at all.
        ("seq", "seven"),
        ("turn", HUGE),
        ("turn", "1"),
        ("bytes", HUGE),
        ("bytes", "12"),
        ("output_bytes", HUGE),
        ("folded_windows", "12"),
        ("kind", 5),
        ("kind", {"nested": True}),
        ("subject", ["a", "list"]),
        ("subject", 3.5),
    )

    def test_each_hostile_value_is_a_page_and_a_document_with_the_journal_degraded(self) -> None:
        for key, value in self.CASES:
            with self.subTest(key=key, value=repr(value)[:40]):
                run_id = f"{abs(hash((key, repr(value)))):032x}"[:32]
                self.history[REF].append(_routing_of(run_id))
                directory = self.run_dir(run_id, tail=b"fine\n")
                record = {
                    "schema_version": 1,
                    "run_id": run_id,
                    "seq": 5,
                    "kind": "turn.finished",
                    "at": 1_790_000_000.0,
                    "turn": 1,
                    "reason": "quiet",
                    key: value,
                }
                with open(directory / "journal.jsonl", "a", encoding="utf-8") as journal:
                    journal.write(json.dumps(record) + "\n")
                status, page = self.get(f"/tasks/{REF}/heads/{run_id}")
                self.assertEqual(status, 200)
                self.assertNotIn("could not be shown", page, "the layer, not the backstop, handled it")
                response = self.app().handle("GET", f"/api/tasks/{REF}/heads/{run_id}")
                self.assertEqual(response.status, 200)
                journal_section = json.loads(response.body)["journal"]
                self.assertEqual(journal_section["state"], "degraded")
                for shown in journal_section["tail"]:
                    self.assertTrue(set(shown) <= set(head_reads.JOURNAL_KEYS))
                    if "at" in shown:
                        self.assertTrue(0 < shown["at"] < 253402300800)

    def test_the_normaliser_is_total(self) -> None:
        for key, value in self.CASES:
            with self.subTest(key=key, value=repr(value)[:40]):
                record, lost = head_reads.journal_record({"seq": 1, "kind": "run.started", key: value})
                self.assertNotIn(key, record)
                self.assertEqual(lost, 1)
        self.assertEqual(head_reads.journal_record(["not", "a", "record"]), ({}, 0))

    def test_a_section_that_cannot_be_drawn_says_so_and_the_page_is_served(self) -> None:
        from ummanu.web import page_card

        self.run_dir(WORKER, tail=b"x\n")
        for section in ("_head_header", "_head_journal"):
            with self.subTest(section=section), mock.patch.object(page_card, section, side_effect=RuntimeError("boom")) as failed:
                status, page = self.get(f"/tasks/{REF}/heads/{WORKER}")
                failed.assert_called_once()
                self.assertEqual(status, 200)
                self.assertIn("this section could not be shown (RuntimeError)", page)


class NoOrcaOnThesePathsTests(HeadViewFixture):
    def test_nothing_on_the_card_page_or_the_head_view_runs_a_process(self) -> None:
        self.run_dir(WORKER, tail=b"x\n")
        (self.root / WORKER / "head.sock").write_text("")
        calls: list[Any] = []

        def record(*args: Any, **kwargs: Any) -> Any:
            calls.append(args[0] if args else kwargs.get("args"))
            raise AssertionError(f"a process was started: {args!r}")

        with (
            mock.patch.object(subprocess, "Popen", side_effect=record),
            mock.patch.object(subprocess, "run", side_effect=record),
            mock.patch.object(os, "execvp", side_effect=record),
            mock.patch.object(os, "execvpe", side_effect=record),
            mock.patch.object(os, "posix_spawn", side_effect=record),
            mock.patch.object(os, "posix_spawnp", side_effect=record),
            mock.patch.object(os, "system", side_effect=record),
        ):
            for path in (
                f"/tasks/{REF}",
                f"/api/tasks/{REF}",
                f"/tasks/{REF}/heads/{WORKER}",
                f"/api/tasks/{REF}/heads/{WORKER}",
                f"/tasks/{REF}/heads/{LEGACY}",
            ):
                with self.subTest(path=path):
                    self.assertEqual(self.get(path)[0], 200)
        self.assertEqual([call for call in calls if "orca" in json.dumps(call, default=str)], [])
        self.assertEqual(calls, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
