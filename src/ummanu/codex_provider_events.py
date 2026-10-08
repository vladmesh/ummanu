"""Best-effort, launch-bound ingestion of Codex's structured session event journal.

This is intentionally separate from the inexpensive workspace activity lookup used by the
watchdog.  A source is accepted only after it is bound to the exact ``HeadRun`` and its cursor is
durably written.  Every later read re-proves that binding before consuming a new line.  Missing,
malformed, ambiguous, or unwritable fan-out telemetry never controls launch, delivery, stop,
replacement, board state, or continuation liveness.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ummanu.runtime.codex_preflight import (
    EVENT_CHILD_THREAD_EDGE,
    EVENT_COLLABORATION_CALL,
    EVENT_UNKNOWN_THREAD_EDGE,
    EVENT_UNPARSEABLE_PROVIDER_EVENT,
    FANOUT_TERMINAL_UNKNOWN,
    CodexFanoutPolicyError,
    CodexProviderEventRecorder,
    codex_provider_source_descriptor,
    enforce_provider_event,
)
from ummanu.runtime.head import HeadRun

SOURCE_VERSION = 1
SOURCE_KIND = "codex_session_event_jsonl"


class CodexProviderSourceError(CodexFanoutPolicyError):
    """The run cannot safely attribute a structured provider-event source to itself."""


@dataclass(frozen=True)
class SourceLine:
    """One physical provider event line, including the durable cursor that names it."""

    number: int
    raw: str
    digest: str
    event: Any


class CodexProviderEventIngress:
    """One best-effort Codex event source and its exact ``HeadRun``.

    ``persist`` is the same lifecycle writer used for the role's HeadRun.  ``stop`` and ``block``
    are retained callback parameters for the installed launch-owner shape, but this observer has
    no lifecycle authority: source and event diagnostics never invoke them.
    """

    def __init__(
        self,
        run: HeadRun,
        persist: Callable[[HeadRun], None],
        *,
        stop: Callable[[HeadRun, str], None],
        block: Callable[[dict[str, Any]], None],
    ) -> None:
        self.run = run
        self.persist = persist
        self.stop = stop
        self.block = block

    @property
    def source(self) -> dict[str, Any]:
        value = self.run.fanout_policy.get("provider_source")
        return dict(value) if isinstance(value, dict) else {}

    def commit_run(self, run: HeadRun) -> None:
        """Persist the handle/leaf rebinding before source binding or prompt delivery."""
        if not self.run.same_run(run):
            raise CodexProviderSourceError("Codex provider ingress was handed another HeadRun", run=self.run)
        self.persist(run)
        self.run = run

    def bind_before_delivery(self) -> HeadRun:
        """Bind and scan a Codex session before any prompt is sent, returning its handoff run.

        Binding is not a permission to skip what the provider wrote while the pane was coming
        up.  The durable parent cursor is the only point at which a source belongs to this run;
        once it is committed, ``poll`` is the one classifier for the complete initially observed
        source and every later lifecycle poll.  Source selection and event classification are
        advisory telemetry, so an unavailable or ambiguous recorder cannot prevent delivery.
        """
        self.poll()
        return self.run

    def _bind_new_session(self, source: dict[str, Any]) -> bool:
        """Select the one new journal this run created, or leave the source unbound.

        Selection is fail-closed on every axis a journal can be claimed by: the immutable
        preflight descriptor, the pre-pane baseline, the exact workspace the session names, and
        the requirement that exactly one candidate remains.  A pane which has not written its
        session yet therefore leaves the source unbound for a later attempt rather than
        borrowing a journal from another head.
        """
        if not _source_descriptor_matches_run(source, self.run):
            self._unknown("Codex provider source descriptor does not match this HeadRun")
            return False
        root = Path(str(source.get("root") or ""))
        if not root.is_dir():
            self._unknown("Codex provider session source root is unavailable")
            return False
        baseline = source.get("baseline")
        if not isinstance(baseline, list) or not all(isinstance(path, str) for path in baseline):
            self._unknown("Codex provider source baseline is malformed")
            return False
        candidates: list[tuple[Path, dict[str, Any], list[SourceLine]]] = []
        try:
            paths = list(root.rglob("*.jsonl"))
            root_resolved = root.resolve(strict=True)
        except OSError:
            self._unknown("Codex provider session source cannot be enumerated")
            return False
        baseline_paths = set(baseline)
        for path in paths:
            try:
                resolved_path = path.resolve(strict=True)
                if not resolved_path.is_relative_to(root_resolved):
                    continue
                resolved = str(resolved_path)
            except (OSError, ValueError):
                continue
            if resolved in baseline_paths:
                continue
            parsed = _read_source(path)
            if parsed is None:
                continue
            meta, lines = parsed
            if str(meta.get("cwd") or "") != str(Path(self.run.workspace).resolve(strict=False)):
                continue
            session_id = str(meta.get("session_id") or "")
            parent = _parent_thread(lines, session_id=session_id)
            if not session_id or not parent:
                continue
            candidates.append((path, {"session_id": session_id, "parent_thread_id": parent}, lines))
        if len(candidates) != 1:
            self._unknown(
                "Codex provider source is unbound: expected exactly one new session with a parent thread"
            )
            return False
        path, identity, lines = candidates[0]
        parent_line = next(
            line
            for line in lines
            if _is_parent_anchor(
                line.event,
                identity["parent_thread_id"],
                identity["session_id"],
            )
        )
        first_line = lines[0]
        # The preflight descriptor is immutable.  Binding adds the facts the selected journal can
        # prove, but must not replace its run fence with a journal-only record: the retained
        # continuation reader needs the same exact HeadRun facts on every later poll.
        bound = {
            **source,
            "state": "bound",
            "path": str(path.resolve(strict=False)),
            "session_id": identity["session_id"],
            "parent_thread_id": identity["parent_thread_id"],
            # This is not a clean root cursor.  It anchors the exact range selected by the
            # session/root identity before the shared scanner has classified even its preamble.
            # A crash after this persistence resumes at cursor zero and scans the same first raw
            # record, while any later recovery can prove that neither range endpoint changed.
            "initial_range": {
                "first": {"line": first_line.number, "digest": first_line.digest},
                "root": {"line": parent_line.number, "digest": parent_line.digest},
                "last": {"line": lines[-1].number, "digest": lines[-1].digest},
                "digest": _range_digest(lines),
            },
            "cursor": {"line": 0, "digest": first_line.digest},
            "bound_at": _now(),
        }
        self._replace_source(bound)
        # A binding which could not be written down never happened: the caller keeps the unbound
        # source it already had rather than consuming events under an in-memory-only identity.
        return str(self.source.get("state") or "") == "bound"

    def poll(self) -> None:
        """Bind a still-unbound source, then consume every new provider event.

        Binding is the first step of a poll rather than a launch-only act.  A pane whose journal
        appeared after the launch attempt is picked up by the next lifecycle poll, which is what
        keeps a source from staying unbound for the whole life of a head that is working
        normally.  Selection by session/root identity does not exempt the source preamble: the
        shared scanner starts at the first raw line of a freshly bound journal, crosses the root
        and consumes the pre-existing tail before the caller can type a prompt.
        """
        source = self.source
        if str(source.get("state") or "") == "unbound" and not self._bind_new_session(source):
            # ``_bind_new_session`` has already recorded why this attempt could not select a
            # journal.  Do not overwrite that typed reason with the generic recovery diagnostic.
            return
        source = self.source
        if str(source.get("state") or "") != "bound":
            self._unknown("Codex provider source is not bound during lifecycle recovery")
            return
        parsed = self._verify_binding(source)
        if parsed is None:
            return
        _meta, lines = parsed
        cursor = source.get("cursor")
        if not isinstance(cursor, dict):
            self._unknown("Codex provider source cursor is missing")
            return
        cursor_line = cursor.get("line")
        cursor_digest = str(cursor.get("digest") or "")
        if not isinstance(cursor_line, int) or cursor_line < 0 or not cursor_digest:
            self._unknown("Codex provider source cursor is malformed")
            return
        if cursor_line == 0:
            # ``_verify_binding`` has already proved this digest belongs to the first selected
            # source record.  Line zero is deliberately a scanner sentinel, not a journal line.
            fresh = lines
        else:
            prior = next((line for line in lines if line.number == cursor_line), None)
            if prior is None or prior.digest != cursor_digest:
                self._unknown("Codex provider source cursor no longer matches the bound event journal")
                return
            fresh = [line for line in lines if line.number > cursor_line]
        # An ordinary line only moves the advisory cursor. It is kept in memory and written once at
        # the end of the scan, or carried by the next event's durable recorder write: one durable
        # write per scan instead of one per rollout line. A crash before that write rescans these
        # lines, which produce no events and therefore no duplicate effects.
        unsaved: SourceLine | None = None
        for line in fresh:
            events = list(
                _provider_events(
                    line.event,
                    str(source.get("parent_thread_id") or ""),
                    raw_event_digest=line.digest,
                )
            )
            if not events:
                unsaved = line
                continue
            unsaved = None
            for raw_event in events:
                durable_run = self.run
                run = self._run_at_cursor(source, line)
                recorder = CodexProviderEventRecorder(
                    run,
                    self._persist,
                    expected_parent_thread_id=str(source.get("parent_thread_id") or ""),
                )
                outcome = enforce_provider_event(
                    recorder,
                    raw_event,
                    source_sequence=line.number,
                    source_location=f"{source.get('path')}:{line.number}",
                    stop=self.stop,
                    block=self.block,
                )
                # The event recorder is the only writer for this line.  If it cannot save, the
                # cursor-bearing candidate was not durable either, so retain the preceding run
                # rather than leaking an in-memory telemetry state into the delivery handoff.
                self.run = outcome.run if outcome.event else durable_run
            # A source line which resulted in events was persisted by the recorder at its cursor
            # when possible.  A failed telemetry write leaves the preceding durable run in force.
        if unsaved is not None:
            self._advance_cursor(source, unsaved)

    def _verify_binding(self, source: Mapping[str, Any]) -> tuple[dict[str, Any], list[SourceLine]] | None:
        if not _source_descriptor_matches_run(source, self.run):
            self._unknown("Codex provider source descriptor does not match this HeadRun")
            return None
        if (
            source.get("version") != SOURCE_VERSION
            or source.get("kind") != SOURCE_KIND
            or not str(source.get("path") or "")
            or not str(source.get("session_id") or "")
            or not str(source.get("parent_thread_id") or "")
        ):
            self._unknown("Codex provider source identity is malformed")
            return None
        root = Path(str(source.get("root") or ""))
        path = Path(str(source.get("path") or ""))
        try:
            root_resolved = root.resolve(strict=True)
            path_resolved = path.resolve(strict=True)
            if not path_resolved.is_relative_to(root_resolved) or path_resolved.suffix != ".jsonl":
                raise OSError("source path is outside the bound Codex session root")
        except (OSError, ValueError):
            self._unknown("Codex provider source path is unreadable or outside its bound root")
            return None
        parsed = _read_source(path_resolved)
        if parsed is None:
            self._unknown("Codex provider source cannot be read as structured JSONL")
            return None
        meta, lines = parsed
        if (
            str(meta.get("session_id") or "") != str(source.get("session_id") or "")
            or str(meta.get("cwd") or "") != str(Path(self.run.workspace).resolve(strict=False))
            or _parent_thread(lines, session_id=str(meta.get("session_id") or ""))
            != str(source.get("parent_thread_id") or "")
        ):
            self._unknown("Codex provider source identity no longer matches this HeadRun")
            return None
        if not _initial_range_matches(source, lines):
            self._unknown("Codex provider source initial range no longer matches this HeadRun")
            return None
        return meta, lines

    def _advance_cursor(self, source: Mapping[str, Any], line: SourceLine) -> None:
        self._replace_source({**dict(source), "cursor": {"line": line.number, "digest": line.digest}})

    def _run_at_cursor(self, source: Mapping[str, Any], line: SourceLine) -> HeadRun:
        policy = dict(self.run.fanout_policy)
        policy["provider_source"] = {
            **dict(source),
            "cursor": {"line": line.number, "digest": line.digest},
        }
        return self.run.with_fanout_policy(policy)

    def _replace_source(self, source: dict[str, Any]) -> None:
        policy = dict(self.run.fanout_policy)
        policy["provider_source"] = source
        updated = self.run.with_fanout_policy(policy)
        try:
            self.persist(updated)
        except Exception:
            # Cursor telemetry is best effort.  Do not manufacture a non-durable source state or
            # turn a writer failure into a signal, block, replacement, or liveness input.
            return
        self.run = updated

    def _persist(self, run: HeadRun) -> None:
        self.persist(run)

    def _unknown(self, reason: str) -> None:
        policy = dict(self.run.fanout_policy)
        policy["state"] = "unknown"
        policy["terminal_state"] = FANOUT_TERMINAL_UNKNOWN
        policy["reason"] = reason
        updated = self.run.with_fanout_policy(policy)
        try:
            self.persist(updated)
        except Exception:
            # The prior durable run stays authoritative when diagnostic telemetry cannot be
            # written.  In particular, do not invoke lifecycle callbacks or raise into delivery.
            return
        self.run = updated


def _source_descriptor_matches_run(source: Mapping[str, Any], run: HeadRun) -> bool:
    """Whether the persisted source retained the immutable descriptor preflight wrote."""
    expected = codex_provider_source_descriptor(run)
    return all(source.get(name) == value for name, value in expected.items())


def _read_source(path: Path) -> tuple[dict[str, Any], list[SourceLine]] | None:
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    lines: list[SourceLine] = []
    meta: dict[str, Any] = {}
    for number, raw in enumerate(raw_lines, 1):
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        try:
            event: Any = json.loads(raw)
        except json.JSONDecodeError:
            event = _malformed(raw)
        lines.append(SourceLine(number=number, raw=raw, digest=digest, event=event))
        if not isinstance(event, dict) or event.get("type") != "session_meta" or meta:
            continue
        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        session_id = payload.get("session_id") or payload.get("id") or event.get("session_id")
        cwd = payload.get("cwd") or event.get("cwd")
        if isinstance(session_id, str) and isinstance(cwd, str):
            meta = {"session_id": session_id, "cwd": cwd}
    return meta, lines


def _parent_thread(lines: Iterable[SourceLine], *, session_id: str = "") -> str:
    parents = [
        str(_event_view(line.event).get("thread_id") or "")
        for line in lines
        if _is_parent_started(_event_view(line.event))
    ]
    parents = [parent for parent in parents if parent]
    # The first ``thread.started`` is the parent established at source binding.  Later starts
    # are not allowed to rewrite that identity: ``_provider_events`` records them as unknown
    # relations during polling instead of making recovery attribute a child as the parent.
    # Codex 0.147's interactive journal identifies the root thread in ``session_meta`` and then
    # emits ``task_started``/``task_complete`` without a separate ``thread.started`` record.  The
    # session id is the provider thread id in that format. Prefer the explicit event when present,
    # otherwise retain the exact selected journal's immutable session identity as the root.
    return parents[0] if parents else session_id


def _is_parent_anchor(event: Any, parent_thread_id: str, session_id: str) -> bool:
    """Whether one physical line proves the selected journal's root identity."""
    view = _event_view(event)
    if _is_parent_started(view):
        return str(view.get("thread_id") or "") == parent_thread_id
    if not isinstance(event, Mapping) or event.get("type") != "session_meta":
        return False
    payload = event.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    recorded = payload.get("session_id") or payload.get("id") or event.get("session_id")
    return bool(session_id and parent_thread_id == session_id and recorded == session_id)


