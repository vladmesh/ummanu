"""Addressable DoD items on a real sprint store: weights, evidence, rewording, descoping, history and the
close guard."""

from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

from tests.fakes.sprints import KEEP_THE_ISSUE_OPEN, SprintFixture
from ummanu.board import sprint_dod
from ummanu.cli import main
from ummanu.tasks import TaskError, TaskReader, TaskWriter


class SprintDodTests(SprintFixture):
    def _open(self, *items: str) -> str:
        return self._create(goal="addressable done", dod_items=list(items))["sprint"]["ref"]

    def _history(self, reference: str, kind: str) -> list[dict]:
        return [event["payload"] for event in self._events() if event["ref"] == reference and event["kind"] == kind]

    def test_items_open_unweighed_and_take_weights_evidence_and_re_estimates(self) -> None:
        ref = self._open("the adapter is rewritten", "the owner accepts it live")
        dod = self.sprint(ref)["dod"]
        self.assertEqual([(item["id"], item["state"], item["weight"]) for item in dod["items"]],
                         [("d1", "open", None), ("d2", "open", None)])
        self.assertFalse(dod["estimated"])

        self.writer.set_dod_weight(role="observer", actor="observer", reference=ref, item="d1", weight=8,
                                   reason="most of the code", request_id="w1")
        self.writer.set_dod_weight(role="observer", actor="observer", reference=ref, item="d2", weight=2,
                                   reason="one session", request_id="w2")
        self.writer.set_dod_weight(role="observer", actor="observer", reference=ref, item="d1", weight=5,
                                   reason="half of it already exists", request_id="w3")
        self.writer.meet_dod_item(role="observer", actor="observer", reference=ref, item="d2",
                                  evidence="sprint comment of the owner", request_id="m1")

        dod = self.sprint(ref)["dod"]
        self.assertTrue(dod["estimated"])
        self.assertAlmostEqual(dod["progress"], 2 / 7)
        self.assertEqual(dod["items"][1]["met_by"]["role"], "observer")
        self.assertEqual([entry["weight"] for entry in self._history(ref, "dod_item_weighed")], [8, 2, 5])
        self.assertEqual(self._history(ref, "dod_item_weighed")[-1]["reason"], "half of it already exists")

    def test_a_repeated_request_writes_once_and_a_different_one_under_its_id_is_refused(self) -> None:
        ref = self._open("a fact")
        first = self.writer.meet_dod_item(role="po", actor="operator", reference=ref, item="d1",
                                          evidence="PR 7", request_id="meet")
        again = self.writer.meet_dod_item(role="po", actor="operator", reference=ref, item="d1",
                                          evidence="PR 7", request_id="meet")

        self.assertEqual(first["event_id"], again["event_id"])
        with self.assertRaises(TaskError) as refused:
            self.writer.meet_dod_item(role="po", actor="operator", reference=ref, item="d1",
                                      evidence="PR 8", request_id="meet")
        self.assertEqual(refused.exception.code, "validation")

    def test_manual_main_evidence_commits_once_and_does_not_release_the_close_guard(self):
        ref = self._open("ship", "accept")
        fields = {"role": "observer", "actor": "observer", "reference": ref, "item": "d1",
                  "evidence": "main checked by operator", "request_id": "main-proof"}
        first = self.writer.mark_dod_in_main(**fields)
        self.assertEqual(self.writer.mark_dod_in_main(**fields)["event_id"], first["event_id"])
        self.assertEqual(self._history(ref, "dod_item_in_main"),
                         [{"item": "d1", "evidence": "main checked by operator"}])
        dod = self.sprint(ref)["dod"]
        self.assertEqual((dod["in_main"], dod["met"]), (1, 0))
        self.assertEqual(dod["items"][0]["delivery"]["by"]["actor"], "observer")
        with self.assertRaises(TaskError):
            self.writer.mark_dod_in_main(**{**fields, "evidence": "other"})
        with self.assertRaises(TaskError) as refused:
            self.writer.close(role="observer", actor="observer", reference=ref, decisions=KEEP_THE_ISSUE_OPEN,
                              reason="delivery only")
        self.assertEqual(refused.exception.code, "close_plan_forbidden")
        with mock.patch.object(self.client, "_rpc_saveTaskMetadata", side_effect=OSError("lost write")):
            with self.assertRaises(TaskError):
                self.writer.mark_dod_in_main(**{**fields, "item": "d2", "request_id": "lost-main"})
        self.assertIsNone(self.writer.audit.committed_event("lost-main"))
        self.assertEqual(self.sprint(ref)["dod"]["in_main"], 1)

    def test_a_free_text_dod_takes_no_weight_until_the_po_adds_items(self) -> None:
        ref = self._open()
        self.assertFalse(self.sprint(ref)["dod"]["structured"])
        with self.assertRaises(TaskError) as refused:
            self.writer.set_dod_weight(role="observer", actor="observer", reference=ref, item="d1", weight=3,
                                       reason="guess")
        self.assertEqual(refused.exception.code, "validation")
        with self.assertRaises(TaskError):
            self.writer.add_dod_items(role="observer", actor="observer", reference=ref, texts=["x"], reason="r")

        self.writer.add_dod_items(role="po", actor="operator", reference=ref, texts=["the export restores"],
                                  reason="the owner made the DoD addressable")

        self.assertEqual([item["id"] for item in self.sprint(ref)["dod"]["items"]], ["d1"])

    def test_the_observer_cannot_close_with_an_unmet_item(self) -> None:
        ref = self._open("first", "second")
        self.writer.meet_dod_item(role="observer", actor="observer", reference=ref, item="d1", evidence="PR 1")

        with self.assertRaises(TaskError) as refused:
            self.writer.close(role="observer", actor="observer", reference=ref, decisions=KEEP_THE_ISSUE_OPEN,
                              reason="looks done")

        self.assertEqual(refused.exception.code, "close_plan_forbidden")
        self.assertIn("d2", refused.exception.message)
        self.assertEqual(self.sprint(ref)["status"], "open")
        self.writer.meet_dod_item(role="observer", actor="observer", reference=ref, item="d2", evidence="PR 2")
        self.writer.close(role="observer", actor="observer", reference=ref, decisions=KEEP_THE_ISSUE_OPEN,
                          reason="every item is met")
        self.assertEqual(self.sprint(ref)["status"], "closed")

    def test_an_owner_close_files_each_unmet_item_as_an_issue_with_its_weight(self) -> None:
        ref = self._open("landed", "the owner accepts it live")
        self.writer.set_dod_weight(role="po", actor="operator", reference=ref, item="d2", weight=5,
                                   reason="a live session")
        self.writer.meet_dod_item(role="po", actor="operator", reference=ref, item="d1", evidence="PR 1")

        self.writer.close(role="po", actor="operator", reference=ref, decisions=KEEP_THE_ISSUE_OPEN,
                          reason="the owner stops here", request_id="owner-close")
        self.writer.close(role="po", actor="operator", reference=ref, decisions=KEEP_THE_ISSUE_OPEN,
                          reason="the owner stops here", request_id="owner-close")

        filed = [
            issue for issue in self.product_issue_store().list_issues(product="ummanu")
            if "the owner accepts it live" in issue["title"]
        ]
        self.assertEqual(len(filed), 1)
        self.assertIn(sprint_dod.OWNER_CLOSE_REASON, filed[0]["description"])
        self.assertIn(ref, filed[0]["description"])
        self.assertIn("weight 5", filed[0]["description"])
        self.assertFalse(any("landed" in issue["title"] and ref in issue["title"]
                             for issue in self.product_issue_store().list_issues(product="ummanu")))
        self.assertEqual(self.sprint(ref)["status"], "closed")


