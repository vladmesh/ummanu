"""CLI handlers for sprint entities.

Six of these are clients rather than implementations. `sprint list` and `sprint status` do not read
a board or decide what a sprint's state is; `sprint comment` does not decide what a comment is or
when a repeat is a repeat; `sprint comment-delivery` decides nothing about delivery at all; and
`sprint close` decides nothing about what a close is -- which decisions it needs, what it writes and
in what order, when a repeat resumes it -- while `sprint close-result` reads that back. All six
call the named operations of :mod:`ummanu.webproto.sprint_reads` and
:mod:`ummanu.webproto.sprint_ops`, print the document those return, and map a typed protocol code
onto the exit status `ummanu web-read` already uses. Until secretary-1573 the two reads built a
`SprintReader` of their own beside the layer, which is how one surface could answer a question
differently from the other; what is left here is argument parsing, output and that mapping.

The remaining writes are unchanged: they go to `SprintWriter`, which owns every rule about what a
sprint may become.
"""

from __future__ import annotations

import argparse
import json
import os
import uuid
from collections.abc import Callable
from pathlib import Path

from ummanu.board.backend import SPRINT, board_client
from ummanu.board.roles import Role
from ummanu.config import ConfigError, load_config
from ummanu.po import PO_SESSION_ENV
from ummanu.sprint_observer import observer_choice
from ummanu.sprints import (
    BUDGET_RECORDED_EVENT_TYPES,
    SprintReader,
    SprintWriter,
    public_current_task,
)
from ummanu.task_commands import _add_data_dir_args, _read_body, resolve_data_dir
from ummanu.tasks import TaskError, admit_role
from ummanu.webproto.commands import (
    _EXIT_BY_CODE,
    _RUN_EXIT_BY_CODE,
    EXIT_BACKEND,
    EXIT_PENDING,
)
from ummanu.webproto.errors import OperationPending, ReadError
from ummanu.webproto.sprint_ops import (
    SPRINT_CLOSE_ROLES,
    SPRINT_COMMENT_ROLES,
    SprintOperationLayer,
)
from ummanu.webproto.sprint_reads import SprintReadLayer


