"""Runtime consumers agree on single-line EnvironmentFile values."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

from ummanu import runtime_env
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
