"""A `wait` card: one durable, headless wait for an external or board fact (secretary-1790).

A wait card names one **target** (a GitHub Actions run, another card reaching one of a named set of
states, or a point in time), a **deadline**, and one or more **return addresses** (`observer`,
`po-session:<id>`, `dependents`; `card:<ref>` only on a wait the dispatcher creates itself). No head runs it: the dispatcher claims it, advances it once per tick
and delivers its terminal outcome to every return address exactly once.

Everything the wait is lives on the card, in three typed fields of its extension bag
(`extensions.extra`, docs/BOARD_STORE.md §8.2), each one JSON text:

- `wait`: the spec, written once by `task create --type wait` and never changed;
- `wait_state`: what the dispatcher knows: when it started waiting, the last observation and the last
  error, the frozen result, and one delivery record per return address. Only the dispatcher writes it;
- `wait_cancel`: an explicit cancel (`task cancel`), with who, when and why. Only `task cancel`
  writes it, so it never races the dispatcher's own field.

A new dispatcher process reads all three back and continues a pending wait exactly where the last one
stopped: there is no process-local watcher. A field that does not parse reads as absent rather than
as a guess.

The first terminal fact observed is **frozen** into `wait_state.result` before anything is delivered,
and it is never overwritten. Every delivery is keyed by the card, the address and the frozen result
(:func:`result_key`), so a delivery repeated after a crash carries the same key and the receiving side
makes it a no-op.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ummanu.board.extension_bag import bag_json
from ummanu.board.models import CardState
from ummanu.board.sql_rows import parse_rfc3339, rfc3339

#: The three bag fields (see the module docstring).
WAIT_SPEC = "wait"
WAIT_STATE = "wait_state"
WAIT_CANCEL = "wait_cancel"

#: Target kinds.
TARGET_RUN = "github_run"
TARGET_CARD = "card"
TARGET_TIME = "time"

#: Return addresses.
OBSERVER = "observer"
DEPENDENTS = "dependents"
PO_SESSION_PREFIX = "po-session:"
#: One named card (secretary-1795), and only on a wait the dispatcher creates: its e2e stage waits for
#: a run it dispatched for a code card and names that card, which is not `blocked_by` the wait (it is in
#: Validate or Assessment, not Ready), so `dependents` would not reach it. The writer refuses it from
#: every other role, so it is not one of the `--wait-return` values a PO or an observer is offered.
CARD_PREFIX = "card:"

#: Terminal outcomes. Only `target_reached` ends in Done; every other one ends in Blocked.
TARGET_REACHED = "target_reached"
CANCELLED = "cancelled"
DEADLINE_PASSED = "deadline_passed"
SOURCE_UNREACHABLE = "source_unreachable"
OUTCOMES = (TARGET_REACHED, CANCELLED, DEADLINE_PASSED, SOURCE_UNREACHABLE)

#: The states `task show` names besides the three non-reached outcomes.
WAITING = "waiting"
RESULT_READY = "result_ready"
DELIVERED = "delivered"

#: A delivery record's one status: the receiving side took it. Nothing is recorded before that.
ACCEPTED = "accepted"

#: How long consecutive transient source errors (network, 5xx, rate limit) may last before the wait
#: ends as `source_unreachable`. Never past the deadline, which ends it first.
DEFAULT_TRANSIENT_WINDOW_SECONDS = 30 * 60

_REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
_RUN_URL_RE = re.compile(
    r"^https://github\.com/(?P<repo>[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100})"
    r"/actions/runs/(?P<run>[0-9]{1,20})(?:/attempts/[0-9]+)?/?$"
)
_DURATION_RE = re.compile(r"^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")
_CARD_REF_RE = re.compile(r"^[a-z0-9][a-z0-9-]*-[0-9]+$")


class WaitSpecError(ValueError):
    """A wait card's create input is missing or malformed; the message says which and why."""


def utc_text(moment: datetime) -> str:
    """RFC 3339 UTC with a `Z`, to the second."""
    return rfc3339(moment)


def parse_utc(value: str, what: str) -> datetime:
    """An ISO-8601 time that names its zone, as UTC. A time without a zone is refused, not guessed."""
    text = str(value or "").strip()
    moment = parse_rfc3339(text)
    if moment is None:
        raise WaitSpecError(f"{what} {text!r} is not an ISO-8601 time")
    if moment.tzinfo is None:
        raise WaitSpecError(f"{what} {text!r} names no zone; give UTC, e.g. 2026-09-28T12:00:00Z")
    return moment.astimezone(UTC)