def add_sprint_subcommands(subparsers) -> None:
    sprint = subparsers.add_parser("sprint", help="manage sprint entities on the dedicated sprint board")
    commands = sprint.add_subparsers(dest="sprint_command")
    listed = commands.add_parser("list")
    listed.add_argument("--status", action="append", choices=("open", "closed", "stopped"))
    # `_read` names an installation like every other sprint command. Without this the namespace
    # carries no `instance` at all and the command dies with an AttributeError before it reads
    # anything — the same gap the task reads had, arriving as a crash rather than as the wrong board.
    _add_data_dir_args(listed)
    listed.set_defaults(handler=run_list)
    shown = commands.add_parser("show")
    shown.add_argument("--ref", required=True)
    _add_data_dir_args(shown)
    shown.set_defaults(handler=run_show)
    status = commands.add_parser("status")
    status.add_argument("--ref", required=True)
    _add_data_dir_args(status)
    status.set_defaults(handler=run_status)
    created = commands.add_parser("create")
    created.add_argument("--role", required=True, choices=("po", "steward"))
    created.add_argument("--actor", default=os.environ.get("BOARD_ACTOR"))
    _add_data_dir_args(created)
    created.add_argument("--request-id")
    created.add_argument("--goal", required=True)
    created.add_argument("--definition-of-done", default="")
    created.add_argument("--dod-file")
    created.add_argument(
        "--local-run-exceptions-file",
        help="JSON list of {project, argv, rationale} exceptions declared at creation; default []",
    )
    created.add_argument("--owner-decisions-file", help="PO only: JSON list of {id, scope, kind, value, quotation}; see record-owner-decisions")
    created.add_argument("--repository", action="append", default=[])
    created.add_argument("--product", required=True, help="product id the sprint belongs to")
    created.add_argument(
        "--issue",
        action="append",
        required=True,
        help="open issue of that product the sprint serves; repeat for more",
    )
    created.add_argument(
        "--project",
        action="append",
        required=True,
        help="registered project the sprint reserves; repeat for more",
    )
    created.add_argument("--ref", default="")
    _add_observer_argument(created)
    _add_executor_arguments(created)
    _add_po_channel_arguments(created)
    created.add_argument(
        "--e2e-budget",
        type=_runs(0),
        default=None,
        help="e2e runs this sprint may dispatch (0 or more); default 3. Each run pays for stands, and a "
        "spent budget is raised only on the owner's word (`sprint e2e-budget`)",
    )
    created.set_defaults(handler=run_create)
    delivery = commands.add_parser(
        "comment-delivery",
        help="what happened to one saved sprint comment, as far as durable state can say",
    )
    delivery.add_argument("--ref", required=True)
    delivery.add_argument(
        "--comment-id",
        required=True,
        help="the identifier `sprint comment` answered with",
    )
    _add_data_dir_args(delivery)
    delivery.set_defaults(handler=run_comment_delivery)
    close_result = commands.add_parser(
        "close-result",
        help="what one close decided and what it left behind, as far as durable state can say",
    )
    close_result.add_argument("--ref", required=True)
    close_result.add_argument(
        "--event-id", required=True, help="the identifier `sprint close` answered with"
    )
    _add_data_dir_args(close_result)
    close_result.set_defaults(handler=run_close_result)
    for name, handler, roles in (
        # The roles the writer admits, taken from the layer rather than spelled a second time:
        # a command offering a role the operation refuses would be offering a dead end.
        ("comment", run_comment, SPRINT_COMMENT_ROLES),
        ("current-task", run_current_task, ("po", "dispatcher", "observer", "steward")),
        ("budget", run_budget, ("po", "dispatcher", "steward")),
        ("resume", run_resume, ("po", "dispatcher", "observer", "steward")),
        ("reopen", run_reopen, ("po",)),
        ("close", run_close, SPRINT_CLOSE_ROLES),
    ):
        command = commands.add_parser(name)
        command.add_argument("--ref", required=True)
        command.add_argument("--role", required=True, choices=roles)
        command.add_argument("--actor", default=os.environ.get("BOARD_ACTOR"))
        _add_data_dir_args(command)
        command.add_argument("--request-id")
        if name == "comment":
            command.add_argument("--body-file", required=True)
        elif name == "current-task":
            command.add_argument("--task", required=True)
        elif name == "budget":
            command.add_argument("--type", required=True, choices=BUDGET_RECORDED_EVENT_TYPES)
        elif name == "resume":
            command.add_argument("--body-file", required=True)
            command.add_argument("--delivery-id")
            command.add_argument("--through-event")
        elif name == "reopen":
            _add_observer_argument(command)
        elif name == "close":
            command.add_argument(
                "--decisions-file",
                help="YAML file stating the verdict on every declared issue and the disposition "
                "of every card that is not done",
            )
            command.add_argument(
                "--reason",
                required=True,
                help="why the owner is closing this sprint",
            )
            command.add_argument(
                "--closeout-file",
                required=True,
                help="markdown file stating what became of the work, what is left unfinished and "
                "the owner's decision; the close writes it into state/knowledge and links it to "
                "the sprint. Closing is not a claim that the Definition of Done was reached",
            )
        command.set_defaults(handler=handler)
    allowed = commands.add_parser(
        "allow-production",
        help="record the PO's decision that this sprint's operations may touch one more production",
    )
    allowed.add_argument("--ref", required=True)
    # Every board role parses: the writer admits `po` only and answers the others, the observer
    # first, with `role_forbidden`, and `po` as the observer with `role_masquerade` (`admit_role`).
    allowed.add_argument("--role", required=True, choices=tuple(role.value for role in Role))
    allowed.add_argument("--actor", default=os.environ.get("BOARD_ACTOR"))
    allowed.add_argument("--project", required=True, help="the registered project whose production to allow")
    allowed.add_argument("--reason", required=True, help="why the PO may allow it (the owner's rule it follows)")
    _add_data_dir_args(allowed)
    allowed.add_argument("--request-id")
    allowed.set_defaults(handler=run_allow_production)
    raised = commands.add_parser(
        "e2e-budget",
        help="PO only: raise the sprint's e2e run budget by the runs the owner granted, on the owner's "
        "comment on the budget decision card",
    )
    raised.add_argument("--ref", required=True)
    # Every board role parses: the writer admits `po` only and answers the others with `role_forbidden`.
    raised.add_argument("--role", required=True, choices=tuple(role.value for role in Role))
    raised.add_argument("--actor", default=os.environ.get("BOARD_ACTOR"))
    # A plain int: the writer refuses fewer than 1, after the role check every verb makes first.
    raised.add_argument(
        "--add",
        type=int,
        help="the runs the owner's comment raises by (`e2e budget: raise <N>`); optional, and refused unless it "
        "equals that N",
    )
    raised.add_argument(
        "--authorized-by",
        required=True,
        help="the event id of the owner's comment on this sprint's e2e budget decision card, made after "
        "its handover, whose one answer line is `e2e budget: raise <N>`",
    )
    _add_data_dir_args(raised)
    raised.add_argument("--request-id")
    raised.set_defaults(handler=run_e2e_budget)
    decisions = commands.add_parser("record-owner-decisions", help="PO only: append verbatim quoted owner answers; stable IDs make grants idempotent. Read back with sprint show")
    decisions.add_argument("--ref", required=True)
    decisions.add_argument("--role", required=True, choices=tuple(role.value for role in Role))
    decisions.add_argument("--actor", default=os.environ.get("BOARD_ACTOR"))
    decisions.add_argument("--decisions-file", required=True, help="JSON list: id, scope, kind, value, quotation. production/project/bool; e2e_grant/sprint/positive runs; e2e_refusal/sprint/no_more_e2e; advance_consent/scope/{action,max_uses}. Later answers supersede earlier answers")
    decisions.add_argument("--request-id")
    _add_data_dir_args(decisions)
    decisions.set_defaults(handler=run_owner_decisions)
    sprint.set_defaults(handler=not_implemented)


