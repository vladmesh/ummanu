"""Bounded current-owner inspection, without a permanent historical source pin."""

from __future__ import annotations

import ast
from pathlib import Path

from tests import config_dependencies, source_trees

OWNERS = ("sprints", "board/sprint_protocol/lifecycle", "board/sprint_protocol/shared", "board/sprint_protocol/reservations")


def sources(root: Path) -> dict[str, str]:
    return {name: (root / f"src/ummanu/{name}.py").read_text() for name in OWNERS}


def inventory(sources: dict[str, str]) -> dict[str, tuple[str, ast.AST]]:
    result = {}
    for owner, source in sources.items():
        for node in source_trees.parse(source, filename=owner).body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                names = [node.name]
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = [n.id for target in targets for n in source_trees.walk(target) if isinstance(n, ast.Name)]
            else:
                continue
            for name in names:
                if name in result:
                    raise AssertionError(f"duplicate sprint implementation: {name}: {result[name][0]}, {owner}")
                result[name] = owner, node
    methods = {}
    for owner, node in result.values():
        if isinstance(node, ast.ClassDef) and node.name in {"SprintWriter", "SprintLifecycleMixin"}:
            for method in node.body:
                if isinstance(method, ast.FunctionDef):
                    if method.name in methods:
                        raise AssertionError(f"duplicate writer method: {method.name}")
                    methods[method.name] = owner
    return result


def file_limits(sources: dict[str, str]) -> list[str]:
    return [name for name, source in sources.items() if source.count("\n") > 3000]


def facade_dependencies(owner: str, source: str) -> set[str]:
    targets = config_dependencies.dependencies(f"ummanu.{owner.replace('/', '.')}", Path(owner + ".py"), source, strict_dynamic=False)
    return {target for target in targets if target == "ummanu.sprints" or target.startswith("ummanu.sprints.")}
