"""`ummanu web-read` and `ummanu web-run`: the layer, callable before any web transport exists.

Two command groups side by side, one per half of the layer, with one subcommand per operation --
so both halves can be exercised, diffed and scripted from a shell, and so the card that adds a real
transport starts from a surface an operator has already read with their own eyes and driven a real
Codex worker and a real Claude reviewer through, rather than from an untried API.

This module is the only file under `webproto` that knows a caller exists, and it stays the only one
now that there are two groups: it parses arguments, prints JSON and maps a typed error onto an exit
status, and the layer itself does none of those things, which is what keeps a second transport from
having to re-implement any of it.

The operator loop `web-run` makes is the loop a web page will make:

    ummanu web-run start  --instance I --ref R --request-id X --profile P
    ummanu web-run state  --instance I --run-id  <run>          # until state.ended
    ummanu web-run review --instance I --worker-run <run> --request-id Y --profile Q
    ummanu web-run state  --instance I --run-id  <review>

`start` and `review` are idempotent on `--request-id`: running either twice with the same one
returns the same run and raises no second head.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from ummanu.runtime.paths import add_instance_argument
from ummanu.webproto.command_reads import CommandReadLayer
from ummanu.webproto.errors import ReadError
from ummanu.webproto.journal import DEFAULT_LIMIT
from ummanu.webproto.ops import OperationLayer
from ummanu.webproto.reads import TASK_SNAPSHOT_EVENTS, ReadLayer
from ummanu.webproto.runs import DEFAULT_DEADLINE_SECONDS

#: Exit statuses, the same ones `ummanu task` uses for the same two situations.
EXIT_VALIDATION = 2
EXIT_BACKEND = 1
_EXIT_BY_CODE = {"not_found": 2, "validation": 2, "backend_unavailable": 1}


def add_web_read_subcommands(subparsers) -> None:
    """Register the group. Read-only: none of these three takes an actor or writes anything."""
    group = subparsers.add_parser(
        "web-read",
        help="read the transport-independent system, task and event snapshots",
    )
    commands = group.add_subparsers(dest="web_read_command")

    system = commands.add_parser(
        "system", help="installation health, projects, current tasks and running agents"
    )
    _common(system)
    system.set_defaults(handler=run_web_read_system)

    task = commands.add_parser("task", help="one card: state, project, recent events and result")
    _common(task)
    task.add_argument("--ref", required=True, help="the card reference, as `ummanu task show` takes it")
    task.add_argument(
        "--events",
        type=int,
        default=TASK_SNAPSHOT_EVENTS,
        help="how many of the card's most recent events to include",
    )
    task.set_defaults(handler=run_web_read_task)

    events = commands.add_parser("events", help="one page of a card's events, and a cursor to continue")
    _common(events)
    events.add_argument("--ref", required=True, help="the card reference")
    events.add_argument(
        "--cursor",
        help="the `next_cursor` of an earlier page; omit to start at the beginning of the journal",
    )
    events.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="page size")
    events.set_defaults(handler=run_web_read_events)

    history = commands.add_parser(
        "commands",
        help="the last commands across every entity: who ran what, on what, and how it ended",
    )
    _installation(history)
    history.add_argument(
        "--cursor",
        help="the `next_cursor` of an earlier page; omit to start at the newest command",
    )
    history.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="page size")
    history.set_defaults(handler=run_web_read_commands)

    request = commands.add_parser(
        "request", help="what became of one request id: not found, pending, committed or unknown"
    )
    _installation(request)
    request.add_argument("--request-id", required=True, help="the id the operation was sent with")
    request.set_defaults(handler=run_web_read_request)

    group.set_defaults(handler=_usage)


def _common(parser: argparse.ArgumentParser) -> None:
    _installation(parser)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="collect installation health without inspecting the live host",
    )


def _installation(parser: argparse.ArgumentParser) -> None:
    """The arguments every read of this group takes: which installation, and how to print it.

    `--offline` is deliberately not among them. It is the host-inspection switch of the
    installation health collector, and the two command-history reads consult no host: offering it
    there would offer a mode that changes nothing.
    """
    add_instance_argument(parser, help="path to an instance dir or instance.yaml")
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("UMMANU_DATA_DIR"),
        help="override the instance's configured data directory",
    )
    parser.add_argument("--json", action="store_true", help="print the snapshot as JSON")


def _usage(_args: argparse.Namespace) -> int:
    print(json.dumps({"error": {"code": "usage", "message": "web-read subcommand required"}}))
    return EXIT_VALIDATION


def _layer(args: argparse.Namespace) -> ReadLayer:
    return ReadLayer(args.instance, data_dir=args.data_dir, offline=bool(args.offline))


def _command_layer(args: argparse.Namespace) -> CommandReadLayer:
    return CommandReadLayer(args.instance, data_dir=args.data_dir)


def run_web_read_system(args: argparse.Namespace) -> int:
    return _emit(args, lambda: _layer(args).system_snapshot(), _system_lines)


def run_web_read_task(args: argparse.Namespace) -> int:
    return _emit(args, lambda: _layer(args).task_snapshot(args.ref, events=args.events), _task_lines)


def run_web_read_events(args: argparse.Namespace) -> int:
    return _emit(
        args,
        lambda: _layer(args).task_events(args.ref, args.cursor, limit=args.limit),
        _event_lines,
    )


def run_web_read_commands(args: argparse.Namespace) -> int:
    return _emit(
        args,
        lambda: _command_layer(args).command_history(args.cursor, limit=args.limit),
        _command_lines,
    )


def run_web_read_request(args: argparse.Namespace) -> int:
    return _emit(
        args,
        lambda: _command_layer(args).command_request(args.request_id),
        _request_lines,
    )


def _emit(args: argparse.Namespace, operation, render) -> int:
    """Run one read, print it, and turn a typed refusal into the group's exit status."""
    try:
        snapshot = operation()
    except ReadError as exc:
        print(json.dumps({"error": exc.to_json()}), file=sys.stderr)
        return _EXIT_BY_CODE.get(exc.code, EXIT_BACKEND)
    if args.json:
        print(json.dumps(snapshot, sort_keys=True))
        return 0
    for line in render(snapshot):
        print(line)
    return 0


