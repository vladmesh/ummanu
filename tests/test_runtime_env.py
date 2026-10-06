"""Runtime consumers and secret round-trips agree on single-line EnvironmentFile values."""

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
from typing import ClassVar
from unittest import mock

from tests.test_secret_store import EnvStoreCase
from ummanu import runtime_env, secret_store
from ummanu.runtime import role_env


class RuntimeEnvSyntaxTests(unittest.TestCase):
    # Golden values from systemd.exec(5), including interior spaces/# that shlex truncated.
    VALUES: ClassVar[tuple[tuple[str, str], ...]] = (
        ("opaque==", "opaque=="),
        ("a b", "a b"),
        ("a#b", "a#b"),
        ("a #b", "a #b"),
        ('"a b"', "a b"),
        ("'a b'", "a b"),
        (r"a\b", "ab"),
        (r"a\\b", r"a\b"),
        (r'"a\b"', r"a\b"),
        (r"'a\b'", r"a\b"),
        (r"\"a\"", '"a"'),
        (r'"\$x"', "$x"),
        ("$x", "$x"),
        ('a"b', 'a"b'),
        ("  a b  ", "a b"),
        ("a\\ ", "a "),
        ('" a "', " a "),
        ("", ""),
        ("☃", "☃"),
    )

    def test_installer_and_role_read_the_same_environment_file_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            path = instance / "runtime.env"
            for serialized, expected in self.VALUES:
                with self.subTest(serialized=serialized):
                    path.write_text(f"EXAMPLE={serialized}\n", encoding="utf-8")
                    path.chmod(0o600)
                    self.assertEqual(runtime_env.read_runtime_env(instance), {"EXAMPLE": expected})
                    self.assertEqual(role_env.load_env_file(path), {"EXAMPLE": expected})

    def test_comments_crlf_and_repeated_assignments_follow_runtime_policy(self) -> None:
        text = " # comment\r\n; comment\r\n\r\n KEY =first\r\nKEY=last # literal\r\n"
        self.assertEqual(runtime_env.parse_runtime_env(text), {"KEY": "last # literal"})

    def test_unsupported_syntax_is_refused_without_echoing_values(self) -> None:
        for text in (
            "export KEY=opaque-sentinel\n",
            "KEY='opaque-sentinel\n",
            'KEY="opaque-sentinel\n',
            "KEY=opaque-sentinel\\\nKEY2=value\n",
            'KEY="opaque-sentinel"suffix\n',
            "KEY=opaque-sentinel\x00\n",
            "KEY=opaque-sentinel\ufeff\n",
            "KEY=opaque-sentinel\rOTHER=unexpected\n",
            "1BAD=opaque-sentinel\n",
        ):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as tmp:
                instance = Path(tmp)
                path = instance / "runtime.env"
                path.write_bytes(text.encode())
                path.chmod(0o600)
                for call, error in (
                    (
                        lambda instance=instance: runtime_env.read_runtime_env(instance),
                        runtime_env.RuntimeEnvError,
                    ),
                    (lambda path=path: role_env.load_env_file(path), role_env.RoleEnvError),
                ):
                    with self.assertRaises(error) as caught:
                        call()
                    self.assertNotIn("opaque-sentinel", str(caught.exception))

    def test_private_file_validation_precedes_value_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            path = instance / "runtime.env"
            path.write_text('KEY="unclosed\n', encoding="utf-8")
            path.chmod(0o644)
            with self.assertRaisesRegex(runtime_env.RuntimeEnvError, "permissions"):
                runtime_env.read_runtime_env(instance)
            path.chmod(0o600)
            link = instance / "linked.env"
            link.symlink_to(path)
            with self.assertRaisesRegex(runtime_env.RuntimeEnvError, "regular file"):
                runtime_env.read_runtime_env(instance, str(link))

    def test_role_cli_refuses_bad_syntax_before_exec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime.env"
            path.write_text('KEY="opaque-sentinel\n', encoding="utf-8")
            errors = io.StringIO()
            with mock.patch.object(role_env.os, "execvpe") as execute, contextlib.redirect_stderr(errors):
                code = role_env.main(["exec", "--role", "pipeline", "--env-file", str(path), "--", "true"])
            self.assertEqual(code, 125)
            execute.assert_not_called()
            self.assertNotIn("opaque-sentinel", errors.getvalue())


