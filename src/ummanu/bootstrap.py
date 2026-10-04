"""Bootstrap the host-owned PostgreSQL board store and its Docker prerequisites.

The checkpoint deliberately does not carry these services or their credentials. They are
reproducible host state: this module installs Docker and Compose (and the distribution's Caddy when
the installation enables the web-front component), provisions the
PostgreSQL board store (`board/provision.py`), migrates it to this build's schema
(`board/migrate.py`) and verifies its role contract. That empty, migrated store is the whole board a fresh installation starts from:
cards come later from `task create` or from install recovery restoring a checkpoint into it.

The instance comes from the remote through recovery's own clone step (docs/RECOVERY.md, "Fresh
install and recovery"): a legacy checkpoint is cloned as a Git checkout, an exporter snapshot is laid
out as a plain live root and a snapshot repository, exactly as `recover` lays it out.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import time
from pathlib import Path

from ummanu import _proc
from ummanu._fsutil import write_text_atomic
from ummanu.board.migrate import migrate_instance
from ummanu.board.provision import provision as provision_board_store
from ummanu.board.provision import verify_roles as verify_board_store_roles
from ummanu.installation import (
    InstallError,
    SnapshotCheckout,
    _bootstrap_credential,
    _clone_or_reuse,
    _ensure_installation_user,
    _reads_remote_shape,
    _run,
    _set_installation_owner,
    _snapshot_checkout,
    caddy_installed,
    web_front_wanted,
)

BOOTSTRAP_STAMP = ".ummanu-bootstrap"


class BootstrapError(RuntimeError):
    pass


def _host_supported(os_release: Path = Path("/etc/os-release")) -> None:
    try:
        fields = dict(
            line.split("=", 1) for line in os_release.read_text(encoding="utf-8").splitlines() if "=" in line
        )
    except OSError:
        raise BootstrapError("could not identify the operating system") from None
    if fields.get("ID", "").strip('"') != "ubuntu" or fields.get("VERSION_ID", "").strip('"') != "24.04":
        raise BootstrapError("bootstrap supports Ubuntu 24.04 only")


def _install_platform(*, dry_run: bool, runtime_user: str | None = None, web_front: bool = False) -> None:
    """Install Docker and Compose, and the distribution's Caddy when the web-front component is enabled."""
    if dry_run:
        return
    needs_docker = shutil.which("docker") is None
    needs_compose = not _docker_compose_available()
    needs_caddy = web_front and not caddy_installed()
    if needs_docker or needs_compose or needs_caddy:
        if os.geteuid() != 0:
            raise BootstrapError("host prerequisites are absent; rerun bootstrap as root")
        _run(["apt-get", "update"], label="refresh apt")
        packages: list[str] = []
        if needs_docker:
            packages.append("docker.io")
        if needs_compose:
            packages.append(_compose_package())
        if needs_caddy:
            # Masked before the package exists, so its own `caddy.service` never starts an
            # unconfigured public listener; `ummanu-web-front.service` is the only Caddy that runs.
            _run(["systemctl", "mask", "caddy.service"], label="mask the distribution's caddy.service")
            packages.append("caddy")
        _run(
            ["apt-get", "install", "--yes", *packages],
            label="install host prerequisites" if needs_caddy else "install Docker prerequisites",
        )
    _ensure_docker_ready()


