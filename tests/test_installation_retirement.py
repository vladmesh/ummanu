"""Retired installation engine: ordinary CLI refusal and no executable dependencies."""

from __future__ import annotations

import ast
import contextlib
import io
import json
import re
import unittest
from pathlib import Path
from unittest import mock

from tests import config_dependencies, source_trees
from ummanu import cli
from ummanu.dispatch.entrypoint_guard import RUNBOOK

ROOT = Path(__file__).resolve().parents[1]
REMOVED_PACKAGE = "ummanu.transition"
REMOVED_FILES = (
    "scripts/rename_to_ummanu.py",
    "scripts/transition-from-secretary.sh",
    "tests/test_transition_from_secretary.py",
    "tests/test_transition_board.py",
    "tests/test_transition_products.py",
)
ENGINE_SPELLINGS = re.compile(
    r"\bummanu[./]transition\b|"
    r"\b(?:from|import)\s+[\w.,\s]+\btransition\b|"
    r"\bfrom\s+[\w.]+\s+import\s+\(?[\w,\s]*\btransition\b|"
    r"['\"]\.transition(?:[.'\"])|"
    r"\bclass\s+(?:TransitionError|Names|Rewrapped)\b"
)


def engine_references(path: Path, source: str) -> set[str]:
    """Resolve absolute/relative imports and constant dynamic targets, including absent modules."""
    parts = path.with_suffix("").parts
    if parts[0] == "src":
        parts = parts[1:]
    name = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
    targets = config_dependencies.dependencies(name, path, source, strict_dynamic=False)
    found = {
        target for target in targets if target == REMOVED_PACKAGE or target.startswith(REMOVED_PACKAGE + ".")
    }
    tree = source_trees.parse(source)
    for node in source_trees.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and re.search(r"\bummanu[./]transition(?:\b|/)", node.value)
        ):
            found.add(node.value)
        if isinstance(node, ast.ClassDef) and node.name in {"TransitionError", "Names", "Rewrapped"}:
            found.add(node.name)
    return found


class InstallationRetirementTests(unittest.TestCase):
    def test_former_commands_use_the_real_unknown_command_path_before_any_effect(self) -> None:
        for argv in (
            ["transition"],
            ["transition", "from-secretary", "--plan"],
            ["transition", "from-secretary", "--apply"],
            ["transition", "from-secretary", "--rollback"],
            ["transition", "from-secretary", "--repair-products"],
            ["transition", "from-secretary", "--repair-scope-owners"],
            ["transition", "from-secretary", "--repair-products", "--apply"],
            ["transition", "from-secretary", "--repair-scope-owners", "--apply"],
        ):
            with self.subTest(argv=argv):
                parser = cli.build_parser()
                self.assertIsInstance(parser, cli.StructuredArgumentParser)
                handlers = [
                    action.choices
                    for action in parser._actions
                    if hasattr(action, "choices") and isinstance(action.choices, dict)
                ]
                self.assertEqual(len(handlers), 1)
                self.assertNotIn("transition", handlers[0])
                self.assertNotIn("transition", parser.format_help())
                out, err = io.StringIO(), io.StringIO()
                with (
                    mock.patch.object(cli, "build_parser", return_value=parser),
                    mock.patch.object(cli, "resolve_instance_argument") as resolve,
                    mock.patch.object(cli, "run_automations") as automations,
                    mock.patch.object(parser, "print_help") as help_handler,
                    contextlib.redirect_stdout(out),
                    contextlib.redirect_stderr(err),
                ):
                    self.assertEqual(cli.main(argv), 2)
                self.assertEqual(out.getvalue(), "")
                self.assertEqual(len(err.getvalue().splitlines()), 1)
                envelope = json.loads(err.getvalue())
                self.assertEqual(set(envelope), {"error"})
                self.assertEqual(set(envelope["error"]), {"code", "message"})
                self.assertEqual(envelope["error"]["code"], "usage")
                self.assertIn("invalid choice: 'transition'", envelope["error"]["message"])
                resolve.assert_not_called()
                automations.assert_not_called()
                help_handler.assert_not_called()

    def test_other_commands_still_route_through_their_existing_handlers(self) -> None:
        with mock.patch.object(cli, "run_automations", return_value=101) as automations:
            self.assertEqual(cli.main(["automations", "curator", "rebind", "--dry-run"]), 101)
        automations.assert_called_once_with(["curator", "rebind", "--dry-run"])
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["--help"]), 0)
        self.assertEqual(err.getvalue(), "")
        self.assertIn("doctor", out.getvalue())

    def test_removed_inventory_and_all_executable_references_stay_absent(self) -> None:
        self.assertFalse((ROOT / "src/ummanu/transition").exists())
        self.assertEqual(list((ROOT / "tests").glob("test_transition_*.py")), [])
        for name in REMOVED_FILES:
            self.assertFalse((ROOT / name).exists(), name)
        found = {}
        for directory in ("src", "tests", "scripts", "packaging"):
            for path in (ROOT / directory).rglob("*.py"):
                if path == Path(__file__) or path.name == "test_old_name_guard.py":
                    continue  # exact negative guard fixtures, never executable dependencies
                source = path.read_text()
                if not ENGINE_SPELLINGS.search(source):
                    continue
                references = engine_references(path.relative_to(ROOT), source)
                if references:
                    found[str(path.relative_to(ROOT))] = sorted(references)
        self.assertEqual(found, {})

    def test_the_reference_guard_refuses_planted_imports_strings_and_engine_classes(self) -> None:
        for source in (
            "import ummanu.transition.rewrite as rewrite",
            "from ummanu import transition",
            "from .transition import names",
            "from . import transition",
            "from . import (\n    transition,\n)",
            "from . import config, transition as retired",
            "import importlib; importlib.import_module('ummanu.transition.names')",
            "from importlib import import_module; import_module('.transition', package='ummanu')",
            "target = 'ummanu.transition.commands:add_transition_subcommands'",
            "target = 'src/ummanu/transition/context.py'",
            "class TransitionError(RuntimeError): pass",
            "class Names: pass",
            "class Rewrapped: pass",
        ):
            with self.subTest(source=source):
                self.assertTrue(ENGINE_SPELLINGS.search(source))
                self.assertTrue(engine_references(Path("src/ummanu/planted.py"), source))
        self.assertFalse(
            engine_references(
                Path("src/ummanu/planted.py"), "from ummanu.board.transitions import transition_for"
            )
        )

    def test_the_entrypoint_runbook_locator_names_current_guidance(self) -> None:
        path, heading = RUNBOOK.split(" §")
        self.assertIn("### " + heading + "\n", (ROOT / path).read_text())
        policy = (ROOT / "docs/RENAME.md").read_text()
        self.assertNotIn("ummanu.transition", policy)
        self.assertNotIn("src/ummanu/transition/", policy)
        self.assertNotIn("§T3", policy)
