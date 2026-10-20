"""Executable boundaries for the incremental source-layout migration."""

from __future__ import annotations

import ast
import inspect
import unittest
from pathlib import Path
from unittest import mock

from tests import source_trees

ROOT = Path(__file__).resolve().parents[1]


# architecture-guard: most guards below scan every `src/` tree; built once, their cold cost is the module's
def setUpModule() -> None:
    for _path, tree in source_trees.modules(ROOT / "src"):
        source_trees.walk(tree)


# Existing flat modules may leave this set one feature at a time. New modules belong in one of the
# feature packages documented in ARCHITECTURE.md instead of making the root wider again.
LEGACY_FLAT_MODULES = frozenset(
    """
    __init__.py __main__.py _fsutil.py _proc.py backup.py
    backup_policy.py backup_retention.py backup_verify.py bootstrap.py
    broad_check.py candidate_history.py check_commands.py checkpoint.py cli.py cli_output.py
    codex_provider_events.py config.py data.py
    gate.py
    head_health.py head_registry.py host.py host_apply.py host_commands.py
    knowledge_write.py memory_errors.py memory_journal.py memory_reindex.py memory_service.py
    memory_write.py observer_root.py onboarding.py product_issue_commands.py product_issues.py
    product_lanes.py provision.py restore.py restore_commands.py role_skills.py
    routing_journal.py runtime_env.py secret_commands.py secret_recover.py secret_store.py
    secret_words.py session.py sprint_close.py sprint_commands.py sprint_observer.py sprints.py
    state_repo.py status.py task_commands.py task_restore.py tasks.py upgrade.py
    """.split()
)

# The background agents (curator, retro, steward) are `ummanu.automations`, built on top of the
# rest of `ummanu`: the package may import any `ummanu` module, and no other `ummanu` module
# imports it back. The one admitted edge is the `automations` subcommand of `ummanu.cli`, which
# hands its argv to the composition root through an import inside the handler, so no other command
# pays for the agents' wiring. The package was the top-level `triggered_agents` until sprint:1459;
# that name must not come back anywhere.
AUTOMATIONS_PACKAGE = "ummanu.automations"
AUTOMATIONS_ENTRY = "src/ummanu/cli.py"


def _absolute_imports(relative: str, source: str) -> list[tuple[ast.stmt, str, bool]]:
    """Every absolute import in one module: (node, imported module, whether at module level)."""
    tree = source_trees.parse(source, filename=relative)
    top_level = {id(node) for node in tree.body}
    found: list[tuple[ast.stmt, str, bool]] = []
    for node in source_trees.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node, alias.name, id(node) in top_level) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append((node, node.module, id(node) in top_level))
            found.extend((node, f"{node.module}.{alias.name}", id(node) in top_level) for alias in node.names)
    return found


def _names(module: str, package: str) -> bool:
    return module == package or module.startswith(package + ".")


# The dispatcher's state machine (`ummanu.dispatch.runtime`) calls each flow of a card through a
# module of its own; a flow module never imports the state machine back, so the flows stay
# testable without it and there is no cycle through the runtime class.
DISPATCH_STATE_MACHINE = "ummanu.dispatch.runtime"
DISPATCH_FLOW_MODULES = (
    "claim",
    "worker_launch",
    "worker_report",
    "worker_continuation",
    "gate_lifecycle",
    "review_verdict",
    "assessment_decision",
    "assessment_park",
    "release_lifecycle",
    "attempt_accounting",
    "wait_vitality",
)