def _source(section: dict[str, Any]) -> str:
    source = section.get("source") or {}
    if source.get("state") == "available":
        return "available"
    age = source.get("data_age_seconds")
    aged = "" if age is None else f", showing data {int(age)}s old"
    return f"unavailable ({source.get('reason')}){aged}"


def _system_lines(snapshot: dict[str, Any]):
    installation = snapshot["installation"]
    yield f"instance: {installation['instance']} ({installation['name'] or 'unnamed'})"
    yield f"health: {_source(installation['health'])}"
    yield f"projects: {len(snapshot['projects']['items'])} registered, {_source(snapshot['projects'])}"
    yield f"tasks: {len(snapshot['tasks']['items'])} current, {_source(snapshot['tasks'])}"
    yield f"agents: {_source(snapshot['agents'])}"
    for agent in snapshot["agents"]["items"]:
        yield f"  {agent['ref']} {agent['role']}: {agent['state']} — {agent['reason']}"


def _task_lines(snapshot: dict[str, Any]):
    card = snapshot["card"]["value"]
    yield f"{snapshot['ref']}: {card['state'] if card else 'card ' + _source(snapshot['card'])}"
    if card:
        yield f"title: {card['title']}"
    yield f"project: {snapshot['project']['id'] or 'unknown'}"
    for agent in snapshot["agents"]["items"]:
        yield f"  {agent['role']}: {agent['state']} — {agent['reason']}"
    outcome = snapshot["work"]["outcome"]
    yield f"outcome: {outcome['kind']}:{outcome['value']} at {outcome['at']}" if outcome else "outcome: none"
    yield f"events: {len(snapshot['events']['items'])} shown, next cursor {snapshot['events']['next_cursor']}"


