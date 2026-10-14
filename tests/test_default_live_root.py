"""The default live root `~/ummanu-data/instance`, refused when absent, and doctor's old-shape findings.

ummanu-39 (sprint:1476, DoD 1). One function spells the default and every former spelling goes through
it; a command that would fall back to a default that does not exist refuses, naming it, and creates
nothing; doctor reds a live root that is a Git work tree and any unit or env naming the old path.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ummanu import cli
from ummanu.infra import live_root_findings as lrf
from ummanu.infra.old_name_guard import INSTANCE_PROJECT
from ummanu.runtime import paths

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_INSTANCE = REPO_ROOT / "examples" / "instance"


def run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class HomeCase(unittest.TestCase):
    """A fresh home with nothing in it, and no installation named by the environment."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.home.mkdir()
        self.enterContext(mock.patch.dict(os.environ, {"HOME": str(self.home)}))
        for name in (*lrf.BINDING_ENV, "UMMANU_DATA_DIR"):
            os.environ.pop(name, None)
        self.default = self.home / "ummanu-data" / "instance"


class DefaultPathTests(HomeCase):
    def test_the_default_live_root_is_a_directory_below_the_data_plane(self):
        self.assertEqual(paths.default_instance_path(), self.default)

    def test_every_former_spelling_goes_through_the_one_function(self):
        from ummanu import host, onboarding, task_commands
        from ummanu.automations.agents.curator import memory_protocol
        from ummanu.automations.runtime import production_telemetry
        from ummanu.runtime import redact, role_env

        # Resolved per call, under this home.
        self.assertEqual(host.default_systemd_layout().instance_path, self.default)
        self.default.mkdir(parents=True)
        self.assertEqual(task_commands._instance(argparse.Namespace(instance=None)), str(self.default))
        self.assertEqual(memory_protocol.default_ummanu_instance(), self.default)

        # Resolved at import, under the home of the process that imported them: the shape is the pin.
        tail = (paths.DATA_DIRNAME, paths.INSTANCE_DIRNAME)
        for name, value in (
            ("onboarding.DEFAULT_INSTANCE", Path(onboarding.DEFAULT_INSTANCE)),
            ("role_env.RUNTIME_ENV_DEFAULT", Path(role_env.RUNTIME_ENV_DEFAULT).parent),
            ("production_telemetry.DEFAULT_INSTANCE", production_telemetry.DEFAULT_INSTANCE),
            ("memory_protocol.DEFAULT_UMMANU_INSTANCE", memory_protocol.DEFAULT_UMMANU_INSTANCE),
            ("redact.DEFAULT_ENV_FILES", redact.DEFAULT_ENV_FILES[-1].parent),
        ):
            with self.subTest(name):
                self.assertEqual(value.parts[-2:], tail)
        self.assertEqual(redact.DEFAULT_ENV_FILES[-1].name, "runtime.env")

    def test_the_memory_service_canon_defaults_under_the_live_root(self):
        source = (REPO_ROOT / "src" / "ummanu" / "memory_service.py").read_text(encoding="utf-8")
        self.assertIn('DEFAULT_CANON = default_instance_path() / "state" / "memory" / "facts"', source)
        if importlib.util.find_spec("numpy") is None or importlib.util.find_spec("fastembed") is None:
            return  # the memory extra is not installed in this suite; the source pin above stands
        from ummanu import memory_service

        self.assertEqual(memory_service.DEFAULT_CANON.parent.parent.parent.name, paths.INSTANCE_DIRNAME)


