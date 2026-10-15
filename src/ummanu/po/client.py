"""The PO service's local endpoint, seen from a submitter: the web, the upgrade, later the dispatcher.

The service (`ummanu.po.service`, unit `ummanu-po.service`) listens on the Unix socket
``<data_dir>/po-service/po.sock`` (mode 0600, in a 0700 directory, so only the runtime user and root
reach it). One connection carries one request, a JSON object on one line with an ``op``, and one
answer line: ``{"ok": true, "result": {...}}`` or ``{"ok": false, "error": {"code", "message"}}``.
The codes are the store's outcomes (`session_not_found`, `session_closed`, `request_conflict`,
`turn_in_progress`) plus `validation`, `unavailable` and `outcome_unknown`; :class:`PoServiceClient`
raises the store's own exception for the first four, so a caller keeps the vocabulary it had when the
runner was local. A refusal the service knows wrote nothing carries ``"nothing_written": true``, and the
raised exception an attribute of the same name; every other refusal may follow an accepted request, and
its safe repeat is the same request id.

Two failures are told apart, because a submitter acts on them differently. Before the request line is
written — no socket, the connection refused, the send failing — nothing reached the service: the
service executes only a complete line, so :class:`ServiceUnavailable` means "not running, nothing
written". After the line was written a lost or late answer is :class:`OutcomeUnknown`: the service may
have carried the request out, and the safe repeat is the same request (the same request id), which the
service answers as a replay. Nothing here falls back to running a turn: this module imports no runner.
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ummanu.po.store import RequestConflict, SessionClosed, SessionNotFound, TurnInProgress

SERVICE_DIR_NAME = "po-service"
SOCKET_NAME = "po.sock"
LOCK_NAME = "service.lock"
RESTART_MARKER_NAME = "restart-pending"
# Written by the service when it starts; `ummanu upgrade` (`step_po`) compares it with the checkout.
PROCESS_RECEIPT_NAME = "process-receipt.json"
# A stop joins the turn's waiter for up to ten seconds; a submit may launch a turn.
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_MESSAGE_BYTES = 4 * 1024 * 1024
NOT_RUNNING = "the PO service is not running (ummanu-po.service)"


class PoServiceError(RuntimeError):
    """The PO service refused a request or could not be asked."""


class ServiceUnavailable(PoServiceError):
    """Nothing reached the PO service: no socket, the connection refused, or the send failed."""

    nothing_written = True


class OutcomeUnknown(PoServiceError):
    """The request was written to the service and no readable answer came back: it may have been done."""


class ServiceRefused(PoServiceError):
    """The service answered with a refusal outside the store's own outcomes."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


_STORE_OUTCOMES: dict[str, type[Exception]] = {
    "session_not_found": SessionNotFound,
    "session_closed": SessionClosed,
    "request_conflict": RequestConflict,
    "turn_in_progress": TurnInProgress,
}


def service_dir(data_dir: Path | str) -> Path:
    return Path(data_dir) / SERVICE_DIR_NAME


def socket_path(data_dir: Path | str) -> Path:
    return service_dir(data_dir) / SOCKET_NAME


def restart_marker_path(data_dir: Path | str) -> Path:
    return service_dir(data_dir) / RESTART_MARKER_NAME


def process_receipt_path(data_dir: Path | str) -> Path:
    return service_dir(data_dir) / PROCESS_RECEIPT_NAME


