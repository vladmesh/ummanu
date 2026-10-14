"""Installation owner contracts and small planted static graph regressions."""

from __future__ import annotations

import argparse
import ast
import contextlib
import copy
import hashlib
import io
import json
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests import source_dependencies, source_trees
from ummanu.installation import clone, commands, credentials, materialize, models, provision, support
from ummanu.runtime import cli_composition

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "ummanu" / "installation"
INVENTORY = ROOT / "docs" / "installation-boundary-inventory.json"


class CliRootRegistration(ast.NodeTransformer):
    """Expand the shared root facts when comparing the existing installation registrar.

    The historical source capture stays intact. Only the two add_parser expressions moved
    in ummanu-272; their names, help, nested arguments and handler AST must still match it.
    """

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name) and node.func.id == "add_command_parser":
            if len(node.args) != 2 or node.keywords:
                raise AssertionError("root registration must name its subparsers and one command")
            name = ast.literal_eval(node.args[1])
            options = dict(cli_composition.ROOT_COMMANDS["install"])[name]
            return ast.Call(
                func=ast.Attribute(value=node.args[0], attr="add_parser", ctx=ast.Load()),
                args=[ast.Constant(name)],
                keywords=[ast.keyword(arg=key, value=ast.Constant(value)) for key, value in options.items()],
            )
        return self.generic_visit(node)


class InstallationBoundaryTests(unittest.TestCase):
    def test_package_root_is_only_a_docstring(self) -> None:
        tree = ast.parse((PACKAGE / "__init__.py").read_text())
        self.assertEqual(len(tree.body), 1)
        self.assertIsInstance(tree.body[0], ast.Expr)
        self.assertIsInstance(tree.body[0].value, ast.Constant)
        self.assertIsInstance(tree.body[0].value.value, str)

    def test_definitions_match_baseline_ast_and_files_stay_bounded(self) -> None:
        inventory = json.loads(INVENTORY.read_text())
        found = {}
        for path in PACKAGE.glob("*.py"):
            self.assertLessEqual(len(path.read_text().splitlines()), 1500, path.name)
            tree = ast.parse(path.read_text())
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                    name = node.name
                elif isinstance(node, ast.Assign):
                    name = node.targets[0].id
                else:
                    continue
                self.assertNotIn(name, found)
                if name == "add_install_commands":
                    self.assertIs(commands.add_command_parser, cli_composition.add_command_parser)
                    node = CliRootRegistration().visit(copy.deepcopy(node))
                found[name] = {
                    "owner": path.stem,
                    "ast_sha256": hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest(),
                }
        expected = {name: {key: entry[key] for key in ("owner", "ast_sha256")} for name, entry in inventory["definitions"].items()}
        self.assertEqual(found, expected)

    def test_owner_dependencies_are_directed_and_share_one_error(self) -> None:
        allowed = {
            "models": set(),
            "support": {"models"},
            "credentials": {"models", "support"},
            "clone": {"models", "support"},
            "materialize": {"models", "support", "credentials"},
            "provision": {"models", "support", "materialize"},
            "commands": {"models", "support", "credentials", "clone", "materialize", "provision"},
        }
        for path in PACKAGE.glob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("ummanu.installation"):
                    self.assertFalse(node.level)
                    self.assertNotEqual(node.module, "ummanu.installation")
                    owner = node.module.removeprefix("ummanu.installation.")
                    self.assertIn(owner, allowed[path.stem])
                    self.assertTrue(all(alias.name != "*" for alias in node.names))
        for owner in (clone, commands, credentials, materialize, provision, support):
            self.assertIs(owner.InstallError, models.InstallError)
        self.assertIs(commands.InstallResult, models.InstallResult)
        self.assertIs(materialize.PipelineStateMaterialization, models.PipelineStateMaterialization)

    def test_import_and_patch_seams_resolve_to_existing_owner_bindings(self) -> None:
        bindings = {}
        for path in PACKAGE.glob("*.py"):
            names = set()
            for node in ast.parse(path.read_text()).body:
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                    names.add(node.name)
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.Assign):
                    names.update(target.id for target in node.targets if isinstance(target, ast.Name))
            bindings[path.stem] = names
        for directory in (ROOT / "src", ROOT / "tests"):
            for path, tree in source_trees.modules(directory, naming=("ummanu.installation",)):
                aliases = {}
                for node in source_trees.imports(tree):
                    if isinstance(node, ast.ImportFrom) and node.module == "ummanu.installation":
                        for alias in node.names:
                            self.assertIn(alias.name, bindings, (path, node.lineno))
                            aliases[alias.asname or alias.name] = alias.name
                for node in source_trees.walk(tree):
                    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or not node.args:
                        continue
                    target = node.args[0]
                    if node.func.attr == "patch" and isinstance(target, ast.Constant) and isinstance(target.value, str):
                        match = re.match(r"^ummanu\.installation\.([A-Za-z_]+)\.([A-Za-z_]+)", target.value)
                        if target.value.startswith("ummanu.installation."):
                            self.assertIsNotNone(match, (path, node.lineno, target.value))
                            assert match is not None
                            owner, name = match.groups()
                            self.assertIn(owner, bindings, (path, node.lineno))
                            self.assertIn(name, bindings[owner], (path, node.lineno, target.value))
                    elif node.func.attr == "object" and isinstance(target, ast.Name) and target.id in aliases and len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                        self.assertIn(node.args[1].value, bindings[aliases[target.id]], (path, node.lineno))

    def test_result_render_json_order_and_exit_codes(self) -> None:
        # Expected bytes are captured from baseline source and existing golden assertions,
        # not from an independently executed pre-extraction installation.
        result = models.InstallResult(
            steps=[models.InstallStep("clone", "changed", "cloned private instance remote")],
            projects=[models.ProjectProvisionResult("demo", "/work/demo", "https", "failed", "timeout", "clone timed out", True)],
        )
        self.assertEqual(result.render(), "ummanu install\n  changed   clone: cloned private instance remote\nprojects:\n  failed    demo target=/work/demo transport=https code=timeout retryable=yes: clone timed out\nstatus: degraded")
        args = SimpleNamespace(bootstrap_credential_stdin=False, recovery_phrase_stdin=False, json=True)
        output = io.StringIO()
        with mock.patch.object(commands, "install", return_value=result), contextlib.redirect_stdout(output):
            self.assertEqual(commands.run_install(args), 1)
        self.assertEqual(output.getvalue(), '{\n  "projects": [\n    {\n      "code": "timeout",\n      "outcome": "failed",\n      "project_id": "demo",\n      "reason": "clone timed out",\n      "retryable": true,\n      "target": "/work/demo",\n      "transport": "https"\n    }\n  ],\n  "status": "degraded",\n  "steps": [\n    {\n      "detail": "cloned private instance remote",\n      "name": "clone",\n      "status": "changed"\n    }\n  ]\n}\n')
        for steps, expected in (([], 0), ([models.InstallStep("install", "failed", "refused")], 1)):
            with self.subTest(expected=expected), mock.patch.object(commands, "install", return_value=models.InstallResult(steps=steps)), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(commands.run_install(args), expected)
        args.bootstrap_credential_stdin = args.recovery_phrase_stdin = True
        with mock.patch.object(commands, "install") as engine, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(commands.run_install(args), 2)
        engine.assert_not_called()

    def test_install_recover_parser_keeps_handler_and_defaults(self) -> None:
        parser = argparse.ArgumentParser()
        commands.add_install_commands(parser.add_subparsers(dest="command"))
        for command, recovery in (("install", False), ("recover", True)):
            args = parser.parse_args([command, "--instance-remote", "remote", "--instance-dir", "/tmp/instance", "--installation-user", "operator"])
            self.assertIs(args.handler, commands.run_install)
            self.assertEqual(args.recover, recovery)
            self.assertFalse(args.adopt)
            self.assertFalse(args.dry_run)
            self.assertFalse(args.json)
            self.assertIsNone(args.bootstrap_credential_file)
            self.assertFalse(args.bootstrap_credential_stdin)


