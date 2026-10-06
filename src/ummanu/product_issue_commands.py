"""Public CLI for the Product and Issue board records."""

from __future__ import annotations

import argparse
import json
import os

from ummanu.board.backend import PRODUCT_ISSUE, board_client
from ummanu.product_issues import ISSUE_WRITE_ROLES, ProductIssueStore
from ummanu.runtime.paths import add_instance_argument
from ummanu.task_commands import _read_body, resolve_data_dir, run_task_command
from ummanu.tasks import TaskError


def _common(
    parser: argparse.ArgumentParser, *, write: bool = False, roles: tuple[str, ...] = ISSUE_WRITE_ROLES
) -> None:
    add_instance_argument(parser)
    parser.add_argument("--data-dir", default=os.environ.get("UMMANU_DATA_DIR"))
    if write:
        parser.add_argument("--role", required=True, choices=roles)
        # Who wrote it, never inferred from the role: every role head's environment names its actor
        # (`BOARD_ACTOR`), and a write that names none is refused as `actor_required`.
        parser.add_argument("--actor", default=os.environ.get("BOARD_ACTOR"))
        parser.add_argument("--request-id")


def add_product_issue_subcommands(subparsers) -> None:
    product = subparsers.add_parser("product", help="manage durable Product records")
    product_sub = product.add_subparsers(dest="product_command")
    create = product_sub.add_parser("create")
    _common(create, write=True, roles=("po",))
    create.add_argument("--id", required=True)
    create.add_argument("--project", action="append", required=True)
    create.add_argument("--title", required=True)
    create.add_argument("--description", default="")
    create.set_defaults(handler=run_product_create)
    listing = product_sub.add_parser("list")
    _common(listing)
    listing.set_defaults(handler=run_product_list)
    show = product_sub.add_parser("show")
    _common(show)
    show.add_argument("--id", required=True)
    show.set_defaults(handler=run_product_show)
    lanes = product_sub.add_parser(
        "reconcile-lanes",
        help="move Product and Issue rows into the lane of their product; plans unless --apply",
    )
    _common(lanes)
    lanes.add_argument(
        "--apply",
        action="store_true",
        help="perform the planned moves; without it the command writes nothing",
    )
    lanes.set_defaults(handler=run_product_reconcile_lanes)
    # The staged journal is shared by Product and Issue writes, so its repair commands live
    # once, under `product`.
    transaction = product_sub.add_parser("transaction", help="inspect and repair staged Product/Issue writes")
    transaction_sub = transaction.add_subparsers(dest="transaction_command")
    transaction_list = transaction_sub.add_parser("list")
    _common(transaction_list)
    transaction_list.set_defaults(handler=run_transaction_list)
    transaction_retry = transaction_sub.add_parser("retry")
    _common(transaction_retry)
    transaction_retry.add_argument("--request-id", required=True)
    transaction_retry.set_defaults(handler=run_transaction_retry)
    transaction_discard = transaction_sub.add_parser("discard")
    _common(transaction_discard)
    transaction_discard.add_argument("--request-id", required=True)
    transaction_discard.set_defaults(handler=run_transaction_discard)
    transaction_adopt = transaction_sub.add_parser("adopt")
    _common(transaction_adopt)
    transaction_adopt.add_argument("--path", required=True)
    transaction_adopt.set_defaults(handler=run_transaction_adopt)
    transaction.set_defaults(handler=_missing("product transaction subcommand required"))
    product.set_defaults(handler=_missing("product subcommand required"))

    issue = subparsers.add_parser("issue", help="manage durable Product issues")
    issue_sub = issue.add_subparsers(dest="issue_command")
    create = issue_sub.add_parser("create")
    _common(create, write=True)
    create.add_argument(
        "--product", default="", help="required for the PO; the observer files for its sprint's product"
    )
    create.add_argument("--kind", required=True, choices=("bug", "feature", "question", "improvement"))
    create.add_argument("--priority", required=True, choices=("P0", "P1", "P2", "P3"))
    create.add_argument("--title", required=True)
    create.add_argument("--description", default="")
    create.set_defaults(handler=run_issue_create)
    listing = issue_sub.add_parser("list")
    _common(listing)
    listing.add_argument("--product")
    states = listing.add_mutually_exclusive_group()
    states.add_argument("--closed", action="store_true", help="list only closed issues (default: open only)")
    states.add_argument("--all", action="store_true", help="list both open and closed issues")
    listing.set_defaults(handler=run_issue_list)
    show = issue_sub.add_parser("show")
    _common(show)
    show.add_argument("--ref", required=True)
    show.set_defaults(handler=run_issue_show)
    priority = issue_sub.add_parser("update-priority")
    _common(priority, write=True)
    priority.add_argument("--ref", required=True)
    priority.add_argument("--priority", required=True, choices=("P0", "P1", "P2", "P3"))
    priority.add_argument("--reason", required=True)
    priority.set_defaults(handler=run_issue_priority)
    append = issue_sub.add_parser("append", help="add a dated block after the description of an open issue")
    _common(append, write=True)
    append.add_argument("--ref", required=True)
    append.add_argument("--reason", required=True)
    append.add_argument("--body-file", required=True, help="the block to append, read as UTF-8")
    append.set_defaults(handler=run_issue_append)
    edit = issue_sub.add_parser("edit", help="PO only: replace exactly an open issue's description, with native audit")
    _common(edit, write=True)
    edit.add_argument("--ref", required=True)
    edit.add_argument("--reason", required=True, help="non-empty literal reason")
    content = edit.add_mutually_exclusive_group(required=True)
    content.add_argument("--description", help="full replacement description, verbatim")
    content.add_argument("--body-file", help="full replacement description, read as UTF-8")
    edit.set_defaults(handler=run_issue_edit)
    close = issue_sub.add_parser("close")
    _common(close, write=True)
    close.add_argument("--ref", required=True)
    close.add_argument("--reason", required=True, choices=("resolved", "invalid", "duplicate", "wont_do"))
    close.set_defaults(handler=run_issue_close)
    issue.set_defaults(handler=_missing("issue subcommand required"))


