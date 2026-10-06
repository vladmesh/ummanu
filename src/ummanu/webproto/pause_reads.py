"""The pause reads: `pause_state` (the pause as `ummanu pause-status` reports it) and `pause_scope`
(what a pause command would reach, answered before it is issued). Both write nothing.

Every document states the pause's properties as fields: it is pipeline-wide (:data:`PIPELINE_WIDE`),
a drain stops no running head (:data:`DRAIN_CONTRACT`), and a freeze is a separate command never
reached implicitly (:data:`FREEZE_CONTRACT`). Rules stay in :mod:`ummanu.dispatch.pause`,
`dispatcher_pause_ops.head_lines` and `observer_snapshot`; nothing is re-decided here.

The flag, production state, sprint board and Pipeline listing are separate sources (via
:mod:`ummanu.webproto.section`), so one refusing does not take the others' fields. Each source read
(:func:`_source`) catches everything raised while reading and converting its one document; anything
outside that span is a defect and propagates. See docs/PROTOCOLS.md, "The pause as protocol
operations".
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ummanu.board.backend import SPRINT, board_client
from ummanu.config import InstanceReport, validate_instance
from ummanu.dispatch.observer import observer_snapshot
from ummanu.dispatch.pause import (
    ProductionPause,
    auto_resume_status,
    legacy_mirror_path,
    normalize_pause_mode,
    on_resume_text,
)
from ummanu.dispatch.pause_ops import head_lines
from ummanu.dispatch.production import ProductionState
from ummanu.sprints import SprintReader
from ummanu.tasks import _TYPED_RECORD_TYPES
from ummanu.webproto import sources
from ummanu.webproto.boundary import ProtocolBoundary
from ummanu.webproto.errors import ValidationRefused
from ummanu.webproto.section import Reading, Section, SectionSet, SourceSet, render, rule
from ummanu.webproto.section import read_source as _source

SCHEMA_VERSION = 1

#: The name of the soft pause, spelled once.
DRAIN = "drain"

#: The sources of a pause document, in precedence order (the order a refusal is attributed in).
SOURCE_INSTALLATION = "installation"
SOURCE_PAUSE = "pause"
SOURCE_LIVENESS = "liveness"
SOURCE_SPRINTS = "sprints"
SOURCE_CARDS = "cards"

#: Property 1, on every document; not read from any source, since it is what the pause is.
PIPELINE_WIDE = (
    "The pause is one pipeline-wide flag on the production dispatcher. There is no per-sprint "
    "pause and no way to pause one sprint: a pause reached from a sprint stops the dispatcher "
    "claiming every card on this installation's Pipeline board -- whichever sprint holds it, and "
    "whether or not one does -- and stops claiming for every open sprint at once."
)

#: Property 2.
DRAIN_CONTRACT = {
    "mode": DRAIN,
    "operation": "pause_drain",
    "stops": [
        "claiming Ready cards",
        "dispatching background roles",
        "raising an observer for a sprint opened during the pause",
    ],
    "does_not_stop": [
        "a worker head that is already running",
        "a reviewer head that is already running",
        "a sprint observer head that is already running",
    ],
    "statement": (
        "A drain stops no running head. A card already in flight rides its cycle to the end: its "
        "worker keeps writing, its reviewer keeps judging, and a green branch still merges. The "
        "heads listed under `heads` keep running through a drain."
    ),
}

#: Property 3. Described so an operator can compare; not reachable from this layer.
FREEZE_CONTRACT = {
    "mode": "freeze",
    "operation": None,
    "stops": [
        "everything a drain stops",
        "the live worker and reviewer heads of every tracked card",
        "the sprint observer heads",
        "the tick itself, which then advances nothing",
    ],
    "statement": (
        "A freeze is a different command, not a stronger drain, and it is never reached "
        "implicitly: this layer exposes no freeze operation, `pause_drain` takes no mode, and "
        "`ummanu.dispatch.pause_ops.pause` refuses to change mode while the pipeline is "
        "paused, so a drain is never quietly turned into a freeze. Freezing is `ummanu pause "
        "freeze`, issued deliberately, after a resume."
    ),
}

#: Board records a pause never reaches: a Product or Issue takes no claim or task transition.
NOT_A_CARD = frozenset(_TYPED_RECORD_TYPES)


class _Unreadable(Exception):
    """Inside one source read: the document could not be read or parsed at all.

    Lets the refusal say what the tick does with an unreadable file (e.g. a flag read as a freeze),
    a sentence a parseable-but-malformed document must not borrow.
    """


@dataclass(frozen=True, slots=True)
class _Dispatcher:
    """The dispatcher's production state, read and converted once per document.

    Converted inside the source read so an unconvertible record (e.g. a non-integer
    `attempt_round`) marks the source unavailable instead of raising past `SourceSet.decide`.
    """

    phase: str
    owner: str
    heads: list[dict[str, Any]]
    observers: list[dict[str, Any]]


class PauseSections(SectionSet):
    """Every section of every pause document; the only place a source is attributed to one.

    Nothing here reads a file: each rule receives exactly the sources it declares.
    """

    # -- what the pause acts on ---------------------------------------------------------------

    def target(self, read: SourceSet) -> Section:
        """Which dispatcher's flag a pause would write, and where it lives.

        Sourced `installation`: these are data-plane locations, known even when the flag is not.
        """
        return read.decide(
            rule(
                SOURCE_INSTALLATION,
                lambda paths: {
                    "dispatcher": "production",
                    "pause_file": str(paths["pause_file"]),
                    "state_file": str(paths["state_file"]),
                    "legacy_mirror_file": str(paths["legacy_mirror_file"]),
                },
            ),
            blank={
                "dispatcher": None,
                "pause_file": None,
                "state_file": None,
                "legacy_mirror_file": None,
            },
            narrates=(),
        )

    def dispatcher(self, read: SourceSet) -> Section:
        """The dispatcher that would be paused, as its own durable state describes it."""
        return read.decide(
            rule(
                SOURCE_LIVENESS,
                lambda live: {
                    "kind": "production",
                    "phase": live.phase,
                    "owner": live.owner,
                    "tracked_cards": len(live.heads),
                },
            ),
            blank={"kind": None, "phase": None, "owner": None, "tracked_cards": None},
            narrates=(),
        )

    # -- the pause itself ---------------------------------------------------------------------

    def state(self, read: SourceSet) -> Section:
        """Whether the pipeline is paused, and what the flag says, as converted by :func:`_flag_state`.

        The conversion happens in the source read so a malformed flag refuses as a source.
        """
        return read.decide(
            rule(SOURCE_PAUSE, dict),
            blank={
                "paused": None,
                "mode": None,
                "since": None,
                "actor": None,
                "pause_reason": None,
                "stopped_worker": None,
                "stopped_reviewer": None,
                "stopped_observer": None,
                "excluded_worker": None,
                "on_resume": None,
                "auto_resume": None,
                "legacy_mirror": None,
            },
            narrates=(),
        )

    # -- what is inside the scope --------------------------------------------------------------

    def heads(self, read: SourceSet) -> Section:
        """The heads the dispatcher holds now, per card (`head_lines`) and per sprint observer.

        A drain stops none of these; :data:`DRAIN_CONTRACT` says so.
        """
        return read.decide(
            rule(SOURCE_LIVENESS, lambda live: {"cards": live.heads, "observers": live.observers}),
            blank={"cards": None, "observers": None},
            narrates=(),
        )

    def sprints(self, read: SourceSet) -> Section:
        """The open sprints in scope, and a count of the others.

        `items` is `null`, never `[]`, when the sprint board did not answer.
        """
        return read.decide(
            rule(
                SOURCE_SPRINTS,
                lambda rows: {
                    "items": [
                        {
                            "ref": str(row.get("ref") or ""),
                            "goal": str(row.get("goal") or ""),
                            "status": str(row.get("status") or ""),
                            "current_task": row.get("current_task"),
                        }
                        for row in _open(rows)
                    ],
                    "other_sprints": len(rows) - len(_open(rows)),
                },
            ),
            blank={"items": None, "other_sprints": None},
            narrates=(),
        )

    def cards(self, read: SourceSet) -> Section:
        """Every card on the Pipeline board, with its sprint or `null`.

        The whole board is the scope: a drain stops claiming a Ready card whether or not a sprint
        holds it. Product and Issue records (`ummanu.tasks._TYPED_RECORD_TYPES`) are excluded, since
        no pause reaches them.
        """

        def from_listing(linked: dict[str, list[dict[str, Any]]]):
            items = [
                {
                    "ref": str(card.get("ref") or ""),
                    "sprint": str(group) or None,
                    "state": str(card.get("state") or ""),
                }
                for group, cards in linked.items()
                for card in cards
                if isinstance(card, dict) and card.get("record_type") not in NOT_A_CARD
            ]
            return {"items": sorted(items, key=lambda entry: entry["ref"])}

        return read.decide(
            rule(SOURCE_CARDS, from_listing),
            blank={"items": None},
            narrates=(),
        )


def _flag_state(state: dict[str, Any]) -> dict[str, Any]:
    """The pause flag as the `state` section publishes it; called inside the source read."""
    mode = normalize_pause_mode(state.get("mode"))
    stopped_worker = _refs(state.get("stopped_worker"))
    stopped_reviewer = _refs(state.get("stopped_reviewer"))
    mirror = state.get("legacy_mirror")
    return {
        "paused": bool(mode),
        "mode": mode or None,
        "since": str(state.get("since") or "") or None,
        "actor": str(state.get("actor") or "") or None,
        "pause_reason": str(state.get("reason") or "") or None,
        "stopped_worker": stopped_worker,
        "stopped_reviewer": stopped_reviewer,
        "stopped_observer": _refs(state.get("stopped_observer")),
        "excluded_worker": _refs(state.get("excluded_worker")),
        "on_resume": on_resume_text(mode, stopped_worker, stopped_reviewer),
        "auto_resume": auto_resume_status(state),
        "legacy_mirror": mirror if isinstance(mirror, dict) else {},
    }


def _refs(value: Any) -> list[str]:
    """One of the flag's head lists; raises unless it is a list of references."""
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(entry, str) for entry in value):
        raise TypeError(f"a pause flag head list is not a list of references: {value!r}")
    return list(value)


