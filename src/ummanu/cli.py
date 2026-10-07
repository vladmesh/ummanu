from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ummanu import state_repo
from ummanu.backup import create_backups, verify_backup
from ummanu.board.owner_event_commands import add_owner_event_subcommands
from ummanu.check_commands import add_check_subcommands
from ummanu.checkpoint import (
    SnapshotExporter,
    checkpoint_snapshot,
    render_checkpoint_lines,
    rpo_problem,
    snapshot_foreign_commits,
)
from ummanu.config import (
    DataDirError,
    fallback_errors,
    instance_data_dir,
    load_config,
    validate,
    validate_instance,
)
from ummanu.data import (
    PIPELINE_STATE_DIR,
    export_all,
    export_artifacts,
    export_board,
    export_memory,
    export_runs,
    export_transcripts,
    init_layout,
)
from ummanu.dispatch.commands import (
    add_dispatcher_subcommands,
    add_head_status_command,
    add_pause_commands,
)
from ummanu.dispatch.pause import ProductionPause
from ummanu.dispatch.runtime_provenance import ProductionRuntime, RuntimeProvenance
from ummanu.dispatch.tick_telemetry import tick_p95_finding, tick_statistics
from ummanu.gate import run_gate
from ummanu.head_health import (
    PROBE_BROKEN,
    PROBE_TIMED_OUT,
    HeadReadiness,
)
from ummanu.head_registry import HeadRegistryConfigError, installed_heads, read_source
from ummanu.host import (
    FIXTURE_UNIT_FILES_DIR,
    KINDS,
    CollectResult,
    FixtureHostSource,
    KindDiff,
    LiveHostSource,
    assess_unit_runtime,
    build_doctor_expectations,
    build_plan,
    foreign_units,
    inventory,
    load_managed_manifest,
    plan_changes,
)
from ummanu.host_apply import resolve_installed_packaged, resolve_runtime_owner
from ummanu.host_commands import add_reconcile_subcommands
from ummanu.infra.checkpoint_run import load_checkpoint_state, run_checkpoint
from ummanu.infra.doctor_findings import accepted, active_findings, apply_acceptance
from ummanu.infra.host_space_policy import ROOT_FREE_MIN_BYTES
from ummanu.infra.recovery_inventory import collect_recovery_inventory
from ummanu.installation import add_install_commands
from ummanu.knowledge_write import (
    KnowledgeError,
    KnowledgeValidationError,
    list_knowledge_documents,
    write_knowledge_directory,
    write_knowledge_document,
)
from ummanu.memory_journal import verify_memory_journal
from ummanu.memory_write import (
    MemoryExportPublishError,
    MemoryLockError,
    MemoryPermissionError,
    MemoryValidationError,
    commit_memory_proposal,
    propose_memory_fact,
    supersede_memory_fact,
)
from ummanu.onboarding import project_add, render_artifact
from ummanu.po.commands import add_po_subcommands
from ummanu.po.service import add_po_serve_subcommands
from ummanu.product_issue_commands import add_product_issue_subcommands
from ummanu.provision import apply_provision_result, render_result, start_provision
from ummanu.restore import RestoreError, _target, restore_findings
from ummanu.restore_commands import add_restore_subcommands, run_memory_reindex
from ummanu.role_skills import add_role_skills_subcommands
from ummanu.runtime import interactive_workspace
from ummanu.runtime.codex_preflight import (
    CODEX_HOME_DATA_DIR,
    CODEX_HOME_ENV,
    CODEX_HOME_PROFILE,
    CodexHomeLoginMissing,
    data_dir_codex_home,
    resolve_codex_home,
)
from ummanu.runtime.paths import MissingDefaultInstance, add_instance_argument, resolve_instance_argument
from ummanu.secret_commands import add_secret_subcommands
from ummanu.secret_store import store_findings as _secret_store_findings
from ummanu.session import run_shell
from ummanu.sprint_commands import add_sprint_subcommands
from ummanu.state_repo import StateRepoError
from ummanu.status import collect_status, disk_free_bytes
from ummanu.task_commands import add_task_subcommands
from ummanu.transition.commands import add_transition_subcommands
from ummanu.upgrade import add_upgrade_command
from ummanu.web.commands import add_web_serve_subcommands
from ummanu.webfront.commands import add_web_front_subcommands
from ummanu.webproto.commands import add_web_read_subcommands, add_web_run_subcommands

NOT_IMPLEMENTED = "not implemented in Phase 1 skeleton"
MEMORY_EXIT_VALIDATION = 2
MEMORY_EXIT_PERMISSION = 3
MEMORY_EXIT_LOCKED = 4
_PROVENANCE_UNSET = object()


@dataclass
class DoctorInspection:
    """Single read-only evaluation shared by doctor renderers."""

    findings: list[dict[str, object]]
    unavailable: bool
    restore: list[str]
    dispatcher: list[str]
    checkpoint: list[str]
    secret_store: list[str]
    resource_probes: list[HeadReadiness]
    recovery: dict[str, object]
    expected: object | None = None
    collected: CollectResult | None = None
    diffs: dict[str, KindDiff] | None = None
    #: The board store's schema against this build (`board_schema_inspection`).
    board_schema: dict[str, object] | None = None


class StructuredArgumentParser(argparse.ArgumentParser):
    """Keep public command validation in the same JSON envelope as handlers."""

    def __init__(self, *args, **kwargs):
        kwargs["allow_abbrev"] = False
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> None:
        self.exit(2, json.dumps({"error": {"code": "usage", "message": message}}) + "\n")


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "automations":
        # The background agents own their argv, help and output: hand it over untouched, before
        # this parser can claim `--help` or reject an agent's own flags.
        return run_automations(argv[1:])
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 2
    try:
        resolve_instance_argument(args)
        return handler(args)
    except MissingDefaultInstance as exc:
        print(f"ummanu: {exc}", file=sys.stderr)
        return 2


def run_automations(argv: list[str]) -> int:
    """`ummanu automations <agent> <cmd> [args]`: the background agents' one entry.

    Imported on demand, so no other command pays for the agents' board wiring; the composition
    root answers with the agents' own exit protocol (0/100/101/102) and output.
    """
    from ummanu.automations.composition import main as automations_main

    return automations_main(list(argv))


