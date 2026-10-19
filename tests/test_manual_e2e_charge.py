"""Manual e2e identities and CLI admission without a backend."""

from __future__ import annotations

import contextlib
import io
import unittest

from ummanu.board.e2e_budget import manual_run_id, stand_intent_id
from ummanu.cli import build_parser
from ummanu.sprints import SprintWriter
from ummanu.tasks import TaskError


class ManualChargeInputTests(unittest.TestCase):
    def test_stand_identity_accepts_bounded_tokens_including_numeric_names(self) -> None:
        for value in ("memory-restore-r1", "123", "A.b_c-9", "a" * 128):
            with self.subTest(value=value):
                self.assertEqual(stand_intent_id(value), value)

    def test_stand_identity_rejects_empty_whitespace_urls_and_unbounded_tokens(self) -> None:
        for value in ("", " ", " intent", "intent ", "a\nb", "a\n", "a/b", "a:b", "-a", "é", "a" * 129):
            with self.subTest(value=value), self.assertRaises(TaskError) as raised:
                stand_intent_id(value)
            self.assertEqual(raised.exception.code, "validation")

    def test_github_identity_keeps_numeric_and_url_forms(self) -> None:
        for value in ("123", " https://github.com/owner/repo/actions/runs/123 ",
                      "https://github.com/owner/repo/actions/runs/123/attempts/2"):
            with self.subTest(value=value):
                self.assertEqual(manual_run_id(value), "123")
        for value in ("", "memory-restore-r1", "https://example.com/actions/runs/123"):
            with self.subTest(value=value), self.assertRaises(TaskError):
                manual_run_id(value)

    def test_cli_requires_exactly_one_explicit_identity_type(self) -> None:
        base = ["sprint", "e2e-charge", "--ref", "sprint:test", "--role", "po", "--card", "ummanu-12"]
        parser = build_parser(base)
        for flag, value in (("--stand-intent", "memory-restore-r1"), ("--run", "123")):
            with self.subTest(flag=flag):
                parsed = parser.parse_args([*base, flag, value])
                self.assertEqual((parsed.run, parsed.stand_intent),
                                 (value, None) if flag == "--run" else (None, value))
        for flags in ([], ["--run", "123", "--stand-intent", "123"]):
            with self.subTest(flags=flags), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                parser.parse_args([*base, *flags])
            self.assertEqual(raised.exception.code, 2)

    def test_writer_rejects_missing_ambiguous_or_malformed_identity_before_backend_access(self) -> None:
        writer = object.__new__(SprintWriter)
        for fields in ({}, {"run": "123", "stand_intent": "123"}, {"run": ""}, {"stand_intent": ""},
                       {"stand_intent": "a/b"}, {"stand_intent": "a" * 129}):
            with self.subTest(fields=fields), self.assertRaises(TaskError) as raised:
                writer.charge_manual_e2e(role="po", actor="po", reference="sprint:test", card="ummanu-12", **fields)
            self.assertEqual(raised.exception.code, "validation")

    def test_writer_preserves_role_and_masquerade_guards_before_backend_access(self) -> None:
        writer = object.__new__(SprintWriter)
        for role, actor, code in (("observer", "observer", "role_forbidden"), ("worker", "worker", "role_forbidden"),
                                  ("po", "observer", "role_masquerade")):
            with self.subTest(role=role, actor=actor), self.assertRaises(TaskError) as raised:
                writer.charge_manual_e2e(role=role, actor=actor, reference="sprint:test", card="ummanu-12",
                                         stand_intent="memory-restore-r1")
            self.assertEqual(raised.exception.code, code)
