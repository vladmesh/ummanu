"""Small dispatcher helpers kept out of the runtime state machine."""

from __future__ import annotations

import base64
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ummanu.dispatch.state import attempt_request_id, request_token

_ASSIGN_RE = re.compile(r"(?i)\b([A-Z0-9_]*(?:TOKEN|KEY|SECRET|PASSWORD|PASSWD)[A-Z0-9_]*)\s*=\s*\S+")
_BLOB_RE = re.compile(r"\b[A-Za-z0-9+=_-]{40,}\b")
_HEX_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _worker_id(task: dict[str, Any]) -> str:
    slug = task.get("workspace", {}).get("slug") or _slug(task.get("title") or task["ref"])
    return f"{task['ref']}-{slug}"[:80].strip("-")


def _legacy_worker_branch(reference: str) -> str:
    return f"pipeline/{reference}"


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return cleaned[:30] or "task"


def _last_marker(task: dict[str, Any], baseline: int, markers: set[str]) -> str | None:
    result = None
    for comment in (task.get("comments") or [])[baseline:]:
        marker = comment.get("marker")
        if marker in markers:
            result = marker
    return result


# How many substantive red reviews a card with nobody to decide for it may collect before it
# stops asking for another worker round (secretary-1033). Three is deliberately early and will
# fire on cards that were about to converge: without an observer, Blocked is a question to a
# person rather than lost work, and it is cheaper to ask than to let a card eat five rounds.
RED_REVIEW_CEILING = 3


def red_review_count(task: dict[str, Any]) -> int:
    """The card's own count of substantive red reviews.

    A reviewer's red verdict is one comment marked `review:red` and the board keeps it for the life
    of the card, so the count is durable and idempotent without bookkeeping. A red mechanical gate
    and a red CI rollup leave dispatcher comments with no verdict marker and are not counted.
    """
    return sum(1 for comment in (task.get("comments") or []) if comment.get("marker") == "review:red")


# The observer decision reaches the worker as prose in its TASK.md, and is recorded once more on a
# single hidden line so a dispatcher record lost mid-round can read back the instruction the live
# worker was actually given.
#
# The recorded copy is base64 of the UTF-8 text, not the text between fences. A card description
# and a decision body are both arbitrary Markdown, so either may contain whatever a fence is made
# of: a description carrying an opening fence would be read as the decision, and a decision
# carrying a closing one would be read truncated. Base64 contains no character that can end the
# field, and the line is written on every worker document, last, after every section either of
# those two can write into. So the final record in the file is always the dispatcher's own, and a
# round with no decision reads back as none rather than as whatever the description happens to
# contain.
_DECISION_RECORD_RE = re.compile(
    r"^<!-- observer-decision generation=(\d+) body=([A-Za-z0-9+/]*={0,2}) -->$", re.MULTILINE
)


def _decision_record_line(generation: int, decision: str) -> str:
    """The hidden line the recovery in `_task_doc_decision` reads back."""
    encoded = base64.b64encode((decision or "").strip().encode("utf-8")).decode("ascii")
    return f"<!-- observer-decision generation={int(generation)} body={encoded} -->"


_PROTOCOL_PREREQUISITES_RECORD_RE = re.compile(
    r"^<!-- protocol-prerequisites generation=(\d+) names=([A-Za-z0-9+/]*={0,2}) -->$", re.MULTILINE
)


def _protocol_prerequisites_record_line(generation: int, prerequisites: Iterable[str]) -> str:
    """The structured prerequisite names the dispatcher, not decision prose, gave the worker."""
    payload = "\n".join(prerequisites)
    encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    return f"<!-- protocol-prerequisites generation={int(generation)} names={encoded} -->"


