"""Cheap, cached preflight checks for dispatcher head resources."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ummanu import _proc
from ummanu._fsutil import write_json
from ummanu.dispatch.types import HostError
from ummanu.runtime.provider_errors import (
    KIND_QUOTA,
    KIND_RECONNECT,
    KIND_SERVER,
    classify_provider_error,
    reset_time,
)
from ummanu.runtime.resource_probe import probe_timeout_s

PROBE_TTL_SECONDS = 300
# How long a resource a head's own turn found spent or refused stays red when the provider named no
# reset time (ummanu-108). Bounded, and followed by a fresh probe; a reset the provider names is
# held to exactly, however far off it is.
QUOTA_BACKOFF_SECONDS = 3600
PROVIDER_FAILURE_BACKOFF_SECONDS = 900
# The outer timeout of the default probe. A resource's own outer timeout is its inner probe timeout
# plus `PROBE_TIMEOUT_MARGIN_SECONDS` (`probe_timeout_seconds`), never less than this.
PROBE_TIMEOUT_SECONDS = 20
# How much longer the outer timeout waits than the inner one: interpreter start-up, and the inner
# probe's own classification and output after its provider call returned or timed out.
PROBE_TIMEOUT_MARGIN_SECONDS = 10
# A head named in a fallback chain that the registry no longer describes. It is a readiness status
# rather than a silent skip because it is the same kind of fact as a red resource — this head
# cannot be launched — and the tick has to be able to say so.
MISSING_HEAD = "missing"
# A probe that never reached the provider: the command could not be started, or the interpreter it
# names could not load the package it was asked to run. That is a defect of this installation's
# configuration, not a fact about the account, so it is neither `unknown` (which means "nothing is
# known about the resource" and lets a claim through) nor one of the red statuses (which claim the
# provider said something). It blocks the claim: a resource nobody can probe is a resource nobody
# has gated, and the fallback chain has to be walked instead of silently trusted.
PROBE_BROKEN = "probe_broken"
# A probe that got no answer in time: the outer command was killed, or the inner probe reported its
# own provider call timed out. Not `unknown` (secretary-1799): on 2026-09-25 Codex answered every
# turn with a 401 after its reconnect loop, the probe gave up first, the timeout read `unknown`,
# `unknown` let the claim through, and every review went back into the dead provider instead of
# down its fallback chain. A provider that cannot answer a ping in the probe's time is not one to
# launch a head into, so this status blocks the claim and the chain is walked.
PROBE_TIMED_OUT = "timed_out"
# The two statuses a claim may be launched on. `unknown` is deliberately here, and only for a probe
# that answered with something nobody could classify: that is not evidence that the account is
# dead, and holding every card on one ambiguous answer costs more than an occasional wasted attempt.
LAUNCH_ALLOWED_STATUSES = frozenset({"ready", "unknown"})
# The inner probe's own failure statuses (`ummanu.runtime.resource_probe`), as its one-line
# report spells them. Read by name, before any wording: the inner probe already knows which it was.
INNER_TIMEOUT_MARKER = "status=timeout"
INNER_PROVIDER_UNAVAILABLE_MARKER = "status=provider-unavailable"
INNER_EXHAUSTED_MARKER = "status=exhausted"
# What a failed *launch* of the probe looks like in the output the shell hands back. None of these
# is something a reachable provider says about an account, so they are read only after the
# provider-failure markers below have had their say.
PROBE_LAUNCH_MARKERS = (
    "no module named",
    "command not found",
    "no such file or directory",
    "can't open file",
    "cannot execute",
    "permission denied",
)
# `sh` answers a command it could not find with 127 and one it could not execute with 126. Both are
# the shell speaking, before the probe ever ran.
PROBE_LAUNCH_EXIT_CODES = (126, 127)


def probe_env() -> dict[str, str]:
    """This process's environment with our own interpreter's directory first on ``PATH``.

    The probe string lives in the head registry (`resources.*.probe`) and stays host-agnostic on
    purpose — it says `python3 -P -m <product module> ...` so the registry restores onto another
    machine — which only resolves to the dispatcher's own interpreter when that interpreter's
    directory is on `PATH`. Under systemd it is not: the unit pins a `PATH` without the venv while
    starting the dispatcher from `.venv/bin/ummanu`, so once `691673d` (2026-08-19) moved the
    package under `src/` and the working directory stopped carrying it, every probe died with
    `No module named ...` for the probe's package. Repairing it here rather than in the unit or in the
    registry keeps both of those portable and fixes every caller of the probe at once.
    """
    env = dict(os.environ)
    if not sys.executable:
        return env
    interpreter_dir = str(Path(sys.executable).parent)
    env["PATH"] = os.pathsep.join((interpreter_dir, env.get("PATH") or os.defpath))
    return env


@dataclass(frozen=True)
class HeadReadiness:
    """One verdict on a resource. `until` (epoch, 0.0 = none) is when a red verdict expires.

    A verdict with an `until` in the future is held to it: no probe runs before then, because a
    cheap probe answering `pong` is no proof there is quota for real work (ummanu-108).
    """

    resource: str
    status: str
    reason: str
    checked_at: float
    cached: bool = False
    until: float = 0.0

    @property
    def launch_allowed(self) -> bool:
        return self.status in LAUNCH_ALLOWED_STATUSES

    def to_json(self) -> dict[str, Any]:
        value = {
            "resource": self.resource,
            "status": self.status,
            "reason": self.reason,
            "checked_at": self.checked_at,
            "cached": self.cached,
        }
        if self.until:
            value["until"] = self.until
        return value


def failure_status(kind: str) -> str:
    """The resource status a head's provider error records: a spent quota is `exhausted`."""
    return "exhausted" if kind == KIND_QUOTA else "unavailable"


