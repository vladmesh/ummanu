"""Executable boundaries for the incremental source-layout migration."""

from __future__ import annotations

import ast
import inspect
import re
import shlex
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from tests import source_trees

ROOT = Path(__file__).resolve().parents[1]


# Existing flat modules may leave this set one feature at a time. New modules belong in one of the
# feature packages documented in ARCHITECTURE.md instead of making the root wider again.
LEGACY_FLAT_MODULES = frozenset(
    """
    __init__.py __main__.py _fsutil.py _proc.py backup.py
    backup_policy.py backup_retention.py backup_verify.py bootstrap.py
    broad_check.py candidate_history.py check_commands.py checkpoint.py cli.py cli_output.py
    codex_provider_events.py config.py data.py
    gate.py
    head_health.py head_registry.py host.py host_apply.py host_commands.py installation.py
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
            found.extend(
                (node, f"{node.module}.{alias.name}", id(node) in top_level) for alias in node.names
            )
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
            if isinstance(node, ast.ImportFrom) and node.level and (
                (node.module or "").split(".")[0] == "automations"
                or any(alias.name == "automations" for alias in node.names)
            ):
                offenders.add(f"{relative}:{node.lineno}: {AUTOMATIONS_PACKAGE}")
    return sorted(offenders)


# Orca is gone from the product's own code (A20, secretary-1725): no module under `src/ummanu`
# imports the pane host, the Orca head backend or an Orca RPC client, and no string constant in it
# names the `orca` / `orca-cli` program. The rule scans the program name itself, wherever the string
# sits, rather than resolving call shapes: an alias, a wrapper or a shell cannot hide a name that is
# looked for in every constant. What is still there is on `ORCA_ALLOWLIST`, one finding each, with
# the owner decision that keeps it. Step 9 (secretary-1726) left only two: neither runs a program.
SOURCE_ROOT = ROOT / "src" / "ummanu"
BANNED_ORCA_MODULES = ("ummanu.runtime.pane_host", "ummanu.runtime.orca_legacy_head")
BANNED_ORCA_LAST_COMPONENTS = frozenset({"pane_host", "orca_legacy_head", "orca_rpc"})
ORCA_PROGRAMS = frozenset({"orca", "orca-cli"})
_DOTTED_NAME = re.compile(r"\.*[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")


@dataclass(frozen=True)
class OrcaAllowance:
    """One finding still allowed: its file, a substring of its line, the flagged value, and why.

    It excuses exactly one finding: the first on a matching line whose flagged value (the string
    constant, or the imported module) is `value`. Every other finding on that line is reported.
    """

    path: str
    line: str
    value: str
    reason: str


@dataclass(frozen=True)
class OrcaFinding:
    lineno: int
    col: int
    what: str
    value: str


ORCA_ALLOWLIST = (
    OrcaAllowance(
        "src/ummanu/host.py",
        '{"unit", "orca"}',
        "orca",
        "owner decision, step 8: the legacy `orca` record kind in host-managed.json stays loadable; "
        "a kind, not a program",
    ),
    OrcaAllowance(
        "src/ummanu/upgrade.py",
        'workspace_root.parent.name == "orca"',
        "orca",
        "owner decision, step 11: the role worktree root `~/orca/workspaces`, compared by name; "
        "a path, not a program",
    ),
)


def _module_of(relative: str) -> str:
    """`src/ummanu/automations/runtime/dispatch.py` -> `ummanu.automations.runtime.dispatch`."""
    parts = Path(relative).with_suffix("").parts
    parts = parts[1:] if parts and parts[0] == "src" else parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _imported_modules(relative: str, source: str) -> list[tuple[int, str]]:
    """Every module an import statement names, relative ones resolved against the file's package."""
    tree = source_trees.parse(source, filename=relative)
    module = _module_of(relative)
    package = module if relative.endswith("__init__.py") else module.rpartition(".")[0]
    found: list[tuple[int, str]] = []
    for node in source_trees.walk(tree):
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


def _first_shell_word(text: str) -> str:
    """The first word of `text` as a shell would split it; later unbalanced quotes do not matter."""
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return lexer.get_token() or ""
    except ValueError:
        words = text.split()
        return words[0] if words else ""


def _shell_words(text: str) -> list[str]:
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def _is_orca_program(word: str) -> bool:
    return word in ORCA_PROGRAMS or word.endswith(tuple(f"/{name}" for name in ORCA_PROGRAMS))


def _is_path_operand(node: ast.Constant, parent: ast.AST | None) -> bool:
    """The one structural exception: a bare `"orca"` used as a path component.

    An operand of `/`, or an argument of `Path(...)`, `os.path.join(...)` or `joinpath(...)`.
    """
    if node.value != "orca" or parent is None:
        return False
    if isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Div):
        return True
    if not isinstance(parent, ast.Call) or node not in parent.args:
        return False
    func = parent.func
    if isinstance(func, ast.Name):
        return func.id == "Path"
    if isinstance(func, ast.Attribute):
        if func.attr in {"Path", "joinpath"}:
            return True
        return func.attr == "join" and isinstance(func.value, ast.Attribute) and func.value.attr == "path"
    return False


