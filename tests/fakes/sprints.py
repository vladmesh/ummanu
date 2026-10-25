"""The shared Sprint fixture: seed data and a real PostgreSQL store per test.

Sprints have one implementation, PostgreSQL (secretary-1670), so there is no in-memory sprint
board here. The seeds below are only input to `tests.sql_backend_fixtures.seed_client`.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

from tests.head_registry import write_installed_pair
from tests.observer_identity import bind_observer
from tests.sprint_close_fixtures import DROP_REASON, KEEP_OPEN_REASON
from tests.sql_backend_fixtures import CardStoreClient, card_store
from ummanu.board.sql_audit import SqlTaskAudit
from ummanu.board.sql_cards import SqlCardClient
from ummanu.product_issues import ProductIssueStore
from ummanu.sprint_observer import (
    head_choice,
)
from ummanu.sprints import (
    SPRINT_BOARD_NAME,
    SprintReader,
    SprintWriter,
)
from ummanu.tasks import TaskReader

# A close states a verdict on every issue its sprint declared, and every sprint this fixture
# opens declares `issue:open`. The tests below are about the rest of the close, so they give
# the verdict that writes nothing: the issue stays open, with the basis on the close record.
KEEP_THE_ISSUE_OPEN = {
    "issues": [{"ref": "issue:open", "verdict": "open", "reason": KEEP_OPEN_REASON}],
    "cards": [],
}


def drop_cards(*refs: str) -> dict:
    """Keep the fixture's issue open and take the named cards off the closing contract."""
    return {
        "issues": list(KEEP_THE_ISSUE_OPEN["issues"]),
        "cards": [{"ref": ref, "verdict": "drop", "reason": DROP_REASON} for ref in refs],
    }


#: The Pipeline's columns, in the numbering the seed rows below use.
SEED_COLUMNS = [
    {"id": 1, "title": "Issues"},
    {"id": 2, "title": "Ready"},
    {"id": 3, "title": "In progress"},
    {"id": 4, "title": "Validate"},
    {"id": 5, "title": "Blocked"},
    {"id": 6, "title": "Done"},
]


class SprintSeed:
    """The starting Pipeline of a sprint test: one Ready card, `secretary-12`, and nothing else.

    Seed input only (`tests.sql_backend_fixtures.seed_client`): Sprints have one implementation,
    PostgreSQL, so a sprint test runs on a real store and this is what is written into it.
    """

    def __init__(self) -> None:
        self.columns = [dict(column) for column in SEED_COLUMNS]
        self.lanes: list[dict[str, object]] = []
        self.tasks: list[dict[str, object]] = [
            {
                "id": 12,
                "project_id": 7,
                "reference": "ummanu-12",
                "title": "existing",
                "description": "",
                "column_id": 2,
                "position": 1,
                "swimlane_id": 0,
                "date_creation": "1720000000",
                "date_modification": "1720000000",
            }
        ]
        self.metadata: dict[int, dict[str, str]] = {12: {"project": "ummanu", "task_type": "code"}}
        self.comments: dict[int, list[dict[str, object]]] = {12: []}


def status_seed() -> SprintSeed:
    """The Pipeline a status read of sprints needs: one card, `secretary-510`, under key 12."""
    seed = SprintSeed()
    seed.tasks[0]["reference"] = "ummanu-510"
    return seed


class ProductSprintSeed(SprintSeed):
    """The same Pipeline, carrying Product and Issue records.

    A sprint names the Product it belongs to and the Issues it serves, so the seed holds one
    product with an open and a closed issue, plus a second product to prove a foreign issue is
    refused.
    """

    def __init__(self) -> None:
        super().__init__()
        self._record(
            20,
            "product:ummanu",
            "Ummanu",
            {
                "record_type": "product",
                "product_id": "ummanu",
                "product_projects": json.dumps(["ummanu", "secretary-instance"]),
            },
        )
        self._record(
            21,
            "product:other",
            "Other",
            {
                "record_type": "product",
                "product_id": "other",
                "product_projects": json.dumps(["other"]),
            },
        )
        self._record(
            22,
            "issue:open",
            "Open issue",
            {
                "record_type": "issue",
                "issue_product": "ummanu",
                "issue_kind": "feature",
                "issue_priority": "P1",
            },
        )
        self._record(
            23,
            "issue:done",
            "Closed issue",
            {
                "record_type": "issue",
                "issue_product": "ummanu",
                "issue_kind": "bug",
                "issue_priority": "P2",
                "issue_closed_reason": "resolved",
            },
            closed=True,
        )
        self._record(
            24,
            "issue:foreign",
            "Issue of another product",
            {
                "record_type": "issue",
                "issue_product": "other",
                "issue_kind": "bug",
                "issue_priority": "P1",
            },
        )

    def _record(
        self, task_id: int, reference: str, title: str, metadata: dict, *, closed: bool = False
    ) -> None:
        self.tasks.append(
            {
                "id": task_id,
                "project_id": 7,
                "reference": reference,
                "title": title,
                "description": "",
                "column_id": 1,
                "position": task_id,
                "swimlane_id": 0,
                "is_active": 0 if closed else 1,
                "date_creation": "1720000000",
                "date_modification": "1720000000",
            }
        )
        self.metadata[task_id] = dict(metadata)
        self.comments[task_id] = []


