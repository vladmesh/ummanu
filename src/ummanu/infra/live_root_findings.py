"""Doctor's red findings for a live root still in its pre-cutover shape.

The live root is a plain directory (`runtime.paths.default_instance_path`), not a Git work tree,
and nothing that starts the installation names the path it had before: the instance repository's
checkout in the runtime home (`old_name_guard.INSTANCE_PROJECT`, the one spelling of that name).

- ``live_root.git_work_tree``: the configured live root holds ``.git``.
- ``live_root.old_path``: an installed ``ummanu-*`` unit file, the installation's runtime env file,
  or the role env of a process bound to this installation names the old path.

Both are red. An installation that has not been cut over yet has both, by design: they name what
the cutover changes (docs/RECOVERY.md, "Names on the host").
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path

from ummanu.infra.old_name_guard import INSTANCE_PROJECT
from ummanu.runtime.paths import INSTANCE_ENV

#: Current product units inspected by this policy; foreign unit prefixes are outside its scope.
UNIT_PREFIX = "ummanu-"

GIT_WORK_TREE = "live_root.git_work_tree"
OLD_PATH = "live_root.old_path"

#: The installation's runtime env file, inside its live root; the units load it, the roles read it.
RUNTIME_ENV_NAME = "runtime.env"
#: The role env names that bind a launched process to an installation (`runtime.role_env`).
BINDING_ENV = (INSTANCE_ENV, "UMMANU_RUNTIME_ENV_FILE", "TA_RUNTIME_ENV_FILE")


def old_path_pattern(home: Path | None = None) -> re.Pattern[str]:
    """The old live root as a path: ``<a home>/<old name>``, not the name in a remote or a card ref."""
    homes = [r"~", r"\$HOME", r"\$\{HOME\}", r"%h", r"/home/[^/\s\"'=:]+", r"/root"]
    if home is not None:
        homes.append(re.escape(str(home)))
    return re.compile(rf"(?:{'|'.join(homes)})/{re.escape(INSTANCE_PROJECT)}(?![\w.-])")


def git_work_tree_finding(live_root: Path) -> dict[str, object] | None:
    """Red when the live root holds ``.git``: a directory, a worktree's file or a dangling link."""
    marker = live_root / ".git"
    try:
        marker.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        pass
    return {
        "code": GIT_WORK_TREE,
        "severity": "red",
        "path": str(marker),
        "message": f"the live root {live_root} is a Git work tree ({marker}); the live root is a plain directory",
    }


def _old_path_finding(
    source: str, where: str, pattern: re.Pattern[str], text: str
) -> dict[str, object] | None:
    match = pattern.search(text)
    if match is None:
        return None
    return {
        "code": OLD_PATH,
        "severity": "red",
        "source": source,
        "path": where,
        "message": f"{source} names the old live root {match.group(0)}",
    }


def unit_file_findings(unit_dir: Path, pattern: re.Pattern[str]) -> list[dict[str, object]]:
    """One finding per installed ``ummanu-*`` unit file that names the old path."""
    try:
        entries = sorted(entry for entry in unit_dir.iterdir() if entry.name.startswith(UNIT_PREFIX))
    except OSError:
        return []
    findings: list[dict[str, object]] = []
    for entry in entries:
        try:
            if not entry.is_file():
                continue
            text = entry.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        finding = _old_path_finding(f"unit {entry.name}", str(entry), pattern, text)
        if finding is not None:
            findings.append(finding)
    return findings


def runtime_env_findings(live_root: Path, pattern: re.Pattern[str]) -> list[dict[str, object]]:
    """The installation's runtime env file, by its lines' values; comments are not configuration."""
    path = live_root / RUNTIME_ENV_NAME
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        finding = _old_path_finding(f"runtime env {path}", str(path), pattern, stripped)
        if finding is not None:
            return [finding]
    return []


def role_env_findings(
    live_root: Path, pattern: re.Pattern[str], environ: Mapping[str, str]
) -> list[dict[str, object]]:
    """The binding names of a process bound to this live root.

    Only a process whose ``UMMANU_INSTANCE`` is this live root carries its role env; one bound to
    another installation (a test, a candidate checkout) says nothing about this one.
    """
    bound = environ.get(INSTANCE_ENV)
    if not bound or not _same_path(Path(bound).expanduser(), live_root):
        return []
    findings = []
    for name in BINDING_ENV:
        value = environ.get(name)
        if value:
            finding = _old_path_finding(f"role env {name}", name, pattern, value)
            if finding is not None:
                findings.append(finding)
    return findings


def _same_path(left: Path, right: Path) -> bool:
    try:
        return os.path.samefile(left, right)
    except OSError:
        return os.path.abspath(left) == os.path.abspath(right)


def live_root_findings(
    live_root: Path,
    *,
    unit_dirs: Iterable[Path] = (),
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> list[dict[str, object]]:
    """Every red live-root finding, the work tree first; ``unit_dirs`` empty reads no host."""
    pattern = old_path_pattern(home)
    findings: list[dict[str, object]] = []
    work_tree = git_work_tree_finding(live_root)
    if work_tree is not None:
        findings.append(work_tree)
    for unit_dir in unit_dirs:
        findings.extend(unit_file_findings(unit_dir, pattern))
    findings.extend(runtime_env_findings(live_root, pattern))
    findings.extend(role_env_findings(live_root, pattern, os.environ if environ is None else environ))
    return findings
