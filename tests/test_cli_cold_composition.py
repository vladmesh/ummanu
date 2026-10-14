"""Cold-process import and parser contracts for ARCH-12/INEF-18."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def stable(value):
    if callable(value):
        return f"{value.__module__}.{value.__qualname__}"
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): stable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [stable(item) for item in value]
    return value


def schema(parser):
    actions = []
    for action in parser._actions:
        row = {
            key: stable(getattr(action, key))
            for key in (
                "option_strings",
                "dest",
                "nargs",
                "const",
                "default",
                "type",
                "choices",
                "required",
                "help",
                "metavar",
            )
        }
        row["action"] = type(action).__name__
        if parser.prog == "ummanu data snapshot" and action.dest == "state_dir":
            from ummanu.cli import PIPELINE_STATE_DIR

            # This legacy default binds at import, before the test's construction-time HOME.
            assert action.default == str(PIPELINE_STATE_DIR)
        if isinstance(action, argparse._SubParsersAction):
            row["choices"] = {name: schema(child) for name, child in action.choices.items()}
        actions.append(row)
    return {
        "prog": parser.prog,
        "allow_abbrev": parser.allow_abbrev,
        "defaults": stable(parser._defaults),
        "actions": actions,
        "mutex": [
            {
                "required": group.required,
                "actions": [parser._actions.index(action) for action in group._group_actions],
            }
            for group in parser._mutually_exclusive_groups
        ],
        "help": parser.format_help(),
    }


def outcome(argv):
    from ummanu.cli import main

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return {"argv": argv, "code": code, "stdout": out.getvalue(), "stderr": err.getvalue()}


class CliCompositionTests(unittest.TestCase):
    def test_selected_parser_and_dispatch_seams(self):
        from ummanu import cli

        parser = cli.build_parser()
        roots = next(
            action.choices for action in parser._actions if isinstance(action, argparse._SubParsersAction)
        )
        # The full inspection API continues to expose the real callable objects.
        from ummanu.task_commands import run_task_show

        task = next(
            action.choices
            for action in roots["task"]._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        self.assertIs(task["show"].get_default("handler"), run_task_show)
        self.assertIs(roots["doctor"].get_default("handler"), cli.run_doctor)
        for name in roots:
            selected = cli.build_parser([name])
            choices = next(
                action.choices
                for action in selected._actions
                if isinstance(action, argparse._SubParsersAction)
            )
            self.assertEqual(list(choices), list(roots))
            self.assertEqual(schema(choices[name]), schema(roots[name]), name)
        for argv in (
            ["--unknown", "task", "show"],
            ["--unknown", "task", "--help"],
            ["--help", "task", "show"],
            ["-h", "task"],
            ["--", "task"],
            ["unknown", "task", "--help"],
            ["task", "--hel"],
            ["config", "check", "--help"],
            ["memory", "reindex", "--help"],
        ):
            with self.subTest(argv=argv):
                actual = outcome(argv)
                with mock.patch.object(cli, "build_parser", return_value=parser):
                    self.assertEqual(actual, outcome(argv))
        with (
            mock.patch.object(cli, "build_parser", return_value=parser) as build,
            mock.patch("ummanu.runtime.test_guard.check_refusal", return_value=125),
            mock.patch.object(cli, "run_automations", return_value=101) as automations,
        ):
            self.assertEqual(cli.main(["check", "--help"]), 125)
            build.assert_not_called()
            argv = ["pipeline", "--help", "--unknown", "value"]
            self.assertEqual(cli.main(["automations", *argv]), 101)
            automations.assert_called_once_with(argv)
            build.assert_not_called()
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            default = home / "ummanu-data/instance"
            with (
                mock.patch.dict(os.environ, {"HOME": temporary, "UMMANU_INSTANCE": ""}),
                mock.patch.object(cli, "run_config_check", return_value=17) as handler,
            ):
                self.assertEqual(outcome(["config", "check"])["code"], 2)
                handler.assert_not_called()
                self.assertFalse(default.exists())
                for source, flags in (
                    ("explicit", ["--instance", str(home / "explicit")]),
                    ("environment", []),
                    ("default", []),
                ):
                    if source == "default":
                        default.mkdir(parents=True)
                    os.environ["UMMANU_INSTANCE"] = (
                        str(home / "environment") if source == "environment" else ""
                    )
                    self.assertEqual(outcome(["config", "check", *flags])["code"], 17)
                    expected = home / source if source != "default" else default
                    self.assertEqual(Path(handler.call_args.args[0].instance), expected)

    def test_doctor_type_contract(self):
        from typing import get_type_hints

        from ummanu import cli
        from ummanu.head_health import PROBE_BROKEN, PROBE_TIMED_OUT, HeadReadiness
        from ummanu.host import CollectResult, KindDiff
        from ummanu.infra.doctor_provider import PROVIDER_RED_STATES

        self.assertEqual(
            [field.name for field in fields(cli.DoctorInspection)],
            [
                "findings",
                "unavailable",
                "restore",
                "dispatcher",
                "checkpoint",
                "secret_store",
                "resource_probes",
                "recovery",
                "expected",
                "collected",
                "diffs",
                "board_schema",
            ],
        )
        # Future annotations retain their domain types without importing those types on CLI load.
        hints = get_type_hints(
            cli.DoctorInspection,
            localns={
                "HeadReadiness": HeadReadiness,
                "CollectResult": CollectResult,
                "KindDiff": KindDiff,
            },
        )
        self.assertEqual(hints["resource_probes"], list[HeadReadiness])
        self.assertEqual(hints["collected"], CollectResult | None)
        self.assertEqual(hints["diffs"], dict[str, KindDiff] | None)
        self.assertIn(PROBE_BROKEN, PROVIDER_RED_STATES)
        self.assertIn(PROBE_TIMED_OUT, PROVIDER_RED_STATES)
        self.assertEqual(
            (cli.MEMORY_EXIT_VALIDATION, cli.MEMORY_EXIT_PERMISSION, cli.MEMORY_EXIT_LOCKED), (2, 3, 4)
        )

    def test_current_parser_errors_and_help(self):
        from ummanu import cli

        parser = cli.build_parser()
        roots = next(
            action.choices for action in parser._actions if isinstance(action, argparse._SubParsersAction)
        )
        self.assertFalse(parser.allow_abbrev)
        root_help = parser.format_help()
        self.assertIn("{" + ",".join(roots) + "}", root_help)
        for argv, code in (([], 2), (["--help"], 0), (["-h"], 0)):
            result = outcome(argv)
            self.assertEqual(result["code"], code)
            self.assertEqual(result["stdout"], root_help)
            self.assertEqual(result["stderr"], "")
        for argv in (["unknown"], ["--unknown"], ["--hel"], ["task", "--hel"]):
            result = outcome(argv)
            self.assertEqual(result["code"], 2)
            self.assertEqual(result["stdout"], "")
            self.assertEqual(json.loads(result["stderr"])["error"]["code"], "usage")
        # Compare against the supported current full parser, including deliberately different
        # bare-family protocols. Future commands/help are taken from that parser, not frozen.
        cases = [[name, "--help"] for name in roots if name != "automations"]
        cases += [[name] for name in ("data", "memory", "knowledge", "config", "task", "project")]
        cases += [["task", "show"], ["doctor", "--offl"], ["task", "show", "--ref", "x", "--instance"]]
        for argv in cases:
            with self.subTest(argv=argv):
                actual = outcome(argv)
                with mock.patch.object(cli, "build_parser", return_value=parser):
                    self.assertEqual(actual, outcome(argv))

    def test_cold_import(self):
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
        command = [sys.executable, "-X", "importtime", "-c", "import ummanu.cli"]
        result = subprocess.run(
            command, env=env, cwd=ROOT, capture_output=True, text=True, timeout=4, check=False
        )
        names = sorted(
            {
                match.group(1)
                for line in result.stderr.splitlines()
                if (match := re.fullmatch(r"import time:\s+\d+\s+\|\s+\d+\s+\|\s+(ummanu\..+)", line))
            }
        )
        provenance_code = """import sys
