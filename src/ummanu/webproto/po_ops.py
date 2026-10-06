"""The PO head's sessions for the dashboard: list, read, create, send, stop, close, rename.

A thin client: reads go to the board store (`ummanu.po.store`) and the PO service's queue
directory; every write goes to the PO service over its socket (`ummanu.po.client`). The web runs no
turn; turn, queue and request-id rules stay in the service and store. This layer only checks the
model and effort lists and maps failures to typed codes. An unreachable service refuses a write
with nothing written.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from ummanu.config import ConfigError, DataDirError, instance_data_dir, load_config
from ummanu.po.client import OutcomeUnknown, PoServiceClient, ServiceRefused, ServiceUnavailable
from ummanu.po.models import (
    DEFAULT_EFFORTS,
    EffortRefused,
    efforts_from_instance,
    models_from_instance,
    require_explicit_effort,
)
from ummanu.po.queue import PoQueue, QueueError
from ummanu.po.context_budget import CONTEXT_METRIC, context_budget_bytes, conversation_bytes
from ummanu.po.store import (
    OWNER,
    RUNNING,
    SESSION_CLOSED,
    SESSION_OPEN,
    FeedEntry,
    PoStore,
    PoStoreError,
    RequestConflict,
    Session,
    SessionClosed,
    SessionNotFound,
    Turn,
    TurnInProgress,
)
from ummanu.webproto.boundary import ProtocolBoundary
from ummanu.webproto.errors import (
    NOTHING_WRITTEN,
    InstallationUnavailable,
    PoOutcomeUnknown,
    PoRequestConflict,
    PoSessionClosed,
    PoSessionNotFound,
    PoTurnInProgress,
    RuntimeUnavailable,
    ValidationRefused,
)


class PoLayer(ProtocolBoundary):
    """One installation's PO sessions; construction does no I/O.

    `store`, `client`, `models` and `efforts` are test seams; with `models` and no `efforts`, the
    product's default efforts are offered.
    """

    def __init__(
        self,
        instance: str | Path,
        *,
        data_dir: str | Path | None = None,
        store: PoStore | None = None,
        client: PoServiceClient | None = None,
        models: Mapping[str, tuple[str, ...]] | None = None,
        efforts: Mapping[str, tuple[str, ...]] | None = None,
    ) -> None:
        self.instance = Path(instance)
        self._data_dir = Path(data_dir) if data_dir is not None else None
        self._po_store = store
        self._client = client
        self._models = dict(models) if models is not None else None
        self._efforts = dict(efforts) if efforts is not None else None
        self._lock = threading.Lock()

    # --- reads ------------------------------------------------------------------------------

    def po_models(self) -> dict[str, Any]:
        return {
            "kind": "po_models",
            "models": {cli: list(values) for cli, values in self._model_list().items()},
            "efforts": {cli: list(values) for cli, values in self._effort_list().items()},
        }

    def po_running_count(self) -> dict[str, Any]:
        store = self._store_or_refuse()
        return {"kind": "po_running", "running": len(self._store(store.running_turns))}

    def po_overview(self, closed: bool = False) -> dict[str, Any]:
        """Open sessions, or with `closed` the closed ones; either way the closed count and running turns."""
        store = self._store_or_refuse()
        sessions = self._store(lambda: store.sessions(SESSION_CLOSED if closed else SESSION_OPEN))
        closed_count = len(sessions) if closed else self._store(lambda: store.session_count(SESSION_CLOSED))
        running = self._store(store.running_turns)
        busy = {turn.session_id for turn in running}
        items = [
            {
                **_session(session, running=session.session_id in busy),
                "first_message": session.first_message,
                "first_message_metadata": session.first_message_metadata,
                "last_activity_at": _time(session.last_activity_at),
            }
            for session in sessions
        ]
        return {
            "kind": "po_overview",
            "closed": closed,
            "closed_count": closed_count,
            "sessions": items,
            "running": len(running),
            **{key: value for key, value in self.po_models().items() if key != "kind"},
        }

    def po_session(self, session_id: str) -> dict[str, Any]:
        """The session, its turns and feed from the store, and its messages still in the service's queue.

        `efforts` is what the installation offers per CLI (none on an unreadable config).
        """
        store = self._store_or_refuse()
        session = self._store(lambda: store.session(session_id))
        turns = self._store(lambda: store.turns(session_id))
        feed = self._store(lambda: store.feed(session_id))
        running = next((turn for turn in turns if turn.state == RUNNING), None)
        try:
            threshold = context_budget_bytes(None if self._models is not None else self._instance_config())
        except ValueError as exc:
            raise RuntimeUnavailable(str(exc)) from None
        return {
            "kind": "po_session",
            "session": _session(session, running=running is not None),
            "turns": [_turn(turn) for turn in turns],
            "feed": [_entry(entry) for entry in feed],
            "running": running is not None,
            "running_seq": running.seq if running is not None else None,
            "last_turn": _turn(turns[-1]) if turns else None,
            "queued": self._queued(session_id),
            "efforts": self._offered_efforts(),
            "context_budget": {"measured_bytes": conversation_bytes(feed),
                               "threshold_bytes": threshold, "metric": CONTEXT_METRIC},
        }

    def po_session_titles(self, session_ids: Iterable[str]) -> dict[str, Any]:
        """The title and state of each named session a card page links to.

        A session the store no longer holds is left out; a store that does not answer refuses the call.
        """
        store = self._store_or_refuse()
        sessions: dict[str, dict[str, Any]] = {}
        for session_id in dict.fromkeys(str(value) for value in session_ids if value):
            try:
                session = self._store(lambda session_id=session_id: store.session(session_id))
            except PoSessionNotFound:
                continue
            sessions[session_id] = {"title": session.title, "state": session.state}
        return {"kind": "po_session_titles", "sessions": sessions}

    def _offered_efforts(self) -> dict[str, list[str]]:
        try:
            return {cli: list(values) for cli, values in self._effort_list().items()}
        except InstallationUnavailable:
            return {}

    def _queued(self, session_id: str) -> list[dict[str, Any]]:
        """Messages the PO service holds for this session and has not started, oldest first.

        An unreadable queue directory reads as empty rather than hiding the feed.
        """
        try:
            waiting = PoQueue(self._resolved_data_dir()).pending(session_id)
        except (QueueError, InstallationUnavailable):
            return []
        return [
            {
                "request_id": item.request_id,
                "text": f"{item.text.rstrip()}\n\n{item.note.strip()}\n" if item.note else item.text,
                "source": item.source,
                "queued_at": item.queued_at,
                "metadata": item.metadata,
            }
            for item in waiting
        ]

    # --- writes -----------------------------------------------------------------------------

    def po_create_session(
        self, *, request_id: str, cli: str, model: str, effort: str = ""
    ) -> dict[str, Any]:
        """One session per request id (`PoStore.claim_session`); a repeat answers the same session.

        `effort` must be one offered for `cli` (`require_explicit_effort`); it is bound to the request
        id with the CLI and the model.
        """
        request_id = _required(request_id, "request_id")
        models = self._model_list()
        if cli not in models or not models[cli]:
            offered = ", ".join(name for name, values in models.items() if values)
            raise ValidationRefused(f"a PO session runs one of: {offered}; not {cli!r}", data=NOTHING_WRITTEN)
        if model not in models[cli]:
            raise ValidationRefused(
                f"{model!r} is not a model this installation offers for {cli}: {', '.join(models[cli])}",
                data=NOTHING_WRITTEN,
            )
        try:
            effort = require_explicit_effort(cli, effort, self._effort_list())
        except EffortRefused as exc:
            raise ValidationRefused(str(exc), data=NOTHING_WRITTEN) from None
        client = self._client_or_refuse()
        created = self._store(
            lambda: client.create_session(cli=cli, model=model, effort=effort, request_id=request_id),
            unknown="the PO service may have opened this session; sending the same form again is safe",
        )
        return {
            "kind": "po_session_created",
            "request_id": request_id,
            "session_id": created["session_id"],
            "effort": created["effort"],
            "repeated": bool(created["repeated"]),
        }

    def po_send(self, *, request_id: str, session_id: str, text: str) -> dict[str, Any]:
        """One message into the PO service's queue per request id, bound to this session and this exact text.

        Answers `queued`, or the turn it became (`seq`, `state`). A repeat answers what the first
        submission made and starts nothing; the id reused for anything else is refused.
        """
        request_id = _required(request_id, "request_id")
        if not str(text or "").strip():
            raise ValidationRefused("an empty message starts no turn", data=NOTHING_WRITTEN)
        client = self._client_or_refuse()
        sent = self._store(
            lambda: client.submit(session_id=session_id, text=text, request_id=request_id),
            unknown="the PO service may have accepted this message; sending again with the same form is safe",
        )
        return {
            "kind": "po_turn_queued" if sent.get("queued") else "po_turn_started",
            "request_id": request_id,
            "session_id": session_id,
            "queued": bool(sent.get("queued")),
            "seq": sent.get("seq"),
            "state": sent.get("state"),
            "repeated": bool(sent.get("repeated")),
        }

    def po_stop(self, *, session_id: str, seq: int) -> dict[str, Any]:
        """Stop turn `seq` if it is the one running; a stale stop form stops nothing newer."""
        client = self._client_or_refuse()
        stopped = bool(
            self._store(
                lambda: client.stop_turn(session_id=session_id, seq=seq),
                unknown="the PO service may have stopped this turn; stopping it again is safe",
            ).get("stopped")
        )
        store = self._store_or_refuse()
        turn = self._store(lambda: store.turn(session_id, seq)) if stopped else None
        return {
            "kind": "po_stop",
            "session_id": session_id,
            "seq": seq,
            "stopped": turn is not None,
            "turn": _turn(turn) if turn is not None else None,
        }

    def po_close(self, *, session_id: str) -> dict[str, Any]:
        """Close a session as the owner (`PoStore.close_session`); already closed answers it unchanged.

        A running turn or queued message is `owner_conflict`, nothing written. Idempotent, no request id.
        """
        client = self._client_or_refuse()
        self._store(
            lambda: client.close_session(session_id=session_id, actor=OWNER),
            unknown="the PO service may have closed this session; closing it again is safe",
        )
        store = self._store_or_refuse()
        session = self._store(lambda: store.session(session_id))
        return {"kind": "po_session_closed", "session": _session(session, running=False)}

    def po_rename(self, *, session_id: str, title: str) -> dict[str, Any]:
        """Set the session's title (`PoStore.set_title`), open or closed; an empty title clears it.

        Idempotent, no request id; a title the store refuses is a validation refusal.
        """
        client = self._client_or_refuse()
        renamed = self._store(
            lambda: client.rename_session(session_id=session_id, title=str(title or "")),
            unknown="the PO service may have renamed this session; renaming it again is safe",
        )
        return {"kind": "po_session_renamed", "session_id": renamed["session_id"], "title": renamed["title"]}

    # --- inside the boundary ----------------------------------------------------------------

    def _store_or_refuse(self) -> PoStore:
        with self._lock:
            if self._po_store is None:
                try:
                    self._po_store = PoStore.for_instance(self.instance)
                except Exception as exc:
                    raise RuntimeUnavailable(
                        f"the PO session store is not available: {type(exc).__name__}: {exc}"
                    ) from exc
            return self._po_store

    def _client_or_refuse(self) -> PoServiceClient:
        with self._lock:
            if self._client is None:
                self._client = PoServiceClient(self._resolved_data_dir())
            return self._client

    def _resolved_data_dir(self) -> Path:
        if self._data_dir is None:
            try:
                self._data_dir = instance_data_dir(self.instance)
            except DataDirError as exc:
                raise InstallationUnavailable(str(exc)) from None
        return self._data_dir

    def _model_list(self) -> dict[str, tuple[str, ...]]:
        if self._models is not None:
            return self._models
        return models_from_instance(self._instance_config())

    def _effort_list(self) -> dict[str, tuple[str, ...]]:
        if self._efforts is not None:
            return self._efforts
        if self._models is not None:
            # A layer whose model list is injected reads no config for its efforts either.
            return dict(DEFAULT_EFFORTS)
        return efforts_from_instance(self._instance_config())

    def _instance_config(self) -> Any:
        path = self.instance / "instance.yaml" if self.instance.is_dir() else self.instance
        try:
            return load_config(path)
        except ConfigError as exc:
            raise InstallationUnavailable(str(exc)) from None

    @staticmethod
    def _store(call: Callable[[], Any], *, unknown: str = "") -> Any:
        """Run one store read or service call, its failure translated; `unknown` words a lost answer."""
        try:
            return call()
        except SessionNotFound as exc:
            raise PoSessionNotFound(str(exc), data=_definite(exc)) from None
        except TurnInProgress as exc:
            raise PoTurnInProgress(str(exc)) from None
        except RequestConflict as exc:
            raise PoRequestConflict(str(exc), data=_definite(exc)) from None
        except SessionClosed as exc:
            raise PoSessionClosed(str(exc), data=_definite(exc)) from None
        except ServiceUnavailable as exc:
            raise RuntimeUnavailable(f"{exc}; nothing was sent or written", data=NOTHING_WRITTEN) from None
        except OutcomeUnknown as exc:
            raise PoOutcomeUnknown(
                f"{unknown or 'the PO service may have carried this out'} ({exc})"
            ) from None
        except ServiceRefused as exc:
            if exc.code == "validation":
                raise ValidationRefused(str(exc), data=_definite(exc)) from None
            raise RuntimeUnavailable(str(exc)) from None
        except PoStoreError as exc:
            raise RuntimeUnavailable(str(exc)) from None
        except ImportError as exc:  # no PostgreSQL driver in this interpreter: the store is unavailable
            raise RuntimeUnavailable(f"the PO session store is not available: {exc}") from None


def _required(value: str, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValidationRefused(f"{name} is required", data=NOTHING_WRITTEN)
    return text


def _definite(exc: Exception) -> dict[str, Any] | None:
    """The `nothing_written` marker when the PO service said this refusal wrote nothing, else none."""
    return NOTHING_WRITTEN if getattr(exc, "nothing_written", False) is True else None


def _time(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _session(session: Session, *, running: bool) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        # Set by the owner or the PO; null for an untitled session.
        "title": session.title,
        "cli": session.cli,
        "model": session.model,
        "effort": session.effort,
        # What the latest turn that reported one ran; null before any did (`Turn.resolved_model`).
        "resolved_model": session.resolved_model,
        "created_at": _time(session.created_at),
        "state": session.state,
        "closed_at": _time(session.closed_at),
        "closed_by": session.closed_by,
        "running": running,
    }


def _turn(turn: Turn) -> dict[str, Any]:
    return {
        "seq": turn.seq,
        "state": turn.state,
        "started_at": _time(turn.started_at),
        "finished_at": _time(turn.finished_at),
        "reason": turn.reason,
        "resolved_model": turn.resolved_model,
    }


def _entry(entry: FeedEntry) -> dict[str, Any]:
    return {
        "turn_seq": entry.turn_seq,
        "role": entry.role,
        "text": entry.text,
        "created_at": _time(entry.created_at),
        "metadata": entry.metadata,
    }


__all__ = ["PoLayer"]
