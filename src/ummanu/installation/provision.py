"""Project checkouts and managed CODEX_HOME provisioning."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import tomllib
from pathlib import Path

from ummanu._fsutil import write_text_atomic
from ummanu.infra.github_credential import CredentialError, RemoteExecution
from ummanu.installation.materialize import _write_recovery_progress
from ummanu.installation.models import InstallError, ProjectProvisionResult
from ummanu.installation.support import _set_installation_owner
from ummanu.memory.client_config import (
    ClientConfigError,
    bridge_executable,
    packaged_codex_home,
    seed_codex_home,
)
from ummanu.projects.availability import binding_disabled
from ummanu.runtime.codex_home import managed_codex_homes


def provision_project_checkouts(
    bindings: list[dict[str, object]],
    installation_user: str | None,
    *,
    instance_dir: Path | None = None,
    bootstrap_credential: Path | None = None,
    timeout: float = 600,
    progress_path: Path | None = None,
    recovery_identity: str = "",
) -> list[ProjectProvisionResult]:
    """Attempt every registered checkout through the credential and atomic-adoption boundary."""
    results: list[ProjectProvisionResult] = []
    persisted: list[dict[str, object]] = []
    for index, binding in enumerate(bindings):
        result = _provision_project_checkout(
            binding,
            index=index,
            installation_user=installation_user,
            instance_dir=instance_dir,
            bootstrap_credential=bootstrap_credential,
            timeout=timeout,
        )
        results.append(result)
        persisted.append(
            {
                "project_id": result.project_id,
                "transport": result.transport,
                "outcome": result.outcome,
                "code": result.code,
                "retryable": result.retryable,
            }
        )
        if progress_path is not None:
            _write_recovery_progress(progress_path, recovery_identity, projects=persisted)
    return results



def _provision_project_checkout(
    binding: dict[str, object],
    *,
    index: int,
    installation_user: str | None,
    instance_dir: Path | None,
    bootstrap_credential: Path | None,
    timeout: float,
) -> ProjectProvisionResult:
    project_id = binding.get("id")
    display_id = (
        project_id
        if isinstance(project_id, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]*", project_id)
        else f"binding-{index + 1}"
    )
    if binding_disabled(binding):
        # A retired or not-yet-onboarded project: nothing runs against it, so nothing is cloned for
        # it, and its absence is not an unavailable checkout.
        return ProjectProvisionResult(
            display_id, "not-inspected", "not-contacted", "disabled", "disabled", "binding is disabled", False
        )
    raw_target = binding.get("repo")
    if not isinstance(raw_target, str) or not raw_target:
        return _project_failure(display_id, "invalid", "unknown", "invalid-binding", False)
    try:
        target = Path(raw_target).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return _project_failure(display_id, "invalid", "unknown", "invalid-target", False)
    target_state = "existing" if target.exists() else "missing"
    if target.exists():
        if target.is_dir() and (target / ".git").exists():
            return ProjectProvisionResult(
                display_id,
                "existing",
                "not-contacted",
                "unchanged",
                "existing",
                "valid checkout left unchanged",
                False,
            )
        return _project_failure(display_id, target_state, "not-contacted", "target-collision", False)
    remote = binding.get("remote")
    branch = binding.get("default_branch")
    if not isinstance(remote, str) or not remote or not isinstance(branch, str) or not branch:
        return _project_failure(display_id, target_state, "unknown", "invalid-binding", False)
    execution = RemoteExecution(
        remote,
        "project-provision",
        instance_dir=instance_dir,
        bootstrap_file=bootstrap_credential,
    )
    transport = execution.transport
    if instance_dir is None:
        return _project_failure(display_id, target_state, transport, "invalid-context", False)
    temporary: Path | None = None
    claimed_target = False
    try:
        created = [parent for parent in (target.parent, *target.parent.parents) if not parent.exists()]
        target.parent.mkdir(parents=True, exist_ok=True)
        if created:
            # Root creates `~/projects` on a fresh host; the installation user clones into it later.
            _set_installation_owner(created[-1], installation_user)
        temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.clone-", dir=target.parent))
        _set_installation_owner(temporary, installation_user)
        staging = temporary / "checkout"
        execution.run_clone(
            staging,
            label=f"clone project {display_id}",
            timeout=timeout,
            clone_args=["--branch", branch, "--single-branch"],
            child_target=instance_dir,
        )
        _set_installation_owner(staging, installation_user)
        # Claim the final name without overwriting a path created since the
        # initial inspection. Replacing our own empty directory is atomic.
        target.mkdir()
        claimed_target = True
        os.replace(staging, target)
        claimed_target = False
    except KeyboardInterrupt:
        raise
    except CredentialError as exc:
        return _project_failure(
            display_id,
            target_state,
            transport,
            exc.code,
            exc.code not in {"unsupported-https", "invalid-branch"},
        )
    except InstallError:
        return _project_failure(display_id, target_state, transport, "ownership", True)
    except OSError:
        return _project_failure(display_id, target_state, transport, "atomic-replace", True)
    finally:
        if claimed_target:
            try:
                target.rmdir()
            except OSError:
                pass
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)
    return ProjectProvisionResult(
        display_id, "adopted", transport, "cloned", "cloned", "checkout cloned atomically", False
    )



def _project_failure(
    project_id: str, target: str, transport: str, code: str, retryable: bool
) -> ProjectProvisionResult:
    reason = {
        "invalid-binding": "project binding is incomplete",
        "invalid-target": "checkout target is invalid",
        "target-collision": "checkout target exists and is not a Git repository",
        "invalid-context": "installation context is unavailable",
        "unsupported-https": "HTTPS host is outside the managed credential boundary",
        "unsupported-transport": "remote transport is unsupported",
        "unsafe-remote": "credential-bearing remote URLs are refused",
        "authentication": "remote authentication failed",
        "network": "remote network access failed",
        "timeout": "Git clone timed out and staging was removed",
        "invalid-branch": "configured default branch is unavailable",
        "command-not-found": "Git is unavailable",
        "process": "Git process could not run",
        "git": "Git clone failed",
        "identity": "Git child identity could not be resolved",
        "ownership": "staged checkout ownership could not be finalized",
        "atomic-replace": "staged checkout could not be adopted",
        "credential": "managed credential is unavailable",
    }.get(code, "project checkout provisioning failed")
    return ProjectProvisionResult(project_id, target, transport, "failed", code, reason, retryable)



def provision_codex_home(
    product_root: Path,
    installation_user: str | None,
    *,
    data_dir: Path | None = None,
) -> int:
    """Seed non-secret Codex runtime files into `<data_dir>/codex-home`, preserving login state.

    That is the one CODEX_HOME the installation manages (`codex_home.managed_codex_homes`); the
    legacy Orca home is not seeded or reconciled since A20 step 7 (secretary-1723). `auth.json` is
    never written: logging in is the PO's own step, `CODEX_HOME=<data_dir>/codex-home codex login`,
    and without it `codex_preflight.resolve_codex_home` refuses to launch a Codex head.

    A `config.toml` seeded here gets the full PO-bridge entry in the same step
    (`memory.client_config.seed_codex_home`, which `reconcile_clients` seeds through too), so the home
    parses under the `-c mcp_servers.po_memory.enabled=false` every pipeline head is launched with.
    That needs the data dir the bridge reads; without one the seed is the packaged file alone. An
    existing config is the upgrade's `memory-clients` step to reconcile.
    """
    if not installation_user:
        return 0
    targets = managed_codex_homes(None if data_dir is None else Path(data_dir))
    if not targets:
        return 0
    source = packaged_codex_home(product_root)
    bridge = (bridge_executable(product_root), Path(data_dir)) if data_dir is not None else None
    changed = 0
    for target in targets:
        changed += _seed_codex_home(target, source, bridge=bridge)
        _set_installation_owner(target, installation_user)
    return changed



def _seed_codex_home(target: Path, source: Path, *, bridge: tuple[Path, Path] | None = None) -> int:
    """Copy-once `AGENTS.md` and `config.toml` into one CODEX_HOME, reconciling the memory entry.

    `bridge` is the PO-bridge executable and data dir a freshly seeded `config.toml` is reconciled
    with (`memory.client_config.seed_codex_home`); the file still counts once.
    """
    changed = 0
    config = target / "config.toml"
    if config.exists() and _reconcile_managed_memory_mcp(config, source / "config.toml"):
        changed += 1
    try:
        changed += len(seed_codex_home(target, source, bridge))
    except ClientConfigError as exc:
        raise InstallError(f"could not provision managed CODEX_HOME: {exc}") from None
    return changed



def _reconcile_managed_memory_mcp(destination: Path, source: Path) -> bool:
    """Add the bearer setting to the one Memory endpoint Ummanu owns.

    CODEX_HOME also holds user login and preferences, so provisioning remains copy-once.  The
    exception is the managed loopback Memory entry: its URL must still exactly match the packaged
    endpoint and only its missing bearer key is inserted.  Any malformed or user-repointed config
    is left untouched.
    """
    try:
        current_text = destination.read_text(encoding="utf-8")
        source_payload = tomllib.loads(source.read_text(encoding="utf-8"))
        current_payload = tomllib.loads(current_text)
        managed = source_payload["mcp_servers"]["memory"]
        current = current_payload["mcp_servers"]["memory"]
        managed_url = managed["url"]
    except (OSError, UnicodeError, KeyError, TypeError, tomllib.TOMLDecodeError):
        return False
    if not isinstance(managed, dict) or not isinstance(current, dict):
        return False
    if not isinstance(managed_url, str) or current.get("url") != managed_url:
        return False
    bearer_name = managed.get("bearer_token_env_var")
    if not isinstance(bearer_name, str) or current.get("bearer_token_env_var"):
        return False
    lines = current_text.splitlines(keepends=True)
    section = "[mcp_servers.memory]"
    start = next((index for index, line in enumerate(lines) if line.strip() == section), None)
    if start is None:
        return False
    end = next(
        (index for index in range(start + 1, len(lines)) if lines[index].lstrip().startswith("[")), len(lines)
    )
    newline = "\r\n" if "\r\n" in current_text else "\n"
    lines.insert(end, f'bearer_token_env_var = "{bearer_name}"{newline}')
    try:
        write_text_atomic(destination, "".join(lines))
    except RuntimeError:
        return False
    return True
