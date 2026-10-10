"""Bounded, import-free CI impact analysis and reproducible execution plans.

Only Python under src/ummanu and tests has a static import contract. Opaque
consumers (source readers, subprocesses, dynamic loading) conservatively depend
on every Python module. Changing an opaque module itself requires the full run.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path

MAX_PLAN_BYTES = 1_000_000
MAX_FILES = 4000
MAX_SOURCE_BYTES = 30_000_000
OPAQUE_CALLS = {"exec", "eval", "spec_from_file_location", "run_module", "run_path",
                "read_text", "read_bytes", "open", "getsource", "source_trees",
                "Popen", "run", "call", "check_output", "check_call", "system", "entry_points"}
DYNAMIC_CALLS = {"import_module", "__import__"}


def call_identities(tree: ast.AST, package: str) -> dict[int, tuple[str | None, set[str]]]:
    """Prove immutable import bindings, not receiver types or runtime values.

    Bindings are lexical and order-independent: a second binding anywhere in a
    scope refuses proof, even if it precedes the import. Only direct scope-body
    imports prove identity. Alias spellings remain conservative after rebinding.
    """
    calls = []
    aliases = {}
    imported = {}
    assignments = []
    invalid = set()
    wildcard = False

    def leaves(node):
        # Identity belongs to the callable's terminal name, not to identifiers
        # used as receivers/data (a record named run does not execute code).
        if isinstance(node, ast.Name):
            return {node.id}
        if isinstance(node, ast.Attribute):
            return {node.attr}
        if isinstance(node, ast.Call):
            return set()  # nested calls are collected independently
        return set().union(*(leaves(child) for child in ast.iter_child_nodes(node)))

    class Collector(ast.NodeVisitor):
        def __init__(self):
            self.scope = None

        def bind(self, name, identity=None):
            self.scope["bindings"].setdefault(name, []).append(identity)
            if identity:
                imported.setdefault(name, set()).add(identity)

        def body(self, node, body, arguments=None):
            parent = self.scope
            closure = parent
            while closure and closure["class"]:
                closure = closure["parent"]
            self.scope = {"bindings": {}, "parent": closure,
                          "class": isinstance(node, ast.ClassDef), "imports": set()}
            self.scope["imports"] = {id(n) for n in body if isinstance(n, (ast.Import, ast.ImportFrom))}
            if arguments:
                for arg in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs):
                    self.bind(arg.arg)
                for arg in (arguments.vararg, arguments.kwarg):
                    if arg:
                        self.bind(arg.arg)
            for item in body:
                self.visit(item)
            self.scope = parent

        def visit_Module(self, node):
            self.body(node, node.body)

        def visit_FunctionDef(self, node):
            self.bind(node.name)
            for item in (*node.decorator_list, *node.args.defaults,
                         *filter(None, node.args.kw_defaults),
                         *[arg.annotation for arg in ast.walk(node.args)
                           if isinstance(arg, ast.arg) and arg.annotation],
                         *([node.returns] if node.returns else []),
                         *node.type_params):
                self.visit(item)
            self.body(node, node.body, node.args)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Lambda(self, node):
            for item in (*node.args.defaults, *filter(None, node.args.kw_defaults)):
                self.visit(item)
            self.body(node, [node.body], node.args)

        def visit_ClassDef(self, node):
            self.bind(node.name)
            for item in (*node.bases, *node.keywords, *node.decorator_list, *node.type_params):
                self.visit(item)
            self.body(node, node.body)

        def visit_Import(self, node):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                target = alias.name if alias.asname else bound
                self.bind(bound, target if id(node) in self.scope["imports"] else None)
                imported.setdefault(bound, set()).add(target)
                aliases.setdefault(bound, set()).add(alias.name.rsplit(".", 1)[-1])

        def visit_ImportFrom(self, node):
            nonlocal wildcard
            prefix = node.module or ""
            if node.level:
                parts = package.split(".")
                prefix = ".".join(parts[:len(parts) - node.level + 1] + ([prefix] if prefix else []))
            for alias in node.names:
                if alias.name == "*":
                    wildcard = True
                    continue
                bound = alias.asname or alias.name
                self.bind(bound, f"{prefix}.{alias.name}" if id(node) in self.scope["imports"] else None)
                imported.setdefault(bound, set()).add(f"{prefix}.{alias.name}")
                aliases.setdefault(bound, set()).add(alias.name)

        def visit_Name(self, node):
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                self.bind(node.id)

        def visit_Assign(self, node):
            assignments.extend((target, node.value) for target in node.targets)
            self.generic_visit(node)

        def visit_AnnAssign(self, node):
            if node.value:
                assignments.append((node.target, node.value))
            self.generic_visit(node)

        def visit_NamedExpr(self, node):
            # A comprehension walrus can write into the enclosing scope.
            self.invalidate_receiver(node.target)
            self.visit_AnnAssign(node)

        def visit_TypeVar(self, node):
            invalid.add(node.name)
            self.generic_visit(node)

        visit_ParamSpec = visit_TypeVar
        visit_TypeVarTuple = visit_TypeVar

        def visit_Global(self, node):
            invalid.update(node.names)

        visit_Nonlocal = visit_Global

        def visit_ExceptHandler(self, node):
            if node.name:
                self.bind(node.name)
            self.generic_visit(node)

        def visit_MatchAs(self, node):
            if node.name:
                self.bind(node.name)
            self.generic_visit(node)

        visit_MatchStar = visit_MatchAs

        def visit_MatchMapping(self, node):
            if node.rest:
                self.bind(node.rest)
            self.generic_visit(node)

        def invalidate_receiver(self, node):
            invalid.update(item.id for item in ast.walk(node) if isinstance(item, ast.Name))

        def visit_Attribute(self, node):
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                self.invalidate_receiver(node)
            self.generic_visit(node)

        def visit_Call(self, node):
            calls.append((node, self.scope))
            if isinstance(node.func, ast.Name) and node.func.id in {"setattr", "delattr"} and node.args:
                self.invalidate_receiver(node.args[0])
            self.generic_visit(node)

        def visit_ListComp(self, node):
            # Only the first iterable executes in the enclosing namespace;
            # subsequent iterables/filters and elements use the comprehension.
            first, *remaining = node.generators
            self.visit(first.iter)
            body = [first.target, *first.ifs]
            for generator in remaining:
                body.extend([generator.iter, generator.target, *generator.ifs])
            body.extend([node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt])
            self.body(node, body)

        visit_SetComp = visit_ListComp
        visit_DictComp = visit_ListComp
        visit_GeneratorExp = visit_ListComp

    Collector().visit(tree)
    # Value aliases are never proof, but must not hide an opaque API's spelling.
    while True:
        previous = {key: set(value) for key, value in aliases.items()}
        previous_invalid = set(invalid)
        for target, value in assignments:
            names = leaves(value)
            possible = names & (OPAQUE_CALLS | DYNAMIC_CALLS)
            for leaf in names:
                possible.update(aliases.get(leaf, set()) & (OPAQUE_CALLS | DYNAMIC_CALLS))
            for item in ast.walk(target):
                if isinstance(item, ast.Name):
                    aliases.setdefault(item.id, set()).update(possible)
                    if item.id in invalid:
                        # A write through a value alias can mutate the imported
                        # namespace too. Refuse all spellings of that namespace.
                        invalid.update(n.id for n in ast.walk(value) if isinstance(n, ast.Name))
        if aliases == previous and invalid == previous_invalid:
            break
    invalid_imports = {identity for bound in invalid for identity in imported.get(bound, set())}

    def resolve(node, scope):
        if isinstance(node, ast.Attribute):
            parent = resolve(node.value, scope)
            return f"{parent}.{node.attr}" if parent else None
        if not isinstance(node, ast.Name) or node.id in invalid or wildcard:
            return None
        while scope:
            bindings = scope["bindings"].get(node.id)
            if bindings is not None:
                identity = bindings[0] if len(bindings) == 1 else None
                if identity and any(identity == target or identity.startswith(target + ".")
                                    or target.startswith(identity + ".") for target in invalid_imports):
                    return None
                return identity
            scope = scope["parent"]
        return f"builtins.{node.id}" if node.id in {"__import__", "exec", "eval", "open"} else None

    result = {}
    for call, scope in calls:
        func = call.func
        names = leaves(func)
        possible = set(names)
        for leaf in names:
            possible.update(aliases.get(leaf, set()))
        result[id(call)] = resolve(func, scope), possible
    return result


class SelectionError(ValueError):
    pass


def json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SelectionError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def git_bytes(root: Path, *args: str, input_data: bytes | None = None) -> bytes:
    try:
        return subprocess.run(["git", *args], cwd=root, input=input_data,
                              capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SelectionError(f"git {args[0]} unavailable") from exc


def exact_sha(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40,64}", value):
        raise SelectionError("selection requires exact SHAs")
    return value


def changed_paths(data: bytes) -> list[tuple[str, tuple[str, ...]]]:
    """Parse --name-status -z, preserving both rename/copy paths and spaces."""
    parts = data.split(b"\0")
    if parts.pop() != b"":
        raise SelectionError("unterminated diff")
    changes = []
    while parts:
        status = parts.pop(0).decode("ascii")
        if not re.fullmatch(r"[AMDT]|[RC][0-9]+", status):
            raise SelectionError("unsupported diff status")
        count = 2 if status[0] in "RC" else 1
        if len(parts) < count:
            raise SelectionError("incomplete diff")
        paths = tuple(parts.pop(0).decode("utf-8") for _ in range(count))
        if any(not p or p.startswith("/") or ".." in p.split("/") for p in paths):
            raise SelectionError("unsafe diff path")
        changes.append((status, paths))
    return changes


def documentation(path: str) -> bool:
    # Executable/config files under docs still affect infrastructure.
    return path.endswith(".md") or (
        path.startswith("docs/") and path.endswith((".rst", ".txt", ".png", ".jpg", ".svg"))
    )


def module_name(path: str) -> str | None:
    if not path.endswith(".py") or not path.startswith(("src/ummanu/", "tests/")):
        return None
    name = path.removeprefix("src/").removesuffix(".py").replace("/", ".")
    name = name.removesuffix(".__init__")
    if not all(re.fullmatch(r"[A-Za-z0-9_]+", part) for part in name.split(".")):
        raise SelectionError(f"ambiguous module path: {path}")
    return name


def snapshot(root: Path, sha: str) -> dict[str, str]:
    entries = git_bytes(root, "ls-tree", "-rlz", sha, "--", "src/ummanu", "tests").split(b"\0")
    objects = []
    total_size = 0
    for entry in filter(None, entries):
        metadata, raw_path = entry.split(b"\t", 1)
        path = raw_path.decode("utf-8")
        if not path.endswith(".py"):
            continue
        mode, kind, oid, raw_size = metadata.split()
        if mode not in {b"100644", b"100755"} or kind != b"blob":
            raise SelectionError(f"unsafe Python object: {path}")
        objects.append((path, oid))
        total_size += int(raw_size)
    if len(objects) > MAX_FILES:
        raise SelectionError("source file bound exceeded")
    if total_size > MAX_SOURCE_BYTES:
        raise SelectionError("source byte bound exceeded")
    payload = git_bytes(root, "cat-file", "--batch",
                        input_data=b"".join(oid + b"\n" for _, oid in objects))
    result, offset, total = {}, 0, 0
    for path, oid in objects:
        end = payload.index(b"\n", offset)
        header = payload[offset:end].split()
        if header[:2] != [oid, b"blob"]:
            raise SelectionError("unexpected source object")
        size = int(header[2])
        total += size
        if total > MAX_SOURCE_BYTES:
            raise SelectionError("source byte bound exceeded")
        offset = end + 1
        result[path] = payload[offset:offset + size].decode("utf-8")
        offset += size + 1
    return result


def import_graph(sources: dict[str, str]) -> tuple[dict[str, set[str]], set[str]]:
    """Every edge goes from a consumer to a dependency; no analyzed code executes."""
    by_name = {}
    for path in sources:
        name = module_name(path)
        if name in by_name:
            raise SelectionError(f"ambiguous module identity: {name}")
        by_name[name] = path
    graph = {name: set() for name in by_name}
    for name in by_name:
        parts = name.split(".")
        for n in range(1, len(parts)):
            graph.setdefault(".".join(parts[:n]), set())
    opaque = set()

    def add(consumer, target):
        parts = target.split(".")
        for n in range(1, len(parts) + 1):
            ancestor = ".".join(parts[:n])
            if ancestor in graph and ancestor != consumer:
                graph[consumer].add(ancestor)

    for name, path in by_name.items():
        tree = ast.parse(sources[path], filename=path)
        nodes = tuple(ast.walk(tree))
        # Documentation is not an executable patch/import target. In particular,
        # test package documentation must not turn every test into a consumer.
        docstrings = {id(item.body[0].value) for item in nodes
                      if isinstance(item, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                      and item.body and isinstance(item.body[0], ast.Expr)
                      and isinstance(item.body[0].value, ast.Constant)
                      and isinstance(item.body[0].value.value, str)}
        package = name if path.endswith("/__init__.py") else name.rpartition(".")[0]
        identities = call_identities(tree, package)
        for node in nodes:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(("ummanu.", "tests.")) and alias.name not in graph:
                        raise SelectionError(f"unresolved internal import: {alias.name}")
                    add(name, alias.name)
            elif isinstance(node, ast.ImportFrom):
                prefix = node.module or ""
                if node.level:
                    components = package.split(".")
                    if node.level > len(components):
                        raise SelectionError(f"invalid relative import: {path}")
                    prefix = ".".join(components[:len(components) - node.level + 1]
                                      + ([prefix] if prefix else []))
                add(name, prefix)
                if prefix.startswith(("ummanu.", "tests.")) and prefix not in graph:
                    raise SelectionError(f"unresolved internal import: {prefix}")
                for alias in node.names:
                    if alias.name == "*":
                        opaque.add(name)
                    else:
                        add(name, f"{prefix}.{alias.name}")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                # Strings used for patch targets and literal dynamic imports are dependencies.
                for target in re.findall(r"(?:ummanu|tests)(?:\.[A-Za-z0-9_]+)+", node.value):
                    add(name, target)
            elif isinstance(node, ast.Call):
                identity, possible = identities[id(node)]
                # mock.call constructs an expectation record; it does not invoke
                # its arguments. This exception is about identity, not spelling.
                if identity == "unittest.mock.call":
                    continue
                if identity in {"importlib.import_module", "builtins.__import__"}:
                    possible = {identity.rsplit(".", 1)[-1]}
                elif identity:
                    possible.add(identity.rsplit(".", 1)[-1])
                if possible & DYNAMIC_CALLS:
                    levels = ([node.args[4]] if len(node.args) > 4 else []) + [
                        item.value for item in node.keywords if item.arg == "level"]
                    fromlists = ([node.args[3]] if len(node.args) > 3 else []) + [
                        item.value for item in node.keywords if item.arg == "fromlist"]
                    if (identity in {"importlib.import_module", "builtins.__import__"}
                            and node.args and isinstance(node.args[0], ast.Constant)
                            and isinstance(node.args[0].value, str)
                            and not node.args[0].value.startswith(".")
                            and not any(isinstance(arg, ast.Starred) for arg in node.args)
                            and not any(item.arg is None for item in node.keywords)
                            and (identity != "builtins.__import__" or all(
                                isinstance(level, ast.Constant) and level.value == 0 for level in levels))
                            and (identity != "builtins.__import__" or all(
                                isinstance(items, (ast.List, ast.Tuple)) and not items.elts
                                for items in fromlists))):
                        add(name, node.args[0].value)
                    else:
                        opaque.add(name)
                # These consumers can inspect or execute code without a Python import edge.
                # asyncio.run remains opaque: import identity alone cannot prove
                # the origin or execution dependencies of an arbitrary awaitable.
                if possible & OPAQUE_CALLS:
                    opaque.add(name)
        add(name, package)
    # Unknown dependencies are universal, never silently absent from the graph.
    for name in opaque:
        dependencies = set(graph) if name.startswith("tests.") else {
            target for target in graph if target == "ummanu" or target.startswith("ummanu.")}
        graph[name].update(dependencies - {name})
    return graph, opaque


def select(grouped: dict[str, list[str]], changes: list[tuple[str, tuple[str, ...]]],
           base_sources: dict[str, str], candidate_sources: dict[str, str]) -> tuple[str, list[str], dict[str, list[str]], dict[str, list[str]]]:
    all_paths = [path for paths in grouped.values() for path in paths]

    def full(reason):
        return "full", [reason], {s: list(p) for s, p in grouped.items()}, {}

    paths = {p for _, items in changes for p in items}
    if not paths:
        return full("empty diff")
    if all(documentation(p) for p in paths):
        return "docs-only", ["all old and new paths are documentation"], {}, {}
    code = {p for p in paths if not documentation(p)}
    if any(module_name(p) is None for p in code):
        return full("unknown path or shared infrastructure/configuration change")
    if any(status[0] in "DRT" for status, items in changes if any(p in code for p in items)):
        return full("removed, renamed or type-changed Python topology")
    try:
        old_graph, old_opaque = import_graph(base_sources)
        new_graph, new_opaque = ((old_graph, old_opaque) if base_sources is candidate_sources
                                 else import_graph(candidate_sources))
    except (ValueError, SyntaxError, UnicodeError) as exc:
        return full(f"unsafe analysis: {exc}")
    changed = {module_name(p) for p in code}
    if changed & (old_opaque | new_opaque):
        return full("changed code has opaque runtime/source dependencies")
    graph = {name: old_graph.get(name, set()) | new_graph.get(name, set())
             for name in old_graph.keys() | new_graph.keys()}
    affected = set(changed)
    while True:
        consumers = {n for n, dependencies in graph.items() if dependencies & affected}
        if consumers <= affected:
            break
        affected.update(consumers)
    selected = {s: [p for p in owned if module_name(p) in affected] for s, owned in grouped.items()}
    selected = {s: owned for s, owned in selected.items() if owned}
    if not selected:
        return full("no test consumer could be established")
    explanations = {}
    for path in all_paths:
        name = module_name(path)
        if name not in affected:
            continue
        explanations[path] = (["changed test"] if name in changed else
                              ["conservative opaque consumer"] if name in old_opaque | new_opaque else
                              [f"consumes {dep}" for dep in sorted(graph[name] & affected)])
    return "affected", ["base/candidate import closure including conservative opaque consumers"], selected, explanations


def build_plan(root: Path, grouped: dict[str, list[str]], candidate_sha: str,
               base_sha: str, event: str, ref: str) -> dict:
    exact_sha(candidate_sha)
    if git_bytes(root, "rev-parse", "HEAD").decode().strip() != candidate_sha:
        raise SelectionError("candidate checkout mismatch")
    tree = git_bytes(root, "rev-parse", f"{candidate_sha}^{{tree}}").decode().strip()
    manifest_bytes = (root / "tests/ci-shards.txt").read_bytes()
    if manifest_bytes != git_bytes(root, "show", f"{candidate_sha}:tests/ci-shards.txt"):
        raise SelectionError("manifest differs from candidate tree")
    mode, reasons, selected, explanations = "full", ["non-PR event requires full profile"], grouped, {}
    merge_base = None
    if event == "pull_request":
        try:
            exact_sha(base_sha)
            merge_base = git_bytes(root, "merge-base", base_sha, candidate_sha).decode().strip()
            exact_sha(merge_base)
            changes = changed_paths(git_bytes(root, "diff", "--name-status", "-z", "--find-renames",
                                              merge_base, candidate_sha, "--"))
            # Docs classification needs only diff evidence; source analysis is CI-only static work.
            code_changes = [(status, items) for status, items in changes
                            if any(not documentation(p) for p in items)]
            if (not code_changes or any(module_name(p) is None for _, items in code_changes
                                       for p in items if not documentation(p))
                    or any(status[0] in "DRT" for status, _ in code_changes)):
                mode, reasons, selected, explanations = select(grouped, changes, {}, {})
            else:
                mode, reasons, selected, explanations = select(
                    grouped, changes, snapshot(root, merge_base), snapshot(root, candidate_sha))
        except (SelectionError, UnicodeError, ValueError) as exc:
            reasons = [f"full fallback: {exc}"]
    elif event not in {"push", "workflow_dispatch"}:
        raise SelectionError("unsupported event")
    return {"schema_version": 1, "candidate_sha": candidate_sha, "candidate_tree": tree,
            "base_sha": base_sha, "merge_base": merge_base, "event": event, "ref": ref,
            "mode": mode, "reasons": reasons, "selected": selected, "explanations": explanations,
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "validation": {"typecheck": mode != "docs-only", "lint": mode != "docs-only"}}


def plan_digest(plan: dict) -> str:
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def read_plan(root: Path, path: Path, grouped: dict[str, list[str]], *, candidate_sha: str,
              base_sha: str, event: str, ref: str) -> dict:
    if path.stat().st_size > MAX_PLAN_BYTES:
        raise SelectionError("selection plan exceeds bound")
    plan = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=json_object)
    expected = build_plan(root, grouped, candidate_sha, base_sha, event, ref)
    if plan != expected:
        raise SelectionError("selection plan differs from exact event/base/candidate analysis")
    return plan


def summary(plan: dict) -> str:
    lines = ["## CI selection", "", *[f"- {key}: `{plan[key]}`" for key in
             ("candidate_sha", "candidate_tree", "base_sha", "merge_base", "event", "ref", "mode")],
             f"- Plan SHA-256: `{plan_digest(plan)}`", *[f"- Reason: {r}" for r in plan["reasons"]]]
    for suite, paths in plan["selected"].items():
        lines.append(f"- Suite `{suite}`: {len(paths)} modules")
        for path in paths:
            lines.append(f"  - `{path}`: {'; '.join(plan['explanations'].get(path, ['full profile']))}")
    if not plan["selected"]:
        lines.append("- No test suites applicable; validated documentation diff")
    return "\n".join(lines) + "\n"


def validation_result(plan: dict, results: dict[str, str]) -> bool:
    required = {"selection": True, "test_suites": bool(plan["selected"]), **plan["validation"]}
    return set(results) == set(required) and all(
        results[job] == ("success" if applicable else "skipped") for job, applicable in required.items())
