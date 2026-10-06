"""`ummanu.runtime.resource_probe`: the registry's probe entry, and how `head_health` reads it.

Every provider call is a fake: no test here spends a token or touches a network.
"""

from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from ummanu import head_health
from ummanu.head_health import HeadHealth
from ummanu.runtime import codex_preflight, heads, resource_probe
from ummanu.runtime.resource_probe import ProbeResult

ROOT = Path(__file__).resolve().parents[1]


def _main(resource: str) -> tuple[int, str]:
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        code = resource_probe.main(["--resource", resource])
    return code, stderr.getvalue()


class _Catalog:
    def head_profile(self, head: str) -> dict:
        return {"resource": head}

    def resource(self, resource: str) -> dict:
        return {"probe": f"python3 -P -m ummanu.runtime.resource_probe --resource {resource}"}


class CliContractTests(unittest.TestCase):
    """0 healthy, 1 with one scrubbed line on stderr, 2 for an id with no probe."""

    def test_a_healthy_resource_exits_zero_silently(self) -> None:
        for resource in ("claude-sub", "openai-sub", "openrouter"):
            with (
                self.subTest(resource),
                mock.patch.dict(resource_probe.BUILTIN_PROBES, {resource: lambda: ProbeResult(True, "fake")}),
            ):
                self.assertEqual(_main(resource), (0, ""))

    def test_a_failed_resource_exits_one_with_its_reason(self) -> None:
        failed = ProbeResult(
            False,
            "builtin:claude-sub",
            command="claude -p ping",
            status="non-zero-exit",
            exit_code=1,
            stderr="API Error: 429 rate limit reached",
        )
        with mock.patch.dict(resource_probe.BUILTIN_PROBES, {"claude-sub": lambda: failed}):
            code, err = _main("claude-sub")
        self.assertEqual(code, 1)
        self.assertEqual(
            err.strip(),
            "resource claude-sub probe failed; class=builtin:claude-sub; status=non-zero-exit; "
            "command=claude -p ping; exit_code=1; stderr=API Error: 429 rate limit reached",
        )

    def test_an_unknown_resource_exits_two(self) -> None:
        code, err = _main("nope")
        self.assertEqual(code, 2)
        self.assertIn("no builtin probe for 'nope'", err)

    def test_the_module_runs_as_the_registry_names_it(self) -> None:
        """The shipped registry's command shape, end to end, on the one id that calls no provider."""
        completed = subprocess.run(
            [sys.executable, "-P", "-m", "ummanu.runtime.resource_probe", "--resource", "nope"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=ROOT,
            check=False,
            env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"},
        )
        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertIn("no builtin probe", completed.stderr)

    def test_the_shipped_registry_names_this_entry_for_every_resource(self) -> None:
        registry = heads.load_registry(heads.HEADS_TOML)
        for rid, resource in registry.resources.items():
            with self.subTest(rid):
                self.assertEqual(
                    resource["probe"], f"python3 -P -m ummanu.runtime.resource_probe --resource {rid}"
                )
                self.assertIn(rid, resource_probe.BUILTIN_PROBES)


class ProviderFakeTests(unittest.TestCase):
    """The provider calls themselves, with the subprocess and the HTTP transport faked."""

    def test_claude_sub_is_one_haiku_call(self) -> None:
        ok = subprocess.CompletedProcess([], 0, "pong", "")
        with mock.patch.object(resource_probe.subprocess, "run", return_value=ok) as run:
            self.assertTrue(resource_probe.probe_claude_sub().ok)
        self.assertEqual(run.call_args.args[0][:4], ["claude", "-p", "ping", "--model"])

    def test_openai_sub_runs_codex_in_the_shared_home(self) -> None:
        failed = subprocess.CompletedProcess([], 1, "", "You've hit your usage limit")
        with mock.patch.object(resource_probe.subprocess, "run", return_value=failed) as run:
            result = resource_probe.probe_openai_sub()
        self.assertFalse(result.ok)
        # ummanu-108: a spent usage limit is named as such, not left as a bare non-zero exit.
        self.assertEqual(result.status, "exhausted")
        self.assertEqual(run.call_args.kwargs["env"]["CODEX_HOME"], codex_preflight.codex_home({}))

    def test_a_spent_quota_is_read_past_the_banner_with_its_reset(self) -> None:
        banner = "Reading additional input from stdin...\nOpenAI Codex v0.159.2\n" + "-" * 600 + "\n"
        failed = subprocess.CompletedProcess(
            [], 1, "",
            banner + "ERROR: You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to "
            "purchase more credits or try again at Oct 9th, 2026 9:11 PM.",
        )
        with mock.patch.object(resource_probe.subprocess, "run", return_value=failed):
            result = resource_probe.probe_openai_sub()
        line = resource_probe.format_probe_failure("openai-sub", result)
        self.assertIn("status=exhausted", line)
        self.assertIn("provider_error=ERROR: You've hit your usage limit", line)
        self.assertIn("try again at Oct 9th, 2026 9:11 PM", line)

    def test_a_missing_binary_and_a_timeout_are_failures_not_exceptions(self) -> None:
        with mock.patch.object(resource_probe.subprocess, "run", side_effect=FileNotFoundError("claude")):
            self.assertEqual(resource_probe.probe_claude_sub().status, "exception")
        with mock.patch.object(
            resource_probe.subprocess, "run", side_effect=subprocess.TimeoutExpired("claude", 20)
        ):
            self.assertEqual(resource_probe.probe_claude_sub().status, "timeout")

    def test_openrouter_without_a_key_calls_nothing(self) -> None:
        with (
            mock.patch.object(resource_probe, "_read_openrouter_key", return_value=None),
            mock.patch.object(resource_probe.urllib.request, "urlopen") as urlopen,
        ):
            result = resource_probe.probe_openrouter()
        self.assertEqual((result.ok, result.status), (False, "auth"))
        urlopen.assert_not_called()

    def test_openrouter_maps_http_answers(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value.status = 200
        with (
            mock.patch.object(resource_probe, "_read_openrouter_key", return_value="sk-or-fake"),
            mock.patch.object(resource_probe.urllib.request, "urlopen", return_value=response),
        ):
            self.assertTrue(resource_probe.probe_openrouter().ok)
        refused = urllib.error.HTTPError("u", 429, "Too Many Requests", {}, io.BytesIO(b"slow down"))  # type: ignore[arg-type]
        with (
            mock.patch.object(resource_probe, "_read_openrouter_key", return_value="sk-or-fake"),
            mock.patch.object(resource_probe.urllib.request, "urlopen", side_effect=refused),
        ):
            result = resource_probe.probe_openrouter()
        self.assertEqual((result.status, result.http_status), ("rate-limit", 429))

    def test_failure_text_is_scrubbed_and_capped(self) -> None:
        secret = "sk-ant-oat01-" + "a" * 40
        noisy = ProbeResult(False, "fake", status="non-zero-exit", stderr=f"token {secret} " + "x" * 2000)
        line = resource_probe.format_probe_failure("claude-sub", noisy)
        self.assertNotIn(secret, line)
        self.assertIn("...[truncated]", line)


class HeadHealthReadsTheEntryTests(unittest.TestCase):
    """`head_health` classifies this entry's exit code and output as it classified the old module's."""

    def verdict(self, resource: str, result: ProbeResult) -> head_health.HeadReadiness:
        with mock.patch.dict(resource_probe.BUILTIN_PROBES, {resource: lambda: result}):
            code, err = _main(resource)
        completed = subprocess.CompletedProcess("probe", code, "", err)
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(head_health._proc, "run_isolated", return_value=completed),
        ):
            return HeadHealth(_Catalog(), Path(tmp)).check(resource)

    def test_each_exit_is_read_as_before(self) -> None:
        cases = {
            "healthy": (ProbeResult(True, "fake"), "ready"),
            "logged out": (
                ProbeResult(
                    False,
                    "builtin:claude-sub",
                    status="non-zero-exit",
                    exit_code=1,
                    stderr="Invalid API key · Please run /login",
                ),
                "unauthenticated",
            ),
            "spent": (
                ProbeResult(
                    False,
                    "builtin:openai-sub",
                    status="non-zero-exit",
                    exit_code=1,
                    stderr="You've hit your usage limit",
                ),
                "exhausted",
            ),
            "openrouter 429": (
                ProbeResult(
                    False,
                    "builtin:openrouter",
                    status="rate-limit",
                    http_status=429,
                    exception="HTTPError: HTTP Error 429: Too Many Requests",
                ),
                "unavailable",
            ),
            "timeout": (
                ProbeResult(
                    False,
                    "builtin:claude-sub",
                    status="timeout",
                    timeout_s=20.0,
                    exception="TimeoutExpired: timed out",
                ),
                # secretary-1799: the inner probe's own timeout is `timed_out`, not `unknown`.
                head_health.PROBE_TIMED_OUT,
            ),
        }
        for label, (result, status) in cases.items():
            with self.subTest(label):
                self.assertEqual(self.verdict("claude-sub", result).status, status)

    def test_an_unknown_id_is_unknown_and_launchable(self) -> None:
        code, err = _main("nope")
        completed = subprocess.CompletedProcess("probe", code, "", err)
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(head_health._proc, "run_isolated", return_value=completed),
        ):
            readiness = HeadHealth(_Catalog(), Path(tmp)).check("nope")
        self.assertEqual(readiness.status, "unknown")
        self.assertTrue(readiness.launch_allowed)


if __name__ == "__main__":
    unittest.main()
