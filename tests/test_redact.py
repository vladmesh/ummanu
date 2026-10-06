"""Board-comment scrub contract (`ummanu.runtime.redact.scrub_secrets`).

It used to live in the pipeline dispatcher's worker.py with no coverage at all; the dispatcher is
gone (secretary-1135) and its two surviving callers — ops.add_comment for steward bodies and the
steward's own precheck error path — now read it from the runtime. These pin what has to keep
being masked and, just as important, what must not be: a git sha in a CI-failure comment.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ummanu.runtime.redact import REDACTED, redact, scrub_secrets
from ummanu.runtime.role_env import load_env_file


class ScrubSecretsTests(unittest.TestCase):
    def test_empty_text_passes_through(self):
        self.assertEqual(scrub_secrets(""), "")

    def test_a_secret_looking_assignment_loses_its_value(self):
        out = scrub_secrets("export GH_TOKEN=ghs_notarealvalue")

        self.assertEqual(out, f"export GH_TOKEN={REDACTED}")

    def test_a_known_key_shape_is_masked_by_the_pattern_layer(self):
        out = scrub_secrets("key sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWX rest")

        self.assertNotIn("sk-ant-api03", out)
        self.assertIn("rest", out)

    def test_a_long_token_shaped_blob_is_masked(self):
        out = scrub_secrets("payload " + "A" * 44)

        self.assertEqual(out, f"payload {REDACTED}:blob")

    def test_a_git_sha_survives(self):
        """A commit reference in a CI-failure comment is the useful part of it, not a leak."""
        sha = "4baca94f8f7605cd18cd9495b838092ba41aaaa1"

        self.assertEqual(scrub_secrets(f"failed at {sha}"), f"failed at {sha}")
        self.assertEqual(scrub_secrets("failed at 4baca94"), "failed at 4baca94")

    def test_a_filesystem_path_survives(self):
        path = "/home/dev/orca/workspaces/ummanu/ummanu-1135-drop-pipeline/tests"

        self.assertEqual(scrub_secrets(f"cwd {path}"), f"cwd {path}")

    def test_plain_runtime_url_is_not_an_exact_value_secret(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir) / "runtime.env"
            url = "https://board.example.invalid/rpc"
            runtime.write_text(
                f"EXAMPLE_URL={url}\nEXAMPLE_API_TOKEN=opaque-token-value\n",
                encoding="utf-8",
            )

            self.assertEqual(redact(f"board is {url}", env_files=[runtime]), f"board is {url}")

    def test_named_runtime_secret_is_an_exact_value_secret(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir) / "runtime.env"
            runtime.write_text("EXAMPLE_API_TOKEN=opaque-token-value\n", encoding="utf-8")

            self.assertEqual(
                redact("token opaque-token-value", env_files=[runtime]),
                f"token {REDACTED}:env-value",
            )

    def test_escaped_runtime_secrets_are_redacted_in_both_serialized_and_decoded_forms(self):
        cases = (
            (r"opaque\-credential\-sentinel", "opaque-credential-sentinel"),
            (r'"opaque\$credential\"sentinel"', 'opaque$credential"sentinel'),
            (r"opaque\ credential\ sentinel", "opaque credential sentinel"),
            (r"'opaque\credential\sentinel'", r"opaque\credential\sentinel"),
            ("opaque\\-credential\\-sentinel\\ ", "opaque-credential-sentinel "),
            ("opaque\\-credential\\-sentinel\\\t", "opaque-credential-sentinel\t"),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir) / "runtime.env"
            for serialized, expected in cases:
                with self.subTest(value=serialized):
                    runtime.write_text(f"API_TOKEN={serialized}\n", encoding="utf-8")
                    actual = load_env_file(runtime)["API_TOKEN"]
                    self.assertEqual(actual, expected)
                    output = redact(f"runtime: {actual}\nfile: {serialized}", env_files=[runtime])
                    self.assertNotIn(expected, output)
                    self.assertNotIn(serialized, output)
                    self.assertEqual(output.count(REDACTED), 2)

    def test_crlf_and_non_lf_unicode_separators_preserve_the_whole_credential(self):
        serialized = "opaque\\-credential\u2028sentinel"
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir) / "runtime.env"
            runtime.write_bytes(f"API_TOKEN={serialized}\r\n".encode())
            actual = load_env_file(runtime)["API_TOKEN"]
            self.assertEqual(actual, "opaque-credential\u2028sentinel")
            output = redact(f"runtime: {actual}\nfile: {serialized}", env_files=[runtime])
        self.assertEqual(output, f"runtime: {REDACTED}:env-value\nfile: {REDACTED}:env-value")

    def test_malformed_env_line_does_not_discard_other_secret_matches(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir) / "runtime.env"
            runtime.write_text(
                'BAD_TOKEN="unfinished-credential-value\nGOOD_TOKEN=second\\-credential\\-value\n',
                encoding="utf-8",
            )
            output = redact("unfinished-credential-value second-credential-value", env_files=[runtime])
        self.assertEqual(output, f"{REDACTED}:env-value {REDACTED}:env-value")

    def test_pat_and_identity_runtime_names_are_exact_value_secrets(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir) / "runtime.env"
            runtime.write_text(
                "GITHUB_PAT=opaque-pat-value\nORCA_IDENTITY=opaque-identity-value\n",
                encoding="utf-8",
            )

            output = redact("pat opaque-pat-value identity opaque-identity-value", env_files=[runtime])

            self.assertNotIn("opaque-pat-value", output)
            self.assertNotIn("opaque-identity-value", output)

    def test_github_pat_and_slack_webhook_patterns_are_redacted(self):
        pat = "github_pat_" + "a" * 24
        webhook = "https://hooks.slack.com/services/T00000000/B00000000/abcdEFGHijklMNOP"

        output = redact(f"{pat} {webhook}", env_files=[])

        self.assertNotIn(pat, output)
        self.assertNotIn(webhook, output)

    def test_selected_runtime_file_adds_to_the_default_scan(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            default = root / "default.env"
            selected = root / "selected.env"
            default.write_text("SERVICE_TOKEN=default-secret-value\n", encoding="utf-8")
            selected.write_text("SERVICE_TOKEN=selected-secret-value\n", encoding="utf-8")
            with mock.patch("ummanu.runtime.redact.DEFAULT_ENV_FILES", [default]):
                output = redact("default-secret-value selected-secret-value", env_files=[selected])

        self.assertNotIn("default-secret-value", output)
        self.assertNotIn("selected-secret-value", output)

    def test_url_with_embedded_credentials_stays_an_exact_value_secret(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir) / "runtime.env"
            url = "https://operator:password@board.example.invalid/rpc"
            runtime.write_text(f"EXAMPLE_URL={url}\n", encoding="utf-8")

            self.assertEqual(redact(f"board is {url}", env_files=[runtime]), f"board is {REDACTED}:env-value")


if __name__ == "__main__":
    unittest.main()
