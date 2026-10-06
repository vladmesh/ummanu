"""CLI handlers for the production dispatcher.

`pause`, `resume` and `pause-status` are clients rather than implementations, in the sense
`sprint list`, `sprint status` and `sprint comment` are: the soft pause, the resume and the pause
state read go through the named operations of :mod:`ummanu.webproto.pause_ops` and
:mod:`ummanu.webproto.pause_reads`, and what is left here is argument parsing, the document on
stdout, and the typed-code-to-exit-status table `ummanu web-run` already uses -- so the exit
status a script reads for a refusal is unchanged.

`pause freeze` is deliberately not routed through that layer. The layer exposes no freeze operation
at all, because a freeze stops live heads and must never be reachable as a variant of the soft path;
the freeze path is the one that was here before and is unchanged.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ummanu.dispatch.bootstrap import runtime_from_args
from ummanu.dispatch.head_status import head_status
from ummanu.dispatch.pause import PAUSE_MODES, normalize_pause_mode
from ummanu.dispatch.types import DispatcherError, HostError
from ummanu.runtime.codex_home import bound_data_dir
from ummanu.runtime.paths import add_instance_argument
from ummanu.tasks import TaskError
from ummanu.webproto.commands import _RUN_EXIT_BY_CODE, EXIT_BACKEND
from ummanu.webproto.errors import ReadError
from ummanu.webproto.pause_ops import PauseOperationLayer
from ummanu.webproto.pause_reads import DRAIN, PauseReadLayer


def add_dispatcher_subcommands(subparsers) -> None:
    dispatcher = subparsers.add_parser("dispatcher", help="run the task and sprint dispatcher")
    commands = dispatcher.add_subparsers(dest="dispatcher_command")

    for name, handler in (
        ("production-tick", run_dispatcher_production_tick),
        ("production-observe", run_dispatcher_production_observe),
        ("resource-health", run_dispatcher_resource_health),
        ("production-run", run_dispatcher_production_run),
    ):
        command = commands.add_parser(name)
        add_common(command)
        command.set_defaults(handler=handler)
        if name == "production-tick":
            command.add_argument(
                "--probe",
                action="store_true",
                help="run the tick with every write aborted, as a health gate",
            )
        if name == "production-run":
            command.add_argument("--interval-seconds", type=float, default=60.0)
            command.add_argument("--max-interval-seconds", type=float, default=300.0)
            command.add_argument("--max-ticks", type=int)

    dispatcher.set_defaults(handler=not_implemented_dispatcher)


def add_pause_commands(subparsers) -> None:
    """The one door to the pipeline-wide pause.

    Top level rather than under `dispatcher`, because pausing is an operator action on the pipeline
    and not one of the dispatcher's own steps. The legacy `pipeline pause` entry refuses and points
    here, so the two implementations cannot drift apart in silence.
    """
    pause = subparsers.add_parser(
        "pause",
        help="stop the pipeline: drain (no new claims) or freeze (heads stopped too)",
    )
    add_common(pause)
    pause.add_argument("mode", choices=(*PAUSE_MODES, "soft", "hard"))
    reason = pause.add_mutually_exclusive_group()
    reason.add_argument("--reason", help="why the pipeline is paused; required")
    reason.add_argument("--reason-file")
    pause.add_argument(
        "--exclude-workspace",
        action="append",
        default=[],
        help="freeze: leave the head in this workspace running (initiator exception)",
    )
    pause.set_defaults(handler=run_pause)

    resume = subparsers.add_parser("resume", help="clear the pause and put back what a freeze stopped")
    add_common(resume)
    resume.set_defaults(handler=run_resume)

    status = subparsers.add_parser("pause-status", help="read the production dispatcher's pause state")
    add_common(status)
    status.set_defaults(handler=run_pause_status)

    scope = subparsers.add_parser(
        "pause-scope",
        help="read what a pause would reach before issuing one: the flag, the open sprints and "
        "cards inside the pipeline-wide scope, and the heads a drain would leave running",
    )
    add_common(scope)
    scope.set_defaults(handler=run_pause_scope)


def add_head_status_command(subparsers) -> None:
    """The operator's read-only answer to "is there a head in this workspace?".

    Top level, beside `pause-status`, for the same reason that one is: this is a question an
    operator asks about the pipeline, not a step of the dispatcher's tick, and the person asking it
    is usually standing in front of a workspace that looks empty. It lives with the dispatcher's
    commands rather than with `reconcile plan/apply/adopt` because the heads it reports on are
    dispatcher state -- the records naming which head serves which card in which workspace -- while
    `host_commands` is about host resources the dispatcher does not own.
    """
    command = subparsers.add_parser(
        "head-status",
        help="read whether the dispatcher's heads in a workspace are alive, and what proved it",
    )
    add_common(command)
    command.add_argument(
        "--workspace",
        required=True,
        help="the live workspace to look at; every head the dispatcher holds there is reported",
    )
    command.set_defaults(handler=run_head_status)


def add_common(parser: argparse.ArgumentParser) -> None:
    add_instance_argument(parser)
    # Same pair, same order, as `ummanu task` (task_commands._add_data_dir_args) and as the
    # background agents' production-telemetry reader: an installation that
    # points its data plane elsewhere through the environment must move the dispatcher's writes
    # and its readers together, or health and steward scan would report a file nobody writes
    # (secretary-833 review, round 3).
    parser.add_argument("--data-dir", default=os.environ.get("UMMANU_DATA_DIR"))
    parser.add_argument(
        "--owner", default=os.environ.get("UMMANU_DISPATCHER_OWNER", "ummanu-dispatcher")
    )
    parser.add_argument("--actor", default=os.environ.get("BOARD_ACTOR", "operator"))
    parser.add_argument(
        "--host-mode",
        choices=("real", "noop"),
        default=os.environ.get("UMMANU_DISPATCHER_HOST_MODE", "real"),
    )


def not_implemented_dispatcher(args: argparse.Namespace) -> int:
    print(json.dumps({"error": {"code": "usage", "message": "dispatcher subcommand required"}}))
    return 2


def run_dispatcher_production_tick(args: argparse.Namespace) -> int:
    if getattr(args, "probe", False):
        return _run_production(args, lambda runtime: runtime.production_probe())
    return _run_production(args, lambda runtime: runtime.production_tick())


def run_dispatcher_production_observe(args: argparse.Namespace) -> int:
    return _run_production(args, lambda runtime: runtime.production_observe())


def run_dispatcher_resource_health(args: argparse.Namespace) -> int:
    return _run_production(
        args,
        lambda runtime: {
            "status": "ok",
            "step": "resource-health",
            "resources": runtime.head_health.snapshot(),
        },
    )


def run_dispatcher_production_run(args: argparse.Namespace) -> int:
    return _run_production(
        args,
        lambda runtime: runtime.production_run(
            interval_seconds=args.interval_seconds,
            max_interval_seconds=args.max_interval_seconds,
            max_ticks=args.max_ticks,
        ),
    )


def run_pause(args: argparse.Namespace) -> int:
    """The soft pause through the named operation; the freeze through the path it always had.

    The split is the point. `pause_drain` takes no mode and there is no freeze operation beside it,
    so no argument, default or fallback of this command can turn a request for a soft pause into a
    freeze: the two spellings reach two different implementations, and the freeze one is the
    deliberate `pause freeze` an operator types.
    """
    reason = (args.reason or "").strip() or _read_optional(args.reason_file).strip()
    if not reason:
        print(json.dumps({"error": {"code": "usage", "message": "pause requires --reason or --reason-file"}}))
        return 2
    if normalize_pause_mode(args.mode) == DRAIN:
        return _answer(lambda: _pause_operations(args).pause_drain(actor=args.actor, reason=reason))
    return _run_production(
        args,
        lambda runtime: runtime.pause_pipeline(
            mode=args.mode,
            actor=args.actor,
            reason=reason,
            exclude_workspaces=list(args.exclude_workspace or []),
        ),
    )


def run_resume(args: argparse.Namespace) -> int:
    return _answer(lambda: _pause_operations(args).pause_resume(actor=args.actor))


def run_pause_status(args: argparse.Namespace) -> int:
    return _answer(lambda: _pause_reads(args).pause_state())


def run_pause_scope(args: argparse.Namespace) -> int:
    return _answer(lambda: _pause_reads(args).pause_scope())


def _pause_operations(args: argparse.Namespace) -> PauseOperationLayer:
    return PauseOperationLayer(
        args.instance,
        data_dir=args.data_dir,
        host_mode=args.host_mode,
        owner=args.owner,
    )


def _pause_reads(args: argparse.Namespace) -> PauseReadLayer:
    return PauseReadLayer(args.instance, data_dir=args.data_dir)


def _answer(call) -> int:
    """One protocol document on stdout, or one typed refusal on stderr with its exit status.

    The statuses are `web-run`'s, which are the ones these commands already answered with: a
    `pause_conflict` is an `owner_conflict` and keeps the exit status 3 it has always had, a
    validation refusal keeps 2, and anything a durable source refused keeps 1.
    """
    try:
        document = call()
    except ReadError as exc:
        print(json.dumps({"error": exc.to_json()}), file=os.sys.stderr)
        return _RUN_EXIT_BY_CODE.get(exc.code, EXIT_BACKEND)
    print(json.dumps(document, sort_keys=True, separators=(",", ":")))
    return 0


def run_head_status(args: argparse.Namespace) -> int:
    return _run_production(args, lambda runtime: head_status(runtime, workspace=args.workspace))


def _run_production(args: argparse.Namespace, operation) -> int:
    try:
        runtime = runtime_from_args(args.instance, args.data_dir, host_mode=args.host_mode, owner=args.owner)
        # Every Codex head this operation launches resolves its CODEX_HOME against this data dir.
        with bound_data_dir(runtime.data_dir):
            result = operation(runtime)
    except (DispatcherError, TaskError) as exc:
        print(
            json.dumps(
                {"error": {"code": exc.code, "message": exc.message}}, sort_keys=True, separators=(",", ":")
            )
        )
        return exc.exit_code
    except HostError as exc:
        print(
            json.dumps(
                {"error": {"code": "host_error", "message": str(exc)}}, sort_keys=True, separators=(",", ":")
            )
        )
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result.get("status") in {"ok", "skipped"} else 3


def _read_optional(path: str | None) -> str:
    if path is None:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise DispatcherError("usage", f"cannot read file: {exc}", 2) from None
