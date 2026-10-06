"""CLI handlers for the public task protocol."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable
from pathlib import Path

from ummanu.board.backend import card_client
from ummanu.board.models import CardState
from ummanu.board.owner_handover import OWNER, OWNER_ROLE
from ummanu.board.roles import BOARD_ROLES, CREATE_ROLES, EDIT_ROLES, Role
from ummanu.board.task_routing import (
    PO_EXECUTED_TYPES,
    BlockClassification,
    FamilyPreference,
    TaskComplexity,
    TaskDecision,
    TaskReview,
    TaskType,
)
from ummanu.cli_output import print_json
from ummanu.config import ConfigError, DataDirError, instance_data_dir, load_config
from ummanu.head_registry import HeadRegistryConfigError, installed_pair, missing_snapshot
from ummanu.po import PO_REQUEST_ENV, PO_SESSION_ENV
from ummanu.runtime.head import CODEX_LAUNCH_MODES
from ummanu.runtime.paths import add_instance_argument, resolve_instance_path
from ummanu.tasks import (
    BOARD_STORE_KIND,
    TaskError,
    TaskReader,
    TaskWriter,
    task_audit_for,
)


def _role_choices(roles: frozenset[Role]) -> tuple[str, ...]:
    """Project a canonical role subset onto argparse's string boundary."""
    return tuple(role.value for role in Role if role in roles)


def _add_instance_arg(parser) -> None:
    """Every task command names the installation it talks to, reads included.

    Reads used to skip this and fall through `_instance` to a home default resolved at import:
    neither ``--instance`` nor ``UMMANU_INSTANCE`` could move them, so a process bound to one
    installation still read the home one. On the appliance host that is the production board,
    reached from a cleared environment — the accident class of secretary-1026, and the reason a
    unit-suite `task show` could answer with live cards. The fallback now goes through the one
    resolver, which refuses a default live root that does not exist.
    """
    add_instance_argument(parser)


def _add_data_dir_args(parser) -> None:
    """Data dir is pinned to the installation, not to the process CWD.

    A worker runs the task protocol from its own project workspace; a CWD-relative
    default would drop the audit trail into that workspace and leave it dirty.
    """
    parser.add_argument("--data-dir", default=os.environ.get("UMMANU_DATA_DIR"))
    _add_instance_arg(parser)


def resolve_data_dir(args: argparse.Namespace) -> str:
    explicit = getattr(args, "data_dir", None)
    if explicit:
        return str(Path(explicit).expanduser())
    instance = resolve_instance_path(getattr(args, "instance", None))
    try:
        return str(instance_data_dir(instance))
    except DataDirError as exc:
        instance_file = instance / "instance.yaml" if instance.is_dir() else instance
        raise TaskError(
            "usage", f"cannot resolve data dir from {instance_file}: {exc}; pass --data-dir", 2
        ) from None


def _instance(args: argparse.Namespace) -> str:
    """One explicit board-routing source for every task command."""
    return str(resolve_instance_path(getattr(args, "instance", None)))