def failure_until(kind: str, reset_at: float, now: float) -> float:
    """When a resource a head's turn found red comes back: the provider's reset, else a backoff."""
    if reset_at > now:
        return reset_at
    return now + (QUOTA_BACKOFF_SECONDS if kind == KIND_QUOTA else PROVIDER_FAILURE_BACKOFF_SECONDS)


def until_text(until: float) -> str:
    """An expiry as an operator reads it: an ISO UTC minute."""
    if not until:
        return ""
    return time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(until))


def probe_timeout_seconds(resource: str) -> int:
    """The outer timeout around this resource's probe command: always above its inner timeout.

    The inner timeout is the probe module's (`resource_probe.probe_timeout_s`, 75 s for
    `openai-sub`, 20 s otherwise, configurable per resource); the outer one adds a margin so the
    inner classifier always gets to answer before its command is killed. Killing it first is what
    turned the 2026-09-25 401 into a bare timeout.
    """
    return max(PROBE_TIMEOUT_SECONDS, probe_timeout_s(resource) + PROBE_TIMEOUT_MARGIN_SECONDS)


def run_probe(resource: str, probe: str, now: float, *, timeout: float | None = None) -> HeadReadiness:
    """Execute one resource probe and classify what came back. Writes nothing.

    Separate from `HeadHealth` because `ummanu doctor` asks the same question read-only: it
    reports on a probe without owning the dispatcher's TTL cache.
    """
    limit = timeout if timeout is not None else probe_timeout_seconds(resource)
    try:
        # Its own process group, so a timeout takes the provider CLI under the shell down too: the
        # production tick's unit no longer kills what it leaves behind (secretary-1699).
        completed = _proc.run_isolated(["/bin/sh", "-c", probe], timeout=limit, env=probe_env())
    except subprocess.TimeoutExpired:
        # A probe that started and got no answer in time. Not a verdict on the account, and not a
        # resource a claim may be launched into either (`PROBE_TIMED_OUT`).
        return HeadReadiness(resource, PROBE_TIMED_OUT, f"probe timed out after {int(limit)}s", now)
    except OSError as exc:
        return HeadReadiness(resource, PROBE_BROKEN, f"probe could not be started: {type(exc).__name__}", now)
    except Exception as exc:  # a broken probe must not turn into a false resource outage  # noqa: BLE001 - a broken probe must not turn into a false outage
        return HeadReadiness(resource, "unknown", f"probe could not run: {type(exc).__name__}", now)
    if completed.returncode == 0:
        return HeadReadiness(resource, "ready", "probe succeeded", now)
    raw = " ".join((completed.stdout or "", completed.stderr or ""))
    text = raw.lower()
    if INNER_TIMEOUT_MARKER in text:
        return HeadReadiness(resource, PROBE_TIMED_OUT, "provider gave the probe no answer in time", now)
    if INNER_EXHAUSTED_MARKER in text:
        return _exhausted(resource, raw, now)
    # The provider's own failure, named before the account's: a 5xx, a reconnect loop that ran out,
    # or the inner probe saying the provider refused a valid login (the 2026-09-25 401). An operator
    # reading `unauthenticated` would log in again, which fixes none of these.
    provider = classify_provider_error(raw)
    if INNER_PROVIDER_UNAVAILABLE_MARKER in text or (
        provider is not None and provider.kind in (KIND_SERVER, KIND_RECONNECT)
    ):
        detail = f": {provider.summary}" if provider is not None and provider.summary else ""
        return HeadReadiness(resource, "unavailable", f"resource provider is unavailable{detail}", now)
    if any(
        marker in text
        for marker in ("login", "not authenticated", "unauthorized", "authentication", " 401", " 403")
    ):
        return HeadReadiness(resource, "unauthenticated", "resource authentication failed", now)
    # A spent subscription answers in its own words, and none of them is "rate limit": codex
    # says "You've hit your usage limit … purchase more credits or try again at <date>".
    # Classified before the provider-unavailable markers because the two read differently to an
    # operator — this resource is not flaky, it is out until the quota resets — and because
    # leaving it unclassified made it `unknown`, which `launch_allowed` treats as usable. On
    # 2026-08-06 that cost sprint:1200 two launches and a round into a dead resource before the
    # watchdog ceiling stopped it.
    if any(marker in text for marker in ("usage limit", "quota", "credits", "insufficient_quota", "billing")):
        return _exhausted(resource, raw, now)
    if any(
        marker in text
        for marker in ("503", "circuit_open", "unavailable", "rate limit", " 429", "connection", "network")
    ):
        return HeadReadiness(resource, "unavailable", "resource provider is unavailable", now)
    # Last, so that a provider which happens to word its refusal like a missing file is still read
    # as the provider talking: everything above is something only a reached provider says.
    if completed.returncode in PROBE_LAUNCH_EXIT_CODES or any(
        marker in text for marker in PROBE_LAUNCH_MARKERS
    ):
        return HeadReadiness(
            resource, PROBE_BROKEN, f"probe could not be launched: {_probe_detail(completed)}", now
        )
    return HeadReadiness(resource, "unknown", "probe returned an unclassified failure", now)


