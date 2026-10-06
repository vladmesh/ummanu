"""`ummanu owner-events list`: the owner's bell from a terminal, read-only (secretary-1770).

The same read the web's list page makes (`OwnerEventStore.events`, open `needs_owner` events first,
then newest first), through the board store's read role: this command marks nothing read.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ummanu.board.owner_events import OwnerEventError, OwnerEventStore
from ummanu.runtime.paths import add_instance_argument
from ummanu.runtime.paths import instance_dir as normalize_instance_dir

EXIT_UNAVAILABLE = 1


def add_owner_event_subcommands(subparsers) -> None:
    command = subparsers.add_parser("owner-events", help="read the owner events behind the web's bell")
    verbs = command.add_subparsers(dest="owner_events_command", required=True)
    listing = verbs.add_parser("list", help="list owner events: what needs the owner first, then newest first")
    add_instance_argument(listing, help="path to an instance dir or instance.yaml")
    listing.add_argument("--unread", action="store_true", help="only the events nobody has read")
    listing.add_argument("--limit", type=int, default=100, help="the most events to print (default 100)")
    listing.add_argument("--json", action="store_true", help="one JSON document instead of lines")
    listing.set_defaults(handler=run_owner_events_list)


def run_owner_events_list(args: argparse.Namespace) -> int:
    try:
        store = OwnerEventStore.for_instance(normalize_instance_dir(Path(args.instance)), role="read")
        events = store.events(unread_only=bool(args.unread), limit=max(1, int(args.limit)))
        unread = store.unread_count()
    except (OwnerEventError, RuntimeError) as exc:
        if args.json:
            print(json.dumps({"error": {"code": "backend_unavailable", "message": str(exc)}}, sort_keys=True))
        else:
            print(f"ummanu owner-events: {exc}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    if args.json:
        print(json.dumps({"unread": unread, "events": [event.to_json() for event in events]}, sort_keys=True))
        return 0
    print(f"{unread} unread")
    for event in events:
        mark = "*" if event.unread else " "
        subject = event.subject_ref or "-"
        print(
            f"{mark} #{event.id} {event.created_at.isoformat()} {event.event_class} {event.kind} {subject}"
        )
        for line in event.text.splitlines() or [""]:
            print(f"    {line}")
    return 0


__all__ = ["add_owner_event_subcommands", "run_owner_events_list"]