def build_parser() -> argparse.ArgumentParser:
    from ummanu.infra.doctor_record import add_subcommand as add_doctor_record

    parser = StructuredArgumentParser(prog="ummanu")
    subparsers = parser.add_subparsers(dest="command")
    add_doctor_record(subparsers)
    add_dispatcher_subcommands(subparsers)
    add_pause_commands(subparsers)
    add_head_status_command(subparsers)
    add_check_subcommands(subparsers)
    add_web_read_subcommands(subparsers)
    add_web_run_subcommands(subparsers)
    add_web_serve_subcommands(subparsers)
    add_web_front_subcommands(subparsers)
    add_po_serve_subcommands(subparsers)
    add_po_subcommands(subparsers)
    add_owner_event_subcommands(subparsers)
    automations = subparsers.add_parser(
        "automations",
        add_help=False,
        help="run a background agent's helpers: automations <agent> <cmd> [args]",
    )
    automations.add_argument("argv", nargs=argparse.REMAINDER)
    automations.set_defaults(handler=lambda args: run_automations(args.argv))

    doctor = subparsers.add_parser("doctor", help="inspect an instance without changing the host")
    doctor.add_argument("--dry-run", action="store_true", help=argparse.SUPPRESS)
    _add_env_instance(doctor)
    doctor.add_argument(
        "--offline",
        action="store_true",
        help="check config and data without inspecting the host",
    )
    doctor.add_argument("--host", action="store_true", help=argparse.SUPPRESS)
    doctor.add_argument(
        "--host-fixture",
        metavar="DIR",
        help="compare against a fixture host dir instead of the live host",
    )
    doctor.add_argument(
        "--strict",
        action="store_true",
        help="treat migration warnings as findings",
    )
    doctor.add_argument("--json", action="store_true", help="print structured findings")
    doctor.set_defaults(handler=run_doctor)

    status = subparsers.add_parser("status", help="show the current installation state")
    _add_env_instance(status)
    status.add_argument("--json", action="store_true", help="print the stable JSON status schema")
    status.add_argument("--offline", action="store_true", help="do not inspect the live host")
    status.add_argument("--host-fixture", metavar="DIR", help="read a fixture host inventory")
    status.set_defaults(handler=run_status)

    config = subparsers.add_parser("config", help="check the live root's configuration")
    config_subcommands = config.add_subparsers(dest="config_command")
    config_check = config_subcommands.add_parser(
        "check",
        help="validate the live root's schema and guard its exported files for the old name, without Git",
    )
    _add_instance(config_check, help="the live root: an instance dir or its instance.yaml")
    config_check.set_defaults(handler=run_config_check)
    config.set_defaults(handler=not_implemented("config"))

    add_upgrade_command(subparsers)
    add_transition_subcommands(subparsers)
    add_install_commands(subparsers)
    add_role_skills_subcommands(subparsers)
    add_sprint_subcommands(subparsers)

    reconcile = subparsers.add_parser("reconcile", help="render or apply the host plan")
    reconcile_subcommands = reconcile.add_subparsers(dest="reconcile_command")
    add_reconcile_subcommands(reconcile_subcommands)
    reconcile.set_defaults(handler=not_implemented("reconcile"))

    data = subparsers.add_parser("data", help="manage the ummanu-data layout")
    data_subcommands = data.add_subparsers(dest="data_command")

    data_init = data_subcommands.add_parser("init", help="create ummanu-data and its manifest")
    _add_instance(
        data_init,
        data_dir=True,
        help="path to an instance dir or instance.yaml",
        data_dir_help="override instance.yaml data_dir",
    )
    data_init.set_defaults(handler=run_data_init)

    export = data_subcommands.add_parser(
        "export",
        help="write board, memory, runs and transcript exports into ummanu-data",
    )
    _add_instance(export, data_dir=True)
    export.add_argument(
        "--copy-transcripts",
        action="store_true",
        help="copy transcript files in addition to writing the inventory",
    )
    export.set_defaults(handler=run_data_export)

    export_board_command = data_subcommands.add_parser(
        "export-board",
        help="write ummanu-data/board normalized cards",
    )
    _add_instance(export_board_command, data_dir=True)
    export_board_command.set_defaults(handler=run_export_board)

    export_memory_command = data_subcommands.add_parser(
        "export-memory",
        help="write ummanu-data/memory facts and export.ndjson",
    )
    _add_instance(export_memory_command, data_dir=True)
    export_memory_command.set_defaults(handler=run_export_memory)

    export_runs_command = data_subcommands.add_parser(
        "export-runs",
        help="write ummanu-data/runs state exports",
    )
    _add_instance(export_runs_command, data_dir=True)
    export_runs_command.add_argument(
        "--state-dir",
        default=str(Path.home() / "orca" / "workspaces" / "ummanu" / "pipeline" / "state" / "pipeline"),
    )
    export_runs_command.set_defaults(handler=run_export_runs)

    export_transcripts_command = data_subcommands.add_parser(
        "export-transcripts",
        help="write ummanu-data/transcripts inventory",
    )
    _add_instance(export_transcripts_command, data_dir=True)
    export_transcripts_command.add_argument("--root", action="append", dest="roots")
    export_transcripts_command.add_argument("--copy", action="store_true")
    export_transcripts_command.set_defaults(handler=run_export_transcripts)

    export_artifacts_command = data_subcommands.add_parser(
        "export-artifacts",
        help="write ummanu-data/artifacts inventory and task docs",
    )
    _add_instance(export_artifacts_command, data_dir=True)
    export_artifacts_command.set_defaults(handler=run_export_artifacts)

    snapshot_command = data_subcommands.add_parser(
        "snapshot",
        help="run one snapshot-exporter window into an explicit bare repository; never pushes",
    )
    _add_instance(snapshot_command, data_dir=True)
    snapshot_command.add_argument(
        "--snapshot-repo",
        required=True,
        help="the bare snapshot repository to commit into; created when absent",
    )
    snapshot_command.add_argument(
        "--seed-from",
        metavar="LEGACY_INSTANCE_DIR",
        help=(
            "cutover only: instead of a window, fetch the legacy checkpoint's branch tip (depth 1) "
            "into the empty snapshot repository, so the next window commits on top of it"
        ),
    )
    snapshot_command.add_argument("--state-dir", default=str(PIPELINE_STATE_DIR))
    snapshot_command.set_defaults(handler=run_data_snapshot)
    data.set_defaults(handler=not_implemented("data"))

    backup = subparsers.add_parser("backup", help="create or verify backups")
    backup_subcommands = backup.add_subparsers(dest="backup_command")

    backup_create = backup_subcommands.add_parser("create")
    _add_instance(backup_create, data_dir=True)
    backup_create.add_argument(
        "--kind",
        choices=("full", "core", "both"),
        default="full",
        help="archive kind to create",
    )
    backup_create.add_argument(
        "--copy-transcripts",
        action="store_true",
        help="copy transcript files in addition to writing the inventory",
    )
    backup_create.add_argument(
        "--no-copy-transcripts",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    backup_create.set_defaults(handler=run_backup_create)

    backup_verify = backup_subcommands.add_parser("verify")
    backup_verify.add_argument("archive")
    backup_verify.add_argument(
        "--strict",
        action="store_true",
        help="treat warnings as findings",
    )
    backup_verify.set_defaults(handler=run_backup_verify)
    backup.set_defaults(handler=not_implemented("backup"))

    add_restore_subcommands(subparsers)

    checkpoint = subparsers.add_parser("checkpoint-run", help="prepare and push the instance checkpoint")
    _add_instance(checkpoint, help="path to an instance dir or instance.yaml")
    checkpoint.set_defaults(handler=run_checkpoint_command)

    maintenance = subparsers.add_parser(
        "instance-maintenance",
        help="pack the instance repository outside any tick (run by its timer)",
    )
    _add_instance(maintenance, help="path to an instance dir or instance.yaml")
    residue = maintenance.add_mutually_exclusive_group()
    residue.add_argument("--residue-inventory", action="store_true",
                         help="read owned and preserved Git residue without effects")
    residue.add_argument("--residue-replay", action="store_true",
                         help="replay only the named --target cleanups of one --project at their read --manifest")
    maintenance.add_argument("--project", help="read or replay only this registered project's residue")
    maintenance.add_argument("--target", action="append", default=[],
                             help="a manifest target id (intent key or ref@tip); repeat, at most 20")
    maintenance.add_argument("--manifest", action="append", default=[],
                             help="the manifest digest read for the --target at the same position")
    maintenance.set_defaults(handler=run_instance_maintenance)

    project = subparsers.add_parser("project")
    project_subcommands = project.add_subparsers(dest="project_command")
    project_add = project_subcommands.add_parser("add")
    project_add.add_argument("path_or_url")
    project_add.add_argument("--dry-run", action="store_true")
    project_add.add_argument(
        "--re-onboard",
        action="store_true",
        help=(
            "take an enabled binding back down to a disabled draft: keep plane, policy, remote "
            "and a legacy orca_binding, drop the canonical adapter, and require provision and gate again"
        ),
    )
    _add_env_instance(project_add)
    project_add.set_defaults(handler=run_project_add)
    provision_start = project_subcommands.add_parser("provision-start")
    provision_start.add_argument("project_id")
    _add_env_instance(provision_start)
    provision_start.set_defaults(handler=run_project_provision_start)
    provision_apply = project_subcommands.add_parser("provision-apply")
    provision_apply.add_argument("project_id")
    provision_apply.add_argument("--result")
    _add_env_instance(provision_apply)
    provision_apply.set_defaults(handler=run_project_provision_apply)
    gate = project_subcommands.add_parser("gate")
    gate.add_argument("project_id")
    _add_env_instance(gate)
    gate.set_defaults(handler=run_project_gate)
    project.set_defaults(handler=not_implemented("project"))

    add_task_subcommands(subparsers)
    add_product_issue_subcommands(subparsers)

    shell = subparsers.add_parser(
        "shell",
        help="launch an interactive ummanu head with the full runtime env",
    )
    shell.add_argument(
        "--head",
        "-H",
        default=None,
        help="head profile or adapter (claude/codex/hermes or any heads.toml profile id); "
        "default: the registry's role_defaults.new_card",
    )
    shell.add_argument(
        "--workspace",
        default=None,
        help="the head's working directory and codex trust directory "
        "(default: the installation's interactive workspace, <data>/interactive)",
    )
    shell.add_argument(
        "--env-file",
        default=None,
        help="runtime env file to load (default: instance runtime.env)",
    )
    shell.add_argument(
        "--print",
        dest="print_command",
        action="store_true",
        help="print the resolved launch command and exit without starting the head",
    )
    shell.set_defaults(handler=run_shell)

    memory = subparsers.add_parser("memory", help="manage the memory journal")
    memory_subcommands = memory.add_subparsers(dest="memory_command")
    memory_verify = memory_subcommands.add_parser(
        "verify",
        help="verify instance memory canon, derived export and index parity",
    )
    _add_instance(memory_verify, data_dir=True)
    memory_verify.set_defaults(handler=run_memory_verify)

    memory_reindex = memory_subcommands.add_parser(
        "reindex", help="rebuild the derived memory index from the local journal"
    )
    _add_instance(memory_reindex)
    memory_reindex.set_defaults(handler=run_memory_reindex)

    memory_propose = memory_subcommands.add_parser("propose")
    add_memory_write_common(memory_propose)
    memory_propose.add_argument("--scope", required=True)
    memory_propose.add_argument("--slug", required=True)
    memory_propose.add_argument("--file", required=True)
    memory_propose.add_argument("--source")
    memory_propose.add_argument("--tags", default="")
    memory_propose.add_argument("--pinned", action="store_true")
    memory_propose.add_argument("--supersedes", default="")
    memory_propose.set_defaults(handler=run_memory_propose)

    memory_commit = memory_subcommands.add_parser("commit")
    add_memory_write_common(memory_commit)
    memory_commit.add_argument("--propose-id", required=True)
    memory_commit.set_defaults(handler=run_memory_commit)

    memory_supersede = memory_subcommands.add_parser("supersede")
    add_memory_write_common(memory_supersede)
    memory_supersede.add_argument("--scope", required=True)
    memory_supersede.add_argument("--slug", required=True)
    memory_supersede.add_argument("--file", required=True)
    memory_supersede.add_argument("--supersedes", required=True)
    memory_supersede.add_argument("--source")
    memory_supersede.add_argument("--tags", default="")
    memory_supersede.add_argument("--pinned", action="store_true")
    memory_supersede.set_defaults(handler=run_memory_supersede)
    memory.set_defaults(handler=not_implemented("memory"))

    knowledge = subparsers.add_parser(
        "knowledge", help="write long recoverable documents into state/knowledge"
    )
    knowledge_subcommands = knowledge.add_subparsers(dest="knowledge_command")
    knowledge_write = knowledge_subcommands.add_parser(
        "write",
        help="write one markdown document or directory into state/knowledge under the writer lock",
    )
    _add_instance(knowledge_write)
    knowledge_write.add_argument("--actor", required=True)
    knowledge_write.add_argument(
        "--path", required=True, help="document or directory path relative to state/knowledge"
    )
    knowledge_source = knowledge_write.add_mutually_exclusive_group(required=True)
    knowledge_source.add_argument("--file", help="source markdown file")
    knowledge_source.add_argument(
        "--dir", help="source directory; replaces the whole target directory (20 MiB cap)"
    )
    knowledge_write.set_defaults(handler=run_knowledge_write)

    knowledge_list = knowledge_subcommands.add_parser(
        "list", help="list documents currently in state/knowledge"
    )
    _add_instance(knowledge_list)
    knowledge_list.set_defaults(handler=run_knowledge_list)
    knowledge.set_defaults(handler=not_implemented("knowledge"))

    add_secret_subcommands(subparsers)

    return parser


def add_memory_write_common(parser: argparse.ArgumentParser) -> None:
    _add_instance(parser, data_dir=True)
    parser.add_argument("--actor", required=True)


def _add_instance(
    parser: argparse.ArgumentParser,
    *,
    data_dir: bool = False,
    help: str | None = None,
    data_dir_help: str | None = None,
) -> None:
    add_instance_argument(parser, help=help)
    if data_dir:
        parser.add_argument("--data-dir", help=data_dir_help)


def _add_env_instance(parser: argparse.ArgumentParser, *, help: str | None = None) -> None:
    add_instance_argument(parser, help=help)


def run_config_check(args: argparse.Namespace) -> int:
    """One finding per line on stdout and exit 1, or a summary on stderr and exit 0."""
    from ummanu.infra.config_check import check_live_root

    result = check_live_root(Path(args.instance))
    for finding in result.findings:
        print(finding)
    verdict = "ok" if result.ok else f"{len(result.findings)} finding(s)"
    print(
        f"ummanu config check: {verdict} ({result.checked_files} exported file(s) in {result.live_root})",
        file=sys.stderr,
    )
    return 0 if result.ok else 1


def run_doctor(args: argparse.Namespace) -> int:
    instance_path = Path(args.instance)
    report = validate_instance(instance_path)

    if args.json:
        return run_doctor_json(args, report)

    if not report.ok:
        print(f"ummanu doctor: {len(report.errors)} config problem(s):")
        for error in report.errors:
            print(f"  {error}")
        return 1 if args.dry_run else 2

    print("Ummanu doctor report")
    print("mode: dry-run" if args.dry_run else "mode: read-only")
    print(f"instance: {report.instance_path}")
    print(f"name: {report.name or 'unnamed'}")
    print(f"projects: {report.projects}")
    print(f"adapters: {report.adapters}")
    print(f"adapter drafts: {report.adapter_drafts}")
    print(f"data manifest: {'present' if report.has_manifest else 'absent'}")
    if report.manifest_path:
        print(f"data manifest path: {report.manifest_path}")
    cache_dir = _memory_cache_dir(report)
    print(f"memory model cache: {cache_dir}")
    if _is_temporary_directory(cache_dir):
        print("warning: memory model cache is in a temporary directory and can be cleaned unexpectedly")
    for line in _codex_home_lines(_codex_home_status(report)):
        print(line)
    if report.data_dir is not None:
        print(interactive_workspace.status_line(interactive_workspace.describe(report.data_dir)))
    if report.warnings:
        print(f"warnings: {len(report.warnings)}")
        for warning in report.warnings:
            print(f"  {warning}")

    inspection = collect_doctor_inspection(report, args)
    print_restore_status(report, findings=inspection.restore)

    inspect_host = inspection.collected is not None
    if inspect_host:
        print_host_inventory(
            report, args, expected=inspection.expected, collected=inspection.collected, diffs=inspection.diffs
        )

    print_dispatcher_status(
        report, inspection.collected, inspect_live=not args.offline, findings=inspection.dispatcher
    )
    print_recovery_inventory(inspection.recovery)
    print_checkpoint_status(report, findings=inspection.checkpoint)
    print_secret_store_status(report, findings=inspection.secret_store)
    print_board_schema_status(inspection.board_schema)

    for finding in inspection.findings:
        if finding["code"] in {"root_disk_low", "root_disk_unavailable"}:
            print(f"root filesystem: {finding['message']}")
        elif finding["code"] == "automation_busy_without_advance":
            print(f"{finding['agent']}: {finding['message']}")
        elif finding["code"] == "head_fallback":
            print(f"error: head fallback: {finding['message']}")
        elif finding["code"] == "dispatcher_tick_p95_slow":
            print(f"red: {finding['code']}: {finding['message']}")
        elif str(finding["code"]).startswith("live_root."):
            print(f"{finding['code']}: {finding['message']}")
        if accepted(finding):
            raw = {key: value for key, value in finding.items()
                   if key not in {"accepted", "acceptance_reason"}}
            print(f"accepted finding: {json.dumps(raw, sort_keys=True)}")
            print(f"  reason: {finding['acceptance_reason']}")

    print("host changes: none")
    if inspection.unavailable:
        # A kind could not be inspected, so this is not a clean "all matched".
        print("status: host inventory incomplete")
        return 2
    active = active_findings(inspection.findings)
    if active:
        warning_only = all(finding["code"] == "config_warning" for finding in active)
        print("status: warnings" if warning_only else "status: findings")
        return 1
    print("status: ok")
    return 0


_CODEX_HOME_KINDS = {
    CODEX_HOME_PROFILE: "profile codex_home",
    CODEX_HOME_ENV: "TA_CODEX_HOME override",
    CODEX_HOME_DATA_DIR: "data-dir home",
}


def _codex_home_status(report) -> dict[str, object]:
    """The CODEX_HOME a Codex head of this installation launches with now, and which rung chose it.

    With none (`CodexHomeLoginMissing`) `path` is None and `login_missing` carries the resolver's
    own message, which names the fix. `codex_required` says whether any installed profile runs
    Codex: only then is a missing login a finding (`collect_doctor_inspection`).
    """
    assert report.data_dir is not None
    data_dir_home = str(data_dir_codex_home(report.data_dir))
    required = _codex_required(report.instance_path.parent)
    try:
        home = resolve_codex_home({}, data_dir=report.data_dir)
    except CodexHomeLoginMissing as exc:
        return {
            "path": None,
            "kind": "",
            "data_dir_home": data_dir_home,
            "login_missing": str(exc),
            "codex_required": required,
        }
    return {
        "path": home.path,
        "kind": home.kind,
        "data_dir_home": data_dir_home,
        "login_missing": "",
        "codex_required": required,
    }


def _codex_required(instance_dir: Path) -> bool:
    """Whether an installed head profile runs on the `codex` adapter; a registry that cannot be read
    cannot rule one out, so it counts as yes."""
    try:
        profiles = installed_heads(instance_dir).get("profiles", {})
    except HeadRegistryConfigError:
        return True
    if not isinstance(profiles, dict):
        return True
    return any(
        isinstance(profile, dict) and profile.get("adapter") == "codex" for profile in profiles.values()
    )


def _codex_home_lines(status: dict[str, object]) -> list[str]:
    """Doctor's lines for the active CODEX_HOME; a missing login an installed profile needs is an error."""
    if status["login_missing"]:
        if status["codex_required"]:
            return [f"error: codex home: {status['login_missing']} (docs/OPERATIONS.md)"]
        return [f"codex home: none, and no installed profile runs Codex ({status['login_missing']})"]
    kind = str(status["kind"])
    return [f"codex home: {status['path']} ({_CODEX_HOME_KINDS.get(kind, kind)})"]


def _memory_cache_dir(report) -> Path:
    """Return the product-owned persistent fastembed cache location."""
    assert report.data_dir is not None
    return report.data_dir / "memory" / "fastembed-cache"


def _is_temporary_directory(path: Path) -> bool:
    """Whether a cache path sits below a system temporary directory."""
    resolved = path.resolve(strict=False)
    for temporary in (Path("/tmp"), Path("/var/tmp")):
        try:
            resolved.relative_to(temporary)
            return True
        except ValueError:
            pass
    return False


def run_status(args: argparse.Namespace) -> int:
    report = validate_instance(Path(args.instance))
    if not report.ok:
        payload = {
            "schema_version": 1,
            "error": "invalid_instance",
            "findings": [str(error) for error in report.errors],
        }
        if args.json:
            print(json.dumps(payload, sort_keys=True))
        else:
            print("ummanu status: invalid instance config")
        return 2
    snapshot = collect_status(report, host_fixture=args.host_fixture, offline=args.offline)
    if args.json:
        print(json.dumps(snapshot, sort_keys=True))
        return 0
    print(f"Ummanu status: {snapshot['installation']['name'] or 'unnamed'}")
    print(f"active attempts: {len(snapshot['dispatcher']['active_attempts'])}")
    canon = snapshot["installation"]["head_registry"]
    if canon["error"]:
        print(f"head registry: {canon['error']}")
    else:
        owner = canon["canonical_owner"] or "unknown"
        print(
            f"head registry: {canon['canonical']} ({owner}-owned), "
            f"built from {canon['product_root']} @ {canon['revision']}"
        )
    observers = snapshot["dispatcher"]["observers"]
    live = sum(1 for observer in observers if observer["alive"])
    print(interactive_workspace.status_line(snapshot["installation"]["interactive_workspace"]))
    print(f"sprint observers: {live} live of {len(observers)}")
    sprint_status = snapshot["installation"]["sprints"]
    if sprint_status["error"]:
        print(f"sprints: unavailable ({sprint_status['error']['message']})")
    else:
        stopped = sum(sprint["status"] == "stopped" for sprint in sprint_status["items"])
        stale = sum(not sprint["resume_freshness"]["fresh"] for sprint in sprint_status["items"])
        print(f"sprints: {len(sprint_status['items'])}, {stopped} stopped, {stale} resume errors")
    memory_facts = snapshot["memory"]["fact_count"]
    print(f"memory facts: {memory_facts if memory_facts is not None else 'unknown'}")
    # What a tick and a checkpoint cost, next to the outcome each of them reached. Both numbers are
    # recorded by the dispatcher itself; this is where an operator reads them without opening a
    # state file (docs/OPERATIONS.md, "Where the durations are").
    last_tick = snapshot["dispatcher"]["last_tick"]
    if last_tick is None:
        print("last tick: none recorded")
    else:
        print(
            f"last tick: #{last_tick['seq']} {last_tick['status']} at {last_tick['at']} "
            f"in {_duration_text(last_tick['duration_ms'])}"
        )
    _print_tick_measurements(snapshot["dispatcher"])
    checkpoint = snapshot["checkpoint"]
    print(
        f"checkpoint: {checkpoint.get('checkpoint_status') or 'pending'} "
        f"in {_duration_text(checkpoint.get('checkpoint_duration_ms'))}"
    )
    print(f"checkpoint lag: {snapshot['checkpoint']['lag_minutes']} min")
    return 0


def _print_tick_measurements(dispatcher: dict[str, Any]) -> None:
    statistics = dispatcher.get("tick_statistics") or tick_statistics({})
    count = statistics["sample_count"]
    if count:
        print(
            f"tick durations: {count} samples, p50 {_duration_text(statistics['p50_duration_ms'])}, "
            f"p95 {_duration_text(statistics['p95_duration_ms'])}"
        )
    else:
        print("tick durations: unavailable (0 samples)")
    last = dispatcher["last_tick"]
    phases = last.get("phases") if last else None
    if phases:
        print("last tick phases: " + ", ".join(f"{name} {_duration_text(ms)}" for name, ms in phases.items()))
    else:
        print("last tick phases: unavailable")


def _duration_text(value: float | None) -> str:
    """Milliseconds as status prints them, or "unknown" for a record made before they existed."""
    return "unknown" if value is None else f"{float(value):.0f} ms"


def run_doctor_json(args: argparse.Namespace, report) -> int:
    """Structured counterpart to doctor without changing its default transcript."""
    if not report.ok:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "ok": False,
                    "findings": [
                        {"code": "config_invalid", "message": str(error)} for error in report.errors
                    ],
                },
                sort_keys=True,
            )
        )
        return 1 if args.dry_run else 2
    inspection = collect_doctor_inspection(report, args)
    snapshot = collect_status(
        report,
        host_fixture=args.host_fixture,
        offline=args.offline,
        recovery=inspection.recovery,
        # Sprints are read from the live board store: `--offline` and `--host-fixture` read none.
        sprints=not (args.offline or args.host_fixture),
    )
    payload = {
        "schema_version": 1,
        "ok": not active_findings(inspection.findings),
        "findings": inspection.findings,
        "board_schema": inspection.board_schema,
        "status": snapshot,
        "codex_home": _codex_home_status(report),
    }
    print(json.dumps(payload, sort_keys=True))
    if inspection.unavailable:
        return 2
    return 1 if active_findings(inspection.findings) else 0