def _initial_range_matches(source: Mapping[str, Any], lines: list[SourceLine]) -> bool:
    """Verify the initial selected-source span persisted before its first scan.

    The session and root identify which journal may belong to this HeadRun.  They do not make
    earlier physical records irrelevant.  Keeping both anchors durable lets a restarted scanner
    either resume its persisted cursor or fence the run when that exact initial range changed.
    """
    initial = source.get("initial_range")
    if not isinstance(initial, Mapping) or not lines:
        return False
    first = initial.get("first")
    root = initial.get("root")
    last = initial.get("last")
    expected_digest = str(initial.get("digest") or "")
    if not isinstance(first, Mapping) or not isinstance(root, Mapping) or not isinstance(last, Mapping):
        return False
    first_line = lines[0]
    if first.get("line") != first_line.number or str(first.get("digest") or "") != first_line.digest:
        return False
    root_line = root.get("line")
    root_digest = str(root.get("digest") or "")
    if not isinstance(root_line, int) or root_line < first_line.number or not root_digest:
        return False
    root_record = next((line for line in lines if line.number == root_line), None)
    if root_record is None or root_record.digest != root_digest:
        return False
    if not _is_parent_anchor(
        root_record.event,
        str(source.get("parent_thread_id") or ""),
        str(source.get("session_id") or ""),
    ):
        return False
    last_line = last.get("line")
    if not isinstance(last_line, int) or last_line < root_line or not expected_digest:
        return False
    initial_lines = [line for line in lines if line.number <= last_line]
    return (
        len(initial_lines) == last_line
        and bool(initial_lines)
        and initial_lines[-1].digest == str(last.get("digest") or "")
        and _range_digest(initial_lines) == expected_digest
    )