def parse_duration(value: str, what: str) -> timedelta:
    """`90m`, `2h`, `1d12h`, `45s`: a positive duration of days, hours, minutes and seconds."""
    text = str(value or "").strip().lower()
    match = _DURATION_RE.match(text) if text else None
    if match is None or not any(match.groups()):
        raise WaitSpecError(f"{what} {value!r} is not a duration like 90m, 2h or 1d12h")
    days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    duration = timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
    if duration <= timedelta(0):
        raise WaitSpecError(f"{what} {value!r} is not a positive duration")
    return duration


def parse_deadline(value: str, now: datetime) -> datetime:
    """An absolute UTC time, or a duration from `now`; either must lie in the future."""
    text = str(value or "").strip()
    if not text:
        raise WaitSpecError("a wait card needs --wait-deadline: an absolute UTC time or a duration from now")
    deadline = now + parse_duration(text, "--wait-deadline") if _DURATION_RE.match(text.lower()) else None
    if deadline is None:
        deadline = parse_utc(text, "--wait-deadline")
    if deadline <= now:
        raise WaitSpecError(f"--wait-deadline {utc_text(deadline)} has already passed (now {utc_text(now)})")
    return deadline


@dataclass(frozen=True)
class WaitTarget:
    """What the wait waits for: exactly one of a run, a card event or a time."""

    kind: str
    repo: str = ""
    run_id: int = 0
    ref: str = ""
    states: tuple[str, ...] = ()
    at: str = ""

    @property
    def link(self) -> str:
        """The evidence link: a run's page; nothing for a card or a time."""
        if self.kind == TARGET_RUN:
            return f"https://github.com/{self.repo}/actions/runs/{self.run_id}"
        return ""

    def describe(self) -> str:
        if self.kind == TARGET_RUN:
            return f"GitHub Actions run {self.repo}#{self.run_id} ({self.link})"
        if self.kind == TARGET_CARD:
            return f"card {self.ref} reaching {' or '.join(self.states)}"
        return f"the time {self.at}"

    def to_json(self) -> dict[str, Any]:
        if self.kind == TARGET_RUN:
            return {"kind": self.kind, "repo": self.repo, "run_id": self.run_id, "url": self.link}
        if self.kind == TARGET_CARD:
            return {"kind": self.kind, "ref": self.ref, "states": list(self.states)}
        return {"kind": self.kind, "at": self.at}

    @classmethod
    def from_json(cls, payload: Any) -> WaitTarget | None:
        if not isinstance(payload, Mapping):
            return None
        kind = str(payload.get("kind") or "")
        if kind == TARGET_RUN:
            repo, run_id = str(payload.get("repo") or ""), payload.get("run_id")
            if (
                _REPO_RE.match(repo)
                and isinstance(run_id, int)
                and not isinstance(run_id, bool)
                and run_id > 0
            ):
                return cls(kind, repo=repo, run_id=run_id)
        elif kind == TARGET_CARD:
            ref, states = str(payload.get("ref") or ""), payload.get("states")
            if ref and isinstance(states, list) and states and all(state in _CARD_STATES for state in states):
                return cls(kind, ref=ref, states=tuple(str(state) for state in states))
        elif kind == TARGET_TIME:
            try:
                return cls(kind, at=utc_text(parse_utc(str(payload.get("at") or ""), "at")))
            except WaitSpecError:
                return None
        return None


_CARD_STATES = frozenset(state.value for state in CardState)


def parse_run_target(value: str, run_id: str = "") -> WaitTarget:
    """`owner/repo` with a run id, or the run's URL (which carries its own id)."""
    text, run_text = str(value or "").strip(), str(run_id or "").strip()
    url = _RUN_URL_RE.match(text)
    if url is not None:
        if run_text and run_text != url.group("run"):
            raise WaitSpecError(f"--wait-run-id {run_text} contradicts the run URL {text}")
        return WaitTarget(TARGET_RUN, repo=url.group("repo"), run_id=int(url.group("run")))
    if not _REPO_RE.match(text):
        raise WaitSpecError(
            f"--wait-run {text!r} is neither owner/repo nor a run URL "
            "(https://github.com/<owner>/<repo>/actions/runs/<id>)"
        )
    if not run_text:
        raise WaitSpecError(f"--wait-run {text} names a repository; give the run with --wait-run-id")
    if not run_text.isdigit() or int(run_text) <= 0:
        raise WaitSpecError(f"--wait-run-id {run_text!r} is not a positive run id")
    return WaitTarget(TARGET_RUN, repo=text, run_id=int(run_text))