def add_task_subcommands(subparsers) -> None:
    task = subparsers.add_parser("task", help="read normalized cards from the Pipeline board")
    task_subcommands = task.add_subparsers(dest="task_command")
    task_list = task_subcommands.add_parser("list")
    task_list.add_argument(
        "--state",
        action="append",
        choices=tuple(state.value for state in CardState),
    )
    task_list.add_argument("--project")
    task_list.add_argument("--sprint")
    _add_instance_arg(task_list)
    task_list.set_defaults(handler=run_task_list)
    task_show = task_subcommands.add_parser("show")
    task_show.add_argument("--ref", required=True)
    _add_instance_arg(task_show)
    task_show.set_defaults(handler=run_task_show)
    repair_preview = task_subcommands.add_parser(
        "repair-references-preview", help="inspect duplicate task references without writing"
    )
    _add_data_dir_args(repair_preview)
    repair_preview.set_defaults(handler=run_task_repair_references_preview)
    repair_apply = task_subcommands.add_parser(
        "repair-references-apply", help="apply an exact previewed duplicate-reference repair"
    )
    _add_data_dir_args(repair_apply)
    repair_apply.add_argument("--role", required=True, choices=(Role.PO.value,))
    repair_apply.add_argument("--actor", default=os.environ.get("BOARD_ACTOR"))
    repair_apply.add_argument("--plan-id", required=True)
    repair_apply.add_argument("--task-id", action="append", required=True, type=int)
    repair_apply.add_argument("--request-id", required=True)
    repair_reason = repair_apply.add_mutually_exclusive_group(required=True)
    repair_reason.add_argument("--reason")
    repair_reason.add_argument("--reason-file")
    repair_apply.set_defaults(handler=run_task_repair_references_apply)
    task_create = task_subcommands.add_parser("create")
    task_create.add_argument(
        "--role", required=True, choices=_role_choices(CREATE_ROLES)
    )
    task_create.add_argument("--actor", default=os.environ.get("BOARD_ACTOR"))
    _add_data_dir_args(task_create)
    task_create.add_argument("--request-id")
    task_create.add_argument("--project", required=True)
    task_create.add_argument("--type", required=True, choices=tuple(kind.value for kind in TaskType))
    task_create.add_argument("--title", required=True)
    task_create.add_argument("--description", default="")
    task_create.add_argument("--body-file")
    task_create.add_argument("--ref", default="")
    task_create.add_argument("--state", choices=(CardState.ISSUES.value, CardState.READY.value), default=CardState.READY.value)
    task_create.add_argument("--blocked-by", default="")
    task_create.add_argument("--head", default="")
    task_create.add_argument("--review-head", default="")
    task_create.add_argument(
        "--review",
        choices=("", *(value.value for value in TaskReview)),
        default="",
        help="whether the card is reviewed; default required for code, skipped for every other kind "
        "(a --review-head the sprint does not pin is refused with skipped)",
    )
    task_create.add_argument(
        "--live-impact",
        action="store_true",
        help="research only: the card touches live systems; its description must declare "
        "'## Impact bounds' with '### Allowed', '### Forbidden' and '### Cleanup'",
    )
    task_create.add_argument(
        "--touches-production",
        default="",
        metavar="PROJECT|none",
        help="operation only, and required there: the registered project whose production the card "
        "touches, or none; the PO service runs it only when the sprint allows that production",
    )
    _add_wait_create_args(task_create)
    task_create.add_argument("--slug", default="")
    task_create.add_argument(
        "--base-branch",
        default="",
        help="the branch this card integrates into; only a branch the project declares",
    )
    task_create.add_argument(
        "--seed-ref",
        default="",
        help="git ref or object id the card's checkout starts from (a reslice successor's predecessor candidate)",
    )
    task_create.add_argument(
        "--supersedes", default="", help="reference of the predecessor card a --seed-ref inherits from"
    )
    task_create.add_argument(
        "--complexity", choices=tuple(value.value for value in TaskComplexity), default=TaskComplexity.STANDARD.value
    )
    task_create.add_argument("--family-preference", choices=tuple(value.value for value in FamilyPreference), default=FamilyPreference.AUTO.value)
    # No `choices`: `--codex-mode exec` names a launch shape the product removed, and it is
    # answered with that sentence in `_validate_codex_mode_for_create` rather than with argparse's
    # "invalid choice" over a flag whose only remaining value is the default anyway.
    task_create.add_argument("--codex-mode", "--codex-launch-mode", dest="codex_mode", default="")
    task_create.add_argument("--sprint", default="", help="link the card to an open sprint reference")
    task_create.add_argument("--priority", default="", help="rejected: tasks do not carry product priority")
    task_create.add_argument(
        "--budget-event",
        choices=("recreated_task", "hotfix"),
        default="",
        help="charge a sprint recreation or hotfix event",
    )
    _add_sprint_override_args(task_create)
    task_create.set_defaults(handler=run_task_create)
    for name, handler in (
        ("comment", run_task_comment),
        ("report", run_task_report),
        ("verdict", run_task_verdict),
        ("decide", run_task_decide),
        ("move", run_task_move),
        ("archive", run_task_archive),
    ):
        command = task_subcommands.add_parser(name)
        command.add_argument("--ref", required=True)
        command.add_argument(
            "--role",
            required=True,
            # The owner answers a card handed to it with a comment, and does nothing else here.
            choices=_role_choices(BOARD_ROLES) + ((OWNER_ROLE,) if name == "comment" else ()),
        )
        command.add_argument("--actor", default=os.environ.get("BOARD_ACTOR"))
        _add_data_dir_args(command)
        command.add_argument("--request-id")
        if name in {"decide", "move", "archive"}:
            content = command.add_mutually_exclusive_group()
            content.add_argument("--body-file", help="UTF-8 reason file")
            content.add_argument("--reason", help="literal reason, never a filename")
            content.add_argument("--reason-file", help="UTF-8 reason file")
        else:
            command.add_argument("--body-file")
        if name == "report":
            command.add_argument("--kind", required=True, choices=("done", "blocked"))
            # Required with `--kind blocked`, refused with `--kind done`; the writer holds both
            # rules so the protocol is the same from a script as from the CLI.
            command.add_argument("--classification", default="", choices=("", *(value.value for value in BlockClassification)))
        if name == "verdict":
            command.add_argument("--kind", required=True, choices=("green", "red"))
        if name == "decide":
            command.add_argument("--kind", required=True, choices=tuple(value.value for value in TaskDecision))
            command.add_argument(
                "--protocol-prerequisite",
                action="append",
                default=[],
                help="registry artifact required by a rework worker; repeat for multiple prerequisites",
            )
        if name == "move":
            # `--target` is the spelling the restore commands use for the same idea, and the one
            # operators reach for. Both names write the same dest, so neither is a second contract.
            command.add_argument(
                "--to",
                "--target",
                dest="to",
                required=True,
                choices=tuple(state.value for state in CardState),
            )
            # A card leaves Assessment on a decision somebody recorded with `task decide`, and
            # the move has to name it: the writer checks it against the card's audit.
            command.add_argument("--decision", default="", choices=("", *(value.value for value in TaskDecision)))
            _add_sprint_override_args(command)
        command.set_defaults(handler=handler)
    task_complete = task_subcommands.add_parser(
        "complete", help="PO only: complete an In progress decision or operation card it answered"
    )
    task_complete.add_argument("--ref", required=True)
    task_complete.add_argument("--role", required=True, choices=(Role.PO.value,))
    task_complete.add_argument("--actor", default=os.environ.get("BOARD_ACTOR"))
    _add_data_dir_args(task_complete)
    task_complete.add_argument("--request-id")
    task_complete.add_argument(
        "--kind", required=True, choices=tuple(kind.value for kind in TaskType if kind in PO_EXECUTED_TYPES)
    )
    task_complete.add_argument(
        "--body-file",
        required=True,
        help="decision: '## Decision' and '## How to verify'; operation: '## What was done' and "
        "'## How to verify', both non-empty",
    )
    task_complete.set_defaults(handler=run_task_complete)
    task_handover = task_subcommands.add_parser(
        "handover",
        help="PO only: hand an In progress decision or operation card to the owner; it stays In progress "
        "and waits for the owner's answer",
    )
    task_handover.add_argument("--ref", required=True)
    task_handover.add_argument("--role", required=True, choices=(Role.PO.value,))
    task_handover.add_argument("--actor", default=os.environ.get("BOARD_ACTOR"))
    _add_data_dir_args(task_handover)
    task_handover.add_argument("--request-id")
    task_handover.add_argument("--to", required=True, choices=(OWNER,))
    reason = task_handover.add_mutually_exclusive_group(required=True)
    reason.add_argument("--reason", help="what the owner has to decide or do")
    reason.add_argument("--reason-file")
    task_handover.set_defaults(handler=run_task_handover)
    answer = task_subcommands.add_parser("record-owner-answer", help="PO only: record a verbatim owner conversation answer to a current handover; settles attention before completion")
    answer.add_argument("--ref", required=True)
    answer.add_argument("--role", required=True, choices=(Role.PO.value,))
    answer.add_argument("--actor", default=os.environ.get("BOARD_ACTOR"))
    _add_data_dir_args(answer)
    answer.add_argument("--request-id")
    answer.add_argument("--handover-event", required=True, help="event_id returned by task handover; another epoch cannot be answered")
    answer.add_argument("--body-file", required=True, help="verbatim non-empty owner quotation; records no new standing grant")
    answer.set_defaults(handler=run_task_owner_answer)
    task_cancel = task_subcommands.add_parser(
        "cancel",
        help="PO, or the observer of its own sprint: cancel a pending wait card; the dispatcher delivers "
        "`cancelled` to its return addresses and Blocks it",
    )
    task_cancel.add_argument("--ref", required=True)
    task_cancel.add_argument("--role", required=True, choices=(Role.PO.value, Role.OBSERVER.value))
    task_cancel.add_argument("--actor", default=os.environ.get("BOARD_ACTOR"))
    _add_data_dir_args(task_cancel)
    task_cancel.add_argument("--request-id")
    cancel_reason = task_cancel.add_mutually_exclusive_group(required=True)
    cancel_reason.add_argument("--reason", help="why the wait is cancelled")
    cancel_reason.add_argument("--reason-file")
    task_cancel.set_defaults(handler=run_task_cancel)
    task_e2e = task_subcommands.add_parser(
        "e2e-budget",
        help="PO only: raise the e2e cap of a code card outside every sprint by the runs the owner granted, "
        "on the owner's comment on its budget decision card",
    )
    task_e2e.add_argument("--ref", required=True)
    # Every board role parses: the writer admits `po` only and answers the others with `role_forbidden`.
    task_e2e.add_argument("--role", required=True, choices=tuple(role.value for role in Role))
    task_e2e.add_argument("--actor", default=os.environ.get("BOARD_ACTOR"))
    _add_data_dir_args(task_e2e)
    task_e2e.add_argument("--request-id")
    task_e2e.add_argument(
        "--add",
        type=int,
        help="the runs the owner's comment raises by (`e2e budget: raise <N>`); optional, and refused unless it "
        "equals that N",
    )
    task_e2e.add_argument(
        "--authorized-by",
        required=True,
        help="the event id of the owner's comment on this card's e2e budget decision card, made after its handover",
    )
    task_e2e.set_defaults(handler=run_task_e2e_budget)
    task_edit = task_subcommands.add_parser("edit")
    task_edit.add_argument("--ref", required=True)
    task_edit.add_argument("--role", required=True, choices=_role_choices(EDIT_ROLES))
    task_edit.add_argument("--actor", default=os.environ.get("BOARD_ACTOR"))
    _add_data_dir_args(task_edit)
    task_edit.add_argument("--request-id")
    task_edit.add_argument("--title")
    task_edit.add_argument("--description")
    task_edit.add_argument("--body-file", help="file with the full replacement description")
    task_edit.add_argument("--head")
    task_edit.add_argument("--review-head")
    _add_sprint_override_args(task_edit)
    task_edit.set_defaults(handler=run_task_edit)
    task_claim = task_subcommands.add_parser("claim")
    task_claim.add_argument("--ref", required=True)
    task_claim.add_argument("--role", required=True, choices=(Role.DISPATCHER.value,))
    task_claim.add_argument("--actor", default=os.environ.get("BOARD_ACTOR"))
    _add_data_dir_args(task_claim)
    task_claim.add_argument("--request-id")
    task_claim.add_argument("--worker", required=True)
    task_claim.add_argument("--resolved-head", default="")
    task_claim.add_argument("--resolved-review-head", default="")
    task_claim.add_argument("--slug", default="")
    task_claim.add_argument("--base-branch", default="")
    task_claim.add_argument("--cap", type=int, default=3)
    task_claim.set_defaults(handler=run_task_claim)
    reconcile_audit = task_subcommands.add_parser("reconcile-audit")
    _add_data_dir_args(reconcile_audit)
    reconcile_audit.set_defaults(handler=run_task_reconcile_audit)
    verify_audit = task_subcommands.add_parser("verify-audit")
    _add_data_dir_args(verify_audit)
    verify_audit.set_defaults(handler=run_task_verify_audit)
    task.set_defaults(handler=not_implemented_task)