def _exhausted(resource: str, raw: str, now: float) -> HeadReadiness:
    """A spent quota, held to the reset the provider names when it names one (ummanu-108)."""
    reset_at = reset_time(raw, now=now)
    until = reset_at if reset_at > now else 0.0
    return HeadReadiness(
        resource,
        "exhausted",
        "resource quota is spent" + (f" until {until_text(until)}" if until else ""),
        now,
        until=until,
    )


def _probe_detail(completed: subprocess.CompletedProcess[str]) -> str:
    """The one line an operator needs to fix the probe, bounded so it stays a status reason.

    The last line, not the first: both shapes this sees put the fact there — `sh` prints one line,
    and an interpreter that could not load the module prints a traceback whose message is its last
    line.
    """
    for stream in (completed.stderr, completed.stdout):
        lines = [line.strip() for line in (stream or "").splitlines() if line.strip()]
        if lines:
            return lines[-1][:200]
    return f"exit {completed.returncode} with no output"


@dataclass(frozen=True)
class HeadChoice:
    """Which head a role is actually launched on, once resource health has had its say.

    ``head`` is empty when nothing reachable from ``preferred`` can be launched — that is the
    claim-skip: the card stays in Ready and no head is put into a dead resource. ``rejected``
    carries every candidate the walk turned down, in the order it read them, so the tick can name
    which resource is dead and why rather than only reporting that nothing was claimed.
    """

    preferred: str
    head: str
    readiness: HeadReadiness
    rejected: tuple[tuple[str, HeadReadiness], ...] = ()

    @property
    def resolved(self) -> bool:
        return bool(self.head)

    @property
    def substituted(self) -> bool:
        """Whether the launch is on a head the card did not ask for."""
        return bool(self.head) and self.head != self.preferred

    @property
    def reason(self) -> str:
        """One line for the tick: why this is the head, or why there is none.

        A preferred head with no chain behind it reads exactly as it did before there were chains —
        the resource's own reason and nothing else — because that is the whole story there. The
        chain is spelled out only when one was actually walked, so the line grows only where this
        card added something to say.
        """
        if not self.head and len(self.rejected) < 2:
            return self.readiness.reason
        rejected = "; ".join(
            f"{head} on {readiness.resource or '(no resource)'} is {readiness.status} ({readiness.reason})"
            for head, readiness in self.rejected
        )
        if not self.head:
            return f"no launchable head for {self.preferred}: {rejected}"
        if not self.substituted:
            return self.readiness.reason
        return (
            f"head {self.preferred} is not launchable ({rejected}); "
            f"falling back to {self.head} on {self.readiness.resource}"
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "preferred": self.preferred,
            "head": self.head,
            "substituted": self.substituted,
            "readiness": self.readiness.to_json(),
            "rejected": [dict(readiness.to_json(), head=head) for head, readiness in self.rejected],
            "reason": self.reason,
        }