def _runs(minimum: int) -> Callable[[str], int]:
    """An argparse type: a whole number of e2e runs, `minimum` or more."""

    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"a whole number of runs, not {text!r}") from None
        if value < minimum:
            raise argparse.ArgumentTypeError(f"{minimum} or more runs, not {value}")
        return value

    return parse


def _add_observer_argument(command: argparse.ArgumentParser) -> None:
    """The sprint's one observer decision, stated by the operator or not at all.

    Required, because there is nothing to fall back to: `none` and a concrete head profile are the
    two answers, and neither is more of a default than the other.
    """
    command.add_argument(
        "--observer",
        required=True,
        help="head profile that observes this sprint, or 'none' to run without one",
    )


def _add_executor_arguments(command: argparse.ArgumentParser) -> None:
    """The two optional executor pins, each stated once or not at all.

    Optional in the full sense: an omitted option is not a hidden default. The sprint then pins no
    profile for that role and its observer chooses one per card under the current rules, which is
    exactly how every sprint opened before these options existed keeps working.

    There is no `none` to pass. It is not a spelling this contract has, and it is refused rather
    than read as the absent state.
    """
    for role in ("worker", "reviewer"):
        command.add_argument(
            f"--{role}",
            help=f"head profile every card of this sprint runs its {role} on; "
            "omit it to pin no profile and leave the choice to the observer",
        )


def _add_po_channel_arguments(command: argparse.ArgumentParser) -> None:
    """The PO session that opens the sprint, and the productions its operations may touch.

    Inside a PO turn the session needs no flag: the PO service gives every turn
    `UMMANU_PO_SESSION`, and that is the default here. Outside one (a steward create) there is
    none, and the sprint records none. Productions are never defaulted: each is named explicitly.
    """
    command.add_argument(
        "--po-session",
        default=os.environ.get(PO_SESSION_ENV) or None,
        help=f"the open PO session creating this sprint; defaults to ${PO_SESSION_ENV}, set in every PO turn",
    )
    command.add_argument(
        "--allow-production",
        action="append",
        default=[],
        help="registered project whose production this sprint's operations may touch; repeat for "
        "more. The default is none",
    )