class SprintStoreClient(CardStoreClient):
    """The card store client, plus the final row probe some sprint cases observe."""

    @property
    def tasks(self) -> list[dict]:
        """Every row of both boards, live and archived, as the board answers it."""
        return [
            row
            for board_id in (1, 2)
            for status_id in (1, 0)
            for row in SqlCardClient.call(self, "getAllTasks", project_id=board_id, status_id=status_id)
        ]


def sprint_store(test: unittest.TestCase, seed: Any = None, *, instance_dir: Any = None) -> SprintStoreClient:
    """A real store of this test's own, seeded with the sprint Pipeline (`ProductSprintSeed`)."""
    return card_store(
        test,
        seed if seed is not None else ProductSprintSeed(),
        instance_dir=instance_dir,
        client_class=SprintStoreClient,
    )


def _write_project_registry(root: Path, *projects: str) -> Path:
    instance = root / "instance"
    (instance / "projects").mkdir(parents=True, exist_ok=True)
    for project in projects:
        repo = root / "project-repos" / project
        repo.mkdir(parents=True, exist_ok=True)
        (instance / "projects" / f"{project}.yaml").write_text(
            f"id: {project}\nrepo: {repo}\nenabled: true\nadapter: ummanu\ndefault_branch: main\n",
            encoding="utf-8",
        )
    # A config that validates, because the reads of this installation are reached through
    # `ummanu.webproto`, and every operation of that layer resolves the instance before it reads
    # anything. A fixture instance without one is not a smaller installation, it is one no protocol
    # operation can be run against.
    (instance / "instance.yaml").write_text(
        "version: 1\nname: test\n"
        f"data_dir: {root}\n"
        "offsite:\n  instance_remote: git@example.invalid:x/y.git\n",
        encoding="utf-8",
    )
    _write_head_registry(instance)
    return instance


# Opening a sprint resolves its declared observer against this installation's head snapshot, the
# same file the dispatcher launches from, so the fixture instance owns a real one. `retired-observer`
# is deliberately absent: it is the profile the tests declare when they want an unknown one.
HEAD_SNAPSHOT = "resources:\n  openai-sub:\n    account: openai-subscription\n  claude-sub:\n    account: claude-subscription\nprofiles:\n  codex-observer:\n    adapter: codex\n    resource: openai-sub\n  claude-observer:\n    adapter: claude\n    resource: claude-sub\nrole_defaults:\n  new_card: codex-observer\n  reviewer: codex-observer\n  observer: codex-observer\n"


def _write_head_registry(instance: Path) -> Path:
    return write_installed_pair(instance, HEAD_SNAPSHOT)


class SprintBackendFixture:
    """The one factory every sprint suite shares: a real store seeded with `ProductSprintSeed`."""

    def make_sprint_client(self) -> SprintStoreClient:
        root = getattr(getattr(self, "tmp", None), "name", None)
        return sprint_store(self, instance_dir=root)  # type: ignore[arg-type]

    def make_ownership_client(self) -> SprintStoreClient:
        """A store holding only the Products and Issues, and no card."""
        seed = ProductSprintSeed()
        seed.tasks = [row for row in seed.tasks if str(row.get("reference", "")).startswith(("product:", "issue:"))]
        root = getattr(getattr(self, "tmp", None), "name", None)
        return sprint_store(self, seed, instance_dir=root)  # type: ignore[arg-type]

    def make_empty_sprint_client(self) -> SprintStoreClient:
        """A restore target: the Products and Issues a restored sprint names, and no card or sprint."""
        return self.make_ownership_client()

    make_target_client = make_empty_sprint_client

    @staticmethod
    def persisted_record_count(client: Any) -> int:
        """How many cards and sprints a store holds, live or archived."""
        return int(client._query("SELECT count(*) FROM tasks")[0][0]) + int(
            client._query("SELECT count(*) FROM sprints")[0][0]
        )


