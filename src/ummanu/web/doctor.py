"""The doctor lamp's reading: recorded installation health, classified by code, and cached.

Red when the installation cannot be trusted to run work or when
its health could not be read at all, which is the case a lamp must never draw as green; yellow when
it runs but somebody should look; green when health was read and reports no problem. The rule lives
in :data:`ummanu.webproto.reads.PROBLEM_SEVERITY` beside the codes it classifies, and this module
only applies it and adds recorded-read and stuck-collection problems. An expected first result
is unknown until collected; independent status problems retain their severity.

Recorded state only: status health and the latest result of the packaged doctor timer.
The reader launches no doctor, SSH or provider probe. Doctor's run time remains separate
from the time the web collected its cached reading. Unusable results are explicit problems;
an expected missing first result is unknown. Neither source can hide the other's findings.

Cached for the same reason the provider layer is (:mod:`ummanu.web.provider_usage`, whose shape
this copies): the bar is rendered by every page, and the collection behind it is not cheap, so one
in-process cache with its own TTL and an injectable clock decides how often it actually runs. It is
the only health cache of the process: the dashboard's health panel reads the same cached reading
(:meth:`DoctorLayer.health_snapshot`), so it is up to `CACHE_SECONDS` stale exactly as the lamp is,
and within one window the two are one reading.
"""

from __future__ import annotations

import copy
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from ummanu.webproto.errors import ReadError
from ummanu.webproto.reads import accepted, lamp_colour, problem_severity

#: How long one health reading serves the lamp. Shorter than the provider window: this reading is
#: local, and an operator who repaired a unit should see the lamp change within about a minute.
CACHE_SECONDS = 60

#: The problem a reading that did not happen carries. It is a problem like any other, with a code
#: like any other, so that "health is unknown" is classified by the same table as everything else.
UNREADABLE_CODE = "health.unreadable"

#: Said when this web process was built without a doctor layer at all. Health is then unknown,
#: which is red -- a lamp that stayed green because nothing was wired would be the worst kind.
DOCTOR_NOT_BUILT = "this web process was built without the doctor layer"


@dataclass(frozen=True, slots=True)
class HealthReading:
    """One published reading: when it was collected, what it was, and the lamp's document for it.

    Published once and never changed: nothing is written into it after the cache holds it, and a
    caller receives copies of its documents (:meth:`DoctorLayer.health_snapshot`), so no reader can
    change what another reader of the same window sees.
    """

    observed: float
    snapshot: dict[str, Any] | ReadError
    document: dict[str, Any]


class _Pin:
    """The reading one request has taken, held for the rest of that request."""

    __slots__ = ("reading",)

    def __init__(self) -> None:
        self.reading: HealthReading | None = None


