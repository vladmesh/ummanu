"""The source index's lifecycle: one parse per unchanged file per process, and only an explicit reset.

Every count is over a private index on a fake tree of three files, so nothing here touches the
index the architectural checks of this process share.
"""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path

from tests import source_trees


class SourceIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.index = source_trees.SourceIndex(freeze=False)
        self.write("a.py", "import os\n")
        self.write("pkg/b.py", "VALUE = 1\n")
        self.write("pkg/c.py", "def f():\n    return 2\n")

    def write(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_an_unchanged_tree_is_parsed_once_however_often_it_is_read(self) -> None:
        first = self.index.modules(self.root)
        self.assertEqual(self.index.parses, 3)
        self.assertEqual(
            [path.relative_to(self.root).as_posix() for path, _ in first], ["a.py", "pkg/b.py", "pkg/c.py"]
        )
        for _ in range(3):
            again = self.index.modules(self.root)
            self.assertEqual([tree for _, tree in again], [tree for _, tree in first])
        # A check that reads one file itself gets the same tree, and the walks are shared too.
        tree = self.index.parse((self.root / "a.py").read_text(encoding="utf-8"))
        self.assertIs(tree, first[0][1])
        self.assertIs(self.index.walk(tree), self.index.walk(tree))
        self.assertIs(self.index.parents(tree), self.index.parents(tree))
        self.assertEqual(self.index.parses, 3)

    def test_a_substituted_file_is_parsed_again_without_a_reset(self) -> None:
        """The key is the text: a fixture that swaps a file cannot be answered from the old tree."""
        self.index.modules(self.root)
        self.write("pkg/b.py", "VALUE = 2\n")
        self.write("pkg/d.py", "OTHER = 3\n")
        trees = dict(self.index.modules(self.root))
        self.assertEqual(self.index.parses, 5)
        self.assertEqual(ast.literal_eval(trees[self.root / "pkg" / "b.py"].body[0].value), 2)  # type: ignore[attr-defined]
        self.assertIn(self.root / "pkg" / "d.py", trees)

    def test_only_an_explicit_reset_parses_the_same_tree_again(self) -> None:
        self.index.modules(self.root)
        self.index.modules(self.root)
        self.assertEqual(self.index.parses, 3)
        self.index.clear()
        self.index.modules(self.root)
        self.assertEqual(self.index.parses, 6)

    def test_a_named_read_parses_only_the_files_that_can_spell_the_name(self) -> None:
        named = self.index.modules(self.root, naming=("VALUE",))
        self.assertEqual([path.name for path, _ in named], ["b.py"])
        self.assertEqual(self.index.parses, 1)
        # A lookalike spelling is the same identifier once Python normalizes it, so it is read too.
        self.write("pkg/d.py", "\uff36ALUE = 4\n")
        named = dict(self.index.modules(self.root, naming=("VALUE",)))
        self.assertEqual(sorted(path.name for path in named), ["b.py", "d.py"])
        self.assertEqual(named[self.root / "pkg" / "d.py"].body[0].targets[0].id, "VALUE")  # type: ignore[attr-defined]
        self.assertEqual(self.index.parses, 2)
        # And the full read still answers every file, from the trees already held.
        self.assertEqual(len(self.index.modules(self.root)), 4)
        self.assertEqual(self.index.parses, 4)

    def test_a_one_pass_parse_keeps_nothing(self) -> None:
        source = "import json\n"
        tree = self.index.parse(source, retain=False)
        self.assertIsNot(self.index.parse(source, retain=False), tree)
        self.assertEqual(self.index.parses, 2)
        # Its walk is not cached either, so nothing keeps the tree alive behind the index's back.
        self.assertIsNot(self.index.walk(tree), self.index.walk(tree))
        retained = self.index.parse(source)
        self.assertIs(self.index.parse(source), retained)
        self.assertEqual(self.index.parses, 3)

    def test_a_syntax_error_is_never_cached(self) -> None:
        for _ in range(2):
            with self.assertRaises(SyntaxError):
                self.index.parse("def broken(:\n", filename="broken.py")
        self.assertEqual(self.index.parses, 0)


if __name__ == "__main__":
    unittest.main()