# The round's own identity, recorded the same way and for the same reason as the decision above:
# the report commands are rendered as prose in the same document as the card description, and a
# description is arbitrary Markdown. Reading the ids back by scanning every `--request-id` token in
# the file made any description carrying such a token an id "this round issued", so a report
# committed under it ended a round the dispatcher never handed it to (secretary-1065).
#
# A second record rather than a field on the decision line: the two answer different questions and
# fail independently. A decision body that cannot be decoded must read back as "no decision", which
# is a legitimate state; the round's ids failing to decode must fall through to what the dispatcher
# would issue itself. One line would tie those two fallbacks together, and would make the ids
# unreadable on every document whose decision payload is malformed.
_ROUND_RECORD_RE = re.compile(
    r"^<!-- report-round generation=(\d+) ids=([A-Za-z0-9+/]*={0,2}) -->$", re.MULTILINE
)


def _round_record_line(generation: int, request_ids: Iterable[str]) -> str:
    """The hidden line naming the report request ids this document handed its worker."""
    payload = "\n".join(sorted(request_ids))
    encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    return f"<!-- report-round generation={int(generation)} ids={encoded} -->"


def _task_doc_round_record(workspace: str) -> tuple[int, set[str]]:
    """The round this checkout's `TASK.md` names, as `(generation, request ids)`.

    The last record in the file wins and the dispatcher writes its own last, after every section a
    card description or an observer decision can write into, so a forged line earlier in the document
    is outranked. Written on every worker document, empty round included: the absence of a record has
    to read as absence rather than as whatever the description happens to contain.
    """
    if not workspace:
        return 0, set()
    try:
        document = (Path(workspace) / "TASK.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return 0, set()
    records = _ROUND_RECORD_RE.findall(document)
    if not records:
        return 0, set()
    generation, encoded = records[-1]
    try:
        decoded = base64.b64decode(encoded.encode("ascii"), validate=True).decode("utf-8")
    except (ValueError, UnicodeError):
        return 0, set()
    return int(generation), {line for line in decoded.splitlines() if line}


_GATE_RED_PREFIX = "The mechanical validation gate is red"
# Hidden marker line carrying the SHA-independent failure fingerprint (secretary-766): a visible
# GitHub `detail` always contains the head SHA, which changes on every rework commit, so repeat
# detection cannot key off the rendered text itself. Stripped back out before the body reaches
# the worker's TASK.md.
_GATE_FINGERPRINT_PREFIX = "<!-- gate-fingerprint: "


def _last_gate_red_body(task: dict[str, Any]) -> str | None:
    """Most recent mechanical-gate-red bounce comment, for delivery to the rework worker."""
    body = None
    for comment in task.get("comments") or []:
        if comment.get("marker") == "dispatcher" and _GATE_RED_PREFIX in (comment.get("body") or ""):
            body = comment.get("body")
    if not body:
        return None
    lines = body.splitlines()
    if lines and lines[0].strip() == "[dispatcher]":
        lines = lines[1:]
    lines = [line for line in lines if not line.startswith(_GATE_FINGERPRINT_PREFIX)]
    return "\n".join(lines).strip() or None


def _gate_red_repeat_count(task: dict[str, Any], fingerprint: str) -> int:
    """How many times this exact gate-failure fingerprint has already bounced this card.

    Keyed on the fingerprint rather than the rendered detail text: a GitHub `detail` always carries
    the head SHA, which changes on every rework commit, so matching on detail text would never
    recognise a repeat.
    """
    if not fingerprint:
        return 0
    needle = f"{_GATE_FINGERPRINT_PREFIX}{fingerprint} -->"
    return sum(
        1
        for comment in task.get("comments") or []
        if comment.get("marker") == "dispatcher" and needle in (comment.get("body") or "")
    )


WORKER_REPORT_MARKERS = {"report:done", "report:blocked"}
# The actions the dispatcher issues a report request id for. One per command in `TASK.md`: the done
# report and one per block classification, since a request id claims its payload.
_REPORT_ACTIONS = (
    "worker-report-done",
    "worker-report-blocked-external_fact",
    "worker-report-blocked-wrong_task_definition",
)


def _round_report_ids(workspace: str, attempt_id: str, reference: str, generation: int) -> set[str]:
    """The report request ids this round issued: the exact commands its worker was handed.

    Every one is unique to the round: the attempt is in there, so a later attempt never inherits an
    earlier one's reports, and so is the generation.

    The checkout answers first, because the document is what the live worker is holding and the
    dispatcher's own attempt id may have moved under it. Only ids that name the open generation are
    taken from it. It answers through the dispatcher's own record line, never by scanning the
    document for report commands: the card description is rendered into the same file, so a
    `--request-id` token in ordinary prose would otherwise be admitted as an id this round issued.
    """
    recorded_generation, recorded_ids = _task_doc_round_record(workspace)
    from_document = {
        request_id
        for request_id in recorded_ids
        if recorded_generation == generation
        and request_id.endswith(f"-{request_token(reference)}-{generation}")
    }
    if from_document:
        return from_document
    return {attempt_request_id(attempt_id, action, reference, str(generation)) for action in _REPORT_ACTIONS}


def _round_report_marker(audit: Any, reference: str, round_ids: set[str]) -> str | None:
    """The marker of a report that belongs to this round, or None if the round has none yet.

    The comment is `[report:done]` whoever wrote it and for whichever round; what names the round is
    the request id its command carried, which the audit keeps beside the marker. A report filed under
    any other id records nothing of this round.

    Committed events only. `TaskWriter` stages its event before it writes the comment, so a staged
    event is a report that may not be on the board at all yet, and consuming one would end the round
    on a call that had not happened.
    """
    marker = None
    for event in audit.events(reference, kind="reported"):
        if str(event.get("request_id") or "") not in round_ids:
            continue
        payload = (
            event.get("data") if event.get("record_type") == "board.protocol_event" else event.get("payload")
        )
        candidate = (payload or {}).get("marker") if isinstance(payload, dict) else None
        if candidate in WORKER_REPORT_MARKERS:
            marker = candidate
    return marker


def _round_blocked_report_classification(audit: Any, reference: str, round_ids: set[str]) -> str | None:
    """The current round's exact worker classification, not prose from another round."""
    classification = None
    for event in audit.events(reference, kind="reported"):
        if str(event.get("request_id") or "") not in round_ids:
            continue
        payload = (
            event.get("data") if event.get("record_type") == "board.protocol_event" else event.get("payload")
        )
        if not isinstance(payload, dict) or payload.get("marker") != "report:blocked":
            continue
        value = payload.get("classification")
        if isinstance(value, str) and value:
            classification = value
    return classification


def _round_done_report_body(audit: Any, reference: str, round_ids: set[str]) -> str | None:
    """The body of this round's done report, from the event its request id names."""
    body = None
    for event in audit.events(reference, kind="reported"):
        if str(event.get("request_id") or "") not in round_ids:
            continue
        payload = (
            event.get("data") if event.get("record_type") == "board.protocol_event" else event.get("payload")
        )
        if not isinstance(payload, dict) or payload.get("marker") != "report:done":
            continue
        value = payload.get("body")
        if isinstance(value, str) and value.strip():
            body = value
    return body


def _round_worker_check(audit: Any, reference: str, round_ids: set[str]) -> dict[str, Any] | None:
    """Immutable admission evidence from this round's accepted report, never its body."""
    evidence = None
    for event in audit.events(reference):
        if str(event.get("request_id") or "") not in round_ids:
            continue
        data = event.get("data") if event.get("record_type") == "board.protocol_event" else event.get("payload")
        if isinstance(data, dict) and data.get("marker") == "report:done":
            value = data.get("worker_check")
            evidence = value if isinstance(value, dict) else None
    return evidence


def _last_marker_body(task: dict[str, Any], marker: str) -> str | None:
    """Text of the most recent comment carrying this marker, with the marker line stripped."""
    body = None
    for comment in task.get("comments") or []:
        if comment.get("marker") == marker:
            body = comment.get("body")
    if not body:
        return None
    lines = body.splitlines()
    if lines and lines[0].strip() == f"[{marker}]":
        lines = lines[1:]
    return "\n".join(lines).strip() or None


def _last_review_red_body(task: dict[str, Any]) -> str | None:
    """Findings from the most recent review:red verdict, for delivery to the rework worker."""
    return _last_marker_body(task, "review:red")


def _review_adoption_baseline(task: dict[str, Any]) -> int:
    baseline = len(task.get("comments") or [])
    for index, comment in enumerate(task.get("comments") or []):
        if comment.get("marker") == "report:done":
            baseline = index + 1
    return baseline


def _task_doc_report_generation(workspace: str) -> int:
    """The report generation the worker in this checkout was actually handed, or 0.

    A dispatcher record can be lost at any point and the generation is dispatcher state; the document
    the live worker is working from is not. Read from the record line and not from the report commands
    rendered beside it, for the reason in `_task_doc_round_record`.
    """
    return _task_doc_round_record(workspace)[0]


def _task_doc_decision(workspace: str) -> str:
    """The observer decision the worker in this checkout was actually handed, or "".

    The same recovery as `_task_doc_report_generation`, from the same file. Re-reading the card's
    newest decision comment instead would be the defect this record exists to close.
    """
    if not workspace:
        return ""
    try:
        document = (Path(workspace) / "TASK.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""
    records = _DECISION_RECORD_RE.findall(document)
    if not records:
        return ""
    try:
        decoded = base64.b64decode(records[-1][1].encode("ascii"), validate=True)
        return decoded.decode("utf-8").strip()
    except (ValueError, UnicodeError):
        return ""


def _task_doc_protocol_prerequisites(workspace: str) -> tuple[str, ...]:
    """Read the dispatcher-written structured prerequisite record from a worker document."""
    if not workspace:
        return ()
    try:
        document = (Path(workspace) / "TASK.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ()
    records = _PROTOCOL_PREREQUISITES_RECORD_RE.findall(document)
    if not records:
        return ()
    try:
        decoded = base64.b64decode(records[-1][1].encode("ascii"), validate=True).decode("utf-8")
    except (ValueError, UnicodeError):
        return ()
    names = tuple(filter(None, decoded.splitlines()))
    return names if len(set(names)) == len(names) else ()


def _spent_report_generations(task: dict[str, Any]) -> int:
    """A floor under the generations this card has already spent, from its report markers.

    Only for recovery, and only as a floor: undershooting would hand a new round an id an earlier one
    already committed. Only consumed reports count — a report still waiting to be read belongs to the
    round that is running, and stepping over it would leave the adopted record holding a generation
    no report on the board names.
    """
    return sum(
        1
        for comment in (task.get("comments") or [])[: _report_adoption_baseline(task)]
        if comment.get("marker") in WORKER_REPORT_MARKERS
    )


def _report_adoption_baseline(task: dict[str, Any]) -> int:
    """Where to start looking for worker report markers on an adopted card.

    `len(comments)` would hide a `report:done` the worker already posted: the dispatcher can lose its
    record at any point, and re-adoption then blinds it to the finished report, burning the whole
    worker ceiling and respawning the head to redo work already on the board.

    Every report the dispatcher has consumed is followed by the dispatcher's own comment, so the last
    dispatcher comment is the boundary: markers after it are unconsumed.
    """
    baseline = 0
    for index, comment in enumerate(task.get("comments") or []):
        if comment.get("marker") == "dispatcher":
            baseline = index + 1
    return baseline


def scrub_host_output(text: str) -> str:
    text = _ASSIGN_RE.sub(r"\1=<redacted>", text)
    return _BLOB_RE.sub(lambda match: match.group(0) if _HEX_RE.match(match.group(0)) else "<redacted>", text)


def safe_one_line(text: object, *, limit: int = 500) -> str:
    """Redact and flatten untrusted host/board text before it enters a role prompt or board artifact.

    GitHub check names, URLs and verdict bodies are remote-controlled text. A receipt is evidence,
    not an instruction channel, so control characters and Markdown-shaped newlines must not be able
    to create a second prompt section or command. Bounded as well: a check title is an identifier.
    """
    flattened = "".join(
        " " if ord(char) < 32 or ord(char) == 127 else char for char in scrub_host_output(str(text))
    )
    return re.sub(r"\s+", " ", flattened).strip()[:limit]


def _tail(text: str, lines: int = 40) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])
