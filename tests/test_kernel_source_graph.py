"""ARCH-04's full source graph belongs to CI, alongside recovery's integration shard."""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

from tests import config_dependencies, source_dependencies, source_trees

PRODUCT = Path(__file__).resolve().parents[1] / "src" / "ummanu"


class KernelSourceGraphTests(unittest.TestCase):
    def test_arch04_all_four_kernel_facts_on_uncut_source_graph(self) -> None:
        sources = {str(path.relative_to(PRODUCT.parent)): path.read_text() for path in PRODUCT.rglob("*.py")}
        graph = source_dependencies.source_graph(sources)
        groups = source_dependencies.components(graph)
        self.assertLessEqual(len(groups[0]), 60, groups[0])
        reached = config_dependencies.closure(PRODUCT)
        self.assertLessEqual(len(reached), 10, sorted(reached))
        self.assertFalse(any(name == prefix or name.startswith(prefix + ".") for name in reached for prefix in ("ummanu.tasks", "ummanu.sprints")))
        leaf = source_trees.parse((PRODUCT / "kernel" / "errors.py").read_text())
        for node in source_trees.imports(leaf):
            names = [node.module or ""] if isinstance(node, ast.ImportFrom) else [alias.name for alias in node.names]
            self.assertFalse(getattr(node, "level", 0))
            self.assertTrue(all(name.split(".")[0] in sys.stdlib_module_names for name in names), names)
        board = source_trees.parse((PRODUCT / "board" / "__init__.py").read_text())
        self.assertEqual(len(board.body), 1)
        self.assertIsInstance(board.body[0], ast.Expr)
        self.assertIsInstance(board.body[0].value, ast.Constant)
        self.assertIsInstance(board.body[0].value.value, str)
