"""The first slice of seam M: leaf task errors and a board package without exports."""

from __future__ import annotations

import ast
import importlib
import inspect
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests import source_trees

ROOT = Path(__file__).resolve().parents[1]
PRODUCT = ROOT / "src" / "ummanu"
LEAF = PRODUCT / "kernel" / "errors.py"


def symbol_imports(path: Path, tree: ast.Module) -> list[str]:
    """Old symbol imports, including qualified and constant getattr references."""
    relative = path.relative_to(ROOT).as_posix()
    package = ".".join(path.relative_to(ROOT / "src").with_suffix("").parts[:-1]) if path.is_relative_to(ROOT / "src") else "tests"
    aliases: dict[str, str] = {}
    violations = []
    for node in source_trees.imports(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                module = ".".join(package.split(".")[:len(package.split(".")) - node.level + 1] + ([module] if module else []))
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{module}.{alias.name}"
                if path.is_relative_to(PRODUCT) and module == "ummanu.tasks" and alias.name in {"TaskError", "*"}:
                    violations.append(f"{relative}:{node.lineno}: tasks error import")
                if module == "ummanu.board" and not (PRODUCT / "board" / f"{alias.name}.py").is_file() and not (PRODUCT / "board" / alias.name / "__init__.py").is_file():
                    violations.append(f"{relative}:{node.lineno}: board symbol {alias.name}")

    def qualified(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return f"{qualified(node.value)}.{node.attr}"
        return ""

    qualified_modules = {"ummanu", "ummanu.tasks", "ummanu.board"}
    if not qualified_modules.intersection(aliases.values()):
        return violations
    for node in source_trees.walk(tree):
        name = qualified(node) if isinstance(node, ast.Attribute) else ""
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr" and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
            name = f"{qualified(node.args[0])}.{node.args[1].value}"
        board_name = name.rsplit(".", 1)[-1]
        board_symbol = name.startswith("ummanu.board.") and name.count(".") == 2 and not board_name.startswith("__") and not (PRODUCT / "board" / f"{board_name}.py").is_file() and not (PRODUCT / "board" / board_name / "__init__.py").is_file()
        if (path.is_relative_to(PRODUCT) and name == "ummanu.tasks.TaskError") or board_symbol:
            violations.append(f"{relative}:{node.lineno}: {name}")
    return violations


class KernelImportBoundaryTests(unittest.TestCase):
    def test_task_error_has_one_leaf_definition_without_domain_dependencies(self) -> None:
        self.assertTrue(LEAF.is_file(), "TaskError must have a leaf owner")
        tree = source_trees.parse(LEAF.read_text(), filename=str(LEAF))
        imports = source_trees.imports(tree)
        for node in imports:
            names = [node.module or ""] if isinstance(node, ast.ImportFrom) else [alias.name for alias in node.names]
            self.assertFalse(getattr(node, "level", 0))
            self.assertTrue(all(name.split(".")[0] in sys.stdlib_module_names for name in names), names)
        definitions = []
        for path, module in source_trees.modules(PRODUCT, naming=("class TaskError",)):
            definitions.extend(path.relative_to(ROOT).as_posix() for node in source_trees.walk(module) if isinstance(node, ast.ClassDef) and node.name == "TaskError")
        self.assertEqual(definitions, ["src/ummanu/kernel/errors.py"])

    def test_board_package_has_no_symbol_barrel_or_import_hooks(self) -> None:
        tree = source_trees.parse((PRODUCT / "board" / "__init__.py").read_text())
        self.assertEqual(len(tree.body), 1, "board initialization must contain only its docstring")
        self.assertIsInstance(tree.body[0], ast.Expr)
        self.assertIsInstance(tree.body[0].value, ast.Constant)
        self.assertIsInstance(tree.body[0].value.value, str)

    def assert_consumers_use_owning_modules(self, directory: Path, naming: tuple[str, ...]) -> None:
        violations = []
        for path, tree in source_trees.modules(directory, naming=naming):
            if "board/migrations/versions/" in path.as_posix():
                continue
            violations.extend(symbol_imports(path, tree))
        self.assertEqual(violations, [])

    def test_product_consumers_use_leaf_errors_and_board_owning_modules(self) -> None:
        self.assert_consumers_use_owning_modules(ROOT / "src", ("ummanu.board", ".board import", "TaskError"))

    def test_test_consumers_use_board_owning_modules(self) -> None:
        self.assert_consumers_use_owning_modules(ROOT / "tests", ("from ummanu.board import", "import ummanu.board", "from ummanu import board", ".board import"))

    def test_script_consumers_use_board_owning_modules(self) -> None:
        self.assert_consumers_use_owning_modules(ROOT / "scripts", ("from ummanu.board import", "import ummanu.board", "from ummanu import board", ".board import"))

    def test_guard_detects_qualified_and_dynamic_legacy_symbols(self) -> None:
        tree = ast.parse("import ummanu.tasks as tasks\nimport ummanu.board as board\na = tasks.TaskError\nb = getattr(tasks, 'TaskError')\nc = board.Actor\nd = getattr(board, 'transition')\n")
        self.assertEqual(len(symbol_imports(PRODUCT / "probe.py", tree)), 4)
        tree = ast.parse("from ummanu.board import owner_events\nfrom ummanu.tasks import TaskReader\n")
        self.assertEqual(symbol_imports(PRODUCT / "probe.py", tree), [])

    def test_leaf_and_models_import_without_upper_owners(self) -> None:
        # Restore the test process's module table after exercising a cold import.
        with mock.patch.dict(sys.modules):
            for name in tuple(sys.modules):
                if name == "ummanu" or name.startswith("ummanu."):
                    del sys.modules[name]
            leaf = importlib.import_module("ummanu.kernel.errors")
            self.assertEqual({name for name in sys.modules if name.startswith("ummanu.")}, {"ummanu.kernel", leaf.__name__})
            importlib.import_module("ummanu.board.models")
            forbidden = ("ummanu.tasks", "ummanu.sprints", "ummanu.dispatch", "ummanu.board.host", "ummanu.board.events", "ummanu.board.fake", "ummanu.board.sql_host", "ummanu.board.sql_cards", "ummanu.board.sql_audit", "ummanu.board.sql_sprints", "ummanu.board.sql_product_issues")
            self.assertEqual([name for name in sys.modules if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)], [])

    def test_error_identity_contract_and_real_role_refusal(self) -> None:
        from ummanu.board.backend import _store_refusal
        from ummanu.kernel.errors import TaskError
        from ummanu.tasks import ArtifactOwnershipTaskError, TaskError as LegacyTaskError, admit_role

        self.assertIs(LegacyTaskError, TaskError)
        self.assertEqual(str(inspect.signature(TaskError)), "(code: 'str', message: 'str', exit_code: 'int') -> 'None'")
        error = TaskError("refused", "safe message", 3)
        self.assertEqual((error.code, error.message, error.exit_code), ("refused", "safe message", 3))
        self.assertEqual(error.args, ("safe message",))
        self.assertEqual(str(error), "safe message")
        self.assertIsInstance(error, Exception)
        self.assertTrue(issubclass(ArtifactOwnershipTaskError, TaskError))
        refusal = _store_refusal(RuntimeError("missing configuration"))
        self.assertIsInstance(refusal, LegacyTaskError)
        self.assertEqual((refusal.code, refusal.message, refusal.exit_code), ("backend_unavailable", "board store is not usable: missing configuration", 1))
        class DerivedError(TaskError):
            pass
        self.assertIsInstance(DerivedError("refused", "safe message", 3), LegacyTaskError)
        with self.assertRaises(LegacyTaskError) as caught:
            admit_role("po", "observer", {"po"})
        self.assertEqual((caught.exception.code, caught.exception.exit_code), ("role_masquerade", 3))