def not_implemented(args: argparse.Namespace) -> int:
    print(json.dumps({"error": {"code": "usage", "message": "sprint subcommand required"}}))
    return 2


def _read(
    args: argparse.Namespace,
    operation: Callable[[SprintReader], object],
    *,
    data_dir: str | None = None,
    thresholds: dict | None = None,
) -> int:
    try:
        result = operation(
            SprintReader(
                board_client(args.instance, serves=(SPRINT,)), data_dir=data_dir, thresholds=thresholds
            )
        )
    except TaskError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message}}), file=os.sys.stderr)
        return exc.exit_code
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


def _write(args: argparse.Namespace, operation: Callable[[SprintWriter], object]) -> int:
    try:
        result = operation(
            SprintWriter(
                board_client(args.instance, serves=(SPRINT,)),
                data_dir=resolve_data_dir(args),
                thresholds=_thresholds(args),
                instance=getattr(args, "instance", None) or None,
            )
        )
    except TaskError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message}}), file=os.sys.stderr)
        return exc.exit_code
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


def _operation(args: argparse.Namespace, operation: Callable[[SprintReadLayer], object]) -> int:
    """Run one protocol operation, print its document, and map a typed refusal to an exit status.

    The whole of this command group's knowledge about reading a sprint. The codes are the layer's
    own and the statuses are the ones `ummanu web-read` maps them to, taken from that module
    rather than restated here, so the two surfaces cannot drift apart.
    """
    explicit = getattr(args, "data_dir", None)
    layer = SprintReadLayer(
        args.instance, data_dir=Path(explicit).expanduser() if explicit else None
    )
    return _answer(lambda: operation(layer), _EXIT_BY_CODE)


def _sprint_operation(
    args: argparse.Namespace,
    operation: Callable[[SprintOperationLayer], object],
    *,
    pending: int | None = None,
) -> int:
    """Run one mutating protocol operation, and answer exactly as the read client does.

    A second builder rather than a second rule: the operation layer is what decides everything about
    a comment, and this command's whole knowledge of it is which operation to call, what to print,
    and the typed-code-to-exit-status table `_operation` already uses.
    """
    explicit = getattr(args, "data_dir", None)
    layer = SprintOperationLayer(
        args.instance, data_dir=Path(explicit).expanduser() if explicit else None
    )
    # The mutation table and not the read one: `owner_conflict` is a refusal about the state of the
    # world -- the sprint is closed -- and `web-run` already gives it its own status so a script can
    # tell it from a malformed request. It is also the status this command answered a closed sprint
    # with before it became a client, so nothing an operator scripts against moves.
    return _answer(lambda: operation(layer), _RUN_EXIT_BY_CODE, pending=pending)


def _answer(
    call: Callable[[], object], statuses: dict[str, int], *, pending: int | None = None
) -> int:
    """One protocol answer on stdout, or one typed refusal on stderr with its exit status.

    `pending` is the status a half-finished operation answers with, for the commands that had one
    before they became clients. A close has always told an operator that its transaction is
    repairable with its own status, and a script that branches on it keeps working: the typed
    failure carries the same fact (`OperationPending`, with the request id to repeat), and this is
    the one place that turns it back into the number.
    """
    try:
        document = call()
    except OperationPending as exc:
        if pending is None:
            raise
        print(json.dumps({"error": exc.to_json()}), file=os.sys.stderr)
        return pending
    except ReadError as exc:
        print(json.dumps({"error": exc.to_json()}), file=os.sys.stderr)
        return statuses.get(exc.code, EXIT_BACKEND)
    print(json.dumps(_status_first(document), separators=(",", ":")))
    return 0