def _imports_dispatch_state_machine(relative: str, source: str) -> list[str]:
    """Every import of `ummanu.dispatch.runtime` in one module of `ummanu.dispatch`, relative or not."""
    package = relative.removeprefix("src/").removesuffix(".py").split("/")[:-1]
    offenders: list[str] = []
    for node in source_trees.imports(source_trees.parse(source, filename=relative)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        else:
            base = package[: len(package) - node.level + 1] if node.level else []
            module = ".".join([*base, *([node.module] if node.module else [])])
            names = [module, *(f"{module}.{alias.name}" for alias in node.names)]
        if any(_names(name, DISPATCH_STATE_MACHINE) for name in names):
            offenders.append(f"{relative}:{node.lineno}")
    return offenders


def _imports_automations(relative: str, source: str) -> list[str]:
    """Every back edge from a `ummanu` module outside the agents into `ummanu.automations`."""
    if relative.startswith("src/ummanu/automations/"):
        return []
    offenders: set[str] = set()
    for node, module, top_level in _absolute_imports(relative, source):
        # `from ummanu import automations` names the package through its alias.
        if not _names(module, AUTOMATIONS_PACKAGE):
            continue
        if relative == AUTOMATIONS_ENTRY and not top_level:
            continue
        offenders.add(f"{relative}:{node.lineno}: {AUTOMATIONS_PACKAGE}")
    if relative.startswith("src/ummanu/"):
        tree = source_trees.parse(source, filename=relative)
        for node in source_trees.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.level
                and (
                    (node.module or "").split(".")[0] == "automations"
                    or any(alias.name == "automations" for alias in node.names)
                )
            ):
                offenders.add(f"{relative}:{node.lineno}: {AUTOMATIONS_PACKAGE}")
    return sorted(offenders)


def _module_of(relative: str) -> str:
    """`src/ummanu/automations/runtime/dispatch.py` -> `ummanu.automations.runtime.dispatch`."""
    parts = Path(relative).with_suffix("").parts
    parts = parts[1:] if parts and parts[0] == "src" else parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _imported_modules(relative: str, imports: tuple[ast.Import | ast.ImportFrom, ...]) -> list[tuple[int, str]]:
    """Every module an import statement names, relative ones resolved against the file's package."""
    module = _module_of(relative)
    package = module if relative.endswith("__init__.py") else module.rpartition(".")[0]
    found: list[tuple[int, str]] = []
    for node in imports:
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base_parts = package.split(".")
                base = ".".join(base_parts[: len(base_parts) - (node.level - 1)])
                target = f"{base}.{node.module}" if node.module else base
            else:
                target = node.module or ""
            found.append((node.lineno, target))
            found.extend((node.lineno, f"{target}.{alias.name}") for alias in node.names)
    return found


