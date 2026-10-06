"""`transition from-secretary --instance <dir> (--plan | --apply | --rollback)`, and after it
`--repair-scope-owners [--apply]` (`scope_owners`) and `--repair-products [--apply]` (`products`).

Only the parser lives here; the transition itself is imported when the command runs.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..runtime import paths
from .names import OLD


def add_transition_subcommands(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    group = subparsers.add_parser(
        "transition", help="one-shot installation transitions (docs/RENAME.md §T3)"
    )
    verbs = group.add_subparsers(dest="transition_command")
    command = verbs.add_parser(
        f"from-{OLD.package}",
        help=f"move this installation from {OLD.package} to its new name; --plan first",
    )
    paths.add_instance_argument(command, help="the instance repository (it keeps its name)")
    mode = command.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="print every step and precondition; write nothing")
    mode.add_argument("--apply", action="store_true", help="run, resuming at the first unfinished step")
    mode.add_argument("--rollback", action="store_true", help="undo what the journal says was done")
    command.add_argument(
        "--repair-scope-owners",
        action="store_true",
        help="after the transition: list settled heads' scope owners still naming the old unit; "
        "--apply renames them",
    )
    command.add_argument(
        "--repair-products",
        action="store_true",
        help="after the transition: show the open cutover canary Product still linking the old project; "
        "--apply archives it",
    )
    command.add_argument("--through", default="", help="stop after this step (the bootstrap stops after 'move')")
    command.add_argument("--sprint", default="", help="the sprint the observer prepared (default: found by marker)")
    command.add_argument(
        "--allow-extra-merge",
        action="append",
        default=[],
        metavar="SHA",
        help="a merge between the checkout and origin/main besides the rename's, checked by hand",
    )
    command.add_argument("--home", default="", help=argparse.SUPPRESS)
    command.set_defaults(handler=run_transition)
    group.set_defaults(handler=lambda args: (group.print_help(), 2)[1])


def run_transition(args: argparse.Namespace) -> int:
    from . import engine, products, scope_owners, steps
    from .context import Context, Journal, Layout, Runner, TransitionError

    if args.repair_scope_owners and args.repair_products:
        return _usage("--repair-scope-owners and --repair-products run one at a time")
    repairs = (("--repair-scope-owners", args.repair_scope_owners), ("--repair-products", args.repair_products))
    for flag, repair in repairs:
        if repair and (args.plan or args.rollback):
            return _usage(f"{flag} lists without --apply and repairs with it")
    if not (args.repair_scope_owners or args.repair_products or args.plan or args.apply or args.rollback):
        return _usage("one of --plan, --apply, --rollback, --repair-scope-owners or --repair-products is required")
    instance = Path(args.instance).expanduser()
    if instance.name == "instance.yaml":
        instance = instance.parent
    layout = Layout(home=Path(args.home).expanduser() if args.home else Path.home(), instance=instance.resolve())
    try:
        if args.repair_scope_owners:
            return scope_owners.repair_scope_owners(layout, apply=args.apply)
        if args.repair_products:
            return products.repair_products(layout, apply=args.apply)
        ctx = Context(
            layout=layout,
            runner=Runner(),
            journal=Journal.load(layout.journal_path),
            sprint=args.sprint,
            allow_extra_merges=tuple(args.allow_extra_merge),
            board=steps.BoardOps(),
        )
        if args.plan:
            return engine.plan(ctx)
        if args.rollback:
            return engine.run_rollback(ctx)
        return engine.apply(ctx, through=args.through)
    except TransitionError as exc:
        print(json.dumps({"error": {"code": "transition_refused", "message": str(exc)}}), file=sys.stderr)
        return 3


def _usage(message: str) -> int:
    print(f"transition from-{OLD.package}: {message}", file=sys.stderr)
    return 2


__all__ = ["add_transition_subcommands", "run_transition"]