def _names_banned_module(module: str) -> bool:
    return any(_names(module, banned) for banned in BANNED_ORCA_MODULES) or (
        module.rsplit(".", 1)[-1] == "orca_rpc"
    )


def _orca_references(relative: str, source: str) -> list[OrcaFinding]:
    """Every Orca import and every string constant naming the Orca program, one finding per node."""
    tree = source_trees.parse(source, filename=relative)
    found: set[OrcaFinding] = set()
    for lineno, module in _imported_modules(relative, source):
        if _names_banned_module(module):
            found.add(OrcaFinding(lineno, -1, f"imports {module}", module))
    parents = source_trees.parents(tree)
    for node in source_trees.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
            # `importlib.import_module("…")` and friends name a module by string.
            if _DOTTED_NAME.fullmatch(text) and text.rsplit(".", 1)[-1] in BANNED_ORCA_LAST_COMPONENTS:
                found.add(OrcaFinding(node.lineno, node.col_offset, f"names module {text}", text))
            if _is_orca_program(_first_shell_word(text)) and not _is_path_operand(
                node, parents.get(id(node))
            ):
                found.add(
                    OrcaFinding(node.lineno, node.col_offset, f"names the orca program: {text[:60]!r}", text)
                )
        if isinstance(node, (ast.List, ast.Tuple)):
            words = [
                element.value
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
                else None
                for element in node.elts
            ]
            if "-c" in words:
                start = words.index("-c") + 1
                for element, word in zip(node.elts[start:], words[start:], strict=True):
                    if word and any(_is_orca_program(part) for part in _shell_words(word)):
                        found.add(
                            OrcaFinding(
                                element.lineno,
                                element.col_offset,
                                f"runs the orca program through -c: {word[:60]!r}",
                                word,
                            )
                        )
    return sorted(found, key=lambda finding: (finding.lineno, finding.col, finding.what))


def _apply_allowlist(relative: str, source: str) -> tuple[list[str], list[OrcaAllowance]]:
    """The findings no allowance excuses, as offender lines, and the allowances that excused one.

    Each allowance is spent on the first finding it matches, so a second finding with the same value
    on the same line is still an offender.
    """
    lines = source.splitlines()
    unspent = [allowance for allowance in ORCA_ALLOWLIST if allowance.path == relative]
    spent: list[OrcaAllowance] = []
    offenders: list[str] = []
    for finding in _orca_references(relative, source):
        line = lines[finding.lineno - 1] if finding.lineno <= len(lines) else ""
        allowance = next(
            (
                candidate
                for candidate in unspent
                if candidate.line in line and candidate.value == finding.value
            ),
            None,
        )
        if allowance is None:
            offenders.append(f"{relative}:{finding.lineno}: {finding.what}")
            continue
        unspent.remove(allowance)
        spent.append(allowance)
    return offenders, spent


def _orca_offenders(relative: str, source: str) -> list[str]:
    return _apply_allowlist(relative, source)[0]