def _board_fake_offenders(relative: str, source: str) -> list[str]:
    """Retired imports, product test dependencies and misplaced host definitions."""
    product = relative.startswith("src/")
    tree = source_trees.parse(source, filename=relative)
    imports = source_trees.imports(tree)
    targets = _imported_modules(relative, imports)
    aliases = {
        alias.asname or alias.name: alias.name.rpartition(".")[2]
        for node in imports
        for alias in node.names
    }
    offenders = []
    if relative == "src/ummanu/board/fake.py":
        offenders.append(f"{relative}: retired product location")
    # Imports need only statement traversal. A full expression walk is needed only when a host
    # class, or a dynamic import/patch call with a target this guard refuses, can be spelled here.
    classes = "class" in source and source_trees.may_name(source, ("FakeBoardHost", "MemoryAudit"))
    calls = source_trees.may_name(source, ("import_module", "__import__", "patch")) and source_trees.may_name(
        source, ("fake", "tests") if product else ("fake",), whole=True
    )
    for node in source_trees.walk(tree) if classes or calls else ():
        if isinstance(node, ast.ClassDef) and (
            (node.name == "FakeBoardHost" and relative != "tests/fakes/board.py")
            or (product and node.name == "MemoryAudit")
        ):
            offenders.append(f"{relative}:{node.lineno}: misplaced {node.name}")
        if isinstance(node, ast.Call):
            called = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else (node.func.attr if isinstance(node.func, ast.Attribute) else "")
            )
            if (
                aliases.get(called, called) in {"import_module", "__import__", "patch"}
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                target = node.args[0].value
                if target.startswith(".") and len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                    import importlib.util

                    target = importlib.util.resolve_name(target, node.args[1].value)
                targets.append((node.lineno, target))
    for line, target in targets:
        if _names(target, "ummanu.board.fake") or (product and _names(target, "tests")):
            offenders.append(f"{relative}:{line}: {target}")
    return offenders


class SourceLayoutTests(unittest.TestCase):
    def test_board_double_static_imports_need_no_expression_walk(self) -> None:
        for source, expected in (
            ("from ummanu.board.models import Event\nVALUE = sum(range(100))\n", []),
            (
                "from ummanu.board.fake import MemoryAudit\n",
                [
                    "src/ummanu/probe.py:1: ummanu.board.fake",
                    "src/ummanu/probe.py:1: ummanu.board.fake.MemoryAudit",
                ],
            ),
            (
                "if True:\n    from tests.fakes import board\n",
                [
                    "src/ummanu/probe.py:2: tests.fakes",
                    "src/ummanu/probe.py:2: tests.fakes.board",
                ],
            ),
        ):
            with (
                self.subTest(source=source),
                mock.patch.object(
                    source_trees, "walk", side_effect=AssertionError("unnecessary expression walk")
                ),
            ):
                self.assertEqual(_board_fake_offenders("src/ummanu/probe.py", source), expected)

    def test_board_double_walks_only_where_a_call_can_name_a_refused_target(self) -> None:
        for relative, source in (
            ("tests/probe.py", "from unittest import mock\nmock.patch('ummanu.dispatch.fakes.clock')\n"),
            ("src/ummanu/probe.py", "import importlib\nimportlib.import_module('ummanu.board.models')\n"),
        ):
            with (
                self.subTest(relative=relative, source=source),
                mock.patch.object(
                    source_trees, "walk", side_effect=AssertionError("unnecessary expression walk")
                ),
            ):
                self.assertEqual(_board_fake_offenders(relative, source), [])

    def test_board_double_has_one_test_owner_and_no_product_dependency(self) -> None:
        self.assertFalse((ROOT / "src/ummanu/board/fake.py").exists())
        owner = ROOT / "tests/fakes/board.py"
        tree = source_trees.parse(owner.read_text())
        self.assertEqual(
            [node.name for node in tree.body if isinstance(node, ast.ClassDef)],
            ["MemoryAudit", "FakeBoardHost"],
        )
        offenders = []
        for directory in ("src", "tests", "scripts"):
            naming = (
                ("fake", "FakeBoardHost", "MemoryAudit", "tests")
                if directory == "src"
                else ("ummanu.board", "FakeBoardHost")
            )
            for path, _tree in source_trees.modules(ROOT / directory, naming=naming):
                offenders.extend(_board_fake_offenders(path.relative_to(ROOT).as_posix(), path.read_text()))
        self.assertEqual(offenders, [])

    def test_board_double_guard_detects_planted_imports_and_locations(self) -> None:
        for relative, source in (
            ("src/ummanu/board/fake.py", ""),
            ("src/ummanu/board/probe.py", "from . import fake as owner\n"),
            ("tests/probe.py", "from ummanu.board.fake import MemoryAudit\n"),
            ("src/ummanu/probe.py", "from tests.fakes import board\n"),
            (
                "src/ummanu/probe.py",
                "from importlib import import_module as load\nload('tests.fakes.board')\n",
            ),
            ("tests/probe.py", "import importlib as loader\nloader.import_module('.fake', 'ummanu.board')\n"),
            ("tests/probe.py", "from unittest.mock import patch as p\np('ummanu.board.fake.uuid')\n"),
            ("tests/fakes/duplicate.py", "class FakeBoardHost: pass\n"),
            ("src/ummanu/probe.py", "class MemoryAudit: pass\n"),
        ):
            with self.subTest(relative=relative, source=source):
                self.assertTrue(_board_fake_offenders(relative, source))
        self.assertEqual(
            _board_fake_offenders(
                "tests/probe.py",
                "FORBIDDEN = ('ummanu.board.fake',)\nfrom tests.fakes.board import MemoryAudit\n",
            ),
            [],
        )

    def test_test_support_never_imports_a_test_module(self) -> None:
        """Shared fakes are a one-way dependency, not bridges between test modules."""
        offenders: list[str] = []
        for path in (ROOT / "tests").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if not source_trees.may_name(text, ("tests.test",)):
                continue
            # Read once, by this check only: nothing keeps the test files' trees.
            tree = source_trees.parse(text, filename=str(path), retain=False)
            for node in source_trees.imports(tree):
                module = node.module if isinstance(node, ast.ImportFrom) else None
                if module and (module == "tests.test" or module.startswith("tests.test_")):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}: {module}")
        self.assertEqual(offenders, [])

    def test_new_ummanu_modules_do_not_widen_the_flat_root(self) -> None:
        current = {path.name for path in (ROOT / "src" / "ummanu").glob("*.py")}
        self.assertEqual(current - LEGACY_FLAT_MODULES, set())

    def test_import_walk_matches_ast_walk_in_nested_statement_containers(self) -> None:
        tree = ast.parse("""
import top
def function():
    if True:
        from package import name
    try:
        with manager():
            import nested
    except Exception:
        import handled
    finally:
        import final
    match value:
        case 1:
            import matched
    class Nested:
        import class_body
async def asynchronous():
    async for value in values:
        import async_body
    else:
        import async_else
    async with manager():
        import async_with
    try:
        import grouped_try
    except* Exception:
        import grouped_handler
    else:
        import grouped_else
    finally:
        import grouped_finally
""")
        expected = {id(node) for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))}
        self.assertEqual({id(node) for node in source_trees.imports(tree)}, expected)

    def test_no_dispatcher_flow_module_imports_the_state_machine_back(self) -> None:
        """The dependency runs one way: `ummanu.dispatch.runtime` -> the flow modules it calls."""
        offenders: list[str] = []
        for name in DISPATCH_FLOW_MODULES:
            relative = f"src/ummanu/dispatch/{name}.py"
            offenders.extend(
                _imports_dispatch_state_machine(relative, (ROOT / relative).read_text(encoding="utf-8"))
            )
        self.assertEqual(offenders, [])
        # Every spelling of the back edge is caught, at module level or inside a function.
        for source in (
            "from ummanu.dispatch.runtime import DispatcherRuntime\n",
            "import ummanu.dispatch.runtime as runtime\n",
            "from ummanu.dispatch import runtime\n",
            "def f():\n    from ummanu.dispatch import gate, runtime\n",
            "from . import runtime\n",
            "from .runtime import DispatcherRuntime\n",
            "from ..dispatch.runtime import DispatcherRuntime\n",
        ):
            with self.subTest(source=source):
                self.assertEqual(
                    len(_imports_dispatch_state_machine("src/ummanu/dispatch/planted.py", source)), 1
                )
        # A sibling flow module or a similarly named module is not the state machine.
        for source in (
            "from ummanu.dispatch import runtime_preflight\n",
            "from .gate_lifecycle import run_gate\n",
        ):
            with self.subTest(source=source):
                self.assertEqual(
                    _imports_dispatch_state_machine("src/ummanu/dispatch/planted.py", source), []
                )

    def test_no_ummanu_module_imports_the_agents_back(self) -> None:
        """The dependency runs one way: `ummanu.automations` -> the rest of `ummanu`."""
        package = ROOT / "src" / "ummanu"
        offenders: list[str] = []
        for path in sorted(package.rglob("*.py")):
            relative = path.relative_to(ROOT).as_posix()
            offenders.extend(_imports_automations(relative, path.read_text(encoding="utf-8")))
        self.assertEqual(offenders, [])
        # The admitted edge exists and is the on-demand one, or the exception above guards nothing.
        entry = (ROOT / AUTOMATIONS_ENTRY).read_text(encoding="utf-8")
        self.assertIn(
            (AUTOMATIONS_PACKAGE + ".composition", False),
            {(module, top) for _, module, top in _absolute_imports(AUTOMATIONS_ENTRY, entry)},
        )

    def test_a_planted_back_edge_is_caught_anywhere_in_ummanu(self) -> None:
        for relative, source in (
            ("src/ummanu/dispatch/planted.py", "from ummanu.automations import __main__\n"),
            ("src/ummanu/planted.py", "import ummanu.automations.runtime.dispatch as d\n"),
            ("src/ummanu/runtime/planted.py", "def f():\n    from ummanu.automations.agents import x\n"),
            ("src/ummanu/board/planted.py", "from ummanu import automations\n"),
            ("src/ummanu/planted.py", "from .automations import composition\n"),
            (AUTOMATIONS_ENTRY, "from ummanu.automations.composition import main\n"),
        ):
            with self.subTest(relative, source=source):
                self.assertEqual(len(_imports_automations(relative, source)), 1)
        # The admitted directions are not back edges.
        self.assertEqual(_imports_automations("src/ummanu/x.py", "from ummanu import tasks\n"), [])
        self.assertEqual(
            _imports_automations(
                "src/ummanu/automations/composition.py", "from ummanu.automations import __main__\n"
            ),
            [],
        )
        self.assertEqual(
            _imports_automations(
                AUTOMATIONS_ENTRY, "def run():\n    from ummanu.automations.composition import main\n"
            ),
            [],
        )


