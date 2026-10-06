from __future__ import annotations

import ast
import inspect
import textwrap
import unittest
from pathlib import Path

from tests import (
    test_sprint_executors,
    test_sprint_listing_budget,
    test_sprint_restore,
    test_sprints,
)
from tests.fakes.sprints import SprintBackendFixture, SprintFixture
from ummanu.tasks import TaskError

SUITES = (
    test_sprints,
    test_sprint_executors,
    test_sprint_restore,
    test_sprint_listing_budget,
)
EXPECTED_METHODS = {
    "tests.test_sprints": 113,
    "tests.test_sprint_executors": 21,
    "tests.test_sprint_restore": 25,
    "tests.test_sprint_listing_budget": 4,
}
BEFORE_REACH_INS = {
    "client.calls": 34,
    "client.tasks": 28,
    "_sprint_rows": 18,
    "client.metadata": 18,
    "_transactions": 12,
    "client.comments": 6,
}
AFTER_REACH_INS = {
    "client.calls": 0,
    "client.tasks": 0,
    "_sprint_rows": 0,
    "client.metadata": 0,
    "_transactions": 0,
    "client.comments": 0,
}
EXPECTED_CLASSES = {
    "tests.test_sprints.SprintOwnershipTests": 21,
    "tests.test_sprints.TwoOpenSprintAdmissionTests": 18,
    "tests.test_sprints.TwoOpenSprintIsolationTests": 9,
    "tests.test_sprints.SprintTests": 21,
    "tests.test_sprints.SprintStatusHeadlessCommandTests": 3,
    "tests.test_sprints.SprintAuditTraversalTests": 7,
    "tests.test_sprints.SprintSingleWriterGuardTests": 13,
    "tests.test_sprints.SprintReservedProjectGuardTests": 6,
    "tests.test_sprints.SprintCloseDecisionTests": 12,
    "tests.test_sprints.CloseDecisionFileTests": 3,
    "tests.test_sprint_executors.ExecutorValueTests": 2,
    "tests.test_sprint_executors.SprintExecutorPinTests": 5,
    "tests.test_sprint_executors.ObserverPromptExecutorTests": 3,
    "tests.test_sprint_executors.SprintCardExecutorTests": 4,
    "tests.test_sprint_executors.SprintExecutorRecoveryTests": 4,
    "tests.test_sprint_executors.CardEditExecutorTests": 3,
    "tests.test_sprint_restore.SprintRestoreTests": 25,
    "tests.test_sprint_listing_budget.SprintListingBudgetTests": 4,
}

# The complete permitted locations of a storage reach-in: the listing-budget cases, whose subject is
# what the store is asked. Sprints have one implementation, PostgreSQL (secretary-1670), and the 33
# cases that were allowed one because their subject was the retired transport or its half-applied
# filesystem transaction were deleted with it.
ALLOWED_STORAGE_LOCATIONS: frozenset[str] = frozenset(
    f"tests.test_sprint_listing_budget.SprintListingBudgetTests.{name}"
    for name in (
        "test_the_listing_costs_the_same_whether_it_lists_two_sprints_or_forty",
        "test_the_listing_reads_no_sprint_comments",
        "test_one_listing_traverses_the_committed_audit_at_most_once",
        "test_watching_one_sprint_costs_what_listing_them_all_does",
    )
)


def _methods(module: object) -> dict[str, object]:
    found: dict[str, object] = {}
    for _name, owner in inspect.getmembers(module, inspect.isclass):
        if owner.__module__ != module.__name__:
            continue
        for name, value in owner.__dict__.items():
            if name.startswith("test_") and callable(value):
                found[f"{module.__name__}.{owner.__name__}.{name}"] = value
    return found


# The retired fake sprint classes are gone from the tree, so a case naming one fails on its own;
# what is left to forbid by name is the board helper they were built on.
FORBIDDEN_NAMES = {"ensure_sprint_board"}
FORBIDDEN_RPC = {
    "getProjectByName",
    "getColumns",
    "getAllTasks",
    "getTaskByReference",
    "getTaskMetadata",
    "getAllComments",
    "createProject",
    "createTask",
    "updateTask",
    "saveTaskMetadata",
    "createComment",
    "closeTask",
    "removeTask",
    "moveTaskPosition",
}


