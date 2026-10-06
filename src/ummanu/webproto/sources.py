"""One shape for "did this source answer, and if not, why, and how old is what we have".

Snapshot sources fail apart, so every section carries a `Source`: ``state`` (``available`` or
``unavailable``, never inferred from emptiness), ``reason``, ``observed_at`` and
``data_age_seconds``. An available source is dated at the read with age 0; an unavailable one is
dated by the newest evidence still on disk. See docs/PROTOCOLS.md "Sources fail apart".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ummanu.head_registry import HeadRegistryConfigError
from ummanu.sprint_observer import ObserverMetadataError
from ummanu.tasks import TaskError

AVAILABLE = "available"
UNAVAILABLE = "unavailable"

#: What a source read may raise instead of answering; each becomes an unavailable `Source`, never an
#: exception. Wider than missing/unparsable files: a readable document whose fields do not convert
#: raises `ValueError`, `TypeError` or `KeyError`. Read by `ummanu.webproto.sprint_reads`; note it
#: omits `DispatcherError`, which is why `pause_reads` catches everything within one document's read.
SOURCE_FAILURES: tuple[type[BaseException], ...] = (
    TaskError,
    HeadRegistryConfigError,
    ObserverMetadataError,
    OSError,
    ValueError,
    KeyError,
    TypeError,
)


def isoformat(moment: float) -> str:
    """The journal's own UTC spelling, so timestamps compare as strings across snapshots."""
    return datetime.fromtimestamp(moment, UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class Source:
    """The availability of one source of one snapshot section."""

    state: str
    reason: str | None = None
    observed_at: float | None = None
    data_age_seconds: float | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.reason,
            "observed_at": None if self.observed_at is None else isoformat(self.observed_at),
            "data_age_seconds": self.data_age_seconds,
        }


def available(now: float) -> Source:
    return Source(AVAILABLE, None, now, 0.0)


def unavailable(reason: str, *, now: float, evidence: Path | None = None) -> Source:
    """A refusal, dated by the mtime of ``evidence`` (the file the section would come from).

    A missing evidence file dates nothing, so both fields stay null.
    """
    stamped: float | None = None
    if evidence is not None:
        try:
            stamped = evidence.stat().st_mtime
        except OSError:
            stamped = None
    age = None if stamped is None else max(0.0, round(now - stamped, 3))
    return Source(UNAVAILABLE, reason[:400], stamped, age)
