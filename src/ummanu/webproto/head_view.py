"""A card's heads, and a read-only view of one local-pty head's journal.

Read-only like `head-status`: nothing here delivers, attaches, drains, stops or writes.

- A card's runs are only the ones it recorded: the dispatcher record's current worker/reviewer runs
  and every `launch_id` named by its `routing` and `attempt.usage` history events. Any other run id
  is not found, so a path is only built from a recorded run id; a run directory whose journal names
  another card is not read.
- A row joins, by run id, the `routing` launch configuration with what the latest `attempt.usage`
  occurrence says the CLI resolved (:mod:`ummanu.runtime.provider_models`). Neither fills the
  other's gaps.
- The journal is untrusted (its first record holds the head's memory token): only `JOURNAL_KEYS`
  are shown, through `journal_record`.
- No source can fail the read: each is read under `_source`.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ummanu.routing_journal import MODEL_UNKNOWN, RoutingHeadSnapshot
from ummanu.runtime.head import HeadRun, HeadRunError, TaskRefError
from ummanu.runtime.head_runtime_backends import head_runtime_name, is_legacy_record
from ummanu.runtime.head_runtimes import LOCAL_PTY_RUNTIME, ORCA_LEGACY_RUNTIME
from ummanu.runtime.local_pty_head import (
    head_run_directory,
    head_run_first_record,
    head_run_journal_tail,
    head_run_supervisor_lease,
)
from ummanu.runtime.redact import scrub_secrets

#: What a row of a card's heads, or a head view, says about a head on the legacy runtime.
LEGACY_NOTICE = "no local-pty journal (legacy runtime)"
#: Stated on every view, so a reader never has to wonder whether it can act on the head.
READ_ONLY_NOTICE = (
    "read-only: this view reads the head's journal; nothing is typed into the head, "
    "and nothing here drains or stops it"
)

#: The journal keys a view shows, including the fold count on progress records.
JOURNAL_KEYS = ("seq", "kind", "at", "turn", "reason", "bytes", "subject", "output_bytes", "folded_windows")
#: How many of the journal's last records a view shows.
JOURNAL_TAIL_RECORDS = 40

#: A head's state, as its supervisor lock says it: a held lock is a supervisor that owns the run.
RUNNING = "running"
FINISHED = "finished"
UNKNOWN = "unknown"

#: The history events that name a head run, and where in them the run id is.
_ROUTING = "routing"
_USAGE = "attempt.usage"
#: A recorded run id has this shape or it is not used at all, not even to look for a directory.
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_ROLES = (("worker", "worker"), ("review", "reviewer"))


@dataclass(frozen=True)
class RecordedHead:
    """One head run the card recorded, and what the recording says about it."""

    run_id: str
    role: str
    head: str = ""
    #: The runtime the durable run names, or `""` when only the card's history names the run.
    runtime: str = ""
    #: Whether the dispatcher record still holds this run's head identity for the card.
    current: bool = False
    #: The attempt the routing journal launched this run in; `None` when only the record names it.
    attempt: int | None = None
    #: The launch configuration from the routing snapshot: what the head was asked to run on.
    adapter: str = ""
    model: str = ""
    model_source: str = MODEL_UNKNOWN
    effort: str = ""
    #: What the provider journal says this run actually resolved, from its latest `attempt.usage`
    #: occurrence by report generation; empty until a phase of the run finished, and for an
    #: occurrence written before the fields existed.
    resolved_model: str = ""
    resolved_models: tuple[str, ...] = ()
    resolved_effort: str = ""
    resolved_report_generation: int | None = None

    def launched(self, snapshot: RoutingHeadSnapshot, attempt: int | None) -> RecordedHead:
        """This run with its launch configuration, from the routing snapshot that recorded it."""
        return replace(
            self,
            role=snapshot.role or self.role,
            head=snapshot.head or self.head,
            attempt=attempt if attempt is not None else self.attempt,
            adapter=snapshot.adapter,
            model=snapshot.model,
            model_source=snapshot.model_source,
            effort=snapshot.effort,
        )

    def resolved(self, occurrence: dict[str, Any]) -> RecordedHead:
        """This run with what one `attempt.usage` occurrence says it ran, if it is the latest."""
        generation = occurrence.get("report_generation")
        if not isinstance(generation, int) or isinstance(generation, bool):
            return self
        if self.resolved_report_generation is not None and self.resolved_report_generation > generation:
            return self
        models = occurrence.get("resolved_models")
        return replace(
            self,
            role=self.role or _text(occurrence.get("role")),
            head=self.head or _text(occurrence.get("head")),
            attempt=self.attempt if self.attempt is not None else _attempt(occurrence.get("attempt")),
            resolved_model=_text(occurrence.get("resolved_model")),
            resolved_models=tuple(item for item in models if isinstance(item, str))
            if isinstance(models, list)
            else (),
            resolved_effort=_text(occurrence.get("resolved_effort")),
            resolved_report_generation=generation,
        )


def recorded_heads(record: Any, history: Iterable[dict[str, Any]] | None) -> list[RecordedHead]:
    """The card's head runs, oldest first: its history's, then the dispatcher's current ones.

    `record` is the card's `DispatcherRecord` or `None`; `history` is the card's committed records
    as `kind` and `data`. A run both name is one row, carrying the runtime the durable run says.
    """
    found: dict[str, RecordedHead] = {}
    for event in history or ():
        data = event.get("data") if isinstance(event, dict) else None
        if not isinstance(data, dict):
            continue
        kind = event.get("kind")
        if kind == _ROUTING:
            attempt = _attempt(data.get("attempt"))
            heads = data.get("heads")
            for head in heads if isinstance(heads, list) else []:
                if not isinstance(head, dict):
                    continue
                run_id = _run_id(head.get("launch_id"))
                if not run_id:
                    continue
                snapshot = RoutingHeadSnapshot.from_json(head)
                found[run_id] = (
                    found.get(run_id) or RecordedHead(run_id=run_id, role=snapshot.role)
                ).launched(snapshot, attempt)
        elif kind == _USAGE:
            run_id = _run_id(data.get("launch_id"))
            if run_id:
                found[run_id] = (
                    found.get(run_id) or RecordedHead(run_id=run_id, role=_text(data.get("role")))
                ).resolved(data)
    if record is not None:
        for kind, role in _ROLES:
            raw = record.review_head_run if kind == "review" else record.worker_head_run
            run_id = _run_id(raw.get("run_id") if isinstance(raw, dict) else None)
            if not run_id:
                continue
            profile = (record.review_head if kind == "review" else record.head) or ""
            earlier = found.pop(run_id, None) or RecordedHead(run_id=run_id, role=role)
            found[run_id] = replace(
                earlier,
                role=role,
                head=profile or earlier.head,
                runtime=_runtime_of(raw),
                current=record.owns_head(kind),
            )
    return list(found.values())


def _attempt(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def head_rows(ref: str, heads: list[RecordedHead], root: Path) -> list[dict[str, Any]]:
    """One row per recorded head for the card page: role, run id, state, and whether it has a view."""
    return [_row(ref, head, root) for head in heads]


def head_view(
    ref: str, run_id: str, heads: list[RecordedHead], root: Path, *, observed_at: str
) -> dict[str, Any] | None:
    """The read-only view of one of the card's heads, or `None` when the card recorded no such run.

    The run directory is built from the matched recorded value, never from `run_id` itself.
    """
    head = next((head for head in heads if head.run_id == run_id), None)
    if head is None:
        return None
    row = _row(ref, head, root)
    document: dict[str, Any] = {
        "schema_version": 1,
        "kind": "head_view",
        "observed_at": observed_at,
        "ref": ref,
        "run_id": head.run_id,
        "head": row,
        "read_only": READ_ONLY_NOTICE,
    }
    if not row["local_pty"]:
        document["journal"] = {**_not_applicable(row["reason"]), "tail": []}
        return document
    run_dir = head_run_directory(root, head.run_id)
    document["journal"] = _source("journal", lambda: _journal(run_dir), tail=[])
    return document


# -- one head --------------------------------------------------------------------------------


def _row(ref: str, head: RecordedHead, root: Path) -> dict[str, Any]:
    """What the card page says about one recorded head, decided from the run directory it names."""
    row: dict[str, Any] = {
        "ref": ref,
        "role": head.role or "head",
        "run_id": head.run_id,
        "head": head.head or None,
        "current": head.current,
        "attempt": head.attempt,
        "adapter": head.adapter or None,
        "model": head.model or None,
        "model_source": head.model_source,
        "effort": head.effort or None,
        "resolved_model": head.resolved_model or None,
        "resolved_models": list(head.resolved_models),
        "resolved_effort": head.resolved_effort or None,
        "resolved_report_generation": head.resolved_report_generation,
        "runtime": head.runtime or None,
        "local_pty": False,
        "legacy_record": False,
        "state": UNKNOWN,
        "reason": "",
    }
    if head.runtime and head.runtime != LOCAL_PTY_RUNTIME:
        # A legacy Orca record is shown as one and has no local-pty journal.
        return {**row, "legacy_record": is_legacy_record(head.runtime), "reason": LEGACY_NOTICE}
    identity = _source("run directory", lambda: _identity(root, head.run_id), found=False)
    if not identity["answered"]:
        return {**row, "runtime": LOCAL_PTY_RUNTIME, "local_pty": True, "reason": identity["reason"]}
    if not identity["found"]:
        if head.runtime == LOCAL_PTY_RUNTIME:
            return {
                **row,
                "local_pty": True,
                "reason": "the run directory holds no journal yet, so this head has said nothing",
            }
        # Only the card's history names this run and no local-pty journal exists; local-pty runs
        # always leave one (never swept), so this is a legacy run.
        return {**row, "runtime": ORCA_LEGACY_RUNTIME, "legacy_record": True, "reason": LEGACY_NOTICE}
    if identity.get("task") != f"card:{ref}":
        return {
            **row,
            "runtime": LOCAL_PTY_RUNTIME,
            "reason": "the run directory under this run id belongs to another card, so it is not read",
        }
    row.update(runtime=LOCAL_PTY_RUNTIME, local_pty=True)
    lease = _source("supervisor lock", lambda: _lease(root, head.run_id), state=UNKNOWN)
    row.update(state=lease["state"], reason=lease["reason"])
    return row


def _identity(root: Path, run_id: str) -> dict[str, Any]:
    first = head_run_first_record(head_run_directory(root, run_id))
    if first is None:
        return {"answered": True, "found": False}
    return {"answered": True, "found": True, "task": _text(first.get("task"))}


def _lease(root: Path, run_id: str) -> dict[str, Any]:
    lease = head_run_supervisor_lease(head_run_directory(root, run_id))
    if not lease.lock_readable or not lease.table_readable:
        return {
            "answered": False,
            "state": UNKNOWN,
            "reason": f"the supervisor lock could not be read ({lease.error or 'unreadable'})",
        }
    if lease.holders:
        return {"answered": True, "state": RUNNING, "reason": "a supervisor holds this run"}
    return {"answered": True, "state": FINISHED, "reason": "no supervisor holds this run any more"}


def _journal(run_dir: Path) -> dict[str, Any]:
    """The last records of the head's journal, each through `journal_record`, and what was left out."""
    read = head_run_journal_tail(run_dir)
    tail = []
    dropped = 0
    for event in read.events[-JOURNAL_TAIL_RECORDS:]:
        record, lost = journal_record(event)
        tail.append(record)
        dropped += lost
    damage = []
    if read.malformed:
        damage.append(f"{read.malformed} malformed line(s) skipped")
    if read.truncated_tail:
        damage.append("the final line is torn")
    if not read.ordered:
        damage.append("records are out of sequence")
    if dropped:
        damage.append(f"{dropped} field value(s) out of shape were left out")
    return {
        "answered": True,
        "state": "degraded" if damage else "available",
        "reason": ("the journal tail is incomplete: " + ", ".join(damage)) if damage else "",
        "partial": read.partial_head,
        "tail": tail,
    }