def _method_findings(method: object, *, include_rpc: bool = True) -> set[str]:
    body = textwrap.dedent(inspect.getsource(method))
    tree = ast.parse(body)
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} & FORBIDDEN_NAMES
    rpc = (
        {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)} & FORBIDDEN_RPC
        if include_rpc
        else set()
    )
    reach_ins = {
        token
        for token in (
            "self.client.calls",
            "self.client.tasks",
            "self.client.metadata",
            "self.client.comments",
            "self._sprint_rows()",
            "self._transactions()",
        )
        if token in body
    }
    storage_attrs = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or node.attr not in {
            "calls",
            "tasks",
            "metadata",
            "comments",
        }:
            continue
        owner = node.value
        if (isinstance(owner, ast.Name) and owner.id in {"client", "board", "source", "target"}) or (
            isinstance(owner, ast.Attribute)
            and owner.attr in {"client", "board", "source", "target"}
        ):
            storage_attrs.add(node.attr)
    return names | rpc | reach_ins | storage_attrs


def _owner_of(method: object) -> type:
    module = inspect.getmodule(method)
    if module is None:
        raise AssertionError(f"no module for {method!r}")
    owner_name = method.__qualname__.split(".", 1)[0]  # type: ignore[attr-defined]
    return getattr(module, owner_name)


def _reachable_helpers(owner: type, root: object) -> list[tuple[tuple[str, ...], object]]:
    pending = [((root.__name__,), root)]  # type: ignore[attr-defined]
    suite_module = root.__module__  # type: ignore[attr-defined]
    reached: list[tuple[tuple[str, ...], object]] = []
    seen: set[object] = set()
    while pending:
        path, method = pending.pop()
        if method in seen:
            continue
        seen.add(method)
        reached.append((path, method))
        tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
        helper_names = {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        }
        for name in sorted(helper_names):
            helper = getattr(owner, name, None)
            if inspect.isfunction(helper) and helper.__module__ == suite_module:
                pending.append((path + (name,), helper))
    return reached


def _portable_storage_violations(
    methods: dict[str, object], allowed: frozenset[str]
) -> list[str]:
    violations: list[str] = []
    for qualified, method in methods.items():
        if qualified in allowed:
            continue
        owner = _owner_of(method)
        for path, reached in _reachable_helpers(owner, method):
            found = sorted(_method_findings(reached, include_rpc=len(path) == 1))
            if found:
                violations.append(f"{qualified} via {' -> '.join(path)}: {', '.join(found)}")
    return violations


class _HelperBypass:
    def test_portable(self) -> None:
        self._writes()

    def _writes(self) -> object:
        return self.client.calls  # type: ignore[attr-defined]


