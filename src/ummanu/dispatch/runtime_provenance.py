"""In-process access to the production pre-import provenance contract.

The systemd boundary executes :mod:`runtime_preflight` by pathname before the package entry point
is imported. This module launches that same stdlib-only file for dispatcher-owned workspace, gate,
release and cleanup fences, so those callers do not grow a second set of path rules.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ummanu import _proc
from ummanu.dispatch.runtime_preflight import PACKAGE, RuntimeProvenance


def _preflight_source() -> Path:
    return Path(__file__).with_name("runtime_preflight.py")


@dataclass(frozen=True)
class ProductionRuntime:
    """The fixed interpreter and checkout whose integrity dispatcher effects require."""

    interpreter: str
    product_root: str
    package: str = PACKAGE
    workspaces_root: str = ""
    # `<data_dir>/workspaces`, the git-managed card and observer worktrees. Empty checks only Orca's.
    git_workspaces_root: str = ""

    @classmethod
    def current(
        cls,
        product_root: Path | str,
        *,
        workspaces_root: Path | str = "",
        git_workspaces_root: Path | str = "",
    ) -> ProductionRuntime:
        root = workspaces_root or os.environ.get(
            "UMMANU_DISPATCHER_WORKSPACES_ROOT", str(Path.home() / "orca" / "workspaces")
        )
        return cls(
            sys.executable,
            str(Path(product_root).expanduser()),
            workspaces_root=str(root),
            git_workspaces_root=str(git_workspaces_root),
        )

    @classmethod
    def installed(
        cls,
        product_root: Path | str,
        *,
        workspaces_root: Path | str = "",
        git_workspaces_root: Path | str = "",
    ) -> ProductionRuntime:
        """Address the production venv without borrowing Doctor's own interpreter."""
        root = Path(product_root).expanduser()
        workspace_root = workspaces_root or os.environ.get(
            "UMMANU_DISPATCHER_WORKSPACES_ROOT", str(Path.home() / "orca" / "workspaces")
        )
        return cls(
            str(root / ".venv" / "bin" / "python3"),
            str(root),
            workspaces_root=str(workspace_root),
            git_workspaces_root=str(git_workspaces_root),
        )

    def probe(self, within: Callable[[], float] | None = None) -> RuntimeProvenance:
        """Observe the configured interpreter through the executable pre-import boundary.

        With a caller's `within` (seconds left of its deadline) the preflight child runs in its own
        process group inside that deadline (`_proc.run_isolated`); one cut short is unavailable."""
        expected_python = Path(self.interpreter).expanduser().absolute()
        root = Path(self.product_root).expanduser().resolve(strict=False)
        if not expected_python.is_file() or not os.access(expected_python, os.X_OK):
            return RuntimeProvenance("interpreter_unavailable", str(expected_python), str(root), "", ())
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        argv = [
            str(expected_python),
            "-I",
            str(_preflight_source()),
            "--product-root",
            str(root),
            "--interpreter",
            str(expected_python),
            "--package",
            self.package,
            "--workspaces-root",
            self.workspaces_root,
            *(["--git-workspaces-root", self.git_workspaces_root] if self.git_workspaces_root else []),
            "--json",
        ]
        try:
            if within is None:
                completed = subprocess.run(argv, text=True, capture_output=True, timeout=30, check=False, env=env)
            else:
                completed = _proc.run_isolated(argv, env=env, timeout=30, within=within)
        except (OSError, subprocess.SubprocessError):
            return RuntimeProvenance("interpreter_unavailable", str(expected_python), str(root), "", ())
        try:
            payload = json.loads(completed.stdout)
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict) or not payload:
            return RuntimeProvenance("interpreter_unavailable", str(expected_python), str(root), "", ())
        return RuntimeProvenance.from_dict(payload)
