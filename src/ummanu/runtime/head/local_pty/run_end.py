"""Whether a head's run has ended, decided from facts that outlive the head and a reboot."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..identity import HEARTBEAT_NOT_YET_WRITTEN, HEARTBEAT_UNREADABLE, current_boot_id
from .journal import RUN_EXITED
from .scoped_lifecycle import ScopedHeadLifecycle

RUN_END_EXITED = "run-exited"
RUN_END_EARLIER_BOOT = "launched-in-an-earlier-boot"


@dataclass(frozen=True)
class RunEnd:
    """Proof that one run has ended: why, and the signal its supervisor recorded for an exit."""

    reason: str
    signal: int | None = None

    def describe(self) -> str:
        return self.reason if self.signal is None else f"{self.reason} (signal {self.signal})"


def run_end(
    events: Sequence[Mapping[str, Any]], *, heartbeat_state: str, launched_boot: str
) -> RunEnd | None:
    """The run's end, or None when the facts prove nothing.

    Ended: the journal holds `run.exited` after the run's last `run.started`, or the launch
    identity is unwritten or unreadable and `launched_boot` names another boot. An empty journal
    or an empty `launched_boot` alone is not proof.
    """
    observed = ScopedHeadLifecycle.started_or_exited([dict(event) for event in events], 0)
    if observed is not None and observed[1]:
        started_seq = int(observed[0].get("seq") or 0)
        exit_record = next(
            event
            for event in events
            if event.get("kind") == RUN_EXITED and int(event.get("seq") or 0) > started_seq
        )
        signal = exit_record.get("signal")
        return RunEnd(RUN_END_EXITED, signal if type(signal) is int else None)
    if (
        heartbeat_state in (HEARTBEAT_NOT_YET_WRITTEN, HEARTBEAT_UNREADABLE)
        and launched_boot
        and launched_boot != current_boot_id()
    ):
        return RunEnd(RUN_END_EARLIER_BOOT)
    return None