class MissingDefaultRefusedTests(HomeCase):
    """No `--instance`, no `UMMANU_INSTANCE`, no directory at the default: refuse, create nothing."""

    COMMANDS = (
        ("task", "list"),
        ("upgrade", "--dry-run", "--no-pull"),
        ("doctor", "--offline"),
        ("status", "--offline"),
        ("product", "list"),
        ("check", "show", "--module", "tests.broad"),
        ("role-skills", "audit"),
    )

    def test_each_command_exits_non_zero_naming_the_default_and_the_two_ways_out(self):
        for argv in self.COMMANDS:
            with self.subTest(argv=argv):
                code, out, err = run(list(argv))
                self.assertNotEqual(code, 0)
                self.assertIn(str(self.default), err)
                self.assertIn("--instance", err)
                self.assertIn("UMMANU_INSTANCE", err)
                self.assertEqual(out, "")
                self.assertEqual(list(self.home.iterdir()), [], "a refused command created something")

    def test_the_resolver_refuses_the_absent_default_and_takes_an_explicit_or_bound_one(self):
        with self.assertRaises(paths.MissingDefaultInstance) as raised:
            paths.resolve_instance_path(None, {})
        self.assertEqual(raised.exception.path, self.default)
        self.assertFalse(self.default.exists())
        self.assertEqual(paths.resolve_instance_path("~/x", {}), self.home / "x")
        self.assertEqual(paths.resolve_instance_path(None, {"UMMANU_INSTANCE": "/srv/i"}), Path("/srv/i"))
        self.default.mkdir(parents=True)
        self.assertEqual(paths.resolve_instance_path(None, {}), self.default)


class BoundInstanceUnchangedTests(HomeCase):
    """With `UMMANU_INSTANCE` set, a command runs exactly as with `--instance`."""

    def instance(self) -> Path:
        target = self.home / "elsewhere"
        shutil.copytree(EXAMPLE_INSTANCE, target)
        return target

    def test_doctor_reads_the_bound_instance_as_if_it_were_named(self):
        target = self.instance()
        # Under the same binding: doctor's recovery inventory reads a bound environment's overrides.
        with mock.patch.dict(os.environ, {"UMMANU_INSTANCE": str(target)}):
            named = run(["doctor", "--dry-run", "--offline", "--instance", str(target)])
            bound = run(["doctor", "--dry-run", "--offline"])
        self.assertEqual(bound, named)
        self.assertIn(f"instance: {target / 'instance.yaml'}", bound[1])

    def test_upgrade_and_task_receive_the_bound_instance(self):
        from ummanu import task_commands, upgrade

        target = self.instance()
        seen: list[str] = []

        def record(args):
            seen.append(args.instance)
            return 0

        with (
            mock.patch.dict(os.environ, {"UMMANU_INSTANCE": str(target)}),
            mock.patch.object(upgrade, "run_upgrade", side_effect=record),
        ):
            self.assertEqual(run(["upgrade", "--dry-run", "--no-pull"])[0], 0)
            self.assertEqual(task_commands._instance(argparse.Namespace(instance=None)), str(target))
        self.assertEqual(seen, [str(target)])
        self.assertFalse((self.home / "ummanu-data").exists())

    def test_an_existing_default_is_used_when_nothing_names_one(self):
        shutil.copytree(EXAMPLE_INSTANCE, self.default)
        code, out, err = run(["doctor", "--dry-run", "--offline"])
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"instance: {self.default / 'instance.yaml'}", out)


def unit_text(live_root: str) -> str:
    return (
        "[Service]\n"
        f"EnvironmentFile=-{live_root}/runtime.env\n"
        f"Environment=UMMANU_INSTANCE={live_root}\n"
        f"ExecStart=/usr/bin/true --instance {live_root}\n"
    )


