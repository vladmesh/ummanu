"""Final safety guard for destructive wait/watchdog actions.

Refuse missing, foreign-run, healthy, unverifiable, suspended and suspected
stall evidence. ConfirmedStall/Dead may pass only with current-run evidence;
PID-only or currently dark progress requires the caller's outer stall ceiling.
The guard performs no destructive action itself."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ummanu.dispatch.head_vitality import PROGRESS_SOURCES
from ummanu.dispatch.head_vitality_episode import (
    VitalityEpisode,
    VitalityVerdict,
)


class GuardRefusal(StrEnum):
    """Why a destructive step was refused, in the vocabulary operators read."""

    MISSING_EPISODE = "missing-episode"
    FOREIGN_RUN = "foreign-run"
    HEALTHY_ACTIVE = "healthy-active"
    HEALTHY_QUIET = "healthy-quiet"
    SUSPENDED = "suspended"
    RETAINED = "retained"
    UNVERIFIABLE = "unverifiable"
    SUSPECTED_STALL = "suspected-stall"
    PID_ONLY_CEILING_UNELAPSED = "pid-only-ceiling-unelapsed"


#: Verdicts that may never ground a destructive step, each mapped to its refusal class
#: and the operator-facing why. Spelled out per verdict so a new ladder rung added later
#: fails here loudly instead of silently falling through to allowed.
_REFUSED_VERDICTS: dict[VitalityVerdict, tuple[GuardRefusal, str]] = {
    VitalityVerdict.HEALTHY_ACTIVE: (
        GuardRefusal.HEALTHY_ACTIVE,
        "the head is advancing right now",
    ),
    VitalityVerdict.HEALTHY_QUIET: (
        GuardRefusal.HEALTHY_QUIET,
        "the head is alive and quiet below every stall threshold",
    ),
    VitalityVerdict.UNVERIFIABLE: (
        GuardRefusal.UNVERIFIABLE,
        "no strong channel answered; nothing is concluded",
    ),
    VitalityVerdict.SUSPENDED: (
        GuardRefusal.SUSPENDED,
        "the process is parked on a stop signal: SIGCONT territory, not kill territory",
    ),
    VitalityVerdict.RETAINED: (
        GuardRefusal.RETAINED,
        "the dispatcher's own confirmed retention is holding this process: not a head to recover",
    ),
    VitalityVerdict.SUSPECTED_STALL: (
        GuardRefusal.SUSPECTED_STALL,
        "suspicion authorises one idempotent nudge, never destruction",
    ),
}


@dataclass(frozen=True)
class GuardDecision:
    """One guard ruling: allowed, or refused with the reason an operator can audit."""

    action: str
    allowed: bool
    refusal: GuardRefusal | None = None
    reason: str = ""
    verdict: str = ""
    episode_run_id: str = ""

    def to_json(self) -> dict[str, Any]:
        """The bounded form that travels in tick outcomes and telemetry comments."""
        return {
            "action": self.action,
            "allowed": self.allowed,
            "refusal": self.refusal.value if self.refusal is not None else "",
            "reason": self.reason,
            "verdict": self.verdict,
            "episode_run_id": self.episode_run_id,
        }


def _progress_source_answering(episode: VitalityEpisode) -> bool:
    """A progress source is a second witness only when it answered and is still
    answering now. A dark or never-seen provider leaves PID-only evidence,
    which requires the additional outer-ceiling hold before destruction."""
    witnessed = bool(set(episode.evidence_cursors) & PROGRESS_SOURCES) or episode.last_progress_at > 0.0
    dark = bool(set(episode.unavailable_since) & PROGRESS_SOURCES)
    return witnessed and not dark


def _refused(
    action: str,
    refusal: GuardRefusal,
    reason: str,
    episode: VitalityEpisode | None = None,
) -> GuardDecision:
    verdict = getattr(getattr(episode, "verdict", None), "value", "")
    return GuardDecision(
        action=action,
        allowed=False,
        refusal=refusal,
        reason=reason,
        verdict=verdict,
        episode_run_id="" if episode is None else episode.run_id,
    )


def assert_destructive_allowed(
    episode: Any,
    action: str,
    now: float,
    *,
    current_run_id: str = "",
    pid_only_outer_ceiling_seconds: float = 0.0,
) -> GuardDecision:
    """Rule on a destructive step against the persisted, current-run episode.

    Missing/foreign/healthy/unknown/suspended/suspected evidence is refused;
    a confirmed stall without a currently answering progress witness needs
    the caller's ordinary outer stall ceiling."""
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(float(now)):
        return _refused(
            action,
            GuardRefusal.MISSING_EPISODE,
            "the guard was handed a clock it cannot read; waiting is the safe side",
        )
    now = float(now)
    if not isinstance(episode, VitalityEpisode):
        return _refused(
            action,
            GuardRefusal.MISSING_EPISODE,
            "no vitality episode is on file; a destructive step nobody observed "
            "would act on nobody's evidence",
        )
    if current_run_id and episode.run_id != str(current_run_id):
        return _refused(
            action,
            GuardRefusal.FOREIGN_RUN,
            f"the persisted episode names run {episode.run_id}, not the current run "
            f"{current_run_id}: one run's stall verdict must never execute against "
            "its replacement",
            episode,
        )
    basis = ", ".join(episode.basis) if episode.basis else "none"
    if episode.verdict in _REFUSED_VERDICTS:
        refusal, why = _REFUSED_VERDICTS[episode.verdict]
        return _refused(
            action,
            refusal,
            f"{why} (basis: {basis})",
            episode,
        )
    if episode.verdict is not VitalityVerdict.CONFIRMED_STALL and episode.verdict is not VitalityVerdict.DEAD:
        # A ladder rung this card does not know -- or a malformed verdict that never was
        # one: refuse on principle. New verdicts must be added to the map above
        # explicitly before they can destroy anything.
        raw = getattr(episode.verdict, "value", episode.verdict)
        return _refused(
            action,
            GuardRefusal.UNVERIFIABLE,
            f"verdict {raw} is not one the guard knows as destructive-authorising; refusing on principle",
            episode,
        )
    if (
        episode.verdict is VitalityVerdict.CONFIRMED_STALL
        and not _progress_source_answering(episode)
        and pid_only_outer_ceiling_seconds > 0
    ):
        elapsed = max(0.0, now - episode.started_at)
        if elapsed < pid_only_outer_ceiling_seconds:
            dark = sorted(set(episode.unavailable_since) & PROGRESS_SOURCES)
            earned_on = (
                f"with {', '.join(dark)} dark (secretary-1543 arm)"
                if dark
                else "on the pid alone (issue 656 arm)"
            )
            return _refused(
                action,
                GuardRefusal.PID_ONLY_CEILING_UNELAPSED,
                f"confirmation was earned {earned_on}, so the "
                f"outer ceiling of {int(pid_only_outer_ceiling_seconds)}s must also "
                f"have elapsed ({int(elapsed)}s have)",
                episode,
            )
    return GuardDecision(
        action=action,
        allowed=True,
        verdict=episode.verdict.value,
        episode_run_id=episode.run_id,
        reason=(f"{episode.verdict.value} stands (basis: {basis})"),
    )