class DoctorLayer:
    """One cached reading of recorded health, as the lamp, the doctor page and the dashboard read it.

    The cache holds the reading itself -- the read layer's health snapshot, or the refusal it
    raised -- beside the lamp's classification of it. :meth:`health_snapshot` hands out that
    reading, which is how the dashboard's health panel reads this same cache (`ReadLayer`'s
    `health_reader`): one collection, one window, so the panel and the lamp cannot disagree.

    Two rules make that hold under a threaded server:

    * **At most one collection in flight.** A miss or an expiry collects under a lock; a request
      arriving meanwhile waits for that collection and receives its reading instead of starting
      its own.
    * **One reading per response.** Inside :meth:`one_reading` -- which the transport opens around
      every request -- the first lookup pins its reading and every later lookup of that request
      answers with it, so a panel and a lamp rendered on either side of an expiry are still one
      reading.
    """

    def __init__(
        self,
        read_health: Callable[[], dict[str, Any]],
        *,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.read_health = read_health
        self.now = now
        self._cached: HealthReading | None = None
        self._lock = threading.Lock()
        self._pin: ContextVar[_Pin | None] = ContextVar(f"doctor-reading-{id(self)}", default=None)

    def doctor_snapshot(self) -> dict[str, Any]:
        """The current colour and the problems behind it, collected at most once per window."""
        return copy.deepcopy(self._reading().document)

    def health_snapshot(self) -> dict[str, Any]:
        """The recorded-health reading the lamp is classified from, out of the same cache."""
        snapshot = self._reading().snapshot
        if isinstance(snapshot, ReadError):
            raise snapshot
        return copy.deepcopy(snapshot)

    @contextmanager
    def one_reading(self) -> Iterator[None]:
        """Pin one reading for the span of one request: every lookup inside answers with the first.

        Lazy: a request that never asks for health -- a JSON route -- takes and pins nothing.
        """
        token = self._pin.set(_Pin())
        try:
            yield
        finally:
            self._pin.reset(token)

    def _reading(self) -> HealthReading:
        pin = self._pin.get()
        if pin is not None and pin.reading is not None:
            return pin.reading
        reading = self._current()
        if pin is not None:
            pin.reading = reading
        return reading

    def _current(self) -> HealthReading:
        """The reading of the current window, collecting it -- once, under the lock -- if due."""
        with self._lock:
            observed = self.now()
            cached = self._cached
            if cached is not None and observed - cached.observed < CACHE_SECONDS:
                return cached
            snapshot: dict[str, Any] | ReadError
            try:
                snapshot = self.read_health()
            except ReadError as exc:
                snapshot = exc
            document = _classify(snapshot)
            if isinstance(snapshot, dict):
                snapshot["health"]["combined"] = {
                    "colour": document["colour"], "findings": document["problems"],
                    "problems": [problem["message"] for problem in document["problems"]
                                 if problem.get("source") == "status" or not accepted(problem)],
                }
            self._cached = HealthReading(observed, snapshot, document)
            return self._cached


def _classify(reading: dict[str, Any] | ReadError) -> dict[str, Any]:
    """The lamp's document for one reading: its colour and the problems behind it."""
    if isinstance(reading, ReadError):
        return unreadable(reading.message)
    snapshot = reading
    section = snapshot.get("health") if isinstance(snapshot, dict) else None
    section = section if isinstance(section, dict) else {}
    status = section.get("status")
    source = section.get("source") if isinstance(section.get("source"), dict) else None
    problems: list[dict[str, Any]] = []
    readable = isinstance(status, dict) and bool(status)
    reason = None if readable else str((source or {}).get("reason") or "installation health was not read")
    if readable:
        problems.extend(
            {**finding, "severity": problem_severity(str(finding.get("code") or "")), "source": "status"}
            for finding in status.get("findings") or [] if isinstance(finding, dict)
        )
    else:
        problems.extend(unreadable(reason)["problems"])
    recorded = section.get("doctor") if isinstance(section.get("doctor"), dict) else {
        "state": "unknown", "reason": "not yet collected", "run_at": None,
    }
    problems.extend(
        {**finding, "message": str(finding.get("message") or _finding_identity(finding)),
         "severity": "neutral" if accepted(finding) else problem_severity(str(finding.get("code") or "")), "source": "doctor"}
        for finding in recorded.get("findings") or [] if isinstance(finding, dict)
    )
    if recorded.get("state") not in ("available", "unknown"):
        problems.append({
            "code": "health.unreadable", "source": "doctor",
            "message": f"recorded doctor is {recorded.get('state') or 'unknown'}: {recorded.get('reason') or 'no reason recorded'}",
            "severity": problem_severity(UNREADABLE_CODE),
        })
    collecting = recorded.get("collecting") or {}
    if collecting.get("stuck"):
        elapsed = collecting["elapsed_seconds"]
        threshold = collecting["threshold_seconds"]
        problems.append({
            "code": "doctor.collection_stuck", "source": "doctor",
            "message": f"doctor collection has run for {elapsed:.2f} seconds (threshold {threshold} seconds); "
                       "the producer may have been interrupted; inspect ummanu-doctor.service and its journal",
            "elapsed_seconds": elapsed, "threshold_seconds": threshold,
            "severity": problem_severity("doctor.collection_stuck"),
        })
    active = [problem for problem in problems if problem.get("source") == "status" or not accepted(problem)]
    colour = lamp_colour(active) if active or recorded.get("state") != "unknown" else "unknown"
    return {
        "kind": "doctor", "observed_at": str(snapshot.get("observed_at") or "") or None,
        "readable": readable, "reason": reason, "colour": colour,
        "problems": problems, "source": source, "doctor": recorded,
        "doctor_run_at": recorded.get("run_at"),
    }


def _finding_identity(finding: dict[str, Any]) -> str:
    return " ".join(str(finding[key]) for key in ("kind", "name", "capability", "resource") if finding.get(key)) or str(finding.get("code") or "doctor finding")


def unreadable(reason: str, *, source: dict[str, Any] | None = None) -> dict[str, Any]:
    """Health that could not be read, said as the problem it is rather than as an empty list."""
    problem = {
        "code": UNREADABLE_CODE,
        "message": f"this installation's health could not be read: {reason}",
        "severity": problem_severity(UNREADABLE_CODE),
    }
    return {
        "kind": "doctor",
        "observed_at": None,
        "readable": False,
        "reason": reason,
        "colour": lamp_colour([problem]),
        "problems": [problem],
        "source": source,
    }