def _add_wait_create_args(parser) -> None:
    """A wait card's create flags (docs/PROTOCOLS.md, "Wait cards"); every other kind refuses them."""
    group = parser.add_argument_group("wait card (--type wait)")
    group.add_argument(
        "--wait-run",
        default="",
        metavar="OWNER/REPO|URL",
        help="target: a GitHub Actions run, as owner/repo with --wait-run-id, or as the run's URL",
    )
    group.add_argument("--wait-run-id", default="", help="the run id when --wait-run names owner/repo")
    group.add_argument("--wait-card", default="", metavar="REF", help="target: another card reaching a state")
    group.add_argument(
        "--wait-states", default="", metavar="STATE[,STATE]", help="the states --wait-card waits for, e.g. done,blocked"
    )
    group.add_argument("--wait-until", default="", metavar="UTC", help="target: a point in time, ISO-8601 with its zone")
    group.add_argument(
        "--wait-deadline",
        default="",
        metavar="UTC|DURATION",
        help="required: when the wait ends as deadline_passed; an ISO-8601 UTC time or a duration from now (90m, 2h, 1d)",
    )
    group.add_argument(
        "--wait-return",
        action="append",
        default=[],
        metavar="ADDRESS",
        help="repeatable: observer (a sprint's card only), po-session:<id> or dependents; required, except "
        "inside a PO turn with --role po, where none means the session of that turn",
    )
    group.add_argument(
        "--wait-transient-window",
        default="",
        metavar="DURATION",
        help="how long consecutive transient source errors may last before source_unreachable (default 30m)",
    )


