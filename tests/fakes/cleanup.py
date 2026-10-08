"""Clocks for the cleanup owner's hourly retry policy (ummanu-132)."""

from __future__ import annotations

from ummanu.dispatch.cleanup import RETRY_COOLDOWN


class HourlyClock:
    """A cleanup clock one retry cooldown later at every read.

    Replay-proof tests retry an obligation to show what each later attempt may do; under the hourly
    retry policy every such retry is an hour after the previous attempt. The policy itself is
    pinned by tests whose clock only moves when told to.
    """

    def __init__(self, start: float = 1_800_000_000.0):
        self.now = start

    def __call__(self) -> float:
        self.now += RETRY_COOLDOWN
        return self.now


class ManualClock:
    """A cleanup clock that moves only when a test sets or advances it."""

    def __init__(self, start: float = 1_800_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds
