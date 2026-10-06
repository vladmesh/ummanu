"""`ummanu po rename`: a PO session's title from a terminal or from a PO turn (secretary-1782).

The write goes where the web's does: `PoLayer.po_rename` over the PO service's socket
(`PoServiceClient.rename_session`), which runs the store's one rule (`ummanu.po.store.session_title`).
Inside a PO turn the session needs no flag: the PO service gives every turn `UMMANU_PO_SESSION`,
and that is the default here, as it is for `sprint create --po-session`. The statuses are `web-run`'s:
a refused title is 2, an unknown session 2, a service that did not answer 1.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from ummanu.po import PO_SESSION_ENV
from ummanu.runtime.paths import add_instance_argument
from ummanu.webproto.commands import _RUN_EXIT_BY_CODE, EXIT_BACKEND, EXIT_VALIDATION
from ummanu.webproto.errors import ReadError


def add_po_subcommands(subparsers) -> None:
    command = subparsers.add_parser("po", help="act on the PO head's sessions through the PO service")
    verbs = command.add_subparsers(dest="po_command", required=True)
    rename = verbs.add_parser(
        "rename", help="set a PO session's title; an empty title clears it, a repeat changes nothing"
    )
    add_instance_argument(rename, help="path to an instance dir or instance.yaml")
    rename.add_argument(
        "--data-dir",
        default=os.environ.get("UMMANU_DATA_DIR"),
        help="override the instance's configured data directory",
    )
    rename.add_argument("--title", required=True, help="the new title, one line of at most 120 characters")
    rename.add_argument(
        "--session",
        default=None,
        help=f"the PO session to rename; defaults to ${PO_SESSION_ENV}, set in every PO turn",
    )
    rename.set_defaults(handler=run_po_rename)


def run_po_rename(args: argparse.Namespace) -> int:
    from ummanu.webproto.po_ops import PoLayer

    session_id = (args.session or os.environ.get(PO_SESSION_ENV) or "").strip()
    if not session_id:
        message = f"name the session with --session; ${PO_SESSION_ENV} is not set outside a PO turn"
        print(json.dumps({"error": {"code": "validation", "message": message}}), file=sys.stderr)
        return EXIT_VALIDATION
    try:
        document = PoLayer(args.instance, data_dir=args.data_dir).po_rename(
            session_id=session_id, title=args.title
        )
    except ReadError as exc:
        print(json.dumps({"error": exc.to_json()}), file=sys.stderr)
        return _RUN_EXIT_BY_CODE.get(exc.code, EXIT_BACKEND)
    print(json.dumps(document, sort_keys=True, separators=(",", ":")))
    return 0


__all__ = ["add_po_subcommands", "run_po_rename"]
