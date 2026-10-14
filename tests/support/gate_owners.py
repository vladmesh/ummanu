"""Bounded mechanical gate ownership inspection, using the shared source index."""

from __future__ import annotations

import ast
from pathlib import Path

from tests import config_dependencies, source_trees

OWNERS = ("gate", "github_ci", "gate_pr", "gate_workflows", "gate_types")
CI_CONSUMERS = ("e2e", "e2e_stage", "e2e_after_merge", "post_merge", "wait_cards")


def sources(root: Path) -> dict[str, str]:
    return {name: (root / f"src/ummanu/dispatch/{name}.py").read_text() for name in OWNERS}


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
                    raise AssertionError(f"duplicate gate implementation: {name}: {result[name][0]}, {owner}")
                result[name] = (owner, node)
    return result


def file_limits(sources: dict[str, str]) -> list[str]:
    return [name for name, source in sources.items() if source.count("\n") > 3000]


def facade_dependencies(owner: str, source: str) -> set[str]:
    name = f"ummanu.dispatch.{owner}"
    targets = config_dependencies.dependencies(name, Path(owner + ".py"), source, strict_dynamic=False)
    return {target for target in targets if target == "ummanu.dispatch.gate" or target.startswith("ummanu.dispatch.gate.")}
