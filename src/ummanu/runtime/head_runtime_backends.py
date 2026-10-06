"""The one place a head-runtime name becomes a backend object, and the one reader of that name.

`head_runtimes` holds the vocabulary; it stays separate so `head.command.validate_launch_shape`
does not import backends. Every caller that raises, observes or stops a head goes through here, so
no caller can raise a head another cannot reach. A legacy record (`orca-legacy`, or no name) still
loads, but `build_head_runtime` refuses it with `LegacyHeadRecordError` and never falls back to
`local-pty`. Backend dependencies are passed as callables, resolved only on build.
See docs/HEAD_RUNTIME.md.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from ummanu.runtime.head_runtimes import (
    HEAD_RUNTIMES,
    LOCAL_PTY_RUNTIME,
    ORCA_LEGACY_RUNTIME,
    RECORD_RUNTIME_WHEN_ABSENT,
    is_legacy_runtime,
)

from .local_pty_head import LocalPtyHeadRuntime


class UnknownHeadRuntimeError(ValueError):
    """A name no validated registry could have produced reached the one place backends are built."""


class LegacyHeadRecordError(UnknownHeadRuntimeError):
    """A legacy Orca record's runtime reached the build site: such a head is never launched."""


def head_runtime_name(subject: Any) -> str:
    """The backend name of a run, spec, name or None.

    The one reader of `HeadSpec.runtime` outside the spec. Subjects are heads or records, never
    profiles, so absence reads by the record rule (`orca-legacy`).
    """
    if subject is None:
        return RECORD_RUNTIME_WHEN_ABSENT
    if isinstance(subject, str):
        return subject or RECORD_RUNTIME_WHEN_ABSENT
    spec = getattr(subject, "spec", subject)
    return str(getattr(spec, "runtime", "") or RECORD_RUNTIME_WHEN_ABSENT)


def is_legacy_record(subject: Any) -> bool:
    """Whether a run, spec or name is a legacy Orca record (names `orca-legacy`, or nothing).

    A legacy record loads and is shown, but is never launched, delivered to or given a backend.
    """
    return is_legacy_runtime(head_runtime_name(subject))


def build_head_runtime(
    name: str,
    *,
    local_pty_root: Callable[[], Path],
    head_process_status: Callable[..., Any],
) -> Any:
    """Build the backend called `name`.

    Unknown and legacy names fail closed by name, never falling back to another backend. Callers
    that keep one instance per name cache it themselves: a rebuilt runtime forgets its handed-out
    turns.
    """
    if name == LOCAL_PTY_RUNTIME:
        return LocalPtyHeadRuntime(local_pty_root(), head_process_status=head_process_status)
    if is_legacy_runtime(name):
        raise LegacyHeadRecordError(
            f"head runtime {ORCA_LEGACY_RUNTIME!r} is a legacy Orca record: it is never launched, "
            f"delivered to or given a backend (known: {', '.join(HEAD_RUNTIMES)})"
        )
    raise UnknownHeadRuntimeError(f"unknown head runtime {name!r} (known: {', '.join(HEAD_RUNTIMES)})")
