"""Temporary adapter and repository for selector behavior, with no operational dependencies."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

from ummanu.cli import main


def role_git_env(root: Path, environment: dict[str, str]) -> dict[str, str]:
    """Keep suite Git maintenance controls valid through role credential filtering.

    tests/__init__.py supplies a GIT_CONFIG_COUNT/KEY/VALUE bundle. Role filtering
    removes KEY names, leaving an invalid bundle. Use repository-local settings
    for this isolated fixture instead, without changing production filtering.
    """
    env = {name: value for name, value in environment.items()
           if name != "GIT_CONFIG_COUNT" and not name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))}
    git = shutil.which("git", path=os.defpath)
    if git is None:
        raise RuntimeError("the role receipt fixture requires git")
    for name, value in (("gc.auto", "0"), ("maintenance.auto", "false")):
        subprocess.run([git, "-C", str(root), "config", "--local", name, value],
                       env=env, capture_output=True, check=True, timeout=10)
    return env


class LocalCheckFixture:
    def setUp(self) -> None:
        super().setUp()
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)
        self.root = self.scratch / "repo"
        self.instance = self.scratch / "instance"
        self.log = self.scratch / "executions"
        self.forbidden = self.scratch / "forbidden-import"
        (self.root / "tests").mkdir(parents=True)
        (self.root / "app").mkdir()
        (self.root / "app" / "__init__.py").write_text("", encoding="utf-8")
        (self.root / "tests" / "__init__.py").write_text("", encoding="utf-8")
        (self.root / ".gitignore").write_text("state/\n__pycache__/\n.pytest_cache/\n", encoding="utf-8")
        (self.root / "tests" / "test_local.py").write_text(
            "import unittest\nfrom pathlib import Path\n"
            "class Cases(unittest.TestCase):\n"
            "    def test_one(self):\n"
            f"        Path({str(self.log)!r}).open('a').write('one\\n')\n"
            "        print('fixture-one')\n"
            "    def test_two(self):\n"
            f"        Path({str(self.log)!r}).open('a').write('two\\n')\n",
            encoding="utf-8",
        )
        (self.root / "tests" / "test_board.py").write_text(
            f"from pathlib import Path\nPath({str(self.forbidden)!r}).touch()\n"
            "raise AssertionError('CI-only test imported')\n",
            encoding="utf-8",
        )
        (self.instance / "adapters").mkdir(parents=True)
        (self.instance / "projects").mkdir()
        (self.instance / "projects" / "fixture.yaml").write_text(
            json.dumps(
                {
                    "id": "fixture",
                    "enabled": True,
                    "repo": str(self.root),
                    "adapter": "fixture",
                }
            ),
            encoding="utf-8",
        )
        self.adapter = {
            "setup": {"commands": ["true"]},
            "smoke": {"command": "true"},
            "validation": {"ci": "github"},
            "artifact_policy": {"write_project_files": False},
            "broad_check": {
                "module": "tests.broad",
                "import_package": "app",
                "local": {
                    "runner": "unittest",
                    "shards": ["unit", "component"],
                    "ci_manifest": "tests/ci-shards.txt",
                },
            },
        }
        source = Path(__file__).resolve().parents[2]
        (self.root / "scripts").mkdir()
        shutil.copy(source / "scripts" / "ci_test_shards.py", self.root / "scripts")
        shutil.copy(source / "scripts" / "ci_selection.py", self.root / "scripts")
        (self.root / "src/ummanu/projects").mkdir(parents=True, exist_ok=True)
        shutil.copy(source / "src/ummanu/projects/test_timing.py", self.root / "src/ummanu/projects")
        shutil.copy(source / "tests" / "broad.py", self.root / "tests")
        from scripts.ci_test_shards import SUITES

        entries = ["unit tests/test_local.py", "integration-board tests/test_board.py"]
        for shard in SUITES:
            if shard in {"unit", "integration-board"}:
                continue
            relative = f"tests/test_{shard.replace('-', '_')}.py"
            (self.root / relative).write_text("", encoding="utf-8")
            entries.append(f"{shard} {relative}")
        (self.root / "tests" / "ci-shards.txt").write_text("\n".join(entries) + "\n")
        self.write_adapter()
        self.init_repository()
        self.enterContext(
            mock.patch.dict(
                os.environ,
                {
                    "UMMANU_INSTANCE": str(self.instance),
                    "PYTHONPATH": str(source / "src") + os.pathsep + os.environ.get("PYTHONPATH", ""),
                },
            )
        )
        self.enterContext(mock.patch("ummanu.broad_check.time.monotonic", return_value=100.0))

    def init_repository(self) -> None:
        for argv in (
            ["init", "-q"],
            ["config", "user.name", "fixture"],
            ["config", "user.email", "fixture@example.invalid"],
            ["add", "-A"],
            ["commit", "-q", "-m", "fixture"],
        ):
            subprocess.run(["git", "-C", str(self.root), *argv], check=True, capture_output=True)

    def write_adapter(self) -> None:
        (self.instance / "adapters" / "fixture.yaml").write_text(
            json.dumps(self.adapter),
            encoding="utf-8",
        )

    def invoke(self, *argv: str) -> tuple[int, dict, str]:
        stdout, stderr = StringIO(), StringIO()
        with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
            status = main(
                [
                    "check",
                    "--root",
                    str(self.root),
                    "--instance",
                    str(self.instance),
                    "--default-interpreter",
                    sys.executable,
                    *argv,
                ]
            )
        # Runner diagnostics stream to stderr before a refusal's final one-line JSON.
        # Successful/native-status responses are the complete JSON document on stdout.
        text = stdout.getvalue() or stderr.getvalue().splitlines()[-1]
        return status, json.loads(text), stderr.getvalue()
