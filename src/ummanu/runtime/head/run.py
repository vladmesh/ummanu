"""`HeadRun`: one running head, from spawn to the initiator that ended it.

- Identity is `run_id`, never the pane handle; `rebound` moves a renamed handle onto the same run.
- Lifecycle moves one way: `spawned` -> `working` (task given) -> `finishing` (stop asked) ->
  `exited` (stop confirmed). It is history, not a busy flag; `HeadRuntime` owns the turn lease and
  activity epoch.
- `finishing` requires an initiator, recorded before the stop; the first one is kept on retries.
- JSON round-trips, because the process that stops a head may not be the one that spawned it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from typing import Any, cast

from ..head_runtimes import RECORD_RUNTIME_WHEN_ABSENT
from .spec import DEFAULT_EFFORT, HeadSpec
from .task_ref import TaskRef

# A head that has a pane and has not been given its task yet.
SPAWNED = "spawned"
# Delivery is historical; current activity comes from the runtime lease and epoch.
WORKING = "working"
# A head somebody has asked to stop; its initiator is on the run from this state on.
FINISHING = "finishing"
# A head whose stop was confirmed. Terminal.
EXITED = "exited"
LIFECYCLE = (SPAWNED, WORKING, FINISHING, EXITED)

# Preflight admits evidence; malformed or historic state is never read as allowed.
FANOUT_POLICY_VERSION = 1
FANOUT_ALLOWED = "allowed"
FANOUT_UNKNOWN = "unknown"
FANOUT_VIOLATION = "violation"
# Absent and unknown schema evidence stay distinct and non-clean.
FANOUT_SCHEMA_ABSENT = "schema_absent"
FANOUT_SCHEMA_UNKNOWN = "schema_unknown"
FANOUT_POLICY_STATES = (
    FANOUT_ALLOWED,
    FANOUT_UNKNOWN,
    FANOUT_VIOLATION,
    FANOUT_SCHEMA_ABSENT,
    FANOUT_SCHEMA_UNKNOWN,
)


class HeadRunError(RuntimeError):
    """A transition or a record that would leave a run saying something untrue about its head."""


@dataclass(frozen=True)
class StopInitiator:
    """Who ended a head, and why; `stop(run, initiator)` takes it positionally, so no stop is anonymous."""

    actor: str
    reason: str = ""

    def __post_init__(self) -> None:
        if not str(self.actor).strip():
            raise HeadRunError("a stop names who initiated it")

    def to_json(self) -> dict[str, Any]:
        return {"actor": self.actor, "reason": self.reason}

    @classmethod
    def from_json(cls, payload: Any) -> StopInitiator | None:
        if not isinstance(payload, dict):
            return None
        actor = str(payload.get("actor") or "")
        if not actor:
            return None
        return cls(actor=actor, reason=str(payload.get("reason") or ""))


@dataclass(frozen=True)
class HeadRun:
    """One started head, as everything after the start sees it.

    Frozen; transitions return new values. Equality is structural (two reads of one record are
    equal); `same_run` asks whether two values are the same head.
    """

    run_id: str
    spec: HeadSpec
    workspace: str
    task_ref: TaskRef
    # Role is attested, never inferred; legacy emptiness cannot allow.
    role: str = ""
    handle: str = ""
    leaf: str = ""
    pid_file: str = ""
    lifecycle: str = SPAWNED
    stopped_by: StopInitiator | None = None
    # Round-trip policy state so malformed or historic values remain unknown.
    fanout_policy: dict[str, Any] = field(default_factory=dict)
    # Scoped incarnations can reuse a run directory; stop must name the one it owns.
    scope_generation: str = ""

    def __post_init__(self) -> None:
        if not self.run_id:
            raise HeadRunError("a head run has an identity of its own")
        if not isinstance(self.scope_generation, str):
            raise HeadRunError("a scope generation is a string")
        if self.lifecycle not in LIFECYCLE:
            raise HeadRunError(
                f"a head run's lifecycle is one of {', '.join(LIFECYCLE)}, not {self.lifecycle!r}"
            )
        if self.lifecycle in (FINISHING, EXITED) and self.stopped_by is None:
            raise HeadRunError(f"a head run in {self.lifecycle} carries the initiator that ended it")
        # Canonicalize first serialization so read-back is not a lifecycle change.
        policy = _fanout_policy_json(self.fanout_policy)
        if policy.get("state") == FANOUT_ALLOWED and (
            policy.get("run_id") != self.run_id
            or policy.get("role") != self.role
            or policy.get("model") != (self.spec.model or "")
        ):
            policy = _unknown_fanout_policy("fan-out policy binding does not match this HeadRun")
        object.__setattr__(self, "fanout_policy", policy)

    @property
    def running(self) -> bool:
        """Whether this run still expects a process behind it."""
        return self.lifecycle in (SPAWNED, WORKING)

    @property
    def settled(self) -> bool:
        """Whether this head's end was confirmed. Not `not running`: `finishing` is neither, and still
        owes a stop with its identity and initiator."""
        return self.lifecycle == EXITED

    def same_run(self, other: HeadRun) -> bool:
        """Whether two values name the same head, whatever pane handle each of them is holding."""
        return self.run_id == other.run_id

    @property
    def fanout_policy_state(self) -> str:
        """The terminal provider policy state, conservatively normalised on every read."""
        return str(_fanout_policy_json(self.fanout_policy).get("terminal_state") or FANOUT_UNKNOWN)

    @property
    def fanout_clean(self) -> bool:
        """Whether this exact run has independently-attested, still-clean provider evidence."""
        policy = _fanout_policy_json(self.fanout_policy)
        return (
            policy.get("state") == FANOUT_ALLOWED
            and policy.get("terminal_state") == "clean"
            and policy.get("run_id") == self.run_id
            and policy.get("role") == self.role
            and policy.get("model") == (self.spec.model or "")
        )

    def with_fanout_policy(self, policy: Any) -> HeadRun:
        """Return this run with a conservatively serialisable policy attestation."""
        return replace(self, fanout_policy=_fanout_policy_json(policy))

    def rebound(self, handle: str, *, leaf: str = "") -> HeadRun:
        """The same run at its current pane handle; `run_id` and lifecycle are unchanged."""
        return replace(self, handle=handle, leaf=leaf or self.leaf)

    def working(self) -> HeadRun:
        """Mark that this head was given its task: a fact about the past, not a busy flag.

        Whether a turn is running is the runtime's lease and activity epoch.
        """
        if self.lifecycle in (FINISHING, EXITED):
            raise HeadRunError(f"a head in {self.lifecycle} is not given more work")
        return replace(self, lifecycle=WORKING)

    def finishing(self, initiator: StopInitiator) -> HeadRun:
        """Record who asked this head to stop, before the stop is attempted.

        Idempotent: the first initiator is kept, so the record names the decision, not the last retry.
        """
        if not isinstance(initiator, StopInitiator):
            raise HeadRunError("a stop initiator is a StopInitiator")
        if self.lifecycle in (FINISHING, EXITED):
            return self
        return replace(self, lifecycle=FINISHING, stopped_by=initiator)

    def exited(self) -> HeadRun:
        """The stop was confirmed. Only reachable from `finishing`, which carries the initiator."""
        if self.lifecycle != FINISHING:
            raise HeadRunError(f"a head exits from {FINISHING}, and this run is in {self.lifecycle}")
        return replace(self, lifecycle=EXITED)

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "spec": _spec_json(self.spec),
            # Backend is a launch fact, outside the hashed provider-session identity.
            "head_runtime": self.spec.runtime,
            "workspace": self.workspace,
            "task_ref": self.task_ref.to_json(),
            "role": self.role,
            "handle": self.handle,
            "leaf": self.leaf,
            "pid_file": self.pid_file,
            "lifecycle": self.lifecycle,
            "stopped_by": self.stopped_by.to_json() if self.stopped_by else {},
            "fanout_policy": _fanout_policy_json(self.fanout_policy),
            **({"scope_generation": self.scope_generation} if self.scope_generation else {}),
        }

    @classmethod
    def from_json(cls, payload: Any) -> HeadRun:
        if not isinstance(payload, dict):
            raise HeadRunError("a head run is read from an object, and this is not one")
        return cls(
            run_id=str(payload.get("run_id") or ""),
            spec=_spec_from_json(payload.get("spec"), str(payload.get("head_runtime") or "")),
            workspace=str(payload.get("workspace") or ""),
            task_ref=TaskRef.from_json(payload.get("task_ref")),
            role=str(payload.get("role") or ""),
            handle=str(payload.get("handle") or ""),
            leaf=str(payload.get("leaf") or ""),
            pid_file=str(payload.get("pid_file") or ""),
            lifecycle=str(payload.get("lifecycle") or SPAWNED),
            stopped_by=StopInitiator.from_json(payload.get("stopped_by")),
            fanout_policy=_fanout_policy_json(payload.get("fanout_policy")),
            scope_generation=payload.get("scope_generation", ""),
        )


def new_run_id() -> str:
    """An identity for one head run, unrelated to anything a session manager can rename."""
    return uuid.uuid4().hex


def _spec_json(spec: HeadSpec) -> dict[str, Any]:
    """The launch shape the head started with, written with the run (the registry may change).

    Codex provider identity uses the fixed projection in `runtime.head_run_binding`, which must not
    grow with this shape. `HeadRun.to_json` writes runtime beside this block for legacy compatibility.
    """
    result: dict[str, Any] = {
        "profile_id": spec.profile_id,
        "adapter": spec.adapter,
        "model": spec.model or "",
        "effort": spec.effort,
        "resource": spec.resource or "",
        "codex_mode": spec.codex_mode or "",
        "fallback": list(spec.fallback),
    }
    if spec.memory_limit_mib is not None:
        result["memory_limit_mib"] = spec.memory_limit_mib
    return result


def _spec_from_json(payload: Any, runtime: str = "") -> HeadSpec:
    """The recorded launch shape, with the backend recorded beside it handed in.

    An absent `runtime` predates the choice of backend and means `RECORD_RUNTIME_WHEN_ABSENT`, not
    today's profile default: a record keeps what it meant when written.
    """
    if not isinstance(payload, dict):
        raise HeadRunError("a head run carries the spec it was launched from")
    profile_id = str(payload.get("profile_id") or "")
    adapter = str(payload.get("adapter") or "")
    if not profile_id or not adapter:
        # Never infer a missing adapter from a damaged record.
        raise HeadRunError("a recorded head run names its profile and its adapter")
    fallback = payload.get("fallback")
    raw_memory_limit = payload.get("memory_limit_mib")
    if raw_memory_limit is not None:
        from .memory import memory_limit_mib

        try:
            raw_memory_limit = memory_limit_mib(raw_memory_limit, profile_id)
        except ValueError as exc:
            raise HeadRunError(str(exc)) from None
    return HeadSpec(
        profile_id=profile_id,
        adapter=adapter,
        model=str(payload.get("model") or "") or None,
        effort=str(payload.get("effort") or DEFAULT_EFFORT),
        resource=str(payload.get("resource") or "") or None,
        codex_mode=str(payload.get("codex_mode") or "") or None,
        fallback=tuple(str(entry) for entry in fallback) if isinstance(fallback, list) else (),
        runtime=runtime or RECORD_RUNTIME_WHEN_ABSENT,
        memory_limit_mib=raw_memory_limit,
    )


def _fanout_policy_json(payload: Any) -> dict[str, Any]:
    """Return one safe policy shape, never upgrading unknown history into an allow.

    Evidence belongs to the launched run; recovery cannot manufacture it from the registry or a screen.
    """
    if not isinstance(payload, dict):
        return _unknown_fanout_policy("fan-out policy attestation is missing")
    version = payload.get("version")
    if version != FANOUT_POLICY_VERSION:
        return _unknown_fanout_policy("fan-out policy attestation has an unsupported version")
    state = str(payload.get("state") or "")
    terminal_state = str(payload.get("terminal_state") or "")
    if state not in FANOUT_POLICY_STATES or terminal_state not in ("clean", FANOUT_UNKNOWN, FANOUT_VIOLATION):
        return _unknown_fanout_policy("fan-out policy attestation is malformed")
    result = dict(payload)
    result["version"] = FANOUT_POLICY_VERSION
    result["state"] = state
    result["terminal_state"] = terminal_state
    events = result.get("events")
    if not isinstance(events, list) or not all(isinstance(event, dict) for event in events):
        return _unknown_fanout_policy("fan-out policy event log is malformed")
    result["events"] = [dict(event) for event in events]
    for event in result["events"]:
        if (
            str(event.get("type") or "")
            not in {
                "collaboration_call",
                "child_thread_edge",
                "unknown_thread_edge",
                "unparseable_provider_event",
            }
            or not str(event.get("raw_event_digest") or "")
            or event.get("source_sequence") is None
            or not str(event.get("source_location") or "")
            or not str(event.get("captured_at") or "")
        ):
            return _unknown_fanout_policy("fan-out policy event log is malformed")
    source_required = result.get("provider_source_required") is True
    source = result.get("provider_source")
    if source_required and source is None:
        return _unknown_fanout_policy(
            "fan-out provider source binding is missing", provider_source_required=True
        )
    if source is not None:
        if not isinstance(source, dict):
            return _unknown_fanout_policy(
                "fan-out provider source binding is malformed", provider_source_required=True
            )
        source_version = source.get("version")
        source_state = str(source.get("state") or "")
        if source_version != 1 or source.get("kind") != "codex_session_event_jsonl":
            return _unknown_fanout_policy(
                "fan-out provider source binding has an unsupported version", provider_source_required=True
            )
        if source_state == "unbound":
            if not str(source.get("root") or "") or not isinstance(source.get("baseline"), list):
                return _unknown_fanout_policy(
                    "fan-out provider source baseline is malformed", provider_source_required=True
                )
        elif source_state == "bound":
            cursor = source.get("cursor")
            initial_range = source.get("initial_range")
            first = initial_range.get("first") if isinstance(initial_range, dict) else None
            root = initial_range.get("root") if isinstance(initial_range, dict) else None
            last = initial_range.get("last") if isinstance(initial_range, dict) else None
            if (
                not str(source.get("root") or "")
                or not str(source.get("path") or "")
                or not str(source.get("session_id") or "")
                or not str(source.get("parent_thread_id") or "")
                or not isinstance(cursor, dict)
                or not isinstance(cursor.get("line"), int)
                or cast(int, cursor.get("line")) < 0
                or not _digest(cursor.get("digest"))
                or not isinstance(first, dict)
                or first.get("line") != 1
                or not _digest(first.get("digest"))
                or not isinstance(root, dict)
                or not isinstance(root.get("line"), int)
                or cast(int, root.get("line")) < cast(int, first.get("line"))
                or not _digest(root.get("digest"))
                or not isinstance(last, dict)
                or not isinstance(last.get("line"), int)
                or cast(int, last.get("line")) < cast(int, root.get("line"))
                or not _digest(last.get("digest"))
                or not _digest(cast(dict[str, Any], initial_range).get("digest"))
                or not str(source.get("bound_at") or "")
            ):
                return _unknown_fanout_policy(
                    "fan-out provider source binding is malformed", provider_source_required=True
                )
        else:
            return _unknown_fanout_policy(
                "fan-out provider source binding is malformed", provider_source_required=True
            )
    if state == FANOUT_ALLOWED and terminal_state == "clean" and result["events"]:
        return _unknown_fanout_policy("a clean fan-out policy record carries provider events")
    # Only complete binding is clean; damaged history remains non-clean.
    if state == FANOUT_ALLOWED and (
        not str(result.get("run_id") or "")
        or not str(result.get("role") or "")
        or not str(result.get("model") or "")
        or not str(result.get("binary_path") or "")
        or not _digest(result.get("binary_digest"))
        or not str(result.get("cli_version") or "")
        or not _digest(result.get("tool_schema_digest"))
        or result.get("provider_schema_verdict") != "no_callable_child_spawn_surface"
    ):
        return _unknown_fanout_policy("fan-out policy allow attestation is incomplete")
    return result


def _digest(value: Any) -> bool:
    text = str(value or "").lower()
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)


def _unknown_fanout_policy(reason: str, *, provider_source_required: bool = False) -> dict[str, Any]:
    result = {
        "version": FANOUT_POLICY_VERSION,
        "state": FANOUT_UNKNOWN,
        "terminal_state": FANOUT_UNKNOWN,
        "reason": reason,
        "events": [],
    }
    if provider_source_required:
        # Preserve provenance so recovery cannot skip the exact-run fence.
        result["provider_source_required"] = True
        result["provider_source"] = {}
    return result