def _source_modules() -> list[Path]:
    """Every Python module under `src/`, both packages."""
    return sorted((ROOT / "src").rglob("*.py"))


class FileAuditOwnershipTests(unittest.TestCase):
    """A live audit reader follows its card client, and the exceptions are named with their reasons."""

    def test_the_card_audit_has_one_owner(self) -> None:
        """`task_audit_for` returns the SQL audit whatever it is handed, never a file one."""
        from ummanu.board.sql_audit import SqlTaskAudit
        from ummanu.tasks import task_audit_for

        self.assertIsInstance(task_audit_for(mock.sentinel.client, "/nonexistent"), SqlTaskAudit)

    def test_no_source_module_writes_a_retired_backend_identity(self) -> None:
        """A new write names the PostgreSQL store; an earlier store's identities are only read.

        The store word can hide in data rather than in a branch: an identity minted with another
        store word, or a `"kind"` literal written into a fresh audit event's `backend`, is not a
        `backend_kind` read, and secretary-1669's first submission restored cards under the retired
        identity that way. So `entity_id` takes no store word at all, and every `backend` document a
        module writes names its kind through a name (`BOARD_STORE_KIND`), never a store literal. The
        only literal kind is the dispatcher's own, which names no store.
        """
        from ummanu.board.backend import entity_id

        self.assertEqual(list(inspect.signature(entity_id).parameters), ["kind", "number"])
        non_store_kinds = {"dispatcher"}
        for path in _source_modules():
            module = str(path.relative_to(ROOT / "src")).removeprefix("ummanu/")
            tree = source_trees.parse(path.read_text(encoding="utf-8"))
            backends: list[ast.AST] = []
            for node in source_trees.walk(tree):
                if isinstance(node, ast.Dict):
                    backends.extend(
                        value
                        for key, value in zip(node.keys, node.values, strict=True)
                        if isinstance(key, ast.Constant) and key.value == "backend"
                    )
                if isinstance(node, ast.Assign):
                    backends.extend(
                        node.value
                        for target in node.targets
                        if isinstance(target, ast.Subscript)
                        and isinstance(target.slice, ast.Constant)
                        and target.slice.value == "backend"
                    )
            found = [
                value.lineno
                for backend in backends
                if isinstance(backend, ast.Dict)
                for key, value in zip(backend.keys, backend.values, strict=True)
                if isinstance(key, ast.Constant)
                and key.value == "kind"
                and isinstance(value, ast.Constant)
                and value.value not in non_store_kinds
            ]
            with self.subTest(module=module):
                self.assertEqual(sorted(set(found)), [], f"{module} writes a store literal as a backend kind")