def _range_digest(lines: Iterable[SourceLine]) -> str:
    """Digest an ordered journal range without persisting provider event content."""
    digest = hashlib.sha256()
    for line in lines:
        digest.update(str(line.number).encode("ascii"))
        digest.update(b":")
        digest.update(line.digest.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _provider_events(event: Any, expected_parent: str, *, raw_event_digest: str) -> Iterable[dict[str, Any]]:
    """Map one journal line to policy events without retaining its raw content.

    ``raw_event_digest`` is the SHA-256 of the exact JSONL line, rather than a digest of a
    normalized reconstruction.  The recorder accepts it as an internal transport field and
    persists only the digest in the terminal event record.
    """
    source = {"_ummanu_raw_event_digest": raw_event_digest}
    if isinstance(event, dict) and event.get("_ummanu_malformed_provider_event"):
        yield {
            "type": EVENT_UNPARSEABLE_PROVIDER_EVENT,
            "parent_thread_id": expected_parent,
            "provider_event": event,
            **source,
        }
        return
    if not isinstance(event, dict):
        yield {
            "type": EVENT_UNPARSEABLE_PROVIDER_EVENT,
            "parent_thread_id": expected_parent,
            "provider_event": event,
            **source,
        }
        return
    view = _event_view(event)
    if _is_parent_started(view):
        thread_id = str(view.get("thread_id") or "")
        if thread_id != expected_parent:
            yield {
                "type": EVENT_UNKNOWN_THREAD_EDGE,
                "parent_thread_id": thread_id,
                "provider_event": event,
                **source,
            }
        return
    item = view.get("item")
    if not isinstance(item, Mapping):
        return
    item_type = str(item.get("type") or "")
    # The retained Codex TUI journal uses ``CollabAgentToolCall`` inside
    # ``event_msg.payload.item``.  Keep the documented exec spelling too.  These are decoder
    # shapes only, never an allow-list for collaboration capability.
    if item_type not in {"collab_tool_call", "CollabAgentToolCall"}:
        if _collaboration_shaped(item_type):
            yield {
                "type": EVENT_UNKNOWN_THREAD_EDGE,
                "parent_thread_id": str(item.get("sender_thread_id") or view.get("thread_id") or ""),
                "tool": str(item.get("tool") or ""),
                "provider_event": event,
                **source,
            }
        return
    parent = str(item.get("sender_thread_id") or view.get("thread_id") or "")
    tool = str(item.get("tool") or "")
    receivers = item.get("receiver_thread_ids")
    if receivers is not None and not isinstance(receivers, list):
        yield {
            "type": EVENT_UNKNOWN_THREAD_EDGE,
            "parent_thread_id": parent,
            "tool": tool,
            "provider_event": event,
            **source,
        }
        return
    children = [str(child) for child in receivers if str(child)] if isinstance(receivers, list) else []
    if children:
        for child in children:
            yield {
                "type": EVENT_CHILD_THREAD_EDGE,
                "parent_thread_id": parent,
                "child_thread_id": child,
                "tool": tool,
                "provider_event": event,
                **source,
            }
        return
    yield {
        "type": EVENT_COLLABORATION_CALL,
        "parent_thread_id": parent,
        "tool": tool,
        "provider_event": event,
        **source,
    }


def _malformed(raw: str) -> dict[str, Any]:
    # Retain no event content.  The recorder records a SHA-256 digest of this fixed typed shape.
    return {"_ummanu_malformed_provider_event": True, "length": len(raw)}


def _event_view(event: Any) -> Mapping[str, Any]:
    """Return the provider event inside Codex's persisted JSONL envelope.

    Codex journals each runtime event as an ``event_msg`` wrapper whose payload is the provider
    event.  Direct event objects remain accepted for the documented JSON event stream and the
    focused fixtures, but policy evidence always retains the digest of the original journal line.
    """
    if not isinstance(event, Mapping):
        return {}
    payload = event.get("payload")
    if event.get("type") == "event_msg" and isinstance(payload, Mapping):
        return payload
    return event


def _is_parent_started(event: Mapping[str, Any]) -> bool:
    return str(event.get("type") or "") in {"thread.started", "thread_started"}


def _collaboration_shaped(item_type: str) -> bool:
    """Whether an unrecognised item must be fenced rather than treated as ordinary output."""
    return "collab" in item_type.lower() or "agenttoolcall" in item_type.lower()


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