class SprintDodEditAndDescopeTests(SprintFixture):
    def _open(self, *items: str) -> str:
        return self._create(goal="addressable done", dod_items=list(items))["sprint"]["ref"]

    def _history(self, reference: str, kind: str) -> list[dict]:
        return [event["payload"] for event in self._events() if event["ref"] == reference and event["kind"] == kind]

    def _cli(self, *argv: str) -> int:
        with self.board_injected(), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return main(["sprint", "dod", *argv, "--data-dir", self.tmp.name, "--instance", str(self.instance)])

    def _refused(self, write, code: str = "validation") -> None:
        with self.assertRaises(TaskError) as refused:
            write()
        self.assertEqual(refused.exception.code, code)

    def test_the_po_rewords_an_item_from_the_cli_and_its_id_weight_state_and_evidence_stay(self) -> None:
        ref = self._open("the adapter is rewritten", "the owner accepts it live")
        self.writer.set_dod_weight(role="observer", actor="observer", reference=ref, item="d1", weight=5,
                                   reason="most of the code")
        self.writer.meet_dod_item(role="observer", actor="observer", reference=ref, item="d1", evidence="PR 9")
        before = self.sprint(ref)["dod"]["items"]
        argv = ("edit", "--ref", ref, "--role", "po", "--actor", "operator", "--item", "d1",
                "--text", "the adapter is rewritten and deployed", "--reason", "the owner sharpened it",
                "--request-id", "reword")

        self.assertEqual(self._cli(*argv), 0)
        self.assertEqual(self._cli(*argv), 0)

        after = self.sprint(ref)["dod"]["items"]
        self.assertEqual(after, [{**before[0], "text": "the adapter is rewritten and deployed"}, before[1]])
        self.assertEqual(self._history(ref, "dod_item_edited"), [{
            "item": "d1", "text": "the adapter is rewritten and deployed",
            "previous_text": "the adapter is rewritten", "reason": "the owner sharpened it",
        }])

    def test_a_descoped_item_leaves_the_progress_and_the_observer_closes_with_the_rest_met(self) -> None:
        ref = self._open("first", "second", "third")
        self.writer.meet_dod_item(role="observer", actor="observer", reference=ref, item="d1", evidence="PR 1")
        self.writer.meet_dod_item(role="observer", actor="observer", reference=ref, item="d3", evidence="PR 3")
        self.assertAlmostEqual(self.sprint(ref)["dod"]["progress"], 2 / 3)

        self.writer.descope_dod_item(role="po", actor="operator", reference=ref, item="d2",
                                     reason="the owner moved it to the next sprint", request_id="descope")

        dod = self.sprint(ref)["dod"]
        self.assertEqual((dod["progress"], dod["met"], dod["total"]), (1.0, 2, 2))
        descoped = dod["items"][1]
        self.assertEqual(descoped["state"], sprint_dod.DESCOPED)
        self.assertEqual((descoped["descoped"]["role"], descoped["descoped"]["actor"], descoped["descoped"]["reason"]),
                         ("po", "operator", "the owner moved it to the next sprint"))
        self.assertTrue(descoped["descoped"]["at"])
        self.assertEqual(self._history(ref, "dod_item_descoped"),
                         [{"item": "d2", "reason": "the owner moved it to the next sprint"}])
        self.writer.close(role="observer", actor="observer", reference=ref, decisions=KEEP_THE_ISSUE_OPEN,
                          reason="every item in scope is met")
        self.assertEqual(self.sprint(ref)["status"], "closed")

    def test_the_observer_neither_rewords_nor_descopes(self) -> None:
        ref = self._open("a fact")
        with self.assertRaises(TaskError):
            self.writer.edit_dod_item(role="observer", actor="observer", reference=ref, item="d1", text="b",
                                      reason="clearer")
        with self.assertRaises(TaskError):
            self.writer.descope_dod_item(role="observer", actor="observer", reference=ref, item="d1",
                                         reason="not needed")
        self.assertEqual(self.sprint(ref)["dod"]["items"][0]["text"], "a fact")
        self.assertEqual(self.sprint(ref)["dod"]["items"][0]["state"], sprint_dod.OPEN)

    def test_a_met_or_descoped_item_an_unknown_one_and_blank_text_or_reason_are_refused(self) -> None:
        ref = self._open("met", "dropped", "open")
        self.writer.meet_dod_item(role="po", actor="operator", reference=ref, item="d1", evidence="PR 1")
        self.writer.descope_dod_item(role="po", actor="operator", reference=ref, item="d2", reason="dropped")
        before = self.sprint(ref)["dod"]["items"]

        po = {"role": "po", "actor": "operator", "reference": ref}
        refusals = {
            "descope a met item": lambda: self.writer.descope_dod_item(**po, item="d1", reason="late"),
            "descope twice": lambda: self.writer.descope_dod_item(**po, item="d2", reason="again"),
            "meet a descoped item": lambda: self.writer.meet_dod_item(**po, item="d2", evidence="PR 2"),
            "reopen a descoped item": lambda: self.writer.reopen_dod_item(**po, item="d2", reason="back"),
            "weigh a descoped item": lambda: self.writer.set_dod_weight(**po, item="d2", weight=3, reason="late"),
            "reword a descoped item": lambda: self.writer.edit_dod_item(**po, item="d2", text="new", reason="r"),
            "reword an unknown item": lambda: self.writer.edit_dod_item(**po, item="d9", text="new", reason="r"),
            "blank text": lambda: self.writer.edit_dod_item(**po, item="d3", text="  ", reason="r"),
            "blank edit reason": lambda: self.writer.edit_dod_item(**po, item="d3", text="new", reason=" "),
            "blank descope reason": lambda: self.writer.descope_dod_item(**po, item="d3", reason=" "),
        }
        for name, write in refusals.items():
            with self.subTest(name):
                self._refused(write)

        self.assertEqual(self.sprint(ref)["dod"]["items"], before)
        self.assertEqual(self._history(ref, "dod_item_edited"), [])
        self.assertEqual(len(self._history(ref, "dod_item_descoped")), 1)

    def test_a_closed_sprint_takes_no_rewording_and_no_descope(self) -> None:
        ref = self._open("landed", "the owner accepts it live")
        self.writer.close(role="po", actor="operator", reference=ref, decisions=KEEP_THE_ISSUE_OPEN,
                          reason="the owner stops here")
        before = self.sprint(ref)["dod"]["items"]

        self._refused(lambda: self.writer.edit_dod_item(role="po", actor="operator", reference=ref, item="d1",
                                                        text="new", reason="r"), "closed")
        self._refused(lambda: self.writer.descope_dod_item(role="po", actor="operator", reference=ref, item="d2",
                                                           reason="r"), "closed")

        self.assertEqual(self.sprint(ref)["dod"]["items"], before)
        self.assertEqual(self._history(ref, "dod_item_edited") + self._history(ref, "dod_item_descoped"), [])


