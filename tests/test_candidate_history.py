"""The candidate-history preflight's own decisions (secretary-1401).

The gate-level tests — where the check sits relative to the branch push — live next to the other
gate tests in `tests/test_dispatcher.py::DispatcherGateTests`. These are about the decision itself:
which trailers are forbidden, which are ordinary collaboration, and what the worker is told.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

import ummanu
from ummanu.candidate_history import (
    Commit,
    ai_attributions,
    parse_shas,
    repair_message,
    without_ai_attribution,
)


def _commit(message: str, sha: str = "a" * 40) -> Commit:
    return Commit(sha=sha, message=message)


class CandidateHistoryTests(unittest.TestCase):
    def test_a_clean_message_carries_no_attribution(self) -> None:
        commits = [_commit("Fix the boundary\n\nA body that mentions Claude in prose.\n")]

        self.assertEqual(ai_attributions(commits), [])

    def test_a_claude_trailer_is_reported_with_its_commit(self) -> None:
        commits = [
            _commit(
                "Add the preflight\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n",
                sha="1" * 40,
            )
        ]

        found = ai_attributions(commits)

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].sha, "1" * 40)
        self.assertEqual(found[0].subject, "Add the preflight")
        self.assertIn("registered agent claude", found[0].identity)
        self.assertIn("noreply@anthropic.com", found[0].trailer)

    def test_a_codex_trailer_is_reported_too(self) -> None:
        commits = [_commit("Work\n\nCo-authored-by: Codex <codex@openai.com>\n")]

        self.assertEqual(
            [item.identity for item in ai_attributions(commits)],
            ["registered agent codex <codex@openai.com>"],
        )

    def test_only_registered_agent_name_and_address_pairs_are_rejected(self) -> None:
        commits = [
            _commit("Work\n\nCo-Authored-By: Claude <claude@example.invalid>\n", sha="1" * 40),
            _commit(
                "More\n\nCo-Authored-By: Copilot <198982749+Copilot@users.noreply.github.com>\n",
                sha="2" * 40,
            ),
        ]

        self.assertEqual(
            [item.identity for item in ai_attributions(commits)],
            ["registered agent copilot <copilot@users.noreply.github.com>"],
        )

    def test_casing_and_leading_space_do_not_hide_a_trailer(self) -> None:
        commits = [_commit("Work\n\n  CO-AUTHORED-BY:  Claude Code <NoReply@Anthropic.com>\n")]

        self.assertEqual(
            [item.identity for item in ai_attributions(commits)],
            ["registered agent claude code <noreply@anthropic.com>"],
        )

    def test_an_addressless_agent_trailer_is_matched_on_its_exact_name(self) -> None:
        """Git never writes a trailer without an address, so a head that does is not following git's
        format either. An exact agent name still counts; a person's name that merely contains one
        does not."""
        forbidden = [_commit("Work\n\nCo-Authored-By: Claude Code\n")]
        human = [_commit("Work\n\nCo-Authored-By: Claude Martin, reviewer\n")]

        self.assertEqual([item.identity for item in ai_attributions(forbidden)], ["name claude code"])
        self.assertEqual(ai_attributions(human), [])

    # secretary-1401 round 2, BLOCKER-human-coauthor-false-positive: display-name matching rejected
    # an ordinary human co-author. Identity is decided on the address now, and these are the names
    # that used to fail.
    def test_a_human_named_after_a_model_stays_an_ordinary_co_author(self) -> None:
        commits = [
            _commit(
                "Pair on the parser\n\n"
                "Co-Authored-By: Claude Martin <claude.martin@human.example>\n"
                "Co-Authored-By: Gemini Rossi <gemini.rossi@human.example>\n"
                "Co-Authored-By: Claudia Ramirez <claudia@example.invalid>\n"
                "Co-Authored-By: A. Codexter <a@codexpertise.example>\n"
                "Co-Authored-By: Vlad <vlad@example.invalid>\n"
                "Co-Authored-By: Cursor Blake <cursor.blake@human.example>\n"
            )
        ]

        self.assertEqual(ai_attributions(commits), [])

    def test_vendor_employees_and_ambiguous_local_parts_stay_human(self) -> None:
        commits = [
            _commit("Work\n\nCo-Authored-By: Taylor <taylor@openai.com>\n"),
            _commit("Work\n\nCo-Authored-By: Claude Henderson <claude@personal.example>\n"),
        ]

        self.assertEqual(ai_attributions(commits), [])

    def test_trailer_shaped_prose_outside_the_final_block_is_ignored(self) -> None:
        commits = [
            _commit(
                "Document the policy\n\n"
                "Example: Co-Authored-By trailers are forbidden.\n"
                "Co-Authored-By: Codex <codex@openai.com>\n"
                "This line is ordinary prose after the example.\n"
            )
        ]

        self.assertEqual(ai_attributions(commits), [])

    def test_a_terminal_example_without_a_blank_separator_is_not_a_git_trailer(self) -> None:
        commits = [
            _commit(
                "Document the policy\nA literal example follows:\nCo-Authored-By: Codex <codex@openai.com>\n"
            )
        ]

        self.assertEqual(ai_attributions(commits), [])

    def test_every_forbidden_trailer_across_every_commit_is_named_at_once(self) -> None:
        commits = [
            _commit("First\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n", sha="1" * 40),
            _commit("Second\n\nno trailer here\n", sha="2" * 40),
            _commit(
                "Third\n\n"
                "Co-Authored-By: Vlad <vlad@example.invalid>\n"
                "Co-Authored-By: Codex <codex@openai.com>\n"
                "Co-Authored-By: GitHub Copilot <copilot@example.invalid>\n",
                sha="3" * 40,
            ),
        ]

        found = ai_attributions(commits)

        self.assertEqual(
            [(item.sha[:1], item.identity) for item in found],
            [
                ("1", "registered agent claude <noreply@anthropic.com>"),
                ("3", "registered agent codex <codex@openai.com>"),
            ],
        )

    # secretary-1401 round 2, BLOCKER-history-record-separator-bypass: the object ids are the only
    # candidate-controlled listing this check parses, and a line that is not one is not skipped.
    def test_free_text_loses_only_the_lines_that_credit_an_agent(self) -> None:
        for line in (
            "🤖 Generated with [Claude Code](https://claude.com/claude-code)",
            "generated by OpenAI Codex.",
            "Co-authored-by: Codex <codex@openai.com>",
        ):
            with self.subTest(line=line):
                self.assertEqual(without_ai_attribution(f"Summary.\n{line}"), "Summary.")
        for prose in ("Generated by the build script.", "Co-Authored-By: Claude Martin <claude@example.org>"):
            with self.subTest(prose=prose):
                self.assertEqual(without_ai_attribution(f"Summary.\n{prose}"), f"Summary.\n{prose}")

    def test_the_object_id_listing_is_read_strictly(self) -> None:
        self.assertEqual(parse_shas(f"{'1' * 40}\n{'2' * 40}\n"), ["1" * 40, "2" * 40])
        self.assertEqual(parse_shas(""), [])
        self.assertEqual(parse_shas("\n"), [])
        with self.assertRaises(ValueError):
            parse_shas(f"{'1' * 40}\nCo-Authored-By: Claude <noreply@anthropic.com>\n")
        with self.assertRaises(ValueError):
            parse_shas("1" * 39)

    def test_a_control_byte_in_the_message_does_not_hide_a_trailer(self) -> None:
        """A message may contain any byte, record and unit separators included. Nothing frames
        messages against each other here, so a trailer behind one is still a trailer."""
        commits = [
            _commit(
                "Sneak\n\n\x1e\x1f\x00\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n",
                sha="1" * 40,
            )
        ]

        self.assertEqual(
            [item.identity for item in ai_attributions(commits)],
            ["registered agent claude <noreply@anthropic.com>"],
        )

    def test_the_repair_message_asks_for_a_local_rewrite_and_no_force_push(self) -> None:
        found = ai_attributions(
            [_commit("Add the preflight\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n")]
        )

        message = repair_message(found, "main")

        self.assertIn("Add the preflight", message)
        self.assertIn("git commit --amend", message)
        self.assertIn("git rebase -i origin/main", message)
        self.assertIn("Nothing has been published", message)
        self.assertIn("do not force-push", message)


class GeneratedPacketInvariantTests(unittest.TestCase):
    """The other half of keeping generated packets out of a candidate (#181): this repository
    ignores them, so a worker that runs `git add -A` cannot commit the round's own TASK.md."""

    def test_the_product_repository_ignores_the_generated_handoff_packets(self) -> None:
        repo = Path(ummanu.__file__).resolve().parent.parent
        if not (repo / ".git").exists():
            self.skipTest("not running from a git checkout of the product repository")

        ignored = subprocess.run(
            ["git", "-C", str(repo), "check-ignore", "TASK.md", "REVIEW.md"],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(ignored.returncode, 0, ignored.stderr)
        self.assertEqual(sorted(ignored.stdout.split()), ["REVIEW.md", "TASK.md"])
        tracked = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "TASK.md", "REVIEW.md"],
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual(tracked.stdout.strip(), "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
