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

from ummanu import head_health
from ummanu.head_health import HeadHealth, HeadReadiness, resolve_head_chain


class Catalog:
    def head_profile(self, head: str):
        return {"resource": head}

    def resource(self, resource: str):
        return {"probe": f"probe {resource}"}


class HeadHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.health = HeadHealth(Catalog(), Path(self.tmpdir.name))

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_auth_failure_is_cached_and_blocks_launch(self) -> None:
        failed = subprocess.CompletedProcess("probe", 1, "", "Login expired. Please run /login")
        with mock.patch("ummanu.head_health._proc.run_isolated", return_value=failed) as run:
            first = self.health.check("openai-sub")
            second = self.health.check("openai-sub")

        self.assertEqual(first.status, "unauthenticated")
        self.assertFalse(first.launch_allowed)
        self.assertTrue(second.cached)
        run.assert_called_once()

    def test_provider_failure_is_unavailable(self) -> None:
        failed = subprocess.CompletedProcess("probe", 1, "", "503 biscuit_baker_service_me_circuit_open")
        with mock.patch("ummanu.head_health._proc.run_isolated", return_value=failed):
            result = self.health.check("openai-sub")

        self.assertEqual(result.status, "unavailable")
        self.assertFalse(result.launch_allowed)

    def test_a_spent_subscription_is_exhausted_and_blocks_launch(self) -> None:
        """The live wording, from the 2026-08-06 canary: nothing in it says "rate limit", so this
        used to fall through to `unknown`, which allows a launch. The dispatcher then claimed a
        card and put two heads into a resource that was out until the quota reset."""
        spent = subprocess.CompletedProcess(
            "probe",
            1,
            "",
            "ERROR: You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage "
            "to purchase more credits or try again at Aug 8th, 2026 9:13 PM.",
        )
        with mock.patch("ummanu.head_health._proc.run_isolated", return_value=spent):
            result = self.health.check("openai-sub")

        self.assertEqual(result.status, "exhausted")
        self.assertEqual(result.reason, "resource quota is spent")
        self.assertFalse(result.launch_allowed)

    def test_a_spent_quota_is_not_read_as_a_flaky_provider(self) -> None:
        """`unavailable` and `exhausted` mean different things to an operator, and a body can carry
        both vocabularies: a 429 quota refusal is spent credit, not a provider having a bad minute."""
        both = subprocess.CompletedProcess(
            "probe",
            1,
            "",
            "429 insufficient_quota: you exceeded your current quota",
        )
        with mock.patch("ummanu.head_health._proc.run_isolated", return_value=both):
            result = self.health.check("openai-sub")

        self.assertEqual(result.status, "exhausted")
        self.assertFalse(result.launch_allowed)

    def test_a_probe_timeout_is_timed_out_and_blocks_launch(self) -> None:
        """secretary-1799 changed this contract: a timeout used to be `unknown` and launchable,
        which sent every 2026-09-25 review back into the dead Codex provider."""
        with mock.patch(
            "ummanu.head_health._proc.run_isolated", side_effect=subprocess.TimeoutExpired("probe", 20)
        ):
            result = self.health.check("openai-sub")

        self.assertEqual(result.status, head_health.PROBE_TIMED_OUT)
        self.assertFalse(result.launch_allowed)


# The registry the walk below reads: two families, each head naming the other family's counterpart,
# which is the cyclic shape a real canon has once both directions are written down.
CHAINS = {
    "codex": ["claude-opus"],
    "codex-reviewer": ["claude-default"],
    "claude-opus": ["codex"],
    "claude-default": ["codex-reviewer"],
    "lonely": [],
}
RESOURCES = {
    "codex": "openai-sub",
    "codex-reviewer": "openai-sub",
    "claude-opus": "claude-sub",
    "claude-default": "claude-sub",
    "lonely": "claude-sub",
}


def _readiness(dead: dict[str, str]):
    def readiness(head: str) -> HeadReadiness:
        resource = RESOURCES.get(head, "")
        status = dead.get(resource, "ready")
        return HeadReadiness(resource, status, f"{resource} is {status}", 1.0)

    return readiness


def _fallback(head: str):
    return CHAINS.get(head)


