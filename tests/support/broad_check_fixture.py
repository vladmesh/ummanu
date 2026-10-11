"""Shared temporary Git checkout and receipt fixture, with no collected tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

from ummanu.broad_check import (
    CheckSpec,
    run_broad_check,
)
from ummanu.cli import main


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _git_out(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    ).stdout.strip()


def _documents(text: str) -> list[str]:
    """Split the concatenated JSON documents one capture may hold."""
    return [part for part in text.replace("}\n{", "}\n\x00{").split("\x00") if part.strip()]


def _status(argv: list[str]) -> int:
    """Run one CLI command for its exit status alone."""
    with mock.patch("sys.stdout", StringIO()), mock.patch("sys.stderr", StringIO()):
        return main(argv)


def _run_main(argv: list[str]) -> dict:
    """Run one CLI command, capturing the JSON document it prints on stdout."""
    stdout = StringIO()
    with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", StringIO()):
        main(argv)
    return json.loads(stdout.getvalue())


class BroadCheckTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name) / "workspace"
        self.scripts = Path(self.tmpdir.name) / "scripts"
        self.scripts.mkdir()
        self._init_workspace(self.root)
        self.stream = StringIO()
        # A live root with no registered project, unless a case names its own with `--instance`: the
        # CLI neither reads this host's installation nor falls back to the default live root, which
        # it refuses when absent (ummanu-39). An empty one answers `no_project_binding`, as before.
        empty_live_root = Path(self.tmpdir.name) / "live-root"
        empty_live_root.mkdir()
        self.enterContext(mock.patch.dict(os.environ, {"UMMANU_INSTANCE": str(empty_live_root)}))

    def _init_workspace(self, root: Path, *, project_package: str = "ummanu") -> Path:
        """A committed candidate checkout, optionally without any importable project package.

        `project_package=""` is not a curiosity: since issue:8b39e60e4df361c6138e the wrapper puts
        the candidate's own import roots at the front of the check process's `sys.path`, so a
        candidate that *does* carry the package can no longer be made to import someone else's copy
        of it. A test about the candidate boundary therefore needs a candidate with nothing of its
        own to import, which is also a real shape: a checkout mid-rename, or a project whose
        adapter names a package this checkout does not contain.
        """
        root.mkdir(parents=True)
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "worker@example.invalid")
        _git(root, "config", "user.name", "worker")
        # `__pycache__/` is ignored here for the same reason every real checkout ignores it:
        # a Python check writes bytecode as it runs, and that is not a change to the code.
        (root / ".gitignore").write_text("/state/\n__pycache__/\n", encoding="utf-8")
        (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        # A candidate workspace is a checkout of the project under check, and reuse is only ever
        # authorized for a check process that imported the project from here.
        if project_package:
            (root / project_package).mkdir()
            (root / project_package / "__init__.py").write_text("", encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "base")
        return root

    def _script(self, name: str, body: str) -> str:
        path = self.scripts / name
        path.write_text(body, encoding="utf-8")
        return f"{sys.executable} {path}"

    def _run(self, command, **kwargs):
        return run_broad_check(command, root=self.root, stream=self.stream, **kwargs)

    def _suite(
        self, name: str, body: str, args: tuple[str, ...] = (), *, root: Path | None = None
    ) -> CheckSpec:
        """A module-shaped check: the standard shape, and the only one that attests an import."""
        ((root or self.root) / f"{name}.py").write_text(body, encoding="utf-8")
        return CheckSpec.for_module(name, args)