class KernelSourceGraphPlantedTests(unittest.TestCase):
    def test_nested_lazy_alias_string_and_package_edges_are_real_cycles(self) -> None:
        sources = {
            "ummanu/__init__.py": '"""root"""\n',
            "ummanu/installation/__init__.py": "from . import commands\n",
            "ummanu/installation/commands.py": "def install():\n    from .. import secret_store as store\n    return store.unlock()\n",
            "ummanu/secret_store.py": 'KEY_PARAMS_FORMAT = "ummanu.installation-key"\n',
        }
        graph = source_dependencies.source_graph(sources)
        self.assertIn("ummanu.installation", graph["ummanu.secret_store"])
        self.assertIn("ummanu.secret_store", graph["ummanu.installation.commands"])
        self.assertIn("ummanu.installation.commands", graph["ummanu.installation"])
        self.assertEqual(source_dependencies.components(graph)[0], ["ummanu.installation", "ummanu.installation.commands", "ummanu.secret_store"])
        sources["ummanu/installation/__init__.py"] = '"""thin namespace"""\n'
        self.assertEqual(max(map(len, source_dependencies.components(source_dependencies.source_graph(sources)))), 1)

    def test_planted_barrel_breaks_the_sixty_module_scc_limit(self) -> None:
        sources = {"ummanu/__init__.py": "from . import node0\n"}
        for number in range(60):
            sources[f"ummanu/node{number}.py"] = f"def lazy():\n    from . import node{(number + 1) % 60}\n"
        self.assertEqual(len(source_dependencies.components(source_dependencies.source_graph(sources))[0]), 61)
        sources["ummanu/__init__.py"] = '"""thin namespace"""\n'
        self.assertEqual(len(source_dependencies.components(source_dependencies.source_graph(sources))[0]), 60)