def _wait_args(args: argparse.Namespace) -> dict[str, object] | None:
    wait = {
        "run": args.wait_run,
        "run_id": args.wait_run_id,
        "card": args.wait_card,
        "states": args.wait_states,
        "until": args.wait_until,
        "deadline": args.wait_deadline,
        "returns": list(args.wait_return or ()),
        "transient_window": args.wait_transient_window,
    }
    return wait if any(wait.values()) else None


def _add_sprint_override_args(parser) -> None:
    parser.add_argument(
        "--sprint-override", action="store_true", help="PO only: bypass an open sprint's single-writer guard"
    )
    parser.add_argument("--sprint-override-reason-file", help="required PO override reason file")


def not_implemented_task(args: argparse.Namespace) -> int:
    print(json.dumps({"error": {"code": "usage", "message": "task subcommand required"}}))
    return 2


def run_task_list(args: argparse.Namespace) -> int:
    return _run_task_read(
        args,
        lambda reader: reader.list(states=set(args.state or ()), project=args.project, sprint=args.sprint),
    )


def run_task_show(args: argparse.Namespace) -> int:
    return _run_task_read(args, lambda reader: reader.show(args.ref))


def run_task_repair_references_preview(args: argparse.Namespace) -> int:
    from ummanu.board.reference_repair import preview_reference_repair

    return run_task_command(
        lambda: preview_reference_repair(
            TaskWriter(card_client(_instance(args)), data_dir=resolve_data_dir(args))
        )
    )