class SprintFixture(SprintBackendFixture, unittest.TestCase):
    """The fixture boundary of the shared sprint contract, over a real store.

    Shared test bodies speak only through SprintReader, SprintWriter, TaskReader/TaskWriter, or
    these helpers.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.instance = _write_project_registry(
            Path(self.tmp.name),
            "ummanu",
            "secretary-instance",
            "other",
        )
        self.client = self.make_sprint_client()
        self.writer = SprintWriter(  # type: ignore[arg-type]
            self.client,
            data_dir=self.tmp.name,
            instance=self.instance,
        )

    #: Where a `ummanu sprint ...` command asks the backend switch for its board: the
    #: `board_client` name bound by the command group and by each protocol layer it builds.
    BOARD_CLIENT_SEAMS: ClassVar[tuple[str, ...]] = (
        "ummanu.sprint_commands.board_client",
        "ummanu.webproto.sprint_reads.board_client",
        "ummanu.webproto.sprint_ops.board_client",
    )

    @contextlib.contextmanager
    def board_injected(self) -> Iterator[None]:
        """Serve this fixture's client wherever a CLI command asks `board_client` for one.

        `board_client` is the one constructor of a board client, so the command's client is
        replaced there.
        """
        with contextlib.ExitStack() as stack:
            for target in self.BOARD_CLIENT_SEAMS:
                stack.enter_context(mock.patch(target, return_value=self.client))
            yield

    def sprint_reader(self) -> SprintReader:
        return SprintReader(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]

    def sprint(
        self, reference: str, *, include_cards: bool = False, include_delivery: bool = False,
    ) -> dict[str, Any]:
        return self.sprint_reader().show(reference, include_cards=include_cards, include_delivery=include_delivery)

    def sprints(self) -> list[dict[str, Any]]:
        return self.sprint_reader().list()

    def ensure_backend_ready(self) -> None:
        """Prepare the sprint store before concurrent writers enter the backend."""
        self.sprint_reader().list()

    def sprint_record_count(self, reference: str | None = None) -> int:
        """Count persisted sprint records through the client boundary, active and archived."""
        project = self.client.call("getProjectByName", name=SPRINT_BOARD_NAME)
        if not isinstance(project, dict) or not project.get("id"):
            return 0
        rows = [
            row
            for status_id in (1, 0)
            for row in self.client.call("getAllTasks", project_id=int(project["id"]), status_id=status_id)
        ]
        if reference is None:
            return len(rows)
        return sum(row.get("reference") == reference for row in rows)

    def transaction_state(self) -> dict[str, int | bool]:
        """Observe recovery ownership without knowing where the store keeps it."""
        return self.writer.transactions.status()

    def arrange_metadata(self, reference: str, **values: object) -> None:
        """Arrange persisted sprint values through the product's own writes.

        A budget is arranged as the charges that make it up, and every other value through the
        verbatim `restore` a checkpoint recovery uses.
        """
        budget = values.pop("sprint_budget", None)
        if budget is not None:
            document = json.loads(str(budget))
            for event_type, count in (document.get("by_type") or {}).items():
                for occurrence in range(int(count)):
                    self.writer.record_budget(
                        role="steward",
                        actor="fixture",
                        reference=reference,
                        event_type=str(event_type),
                        request_id=f"fixture-budget-{reference}-{event_type}-{occurrence}",
                    )
        if values:
            identity = hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()[:16]
            self.writer.restore(
                reference=reference,
                values={str(key): str(value) for key, value in values.items()},
                request_id=f"fixture-restore-{reference}-{identity}",
            )

    def arrange_card_sprint(self, reference: str, sprint: str) -> None:
        """Arrange an already-linked Card without making a cursor write own that relation."""
        project = self.client.call("getProjectByName", name="Pipeline")
        if not isinstance(project, dict) or not project.get("id"):
            self.fail("Pipeline board is not visible while arranging a linked Card")
        row = self.client.call(
            "getTaskByReference", project_id=int(project["id"]), reference=reference
        )
        if not isinstance(row, dict):
            self.fail(f"Card is not visible while arranging {reference}")
        result = self.client.call(
            "saveTaskMetadata", task_id=int(row["id"]), values={"sprint_ref": sprint}
        )
        if result is not True:
            self.fail(f"backend refused fixture Sprint link for {reference}")

    def arrange_historical_sprint(self, reference: str, *, status: str = "closed") -> dict[str, Any]:
        return self.writer.restore_create(
            reference=reference,
            goal="historical",
            status=status,
            request_id=f"fixture-{reference}",
            observer=head_choice("codex-observer"),
        )["sprint"]

    def arrange_issue_closed(self, reference: str = "issue:open") -> None:
        self.product_issue_store().close_issue(
            reference=reference,
            reason="resolved",
            actor="fixture",
            request_id=f"fixture-close-{reference}",
        )

    def product_issue_store(self) -> ProductIssueStore:
        """The public ownership contract used to arrange Product and Issue records."""
        return ProductIssueStore(  # type: ignore[arg-type]
            self.client, data_dir=self.tmp.name, instance=self.instance
        )

    def arrange_product(self, product_id: str, *, projects: list[str]) -> dict[str, Any]:
        """Arrange ownership through the public Product/Issue mutation contract."""
        return self.product_issue_store().create_product(
            product_id=product_id,
            projects=projects,
            title=product_id.title(),
            description="",
            actor="fixture",
            request_id=f"fixture-product-{product_id}",
        )

    def arrange_issue(self, name: str, *, product: str) -> dict[str, Any]:
        """Arrange an Issue and return its backend-independent normalized identity."""
        return self.product_issue_store().create_issue(
            product=product,
            issue_kind="feature",
            priority="P1",
            title=name,
            description="",
            actor="fixture",
            request_id=f"fixture-issue-{product}-{name}",
        )

    def arrange_record_active(self, reference: str, *, active: bool) -> None:
        project = self.client.call("getProjectByName", name="Pipeline")
        if not isinstance(project, dict) or not project.get("id"):
            self.fail("Pipeline is not visible")
        row = self.client.call("getTaskByReference", project_id=int(project["id"]), reference=reference)
        if not isinstance(row, dict):
            self.fail(f"record is not visible while arranging {reference}")
        if not active:
            result = self.client.call("closeTask", task_id=int(row["id"]))
            if result is not True:
                self.fail(f"backend refused fixture archive for {reference}")

    def arrange_pipeline_metadata(self, reference: str, **values: object) -> None:
        project = self.client.call("getProjectByName", name="Pipeline")
        if not isinstance(project, dict) or not project.get("id"):
            self.fail("Pipeline is not visible")
        row = self.client.call("getTaskByReference", project_id=int(project["id"]), reference=reference)
        if not isinstance(row, dict):
            self.fail(f"record is not visible while arranging {reference}")
        result = self.client.call("saveTaskMetadata", task_id=int(row["id"]), values=values)
        if result is not True:
            self.fail(f"backend refused fixture metadata for {reference}")

    def task(self, reference: str) -> dict[str, Any]:
        return TaskReader(self.client).show(reference)  # type: ignore[arg-type]

    def record_is_active(self, reference: str) -> bool:
        project = self.client.call("getProjectByName", name="Pipeline")
        if not isinstance(project, dict) or not project.get("id"):
            self.fail("Pipeline is not visible")
        for status_id, active in ((1, True), (0, False)):
            rows = self.client.call("getAllTasks", project_id=int(project["id"]), status_id=status_id)
            if any(row.get("reference") == reference for row in rows):
                return active
        self.fail(f"record is not visible: {reference}")

    def record_comments(self, reference: str) -> list[str]:
        project = self.client.call("getProjectByName", name="Pipeline")
        if not isinstance(project, dict) or not project.get("id"):
            self.fail("Pipeline is not visible")
        row = self.client.call("getTaskByReference", project_id=int(project["id"]), reference=reference)
        if not isinstance(row, dict):
            self.fail(f"record is not visible: {reference}")
        comments = self.client.call("getAllComments", task_id=int(row["id"]))
        return [str(comment["comment"]) for comment in comments]

    @contextlib.contextmanager
    def named_failure(
        self,
        boundary: str,
        *,
        result: object = False,
        error: Exception | None = None,
    ) -> Iterator[None]:
        """Fail one semantic SprintWriter boundary without naming an RPC in a test body."""
        methods = {
            "record_create": "createTask",
            "record_metadata": "saveTaskMetadata",
            "record_reference": "updateTask",
            "record_remove": "removeTask",
            "record_comment": "createComment",
            "record_archive": "closeTask",
        }
        method = methods[boundary]
        original = self.client.call
        armed = True

        def call(method_name: str, **params: object) -> object:
            nonlocal armed
            if armed and method_name == method:
                armed = False
                if error is not None:
                    raise error
                return result
            return original(method_name, **params)

        self.client.call = call  # type: ignore[method-assign]
        try:
            yield
        finally:
            self.client.call = original  # type: ignore[method-assign]

    def _create(self, **kwargs) -> dict:
        """Open a sprint that owns the fixture's product, open issue and project."""
        for field, value in (
            ("role", "po"),
            ("actor", "operator"),
            ("product", "ummanu"),
            ("issues", ["issue:open"]),
            ("projects", ["ummanu"]),
            ("observer", head_choice("codex-observer")),
        ):
            kwargs.setdefault(field, value)
        created = self.writer.create(**kwargs)
        # The calls that follow act as this sprint's observer head, which is bound to it the way
        # the dispatcher binds a head it launches.
        bind_observer(self, str(created["sprint"]["ref"]))
        return created

    def _events(self) -> list[dict]:
        return SqlTaskAudit(self.client).events()
