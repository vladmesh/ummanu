"""The window between merge and `ummanu upgrade`: installed units against the new product tree.

The production checkout follows main, so at merge the gate script and the source tree change under
units a host installed earlier; their `ExecStart` is not re-rendered until the next upgrade. Each
test renders a background agent's shipped unit, checks that its `ExecStart` is the one an installed
host already runs, and executes that `ExecStart` against this tree on a precheck path that never
reaches a dispatch: retro and steward find no board (101), curator finds its settlement busy (102).
"""

from __future__ import annotations

import fcntl
import shlex
import site
import subprocess
import tempfile
import unittest
import venv
from pathlib import Path

from ummanu.host import SystemdLayout, render_systemd_unit
from ummanu.runtime.state import PRECHECK_BOARD_UNREACHABLE

ROOT = Path(__file__).resolve().parents[1]
UNITS = ROOT / "packaging" / "systemd"


def _unit_settings(payload: bytes) -> tuple[str, dict[str, str], str]:
    """`ExecStart`, the `Environment=` values and `WorkingDirectory` of one rendered unit."""
    exec_start = ""
    working_directory = ""
    environment: dict[str, str] = {}
    for line in payload.decode("utf-8").splitlines():
        key, _, value = line.partition("=")
        if key == "ExecStart":
            exec_start = value
        elif key == "WorkingDirectory":
            working_directory = value
        elif key == "Environment":
            name, _, setting = value.partition("=")
            environment[name] = setting
    return exec_start, environment, working_directory


class InstalledUnitAgainstNewTreeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        cls.root = Path(temporary.name)
        # A product checkout as the gate sees one: this tree's sources and scripts, and a managed
        # venv of its own whose dependencies are the ones this suite runs with.
        cls.product = cls.root / "product"
        cls.product.mkdir()
        for name in ("src", "scripts", "packaging", "pyproject.toml"):
            (cls.product / name).symlink_to(ROOT / name)
        venv.EnvBuilder(with_pip=False, symlinks=True).create(cls.product / ".venv")
        python = cls.product / ".venv" / "bin" / "python3"
        purelib = subprocess.run(
            [str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        (Path(purelib) / "suite-dependencies.pth").write_text(
            "\n".join(site.getsitepackages()) + "\n", encoding="utf-8"
        )
        cls.home = cls.root / "home"
        cls.layout = SystemdLayout(
            product_root=cls.product,
            instance_path=cls.root / "instance",
            data_dir=cls.root / "data",
            runtime_user="dev",
            runtime_home=cls.home,
        )

    def run_unit(self, agent: str) -> subprocess.CompletedProcess:
        payload = render_systemd_unit((UNITS / f"ummanu-{agent}.service").read_bytes(), self.layout)
        exec_start, environment, working_directory = _unit_settings(payload)
        # The ExecStart every installed host already has; this card must not need a re-render.
        self.assertEqual(exec_start, f"{self.product}/scripts/ummanu-agent-gate.sh {agent}")
        self.assertEqual(environment["UMMANU_RUNTIME_PYTHONPATH"], str(self.product))
        Path(working_directory).mkdir(parents=True, exist_ok=True)
        env = {
            "PATH": "/usr/bin:/bin",
            **environment,
            "UMMANU_GATE_BOARD_ATTEMPTS": "1",
            "UMMANU_GATE_BOARD_WAIT": "0",
        }
        return subprocess.run(
            shlex.split(exec_start),
            cwd=working_directory,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )

    def assert_reached_the_precheck(self, result: subprocess.CompletedProcess) -> None:
        self.assertNotIn("configuration error", result.stderr)
        self.assertNotIn("No module named", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("dispatch", result.stdout)

    def test_board_roles_run_their_precheck_and_defer_on_an_absent_board(self) -> None:
        for agent in ("retro", "steward"):
            with self.subTest(agent):
                result = self.run_unit(agent)
                self.assert_reached_the_precheck(result)
                self.assertEqual(result.returncode, PRECHECK_BOARD_UNREACHABLE, result.stderr)
                self.assertIn(f"[ta-{agent}] precheck: board unreachable after 1 attempts", result.stderr)

    def test_curator_runs_its_precheck_and_defers_a_busy_settlement(self) -> None:
        state = self.home / "ummanu-data" / "automation-state" / "curator"
        state.mkdir(parents=True, exist_ok=True)
        with (state / "cursor-settlement.lock").open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            result = self.run_unit("curator")
        self.assert_reached_the_precheck(result)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("curator: cursor settlement is busy; tick deferred", result.stderr)
        self.assertIn("[ta-curator] precheck: settlement busy, tick deferred", result.stderr)


if __name__ == "__main__":
    unittest.main()