def run_task_repair_references_apply(args: argparse.Namespace) -> int:
    from ummanu.board.reference_repair import apply_reference_repair

    return run_task_command(
        lambda: apply_reference_repair(
            TaskWriter(card_client(_instance(args)), data_dir=resolve_data_dir(args)),
            plan_id=args.plan_id,
            task_ids=args.task_id,
            reason=_reason_body(args),
            request_id=args.request_id,
            actor=args.actor or args.role,
            role=args.role,
        )
    )


def _run_task_read(args: argparse.Namespace, operation: Callable[[TaskReader], object]) -> int:
    return run_task_command(lambda: operation(TaskReader(card_client(_instance(args)))))


def run_task_command(
    operation: Callable[[], object], *, exit_code: Callable[[object], int] | None = None
) -> int:
    try:
        result = operation()
    except TaskError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message}}), file=os.sys.stderr)
        return exc.exit_code
    print_json(result, compact=True)
    return exit_code(result) if exit_code is not None else 0


def _read_body(path: str | None) -> str:
    if path is None:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise TaskError("usage", f"cannot read body file: {exc}", 2) from None


def _reason_body(args: argparse.Namespace) -> str:
    literal = getattr(args, "reason", None)
    paths = [getattr(args, name, None) for name in ("body_file", "reason_file")]
    if sum(value is not None for value in [literal, *paths]) > 1:
        raise TaskError("usage", "choose one body/reason source", 2)
    return literal if literal is not None else _read_body(next((path for path in paths if path is not None), None))


