"""Every command a shipped role skill names exists in the product it ships with.

A skill is read by a head as instructions, so a command that names a removed module or subcommand
fails only at run time, inside the head, long after the change that removed it was merged. The
background agents moved from the top-level `triggered_agents` package into `ummanu automations`
(sprint:1459); these tests keep every skill on the product CLI's real tree.
"""

from __future__ import annotations

import argparse
import functools
import inspect
import re
import unittest
from importlib import import_module
from pathlib import Path

from ummanu.automations import __main__ as automations_main
from ummanu.board.card_transitions import CARD_TRANSITIONS
from ummanu.board.roles import CREATE_ROLES, PROPOSAL_CREATE_ROLES, Role
from ummanu.cli import build_parser

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"
RETRO_SKILL = SKILLS / "roles" / "retro" / "retro" / "SKILL.md"
STEWARD_SKILL = SKILLS / "roles" / "steward" / "steward" / "SKILL.md"

# `python3 -P -m ummanu <words>`: the words up to the first flag, placeholder or punctuation.
_COMMAND = re.compile(r"python3 -P -m ummanu\b(?P<rest>[^\n`]*)")
_WORD = re.compile(r"[a-z][a-z0-9-]*")


def _skill_files() -> list[Path]:
    return sorted(SKILLS.rglob("SKILL.md"))


def _commands(text: str) -> list[list[str]]:
    """The subcommand words of every `python3 -P -m ummanu ...` a text names."""
    commands: list[list[str]] = []
    for match in _COMMAND.finditer(text):
        words: list[str] = []
        for token in match.group("rest").split():
            if not _WORD.fullmatch(token):
                break
            words.append(token)
        commands.append(words)
    return commands


def _task_blocks(text: str) -> list[str]:
    """Each `ummanu task` command of a text with its backslash-continued lines."""
    blocks: list[str] = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if "python3 -P -m ummanu task " not in line:
            continue
        block = [line]
        while block[-1].rstrip().endswith("\\") and index + len(block) < len(lines):
            block.append(lines[index + len(block)])
        blocks.append("\n".join(block))
    return blocks


def _subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser] | None:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return None


def _agent_commands(agent: str) -> set[str]:
    """The `<cmd>` words one background agent's CLI answers, plus the runner's own `dispatch`."""
    source = inspect.getsource(import_module(f"ummanu.automations.agents.{agent}.cli").main)
    return set(re.findall(r'"([a-z][a-z0-9-]*)"', source)) | {"dispatch"}


@functools.cache
def _product_parser() -> argparse.ArgumentParser:
    """The product CLI's parser, built once: it is only walked here, never parsed with, and building
    it per named command kept this module near CI's 5 s per-test budget."""
    return build_parser()


def _unknown(words: list[str]) -> str | None:
    """Why this word path is not a real `ummanu` command, or None when it is."""
    if not words:
        return "names no subcommand"
    parser: argparse.ArgumentParser = _product_parser()
    for index, word in enumerate(words):
        choices = _subcommands(parser)
        if choices is None:
            break
        if word not in choices:
            return f"{' '.join(words[:index] + [word])!r} is not a subcommand"
        parser = choices[word]
        if words[: index + 1] == ["automations"]:
            return _unknown_automation(words[index + 1 :])
    return None


def _unknown_automation(words: list[str]) -> str | None:
    if not words:
        return "automations names no agent"
    agent, rest = words[0], words[1:]
    if agent == "health":
        return None
    if agent not in automations_main.AGENTS:
        return f"automations has no agent {agent!r}"
    if not rest:
        return f"automations {agent} names no command"
    if rest[0] not in _agent_commands(agent):
        return f"automations {agent} has no command {rest[0]!r}"
    return None


class SkillCommandTests(unittest.TestCase):
    def test_the_skills_tree_is_where_this_test_looks(self) -> None:
        self.assertIn(RETRO_SKILL, _skill_files())
        self.assertTrue(any(_commands(path.read_text(encoding="utf-8")) for path in _skill_files()))

    def test_no_skill_names_the_retired_agents_package(self) -> None:
        offenders = [
            f"{path.relative_to(ROOT)}:{number}"
            for path in _skill_files()
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
            if "triggered_agents" in line
        ]
        self.assertEqual(offenders, [])

    def test_every_ummanu_command_a_skill_names_is_a_real_subcommand(self) -> None:
        offenders: list[str] = []
        for path in _skill_files():
            for words in _commands(path.read_text(encoding="utf-8")):
                reason = _unknown(words)
                if reason is not None:
                    offenders.append(f"{path.relative_to(ROOT)}: ummanu {' '.join(words)}: {reason}")
        self.assertEqual(offenders, [])

    def test_a_removed_command_is_caught(self) -> None:
        self.assertEqual(
            _commands("run `python3 -P -m ummanu task list --state issues`"), [["task", "list"]]
        )
        self.assertIsNone(_unknown(["task", "list"]))
        self.assertIsNone(_unknown(["automations", "curator", "precheck"]))
        self.assertIsNone(_unknown(["automations", "health"]))
        self.assertIsNotNone(_unknown(["pipeline", "list"]))
        self.assertIsNotNone(_unknown(["task", "idea"]))
        self.assertIsNotNone(_unknown(["automations", "pipeline", "list"]))
        self.assertIsNotNone(_unknown(["automations", "retro", "idea"]))
        self.assertIsNotNone(_unknown([]))


class RetroSkillPermissionTests(unittest.TestCase):
    """The retro skill proposes through the product CLI with exactly what its role may do."""

    def setUp(self) -> None:
        self.text = RETRO_SKILL.read_text(encoding="utf-8")

    def test_retro_reads_and_proposes_only_with_its_role_permissions(self) -> None:
        self.assertIn(Role.RETRO, CREATE_ROLES)
        self.assertIn(Role.RETRO, PROPOSAL_CREATE_ROLES)
        self.assertEqual(CARD_TRANSITIONS[Role.RETRO], frozenset())
        blocks = _task_blocks(self.text)
        verbs = {_commands(block)[0][1] for block in blocks}
        self.assertEqual(verbs, {"list", "create"})
        for block in blocks:
            words = _commands(block)[0]
            if words[1] == "list":
                self.assertIn("--state issues", block)
                self.assertIn("--state ready", block)
            else:
                self.assertIn("--role retro", block)
                # A proposal role may create in Issues only; any other target is refused.
                self.assertIn("--state issues", block)
                self.assertIn("--project", block)
                self.assertIn("--type", block)
                self.assertIn("--title", block)


class StewardSkillProposalTests(unittest.TestCase):
    """secretary-1709: the steward creates only proposals in Issues, with no sprint."""

    def setUp(self) -> None:
        self.text = STEWARD_SKILL.read_text(encoding="utf-8")

    def test_every_steward_create_is_a_proposal_in_issues(self) -> None:
        self.assertIn(Role.STEWARD, CREATE_ROLES)
        self.assertIn(Role.STEWARD, PROPOSAL_CREATE_ROLES)
        creates = [block for block in _task_blocks(self.text) if _commands(block)[0][1:2] == ["create"]]
        self.assertTrue(creates)
        for block in creates:
            self.assertIn("--role steward", block)
            self.assertIn("--state issues", block)
            self.assertNotIn("--sprint", block)
            self.assertIn("--project", block)
            self.assertIn("--type", block)
            self.assertIn("--title", block)


if __name__ == "__main__":
    unittest.main()