def parse_card_target(ref: str, states: str) -> WaitTarget:
    """Another card reaching one of the named states (`--wait-states done,blocked`)."""
    reference = str(ref or "").strip()
    if not _CARD_REF_RE.match(reference):
        raise WaitSpecError(f"--wait-card {reference!r} is not a card reference like secretary-1790")
    named = [part.strip() for part in str(states or "").split(",") if part.strip()]
    if not named:
        raise WaitSpecError("--wait-card needs --wait-states: the states it waits for, e.g. done,blocked")
    unknown = [state for state in named if state not in _CARD_STATES]
    if unknown:
        known = ", ".join(state.value for state in CardState)
        raise WaitSpecError(f"--wait-states names unknown state(s) {', '.join(unknown)} (known: {known})")
    return WaitTarget(TARGET_CARD, ref=reference, states=tuple(dict.fromkeys(named)))


def parse_returns(values: Iterable[str]) -> tuple[str, ...]:
    """The return addresses, in the order given, each once."""
    found: list[str] = []
    for raw in values:
        for part in str(raw or "").split(","):
            address = part.strip()
            if not address:
                continue
            if address in (OBSERVER, DEPENDENTS):
                pass
            elif address.startswith(PO_SESSION_PREFIX) and address[len(PO_SESSION_PREFIX) :].strip():
                address = PO_SESSION_PREFIX + address[len(PO_SESSION_PREFIX) :].strip()
            elif address.startswith(CARD_PREFIX) and _CARD_REF_RE.match(address[len(CARD_PREFIX) :].strip()):
                address = CARD_PREFIX + address[len(CARD_PREFIX) :].strip()
            else:
                raise WaitSpecError(
                    f"--wait-return {address!r} is not observer, dependents or po-session:<id>"
                )
            if address not in found:
                found.append(address)
    if not found:
        raise WaitSpecError(
            "a wait card needs at least one --wait-return: observer, dependents or po-session:<id>"
        )
    return tuple(found)


def returned_card(address: str) -> str:
    """The card a `card:<ref>` address names, or `""` for any other address."""
    return address[len(CARD_PREFIX) :] if address.startswith(CARD_PREFIX) else ""


def po_sessions(returns: Iterable[str]) -> list[str]:
    """The session ids of the `po-session:<id>` addresses."""
    return [address[len(PO_SESSION_PREFIX) :] for address in returns if address.startswith(PO_SESSION_PREFIX)]