def _run_task_write(args: argparse.Namespace, operation: Callable[[TaskWriter, str, str], object]) -> int:
    def command() -> object:
        body = _reason_body(args)
        writer = TaskWriter(card_client(_instance(args)), data_dir=resolve_data_dir(args))
        return operation(writer, body, args.actor or args.role)

    return run_task_command(command)


def run_task_comment(args: argparse.Namespace) -> int:
    return _run_task_write(
        args,
        lambda writer, body, actor: writer.comment(
            role=args.role, actor=actor, reference=args.ref, body=body, request_id=args.request_id
        ),
    )


def run_task_create(args: argparse.Namespace) -> int:
    def command() -> object:
        _validate_codex_mode_for_create(args)
        description = _read_body(args.body_file) if args.body_file else args.description
        writer = TaskWriter(card_client(_instance(args)), data_dir=resolve_data_dir(args))
        return writer.create(
            role=args.role,
            actor=args.actor or args.role,
            project=args.project,
            task_type=args.type,
            title=args.title,
            description=description,
            target=args.state,
            reference=args.ref,
            blocked_by=args.blocked_by,
            head=args.head,
            review_head=args.review_head,
            slug=args.slug,
            base_branch=args.base_branch,
            seed_ref=args.seed_ref,
            supersedes=args.supersedes,
            complexity=args.complexity,
            family_preference=args.family_preference,
            codex_launch_mode=args.codex_mode,
            sprint=args.sprint,
            priority=args.priority,
            budget_event=args.budget_event,
            sprint_override=args.sprint_override,
            sprint_override_reason=_read_body(args.sprint_override_reason_file),
            review=args.review,
            live_impact=args.live_impact,
            touches_production=args.touches_production,
            wait=_wait_args(args),
            origin=_po_turn_origin(args.role),
            request_id=args.request_id,
        )

    return run_task_command(command)


def _po_turn_session() -> str:
    """The PO session whose turn runs this command (`UMMANU_PO_SESSION`), or `""` outside one.

    `task complete` and `task handover` record it (secretary-1792): the proof that a delegated card's
    origin session already has its result. It permits nothing, and no flag sets it.
    """
    return os.environ.get(PO_SESSION_ENV, "").strip()