preloaded = sorted(n for n in sys.modules if n.startswith("ummanu."))
import json
import ummanu.cli
print(json.dumps({"preloaded": preloaded, "cli": ummanu.cli.__file__}))
"""
        provenance = subprocess.run(
            [sys.executable, "-c", provenance_code],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
        self.assertEqual(provenance.returncode, 0, provenance.stderr)
        startup = json.loads(provenance.stdout)
        self.assertEqual(Path(startup["cli"]), ROOT / "src/ummanu/cli.py")
        self.assertEqual(startup["preloaded"], [])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ummanu.cli", names)
        self.assertLessEqual(len(names), 100)
        for name in (
            "ummanu.backup",
            "ummanu.checkpoint",
            "ummanu.host",
            "ummanu.head_health",
            "ummanu.po.service",
            "ummanu.web.server",
            "ummanu.installation.commands",
        ):
            self.assertNotIn(name, names)

    def test_selected_startup(self):
        for argv in (["--help"], ["task", "--help"], ["config", "check", "--help"], ["memory", "--help"]):
            with self.subTest(argv=argv):
                code = "import json,sys; from ummanu.cli import main; r=main(sys.argv[1:]); print(json.dumps(sorted(n for n in sys.modules if n.startswith('ummanu.'))),file=sys.stderr); sys.exit(r)"
                child = subprocess.run(
                    [sys.executable, "-c", code, *argv],
                    cwd=ROOT,
                    env=dict(os.environ, PYTHONPATH=str(ROOT / "src")),
                    text=True,
                    capture_output=True,
                    timeout=4,
                    check=False,
                )
                self.assertEqual(child.returncode, 0, child.stderr)
                names = json.loads(child.stderr)
                for name in (
                    "ummanu.backup",
                    "ummanu.checkpoint",
                    "ummanu.host",
                    "ummanu.head_health",
                    "ummanu.po.service",
                    "ummanu.web.server",
                    "ummanu.installation.commands",
                ):
                    self.assertNotIn(name, names)