class RuntimeEnvRoundTripTests(EnvStoreCase):
    def test_import_materialize_and_process_keep_bytes_and_runtime_meaning(self) -> None:
        payload = (
            'UMMANU_DATA_DIR="data root"\n'
            "EXAMPLE_TOKEN=opaque\\-value\\-sentinel\n"
            "EXAMPLE_HASH=a#b\n"
            "EXAMPLE_SPACE=a b\n"
        )
        self.source.write_text(payload, encoding="utf-8")
        self.do_import()
        before = self.store_state()
        secret_store.materialize_secrets(self.instance_dir)
        self.assertEqual(self.target.read_bytes(), payload.encode())
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store_state(), before)
        self.assertEqual(secret_store.read_secret(self.instance_dir, "ummanu_data_dir"), b'"data root"')
        expected = {
            "UMMANU_DATA_DIR": "data root",
            "EXAMPLE_TOKEN": "opaque-value-sentinel",
            "EXAMPLE_HASH": "a#b",
            "EXAMPLE_SPACE": "a b",
        }
        self.assertEqual(runtime_env.read_runtime_env(self.instance_dir, str(self.target)), expected)
        self.assertEqual(role_env.load_env_file(self.target), expected)
        env = role_env.runtime_env(
            "pipeline",
            base_env={"PATH": os.defpath, "EXAMPLE_TOKEN": "ambient", "UMMANU_DATA_DIR": "ambient"},
            env_file=self.target,
        )
        self.assertEqual(env["UMMANU_DATA_DIR"], "data root")
        self.assertNotIn("EXAMPLE_TOKEN", env)
        child = subprocess.run(
            [sys.executable, "-c", "import json,os; print(json.dumps(os.environ['UMMANU_DATA_DIR']))"],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(json.loads(child.stdout), "data root")

    def test_unsupported_import_does_not_change_store_or_previous_materialization(self) -> None:
        self.do_import()
        secret_store.materialize_secrets(self.instance_dir)
        before_store, before_env = self.store_state(), self.target.read_bytes()
        for value in ('"unclosed-sentinel', "unclosed-sentinel\\", "'x'concatenated-sentinel"):
            with self.subTest(value=value):
                self.source.write_text(f"EXAMPLE_URL={value}\n", encoding="utf-8")
                with self.assertRaises(secret_store.SecretStoreValidationError) as caught:
                    self.do_import()
                self.assertNotIn("sentinel", str(caught.exception))
                self.assertEqual(self.store_state(), before_store)
                self.assertEqual(self.target.read_bytes(), before_env)

    def test_unrenderable_stored_value_preserves_previous_env_file(self) -> None:
        self.do_import()
        secret_store.materialize_secrets(self.instance_dir)
        before = self.target.read_bytes()
        secret_store.set_secret(
            self.instance_dir,
            secret_id="example_url",
            value=b'"unclosed-sentinel',
            scope="installation",
            purpose="synthetic configuration",
            environment="EXAMPLE_URL",
            materialize={"target": "runtime-env", "order": 0},
            actor="tester",
        )
        with self.assertRaises(secret_store.SecretStoreValidationError) as caught:
            secret_store.materialize_secrets(self.instance_dir)
        self.assertNotIn("unclosed-sentinel", str(caught.exception))
        self.assertEqual(self.target.read_bytes(), before)
        self.assertEqual(sorted(self.target.parent.iterdir()), [self.target])
