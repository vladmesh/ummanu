"""Scheduled maintenance of the private instance repository.

Git's implicit ``gc --auto`` would otherwise run inside whichever checkpoint ``git commit`` first
crosses its loose-object threshold, so a dispatcher tick could spend minutes packing a
multi-gigabyte repository.  The lifecycle turns that off (``gc.auto=0`` and ``maintenance.auto=false``
in :data:`ummanu.state_repo.PACKING_CONTROLS`), and this module is what runs instead, from the
``instance-maintenance`` timer and never from a tick.

The run keeps Git's own heuristic, only moved out of the tick: ``gc --auto`` with the stock
thresholds does nothing on a quiet day, packs loose objects incrementally once there are more than
:data:`AUTO_LOOSE_OBJECTS`, and consolidates packs once there are more than :data:`AUTO_PACK_LIMIT`.

Coordination with the checkpoint writer and pusher: the packing step never takes
:func:`ummanu.state_repo.state_repo_lock`.  It touches objects only — Git keeps a concurrent
commit's new objects (loose objects younger than ``gc.pruneExpire`` survive) and a repack replaces
packs before deleting them — and the two ref-writing parts of ``gc`` (``pack-refs`` and reflog
expiry) are switched off for it, so a commit's branch update never meets a ref lock held by
``gc``.  Reflogs are then expired as a separate short step under the state-repo lock; that is the
only moment a tick can wait on maintenance, bounded by :data:`REFLOG_TIMEOUT_SECONDS`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ummanu import state_repo
from ummanu.infra.host_space_policy import BUILD_CACHE_MAX_AGE_HOURS
from ummanu.runtime.container_labels import TEST_BOARD_LABEL, is_test_container

# Git's stock `gc --auto` thresholds, restated because the repository's own `gc.auto` is 0.
AUTO_LOOSE_OBJECTS = 6700
AUTO_PACK_LIMIT = 50
# A full consolidation of a multi-gigabyte repository with `pack.threads=1` is slow, and nothing
# waits on it; the bound only keeps a wedged run from living until the next trigger.
GC_TIMEOUT_SECONDS = 4 * 60 * 60
REFLOG_TIMEOUT_SECONDS = 60
DOCKER_TIMEOUT_SECONDS = 60
DOCKER_OUTPUT_BYTES = 128 * 1024
MAX_CONTAINERS = 256

GC_OVERRIDES = (
    ("gc.auto", str(AUTO_LOOSE_OBJECTS)),
    ("gc.autoPackLimit", str(AUTO_PACK_LIMIT)),
    # A detached gc would outlive the oneshot unit and be killed with its control group.
    ("gc.autoDetach", "false"),
    # Neither ref-writing step may run outside the state-repo lock; see the module docstring.
    ("gc.packRefs", "false"),
    ("gc.reflogExpire", "never"),
    ("gc.reflogExpireUnreachable", "never"),
)


def gc_command() -> list[str]:
    """The Git arguments of the packing step, without the instance-repository prefix."""
    overrides = [argument for key, value in GC_OVERRIDES for argument in ("-c", f"{key}={value}")]
    return [*overrides, "gc", "--auto", "--quiet"]


def count_objects(instance_dir: Path, prefix: list[str] | tuple[str, ...] = ()) -> dict[str, int]:
    """``git count-objects -v`` as integers: ``count`` is the loose objects, ``packs`` the packs."""
    output = state_repo.git(instance_dir, [*prefix, "count-objects", "-v"], label="count instance objects")
    counts: dict[str, int] = {}
    for line in output.splitlines():
        key, _, value = line.partition(":")
        try:
            counts[key.strip()] = int(value.strip())
        except ValueError:
            continue
    return counts


class CleanupError(RuntimeError):
    """A bounded, secret-free Docker failure description."""


@dataclass
class CleanupInventory:
    containers_removed: int = 0
    containers_retained: dict[str, int] = field(default_factory=dict)
    anonymous_volumes_removed: int | None = None
    cache_reclaimed: str = "unknown"
    findings: list[str] = field(default_factory=list)

    def retain(self, reason: str) -> None:
        self.containers_retained[reason] = self.containers_retained.get(reason, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "containers": {"removed": self.containers_removed, "retained": self.containers_retained},
            "anonymous_volumes": {"removed": self.anonymous_volumes_removed,
                                  "retained": "named and in-use volumes are excluded by Docker"},
            "build_cache": {"reclaimed": self.cache_reclaimed,
                            "retained": f"used or newer than {BUILD_CACHE_MAX_AGE_HOURS} hours"},
            "findings": self.findings,
        }


def _docker(*args: str) -> str:
    """Run native Docker with a deadline and bounded output, never publishing stderr."""
    try:
        environment = os.environ.copy()
        # A service environment must not force an older API whose volume-prune default also
        # includes named volumes, or redirect control-host cleanup to a remote context.
        for name in ("DOCKER_API_VERSION", "DOCKER_CONTEXT", "DOCKER_HOST"):
            environment.pop(name, None)
        environment["DOCKER_HOST"] = "unix:///var/run/docker.sock"
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(
                ["docker", *args], stdout=output, stderr=subprocess.DEVNULL,
                check=False, timeout=DOCKER_TIMEOUT_SECONDS, env=environment,
            )
            output.seek(0)
            raw = output.read(DOCKER_OUTPUT_BYTES + 1)
        if result.returncode or len(raw) > DOCKER_OUTPUT_BYTES:
            raise CleanupError("Docker command failed or exceeded output bound")
        return raw.decode("utf-8")
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        raise CleanupError(f"Docker command unavailable ({type(exc).__name__})") from None


def _owner_dead(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (PermissionError, OSError):
        return False
    # kill(0) also succeeds for an unreaped zombie. An unreadable status stays protected.
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return any(line.startswith("State:") and line.split()[1] in {"Z", "X"}
               for line in status.splitlines() if len(line.split()) > 1)


def _container_labels(identifier: str) -> dict[str, str] | None:
    try:
        records = json.loads(_docker("container", "inspect", identifier))
        if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
            raise ValueError
        record = records[0]
        if record.get("Id") != identifier:
            raise ValueError
        labels = record.get("Config", {}).get("Labels")
        return labels if isinstance(labels, dict) else None
    except (ValueError, TypeError, AttributeError):
        raise CleanupError("container inspection malformed") from None


def _cleanup_containers(inventory: CleanupInventory) -> None:
    identifiers = _docker("container", "ls", "--all", "--quiet", "--no-trunc",
                          "--filter", f"label={TEST_BOARD_LABEL}").splitlines()
    if len(identifiers) > MAX_CONTAINERS or len(set(identifiers)) != len(identifiers):
        raise CleanupError("test container inventory exceeds bound or contains duplicates")
    for identifier in identifiers:
        if not re.fullmatch(r"[0-9a-f]{64}", identifier):
            inventory.retain("invalid_id")
            continue
        try:
            labels = _container_labels(identifier)
        except CleanupError:
            inventory.retain("inspection_failed")
            inventory.findings.append("test container inspection failed")
            continue
        if not is_test_container(labels):
            inventory.retain("protected_label")
            continue
        owner_text = labels[TEST_BOARD_LABEL]
        if len(owner_text) > 10 or int(owner_text) > 2**31 - 1:
            inventory.retain("invalid_owner")
            continue
        owner = int(owner_text)
        if not _owner_dead(owner):
            inventory.retain("owner_live_or_unknown")
            continue
        try:
            # Recheck immediately before the destructive call. A changing label or owner fails closed.
            latest = _container_labels(identifier)
            if latest != labels or not is_test_container(latest) or not _owner_dead(owner):
                inventory.retain("changed_or_live")
                continue
            _docker("container", "rm", "--force", identifier)
            inventory.containers_removed += 1
        except CleanupError:
            inventory.retain("removal_failed")
            inventory.findings.append("test container removal failed")


def _deleted_volume_count(output: str) -> int:
    lines = output.splitlines()
    if lines == ["Total reclaimed space: 0B"]:
        return 0
    if not lines or lines[0] != "Deleted Volumes:":
        raise CleanupError("volume prune result malformed")
    names = []
    for line in lines[1:]:
        if line.startswith("Total reclaimed space:"):
            return len(names)
        if not line:
            continue
        if len(line) > 255:
            raise CleanupError("volume prune result malformed")
        names.append(line)
    raise CleanupError("volume prune result incomplete")


def cleanup_docker() -> dict[str, Any]:
    """Remove only dead-owner test containers, dangling anonymous volumes, and old unused cache."""
    inventory = CleanupInventory()
    try:
        _cleanup_containers(inventory)
    except CleanupError as exc:
        inventory.findings.append(str(exc))
    try:
        # The client API is the version that controls the request semantics. A newer daemon does
        # not make an older client safe: before API 1.42, volume prune also removed named volumes.
        version = _docker("version", "--format", "{{.Client.APIVersion}}").strip()
        parts = version.split(".")
        if len(parts) != 2 or not all(part.isdecimal() for part in parts) or tuple(map(int, parts)) < (1, 42):
            raise CleanupError("Docker API below 1.42 or unknown; anonymous-only prune unavailable")
        inventory.anonymous_volumes_removed = _deleted_volume_count(_docker("volume", "prune", "--force"))
    except CleanupError as exc:
        inventory.findings.append(str(exc))
    try:
        output = _docker("builder", "prune", "--force", "--all", "--filter",
                         f"until={BUILD_CACHE_MAX_AGE_HOURS}h")
        match = re.search(
            r"(?m)^Total(?: reclaimed space)?:[ \t]*([0-9]+(?:\.[0-9]+)?[ \t]*[kMGTPE]?B)[ \t]*$",
            output,
        )
        if match is None:
            raise CleanupError("build cache prune result malformed")
        inventory.cache_reclaimed = match.group(1)
    except CleanupError as exc:
        inventory.findings.append(str(exc))
    # Keep the report bounded even on a host with many individual command failures.
    inventory.findings = inventory.findings[:16]
    return inventory.as_dict()


def maintenance_target(instance_dir: Path) -> tuple[Path, Path, list[str]]:
    """``(lock_root, repository, git_prefix)`` of the repository the run packs.

    A live root that is still a Git work tree is packed itself, as before. A plain live root (the
    exporter layout, docs/RECOVERY.md "Writers") has no repository of its own: its checkpoints land
    in the exporter's bare snapshot repository (``offsite.snapshot_repo``), so that is the one that
    accumulates loose objects and the one packed. The state-repo lock stays the live root's, which is
    the lock the exporter takes around a cut. Raises :class:`ummanu.state_repo.StateRepoError` when
    the live root is neither.
    """
    from ummanu.checkpoint import live_root_is_work_tree
    from ummanu.config import DataDirError, instance_data_dir, instance_snapshot_repo

    live = Path(instance_dir).expanduser().resolve()
    if live_root_is_work_tree(live):
        return live, state_repo.require_repo(live), []
    try:
        snapshot = instance_snapshot_repo(live, instance_data_dir(live))
    except DataDirError:
        raise state_repo.StateRepoError(
            f"instance repo is not a git repository and names no snapshot repository: {live}"
        ) from None
    # The bare snapshot repository carries none of the live root's local packing controls
    # (docs/RECOVERY.md), so the same memory bounds ride on the command line instead.
    bounds = [argument for key, value in state_repo.PACKING_CONTROLS if key.startswith("pack.")
              for argument in ("-c", f"{key}={value}")]
    return live, snapshot, [*bounds, "--git-dir", str(snapshot)]


def run(instance_dir: Path) -> dict[str, Any]:
    """One maintenance run. Raises :class:`ummanu.state_repo.StateRepoError` on a Git failure."""
    lock_root, repo, prefix = maintenance_target(instance_dir)
    if prefix and not repo.is_dir():
        # The exporter creates its repository on the first cut; until then there is nothing to pack.
        return {"instance": str(lock_root), "repository": str(repo), "skipped": "snapshot repository absent"}
    started = time.monotonic()
    before = count_objects(repo, prefix)
    state_repo.git(repo, [*prefix, *gc_command()], label="instance gc", timeout=GC_TIMEOUT_SECONDS)
    with state_repo.state_repo_lock(lock_root):
        state_repo.git(
            repo,
            [*prefix, "reflog", "expire", "--all"],
            label="instance reflog expire",
            timeout=REFLOG_TIMEOUT_SECONDS,
        )
    after = count_objects(repo, prefix)
    return {
        "instance": str(lock_root),
        "repository": str(repo),
        "loose_objects": {"before": before.get("count"), "after": after.get("count")},
        "packs": {"before": before.get("packs"), "after": after.get("packs")},
        "duration_s": round(time.monotonic() - started, 3),
    }
