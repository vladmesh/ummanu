from __future__ import annotations

import ast
import inspect
import textwrap
import unittest

from tests import source_trees, test_product_issues as product_issue_tests
from tests.product_issue_fixtures import ProductIssueFixture
from ummanu.tasks import TaskError

SWIMLANE_METHODS = {
    name
    for name, value in product_issue_tests.ProductIssueSwimlaneTests.__dict__.items()
    if name.startswith("test_") and callable(value)
}
STORE_METHODS = {
    name
    for name, value in product_issue_tests.ProductIssueStoreTests.__dict__.items()
    if name.startswith("test_") and callable(value)
}


class ProductIssueFixtureGuards(unittest.TestCase):
    def test_every_method_is_counted(self) -> None:
        self.assertEqual(len(SWIMLANE_METHODS), 5)
        self.assertEqual(len(STORE_METHODS), 15)

    def test_bodies_do_not_reach_into_board_storage(self) -> None:
        forbidden_attrs = {
            "tasks",
            "metadata",
            "comments",
            "swimlanes",
            "calls",
            "call",
            "lane_store",
            "lane_binding_for",
            "_store",
            "_created",
            "_lane_names",
        }
        forbidden_names = {"client"}
        forbidden_rpc = {
            "getTaskByReference",
            "getActiveSwimlanes",
            "getAllTasks",
            "createTask",
            "updateTask",
            "saveTaskMetadata",
            "createComment",
            "closeTask",
            "addSwimlane",
        }
        portable = {
            product_issue_tests.ProductIssueStoreTests: STORE_METHODS,
            product_issue_tests.ProductIssueSwimlaneTests: SWIMLANE_METHODS,
        }
        violations: list[str] = []
        for owner, names in portable.items():
            for name in sorted(names):
                tree = source_trees.parse(textwrap.dedent(inspect.getsource(getattr(owner, name))))
                attrs = {
                    node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
                } & forbidden_attrs
                names_used = {
                    node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
                } & forbidden_names
                rpc_used = {
                    node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)
                } & forbidden_rpc
                found = sorted(attrs | names_used | rpc_used)
                if found:
                    violations.append(f"{owner.__name__}.{name}: {', '.join(found)}")
        self.assertEqual(violations, [])


class ProductIssueFixtureBehaviorTests(ProductIssueFixture, unittest.TestCase):
    def test_named_failure_injects_one_supported_boundary_failure(self) -> None:
        with self.named_failure("record_create"), self.assertRaises(TaskError) as refused:
            self.create_product(
                product_id="ummanu",
                projects=["ummanu"],
                title="Ummanu",
                description="",
                actor="po",
                request_id="fixture-failure",
            )

        # The store rolls the create back whole, so the refusal is a rolled-back transaction
        # (`backend_rejected` was the retired implementation's terminal `false` reply).
        self.assertEqual(refused.exception.code, "backend_error")
        self.assertEqual(self.record_count("product:ummanu"), 0)


if __name__ == "__main__":
    unittest.main()