def _po_turn_origin(role: str) -> dict[str, str] | None:
    """The PO turn a `--role po` create runs in, from the turn's environment; None anywhere else.

    `PoRunner.session_environment` sets both variables in every PO turn. No flag sets them, and no
    other role's create reads them: an origin is only ever the PO turn itself (secretary-1792).
    """
    if role != Role.PO.value:
        return None
    session = os.environ.get(PO_SESSION_ENV, "").strip()
    if not session:
        return None
    return {"session": session, "request": os.environ.get(PO_REQUEST_ENV, "").strip()}


def run_task_edit(args: argparse.Namespace) -> int:
    def command() -> object:
        description = _read_body(args.body_file) if args.body_file else args.description
        writer = TaskWriter(card_client(_instance(args)), data_dir=resolve_data_dir(args))
        return writer.edit(
            role=args.role,
            actor=args.actor or args.role,
            reference=args.ref,
            title=args.title,
            description=description,
            head=args.head,
            review_head=args.review_head,
            sprint_override=args.sprint_override,
            sprint_override_reason=_read_body(args.sprint_override_reason_file),
            request_id=args.request_id,
        )

    return run_task_command(command)


def run_task_report(args: argparse.Namespace) -> int:
    return _run_task_write(
        args,
        lambda writer, body, actor: writer.report(
            role=args.role,
            actor=actor,
            reference=args.ref,
            kind=args.kind,
            body=body,
            classification=args.classification,
            request_id=args.request_id,
        ),
    )


def run_task_cancel(args: argparse.Namespace) -> int:
    return _run_task_write(
        args,
        lambda writer, body, actor: writer.cancel(
            role=args.role,
            actor=actor,
            reference=args.ref,
            reason=args.reason if args.reason is not None else body,
            request_id=args.request_id,
        ),
    )


def run_task_e2e_budget(args: argparse.Namespace) -> int:
    return _run_task_write(
        args,
        lambda writer, _body, actor: writer.raise_e2e_cap(
            role=args.role,
            actor=actor,
            reference=args.ref,
            add=args.add,
            authorized_by=args.authorized_by,
            request_id=args.request_id,
        ),
    )


def run_task_complete(args: argparse.Namespace) -> int:
    return _run_task_write(
        args,
        lambda writer, body, actor: writer.complete(
            role=args.role,
            actor=actor,
            reference=args.ref,
            kind=args.kind,
            body=body,
            request_id=args.request_id,
            po_session=_po_turn_session(),
        ),
    )


def run_task_owner_answer(args: argparse.Namespace) -> int:
    return _run_task_write(args, lambda writer, body, actor: writer.record_owner_answer(
        role=args.role, actor=actor, reference=args.ref, handover_event=args.handover_event,
        quotation=body, request_id=args.request_id))


def run_task_handover(args: argparse.Namespace) -> int:
    return _run_task_write(
        args,
        lambda writer, body, actor: writer.handover(
            role=args.role,
            actor=actor,
            reference=args.ref,
            to=args.to,
            reason=args.reason if args.reason is not None else body,
            request_id=args.request_id,
            po_session=_po_turn_session(),
        ),
    )


def run_task_verdict(args: argparse.Namespace) -> int:
    return _run_task_write(
        args,
        lambda writer, body, actor: writer.verdict(
            role=args.role,
            actor=actor,
            reference=args.ref,
            kind=args.kind,
            body=body,
            request_id=args.request_id,
        ),
    )


def run_task_decide(args: argparse.Namespace) -> int:
    return _run_task_write(
        args,
        lambda writer, body, actor: writer.decide(
            role=args.role,
            actor=actor,
            reference=args.ref,
            kind=args.kind,
            body=body,
            protocol_prerequisites=args.protocol_prerequisite,
            request_id=args.request_id,
        ),
    )


def run_task_move(args: argparse.Namespace) -> int:
    return _run_task_write(
        args,
        lambda writer, body, actor: writer.move(
            role=args.role,
            actor=actor,
            reference=args.ref,
            target=args.to,
            reason=body,
            decision=args.decision,
            sprint_override=args.sprint_override,
            sprint_override_reason=_read_body(args.sprint_override_reason_file),
            request_id=args.request_id,
        ),
    )


