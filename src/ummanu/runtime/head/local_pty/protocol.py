"""The supervisor's socket wire and the limits that bound it.

One JSON request per line, one JSON response per line, payloads base64. Every refusal names the
limit it hit and the size that hit it. Invariants:

  * A response repeats its request's `id`, so a caller can discard a stale frame left by an
    abandoned request instead of reading it as the next answer.
  * No answer waits for the head: input is admitted, then written by the supervisor loop; head
    speed changes what `status` reports, never how long a caller waits.

Limits:

  * `INPUT_MAX_BYTES` (64 KiB): continuation payloads are kilobytes; over the limit is a named
    refusal, never truncated or split. It is real only because the pty is non-canonical
    (canonical mode silently truncates a line at 4095 bytes).
  * `FRAME_MAX_BYTES`: bounds a request line before parsing; sized so any input within
    `INPUT_MAX_BYTES` always fits after base64 and the JSON envelope.
  * `OUTPUT_BUFFER_BYTES`: kept output; readers get the freshest tail plus a dropped count.
  * `ATTACH_MAX_CLIENTS`: bounds per-attachment socket and buffer memory.
  * `CONNECTION_MAX_CLIENTS`: bounds merely-dialled callers (leaked probes, clients stuck
    mid-frame), each costing a descriptor and an inbox up to `FRAME_MAX_BYTES`.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

#: The largest payload one `input` request may carry.
INPUT_MAX_BYTES = 64 * 1024
#: Envelope slack over `INPUT_MAX_BYTES`: base64 costs 4 bytes per 3, plus the JSON keys.
_ENVELOPE_SLACK_BYTES = 4096
#: The largest single request line the supervisor will read before refusing the connection.
FRAME_MAX_BYTES = (INPUT_MAX_BYTES * 4 + 2) // 3 + _ENVELOPE_SLACK_BYTES
#: How much of the head's output the supervisor keeps for a reader that was not attached.
OUTPUT_BUFFER_BYTES = 256 * 1024
#: How many callers may hold the head's live stream at once.
ATTACH_MAX_CLIENTS = 4
#: How many callers may hold a connection at once, attached or not: every attachment plus room for
#: the short-lived probes that connect, ask one question and leave.
CONNECTION_MAX_CLIENTS = ATTACH_MAX_CLIENTS * 4

#: File names inside one run directory. A caller that knows the run directory knows all of them.
SOCKET_NAME = "head.sock"
JOURNAL_NAME = "journal.jsonl"
PID_FILE_NAME = "head.pid"
SUPERVISOR_PID_NAME = "supervisor.pid"
SUPERVISOR_LOCK_NAME = "supervisor.lock"
SUPERVISOR_LOG_NAME = "supervisor.log"
#: A refusal on the way up: the supervisor never took the run over, and nothing of it is running.
STARTUP_ERROR_NAME = "startup.error"
#: A failure after the run was up (distinct from a startup failure).
SUPERVISOR_ERROR_NAME = "supervisor.error"
#: Request verbs.
OP_STATUS = "status"
OP_INPUT = "input"
OP_OUTPUT = "output"
OP_ATTACH = "attach"
OP_RESIZE = "resize"
OP_DRAIN = "drain"
OP_STOP = "stop"
OPS = (OP_STATUS, OP_INPUT, OP_OUTPUT, OP_ATTACH, OP_RESIZE, OP_DRAIN, OP_STOP)

#: The correlation key. An answer repeats a request's id verbatim; frames answering no particular
#: request (connection refused, unparseable bytes) carry none.
REQUEST_ID = "id"

#: Delivery states as `status` reports them; progress is state to ask about, not a wait.
DELIVERY_IN_FLIGHT = "in_flight"
DELIVERY_COMPLETE = "complete"
DELIVERY_STALLED = "stalled"
DELIVERY_FAILED = "failed"
DELIVERY_STATES = (DELIVERY_IN_FLIGHT, DELIVERY_COMPLETE, DELIVERY_STALLED, DELIVERY_FAILED)

#: Refusal tokens callers route on. A stall is not a refusal (the request was accepted): it is
#: `DELIVERY_STALLED` in `status` and the journal.
ERROR_INPUT_TOO_LARGE = "input_too_large"
ERROR_FRAME_TOO_LARGE = "frame_too_large"
ERROR_ATTACH_LIMIT = "attach_limit"
ERROR_CONNECTION_LIMIT = "connection_limit"
ERROR_INPUT_IN_FLIGHT = "input_in_flight"
ERROR_DRAINING = "draining"
ERROR_HEAD_GONE = "head_gone"
ERROR_MALFORMED = "malformed_request"
ERROR_UNKNOWN_OP = "unknown_op"

#: Pushed frames an attached client receives, as distinct from responses to its own requests.
EVENT_OUTPUT = "output"
EVENT_DROPPED = "dropped"
EVENT_EXITED = "exited"

#: Kernel `sun_path` bound; checked where the path is built because the kernel's failure is opaque.
SUN_PATH_MAX = 100


class ProtocolError(RuntimeError):
    """A frame that cannot be spoken or understood."""


def run_dir_for(root: str | os.PathLike[str], run_id: str) -> Path:
    """The one directory that holds everything about one run."""
    if not run_id or "/" in run_id or run_id in (".", ".."):
        raise ProtocolError(f"a run directory is named by a run id, not by {run_id!r}")
    return Path(root) / run_id


def socket_path_for(run_dir: str | os.PathLike[str]) -> Path:
    """The predictable socket path for a run directory, checked against the kernel's limit."""
    path = Path(run_dir) / SOCKET_NAME
    if len(str(path).encode("utf-8")) > SUN_PATH_MAX:
        raise ProtocolError(
            f"the head socket path is {len(str(path))} bytes, over the {SUN_PATH_MAX}-byte "
            f"limit a Unix socket address has: {path}"
        )
    return path


