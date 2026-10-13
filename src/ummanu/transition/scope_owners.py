"""`transition from-<old> --repair-scope-owners [--apply]`: rename settled heads' scope units.

A head run records its systemd scope in `<data_dir>/heads/<run_id>/scope-owner.json`, and the
lifecycle accepts a record only when its `unit` is `scope_unit(run_id)`. Runs that settled before the
rename recorded the old unit prefix, so after the transition every reader of the host inventory
refuses them (ummanu-1). Their scopes are gone and they admit no launch, so the old unit name says
nothing any more; this repair writes the current one in its place and keeps the original under the
transition state directory.

Only a settled record whose unit is exactly the old name of its own run is repaired. Anything else
carrying the old prefix is left as it is and reported, and the command then exits non-zero: an
unsettled run or an inconsistent identity needs a person, not a rename. The lifecycle's validation
stays as strict as it is; the old prefix is named here and nowhere else.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import config
from ..runtime.head import memory
from .context import Layout, TransitionError
from .names import OLD

OWNER = "scope-owner.json"
#: The runtime roots whose run directories own scopes, as the host inventory reads them.
RUNTIME_ROOTS = (Path("heads"), Path("po-heads"), Path("webproto/heads"))


def old_scope_unit(run_id: str) -> str:
    """The unit `scope_unit(run_id)` named before the rename."""
    return f"{OLD.unit_prefix}head-{hashlib.sha256(run_id.encode()).hexdigest()[:24]}.scope"


@dataclass(frozen=True)
class Finding:
    root: Path
    directory: Path
    #: `repair` (a candidate), `repaired`, or `untouched` (carries the old prefix; left as it is).
    action: str
    detail: str

    def line(self) -> str:
        return f"  {self.action:9} {self.root.as_posix()}/{self.directory.name}: {self.detail}"


def _unit(record: Any) -> str:
    return record.get("unit") if isinstance(record, dict) and isinstance(record.get("unit"), str) else ""


def _inspect(root: Path, directory: Path) -> tuple[Finding | None, bytes, dict[str, Any]]:
    """Classify one run directory: no finding for a record without the old prefix."""
    path = directory / OWNER
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None, b"", {}
    if not stat.S_ISREG(info.st_mode):
        return Finding(root, directory, "untouched", f"{OWNER} is not a regular file"), b"", {}
    raw = path.read_bytes()
    try:
        record = json.loads(raw)
    except ValueError as exc:
        return Finding(root, directory, "untouched", f"unreadable {OWNER}: {exc}"), raw, {}
    unit = _unit(record)
    if not unit.startswith(OLD.unit_prefix):
        return None, raw, record
    run_id = record.get("run_id")
    if not isinstance(run_id, str) or not run_id or directory.name != run_id:
        return Finding(root, directory, "untouched", f"{unit}: the run id does not name this directory"), raw, record
    if unit != old_scope_unit(run_id):
        return Finding(root, directory, "untouched", f"{unit} is not the old scope of run {run_id}"), raw, record
    if record.get("cleanup_complete") is not True or record.get("launch_allowed") is not False:
        return Finding(root, directory, "untouched", f"{unit}: the run is not settled"), raw, record
    if info.st_uid != os.geteuid():
        return Finding(root, directory, "untouched", f"{unit}: owned by uid {info.st_uid}; run as that account"), raw, record
    from ..runtime import local_pty_head

    try:
        local_pty_head.head_scope_owner_valid({**record, "unit": memory.scope_unit(run_id)})
    except memory.MemoryScopeError as exc:
        return Finding(root, directory, "untouched", f"{unit}: still invalid once renamed: {exc}"), raw, record
    return Finding(root, directory, "repair", f"{unit} -> {memory.scope_unit(run_id)}"), raw, record


def _write_atomic(path: Path, data: bytes, mode: int) -> None:
    temporary = path.with_name(f".{path.name}.repair")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    try:
        os.fchmod(fd, mode)
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _backup(backups: Path, raw: bytes) -> None:
    """Keep the original once; a copy left by an interrupted run must be that same original."""
    target = backups / OWNER
    if target.exists():
        if target.read_bytes() != raw:
            raise TransitionError(f"{target} holds a different original; refusing to replace it")
        return
    backups.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_atomic(target, raw, 0o600)


def _repair(finding: Finding, raw: bytes, record: dict[str, Any], backups: Path) -> Finding:
    """Rewrite `unit` alone, under the run's owner lock, once the record is still what was read."""
    from ..runtime import local_pty_head

    path = finding.directory / OWNER
    run_id = record["run_id"]
    try:
        with local_pty_head.head_scope_owner_lock(finding.directory):
            if path.read_bytes() != raw:
                return Finding(finding.root, finding.directory, "untouched", "changed while it was read; run again")
            _backup(backups, raw)
            repaired = {**record, "unit": memory.scope_unit(run_id)}
            _write_atomic(path, json.dumps(repaired).encode("utf-8"), stat.S_IMODE(path.lstat().st_mode))
            local_pty_head.head_scope_owner_valid(json.loads(path.read_bytes()))
    except (memory.MemoryScopeError, TransitionError, OSError, ValueError) as exc:
        return Finding(finding.root, finding.directory, "untouched", f"not repaired: {exc}")
    return Finding(finding.root, finding.directory, "repaired", finding.detail)


def repair_scope_owners(layout: Layout, *, apply: bool) -> int:
    """Print every record carrying the old prefix; with `apply`, repair the candidates.

    Exit 0 when every such record is a candidate (or repaired); 1 when one is left untouched.
    """
    try:
        data_dir = config.instance_data_dir(layout.instance)
    except config.DataDirError as exc:
        raise TransitionError(f"cannot resolve the data directory: {exc}") from None
    mode = "apply" if apply else "plan only, nothing is written"
    print(f"Scope owners carrying {OLD.unit_prefix}head-* under {data_dir}: {mode}")
    findings: list[Finding] = []
    for root in RUNTIME_ROOTS:
        base = data_dir / root
        if base.is_symlink():
            findings.append(Finding(root, base, "untouched", "the runtime root is a symlink"))
            continue
        if not base.is_dir():
            continue
        for directory in sorted(base.iterdir()):
            if directory.is_symlink() or not directory.is_dir():
                continue
            finding, raw, record = _inspect(root, directory)
            if finding is None:
                continue
            if apply and finding.action == "repair":
                finding = _repair(finding, raw, record, layout.state_dir / root / directory.name)
            findings.append(finding)
    for finding in findings:
        print(finding.line())
    counts = {action: sum(f.action == action for f in findings) for action in ("repair", "repaired", "untouched")}
    if apply:
        print(f"{counts['repaired']} repaired (originals in {layout.state_dir}), {counts['untouched']} left untouched")
    else:
        print(f"{counts['repair']} would be repaired, {counts['untouched']} would be left untouched")
    return 1 if counts["untouched"] else 0


__all__ = ["RUNTIME_ROOTS", "old_scope_unit", "repair_scope_owners"]