def _status_first(document: object) -> object:
    """The document with its keys sorted at every level, except a top-level `status` goes first.

    `sprint status` names the sprint's status before anything else on its one line of output, and the
    document stays one JSON object the observer parses. Nothing is added or dropped here.
    """

    def ordered(value: object) -> object:
        if isinstance(value, dict):
            return {key: ordered(value[key]) for key in sorted(value)}
        if isinstance(value, list):
            return [ordered(item) for item in value]
        return value

    result = ordered(document)
    if isinstance(result, dict) and "status" in result:
        result = {"status": result["status"], **{k: v for k, v in result.items() if k != "status"}}
    return result


def _public_show(sprint: dict) -> dict:
    """The sprint row as `sprint show` prints it: a closed sprint names no current card."""
    current = public_current_task(str(sprint.get("status") or ""), sprint.get("current_task"))
    return {**sprint, "current_task": current}


def run_list(args: argparse.Namespace) -> int:
    return _operation(args, lambda layer: layer.sprint_list(statuses=args.status or ()))


def run_show(args: argparse.Namespace) -> int:
    return _read(
        args,
        lambda reader: _public_show(reader.show(args.ref)),
        data_dir=resolve_data_dir(args),
        thresholds=_thresholds(args),
    )


def run_status(args: argparse.Namespace) -> int:
    return _operation(args, lambda layer: layer.sprint_state(args.ref))


def _thresholds(args: argparse.Namespace) -> dict | None:
    raw_instance = getattr(args, "instance", "") or ""
    if not raw_instance:
        return None
    instance = Path(raw_instance)
    instance_file = instance / "instance.yaml" if instance.is_dir() else instance
    try:
        config = load_config(instance_file)
    except ConfigError:
        return None
    return config.get("sprint_budget") if isinstance(config, dict) else None


def run_create(args: argparse.Namespace) -> int:
    try:
        standing_decisions = json.loads(_read_body(args.owner_decisions_file)) if args.owner_decisions_file else []
        from ummanu.board.owner_decisions import parse_decisions
        parse_decisions(standing_decisions)
        definition_of_done = _read_body(args.dod_file) if args.dod_file else args.definition_of_done
        local_run_exceptions = (
            json.loads(_read_body(args.local_run_exceptions_file)) if args.local_run_exceptions_file else []
        )
        if not isinstance(local_run_exceptions, list):
            raise ValueError("local_run_exceptions must be a list")  # noqa: TRY004 - uniform JSON value validation
    except ValueError as exc:
        print(json.dumps({"error": {"code": "validation", "message": f"invalid sprint create JSON: {exc}"}}), file=os.sys.stderr)
        return 2
    except TaskError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message}}), file=os.sys.stderr)
        return exc.exit_code
    return _write(
        args,
        lambda writer: writer.create(
            role=args.role,
            actor=args.actor or args.role,
            goal=args.goal,
            definition_of_done=definition_of_done,
            repositories=args.repository,
            product=args.product,
            issues=args.issue,
            projects=args.project,
            reference=args.ref,
            request_id=args.request_id,
            observer=observer_choice(args.observer),
            worker=args.worker,
            reviewer=args.reviewer,
            po_session=args.po_session,
            allowed_productions=args.allow_production,
            e2e_budget=args.e2e_budget,
            local_run_exceptions=local_run_exceptions,
            standing_decisions=standing_decisions,
        ),
    )


def run_owner_decisions(args: argparse.Namespace) -> int:
    try:
        admit_role(args.role, args.actor or args.role, {"po"})
    except TaskError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message}}), file=os.sys.stderr)
        return exc.exit_code
    try:
        from ummanu.board.owner_decisions import parse_decisions
        entries = parse_decisions(json.loads(_read_body(args.decisions_file)))
    except (ValueError, TaskError) as exc:
        print(json.dumps({"error": {"code": "validation", "message": str(exc)}}), file=os.sys.stderr)
        return 2
    return _write(args, lambda writer: writer.record_owner_decisions(
        role=args.role, actor=args.actor or args.role, reference=args.ref,
        entries=entries, request_id=args.request_id,
    ))


def run_allow_production(args: argparse.Namespace) -> int:
    return _write(
        args,
        lambda writer: writer.allow_production(
            role=args.role,
            actor=args.actor or args.role,
            reference=args.ref,
            project=args.project,
            reason=args.reason,
            request_id=args.request_id,
        ),
    )