class PoServiceClient:
    """Requests to one installation's PO service. Construction does no I/O."""

    def __init__(self, data_dir: Path | str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.path = socket_path(data_dir)
        self.timeout = timeout

    def create_session(self, *, cli: str, model: str, effort: str, request_id: str) -> dict[str, Any]:
        return self.call("create_session", cli=cli, model=model, effort=effort, request_id=request_id)

    def submit(
        self,
        *,
        session_id: str,
        text: str,
        request_id: str,
        source: str = "web",
        card: dict[str, Any] | None = None,
        attachments: list[str] | None = None,
    ) -> dict[str, Any]:
        """One input; a dispatcher's carries its card's facts (`card`), which the service checks first.

        The answer is the input queued or the turn it became. An operation card's input is queued with
        the service's production rights note; the rule itself refuses nothing. `attachments` names
        images the session holds, whose paths the text carries.
        """
        fields: dict[str, Any] = {"card": card} if card is not None else {}
        if attachments:
            fields["attachments"] = list(attachments)
        return self.call("submit", session_id=session_id, text=text, request_id=request_id, source=source, **fields)

    def sprint_session(self, *, sprint_ref: str, request_id: str) -> dict[str, Any]:
        """The live PO session of a sprint, `{session_id, created, repeated}` (`PoService.sprint_session`)."""
        return self.call("sprint_session", sprint_ref=sprint_ref, request_id=request_id)

    def stop_turn(self, *, session_id: str, seq: int) -> dict[str, Any]:
        return self.call("stop_turn", session_id=session_id, seq=seq)

    def close_session(self, *, session_id: str, actor: str) -> dict[str, Any]:
        return self.call("close_session", session_id=session_id, actor=actor)

    def rename_session(self, *, session_id: str, title: str) -> dict[str, Any]:
        return self.call("rename_session", session_id=session_id, title=title)

    def status(self) -> dict[str, Any]:
        return self.call("status")

    def request_restart(self, *, reason: str) -> dict[str, Any]:
        return self.call("restart", reason=reason)

    def call(self, op: str, **fields: Any) -> dict[str, Any]:
        line = json.dumps({"op": op, **fields}, ensure_ascii=False).encode("utf-8") + b"\n"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(self.timeout)
            try:
                connection.connect(str(self.path))
                # A failed sendall did not deliver the final newline, and the service runs no
                # request without it: still nothing written.
                connection.sendall(line)
            except OSError as exc:
                raise ServiceUnavailable(f"{NOT_RUNNING}: {self.path}: {exc.strerror or exc}") from None
            try:
                connection.shutdown(socket.SHUT_WR)
                answer = _read_line(connection)
            except OSError as exc:
                raise OutcomeUnknown(
                    f"no answer from the PO service on {self.path}: {exc.strerror or exc}"
                ) from None
        try:
            document = json.loads(answer)
        except ValueError:
            raise OutcomeUnknown(f"no readable answer from the PO service on {self.path}") from None
        if not isinstance(document, dict):
            raise OutcomeUnknown(f"no readable answer from the PO service on {self.path}")
        if document.get("ok") is True and isinstance(document.get("result"), dict):
            return document["result"]
        error = document.get("error") if isinstance(document.get("error"), dict) else {}
        code = str(error.get("code") or "unavailable")
        message = str(error.get("message") or "the PO service refused the request")
        if code == "outcome_unknown":
            raise OutcomeUnknown(message)
        outcome = _STORE_OUTCOMES.get(code)
        refusal = outcome(message) if outcome is not None else ServiceRefused(code, message)
        # Only the service says a refusal wrote nothing; the exception carries that on.
        refusal.nothing_written = error.get("nothing_written") is True  # type: ignore[attr-defined]
        raise refusal


def _read_line(connection: socket.socket) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = connection.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if b"\n" in chunk or size > MAX_MESSAGE_BYTES:
            break
    return b"".join(chunks).split(b"\n", 1)[0]


@dataclass(frozen=True)
class RestartAnswer:
    """What the PO service said to a restart request: `now`, `deferred` or `unanswered`."""

    outcome: str
    running: int | None
    detail: str


def write_restart_marker(data_dir: Path | str, reason: str) -> Path:
    """Durably ask the PO service to restart once it runs no turn; it reads this between turns."""
    from ummanu.po.queue import write_durably

    path = restart_marker_path(data_dir)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_durably(path, json.dumps({"reason": reason, "pid": os.getpid()}, ensure_ascii=False))
    return path


def read_restart_marker(data_dir: Path | str) -> dict[str, str] | None:
    """The pending restart request: `reason` and `requested_at` (the marker's mtime); None without one."""
    path = restart_marker_path(data_dir)
    try:
        requested_at = datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    return {"reason": str(document["reason"]), "requested_at": requested_at}


def request_restart(
    data_dir: Path | str, reason: str, *, client: PoServiceClient | None = None
) -> RestartAnswer:
    """The one way to restart the PO service for new process inputs: never while a turn runs.

    The marker is written first, so the request survives whatever happens to the socket; the service
    then answers whether it is idle (it exits now, and `Restart=always` starts the new code) or busy
    (it starts no new turn and exits as soon as its running turns settle). The rule itself is the
    service's (`PoService.request_restart`); this only asks.
    """
    write_restart_marker(data_dir, reason)
    try:
        answer = (client or PoServiceClient(data_dir)).request_restart(reason=reason)
    except PoServiceError as exc:
        return RestartAnswer("unanswered", None, str(exc))
    outcome = str(answer.get("restart") or "")
    running = answer.get("running")
    running = running if isinstance(running, int) else None
    if outcome not in ("now", "deferred"):
        return RestartAnswer("unanswered", running, f"the PO service answered {answer!r}")
    return RestartAnswer(outcome, running, str(answer.get("detail") or ""))


__all__ = [
    "NOT_RUNNING",
    "OutcomeUnknown",
    "PoServiceClient",
    "PoServiceError",
    "RestartAnswer",
    "ServiceRefused",
    "ServiceUnavailable",
    "process_receipt_path",
    "read_restart_marker",
    "request_restart",
    "restart_marker_path",
    "service_dir",
    "socket_path",
    "write_restart_marker",
]