def collect_doctor_inspection(report, args: argparse.Namespace) -> DoctorInspection:
    """Collect invariant failures once for the text and JSON doctor renderers."""
    findings: list[dict[str, object]] = []
    disk_unavailable = False
    if not args.offline and (not args.dry_run or args.host or args.host_fixture):
        disk_finding = root_disk_finding(report.data_dir)
        if disk_finding is not None:
            findings.append(disk_finding)
            disk_unavailable = disk_finding["code"] == "root_disk_unavailable"
    restore = _restore_findings(report)
    findings.extend({"code": "restore_problem", "message": finding} for finding in restore)
    findings.extend(automation_busy_findings(report.data_dir))
    inspect_host = not args.offline and (not args.dry_run or args.host or args.host_fixture)
    findings.extend(doctor_live_root_findings(report, args, inspect_host=inspect_host))
    collected: CollectResult | None = None
    expected = None
    diffs = None
    unavailable = disk_unavailable
    if inspect_host:
        expected, collected, diffs = collect_host_inventory(report, args)
        for kind, reason in collected.errors.items():
            findings.append({"code": "host_inventory_unavailable", "kind": kind, "message": reason})
        unavailable = unavailable or bool(collected.errors)
        for kind, diff in diffs.items():
            if kind in collected.errors:
                continue
            findings.extend(
                {"code": "missing_on_host", "kind": kind, "name": name} for name in diff.missing_on_host
            )
            findings.extend(
                {"code": "unmanaged_on_host", "kind": kind, "name": name} for name in diff.unmanaged_on_host
            )
        findings.extend(
            {"code": "unit_runtime", "message": finding}
            for finding in _unit_runtime_findings(expected, collected)
        )
        findings.extend(checkpoint_unit_findings(expected, collected))
    provenance = production_runtime_provenance_finding(report, inspect_runtime=not args.offline)
    dispatcher = dispatcher_findings(
        report, collected, inspect_live=not args.offline, provenance=provenance
    )
    checkpoint_rpo = (
        checkpoint_rpo_findings(report) + checkpoint_cut_lag_findings(report)
        + snapshot_foreign_commit_findings(report)
    )
    checkpoint_plain = checkpoint_findings(report)
    checkpoint = [f"{finding['severity']}: {finding['message']}" for finding in checkpoint_rpo] + checkpoint_plain
    secret_store = secret_store_findings(report)
    production = _load_dispatcher_state(report.data_dir / "dispatcher" / "production-state.json")
    checkpoint_state = load_checkpoint_state(report.data_dir)
    checkpoint_snapshot_value = checkpoint_snapshot(
        report.instance_path.parent,
        write_state=checkpoint_state.get("checkpoint"),
        push_state=checkpoint_state.get("checkpoint_push"),
        data_dir=report.data_dir,
    )
    recovery = collect_recovery_inventory(
        report,
        inspect_live=not args.offline,
        checkpoint=checkpoint_snapshot_value,
    )
    resource_probes = [
        HeadReadiness(
            str(row["resource"]),
            str(row["state"]),
            str(row["reason"]),
            float(time.time() - int(row["age_seconds"])) if row.get("age_seconds") is not None else 0,
            row.get("source") == "dispatcher-cache",
        )
        for row in recovery["resources"]
    ]
    findings.extend({"code": "dispatcher", "message": finding} for finding in dispatcher)
    tick_finding = tick_p95_finding(production)
    if tick_finding is not None:
        findings.append(tick_finding)
    if provenance is not None:
        findings.append(provenance)
    findings.extend(checkpoint_rpo)
    findings.extend({"code": "checkpoint", "message": finding} for finding in checkpoint_plain)
    findings.extend({"code": "secret_store", "message": finding} for finding in secret_store)
    # A head profile or PO session that cannot fall over to the other subscription family (ummanu-108).
    findings.extend(
        {"code": "head_fallback", "message": f"{error.path}: {error.message}"}
        for error in fallback_errors(report.instance_path.parent, getattr(report, "instance", None))
    )
    codex_home_status = _codex_home_status(report)
    if codex_home_status["login_missing"] and codex_home_status["codex_required"]:
        # No Codex head of this installation can start: red, with the resolver's own fix text.
        findings.append({"code": "codex_home_login_missing", "message": codex_home_status["login_missing"]})
    findings.extend(
        {"code": "resource_probe", "resource": readiness.resource, "message": _probe_finding(readiness)}
        for readiness in resource_probes
        if readiness.status == PROBE_BROKEN
    )
    if not args.dry_run:
        record_provider_owner_events(report, recovery.get("resources") or [])
    findings.extend(_recovery_findings(recovery))
    board_schema = board_schema_inspection(report, args)
    findings.extend(_board_schema_findings(board_schema))
    if args.strict:
        findings.extend({"code": "config_warning", "message": str(warning)} for warning in report.warnings)
    return DoctorInspection(
        apply_acceptance(findings, getattr(report, "instance", {})),
        unavailable,
        restore,
        dispatcher,
        checkpoint,
        secret_store,
        resource_probes,
        recovery,
        expected,
        collected,
        diffs,
        board_schema,
    )