def _open(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if str(row.get("status") or "") == "open"]


#: Stateless; one instance serves every document.
SECTIONS = PauseSections()


class PauseReadLayer(ProtocolBoundary):
    """One installation's pause, read with no knowledge of who is asking.

    Construction does no I/O. `board_client` and `clock` are seams for tests or transports, not modes.
    """

    def __init__(
        self,
        instance: str | Path,
        *,
        data_dir: str | Path | None = None,
        board_client: Any | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.instance = Path(instance)
        self._data_dir = Path(data_dir) if data_dir is not None else None
        self._board_client = board_client
        self._clock = clock

    # -- operations ---------------------------------------------------------------------------

    def pause_state(self) -> dict[str, Any]:
        """Whether the pipeline is paused, in what mode, since when, and what is behind its cards.

        Writes nothing, takes no lock, and starts, stops or wakes nothing.
        """
        now = self._clock()
        report, installation = self._installation(now=now)
        data_dir = self.data_dir(report)
        read = SourceSet([installation, self._flag(data_dir, now=now), self._production(data_dir, now=now)])
        return render(
            {
                "schema_version": SCHEMA_VERSION,
                "kind": "pause_state",
                "observed_at": sources.isoformat(now),
                "extent": extent(),
                "target": SECTIONS.target(read),
                "dispatcher": SECTIONS.dispatcher(read),
                "state": SECTIONS.state(read),
                "heads": SECTIONS.heads(read),
                "modes": {"drain": DRAIN_CONTRACT, "freeze": FREEZE_CONTRACT},
                "sources": self._marks(read, (SOURCE_PAUSE, SOURCE_LIVENESS, SOURCE_INSTALLATION)),
            }
        )

    def pause_scope(self) -> dict[str, Any]:
        """What a pause command would reach: `pause_state` plus open sprints, Pipeline cards and heads.

        A pure read: no flag write, no tick lock, no head started, stopped or woken.
        """
        now = self._clock()
        report, installation = self._installation(now=now)
        data_dir = self.data_dir(report)
        sprints, cards = self._boards(data_dir, report, now=now)
        read = SourceSet(
            [
                installation,
                self._flag(data_dir, now=now),
                self._production(data_dir, now=now),
                sprints,
                cards,
            ]
        )
        return render(
            {
                "schema_version": SCHEMA_VERSION,
                "kind": "pause_scope",
                "observed_at": sources.isoformat(now),
                "extent": extent(),
                "target": SECTIONS.target(read),
                "dispatcher": SECTIONS.dispatcher(read),
                "state": SECTIONS.state(read),
                "sprints": SECTIONS.sprints(read),
                "cards": SECTIONS.cards(read),
                "heads": SECTIONS.heads(read),
                "modes": {"drain": DRAIN_CONTRACT, "freeze": FREEZE_CONTRACT},
                "sources": self._marks(
                    read,
                    (
                        SOURCE_PAUSE,
                        SOURCE_LIVENESS,
                        SOURCE_SPRINTS,
                        SOURCE_CARDS,
                        SOURCE_INSTALLATION,
                    ),
                ),
            }
        )

    # -- shared plumbing ----------------------------------------------------------------------

    def report(self) -> InstanceReport:
        report, refused = self._installation(now=self._clock())
        if report is None:
            raise ValidationRefused(str(refused.source.reason))
        return report

    def data_dir(self, report: InstanceReport | None = None) -> Path:
        if self._data_dir is not None:
            return self._data_dir
        report = report if report is not None else self.report()
        assert report.data_dir is not None
        return report.data_dir

    def _installation(self, *, now: float) -> tuple[InstanceReport | None, Reading]:
        """The installation config as a source, and the refusal only it can force.

        With an explicit data directory, an invalid config removes only what it owns. Without one,
        the refusal is `validation` (not `backend_unavailable`), keeping the exit status 2 that
        `runtime_from_args`' `invalid_instance` gave `ummanu pause-status`.
        """

        def produce() -> tuple[InstanceReport, dict[str, Path]]:
            report = validate_instance(self.instance)
            if not report.ok or report.data_dir is None:
                raise _Unreadable(
                    "this instance config does not validate: "
                    + "; ".join(str(error) for error in report.errors[:5])
                    if report.errors
                    else "this instance config names no data directory"
                )
            return report, self._paths(report.data_dir)

        reading = _source(
            SOURCE_INSTALLATION,
            produce,
            refusal=lambda exc: (
                str(exc)
                if isinstance(exc, _Unreadable)
                else f"this instance config could not be read: {_reason(exc)}"
            ),
            now=now,
            evidence=self._instance_file(),
        )
        if reading.answered:
            report, paths = reading.value
            return report, Reading(SOURCE_INSTALLATION, reading.source, paths)
        # Raised outside the span: this is the layer answering the caller, not a source failing.
        if self._data_dir is None:
            raise ValidationRefused(str(reading.source.reason))
        return None, reading

    @staticmethod
    def _paths(data_dir: Path) -> dict[str, Path]:
        """The three files a pause acts on, from the one data plane the installation names."""
        return {
            "pause_file": ProductionPause(data_dir).path,
            "state_file": ProductionState(data_dir).path,
            "legacy_mirror_file": legacy_mirror_path(),
        }

    # -- the sources --------------------------------------------------------------------------

    def _flag(self, data_dir: Path, *, now: float) -> Reading:
        """The pause flag, read through `ProductionPause` as the production tick reads it.

        A missing flag (`{}`) means running. A corrupt one is this source refusing; the reason says
        that every tick treats an unreadable flag as a freeze until it is repaired.
        """
        flag = ProductionPause(data_dir)

        def produce() -> dict[str, Any]:
            state = flag.load()
            if state.get("corrupt"):
                raise _Unreadable("the flag could not be read or parsed")
            return _flag_state(state)

        def refusal(exc: Exception) -> str:
            if isinstance(exc, _Unreadable):
                # The case `ProductionPause.load` has already decided the pipeline's behaviour for.
                return (
                    f"the pause flag could not be read: {flag.path}. Until it is repaired every "
                    "production tick reads an unreadable flag as a freeze and advances nothing"
                )
            # The file parses, so the tick still behaves by it: only what it says is unestablished.
            return (
                f"the pause flag parses but does not hold a pause state: {flag.path} "
                f"({_reason(exc)}). The production tick reads the same file, so what could not be "
                "established here is what the flag says, not the pipeline's behaviour"
            )

        return _source(SOURCE_PAUSE, produce, refusal=refusal, now=now, evidence=flag.path)

    def _production(self, data_dir: Path, *, now: float) -> Reading:
        """The dispatcher's production state; the `unavailable` phase is this source refusing.

        A refusal means nobody could say which heads are up, never that none is.
        """
        state = ProductionState(data_dir)

        def produce() -> _Dispatcher:
            payload = state.load()
            if str(payload.get("phase") or "") == "unavailable":
                raise _Unreadable("the state could not be read or parsed")
            return _Dispatcher(
                phase=str(payload.get("phase") or "new"),
                owner=str(payload.get("owner") or ""),
                # Record conversion is inside the span: a shape `DispatcherRecord.from_json`
                # refuses is this source not answering.
                heads=head_lines(state.records(payload)),
                observers=observer_snapshot(payload),
            )

        return _source(
            SOURCE_LIVENESS,
            produce,
            refusal=lambda exc: (
                f"the dispatcher production state could not be read: {state.path}"
                if isinstance(exc, _Unreadable)
                else f"the dispatcher production state could not be read: {state.path} ({_reason(exc)})"
            ),
            now=now,
            evidence=state.path,
        )

    def _boards(
        self, data_dir: Path, report: InstanceReport | None, *, now: float
    ) -> tuple[Reading, Reading]:
        """The sprint board and the Pipeline listing, as two sources that fail apart.

        Read with `create=False`, so no board is created.
        """
        evidence = data_dir / "board" / "cards.ndjson"
        sprints = _source(
            SOURCE_SPRINTS,
            lambda: SprintReader(self._client(), data_dir=data_dir).list(create=False),
            refusal=lambda exc: f"the sprint board could not be read: {_reason(exc)}",
            now=now,
            evidence=evidence,
        )
        cards = _source(
            SOURCE_CARDS,
            lambda: SprintReader(self._client(), data_dir=data_dir).linked_cards(),
            refusal=lambda exc: f"the Pipeline board could not be read: {_reason(exc)}",
            now=now,
            evidence=evidence,
        )
        return sprints, cards

    @staticmethod
    def _marks(read: SourceSet, keys: tuple[str, ...]) -> dict[str, Any]:
        """Every source's availability, under `sources` so a mark never overwrites a same-named section."""
        return {key: read.mark(key) for key in keys}

    def _client(self) -> Any:
        """The sprint board of this installation, named through the switch (board/backend.py)."""
        return self._board_client or board_client(self._instance_dir(), serves=(SPRINT,))

    def _instance_dir(self) -> Path:
        return self.instance.parent if self.instance.is_file() else self.instance

    def _instance_file(self) -> Path:
        """The config file itself, so a refusal can be dated by it even when reading it failed."""
        return self.instance if self.instance.is_file() else self.instance / "instance.yaml"


def extent() -> dict[str, Any]:
    """Property 1 as a field, read from no source, so it is present even when every source refused."""
    return {"scope": "pipeline", "per_sprint": False, "statement": PIPELINE_WIDE}


def _reason(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


__all__ = [
    "DRAIN",
    "DRAIN_CONTRACT",
    "FREEZE_CONTRACT",
    "PIPELINE_WIDE",
    "SCHEMA_VERSION",
    "SOURCE_CARDS",
    "SOURCE_INSTALLATION",
    "SOURCE_LIVENESS",
    "SOURCE_PAUSE",
    "SOURCE_SPRINTS",
    "PauseReadLayer",
    "PauseSections",
    "extent",
]