class OneBoardClientTests(unittest.TestCase):
    """There is one board client, built in one place, and nothing in the environment selects it."""

    def test_the_client_is_built_from_the_instance_and_never_from_the_environment(self) -> None:
        from ummanu.board import backend

        parameters = inspect.signature(backend.board_client).parameters
        self.assertEqual([name for name in parameters], ["instance_dir", "serves", "role"])
        tree = source_trees.parse(
            (ROOT / "src" / "ummanu" / "board" / "backend.py").read_text(encoding="utf-8")
        )
        environment = [
            node.lineno
            for node in source_trees.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr in {"environ", "getenv"}
        ]
        self.assertEqual(environment, [], "board/backend.py reads the process environment")


if __name__ == "__main__":
    unittest.main()


# One home for each of these. A second copy is how the role environment ended up with a façade and
# two entry points (issue:a45731709558936b7b6a, secretary-1683): each lived where its first caller
# was, and the next caller copied rather than imported.
ROLE_ENV_HOME = "ummanu/runtime/role_env.py"
# The one writer of `resource_health.json`, and the only module that may name the file: every reader
# resolves it through `head_health.resource_health_path`.
HEAD_HEALTH_HOME = "ummanu/head_health.py"
RESOURCE_HEALTH_FILE = "resource_health.json"
# Generated state that left the live root for the data directory (ummanu-26). Each class has one
# resolver, and only its module spells the paths: `head_registry.installed_pair`/`generated_pair`
# for the head-registry pair, `runtime.onboarding_storage.OnboardingStorage` for onboarding's drafts, runs and locks.
HEAD_REGISTRY_HOME = "ummanu/head_registry.py"
HEAD_PAIR_FILES = ("heads.yaml", "source.yaml")
ONBOARDING_STORAGE_HOME = "ummanu/runtime/onboarding_storage.py"
ONBOARDING_STORAGE_NAMES = (
    "adapter-drafts",
    "gate-runs",
    "provision-runs",
    "compatibility-manifests",
    ".locks",
)
SINGLE_HOME_ASSIGNMENTS = {
    "ROLE_ALLOWLIST": ROLE_ENV_HOME,
    "SENSITIVE_ENV_NAME_RE": ROLE_ENV_HOME,
    "CODEX_EFFORTS": "ummanu/runtime/head/command.py",
    # The resource-health vocabulary. A second writer once kept its own GREEN/RED cache beside it
    # (`triggered_agents/agents/pipeline/health.py`, gone in secretary-1690).
    "LAUNCH_ALLOWED_STATUSES": HEAD_HEALTH_HOME,
    "PROBE_BROKEN": HEAD_HEALTH_HOME,
    "PROBE_TTL_SECONDS": HEAD_HEALTH_HOME,
}
SINGLE_HOME_FUNCTIONS = {
    "is_sensitive_env_name": ROLE_ENV_HOME,
    # The board's batched transport; the JSON-RPC Kanboard client that had its own is gone.
    "call_batch": "ummanu/board/sql_cards.py",
}