class CardDodStorageTests(SprintFixture):
    def setUp(self):
        super().setUp()
        self.ref = self._create(goal="ship the change", dod_items=["shipped", "accepted"])["sprint"]["ref"]
        self.cards = TaskWriter(self.client, data_dir=self.tmp.name)

    def create_card(self, **fields):
        return self.cards.create(**{
            "role": "observer", "actor": "observer", "project": "ummanu", "task_type": "code",
            "title": "Ship the change", "sprint": self.ref, "dod_item": "d1", **fields,
        })

    def test_card_and_link_commit_together_and_reader_export_preserves_the_link(self):
        card = self.create_card(request_id="create-card")["task"]
        fresh_reader = TaskReader(self.client)
        self.assertEqual(fresh_reader.show(card["ref"])["dod_item"], "d1")
        self.assertEqual(next(row for row in fresh_reader.list(sprint=self.ref)
                              if row["ref"] == card["ref"])["dod_item"], "d1")
        exported = next(row for row in fresh_reader.export() if row["reference"] == card["ref"])
        self.assertEqual(exported["metadata"]["dod_item"], "d1")
        self.assertTrue(self.create_card(request_id="create-card")["replayed"])
        with self.assertRaises(TaskError):
            self.create_card(request_id="create-card", dod_item="d2")
        self.assertIsNone(fresh_reader.show("ummanu-12")["dod_item"])

    def test_release_merge_facts_count_all_cards_and_invalidate_an_earlier_manual_marker(self):
        self.writer.mark_dod_in_main(role="observer", actor="observer", reference=self.ref,
                                     item="d1", evidence="operator proof")
        refs = [self.create_card()["task"]["ref"] for _ in range(2)]
        fields = {"role": "observer", "actor": "observer", "reference": self.ref,
                  "item": "d1", "evidence": "bypass", "request_id": "refused-main"}
        with self.assertRaises(TaskError):
            self.writer.mark_dod_in_main(**fields)
        self.assertEqual(self.sprint(self.ref)["dod"]["in_main"], 0)
        for index, ref in enumerate(refs):
            for target in ("in_progress", "validate", "assessment"):
                self.cards.move(role="dispatcher", actor="dispatcher", reference=ref, target=target, reason=target)
            self.cards.decide(role="observer", actor="observer", reference=ref, kind="accept", body="reviewed")
            self.cards.move(role="dispatcher", actor="dispatcher", reference=ref, target="done", reason="merged",
                            decision="accept", release_merge={"base": "main", "merge_sha": "a" * 40, "path": "pr"})
            self.assertEqual(self.sprint(self.ref)["dod"]["in_main"], index)
        self.cards.archive(role="po", actor="operator", reference=refs[0], reason="retained merge")
        self.assertEqual(self.sprint(self.ref)["dod"]["in_main"], 1)
        self.create_card()
        self.assertEqual(self.sprint(self.ref)["dod"]["in_main"], 0)

    def test_metadata_failure_rolls_back_the_card_and_its_request(self):
        before = self.persisted_record_count(self.client)
        with (mock.patch.object(self.client, "_rpc_saveTaskMetadata", side_effect=OSError("lost metadata")),
              self.assertRaises(TaskError)):
            self.create_card(request_id="failed-create")
        self.assertEqual(self.persisted_record_count(self.client), before)
        self.assertIsNone(self.cards.audit.committed_event("failed-create"))
        self.assertIsNone(self.cards.audit.pending_event("failed-create"))

    def test_bootstrap_binding_is_atomic_when_audit_publication_fails(self):
        self.client.save_metadata(12, sprint_ref=self.ref)
        self.assertIsNone(self.cards.reader.show("ummanu-12")["dod_item"])
        fields = {"role": "observer", "actor": "observer", "reference": "ummanu-12",
                  "sprint": self.ref, "dod_item": "d1", "request_id": "bind-bootstrap"}
        with (mock.patch.object(self.cards.audit, "append", side_effect=OSError("lost audit")),
              self.assertRaises(TaskError)):
            self.cards.bind_dod(**fields)
        self.assertIsNone(self.cards.reader.show("ummanu-12")["dod_item"])
        self.assertIsNone(self.cards.audit.pending_event("bind-bootstrap"))
        self.cards.bind_dod(**fields)
        self.assertTrue(self.cards.bind_dod(**fields)["replayed"])
        self.assertEqual(self.cards.reader.show("ummanu-12")["dod_item"], "d1")

    def test_edit_report_rework_and_release_preserve_the_link(self):
        card = self.create_card()["task"]["ref"]
        self.cards.edit(role="observer", actor="observer", reference=card, title="Revised change")
        self.cards.move(role="dispatcher", actor="dispatcher", reference=card, target="in_progress", reason="claimed")
        self.cards.report(role="worker", actor="worker", reference=card, kind="blocked", body="A fixture blocker",
                          classification="external_fact")
        self.cards.move(role="dispatcher", actor="dispatcher", reference=card, target="validate", reason="ready")
        self.cards.move(role="dispatcher", actor="dispatcher", reference=card, target="assessment", reason="reviewed")
        self.cards.decide(role="observer", actor="observer", reference=card, kind="rework", body="repair it")
        self.cards.move(role="dispatcher", actor="dispatcher", reference=card, target="in_progress", reason="rework",
                        decision="rework")
        self.assertEqual(self.cards.reader.show(card)["dod_item"], "d1")
        self.cards.move(role="dispatcher", actor="dispatcher", reference=card, target="validate", reason="repaired")
        self.cards.move(role="dispatcher", actor="dispatcher", reference=card, target="assessment", reason="reviewed")
        self.cards.decide(role="observer", actor="observer", reference=card, kind="accept", body="accepted")
        self.cards.move(role="dispatcher", actor="dispatcher", reference=card, target="done", reason="merged",
                        decision="accept")
        self.assertEqual(self.cards.reader.show(card)["dod_item"], "d1")


if __name__ == "__main__":
    unittest.main()
