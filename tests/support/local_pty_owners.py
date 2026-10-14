"""Known backend source owners; the substrate and resolver keep their own boundaries."""

from __future__ import annotations

import ast
from pathlib import Path

from tests import config_dependencies, source_trees

OWNERS = (
    "local_pty_head",
    "local_pty_handoff",
    "local_pty_delivery",
    "local_pty_durable",
    "local_pty_journal",
    "local_pty_inspection",
    "local_pty_values",
)
CLASS_OWNER = "src/ummanu/runtime/local_pty_head.py"
BACKEND_PATHS = frozenset(f"src/ummanu/runtime/{owner}.py" for owner in OWNERS)
SUBSTRATE = "ummanu.runtime.head.local_pty"


def sources(root: Path) -> dict[str, str]:
    return {owner: (root / f"src/ummanu/runtime/{owner}.py").read_text() for owner in OWNERS}


def dependencies(owner: str, source: str) -> set[str]:
    return config_dependencies.dependencies(
        f"ummanu.runtime.{owner}",
        Path(owner + ".py"),
        source,
        strict_dynamic=False,
    )


def inventory(sources: dict[str, str]) -> dict[str, str]:
    result = {}
    for owner, source in sources.items():
        for node in source_trees.parse(source, filename=owner).body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                names = [node.name]
                if node.name in {"LocalPtyHeadRuntime", "PromptDeliveryMixin", "DeliveryMixin"}:
                    names += [
                        "runtime." + child.name for child in node.body if isinstance(child, ast.FunctionDef)
                    ]
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = [target.id for target in targets if isinstance(target, ast.Name)]
            else:
                continue
            for name in names:
                if name in result:
                    raise AssertionError(f"duplicate backend definition: {name}: {result[name]}, {owner}")
                result[name] = owner
    return result


def oversized(sources: dict[str, str]) -> list[str]:
    return [owner for owner, source in sources.items() if source.count("\n") > 2000]


def substrate_offenders(sources: dict[str, str]) -> list[str]:
    return sorted(
        path
        for path, source in sources.items()
        if path not in BACKEND_PATHS | {"src/ummanu/po/runner.py"}
        and not path.startswith("src/ummanu/runtime/head/local_pty/")
        and any(
            target == SUBSTRATE or target.startswith(SUBSTRATE + ".")
            for target in config_dependencies.dependencies(
                ".".join(Path(path).with_suffix("").parts[1:]).removesuffix(".__init__"),
                Path(path), source, strict_dynamic=False,
            )
        )
    )


def construction_sites(sources: dict[str, str]) -> dict[str, set[str]]:
    sites = {}
    for path, source in sources.items():
        if path == CLASS_OWNER:
            continue
        tree = source_trees.parse(source, filename=path)
        for holder in source_trees.walk(tree):
            if not isinstance(holder, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(holder):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id.endswith("HeadRuntime")
                ):
                    sites.setdefault(holder.name, set()).add(node.func.id)
    return sites