def doctor_live_root_findings(report, args: argparse.Namespace, *, inspect_host: bool) -> list[dict[str, object]]:
    """`live_root.git_work_tree` and `live_root.old_path` (`infra.live_root_findings`).

    The installed unit files are host state, read only when doctor inspects the host: the live
    systemd directory, or a fixture host's `unit-files/`. A test that replaces the live host source
    with one that names no unit directory reads none.
    """
    from ummanu.infra.live_root_findings import live_root_findings

    live_root = report.instance_path.parent
    unit_dirs: list[Path] = []
    if inspect_host:
        if args.host_fixture:
            unit_dirs.append(Path(args.host_fixture) / FIXTURE_UNIT_FILES_DIR)
        elif isinstance(unit_dir := getattr(LiveHostSource, "unit_files_dir", None), Path):
            unit_dirs.append(unit_dir)
    try:
        _, home = resolve_runtime_owner(live_root)
    except ValueError:
        home = None
    return live_root_findings(live_root, unit_dirs=unit_dirs, home=home)


def automation_busy_findings(data_dir: Path | None) -> list[dict[str, object]]:
    """A curator head busy past its threshold with no advance or memory write (issue:db32299c8).

    Read from the data directory's `automation-state`, where the packaged units' `AgentState` writes.
    """
    from ummanu.automations.agents.curator.busy import busy_without_advance

    if data_dir is None:
        return []
    finding = busy_without_advance(data_dir / "automation-state" / "curator" / "runs.jsonl")
    return [finding] if finding is not None else []


def root_disk_finding(data_dir: Path) -> dict[str, object] | None:
    """The configured data root's filesystem, with unknown distinct from sufficient space."""
    free = disk_free_bytes(data_dir)
    if free is None:
        return {"code": "root_disk_unavailable", "threshold_bytes": ROOT_FREE_MIN_BYTES,
                "message": "free space probe unavailable"}
    if free < ROOT_FREE_MIN_BYTES:
        return {"code": "root_disk_low", "free_bytes": free,
                "threshold_bytes": ROOT_FREE_MIN_BYTES,
                "message": f"{free} bytes free, below {ROOT_FREE_MIN_BYTES} byte threshold"}
    return None


def board_schema_inspection(report, args: argparse.Namespace) -> dict[str, object]:
    """The board store's schema against this build, read once for both doctor renderers.

    The operational readers' own gate (`board.schema_gate`), read on the `read` role and never
    written. `--offline` and `--host-fixture` read nothing live, so they report it not inspected.
    """
    from ummanu.board import schema_gate

    if args.offline or args.host_fixture:
        flag = "--offline" if args.offline else "--host-fixture"
        return {
            "state": "not_inspected",
            "actual": None,
            "expected": schema_gate.EXPECTED_SCHEMA_REVISION,
            "pending": [],
            "reason": f"{flag} reads no live board store",
        }
    return schema_gate.inspect_instance(report.instance_path.parent)


def _board_schema_findings(board_schema: dict[str, object]) -> list[dict[str, object]]:
    """`schema_owed` with the owed migrations, or the inspection that could not be made."""
    from ummanu.board import schema_gate

    state = board_schema.get("state")
    if state == schema_gate.OWED:
        return [
            {
                "code": schema_gate.SCHEMA_OWED,
                "actual": board_schema.get("actual"),
                "expected": board_schema.get("expected"),
                "pending": list(board_schema.get("pending") or []),  # type: ignore[call-overload]
                "message": board_schema.get("message"),
            }
        ]
    if state == schema_gate.UNAVAILABLE:
        return [{"code": "board_schema_unavailable", "message": board_schema.get("reason")}]
    return []


def print_board_schema_status(board_schema: dict[str, object] | None) -> None:
    """The text renderer's lines for `board_schema_inspection`; JSON carries the same dict."""
    if not board_schema:
        return
    state = board_schema.get("state")
    if state == "current":
        print(f"board schema: current at {board_schema.get('expected')}")
    elif state == "owed":
        print(f"board schema: owed: {board_schema.get('message')}")
        print(f"board schema pending: {', '.join(board_schema.get('pending') or [])}")  # type: ignore[arg-type]
    elif state == "ahead":
        print(f"board schema: ahead: {board_schema.get('message')}")
    else:
        print(f"board schema: {str(state).replace('_', ' ')}: {board_schema.get('reason')}")


