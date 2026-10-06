"""The operations that open a sprint, comment on one and close one, and nothing else about what a
sprint is.

Every rule stays in `SprintWriter` (`create`, `comment`, `close`); this layer adds only a typed
refusal in place of a `TaskError` and a request id that owns the outcome. There is no "start" verb:
the production tick raises the observer of an open sprint (`ummanu.dispatch.observer`).

- Create: the request id is claimed in :mod:`ummanu.webproto.sprint_requests` before the writer
  runs and the sprint reference is recorded under it after; a repeat answers from the record, and a
  claim without a reference resumes the writer's staged create under the same id. Any failure after
  the row exists is an `OperationPending` (see :meth:`SprintOperationLayer._after_create`).
- Comment and close need no request index here: the writer's audit claim (comment) or staged close
  transaction already makes a repeat idempotent. A comment answer says saved, never read or accepted
  by the observer (`ummanu.webproto.sprint_reads.ACCEPTANCE_ISSUE`).
- A close is not a completed Definition of Done (:data:`ummanu.sprint_close.CLOSE_NOT_DONE`).

See docs/PROTOCOLS.md, "Opening and watching a sprint".
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ummanu.board.backend import SPRINT, board_client
from ummanu.board.local_run import parse_local_run_exceptions
from ummanu.config import InstanceReport, validate_instance
from ummanu.sprint_observer import observer_choice
from ummanu.sprints import SprintWriter
from ummanu.tasks import TaskError, _digest
from ummanu.webproto import sources
from ummanu.webproto.boundary import ProtocolBoundary
from ummanu.webproto.errors import (
    IDENTITY_REFUSALS,
    InstallationUnavailable,
    OperationPending,
    OwnerConflict,
    RuntimeUnavailable,
    TaskNotFound,
    ValidationRefused,
)
from ummanu.webproto.runs import RequestMismatch, RunStoreError, request_fingerprint
from ummanu.webproto.sprint_reads import SprintReadLayer
from ummanu.webproto.sprint_requests import SPRINT_CREATE_OPERATION, SprintRequestStore

SCHEMA_VERSION = 1

#: Roles `SprintWriter.create` admits, named so a client can offer the choice.
SPRINT_CREATE_ROLES = ("po", "steward")

#: The reason token on an :class:`~ummanu.webproto.errors.OperationPending`, one per operation so a
#: client repeats the right request id.
PENDING_REASON = "sprint_create_pending_repair"
COMMENT_PENDING_REASON = "sprint_comment_pending_repair"

#: The comment operation's name on a pending action; no record in `sprint_requests` (the audit claim
#: on `request_id` already makes a repeat idempotent).
SPRINT_COMMENT_OPERATION = "sprint_comment"

#: The close operation's name on a pending action; no record in `sprint_requests` (`SprintWriter.close`
#: stages and resumes the close under its request id).
SPRINT_CLOSE_OPERATION = "sprint_close"
CLOSE_PENDING_REASON = "sprint_close_pending_repair"

#: Roles `SprintWriter.close` admits: the PO any sprint, the observer only its own sprint.
SPRINT_CLOSE_ROLES = ("po", "observer")

#: Roles `SprintWriter.comment` admits, named so a client can offer the choice.
SPRINT_COMMENT_ROLES = ("po", "dispatcher", "worker", "reviewer", "observer", "steward", "retro")

#: The audit event kind of a sprint comment; used to tell a repeat from an id owning another write.
COMMENT_EVENT_KIND = "commented"

#: Maps a sprint writer `TaskError` code to this layer's code; a mapping, never a re-decision.
#: Refusals on the state of the world (another sprint holds the project, open-sprint limit, closed
#: sprint, live work, concurrent move) are `owner_conflict`: the same request may succeed later.
_CODES: dict[str, Any] = {
    "validation": ValidationRefused,
    "role_forbidden": ValidationRefused,
    "not_found": TaskNotFound,
    "sprint_conflict": OwnerConflict,
    "resource_conflict": OwnerConflict,
    "closed": OwnerConflict,
    "live_work": OwnerConflict,
    "close_plan_forbidden": OwnerConflict,
    "close_conflict": OwnerConflict,
    "backend_error": RuntimeUnavailable,
}


class SprintOperationLayer(ProtocolBoundary):
    """One installation's sprint operations, with no knowledge of who is asking.

    Construction does no I/O. `board_client` and `clock` are seams for tests or transports, not modes.
    """

    def __init__(
        self,
        instance: str | Path,
        *,
        data_dir: str | Path | None = None,
        board_client: Any | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.instance = Path(instance)
        self._data_dir = Path(data_dir) if data_dir is not None else None
        self._board_client = board_client
        self._clock = clock

    # -- shared plumbing -------------------------------------------------------------------

    def report(self) -> InstanceReport:
        report = validate_instance(self.instance)
        if not report.ok or report.data_dir is None:
            raise InstallationUnavailable(
                "this instance config does not validate: "
                + "; ".join(str(error) for error in report.errors[:5])
            )
        return report

    def data_dir(self, report: InstanceReport | None = None) -> Path:
        if self._data_dir is not None:
            return self._data_dir
        report = report if report is not None else self.report()
        assert report.data_dir is not None
        return report.data_dir

    # -- operations ------------------------------------------------------------------------

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
        """Open one sprint, or hand back the sprint this request id already opened.

        Order: claim the request id, call the writer with the same id, record the reference. A repeat
        with a recorded reference never calls the writer; one with a bare claim re-calls it and the
        writer resumes its staged create.

        `worker`/`reviewer` `None` means unpinned and is stored as an absent field; there is no
        "unpin" spelling (empty string and `none` are refused below). `observer` is a profile id or
        `none`, converted by :func:`ummanu.sprint_observer.observer_choice`; the writer judges it.
        """
        now = self._clock()
        if not str(request_id or "").strip():
            raise ValidationRefused("a sprint operation names the request it is made under")
        report = self.report()
        data_dir = self.data_dir(report)
        store = SprintRequestStore(data_dir)
        issue_refs = list(issues or [])
        project_ids = list(projects or [])
        repository_roots = list(repositories or [])
        try:
            exceptions = [
                entry.to_document() for entry in parse_local_run_exceptions(
                    [] if local_run_exceptions is None else local_run_exceptions, projects=project_ids
                )
            ]
        except ValueError as exc:
            raise ValidationRefused(str(exc)) from None
        fingerprint = request_fingerprint(
            SPRINT_CREATE_OPERATION,
            {
                "role": role,
                "actor": actor,
                "product": product,
                "goal": goal,
                "definition_of_done": definition_of_done,
                "reference": reference,
                "observer": observer,
                # JSON-encoded: `request_fingerprint` digests strings, and a bare list would
                # fingerprint as the empty string.
                "issues": json.dumps(issue_refs),
                "projects": json.dumps(project_ids),
                "repositories": json.dumps(repository_roots),
                # `null`, `"codex"` and `""` are three different requests.
                "worker": json.dumps(worker),
                "reviewer": json.dumps(reviewer),
                # Omit the empty default: old claimed web requests keep their fingerprint.
                **({"local_run_exceptions": json.dumps(exceptions, sort_keys=True)} if exceptions else {}),
            },
        )
        existing = self._existing(store, request_id, fingerprint=fingerprint)
        if existing is not None and existing.reference:
            return self._document(existing.reference, request_id=request_id, claimed=False, now=now)

        _record, claimed = self._claim(store, request_id, fingerprint=fingerprint, now=now)
        try:
            created = self._writer(report, data_dir).create(
                role=role,
                actor=actor,
                goal=goal,
                definition_of_done=definition_of_done,
                repositories=repository_roots,
                product=product,
                issues=issue_refs,
                projects=project_ids,
                reference=reference,
                request_id=request_id,
                observer=observer_choice(observer),
                worker=worker,
                reviewer=reviewer,
                local_run_exceptions=exceptions,
            )
        except TaskError as exc:
            raise self._refusal(exc, request_id=request_id) from None
        # A sprint row exists from here; every failure goes through :meth:`_after_create`.
        return self._after_create(store, created, request_id=request_id, claimed=claimed, now=now)

    def sprint_comment(
        self,
        *,
        request_id: str,
        actor: str,
        reference: str,
        body: str,
        role: str = "po",
    ) -> dict[str, Any]:
        """Put one comment on a sprint, and say where it got to without saying more than that.

        A comment on the entity is how a PO intervenes in a running sprint; no path here edits its
        cards. `comment_id` is the audit event id minted by `SprintWriter._write`, stable across
        repeats. A repeat is answered from the audit's committed claim without re-running the
        mutation (no second comment, event or observer wake); :meth:`_same_comment` refuses a reused
        id over different inputs. `saved` is false on a repeat. `delivery` is the shared read.
        """
        now = self._clock()
        if not str(request_id or "").strip():
            raise ValidationRefused("a sprint operation names the request it is made under")
        if not str(reference or "").strip():
            raise ValidationRefused("a sprint comment names the sprint it is made on")
        report = self.report()
        data_dir = self.data_dir(report)
        try:
            writer = self._writer(report, data_dir)
            owned = writer.audit.committed_event(request_id) or writer.audit.pending_event(request_id)
        except TaskError as exc:
            raise self._comment_refusal(exc, request_id=request_id) from None
        if owned is not None:
            self._same_comment(
                owned, role=role, actor=actor, reference=reference, body=body, request_id=request_id
            )
        try:
            written = writer.comment(
                role=role, actor=actor, reference=reference, body=body, request_id=request_id
            )
        except TaskError as exc:
            raise self._comment_refusal(exc, request_id=request_id) from None
        comment_id = str(written.get("event_id") or "")
        if not comment_id:
            raise RuntimeUnavailable(
                "the sprint writer saved a comment that carries no durable identifier"
            )
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "sprint_comment",
            "observed_at": sources.isoformat(now),
            "request_id": request_id,
            "ref": reference,
            "comment_id": comment_id,
            # False on a repeat, which writes nothing.
            "saved": owned is None,
            "delivery": self._reads().sprint_comment_delivery(reference, comment_id),
        }

    def sprint_close(
        self,
        *,
        request_id: str,
        actor: str,
        reference: str,
        reason: str,
        closeout: str,
        decisions: dict[str, list[dict[str, str]]] | str | None = None,
        role: str = "po",
    ) -> dict[str, Any]:
        """Close one sprint, and answer with what became of its work.

        `decisions` is the normalized document, or decisions-file text parsed by
        `sprint_close.parse_close_decisions` (so the transport refuses what the CLI refuses), or
        nothing. Every close rule stays in `SprintWriter.close` and :mod:`ummanu.sprint_close`.
        The closeout is required here only (the writer takes it as optional for recovery and tests).
        Repeats resume the writer's staged close. The answer states the Definition of Done is not
        satisfied (:data:`ummanu.sprint_close.CLOSE_NOT_DONE`).
        """
        from ummanu.sprint_close import CLOSE_NOT_DONE, parse_close_decisions

        now = self._clock()
        if not str(request_id or "").strip():
            raise ValidationRefused("a sprint operation names the request it is made under")
        if not str(reference or "").strip():
            raise ValidationRefused("a sprint close names the sprint it closes")
        if isinstance(decisions, str):
            try:
                decisions = parse_close_decisions(decisions) if decisions.strip() else None
            except TaskError as exc:
                raise ValidationRefused(exc.message) from None
        if not str(reason or "").strip():
            raise ValidationRefused("a sprint close states why the owner is closing this sprint")
        if not str(closeout or "").strip():
            raise ValidationRefused(
                "a sprint close states what became of the work: pass the closeout this close writes "
                "into state/knowledge. It is the account of the outcome, not a claim that the "
                "Definition of Done was reached"
            )
        report = self.report()
        data_dir = self.data_dir(report)
        try:
            closed = self._writer(report, data_dir).close(
                role=role,
                actor=actor,
                reference=reference,
                decisions=decisions,
                request_id=request_id,
                reason=reason,
                closeout=closeout,
            )
        except TaskError as exc:
            raise self._close_refusal(exc, request_id=request_id) from None
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "sprint_closed",
            "observed_at": sources.isoformat(now),
            "request_id": request_id,
            "ref": reference,
            "event_id": str(closed.get("event_id") or ""),
            # Also on the read below; this is the document a closing PO actually reads.
            "definition_of_done": {"satisfied": False, "reason": CLOSE_NOT_DONE},
            # Read back through the protocol, the same document a later read returns.
            "result": self._reads().sprint_close_result(reference, str(closed.get("event_id") or "")),
        }

    # -- the pieces the operation is made of -------------------------------------------------

    def _existing(self, store: SprintRequestStore, request_id: str, *, fingerprint: str) -> Any:
        """The request this exact id already owns, or nothing, or a typed refusal.

        Reusing an id over different inputs is a validation conflict, as in `run_start`.
        """
        try:
            return store.by_request(
                request_id, operation=SPRINT_CREATE_OPERATION, fingerprint=fingerprint
            )
        except RequestMismatch as exc:
            raise ValidationRefused(str(exc)) from None
        except RunStoreError as exc:
            raise RuntimeUnavailable(str(exc)) from None

    def _claim(
        self, store: SprintRequestStore, request_id: str, *, fingerprint: str, now: float
    ) -> tuple[Any, bool]:
        try:
            return store.claim(
                request_id, operation=SPRINT_CREATE_OPERATION, fingerprint=fingerprint, now=now
            )
        except RequestMismatch as exc:
            raise ValidationRefused(str(exc)) from None
        except RunStoreError as exc:
            raise RuntimeUnavailable(str(exc)) from None

    def _after_create(
        self,
        store: SprintRequestStore,
        created: dict[str, Any],
        *,
        request_id: str,
        claimed: bool,
        now: float,
    ) -> dict[str, Any]:
        """Everything this operation does once a sprint row exists, and the one answer it fails with.

        Any failure here, whatever primitive raised it, becomes an
        :class:`~ummanu.webproto.errors.OperationPending` (`backend_unavailable`, the request id,
        "repeat this same request"). `Exception` is caught on purpose: a list of types would miss the
        next primitive. The cause is chained. The message states the created sprint and the safe move
        before the cause, so a caller does not open a second sprint.
        """
        reference = ""
        try:
            reference = str((created.get("sprint") or {}).get("ref") or "")
            if not reference:
                raise RuntimeUnavailable("the sprint writer created a sprint that carries no reference")
            store.record_reference(request_id, reference)
            return self._document(reference, request_id=request_id, claimed=claimed, now=now)
        except Exception as exc:
            raise OperationPending(
                self._pending_message(reference, exc),
                data=self._pending_action(request_id, reference=reference),
            ) from exc

    @staticmethod
    def _pending_message(reference: str, cause: Exception) -> str:
        """The durable fact first, the cause after it."""
        subject = f"sprint {reference}" if reference else "this sprint"
        return (
            f"{subject} was created and the request that made it did not finish; repeat the same "
            f"request id to pick it up rather than opening a second sprint. "
            f"The step that failed was {type(cause).__name__}: {cause}"
        )

    def _same_comment(
        self,
        owned: dict[str, Any],
        *,
        role: str,
        actor: str,
        reference: str,
        body: str,
        request_id: str,
    ) -> None:
        """Refuse a repeat that reuses this id over different inputs, rather than answering it.

        Compares the audit event the id owns (kind, sprint, actor, `body_sha256`) with this request.
        """
        actor_of = owned.get("actor") if isinstance(owned.get("actor"), dict) else {}
        payload = owned.get("payload") if isinstance(owned.get("payload"), dict) else {}
        same = (
            str(owned.get("kind") or "") == COMMENT_EVENT_KIND
            and str(owned.get("ref") or "") == reference
            and str(actor_of.get("role") or "") == role
            and str(actor_of.get("id") or "") == actor
            and str(payload.get("body_sha256") or "") == _digest(body)
        )
        if not same:
            raise ValidationRefused(
                f"request id {request_id!r} already owns a sprint write made with different inputs; "
                "a repeat is a retry of the same request, not a new one"
            )

    def _comment_refusal(self, exc: TaskError, *, request_id: str) -> Exception:
        """One `TaskError` from `SprintWriter.comment`, as this layer's typed failure."""
        return self._refusal(
            exc,
            request_id=request_id,
            operation=SPRINT_COMMENT_OPERATION,
            reason=COMMENT_PENDING_REASON,
        )

    def _close_refusal(self, exc: TaskError, *, request_id: str) -> Exception:
        """One `TaskError` from `SprintWriter.close`, as this layer's typed failure.

        `audit_pending` names this request id: repeating it resumes the staged close.
        """
        return self._refusal(
            exc,
            request_id=request_id,
            operation=SPRINT_CLOSE_OPERATION,
            reason=CLOSE_PENDING_REASON,
        )

    def _refusal(
        self,
        exc: TaskError,
        *,
        request_id: str,
        operation: str = SPRINT_CREATE_OPERATION,
        reason: str = PENDING_REASON,
    ) -> Exception:
        """One `TaskError` from the sprint writer, as this layer's own typed failure.

        `audit_pending` (part-done, repairable) becomes an `OperationPending` whose data says to
        repeat this request id; a new id would open a second write beside the half-written one.
        """
        if exc.code == "audit_pending":
            return OperationPending(
                exc.message,
                data=self._pending_action(request_id, operation=operation, reason=reason),
            )
        return {**_CODES, **IDENTITY_REFUSALS}.get(exc.code, RuntimeUnavailable)(exc.message)

    def _pending_action(
        self,
        request_id: str,
        *,
        reference: str = "",
        operation: str = SPRINT_CREATE_OPERATION,
        reason: str = PENDING_REASON,
    ) -> dict[str, Any]:
        return {
            "reason": reason,
            "action": {
                "operation": operation,
                "repeat_request": True,
                "request_id": request_id,
                # Only when this layer knows which sprint the half-finished request holds.
                "reference": reference or None,
            },
        }

    def _writer(self, report: InstanceReport, data_dir: Path) -> SprintWriter:
        """The sprint writer of this installation, built per call.

        Budget thresholds come from the already-validated instance config, not a second read.
        """
        thresholds = report.instance.get("sprint_budget") if isinstance(report.instance, dict) else None
        return SprintWriter(
            self._client(),
            data_dir=data_dir,
            thresholds=thresholds if isinstance(thresholds, dict) else None,
            instance=self.instance,
        )

    def _reads(self) -> SprintReadLayer:
        """The read layer this operation answers through, built with this layer's own seams."""
        return SprintReadLayer(
            self.instance,
            data_dir=self._data_dir,
            board_client=self._board_client,
            clock=self._clock,
        )

    def _document(
        self, reference: str, *, request_id: str, claimed: bool, now: float
    ) -> dict[str, Any]:
        """What a create answers with: the request, and the sprint as the read layer reads it.

        Right after a create the launch state honestly says the entity is saved and no observer is up.
        """
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "sprint_created",
            "observed_at": sources.isoformat(now),
            "request_id": request_id,
            # False on a repeat, which creates nothing.
            "created": claimed,
            "sprint": self._reads().sprint_state(reference),
        }

    def _client(self) -> Any:
        """The sprint board of this installation, named through the switch (board/backend.py)."""
        return self._board_client or board_client(
            self.instance.parent if self.instance.is_file() else self.instance, serves=(SPRINT,)
        )


__all__ = [
    "CLOSE_PENDING_REASON",
    "COMMENT_PENDING_REASON",
    "PENDING_REASON",
    "SCHEMA_VERSION",
    "SPRINT_CLOSE_OPERATION",
    "SPRINT_CLOSE_ROLES",
    "SPRINT_COMMENT_OPERATION",
    "SPRINT_COMMENT_ROLES",
    "SPRINT_CREATE_ROLES",
    "SprintOperationLayer",
]
