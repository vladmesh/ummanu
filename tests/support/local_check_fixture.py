"""Temporary adapter and repository for selector behavior, with no operational dependencies."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

from ummanu.cli import main


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
        (self.root / "checks").mkdir(parents=True)
        (self.root / "app").mkdir()
        (self.root / "app" / "__init__.py").write_text("", encoding="utf-8")
        (self.root / "checks" / "__init__.py").write_text("", encoding="utf-8")
        (self.root / ".gitignore").write_text("state/\n__pycache__/\n.pytest_cache/\n", encoding="utf-8")
        (self.root / "checks" / "test_local.py").write_text(
            "import unittest\nfrom pathlib import Path\n"
            "class Cases(unittest.TestCase):\n"
            "    def test_one(self):\n"
            f"        Path({str(self.log)!r}).open('a').write('one\\n')\n"
            "        print('fixture-one')\n"
            "    def test_two(self):\n"
            f"        Path({str(self.log)!r}).open('a').write('two\\n')\n",
            encoding="utf-8",
        )
        (self.root / "checks" / "test_board.py").write_text(
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
                "module": "unittest",
                "import_package": "app",
                "local": {
                    "runner": "unittest",
                    "shards": ["unit"],
                    "modules": {
                        "checks/test_local.py": "unit",
                        "checks/test_board.py": "integration-board",
                    },
                },
            },
        }
        self.write_adapter()
        for argv in (
            ["init", "-q"],
            ["config", "user.name", "fixture"],
            ["config", "user.email", "fixture@example.invalid"],
            ["add", "-A"],
            ["commit", "-q", "-m", "fixture"],
        ):
            subprocess.run(["git", "-C", str(self.root), *argv], check=True, capture_output=True)
        self.enterContext(mock.patch.dict(os.environ, {"UMMANU_INSTANCE": str(self.instance)}))
        self.enterContext(mock.patch("ummanu.broad_check.time.monotonic", return_value=100.0))

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
                    *argv,
                    "--root",
                    str(self.root),
                    "--instance",
                    str(self.instance),
                    "--default-interpreter",
                    sys.executable,
                ]
            )
        text = stdout.getvalue() or stderr.getvalue()
        return status, json.loads(text), stderr.getvalue()