def resolve_head_chain(
    preferred: str,
    readiness_of: Callable[[str], HeadReadiness],
    fallback_of: Callable[[str], Sequence[str] | None],
) -> HeadChoice:
    """The first launchable head at or below ``preferred``, breadth-first over the fallback chains.

    A red or spent resource is a property of the account, not of the profile drawing on it, so the
    answer to "the preferred head cannot run" is another head on a *different* resource — the
    chain the canon writes down, and only that chain. Nothing is inferred: a head with no chain
    and a dead resource is a claim-skip, which is the point (a card waiting in Ready costs
    nothing; a head launched into a spent subscription costs an attempt and a round).

    ``fallback_of`` returns None for a head the registry does not describe, and a *chain entry*
    that answers None is dropped without ever reaching ``readiness_of``. Existence is asked here
    because it cannot be asked there: a readiness check reads the profile to find the resource to
    probe, so a head that is not in the registry either raises out of that call (the dispatcher's
    catalog) or answers about nothing at all. Neither is a verdict a claim may act on, and the
    chain is exactly where a deleted profile survives — the registry validates chain targets when
    it is loaded, so what reaches here is whatever a later edit left behind. ``preferred`` itself
    is not filtered that way: whoever chose it (a card override, a role default) resolves it
    against the registry before asking, so a second check here with different manners would answer
    a question that caller has already answered, and answer it more quietly. Chains may be cyclic —
    the codex heads name the claude ones and back — so every candidate is read once.
    """
    seen: set[str] = set()
    rejected: list[tuple[str, HeadReadiness]] = []
    queue = [preferred]
    while queue:
        candidate = queue.pop(0)
        if candidate in seen:
            continue
        seen.add(candidate)
        chain = fallback_of(candidate)
        if chain is None and candidate != preferred:
            rejected.append(
                (
                    candidate,
                    HeadReadiness("", MISSING_HEAD, f"head {candidate} is not in the registry", time.time()),
                )
            )
            continue
        readiness = readiness_of(candidate)
        if readiness.launch_allowed:
            return HeadChoice(preferred, candidate, readiness, tuple(rejected))
        rejected.append((candidate, readiness))
        queue.extend(chain or ())
    first = (
        rejected[0][1]
        if rejected
        else HeadReadiness("", MISSING_HEAD, f"head {preferred} is not in the registry", time.time())
    )
    return HeadChoice(preferred, "", first, tuple(rejected))


def resource_health_path(data_dir: Path) -> Path:
    """The one resource-health cache of an installation: `<data_dir>/dispatcher/resource_health.json`.

    `HeadHealth` is its only writer; readers (steward telemetry, doctor) resolve it here so that no
    second module names the file and so none can grow a second copy of it.
    """
    return data_dir / "dispatcher" / "resource_health.json"