#: The probe verdicts that put a provider in front of the owner, and what each one means to them.
PROVIDER_RED_STATES = {
    "unauthenticated": "its key is expired or its login is missing",
    "exhausted": "its quota is spent",
    "unavailable": "its provider does not answer",
    PROBE_TIMED_OUT: "its provider gave the probe no answer in time, so claims on it are held",
    PROBE_BROKEN: "its probe cannot run, so claims on it are not gated by health",
}


def record_provider_owner_events(report, resources: list[object]) -> int:
    """One `provider_red` notice per provider, condition and UTC day: repeated doctor runs add nothing.

    Doctor is the writer (issue:1d3d86edba8c2193b93e); the web's lamp only reads recorded health. A
    dry run records nothing, and an installation without a board store has nowhere to record.
    """
    from ummanu.board import owner_events

    day = datetime.now(UTC).strftime("%Y-%m-%d")
    recorded = 0
    for row in resources:
        if not isinstance(row, dict):
            continue
        state = str(row.get("state") or "")
        meaning = PROVIDER_RED_STATES.get(state)
        if meaning is None:
            continue
        resource = str(row.get("resource") or "")
        recorded += owner_events.record(
            owner_events.PROVIDER_RED,
            None,
            f"Provider {resource} is {state}: {meaning} ({row.get('reason') or 'no reason recorded'})",
            f"{owner_events.PROVIDER_RED}:{resource}:{state}:{day}",
            to=report.instance_path.parent,
        )
    return recorded


def _recovery_findings(recovery: dict[str, object]) -> list[dict[str, object]]:
    """Actionable recovery defects, kept distinct by capability and resource."""
    findings: list[dict[str, object]] = []
    for row in recovery.get("resources", []):
        if not isinstance(row, dict) or row.get("state") in {"ready", "unknown", "stale"}:
            continue
        if row.get("state") == PROBE_BROKEN:
            continue
        findings.append(
            {
                "code": "resource_readiness",
                "resource": row.get("resource"),
                "state": row.get("state"),
                "message": row.get("reason"),
            }
        )
    for row in recovery.get("credential_consumers", []):
        if not isinstance(row, dict):
            continue
        consumer = str(row.get("consumer") or "")
        if consumer.startswith("project-git:"):
            # A registered project the dispatcher can issue work for, whose Git access would be refused.
            if row.get("enabled") is not True or row.get("state") not in {
                "refused",
                "locked/unverifiable",
                "missing/unavailable",
            }:
                continue
        elif consumer != "checkpoint-github":
            continue
        if row.get("state") in {"managed-ready", "ambient/manual-bypass", "unknown"}:
            continue
        findings.append(
            {
                "code": "credential_consumer",
                "consumer": row.get("consumer"),
                "state": row.get("state"),
                "message": row.get("reason"),
                "supported_next_action": row.get("supported_next_action"),
            }
        )
    for section, code in (
        ("paths", "recovery_path"),
        ("materializations", "materialization"),
        ("bypasses", "recovery_bypass"),
    ):
        for row in recovery.get(section, []):
            if not isinstance(row, dict) or row.get("state") in {"supported", "ready"}:
                continue
            findings.append(
                {
                    "code": code,
                    "capability": row.get("capability"),
                    "state": row.get("state"),
                    "message": row.get("reason") or row.get("supported_next_action"),
                    "supported_next_action": row.get("supported_next_action"),
                }
            )
    for message in recovery.get("catalog_envelope_divergences", []):
        findings.append({"code": "secret_store_divergence", "message": str(message)})
    return findings


def print_restore_status(report, *, findings: list[str] | None = None) -> list[str]:
    findings = _restore_findings(report) if findings is None else findings
    if findings:
        print("restore findings:")
        for finding in findings:
            print(f"  {finding}")
    return findings


def _restore_findings(report) -> list[str]:
    if report.data_dir is None:
        return []
    try:
        _, data_dir, _ = _target(report.instance_path)
    except RestoreError:
        return []
    if not (data_dir / "restore-state.json").is_file():
        return []
    return restore_findings(data_dir)


def run_project_add(args: argparse.Namespace) -> int:
    code, artifact = project_add(
        args.path_or_url, args.instance, dry_run=args.dry_run, re_onboard=args.re_onboard
    )
    print(render_artifact(artifact), end="")
    return code


def run_project_provision_start(args: argparse.Namespace) -> int:
    code, result = start_provision(args.instance, args.project_id)
    print(render_result(result), end="")
    return code


def run_project_provision_apply(args: argparse.Namespace) -> int:
    code, result = apply_provision_result(args.instance, args.project_id, args.result)
    print(render_result(result), end="")
    return code


def run_project_gate(args: argparse.Namespace) -> int:
    code, result = run_gate(args.instance, args.project_id)
    print(render_result(result), end="")
    return code


def print_dispatcher_status(
    report,
    collected_host: CollectResult | None,
    *,
    inspect_live: bool,
    findings: list[str] | None = None,
) -> bool:
    if report.data_dir is None:
        return False
    data_dir = report.data_dir
    production = _load_dispatcher_state(data_dir / "dispatcher" / "production-state.json")
    production_phase = str(production.get("phase") or "new")
    production_owner = str(production.get("owner") or "")

    if not production:
        return False

    owner_state = "production-owner" if production_owner else "unowned"

    findings = (
        dispatcher_findings(report, collected_host, inspect_live=inspect_live)
        if findings is None
        else findings
    )
    print()
    print("dispatcher ownership: read-only")
    print(f"  state: {owner_state}")
    print(f"  production phase: {production_phase}")
    print(f"  production owner: {production_owner or '(none)'}")
    pause = ProductionPause(data_dir).summary()
    if pause.get("paused"):
        since = pause.get("since") or "(unknown)"
        actor = pause.get("actor") or "(unknown)"
        print(f"  pause: {pause['mode']} since {since} by {actor}")
    else:
        print("  pause: none")

    if findings:
        print("dispatcher findings:")
        for finding in findings:
            print(f"  {finding}")
    return bool(findings)


def dispatcher_findings(
    report,
    collected_host: CollectResult | None,
    *,
    inspect_live: bool,
    provenance: dict[str, object] | None | object = _PROVENANCE_UNSET,
) -> list[str]:
    if report.data_dir is None:
        return []
    data_dir = report.data_dir
    production = _load_dispatcher_state(data_dir / "dispatcher" / "production-state.json")
    if provenance is _PROVENANCE_UNSET:
        provenance = production_runtime_provenance_finding(report)
    provenance_message = str(provenance["message"]) if isinstance(provenance, dict) else ""
    if not production:
        return [provenance_message] if provenance_message else []
    # Unresolved divergences are read from the state snapshot itself, not the live host, so they
    # surface under --offline too: an operator diagnosing a broken host still needs to see them.
    findings: list[str] = _divergence_findings(production)
    if provenance_message:
        findings.append(provenance_message)
    if not inspect_live:
        return findings
    if not str(production.get("owner") or ""):
        findings.append("production owner fence is missing")
    findings.extend(_production_host_findings(report, data_dir, collected_host))
    return findings


def production_runtime_provenance_finding(
    report, *, inspect_runtime: bool = True
) -> dict[str, object] | None:
    """Read the installed pre-import boundary without falling back to this checkout.

    Fixtures and offline configuration documents often have no installed source pin.  Falling back
    to the process's configured checkout in that case would make an offline Doctor accidentally
    inspect a developer's live venv, so only an explicit installation pin authorizes this probe.
    """
    try:
        source = read_source(report.instance_path.parent)
    except HeadRegistryConfigError:
        return None
    product_root = source.get("product_root") if isinstance(source, dict) else None
    if not isinstance(product_root, str) or not product_root.strip():
        return None
    # An offline Doctor reads the installation contract and recorded dispatcher state without
    # starting any installation-owned executable.  A refusal persisted by the pre-import fence is
    # already the exact inspector result, so it remains actionable offline.  In the absence of
    # such a refusal, only live Doctor probes the production interpreter: a portable installation
    # can validly be configured before its production venv has ever been materialized.
    recorded = _load_dispatcher_state(report.data_dir / "dispatcher" / "production-state.json")
    runtime_state = recorded.get("runtime_provenance")
    observation = runtime_state.get("observation") if isinstance(runtime_state, dict) else None
    has_recorded_refusal = isinstance(runtime_state, dict) and runtime_state.get("status") == "refused"
    if has_recorded_refusal and isinstance(observation, dict):
        provenance = RuntimeProvenance.from_dict(observation)
    elif not inspect_runtime:
        return None
    else:
        provenance = ProductionRuntime.installed(
            Path(product_root), git_workspaces_root=report.data_dir / "workspaces"
        ).probe()
    if provenance.valid:
        return None
    repair = shlex.join(
        [
            str(Path(provenance.product_root) / ".venv" / "bin" / "python3"),
            "-m",
            "pip",
            "install",
            "--no-deps",
            "-e",
            provenance.product_root,
        ]
    )
    return {
        "code": "production_runtime_provenance",
        "classification": provenance.classification,
        "interpreter": provenance.interpreter,
        "product_root": provenance.product_root,
        "import_origin": provenance.import_origin,
        "metadata_source": provenance.metadata_source,
        "offending_target": provenance.offending_target,
        "repair": repair,
        "message": f"{provenance.refusal('doctor')}; repair: {repair}",
    }


def _divergence_findings(production: dict[str, object]) -> list[str]:
    """Every controlled divergence still open in the production state snapshot.

    Reconciliation (`ummanu/dispatch/production.py`) closes a divergence once its card leaves
    the active dispatcher cycle, so one still open here is either tied to a card still in flight or
    is genuinely stuck and needs an operator.
    """
    raw = production.get("controlled_divergences")
    items = raw if isinstance(raw, list) else []
    findings: list[str] = []
    for item in items:
        if not isinstance(item, dict) or item.get("status") == "closed":
            continue
        ref = str(item.get("pilot_ref") or "?")
        reason = str(item.get("reason") or "unknown")
        divergence_id = str(item.get("id") or "?")
        findings.append(f"unresolved controlled divergence {divergence_id}: ref={ref} reason={reason}")
    return findings


