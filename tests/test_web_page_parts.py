"""The small parts every page is built from: long text, tabs, a head's model and effort, the budget.

They are pure functions of the documents they are handed, so they are pinned here directly rather
than through a transport: what a reader sees on a page is exactly what these return.
"""

from __future__ import annotations

import unittest

from ummanu.web import pages


class LongTextTests(unittest.TestCase):
    def test_completed_po_assignment_is_not_a_pending_origin_return(self) -> None:
        drawn = pages._po_delegated({"items": [{"ref": "ummanu-7", "type": "decision",
            "title": "Choose route", "state": "done", "relation": "assigned"}]})
        self.assertIn("PO assignment", drawn)
        self.assertIn("1 done", drawn)
        self.assertNotIn("not returned yet", drawn)

    def test_short_text_is_shown_as_it_is_in_the_reading_face(self) -> None:
        self.assertEqual(pages._long("one line"), '<span class="prose">one line</span>')

    def test_long_text_is_in_the_page_once_and_folds_in_place(self) -> None:
        text = "The first sentence says what happened. " * 10
        drawn = pages._long(text, chars=80)
        self.assertTrue(drawn.startswith('<details class="text"><summary><span class="prose">'))
        self.assertEqual(drawn.count("The first sentence"), 10, "every sentence once, none repeated")
        self.assertNotIn("<pre>", drawn)

    def test_text_over_several_lines_folds_even_when_it_is_short(self) -> None:
        self.assertIn('<details class="text">', pages._long("one\ntwo"))

    def test_blank_text_is_a_dash(self) -> None:
        self.assertEqual(pages._long("  "), '<span class="empty">—</span>')

    def test_the_text_is_escaped(self) -> None:
        self.assertNotIn("<script>", pages._long("<script>alert(1)</script>"))


class TabsTests(unittest.TestCase):
    def test_every_panel_is_in_the_markup_and_the_first_one_is_chosen(self) -> None:
        drawn = pages._tabs("t", [("One", "<p>first</p>", None), ("Two", "<p>second</p>", 3)])
        self.assertIn("<p>first</p>", drawn)
        self.assertIn("<p>second</p>", drawn)
        self.assertEqual(drawn.count('type="radio"'), 2)
        self.assertEqual(drawn.count(" checked"), 1)
        self.assertIn('id="tab-t-0" checked', drawn)
        self.assertIn('<span class="count">3</span>', drawn)

    def test_the_stylesheet_has_a_rule_for_every_position_a_strip_may_use(self) -> None:
        for position in range(1, pages.MAX_TABS + 1):
            with self.subTest(position=position):
                self.assertIn(f".tab-panel:nth-of-type({position}) {{ display: block; }}", pages.STYLE)
        self.assertNotIn("TAB_RULES", pages.STYLE)

    def test_a_strip_longer_than_the_stylesheet_knows_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            pages._tabs("t", [(str(n), "", None) for n in range(pages.MAX_TABS + 1)])


class ModelNameTests(unittest.TestCase):
    def test_ids_of_a_known_shape_are_said_the_way_people_say_them(self) -> None:
        for model, said in (
            ("claude-opus-5-5", "Opus 5.5"),
            ("claude-sonnet-5", "Sonnet 5"),
            ("claude-haiku-4-5-20251001", "Haiku 4.5"),
            ("gpt-6-sol", "GPT-6 Sol"),
            ("gpt-5.6-terra", "GPT-5.6 Terra"),
        ):
            with self.subTest(model=model):
                self.assertEqual(pages._model_name(model), said)

    def test_an_alias_or_an_unknown_shape_is_left_as_it_is(self) -> None:
        for model in ("opus", "fable", "codex-mini-latest", ""):
            with self.subTest(model=model):
                self.assertEqual(pages._model_name(model), model)


