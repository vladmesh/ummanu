"""Reachable product import edges, using the shared source index without a whole-tree parse."""

from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path

from tests import source_trees

TARGET = re.compile(r"ummanu(?:\.[A-Za-z0-9_]+)+")


def dependencies(name: str, path: Path, source: str, *, strict_dynamic: bool = True) -> set[str]:
    tree = source_trees.parse(source, filename=str(path))
    nodes = source_trees.walk(tree)
    package = name if path.name == "__init__.py" else name.rpartition(".")[0]
    targets = {package} if package else set()
    aliases: dict[str, str] = {}
    for node in source_trees.imports(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                targets.add(alias.name)
                aliases[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
        else:
            prefix = node.module or ""
            if node.level:
                parts = package.split(".")
                prefix = ".".join(parts[: len(parts) - node.level + 1] + ([prefix] if prefix else []))
            targets.add(prefix)
            for alias in node.names:
                target = f"{prefix}.{alias.name}"
                targets.add(target)
                aliases[alias.asname or alias.name] = target

    def qualified(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return f"{qualified(node.value)}.{node.attr}"
        return ""

    docstrings = {
        id(node.body[0].value)
        for node in nodes
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    for node in nodes:
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            targets.update(TARGET.findall(node.value))
        elif isinstance(node, ast.Attribute):
            targets.add(qualified(node))
        elif isinstance(node, ast.Call) and qualified(node.func).rsplit(".", 1)[-1] in {
            "import_module",
            "__import__",
        }:
            if (
                not node.args
                or not isinstance(node.args[0], ast.Constant)
                or not isinstance(node.args[0].value, str)
            ):
                if strict_dynamic:
                    raise AssertionError(f"{name}:{node.lineno}: nonliteral dynamic import")
                continue
            target = node.args[0].value
            if target.startswith("."):
                supplied = node.args[1] if len(node.args) > 1 else next(
                    (keyword.value for keyword in node.keywords if keyword.arg == "package"), None
                )
                if supplied is not None and (
                    not isinstance(supplied, ast.Constant) or not isinstance(supplied.value, str)
                ):
                    if strict_dynamic:
                        raise AssertionError(f"{name}:{node.lineno}: nonliteral dynamic import package")
                    continue
                target = importlib.util.resolve_name(target, supplied.value if supplied is not None else package)
            targets.add(target)
    return {target for target in targets if target == "ummanu" or target.startswith("ummanu.")}


def closure(product: Path, start: str = "ummanu.config") -> set[str]:
    """Follow every reachable nested, relative, string and package edge; execute no product code."""
    reached: set[str] = set()
    pending = {start}
    while pending:
        name = pending.pop()
        if name in reached:
            continue
        path = product.parent.joinpath(*name.split("."))
        path = path / "__init__.py" if path.is_dir() else path.with_suffix(".py")
        if not path.is_file():
            continue  # A symbol on an imported module, whose module ancestors are also followed.
        reached.add(name)
        targets = dependencies(name, path, path.read_text(encoding="utf-8"))
        for target in targets:
            parts = target.split(".")
            pending.update(".".join(parts[:n]) for n in range(1, len(parts) + 1))
    return reached
