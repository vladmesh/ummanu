"""Durable admission and turn shape from the authoritative socket or bounded journal."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ummanu.runtime.head import local_pty
from ummanu.runtime.local_pty_journal import _replay_journal
from ummanu.runtime.local_pty_values import (
    REHYDRATED_FROM_JOURNAL,
    REHYDRATED_FROM_SUPERVISOR,
    REHYDRATED_UNKNOWN,
    _Address,
)


@dataclass(frozen=True)
class _DurableHead:
    """What outlived the process that granted this head's turn, as one value.

    `source` says which witness answered, so "not draining" and "unknown" never look alike. `seq`
    is the journal sequence the activity epoch is raised to.
    """

    source: str
    seq: int = 0
    draining: bool = False
    turn_open: bool = False
    turn: int = 0
    exited: bool = False


def _journal_state(address: _Address, run_id: str) -> _DurableHead:
    """This head's shape from a bounded tail of its journal, replayed in sequence order.

    A replay, not a newest-record search: `turn.started` then `run.exited` is an ended turn, not an
    open one, and `run.started` resets the replay. A window with no record of this run, or one that
    cannot account for itself (torn last record, unreadable records, unordered sequence, begun
    mid-history), is `REHYDRATED_UNKNOWN`, still carrying its last sequence.
    """
    try:
        result = local_pty.read_tail(address.journal_path)
    except OSError:
        return _DurableHead(source=REHYDRATED_UNKNOWN)
    events = [event for event in result.events if str(event.get("run_id") or "") == run_id]
    if not events:
        return _DurableHead(source=REHYDRATED_UNKNOWN)
    seq = int(events[-1].get("seq") or 0)
    if result.truncated_tail or result.malformed or result.partial_head or not result.ordered:
        return _DurableHead(source=REHYDRATED_UNKNOWN, seq=seq)
    replay = _replay_journal(events)
    return _DurableHead(
        source=REHYDRATED_FROM_JOURNAL,
        seq=seq,
        draining=replay.draining,
        turn_open=replay.turn_open,
        turn=replay.turn,
        exited=replay.exited,
    )


def _supervisor_state(status: Mapping[str, Any]) -> _DurableHead:
    """What one `status` frame the supervisor answered says about this head's durable shape."""
    return _DurableHead(
        source=REHYDRATED_FROM_SUPERVISOR,
        seq=int(status.get("journal_seq") or 0),
        # `stopping` counts as draining: `drain.requested` is written before `run.stopping`.
        draining=bool(status.get("draining")) or bool(status.get("stopping")),
        turn_open=bool(status.get("turn_open")),
        turn=int(status.get("turn") or 0),
        exited=not bool(status.get("alive")),
    )
