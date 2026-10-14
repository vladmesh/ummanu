"""Root command facts shared by full inspection and selected-family composition.

The existing registrars own their nested arguments and handlers. Root names and help live
here once, so a help/error-only invocation need not import those registrars' domains.
"""

from __future__ import annotations

ROOT_COMMANDS = {
    "doctor-record": (("doctor-record", {"help": "atomically record one bounded real doctor attempt"}),),
    "dispatcher": (("dispatcher", {"help": "run the task and sprint dispatcher"}),),
    "pause": (
        ("pause", {"help": "stop the pipeline: drain (no new claims) or freeze (heads stopped too)"}),
        ("resume", {"help": "clear the pause and put back what a freeze stopped"}),
        ("pause-status", {"help": "read the production dispatcher's pause state"}),
        (
            "pause-scope",
            {
                "help": "read what a pause would reach before issuing one: the flag, the open sprints and cards inside the pipeline-wide scope, and the heads a drain would leave running"
            },
        ),
    ),
    "head-status": (
        (
            "head-status",
            {"help": "read whether the dispatcher's heads in a workspace are alive, and what proved it"},
        ),
    ),
    "check": (
        ("check", {"help": "run the declared local profile, a module or a test; show its broad receipt"}),
    ),
    "web-read": (("web-read", {"help": "read the transport-independent system, task and event snapshots"}),),
    "web-run": (("web-run", {"help": "start, review and read a product run without a web transport"}),),
    "web-serve": (
        (
            "web-serve",
            {"help": "serve the local dashboard and card pages over the web-read and web-run operations"},
        ),
    ),
    "web-front": (
        ("web-front", {"help": "the password-guarded TLS front that publishes the loopback web transport"}),
    ),
    "po-serve": (
        (
            "po-serve",
            {
                "help": "run the PO head service: the one owner of PO turns, fed by a durable queue over a local socket"
            },
        ),
    ),
    "po": (("po", {"help": "act on the PO head's sessions through the PO service"}),),
    "owner-events": (("owner-events", {"help": "read the owner events behind the web's bell"}),),
    "upgrade": (
        ("upgrade", {"help": "pull the current product version and re-materialize this installation"}),
    ),
    "install": (
        ("install", {"help": "install or resume from a private instance remote"}),
        ("recover", {"help": "recover a lost installation from a private instance remote"}),
    ),
    "sprint": (("sprint", {"help": "manage sprint entities on the dedicated sprint board"}),),
    "restore": (
        ("bootstrap", {"help": "bootstrap a host or create an empty ummanu-data target"}),
        ("restore", {"help": "restore ummanu-data from an archive"}),
        ("restore-postgres", {"help": "restore a PostgreSQL full archive into an empty local board store"}),
        ("restore-board", {"help": "import the normalized board into an empty backend"}),
        ("restore-reconcile", {"help": "verify live managed reconcile after restore"}),
    ),
    "task": (("task", {"help": "read normalized cards from the Pipeline board"}),),
    "product": (
        ("product", {"help": "manage durable Product records"}),
        ("issue", {"help": "manage durable Product issues"}),
    ),
    "secret": (("secret", {"help": "manage the encrypted secret store in the instance repo"}),),
}

_ROOT_OPTIONS = {name: options for commands in ROOT_COMMANDS.values() for name, options in commands}


def add_command_parser(subparsers, name):
    return subparsers.add_parser(name, **_ROOT_OPTIONS[name])


def add_family(subparsers, family, selected):
    """Register every root in order, expanding only the selected family (None means all)."""
    if selected is not None and selected not in {name for name, _ in ROOT_COMMANDS[family]}:
        for name, _ in ROOT_COMMANDS[family]:
            add_command_parser(subparsers, name)
        return
    if family == "doctor-record":
        from ummanu.infra.doctor_record import add_subcommand as register
    elif family == "dispatcher":
        from ummanu.dispatch.commands import add_dispatcher_subcommands as register
    elif family == "pause":
        from ummanu.dispatch.commands import add_pause_commands as register
    elif family == "head-status":
        from ummanu.dispatch.commands import add_head_status_command as register
    elif family == "check":
        from ummanu.check_commands import add_check_subcommands as register
    elif family == "web-read":
        from ummanu.webproto.commands import add_web_read_subcommands as register
    elif family == "web-run":
        from ummanu.webproto.commands import add_web_run_subcommands as register
    elif family == "web-serve":
        from ummanu.web.commands import add_web_serve_subcommands as register
    elif family == "web-front":
        from ummanu.webfront.commands import add_web_front_subcommands as register
    elif family == "po-serve":
        from ummanu.po.service import add_po_serve_subcommands as register
    elif family == "po":
        from ummanu.po.commands import add_po_subcommands as register
    elif family == "owner-events":
        from ummanu.board.owner_event_commands import add_owner_event_subcommands as register
    elif family == "upgrade":
        from ummanu.upgrade import add_upgrade_command as register
    elif family == "install":
        from ummanu.installation.commands import add_install_commands as register
    elif family == "sprint":
        from ummanu.sprint_commands import add_sprint_subcommands as register
    elif family == "restore":
        from ummanu.restore_commands import add_restore_subcommands as register
    elif family == "task":
        from ummanu.task_commands import add_task_subcommands as register
    elif family == "product":
        from ummanu.product_issue_commands import add_product_issue_subcommands as register
    elif family == "secret":
        from ummanu.secret_commands import add_secret_subcommands as register
    else:
        raise ValueError(f"unknown command family: {family}")
    register(subparsers)