def _command_lines(snapshot: dict[str, Any]):
    commands = snapshot["commands"]
    items = commands["items"]
    yield f"commands: {_source(commands)}" if items is None else f"commands: {len(items)} shown"
    for item in items or []:
        actor = item["actor"] or {}
        result = item["result"]["reason"] or item["result"]["outcome"] or ""
        entity = item["entity"]["ref"]
        yield f"  {item['occurred_at']} {actor.get('id') or '?'} {item['action']} {entity} {result}".rstrip()
    yield f"next cursor: {commands['next_cursor']}" + (" (more)" if commands["has_more"] else "")


def _request_lines(snapshot: dict[str, Any]):
    operation = snapshot["operation"]
    yield f"{snapshot['request_id']}: {operation['state']} ({_source(operation)})"
    if operation["entity"]:
        yield f"  {operation['action']} on {operation['entity']['ref']} at {operation['occurred_at']}"
    if operation["continuation"]:
        yield f"  continue by repeating this request id: {operation['continuation']['request_id']}"


def _event_lines(snapshot: dict[str, Any]):
    yield f"{snapshot['ref']}: {len(snapshot['items'])} events, {_source(snapshot)}"
    for item in snapshot["items"]:
        yield f"  {item['occurred_at']} {item['kind']} {item['reason'] or item['outcome'] or ''}".rstrip()
    yield f"next cursor: {snapshot['next_cursor']}" + (" (more)" if snapshot["has_more"] else "")


# -- `ummanu web-run`: the operation half ----------------------------------------------------

#: The same statuses the read group uses for the same situations, plus one. An owner conflict is a
#: refusal about the state of the world rather than a malformed request, so it gets its own status:
#: a script can then tell "somebody else has this card" from "I asked wrongly".
EXIT_CONFLICT = 3
#: The status an operation that is durably part-done and repairable answers with. It is the status
#: `SprintWriter.close` has always given `audit_pending`, kept here so the command that maps it and
#: the table beside it cannot drift: the typed failure is an `OperationPending` carrying the request
#: id to repeat, and this is the number a shell script branches on.
EXIT_PENDING = 4
_RUN_EXIT_BY_CODE = {
    "not_found": 2,
    "validation": 2,
    "backend_unavailable": 1,
    "owner_conflict": EXIT_CONFLICT,
    # A refusal about the caller, with the status the task protocol gives the same refusal.
    "role_masquerade": EXIT_CONFLICT,
    "observer_identity_unbound": EXIT_CONFLICT,
    "observer_sprint_mismatch": EXIT_CONFLICT,
}


def add_web_run_subcommands(subparsers) -> None:
    """Register the group beside `web-read`."""
    group = subparsers.add_parser(
        "web-run",
        help="start, review and read a product run without a web transport",
    )
    commands = group.add_subparsers(dest="web_run_command")

    start = commands.add_parser("start", help="raise a worker head for one card")
    _run_common(start)
    start.add_argument("--ref", required=True, help="the card the run is for")
    start.add_argument("--request-id", required=True, help="the id this start is idempotent on")
    start.add_argument("--profile", required=True, help="the head profile, from the head registry")
    start.add_argument(
        "--instruction",
        default="",
        help="extra instruction added to the run's task document, beside the card's description",
    )
    start.set_defaults(handler=run_web_run_start)

    review = commands.add_parser("review", help="raise a reviewer head by a worker run's result")
    _run_common(review)
    review.add_argument("--request-id", required=True, help="the id this review is idempotent on")
    review.add_argument("--profile", required=True, help="the reviewer head profile")
    review.add_argument("--worker-run", default="", help="the worker run to review")
    review.add_argument("--ref", default="", help="the card, when the worker run is its latest")
    review.set_defaults(handler=run_web_run_review)

    state = commands.add_parser("state", help="one run's state, and the place its ending settles")
    _run_common(state)
    state.add_argument("--run-id", required=True, help="the run to read")
    state.set_defaults(handler=run_web_run_state)

    listing = commands.add_parser("list", help="every product run of one card")
    _run_common(listing)
    listing.add_argument("--ref", required=True, help="the card whose runs to list")
    listing.set_defaults(handler=run_web_run_list)

    group.set_defaults(handler=_run_usage)