def run_e2e_budget(args: argparse.Namespace) -> int:
    return _write(
        args,
        lambda writer: writer.raise_e2e_budget(
            role=args.role,
            actor=args.actor or args.role,
            reference=args.ref,
            add=args.add,
            authorized_by=args.authorized_by,
            request_id=args.request_id,
        ),
    )


def run_comment(args: argparse.Namespace) -> int:
    """`ummanu sprint comment`, as a client of the named operation and nothing more.

    The body is read before the layer is called so that an unreadable file is this command's own
    usage refusal rather than a protocol code; the request id is minted here when the operator gave
    none, which is a convenience of the command and not a rule -- the operation requires one, and a
    person retrying a comment types the same `--request-id` to get the same one back.
    """
    try:
        body = _read_body(args.body_file)
    except TaskError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message}}), file=os.sys.stderr)
        return exc.exit_code
    return _sprint_operation(
        args,
        lambda layer: layer.sprint_comment(
            request_id=args.request_id or str(uuid.uuid4()),
            actor=args.actor or args.role,
            reference=args.ref,
            body=body,
            role=args.role,
        ),
    )


def run_comment_delivery(args: argparse.Namespace) -> int:
    return _operation(
        args, lambda layer: layer.sprint_comment_delivery(args.ref, args.comment_id)
    )


def run_current_task(args: argparse.Namespace) -> int:
    return _write(
        args,
        lambda writer: writer.set_current_task(
            role=args.role,
            actor=args.actor or args.role,
            reference=args.ref,
            task_reference=args.task,
            request_id=args.request_id,
        ),
    )


def run_budget(args: argparse.Namespace) -> int:
    return _write(
        args,
        lambda writer: writer.record_budget(
            role=args.role,
            actor=args.actor or args.role,
            reference=args.ref,
            event_type=args.type,
            request_id=args.request_id,
        ),
    )


def run_resume(args: argparse.Namespace) -> int:
    try:
        entry = json.loads(_read_body(args.body_file))
    except (TaskError, ValueError):
        print(
            json.dumps({"error": {"code": "validation", "message": "resume file must contain JSON"}}),
            file=os.sys.stderr,
        )
        return 2
    return _write(
        args,
        lambda writer: writer.resume(
            role=args.role,
            actor=args.actor or args.role,
            reference=args.ref,
            entry=entry,
            request_id=args.request_id,
            delivery_id=args.delivery_id or "",
            through_event=args.through_event or "",
        ),
    )


def run_reopen(args: argparse.Namespace) -> int:
    return _write(
        args,
        lambda writer: writer.reopen(
            role=args.role,
            actor=args.actor or args.role,
            reference=args.ref,
            request_id=args.request_id,
            observer=observer_choice(args.observer),
        ),
    )


def run_close(args: argparse.Namespace) -> int:
    """`ummanu sprint close`, as a client of the named operation and nothing more.

    Everything a close *is* -- which decisions it needs, what it writes and in what order, when a
    repeat resumes it -- belongs to `SprintWriter.close` and to the operation that calls it. What is
    left here is reading the two files, minting a request id when the operator gave none, and
    turning a typed refusal back into the exit status this command has always answered with.
    """
    from ummanu.sprint_close import parse_close_decisions

    try:
        decisions = parse_close_decisions(_read_body(args.decisions_file)) if args.decisions_file else None
        closeout = _read_body(args.closeout_file)
    except TaskError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message}}), file=os.sys.stderr)
        return exc.exit_code
    return _sprint_operation(
        args,
        lambda layer: layer.sprint_close(
            request_id=args.request_id or str(uuid.uuid4()),
            actor=args.actor or args.role,
            reference=args.ref,
            reason=args.reason,
            closeout=closeout,
            decisions=decisions,
            role=args.role,
        ),
        pending=EXIT_PENDING,
    )


def run_close_result(args: argparse.Namespace) -> int:
    return _operation(args, lambda layer: layer.sprint_close_result(args.ref, args.event_id))