def run_task_archive(args: argparse.Namespace) -> int:
    return _run_task_write(
        args,
        lambda writer, body, actor: writer.archive(
            role=args.role, actor=actor, reference=args.ref, reason=body, request_id=args.request_id
        ),
    )


def run_task_claim(args: argparse.Namespace) -> int:
    return _run_task_write(
        args,
        lambda writer, body, actor: writer.claim(
            role=args.role,
            actor=actor,
            reference=args.ref,
            worker=args.worker,
            resolved_head=args.resolved_head,
            resolved_review_head=args.resolved_review_head,
            slug=args.slug,
            base_branch=args.base_branch,
            cap=args.cap,
            request_id=args.request_id,
        ),
    )


def run_task_reconcile_audit(args: argparse.Namespace) -> int:
    def command() -> object:
        repaired, unresolved = TaskWriter(
            card_client(_instance(args)),
            data_dir=resolve_data_dir(args),
        ).reconcile()
        return {"repaired": repaired, "unresolved": unresolved}

    return run_task_command(command, exit_code=lambda result: 0 if result["unresolved"] == 0 else 1)


def run_task_verify_audit(args: argparse.Namespace) -> int:
    """The audit of the store this installation serves cards from, not of a directory.

    The status answers "is anything staged and unsettled", so it has to be asked of the store that
    holds the claims: `requests` (`docs/BOARD_STORE.md` §7.3). Asked of the file journal beside a
    PostgreSQL client it reported a clean installation it had never read -- the same false green
    secretary-1614 was declared stalled by. The exit contract is unchanged: 0 when nothing is staged, 1 when something is, and
    the named `TaskError` status of any command that cannot reach its backend.
    """

    def command() -> object:
        client = card_client(_instance(args))
        status = dict(task_audit_for(client, resolve_data_dir(args)).status())
        status["backend"] = BOARD_STORE_KIND
        return status

    return run_task_command(command, exit_code=lambda result: 0 if result["ok"] else 1)


def _validate_codex_mode_for_create(args: argparse.Namespace) -> None:
    if not args.codex_mode:
        return
    mode = str(args.codex_mode).strip()
    if mode not in CODEX_LAUNCH_MODES:
        # Refused before the registry is even read, and long before the board is touched: there is
        # one Codex launch shape and it is interactive, so `exec` is not a mode this rejects for
        # being unavailable here — it is a mode that no longer exists anywhere.
        known = ", ".join(sorted(CODEX_LAUNCH_MODES))
        raise TaskError(
            "validation",
            f"--codex-mode {mode!r} is not a Codex launch mode; Codex heads launch through the "
            f"interactive TUI only (known: {known})",
            2,
        )
    heads = _load_heads(Path(args.instance))
    head = args.head or str(heads.get("role_defaults", {}).get("new_card") or "codex")
    profiles = heads.get("profiles", {})
    profile = profiles.get(head) if isinstance(profiles, dict) else None
    if not isinstance(profile, dict):
        raise TaskError(
            "validation", f"--codex-mode requires a known Codex worker head; {head!r} is not defined", 2
        )
    adapter = str(profile.get("adapter") or "")
    if adapter != "codex":
        detail = adapter or "unknown"
        raise TaskError("validation", f"--codex-mode requires a Codex worker head; {head!r} uses {detail}", 2)


def _load_heads(instance: Path) -> dict:
    try:
        heads_file = installed_pair(instance).snapshot
        if not heads_file.exists():
            raise missing_snapshot(instance, heads_file)
        loaded = load_config(heads_file)
    except (ConfigError, HeadRegistryConfigError) as exc:
        raise TaskError("validation", f"cannot validate --codex-mode: {exc}", 2) from None
    if not isinstance(loaded, dict):
        raise TaskError(
            "validation", "cannot validate --codex-mode: heads config has an unsupported shape", 2
        )
    return loaded