class NoOrcaInSourceTests(unittest.TestCase):
    """The one Orca rule: over every module under `src/ummanu`, with one explicit allowlist."""

    def _sources(self):
        for path in sorted(SOURCE_ROOT.rglob("*.py")):
            yield path.relative_to(ROOT).as_posix(), path.read_text(encoding="utf-8")

    def test_no_module_imports_or_names_orca(self) -> None:
        offenders: list[str] = []
        for relative, source in self._sources():
            offenders.extend(_orca_offenders(relative, source))
        self.assertEqual(offenders, [])
        for retired in (
            "runtime/pane_host.py",
            "runtime/orca_legacy_head.py",
            "automations/runtime/orca_rpc.py",
            "automations/runtime/finalizer.py",
        ):
            self.assertFalse((SOURCE_ROOT / retired).exists(), retired)

    def test_each_allowlist_entry_still_excuses_a_real_line(self) -> None:
        """An entry cannot outlive its code: it must match a line that the rule would flag."""
        excused: list[OrcaAllowance] = []
        for relative, source in self._sources():
            excused.extend(_apply_allowlist(relative, source)[1])
        self.assertEqual(len(excused), len(set(excused)))
        for allowance in ORCA_ALLOWLIST:
            with self.subTest(path=allowance.path, line=allowance.line):
                self.assertTrue(allowance.reason)
                self.assertIn(allowance, excused, "no flagged line matches this entry any more")

    def test_each_planted_route_to_orca_fails_the_rule(self) -> None:
        planted = "src/ummanu/runtime/planted.py"
        for source in (
            "import subprocess\nsubprocess.run('orca')\n",
            "import subprocess as sp\nsp.run(['orca', 'x'])\n",
            "from subprocess import run as launch\nlaunch('orca-cli')\n",
            "import subprocess\nsubprocess.run(['/bin/sh', '-c', 'orca terminal list'])\n",
            "import os\nos.system('orca')\n",
            "from importlib import import_module\nimport_module('ummanu.runtime.pane_host')\n",
            # Beyond the card's six: a later word under `-c`, a path, the imports in each form.
            "import subprocess\nsubprocess.run(['sh', '-c', 'cd /tmp && orca terminal list'])\n",
            "BIN = '/usr/local/bin/orca-cli status'\n",
            "from ummanu.runtime.pane_host import Pane\n",
            "from ..runtime import pane_host\n",
            "def f():\n    import ummanu.runtime.orca_legacy_head\n",
            "from . import orca_rpc\n",
            "from .orca_rpc import call\n",
            "__import__('ummanu.automations.runtime.orca_rpc')\n",
        ):
            with self.subTest(source=source):
                self.assertTrue(_orca_offenders(planted, source), source)

    def test_a_path_component_or_a_longer_word_passes_the_rule(self) -> None:
        planted = "src/ummanu/runtime/planted.py"
        for source in (
            "from pathlib import Path\nROOT = Path.home() / 'orca' / 'workspaces'\n",
            "import os\nROOT = os.path.join(home, 'orca')\n",
            "KEY = 'orca_binding'\n",
        ):
            with self.subTest(source=source):
                self.assertEqual(_orca_offenders(planted, source), [])

    def test_an_allowlisted_line_is_excused_only_in_its_own_file(self) -> None:
        line = 'if resource.kind not in {"unit", "orca"}:\n    pass\n'
        self.assertEqual(_orca_offenders("src/ummanu/host.py", line), [])
        self.assertTrue(_orca_offenders("src/ummanu/runtime/planted.py", line))

    def test_an_allowance_excuses_exactly_one_finding_on_its_line(self) -> None:
        """The allowed constant is excused once; everything else the line carries is reported."""
        for path, source, expected in (
            (
                "src/ummanu/host.py",
                'if x in {"unit", "orca"}: import ummanu.runtime.pane_host\n',
                ["imports ummanu.runtime.pane_host"],
            ),
            (
                "src/ummanu/upgrade.py",
                'workspace_root.parent.name == "orca"; os.system("orca-cli")\n',
                ["names the orca program: 'orca-cli'"],
            ),
            (
                "src/ummanu/upgrade.py",
                'workspace_root.parent.name == "orca"; os.system("orca")\n',
                ["names the orca program: 'orca'"],
            ),
            (
                "src/ummanu/host.py",
                'if x in {"unit", "orca"}: subprocess.run(["sh", "-c", "orca terminal list"])\n',
                [
                    "names the orca program: 'orca terminal list'",
                    "runs the orca program through -c: 'orca terminal list'",
                ],
            ),
        ):
            with self.subTest(source=source):
                offenders = _orca_offenders(path, source)
                self.assertEqual([offender.split(": ", 1)[1] for offender in offenders], expected)


class SourceLayoutTests(unittest.TestCase):
    def test_test_support_never_imports_a_test_module(self) -> None:
        """Shared fakes are a one-way dependency, not bridges between test modules."""
        offenders: list[str] = []
        for path in (ROOT / "tests").rglob("*.py"):
            # Read once, by this check only: nothing keeps the test files' trees.
            tree = source_trees.parse(path.read_text(encoding="utf-8"), filename=str(path), retain=False)
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
""")
        expected = {id(node) for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))}
        self.assertEqual({id(node) for node in source_trees.imports(tree)}, expected)

    def test_no_dispatcher_flow_module_imports_the_state_machine_back(self) -> None:
        """The dependency runs one way: `ummanu.dispatch.runtime` -> the flow modules it calls."""
        offenders: list[str] = []
        for name in DISPATCH_FLOW_MODULES:
            relative = f"src/ummanu/dispatch/{name}.py"
            offenders.extend(_imports_dispatch_state_machine(relative, (ROOT / relative).read_text(encoding="utf-8")))
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
                self.assertEqual(len(_imports_dispatch_state_machine("src/ummanu/dispatch/planted.py", source)), 1)
        # A sibling flow module or a similarly named module is not the state machine.
        for source in ("from ummanu.dispatch import runtime_preflight\n", "from .gate_lifecycle import run_gate\n"):
            with self.subTest(source=source):
                self.assertEqual(_imports_dispatch_state_machine("src/ummanu/dispatch/planted.py", source), [])

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
        tree = source_trees.parse((ROOT / "src" / "ummanu" / "board" / "backend.py").read_text(encoding="utf-8"))
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
# for the head-registry pair, `onboarding.OnboardingStorage` for onboarding's drafts, runs and locks.
HEAD_REGISTRY_HOME = "ummanu/head_registry.py"
HEAD_PAIR_FILES = ("heads.yaml", "source.yaml")
ONBOARDING_STORAGE_HOME = "ummanu/onboarding.py"
ONBOARDING_STORAGE_NAMES = ("adapter-drafts", "gate-runs", "provision-runs", "compatibility-manifests", ".locks")
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
                self.assertTrue(offenders[0].startswith(path) and offenders[0].endswith(label.strip()), offenders)
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