#: The latest time `datetime.fromtimestamp(..., UTC)` renders: the start of the year 10000.
_LAST_TIME = 253402300800.0
#: The largest count shown: 2**53, the last integer a browser JSON reader holds exactly.
_LAST_COUNT = 2**53
_COUNTS = ("seq", "turn", "bytes", "output_bytes", "folded_windows")


def journal_record(event: Any) -> tuple[dict[str, Any], int]:
    """One journal record as the view shows it, and how many of its whitelisted values were dropped.

    The one normaliser for every returned record: `at` a float in (0, year 10000), counts ints in
    0..2**53, `kind`/`reason`/`subject` scrubbed bounded strings. Anything else is left out and
    counted, never passed on or raised about.
    """
    record: dict[str, Any] = {}
    lost = 0
    source = event if isinstance(event, dict) else {}
    for key in JOURNAL_KEYS:
        if key not in source:
            continue
        value = source[key]
        if key == "at":
            kept: Any = _time(value)
        elif key in _COUNTS:
            kept = value if _count(value) else None
        else:
            kept = scrub_secrets(value)[:400] if isinstance(value, str) else None
        if kept is None:
            lost += value is not None
            continue
        record[key] = kept
    return record, lost


def _count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= _LAST_COUNT


def _not_applicable(reason: str) -> dict[str, Any]:
    return {"answered": False, "state": "not_applicable", "reason": reason, "source": None, "text": ""}


