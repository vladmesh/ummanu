"""Expected input errors answer one JSON refusal and status 2, never a traceback (ummanu-235).

Each case drives `cli.main` in process: an escaping exception would fail the test before any
assertion, which is the traceback a person at the terminal used to see.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ummanu.cli import main
from ummanu.runtime.paths import instance_file


class CliErrorTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # No ambient installation: a worker's own UMMANU_DATA_DIR would otherwise answer for the
        # instance under test, and UMMANU_INSTANCE would name the live one.
        environment = mock.patch.dict(
            os.environ, {"HOME": str(self.root), "UMMANU_INSTANCE": "", "UMMANU_DATA_DIR": ""}
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.instance = self.root / "instance"
        self.instance.mkdir()
        # A config with no data_dir: the installation names nowhere for its data plane.
        (self.instance / "instance.yaml").write_text("name: test\n", encoding="utf-8")

    def invoke(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def assert_refusal(self, stream: str, code: str, message_part: str) -> None:
        document = json.loads(stream)
        self.assertEqual(set(document), {"error"})
        self.assertEqual(document["error"]["code"], code)
        self.assertIn(message_part, document["error"]["message"])

    def test_pause_with_an_unreadable_reason_file_is_a_usage_refusal(self) -> None:
        missing = self.root / "nonexistent"
        status, out, err = self.invoke(
            ["pause", "drain", "--reason-file", str(missing), "--instance", str(self.instance)]
        )

        self.assertEqual(status, 2)
        # The same stream and envelope as the command's own "pause requires --reason" refusal.
        self.assert_refusal(out, "usage", f"cannot read --reason-file: [Errno 2] No such file or directory: '{missing}'")
        self.assertNotIn("Traceback", err)
        self.assertEqual(err, "")

    def test_sprint_show_without_a_usable_data_dir_is_a_usage_refusal(self) -> None:
        for named in (self.instance / "instance.yaml", self.instance):
            with self.subTest(instance=named.name):
                status, out, err = self.invoke(["sprint", "show", "--ref", "sprint:1", "--instance", str(named)])

                self.assertEqual(status, 2)
                self.assertNotIn("Traceback", err)
                self.assertEqual(err, "")
                self.assert_refusal(
                    out,
                    "usage",
                    f"cannot resolve data dir from {self.instance / 'instance.yaml'}: ",
                )
                self.assertIn("; pass --data-dir", json.loads(out)["error"]["message"])

    def test_bare_command_groups_answer_one_usage_refusal(self) -> None:
        for group in ("data", "memory", "config"):
            with self.subTest(group=group):
                status, out, err = self.invoke([group])

                self.assertEqual(status, 2)
                self.assertEqual(err, "")
                self.assertEqual(
                    out, '{"error": {"code": "usage", "message": "' + group + ' subcommand required"}}\n'
                )

    def test_existing_bare_group_refusals_keep_their_bytes(self) -> None:
        for argv, message in (
            (["task"], "task subcommand required"),
            (["sprint"], "sprint subcommand required"),
            (["product"], "product subcommand required"),
            (["product", "transaction"], "product transaction subcommand required"),
            (["issue"], "issue subcommand required"),
            (["dispatcher"], "dispatcher subcommand required"),
            (["web-read"], "web-read subcommand required"),
            (["web-run"], "web-run subcommand required"),
        ):
            with self.subTest(argv=argv):
                self.assertEqual(
                    self.invoke(argv),
                    (2, '{"error": {"code": "usage", "message": "' + message + '"}}\n', ""),
                )

    def test_parser_errors_keep_their_envelope(self) -> None:
        status, out, err = self.invoke(["task", "show"])

        self.assertEqual(status, 2)
        self.assertEqual(out, "")
        self.assertEqual(
            err,
            '{"error": {"code": "usage", "message": "the following arguments are required: --ref"}}\n',
        )


class InstanceFileTests(unittest.TestCase):
    def test_a_directory_names_its_config_and_anything_else_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            config = root / "instance.yaml"
            config.write_text("name: test\n", encoding="utf-8")
            other = root / "other.yaml"
            other.write_text("name: test\n", encoding="utf-8")

            self.assertEqual(instance_file(root), config)
            self.assertEqual(instance_file(config), config)
            # An explicit file keeps its own name; only an existing directory gains instance.yaml.
            self.assertEqual(instance_file(other), other)
            self.assertEqual(instance_file(root / "missing"), root / "missing")
            # `~` is the caller's to expand.
            self.assertEqual(instance_file(Path("~/no-such-instance")), Path("~/no-such-instance"))


if __name__ == "__main__":
    unittest.main()