class HeadTests(unittest.TestCase):
    def test_the_model_that_answered_wins_over_the_configured_alias(self) -> None:
        drawn = pages._head(
            "worker", {"model": "opus", "resolved_model": "claude-opus-5-5", "state": "running"}
        )
        self.assertIn('<span class="model">Opus 5.5</span>', drawn)
        self.assertIn("configured as opus", drawn)
        self.assertIn('class="pulse live"', drawn)

    def test_a_head_with_only_an_alias_says_it_is_configured_not_reported(self) -> None:
        drawn = pages._head("observer", {"model": "opus", "effort": "high"})
        self.assertIn('<span class="model">opus</span>', drawn)
        self.assertIn("no run has reported its model yet", drawn)

    def test_a_head_with_no_model_is_an_unknown_model_and_not_a_guess(self) -> None:
        self.assertIn("unknown model", pages._head("reviewer", {}))

    def test_effort_is_bars_and_a_word(self) -> None:
        self.assertEqual(pages._effort("high").count('<i class="on">'), 3)
        self.assertEqual(pages._effort("extra").count('<i class="on">'), 4)
        self.assertIn("<span>xhigh</span>", pages._effort("extra"))
        self.assertIn("<span>max</span>", pages._effort("max"))

    def test_no_effort_passed_is_the_cli_default_and_never_zero_bars_of_a_level(self) -> None:
        for effort in (None, "", "default"):
            with self.subTest(effort=effort):
                drawn = pages._effort(effort)
                self.assertIn("segs unset", drawn)
                self.assertIn("CLI default", drawn)

    def test_an_effort_word_off_the_scale_is_shown_as_its_word(self) -> None:
        drawn = pages._effort("turbo")
        self.assertIn("<span>turbo</span>", drawn)
        self.assertNotIn('class="on"', drawn)


class SprintHeadsTests(unittest.TestCase):
    def item(self) -> dict:
        return {
            "head_profiles": {
                "observer": {
                    "profile": "codex-sol-medium",
                    "via": "launched",
                    "model": "gpt-6-sol",
                    "effort": "medium",
                },
                "worker": {"profile": "claude-opus", "via": "pinned", "model": "opus", "effort": None},
                "reviewer": {"profile": None, "via": "unset", "model": None, "effort": None},
            },
            "observer": {"launch": {"state": "running"}},
        }

    def test_each_profiled_role_is_drawn_with_its_model_and_effort(self) -> None:
        drawn = pages._sprint_heads(self.item())
        self.assertIn('<span class="model">GPT-6 Sol</span>', drawn)
        self.assertIn("<span>medium</span>", drawn)
        self.assertIn('<span class="model">opus</span>', drawn)

    def test_the_observers_liveness_is_the_launchs(self) -> None:
        self.assertEqual(pages._heads_of(self.item())["observer"]["state"], "running")
        self.assertIn('class="pulse live"', pages._sprint_heads(self.item()))

    def test_a_role_the_dispatcher_picks_per_card_is_not_drawn(self) -> None:
        self.assertNotIn("reviewer", pages._heads_of(self.item()))
        self.assertEqual(pages._sprint_heads({}), "")


class BudgetLineTests(unittest.TestCase):
    def test_the_budget_is_a_thin_line_with_its_signal_mark_and_its_words(self) -> None:
        drawn = pages._budget_line(
            {"total": 3, "thresholds": {"signal": 12, "hard": 30}, "by_type": {"red_ci": 3}}
        )
        self.assertIn('class="budget-line "', drawn)
        self.assertIn('style="width:10.0%"', drawn)
        self.assertIn('style="left:40%"', drawn)
        self.assertIn("3 / 30 cards · signal 12 · red_ci 3", drawn)

    def test_a_reached_threshold_colours_the_line(self) -> None:
        self.assertIn(
            'class="budget-line hard"',
            pages._budget_line({"total": 30, "thresholds": {"signal": 12, "hard": 30}, "hard_reached": True}),
        )


class AttentionTests(unittest.TestCase):
    def installation(self, problems: list[str]) -> dict:
        available = {"state": "available", "reason": None, "data_age_seconds": 0.0}
        return {"health": {"source": available, "status": {"state": "attention", "problems": problems}}}

    def test_a_problem_is_named_under_the_strip(self) -> None:
        drawn = pages._attention(self.installation(["unit x is failed", "disk is low"]))
        self.assertIn("unit x is failed", drawn)
        self.assertIn("and 1 more", drawn)
        self.assertIn('href="/doctor"', drawn)

    def test_no_problem_draws_no_banner(self) -> None:
        self.assertNotIn('class="attention"', pages._attention(self.installation([])))

    def test_health_that_could_not_be_read_is_the_marked_block(self) -> None:
        refused = {"state": "unavailable", "reason": "the collector refused", "data_age_seconds": None}
        drawn = pages._attention({"health": {"source": refused, "status": None}})
        self.assertIn("the collector refused", drawn)
        self.assertIn('class="unavailable"', drawn)


if __name__ == "__main__":
    unittest.main()
