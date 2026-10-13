"""The one read-only index of parsed Python sources behind the repository-wide static checks.

Several checks enforce a rule over every module under `src/`, `tests/` or `scripts/`. Each check used
to parse and walk the whole tree again (about 700 files and 2.3 million AST nodes), and under the
coverage tracer of the CI `unit` job those repeated walks were the largest single cost of the suite:
about 100 s of its 420 s.

A source text maps to one tree, so the tree and its node order are computed once per distinct text
and reused by every check of the test process, across modules. Callers treat the returned trees as
read-only. The lifecycle is explicit:

* a tree stays in the index for the whole process: no module drops it on the way out;
* the key is the text, so a fixture that rewrites or swaps a file gets the new tree without a reset,
  and an unchanged file is never parsed twice;
* `clear()` is the only reset. A one-pass scan whose trees nobody reads again (the test files
  themselves) parses with `retain=False` and leaves nothing behind.

The cached trees are millions of long-lived container objects. Left in the collector's generations,
each full collection would traverse all of them again, which costs more than the reparsing saved.
So the shared index moves newly cached trees, walks and parent maps to the permanent generation
(`gc.freeze`), once per batch, and `clear()` returns everything to normal collection.
"""

from __future__ import annotations

import ast
import gc
from collections import deque
from pathlib import Path


class SourceIndex:
    """Trees by source text, with their walks and parent maps. `parses` counts every real parse."""

    def __init__(self, *, freeze: bool = True) -> None:
        self._freeze = freeze
        self._trees: dict[str, ast.Module] = {}
        # Only retained trees are cached by identity: they cannot die while the index holds them.
        self._retained_ids: set[int] = set()
        self._walks: dict[int, tuple[ast.AST, ...]] = {}
        self._parents: dict[int, dict[int, ast.AST]] = {}
        self.parses = 0

    def parse(self, source: str, filename: str = "<unknown>", *, retain: bool = True) -> ast.Module:
        """`ast.parse(source, filename=filename)`, once per distinct text while it is retained.

        The filename only labels a `SyntaxError`, which is never cached, so it is not part of the key.
        """
        tree, parsed = self._parse(source, filename, retain=retain)
        if parsed and retain:
            self._frozen()
        return tree

    def modules(self, root: Path) -> tuple[tuple[Path, ast.Module], ...]:
        """Every `*.py` under `root`, sorted, with its tree. Read on every call, parsed once per text."""
        found = []
        parsed = False
        for path in sorted(root.rglob("*.py")):
            tree, new = self._parse(path.read_text(encoding="utf-8"), str(path), retain=True)
            parsed = parsed or new
            found.append((path, tree))
        if parsed:
            self._frozen()
        return tuple(found)

    def walk(self, tree: ast.AST) -> tuple[ast.AST, ...]:
        """`tuple(ast.walk(tree))`, in the same order, once per retained tree."""
        nodes = self._walks.get(id(tree))
        if nodes is None:
            nodes = tuple(ast.walk(tree))
            if id(tree) in self._retained_ids:
                self._walks[id(tree)] = nodes
                self._frozen()
        return nodes

    def parents(self, tree: ast.AST) -> dict[int, ast.AST]:
        """`id(child) -> parent` for every node of `tree`, once per retained tree. Do not mutate it."""
        found = self._parents.get(id(tree))
        if found is None:
            found = {id(child): node for node in self.walk(tree) for child in ast.iter_child_nodes(node)}
            if id(tree) in self._retained_ids:
                self._parents[id(tree)] = found
                self._frozen()
        return found

    def clear(self) -> None:
        """Drop every cached tree and give the frozen objects back to the collector."""
        self._parents.clear()
        self._walks.clear()
        self._trees.clear()
        self._retained_ids.clear()
        if self._freeze:
            gc.unfreeze()

    def _parse(self, source: str, filename: str, *, retain: bool) -> tuple[ast.Module, bool]:
        tree = self._trees.get(source)
        if tree is not None:
            return tree, False
        tree = ast.parse(source, filename=filename)
        self.parses += 1
        if retain:
            self._trees[source] = tree
            self._retained_ids.add(id(tree))
        return tree, True

    def _frozen(self) -> None:
        if self._freeze:
            gc.freeze()


def imports(tree: ast.AST) -> tuple[ast.Import | ast.ImportFrom, ...]:
    """All import statements, without walking expression trees that cannot contain them.

    Python statement bodies include exception handlers and match cases. Imports cannot
    occur inside an expression, decorator, argument, annotation or pattern.
    """
    pending = deque([tree])
    found = []
    while pending:
        node = pending.popleft()
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            found.append(node)
        pending.extend(child for child in ast.iter_child_nodes(node)
                       if isinstance(child, (ast.stmt, ast.ExceptHandler, ast.match_case)))
    return tuple(found)


#: The index every check of this process shares.
INDEX = SourceIndex()
parse = INDEX.parse
modules = INDEX.modules
walk = INDEX.walk
parents = INDEX.parents
clear = INDEX.clear