def _probe_finding(readiness: HeadReadiness) -> str:
    """Name a broken probe as the gating failure it is, not as a red resource.

    A red resource is an ordinary operational fact and is printed above without becoming a finding.
    This one is a defect of the installation: while it lasts, every claim on this resource was
    allowed without the health gate ever having an opinion.
    """
    return (
        f"resource {readiness.resource} probe cannot run ({readiness.reason}); "
        "claims on this resource are not gated by health until it is repaired"
    )


def print_recovery_inventory(recovery: dict[str, object]) -> None:
    """Render the same recovery facts exposed by structured status."""
    resources = recovery.get("resources") if isinstance(recovery.get("resources"), list) else []
    if resources:
        print()
        print("resource probes: read-only")
        for row in resources:
            if not isinstance(row, dict):
                continue
            profiles = ",".join(str(item) for item in row.get("profiles", [])) or "(none)"
            observed = row.get("observed_at") or "never"
            age = f", age={row['age_seconds']}s" if row.get("age_seconds") is not None else ""
            prior = f", observed_state={row['observed_state']}" if row.get("observed_state") else ""
            recorded = " (recorded)" if row.get("source") == "dispatcher-cache" else ""
            if row.get("until"):
                recorded += f" until {row['until']}"
            print(
                f"  {row['resource']}: {row['state']}{recorded} - {row['reason']} "
                f"[source={row['source']}, freshness={row['freshness']}, "
                f"observed={observed}{age}{prior}; profiles={profiles}]"
            )
        broken = [row for row in resources if isinstance(row, dict) and row.get("state") == PROBE_BROKEN]
        if broken:
            print("resource probe findings:")
            for row in broken:
                readiness = HeadReadiness(str(row["resource"]), PROBE_BROKEN, str(row["reason"]), 0)
                print(f"  {_probe_finding(readiness)}")
    consumers = (
        recovery.get("credential_consumers") if isinstance(recovery.get("credential_consumers"), list) else []
    )
    if consumers:
        print("credential consumers: read-only")
        for row in consumers:
            if not isinstance(row, dict):
                continue
            verified = row.get("verified_at") or "never"
            reason = f" - {row['reason']}" if row.get("reason") else ""
            transport = (
                f"transport={row['transport']}, managed={row.get('managed_readiness')}, "
                if row.get("transport")
                else ""
            )
            print(
                f"  {row['consumer']}: {row['state']}{reason} "
                f"[{transport}source={row['source']}, verification={row['verification_source']} at {verified}; "
                f"capability={row['capability']}]"
            )
    for key, title in (
        ("paths", "recovery paths"),
        ("materializations", "materialization targets"),
        ("bypasses", "recovery bypasses"),
    ):
        rows = recovery.get(key) if isinstance(recovery.get(key), list) else []
        if not rows:
            continue
        print(f"{title}: read-only")
        for row in rows:
            if not isinstance(row, dict):
                continue
            reason = row.get("reason") or row.get("kind") or "recorded"
            print(
                f"  {row.get('capability') or row.get('target')}: {row.get('state')} - {reason}; "
                f"next: {row.get('supported_next_action') or 'none'}"
            )
    divergences = recovery.get("catalog_envelope_divergences")
    if isinstance(divergences, list) and divergences:
        print("catalog/envelope divergences:")
        for item in divergences:
            print(f"  {item}")


def print_checkpoint_status(report, *, findings: list[str] | None = None) -> list[str]:
    """Checkpoint freshness: docs/RECOVERY.md, "Observability".

    Last commit, last push, lag, the gate's blocking reason and the
    `remote diverged` alarm.
    """
    if report.data_dir is None:
        return []
    data_dir = report.data_dir
    production = load_checkpoint_state(data_dir)
    if findings is None:
        findings = [
            f"{finding['severity']}: {finding['message']}"
            for finding in checkpoint_rpo_findings(report) + checkpoint_cut_lag_findings(report)
        ] + checkpoint_findings(report)
    if "checkpoint" not in production and "checkpoint_push" not in production:
        if findings:
            print()
            print("checkpoint findings:")
            for finding in findings:
                print(f"  {finding}")
        return findings

    snapshot = checkpoint_snapshot(
        report.instance_path.parent,
        write_state=production.get("checkpoint"),
        push_state=production.get("checkpoint_push"),
        data_dir=report.data_dir,
    )
    print()
    print("checkpoint freshness: read-only")
    for line in render_checkpoint_lines(snapshot):
        print(f"  {line}")

    if findings:
        print("checkpoint findings:")
        for finding in findings:
            print(f"  {finding}")
    return findings


def checkpoint_unit_findings(expected, collected: CollectResult) -> list[dict[str, object]]:
    """One-shot services may be inactive, but a missing or failed checkpoint owner is red."""
    if "units" in collected.errors:
        return []
    findings = []
    for name in sorted(expected.units):
        if not name.endswith(("checkpoint.service", "checkpoint.timer")):
            continue
        state = collected.inventory.unit_states.get(name)
        reason = "missing" if name not in collected.inventory.units else (
            "failed" if state and state[1] == "failed" else ""
        )
        if reason:
            findings.append({"code": "checkpoint.unit_unhealthy", "severity": "red",
                             "message": f"checkpoint unit {name} is {reason}"})
    return findings


def checkpoint_cut_lag_findings(report) -> list[dict[str, object]]:
    if report.data_dir is None:
        return []
    state = load_checkpoint_state(report.data_dir)
    snapshot = checkpoint_snapshot(
        report.instance_path.parent, write_state=state.get("checkpoint"),
        push_state=state.get("checkpoint_push"), data_dir=report.data_dir,
    )
    epoch = snapshot.get("last_checkpoint_prepared_epoch") or 0.0
    age = snapshot.get("last_checkpoint_prepared_age_minutes")
    stale = time.time() - epoch > 15 * 60 if epoch > 0 else age is not None and age > 15
    if not stale:
        return []
    return [{"code": "checkpoint.cut_lag_exceeded", "severity": "red",
             "message": f"last successful checkpoint cut is {age} min old (limit 15 min)"}]


def checkpoint_rpo_findings(report) -> list[dict[str, object]]:
    """A checkpoint that has not published for longer than the RPO, as a classified finding.

    Its code is classified by the doctor lamp's own table (`webproto.reads.PROBLEM_SEVERITY`), and
    its sentence names what stopped it: the gate's blocked reason and since when, or the push
    failure.
    """
    if report.data_dir is None:
        return []
    production = load_checkpoint_state(report.data_dir)
    if "checkpoint" not in production and "checkpoint_push" not in production:
        return []
    snapshot = checkpoint_snapshot(
        report.instance_path.parent,
        write_state=production.get("checkpoint"),
        push_state=production.get("checkpoint_push"),
        data_dir=report.data_dir,
    )
    message = rpo_problem(snapshot)
    if not message:
        return []
    from ummanu.webproto.reads import CHECKPOINT_RPO_EXCEEDED, problem_severity

    return [
        {
            "code": CHECKPOINT_RPO_EXCEEDED,
            "severity": problem_severity(CHECKPOINT_RPO_EXCEEDED),
            "message": message,
        }
    ]


def snapshot_foreign_commit_findings(report) -> list[dict[str, object]]:
    """Red `snapshot.foreign_commit` when the snapshot branch holds history the exporter did not make.

    Absent in legacy mode and while there is no snapshot repository (docs/RECOVERY.md, "Snapshot
    repository").
    """
    if report.data_dir is None:
        return []
    message = snapshot_foreign_commits(report.instance_path.parent, report.data_dir)
    if not message:
        return []
    from ummanu.webproto.reads import SNAPSHOT_FOREIGN_COMMIT, problem_severity

    return [
        {
            "code": SNAPSHOT_FOREIGN_COMMIT,
            "severity": problem_severity(SNAPSHOT_FOREIGN_COMMIT),
            "message": message,
        }
    ]


def checkpoint_findings(report) -> list[str]:
    if report.data_dir is None:
        return []
    production = load_checkpoint_state(report.data_dir)
    findings: list[str] = []
    if "checkpoint" in production or "checkpoint_push" in production:
        snapshot = checkpoint_snapshot(
            report.instance_path.parent,
            write_state=production.get("checkpoint"),
            push_state=production.get("checkpoint_push"),
            data_dir=report.data_dir,
        )
        if snapshot["remote_diverged"]:
            findings.append(f"remote diverged: {snapshot['push_reason'] or 'push stopped, resolve by hand'}")
        elif snapshot["push_status"] == "failed":
            attempted = snapshot["push_attempted_at"] or "unknown time"
            findings.append(
                f"checkpoint push failed at {attempted}: "
                f"{snapshot['push_reason'] or 'push failure reason unavailable'}"
            )
        if snapshot["blocked_reason"]:
            findings.append(f"checkpoint gate blocked: {snapshot['blocked_reason']}")
    instance = report.instance_path.parent
    # Example and pre-install configuration documents are intentionally not
    # instance repositories. The lifecycle cannot have established local Git
    # controls there, so doctor keeps its existing configuration-only contract.
    if (instance / ".git").exists():
        try:
            packing = state_repo.packing_controls(instance)
        except state_repo.StateRepoError as exc:
            findings.append(f"instance Git packing controls unavailable at {instance}: {exc}")
        else:
            drifted = [
                f"{key}={actual!r} (expected {expected!r})"
                for key, expected in state_repo.PACKING_CONTROLS
                if (actual := packing.get(key)) != expected
            ]
            if drifted:
                commands = "; ".join(
                    f"git -C {instance} config --local --replace-all {key} {expected}"
                    for key, expected in state_repo.PACKING_CONTROLS
                )
                findings.append(
                    f"instance Git packing controls drifted at {instance}: {', '.join(drifted)}; remediate: {commands}"
                )
    return findings


def print_secret_store_status(report, *, findings: list[str] | None = None) -> list[str]:
    """Secret store health: catalog/values consistency and installation key health."""
    findings = secret_store_findings(report) if findings is None else findings
    if findings:
        print()
        print("secret store findings:")
        for finding in findings:
            print(f"  {finding}")
    return findings


