"""The sprint form, the sprint page and the cross-origin guard, driven as a person drives them.

Every test here goes through :class:`ummanu.web.app.WebApp` — the same object the socket handler
calls — over fakes of the sprint layer. There is no live installation, no board and no head
registry, and that is the point twice over: what is being pinned is the *transport*, and the layer
it calls already has its own suite (`tests/test_web_sprint_protocol.py`) proving the rules those
fakes stand in for.

The fakes are not permissive stand-ins. `FakeSprintOps` holds the two properties the transport is
built on top of and would otherwise be able to fake past: a request id owns one sprint, and a
create that fails after the row exists is repeated rather than restarted. A transport that
regenerated its request id per POST, or that answered a partial failure by offering a fresh form,
fails here.
"""

from __future__ import annotations

import json
import re
import unittest
from http.client import HTTPConnection
from threading import Thread
from typing import Any
from unittest import mock
from urllib.parse import quote, urlencode

from tests.webproto_sprint_fixtures import OBSERVER_PROFILE as PROTOCOL_OBSERVER, SprintProtocolFixture
from ummanu.web.app import ROUTES, WebApp, cross_origin_reason
from ummanu.web.server import build_server
from ummanu.webproto.errors import (
    OperationPending,
    OwnerConflict,
    ReadError,
    TaskNotFound,
    ValidationRefused,
)
from ummanu.webproto.runs import RunStoreError
from ummanu.webproto.sprint_ops import PENDING_REASON
from ummanu.webproto.sprint_requests import SPRINT_CREATE_OPERATION, SprintRequestStore

OBSERVER_PROFILE = "claude-observer"
WORKER_PROFILE = "codex-product-worker"
REVIEWER_PROFILE = "claude-product-reviewer"

#: The profiles the fake registry holds, in the shape `sprint_options` publishes them.
HEADS = [
    {
        "id": OBSERVER_PROFILE,
        "label": "claude · opus · high effort",
        "adapter": "claude",
        "model": "opus",
        "effort": "high",
        "resource": "claude-sub",
        "observer": True,
        "observer_reason": None,
        "role_default_for": ["observer"],
    },
    {
        "id": WORKER_PROFILE,
        "label": "codex · gpt-5.6-terra · default effort",
        "adapter": "codex",
        "model": "gpt-5.6-terra",
        "effort": "default",
        "resource": "openai-sub",
        "observer": False,
        "observer_reason": "this profile is not eligible to observe",
        "role_default_for": [],
    },
    {
        "id": REVIEWER_PROFILE,
        "label": "claude · opus · high effort",
        "adapter": "claude",
        "model": "opus",
        "effort": "high",
        "resource": "claude-sub",
        "observer": True,
        "observer_reason": None,
        "role_default_for": [],
    },
]


def available() -> dict[str, Any]:
    return {"state": "available", "reason": None, "observed_at": "2026-09-06T00:00:00Z"}


def unavailable(reason: str) -> dict[str, Any]:
    return {"state": "unavailable", "reason": reason, "observed_at": "2026-09-06T00:00:00Z"}


def options_document(**overrides: Any) -> dict[str, Any]:
    """A `sprint_options` document, as the read layer publishes one."""
    document = {
        "schema_version": 1,
        "kind": "sprint_options",
        "observed_at": "2026-09-06T00:00:00Z",
        "products": {
            "source": available(),
            "items": [
                {"id": "ummanu", "label": "Ummanu", "ref": "product:1", "projects": ["ummanu"]},
                {"id": "codegen", "label": "Codegen", "ref": "product:2", "projects": ["codegen"]},
            ],
        },
        "issues": {
            "source": available(),
            "items": [
                {
                    "ref": "issue:web",
                    "label": "the web needs a sprint form",
                    "product": "ummanu",
                    "kind": "feature",
                    "priority": None,
                },
                {
                    "ref": "issue:codegen",
                    "label": "codegen needs a template",
                    "product": "codegen",
                    "kind": None,
                    "priority": None,
                },
            ],
        },
        "projects": {
            "source": available(),
            "items": [
                {"id": "ummanu", "label": "ummanu", "reserved_by": []},
                {"id": "codegen", "label": "codegen", "reserved_by": ["sprint:7"]},
                {"id": "orca", "label": "orca", "reserved_by": None},
            ],
        },
        "heads": {
            "source": available(),
            "items": [dict(head) for head in HEADS],
            "observer": {"none": "none", "default": OBSERVER_PROFILE},
            "role_defaults": {"observer": OBSERVER_PROFILE},
            "executor_roles": ["worker", "reviewer"],
        },
    }
    document.update(overrides)
    return document