def _source(name: str, read: Callable[[], dict[str, Any]], **empty: Any) -> dict[str, Any]:
    """One source's answer, or the uniform shape of a source that did not answer; never raises.

    Mirrors `ummanu.dispatch.head_status._source`: supervisors, lock files and journals are
    untrusted, so anything their read raises is that source not answering.
    """
    try:
        return read()
    except Exception as exc:  # noqa: BLE001 - every head source is untrusted input
        return {
            **empty,
            "answered": False,
            "state": "unavailable",
            "reason": f"the {name} could not be read ({type(exc).__name__}: {scrub_secrets(str(exc))[:200]})",
        }


# -- small readers ----------------------------------------------------------------------------


def _runtime_of(raw: Any) -> str:
    """The runtime a durable run names, or `""` when the record holds no readable run."""
    if not isinstance(raw, dict) or not raw.get("run_id"):
        return ""
    try:
        return head_runtime_name(HeadRun.from_json(raw))
    except (HeadRunError, TaskRefError, TypeError, ValueError):
        return ""


def _run_id(value: Any) -> str:
    text = value if isinstance(value, str) else ""
    return text if _RUN_ID.fullmatch(text) else ""


def _time(value: Any) -> float | None:
    """A time the page can render: a float with 0 < at < year 10000, or `None` for anything else."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        seconds = float(value)
    except (OverflowError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and 0 < seconds < _LAST_TIME else None


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""
