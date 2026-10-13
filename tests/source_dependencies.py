"""Uncut static source graph, using the existing source/closure capability.

Like coupling.py's uncut, module-level tier_none graph, this follows nested and
lazy imports, import aliases, strings (except docstrings), and every package
ancestor. It records source edges, not selector opacity's universal fallback or
runtime sys.modules. Unknown dynamic targets are not inferred; config's narrower
guard continues to refuse them. SCCs use all product modules, without layer cuts.
"""

from __future__ import annotations

from pathlib import Path

from tests import config_dependencies


def source_graph(sources: dict[str, str]) -> dict[str, set[str]]:
    by_name = {}
    for relative in sources:
        path = Path(relative)
        parts = path.with_suffix("").parts
        if parts[0] == "src":
            parts = parts[1:]
        if parts[0] != "ummanu":
            continue
        name = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
        if name in by_name:
            raise AssertionError(f"ambiguous module identity: {name}")
        by_name[name] = path
    graph: dict[str, set[str]] = {name: set() for name in by_name}
    for name in by_name:
        parts = name.split(".")
        for length in range(1, len(parts)):
            graph.setdefault(".".join(parts[:length]), set())
    for name, path in by_name.items():
        for target in config_dependencies.dependencies(name, path, sources[str(path)], strict_dynamic=False):
            parts = target.split(".")
            graph[name].update(
                ancestor for length in range(1, len(parts) + 1)
                if (ancestor := ".".join(parts[:length])) in graph and ancestor != name
            )
    return graph


def components(graph: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan's SCC traversal; deterministic membership for assertion diagnostics."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    active: set[str] = set()
    found: list[list[str]] = []

    def visit(name: str) -> None:
        index[name] = low[name] = len(index)
        stack.append(name)
        active.add(name)
        for target in sorted(graph[name]):
            if target not in index:
                visit(target)
                low[name] = min(low[name], low[target])
            elif target in active:
                low[name] = min(low[name], index[target])
        if low[name] == index[name]:
            group = []
            while True:
                member = stack.pop()
                active.remove(member)
                group.append(member)
                if member == name:
                    break
            found.append(sorted(group))

    for name in sorted(graph):
        if name not in index:
            visit(name)
    return sorted(found, key=lambda group: (-len(group), group))