def _compose_package() -> str:
    """Return the Compose v2 package exposed by this distribution's own apt archive."""
    for package in ("docker-compose-v2", "docker-compose-plugin"):
        try:
            result = _proc.run(["apt-cache", "show", package], timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            raise BootstrapError("could not inspect apt packages for Docker Compose") from None
        if result.returncode == 0:
            return package
    raise BootstrapError("no Docker Compose v2 package is available from configured apt sources")


def _docker_compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return _proc.run(["docker", "compose", "version"], timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _ensure_docker_ready(*, timeout: int = 60) -> None:
    """Enable Docker and wait until its daemon accepts a client connection."""
    if os.geteuid() != 0:
        raise BootstrapError("Docker must be started by root")
    _run(["systemctl", "enable", "--now", "docker"], label="start Docker")
    deadline = time.monotonic() + timeout
    while True:
        try:
            ready = _proc.run(["docker", "info"], timeout=15).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            ready = False
        if ready:
            return
        if time.monotonic() >= deadline:
            raise BootstrapError("Docker daemon did not become ready")
        time.sleep(1)


def _mark_bootstrap_checkout(target: Path, *, work_tree: bool = True) -> None:
    """Mark the one clean checkout that may proceed through its first install.

    A snapshot live root (`work_tree=False`) has no Git to exclude the stamp from: the export
    allowlist does not match it, so it is host-local there without any entry.
    """
    stamp = target / BOOTSTRAP_STAMP
    write_text_atomic(stamp, "created by ummanu bootstrap\n")
    if not work_tree:
        return
    exclude = target / ".git" / "info" / "exclude"
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    entries = (f"/{BOOTSTRAP_STAMP}", "/runtime.env")
    known = set(existing.splitlines())
    missing = [entry for entry in entries if entry not in known]
    if missing:
        suffix = "" if not existing or existing.endswith("\n") else "\n"
        write_text_atomic(exclude, existing + suffix + "".join(f"{entry}\n" for entry in missing))


def bootstrap(args: argparse.Namespace) -> int:
    target = Path(args.instance_dir).expanduser().resolve()
    snapshot: SnapshotCheckout | None = None
    credential: Path | None = None
    disposable_credential: Path | None = None
    try:
        if not args.dry_run and os.geteuid() != 0:
            raise BootstrapError("host bootstrap must run as root")
        if not args.dry_run:
            _host_supported()
            # The external credential for a private remote is read and checked exactly as `recover`
            # reads it, before the host is changed; a preview clones nothing, so it consumes none.
            credential, disposable_credential = _bootstrap_credential(args, target)
        # Bootstrap may be safely rerun for an existing dedicated user.
        _ensure_installation_user(args.installation_user, recovery=True, dry_run=args.dry_run)
        # The clone step makes recovery's one shape decision (docs/RECOVERY.md, "Two remote
        # shapes"): an exporter snapshot is laid out exactly as `recover` lays it out, so the
        # `recover` that follows finds the same tip and continues; a legacy checkpoint is cloned.
        if _reads_remote_shape(target, recovery=True, dry_run=args.dry_run):
            snapshot = _snapshot_checkout(
                args.instance_remote,
                target,
                dry_run=args.dry_run,
                bootstrap_credential=credential,
                installation_user=args.installation_user,
            )
        if snapshot is None:
            _clone_or_reuse(
                args.instance_remote,
                target,
                recovery=True,
                dry_run=args.dry_run,
                bootstrap_credential=credential,
                installation_user=args.installation_user,
            )
        if not args.dry_run:
            _mark_bootstrap_checkout(target, work_tree=snapshot is None)
            _install_platform(
                dry_run=False, runtime_user=args.installation_user, web_front=web_front_wanted(target)
            )
            provision_board_store(target, allow_create=True)
            migrate_instance(target)
            verify_board_store_roles(target)
            # Last, so the handoff covers what provisioning created as root under the instance:
            # `board-store.env` (0600, read by every role and instance-bound CLI). The Compose
            # definition stays root's in /opt/ummanu. A snapshot also laid the data directory out.
            _set_installation_owner(target, args.installation_user)
            if snapshot is not None:
                _set_installation_owner(snapshot.data_dir, args.installation_user)
        print("ummanu bootstrap\nstatus: " + ("preview" if args.dry_run else "ok"))
        return 0
    except (BootstrapError, InstallError, OSError, RuntimeError) as exc:
        print(f"ummanu bootstrap\nstatus: failed: {exc}")
        return 1
    finally:
        if disposable_credential is not None:
            disposable_credential.unlink(missing_ok=True)
        if snapshot is not None:
            shutil.rmtree(snapshot.scratch, ignore_errors=True)
