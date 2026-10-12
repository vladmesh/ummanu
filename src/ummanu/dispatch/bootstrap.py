"""Construction boundary for the production dispatcher runtime.

The dispatcher state machine lives in :mod:`ummanu.dispatch.runtime`; the installed
CLI/web entry points build it here. Keeping backend selection, instance data-dir resolution and
host construction in this small module prevents new callers from treating the legacy dispatcher
monolith as a general-purpose API.
"""

from __future__ import annotations

from pathlib import Path

from ummanu.board.backend import CARD, SPRINT, board_client
from ummanu.checkpoint import tick_checkpoint_pusher, tick_checkpoint_writer
from ummanu.config import DataDirError, instance_data_dir
from ummanu.dispatch.git_workspace import orca_workspaces_root, workspace_roots_overlap
from ummanu.dispatch.host import CommandHostRuntime, InstanceCatalog
from ummanu.dispatch.runtime import DispatcherRuntime
from ummanu.dispatch.types import DispatcherError
from ummanu.runtime.paths import instance_file
from ummanu.sprints import SprintReader
from ummanu.tasks import TaskReader, TaskWriter, task_audit_for


def default_data_dir(instance_path: Path) -> Path:
    try:
        return instance_data_dir(instance_file(instance_path))
    except DataDirError as exc:
        raise DispatcherError("invalid_instance", f"invalid instance: {exc}", 2) from None


def validate_workspace_roots(data_dir: Path) -> None:
    """Refuse to run a real host whose Orca and git workspaces roots overlap in either direction."""
    overlap = workspace_roots_overlap(orca_workspaces_root(), data_dir)
    if overlap is not None:
        raise DispatcherError("workspace_roots_overlap", overlap, 2)


def runtime_from_args(
    instance: str, data_dir: str | None, *, host_mode: str, owner: str
) -> DispatcherRuntime:
    instance_path = Path(instance)
    data = Path(data_dir).expanduser() if data_dir else default_data_dir(instance_path)
    if host_mode == "real":
        validate_workspace_roots(data)
    # DispatcherRuntime also constructs a SprintReader from this client. The client is built by
    # the switch rather than by naming one backend here.
    client = board_client(instance_path, serves=(CARD, SPRINT))
    catalog = InstanceCatalog(instance_path)
    # The audit follows the client: the same requests/board_events tables the writer commits to.
    # The command host reads the same one, so TASK.md
    # feedback selection and report/verdict waits never disagree about what happened.
    audit = task_audit_for(client, data)
    # A live root that is still a Git work tree keeps today's checkpoint commit and push; any other
    # live root gets the snapshot exporter, and the pusher publishes its snapshot branch instead
    # (docs/RECOVERY.md, "Writers").
    checkpoint = tick_checkpoint_writer(data, catalog.instance_dir)
    return DispatcherRuntime(
        TaskReader(client),
        TaskWriter(client, data_dir=data),
        audit,
        data,
        catalog,
        CommandHostRuntime(catalog, data, mode=host_mode, audit=audit, sprint_reader=SprintReader(client)),
        owner=owner,
        checkpoint=checkpoint,
        checkpoint_push=tick_checkpoint_pusher(checkpoint),
    )
