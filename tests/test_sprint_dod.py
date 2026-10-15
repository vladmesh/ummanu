"""Definition of Done items, weights and progress as every reader receives them, without a board."""

from __future__ import annotations

import unittest

from ummanu.board import sprint_dod
from ummanu.dispatch.observer_prompt import render_observer_prompt

BY = {"role": "observer", "actor": "observer", "at": "2026-10-15T00:00:00Z"}


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


class ReaderTests(unittest.TestCase):
    def test_the_observer_launch_document_lists_each_item_with_state_and_weight(self) -> None:
        items = sprint_dod.met(weighed(5, 2), "d1", "PR 4", BY)

        document = render_observer_prompt({"ref": "sprint:1", "comments": [], "dod": sprint_dod.section(items)})

        self.assertIn("- d1 [met, weight 5; evidence: PR 4] fact 1", document)
        self.assertIn("- d2 [open, weight 2] fact 2", document)
        self.assertIn("1 of 2 met.", document)


if __name__ == "__main__":
    unittest.main()