def sprint_document(
    reference: str = "sprint:1",
    *,
    launch: str = "not_started",
    observer: str | None = OBSERVER_PROFILE,
    worker: str | None = None,
    reviewer: str | None = None,
    record: dict[str, Any] | None = None,
    launch_source: dict[str, Any] | None = None,
    reason: str = "the sprint is saved and the production tick holds no observer for it yet",
    resume: dict[str, Any] | None = None,
    current_task: str | None = None,
    work: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A `sprint_state` document, as the read layer publishes one."""

    def pin(profile: str | None) -> dict[str, Any]:
        return {"state": "pinned", "profile": profile} if profile else {"state": "unset"}

    declared = (
        {"state": "declared", "value": {"kind": "head", "profile": observer}, "profile": observer}
        if observer
        else {"state": "declared", "value": {"kind": "none"}, "profile": None}
    )
    return {
        "schema_version": 1,
        "kind": "sprint",
        "observed_at": "2026-09-06T00:00:00Z",
        "ref": reference,
        **({"work": work} if work else {}),
        "sprint": {
            "source": available(),
            "value": {
                "ref": reference,
                "goal": "Make the web a client of the sprint contract",
                "definition_of_done": "the form and the page exist and are tested",
                "status": "open",
                "product": "ummanu",
                "issues": ["issue:web"],
                "reservations": ["ummanu"],
                "repositories": [],
                "current_task": current_task,
                "executors": {"worker": pin(worker), "reviewer": pin(reviewer)},
                "resume": resume,
                "budget": None,
                "audit": None,
            },
        },
        "observer": {
            "declared": declared,
            "launch": {
                "source": launch_source or available(),
                "state": launch,
                "reason": reason,
                "record": record,
            },
        },
    }


class FakeSprintReads:
    """The two reads, answering documents a test handed it — or refusing the way the layer does."""

    def __init__(self, options: Any = None, states: dict[str, Any] | None = None) -> None:
        self.options = options if options is not None else options_document()
        self.states = states or {}
        self.options_refusal: ReadError | None = None
        self.reads: list[str] = []

    def sprint_options(self) -> dict[str, Any]:
        self.reads.append("sprint_options")
        if self.options_refusal is not None:
            raise self.options_refusal
        return self.options

    def sprint_state(self, ref: str) -> dict[str, Any]:
        self.reads.append(f"sprint_state:{ref}")
        if ref in self.states:
            return self.states[ref]
        raise TaskNotFound(f"the board holds no sprint {ref!r}")


class FakeSprintOps:
    """`sprint_create`, in the order the real one does it, with the same refusals and no board.

    The order is the point and the previous round of this card got it wrong. `SprintOperationLayer`
    **claims the request id, with a digest of the inputs, before the writer judges anything**, so a
    refusal spends the id and a resubmission under it carrying corrected inputs is answered
    `validation: different inputs`. A fake that recorded the request only after validation would be
    green over exactly the dead end the transport now has to avoid, which is what happened. So four
    behaviours are modelled here:

    * the claim comes first, before any judgement of the inputs;
    * a repeat naming other inputs is a `validation` refusal rather than somebody else's sprint;
    * a repeat naming the same inputs answers from the record and creates nothing;
    * `pending_once` is the partial failure: the row is written and the reference is not recorded,
      and the same request id repeated afterwards resumes that staged create rather than opening a
      second sprint.

    `tests/test_web_sprint_transport.py::RealLayerFormTests` stands over the real layer for the
    same properties, so this fake is a convenience and never the only evidence.

    Everything about what a sprint may be stays a judgement of the writer, so the only rule copied
    here is the one criterion 3 is about: an observer that is not a profile of the registry is
    refused, exactly as `check_observer_profile` refuses it below this layer.
    """

    def __init__(self, reads: FakeSprintReads) -> None:
        self.reads = reads
        self.calls: list[dict[str, Any]] = []
        self.by_request: dict[str, tuple[str, str]] = {}
        self.staged: dict[str, str] = {}
        self.created: list[str] = []
        self.pending_once = False
        self.refusal: ReadError | None = None

    def sprint_create(
        self,
        *,
        request_id: str,
        actor: str,
        product: str,
        goal: str,
        issues: list[str] | None = None,
        projects: list[str] | None = None,
        observer: str = "",
        definition_of_done: str = "",
        repositories: list[str] | None = None,
        worker: str | None = None,
        reviewer: str | None = None,
        role: str = "po",
        reference: str = "",
        local_run_exceptions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        call = {
            "request_id": request_id,
            "actor": actor,
            "role": role,
            "product": product,
            "goal": goal,
            "definition_of_done": definition_of_done,
            "issues": list(issues or []),
            "projects": list(projects or []),
            "observer": observer,
            "worker": worker,
            "reviewer": reviewer,
            "local_run_exceptions": local_run_exceptions or [],
        }
        self.calls.append(call)
        fingerprint = json.dumps(call, sort_keys=True)
        # The claim, and it happens before anything below is judged. See the class docstring.
        recorded = self.by_request.get(request_id)
        if recorded is not None:
            if recorded[1] != fingerprint:
                raise ValidationRefused(
                    f"request {request_id!r} already names another {SPRINT_CREATE_OPERATION} "
                    "made with different inputs"
                )
            if recorded[0]:
                return self._document(recorded[0], request_id=request_id, created=False)
        else:
            self.by_request[request_id] = ("", fingerprint)
        if self.refusal is not None:
            raise self.refusal
        known = {head["id"] for head in HEADS if head["observer"]}
        if observer != "none" and observer not in known:
            raise ValidationRefused(f"{observer!r} is not a head profile of this installation's registry")
        for pinned in (worker, reviewer):
            if pinned is not None and pinned not in {head["id"] for head in HEADS}:
                raise ValidationRefused(f"{pinned!r} is not a head profile of this installation")
        made = self.staged.get(request_id)
        if made is None:
            made = f"sprint:{len(self.created) + 1}"
            self.created.append(made)
            self.staged[request_id] = made
            self.reads.states[made] = sprint_document(
                made,
                observer=None if observer == "none" else observer,
                worker=worker,
                reviewer=reviewer,
            )
        if self.pending_once:
            self.pending_once = False
            raise OperationPending(
                f"sprint {made} was created and the request that made it did not finish; repeat the "
                "same request id to pick it up rather than opening a second sprint.",
                data={
                    "reason": PENDING_REASON,
                    "action": {
                        "operation": SPRINT_CREATE_OPERATION,
                        "repeat_request": True,
                        "request_id": request_id,
                        "reference": made,
                    },
                },
            )
        self.by_request[request_id] = (made, fingerprint)
        return self._document(made, request_id=request_id, created=True)

    def _document(self, reference: str, *, request_id: str, created: bool) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "sprint_created",
            "observed_at": "2026-09-06T00:00:00Z",
            "request_id": request_id,
            "created": created,
            "sprint": self.reads.sprint_state(reference),
        }


class RecordingOps:
    """The product-run half, recording what was called on it and nothing else."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str):
        def record(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            self.calls.append(name)
            return {"ok": True, "run": {"run_id": "pr-1"}}

        return record


class SprintTransportFixture(unittest.TestCase):
    """One application over the two fakes, driven the way a browser drives it."""

    def setUp(self) -> None:
        self.sprint_reads = FakeSprintReads()
        self.sprint_ops = FakeSprintOps(self.sprint_reads)
        self.ops = RecordingOps()
        self.app = WebApp(
            RecordingOps(), self.ops, self.sprint_reads, self.sprint_ops, *(RecordingOps() for _ in range(4))
        )

    # -- driving it --------------------------------------------------------------------------

    def get(self, path: str, **kwargs: Any):
        return self.app.handle("GET", path, **kwargs)

    def submit(self, fields: Any, *, headers: Any = None, path: str = "/sprints"):
        """One form submission, encoded exactly as a browser encodes one."""
        pairs = fields.items() if isinstance(fields, dict) else fields
        flat: list[tuple[str, str]] = []
        for name, value in pairs:
            if isinstance(value, (list, tuple)):
                flat += [(name, one) for one in value]
            else:
                flat.append((name, value))
        return self.app.handle("POST", path, body=urlencode(flat).encode("utf-8"), headers=headers)

    def text_of(self, response) -> str:
        return response.body.decode("utf-8")

    def form(self) -> str:
        response = self.get("/sprints/new")
        self.assertEqual(response.status, 200)
        return self.text_of(response)

    def request_id_of(self, markup: str) -> str:
        found = re.search(r'name="request_id" value="([^"]+)"', markup)
        self.assertIsNotNone(found, "the form carries no request id")
        assert found is not None
        return found.group(1)

    def valid(self, **overrides: Any) -> dict[str, Any]:
        fields = {
            "request_id": self.request_id_of(self.form()),
            "product": "ummanu",
            "goal": "Make the web a client of the sprint contract",
            "definition_of_done": "the form and the page exist and are tested",
            "issues": ["issue:web"],
            "projects": ["ummanu"],
            "observer": OBSERVER_PROFILE,
            "worker": "",
            "reviewer": "",
        }
        fields.update(overrides)
        return fields


# -- criterion 1: the routes ----------------------------------------------------------------------


class SprintRouteTests(SprintTransportFixture):
    def test_the_three_routes_are_in_the_table_naming_their_operation(self) -> None:
        table = {(route.method, route.pattern): route.operation for route in ROUTES}
        self.assertEqual(table[("GET", "/sprints/new")], "sprint_reads.sprint_options")
        self.assertEqual(table[("POST", "/sprints")], "sprint_ops.sprint_create")
        self.assertEqual(table[("GET", "/sprints/{ref}")], "sprint_reads.sprint_state")

    def test_the_form_route_is_not_read_as_a_sprint_named_new(self) -> None:
        """A literal route wins over a placeholder, so `/sprints/new` is the form and not a read."""
        self.assertEqual(self.get("/sprints/new").status, 200)
        self.assertEqual(self.sprint_reads.reads, ["sprint_options"])

    def test_an_unrouted_sprint_path_is_404_and_reaches_no_layer(self) -> None:
        for path in ("/sprints/new/again", "/sprints/sprint:1/start", "/api/sprints/sprint:1"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path).status, 404)
        self.assertEqual(self.sprint_reads.reads, [])
        self.assertEqual(self.sprint_ops.calls, [])

    def test_an_unrouted_method_on_a_sprint_path_is_405(self) -> None:
        self.assertEqual(self.app.handle("PUT", "/sprints").status, 405)
        self.assertEqual(self.submit({}, path="/sprints/new").status, 405)
        self.assertEqual(self.sprint_ops.calls, [])

    def test_a_form_field_this_route_does_not_take_is_refused(self) -> None:
        response = self.submit(self.valid(role="steward"))
        self.assertEqual(response.status, 400)
        self.assertIn("role", self.text_of(response))
        self.assertEqual(self.sprint_ops.calls, [])


# -- criterion 2: the form is the installation's own catalogue -------------------------------------


class SprintFormTests(SprintTransportFixture):
    def test_local_run_json_is_visible_passed_and_invalid_json_is_refused_before_create(self) -> None:
        self.assertIn('name="local_run_exceptions"', self.form())
        entries = [{"project": "ummanu", "argv": ["docker", "run", "two words"], "rationale": "owner's probe"}]
        response = self.submit(self.valid(local_run_exceptions=json.dumps(entries)))
        self.assertEqual(response.status, 303)
        self.assertEqual(self.sprint_ops.calls[-1]["local_run_exceptions"], entries)
        before = len(self.sprint_ops.calls)
        for value in ("not JSON", "null", "{}"):
            response = self.submit(self.valid(local_run_exceptions=value))
            self.assertEqual(response.status, 400)
        self.assertEqual(len(self.sprint_ops.calls), before)

    def test_the_form_offers_the_products_issues_projects_and_profiles_the_layer_answered(self) -> None:
        markup = self.form()
        for expected in (
            'value="ummanu"',
            'value="codegen"',
            "issue:web",
            "the web needs a sprint form",
            "issue:codegen",
            'value="orca"',
            OBSERVER_PROFILE,
            "opus",
            "high",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, markup)

    def test_a_different_catalogue_is_a_different_form_and_nothing_is_written_into_the_page(self) -> None:
        """Criterion 2's real claim: the list is the layer's answer, not a list in this product."""
        other = options_document()
        other["products"]["items"] = [{"id": "orca", "label": "Orca", "ref": "product:9", "projects": []}]
        other["issues"]["items"] = [
            {
                "ref": "issue:orca",
                "label": "orca needs a bridge",
                "product": "orca",
                "kind": None,
                "priority": None,
            }
        ]
        other["heads"]["items"] = [
            {
                "id": "hermes-observer",
                "label": "hermes · sonnet · low effort",
                "adapter": "hermes",
                "model": "sonnet",
                "effort": "low",
                "resource": "r",
                "observer": True,
                "observer_reason": None,
                "role_default_for": [],
            }
        ]
        self.sprint_reads.options = other
        markup = self.form()
        self.assertIn("issue:orca", markup)
        self.assertIn("hermes-observer", markup)
        self.assertNotIn("issue:web", markup)
        self.assertNotIn(OBSERVER_PROFILE, markup)

    def test_no_field_asks_a_person_to_type_a_profile_identifier(self) -> None:
        """Criterion 2 and 3: every head is chosen from the registry, never typed from memory."""
        markup = self.form()
        for role in ("observer", "worker", "reviewer"):
            with self.subTest(role=role):
                self.assertIn(f'<select id="{role}" name="{role}">', markup)
                self.assertNotIn(f'<input id="{role}"', markup)

    def test_a_project_an_open_sprint_holds_says_so_and_one_nobody_could_check_says_that(self) -> None:
        markup = self.form()
        self.assertIn("an open sprint already holds it: sprint:7", markup)
        self.assertIn("could not be established", markup)

    def test_an_unreadable_catalogue_section_marks_that_section_and_not_the_others(self) -> None:
        options = options_document()
        options["heads"] = {
            "source": unavailable("the head registry could not be read: no such file"),
            "items": [],
            "observer": {"none": "none", "default": None},
            "role_defaults": {},
            "executor_roles": ["worker", "reviewer"],
        }
        self.sprint_reads.options = options
        markup = self.form()
        self.assertIn("could not find out which head profiles this installation has", markup)
        self.assertIn("no such file", markup)
        self.assertIn("issue:web", markup)

    def test_a_catalogue_that_cannot_be_read_at_all_is_the_layers_status_and_not_an_empty_form(self) -> None:
        self.sprint_reads.options_refusal = TaskNotFound("this instance config does not validate")
        response = self.get("/sprints/new")
        self.assertEqual(response.status, 404)
        self.assertIn("this instance config does not validate", self.text_of(response))


# -- criterion 3: the observer ---------------------------------------------------------------------


class ObserverFieldTests(SprintTransportFixture):
    def test_each_observer_choice_shows_its_name_model_and_effort(self) -> None:
        markup = self.form()
        observer = markup.split('<select id="observer"')[1].split("</select>")[0]
        self.assertIn("claude · opus · high effort", observer)
        self.assertIn("model opus", observer)
        self.assertIn("effort high", observer)
        self.assertIn(OBSERVER_PROFILE, observer)

    def test_only_the_profiles_the_layer_calls_eligible_are_offered_as_observers(self) -> None:
        observer = self.form().split('<select id="observer"')[1].split("</select>")[0]
        self.assertIn(OBSERVER_PROFILE, observer)
        self.assertNotIn(WORKER_PROFILE, observer)

    def test_the_no_observer_answer_is_not_offered_on_this_route(self) -> None:
        """A sprint with no observer starts nothing, so the button that says "start" cannot offer it.

        It stays a legal answer to `ummanu sprint create` and to the rows that already carry it;
        what is narrowed here is the browser client, not the sprint contract.
        """
        observer = self.form().split('<select id="observer"')[1].split("</select>")[0]
        self.assertNotIn('value="none"', observer)
        self.assertNotIn("runs without one", observer)

    def test_a_crafted_no_observer_submission_is_refused_before_the_layer(self) -> None:
        response = self.submit(self.valid(observer="none"))
        self.assertEqual(response.status, 400)
        self.assertIn("names the head that will run it", self.text_of(response))
        self.assertEqual(self.sprint_ops.calls, [])
        # And the id is untouched, because nothing was claimed: this form is submittable as it is.
        self.assertIn("observer", self.text_of(response))

    def test_an_observer_nobody_chose_is_refused_by_name_and_opens_nothing(self) -> None:
        response = self.submit(self.valid(observer=""))
        self.assertEqual(response.status, 400)
        self.assertIn("observer", self.text_of(response))
        self.assertIn("choose the observer head", self.text_of(response))
        self.assertEqual(self.sprint_ops.calls, [])

    def test_a_profile_the_registry_does_not_hold_is_refused_by_the_server(self) -> None:
        """Criterion 3: refused, and never quietly replaced with a profile nobody chose."""
        response = self.submit(self.valid(observer="a-profile-that-was-deleted"))
        self.assertEqual(response.status, 400)
        markup = self.text_of(response)
        self.assertIn("a-profile-that-was-deleted", markup)
        self.assertIn("is not a head profile", markup)
        self.assertEqual(self.sprint_ops.created, [])
        # And what reached the layer was the profile that was submitted, not another one.
        self.assertEqual(self.sprint_ops.calls[-1]["observer"], "a-profile-that-was-deleted")
        # It also comes back on the form, marked: a page that dropped it would be showing a
        # submission nobody made.
        self.assertIn('value="a-profile-that-was-deleted" selected', markup)


# -- criterion 4: the two optional pins -------------------------------------------------------------


class ExecutorPinTests(SprintTransportFixture):
    def test_both_roles_default_to_the_observer_choosing(self) -> None:
        markup = self.form()
        for role in ("worker", "reviewer"):
            with self.subTest(role=role):
                select = markup.split(f'<select id="{role}"')[1].split("</select>")[0]
                self.assertIn('<option value="" selected>the observer chooses</option>', select)

    def test_the_observer_chooses_reaches_the_layer_as_nothing_said(self) -> None:
        self.assertEqual(self.submit(self.valid()).status, 303)
        call = self.sprint_ops.calls[-1]
        self.assertIsNone(call["worker"])
        self.assertIsNone(call["reviewer"])

    def test_a_chosen_profile_reaches_the_layer_as_that_profile(self) -> None:
        self.assertEqual(
            self.submit(self.valid(worker=WORKER_PROFILE, reviewer=REVIEWER_PROFILE)).status, 303
        )
        call = self.sprint_ops.calls[-1]
        self.assertEqual(call["worker"], WORKER_PROFILE)
        self.assertEqual(call["reviewer"], REVIEWER_PROFILE)

    def test_the_two_roles_are_independent(self) -> None:
        self.assertEqual(self.submit(self.valid(worker=WORKER_PROFILE)).status, 303)
        call = self.sprint_ops.calls[-1]
        self.assertEqual(call["worker"], WORKER_PROFILE)
        self.assertIsNone(call["reviewer"])

    def test_an_empty_string_never_travels_down_as_a_profile_name(self) -> None:
        """The one spelling the layer refuses rather than folds into absence. Criterion 4."""
        self.submit(self.valid())
        for call in self.sprint_ops.calls:
            self.assertNotEqual(call["worker"], "")
            self.assertNotEqual(call["reviewer"], "")


# -- criterion 5: what is missing, and what was typed ------------------------------------------------


class FormRefusalTests(SprintTransportFixture):
    def test_every_required_field_is_refused_by_name_and_by_what_is_wrong(self) -> None:
        for field, expected in (
            ("goal", "say what this sprint is for"),
            ("definition_of_done", "say what would make this sprint done"),
            ("observer", "choose the observer head"),
            ("issues", "choose at least one open issue"),
            ("projects", "choose at least one registered project"),
            ("product", "choose the product"),
        ):
            with self.subTest(field=field):
                response = self.submit(self.valid(**{field: [] if field in ("issues", "projects") else ""}))
                self.assertEqual(response.status, 400)
                markup = self.text_of(response)
                self.assertIn(field.replace("_", " "), markup)
                self.assertIn(expected, markup)
        self.assertEqual(self.sprint_ops.calls, [])

    def test_nothing_a_person_typed_is_lost_when_the_form_comes_back(self) -> None:
        fields = self.valid(
            goal="A goal nobody should have to type twice",
            definition_of_done="A definition of done that is equally hard to retype",
            observer="",
            issues=["issue:web", "issue:codegen"],
            projects=["ummanu", "orca"],
            worker=WORKER_PROFILE,
        )
        markup = self.text_of(self.submit(fields))
        self.assertIn("A goal nobody should have to type twice", markup)
        self.assertIn("A definition of done that is equally hard to retype", markup)
        self.assertIn('value="ummanu" selected', markup)
        for checked in (
            'name="issues" value="issue:web" checked',
            'name="issues" value="issue:codegen" checked',
            'name="projects" value="ummanu" checked',
            'name="projects" value="orca" checked',
        ):
            with self.subTest(checked=checked):
                self.assertIn(checked, markup)
        self.assertIn(f'value="{WORKER_PROFILE}" selected', markup)
        # And it comes back under the same request id, so fixing a field is still one sprint.
        self.assertEqual(self.request_id_of(markup), fields["request_id"])

    def test_a_refusal_the_layer_made_is_shown_on_the_form_with_the_values_still_in_it(self) -> None:
        """The refusal and every value come back; the request id deliberately does not.

        This assertion is the reverse of the one this file carried in the previous round, and the
        reversal is the fix. The layer claims the request id before the writer judges the inputs,
        so an id that has been refused is spent: a form handing it back would let the owner correct
        a field and be told the correction is a different request, with no sprint and no way on.
        Nothing durable was created, so a corrected submission really is a new request.
        """
        self.sprint_ops.refusal = OwnerConflict("an open sprint already reserves ummanu")
        fields = self.valid(goal="A goal that survives a conflict")
        response = self.submit(fields)
        self.assertEqual(response.status, 409)
        markup = self.text_of(response)
        self.assertIn("an open sprint already reserves ummanu", markup)
        self.assertIn("A goal that survives a conflict", markup)
        self.assertNotEqual(self.request_id_of(markup), fields["request_id"])
        self.assertIn("new request id", markup)

    def test_the_corrected_form_a_refusal_returns_can_actually_be_submitted(self) -> None:
        """The dead end this round exists to remove, end to end over the transport."""
        first = self.submit(self.valid(observer="a-profile-that-was-deleted"))
        self.assertEqual(first.status, 400)
        offered = self.text_of(first)
        # What comes back is submittable: the values, a fresh id, and the choice that is gone still
        # visible rather than silently swapped for one nobody picked.
        self.assertIn("a-profile-that-was-deleted", offered)
        self.assertIn("no longer offers this choice", offered)
        corrected = self.valid(request_id=self.request_id_of(offered), observer=OBSERVER_PROFILE)
        second = self.submit(corrected)
        self.assertEqual(second.status, 303)
        self.assertEqual(self.sprint_ops.created, ["sprint:1"])

    def test_reusing_the_burnt_request_id_is_what_the_form_no_longer_asks_for(self) -> None:
        """Why the id is reissued: the layer refuses the corrected inputs under the old one."""
        fields = self.valid(observer="a-profile-that-was-deleted")
        self.assertEqual(self.submit(fields).status, 400)
        again = self.submit(self.valid(request_id=fields["request_id"], observer=OBSERVER_PROFILE))
        self.assertEqual(again.status, 400)
        self.assertIn("different inputs", self.text_of(again))
        self.assertEqual(self.sprint_ops.created, [])

    def test_a_refusal_shown_over_an_unreadable_catalogue_is_still_the_refusal(self) -> None:
        self.sprint_ops.refusal = OwnerConflict("an open sprint already reserves ummanu")
        fields = self.valid()
        self.sprint_reads.options_refusal = TaskNotFound("the board could not be read")
        response = self.submit(fields)
        self.assertEqual(response.status, 409)
        markup = self.text_of(response)
        self.assertIn("an open sprint already reserves ummanu", markup)
        self.assertIn("the board could not be read", markup)


# -- criteria 6: the sprint page ---------------------------------------------------------------------


class SprintPageTests(SprintTransportFixture):
    def page(self, document: dict[str, Any], reference: str = "sprint:1") -> str:
        self.sprint_reads.states[reference] = document
        response = self.get(f"/sprints/{reference}")
        self.assertEqual(response.status, 200)
        return self.text_of(response)

    def test_the_page_shows_what_the_sprint_was_opened_with(self) -> None:
        markup = self.page(
            sprint_document(
                worker=WORKER_PROFILE,
                current_task="ummanu-1570",
                resume={"selected_step": "the transport", "next_safe_step": "write the tests"},
            )
        )
        for expected in (
            "Make the web a client of the sprint contract",
            "the form and the page exist and are tested",
            "ummanu",
            "issue:web",
            OBSERVER_PROFILE,
            f"pinned to {WORKER_PROFILE}",
            "the observer chooses",
            "ummanu-1570",
            "the transport",
            "write the tests",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, markup)

    def test_the_observers_call_is_drawn_once_and_the_resume_tab_keeps_only_the_rest(self) -> None:
        entry = {
            "selected_step": "Keep the card in progress",
            "selected_why": "the gate is red on a fixture defect",
            "rejected_alternatives": "do not reslice",
            "next_safe_step": "let the worker fix it",
            "dod_state": "no item is closed",
            "recorded_at": "2026-09-06T00:00:00Z",
        }
        markup = self.page(
            sprint_document(
                resume=entry,
                work={"decision": {"entry": entry, "freshness": {"value": {"fresh": True}}}},
            )
        )
        for said in (
            "Keep the card in progress",
            "the gate is red on a fixture defect",
            "let the worker fix it",
        ):
            with self.subTest(said=said):
                self.assertEqual(markup.count(said), 1, "the call panel draws it; the resume tab does not")
        self.assertIn("no item is closed", markup)
        self.assertIn("why · rejected alternatives", markup)

    def test_the_definition_of_done_is_rendered_from_its_markdown_in_a_tab(self) -> None:
        document = sprint_document()
        document["sprint"]["value"]["definition_of_done"] = "## Done\n\n- **one** item\n- two"
        markup = self.page(document)
        self.assertIn('<label for="tab-sprint-sprint:1-1">Definition of done</label>', markup)
        self.assertIn("<strong>one</strong>", markup)
        self.assertNotIn("<pre>## Done", markup)

    def test_the_declared_observer_is_named_once_when_the_dispatcher_holds_it(self) -> None:
        record = {"head": OBSERVER_PROFILE, "heartbeat_state": "pid"}
        markup = self.page(sprint_document(launch="running", record=record))
        main = markup[markup.index("<main>") : markup.index("</main>")]
        self.assertEqual(main.count(f'<span class="ref">{OBSERVER_PROFILE}</span>'), 1)
        self.assertNotIn("not the declared head", main)
        other = self.page(sprint_document(launch="running", record={**record, "head": "claude-opus"}))
        self.assertIn("not the declared head", other)

    def test_the_observer_head_the_dispatcher_holds_links_to_its_head_view(self) -> None:
        record = {"head": OBSERVER_PROFILE, "run_id": "c" * 32}
        markup = self.page(sprint_document(launch="running", record=record))
        self.assertIn(f'href="/sprints/sprint%3A1/heads/{"c" * 32}"', markup)

    def test_the_page_says_where_the_current_card_stands_and_since_when(self) -> None:
        """The duration in the text, the exact ISO moment as the title -- `_reset`'s convention."""
        markup = self.page(
            sprint_document(
                current_task="ummanu-1570",
                work={
                    "current_task": {
                        "source": available(),
                        "ref": "ummanu-1570",
                        "live": True,
                        "reason": "the observer has it as the current card",
                    },
                    "current_card_state": {
                        "source": available(),
                        "card": "ummanu-1570",
                        "state": "validate",
                        "since": "2026-09-05T21:30:00Z",
                        "age_seconds": 9000.0,
                        "transition": "recorded",
                        "reason": "ummanu-1570 stands in validate",
                    },
                },
            )
        )
        self.assertIn('title="2026-09-05T21:30:00Z"', markup)
        self.assertIn("2h in this state", markup)
        self.assertIn(">validate<", markup)

    def test_the_page_never_shows_an_age_for_a_card_no_transition_dates(self) -> None:
        """Criteria 5 and 6 on the page: words where a duration would be, and never a zero."""
        for transition, reason, said in (
            ("absent", "the committed audit records no state transition", "no transition recorded"),
            ("not_applicable", "sprint:1 is closed: it is the card it ended on", "the card it ended on"),
            ("unknown", "the committed audit journal could not be read", "could not be read"),
        ):
            with self.subTest(transition=transition):
                markup = self.page(
                    sprint_document(
                        current_task="ummanu-1570",
                        work={
                            "current_task": {
                                "source": available(),
                                "ref": "ummanu-1570",
                                "live": transition != "not_applicable",
                                "reason": reason,
                            },
                            "current_card_state": {
                                "source": available(),
                                "card": "ummanu-1570",
                                "state": None,
                                "since": None,
                                "age_seconds": None,
                                "transition": transition,
                                "reason": reason,
                            },
                        },
                    )
                )
                self.assertIn(said, markup)
                self.assertIn("ummanu-1570", markup)
                self.assertNotIn("in this state", markup)

    def test_the_three_launch_states_are_told_apart_in_words(self) -> None:
        """Criterion 6: the words differ, so the page reads the same without colour."""
        saved = self.page(sprint_document(launch="not_started"))
        running = self.page(
            sprint_document(
                launch="running",
                reason=f"an observer head is up on {OBSERVER_PROFILE}",
                record={"head": OBSERVER_PROFILE, "alive": True, "heartbeat_state": "fresh"},
            )
        )
        blind = self.page(
            sprint_document(
                launch="unavailable",
                launch_source=unavailable("the dispatcher production state could not be read"),
                reason="the dispatcher production state could not be read",
            )
        )
        self.assertIn("saved — no observer is up for it yet", saved)
        self.assertIn("running — an observer head is up", running)
        self.assertIn("not established — this could not be read at all", blind)
        for one, others in ((saved, (running, blind)), (running, (saved, blind)), (blind, (saved, running))):
            said = one.split('class="launch')[1].split("</span>")[0]
            for other in others:
                self.assertNotIn(said, other)

    def test_a_sprint_that_declared_no_observer_does_not_read_as_one_waiting_for_one(self) -> None:
        markup = self.page(
            sprint_document(
                observer=None,
                launch="not_declared",
                reason="this sprint declares no observer, so the tick raises none for it",
            )
        )
        self.assertIn("no observer — this sprint declared none", markup)
        self.assertNotIn("saved — no observer is up for it yet", markup)

    def test_a_stopped_observer_is_neither_running_nor_never_started(self) -> None:
        markup = self.page(
            sprint_document(
                launch="stopped",
                reason="the dispatcher holds an observer record whose head is not alive",
                record={"head": OBSERVER_PROFILE, "alive": False, "heartbeat_state": "stale"},
            )
        )
        self.assertIn("stopped — an observer was raised for it and is not alive", markup)
        self.assertIn("stale", markup)

    def test_a_sprint_the_board_does_not_hold_is_a_404_page(self) -> None:
        response = self.get("/sprints/sprint:404")
        self.assertEqual(response.status, 404)
        self.assertIn("text/html", response.content_type)

    def test_the_page_escapes_what_the_layer_gave_it(self) -> None:
        document = sprint_document()
        document["sprint"]["value"]["goal"] = "<script>alert(1)</script>"
        markup = self.page(document)
        self.assertNotIn("<script>alert(1)</script>", markup)
        self.assertIn("&lt;script&gt;", markup)


# -- criterion 7: one submission is one sprint --------------------------------------------------------


class IdempotentSubmissionTests(SprintTransportFixture):
    def test_a_valid_submission_opens_one_sprint_and_lands_on_its_page(self) -> None:
        response = self.submit(self.valid())
        self.assertEqual(response.status, 303)
        self.assertEqual(response.headers["Location"], "/sprints/sprint%3A1")
        self.assertEqual(self.sprint_ops.created, ["sprint:1"])
        page = self.get("/sprints/sprint:1")
        self.assertEqual(page.status, 200)
        self.assertIn("Make the web a client of the sprint contract", self.text_of(page))

    def test_the_same_submission_twice_is_one_sprint(self) -> None:
        """A double click, a retry and a reconnected client are all this: the same form, again."""
        fields = self.valid()
        first = self.submit(fields)
        second = self.submit(fields)
        self.assertEqual(first.status, 303)
        self.assertEqual(second.status, 303)
        self.assertEqual(first.headers["Location"], second.headers["Location"])
        # Both submissions reached the layer -- the transport swallows neither -- and the layer
        # answered the second from the request it already owns.
        self.assertEqual(len(self.sprint_ops.calls), 2)
        self.assertEqual(self.sprint_ops.created, ["sprint:1"])

    def test_the_request_id_is_the_forms_and_is_not_minted_per_post(self) -> None:
        markup = self.form()
        submitted = self.request_id_of(markup)
        self.submit(self.valid(request_id=submitted))
        self.submit(self.valid(request_id=submitted))
        self.assertEqual({call["request_id"] for call in self.sprint_ops.calls}, {submitted})
        self.assertEqual(self.sprint_ops.created, ["sprint:1"])

    def test_two_forms_opened_separately_are_two_intentions(self) -> None:
        self.assertNotEqual(self.request_id_of(self.form()), self.request_id_of(self.form()))

    def test_a_submission_with_no_request_id_is_refused_rather_than_given_a_new_one(self) -> None:
        response = self.submit(self.valid(request_id=""))
        self.assertEqual(response.status, 400)
        self.assertIn("request id", self.text_of(response))
        self.assertEqual(self.sprint_ops.calls, [])

    def test_after_a_partial_failure_the_same_submission_reaches_the_sprint_that_exists(self) -> None:
        """Criterion 7's other half: the repair is a repeat, and the page says so in those words."""
        self.sprint_ops.pending_once = True
        fields = self.valid()
        refused = self.submit(fields)
        self.assertEqual(refused.status, 503)
        markup = self.text_of(refused)
        self.assertIn("this sprint exists and the request that opened it did not finish", markup)
        self.assertIn("Submitting this form again is safe", markup)
        self.assertIn("sprint:1", markup)
        self.assertEqual(self.request_id_of(markup), fields["request_id"])

        again = self.submit(fields)
        self.assertEqual(again.status, 303)
        self.assertEqual(again.headers["Location"], "/sprints/sprint%3A1")
        self.assertEqual(self.sprint_ops.created, ["sprint:1"])


# -- criterion 8: a mutation comes from this service's own pages ---------------------------------------


class CrossOriginTests(SprintTransportFixture):
    HOST = "ummanu.example"

    def mutations(self) -> list[tuple[str, bytes, str]]:
        """Every mutating route, with a body it would otherwise be answered on."""
        return [
            (
                "/api/runs/start",
                json.dumps({"ref": "ummanu-1", "request_id": "r", "profile": WORKER_PROFILE}).encode(
                    "utf-8"
                ),
                "run_start",
            ),
            (
                "/api/runs/review",
                json.dumps({"request_id": "r", "profile": REVIEWER_PROFILE, "worker_run_id": "pr-1"}).encode(
                    "utf-8"
                ),
                "run_review",
            ),
            ("/sprints", urlencode(list(self.valid().items()), doseq=True).encode("utf-8"), ""),
        ]

    def post(self, path: str, body: bytes, headers: dict[str, str]):
        return self.app.handle("POST", path, body=body, headers=headers)

    def test_a_browser_posting_from_another_origin_is_refused_on_every_mutating_route(self) -> None:
        for path, body, _operation in self.mutations():
            with self.subTest(path=path):
                response = self.post(
                    path,
                    body,
                    {"Origin": "https://attacker.example", "Host": self.HOST},
                )
                self.assertEqual(response.status, 403)
                self.assertIn("this service did not serve", self.text_of(response))
        self.assertEqual(self.ops.calls, [])
        self.assertEqual(self.sprint_ops.calls, [])
        self.assertEqual(self.sprint_ops.created, [])

    def test_a_null_origin_is_refused_too(self) -> None:
        for path, body, _operation in self.mutations():
            with self.subTest(path=path):
                response = self.post(path, body, {"Origin": "null", "Host": self.HOST})
                self.assertEqual(response.status, 403)
        self.assertEqual(self.sprint_ops.calls, [])

    def test_the_services_own_pages_still_post(self) -> None:
        for path, body, _operation in self.mutations():
            with self.subTest(path=path):
                response = self.post(path, body, {"Origin": f"https://{self.HOST}", "Host": self.HOST})
                self.assertNotEqual(response.status, 403)
        self.assertEqual(self.sprint_ops.created, ["sprint:1"])

    def test_the_scheme_is_not_compared_because_the_front_terminates_tls(self) -> None:
        """A real request through the published front arrives as https origin over plain http."""
        self.assertIsNone(
            cross_origin_reason({"Origin": "https://ummanu.example", "Host": "ummanu.example"})
        )
        self.assertIsNone(cross_origin_reason({"Origin": "http://127.0.0.1:8787", "Host": "127.0.0.1:8787"}))

    def test_a_port_that_differs_is_another_origin(self) -> None:
        self.assertIsNotNone(
            cross_origin_reason({"Origin": "http://127.0.0.1:9999", "Host": "127.0.0.1:8787"})
        )

    def test_a_client_that_sends_no_origin_is_not_a_browser_and_keeps_working(self) -> None:
        """`ummanu web-run`, curl and the OPERATIONS.md diagnostics send none. Criterion 8."""
        for path, body, _operation in self.mutations():
            with self.subTest(path=path):
                response = self.post(path, body, {"Host": self.HOST})
                self.assertNotEqual(response.status, 403)
        self.assertEqual(self.sprint_ops.created, ["sprint:1"])
        self.assertIsNone(cross_origin_reason(None))
        self.assertIsNone(cross_origin_reason({}))

    def test_the_check_sees_the_headers_a_real_request_arrives_with(self) -> None:
        """One real socket, because the header path between `http.server` and the app is the point.

        Everything else here calls `WebApp.handle` directly, which is exactly where a check that is
        never handed the headers would still look correct. So this one goes through the server.
        """
        server = build_server(self.app, host="127.0.0.1", port=0)
        self.addCleanup(server.server_close)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        host, port = server.server_address[0], server.server_address[1]
        connection = HTTPConnection(host, port, timeout=10)
        self.addCleanup(connection.close)
        body = urlencode(list(self.valid().items()), doseq=True)
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        connection.request(
            "POST", "/sprints", body=body, headers={**headers, "Origin": "https://attacker.example"}
        )
        refused = connection.getresponse()
        refused.read()
        self.assertEqual(refused.status, 403)
        self.assertEqual(self.sprint_ops.created, [])

        connection.request(
            "POST", "/sprints", body=body, headers={**headers, "Origin": f"http://{host}:{port}"}
        )
        opened = connection.getresponse()
        opened.read()
        self.assertEqual(opened.status, 303)
        self.assertEqual(opened.getheader("Location"), "/sprints/sprint%3A1")
        self.assertEqual(self.sprint_ops.created, ["sprint:1"])

    def test_a_read_is_not_asked_about_its_origin(self) -> None:
        """The check is on mutations. A GET from anywhere is still only a read of this service."""
        response = self.app.handle(
            "GET", "/sprints/new", headers={"Origin": "https://attacker.example", "Host": self.HOST}
        )
        self.assertEqual(response.status, 200)


# -- the same user path, over the real layer ------------------------------------------------------


class RealLayerFormTests(SprintProtocolFixture):
    """The form and the create over the real `SprintOperationLayer`, its real request store and the
    repository's board fixture — no fake of the layer anywhere.

    This class exists because the previous round's fake differed from the layer in the one place it
    was written for: the real operation claims the request id *before* the writer judges the inputs,
    the fake recorded it after, and a dead end on the user path was therefore green. What is pinned
    here is the behaviour of the two together, so a transport that stopped matching the layer's
    order fails whatever a fake believes.
    """

    def setUp(self) -> None:
        super().setUp()
        self.app = WebApp(
            RecordingOps(), RecordingOps(), self.reads(), self.ops(), *(RecordingOps() for _ in range(4))
        )

    # -- driving it --------------------------------------------------------------------------

    def form(self) -> str:
        response = self.app.handle("GET", "/sprints/new")
        self.assertEqual(response.status, 200)
        return response.body.decode("utf-8")

    def request_id_of(self, markup: str) -> str:
        found = re.search(r'name="request_id" value="([^"]+)"', markup)
        assert found is not None, "the form carries no request id"
        return found.group(1)

    def fields(self, **overrides: Any) -> list[tuple[str, str]]:
        values: dict[str, Any] = {
            "request_id": self.request_id_of(self.form()),
            "product": "ummanu",
            "goal": "Open a sprint from the browser",
            "definition_of_done": "the form works end to end",
            "issues": ["issue:open"],
            "projects": ["ummanu"],
            "observer": PROTOCOL_OBSERVER,
            "worker": "",
            "reviewer": "",
        }
        values.update(overrides)
        flat: list[tuple[str, str]] = []
        for name, value in values.items():
            if isinstance(value, (list, tuple)):
                flat += [(name, one) for one in value]
            else:
                flat.append((name, value))
        return flat

    def submit(self, fields: list[tuple[str, str]]):
        return self.app.handle("POST", "/sprints", body=urlencode(fields).encode("utf-8"))

    def replace(self, fields: list[tuple[str, str]], name: str, value: str) -> list[tuple[str, str]]:
        return [(key, value if key == name else held) for key, held in fields]

    # -- the catalogue this form is served on --------------------------------------------------

    def test_the_form_is_this_installations_own_catalogue(self) -> None:
        markup = self.form()
        self.assertIn("issue:open", markup)
        self.assertIn(PROTOCOL_OBSERVER, markup)
        self.assertIn('value="ummanu"', markup)
        # The one answer that is not a profile is not offered on this route.
        observer = markup.split('<select id="observer"')[1].split("</select>")[0]
        self.assertNotIn('value="none"', observer)

    def test_a_valid_submission_opens_one_sprint_on_the_board(self) -> None:
        response = self.submit(self.fields())
        self.assertEqual(response.status, 303)
        rows = self.sprint_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(response.headers["Location"], f"/sprints/{quote(str(rows[0]['reference']))}")

    # -- the defect this round repairs ----------------------------------------------------------

    def test_a_refused_profile_leaves_a_form_that_can_actually_be_submitted(self) -> None:
        """The reviewer's scenario, on the real layer, from the refusal to the sprint.

        `SprintOperationLayer.sprint_create` claims the id and the digest of the inputs before
        `SprintWriter.create` refuses the unknown profile, so the id is spent while no sprint
        exists. The form that comes back therefore carries a new one — and the profile that is gone
        stays visible on it, because a page that swapped it for a current profile would be showing
        a submission nobody made.
        """
        first = self.fields(observer="retired-observer")
        refused = self.submit(first)
        self.assertEqual(refused.status, 400)
        markup = refused.body.decode("utf-8")
        self.assertIn("retired-observer", markup)
        self.assertIn("no longer offers this choice", markup)
        self.assertIn("Open a sprint from the browser", markup)
        self.assertEqual(self.sprint_rows(), [])

        reissued = self.request_id_of(markup)
        self.assertNotEqual(reissued, dict(first)["request_id"])
        corrected = self.replace(self.replace(first, "observer", PROTOCOL_OBSERVER), "request_id", reissued)
        opened = self.submit(corrected)
        self.assertEqual(opened.status, 303)
        self.assertEqual(len(self.sprint_rows()), 1)

    def test_the_burnt_request_id_is_exactly_what_the_layer_refuses(self) -> None:
        """Why the id is reissued rather than kept: the real store says so."""
        first = self.fields(observer="retired-observer")
        self.assertEqual(self.submit(first).status, 400)
        again = self.submit(self.replace(first, "observer", PROTOCOL_OBSERVER))
        self.assertEqual(again.status, 400)
        self.assertIn("different inputs", again.body.decode("utf-8"))
        self.assertEqual(self.sprint_rows(), [])

    def test_a_partial_failure_keeps_the_id_and_the_repeat_reaches_the_same_sprint(self) -> None:
        """The opposite case, and it must stay opposite: a sprint exists, so the id is not touched.

        The failure is injected where the real layer has one — the reference is written under the
        request id after the writer returns — so what is exercised is the layer's own pending path
        and not a fake of it.
        """
        fields = self.fields()
        with mock.patch.object(
            SprintRequestStore, "record_reference", side_effect=RunStoreError("the disk went away")
        ):
            pending = self.submit(fields)
        self.assertEqual(pending.status, 503)
        markup = pending.body.decode("utf-8")
        self.assertIn("this sprint exists and the request that opened it did not finish", markup)
        self.assertIn("Submitting this form again is safe", markup)
        self.assertEqual(self.request_id_of(markup), dict(fields)["request_id"])
        self.assertEqual(len(self.sprint_rows()), 1)

        repeated = self.submit(fields)
        self.assertEqual(repeated.status, 303)
        self.assertEqual(len(self.sprint_rows()), 1, "the repeat resumed the sprint that exists")

    def test_a_crafted_no_observer_submission_never_reaches_the_board(self) -> None:
        response = self.submit(self.fields(observer="none"))
        self.assertEqual(response.status, 400)
        self.assertIn("names the head that will run it", response.body.decode("utf-8"))
        self.assertEqual(self.sprint_rows(), [])

    def test_a_sprint_that_already_runs_without_an_observer_still_reads_on_its_page(self) -> None:
        """Narrowing the browser client narrows nothing about the sprints that exist."""
        created = self.ops().sprint_create(
            request_id="cli-1",
            actor="operator",
            product="ummanu",
            goal="A sprint the CLI opened without an observer",
            definition_of_done="it exists",
            issues=["issue:open"],
            projects=["ummanu"],
            observer="none",
        )
        reference = str(created["sprint"]["ref"])
        page = self.app.handle("GET", f"/sprints/{reference}")
        self.assertEqual(page.status, 200)
        markup = page.body.decode("utf-8")
        self.assertIn("no observer — this sprint declared none", markup)
        self.assertIn("A sprint the CLI opened without an observer", markup)


if __name__ == "__main__":
    unittest.main()