def _missing(message: str):
    def handler(_args: argparse.Namespace) -> int:
        print(json.dumps({"error": {"code": "usage", "message": message}}))
        return 2

    return handler


def _store(args: argparse.Namespace) -> ProductIssueStore:
    return ProductIssueStore(
        board_client(args.instance, serves=(PRODUCT_ISSUE,)),
        data_dir=resolve_data_dir(args),
        instance=args.instance,
    )


def _run(args: argparse.Namespace, callback) -> int:
    return run_task_command(lambda: callback(_store(args)))


def _actor(args: argparse.Namespace) -> str:
    actor = str(args.actor or "").strip()
    if not actor:
        raise TaskError("actor_required", "name the writer: pass --actor or set BOARD_ACTOR", 2)
    return actor


def _write(args: argparse.Namespace, callback) -> int:
    """A Product or Issue write, refused before the store is built when it names no actor."""
    def command() -> object:
        actor = _actor(args)
        return callback(_store(args), actor)

    return run_task_command(command)


def run_product_create(args):
    return _write(
        args,
        lambda store, actor: store.create_product(
            product_id=args.id,
            projects=args.project,
            title=args.title,
            description=args.description,
            actor=actor,
            request_id=args.request_id,
            role=args.role,
        ),
    )


def run_product_list(args):
    return _run(args, lambda store: store.list_products())


def run_product_show(args):
    return _run(args, lambda store: store.show_product(args.id))


def run_product_reconcile_lanes(args):
    return _run(args, lambda store: store.reconcile_lanes(apply=args.apply))


def run_issue_create(args):
    return _write(
        args,
        lambda store, actor: store.create_issue(
            product=args.product,
            issue_kind=args.kind,
            priority=args.priority,
            title=args.title,
            description=args.description,
            actor=actor,
            request_id=args.request_id,
            role=args.role,
        ),
    )


def run_issue_list(args):
    def listing(store):
        issues = store.list_issues(product=args.product, include_closed=args.closed or args.all)
        return [issue for issue in issues if issue["closed"]] if args.closed else issues

    return _run(args, listing)


def run_issue_show(args):
    return _run(args, lambda store: store.show_issue(args.ref))


def run_issue_priority(args):
    return _write(
        args,
        lambda store, actor: store.update_priority(
            reference=args.ref,
            priority=args.priority,
            reason=args.reason,
            actor=actor,
            request_id=args.request_id,
            role=args.role,
        ),
    )


def run_issue_append(args):
    return _write(
        args,
        lambda store, actor: store.append_description(
            reference=args.ref,
            body=_read_body(args.body_file),
            reason=args.reason,
            actor=actor,
            request_id=args.request_id,
            role=args.role,
        ),
    )


def run_issue_close(args):
    return _write(
        args,
        lambda store, actor: store.close_issue(
            reference=args.ref, reason=args.reason, actor=actor, request_id=args.request_id, role=args.role
        ),
    )


def run_issue_edit(args):
    def command():
        actor = _actor(args)
        description = _read_body(args.body_file) if args.body_file is not None else args.description
        return _store(args).edit_description(
            reference=args.ref,
            description=description,
            reason=args.reason,
            actor=actor,
            request_id=args.request_id,
            role=args.role,
        )

    return run_task_command(command)


def run_transaction_list(args):
    return _run(args, lambda store: store.list_transactions())


def run_transaction_retry(args):
    return _run(args, lambda store: store.retry_transaction(args.request_id))


def run_transaction_discard(args):
    return _run(args, lambda store: store.discard_transaction(args.request_id))


def run_transaction_adopt(args):
    return _run(args, lambda store: store.adopt_transaction(args.path))
