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


class DeadPaneDeferralTests(unittest.TestCase):
    def test_retired_pane_names_stay_out_of_source_and_docs(self) -> None:
        banned = (
            "HeadPaneBusy",
            "HeadPaneNotReady",
            "UMMANU_BRINGUP_DEFER_ATTEMPTS",
            "pane_never_ready",
        )
        for directory in (ROOT / "src", ROOT / "docs"):
            for path in directory.rglob("*"):
                if not path.is_file() or path.suffix == ".pyc":
                    continue
                content = path.read_text(encoding="utf-8", errors="replace")
                for name in banned:
                    with self.subTest(file=str(path.relative_to(ROOT)), name=name):
                        self.assertNotIn(name, content, f"{path.relative_to(ROOT)} contains {name}")

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
RETIRED_AGENTS_PACKAGE = "triggered_agents"


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


def _imports_retired_agents_package(relative: str, source: str) -> list[str]:
    """Every import of the retired top-level `triggered_agents` package, as offender lines."""
    return sorted(
        {
            f"{relative}:{node.lineno}: {module.split('.')[0]}"
            for node, module, _ in _absolute_imports(relative, source)
            if _names(module, RETIRED_AGENTS_PACKAGE)
        }
    )


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

    def test_the_allowlist_keeps_only_the_two_owner_decisions(self) -> None:
        """A20 step 9 (secretary-1726) emptied the step-9 class: no entry excuses a program."""
        self.assertEqual(
            {(allowance.path, allowance.line, allowance.value) for allowance in ORCA_ALLOWLIST},
            {
                ("src/ummanu/host.py", '{"unit", "orca"}', "orca"),
                ("src/ummanu/upgrade.py", 'workspace_root.parent.name == "orca"', "orca"),
            },
        )
        for allowance in ORCA_ALLOWLIST:
            with self.subTest(path=allowance.path):
                self.assertTrue(allowance.reason.startswith("owner decision, step "), allowance.reason)
                self.assertNotIn("step 9", allowance.reason)

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


# The dispatcher state machine lives in `ummanu.dispatch.runtime`. The retired flat root module
# must not come back, and nothing may import it under its old name.
RETIRED_DISPATCHER_MODULE = ("ummanu", "dispatcher")


