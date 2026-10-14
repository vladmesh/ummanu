"""Focused contract tests for the one entry of the background agents, `ummanu automations`."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ummanu import cli as ummanu_cli
from ummanu.automations import __main__ as triggered_main, composition
from ummanu.automations.agents.retro import cli as retro_cli
from ummanu.automations.agents.steward import cli as steward_cli
from ummanu.board.steward_reports import StewardReportBoard
from ummanu.config import DataDirError
from ummanu.runtime.state import (
    PRECHECK_BOARD_UNREACHABLE,
    PRECHECK_DEFERRED,
    PRECHECK_SKIP,
    AgentState,
    BoardUnavailable,
)
from ummanu.tasks import TaskError


class StewardCliReaderTests(unittest.TestCase):
    class Reader:
        def active_cards(self, *, states=None, project=None):
            return []

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = AgentState("steward", state_dir=Path(self.tmp.name) / "state")
        self.reader = self.Reader()

    def test_scan_precheck_and_advance_pass_an_explicit_reader(self) -> None:
        batch = {"pending": {"notified_blocked": []}, "signals": {"new_blocked": []}}
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(steward_cli, "STATE", self.state))
            scan = stack.enter_context(mock.patch.object(steward_cli.signals, "scan", return_value=batch))
            stack.enter_context(mock.patch.object(steward_cli.signals, "ensure_pipeline_baseline"))
            stack.enter_context(mock.patch.object(steward_cli.signals, "has_signal", return_value=False))
            self.assertEqual(steward_cli.cmd_scan(True, self.reader), 0)
            scan.assert_called_once_with(self.reader)
            scan.reset_mock()
            self.assertEqual(steward_cli.cmd_precheck(self.reader), 100)
            scan.assert_called_once_with(self.reader)

            self.state.ensure_dir()
            self.state.pending_file.write_text(json.dumps({"notified_blocked": []}), encoding="utf-8")
            self.assertEqual(steward_cli.cmd_advance(self.reader), 0)


class StandingAgentEntrypointTests(unittest.TestCase):
    def test_signal_commands_get_the_canonical_reader(self) -> None:
        reader = object()
        with (
            mock.patch.object(composition, "_signal_board", return_value=reader) as board,
            mock.patch.object(steward_cli, "main", return_value=17) as main,
        ):
            self.assertEqual(composition.main(["steward", "scan", "--json"]), 17)

        board.assert_called_once_with()
        main.assert_called_once_with(["scan", "--json"], reader=reader)

    def test_normal_dispatch_uses_one_client_for_canonical_report_adapter(self) -> None:
        client = object()
        writer_calls: list[tuple[object, Path, object]] = []

        class Writer:
            def __init__(self, actual_client, *, data_dir):
                writer_calls.append((actual_client, Path(data_dir), self))

        with (
            mock.patch.dict(
                os.environ,
                {"UMMANU_INSTANCE": "/instance", "UMMANU_DATA_DIR": "/audit"},
                clear=False,
            ),
            mock.patch.object(composition, "card_client", return_value=client) as factory,
            mock.patch.object(composition, "TaskWriter", Writer),
            mock.patch.object(composition.dispatch, "run", return_value=0) as run,
        ):
            self.assertEqual(composition.main(["steward", "dispatch", "deep-sweep"]), 0)
            _, kwargs = run.call_args
            board = kwargs["report_board"]
            self.assertIsInstance(board, StewardReportBoard)
            self.assertIs(board.reader.client, client)
            self.assertIs(board.writer, writer_calls[0][2])

        factory.assert_called_once_with(Path("/instance"))
        self.assertEqual(
            [(actual_client, data_dir) for actual_client, data_dir, _ in writer_calls],
            [(client, Path("/audit"))],
        )

    def test_report_board_is_lazy_until_runtime_needs_a_report(self) -> None:
        with mock.patch.object(composition, "card_client", side_effect=AssertionError("client")
        ):
            board = composition._report_board()
        self.assertIsInstance(board, StewardReportBoard)

    def test_cleanup_only_never_constructs_board_or_reads_instance(self) -> None:
        with (
            mock.patch.object(composition, "_instance_path", side_effect=AssertionError("instance")),
            mock.patch.object(composition, "_report_board", side_effect=AssertionError("report board")),
            mock.patch.object(composition.dispatch, "run", return_value=0) as run,
        ):
            self.assertEqual(composition.main(["steward", "dispatch", "--cleanup-only"]), 0)

        run.assert_called_once_with("steward", None, cleanup_only=True)

    def test_non_steward_is_delegated_without_reinterpretation(self) -> None:
        argv = ["retro", "dispatch", "--generation", "bad"]
        with mock.patch.object(triggered_main, "main", return_value=4) as main:
            self.assertEqual(composition.main(argv), 4)
        main.assert_called_once_with(argv)

    def test_retro_cleanup_port_is_lazy_and_only_passed_to_cleanup_commands(self) -> None:
        with (
            mock.patch.object(composition, "card_client", side_effect=AssertionError("client")
            ),
            mock.patch.object(retro_cli, "main", return_value=8) as main,
        ):
            self.assertEqual(composition.main(["retro", "harvest", "--json"]), 8)
        board = main.call_args.kwargs["retention"]
        self.assertIsNotNone(board)

        with mock.patch.object(triggered_main, "main", return_value=9) as legacy:
            self.assertEqual(composition.main(["retro", "status"]), 9)
        legacy.assert_called_once_with(["retro", "status"])

    def test_retro_precheck_maps_canonical_backend_unavailable_to_101(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = AgentState("retro", state_dir=Path(tmp) / "state")
            with (
                mock.patch.object(retro_cli, "STATE", state),
                mock.patch.object(composition, "card_client",
                    side_effect=TaskError("backend_unavailable", "transport unavailable", 1),
                ),
            ):
                self.assertEqual(composition.main(["retro", "precheck"]), PRECHECK_BOARD_UNREACHABLE)

    def test_retro_late_canonical_error_is_mapped_by_the_lazy_port(self) -> None:
        class Reader:
            def done_retention_candidates(self):
                raise TaskError("backend_unavailable", "late RPC", 1)

        class Writer:
            pass

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"UMMANU_DATA_DIR": tmp}, clear=False),
            mock.patch.object(composition, "card_client", return_value=object()),
            mock.patch.object(composition, "TaskReader", return_value=Reader()),
            mock.patch.object(composition, "TaskWriter", return_value=Writer()),
            self.assertRaises(BoardUnavailable),
        ):
            port = composition._done_retention_board()
            port.close_old_done()

    def test_report_config_errors_remain_failures_when_a_report_is_needed(self) -> None:
        with (
            mock.patch.dict(
                os.environ, {"UMMANU_INSTANCE": "/instance", "UMMANU_DATA_DIR": ""}, clear=False
            ),
            mock.patch.object(composition, "card_client", return_value=object()),
            mock.patch.object(composition, "instance_data_dir", side_effect=DataDirError("bad instance")),
            self.assertRaisesRegex(DataDirError, "bad instance"),
        ):
            _ = composition._report_board().reader

    def test_precheck_classifies_canonical_backend_unavailable_as_deferred(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = AgentState("steward", state_dir=Path(tmp) / "state")
            with (
                mock.patch.object(steward_cli, "STATE", state),
                mock.patch.object(
                    steward_cli.signals,
                    "scan",
                    side_effect=lambda reader: reader.active_cards(),
                ),
                mock.patch.object(composition, "card_client",
                    side_effect=TaskError("backend_unavailable", "transport unavailable", 1),
                ) as factory,
            ):
                self.assertEqual(composition.main(["steward", "precheck"]), PRECHECK_BOARD_UNREACHABLE)

            factory.assert_called_once_with(mock.ANY)
            self.assertIn(
                '"result": "board-unreachable"', (state.dir / "runs.jsonl").read_text(encoding="utf-8")
            )

    def test_advance_without_pending_does_not_construct_a_client(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = AgentState("steward", state_dir=Path(tmp) / "state")
            with (
                mock.patch.object(steward_cli, "STATE", state),
                mock.patch.object(composition, "card_client", side_effect=AssertionError("client")
                ),
            ):
                self.assertEqual(composition.main(["steward", "advance"]), 1)

    def test_precheck_keeps_other_canonical_task_errors_as_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = AgentState("steward", state_dir=Path(tmp) / "state")
            with (
                mock.patch.object(steward_cli, "STATE", state),
                mock.patch.object(
                    steward_cli.signals,
                    "scan",
                    side_effect=lambda reader: reader.active_cards(),
                ),
                mock.patch.object(composition, "card_client",
                    side_effect=TaskError("backend_error", "bad board response", 1),
                ),
            ):
                self.assertEqual(composition.main(["steward", "precheck"]), 2)

    def test_precheck_maps_reader_backend_unavailable_to_deferred(self) -> None:
        class Reader:
            def steward_signal_cards(self, *, states, project):
                raise TaskError("backend_unavailable", "board unavailable", 1)

        with tempfile.TemporaryDirectory() as tmp:
            state = AgentState("steward", state_dir=Path(tmp) / "state")
            with (
                mock.patch.object(steward_cli, "STATE", state),
                mock.patch.object(
                    steward_cli.signals,
                    "scan",
                    side_effect=lambda reader: reader.active_cards(),
                ),
                mock.patch.object(composition, "card_client", return_value=object()),
                mock.patch.object(composition, "TaskReader", return_value=Reader()),
            ):
                self.assertEqual(composition.main(["steward", "precheck"]), PRECHECK_BOARD_UNREACHABLE)

    def test_precheck_keeps_reader_backend_error_as_failure(self) -> None:
        class Reader:
            def steward_signal_cards(self, *, states, project):
                raise TaskError("backend_error", "bad response", 1)

        with tempfile.TemporaryDirectory() as tmp:
            state = AgentState("steward", state_dir=Path(tmp) / "state")
            with (
                mock.patch.object(steward_cli, "STATE", state),
                mock.patch.object(
                    steward_cli.signals,
                    "scan",
                    side_effect=lambda reader: reader.active_cards(),
                ),
                mock.patch.object(composition, "card_client", return_value=object()),
                mock.patch.object(composition, "TaskReader", return_value=Reader()),
            ):
                self.assertEqual(composition.main(["steward", "precheck"]), 2)


class ModuleEntryTests(unittest.TestCase):
    """`python3 -P -m ummanu automations` is the one entry, and it always goes through the wiring."""

    def test_module_entry_injects_board_ports_for_every_board_role(self) -> None:
        # An Orca automation's precheck string names the module, not a composition root. Before the
        # entry was the root, that path reached the helpers without a port and failed as a wiring
        # error; now it classifies an absent board exactly as the systemd gate path does.
        for agent in ("steward", "retro"):
            with self.subTest(agent), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                env = {
                    "PATH": "/usr/bin:/bin",
                    "HOME": str(root / "home"),
                    "UMMANU_STATE": str(root / "state"),
                    "UMMANU_INSTANCE": str(root / "instance"),
                    "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                }
                result = subprocess.run(
                    [sys.executable, "-P", "-m", "ummanu", "automations", agent, "precheck"],
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=60,
                )
                self.assertNotIn("must be supplied by the composition root", result.stderr)
                self.assertEqual(result.returncode, PRECHECK_BOARD_UNREACHABLE, result.stderr)


class UmmanuCliEntryTests(unittest.TestCase):
    """`ummanu automations <agent> <cmd>` is the composition root behind the product CLI."""

    def _environment(self, root: Path) -> dict[str, str]:
        return {
            "PATH": "/usr/bin:/bin",
            "HOME": str(root / "home"),
            "UMMANU_STATE": str(root / "state"),
            "UMMANU_INSTANCE": str(root / "instance"),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        }

    def test_the_argv_reaches_the_composition_root_untouched(self) -> None:
        argv = ["curator", "baseline", "--project", "review:po", "--json", "--help"]
        with mock.patch.object(composition, "main", return_value=0) as composed:
            self.assertEqual(ummanu_cli.main(["automations", *argv]), 0)
        composed.assert_called_once_with(argv)

    def test_every_precheck_code_is_the_commands_exit_code(self) -> None:
        for code in (0, PRECHECK_SKIP, PRECHECK_BOARD_UNREACHABLE, PRECHECK_DEFERRED, 1, 2):
            for agent in triggered_main.AGENTS:
                with self.subTest(agent=agent, code=code):
                    with mock.patch.object(composition, "main", return_value=code) as composed:
                        self.assertEqual(ummanu_cli.main(["automations", agent, "precheck"]), code)
                    composed.assert_called_once_with([agent, "precheck"])

    def test_help_and_an_unknown_agent_answer_as_the_agents_runner_does(self) -> None:
        for argv in ([], ["--help"], ["help"]):
            with self.subTest(argv=argv):
                through_cli, direct = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(through_cli):
                    cli_code = ummanu_cli.main(["automations", *argv])
                with contextlib.redirect_stdout(direct):
                    direct_code = composition.main(argv)
                self.assertEqual(cli_code, direct_code)
                self.assertEqual(through_cli.getvalue(), direct.getvalue())
                self.assertIn("python3 -P -m ummanu automations <agent> <cmd>", direct.getvalue())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ummanu_cli.main(["automations", "pipeline", "list"]), 2)

    def test_health_is_the_cross_agent_check(self) -> None:
        from ummanu.automations.runtime import health

        with mock.patch.object(health, "check", return_value=0) as check:
            self.assertEqual(ummanu_cli.main(["automations", "health"]), 0)
        check.assert_called_once_with(triggered_main.HEALTH_COMPONENTS)

    def test_the_parser_tree_names_the_subcommand(self) -> None:
        parser = ummanu_cli.build_parser()
        subcommands = next(
            action.choices for action in parser._actions if hasattr(action, "choices") and action.choices
        )
        self.assertIn("automations", subcommands)
        args = parser.parse_args(["automations", "curator", "precheck"])
        with mock.patch.object(composition, "main", return_value=PRECHECK_SKIP) as composed:
            self.assertEqual(args.handler(args), PRECHECK_SKIP)
        composed.assert_called_once_with(["curator", "precheck"])

    def test_the_process_entry_keeps_the_exit_protocol(self) -> None:
        """A real process per agent: curator skips on no new turns, retro and steward defer."""
        expected = {
            "curator": PRECHECK_SKIP,
            "retro": PRECHECK_BOARD_UNREACHABLE,
            "steward": PRECHECK_BOARD_UNREACHABLE,
        }
        for module in (["ummanu", "automations"], ["ummanu.automations"]):
            for agent, code in expected.items():
                with self.subTest(module=module, agent=agent), tempfile.TemporaryDirectory() as tmp:
                    result = subprocess.run(
                        [sys.executable, "-P", "-m", *module, agent, "precheck"],
                        env=self._environment(Path(tmp)),
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=60,
                    )
                    self.assertEqual(result.returncode, code, result.stderr)

    def test_the_process_entry_runs_health(self) -> None:
        """No installation behind it: every component says so, and the check fails rather than crash."""
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [sys.executable, "-P", "-m", "ummanu", "automations", "health"],
                env=self._environment(Path(tmp)),
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        for component in triggered_main.HEALTH_COMPONENTS:
            self.assertIn(
                f"ERROR {component}: effective installation configuration unavailable",
                result.stdout + result.stderr,
            )


class DispatchArgumentParityTests(unittest.TestCase):
    def test_the_variant_is_the_first_non_flag_and_cleanup_only_is_still_accepted(self) -> None:
        parsed = triggered_main.parse_dispatch_arguments(["deep-sweep"])
        self.assertIs(parsed.cleanup_only, False)
        self.assertEqual(parsed.variant, "deep-sweep")

        parsed = triggered_main.parse_dispatch_arguments(["--cleanup-only"])
        self.assertIs(parsed.cleanup_only, True)
        self.assertIsNone(parsed.variant)

    def test_the_retired_finalizer_flags_are_gone(self) -> None:
        """secretary-1720: the pane finalizer trailer and its helper are deleted with the pane."""
        self.assertEqual(set(vars(triggered_main.parse_dispatch_arguments([]))), {"cleanup_only", "variant"})


if __name__ == "__main__":
    unittest.main()