class LiveRootFindingTests(unittest.TestCase):
    """`live_root.git_work_tree` and `live_root.old_path`, from the module and through doctor."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.live_root = self.root / "instance"
        shutil.copytree(EXAMPLE_INSTANCE, self.live_root)
        data = self.root / "data"
        config = self.live_root / "instance.yaml"
        config.write_text(
            config.read_text(encoding="utf-8").replace("/var/lib/ummanu-data", str(data)), encoding="utf-8"
        )
        self.fixture = self.root / "host"
        self.unit_files = self.fixture / "unit-files"
        self.unit_files.mkdir(parents=True)
        self.old = f"/home/operator/{INSTANCE_PROJECT}"
        self.new = "/home/operator/ummanu-data/instance"

    def doctor_codes(self, *extra: str) -> tuple[list[dict], str]:
        _, out, err = run(["doctor", "--dry-run", "--json", "--instance", str(self.live_root), *extra])
        findings = json.loads(out)["findings"]
        return [finding for finding in findings if str(finding["code"]).startswith("live_root.")], out + err

    def test_a_live_root_with_git_is_red(self):
        (self.live_root / ".git").mkdir()
        findings, output = self.doctor_codes("--offline")
        self.assertEqual([finding["code"] for finding in findings], [lrf.GIT_WORK_TREE], output)
        self.assertEqual(findings[0]["severity"], "red")
        self.assertIn(str(self.live_root), findings[0]["message"])

    def test_a_linked_worktree_marker_file_counts_too(self):
        (self.live_root / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
        self.assertEqual(lrf.git_work_tree_finding(self.live_root)["code"], lrf.GIT_WORK_TREE)

    def test_a_unit_naming_the_old_path_is_red_and_named(self):
        (self.unit_files / "ummanu-po.service").write_text(unit_text(self.old), encoding="utf-8")
        (self.unit_files / "ummanu-web.service").write_text(unit_text(self.new), encoding="utf-8")
        # Not ours: another product's unit is not this installation's.
        (self.unit_files / "other.service").write_text(unit_text(self.old), encoding="utf-8")

        findings, output = self.doctor_codes("--host-fixture", str(self.fixture))

        self.assertEqual([finding["code"] for finding in findings], [lrf.OLD_PATH], output)
        self.assertEqual(findings[0]["severity"], "red")
        self.assertEqual(findings[0]["source"], "unit ummanu-po.service")
        self.assertIn("ummanu-po.service", findings[0]["message"])
        self.assertIn(self.old, findings[0]["message"])

    def test_text_doctor_prints_the_finding_and_exits_red(self):
        (self.live_root / ".git").mkdir()
        code, out, _ = run(["doctor", "--dry-run", "--offline", "--instance", str(self.live_root)])
        self.assertEqual(code, 1, out)
        self.assertIn("live_root.git_work_tree: the live root", out)
        self.assertIn("status: findings", out)

    def test_a_clean_plain_live_root_with_clean_units_has_neither(self):
        (self.unit_files / "ummanu-po.service").write_text(unit_text(self.new), encoding="utf-8")
        # The instance repository's remote keeps its name; a remote is not the live root's path.
        (self.live_root / "runtime.env").write_text(
            f"INSTANCE_REMOTE=git@github.com:vladmesh/{INSTANCE_PROJECT}.git\n# was {self.old}\n",
            encoding="utf-8",
        )
        findings, output = self.doctor_codes("--host-fixture", str(self.fixture))
        self.assertEqual(findings, [], output)
        offline, output = self.doctor_codes("--offline")
        self.assertEqual(offline, [], output)

    def test_the_runtime_env_and_the_bound_role_env_are_read(self):
        (self.live_root / "runtime.env").write_text(
            f"UMMANU_RUNTIME_ENV_FILE={self.old}/runtime.env\n", encoding="utf-8"
        )
        bound = {
            "UMMANU_INSTANCE": str(self.live_root),
            "UMMANU_RUNTIME_ENV_FILE": f"~/{INSTANCE_PROJECT}/runtime.env",
        }

        findings = lrf.live_root_findings(self.live_root, environ=bound)

        self.assertEqual(
            [finding["source"] for finding in findings],
            [f"runtime env {self.live_root / 'runtime.env'}", "role env UMMANU_RUNTIME_ENV_FILE"],
        )
        # A process bound to another installation says nothing about this one.
        elsewhere = {"UMMANU_INSTANCE": self.old, "UMMANU_RUNTIME_ENV_FILE": f"{self.old}/runtime.env"}
        (self.live_root / "runtime.env").unlink()
        self.assertEqual(lrf.live_root_findings(self.live_root, environ=elsewhere), [])

    def test_the_old_spelling_is_matched_as_a_path_only(self):
        pattern = lrf.old_path_pattern(Path("/srv/acct"))
        for text in (
            self.old,
            f"~/{INSTANCE_PROJECT}",
            f"%h/{INSTANCE_PROJECT}/x",
            f"/srv/acct/{INSTANCE_PROJECT}",
        ):
            with self.subTest(text=text):
                self.assertIsNotNone(pattern.search(text))
        for text in (
            f"git@github.com:vladmesh/{INSTANCE_PROJECT}.git",
            f"https://github.com/vladmesh/{INSTANCE_PROJECT}",
            f"{INSTANCE_PROJECT}-maintenance",
            f"/home/dev/{INSTANCE_PROJECT}-maintenance.service",
            self.new,
        ):
            with self.subTest(text=text):
                self.assertIsNone(pattern.search(text))


if __name__ == "__main__":
    unittest.main()
