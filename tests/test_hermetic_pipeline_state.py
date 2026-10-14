"""The unit suite reads its own pipeline state dir, never the live installation's.

`ummanu.automations.agents.pipeline.state` resolves `STATE` at import time and
`agents.pipeline.pause` binds `PAUSE_FILE` off it, so the pause path every
triggered-dispatch test runs against is fixed before the first test body. If that path
is the live `<workspaces>/ummanu/pipeline/state/pipeline`, an operator holding a
freeze on the host running the suite turns `runtime/dispatch._pipeline_paused()` true
and every dispatch test quietly takes the "pipeline paused — no dispatch" branch
instead of the lifecycle branch it asserts about. `tests/__init__.py` closes that by
claiming a throwaway `UMMANU_PIPELINE_STATE_DIR` for the whole run (secretary-1403).

These tests hold both ends of that seam: the default is in force and points somewhere
this run owns, and the pause read itself still works — a suite that simply stopped
reading pause.json would pass a "not paused" assertion for the wrong reason.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests
from ummanu.automations.agents.pipeline import pause as pipeline_pause, state as pipeline_state
from ummanu.automations.runtime import dispatch
from ummanu.runtime import shared_state, state as runtime_state

_REPO_ROOT = Path(__file__).resolve().parent.parent

_HARD_FREEZE = {
    "mode": "hard",
    "since": "2026-08-10T00:00:00Z",
    "reason": "maintenance window held by an operator while the suite runs",
    "actor": "vladmesh",
}


def _write_production_like_state_dir(root: Path) -> Path:
    """A frozen state dir laid out exactly where `resolve_pipeline_state_dir` looks without
    the override: `<workspaces>/ummanu/pipeline/state/pipeline`. Built under a temporary
    workspaces root — the live one is read by running agents and is never written here."""
    state_dir = root / shared_state.AGENTS_PROJECT / "pipeline" / "state" / "pipeline"
    state_dir.mkdir(parents=True)
    (state_dir / "pause.json").write_text(json.dumps(_HARD_FREEZE), encoding="utf-8")
    return state_dir


class SuitePipelineStateDirTests(unittest.TestCase):
    def test_the_suite_owns_the_pipeline_state_dir_it_resolved_at_import(self) -> None:
        suite_dir = Path(os.environ["UMMANU_PIPELINE_STATE_DIR"])
        self.assertEqual(suite_dir, tests._SUITE_PIPELINE_STATE_DIR)
        self.assertEqual(shared_state.resolve_pipeline_state_dir(), suite_dir)
        # The import-time bindings, not just a fresh resolve: these are what dispatch reads.
        self.assertEqual(pipeline_state.STATE.dir, suite_dir)
        self.assertEqual(pipeline_pause.PAUSE_FILE, suite_dir / "pause.json")
        self.assertNotIn(shared_state.WORKSPACES_ROOT, suite_dir.parents)

    def test_a_freeze_in_a_production_like_state_dir_is_not_the_file_the_suite_reads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frozen = _write_production_like_state_dir(Path(tmp) / "workspaces")
            with mock.patch.object(shared_state, "WORKSPACES_ROOT", Path(tmp) / "workspaces"):
                self.assertEqual(shared_state.resolve_pipeline_state_dir(), tests._SUITE_PIPELINE_STATE_DIR)
                self.assertNotEqual(pipeline_pause.PAUSE_FILE, frozen / "pause.json")
                self.assertFalse(pipeline_pause.is_paused())

    def test_the_suite_default_replaces_an_ambient_override_from_the_operators_shell(self) -> None:
        """A worker/reviewer shell exports the live installation's UMMANU_PIPELINE_STATE_DIR. Importing
        `tests` has to take that path away, so the check runs in a child process that inherits one."""
        with tempfile.TemporaryDirectory() as tmp:
            frozen = _write_production_like_state_dir(Path(tmp) / "workspaces")
            env = dict(os.environ)
            env["UMMANU_PIPELINE_STATE_DIR"] = str(frozen)
            env["PYTHONPATH"] = os.pathsep.join(
                [str(_REPO_ROOT / "src"), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
            )
            probe = (
                "import json, tests\n"
                "from ummanu.automations.agents.pipeline import pause\n"
                "print(json.dumps({'paused': pause.is_paused(), 'file': str(pause.PAUSE_FILE)}))\n"
            )
            done = subprocess.run(
                [sys.executable, "-c", probe],
                check=False,
                cwd=str(_REPO_ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
        self.assertEqual(done.returncode, 0, done.stderr)
        seen = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertNotEqual(Path(seen["file"]), frozen / "pause.json")
        self.assertFalse(seen["paused"])

    def test_the_suites_own_pause_file_is_still_read(self) -> None:
        """Not a vacuous "not paused": the reader works, it just reads a file this run owns."""
        self.assertFalse(pipeline_pause.is_paused())
        pipeline_pause.PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.addCleanup(pipeline_pause.PAUSE_FILE.unlink, missing_ok=True)
        pipeline_pause.PAUSE_FILE.write_text(json.dumps(_HARD_FREEZE), encoding="utf-8")
        self.assertTrue(pipeline_pause.is_paused())


class TriggeredDispatchIgnoresAProductionFreezeTests(unittest.TestCase):
    """The behavioural half: a hard freeze sitting in a production-like state directory cannot
    make a triggered-dispatch test skip. A regression shows up as "paused" where a bring-up is
    expected."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_root = Path(self.tmp.name) / "state"
        self.workspace = str(Path(self.tmp.name) / "workspace")
        Path(self.workspace).mkdir()
        env = mock.patch.dict(os.environ, {"UMMANU_STATE": str(self.state_root)})
        env.start()
        self.addCleanup(env.stop)
        state_root_patch = mock.patch.object(runtime_state, "STATE_ROOT", self.state_root)
        state_root_patch.start()
        self.addCleanup(state_root_patch.stop)
        self.resolution = dispatch.LaunchResolution("/retro", "head", {"adapter": "claude"})

    def _actions(self, agent: str = "retro") -> list[str]:
        runs = self.state_root / agent / "runs.jsonl"
        return [json.loads(line)["action"] for line in runs.read_text(encoding="utf-8").splitlines()]

    def test_a_hard_freeze_in_a_production_like_state_dir_does_not_skip_a_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspaces = Path(tmp) / "workspaces"
            _write_production_like_state_dir(workspaces)
            with (
                mock.patch.object(shared_state, "WORKSPACES_ROOT", workspaces),
                mock.patch.object(dispatch, "_workspace", return_value=self.workspace),
                mock.patch.object(dispatch, "_resolve_launch", return_value=self.resolution),
                mock.patch.object(dispatch, "_supervised_bring_up", return_value=0) as bring_up,
            ):
                self.assertEqual(dispatch.run("retro"), 0)

        bring_up.assert_called_once()
        runs = self.state_root / "retro" / "runs.jsonl"
        self.assertNotIn("paused", self._actions() if runs.exists() else [])


if __name__ == "__main__":
    unittest.main()