class ResolveHeadChainTests(unittest.TestCase):
    """secretary-1165: which head a role is launched on when its resource is not launchable."""

    def resolve(self, preferred: str, dead: dict[str, str]):
        return resolve_head_chain(preferred, _readiness(dead), _fallback)

    def test_a_green_resource_keeps_the_preferred_head(self) -> None:
        choice = self.resolve("codex", {})

        self.assertEqual(choice.head, "codex")
        self.assertFalse(choice.substituted)
        self.assertEqual(choice.rejected, ())

    def test_a_dead_resource_walks_the_chain_to_the_other_family(self) -> None:
        for status in ("unavailable", "exhausted", "unauthenticated"):
            with self.subTest(status=status):
                choice = self.resolve("codex", {"openai-sub": status})

                self.assertEqual(choice.head, "claude-opus")
                self.assertTrue(choice.substituted)
                self.assertIn("falling back to claude-opus", choice.reason)

    def test_two_dead_resources_leave_no_head_and_name_both(self) -> None:
        choice = self.resolve("codex", {"openai-sub": "exhausted", "claude-sub": "unavailable"})

        self.assertEqual(choice.head, "")
        self.assertFalse(choice.resolved)
        self.assertEqual([head for head, _ in choice.rejected], ["codex", "claude-opus"])
        self.assertIn("openai-sub is exhausted", choice.reason)
        self.assertIn("claude-sub is unavailable", choice.reason)

    def test_a_head_with_no_chain_and_a_dead_resource_is_a_skip(self) -> None:
        """No chain is the canon saying "this head or nothing", and the reason stays the resource's
        own: there is no walk to report."""
        choice = self.resolve("lonely", {"claude-sub": "exhausted"})

        self.assertEqual(choice.head, "")
        self.assertEqual(choice.reason, "claude-sub is exhausted")

    def test_a_cyclic_chain_terminates(self) -> None:
        choice = self.resolve("codex", {"openai-sub": "unavailable", "claude-sub": "unavailable"})

        self.assertEqual([head for head, _ in choice.rejected], ["codex", "claude-opus"])

    def test_an_unknown_chain_entry_is_never_launched(self) -> None:
        """`unknown` readiness is launch-allowed, and a head the registry cannot describe answers
        exactly that. Reaching one through a chain must not pin the claim to it."""
        choice = resolve_head_chain(
            "codex",
            _readiness({"openai-sub": "exhausted"}),
            lambda head: ["retired", "claude-opus"] if head == "codex" else _fallback(head),
        )

        self.assertEqual(choice.head, "claude-opus")
        self.assertEqual(
            [(head, readiness.status) for head, readiness in choice.rejected],
            [("codex", "exhausted"), ("retired", "missing")],
        )

    def test_an_unknown_preferred_head_is_left_to_its_own_readiness(self) -> None:
        """The card override and the role default are validated where they are read. A second check
        here would answer that question differently and turn a known failure into a silent skip."""
        choice = resolve_head_chain("mystery", _readiness({}), _fallback)

        self.assertEqual(choice.head, "mystery")


# The PATH `packaging/systemd/ummanu-dispatcher-production.service` pins for the dispatcher: the
# ordinary system directories and nothing else. The unit starts the dispatcher from its venv but
# never puts that venv on PATH, which is what this test environment reproduces.
UNIT_PATH_DIRECTORIES = (
    "/usr/local/sbin",
    "/usr/local/bin",
    "/usr/sbin",
    "/usr/bin",
    "/sbin",
    "/bin",
)
SRC = Path(__file__).resolve().parents[1] / "src"
# A probe that reaches no provider and answers exactly the question the real one dies on: can the
# interpreter this command resolves to import the product at all.
IMPORT_PROBE = 'python3 -P -c "import ummanu.automations"'


def _interpreter(directory: Path, *, pythonpath: Path | None) -> Path:
    """A `python3` on disk that stands in for one of the two interpreters a probe can resolve to.

    Both run this process's own interpreter, so the test says nothing about the developer's venv;
    what separates them is whether the product is on their import path. `-S` keeps site-packages
    out of both, and `-E` keeps the suite's own PYTHONPATH out of the system one, so "can it import
    ummanu.automations" has the same answer on every machine.
    """
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "python3"
    if pythonpath is None:
        body = f'exec "{sys.executable}" -S -E "$@"\n'
    else:
        body = f'PYTHONPATH="{pythonpath}"\nexport PYTHONPATH\nexec "{sys.executable}" -S "$@"\n'
    script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    script.chmod(0o755)
    return script