class SprintFixtureGuards(unittest.TestCase):
    def test_every_method_is_counted(self) -> None:
        """204 originally; secretary-1669 removed the 24 cases that wrote a card through the retired
        implementation.

        secretary-1670 removed the 33 remaining cases that only the retired Sprint implementation
        had, with it, moved three SQL-only cases in from tests/test_sprints_sql_backend.py, and
        rewrote the four listing-budget cases in store statements: 180 - 33 + 3 + 4 = 154.

        secretary-1712 added two single-writer guard cases for the steward's own report card: 156.

        secretary-1765 added the observer's end-to-end close of its own sprint: 157.

        secretary-1842 added two local-run declaration cases and two snapshot/restore cases: 161.

        ummanu-69 added the quoted owner-decision and paid-budget roundtrip case: 162.

        ummanu-92 added close/retry preservation of previously archived cards and cleanup: 163.
        """
        methods = {qualified: value for module in SUITES for qualified, value in _methods(module).items()}
        by_module = {module.__name__: len(_methods(module)) for module in SUITES}
        self.assertEqual(by_module, EXPECTED_METHODS)
        self.assertEqual(len(methods), 163)
        by_class: dict[str, int] = {}
        for qualified in methods:
            owner = qualified.rsplit(".", 1)[0]
            by_class[owner] = by_class.get(owner, 0) + 1
        self.assertEqual(by_class, EXPECTED_CLASSES)

    def test_saved_before_inventory_is_reproducible(self) -> None:
        roots = Path(__file__).parent
        source = "\n".join(
            (roots / name).read_text(encoding="utf-8")
            for name in (
                "test_sprints.py",
                "test_sprint_executors.py",
                "test_sprint_restore.py",
                "test_sprint_listing_budget.py",
            )
        )
        actual = {
            "client.calls": source.count("self.client.calls"),
            "client.tasks": source.count("self.client.tasks"),
            "_sprint_rows": source.count("self._sprint_rows()"),
            "client.metadata": source.count("self.client.metadata"),
            "_transactions": source.count("self._transactions()"),
            "client.comments": source.count("self.client.comments"),
        }
        # This assertion deliberately records the old report's inventory.  Once the bodies are
        # neutralized, the exact current count is reported by the next test and this historical
        # value remains reviewable here rather than being inferred from a weaker total.
        self.assertLessEqual(sum(actual.values()), sum(BEFORE_REACH_INS.values()))
        self.assertEqual(actual, AFTER_REACH_INS)
        # The saved report and TASK.md call this 118, but their six reproduced category counts
        # add up to 116.  Keep the primary counts, and make the arithmetic discrepancy explicit.
        self.assertEqual(sum(BEFORE_REACH_INS.values()), 116)

    def test_portable_bodies_do_not_reach_into_fake_or_storage_layout(self) -> None:
        methods = {qualified: value for module in SUITES for qualified, value in _methods(module).items()}
        self.assertEqual(_portable_storage_violations(methods, ALLOWED_STORAGE_LOCATIONS), [])

    def test_portable_helper_indirection_cannot_bypass_the_storage_guard(self) -> None:
        qualified = f"{_HelperBypass.__module__}.{_HelperBypass.__qualname__}.test_portable"
        violations = _portable_storage_violations(
            {qualified: _HelperBypass.test_portable}, frozenset()
        )
        self.assertEqual(len(violations), 1)
        self.assertIn("test_portable -> _writes", violations[0])
        self.assertIn("self.client.calls", violations[0])

    def test_portable_fixture_helpers_do_not_reach_into_fake_storage(self) -> None:
        allowed = {"make_sprint_client"}
        violations: list[str] = []
        for name, method in SprintFixture.__dict__.items():
            if name in allowed or not callable(method):
                continue
            body = textwrap.dedent(inspect.getsource(method))
            found = sorted(
                token
                for token in (
                    "self.client.calls",
                    "self.client.tasks",
                    "self.client.metadata",
                    "self.client.comments",
                    "self.client._record",
                )
                if token in body
            )
            if found:
                violations.append(f"{name}: {', '.join(found)}")
        self.assertEqual(violations, [])

    def test_every_backend_dependent_portable_suite_uses_the_one_factory_seam(self) -> None:
        owners = (
            test_sprints.SprintOwnershipTests,
            test_sprints.TwoOpenSprintAdmissionTests,
            test_sprints.TwoOpenSprintIsolationTests,
            test_sprints.SprintTests,
            test_sprints.SprintStatusHeadlessCommandTests,
            test_sprints.SprintAuditTraversalTests,
            test_sprints.SprintSingleWriterGuardTests,
            test_sprints.SprintReservedProjectGuardTests,
            test_sprints.SprintCloseDecisionTests,
            test_sprint_executors.SprintExecutorPinTests,
            test_sprint_executors.SprintCardExecutorTests,
            test_sprint_executors.SprintExecutorRecoveryTests,
            test_sprint_executors.CardEditExecutorTests,
            test_sprint_restore.SprintRestoreTests,
        )
        self.assertTrue(all(issubclass(owner, SprintBackendFixture) for owner in owners))


class SprintFixtureBehaviorTests(SprintFixture):
    def test_named_failure_injects_one_semantic_boundary_failure(self) -> None:
        with self.named_failure("record_create"), self.assertRaises(TaskError) as refused:
            self._create(goal="fixture failure", reference="sprint:fixture-failure")

        # The store rolls the whole create back, so the refusal owes no repair (`audit_pending` was
        # the retired implementation's half-applied create).
        self.assertEqual(refused.exception.code, "backend_error")
        self.assertEqual(self.sprint_record_count("sprint:fixture-failure"), 0)