def secret_store_findings(report) -> list[str]:
    return list(_secret_store_findings(report.instance_path.parent))


def _load_dispatcher_state(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _production_host_findings(report, data_dir: Path, collected_host: CollectResult | None) -> list[str]:
    if collected_host is None or collected_host.errors.get("units"):
        return []
    prefix = report.host.get("unit_prefix", "") if isinstance(report.host, dict) else ""
    prefix = prefix if isinstance(prefix, str) else ""
    assert report.data_dir is not None
    packaged = resolve_installed_packaged(
        report.instance,
        instance_path=report.instance_path.parent,
        data_dir=report.data_dir,
    )
    desired = build_plan(report.instance, report.bindings, packaged=packaged)
    managed, error = load_managed_manifest(data_dir / "host-managed.json")
    if error:
        return ["production dispatcher managed manifest unavailable: " + error]
    changes = plan_changes(desired, collected_host.inventory, managed, prefix, foreign_units(report.host))
    findings = []
    for change in changes:
        if not change.logical_id.startswith("systemd:dispatcher:production"):
            continue
        if change.action == "unchanged":
            continue
        findings.append(f"production dispatcher managed unit mismatch: {change.action} {change.name}")
    return findings


def run_data_init(args: argparse.Namespace) -> int:
    data_dir = _data_dir_from_args(args, validate_tree=False)
    if data_dir is None:
        return 1

    try:
        layout = init_layout(data_dir)
    except RuntimeError as exc:
        print(f"ummanu data init: {exc}")
        return 1

    manifest = load_config(layout.manifest_path)
    errors = validate(manifest, "data-manifest", layout.manifest_path.name)
    if errors:
        print(f"ummanu data init: generated invalid data manifest at {layout.manifest_path}")
        for error in errors:
            print(f"  {error}")
        return 1

    print(f"ummanu-data: {layout.data_dir}")
    print(f"manifest: {layout.manifest_path}")
    print(f"created directories: {_join([str(p.relative_to(layout.data_dir)) for p in layout.created_dirs])}")
    print("status: ok")
    return 0


def run_data_export(args: argparse.Namespace) -> int:
    data_dir = _data_dir_from_args(args, validate_tree=True)
    if data_dir is None:
        return 1

    try:
        exports = export_all(data_dir, _instance_dir(args.instance), copy_transcripts=args.copy_transcripts)
    except RuntimeError as exc:
        print(f"ummanu data export: {exc}")
        return 1

    for name in ("board", "memory", "runs", "transcripts", "artifacts"):
        result = exports[name]
        print(f"{name}: {result.count} -> {result.path}")
    print("status: ok")
    return 0


def run_export_board(args: argparse.Namespace) -> int:
    data_dir = _data_dir_from_args(args, validate_tree=True)
    if data_dir is None:
        return 1
    try:
        result = export_board(data_dir, instance_dir=Path(args.instance).expanduser())
    except RuntimeError as exc:
        print(f"ummanu data export-board: {exc}")
        return 1
    print(f"board cards: {result.count}")
    print(f"export: {result.path}")
    print("status: ok")
    return 0


def run_export_memory(args: argparse.Namespace) -> int:
    data_dir = _data_dir_from_args(args, validate_tree=True)
    if data_dir is None:
        return 1
    try:
        result = export_memory(data_dir, _instance_dir(args.instance))
    except RuntimeError as exc:
        print(f"ummanu data export-memory: {exc}")
        return 1
    print(f"memory facts: {result.count}")
    print(f"export: {result.path}")
    print("status: ok")
    return 0


def run_memory_verify(args: argparse.Namespace) -> int:
    data_dir = _data_dir_from_args(args, validate_tree=True)
    if data_dir is None:
        return 1
    try:
        result = verify_memory_journal(data_dir, _instance_dir(args.instance))
    except RuntimeError as exc:
        print(f"ummanu memory verify: {exc}")
        return 1
    print(f"canon: {result.facts_dir}")
    print(f"canon revision: {result.journal_commit or '(none)'}")
    print(f"memory facts: {result.fact_count}")
    export_count = result.export_count if result.export_count is not None else "(missing)"
    index_count = result.index_count if result.index_count is not None else "(missing)"
    print(f"export facts: {export_count}")
    print(f"index facts: {index_count}")
    print(f"undo pending: {'yes' if result.dirty else 'no'}")
    if result.findings:
        print("findings:")
        for finding in result.findings:
            print(f"  {finding}")
    print(f"status: {'ok' if result.ok else 'failed'}")
    return 0 if result.ok else 1


def run_memory_propose(args: argparse.Namespace) -> int:
    data_dir = _data_dir_from_args(args, validate_tree=True)
    if data_dir is None:
        return 1
    try:
        result = propose_memory_fact(
            data_dir,
            actor=args.actor,
            scope=args.scope,
            slug=args.slug,
            fact_file=Path(args.file),
            source=args.source,
            tags=_split_csv(args.tags),
            pinned=args.pinned,
            supersedes=_split_csv(args.supersedes),
        )
    except Exception as exc:  # noqa: BLE001 - the memory service has no narrower error contract
        return _print_memory_error("propose", exc)
    _print_json(
        {
            "ok": True,
            "op": "propose",
            "propose_id": result.propose_id,
            "proposal": str(result.path),
            "fact": f"{result.scope_dir}/{result.slug}",
            "actor": result.actor,
            "source": result.source,
            "supersedes": list(result.supersedes),
        }
    )
    return 0


def run_memory_commit(args: argparse.Namespace) -> int:
    data_dir = _data_dir_from_args(args, validate_tree=True)
    if data_dir is None:
        return 1
    try:
        result = commit_memory_proposal(
            data_dir,
            _instance_dir(args.instance),
            actor=args.actor,
            propose_id=args.propose_id,
        )
    except Exception as exc:  # noqa: BLE001 - the memory service has no narrower error contract
        return _print_memory_error("commit", exc)
    _print_memory_write_result(result)
    return 0


def run_memory_supersede(args: argparse.Namespace) -> int:
    data_dir = _data_dir_from_args(args, validate_tree=True)
    if data_dir is None:
        return 1
    try:
        result = supersede_memory_fact(
            data_dir,
            _instance_dir(args.instance),
            actor=args.actor,
            scope=args.scope,
            slug=args.slug,
            fact_file=Path(args.file),
            supersedes=_split_csv(args.supersedes),
            source=args.source,
            tags=_split_csv(args.tags),
            pinned=args.pinned,
        )
    except Exception as exc:  # noqa: BLE001 - the memory service has no narrower error contract
        return _print_memory_error("supersede", exc)
    _print_memory_write_result(result)
    return 0


def run_knowledge_write(args: argparse.Namespace) -> int:
    try:
        if args.dir is not None:
            result = write_knowledge_directory(
                _instance_dir(args.instance),
                directory=args.path,
                actor=args.actor,
                source_dir=Path(args.dir),
            )
        else:
            result = write_knowledge_document(
                _instance_dir(args.instance),
                document=args.path,
                actor=args.actor,
                source_file=Path(args.file),
            )
    except KnowledgeValidationError as exc:
        _print_json({"ok": False, "op": "write", "error": "validation", "message": str(exc)})
        return MEMORY_EXIT_VALIDATION
    except (KnowledgeError, StateRepoError) as exc:
        _print_json({"ok": False, "op": "write", "error": "runtime", "message": str(exc)})
        return 1
    _print_json(
        {
            "ok": True,
            "op": "write",
            "document": result.document,
            "path": str(result.path),
            "commit": result.commit,
            "actor": result.actor,
            "changed": result.changed,
        }
    )
    return 0


def run_knowledge_list(args: argparse.Namespace) -> int:
    try:
        documents = list_knowledge_documents(_instance_dir(args.instance))
    except (KnowledgeError, StateRepoError) as exc:
        _print_json({"ok": False, "op": "list", "error": "runtime", "message": str(exc)})
        return 1
    _print_json({"ok": True, "op": "list", "documents": list(documents)})
    return 0


def _print_memory_write_result(result) -> None:
    _print_json(
        {
            "ok": True,
            "op": result.op,
            "commit": result.commit,
            "journal": str(result.facts_dir),
            "fact": result.fact,
            "actor": result.actor,
            "source": result.source,
            "changed_facts": list(result.changed_facts),
            "propose_id": result.propose_id,
        }
    )


def _print_memory_error(op: str, exc: Exception) -> int:
    if isinstance(exc, MemoryExportPublishError):
        result = exc.result
        _print_json(
            {
                "ok": False,
                "op": op,
                "error": "export",
                "message": str(exc),
                "commit": result.commit,
                "journal": str(result.facts_dir),
                "fact": result.fact,
                "actor": result.actor,
                "source": result.source,
                "changed_facts": list(result.changed_facts),
                "propose_id": result.propose_id,
            }
        )
        return 1
    if isinstance(exc, MemoryValidationError):
        code = MEMORY_EXIT_VALIDATION
        kind = "validation"
    elif isinstance(exc, MemoryPermissionError):
        code = MEMORY_EXIT_PERMISSION
        kind = "permission"
    elif isinstance(exc, MemoryLockError):
        code = MEMORY_EXIT_LOCKED
        kind = "locked"
    else:
        code = 1
        kind = "runtime"
    _print_json({"ok": False, "op": op, "error": kind, "message": str(exc)})
    return code


def _print_json(payload: dict) -> None:
    from ummanu.cli_output import print_json

    print_json(payload)


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def run_export_runs(args: argparse.Namespace) -> int:
    data_dir = _data_dir_from_args(args, validate_tree=True)
    if data_dir is None:
        return 1
    try:
        result = export_runs(data_dir, state_dir=Path(args.state_dir))
    except RuntimeError as exc:
        print(f"ummanu data export-runs: {exc}")
        return 1
    print(f"run records: {result.count}")
    print(f"export: {result.path}")
    print("status: ok")
    return 0


def run_data_snapshot(args: argparse.Namespace) -> int:
    """One exporter window against `--snapshot-repo`, whatever the live root is.

    The live root is only read (and its writer lock taken), so this works on a live root that is
    still a Git work tree without touching its repository. Nothing is pushed. With `--seed-from`
    it runs no window and seeds the empty repository from the legacy branch tip instead.
    """
    data_dir = _data_dir_from_args(args, validate_tree=False)
    if data_dir is None:
        return 1
    exporter = SnapshotExporter(
        data_dir,
        _instance_dir(args.instance),
        snapshot_repo=Path(args.snapshot_repo),
        state_dir=Path(args.state_dir),
    )
    if args.seed_from:
        seeded = exporter.seed(Path(args.seed_from))
        print(json.dumps({**seeded, "snapshot_repo": str(exporter.snapshot_repo)}, sort_keys=True))
        return 0 if seeded["status"] in {"seeded", "unchanged"} else 1
    result = exporter.write()
    print(json.dumps({**result.to_json(), "snapshot_repo": str(exporter.snapshot_repo)}, sort_keys=True))
    return 0 if result.status in {"committed", "unchanged"} else 1


def run_export_transcripts(args: argparse.Namespace) -> int:
    data_dir = _data_dir_from_args(args, validate_tree=True)
    if data_dir is None:
        return 1
    roots = [Path(root) for root in args.roots] if args.roots else None
    try:
        result = export_transcripts(data_dir, roots=roots, copy=args.copy)
    except RuntimeError as exc:
        print(f"ummanu data export-transcripts: {exc}")
        return 1
    print(f"transcripts: {result.count}")
    print(f"inventory: {result.path}")
    print("status: ok")
    return 0


def run_export_artifacts(args: argparse.Namespace) -> int:
    data_dir = _data_dir_from_args(args, validate_tree=True)
    if data_dir is None:
        return 1
    try:
        result = export_artifacts(data_dir)
    except RuntimeError as exc:
        print(f"ummanu data export-artifacts: {exc}")
        return 1
    print(f"artifacts: {result.count}")
    print(f"inventory: {result.path}")
    print("status: ok")
    return 0


def run_backup_create(args: argparse.Namespace) -> int:
    kinds = ("core", "full") if args.kind == "both" else (args.kind,)
    try:
        results = create_backups(
            Path(args.instance),
            data_dir=Path(args.data_dir) if args.data_dir else None,
            copy_transcripts=args.copy_transcripts,
            backup_kinds=kinds,
        )
    except RuntimeError as exc:
        print(f"ummanu backup create: {exc}")
        return 1

    for result in results:
        print(f"archive: {result.archive}")
        print(f"kind: {result.manifest.get('backup_kind', 'full')}")
        print(f"version: {result.manifest['version']}")
    print("status: ok")
    return 0


def run_checkpoint_command(args: argparse.Namespace) -> int:
    from ummanu.dispatch.bootstrap import runtime_from_args
    from ummanu.dispatch.types import DispatcherError, HostError

    try:
        runtime = runtime_from_args(args.instance, None, host_mode="real", owner="checkpoint")
        result = run_checkpoint(runtime)
    except (DispatcherError, HostError, OSError, RuntimeError) as exc:
        print(json.dumps({"status": "failed", "step": "checkpoint-run", "reason": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 1 if result["status"] == "failed" else 0


def run_instance_maintenance(args: argparse.Namespace) -> int:
    if getattr(args, "residue_inventory", False) or getattr(args, "residue_replay", False):
        return run_residue_maintenance(args)
    if getattr(args, "project", None) or getattr(args, "target", None) or getattr(args, "manifest", None):
        print(json.dumps({"status": "refused", "error": "--project, --target and --manifest belong to "
                          "--residue-inventory or --residue-replay"}, sort_keys=True))
        return 2
    from ummanu.infra import instance_maintenance
    from ummanu.runtime.paths import instance_dir

    try:
        result = instance_maintenance.run(instance_dir(args.instance))
    except state_repo.StateRepoError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, sort_keys=True))
        return 1
    cleanup = instance_maintenance.cleanup_docker()
    failed = bool(cleanup["findings"])
    print(json.dumps({"status": "failed" if failed else "ok", **result, "cleanup": cleanup}, sort_keys=True))
    return 1 if failed else 0


def run_residue_maintenance(args: argparse.Namespace) -> int:
    from ummanu.dispatch.bootstrap import runtime_from_args
    from ummanu.dispatch.cleanup import UnknownProject
    from ummanu.dispatch.types import DispatcherError, HostError
    project = getattr(args, "project", None)
    targets = list(getattr(args, "target", None) or [])
    digests = list(getattr(args, "manifest", None) or [])
    # No global batch exists: a replay names one project and its exact manifest targets.
    refusal = ""
    if args.residue_replay and (not project or not targets):
        refusal = "--residue-replay needs --project and at least one --target with its --manifest digest"
    elif args.residue_replay and len(digests) != len(targets):
        refusal = "--residue-replay needs one --manifest digest per --target, in the same order"
    elif not args.residue_replay and (targets or digests):
        refusal = "--target and --manifest belong to --residue-replay"
    if refusal:
        print(json.dumps({"status": "refused", "error": refusal}, sort_keys=True))
        return 2
    try:
        runtime = runtime_from_args(args.instance, None, host_mode="real", owner="instance-maintenance")
        result: dict = {}
        if args.residue_replay:
            result["replay"] = runtime.cleanup.replay_targets(project, list(zip(targets, digests)))
        result.update(runtime.cleanup.inventory(project=project))
        failed = (any(row["status"] == "pending" for row in result["intents"] + result["residue"])
                  or any(item["status"] in {"pending", "refused"} for item in result.get("replay", [])))
        print(json.dumps({"status": "pending" if failed else "ok", **result}, sort_keys=True))
        return 1 if failed else 0
    except UnknownProject as exc:
        print(json.dumps({"status": "refused", "error": str(exc)}, sort_keys=True))
        return 2
    except (DispatcherError, HostError, OSError) as exc:
        print(json.dumps({"status": "pending", "error": str(exc)}, sort_keys=True))
        return 1


def run_backup_verify(args: argparse.Namespace) -> int:
    result = verify_backup(Path(args.archive))
    print(f"archive: {args.archive}")
    if result.manifest:
        print(f"kind: {result.manifest.get('backup_kind', 'full')}")
        print(f"version: {result.manifest.get('version', '(unknown)')}")
    if result.warnings:
        print(f"warnings: {len(result.warnings)}")
        for warning in result.warnings:
            print(f"  {warning}")
    findings = list(result.findings)
    if args.strict:
        findings.extend(result.warnings)
    if findings:
        print(f"findings: {len(findings)}")
        for finding in findings:
            print(f"  {finding}")
    if result.code == 2:
        print("status: unavailable")
        return 2
    if findings:
        print("status: findings")
        return 1
    print("status: ok")
    return 0


def print_host_inventory(
    report,
    args: argparse.Namespace,
    *,
    expected=None,
    collected: CollectResult | None = None,
    diffs: dict[str, KindDiff] | None = None,
) -> tuple[bool, bool, CollectResult]:
    """Print the read-only host inventory: matched / missing / unmanaged per kind.

    Returns True if any kind could not be inspected (reported as unavailable).
    """
    if expected is None or collected is None or diffs is None:
        expected, collected, diffs = collect_host_inventory(report, args)

    print()
    print("host inventory: read-only")
    if collected.inventory.runtime_scopes is not None:
        for unit, scope in sorted(collected.inventory.runtime_scopes.scopes.items()):
            print(f"runtime scope: {unit} preserved by {scope['role']} lifecycle ({scope['generation']})")
    for kind in KINDS:
        reason = collected.errors.get(kind)
        if reason:
            print(f"{kind}:")
            print(f"  unavailable: {reason}")
        else:
            _print_kind(kind, diffs[kind])
    parity_findings = any(
        diff.missing_on_host or diff.unmanaged_on_host
        for kind, diff in diffs.items()
        if kind not in collected.errors
    )
    runtime_findings = _print_unit_runtime(expected, collected)
    return bool(collected.errors), parity_findings or runtime_findings, collected


def collect_host_inventory(report, args: argparse.Namespace):
    assert report.data_dir is not None
    runtime_user, _ = resolve_runtime_owner(report.instance_path.parent)
    source = FixtureHostSource(Path(args.host_fixture)) if args.host_fixture else LiveHostSource(runtime_user)
    packaged = resolve_installed_packaged(
        report.instance,
        instance_path=report.instance_path.parent,
        data_dir=report.data_dir,
    )
    expected = build_doctor_expectations(
        report.instance,
        report.bindings,
        packaged=packaged,
        data_dir=report.data_dir,
    )
    collected = source.collect(expected)
    return expected, collected, inventory(expected, collected.inventory)


def _print_kind(kind: str, diff: KindDiff) -> None:
    print(f"{kind}:")
    print(f"  matched: {_join(diff.matched)}")
    print(f"  missing-on-host: {_join(diff.missing_on_host)}")
    print(f"  unmanaged-on-host: {_join(diff.unmanaged_on_host)}")


def _print_unit_runtime(expected, collected: CollectResult) -> bool:
    """Report required enabled/active state separately from unit-file parity."""
    if "units" in collected.errors:
        return False
    findings = _unit_runtime_findings(expected, collected)
    if findings:
        print("unit runtime findings:")
        for finding in findings:
            print(f"  {finding}")
    return bool(findings)


def _unit_runtime_findings(expected, collected: CollectResult) -> list[str]:
    return [finding.render() for finding in assess_unit_runtime(expected.unit_runtime, collected)]


def _join(names: list[str]) -> str:
    return ", ".join(names) if names else "(none)"


def _instance_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path / "instance.yaml" if path.is_dir() else path


def _instance_dir(value: str) -> Path:
    """The private repo root. `--instance` may name it or its instance.yaml."""
    return _instance_path(value).parent


def _data_dir_from_args(args: argparse.Namespace, *, validate_tree: bool) -> Path | None:
    if args.data_dir:
        return Path(args.data_dir).expanduser()

    if validate_tree:
        report = validate_instance(_instance_path(args.instance))
        if report.errors:
            print(f"ummanu data: {len(report.errors)} config problem(s):")
            for error in report.errors:
                print(f"  {error}")
            return None

    try:
        return instance_data_dir(_instance_path(args.instance))
    except DataDirError as exc:
        print(f"ummanu data: cannot resolve instance data_dir: {exc}")
        return None


def not_implemented(command: str):
    def handler(_args: argparse.Namespace) -> int:
        print(f"ummanu {command}: {NOT_IMPLEMENTED}")
        return 1

    return handler