class ImportProbeCatalog:
    def head_profile(self, head: str):
        return {"resource": head}

    def resource(self, resource: str):
        return {"probe": IMPORT_PROBE}


class ProbeInterpreterTests(unittest.TestCase):
    """secretary-1464: the probe has to run under the interpreter the dispatcher itself runs under.

    Reproduced under the unit's environment rather than under the test process's, because the test
    process is exactly the one place where this defect never showed: it inherits a PATH with a venv
    on it, so `python3` found the product and the probe worked. Production did not, and from
    `691673d` (the src/ layout, 2026-08-19) every probe there answered `No module named
    triggered_agents` — an unclassified failure, which was `unknown`, which allowed every claim.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        root = Path(self.tmpdir.name)
        self.system = _interpreter(root / "usr" / "bin", pythonpath=None)
        self.venv = _interpreter(root / "venv" / "bin", pythonpath=SRC)
        self.health = HeadHealth(ImportProbeCatalog(), root / "data")
        self.unit_env = {
            "PATH": os.pathsep.join((str(self.system.parent), *UNIT_PATH_DIRECTORIES)),
            "HOME": str(root),
        }

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_a_path_without_the_venv_still_finds_the_product(self) -> None:
        with (
            mock.patch.dict(os.environ, self.unit_env, clear=True),
            mock.patch.object(head_health.sys, "executable", str(self.venv)),
        ):
            result = self.health.check("openai-sub")

        self.assertEqual(result.status, "ready", result.reason)
        self.assertTrue(result.launch_allowed)

    def test_an_interpreter_that_cannot_import_the_product_is_a_broken_probe(self) -> None:
        """The pre-fix production shape, kept as the negative control: when prepending our own
        interpreter changes nothing because that interpreter is the one without the product, the
        probe is reported as unrunnable rather than as an unknown resource."""
        with (
            mock.patch.dict(os.environ, self.unit_env, clear=True),
            mock.patch.object(head_health.sys, "executable", str(self.system)),
        ):
            result = self.health.check("openai-sub")

        self.assertEqual(result.status, head_health.PROBE_BROKEN)
        self.assertIn("No module named", result.reason)
        self.assertFalse(result.launch_allowed)

    def test_the_environment_of_this_process_is_not_modified(self) -> None:
        before = dict(os.environ)
        with mock.patch.object(head_health.sys, "executable", str(self.venv)):
            environment = head_health.probe_env()

        self.assertEqual(dict(os.environ), before)
        self.assertTrue(environment["PATH"].startswith(f"{self.venv.parent}{os.pathsep}"))
        self.assertIn(os.environ.get("PATH", ""), environment["PATH"])


class BrokenProbeStatusTests(unittest.TestCase):
    """secretary-1464: "the probe could not be run" is its own verdict, and it blocks the claim."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.health = HeadHealth(Catalog(), Path(self.tmpdir.name))

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def check(self, completed: subprocess.CompletedProcess) -> HeadReadiness:
        """One verdict per call: a fresh store, so the TTL cache never answers for the next case."""
        health = HeadHealth(Catalog(), Path(tempfile.mkdtemp(dir=self.tmpdir.name)))
        with mock.patch("ummanu.head_health._proc.run_isolated", return_value=completed):
            return health.check("openai-sub")

    def test_a_missing_module_is_a_broken_probe(self) -> None:
        result = self.check(
            subprocess.CompletedProcess(
                "probe", 1, "", "/usr/bin/python3: No module named triggered_agents\n"
            )
        )

        self.assertEqual(result.status, head_health.PROBE_BROKEN)
        self.assertEqual(
            result.reason,
            "probe could not be launched: /usr/bin/python3: No module named triggered_agents",
        )
        self.assertFalse(result.launch_allowed)

    def test_a_command_the_shell_cannot_find_is_a_broken_probe(self) -> None:
        result = self.check(subprocess.CompletedProcess("probe", 127, "", "sh: 1: python3: not found\n"))

        self.assertEqual(result.status, head_health.PROBE_BROKEN)
        self.assertFalse(result.launch_allowed)

    def test_a_probe_that_cannot_be_started_at_all_is_a_broken_probe(self) -> None:
        """No shell to run it with is the same defect as no interpreter to run it under."""
        with mock.patch(
            "ummanu.head_health._proc.run_isolated", side_effect=OSError(8, "Exec format error")
        ):
            result = self.health.check("openai-sub")

        self.assertEqual(result.status, head_health.PROBE_BROKEN)
        self.assertFalse(result.launch_allowed)

    def test_an_unexplained_provider_refusal_is_still_unknown(self) -> None:
        result = self.check(subprocess.CompletedProcess("probe", 1, "", "the model declined\n"))

        self.assertEqual(result.status, "unknown")
        self.assertTrue(result.launch_allowed)

    def test_a_classified_provider_failure_is_never_read_as_a_broken_probe(self) -> None:
        """The three statuses the classifier already names keep their meaning even when the body
        also carries a word this card reads as a launch failure — only a provider that was actually
        reached can say any of them."""
        for text, expected in (
            ("Please run /login: no such file or directory ~/.codex/auth.json", "unauthenticated"),
            ("You've hit your usage limit; no such file or directory", "exhausted"),
            ("503 service unavailable, no such file or directory", "unavailable"),
        ):
            with self.subTest(expected=expected):
                result = self.check(subprocess.CompletedProcess("probe", 1, "", text))

                self.assertEqual(result.status, expected)
                self.assertFalse(result.launch_allowed)


