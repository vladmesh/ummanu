"""The one gate a product run passes before it may exist for a card.

Both start paths (`run_start`, `run_review`) call :func:`admit` before building or spawning
anything, and use the `Admission` it returns. Checks, in order: card exists, project registered and
enabled, no open sprint reserves the project, card not in the dispatcher's lane, no dispatcher
record for it, no run of this layer for it that is not over. The last check consults only
`ProductRun.ended`, never how a run ended; a run whose head could not be confirmed stopped is not
over, which is all that fences an unresolved run. Every fact read here is owned elsewhere.
See docs/PROTOCOLS.md "One owner of a card".
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ummanu.config import InstanceReport
from ummanu.tasks import ACTIVE_STATES, TaskError, TaskReader
from ummanu.webproto.errors import OwnerConflict, TaskNotFound, ValidationRefused
from ummanu.webproto.runs import ProductRun, RunStore

#: The states in which a card belongs to the production pipeline: `ready` (claimed), `ACTIVE_STATES`
#: (workspace, suspended worker or running head) and `blocked` (waiting on an observer decision).
DISPATCHER_LANE = frozenset({"ready", "blocked"}) | ACTIVE_STATES

#: The states a product run may take a card from: the backlog, and only it.
ADMISSIBLE_STATES = frozenset({"issues"})


@dataclass(frozen=True)
class Admission:
    """A card this layer is allowed to run, with what the caller needs so it need not re-read."""

    ref: str
    project: str
    card: dict[str, Any]
    binding: dict[str, Any]
    #: The runs this card already has, oldest first, all over. `run_review` finds its worker here.
    runs: tuple[ProductRun, ...] = ()

    @property
    def repo(self) -> str:
        return str(self.binding.get("repo") or "")

    @property
    def default_branch(self) -> str:
        return str(self.binding.get("default_branch") or "main")


def admit(
    ref: str,
    *,
    report: InstanceReport,
    data_dir: Path,
    board: Any,
    store: RunStore,
    production_state: Path,
) -> Admission:
    """Whether this card may carry a product run, decided once, in the order documented above."""
    reference = str(ref or "")
    if not reference:
        raise ValidationRefused("a product run names the card it runs")

    card = _card(reference, board)
    project = str(card.get("project") or "")
    binding = _binding(report, project)
    _refuse_reserved_project(project, data_dir)
    _refuse_dispatcher_lane(reference, str(card.get("state") or ""))
    _refuse_dispatcher_record(reference, production_state)
    runs = tuple(store.for_ref(reference))
    _refuse_open_run(reference, runs)
    return Admission(ref=reference, project=project, card=card, binding=binding, runs=runs)


def _card(ref: str, board: Any) -> dict[str, Any]:
    try:
        return TaskReader(board).show(ref)
    except TaskError as exc:
        if exc.code == "not_found":
            raise TaskNotFound(f"the board holds no card {ref!r}") from None
        raise OwnerConflict(
            f"the board could not say who owns {ref!r}, so no run is started over it: {exc.message}"
        ) from None


def _binding(report: InstanceReport, project: str) -> dict[str, Any]:
    if not project:
        raise ValidationRefused("this card names no project, so there is no repository to run in")
    for binding in report.bindings:
        if isinstance(binding, dict) and str(binding.get("id") or "") == project:
            if not bool(binding.get("enabled", True)):
                raise ValidationRefused(f"project {project!r} is registered here but disabled")
            if not str(binding.get("repo") or ""):
                raise ValidationRefused(f"project {project!r} declares no repository to run in")
            return binding
    raise ValidationRefused(f"project {project!r} is not registered on this installation")


def _refuse_reserved_project(project: str, data_dir: Path) -> None:
    """An open sprint's project belongs to that sprint, and its observer decides what runs in it."""
    from ummanu.sprints import active_sprint_projects

    holders = active_sprint_projects(data_dir).get(project) or []
    if holders:
        raise OwnerConflict(
            f"project {project!r} is reserved by open sprint {', '.join(sorted(holders))}: its "
            "observer owns what runs there, so this product run is refused rather than started "
            "beside it"
        )


def _refuse_dispatcher_lane(ref: str, state: str) -> None:
    if state in DISPATCHER_LANE:
        raise OwnerConflict(
            f"card {ref} is in {state!r}, which is the production dispatcher's lane: it claims "
            f"{', '.join(sorted(DISPATCHER_LANE))} cards itself, and a product run over one of "
            "them would be a second owner of the same attempt"
        )
    if state not in ADMISSIBLE_STATES:
        raise OwnerConflict(
            f"card {ref} is in {state!r}, and a product run takes a card only from "
            f"{', '.join(sorted(ADMISSIBLE_STATES))}"
        )


def _refuse_dispatcher_record(ref: str, production_state: Path) -> None:
    """The dispatcher's own durable answer to "am I running this card", read and never written.

    An unreadable state file refuses: it is a source that could not say, not "running nothing".
    """
    import json

    try:
        payload = json.loads(production_state.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except (OSError, ValueError) as exc:
        raise OwnerConflict(
            f"the dispatcher production state at {production_state} could not be read ({exc}), so "
            "whether it is running this card could not be established and no run is started"
        ) from None
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, dict):
        raise OwnerConflict(
            f"the dispatcher production state at {production_state} carries no records object, so "
            "whether it is running this card could not be established and no run is started"
        )
    if ref in records:
        raise OwnerConflict(
            f"the production dispatcher holds a durable record for {ref}: it owns this card's "
            "attempt, and a product run over it would be a second owner"
        )


def _refuse_open_run(ref: str, runs: tuple[ProductRun, ...]) -> None:
    """Refuse while any run of this card is not over; decided by `run.ended` alone, never the outcome."""
    open_runs = [run for run in runs if not run.ended]
    if open_runs:
        names = ", ".join(f"{run.run_id} ({run.role})" for run in open_runs)
        raise OwnerConflict(
            f"card {ref} already carries an unsettled product run: {names}. One product run owns a "
            "card at a time; read its state, and start the next one when it has ended"
        )
