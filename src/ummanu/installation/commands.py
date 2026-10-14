"""Supported fresh-install and recovery flow, from a legacy Git checkpoint or an exporter snapshot.

The private instance repository is the only portable input. This module turns its normalized
checkpoint into a new local data plane and then calls the same materializer ``ummanu upgrade``
uses.

The secret store opens before anything reads ``runtime.env``, because on a clean host that file
does not exist yet: it is what the store writes once the recovery phrase rebuilds the
installation key. Without the phrase the recovery still brings back everything that needs no
credentials and reports which secrets stayed locked or went missing.

It deliberately does not install the board store (`bootstrap` does): its supported version is a
product decision gate, so a missing store is reported before any live state is written."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from ummanu.cli_output import print_error
from ummanu.config import validate_instance
from ummanu.installation.clone import (
    SnapshotCheckout,
    _clone_or_reuse,
    _reads_remote_shape,
    _snapshot_checkout,
)
from ummanu.installation.credentials import (
    _add_secret_steps,
    _blocked_by_secrets,
    _bootstrap_credential,
    _checked_file_targets,
    _open_secret_store,
    _recovery_phrase,
    _runtime_env_file,
    _runtime_environment,
    _secret_store_step,
    add_bootstrap_credential_arguments,
)
from ummanu.installation.materialize import (
    _publish_recovered_memory_export,
    _read_recovery_progress,
    _recovery_identity,
    _unchanged_memory_step,
    _write_recovery_progress,
    check_prerequisites,
    materialize_checkpoint,
    materialize_head_registry,
    materialize_host,
    materialize_pipeline_state,
    pipeline_state_path,
)
from ummanu.installation.models import RECOVERY_PROGRESS_FILE, InstallError, InstallResult
from ummanu.installation.provision import provision_codex_home, provision_project_checkouts
from ummanu.installation.support import (
    _ensure_installation_user,
    _establish_recovery_ownership_barrier,
    _named_path,
    _set_installation_owner,
)
from ummanu.installation.upgrade.types import UpgradeContext
from ummanu.memory.config import index_matches, memory_config
from ummanu.projects.availability import ProjectAvailability
from ummanu.restore import (
    RestoreError,
    import_normalized_board,
    mark_reconcile_applied,
    rebuild_memory_index,
    restore_findings,
    restore_state,
)
from ummanu.runtime.cli_composition import add_command_parser
from ummanu.runtime.paths import PRODUCT_DIRNAME, PRODUCT_ENV
from ummanu.runtime_env import RuntimeEnvError, RuntimeEnvMissing, read_runtime_env
from ummanu.secret_store import key_path
from ummanu.upgrade import default_product_root


def _validated_instance(instance_dir: Path):
    report = validate_instance(instance_dir)
    if not report.ok:
        raise InstallError("invalid cloned instance: " + "; ".join(map(str, report.errors)))
    return report



PRODUCT_MARKERS = (Path("packaging") / "codex-home", Path("skills") / "manifest.toml")



def _product_root(args: argparse.Namespace) -> Path:
    """The checkout this install materializes from, refused here if it is not one.

    Named, else configured, else the home default — never the checkout running this module. A path
    that holds no product would otherwise surface as an ENOENT from whichever step read it first.
    """
    if args.product_root:
        root = Path(args.product_root).expanduser().resolve()
        source = "--product-root"
    else:
        root = default_product_root()
        source = f"{PRODUCT_ENV} or the {PRODUCT_DIRNAME} default under this account's home"
    missing = [str(marker) for marker in PRODUCT_MARKERS if not (root / marker).exists()]
    if missing:
        raise InstallError(
            f"not a product checkout: {root} (from {source}) has no {missing[0]}; "
            "name the checkout to install with --product-root"
        )
    return root



def _restore_without_credentials(
    args: argparse.Namespace,
    target: Path,
    result: InstallResult,
    bootstrap_credential: Path | None,
    checkpoint_root: Path | None = None,
    written: tuple[Path, ...] = (),
) -> None:
    """Recover everything that does not go through the board.

    A locked store costs the operator their credentials, not their installation. What is left undone
    is named as skipped rather than quietly attempted with half a configuration, and the caller then
    fails with the secret report.
    """
    report = _validated_instance(target)
    assert report.data_dir is not None
    data_dir = report.data_dir
    checkpoint_root = target if checkpoint_root is None else checkpoint_root
    identity = _recovery_identity(target, report.bindings, checkpoint_root=checkpoint_root)
    progress_path = data_dir / RECOVERY_PROGRESS_FILE
    progress = _read_recovery_progress(progress_path, identity)
    checkpoint_complete = progress.get("checkpoint") == "complete"
    cards, runs = materialize_checkpoint(
        checkpoint_root, data_dir, dry_run=args.dry_run or checkpoint_complete, own=written
    )
    if args.dry_run:
        result.add(
            "checkpoint",
            "would-change",
            f"would materialize {cards} board card(s), {runs} run record(s)",
        )
        return
    _set_installation_owner(data_dir, args.installation_user)
    result.add(
        "checkpoint",
        "unchanged" if checkpoint_complete else "changed",
        f"{cards} board card(s), {runs} run record(s) verified"
        if checkpoint_complete
        else f"{cards} board card(s), {runs} run record(s)",
    )
    _write_recovery_progress(progress_path, identity, checkpoint="complete")
    host = report.host if isinstance(report.host, dict) else {}
    memory = memory_config(host)
    if progress.get("memory") == "complete" and index_matches(data_dir / "memory" / "index.sqlite", memory):
        result.add("memory", *_unchanged_memory_step(data_dir, target, args.installation_user))
    else:
        _write_recovery_progress(progress_path, identity, memory="started")
        count = rebuild_memory_index(
            data_dir, target, model=memory.model, dim=memory.dim, threads=memory.threads, isolated=True
        )
        _publish_recovered_memory_export(data_dir, target, args.installation_user)
        result.add("memory", "changed", f"rebuilt index for {count} fact(s)")
        _write_recovery_progress(progress_path, identity, memory="complete")
    project_results = provision_project_checkouts(
        report.bindings,
        args.installation_user,
        instance_dir=target,
        bootstrap_credential=bootstrap_credential,
        progress_path=progress_path,
        recovery_identity=identity,
    )
    result.projects.extend(project_results)
    seeded = provision_codex_home(_product_root(args), args.installation_user, data_dir=data_dir)
    cloned = sum(project.outcome == "cloned" for project in project_results)
    failed = sum(project.outcome == "failed" for project in project_results)
    result.add(
        "runtime",
        "degraded" if failed else ("changed" if cloned or seeded else "unchanged"),
        f"{cloned} project checkout(s) cloned, {failed} unavailable, {seeded} CODEX_HOME file(s) seeded",
    )
    result.add("board", "skipped", "requires an available board backend after locked secret recovery")
    result.add("host", "skipped", "requires full recovery before host materialization")



def install(args: argparse.Namespace) -> InstallResult:
    result = InstallResult()
    target = Path(args.instance_dir).expanduser().resolve()
    recovery = bool(args.recover)
    disposable_bootstrap: Path | None = None
    bootstrap: Path | None = None
    recovery_data_dir: Path | None = None
    recovery_runtime_paths: list[Path] = []
    snapshot: SnapshotCheckout | None = None
    if args.adopt:
        result.add("mode", "failed", "full live-host adoption is not supported by this flow")
        return result
    try:
        # bootstrap creates this user before the first install. Its stamp is also
        # checked by _clone_or_reuse, so it is the narrow exception to the usual
        # refusal to touch an existing installation user.
        bootstrap_checkout = (target / ".ummanu-bootstrap").is_file()
        _ensure_installation_user(
            args.installation_user,
            recovery=recovery or bootstrap_checkout,
            dry_run=args.dry_run,
        )
        result.add(
            "installation-user",
            "unchanged"
            if recovery or bootstrap_checkout
            else ("would-change" if args.dry_run else "changed"),
            args.installation_user,
        )
        # A retry may need its already-restored managed credential to update the
        # checkpoint checkout. Repair the previous partial run before that first
        # user-owned consumer; the post-secret call below closes the same barrier
        # for files materialized by this invocation.
        if recovery and not args.dry_run and key_path(target).exists():
            ownership_report = _validated_instance(target)
            assert ownership_report.data_dir is not None
            recovery_data_dir = ownership_report.data_dir
            _establish_recovery_ownership_barrier(
                target,
                recovery_data_dir,
                args.installation_user,
            )
        # A supplied bootstrap capability can authenticate a reused recovery
        # checkout too, so consume it only when this non-dry-run invocation
        # will execute one of those remote paths.
        needs_clone = not target.exists() or (target.is_dir() and not any(target.iterdir()))
        if not args.dry_run and (
            needs_clone
            or getattr(args, "bootstrap_credential_file", None)
            or getattr(args, "bootstrap_credential_stdin", False)
        ):
            bootstrap, disposable_bootstrap = _bootstrap_credential(args, target)
        # The one shape decision (docs/RECOVERY.md, "Two remote shapes"). An exporter snapshot takes
        # the snapshot path; a legacy checkpoint and every target that does not read the shape take
        # the clone step below unchanged.
        if _reads_remote_shape(target, recovery=recovery, dry_run=args.dry_run):
            snapshot = _snapshot_checkout(
                args.instance_remote,
                target,
                dry_run=args.dry_run,
                bootstrap_credential=bootstrap,
                installation_user=args.installation_user,
            )
        if snapshot is not None:
            detail = snapshot.detail
            result.add(
                "instance-checkout",
                "would-change" if args.dry_run else ("changed" if snapshot.changed else "unchanged"),
                detail,
            )
            recovery_runtime_paths.append(snapshot.repository)
        else:
            detail = _clone_or_reuse(
                args.instance_remote,
                target,
                recovery=recovery,
                dry_run=args.dry_run,
                bootstrap_credential=bootstrap,
                installation_user=args.installation_user,
            )
            result.add(
                "instance-checkout",
                "unchanged"
                if detail.startswith("reused")
                else ("would-change" if args.dry_run else "changed"),
                detail,
            )
        # Board and runs: the checkout of a legacy checkpoint, or the snapshot's extracted tree.
        checkpoint_root = snapshot.checkpoint_root if snapshot is not None else target
        if args.dry_run and not target.exists():
            result.add("secret-store", "skipped", "available only after clone")
            result.add("runtime-env", "skipped", "available only after clone")
            return result
        # The store opens before anything reads runtime.env, because on a clean
        # host that file is the store's output and does not exist yet.
        runtime_env = _runtime_env_file(target, args.runtime_env)
        # Before the store writes: which of its file targets in the data directory are new.
        created = _checked_file_targets(target)
        secrets = _open_secret_store(
            target,
            runtime_env,
            phrase=_recovery_phrase(args, target),
            dry_run=args.dry_run,
        )
        result.add("secret-store", *_secret_store_step(secrets))
        _add_secret_steps(result, secrets)
        # This run's own files in the data directory: the file targets it created there.
        written = tuple(item.path for item in secrets.materialized if _named_path(item.path) in created)

        # Secret recovery can create a root-owned 0600 installation key. Cross
        # ownership once, before any runtime-user Git or remote consumer starts.
        if recovery and not args.dry_run:
            ownership_report = _validated_instance(target)
            assert ownership_report.data_dir is not None
            recovery_data_dir = ownership_report.data_dir
            _establish_recovery_ownership_barrier(
                target,
                recovery_data_dir,
                args.installation_user,
            )
            result.add(
                "recovery-ownership",
                "unchanged",
                "restored paths handed to the installation user",
            )

        runtime_loaded = True
        try:
            values = read_runtime_env(target, args.runtime_env)
        except RuntimeEnvMissing as exc:
            # A legacy board-only catalog is inert after this migration. It
            # must not force a recovery phrase merely to recreate transport.
            runtime_loaded = False
            runtime_required = any(
                entry.get("target") == "runtime-env" for entry in (*secrets.locked, *secrets.missing)
            )
            if not runtime_required:
                values = {}
            else:
                _restore_without_credentials(args, target, result, bootstrap, checkpoint_root, written)
                raise _blocked_by_secrets(exc, secrets, runtime_env) from None
        except RuntimeEnvError as exc:
            raise InstallError(str(exc)) from None
        try:
            canonical_runtime_env = runtime_env.resolve() == target.resolve() / "runtime.env"
        except OSError:
            canonical_runtime_env = False
        if bootstrap_checkout and runtime_loaded:
            # A bootstrap checkout may hold a runtime.env that carries none of the store's
            # variables, so on it the file being there does not say they arrived.
            unavailable = sorted(
                {
                    str(entry["environment"])
                    for entry in (*secrets.locked, *secrets.missing)
                    if entry.get("target") == "runtime-env" and entry.get("environment")
                }
                - set(values)
            )
            if unavailable:
                _restore_without_credentials(args, target, result, bootstrap, checkpoint_root, written)
                raise _blocked_by_secrets(
                    InstallError(f"runtime.env lacks {', '.join(unavailable)}"), secrets, runtime_env
                ) from None
        if not args.dry_run:
            if canonical_runtime_env:
                _set_installation_owner(runtime_env, args.installation_user)
            _set_installation_owner(target / ".gitignore", args.installation_user)
            _set_installation_owner(target / ".git", args.installation_user)
        result.add(
            "runtime-env",
            "unchanged" if runtime_loaded else "skipped",
            "host-only runtime configuration loaded"
            if runtime_loaded
            else "not required by this installation",
        )
        with _runtime_environment({**values, "UMMANU_INSTANCE": str(target)}):
            check_prerequisites(target)
            result.add("prerequisites", "unchanged", "PostgreSQL is reachable")
            report = _validated_instance(target)
            assert report.data_dir is not None
            data_dir = report.data_dir
            recovery_data_dir = data_dir
            identity = _recovery_identity(target, report.bindings, checkpoint_root=checkpoint_root)
            progress_path = data_dir / RECOVERY_PROGRESS_FILE
            progress = _read_recovery_progress(progress_path, identity)
            checkpoint_complete = progress.get("checkpoint") == "complete"
            cards, runs = materialize_checkpoint(
                checkpoint_root, data_dir, dry_run=args.dry_run or checkpoint_complete, own=written
            )
            if args.dry_run:
                result.add(
                    "checkpoint",
                    "would-change",
                    f"would materialize {cards} board card(s), {runs} run record(s)",
                )
                result.add("board", "would-change", f"would restore {cards} card(s) and verify parity")
                result.add("memory", "would-change", "would rebuild the index from checkpoint facts")
                result.add("host", "would-change", "would run the host materializer")
                result.add("status", "skipped", "preview made no recovery changes")
                return result
            _set_installation_owner(data_dir, args.installation_user)
            result.add(
                "checkpoint",
                "unchanged" if checkpoint_complete else "changed",
                f"{cards} board card(s), {runs} run record(s) verified"
                if checkpoint_complete
                else f"{cards} board card(s), {runs} run record(s)",
            )
            _write_recovery_progress(progress_path, identity, checkpoint="complete")
            product_root = _product_root(args)
            # Before the board: the import checks open sprints' observer heads against this pair.
            result.add(
                "head-registry",
                *materialize_head_registry(target, product_root, report, args.installation_user),
            )
            recovered_board_completion = (
                progress.get("board") == "started"
                and restore_state(data_dir).get("board_parity") == "complete"
            )
            if progress.get("board") == "complete" or recovered_board_completion:
                result.add("board", "unchanged", f"{cards} card(s) already restored at parity")
                if recovered_board_completion:
                    _write_recovery_progress(progress_path, identity, board="complete")
            else:
                _write_recovery_progress(progress_path, identity, board="started")
                restored = import_normalized_board(data_dir, instance=target)
                result.add("board", "changed", f"{restored} card(s) at parity")
                _write_recovery_progress(progress_path, identity, board="complete")
            host = report.host if isinstance(report.host, dict) else {}
            memory = memory_config(host)
            compatible_index = index_matches(data_dir / "memory" / "index.sqlite", memory)
            recovered_memory_completion = (
                progress.get("memory") == "started"
                and restore_state(data_dir).get("memory_index") == "complete"
                and compatible_index
            )
            if (progress.get("memory") == "complete" and compatible_index) or recovered_memory_completion:
                result.add("memory", *_unchanged_memory_step(data_dir, target, args.installation_user))
                if recovered_memory_completion:
                    _write_recovery_progress(progress_path, identity, memory="complete")
            else:
                _write_recovery_progress(progress_path, identity, memory="started")
                # In a child process: the host step starts `ummanu-memory-mcp`, and this process must
                # not still hold its own copy of the embedding model beside it (ummanu-53 P14).
                count = rebuild_memory_index(
                    data_dir,
                    target,
                    model=memory.model,
                    dim=memory.dim,
                    threads=memory.threads,
                    isolated=True,
                )
                _publish_recovered_memory_export(data_dir, target, args.installation_user)
                result.add("memory", "changed", f"rebuilt index for {count} fact(s)")
                _write_recovery_progress(progress_path, identity, memory="complete")
            project_results = provision_project_checkouts(
                report.bindings,
                args.installation_user,
                instance_dir=target,
                bootstrap_credential=bootstrap,
                progress_path=progress_path,
                recovery_identity=identity,
            )
            result.projects.extend(project_results)
            seeded = provision_codex_home(product_root, args.installation_user, data_dir=data_dir)
            cloned = sum(project.outcome == "cloned" for project in project_results)
            failed = sum(project.outcome == "failed" for project in project_results)
            project_availability = ProjectAvailability(
                frozenset(project.project_id for project in project_results if project.outcome == "failed")
            )
            result.add(
                "runtime",
                "degraded" if failed else ("changed" if cloned or seeded else "unchanged"),
                f"{cloned} project checkout(s) cloned, {failed} unavailable, {seeded} CODEX_HOME file(s) seeded",
            )
            restored_runs = 0
            pipeline_state_changed = False

            def restore_pipeline_source(context: UpgradeContext) -> None:
                nonlocal restored_runs, pipeline_state_changed
                state_path = pipeline_state_path(context.runtime_home or Path.home())
                recovery_runtime_paths.append(state_path.parent)
                restored = materialize_pipeline_state(
                    checkpoint_root,
                    state_path,
                    dry_run=False,
                )
                restored_runs = restored.records
                pipeline_state_changed = restored.changed
                _establish_recovery_ownership_barrier(
                    target,
                    data_dir,
                    args.installation_user,
                    additional_paths=(state_path.parent,),
                )

            host_result = materialize_host(
                target,
                product_root,
                Path(args.host_fixture).expanduser().resolve() if args.host_fixture else None,
                args.installation_user,
                before_host=restore_pipeline_source,
                project_availability=project_availability,
            )
            mark_reconcile_applied(data_dir)
            changed = sum(step.status == "changed" for step in host_result.steps)
            result.add(
                "host",
                "changed" if changed else "unchanged",
                f"materializer complete ({changed} changed step(s))",
            )
            result.add(
                "pipeline-state",
                "changed" if pipeline_state_changed else "unchanged",
                f"materialized {restored_runs} run record(s)",
            )
            findings = restore_findings(data_dir)
            if findings:
                raise InstallError("status findings: " + "; ".join(findings))
            if not recovery:
                (target / ".ummanu-bootstrap").unlink(missing_ok=True)
            result.add(
                "status",
                "degraded" if failed else "unchanged",
                f"core ready; {failed} project checkout(s) unavailable; rerun ummanu recover with the same inputs"
                if failed
                else "board, memory and operational configuration are ready"
                if secrets.complete
                else f"board and memory are ready, but {secrets.summary()}",
            )
    except (InstallError, RestoreError, RuntimeError) as exc:
        result.add("install", "failed", str(exc))
    finally:
        if recovery and not args.dry_run:
            try:
                _establish_recovery_ownership_barrier(
                    target,
                    recovery_data_dir,
                    args.installation_user,
                    additional_paths=tuple(recovery_runtime_paths),
                )
            except InstallError as exc:
                if any(step.status == "failed" for step in result.steps):
                    result.add(
                        "recovery-ownership-cleanup",
                        "failed",
                        f"ownership cleanup also failed: {exc}; original failure is retained above",
                    )
                else:
                    result.add("install", "failed", str(exc))
        if disposable_bootstrap is not None:
            disposable_bootstrap.unlink(missing_ok=True)
        if snapshot is not None:
            shutil.rmtree(snapshot.scratch, ignore_errors=True)
    return result



def run_install(args: argparse.Namespace) -> int:
    if args.bootstrap_credential_stdin and args.recovery_phrase_stdin:
        print_error(
            {
                "code": "usage",
                "message": "--bootstrap-credential-stdin and --recovery-phrase-stdin cannot share standard input; use a mode-0600 file for one input",
            },
            stderr=True,
        )
        return 2
    result = install(args)
    if args.json:
        print(
            json.dumps(
                {
                    "status": result.status,
                    "steps": [step.__dict__ for step in result.steps],
                    "projects": [project.__dict__ for project in result.projects],
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(result.render())
    return 0 if result.ok else 1



def add_install_commands(subparsers) -> None:
    def arguments(parser, *, recovery_default: bool) -> None:
        parser.add_argument("--instance-remote", required=True, help="private Git checkpoint remote")
        parser.add_argument("--instance-dir", required=True, help="local checkout destination")
        parser.add_argument("--installation-user", required=True, help="dedicated OS account")
        parser.add_argument(
            "--runtime-env",
            help="host-only credentials file (default: INSTANCE/runtime.env)",
        )
        phrase = parser.add_mutually_exclusive_group()
        phrase.add_argument(
            "--recovery-phrase-file",
            help="read the recovery phrase from this file; never pass it on the command line",
        )
        add_bootstrap_credential_arguments(parser)
        phrase.add_argument(
            "--recovery-phrase-stdin",
            action="store_true",
            help="read the recovery phrase from standard input",
        )
        parser.add_argument("--product-root", help="installed product checkout")
        parser.add_argument(
            "--recover",
            action="store_true",
            default=recovery_default,
            help="resume or recover the same installation without overwriting local changes",
        )
        parser.add_argument(
            "--adopt",
            action="store_true",
            help="select the separate live-host adoption path",
        )
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--json", action="store_true")
        parser.add_argument("--host-fixture", help=argparse.SUPPRESS)
        parser.set_defaults(handler=run_install)

    fresh = add_command_parser(subparsers, "install")
    arguments(fresh, recovery_default=False)
    recover = add_command_parser(subparsers, "recover")
    arguments(recover, recovery_default=True)