class BrokenProbeChainTests(unittest.TestCase):
    """secretary-1464: a resource nobody can probe is walked past, not launched into."""

    def test_a_broken_probe_is_not_claimable_and_the_chain_is_walked(self) -> None:
        choice = resolve_head_chain("codex", _readiness({"openai-sub": head_health.PROBE_BROKEN}), _fallback)

        self.assertEqual(choice.head, "claude-opus")
        self.assertTrue(choice.substituted)
        self.assertEqual([head for head, _ in choice.rejected], ["codex"])
        self.assertIn("openai-sub is probe_broken", choice.reason)

    def test_a_broken_probe_everywhere_claims_nothing(self) -> None:
        """The whole point of the new status: with the gate itself broken on every resource the
        card stays in Ready instead of being handed to an unprobed account."""
        choice = resolve_head_chain(
            "codex",
            _readiness({"openai-sub": head_health.PROBE_BROKEN, "claude-sub": head_health.PROBE_BROKEN}),
            _fallback,
        )

        self.assertEqual(choice.head, "")
        self.assertFalse(choice.resolved)


class _ChainCatalog:
    """Two heads on two resources, each with the registry's own probe command."""

    PROFILES: ClassVar[dict[str, dict]] = {
        "codex-reviewer": {"resource": "openai-sub", "fallback": ["claude-opus"]},
        "claude-opus": {"resource": "claude-sub", "fallback": []},
    }

    def head_profile(self, head: str):
        return self.PROFILES[head]

    def resource(self, resource: str):
        return {"probe": f"python3 -P -m ummanu.runtime.resource_probe --resource {resource}"}

    def head_fallback(self, head: str):
        return self.PROFILES[head]["fallback"] if head in self.PROFILES else None


def _probe_by_resource(answers):
    """The outer probe command, answered per resource by `answers[resource](timeout)`."""

    def run_isolated(argv, *, timeout, env):
        resource = argv[-1].rsplit(" ", 1)[-1]
        return answers[resource](timeout)

    return run_isolated


