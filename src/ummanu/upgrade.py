"""``ummanu upgrade``: pull a new product version and re-materialize the host.

One materializer and three entry points into it: ``upgrade`` pulls the new version and then runs
it, a fresh install runs it against an empty host, recovery runs it against a half-built one.
Nothing about a step knows which of the three called it, which is what makes them stay identical.

Every step is idempotent and reports one of ``changed``/``unchanged``/``skipped``/``failed``. A
failed step stops the run: later steps assume the earlier ones landed. ``--dry-run`` runs the
same decisions and performs no writes."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ummanu.config import validate_instance
from ummanu.host_apply import (
    HostCommandError,
    SystemdUnitInstaller,
    resolve_runtime_owner,
)
from ummanu.installation.upgrade.checkout import (
    _git,
    record_change_plan,
    step_pull,
)
from ummanu.installation.upgrade.dependencies import (
    step_dependencies,
    step_dependency_provenance,
)
from ummanu.installation.upgrade.fingerprints import SourceCut, SourceCutInvalidated
from ummanu.installation.upgrade.host import (
    step_board_store,
    step_board_store_provision,
    step_board_store_roles,
    step_host,
    step_pipeline_state,
    step_runtime_owner,
    step_web_front_config,
)
from ummanu.installation.upgrade.services import (
    step_memory,
    step_po,
    step_verify,
    step_web,
)
from ummanu.installation.upgrade.types import (
    GitError,
    StepResult as StepResult,
    UpgradeContext as UpgradeContext,
    UpgradeResult as UpgradeResult,
)
from ummanu.installation.upgrade.workspaces import (
    step_codex_home,
    step_git_hooks,
    step_head_registry,
    step_instance_packing,
    step_interactive_workspace,
    step_memory_clients,
    step_memory_pack,
    step_po_workspace,
    step_po_workspace_owner,
    step_registries,
    step_role_skills,
    step_worktrees,
)
from ummanu.runtime.cli_composition import add_command_parser
from ummanu.runtime.paths import add_instance_argument, configured_product_root

# Validate registries before any mutating materialization step.
STEPS: tuple[Callable[[UpgradeContext], StepResult], ...] = (
    step_pull,
    step_registries,
    step_memory_pack,
    step_runtime_owner,
    step_dependencies,
    step_dependency_provenance,
    step_board_store_provision,
    step_board_store,
    step_board_store_roles,
    step_memory_clients,
    step_codex_home,
    step_git_hooks,
    step_po_workspace,
    step_interactive_workspace,
    step_head_registry,
    step_instance_packing,
    step_worktrees,
    # Right after the worktrees: a recreated pipeline worktree has no untracked run journals.
    step_pipeline_state,
    step_role_skills,
    step_po_workspace_owner,
    # Before the host step: it enables and starts the front, whose ExecStartPre validates this file.
    step_web_front_config,
    step_host,
    step_memory,
    # Never kills a running PO turn: the service restarts itself once idle.
    step_po,
    # Last of the materializing steps: a restart is the moment the new code becomes the code that
    # answers, so it follows the checkout, the dependencies, the schemas and the unit.
    step_web,
    step_verify,
)


def run_steps(context: UpgradeContext, steps=STEPS) -> UpgradeResult:
    result = UpgradeResult()
    cut = SourceCut()
    context.source_cut = cut
    try:
        for step in steps:
            try:
                cut.check(context.product_root)
                outcome = step(context)
                # Preserve an existing step failure and its diagnostic precedence.
                if not outcome.failed:
                    cut.check(context.product_root)
            except (HostCommandError, SourceCutInvalidated) as exc:
                outcome = StepResult(step.__name__.removeprefix("step_"), "failed", str(exc))
            result.steps.append(outcome)
            if outcome.failed:
                break
    finally:
        cut.close()
        context.source_cut = None
    return result


def running_product_root() -> Path:
    """The checkout this module was imported from.

    For reading what this process itself ships. Never for deciding what to install: that is
    `default_product_root`.
    """
    return Path(__file__).resolve().parents[2]


def default_product_root() -> Path:
    """The checkout an install or upgrade materializes when nothing names one.

    The configured one, or ``~/ummanu`` — never the checkout the running module happens to sit in.
    A candidate checkout is a normal place to run ``ummanu upgrade`` from, and installing whatever
    executed the command would make the caller's working directory decide the product version.
    ``--product-root`` and ``UMMANU_REPO`` still win, in that order.
    """
    return configured_product_root()


def run_upgrade(args) -> int:
    report = validate_instance(Path(args.instance))
    if not report.ok:
        print(f"ummanu upgrade: {len(report.errors)} config problem(s):")
        for error in report.errors:
            print(f"  {error}")
        return 2
    product_root = Path(args.product_root).expanduser() if args.product_root else default_product_root()
    # Resolve the checkout owner before materializing home-relative paths.
    instance_path = report.instance_path.parent
    try:
        runtime_user, runtime_home = resolve_runtime_owner(instance_path, getattr(args, "runtime_user", None))
    except ValueError as exc:
        print(f"ummanu upgrade: {exc}")
        return 2
    context = UpgradeContext(
        instance_path=instance_path,
        product_root=product_root,
        base_branch=args.base_branch,
        dry_run=args.dry_run,
        units=SystemdUnitInstaller(runtime_user=runtime_user),
        host_fixture=Path(args.host_fixture) if args.host_fixture else None,
        pull=not args.no_pull,
        report=report,
        runtime_user=runtime_user,
        runtime_home=runtime_home,
    )
    handoff = os.environ.get("UMMANU_UPGRADE_HANDOFF")
    if handoff:
        try:
            marker = json.loads(handoff)
            before = str(marker["before"])
            after = str(marker["after"])
            if _git(product_root, ["rev-parse", "HEAD"]) != after:
                raise ValueError("checkout no longer matches the pulled revision")
            if _git(product_root, ["status", "--porcelain"]):
                raise ValueError("checkout became dirty during pulled-code handoff")
            changed_paths = marker.get("changed_paths")
            if not isinstance(changed_paths, list) or not all(
                isinstance(path, str) for path in changed_paths
            ):
                raise ValueError("handoff has no valid changed-path set")
        except (GitError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            print(f"ummanu upgrade: invalid pulled-code handoff: {exc}")
            return 1
        context.pull = False
        context.handoff_before = before
        context.handoff_after = after
        record_change_plan(context, tuple(changed_paths))
    elif context.pull and not context.dry_run:
        pulled = step_pull(context)
        if pulled.failed:
            result = UpgradeResult([pulled])
            print(result.render())
            return 1
        if pulled.status == "changed":
            before = context.pulled_before
            after = context.pulled_after
            if before is None or after is None:
                print("ummanu upgrade: pull did not record the before/after revisions")
                return 1
            try:
                _exec_pulled_upgrade(
                    args,
                    product_root,
                    before=before,
                    after=after,
                    changed_paths=context.changed_paths,
                )
            except OSError as exc:
                print(f"ummanu upgrade: could not run pulled revision {after[:12]}: {exc}")
                return 1
            raise AssertionError("exec returned unexpectedly")
        context.pull_result = pulled
    result = run_steps(context)
    if args.json:
        print(
            json.dumps(
                {
                    "status": "ok" if result.ok else "failed",
                    "dry_run": context.dry_run,
                    "steps": [
                        {"name": step.name, "status": step.status, "detail": step.detail}
                        for step in result.steps
                    ],
                },
                sort_keys=True,
                indent=2,
            )
        )
    else:
        print(result.render())
    return 0 if result.ok else 1


def _exec_pulled_upgrade(
    args: Any,
    product_root: Path,
    *,
    before: str,
    after: str,
    changed_paths: tuple[str, ...],
) -> None:
    """Replace the import-bound process with the exact checkout revision it just pulled."""
    python = product_root / ".venv" / "bin" / "python"
    if not python.is_file():
        raise FileNotFoundError(f"the pulled checkout has no production interpreter at {python}")
    argv = [
        str(python),
        "-P",
        "-m",
        "ummanu",
        "upgrade",
        "--instance",
        str(args.instance),
        "--no-pull",
        "--base-branch",
        str(args.base_branch),
        "--product-root",
        str(product_root),
    ]
    if args.dry_run:
        argv.append("--dry-run")
    if getattr(args, "runtime_user", None):
        argv.extend(("--runtime-user", str(args.runtime_user)))
    if getattr(args, "host_fixture", None):
        argv.extend(("--host-fixture", str(args.host_fixture)))
    if args.json:
        argv.append("--json")
    environment = dict(os.environ)
    environment["UMMANU_UPGRADE_HANDOFF"] = json.dumps(
        {"before": before, "after": after, "changed_paths": list(changed_paths)}
    )
    os.execve(python, argv, environment)


def add_upgrade_command(subparsers) -> None:
    upgrade = add_command_parser(subparsers, "upgrade")
    add_instance_argument(upgrade)
    upgrade.add_argument("--dry-run", action="store_true", help="decide every step but write nothing")
    upgrade.add_argument("--no-pull", action="store_true", help="re-materialize without moving the checkout")
    upgrade.add_argument("--base-branch", default="main")
    upgrade.add_argument(
        "--product-root",
        help="product checkout to upgrade (defaults to UMMANU_REPO, else ~/ummanu)",
    )
    upgrade.add_argument(
        "--runtime-user",
        help="account this installation belongs to, whose home every home-relative path is "
        "materialized under (default: the owner of the instance checkout)",
    )
    upgrade.add_argument("--host-fixture", metavar="DIR", help=argparse.SUPPRESS)
    upgrade.add_argument("--json", action="store_true", help="emit the step report as JSON")
    upgrade.set_defaults(handler=run_upgrade)