class SourceLayoutTests(unittest.TestCase):
    def test_test_support_never_imports_a_test_module(self) -> None:
        """Shared fakes are a one-way dependency, not bridges between test modules."""
        offenders: list[str] = []
        for path in (ROOT / "tests").rglob("*.py"):
            tree = source_trees.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in source_trees.walk(tree):
                module = node.module if isinstance(node, ast.ImportFrom) else None
                if module and (module == "tests.test" or module.startswith("tests.test_")):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}: {module}")
        self.assertEqual(offenders, [])

    def test_new_ummanu_modules_do_not_widen_the_flat_root(self) -> None:
        current = {path.name for path in (ROOT / "src" / "ummanu").glob("*.py")}
        self.assertEqual(current - LEGACY_FLAT_MODULES, set())

    def test_retired_dispatcher_root_module_stays_retired(self) -> None:
        """The retired flat dispatcher root module is gone and nothing imports it by its old name."""
        self.assertFalse((ROOT / "src" / "ummanu" / "dispatcher.py").exists())
        retired = ".".join(RETIRED_DISPATCHER_MODULE)
        offenders: list[str] = []
        for tree_root in ("src", "tests", "scripts"):
            for path in (ROOT / tree_root).rglob("*.py"):
                source = path.read_text(encoding="utf-8")
                tree = source_trees.parse(source, filename=str(path))
                for node in source_trees.imports(tree):
                    if isinstance(node, ast.Import):
                        modules = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                        modules = [node.module]
                        modules += [f"{node.module}.{alias.name}" for alias in node.names]
                    else:
                        continue
                    for module in modules:
                        if module == retired or module.startswith(f"{retired}."):
                            offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}: {module}")
        self.assertEqual(offenders, [])

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

    def test_dispatcher_claim_flow_is_package_owned(self) -> None:
        dispatcher_source = (ROOT / "src" / "ummanu" / "dispatch" / "runtime.py").read_text(
            encoding="utf-8"
        )
        production_source = (
            ROOT / "src" / "ummanu" / "dispatch" / "production.py"
        ).read_text(encoding="utf-8")
        for helper in (
            "_failover_collapse",
            "_broad_check_contract_verdict",
            "_sprint_admission_refusal",
            "_project_git_access",
            "_write_claim_preflight_block",
        ):
            self.assertNotIn(f"\n    def {helper}(", dispatcher_source)
        self.assertNotIn("runtime._claim(", production_source)

    def test_dispatcher_worker_launch_flow_is_package_owned(self) -> None:
        dispatcher_source = (ROOT / "src" / "ummanu" / "dispatch" / "runtime.py").read_text(
            encoding="utf-8"
        )
        claim_source = (
            ROOT / "src" / "ummanu" / "dispatch" / "claim.py"
        ).read_text(encoding="utf-8")
        worker_launch_source = (
            ROOT / "src" / "ummanu" / "dispatch" / "worker_launch.py"
        ).read_text(encoding="utf-8")
        for helper in (
            "_launch_worker_after_claim",
            "_worker_launch_failure",
            "_bring_up_worker_head",
            "_worker_relaunch_intent",
            "_resolve_headless_worker",
            "_relaunch_headless_worker",
            "_refuse_headless_worker",
        ):
            self.assertNotIn(f"\n    def {helper}(", dispatcher_source)
        self.assertNotIn("runtime._launch_worker_after_claim(", claim_source)
        self.assertIn("def launch_worker_after_claim(", worker_launch_source)
        self.assertIn("def resolve_headless_worker(", worker_launch_source)

    def test_dispatcher_worker_report_flow_is_package_owned(self) -> None:
        dispatcher_source = (ROOT / "src" / "ummanu" / "dispatch" / "runtime.py").read_text(encoding="utf-8")
        report_source = (ROOT / "src" / "ummanu" / "dispatch" / "worker_report.py").read_text(encoding="utf-8")
        for helper in (
            "_record_infra_completion", "_accept_stale_infrastructure_done",
            "_block_repeated_infrastructure_done", "_reject_stale_done", "_prompt_worker_report",
        ):
            self.assertNotIn(f"\n    def {helper}(", dispatcher_source)
            self.assertNotIn(f"self.{helper}(", dispatcher_source)
            self.assertNotIn(f"runtime.{helper}(", report_source)
        runtime_tree = source_trees.parse(dispatcher_source)
        advance = next(node for node in source_trees.walk(runtime_tree) if isinstance(node, ast.FunctionDef) and node.name == "_advance_worker")
        advance_source = ast.get_source_segment(dispatcher_source, advance)
        self.assertIn("_worker_report_marker(", advance_source)
        self.assertIn("_handle_worker_report(", advance_source)
        self.assertNotIn("verify_worker_result(", advance_source)
        self.assertLess(
            advance_source.index("_worker_report_marker("),
            advance_source.index("_recover_worker_continuation("),
        )
        self.assertLess(
            advance_source.index("_recover_worker_continuation("),
            advance_source.index("_handle_worker_report("),
        )
        self.assertLess(
            advance_source.index("_handle_worker_report("),
            advance_source.index("_wait_watchdog(self, "),
        )
        for entry in ("worker_report_marker", "handle_worker_report", "prompt_worker_report"):
            self.assertIn(f"def {entry}(", report_source)
        self.assertNotIn("from ummanu.dispatch.runtime import", report_source)

    def test_dispatcher_gate_lifecycle_is_package_owned(self) -> None:
        dispatcher_source = (ROOT / "src" / "ummanu" / "dispatch" / "runtime.py").read_text(encoding="utf-8")
        gate_source = (
            ROOT / "src" / "ummanu" / "dispatch" / "gate_lifecycle.py"
        ).read_text(encoding="utf-8")
        gate_domain_source = (
            ROOT / "src" / "ummanu" / "dispatch" / "gate.py"
        ).read_text(encoding="utf-8")
        helpers = (
            "_run_gate",
            "_accept_green_gate",
            "_block_missing_gate_receipt",
            "_gate_red_to_worker",
            "_retry_infrastructure_gate",
            "_reset_infrastructure_reruns",
            "_block_infrastructure_reruns_exhausted",
            "_block_infrastructure_rerun_unavailable",
            "_gate_answered",
            "_gate_transport_retry",
            "_gate_rerun_transport_retry",
            "_block_gate_transport",
            "_gate_pending",
            "_worker_vitality_for_gate",
        )
        for helper in helpers:
            self.assertNotIn(f"\n    def {helper}(", dispatcher_source)
            self.assertNotIn(f"runtime.{helper}(", gate_source)
        for entry in (
            "run_gate",
            "accept_green_gate",
            "gate_red_to_worker",
            "gate_transport_retry",
            "block_gate_transport",
            "gate_answered",
            "gate_pending",
        ):
            self.assertIn(f"def {entry}(", gate_source)
        self.assertIn("def reset_infrastructure_reruns(", gate_domain_source)
        self.assertNotIn("from ummanu.dispatch.runtime import", gate_source)

    def test_dispatcher_review_verdict_parking_is_package_owned(self) -> None:
        dispatcher_source = (ROOT / "src" / "ummanu" / "dispatch" / "runtime.py").read_text(encoding="utf-8")
        verdict_source = (
            ROOT / "src" / "ummanu" / "dispatch" / "review_verdict.py"
        ).read_text(encoding="utf-8")
        helpers = (
            "_parks_for_decision",
            "_park_green_verdict",
            "_merge_ready_for_park",
            "_begin_park",
            "_complete_park",
            "_block_red_review_ceiling",
        )
        for helper in helpers:
            self.assertNotIn(f"\n    def {helper}(", dispatcher_source)
            self.assertNotIn(f"self.{helper}(", dispatcher_source)
            self.assertNotIn(f"runtime.{helper}(", verdict_source)
        for entry in (
            "advance_review_verdict",
            "park_green_verdict",
            "merge_ready_for_park",
            "begin_park",
            "complete_park",
        ):
            self.assertIn(f"def {entry}(", verdict_source)
        self.assertIn("_advance_review_verdict(self, task, record, records, payload, attempt_id)", dispatcher_source)
        self.assertNotIn("\n    def _advance_assessment(", dispatcher_source)
        self.assertIn("_advance_assessment(self, task, records, payload, attempt_id)", dispatcher_source)
        self.assertNotIn("\n    def _release_parked(", dispatcher_source)
        self.assertNotIn("from ummanu.dispatch.runtime import", verdict_source)

    def test_dispatcher_assessment_decision_flow_is_package_owned(self) -> None:
        dispatcher_source = (ROOT / "src" / "ummanu" / "dispatch" / "runtime.py").read_text(encoding="utf-8")
        decision_source = (
            ROOT / "src" / "ummanu" / "dispatch" / "assessment_decision.py"
        ).read_text(encoding="utf-8")
        for helper in (
            "_advance_assessment",
            "_recorded_decision",
            "_rework_parked",
            "_reslice_parked",
        ):
            self.assertNotIn(f"\n    def {helper}(", dispatcher_source)
        for entry in (
            "advance_assessment",
            "recorded_decision",
            "rework_parked",
            "reslice_parked",
        ):
            self.assertIn(f"def {entry}(", decision_source)
        self.assertIn("_advance_assessment(self, task, records, payload, attempt_id)", dispatcher_source)
        self.assertIn("release_lifecycle.release_parked(", decision_source)
        self.assertNotIn("runtime._release_parked(", decision_source)
        self.assertNotIn("\n    def _release_parked(", dispatcher_source)
        self.assertNotIn("from ummanu.dispatch.runtime import", decision_source)

    def test_dispatcher_release_completion_flow_is_package_owned(self) -> None:
        dispatcher_source = (ROOT / "src" / "ummanu" / "dispatch" / "runtime.py").read_text(encoding="utf-8")
        release_source = (
            ROOT / "src" / "ummanu" / "dispatch" / "release_lifecycle.py"
        ).read_text(encoding="utf-8")
        gate_source = (
            ROOT / "src" / "ummanu" / "dispatch" / "gate_lifecycle.py"
        ).read_text(encoding="utf-8")
        verdict_source = (
            ROOT / "src" / "ummanu" / "dispatch" / "review_verdict.py"
        ).read_text(encoding="utf-8")
        decision_source = (
            ROOT / "src" / "ummanu" / "dispatch" / "assessment_decision.py"
        ).read_text(encoding="utf-8")

        for helper in (
            "_block_merge_path",
            "_release_parked",
            "_release_effect",
            "_require_completion_evidence",
            "_transfer_research_report",
        ):
            self.assertNotIn(f"\n    def {helper}(", dispatcher_source)
        for helper in ("_released_verdict", "_merge_terminal_reason"):
            self.assertNotIn(f"\ndef {helper}(", dispatcher_source)
        for entry in (
            "block_merge_path",
            "release_parked",
            "release_effect",
            "require_completion_evidence",
            "transfer_research_report",
            "review_drift",
            "merge_readiness",
        ):
            self.assertIn(f"def {entry}(", release_source)

        self.assertNotIn("def review_drift(", verdict_source)
        self.assertNotIn("def merge_readiness(", verdict_source)
        self.assertIn("release_lifecycle.merge_readiness(runtime,", verdict_source)
        self.assertIn("release_lifecycle.release_effect(runtime,", verdict_source)
        self.assertIn("release_lifecycle.release_parked(", decision_source)
        self.assertIn("release_lifecycle.block_merge_path(runtime,", gate_source)
        self.assertNotIn("runtime._block_merge_path(", gate_source)
        self.assertNotIn("runtime._block_merge_path(", verdict_source)
        self.assertNotIn("runtime._release_effect(", verdict_source)
        self.assertNotIn("runtime._release_parked(", decision_source)
        self.assertNotIn("from ummanu.dispatch.runtime import", release_source)

    def test_dispatcher_attempt_accounting_is_package_owned(self) -> None:
        dispatcher_source = (ROOT / "src" / "ummanu" / "dispatch" / "runtime.py").read_text(encoding="utf-8")
        accounting_source = (
            ROOT / "src" / "ummanu" / "dispatch" / "attempt_accounting.py"
        ).read_text(encoding="utf-8")
        for helper in (
            "pending_attempt_usage",
            "_attempt_outcome_obligation",
            "_outcome_lineage_sources",
            "_outcome_round_context_request_id",
            "_persist_outcome_round_context",
            "_capture_outcome_source",
            "_outcome_round_context",
            "_outcome_usage_source",
            "_finish_attempt_outcome",
            "terminal_effect",
            "publish_pending_attempt_outcomes",
            "publish_pending_attempt_usage",
            "record_attempt_usage",
            "_write_attempt_usage",
        ):
            self.assertNotIn(f"\n    def {helper}(", dispatcher_source)
        for entry in (
            "persist_outcome_round_context",
            "capture_outcome_source",
            "terminal_effect",
            "publish_pending_attempt_outcomes",
            "publish_pending_attempt_usage",
            "record_attempt_usage",
        ):
            self.assertIn(f"def {entry}(", accounting_source)
        for path in (ROOT / "src" / "ummanu" / "dispatch").glob("*.py"):
            if path.name == "attempt_accounting.py":
                continue
            source = path.read_text(encoding="utf-8")
            for legacy_call in (
                "runtime.terminal_effect(",
                "runtime._persist_outcome_round_context(",
                "runtime._capture_outcome_source(",
                "runtime.record_attempt_usage(",
                "runtime.publish_pending_attempt_outcomes(",
                "runtime.publish_pending_attempt_usage(",
            ):
                self.assertNotIn(legacy_call, source, path.name)
        self.assertNotIn("from ummanu.dispatch.runtime import", accounting_source)

    def test_dispatcher_wait_vitality_flow_is_package_owned(self) -> None:
        dispatcher_source = (ROOT / "src" / "ummanu" / "dispatch" / "runtime.py").read_text(encoding="utf-8")
        wait_source = (
            ROOT / "src" / "ummanu" / "dispatch" / "wait_vitality.py"
        ).read_text(encoding="utf-8")
        helpers = (
            "_decide_wait_by_verdict",
            "_escalate_unobservable_wait",
            "_recovery_policy_decision",
            "_execute_recovery_intent",
            "_sigcont_head",
            "_guard_or_wait",
            "_trigger_wait_watchdog",
            "_respawn_wait",
            "_escalate_wait",
        )
        for helper in helpers:
            self.assertNotIn(f"\n    def {helper}(", dispatcher_source)
            self.assertNotIn(f"runtime.{helper}(", wait_source)
        for entry in (
            "wait_watchdog",
            "execute_recovery_intent",
            "recovery_policy_outcome",
            "reduce_and_store_vitality_episode",
        ):
            self.assertIn(f"def {entry}(", wait_source)
        self.assertNotIn("from ummanu.dispatch.runtime import", wait_source)

    def test_the_retired_agents_package_stays_retired(self) -> None:
        """`src/triggered_agents` is gone, and nothing in `src/`, `tests/` or `scripts/` imports it."""
        self.assertFalse((ROOT / "src" / RETIRED_AGENTS_PACKAGE).exists())
        self.assertTrue((ROOT / "src" / "ummanu" / "automations" / "composition.py").is_file())
        offenders: list[str] = []
        for tree_root in ("src", "tests", "scripts"):
            for path in sorted((ROOT / tree_root).rglob("*.py")):
                relative = path.relative_to(ROOT).as_posix()
                offenders.extend(_imports_retired_agents_package(relative, path.read_text(encoding="utf-8")))
        self.assertEqual(offenders, [])

    def test_a_planted_import_of_the_retired_package_is_caught(self) -> None:
        for relative, source in (
            ("tests/test_planted.py", "from triggered_agents import __main__\n"),
            ("scripts/planted.py", "import triggered_agents.runtime.dispatch as d\n"),
            ("src/ummanu/planted.py", "def f():\n    from triggered_agents.agents import x\n"),
        ):
            with self.subTest(relative):
                self.assertEqual(len(_imports_retired_agents_package(relative, source)), 1)
        self.assertEqual(
            _imports_retired_agents_package("tests/x.py", "from ummanu.automations import composition\n"), []
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

    def test_ummanu_never_names_triggered_agents(self) -> None:
        """`grep -rn triggered_agents src/ummanu` is empty: code, data and comments alike."""
        package = ROOT / "src" / "ummanu"
        mentions: set[str] = set()
        for path in sorted(package.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if RETIRED_AGENTS_PACKAGE in line:
                    mentions.add(f"{path.relative_to(package).as_posix()}:{number}")
        self.assertEqual(mentions, set())


# Every place in `ummanu` that builds the *file* audit (`TaskAudit` over a data dir) rather than
# asking `ummanu.tasks.task_audit_for` for the audit owner of a card client, and the reason each
# one may. `requests`/`board_events` is the card audit (`docs/BOARD_STORE.md` §7.3), so a live
# reader built from the file journal of a data directory alone answers from a store the
# installation does not write: on 2026-09-10 that made a committed
# `report:done` invisible to the dispatcher (sprint:1437, secretary-1614), and it is the same shape
# as an empty command history or a false `not_found`. A new entry here is a new reader that decided
# its audit by default instead of by its client, so it is added deliberately with its reason or it
# is a defect.
#
# Since secretary-1673 the file audit class is gone, so the allowance is empty: the typed canon takes
# its audit owner as a required argument, and the fake host hands in an in-memory one.
FILE_AUDIT_CONSTRUCTIONS: dict[str, str] = {}

#: Where a live audit reader asks for its owner. Cards, Sprints and Products/Issues have one
#: implementation, PostgreSQL, and so one audit owner (`task_audit_for`).
LIVE_AUDIT_SELECTORS = {
    "checkpoint.py": "task_audit_for(",
    "task_commands.py": "task_audit_for(",
    "webproto/command_reads.py": "task_audit_for(",
    "webproto/ops.py": "task_audit_for(",
    "webproto/reads.py": "task_audit_for(",
    "webproto/sprint_reads.py": "task_audit_for(",
    "board/sql_host.py": "task_audit_for(",
    "sprints.py": "task_audit_for(",
    "data.py": "task_audit_for(",
    "dispatch/bootstrap.py": "task_audit_for(",
    "product_issues.py": "task_audit_for(",
}


def _source_modules() -> list[Path]:
    """Every Python module under `src/`, both packages."""
    return sorted((ROOT / "src").rglob("*.py"))


class FileAuditOwnershipTests(unittest.TestCase):
    """A live audit reader follows its card client, and the exceptions are named with their reasons."""

    def _constructions(self) -> dict[str, list[int]]:
        """Every call of a `TaskAudit` name in `src/ummanu`, by module and line."""
        found: dict[str, list[int]] = {}
        for path in sorted((ROOT / "src" / "ummanu").rglob("*.py")):
            tree = source_trees.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in source_trees.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                target = node.func
                if isinstance(target, ast.Name) and target.id == "TaskAudit":
                    key = str(path.relative_to(ROOT / "src" / "ummanu"))
                    found.setdefault(key, []).append(node.lineno)
        return found

    def test_only_the_named_modules_build_the_file_audit_directly(self) -> None:
        """Anything else reads a journal its installation's backend may never write."""
        offenders = sorted(set(self._constructions()) - set(FILE_AUDIT_CONSTRUCTIONS))
        self.assertEqual(
            offenders,
            [],
            "these modules build the file journal's TaskAudit from a data directory instead of "
            "asking ummanu.tasks.task_audit_for for the audit owner of their card client",
        )

    def test_every_named_module_still_builds_one(self) -> None:
        """The allowance is a statement about live code, not a list that outlives its reasons."""
        self.assertEqual(sorted(self._constructions()), sorted(FILE_AUDIT_CONSTRUCTIONS))

    def test_the_file_audit_is_gone_and_the_canon_names_its_owner(self) -> None:
        """No `TaskAudit` to build, and no canon that falls back to one (secretary-1673)."""
        from ummanu import tasks
        from ummanu.board.events import BoardEventCanon

        self.assertFalse(hasattr(tasks, "TaskAudit"))
        parameters = inspect.signature(BoardEventCanon.__init__).parameters
        self.assertEqual([name for name in parameters if name != "self"], ["audit"])
        self.assertIs(parameters["audit"].default, inspect.Parameter.empty)
        source = inspect.getsource(BoardEventCanon)
        for forbidden in ("data_dir", "events.ndjson", "TaskAudit"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_every_live_reader_construction_site_goes_through_the_selector(self) -> None:
        """The readers this card enumerated, each holding the selector call in its own source.

        Named rather than derived: these are the sites that had a data-dir-only audit and are the
        ones a regression would arrive at again. A module that stops calling `task_audit_for` is
        either a deliberate removal of a reader or the bypass coming back, and both belong in a diff
        that has to change this list.
        """
        for module, selector in LIVE_AUDIT_SELECTORS.items():
            source = (ROOT / "src" / "ummanu" / module).read_text(encoding="utf-8")
            with self.subTest(module=module):
                self.assertIn(selector, source, module)

    def test_the_card_audit_has_one_owner(self) -> None:
        """`task_audit_for` returns the SQL audit whatever it is handed; no card writer builds a file one."""
        from ummanu.board.sql_audit import SqlTaskAudit
        from ummanu.tasks import task_audit_for

        self.assertIsInstance(task_audit_for(mock.sentinel.client, "/nonexistent"), SqlTaskAudit)
        source = inspect.getsource(task_audit_for)
        self.assertIsNone(re.search(r"(?<!Sql)TaskAudit\(", source))

    def test_no_source_module_has_a_backend_branch(self) -> None:
        """Cards, Sprints and Products/Issues have one implementation, so nothing asks which one it holds."""
        for path in _source_modules():
            module = str(path.relative_to(ROOT / "src"))
            tree = source_trees.parse(path.read_text(encoding="utf-8"))
            reads = [
                node.lineno
                for node in source_trees.walk(tree)
                if (isinstance(node, ast.Attribute) and node.attr == "backend_kind")
                or (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "getattr"
                    and any(
                        isinstance(argument, ast.Constant) and argument.value == "backend_kind"
                        for argument in node.args
                    )
                )
            ]
            with self.subTest(module=module):
                self.assertEqual(reads, [], f"{module} reads a client's backend_kind")

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

    def test_the_product_issue_store_keeps_no_file_journal_guard(self) -> None:
        """The pre-cutover file-claim guard protected nothing after the importer copied every id.

        On 2026-09-22 the live `board/events.ndjson` (last written 2026-09-10) held 27,966 request
        ids, all but three already in SQL `requests`, and those three were dispatcher records, not
        Product/Issue ones; `board/pending-audit` was empty (secretary-1670).
        """
        from ummanu.board.sql_audit import SqlTaskAudit
        from ummanu.product_issues import ProductIssueStore

        self.assertFalse(hasattr(ProductIssueStore, "_require_sql_legacy_namespace_free"))
        self.assertNotIn("legacy_audit", inspect.getsource(ProductIssueStore))
        self.assertNotIn("legacy_audit", inspect.getsource(SqlTaskAudit))
        self.assertFalse(hasattr(SqlTaskAudit, "require_pending_layout"))

    def test_the_sprint_traversal_cannot_be_built_from_a_data_directory(self) -> None:
        """`_AuditOnce` takes records or an audit owner, and has no directory to fall back to."""
        from ummanu.sprints import _AuditOnce

        parameters = inspect.signature(_AuditOnce.__init__).parameters
        self.assertEqual([name for name in parameters if name != "self"], ["events", "audit"])
        self.assertEqual(_AuditOnce().events(), [])


class IndirectFileAuditReaderTests(unittest.TestCase):
    """No reader opens the file journal itself: the card history is the card audit's traversal.

    `secretary-1622`'s first submission is why this class exists: the product-run events had moved
    to `requests` while `ReadLayer.task_snapshot` and `task_events` still built
    `EventJournal(data_dir)`, so a migrated installation answered a card's history from a projection
    its writers never touch. The file reader is gone with the second card backend.
    """

    def test_the_file_event_reader_is_gone(self) -> None:
        from ummanu.webproto import journal

        self.assertFalse(hasattr(journal, "EventJournal"))

    def test_the_read_layer_selects_its_event_reader_from_its_client(self) -> None:
        """Both card-event operations read the owner the client names, and neither a file by default."""
        source = (ROOT / "src" / "ummanu" / "webproto" / "reads.py").read_text(encoding="utf-8")
        self.assertIn("task_audit_for(", source)
        self.assertIn("CommittedAudit(", source)
        for operation in ("def task_snapshot", "def task_events"):
            with self.subTest(operation=operation):
                body = source[source.index(operation) :]
                body = body[: body.index("\n    def ", 1)]
                self.assertNotIn("EventJournal(", body)

    def test_the_sql_reader_never_touches_a_path(self) -> None:
        """`CommittedAudit` has no data directory to read: it pages what its audit owner traverses."""
        from ummanu.webproto.journal import CommittedAudit

        parameters = inspect.signature(CommittedAudit.__init__).parameters
        self.assertEqual([name for name in parameters if name != "self"], ["audit", "backend"])
        source = inspect.getsource(CommittedAudit)
        for forbidden in ("open(", "Path(", "events.ndjson"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


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

    def test_the_legacy_host_module_is_gone(self) -> None:
        """The host `SqlCardClient` runs on has a neutral name, and no other module is its alias.

        An alias is a board module that only imports from `sql_host`: the shape the old host module
        would keep if it stayed behind as a compatibility name.
        """
        board = ROOT / "src" / "ummanu" / "board"
        self.assertTrue((board / "sql_host.py").exists())
        aliases: list[str] = []
        for path in sorted(board.glob("*.py")):
            if path.name in {"sql_host.py", "__init__.py"}:
                continue
            body = source_trees.parse(path.read_text(encoding="utf-8")).body
            imports_host = any(
                isinstance(node, ast.ImportFrom) and node.module == "ummanu.board.sql_host" for node in body
            )
            only_imports = all(
                isinstance(node, (ast.Import, ast.ImportFrom))
                or (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
                or (
                    isinstance(node, ast.Assign)
                    and [getattr(target, "id", "") for target in node.targets] == ["__all__"]
                )
                for node in body
            )
            if imports_host and only_imports:
                aliases.append(path.name)
        self.assertEqual(aliases, [])



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


def tearDownModule() -> None:
    source_trees.clear()
