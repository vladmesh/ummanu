from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ummanu.automations.runtime import dispatch
from ummanu.runtime import codex_preflight, state as runtime_state
from ummanu.runtime.head import HeadSpec
from ummanu.runtime.head_runtimes import LOCAL_PTY_RUNTIME


class RecordingReports:
    """In-memory structural report port for runtime lifecycle tests."""

    def __init__(self) -> None:
        self.created: list[tuple[str, str, str]] = []
        self.moves: list[tuple[str, str, str]] = []

    def create_report(self, *, project: str, title: str, slug: str) -> str:
        self.created.append((project, title, slug))
        return "ummanu-report-1"

    def move_report(self, *, reference: str, target: str, reason: str) -> None:
        self.moves.append((reference, target, reason))


class RecordingRuntime:
    """The supervised backend a bring-up asks to start a head, reduced to what it was asked.

    `test_automations_dispatch_local_pty` raises real heads under a real supervisor; these tests
    are about what a bring-up does before and around `start`, so the start itself is recorded.
    """

    def __init__(self, order: list[str] | None = None, on_start=None) -> None:
        self.order = [] if order is None else order
        self.on_start = on_start
        self.starts: list[dict] = []

    def start(self, spec, workspace, task_ref, **kwargs):
        self.order.append("start")
        if self.on_start is not None:
            self.on_start()
        self.starts.append({"spec": spec, "workspace": workspace, "task_ref": task_ref, **kwargs})
        run = mock.Mock(run_id="run-1", handle="run-1")
        run.to_json.return_value = {"run_id": "run-1"}
        return mock.Mock(status=dispatch.HEAD_ALIVE, ok=True, run=run, evidence={}, reason="")


class TriggeredDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_root = Path(self.tmp.name) / "state"
        self.workspace = str(Path(self.tmp.name) / "workspace")
        Path(self.workspace).mkdir()
        self.env = mock.patch.dict(os.environ, {"TA_STATE": str(self.state_root)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.state_root_patch = mock.patch.object(runtime_state, "STATE_ROOT", self.state_root)
        self.state_root_patch.start()
        self.addCleanup(self.state_root_patch.stop)

    def test_scheduled_memory_launch_binds_the_bearer_to_its_heartbeat(self):
        data_dir = Path(self.tmp.name) / "data"
        spec = HeadSpec(profile_id="test", adapter="codex")
        with mock.patch.object(dispatch, "_installation_data_dir", return_value=data_dir):
            run = dispatch._standing_memory_run("curator", spec, self.workspace, "curator-run")
            command = dispatch._memory_heartbeat(run, dispatch._memory_bound_launch("curator", run, "codex"))

        self.assertIn("ummanu.memory.grant_env", command)
        self.assertIn(str(Path(run.pid_file)), command)
        self.assertNotIn("UMMANU_MEMORY_ACCESS_TOKEN=", command)
        self.assertNotIn("env $(", command, "the bearer must not become an argv entry")
        self.assertIn('export "$grant"', command)

    def test_unreadable_pause_state_blocks_dispatch_and_is_reported(self) -> None:
        output = io.StringIO()
        with (
            mock.patch(
                "ummanu.automations.agents.pipeline.pause.is_paused",
                side_effect=OSError("pause.json: input/output error"),
            ),
            contextlib.redirect_stderr(output),
        ):
            self.assertTrue(dispatch._pipeline_paused())

        self.assertIn("pipeline pause state is unreadable; refusing dispatch", output.getvalue())

    def test_injected_steward_report_port_owns_create_done_and_blocked(self) -> None:
        reports = RecordingReports()
        state = mock.Mock()
        cmd = dispatch.DispatchCommand("/steward", "claude /steward", None, "ummanu-report-1")

        self.assertEqual(
            dispatch._steward_report_card("steward", "hourly", report_board=reports),
            "ummanu-report-1",
        )
        dispatch._release_steward_report(state, "tick", cmd, "not dispatched", report_board=reports)
        dispatch._escalate_steward_preflight_failure(
            state, "tick", cmd, RuntimeError("preflight"), report_board=reports
        )

        self.assertEqual(len(reports.created), 1)
        self.assertEqual([move[1] for move in reports.moves], ["done", "blocked"])


class StandingHeadReadinessTests(unittest.TestCase):
    """The resolution reads `ummanu.head_health`'s one cache and vocabulary.

    The verdicts are planted in `<data>/dispatcher/resource_health.json` exactly as the production
    dispatcher leaves them, so no probe runs: a fresh cache entry answers within the TTL.
    """

    REGISTRY = {  # noqa: RUF012
        "resources": {
            "claude-sub": {"account": "claude", "probe": "false"},
            "openai-sub": {"account": "openai", "probe": "false"},
        },
        "profiles": {
            "claude-high": {"resource": "claude-sub", "adapter": "claude", "fallback": ["codex"]},
            "codex": {"resource": "openai-sub", "adapter": "codex", "fallback": []},
        },
        "role_defaults": {"steward": "claude-high"},
    }

    def setUp(self) -> None:
        from ummanu.runtime import heads as pipeline_heads

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name)
        self.registry = pipeline_heads.Registry(
            self.REGISTRY["resources"], self.REGISTRY["profiles"], self.REGISTRY["role_defaults"]
        )
        self.snapshot = dispatch.RegistrySnapshot(self.registry)
        env = mock.patch.dict(os.environ, {"UMMANU_DATA_DIR": str(self.data_dir)})
        env.start()
        self.addCleanup(env.stop)
        spec = mock.patch.object(dispatch, "_load_spec", return_value={"skill": "/steward"})
        spec.start()
        self.addCleanup(spec.stop)

    def plant(self, **statuses: str) -> None:
        cache = {
            resource.replace("_", "-"): {"resource": resource.replace("_", "-"), "status": status,
                                         "reason": "planted", "checked_at": time.time()}
            for resource, status in statuses.items()
        }
        path = self.data_dir / "dispatcher" / "resource_health.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache), encoding="utf-8")

    def test_a_red_preferred_head_resolves_down_its_chain(self) -> None:
        self.plant(claude_sub="unavailable", openai_sub="ready")
        self.assertEqual(dispatch._resolve_launch("steward", snapshot=self.snapshot).profile, "codex")

    def test_an_unknown_resource_keeps_the_preferred_head(self) -> None:
        self.plant(claude_sub="unknown", openai_sub="ready")
        self.assertEqual(dispatch._resolve_launch("steward", snapshot=self.snapshot).profile, "claude-high")

    def test_nothing_launchable_keeps_the_preferred_head(self) -> None:
        """As before: an all-red chain still launches the preferred profile rather than none."""
        self.plant(claude_sub="unavailable", openai_sub="exhausted")
        self.assertEqual(dispatch._resolve_launch("steward", snapshot=self.snapshot).profile, "claude-high")

    def test_an_unreadable_registry_is_refused_rather_than_launched_bare(self) -> None:
        """secretary-1720: there is no bare default-model `claude` fallback any more."""
        with self.assertRaisesRegex(dispatch.NoSupervisedHead, "the head registry would not load"):
            dispatch._resolve_launch("steward", snapshot=dispatch.RegistrySnapshot(None, "OSError: gone"))


class TriggeredCodexHeadTests(unittest.TestCase):
    """A service head on Codex is an interactive session, brought up and then prompted.

    Curator, retro and steward launch through the same registry as a worker, so they inherited the
    TUI-only rule with it (secretary-1173): their command carries no skill, and the skill is handed
    to the head across the supervisor's boundary after it is up.
    """

    REGISTRY = {  # noqa: RUF012
        "resources": {"openai-sub": {"account": "openai-subscription", "probe": "true"}},
        "profiles": {"codex": {"resource": "openai-sub", "adapter": "codex", "fallback": []}},
        "role_defaults": {"retro": "codex"},
    }

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = str(Path(self.tmp.name) / "workspace")
        Path(self.workspace).mkdir()
        from ummanu.runtime import heads as pipeline_heads

        self.registry = pipeline_heads.Registry(
            self.REGISTRY["resources"], self.REGISTRY["profiles"], self.REGISTRY["role_defaults"]
        )

    def test_a_codex_service_head_is_launched_without_its_skill(self) -> None:
        from ummanu.head_health import HeadChoice, HeadReadiness
        from ummanu.runtime import heads as pipeline_heads

        chosen = HeadChoice("codex", "codex", HeadReadiness("openai-sub", "ready", "probe succeeded", 0.0))
        with (
            mock.patch.object(dispatch, "_load_spec", return_value={"skill": "/retro"}),
            mock.patch.object(dispatch, "_workspace", return_value=self.workspace),
            mock.patch.object(pipeline_heads, "load_registry", return_value=self.registry),
            mock.patch.object(dispatch, "resolve_head_chain", return_value=chosen),
        ):
            skill, launch, profile, after_start, profile_data = dispatch._launch_cmd("retro")

        self.assertEqual((skill, profile), ("/retro", "codex"))
        self.assertTrue(after_start)
        self.assertNotIn("codex exec", launch)
        self.assertNotIn("/retro", launch)
        # The profile the command was rendered from travels with it, so the preflight that has to
        # run before the head exists reads the same CODEX_HOME the launch names.
        self.assertEqual(profile_data, self.REGISTRY["profiles"]["codex"])

    def test_the_skill_is_handed_to_the_supervisor_as_the_heads_prompt(self) -> None:
        """Through the backend's own pointer, never typed into the launch command."""
        command = dispatch.DispatchCommand(
            "/retro",
            "codex",
            "codex",
            None,
            prompt_after_start=True,
            head_profile=self.REGISTRY["profiles"]["codex"],
        )
        runtime = RecordingRuntime()
        state = mock.Mock()
        state.load_head_run.return_value = None

        with (
            mock.patch.object(dispatch, "_ensure_head_ready"),
            mock.patch.object(dispatch, "_local_pty_runtime", return_value=runtime),
            mock.patch.object(dispatch, "_installation_data_dir", return_value=Path(self.tmp.name) / "data"),
        ):
            self.assertEqual(
                dispatch._supervised_bring_up("retro", self.workspace, state, "dispatch", command), 0
            )

        (start,) = runtime.starts
        self.assertEqual(start["pointer"].text, "$retro ")
        self.assertIsNotNone(start["transport"])
        self.assertNotIn("/retro", start["command"])

    def test_an_agent_spec_head_is_used_under_its_own_id(self) -> None:
        """The spec's last-resort head is an ordinary id: returned as-is when the registry has it."""
        from ummanu.runtime import heads as pipeline_heads

        registry = pipeline_heads.Registry(self.REGISTRY["resources"], self.REGISTRY["profiles"], {})

        with mock.patch.object(pipeline_heads, "load_registry", return_value=registry):
            self.assertEqual(dispatch._preferred_head("retro", {"head": "codex"}), "codex")
            self.assertIsNone(dispatch._preferred_head("retro", {}))

    def test_a_service_head_pinned_to_a_retired_id_fails_closed(self) -> None:
        """secretary-1697: no alias table stands a retired id in for a tier of today's registry.

        The dispatch is refused by name rather than rendered as whatever profile looks closest.
        """
        from ummanu.runtime import heads as pipeline_heads

        tiers = pipeline_heads.Registry(
            self.REGISTRY["resources"],
            {
                "codex-terra-high": {"resource": "openai-sub", "adapter": "codex", "fallback": []},
                "claude-opus-high": {"resource": "openai-sub", "adapter": "claude", "fallback": []},
            },
            {},
        )

        with (
            mock.patch.object(pipeline_heads, "load_registry", return_value=tiers),
            mock.patch.object(
                dispatch, "_load_spec", return_value={"skill": "/retro", "head": "codex-terra"}
            ),
            mock.patch.object(dispatch, "_workspace", return_value=self.workspace),
        ):
            with self.assertRaisesRegex(pipeline_heads.HeadRegistryError, "unknown head 'codex-terra'"):
                dispatch._preferred_head("retro", {"head": "codex-terra"})
            with self.assertRaisesRegex(dispatch.NoSupervisedHead, "unknown head 'codex-terra'"):
                dispatch._launch_cmd("retro")


class TriggeredCodexPreflightTests(unittest.TestCase):
    """A service head is only started once its workspace can actually hold a Codex head.

    The interactive Codex heads a service tick brings up (curator, retro, steward) are asked about
    directory trust before they will take a prompt, and nobody is sitting in front of the head to
    answer. So the tick answers it first, through the same preflight the Ummanu dispatcher uses,
    and a workspace it cannot prepare fails before the supervisor starts anything (secretary-1173).
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = str(self.root / "workspace")
        Path(self.workspace).mkdir()
        self.codex_home = self.root / "codex-home"
        self.profile = {
            "resource": "openai-sub",
            "adapter": "codex",
            "model": "gpt-5.6-terra",
            "codex_home": str(self.codex_home),
            "fallback": [],
            "runtime": LOCAL_PTY_RUNTIME,
        }
        self.command = dispatch.DispatchCommand(
            "/retro", "codex", "codex", None, prompt_after_start=True, head_profile=self.profile
        )
        data_dir = mock.patch.object(dispatch, "_installation_data_dir", return_value=self.root / "data")
        data_dir.start()
        self.addCleanup(data_dir.stop)

    def _trusted(self) -> dict:
        import tomllib

        return tomllib.loads((self.codex_home / "config.toml").read_text(encoding="utf-8"))

    @staticmethod
    def _state() -> mock.Mock:
        state = mock.Mock()
        state.load_head_run.return_value = None
        return state

    @staticmethod
    def _allowed_attestation(_profile, run, **_kwargs):
        """An independently accepted provider-schema result for shared-boundary tests."""
        return run.with_fanout_policy(
            {
                "version": 1,
                "state": "allowed",
                "terminal_state": "clean",
                "run_id": run.run_id,
                "role": run.role,
                "model": run.spec.model or "",
                "binary_path": "/test/codex",
                "binary_digest": "0" * 64,
                "cli_version": "test-codex",
                "tool_schema_digest": "0" * 64,
                "provider_schema_verdict": "no_callable_child_spawn_surface",
                "events": [],
            }
        )

    def test_a_fresh_codex_service_workspace_is_trusted_before_its_head_starts(self) -> None:
        order: list[str] = []
        # What the head is started against has to be a workspace already recorded as trusted;
        # answering the dialog afterwards would be answering it too late.
        runtime = RecordingRuntime(
            order,
            on_start=lambda: order.append(
                "trusted" if (self.codex_home / "config.toml").is_file() else "untrusted"
            ),
        )
        shared_preflight = dispatch.preflight_codex_launch

        def recording_preflight(*args, **kwargs):
            order.append("preflight")
            return shared_preflight(*args, **kwargs)

        with (
            mock.patch.object(dispatch, "_local_pty_runtime", return_value=runtime),
            mock.patch.object(codex_preflight, "attest_codex_fanout", side_effect=self._allowed_attestation),
            mock.patch.object(dispatch, "preflight_codex_launch", side_effect=recording_preflight),
        ):
            dispatch._supervised_bring_up("retro", self.workspace, self._state(), "dispatch", self.command)

        self.assertEqual(order, ["preflight", "start", "trusted"])
        trusted = self._trusted()
        self.assertEqual(trusted["projects"][str(Path(self.workspace).resolve())]["trust_level"], "trusted")

    def test_a_claude_service_head_keeps_its_own_best_effort_preparation(self) -> None:
        """Claude's first-run prep is unchanged, and no codex config is written for it."""
        command = dispatch.DispatchCommand(
            "/retro", "claude '/retro'", "claude-opus", None, head_profile={"adapter": "claude"}
        )

        with (
            mock.patch.object(dispatch, "_local_pty_runtime", return_value=RecordingRuntime()),
            mock.patch.object(dispatch, "_ensure_claude_ready") as claude_ready,
        ):
            dispatch._supervised_bring_up("retro", self.workspace, self._state(), "dispatch", command)

        claude_ready.assert_called_once_with(self.workspace)
        self.assertFalse(self.codex_home.exists())

    def test_a_schema_absent_service_preflight_starts_the_head(self) -> None:
        """Schema evidence is advisory; the shared trust preflight still precedes the start."""
        runtime = RecordingRuntime()
        state = self._state()

        with mock.patch.object(dispatch, "_local_pty_runtime", return_value=runtime):
            dispatch._supervised_bring_up("retro", self.workspace, state, "dispatch", self.command)

        self.assertEqual(len(runtime.starts), 1)
        state.save_active_report.assert_called_once_with(None, "run-1")
        self.assertEqual(
            self._trusted()["projects"][str(Path(self.workspace).resolve())]["trust_level"], "trusted"
        )

    def test_an_untrusted_workspace_rejects_an_otherwise_allowed_service_preflight(self) -> None:
        """Trust is still the hard pre-start check, regardless of provider telemetry."""
        self.codex_home.mkdir()
        config = self.codex_home / "config.toml"
        config.write_text(
            f'[projects.{json.dumps(str(Path(self.workspace).resolve()))}]\ntrust_level = "untrusted"\n',
            encoding="utf-8",
        )
        runtime = RecordingRuntime()

        with (
            mock.patch.object(dispatch, "_local_pty_runtime", return_value=runtime),
            mock.patch.object(codex_preflight, "attest_codex_fanout", side_effect=self._allowed_attestation),
            self.assertRaises(dispatch.CodexPreflightError),
        ):
            dispatch._supervised_bring_up(
                "retro", self.workspace, self._state(), "dispatch", self.command
            )

        self.assertEqual(runtime.starts, [])
        self.assertIn('trust_level = "untrusted"', config.read_text(encoding="utf-8"))

    def test_a_failed_preflight_escalates_the_steward_card_instead_of_closing_it(self) -> None:
        """No head was started, so no sweep happened: the card must not be recorded as one.

        Done is what a dispatch that already had a head closes out with. A workspace that could
        never hold a head is a condition a later tick cannot heal on its own, so the card goes to
        the board's own wait-for-a-human state with the reason attached.
        """
        self.codex_home.mkdir()
        (self.codex_home / "config.toml").write_text(
            f'[projects.{json.dumps(str(Path(self.workspace).resolve()))}]\ntrust_level = "untrusted"\n',
            encoding="utf-8",
        )
        command = dispatch.DispatchCommand(
            "/steward --card ummanu-817",
            "codex",
            "codex",
            "ummanu-817",
            prompt_after_start=True,
            head_profile=self.profile,
        )
        reports = RecordingReports()
        state = self._state()
        runtime = RecordingRuntime()

        with (
            mock.patch.object(dispatch, "_local_pty_runtime", return_value=runtime),
            self.assertRaises(dispatch.CodexPreflightError),
        ):
            dispatch._supervised_bring_up(
                "steward",
                self.workspace,
                state,
                "dispatch",
                command,
                reports=dispatch._TickReports("steward", state, "dispatch", report_board=reports),
            )

        self.assertEqual(runtime.starts, [])
        self.assertEqual(reports.moves[0][:2], ("ummanu-817", "blocked"))
        self.assertIn("no head was started", reports.moves[0][2])
        self.assertIn("trust_level 'untrusted'", reports.moves[0][2])
        self.assertNotIn("done", [call[1] for call in reports.moves])
        state.clear_active_report.assert_called_once_with("ummanu-817")

    def test_the_service_launcher_and_the_dispatcher_run_one_preflight(self) -> None:
        """Not two implementations that agree today: the same function object."""
        from ummanu.dispatch import launcher as dispatcher_launcher

        self.assertIs(dispatch.preflight_codex_launch, codex_preflight.preflight_codex_launch)
        self.assertIs(
            dispatcher_launcher._preflight_codex_workspace, codex_preflight.ensure_codex_workspace_trusted
        )


if __name__ == "__main__":
    unittest.main()
