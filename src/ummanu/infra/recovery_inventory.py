"""Read-only recovery-readiness inventory shared by status and doctor."""

from __future__ import annotations

import os
import stat
import subprocess
import time
from pathlib import Path
from typing import Any

from ummanu import _proc, head_registry, state_repo
from ummanu.checkpoint import DEFAULT_REMOTE
from ummanu.head_health import PROBE_TTL_SECONDS, HeadHealth, HeadReadiness, run_probe
from ummanu.infra.github_credential import (
    CredentialError,
    CredentialReadiness,
    RemoteExecution,
    checkpoint_credential_readiness_for_child,
    project_remote_execution,
)
from ummanu.runtime.paths import configured_product_root
from ummanu.runtime.role_env import RUNTIME_ENV_FILE_ENVS
from ummanu.secret_store import (
    SecretStoreError,
    store_divergence,
    store_health,
)


def collect_recovery_inventory(
    report,
    *,
    inspect_live: bool,
    now: float | None = None,
    checkpoint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Collect metadata and bounded probes without mutating any installation state."""
    stamp = time.time() if now is None else float(now)
    instance_dir = report.instance_path.parent
    resources, registry_error = _resource_rows(report, inspect_live=inspect_live, now=stamp)
    checkpoint = checkpoint or {}
    projects = _project_git_consumers(report, instance_dir)
    consumers = _credential_consumers(instance_dir, resources, checkpoint, projects)
    paths = _path_rows(report)
    bypasses = _git_bypasses(instance_dir, projects)
    materializations = _materialization_rows(instance_dir)
    store = store_health(instance_dir)
    if store.get("initialized"):
        try:
            catalog_divergences = list(store_divergence(instance_dir))
        except SecretStoreError:
            catalog_divergences = ["secret-store catalog/envelope comparison is unavailable"]
    else:
        catalog_divergences = []
    return {
        "resources": resources,
        "credential_consumers": consumers,
        "paths": paths,
        "materializations": materializations,
        "bypasses": bypasses,
        "catalog_envelope_divergences": catalog_divergences,
        "registry_error": registry_error,
    }


def _resource_rows(report, *, inspect_live: bool, now: float) -> tuple[list[dict[str, Any]], str | None]:
    if report.data_dir is None:
        return [], None
    instance_dir = report.instance_path.parent
    try:
        registry = head_registry.installed_heads(instance_dir)
    except head_registry.HeadRegistryConfigError as exc:
        return [], str(exc)
    resources = registry.get("resources") if isinstance(registry, dict) else {}
    profiles = registry.get("profiles") if isinstance(registry, dict) else {}
    if not isinstance(resources, dict):
        return [], "installed registry resources table is unavailable"
    recorded = HeadHealth(None, report.data_dir).snapshot()
    rows: list[dict[str, Any]] = []
    for name in sorted(resources):
        entry = resources.get(name) if isinstance(resources.get(name), dict) else {}
        used_by = sorted(
            str(profile)
            for profile, config in (profiles.items() if isinstance(profiles, dict) else ())
            if isinstance(config, dict) and config.get("resource") == name
        )
        adapters = sorted(
            {
                str(profiles[profile].get("adapter"))
                for profile in used_by
                if isinstance(profiles.get(profile), dict) and profiles[profile].get("adapter")
            }
        )
        probe = str(entry.get("probe") or "")
        readiness, source, freshness, observed_state = _resource_readiness(
            str(name), probe, recorded, inspect_live=inspect_live, now=now
        )
        age_seconds = max(0, int(now - readiness.checked_at)) if readiness.checked_at > 0 else None
        rows.append(
            {
                "resource": str(name),
                "account": str(entry.get("account") or "") or None,
                "profiles": used_by,
                "consumers": adapters,
                "probe_configured": bool(probe),
                "state": readiness.status,
                "observed_state": observed_state,
                "reason": readiness.reason,
                "source": source,
                "freshness": freshness,
                "observed_at": _rfc3339(readiness.checked_at),
                "age_seconds": age_seconds,
                # When a red verdict a head's own turn recorded expires (ummanu-108): the reset the
                # provider named, else the end of the backoff. None for a verdict with no expiry.
                "until": _rfc3339(readiness.until) if readiness.until else None,
            }
        )
    return rows, None


def _resource_readiness(
    resource: str,
    probe: str,
    recorded: dict[str, Any],
    *,
    inspect_live: bool,
    now: float,
) -> tuple[HeadReadiness, str, str, str | None]:
    entry = recorded.get(resource)
    cached = _recorded(entry, resource)
    # A verdict held to an expiry answers until then, and no probe is run over it: a cheap probe
    # is no proof there is quota for real work (ummanu-108).
    if cached is not None and (
        now < cached.until if cached.until else now - cached.checked_at < PROBE_TTL_SECONDS
    ):
        return cached, "dispatcher-cache", "fresh", None
    if inspect_live and probe:
        return run_probe(resource, probe, now), "live-read-only-probe", "fresh", None
    if cached is not None:
        return (
            HeadReadiness(
                resource,
                "stale",
                "dispatcher verdict is stale; run doctor online to refresh the observation",
                cached.checked_at,
            ),
            "dispatcher-cache",
            "stale",
            cached.status,
        )
    reason = (
        "no recorded verdict; run doctor online to inspect this resource"
        if probe
        else "resource has no configured probe"
    )
    return HeadReadiness(resource, "unknown", reason, 0), "unavailable", "unknown", None


def _recorded(entry: object, resource: str) -> HeadReadiness | None:
    if not isinstance(entry, dict):
        return None
    try:
        checked_at = float(entry.get("checked_at") or 0)
    except (TypeError, ValueError):
        return None
    if checked_at <= 0:
        return None
    try:
        until = float(entry.get("until") or 0)
    except (TypeError, ValueError):
        until = 0.0
    return HeadReadiness(
        resource,
        str(entry.get("status") or "unknown"),
        str(entry["reason"])
        if "reason" in entry and entry["reason"] is not None
        else "recorded verdict has no reason",
        checked_at,
        True,
        until,
    )


_UNMANAGED_HTTP_TRANSPORTS = frozenset({"https-unsupported", "unmanaged"})


def _project_git_consumers(report, instance_dir: Path) -> list[dict[str, Any]]:
    """One dispatcher Git consumer per registered project, from its checkout's effective origin.

    The transport is classified after URL rewriting, exactly as the dispatcher's boundary will
    classify it; reading remote configuration contacts no remote. Managed-store readiness is
    inspected once, as the Git child, and reported on every row independently of its transport.
    """
    bindings = sorted(
        (
            binding
            for binding in (getattr(report, "bindings", None) or [])
            if isinstance(binding, dict) and isinstance(binding.get("id"), str) and binding.get("id")
        ),
        key=lambda binding: str(binding["id"]),
    )
    if not bindings:
        return []
    # Readiness is per Git child: a project checkout may belong to a different user than the
    # instance checkout, and the dispatcher's preflight reads the store as that project's child.
    readiness_by_child: dict[tuple[int, int], CredentialReadiness] = {}
    rows: list[dict[str, Any]] = []
    for binding in bindings:
        repo = binding.get("repo")
        transport = "unknown"
        checkout = Path(repo).expanduser() if isinstance(repo, str) and repo else None
        if checkout is None or not checkout.exists():
            # An unprovisioned project has no Git consumer on this host yet; the dispatcher answers
            # `not-applicable` for it and project availability names the missing checkout.
            continue
        if (checkout / ".git").exists():
            try:
                transport = project_remote_execution(checkout, instance_dir=instance_dir).transport
            except CredentialError:
                transport = "unknown"
        try:
            child = state_repo.git_child_identity(checkout)
        except state_repo.StateRepoError as exc:
            managed = CredentialReadiness("missing/unavailable", " ".join(str(exc).split())[:240])
        else:
            key = (child.uid, child.gid)
            if key not in readiness_by_child:
                readiness_by_child[key] = checkpoint_credential_readiness_for_child(instance_dir, child)
            managed = readiness_by_child[key]
        rows.append(_project_git_row(str(binding["id"]), binding.get("enabled") is True, transport, managed))
    return rows


def _project_git_row(
    project: str, enabled: bool, transport: str, managed: CredentialReadiness
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "consumer": f"project-git:{project}",
        "capability": "dispatcher gate fetch, branch publication, release push and checkout refresh",
        "project": project,
        "enabled": enabled,
        "transport": transport,
        "managed_readiness": managed.state,
        "canonical_source": f"not-applicable ({transport} transport)",
        "verification_source": "effective-remote-classification",
        "verified_at": None,
        "verification_age_minutes": None,
    }
    if transport == "github-https":
        row.update(
            source="managed-store",
            canonical_source="encrypted-store:github.checkpoint-token",
            state=managed.state,
            reason=managed.reason,
            verification_source="managed-store-readiness",
            supported_next_action="none"
            if managed.ready
            else "unlock the store or set the token with `ummanu secret checkpoint-github set --stdin`, "
            "then rerun doctor",
        )
    elif transport == "local":
        row.update(
            source="local",
            state="not-applicable",
            reason="local/file transport needs no credential",
            supported_next_action="none",
        )
    elif transport == "unmanaged":
        row.update(
            source="manual-bypass",
            state="ambient/manual-bypass",
            reason="a non-HTTPS network transport is used as configured; Ummanu manages no credential for it",
            supported_next_action="use an https://github.com origin for the managed credential, or SSH",
        )
    elif transport == "ssh":
        row.update(
            source="manual-bypass",
            state="ambient/manual-bypass",
            reason="SSH transport uses the runtime user's SSH access; Ummanu manages no SSH key",
            supported_next_action="keep the runtime user's SSH access working, or use an https://github.com "
            "origin for the managed credential",
        )
    elif transport == "unknown":
        row.update(
            source="none",
            state="unknown",
            reason="project checkout or its origin remote is unavailable",
            supported_next_action="restore the project checkout, then rerun doctor",
        )
    else:
        row.update(
            source="none",
            state="refused",
            reason=f"the dispatcher refuses Git access over {transport} transport",
            supported_next_action="point origin at an https://github.com, SSH or local remote",
        )
    return row


def _credential_consumers(
    instance_dir: Path,
    resources: list[dict[str, Any]],
    checkpoint: dict[str, Any],
    projects: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    credential = checkpoint.get("credential") if isinstance(checkpoint.get("credential"), dict) else {}
    credential_state = credential.get("state") or "unknown"
    credential_reason = (
        str(credential["reason"])
        if "reason" in credential and credential["reason"] is not None
        else "checkpoint credential has not been inspected"
    )
    try:
        if not (instance_dir / ".git").exists():
            raise state_repo.StateRepoError(f"instance repo is unavailable: {instance_dir}")
        remote = state_repo.git(
            instance_dir,
            ["config", "--get", f"remote.{DEFAULT_REMOTE}.url"],
            label="inspect checkpoint remote",
        ).strip()
        if not remote:
            raise state_repo.StateRepoError("checkpoint remote is unavailable")
    except state_repo.StateRepoError:
        if credential_state != "managed-ready":
            credential_state = "unknown"
            credential_reason = "checkpoint remote is unavailable, so credential applicability is unknown"
    rows.append(
        {
            "consumer": "checkpoint-github",
            "capability": "push and recover the private instance checkpoint",
            "source": credential.get("source") or "none",
            "canonical_source": "encrypted-store:github.checkpoint-token",
            "state": credential_state,
            "reason": credential_reason,
            "verification_source": "managed-store-readiness",
            "verified_at": credential.get("last_verified_at") or None,
            "verification_age_minutes": credential.get("last_verified_age_minutes"),
            "supported_next_action": "use `ummanu secret checkpoint-github set --stdin` if managed readiness is unavailable",
        }
    )
    rows.extend(projects or [])
    for resource in resources:
        adapters = resource.get("consumers") or []
        rows.append(
            {
                "consumer": f"provider-login:{resource['resource']}",
                "capability": "launch profiles "
                + (", ".join(resource.get("profiles") or []) or "using this resource"),
                "source": "provider-cli-login",
                "canonical_source": "intentionally-unmanaged provider login",
                "state": resource["state"],
                "reason": resource["reason"],
                "verification_source": resource["source"],
                "verified_at": resource["observed_at"],
                "verification_age_minutes": None
                if resource["age_seconds"] is None
                else resource["age_seconds"] // 60,
                "profiles": resource.get("profiles") or [],
                "adapters": adapters,
                "supported_next_action": _provider_action(adapters),
            }
        )
    return rows


def _provider_action(adapters: list[str]) -> str:
    if "codex" in adapters:
        return "restore the configured Codex CLI login, then rerun doctor online"
    if "claude" in adapters:
        return "restore the configured Claude CLI login, then rerun doctor online"
    return "restore the provider login required by the installed profile, then rerun doctor online"


def _path_rows(report) -> list[dict[str, Any]]:
    instance_dir = report.instance_path.parent.expanduser().resolve(strict=False)
    source = None
    try:
        source = head_registry.read_source(instance_dir)
    except head_registry.HeadRegistryConfigError:
        pass
    pinned_product = (
        Path(str(source.get("product_root"))).expanduser().resolve(strict=False)
        if isinstance(source, dict) and source.get("product_root")
        else None
    )
    bound_instance = os.environ.get("UMMANU_INSTANCE")
    try:
        environment_is_bound = bool(bound_instance) and Path(str(bound_instance)).expanduser().resolve(
            strict=False
        ) in {
            instance_dir,
            report.instance_path.expanduser().resolve(strict=False),
        }
    except OSError:
        environment_is_bound = False
    product_override = os.environ.get("UMMANU_REPO") if environment_is_bound else None
    configured_product = (
        Path(product_override).expanduser().resolve(strict=False)
        if product_override
        else (pinned_product or configured_product_root().resolve(strict=False))
    )
    runtime_override = next(
        (os.environ[name] for name in RUNTIME_ENV_FILE_ENVS if environment_is_bound and os.environ.get(name)),
        None,
    )
    canonical_runtime = (instance_dir / "runtime.env").resolve(strict=False)
    configured_runtime = (
        Path(runtime_override).expanduser().resolve(strict=False) if runtime_override else canonical_runtime
    )
    rows = [
        _path_row(
            "instance",
            instance_dir,
            None,
            instance_dir,
            "pass the installed instance path explicitly",
        ),
        _path_row(
            "data-plane",
            report.data_dir,
            os.environ.get("UMMANU_DATA_DIR") if environment_is_bound else None,
            report.data_dir,
            "make UMMANU_DATA_DIR match instance.yaml or remove the override",
        ),
        _path_row(
            "runtime-env",
            canonical_runtime,
            str(configured_runtime) if runtime_override else None,
            canonical_runtime,
            "point UMMANU_RUNTIME_ENV_FILE at the declared installation runtime.env",
        ),
        _path_row(
            "product-root",
            pinned_product or configured_product,
            str(configured_product) if product_override else None,
            pinned_product or configured_product,
            "make UMMANU_REPO match the installed head-registry source pin",
        ),
    ]
    workspaces_override = os.environ.get("TA_WORKSPACES_ROOT") if environment_is_bound else None
    workspaces = (
        Path(workspaces_override).expanduser() if workspaces_override else Path.home() / "orca" / "workspaces"
    )
    canonical_state = (workspaces / "ummanu" / "pipeline" / "state" / "pipeline").resolve(strict=False)
    state_override = os.environ.get("TA_PIPELINE_STATE_DIR") if environment_is_bound else None
    rows.append(
        _path_row(
            "pipeline-run-state",
            canonical_state,
            state_override,
            canonical_state,
            "remove TA_PIPELINE_STATE_DIR or make it match the declared workspaces-root contract",
        )
    )
    return rows


def _path_row(
    capability: str, canonical: Path, configured: str | None, declared: Path, action: str
) -> dict[str, Any]:
    selected = Path(configured).expanduser().resolve(strict=False) if configured else canonical
    matches = selected == declared
    return {
        "capability": capability,
        "canonical": str(declared),
        "configured": str(selected),
        "source": "environment-override" if configured else "declared-installation",
        "state": "supported" if matches else "conflicting-override",
        "supported": matches,
        "reason": "configured path matches the declared installation contract"
        if matches
        else "configured path conflicts with the declared installation contract",
        "supported_next_action": "none" if matches else action,
    }


def _ambient_credential_action(projects: list[dict[str, Any]], subject: str) -> str:
    """Advice about an ambient credential, conditional on the consumers actually inventoried."""
    dependents = sorted(
        str(row.get("project")) for row in projects if row.get("transport") in _UNMANAGED_HTTP_TRANSPORTS
    )
    if dependents:
        return (
            f"keep the ambient {subject}: registered project(s) {', '.join(dependents)} use an unmanaged "
            "HTTPS origin that manual Git may authenticate through it; move them to https://github.com or "
            "SSH before retiring it"
        )
    return (
        f"Ummanu's managed GitHub operations disable the ambient {subject}; retire it only after "
        "confirming nothing outside the inventoried consumers needs it"
    )


def _git_bypasses(instance_dir: Path, projects: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    projects = projects or []
    rows: list[dict[str, Any]] = []
    if not (instance_dir / ".git").exists():
        return rows
    try:
        remote = state_repo.git(
            instance_dir,
            ["config", "--get", f"remote.{DEFAULT_REMOTE}.url"],
            label="inspect checkpoint remote",
        ).strip()
        if not remote:
            return rows
        transport = RemoteExecution(remote, "checkpoint", instance_dir=instance_dir).transport
    except state_repo.StateRepoError:
        return rows
    # Ambient helpers and credential files matter as soon as any inventoried consumer authenticates.
    authenticated = transport != "local" or any(row.get("transport") != "local" for row in projects)
    # A local/file remote is the supported hermetic checkpoint transport. It needs no
    # authentication and therefore adds no transport row. Applicable rewrites and ambient Git
    # configuration are still inventoried below because Git can apply them before transport.
    if transport in {"ssh", "unmanaged", "https-unsupported"}:
        rows.append(
            {
                "capability": "checkpoint-git-authentication",
                "kind": "manual-transport",
                "state": "manual-only" if transport in {"ssh", "unmanaged"} else "broken",
                "supported": False,
                "reason": f"checkpoint remote uses {transport} transport",
                "supported_next_action": "configure an HTTPS github.com origin to use the managed checkpoint credential",
            }
        )
    try:
        result = _proc.run(
            [
                "git",
                "-C",
                str(instance_dir),
                "config",
                "--show-origin",
                "--get-regexp",
                r"^(url\..*\.insteadof|credential(\..*)?\.helper)$",
            ],
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        result = None
    for line in result.stdout.splitlines() if result is not None and result.returncode == 0 else []:
        origin, separator, setting = line.partition("\t")
        if not separator:
            origin, setting = "unknown", line
        key, _, value = setting.partition(" ")
        lower = key.lower()
        if lower.startswith("url.") and lower.endswith(".insteadof"):
            if remote and value and not remote.startswith(value):
                continue
            kind = "insteadOf"
            reason = "an applicable Git URL rewrite can bypass the declared checkpoint transport"
        else:
            if not authenticated:
                continue
            kind = "credential-helper"
            reason = (
                "ambient Git credential configuration exists; managed Ummanu GitHub operations disable it, "
                "but manual Git may use it"
            )
        rows.append(
            {
                "capability": "checkpoint-git-authentication",
                "kind": kind,
                "state": "manual-only",
                "supported": False,
                "configuration_key": "url.*.insteadOf" if kind == "insteadOf" else "credential.*",
                "configuration_scope": _git_configuration_scope(origin, instance_dir),
                "reason": reason,
                "supported_next_action": "remove the ambient Git bypass after confirming no unrelated repository needs it"
                if kind == "insteadOf"
                else _ambient_credential_action(projects, "credential helper"),
            }
        )
    if not authenticated:
        # Credential helpers and files cannot authenticate a local transport. URL rewriting was
        # still inspected above because an applicable rule can change which transport Git uses.
        return rows
    candidates = [Path.home() / ".git-credentials"]
    xdg = os.environ.get("XDG_CONFIG_HOME")
    candidates.append((Path(xdg) if xdg else Path.home() / ".config") / "git" / "credentials")
    for path in candidates:
        try:
            present = path.is_file()
        except OSError:
            present = False
        if present:
            rows.append(
                {
                    "capability": "checkpoint-git-authentication",
                    "kind": "ambient-credential-file",
                    "state": "manual-only",
                    "supported": False,
                    "path": str(path),
                    "reason": "an ambient Git credential file exists; its contents were not read",
                    "supported_next_action": _ambient_credential_action(projects, "credential file"),
                }
            )
    return rows


def _git_configuration_scope(origin: str, instance_dir: Path) -> str:
    """Classify provenance without echoing a config path or credential-bearing command line."""
    if origin.startswith("file:"):
        try:
            configured = Path(origin.removeprefix("file:")).expanduser().resolve(strict=False)
            local = (instance_dir / ".git" / "config").resolve(strict=False)
        except OSError:
            return "file"
        return "repository" if configured == local else "ambient-file"
    return "command" if origin.startswith("command line:") else "unknown"


def _materialization_rows(instance_dir: Path) -> list[dict[str, Any]]:
    health = store_health(instance_dir)
    rows: list[dict[str, Any]] = []
    for item in health.get("materialize", []):
        target = str(item.get("target") or "")
        raw_path = item.get("path")
        bound = os.environ.get("UMMANU_INSTANCE")
        try:
            environment_is_bound = bool(bound) and Path(str(bound)).expanduser().resolve(strict=False) in {
                instance_dir.resolve(strict=False),
                (instance_dir / "instance.yaml").resolve(strict=False),
            }
        except OSError:
            environment_is_bound = False
        override = next(
            (
                os.environ[name]
                for name in RUNTIME_ENV_FILE_ENVS
                if environment_is_bound and os.environ.get(name)
            ),
            None,
        )
        path = (
            Path(str(raw_path)).expanduser()
            if raw_path
            else (Path(override).expanduser() if override else instance_dir / "runtime.env")
        )
        try:
            info = path.lstat()
            present = True
            kind = (
                "file"
                if stat.S_ISREG(info.st_mode)
                else ("symlink" if stat.S_ISLNK(info.st_mode) else "non-file")
            )
            mode = oct(info.st_mode & 0o777)
        except FileNotFoundError:
            present, kind, mode = False, "missing", None
        except OSError:
            present, kind, mode = None, "unverifiable", None
        if present and kind == "file" and mode == "0o600":
            state = "ready"
        elif present:
            state = "drifted"
        else:
            state = "missing" if present is False else "unverifiable"
        rows.append(
            {
                "capability": "materialized-runtime-credential",
                "target": target,
                "path": str(path),
                "declared_secret_count": item.get("count", 0),
                "present": present,
                "kind": kind,
                "mode": mode,
                "state": state,
                "supported_next_action": "none"
                if state == "ready"
                else "unlock the store and run the supported recovery materialization step",
            }
        )
    return rows


def _rfc3339(epoch: float) -> str | None:
    if epoch <= 0:
        return None
    from datetime import UTC, datetime

    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")
