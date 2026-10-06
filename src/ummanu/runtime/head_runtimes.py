"""The closed vocabulary of head backends: currently only `local-pty`.

The runtime is orthogonal to the adapter (which CLI and effort). It lives outside the backend-free
`head` package so that `head.command.validate_launch_shape` checks registries against this single
list. Any other profile name is refused when the registry is read. `orca-legacy` is only a record
marker: a `HeadRun` written while heads were Orca panes still loads, reported as legacy, and is
never launched, delivered to or given a backend.
"""

from __future__ import annotations

#: The local-pty path: a supervisor of this product's own holds the head's pty and its journal.
LOCAL_PTY_RUNTIME = "local-pty"
HEAD_RUNTIMES = (LOCAL_PTY_RUNTIME,)

#: What an absent `runtime` key means in a head *profile*.
DEFAULT_HEAD_RUNTIME = LOCAL_PTY_RUNTIME

#: Not a runtime: the marker of a durable record whose head was an Orca pane. Never a valid profile.
ORCA_LEGACY_RUNTIME = "orca-legacy"
#: What an absent `runtime` means in a durable record (a `HeadRun` or spec from before backends
#: were selectable): every such head was an Orca pane, so the record is legacy.
RECORD_RUNTIME_WHEN_ABSENT = ORCA_LEGACY_RUNTIME


def is_legacy_runtime(name: str) -> bool:
    """Whether a runtime name read off a record marks a legacy Orca record (absence included)."""
    return (name or RECORD_RUNTIME_WHEN_ABSENT) == ORCA_LEGACY_RUNTIME
