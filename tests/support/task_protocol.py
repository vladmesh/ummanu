"""Bounded source inventory for the six task protocol files, using the shared AST index."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

from tests import source_trees


def digest(node: ast.AST) -> str:
    return hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()


def definitions(source: str, filename: str) -> dict[str, ast.AST]:
    tree = source_trees.parse(source, filename=filename)
    result = {}
    def bind(name: str, node: ast.AST) -> None:
        if name in result:
            raise AssertionError(f"duplicate task implementation: {name}: {filename}")
        result[name] = node
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            if isinstance(node, ast.ClassDef) and node.name in {
                "TaskReader", "TaskWriter", "DispatcherTaskWrites", "SprintGuardMixin", "RecoveryMixin",
            }:
                prefix = "TaskReader" if node.name == "TaskReader" else "TaskWriter"
                for method in node.body:
                    if isinstance(method, ast.FunctionDef):
                        bind(f"{prefix}.{method.name}", method)
            else:
                bind(node.name, node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for name in source_trees.walk(node):
                if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Store):
                    bind(name.id, node)
    return result


def inventory(sources: dict[str, str]) -> dict[str, tuple[str, ast.AST]]:
    result = {}
    for path, source in sources.items():
        for name, node in definitions(source, path).items():
            if name in result:
                raise AssertionError(f"duplicate task implementation: {name}: {result[name][0]}, {path}")
            result[name] = (path, node)
    return result


def file_limits(sources: dict[str, str]) -> list[str]:
    # Count newline bytes, the wc -l contract, including a missing final newline.
    return [f"{path}: {source.count(chr(10))} > 3000" for path, source in sources.items()
            if source.count("\n") > 3000]


def read_sources(root: Path, paths: list[str]) -> dict[str, str]:
    return {path: (root / path).read_text() for path in paths}
