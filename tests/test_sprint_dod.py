"""Definition of Done items, weights and progress as every reader receives them, without a board."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ummanu import restore
from ummanu.board import sprint_dod
from ummanu.data import normalize_sprint_entity
from ummanu.dispatch.observer_prompt import render_observer_prompt
from ummanu.sprint_close import closeout_document

BY = {"role": "observer", "actor": "observer", "at": "2026-10-15T00:00:00Z"}
PO = {"role": "po", "actor": "operator", "at": "2026-10-16T00:00:00Z"}


def weighed(*weights: int) -> list[dict]:
    items = sprint_dod.new_items([f"fact {index}" for index in range(1, len(weights) + 1)])
    for index, weight in enumerate(weights, 1):
        items = sprint_dod.weighed(items, f"d{index}", weight, "beside the others")
    return items


class ProgressTests(unittest.TestCase):
    def test_progress_is_the_met_weight_over_all_weight(self) -> None:
        items = sprint_dod.met(weighed(8, 1, 3), "d2", "PR 12", BY)
        items = sprint_dod.met(items, "d3", "PR 13", BY)

        view = sprint_dod.section(items)

        self.assertTrue(view["estimated"])
        self.assertAlmostEqual(view["progress"], 4 / 12)
        self.assertEqual((view["met"], view["total"]), (2, 3))
        self.assertEqual([round(item["share"], 4) for item in view["items"]], [0.6667, 0.0833, 0.25])

    def test_until_every_item_is_weighed_each_counts_one(self) -> None:
        items = sprint_dod.weighed(sprint_dod.new_items(["a", "b"]), "d1", 13, "the adapter rewrite")
        items = sprint_dod.met(items, "d1", "PR 1", BY)

        view = sprint_dod.section(items)

        self.assertFalse(view["estimated"])
        self.assertEqual(view["reason"], sprint_dod.UNESTIMATED)
        self.assertEqual(view["progress"], 0.5)

    def test_a_free_text_dod_has_no_items_and_no_progress(self) -> None:
        view = sprint_dod.section(sprint_dod.stored_items(None))

        self.assertEqual(
            (view["structured"], view["items"], view["progress"], view["reason"]),
            (False, [], None, sprint_dod.UNSTRUCTURED),
        )
        self.assertIsNone(sprint_dod.items_of(view))


class ItemChangeTests(unittest.TestCase):
    def test_a_re_estimate_replaces_the_weight_and_its_reason(self) -> None:
        items = sprint_dod.weighed(weighed(3), "d1", 8, "the migration is bigger than it looked")

        self.assertEqual((items[0]["weight"], items[0]["weight_reason"]), (8, "the migration is bigger than it looked"))

    def test_a_weight_off_the_scale_or_without_a_reason_is_refused(self) -> None:
        items = sprint_dod.new_items(["a"])
        for weight, reason in ((4, "between"), (21, "huge"), (5, " ")):
            with self.subTest(weight=weight, reason=reason), self.assertRaises(ValueError):
                sprint_dod.weighed(items, "d1", weight, reason)

    def test_met_needs_evidence_and_happens_once_until_reopened(self) -> None:
        items = sprint_dod.new_items(["a"])
        with self.assertRaises(ValueError):
            sprint_dod.met(items, "d1", "  ", BY)
        items = sprint_dod.met(items, "d1", "card ummanu-9", BY)
        with self.assertRaises(ValueError):
            sprint_dod.met(items, "d1", "card ummanu-10", BY)

        items = sprint_dod.reopened(items, "d1")

        self.assertEqual((items[0]["state"], items[0]["evidence"], items[0]["met_by"]), ("open", None, None))
        with self.assertRaises(ValueError):
            sprint_dod.reopened(items, "d1")

    def test_an_unknown_item_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "d7"):
            sprint_dod.met(sprint_dod.new_items(["a"]), "d7", "PR 1", BY)

    def test_added_items_continue_the_numbering(self) -> None:
        items = sprint_dod.new_items(["c"], existing=sprint_dod.new_items(["a", "b"]))

        self.assertEqual([item["id"] for item in items], ["d1", "d2", "d3"])

    def test_stored_items_round_trip_and_inconsistent_ones_are_refused(self) -> None:
        items = sprint_dod.met(weighed(2, 5), "d1", "PR 3", BY)
        self.assertEqual(sprint_dod.stored_items(sprint_dod.encode(items)), items)
        broken = [
            [{**items[0], "evidence": None}],
            [items[0], {**items[1], "id": "d1"}],
            [{**items[1], "weight": 4}],
            [{**items[1], "weight_reason": None}],
        ]
        for value in broken:
            with self.subTest(value=value), self.assertRaises(ValueError):
                sprint_dod.stored_items(value)


class DescopeTests(unittest.TestCase):
    def test_a_descoped_item_counts_in_no_progress_and_is_not_unmet(self) -> None:
        items = sprint_dod.met(weighed(3, 8, 2), "d1", "PR 1", BY)
        items = sprint_dod.descoped(items, "d2", "the owner dropped the live check", PO)

        view = sprint_dod.section(items)

        self.assertAlmostEqual(view["progress"], 3 / 5)
        self.assertEqual((view["met"], view["total"]), (1, 2))
        self.assertEqual([round(item["share"], 4) for item in view["items"]], [0.6, 0.0, 0.4])
        self.assertEqual(view["items"][1]["descoped"], {**PO, "reason": "the owner dropped the live check"})
        self.assertEqual([item["id"] for item in sprint_dod.unmet(items)], ["d3"])
        self.assertEqual(sprint_dod.unmet(sprint_dod.met(items, "d3", "PR 3", BY)), [])

    def test_an_unweighed_descoped_item_leaves_the_scale_estimated(self) -> None:
        items = sprint_dod.weighed(sprint_dod.new_items(["a", "b"]), "d1", 5, "the adapter")
        items = sprint_dod.descoped(items, "d2", "out of this sprint", PO)

        self.assertTrue(sprint_dod.section(items)["estimated"])

    def test_only_an_open_item_is_descoped_and_only_with_a_reason(self) -> None:
        items = sprint_dod.met(sprint_dod.new_items(["a", "b"]), "d1", "PR 1", BY)
        with self.assertRaisesRegex(ValueError, "d1 is met"):
            sprint_dod.descoped(items, "d1", "too late", PO)
        with self.assertRaises(ValueError):
            sprint_dod.descoped(items, "d2", "  ", PO)
        items = sprint_dod.descoped(items, "d2", "dropped", PO)
        with self.assertRaisesRegex(ValueError, "d2 is descoped"):
            sprint_dod.descoped(items, "d2", "again", PO)

    def test_a_descoped_item_takes_no_meet_reopen_weight_or_rewording(self) -> None:
        items = sprint_dod.descoped(sprint_dod.new_items(["a"]), "d1", "dropped", PO)
        refusals = {
            "meet": lambda: sprint_dod.met(items, "d1", "PR 1", BY),
            "reopen": lambda: sprint_dod.reopened(items, "d1"),
            "weigh": lambda: sprint_dod.weighed(items, "d1", 3, "late estimate"),
            "reword": lambda: sprint_dod.reworded(items, "d1", "new words"),
        }
        for verb, change in refusals.items():
            with self.subTest(verb=verb), self.assertRaisesRegex(ValueError, "descoped"):
                change()

    def test_rewording_keeps_id_weight_state_and_evidence(self) -> None:
        items = sprint_dod.met(weighed(5, 2), "d1", "PR 4", BY)

        changed = sprint_dod.reworded(items, "d1", "  the adapter is rewritten and deployed ")

        self.assertEqual(changed[0], {**items[0], "text": "the adapter is rewritten and deployed"})
        self.assertEqual(changed[1], items[1])
        with self.assertRaises(ValueError):
            sprint_dod.reworded(items, "d2", " ")
        with self.assertRaisesRegex(ValueError, "d9"):
            sprint_dod.reworded(items, "d9", "words")

    def test_stored_descoped_items_round_trip_and_a_bare_one_is_refused(self) -> None:
        items = sprint_dod.descoped(weighed(2, 5), "d2", "dropped", PO)
        self.assertEqual(sprint_dod.stored_items(sprint_dod.encode(items)), items)
        broken = [
            [{key: value for key, value in items[1].items() if key != "descoped"}],
            [{**items[0], "descoped": {**PO, "reason": "dropped"}}],
            [{**items[1], "descoped": PO}],
        ]
        for value in broken:
            with self.subTest(value=value), self.assertRaises(ValueError):
                sprint_dod.stored_items(value)

    def test_a_checkpoint_of_a_descoped_item_restores_it(self) -> None:
        items = sprint_dod.met(sprint_dod.new_items(["a", "b", "c"]), "d1", "PR 1", BY)
        items = sprint_dod.descoped(items, "d2", "dropped", PO)
        sprint = {
            "ref": "sprint:7", "status": "open", "goal": "goal", "definition_of_done": "", "current_task": "",
            "repositories": [], "dod": sprint_dod.section(items),
        }
        with tempfile.TemporaryDirectory() as directory:
            board = Path(directory) / "board"
            board.mkdir()
            exported = {"sprints": [normalize_sprint_entity(sprint)]}
            (board / "sprints.json").write_text(json.dumps(exported), encoding="utf-8")
            (validated,) = restore._normalized_sprints(Path(directory))

        restored = restore._restore_sprint_metadata(validated)[sprint_dod.FIELD]

        self.assertEqual(sprint_dod.stored_items(restored), items)


class ReaderTests(unittest.TestCase):
    def test_the_observer_launch_document_lists_each_item_with_state_and_weight(self) -> None:
        items = sprint_dod.met(weighed(5, 2), "d1", "PR 4", BY)

        document = render_observer_prompt({"ref": "sprint:1", "comments": [], "dod": sprint_dod.section(items)})

        self.assertIn("- d1 [met, weight 5; evidence: PR 4] fact 1", document)
        self.assertIn("- d2 [open, weight 2] fact 2", document)
        self.assertIn("1 of 2 met.", document)

    def test_the_observer_launch_document_and_the_closeout_show_a_descoped_item_with_its_reason(self) -> None:
        items = sprint_dod.met(sprint_dod.new_items(["landed", "live check"]), "d1", "PR 4", BY)
        dod = sprint_dod.section(sprint_dod.descoped(items, "d2", "the owner dropped it", PO))

        document = render_observer_prompt({"ref": "sprint:1", "comments": [], "dod": dod})
        closeout = closeout_document(
            reference="sprint:1", goal="goal", actor="observer", reason="done", body="all landed",
            decisions=None, dod=dod,
        )

        self.assertIn("- d2 [descoped, weight not weighed; reason: the owner dropped it] live check", document)
        self.assertIn("1 of 1 met.", document)
        self.assertIn("- d1 [met; evidence: PR 4] landed", closeout)
        self.assertIn("- d2 [descoped; reason: the owner dropped it] live check", closeout)


if __name__ == "__main__":
    unittest.main()
