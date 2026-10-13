"""Composition root of the three background agents: ``python3 -P -m ummanu automations``.

Every entry — the systemd gate, an Orca precheck, a manual run — comes through here. Curator
needs no board port and is passed straight on; steward and retro get Ummanu's canonical
task adapters injected, so every board read/write goes through ``ummanu.tasks``.  The
generic triggered-agent runtime stays independent: it receives only its narrow structural
ports.  The dependency runs one way: this package imports ``ummanu``, never the reverse.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import cast

from ummanu.automations import __main__ as triggered_main
from ummanu.automations.agents.retro import cli as retro_cli
from ummanu.automations.agents.steward import cli as steward_cli
from ummanu.automations.agents.steward.signals import StewardSignalReader
from ummanu.automations.runtime import dispatch
from ummanu.board.backend import card_client
from ummanu.board.done_retention import DoneRetentionBoard
from ummanu.board.steward_reports import StewardReportBoard, StewardSignalBoard
from ummanu.config import instance_data_dir
from ummanu.kernel.errors import TaskError
from ummanu.runtime.codex_home import bound_data_dir
from ummanu.runtime.paths import default_instance_path
from ummanu.runtime.state import BoardUnavailable
from ummanu.tasks import TaskReader, TaskWriter

_SIGNAL_COMMANDS = frozenset({"scan", "precheck", "advance"})


def _instance_path() -> Path:
    """Resolve the same instance route as the public task commands."""
    return Path(os.environ.get("UMMANU_INSTANCE") or default_instance_path()).expanduser()


def _data_dir(instance: Path) -> Path:
    """Use an explicit process override or the instance-owned canonical path."""
    configured = os.environ.get("UMMANU_DATA_DIR")
    return Path(configured).expanduser() if configured else instance_data_dir(instance)


def _canonical_reader() -> TaskReader:
    """Build a reader only when a signal command actually reads the board."""
    try:
        return TaskReader(card_client(_instance_path()))
    except TaskError as exc:
        if exc.code == "backend_unavailable":
            # Precheck already distinguishes this historical "board is not
            # available yet" result from a broken deterministic helper.
            raise BoardUnavailable(exc.message) from None
        raise


def _map_signal_error(error: TaskError) -> None:
    """Keep the steward precheck's historical deferred-board classification."""
    if error.code == "backend_unavailable":
        raise BoardUnavailable(error.message) from None


def _signal_board() -> StewardSignalBoard:
    return StewardSignalBoard(reader_factory=_canonical_reader, error_mapper=_map_signal_error)


def _report_board() -> StewardReportBoard:
    def build() -> tuple[TaskReader, TaskWriter]:
        instance = _instance_path()
        client = card_client(instance)
        return TaskReader(client), TaskWriter(client, data_dir=_data_dir(instance))

    return StewardReportBoard(board_factory=build)


def _done_retention_board() -> DoneRetentionBoard:
    """Construct no config/client/audit state until retro actually cleans Done."""

    def build() -> tuple[TaskReader, TaskWriter]:
        instance = _instance_path()
        client = card_client(instance)
        return TaskReader(client), TaskWriter(client, data_dir=_data_dir(instance))

    return DoneRetentionBoard(board_factory=build, error_mapper=_map_signal_error)


def _steward(argv: list[str]) -> int:
    command = argv[0] if argv else "help"
    if command != "dispatch":
        # The board adapter answers with the board's card dicts; they carry the StewardSignalCard
        # keys, which `ummanu` cannot name without importing this package back.
        reader = cast(StewardSignalReader, _signal_board()) if command in _SIGNAL_COMMANDS else None
        return steward_cli.main(argv, reader=reader)

    parsed = triggered_main.parse_dispatch_arguments(argv[1:])
    # A precheck skip's `--cleanup-only` must stay free of config, board and audit construction:
    # it is a zero-side-effect early return in dispatch.run.
    if parsed.cleanup_only:
        return dispatch.run("steward", parsed.variant, cleanup_only=True)
    return dispatch.run("steward", parsed.variant, report_board=_report_board())


def _retro(argv: list[str]) -> int:
    command = argv[0] if argv else "help"
    if command in {"precheck", "harvest"}:
        return retro_cli.main(argv, retention=_done_retention_board())
    return triggered_main.main(["retro", *argv])


def main(argv: list[str] | None = None) -> int:
    """Run ``<agent> <cmd> [args]`` with the agent's board ports wired in."""
    # An agent's Codex heads resolve their CODEX_HOME against the selected installation's data dir.
    with bound_data_dir():
        return _main(argv)


def _main(argv: list[str] | None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return triggered_main.main(argv)
    if argv[0] == "retro":
        return _retro(argv[1:])
    if argv[0] != "steward":
        return triggered_main.main(argv)
    return _steward(argv[1:])