def _run_common(parser: argparse.ArgumentParser) -> None:
    add_instance_argument(parser, help="path to an instance dir or instance.yaml")
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("UMMANU_DATA_DIR"),
        help="override the instance's configured data directory",
    )
    parser.add_argument(
        "--heads-registry",
        default=os.environ.get("TA_HEADS_REGISTRY"),
        help="read head profiles from this registry instead of the installation's own",
    )
    parser.add_argument(
        "--deadline-seconds",
        type=float,
        default=DEFAULT_DEADLINE_SECONDS,
        help="how long a run may take before the product ends the head holding it",
    )
    parser.add_argument("--json", action="store_true", help="print the document as JSON")


def _run_usage(_args: argparse.Namespace) -> int:
    print(json.dumps({"error": {"code": "usage", "message": "web-run subcommand required"}}))
    return EXIT_VALIDATION


def _ops_layer(args: argparse.Namespace) -> OperationLayer:
    return OperationLayer(
        args.instance,
        data_dir=args.data_dir,
        registry_path=args.heads_registry,
        deadline_seconds=args.deadline_seconds,
    )


def run_web_run_start(args: argparse.Namespace) -> int:
    return _emit_run(
        args,
        lambda: _ops_layer(args).run_start(
            args.ref,
            request_id=args.request_id,
            profile=args.profile,
            instruction=args.instruction,
        ),
        _run_lines,
    )


def run_web_run_review(args: argparse.Namespace) -> int:
    return _emit_run(
        args,
        lambda: _ops_layer(args).run_review(
            request_id=args.request_id,
            profile=args.profile,
            ref=args.ref,
            worker_run_id=args.worker_run,
        ),
        _review_lines,
    )


def run_web_run_state(args: argparse.Namespace) -> int:
    return _emit_run(args, lambda: _ops_layer(args).run_state(args.run_id), _run_lines)


def run_web_run_list(args: argparse.Namespace) -> int:
    return _emit_run(args, lambda: _ops_layer(args).run_list(args.ref), _list_lines)


def _emit_run(args: argparse.Namespace, operation, render) -> int:
    """Run one operation, print it, and turn a typed refusal into the group's exit status."""
    try:
        document = operation()
    except ReadError as exc:
        print(json.dumps({"error": exc.to_json()}), file=sys.stderr)
        return _RUN_EXIT_BY_CODE.get(exc.code, EXIT_BACKEND)
    if args.json:
        print(json.dumps(document, sort_keys=True))
        return 0
    for line in render(document):
        print(line)
    return 0


def _run_lines(document: dict[str, Any]):
    run = document["run"]
    state = document["state"]
    yield f"{run['run_id']} {run['role']} {run['ref']} ({run['project']}) on {run['profile']}"
    yield f"phase: {run['phase']}" + (
        f" — {run['unresolved_reason']}" if run.get("unresolved_reason") else ""
    )
    yield f"state: {state['value']} — {state['reason']}"
    exit_status = state["exit"]
    if exit_status["code"] is not None or exit_status["signal"] is not None:
        yield f"exit: code={exit_status['code']} signal={exit_status['signal']}"
    result = state["result"]
    yield f"result: {'published' if result['present'] else 'none'}" + (
        f", verdict {result['verdict']}" if result["verdict"] else ""
    )
    yield f"workspace: {run['workspace']}"
    yield f"run dir: {run['run_dir']}"
    yield f"journal: {run['journal_path']}"
    yield f"pid file: {run['pid_file']} (head pid {run['head_pid']})"
    yield f"read it back with: {document['reads']['task_snapshot']}"


def _review_lines(document: dict[str, Any]):
    if document.get("worker"):
        yield "worker:"
        for line in _run_lines(document["worker"]):
            yield f"  {line}"
    yield "review:"
    for line in _run_lines(document["review"]):
        yield f"  {line}"


def _list_lines(document: dict[str, Any]):
    yield f"{document['ref']}: {len(document['items'])} product run(s)"
    for item in document["items"]:
        run, state = item["run"], item["state"]
        over = "over" if state["ended"] else "open"
        yield f"  {run['run_id']} {run['role']} {run['profile']} {state['value']} ({over}) — {state['reason']}"