class HeadHealth:
    """Store resource verdicts independently from the dispatcher attempt state.

    A failed probe is not proof that the provider is down.  A definite authentication or provider
    failure stops a launch, and so does a probe that could not be launched at all (``PROBE_BROKEN``,
    a defect of this installation rather than of the account) and one that got no answer in time
    (``PROBE_TIMED_OUT``); an answered but unclassifiable failure is recorded as ``unknown`` and
    retries after the normal TTL.

    The dispatcher also writes here without a probe (``record``): a head whose first turn ended on a
    provider error is a fresher verdict on its resource than any probe (secretary-1799), so it
    replaces the cached entry and holds for the same TTL.
    """

    def __init__(self, catalog: Any, data_dir: Path) -> None:
        self.catalog = catalog
        self.path = resource_health_path(data_dir)

    def check(self, head: str) -> HeadReadiness:
        try:
            profile = self.catalog.head_profile(head)
            resource = str(profile["resource"])
        # HostError is how the catalog says "no such head"; a health probe answers that the same
        # way it answers every other unreadable configuration — unknown, not a crash on the
        # claim-time walk that is only asking whether this candidate is usable.
        except (AttributeError, HostError, KeyError, TypeError, ValueError) as exc:
            return HeadReadiness(
                "", "unknown", f"head health configuration unavailable: {type(exc).__name__}", time.time()
            )
        cache = self._load()
        entry = cache.get(resource)
        now = time.time()
        # A fresh recorded verdict answers before the probe command is even looked at: it may have
        # come from a head's own provider error rather than from a probe (`record`), and it holds
        # for its TTL on a resource with no probe as much as on one with a probe. A verdict with an
        # expiry holds until that expiry instead, however long, and is probed afresh once it passes.
        if isinstance(entry, dict):
            until = _float(entry.get("until"))
            fresh = now < until if until else now - _float(entry.get("checked_at")) < PROBE_TTL_SECONDS
            if fresh:
                return HeadReadiness(
                    resource,
                    str(entry.get("status") or "unknown"),
                    str(entry.get("reason") or ""),
                    _float(entry.get("checked_at")),
                    True,
                    until,
                )
        try:
            probe = str(self.catalog.resource(resource).get("probe") or "")
        except (AttributeError, HostError, KeyError, TypeError, ValueError) as exc:
            return HeadReadiness(
                "", "unknown", f"head health configuration unavailable: {type(exc).__name__}", time.time()
            )
        if not probe:
            return HeadReadiness(resource, "unknown", "resource has no probe command", time.time())

        verdict = self._run(resource, probe, now)
        cache[resource] = verdict.to_json()
        try:
            self._save(cache)
        except RuntimeError:
            # The preflight still has a useful verdict when its observability cache cannot be
            # written.  A later dispatcher write will surface a broader data-dir failure.
            pass
        return verdict

    def record(
        self, resource: str, status: str, reason: str, *, now: float | None = None, until: float = 0.0
    ) -> HeadReadiness:
        """Record a verdict on `resource` observed outside a probe, replacing its cached entry.

        Replacing the entry is the cache invalidation: the next `check` within the TTL (or before
        `until`, when given) answers with this verdict instead of a probe result from before it,
        and the probe runs again once that has passed. A cache that cannot be written raises,
        because the caller's next step (walking the fallback chain past this resource) depends on
        the verdict having landed.
        """
        verdict = HeadReadiness(resource, status, reason, time.time() if now is None else now, until=until)
        cache = self._load()
        cache[resource] = verdict.to_json()
        self._save(cache)
        return verdict

    def snapshot(self) -> dict[str, Any]:
        return self._load()

    def _run(self, resource: str, probe: str, now: float) -> HeadReadiness:
        return run_probe(resource, probe, now)

    def red_resources(self, *, now: float | None = None) -> dict[str, HeadReadiness]:
        """Every resource held red by an unexpired verdict, read from the cache without probing."""
        moment = time.time() if now is None else now
        held: dict[str, HeadReadiness] = {}
        for resource, entry in self._load().items():
            if not isinstance(entry, dict):
                continue
            until = _float(entry.get("until"))
            status = str(entry.get("status") or "unknown")
            if until > moment and status not in LAUNCH_ALLOWED_STATUSES:
                held[str(resource)] = HeadReadiness(
                    str(resource), status, str(entry.get("reason") or ""),
                    _float(entry.get("checked_at")), True, until,
                )
        return held

    def _load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _save(self, cache: dict[str, Any]) -> None:
        write_json(self.path, cache)


def _float(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