def _is_sensitive_name_pattern(text: str) -> bool:
    """The name classifier's shape: credential words anchored between `_` or the string's ends."""
    return "(^|_)" in text and "(_|$)" in text and "TOKEN" in text.upper()


def _names_resource_health_file(text: str) -> bool:
    """A string that is the cache's file name or a path ending in it; prose that mentions it is not."""
    return text == RESOURCE_HEALTH_FILE or (
        text.endswith("/" + RESOURCE_HEALTH_FILE) and not any(ch.isspace() for ch in text)
    )


def _names_path_part(text: str, name: str) -> bool:
    """A string that is `name` or a path with `name` as one of its parts; prose that mentions it is not."""
    if any(ch.isspace() for ch in text):
        return False
    return name in text.replace("\\", "/").split("/")


def _second_copies(sources: dict[str, str]) -> list[str]:
    """Every definition in `sources` (path under `src/` -> text) that is not in its one home."""
    offenders: list[str] = []
    for path, text in sorted(sources.items()):
        if Path(path).name == "role_env.py" and path != ROLE_ENV_HOME:
            offenders.append(f"{path}: role_env module")
        tree = source_trees.parse(text, filename=path)
        for node in source_trees.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    home = SINGLE_HOME_ASSIGNMENTS.get(getattr(target, "id", ""))
                    if home is not None and path != home:
                        offenders.append(f"{path}:{node.lineno}: {target.id}")  # type: ignore[attr-defined]
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                home = SINGLE_HOME_FUNCTIONS.get(node.name)
                if home is not None and path != home:
                    offenders.append(f"{path}:{node.lineno}: def {node.name}")
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and _is_sensitive_name_pattern(node.value)
                and path != ROLE_ENV_HOME
            ):
                offenders.append(f"{path}:{node.lineno}: sensitive-name pattern")
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and _names_resource_health_file(node.value)
                and path != HEAD_HEALTH_HOME
            ):
                offenders.append(f"{path}:{node.lineno}: resource-health file")
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and any(_names_path_part(node.value, name) for name in HEAD_PAIR_FILES)
                and path != HEAD_REGISTRY_HOME
            ):
                offenders.append(f"{path}:{node.lineno}: head-registry pair path")
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and any(_names_path_part(node.value, name) for name in ONBOARDING_STORAGE_NAMES)
                and path != ONBOARDING_STORAGE_HOME
            ):
                offenders.append(f"{path}:{node.lineno}: onboarding storage path")
    return offenders