class ProbeTimeoutAndProviderTests(unittest.TestCase):
    """secretary-1799 / issue:88142506: the probe waits for Codex's slow refusal and a timeout
    admits no launch."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.codex_home = self.root / "codex-home"
        self.codex_home.mkdir()
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for name in list(os.environ):
            if name.startswith("TA_PROBE_TIMEOUT_S"):
                del os.environ[name]

    def _login(self, mode: str) -> None:
        auth = {"auth_mode": mode, "OPENAI_API_KEY": None, "tokens": {"id_token": "t"}}
        (self.codex_home / "auth.json").write_text(json.dumps(auth), encoding="utf-8")

    def _inner(self, provider_answers_after: float, text: str) -> tuple[int, str]:
        """Run the real `openai-sub` probe against a Codex that answers after N seconds."""
        from ummanu.runtime import resource_probe

        def codex(cmd, *, capture_output, text: bool, timeout, env):
            if timeout < provider_answers_after:
                raise subprocess.TimeoutExpired(cmd, timeout, output="Reconnecting... 3/5\n")
            return subprocess.CompletedProcess(cmd, 1, "", answer)

        answer = text
        home = mock.Mock(path=str(self.codex_home))
        stderr = io.StringIO()
        with (
            mock.patch.object(resource_probe, "installation_codex_home", return_value=home),
            mock.patch.object(resource_probe.subprocess, "run", side_effect=codex),
            contextlib.redirect_stderr(stderr),
        ):
            code = resource_probe.main(["--resource", "openai-sub"])
        return code, stderr.getvalue()

    def _walk(self, openai_answer) -> head_health.HeadChoice:
        def ready(_timeout):
            return subprocess.CompletedProcess("probe", 0, "pong", "")

        health = HeadHealth(_ChainCatalog(), self.root / "data")
        with mock.patch(
            "ummanu.head_health._proc.run_isolated",
            side_effect=_probe_by_resource({"openai-sub": openai_answer, "claude-sub": ready}),
        ):
            return resolve_head_chain("codex-reviewer", health.check, _ChainCatalog().head_fallback)

    def test_the_outer_timeout_is_always_greater_than_the_inner_one(self) -> None:
        from ummanu.runtime.resource_probe import probe_timeout_s

        self.assertEqual(probe_timeout_s("openai-sub"), 75)
        for resource in ("claude-sub", "openrouter", "another"):
            self.assertEqual(probe_timeout_s(resource), 20)
        for overrides in (
            {},
            {"TA_PROBE_TIMEOUT_S": "40"},
            {"TA_PROBE_TIMEOUT_S_OPENAI_SUB": "120", "TA_PROBE_TIMEOUT_S_CLAUDE_SUB": "5"},
        ):
            with mock.patch.dict(os.environ, overrides):
                for resource in ("openai-sub", "claude-sub", "openrouter", "another"):
                    with self.subTest(overrides=overrides, resource=resource):
                        self.assertGreater(
                            head_health.probe_timeout_seconds(resource), probe_timeout_s(resource)
                        )
        with mock.patch.dict(os.environ, {"TA_PROBE_TIMEOUT_S_OPENAI_SUB": "120"}):
            self.assertEqual(probe_timeout_s("openai-sub"), 120)

    def test_the_probe_command_is_run_under_its_resource_outer_timeout(self) -> None:
        seen: list[float] = []

        def run_isolated(argv, *, timeout, env):
            seen.append(timeout)
            return subprocess.CompletedProcess("probe", 0, "", "")

        with mock.patch("ummanu.head_health._proc.run_isolated", side_effect=run_isolated):
            head_health.run_probe("openai-sub", "probe", 1.0)
        self.assertEqual(seen, [head_health.probe_timeout_seconds("openai-sub")])
        self.assertGreater(seen[0], 75)

    def test_a_slow_401_after_20s_and_before_75s_is_unavailable_and_the_chain_is_walked(self) -> None:
        self._login("chatgpt")
        refusal = (
            "Reconnecting... 5/5\nERROR: unexpected status 401 Unauthorized: Incorrect API key provided: "
            "sk-svcac" + "*" * 30 + "fvMA. You can find your API key at https://platform.openai.com, "
            "url: https://chatgpt.com/backend-api/codex/responses, cf-ray: a40da28f7896ec3e-VNO"
        )
        code, err = self._inner(45, refusal)

        self.assertEqual(code, 1)
        self.assertIn("status=provider-unavailable", err)
        choice = self._walk(lambda timeout: subprocess.CompletedProcess("probe", code, "", err))

        self.assertEqual(choice.head, "claude-opus")
        [(head, readiness)] = choice.rejected
        self.assertEqual((head, readiness.status), ("codex-reviewer", "unavailable"))
        self.assertFalse(readiness.launch_allowed)
        self.assertNotIn("sk-svcac", readiness.reason)

    def test_the_same_slow_401_under_the_old_20s_limit_is_a_timeout_not_a_launch(self) -> None:
        self._login("chatgpt")
        with mock.patch.dict(os.environ, {"TA_PROBE_TIMEOUT_S_OPENAI_SUB": "20"}):
            code, err = self._inner(45, "unexpected status 401 Unauthorized: Incorrect API key provided")
        self.assertIn("status=timeout", err)

        choice = self._walk(lambda timeout: subprocess.CompletedProcess("probe", code, "", err))

        self.assertEqual(choice.head, "claude-opus")
        self.assertEqual(choice.rejected[0][1].status, head_health.PROBE_TIMED_OUT)

    def test_a_real_timeout_is_timed_out_not_launchable_and_the_chain_is_walked(self) -> None:
        def hang(timeout):
            raise subprocess.TimeoutExpired("probe", timeout)

        choice = self._walk(hang)

        self.assertEqual(choice.head, "claude-opus")
        [(head, readiness)] = choice.rejected
        self.assertEqual((head, readiness.status), ("codex-reviewer", head_health.PROBE_TIMED_OUT))
        self.assertFalse(readiness.launch_allowed)
        self.assertIn("timed out after 85s", readiness.reason)

    def test_an_answered_unclassifiable_probe_is_still_unknown_and_launchable(self) -> None:
        choice = self._walk(lambda timeout: subprocess.CompletedProcess("probe", 1, "", "the model declined"))
        self.assertEqual(choice.head, "codex-reviewer")
        self.assertEqual(choice.readiness.status, "unknown")

    def test_a_logged_out_account_is_still_unauthenticated(self) -> None:
        from ummanu.runtime import resource_probe
        from ummanu.runtime.codex_preflight import CodexHomeLoginMissing

        stderr = io.StringIO()
        with (
            mock.patch.object(
                resource_probe, "installation_codex_home", side_effect=CodexHomeLoginMissing(self.codex_home)
            ),
            contextlib.redirect_stderr(stderr),
        ):
            code = resource_probe.main(["--resource", "openai-sub"])
        for label, (answer_code, answer) in {
            "no login at all": (code, stderr.getvalue()),
            "codex says so": self._inner_with_login("chatgpt", "Error: Not logged in. Run `codex login`."),
            "401 on an API-key login": self._inner_with_login(
                "apikey", "unexpected status 401 Unauthorized: Incorrect API key provided: sk-abc"
            ),
        }.items():
            with self.subTest(label):
                self.assertNotIn("status=provider-unavailable", answer)
                choice = self._walk(
                    lambda timeout, c=answer_code, a=answer: subprocess.CompletedProcess("probe", c, "", a)
                )
                self.assertEqual(choice.rejected[0][1].status, "unauthenticated")

    def _inner_with_login(self, mode: str, text: str) -> tuple[int, str]:
        self._login(mode)
        return self._inner(0, text)

    def test_5xx_and_an_exhausted_reconnect_loop_are_the_provider_not_the_account(self) -> None:
        for text in (
            "ERROR: unexpected status 502 Bad Gateway; try to login again later",
            "Reconnecting... 5/5 (stream disconnected before completion); authentication pending",
        ):
            with self.subTest(text=text):
                with mock.patch(
                    "ummanu.head_health._proc.run_isolated",
                    return_value=subprocess.CompletedProcess("probe", 1, "", text),
                ):
                    verdict = head_health.run_probe("openai-sub", "probe", 1.0)
                self.assertEqual(verdict.status, "unavailable")
                self.assertFalse(verdict.launch_allowed)

    def test_a_recorded_verdict_replaces_the_cached_probe_and_holds_without_a_probe(self) -> None:
        health = HeadHealth(_ChainCatalog(), self.root / "data")
        with mock.patch(
            "ummanu.head_health._proc.run_isolated",
            return_value=subprocess.CompletedProcess("probe", 0, "", ""),
        ) as run:
            self.assertEqual(health.check("codex-reviewer").status, "ready")
            health.record("openai-sub", "unavailable", "provider error on the first turn")
            recorded = health.check("codex-reviewer")
        self.assertEqual(recorded.status, "unavailable")
        self.assertTrue(recorded.cached)
        run.assert_called_once()

    def test_doctor_shows_timed_out_like_the_other_non_ready_statuses(self) -> None:
        from ummanu.cli import _recovery_findings
        from ummanu.infra.doctor_provider import PROVIDER_RED_STATES

        self.assertIn(head_health.PROBE_TIMED_OUT, PROVIDER_RED_STATES)
        findings = _recovery_findings(
            {
                "resources": [
                    {
                        "resource": "openai-sub",
                        "state": head_health.PROBE_TIMED_OUT,
                        "reason": "probe timed out",
                    },
                    {"resource": "claude-sub", "state": "unavailable", "reason": "down"},
                    {"resource": "openrouter", "state": "ready", "reason": "ok"},
                ]
            }
        )
        self.assertEqual(
            [
                (finding["resource"], finding["state"])
                for finding in findings
                if finding["code"] == "resource_readiness"
            ],
            [("openai-sub", head_health.PROBE_TIMED_OUT), ("claude-sub", "unavailable")],
        )
