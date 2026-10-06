"""The routes, and what each of them calls. One layer operation per route, and no route without one.

Stateless transport over :mod:`ummanu.webproto`: no session, cache or state between requests (watch
cursors and form request ids live in the client). The only decision made here is who may make a
mutation (:func:`cross_origin_reason`, plus the PO token under /po), once on the POST path. `ROUTES`
is the whole surface: no route runs a command, script or path, and there is no catch-all (unrouted
path 404, unrouted method 405). See docs/PROTOCOLS.md, "Serving the pipeline locally".
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field, replace
from http.cookies import CookieError, SimpleCookie
from typing import Any
from urllib.parse import parse_qs, quote, unquote

from ummanu.web import pages
from ummanu.web.doctor import DoctorLayer
from ummanu.web.statuses import status_for
from ummanu.webproto.errors import OperationPending, ReadError, RuntimeUnavailable, ValidationRefused
from ummanu.webproto.journal import DEFAULT_LIMIT, MAX_LIMIT
from ummanu.webproto.owner_events import OwnerEventLayer
from ummanu.webproto.po_auth import COOKIE_NAME as PO_COOKIE_NAME
from ummanu.webproto.po_auth import COOKIE_PATH as PO_COOKIE_PATH
from ummanu.webproto.reads import TASK_SNAPSHOT_EVENTS
from ummanu.webproto.sprint_reads import NONE_SPELLING

#: The largest request body read; every accepted body is a handful of short fields.
MAX_BODY_BYTES = 64 * 1024

JSON_TYPE = "application/json; charset=utf-8"
HTML_TYPE = "text/html; charset=utf-8"
FORM_TYPE = "application/x-www-form-urlencoded"

#: Route body encodings: a JSON object (programs) or a submitted form (browsers; the sprint form's
#: request id lives in the markup, so a double click or retry is one sprint).
JSON_BODY = "json"
FORM_BODY = "form"


@dataclass(frozen=True, slots=True)
class Response:
    """What a handler produced, before any HTTP machinery has touched it."""

    status: int
    body: bytes
    content_type: str
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Route:
    """One reachable thing, and the operation behind it."""

    method: str
    pattern: str
    handler: str
    #: The layer call this route is a transport for, named so the table reads as the contract it is.
    operation: str
    #: JSON object or HTML form; both decode to the same shape under the same closed field list.
    body: str = JSON_BODY
    #: Page routes render a refusal as a page with the same status.
    page: bool = False

    @property
    def segments(self) -> tuple[str, ...]:
        return tuple(part for part in self.pattern.split("/") if part)


#: The whole externally reachable surface of this service.
ROUTES: tuple[Route, ...] = (
    Route("GET", "/", "dashboard", "reads.system_snapshot", page=True),
    Route("GET", "/tasks/{ref}", "task_page", "reads.task_snapshot", page=True),
    # One of the card's local-pty heads, read-only; an unrecorded run id is 404.
    Route("GET", "/tasks/{ref}/heads/{run_id}", "head_page", "reads.head_view", page=True),
    Route("GET", "/sprints", "sprints_page", "sprint_reads.sprint_list", page=True),
    Route("GET", "/projects", "projects_page", "reads.system_snapshot", page=True),
    Route("GET", "/projects/{project}", "project_page", "reads.system_snapshot", page=True),
    Route("GET", "/sprints/new", "sprint_form", "sprint_reads.sprint_options", page=True),
    Route("POST", "/sprints", "sprint_create", "sprint_ops.sprint_create", body=FORM_BODY, page=True),
    Route("GET", "/sprints/{ref}", "sprint_page", "sprint_reads.sprint_state", page=True),
    Route("GET", "/api/system", "system", "reads.system_snapshot"),
    Route("GET", "/api/tasks/{ref}", "task", "reads.task_snapshot"),
    Route("GET", "/api/tasks/{ref}/events", "events", "reads.task_events"),
    Route("GET", "/api/tasks/{ref}/runs", "task_runs", "ops.run_list"),
    Route("GET", "/api/tasks/{ref}/heads/{run_id}", "head", "reads.head_view"),
    Route("GET", "/api/runs/{run_id}", "run", "ops.run_state"),
    Route("POST", "/api/runs/start", "start", "ops.run_start"),
    Route("POST", "/api/runs/review", "review", "ops.run_review"),
    # The operator's routes: each is one existing layer operation.
    Route("GET", "/history", "commands_page", "command_reads.command_history", page=True),
    # The page behind the bottom bar's lamp: recorded problems by code, and which make it red.
    Route("GET", "/doctor", "doctor_page", "doctor.doctor_snapshot", page=True),
    Route("GET", "/api/pause", "pause", "pause_reads.pause_state"),
    Route("GET", "/api/pause/scope", "pause_scope", "pause_reads.pause_scope"),
    Route("POST", "/api/pause/drain", "pause_drain", "pause_ops.pause_drain"),
    Route("POST", "/api/pause/resume", "pause_resume", "pause_ops.pause_resume"),
    Route("GET", "/api/sprints", "sprints", "sprint_reads.sprint_list"),
    Route("POST", "/api/sprints/{ref}/comment", "sprint_comment", "sprint_ops.sprint_comment"),
    Route("POST", "/api/sprints/{ref}/close", "sprint_close", "sprint_ops.sprint_close"),
    Route("GET", "/api/history", "commands", "command_reads.command_history"),
    Route("GET", "/api/history/{request_id}", "command_request", "command_reads.command_request"),
    Route("POST", "/api/tasks/{ref}/comment", "task_comment", "card_ops.task_comment"),
    Route("POST", "/api/tasks/{ref}/move", "task_move", "card_ops.task_move"),
    # Spends one Codex rate-limit reset credit; idempotent on `request_id` (also the provider's redeem id).
    Route("POST", "/api/providers/codex/reset-limit", "codex_reset_limit", "provider_ops.codex_reset_limit"),
    # The PO head: every /po route requires the PO token (:func:`requires_po_token`) except login.
    Route("POST", "/po/login", "po_login", "po_auth.po_login", body=FORM_BODY, page=True),
    Route("GET", "/po", "po_page", "po.po_overview", page=True),
    Route("POST", "/po/sessions", "po_create", "po.po_create_session", body=FORM_BODY, page=True),
    Route("GET", "/po/sessions/{session}", "po_session_page", "po.po_session", page=True),
    Route("POST", "/po/sessions/{session}/messages", "po_send", "po.po_send", body=FORM_BODY, page=True),
    Route("POST", "/po/sessions/{session}/stop", "po_stop", "po.po_stop", body=FORM_BODY, page=True),
    Route("POST", "/po/sessions/{session}/close", "po_close", "po.po_close", body=FORM_BODY, page=True),
    Route("POST", "/po/sessions/{session}/title", "po_rename", "po.po_rename", body=FORM_BODY, page=True),
    Route("GET", "/po/api/sessions/{session}", "po_session_json", "po.po_session"),
    # The owner's bell: list, mark one read, mark all read (notices only); board `owner_events` only.
    Route("GET", "/owner-events", "owner_events_page", "owner_events.owner_event_list", page=True),
    Route(
        "POST", "/owner-events/read-all", "owner_events_read_all", "owner_events.mark_all_read",
        body=FORM_BODY, page=True,
    ),
    Route(
        "POST", "/owner-events/{event_id}/read", "owner_event_read", "owner_events.mark_read",
        body=FORM_BODY, page=True,
    ),
)

#: The prefix the PO token guards, and the cookie's `Path`: one value, so no route under it is outside.
PO_PREFIX = PO_COOKIE_PATH
#: The only /po routes answered without a valid cookie.
PO_OPEN_ROUTES = frozenset({("POST", "/po/login")})
PO_LOGIN_FIELDS = frozenset({"token"})
PO_CREATE_FIELDS = frozenset({"request_id", "cli", "model", "effort"})
PO_SEND_FIELDS = frozenset({"request_id", "text"})
PO_STOP_FIELDS = frozenset({"seq"})
PO_CLOSE_FIELDS: frozenset[str] = frozenset()
PO_RENAME_FIELDS = frozenset({"title"})
#: Form fields of every /po POST, by handler. A set with `request_id` marks a route whose operation
#: takes the id into `PoStore`'s request transaction (held by `tests.test_web_po_transport`).
PO_FORM_FIELDS = {
    "po_login": PO_LOGIN_FIELDS,
    "po_create": PO_CREATE_FIELDS,
    "po_send": PO_SEND_FIELDS,
    "po_stop": PO_STOP_FIELDS,
    "po_close": PO_CLOSE_FIELDS,
    "po_rename": PO_RENAME_FIELDS,
}
#: How long a browser keeps the PO cookie. Replacing the token file ends it sooner.
PO_COOKIE_MAX_AGE = 30 * 24 * 3600
PO_TOKEN_REQUIRED = "the PO head is behind its own token; enter it to continue"
PO_TOKEN_WRONG = "that is not this installation's PO token"
PO_NOT_SERVED = "this web process was built without the PO layers, so /po is not served"


def requires_po_token(route: Route) -> bool:
    """Whether `route` is answered only with a valid PO cookie: everything under /po but the login."""
    under = route.pattern == PO_PREFIX or route.pattern.startswith(PO_PREFIX + "/")
    return under and (route.method, route.pattern) not in PO_OPEN_ROUTES


#: The fields each POST accepts, and the only ones; unknown fields are refused, never ignored.
START_FIELDS = frozenset({"ref", "request_id", "profile", "instruction"})
SPRINT_COMMENT_FIELDS = frozenset({"request_id", "body"})
SPRINT_CLOSE_FIELDS = frozenset({"request_id", "reason", "closeout", "decisions"})
PAUSE_DRAIN_FIELDS = frozenset({"reason"})
PAUSE_RESUME_FIELDS: frozenset[str] = frozenset()
TASK_COMMENT_FIELDS = frozenset({"request_id", "body"})
TASK_MOVE_FIELDS = frozenset({"request_id", "target", "reason", "sprint_override", "sprint_override_reason"})
CODEX_RESET_FIELDS = frozenset({"request_id"})
PROVIDER_OPS_NOT_BUILT = "this web process was built without the provider operation layer"
#: Owner event forms carry only the return view: `view=all`, else the unread default.
OWNER_EVENT_FIELDS = frozenset({"view"})
OWNER_EVENTS_NOT_BUILT = "this web process was built without the owner events layer"

#: How many commands the dashboard's feed and the commands page show per read.
FEED_LIMIT = 25
#: Card-page event read size: enough for a full transition timeline (the API default is a short tail).
TASK_PAGE_EVENTS = 200
REVIEW_FIELDS = frozenset({"ref", "request_id", "profile", "worker_run_id"})
SPRINT_FIELDS = frozenset(
    {
        "request_id",
        "product",
        "goal",
        "definition_of_done",
        "issues",
        "projects",
        "observer",
        "worker",
        "reviewer",
        "local_run_exceptions",
    }
)

#: Role and actor for writes from here. The web has no identity of its own (the front checks the
#: owner's password), so it acts as `po` and names itself `web` in the audit.
SPRINT_ROLE = "po"
SPRINT_ACTOR = "web"

#: The executor selects' "observer chooses" option; :func:`_pin` turns it into `None` (unpinned).
EXECUTOR_UNPINNED = ""


class WebApp:
    """The routing half, built over the read, operation and sprint layers.

    Layers are injected (so tests drive every route against fakes). Required layers are positional,
    so a missing one fails at construction; the keyword-only ones are optional features.
    """

    def __init__(
        self,
        reads: Any,
        ops: Any,
        sprint_reads: Any,
        sprint_ops: Any,
        pause_reads: Any,
        pause_ops: Any,
        command_reads: Any,
        card_ops: Any,
        provider_usage: Any | None = None,
        doctor: Any | None = None,
        *,
        po_auth: Any | None = None,
        po: Any | None = None,
        owner_events: Any | None = None,
        provider_ops: Any | None = None,
    ) -> None:
        self.reads = reads
        self.ops = ops
        self.sprint_reads = sprint_reads
        self.sprint_ops = sprint_ops
        self.pause_reads = pause_reads
        self.pause_ops = pause_ops
        self.command_reads = command_reads
        self.card_ops = card_ops
        self.provider_usage = provider_usage
        #: Cached recorded health behind the doctor lamp and page. Optional: without it every page
        #: is served and the lamp reads unknown (red).
        self.doctor = doctor
        #: PO token check and sessions. Optional: without them /po is not served.
        self.po_auth = po_auth
        self.po = po
        #: The owner's bell. Optional: without it no bell is drawn and its routes answer the reason.
        self.owner_events = owner_events
        #: The Codex reset. Optional: without it the reset route answers 503 and spends nothing.
        self.provider_ops = provider_ops

    # -- the entry point -------------------------------------------------------------------

    def handle(
        self,
        method: str,
        path: str,
        *,
        query: str = "",
        body: bytes = b"",
        headers: Any = None,
    ) -> Response:
        """One request, answered. The only place a protocol code becomes a status.

        The cross-origin check runs here, once for every POST, before any handler. Per-request
        context (set for this call only, never shared between requests): the bottom bar's limits
        source, the doctor lamp source and the bell source as callables (so JSON routes pay no
        read), one pinned health and owner-events reading, and whether this is a POST (a page
        answering a submission must not auto-reload, which would re-send it).
        """
        with (
            pages.limits_source(self._limits_section),
            pages.doctor_source(self._doctor_section),
            pages.bell_source(self._bell_section if self.owner_events is not None else None),
            pages.from_post(method == "POST"),
            self._one_health_reading(),
            self.owner_events.one_reading() if isinstance(self.owner_events, OwnerEventLayer) else nullcontext(),
        ):
            return self._handle(method, path, query=query, body=body, headers=headers)

    def _handle(
        self,
        method: str,
        path: str,
        *,
        query: str = "",
        body: bytes = b"",
        headers: Any = None,
    ) -> Response:
        route, params = self.match(method, path)
        if route is None:
            return self._refuse(
                method,
                path,
                status=params["status"],
                code=params["code"],
                message=params["message"],
            )
        if route.method == "POST":
            reason = cross_origin_reason(headers)
            if reason is not None:
                return self._deny(route, status=403, code="cross_origin", message=reason)
        # The PO token, checked by path here so any future /po route is guarded too.
        if route.pattern == PO_PREFIX or route.pattern.startswith(PO_PREFIX + "/"):
            refusal = self._po_gate(route, headers)
            if refusal is not None:
                return refusal
        handler: Callable[..., Response] = getattr(self, f"_{route.handler}")
        try:
            payload = _payload(route, body)
            response = handler(params, _query(query), payload)
        except ReadError as exc:
            return self._error(route, exc)
        return _secure_cookie(response, headers)

    def match(self, method: str, path: str) -> tuple[Route | None, dict[str, Any]]:
        """The route for this request, or why there is none: 404 for a path, 405 for a method.

        A literal route wins over a placeholder route (`/api/runs/start` is not a run named
        "start"), independent of table order.
        """
        wanted = [unquote(part) for part in path.split("/") if part]
        candidates = [
            (route, params)
            for route, params in ((route, _bind(route.segments, wanted)) for route in ROUTES)
            if params is not None
        ]
        literal = [pair for pair in candidates if not pair[1]]
        for route, params in literal or candidates:
            if route.method == method.upper():
                return route, params
        if candidates:
            allowed = ", ".join(sorted({route.method for route, _ in (literal or candidates)}))
            return None, {
                "status": 405,
                "code": "method_not_allowed",
                "message": f"{method} is not one of the methods this route answers ({allowed})",
            }
        return None, {
            "status": 404,
            "code": "not_found",
            "message": "this service answers only the named routes it publishes; there is no route here",
        }

    # -- read routes -----------------------------------------------------------------------

    def _system(self, _params, _query, _body) -> Response:
        return _json(200, self.reads.system_snapshot())

    def _task(self, params, query, _body) -> Response:
        return _json(200, self.reads.task_snapshot(params["ref"], events=_events_count(query)))

    def _events(self, params, query, _body) -> Response:
        return _json(
            200,
            self.reads.task_events(params["ref"], _one(query, "cursor"), limit=_limit(query)),
        )

    def _head(self, params, _query, _body) -> Response:
        return _json(200, self.reads.head_view(params["ref"], params["run_id"]))

    def _task_runs(self, params, _query, _body) -> Response:
        return _json(200, self.ops.run_list(params["ref"]))

    def _run(self, params, _query, _body) -> Response:
        return _json(200, self.ops.run_state(params["run_id"]))

    # -- run routes ------------------------------------------------------------------------

    def _start(self, _params, _query, body) -> Response:
        _fields(body, START_FIELDS, "start")
        return _json(
            200,
            self.ops.run_start(
                _required(body, "ref"),
                request_id=_required(body, "request_id"),
                profile=_required(body, "profile"),
                instruction=_text(body.get("instruction")),
            ),
        )

    def _review(self, _params, _query, body) -> Response:
        _fields(body, REVIEW_FIELDS, "review")
        return _json(
            200,
            self.ops.run_review(
                request_id=_required(body, "request_id"),
                profile=_required(body, "profile"),
                ref=_text(body.get("ref")),
                worker_run_id=_text(body.get("worker_run_id")),
            ),
        )

    # -- pause routes ----------------------------------------------------------------------

    def _pause(self, _params, _query, _body) -> Response:
        return _json(200, self.pause_reads.pause_state())

    def _pause_scope(self, _params, _query, _body) -> Response:
        return _json(200, self.pause_reads.pause_scope())

    def _pause_drain(self, _params, _query, body) -> Response:
        _fields(body, PAUSE_DRAIN_FIELDS, "pause drain")
        return _json(200, self.pause_ops.pause_drain(actor=SPRINT_ACTOR, reason=_required(body, "reason")))

    def _pause_resume(self, _params, _query, body) -> Response:
        _fields(body, PAUSE_RESUME_FIELDS, "pause resume")
        return _json(200, self.pause_ops.pause_resume(actor=SPRINT_ACTOR))

    # -- sprint operation routes -----------------------------------------------------------

    def _sprints(self, _params, query, _body) -> Response:
        statuses = [value for value in query.get("status") or [] if value]
        return _json(200, self.sprint_reads.sprint_list(statuses=statuses or None))

    def _sprint_comment(self, params, _query, body) -> Response:
        _fields(body, SPRINT_COMMENT_FIELDS, "sprint comment")
        return _json(
            200,
            self.sprint_ops.sprint_comment(
                request_id=_required(body, "request_id"),
                actor=SPRINT_ACTOR,
                role=SPRINT_ROLE,
                reference=params["ref"],
                body=_required(body, "body"),
            ),
        )

    def _sprint_close(self, params, _query, body) -> Response:
        _fields(body, SPRINT_CLOSE_FIELDS, "sprint close")
        return _json(
            200,
            self.sprint_ops.sprint_close(
                request_id=_required(body, "request_id"),
                actor=SPRINT_ACTOR,
                role=SPRINT_ROLE,
                reference=params["ref"],
                reason=_required(body, "reason"),
                closeout=_required(body, "closeout"),
                decisions=_decisions(body.get("decisions")),
            ),
        )

    # -- command routes --------------------------------------------------------------------

    def _commands(self, _params, query, _body) -> Response:
        return _json(200, self.command_reads.command_history(_one(query, "cursor"), limit=_limit(query)))

    def _command_request(self, params, _query, _body) -> Response:
        return _json(200, self.command_reads.command_request(params["request_id"]))

    # -- card operation routes -------------------------------------------------------------

    def _task_comment(self, params, _query, body) -> Response:
        _fields(body, TASK_COMMENT_FIELDS, "card comment")
        return _json(
            200,
            self.card_ops.task_comment(
                request_id=_required(body, "request_id"),
                actor=SPRINT_ACTOR,
                role=SPRINT_ROLE,
                reference=params["ref"],
                body=_required(body, "body"),
            ),
        )

    def _task_move(self, params, _query, body) -> Response:
        _fields(body, TASK_MOVE_FIELDS, "card move")
        return _json(
            200,
            self.card_ops.task_move(
                request_id=_required(body, "request_id"),
                actor=SPRINT_ACTOR,
                role=SPRINT_ROLE,
                reference=params["ref"],
                target=_required(body, "target"),
                reason=_required(body, "reason"),
                sprint_override=_flag(body.get("sprint_override")),
                sprint_override_reason=_text(body.get("sprint_override_reason")),
            ),
        )

    # -- provider operation routes ---------------------------------------------------------

    def _codex_reset_limit(self, _params, _query, body) -> Response:
        _fields(body, CODEX_RESET_FIELDS, "Codex reset")
        if self.provider_ops is None:
            raise RuntimeUnavailable(PROVIDER_OPS_NOT_BUILT)
        return _json(
            200,
            self.provider_ops.codex_reset_limit(request_id=_required(body, "request_id"), actor=SPRINT_ACTOR),
        )

    # -- pages -----------------------------------------------------------------------------

    def _dashboard(self, _params, _query, _body) -> Response:
        """The operator's one screen, assembled from four reads that fail apart.

        Only the snapshot's refusal fails the page; pause, open sprints and the command feed each
        show their own reason instead.
        """
        snapshot = self.reads.system_snapshot()
        pause = self._or_reason(self.pause_reads.pause_state)
        sprints = self._or_reason(lambda: self.sprint_reads.sprint_list(statuses=["open"]))
        limits = self._limits_section()
        # A count only, so no token; an unavailable PO store just hides it.
        po = self._or_reason(self.po.po_running_count) if self.po is not None else None
        return _html(200, pages.dashboard(snapshot, pause=pause, sprints=sprints, limits=limits, po=po))

    def _task_page(self, params, query, _body) -> Response:
        ref = params["ref"]
        snapshot = self.reads.task_snapshot(
            ref, events=_int(query, "events", TASK_PAGE_EVENTS, ceiling=MAX_LIMIT)
        )
        return _html(
            200,
            pages.task(snapshot, runs=self._runs_or_reason(ref), sessions=self._session_titles(snapshot)),
        )

    def _session_titles(self, snapshot: dict[str, Any]) -> dict[str, Any] | None:
        """Titles of the PO sessions a card page links to, or the reason they are unavailable.

        None when no session is named or there is no PO layer (the page shows short ids).
        """
        named = pages.po_sessions_named(snapshot)
        if not named or self.po is None:
            return None
        return self._or_reason(lambda: self.po.po_session_titles(named))

    def _head_page(self, params, _query, _body) -> Response:
        return _html(200, pages.head_view(self.reads.head_view(params["ref"], params["run_id"])))

    def _sprints_page(self, _params, query, _body) -> Response:
        view = "archive" if _one(query, "view") == "archive" else "active"
        statuses = ["open"] if view == "active" else ["closed", "stopped"]
        return _html(
            200,
            pages.sprints_page(
                self.sprint_reads.sprint_list(statuses=statuses),
                view=view,
                search=_one(query, "q") or "",
                project=_one(query, "project") or "",
            ),
        )

    def _projects_page(self, _params, _query, _body) -> Response:
        return _html(200, pages.projects_page(self.reads.system_snapshot()))

    def _project_page(self, params, _query, _body) -> Response:
        snapshot = self.reads.system_snapshot()
        projects = (snapshot.get("projects") or {}).get("items") or []
        if not any(
            str(item.get("id") or "") == params["project"] for item in projects if isinstance(item, dict)
        ):
            return _html(
                404, pages.error(404, "project_not_found", f"project {params['project']} is not registered")
            )
        sprints = self._or_reason(lambda: self.sprint_reads.sprint_list(statuses=None))
        markup = pages.project_page(snapshot, project_id=params["project"], sprints=sprints)
        return _html(200, markup)

    def _doctor_page(self, _params, _query, _body) -> Response:
        return _html(200, pages.doctor(self._doctor_section()))

    def _commands_page(self, _params, query, _body) -> Response:
        return _html(
            200,
            pages.commands(self.command_reads.command_history(_one(query, "cursor"), limit=_limit(query))),
        )

    def _owner_events_page(self, _params, query, _body) -> Response:
        # Unread is the default; only `?all=1` widens it, any other value falls back.
        unread_only = _one(query, "all") not in {"1", "true", "yes", "on"}
        document = self._owner_event_layer().owner_event_list(unread_only=unread_only)
        marked = _one(query, "marked")
        if marked is not None and marked.isdecimal():
            document["read_feedback"] = _owner_read_feedback(int(marked), document)
        return _html(200, pages.owner_events(document))

    def _owner_event_read(self, params, _query, body) -> Response:
        _fields(body, OWNER_EVENT_FIELDS, "owner event read")
        self._owner_event_layer().mark_read(params["event_id"])
        return _redirect(_owner_events_back(body), what="the event is read")

    def _owner_events_read_all(self, _params, _query, body) -> Response:
        _fields(body, OWNER_EVENT_FIELDS, "owner events read")
        result = self._owner_event_layer().mark_all_read()
        marked = result["marked"]
        back = _owner_events_back(body)
        back += ("&" if "?" in back else "?") + f"marked={marked}"
        return _redirect(back, what=_owner_read_feedback(marked, result))

    def _owner_event_layer(self) -> Any:
        if self.owner_events is None:
            raise RuntimeUnavailable(OWNER_EVENTS_NOT_BUILT)
        return self.owner_events

    def _bell_section(self) -> dict[str, Any]:
        """The header bell's count, read from the board for this render; a refusal is its reason."""
        try:
            return self._owner_event_layer().unread_count()
        except ReadError as exc:
            return {"state": "unavailable", "reason": exc.message, "count": 0}

    def _limits_section(self) -> dict[str, Any] | None:
        """The provider limits for a page, or `None` when this process was built without them.

        The cached layer's call, behind both the dashboard panel and every page's bottom bar.
        """
        if self.provider_usage is None:
            return None
        return self._or_reason(self.provider_usage.usage_snapshot)

    def _one_health_reading(self) -> AbstractContextManager[None]:
        """One health reading for this whole request: the dashboard's panel and the lamp alike.

        Pins the doctor cache for the request so both lookups agree across a cache expiry. Only a
        real `DoctorLayer` is pinned.
        """
        if isinstance(self.doctor, DoctorLayer):
            return self.doctor.one_reading()
        return nullcontext()

    def _doctor_section(self) -> dict[str, Any] | None:
        """The recorded health for a page, or `None` when this process was built without it.

        The cached layer's call, behind both the lamp and the doctor page.
        """
        if self.doctor is None:
            return None
        return self._or_reason(self.doctor.doctor_snapshot)

    def _or_reason(self, read: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """A section's document, or the reason it has none, for a page made of several reads."""
        try:
            return {"available": True, "reason": None, "document": read()}
        except ReadError as exc:
            return {"available": False, "reason": exc.message, "document": None}

    def _runs_or_reason(self, ref: str) -> dict[str, Any]:
        """The card's product runs for the page, or the reason there are none to show.

        A refused run listing does not take the card page down.
        """
        try:
            return {"available": True, "reason": None, "items": self.ops.run_list(ref)["items"]}
        except ReadError as exc:
            return {"available": False, "reason": exc.message, "items": []}

    # -- sprint routes ---------------------------------------------------------------------

    def _sprint_form(self, _params, _query, _body) -> Response:
        """The empty form, on this installation's own catalogue.

        The request id is minted here, once per rendered form: a reload is a new intention (new id),
        a resubmission of the open form is the same one (same id).
        """
        return _html(
            200,
            pages.sprint_form(
                self.sprint_reads.sprint_options(),
                submitted=_blank_submission(_request_id()),
                errors={},
            ),
        )

    def _sprint_create(self, _params, _query, body) -> Response:
        """One submission: refuse what is incomplete, hand the rest down, and go to the sprint.

        Missing form fields are refused here per field; admissibility (profiles, issues, projects,
        reservations) is the writer's judgement, shown beside the typed values. Success is a 303 to
        the sprint. `sprint_create` claims the id with an input digest before judging, so a refusal
        other than `OperationPending` re-renders with a fresh id (the old one is spent and nothing was
        created); `OperationPending` keeps the id and values, because only that id reaches the sprint.
        """
        _fields(body, SPRINT_FIELDS, "sprint create")
        submitted = _submission(body)
        errors = _incomplete(submitted)
        try:
            local_run_exceptions = json.loads(submitted["local_run_exceptions"] or "[]")
            if not isinstance(local_run_exceptions, list):
                raise ValueError("local_run_exceptions must be a list")  # noqa: TRY004 - uniform JSON value validation
        except ValueError:
            errors["local_run_exceptions"] = "Enter a JSON list of project, argv and rationale entries."
        if errors:
            # Nothing reached the layer, so this id was never claimed.
            return self._form_again(submitted, errors=errors, status=400)
        try:
            created = self.sprint_ops.sprint_create(
                request_id=submitted["request_id"],
                actor=SPRINT_ACTOR,
                role=SPRINT_ROLE,
                product=submitted["product"],
                goal=submitted["goal"],
                definition_of_done=submitted["definition_of_done"],
                issues=list(submitted["issues"]),
                projects=list(submitted["projects"]),
                observer=submitted["observer"],
                worker=_pin(submitted["worker"]),
                reviewer=_pin(submitted["reviewer"]),
                local_run_exceptions=local_run_exceptions,
            )
        except OperationPending as exc:
            return self._form_again(submitted, errors={}, status=status_for(exc.code), refusal=exc)
        except ReadError as exc:
            return self._form_again(
                submitted, errors={}, status=status_for(exc.code), refusal=exc, fresh=True
            )
        reference = str((created.get("sprint") or {}).get("ref") or "")
        return _redirect(f"/sprints/{quote(reference)}")

    def _sprint_page(self, params, _query, _body) -> Response:
        return _html(200, pages.sprint(self.sprint_reads.sprint_state(params["ref"])))

    def _form_again(
        self,
        submitted: dict[str, Any],
        *,
        errors: dict[str, str],
        status: int,
        refusal: ReadError | None = None,
        fresh: bool = False,
    ) -> Response:
        """The form the person just submitted, with what was refused and everything they typed.

        `fresh` mints a new request id (only when the layer refused without creating a sprint); every
        other value comes back untouched, including a choice the catalogue no longer offers. An
        unreadable catalogue is shown as its own section rather than replacing the refusal.
        """
        shown = dict(submitted)
        if fresh:
            shown["request_id"] = _request_id()
        try:
            options, catalogue = self.sprint_reads.sprint_options(), None
        except ReadError as exc:
            options, catalogue = None, exc.message
        return _html(
            status,
            pages.sprint_form(
                options,
                submitted=shown,
                errors=errors,
                refusal=None if refusal is None else refusal.to_json(),
                catalogue=catalogue,
                reissued=fresh,
            ),
        )

    # -- the PO head -----------------------------------------------------------------------

    def _po_gate(self, route: Route, headers: Any) -> Response | None:
        """Why this /po request is refused before its handler, or `None` when it may go on.

        Only the token file is read; without a valid cookie nothing reaches the PO service or board.
        """
        if self.po_auth is None or self.po is None:
            return self._deny(route, status=503, code="po_unavailable", message=PO_NOT_SERVED)
        if (route.method, route.pattern) in PO_OPEN_ROUTES:
            return None
        try:
            admitted = bool(self.po_auth.po_admits(_presented_cookie(headers)).get("admitted"))
        except ReadError as exc:
            return self._error(route, exc)
        if admitted:
            return None
        if route.page:
            return _html(401, pages.po_login(PO_TOKEN_REQUIRED))
        return _json(401, {"error": {"code": "po_token_required", "message": PO_TOKEN_REQUIRED}})

    def _po_login(self, _params, _query, body) -> Response:
        _fields(body, PO_LOGIN_FIELDS, "PO login")
        answer = self.po_auth.po_login(_first(body, "token"))
        if not answer.get("admitted") or not answer.get("cookie"):
            return _html(401, pages.po_login(PO_TOKEN_WRONG))
        cookie = (
            f"{PO_COOKIE_NAME}={answer['cookie']}; Path={PO_COOKIE_PATH}; Max-Age={PO_COOKIE_MAX_AGE}; "
            "HttpOnly; SameSite=Strict"
        )
        return Response(
            303,
            pages.redirect("/po", what="the PO head is open").encode("utf-8"),
            HTML_TYPE,
            {"Location": "/po", "Set-Cookie": cookie},
        )

    def _po_page(self, _params, query, _body) -> Response:
        closed = _first(query, "closed") == "1"
        return _html(200, pages.po_page(self.po.po_overview(closed=closed), request_id=_po_request_id()))

    def _po_create(self, _params, _query, body) -> Response:
        _fields(body, PO_CREATE_FIELDS, "PO session create")
        cli, model = _first(body, "cli"), _first(body, "model")
        # No effort, or `default`, is refused by the PO layer and re-renders the form with the reason.
        effort = _first(body, "effort")
        try:
            created = self.po.po_create_session(
                request_id=_first(body, "request_id"), cli=cli, model=model, effort=effort
            )
        except Exception as caught:  # noqa: BLE001 - any refusal re-renders the form, its id kept unless definite
            exc = _as_refusal(caught)
            return _html(
                status_for(exc.code),
                pages.po_page(
                    self.po.po_overview(),
                    request_id=_first(body, "request_id") if _keeps_request_id(exc) else _po_request_id(),
                    refusal=exc.to_json(),
                    submitted={"cli": cli, "model": model, "effort": effort},
                ),
            )
        return _redirect(f"/po/sessions/{quote(str(created['session_id']))}", what="the session is open")

    def _po_session_page(self, params, _query, _body) -> Response:
        document = self._po_session_document(params["session"])
        return _html(200, pages.po_session(document, request_id=_po_request_id()))

    def _po_session_json(self, params, query, _body) -> Response:
        # The page's poller watches turns only, so `cards=0` skips the board listing.
        return _json(200, self._po_session_document(params["session"], cards=_one(query, "cards") != "0"))

    def _po_session_document(self, session_id: str, *, cards: bool = True) -> dict[str, Any]:
        """The session from the PO layer, plus the cards it delegated from one board listing.

        A refused board marks only `delegated`. Without `cards`, `delegated` is null and the board is
        not read.
        """
        document = self.po.po_session(session_id)
        if not cards:
            return {**document, "delegated": None}
        try:
            delegated = self.reads.po_delegated(session_id)
        except ReadError as exc:
            delegated = {
                "kind": "po_delegated",
                "session": session_id,
                "source": {"state": "unavailable", "reason": exc.message},
                "items": None,
            }
        return {**document, "delegated": delegated}

    def _po_send(self, params, _query, body) -> Response:
        """One message into the PO service's queue. A refusal renders the session again with the text kept.

        A message for a running turn is queued, not refused. The re-rendered form keeps its request id
        (a resend replays) unless the refusal wrote nothing (`_keeps_request_id`).
        """
        _fields(body, PO_SEND_FIELDS, "PO message")
        session_id = params["session"]
        request_id, text = _first(body, "request_id"), _first(body, "text")
        try:
            self.po.po_send(request_id=request_id, session_id=session_id, text=text)
        except Exception as caught:  # noqa: BLE001 - any refusal re-renders the form, its id kept unless definite
            exc = _as_refusal(caught)
            if exc.code == "not_found":
                raise exc from None
            return _html(
                status_for(exc.code),
                pages.po_session(
                    self._po_session_document(session_id),
                    request_id=request_id if _keeps_request_id(exc) else _po_request_id(),
                    draft=text,
                    refusal=exc.to_json(),
                ),
            )
        return _redirect(f"/po/sessions/{quote(session_id)}", what="the message is sent")

    def _po_stop(self, params, _query, body) -> Response:
        _fields(body, PO_STOP_FIELDS, "PO stop")
        raw = _first(body, "seq")
        if not raw.isdigit():
            raise ValidationRefused("seq names the running turn to stop, as a whole number")
        self.po.po_stop(session_id=params["session"], seq=int(raw))
        return _redirect(f"/po/sessions/{quote(params['session'])}", what="the turn is stopped")

    def _po_close(self, params, _query, body) -> Response:
        """Close as the owner; closed already is the same answer. A running turn renders the session refused."""
        _fields(body, PO_CLOSE_FIELDS, "PO close")
        session_id = params["session"]
        try:
            self.po.po_close(session_id=session_id)
        except ReadError as exc:
            if exc.code == "not_found":
                raise
            return _html(
                status_for(exc.code),
                pages.po_session(
                    self._po_session_document(session_id),
                    request_id=_po_request_id(),
                    refusal=exc.to_json(),
                    refused="close",
                ),
            )
        return _redirect("/po", what="the session is closed")

    def _po_rename(self, params, _query, body) -> Response:
        """Set the title, open or closed; an empty one clears it. A refusal renders the session with the text kept."""
        _fields(body, PO_RENAME_FIELDS, "PO title")
        session_id, title = params["session"], _first(body, "title")
        try:
            self.po.po_rename(session_id=session_id, title=title)
        except ReadError as exc:
            if exc.code == "not_found":
                raise
            return _html(
                status_for(exc.code),
                pages.po_session(
                    self._po_session_document(session_id),
                    request_id=_po_request_id(),
                    refusal=exc.to_json(),
                    refused="title",
                    title_draft=title,
                ),
            )
        return _redirect(f"/po/sessions/{quote(session_id)}", what="the title is saved")

    # -- failures --------------------------------------------------------------------------

    def _error(self, route: Route, exc: ReadError) -> Response:
        status = status_for(exc.code)
        if route.page:
            return _html(status, pages.error(status, exc.code, exc.message))
        return _json(status, {"error": exc.to_json()})

    def _deny(self, route: Route, *, status: int, code: str, message: str) -> Response:
        """A refusal this transport made itself, in the shape the route answers in."""
        if route.page:
            return _html(status, pages.error(status, code, message))
        return _json(status, {"error": {"code": code, "message": message}})

    def _refuse(self, method: str, path: str, *, status: int, code: str, message: str) -> Response:
        if status == 404 and method.upper() == "GET" and not path.startswith("/api/"):
            return _html(status, pages.error(status, code, message))
        return _json(status, {"error": {"code": code, "message": message}})


# -- request parsing ----------------------------------------------------------------------------


def _bind(pattern: tuple[str, ...], wanted: list[str]) -> dict[str, Any] | None:
    if len(pattern) != len(wanted):
        return None
    params: dict[str, Any] = {}
    for expected, given in zip(pattern, wanted, strict=True):
        if expected.startswith("{") and expected.endswith("}"):
            params[expected[1:-1]] = given
        elif expected != given:
            return None
    return params


def _query(raw: str) -> dict[str, list[str]]:
    return parse_qs(raw or "", keep_blank_values=True)


def _one(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name) or []
    return values[-1] if values and values[-1] != "" else None


def _int(query: dict[str, list[str]], name: str, default: int, *, ceiling: int) -> int:
    raw = _one(query, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValidationRefused(f"{name} must be a whole number, not {raw!r}") from None
    if value < 1 or value > ceiling:
        raise ValidationRefused(f"{name} must be between 1 and {ceiling}")
    return value


def _limit(query: dict[str, list[str]]) -> int:
    return _int(query, "limit", DEFAULT_LIMIT, ceiling=MAX_LIMIT)


def _events_count(query: dict[str, list[str]]) -> int:
    return _int(query, "events", TASK_SNAPSHOT_EVENTS, ceiling=MAX_LIMIT)


def _payload(route: Route, raw: bytes) -> dict[str, Any]:
    """The body of this request, decoded the way this route says its clients send one."""
    if route.method != "POST":
        return {}
    return _form(raw) if route.body == FORM_BODY else _body(raw)


def _form(raw: bytes) -> dict[str, Any]:
    """A submitted HTML form, as the fields it carries.

    Repeated fields are lists. Blank values are kept: an empty executor select means "observer
    chooses", which must differ from an absent field.
    """
    if len(raw) > MAX_BODY_BYTES:
        raise ValidationRefused(f"this request body is larger than the {MAX_BODY_BYTES} bytes accepted here")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationRefused(f"this form is not UTF-8 text: {exc}") from None
    return {name: values for name, values in parse_qs(text, keep_blank_values=True).items()}


def _body(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_BODY_BYTES:
        raise ValidationRefused(f"this request body is larger than the {MAX_BODY_BYTES} bytes accepted here")
    if not raw.strip():
        raise ValidationRefused("this route takes a JSON object body")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValidationRefused(f"this request body is not JSON: {exc}") from None
    if not isinstance(payload, dict):
        raise ValidationRefused("this route takes a JSON object body")
    return payload


def _fields(body: dict[str, Any], allowed: frozenset[str], operation: str) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise ValidationRefused(
            f"the {operation} operation takes only {', '.join(sorted(allowed))}; "
            f"it was given {', '.join(unknown)}"
        )


def _required(body: dict[str, Any], name: str) -> str:
    value = _text(body.get(name))
    if not value:
        raise ValidationRefused(f"{name} is required")
    return value


def _text(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValidationRefused("every field of these requests is a string")
    return value


def _flag(value: Any) -> bool:
    """A JSON boolean, or the strings a form sends for one. Anything else is refused by name."""
    if value is None or value is False or value == "":
        return False
    if value is True:
        return True
    if isinstance(value, str) and value.lower() in {"true", "on", "yes", "1"}:
        return True
    if isinstance(value, str) and value.lower() in {"false", "off", "no", "0"}:
        return False
    raise ValidationRefused("sprint_override is a boolean")


def _decisions(value: Any) -> Any:
    """The close decisions as sent: absent, the CLI's decisions file as text, or its parsed object.

    Not parsed here; the layer uses the CLI's parser, so the web refuses exactly what it refuses.
    """
    if value is None or value == "" or value == {}:
        return None
    if not isinstance(value, str | dict):
        raise ValidationRefused("decisions is the decisions document as text, or as an object")
    return value


# -- the sprint submission ----------------------------------------------------------------------


def _submission(form: dict[str, Any]) -> dict[str, Any]:
    """One form as the sprint create's own vocabulary, and as what to put back in the boxes.

    The single shape both the handler and the page use, so a refused submission re-renders from
    exactly what was sent.
    """
    return {
        "request_id": _first(form, "request_id"),
        "product": _first(form, "product"),
        "goal": _first(form, "goal"),
        "definition_of_done": _first(form, "definition_of_done"),
        "issues": _all(form, "issues"),
        "projects": _all(form, "projects"),
        "observer": _first(form, "observer"),
        "worker": _first(form, "worker"),
        "reviewer": _first(form, "reviewer"),
        "local_run_exceptions": _first(form, "local_run_exceptions"),
    }


def _blank_submission(request_id: str) -> dict[str, Any]:
    submission = _submission({})
    submission["request_id"] = request_id
    return submission


def _first(form: dict[str, Any], name: str) -> str:
    values = _all(form, name)
    return values[-1] if values else ""


def _all(form: dict[str, Any], name: str) -> list[str]:
    raw = form.get(name)
    values = raw if isinstance(raw, list) else [] if raw is None else [raw]
    for value in values:
        if not isinstance(value, str):
            raise ValidationRefused("every field of a submitted form is text")
    return [value.strip() for value in values if str(value).strip()]


#: Fields the form itself requires, with their refusal text. Admissibility of filled values is the
#: writer's, never decided here.
_REQUIRED: tuple[tuple[str, str], ...] = (
    (
        "request_id",
        "this submission carries no request id, so it cannot be repeated safely; open the form again",
    ),
    ("product", "choose the product this sprint serves"),
    ("goal", "say what this sprint is for; a sprint with no goal cannot be reviewed against one"),
    ("definition_of_done", "say what would make this sprint done"),
    ("issues", "choose at least one open issue of that product for this sprint to serve"),
    ("projects", "choose at least one registered project for this sprint to reserve"),
    ("observer", "choose the observer head that will run this sprint"),
)


#: `none` is refused on this route only: it raises no observer, so the "start" button would start
#: nothing. It stays legal for `ummanu sprint create` and existing rows. Compared against the layer's
#: own `NONE_SPELLING`.
OBSERVER_MUST_BE_A_PROFILE = (
    "a sprint opened from here names the head that will run it: opening one with no observer means "
    "nothing is raised for it, which is not what this page's button says. Choose a profile, or open "
    "such a sprint with `ummanu sprint create --observer none`"
)


def _incomplete(submitted: dict[str, Any]) -> dict[str, str]:
    errors = {name: reason for name, reason in _REQUIRED if not submitted.get(name)}
    if "observer" not in errors and submitted.get("observer") == NONE_SPELLING:
        errors["observer"] = OBSERVER_MUST_BE_A_PROFILE
    return errors


def _pin(value: str) -> str | None:
    """One executor select, as the layer spells it: a profile, or `None` (nothing said, unpinned).

    An empty string must never travel down as if it were a profile name.
    """
    text = str(value or "").strip()
    return None if text == EXECUTOR_UNPINNED else text


def _owner_events_back(body: dict[str, Any]) -> str:
    """Where an owner event form returns: the all view when pressed from it, else the unread default."""
    return "/owner-events?all=1" if _one(body, "view") == "all" else "/owner-events"


def _request_id() -> str:
    """The id one form carries for its whole life. See :meth:`WebApp._sprint_form`."""
    return f"web-sprint-{uuid.uuid4()}"


# -- who may make a mutation ----------------------------------------------------------------------

#: The cross-origin refusal text, shown on the page.
CROSS_ORIGIN_REFUSAL = (
    "this request was made from a page this service did not serve, so it is refused before any "
    "operation runs; open the page from this service's own address and submit it there"
)


def cross_origin_reason(headers: Any) -> str | None:
    """Why this POST is refused as cross-origin, or `None` if it may proceed.

    Browsers send `Origin` on every non-GET/HEAD request, so an `Origin` whose host differs from
    `Host` (or `null`, or no `Host`) is refused. No `Origin` means not a browser (`ummanu web-run`,
    `curl`) and is allowed. Compare host and port, never scheme: the TLS front proxies to
    `127.0.0.1` over plain HTTP.
    """
    origin = _header(headers, "Origin")
    if not origin:
        return None
    if origin.strip().lower() == "null":
        return CROSS_ORIGIN_REFUSAL
    host = _header(headers, "Host")
    if not host:
        return CROSS_ORIGIN_REFUSAL
    return None if _authority(origin) == host.strip().lower() else CROSS_ORIGIN_REFUSAL


def _authority(origin: str) -> str:
    """The `host:port` of an origin, with the scheme dropped. See :func:`cross_origin_reason`."""
    text = origin.strip().lower()
    _scheme, separator, rest = text.partition("://")
    return (rest if separator else text).split("/")[0]


def _header(headers: Any, name: str) -> str:
    """One header, from whatever the caller was handed: a mapping, or `http.client.HTTPMessage`."""
    if headers is None:
        return ""
    getter = getattr(headers, "get", None)
    if getter is None:
        return ""
    value = getter(name)
    if value is None:
        value = getter(name.lower())
    return str(value or "")


# -- responses ----------------------------------------------------------------------------------


def _json(status: int, document: Any) -> Response:
    return Response(status, json.dumps(document, sort_keys=True).encode("utf-8"), JSON_TYPE)


def _html(status: int, markup: str) -> Response:
    return Response(status, markup.encode("utf-8"), HTML_TYPE)


def _po_request_id() -> str:
    """The id one PO form carries for its whole life, as the sprint form's does."""
    return f"web-po-{uuid.uuid4()}"


def _as_refusal(exc: Exception) -> ReadError:
    """A /po form's failure as a refusal to render; an exception of any other type is unmarked."""
    if isinstance(exc, ReadError):
        return exc
    return RuntimeUnavailable(f"the PO layer failed: {type(exc).__name__}: {exc}")


def _keeps_request_id(exc: ReadError) -> bool:
    """Whether a refused create or send form keeps its request id: always, unless it wrote nothing.

    Only a refusal marked `nothing_written` (`ummanu.webproto.errors.NOTHING_WRITTEN`) gets a fresh
    id. Anything else may follow an accepted request, and only the same id makes a resend a replay.
    """
    return exc.data.get("nothing_written") is not True


def _presented_cookie(headers: Any) -> str:
    """The PO cookie's value from the `Cookie` header, or "" when there is none or it does not parse."""
    raw = _header(headers, "Cookie")
    if not raw:
        return ""
    jar: SimpleCookie = SimpleCookie()
    try:
        jar.load(raw)
    except CookieError:
        return ""
    morsel = jar.get(PO_COOKIE_NAME)
    return morsel.value if morsel is not None else ""


def via_tls(headers: Any) -> bool:
    """Whether the browser reached this service through the TLS front.

    The front sets `X-Forwarded-Proto` and replaces a client's value; a loopback client forging it
    only makes its own cookie stricter.
    """
    return _header(headers, "X-Forwarded-Proto").split(",")[0].strip().lower() == "https"


def _secure_cookie(response: Response, headers: Any) -> Response:
    """A cookie set in answer to a request that came through TLS is `Secure`."""
    cookie = response.headers.get("Set-Cookie")
    if not cookie or not via_tls(headers):
        return response
    return replace(response, headers={**response.headers, "Set-Cookie": f"{cookie}; Secure"})


def _redirect(location: str, *, what: str = "this sprint is open") -> Response:
    """See the thing that was made, at its own address.

    303 so the browser GETs the result and a refresh re-reads it instead of re-posting the form.
    """
    return Response(
        303, pages.redirect(location, what=what).encode("utf-8"), HTML_TYPE, {"Location": location}
    )


def _owner_read_feedback(marked: int, document: dict[str, Any]) -> str:
    held = document["held_count"]
    needs = document["needs_owner_count"]
    return (f"Marked {marked} notices read; {needs} owner-attention events remain: "
            f"{held} held by an unanswered handover or unresolved escalation, "
            f"{needs - held} can be marked read individually. Bulk read marks notices only.")
