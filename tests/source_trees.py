"""Parsed Python sources shared by the repository-wide static checks of one test module.

Several unit checks enforce a rule over every module under `src/`, `tests/` or `scripts/`. Each
check used to parse and walk the whole tree again (about 700 files and 2.3 million AST nodes), and
under the coverage tracer of the CI `unit` job those repeated walks were the largest single cost of
the suite: about 100 s of its 420 s.

A source text maps to one tree, so the tree and its node order are computed once per distinct text
and reused by every check in the module. Callers treat the returned trees as read-only.

The cached trees are millions of long-lived container objects. Left in the collector's generations,
each full collection would traverse all of them again, which costs more than the reparsing saved.
So every newly cached tree is moved to the permanent generation (`gc.freeze`) and `clear()`, called
from the module's `tearDownModule`, returns everything to normal collection before the next module.
"""

from __future__ import annotations

import ast
import gc
from collections import deque

_trees: dict[str, ast.Module] = {}
_walks: dict[int, tuple[ast.AST, ...]] = {}
_parents: dict[int, dict[int, ast.AST]] = {}


def parse(source: str, filename: str = "<unknown>") -> ast.Module:
    """`ast.parse(source, filename=filename)`, once per distinct text.

    The filename only labels a `SyntaxError`, which is never cached, so it is not part of the key.
    """
    tree = _trees.get(source)
    if tree is None:
        tree = ast.parse(source, filename=filename)
        _trees[source] = tree
        gc.freeze()
    return tree


def walk(tree: ast.AST) -> tuple[ast.AST, ...]:
    """`tuple(ast.walk(tree))`, in the same order, once per cached tree."""
    nodes = _walks.get(id(tree))
    if nodes is None or nodes[0] is not tree:
        nodes = tuple(ast.walk(tree))
        _walks[id(tree)] = nodes
    return nodes


def parents(tree: ast.AST) -> dict[int, ast.AST]:
    """`id(child) -> parent` for every node of `tree`, once per cached tree. Do not mutate it."""
    found = _parents.get(id(tree))
    if found is None or _walks.get(id(tree), (None,))[0] is not tree:
        found = {id(child): node for node in walk(tree) for child in ast.iter_child_nodes(node)}
        _parents[id(tree)] = found
    return found


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


def clear() -> None:
    """Drop every cached tree and give the frozen objects back to the collector."""
    _parents.clear()
    _walks.clear()
    _trees.clear()
    gc.unfreeze()
