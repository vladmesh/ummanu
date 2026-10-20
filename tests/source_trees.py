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
  themselves) parses with `retain=False` and leaves nothing behind;
* a check that looks only for nodes naming one identifier asks `modules(root, naming=...)`, and the
  files that cannot spell it are not parsed at all.

The cached trees are millions of long-lived container objects. Left in the collector's generations,
each full collection would traverse all of them again, which costs more than the reparsing saved.
So the shared index moves each tree to the permanent generation (`gc.freeze`) as it is parsed,
before the collector can run again, and each walk and parent map as it is cached; `clear()` returns
everything to normal collection.
"""

from __future__ import annotations

import ast
import gc
import re
import unicodedata
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
        return self._parse(source, filename, retain=retain)

    def modules(self, root: Path, *, naming: tuple[str, ...] = ()) -> tuple[tuple[Path, ast.Module], ...]:
        """Every `*.py` under `root`, sorted, with its tree. Read on every call, parsed once per text.

        `naming` narrows the answer to the files that can hold a node naming one of those identifier
        fragments, for a check that only ever looks for such a node: a file whose text spells none of
        them is skipped unparsed. See :func:`may_name`.
        """
        found = []
        for path in sorted(root.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            if naming and not may_name(text, naming):
                continue
            found.append((path, self._parse(text, str(path), retain=True)))
        return tuple(found)

    def walk(self, tree: ast.AST) -> tuple[ast.AST, ...]:
        """`tuple(ast.walk(tree))`, in the same order, once per retained tree."""
        nodes = self._walks.get(id(tree))
        if nodes is None:
            nodes = _breadth_first(tree)
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

    def _parse(self, source: str, filename: str, *, retain: bool) -> ast.Module:
        tree = self._trees.get(source)
        if tree is not None:
            return tree
        # A tree holds no cycle, so the collector has nothing to find while it is being built; left
        # on, it would traverse the half-built tree, and the whole heap behind it, over and over.
        enabled = gc.isenabled()
        gc.disable()
        try:
            tree = ast.parse(source, filename=filename)
            self.parses += 1
            if retain:
                self._trees[source] = tree
                self._retained_ids.add(id(tree))
                self._frozen()
        finally:
            if enabled:
                gc.enable()
        return tree

    def _frozen(self) -> None:
        if self._freeze:
            gc.freeze()


def _breadth_first(tree: ast.AST) -> tuple[ast.AST, ...]:
    """The nodes of `ast.walk(tree)`, in its order, without a generator per node."""
    nodes = [tree]
    for node in nodes:
        for field in node._fields:
            value = getattr(node, field)
            if isinstance(value, ast.AST):
                nodes.append(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, ast.AST):
                        nodes.append(item)
    return tuple(nodes)


def may_name(text: str, fragments: tuple[str, ...], *, whole: bool = False) -> bool:
    """Whether `text` could parse to an identifier that contains one of `fragments`, or with `whole`,
    to a name or dotted part of one that is exactly one of them.

    An identifier, and so every dotted module name, is spelled out whole in the source: no
    continuation, string or comment splits one. Python normalizes identifiers with NFKC, so a
    non-ASCII text is also searched in that form, where a lookalike spelling would surface.
    """

    def spelled(candidate: str) -> bool:
        if whole:
            return any(re.search(rf"(?<!\w){re.escape(fragment)}(?!\w)", candidate) for fragment in fragments)
        return any(fragment in candidate for fragment in fragments)

    if spelled(text):
        return True
    if text.isascii():
        return False
    return spelled(unicodedata.normalize("NFKC", text))


def imports(tree: ast.AST) -> tuple[ast.Import | ast.ImportFrom, ...]:
    """All imports in statement bodies, including exception handlers and match cases.
    Expressions, decorators, arguments, annotations and patterns cannot contain imports.
    """
    pending = deque([tree])
    found = []
    while pending:
        node = pending.popleft()
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            found.append(node)
        for field, children in ast.iter_fields(node):
            if field in {"body", "orelse", "finalbody", "handlers", "cases"} and isinstance(children, list):
                pending.extend(children)
    return tuple(found)


#: The index every check of this process shares.
INDEX = SourceIndex()
parse = INDEX.parse
modules = INDEX.modules
walk = INDEX.walk
parents = INDEX.parents
clear = INDEX.clear