class SingleHomeTests(unittest.TestCase):
    """The role environment, the sensitive-name pattern, the Codex effort table, the board
    transport, the resource-health cache (its file and its status vocabulary), the head-registry
    pair's path and onboarding's storage paths each have one definition under `src/`; a second one
    anywhere fails here."""

    def test_nothing_under_src_defines_a_second_copy(self) -> None:
        src = ROOT / "src"
        sources = {
            path.relative_to(src).as_posix(): path.read_text(encoding="utf-8") for path in _source_modules()
        }
        homes = {ROLE_ENV_HOME, HEAD_REGISTRY_HOME, ONBOARDING_STORAGE_HOME}
        for home in {*homes, *SINGLE_HOME_ASSIGNMENTS.values(), *SINGLE_HOME_FUNCTIONS.values()}:
            self.assertIn(home, sources)
        self.assertEqual(_second_copies(sources), [])

    def test_each_second_copy_is_caught(self) -> None:
        probes = {
            "role_env module": ("ummanu/automations/runtime/role_env.py", "X = 1\n"),
            "ROLE_ALLOWLIST": ("ummanu/session.py", "ROLE_ALLOWLIST = {}\n"),
            "SENSITIVE_ENV_NAME_RE": ("ummanu/tasks.py", "SENSITIVE_ENV_NAME_RE = None\n"),
            "sensitive-name pattern": (
                "ummanu/automations/runtime/scrub.py",
                'import re\nNAMES = re.compile(r"(^|_)(TOKEN|SECRET)(_|$)")\n',
            ),
            "def is_sensitive_env_name": (
                "ummanu/checkpoint.py",
                "def is_sensitive_env_name(n):\n    return n\n",
            ),
            "CODEX_EFFORTS": ("ummanu/dispatch/launcher.py", "CODEX_EFFORTS: dict = {}\n"),
            "LAUNCH_ALLOWED_STATUSES": (
                "ummanu/automations/runtime/dispatch.py",
                'LAUNCH_ALLOWED_STATUSES = frozenset({"green"})\n',
            ),
            "PROBE_TTL_SECONDS": ("ummanu/runtime/resource_probe.py", "PROBE_TTL_SECONDS = 300\n"),
            # The shape the deleted second writer had: its own cache file next to its own state.
            "resource-health file": (
                "ummanu/automations/agents/pipeline/health.py",
                'from pathlib import Path\nHEALTH_FILE = Path("state") / "resource_health.json"\n',
            ),
            "def call_batch": (
                "ummanu/board/kanboard.py",
                "class Client:\n    def call_batch(self, calls):\n        return []\n",
            ),
            # The shapes the readers had before ummanu-26: each built the live root's pair itself.
            "head-registry pair path": (
                "ummanu/task_commands.py",
                'from pathlib import Path\nHEADS = Path("i") / "heads" / "heads.yaml"\n',
            ),
            "head-registry pair path ": ("ummanu/po/runner.py", 'PIN = "heads/source.yaml"\n'),
            "onboarding storage path": (
                "ummanu/gate.py",
                'from pathlib import Path\nRUNS = Path("i") / "gate-runs"\n',
            ),
            "onboarding storage path ": ("ummanu/config.py", 'DRAFTS = "instance/adapter-drafts"\n'),
        }
        for label, (path, text) in probes.items():
            with self.subTest(label):
                offenders = _second_copies({path: text})
                self.assertEqual(len(offenders), 1, offenders)
                self.assertTrue(
                    offenders[0].startswith(path) and offenders[0].endswith(label.strip()), offenders
                )
        # The homes themselves are not second copies.
        self.assertEqual(
            _second_copies(
                {
                    ROLE_ENV_HOME: 'ROLE_ALLOWLIST = {}\nSENSITIVE_ENV_NAME_RE = r"(^|_)(TOKEN)(_|$)"\n',
                    "ummanu/board/sql_cards.py": "def call_batch(calls):\n    return []\n",
                    HEAD_HEALTH_HOME: (
                        'PROBE_BROKEN = "probe_broken"\n'
                        'def resource_health_path(d):\n    return d / "dispatcher" / "resource_health.json"\n'
                    ),
                    # A reader that mentions the file in prose is not a writer.
                    "ummanu/automations/agents/steward/signals.py": '"""Reads the resource_health.json cache."""\n',
                    HEAD_REGISTRY_HOME: 'SNAPSHOT_NAME = "heads.yaml"\nSOURCE_NAME = "source.yaml"\n',
                    ONBOARDING_STORAGE_HOME: 'def gate_runs(root):\n    return root / "gate-runs"\n',
                    "ummanu/dispatch/host.py": 'MESSAGE = "a head that left `heads.yaml` stops the attempt"\n',
                }
            ),
            [],
        )