def encode_frame(payload: dict[str, Any]) -> bytes:
    """One JSON object, one line."""
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def decode_frame(line: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError(f"unreadable frame: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ProtocolError("a frame is a JSON object")
    return parsed


def encode_payload(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def decode_payload(data: Any) -> bytes:
    if not isinstance(data, str):
        raise ProtocolError("a payload is base64 text")
    try:
        return base64.b64decode(data.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise ProtocolError(f"a payload is base64 text: {exc}") from exc


def input_refusal(size: int) -> dict[str, Any]:
    """The refusal an oversized input gets: the limit, the actual size, and no truncation."""
    return {
        "ok": False,
        "error": ERROR_INPUT_TOO_LARGE,
        "limit_bytes": INPUT_MAX_BYTES,
        "size_bytes": size,
        "detail": (
            f"input of {size} bytes exceeds the {INPUT_MAX_BYTES}-byte limit; "
            "it was neither truncated nor split"
        ),
    }


#: How long the loop carries an admitted payload for a head that stopped reading before abandoning
#: the rest and recording `stalled`. No request waits on it; it only changes when `status` says
#: `stalled`.
INPUT_DELIVERY_SECONDS = 10.0


def in_flight_refusal(delivery: dict[str, Any]) -> dict[str, Any]:
    """The refusal while a previous delivery is still being written; carries that delivery.

    One payload at a time: two in flight would interleave on the terminal and make what the head
    received undescribable.
    """
    return {
        "ok": False,
        "error": ERROR_INPUT_IN_FLIGHT,
        "delivery": delivery,
        "detail": (
            f"delivery {delivery.get('id')} is still being written "
            f"({delivery.get('written_bytes')} of {delivery.get('size_bytes')} bytes); "
            "this head takes one payload at a time"
        ),
    }


def delivery_detail(state: str, size: int, written: int, why: str, seconds: float) -> str:
    """How a delivery reads in `status` and the journal; a partial one always shows both sizes."""
    if state == DELIVERY_COMPLETE:
        return f"all {written} bytes reached the head's terminal"
    if state == DELIVERY_IN_FLIGHT:
        return f"{written} of {size} bytes have reached the head's terminal so far"
    if state == DELIVERY_STALLED:
        return (
            f"{written} of {size} bytes reached the head's terminal within {seconds:g}s and the "
            f"delivery was abandoned there: {why}"
        )
    return f"{written} of {size} bytes reached the head's terminal and the delivery ended: {why}"


def connection_refusal(connections: int) -> dict[str, Any]:
    """The refusal a caller gets when the supervisor is already holding all the callers it will."""
    return {
        "ok": False,
        "error": ERROR_CONNECTION_LIMIT,
        "limit": CONNECTION_MAX_CLIENTS,
        "connections": connections,
        "detail": (
            f"{connections} callers already hold a connection to this head, which is the "
            f"{CONNECTION_MAX_CLIENTS}-connection limit"
        ),
    }