@dataclass(frozen=True)
class WaitSpec:
    target: WaitTarget
    deadline: str
    returns: tuple[str, ...]
    created_at: str
    transient_window_seconds: int = DEFAULT_TRANSIENT_WINDOW_SECONDS

    def to_json(self) -> dict[str, Any]:
        return {
            "target": self.target.to_json(),
            "deadline": self.deadline,
            "return": list(self.returns),
            "created_at": self.created_at,
            "transient_window_seconds": self.transient_window_seconds,
        }

    def text(self) -> str:
        return json.dumps(self.to_json(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, payload: Any) -> WaitSpec | None:
        if not isinstance(payload, Mapping):
            return None
        target = WaitTarget.from_json(payload.get("target"))
        returns = payload.get("return")
        window = payload.get("transient_window_seconds", DEFAULT_TRANSIENT_WINDOW_SECONDS)
        try:
            deadline = utc_text(parse_utc(str(payload.get("deadline") or ""), "deadline"))
            created = utc_text(parse_utc(str(payload.get("created_at") or ""), "created_at"))
            addresses = parse_returns(returns if isinstance(returns, list) else [])
        except WaitSpecError:
            return None
        if target is None or not isinstance(window, int) or isinstance(window, bool) or window <= 0:
            return None
        return cls(target, deadline, addresses, created, window)


def build_wait_spec(
    *,
    run: str = "",
    run_id: str = "",
    card: str = "",
    states: str = "",
    until: str = "",
    deadline: str = "",
    returns: Iterable[str] = (),
    transient_window: str = "",
    sprint: str = "",
    now: datetime,
) -> WaitSpec:
    """Validate a wait card's create input into its spec, or raise :class:`WaitSpecError`.

    Whether a named PO session exists is the writer's question (it needs the PO store); everything
    else is decided here, before anything is read.
    """
    named = [
        flag for flag, value in (("--wait-run", run), ("--wait-card", card), ("--wait-until", until)) if value
    ]
    if not named:
        raise WaitSpecError("a wait card needs exactly one target: --wait-run, --wait-card or --wait-until")
    if len(named) > 1:
        raise WaitSpecError(f"a wait card waits for exactly one target, not {' and '.join(named)}")
    if run_id and not run:
        raise WaitSpecError("--wait-run-id belongs to --wait-run owner/repo")
    if states and not card:
        raise WaitSpecError("--wait-states belongs to --wait-card")
    if run:
        target = parse_run_target(run, run_id)
    elif card:
        target = parse_card_target(card, states)
    else:
        target = WaitTarget(TARGET_TIME, at=utc_text(parse_utc(until, "--wait-until")))
    moment = parse_deadline(deadline, now)
    if target.kind == TARGET_TIME and parse_utc(target.at, "--wait-until") > moment:
        raise WaitSpecError(f"--wait-until {target.at} lies after the deadline {utc_text(moment)}")
    addresses = parse_returns(returns)
    if OBSERVER in addresses and not sprint:
        raise WaitSpecError("--wait-return observer needs --sprint: only a sprint's card has an observer")
    window = (
        int(parse_duration(transient_window, "--wait-transient-window").total_seconds())
        if str(transient_window or "").strip()
        else DEFAULT_TRANSIENT_WINDOW_SECONDS
    )
    return WaitSpec(target, utc_text(moment), addresses, utc_text(now), window)


def wait_spec(task: Mapping[str, Any]) -> WaitSpec | None:
    """The card's wait spec, or None when it carries no well-formed one."""
    return WaitSpec.from_json(bag_json(task, WAIT_SPEC))


def wait_cancel(task: Mapping[str, Any]) -> dict[str, str] | None:
    """The card's cancel as `{at, by, role, reason}`, or None."""
    payload = bag_json(task, WAIT_CANCEL)
    if not isinstance(payload, Mapping):
        return None
    record = {key: str(payload.get(key) or "").strip() for key in ("at", "by", "role", "reason")}
    return record if record["reason"] and record["at"] else None


def cancel_text(at: str, by: str, role: str, reason: str) -> str:
    return json.dumps({"at": at, "by": by, "role": role, "reason": reason}, sort_keys=True)


def result_key(result: Mapping[str, Any]) -> str:
    """The frozen result's identity: part of every delivery key."""
    identity = {"outcome": result.get("outcome"), "fact": result.get("fact")}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()[:16]


@dataclass
class WaitState:
    """The dispatcher's side of one wait, as `wait_state` holds it."""

    since: str = ""
    observed_at: str = ""
    observation: str = ""
    error: str = ""
    error_since: str = ""
    error_at: str = ""
    # {outcome, fact, summary, evidence, frozen_at, key}; None until the first terminal fact.
    result: dict[str, Any] | None = None
    # address -> {status, at, detail[, session]}; `session` is the PO session that took it.
    deliveries: dict[str, dict[str, str]] = field(default_factory=dict)
    # po-session address -> {replaces, via, session}: the successor of a closed or missing session,
    # its route recorded before it is opened and its id right after, so a repeat opens no second one.
    successors: dict[str, dict[str, str]] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "since": self.since,
            "observed_at": self.observed_at,
            "observation": self.observation,
            "error": self.error,
            "error_since": self.error_since,
            "error_at": self.error_at,
            "result": self.result,
            "deliveries": self.deliveries,
            "successors": self.successors,
        }

    def text(self) -> str:
        return json.dumps(self.to_json(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, payload: Any) -> WaitState:
        if not isinstance(payload, Mapping):
            return cls()
        result = payload.get("result")
        if not (isinstance(result, Mapping) and result.get("outcome") in OUTCOMES and result.get("key")):
            result = None
        deliveries = payload.get("deliveries")
        successors = payload.get("successors")
        return cls(
            **{key: str(payload.get(key) or "") for key in ("since", "observed_at", "observation", "error")},
            error_since=str(payload.get("error_since") or ""),
            error_at=str(payload.get("error_at") or ""),
            result=dict(result) if result is not None else None,
            deliveries={
                str(address): {key: str(value) for key, value in record.items()}
                for address, record in (deliveries.items() if isinstance(deliveries, Mapping) else ())
                if isinstance(record, Mapping) and record.get("status") == ACCEPTED
            },
            successors={
                str(address): {key: str(value) for key, value in record.items()}
                for address, record in (successors.items() if isinstance(successors, Mapping) else ())
                if isinstance(record, Mapping) and record.get("replaces") and record.get("via")
            },
        )


def wait_state(task: Mapping[str, Any]) -> WaitState:
    return WaitState.from_json(bag_json(task, WAIT_STATE))


def pending_addresses(spec: WaitSpec, state: WaitState) -> list[str]:
    """The addresses the frozen result still owes a delivery to. `observer` is the terminal move."""
    return [address for address in spec.returns if address != OBSERVER and address not in state.deliveries]


def wait_view(task: Mapping[str, Any]) -> dict[str, Any] | None:
    """The one structured block `task show` carries for a wait card, or None for any other card."""
    if str(task.get("type") or "") != "wait":
        return None
    spec = wait_spec(task)
    if spec is None:
        return {"state": "malformed", "reason": "the card carries no well-formed wait spec"}
    state = wait_state(task)
    card_state = str(task.get("state") or "")
    terminal = card_state in (CardState.DONE.value, CardState.BLOCKED.value)
    deliveries: dict[str, str] = {}
    for address in spec.returns:
        if address == OBSERVER:
            deliveries[address] = ACCEPTED if state.result is not None and terminal else "pending"
        else:
            deliveries[address] = (state.deliveries.get(address) or {}).get("status", "pending")
    result = state.result
    if result is None:
        name = WAITING
    elif result["outcome"] == TARGET_REACHED:
        name = DELIVERED if all(status != "pending" for status in deliveries.values()) else RESULT_READY
    else:
        name = str(result["outcome"])
    target = spec.target.to_json()
    if spec.target.link:
        target["link"] = spec.target.link
    return {
        "state": name,
        "target": target,
        "waiting_since": state.since or spec.created_at,
        "deadline": spec.deadline,
        "return_to": list(spec.returns),
        "transient_window_seconds": spec.transient_window_seconds,
        "last_observation": (
            {"at": state.observed_at, "text": state.observation} if state.observation else None
        ),
        "last_error": (
            {"at": state.error_at, "since": state.error_since, "text": state.error} if state.error else None
        ),
        "result": (
            {key: result.get(key) for key in ("outcome", "summary", "evidence", "fact", "frozen_at")}
            if result is not None
            else None
        ),
        "delivery": "pending" if any(status == "pending" for status in deliveries.values()) else "complete",
        "deliveries": deliveries,
        # Each PO address: the session it names, and the one that took the result (its successor
        # when the named one was closed or missing), or None while it has not been taken.
        "po_sessions": {
            address: {
                "addressed": address[len(PO_SESSION_PREFIX) :],
                "received_by": (state.deliveries.get(address) or {}).get("session") or None,
            }
            for address in spec.returns
            if address.startswith(PO_SESSION_PREFIX)
        },
        "cancel": wait_cancel(task),
    }


__all__ = [
    "ACCEPTED",
    "CANCELLED",
    "CARD_PREFIX",
    "DEADLINE_PASSED",
    "DEFAULT_TRANSIENT_WINDOW_SECONDS",
    "DELIVERED",
    "DEPENDENTS",
    "OBSERVER",
    "OUTCOMES",
    "PO_SESSION_PREFIX",
    "RESULT_READY",
    "SOURCE_UNREACHABLE",
    "TARGET_CARD",
    "TARGET_REACHED",
    "TARGET_RUN",
    "TARGET_TIME",
    "WAITING",
    "WAIT_CANCEL",
    "WAIT_SPEC",
    "WAIT_STATE",
    "WaitSpec",
    "WaitSpecError",
    "WaitState",
    "WaitTarget",
    "build_wait_spec",
    "cancel_text",
    "parse_card_target",
    "parse_deadline",
    "parse_duration",
    "parse_returns",
    "parse_run_target",
    "parse_utc",
    "pending_addresses",
    "po_sessions",
    "result_key",
    "returned_card",
    "utc_text",
    "wait_cancel",
    "wait_spec",
    "wait_state",
    "wait_view",
]
