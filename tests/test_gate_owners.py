"""Owner and binding regressions; scripted answers never attest real GitHub execution.

Expected commands/bytes are source-derived contract examples, not an independently
executed historical baseline. The existing Git/backend corpus remains in CI.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import yaml

from tests.support import gate_owners
from tests.support.fakes.gate import CiAnswers, ScriptedGateHost, github_fixture
from ummanu.dispatch import gate, gate_pr, gate_receipt, gate_types, gate_workflows, github_ci, types

ROOT = Path(__file__).resolve().parents[1]


class GateOwnerTests(unittest.TestCase):
    def test_unique_definitions_and_capability_owners(self):
        sources = gate_owners.sources(ROOT)
        inventory = gate_owners.inventory(sources)
        self.assertEqual(gate_owners.file_limits(sources), [])
        for owner, names in (
            ("gate", ("gate_check", "_local_gate", "_recover_base", "_github_gate", "_candidate_history_gate")),
            ("github_ci", ("_backend_call", "_poll_ci", "_rollup", "_failed_log", "_LogFragment", "_required_checks", "rerun_failed_ci")),
            ("gate_pr", ("_publish_branch", "_ensure_pr", "_pr_digest", "_remember_pr", "_LEASE_REFUSED_RE")),
            ("gate_workflows", ("_impossible_trigger_reason", "push_trigger_absent_reason", "_PR_EVENTS")),
            ("gate_types", ("GateResult", "_fingerprint")),
        ):
            for name in names:
                self.assertEqual(inventory[name][0], owner, name)
        for owner in gate_owners.OWNERS[1:]:
            self.assertEqual(gate_owners.facade_dependencies(owner, sources[owner]), set(), owner)
        for consumer in gate_owners.CI_CONSUMERS:
            source = (ROOT / f"src/ummanu/dispatch/{consumer}.py").read_text()
            self.assertEqual(gate_owners.facade_dependencies(consumer, source), set(), consumer)

    def test_guards_detect_duplicate_size_and_nested_relative_dynamic_string_edges(self):
        with self.assertRaisesRegex(AssertionError, "duplicate gate implementation"):
            gate_owners.inventory({"gate": "def _backend_call(): pass", "github_ci": "def _backend_call(): pass"})
        self.assertEqual(gate_owners.file_limits({"planted": "\n" * 3001}), ["planted"])
        for source in (
            "def f():\n from .gate import GateResult\n",
            "from . import gate as facade\nx = facade._backend_call\n",
            "import ummanu.dispatch.gate as facade\nx = facade.GateResult\n",
            "from importlib import import_module as load\nx = load('.gate', 'ummanu.dispatch')\n",
            "TARGET = 'ummanu.dispatch.gate._backend_call'\n",
        ):
            with self.subTest(source=source):
                self.assertTrue(gate_owners.facade_dependencies("github_ci", source))

    def test_result_identity_fields_defaults_and_error_identities(self):
        self.assertIs(gate.GateResult, gate_types.GateResult)
        self.assertIs(github_ci.GateResult, gate.GateResult)
        self.assertIs(gate_pr.GateResult, gate.GateResult)
        self.assertEqual(list(dataclasses.asdict(gate.GateResult("green", "ok"))), [
            "status", "summary", "log", "fingerprint", "failure_class", "failure_reason",
            "failed_run_id", "failed_run_repo", "attestation",
        ])
        self.assertEqual(dataclasses.asdict(github_ci._LogFragment(False)), {
            "available": False, "step": "", "text": "", "failure_class": "substantive",
            "failure_reason": "", "reason": "",
        })
        for name in ("HostError", "GateTransportError", "ProjectGitAccessError"):
            self.assertIs(getattr(github_ci, name), getattr(types, name))
        self.assertIs(gate.GATE_PENDING_STALL_SECONDS, gate_types.GATE_PENDING_STALL_SECONDS)
        self.assertIs(github_ci.GATE_LOG_FRAGMENT_LINES, gate_types.GATE_LOG_FRAGMENT_LINES)
        self.assertIs(gate_pr.PR_BODY_SECTION_CHARS, gate_types.PR_BODY_SECTION_CHARS)

    def test_environment_limits_keep_import_time_order_and_owner_defaults(self):
        source = (ROOT / "src/ummanu/dispatch/gate_types.py").read_text()
        names = [node.targets[0].id for node in ast.parse(source).body if isinstance(node, ast.Assign)]
        self.assertEqual(names, ["GATE_PENDING_STALL_SECONDS", "GATE_LOG_FRAGMENT_LINES", "GATE_TRANSPORT_MAX_ATTEMPTS",
                                 "GATE_INFRASTRUCTURE_RERUN_MAX_ATTEMPTS", "PR_BODY_SECTION_CHARS"])
        self.assertEqual(github_ci._failed_log.__defaults__, (gate_types.GATE_LOG_FRAGMENT_LINES,))
        self.assertEqual(gate_workflows._workflow_triggers_base.__defaults__, (gate_workflows._PR_EVENTS,))
        with mock.patch.object(gate_pr, "PR_BODY_SECTION_CHARS", 3):
            self.assertEqual(gate_pr._pr_section("abcdef"), "abc\n\n… (truncated; the full text is on the card)")

    def test_shared_backend_spy_observes_core_pr_and_ci_in_order(self):
        host, task, record, sha = github_fixture()
        with (mock.patch.object(github_ci, "_backend_call", wraps=github_ci._backend_call) as backend,
              mock.patch.object(gate_receipt, "datetime") as clock):
            clock.now.return_value = datetime(2026, 10, 13, tzinfo=UTC)
            result = gate._github_gate(host, task, record, "/fixture", "main", ["tests"])
        self.assertEqual(result.status, "green")
        self.assertEqual([call.args[2] for call in backend.call_args_list], [
            "gate remote branch sha", "gate publish branch", "gate pr list", "gate pr view", "gate repo view",
            "gate gh api", "gate gh api", "gate gh api",
        ])
        self.assertEqual(host.events, [
            "gate head sha", "gate remote branch sha", "gate publish branch", "commit publication",
            "gate pr list", "gate pr view", "gate repo view", "gate gh api", "gate gh api", "gate gh api",
            "gate attestation base",
        ])
        self.assertEqual(host.calls[2], ([
            "git", "-C", "/fixture", "push", "--force-with-lease=refs/heads/pipeline/p-1:",
            "origin", "pipeline/p-1:refs/heads/pipeline/p-1",
        ], "gate publish branch", None, "p"))
        self.assertEqual(host.calls[3], ([
            "gh", "pr", "list", "--head", "pipeline/p-1", "--state", "open", "--json", "number,baseRefName",
        ], "gate pr list", Path("/fixture"), ""))
        expected = {
            "validated_sha": sha, "base_sha": "b" * 40, "gate_mode": "github",
            "required_checks": [{"name": "tests", "conclusion": "SUCCESS", "url": "https://github.com/org/repo/actions/runs/7"}],
            "completed_at": "2026-10-13T00:00:00+00:00",
            "command_or_check_set_digest": "15af5694998b7fcbefc44c85706c877cd7287854dda818098eea18cf314ded98",
        }
        self.assertEqual(result.attestation, expected)
        self.assertEqual(list(result.attestation), list(expected))

    def test_pr_rendered_bytes_and_failure_fingerprint(self):
        task = {"ref": "p-1", "title": "Gate"}
        self.assertEqual(gate_pr._pr_title(task, "pipeline/p-1"), "p-1: Gate")
        self.assertEqual(gate_pr._pr_body(task, "pipeline/p-1", "a" * 40, ["tests"]),
            "## What\n\nGate\n\n## Why\n\nGate\n\n## How verified\n\n"
            "CI runs `tests` on `aaaaaaaaaaaa`; the merge waits for them to pass.\n\nCard: `p-1`")
        self.assertEqual(gate_types._fingerprint("github", "tests", "Run tests", "##[error]assert False"), "09dd46dc3f1a9638")

    def test_core_base_fetch_and_project_credential_refusal_use_shared_backend(self):
        refusal = types.ProjectGitAccessError("p", "credential_refused", "credential sentinel")
        host = ScriptedGateHost({"gate base fetch": refusal})
        with (mock.patch.object(github_ci, "_backend_call", wraps=github_ci._backend_call) as backend,
              self.assertRaises(types.ProjectGitAccessError) as raised):
            gate._recover_base(host, "/fixture", "main", "p")
        self.assertIs(raised.exception, refusal)
        backend.assert_called_once_with(host, ["git", "-C", "/fixture", "fetch", "origin", "main"], "gate base fetch", project="p")

    def test_pr_retarget_commands_and_readback_authorship_order(self):
        host = ScriptedGateHost({label: (0, '{"title":"stored","body":"readback"}', "") for label in (
            "gate pr retarget", "gate pr close", "gate pr reopen", "gate pr view",
        )})
        record = mock.Mock(gate_pr_authorship=None)
        with mock.patch.object(github_ci, "_backend_call", wraps=github_ci._backend_call) as backend:
            gate_pr._retarget_pr(host, "/fixture", 7, "old", "main")
            gate_pr._remember_pr(host, "/fixture", record, 7, "sent", "body")
        self.assertEqual([call.args[1] for call in backend.call_args_list[:3]], [
            ["gh", "pr", "edit", "7", "--base", "main"], ["gh", "pr", "close", "7"], ["gh", "pr", "reopen", "7"],
        ])
        self.assertEqual(host.events[-2:], ["gate pr view", "commit authorship"])
        self.assertEqual(record.gate_pr_authorship["digest"], gate_pr._pr_digest("stored", "readback"))
        self.assertEqual(record.gate_pr_authorship["sent"], gate_pr._pr_digest("sent", "body"))

    def test_failed_log_owner_degrades_silence_and_keeps_job_step_classification(self):
        item = {"name": "tests", "details_url": "https://github.com/org/repo/actions/runs/7"}
        host = ScriptedGateHost({"gate failed log": (0,
            ("other\tRun tests\t##[error]foreign\ntests\tSet up job\tGetting action download info\n"
             "tests\tSet up job\t##[error]Failed to download action HTTP 503"), "")})
        fragment = github_ci._failed_log(host, "org/repo", item)
        self.assertEqual((fragment.available, fragment.step, fragment.failure_class, fragment.failure_reason),
                         (True, "Set up job", "infrastructure", "action-download-http-5xx"))
        self.assertNotIn("foreign", fragment.text)
        host.answers["gate failed log"] = (1, "", "unclassified silence")
        degraded = github_ci._failed_log(host, "org/repo", item)
        self.assertFalse(degraded.available)
        self.assertIn("got no answer: unclassified silence", degraded.reason)

    def test_workflow_proofs_remain_conservative_for_yaml_boolean_and_unreadable_files(self):
        self.assertTrue(gate_workflows._workflow_triggers_base(yaml.safe_load("on: pull_request"), "main"))
        self.assertFalse(gate_workflows._branch_pattern_matches("release/*", "release/a/b"))
        self.assertTrue(gate_workflows._branch_pattern_matches("release/**", "release/a/b"))
        self.assertFalse(gate_workflows._workflow_triggers_base({"on": {"push": {"tags": ["*"]}}}, "main", ("push",)))
        with tempfile.TemporaryDirectory() as tmp:
            workflows = Path(tmp) / ".github/workflows"
            workflows.mkdir(parents=True)
            (workflows / "b.yml").write_text("on: {pull_request: {branches: [other]}, push: {tags: ['*']}}")
            (workflows / "a.yml").write_text("on: workflow_dispatch")
            self.assertIn("2 workflow file(s) read: a.yml, b.yml", gate_workflows._impossible_trigger_reason(tmp, "main"))
            self.assertTrue(gate_workflows.push_trigger_absent_reason(tmp, "main"))
            with mock.patch.object(gate_workflows.yaml, "safe_load", side_effect=OSError("unreadable")):
                self.assertEqual(gate_workflows._impossible_trigger_reason(tmp, "main"), "")


SHA = "c" * 40
DONE_PR_RUN = {"id": 11, "event": "pull_request", "status": "completed", "conclusion": "success"}


def _run_check(name, conclusion, *, run=11, status="completed", **extra):
    return {"name": name, "status": status, "conclusion": conclusion,
            "details_url": f"https://github.com/org/repo/actions/runs/{run}/job/1", **extra}


class GithubCiReaderTests(unittest.TestCase):
    def _poll(self, host, required=()):
        rollup, failed, _checked = github_ci._poll_ci(host, "org/repo", SHA, list(required))
        return rollup, failed

    def test_check_with_conclusion_and_completed_at_is_terminal_although_status_lags(self):
        ghost = _run_check("detect", "success", status="in_progress", completed_at="2026-10-06T20:14:33Z")
        host = CiAnswers([ghost], [DONE_PR_RUN])
        self.assertEqual(self._poll(host, ["detect"]), ("SUCCESS", None))

    def test_conclusion_outside_github_vocabulary_is_pending(self):
        host = CiAnswers([_run_check("tests", "mystery")], [DONE_PR_RUN])
        self.assertEqual(self._poll(host, ["tests"]), ("PENDING", None))

    def test_check_runs_are_read_across_every_page(self):
        first = json.dumps({"total_count": 2, "check_runs": [_run_check("a", "success")]})
        second = json.dumps({"total_count": 2, "check_runs": [_run_check("b", "failure")]})
        host = CiAnswers(workflow_runs=[DONE_PR_RUN], raw={"check-runs": f"{first}\n{second}\n"})
        rollup, failed = self._poll(host, ["a", "b"])
        self.assertEqual((rollup, failed["name"]), ("FAILURE", "b"))

    def test_empty_unreadable_or_short_answers_are_transport_errors(self):
        short = json.dumps({"total_count": 3, "check_runs": [_run_check("a", "success")]})
        for text in ("", "not json", "[]", short):
            with self.subTest(text=text):
                host = CiAnswers(workflow_runs=[DONE_PR_RUN], raw={"check-runs": text})
                with self.assertRaises(types.GateTransportError):
                    self._poll(host)
        with self.assertRaises(types.GateTransportError):
            github_ci._gh_api(CiAnswers(raw={"compare/x...y": ""}), "repos/o/r/compare/x...y", jq="{status}")

    def test_early_failure_is_red_while_required_matrix_checks_were_never_created(self):
        running = {**DONE_PR_RUN, "status": "in_progress", "conclusion": None}
        host = CiAnswers([_run_check("fast", "failure")], [running])
        rollup, failed = self._poll(host, ["fast", "matrix (a)"])
        self.assertEqual((rollup, failed["name"]), ("FAILURE", "fast"))

    def test_missing_required_check_waits_while_a_run_is_open_and_is_red_once_all_completed(self):
        running = {**DONE_PR_RUN, "status": "in_progress", "conclusion": None}
        self.assertEqual(self._poll(CiAnswers([_run_check("fast", "success")], [running]), ["fast", "matrix"]),
                         ("PENDING", None))
        rollup, failed = self._poll(CiAnswers([_run_check("fast", "success")], [DONE_PR_RUN]), ["fast", "matrix"])
        self.assertEqual((rollup, failed["name"], failed["conclusion"]),
                         ("FAILURE", "matrix", github_ci.MISSING_CONCLUSION))

    def test_gate_names_a_required_check_that_was_never_posted(self):
        host, task, record, _sha = github_fixture()
        with mock.patch.object(gate_receipt, "datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 13, tzinfo=UTC)
            result = gate._github_gate(host, task, record, "/fixture", "main", ["tests", "matrix"])
        self.assertEqual(result.status, "red")
        self.assertIn("«matrix» was never posted", result.summary)

    def test_cancelled_workflow_dispatch_run_does_not_mask_a_green_pull_request_run(self):
        dispatch = {"id": 12, "event": "workflow_dispatch", "status": "completed", "conclusion": "cancelled"}
        checks = [_run_check("test", "success"), _run_check("test", "failure", run=12)]
        self.assertEqual(self._poll(CiAnswers(checks, [DONE_PR_RUN, dispatch]), ["test"]), ("SUCCESS", None))
        cancelled_pr = {**dispatch, "event": "pull_request"}
        rollup, _failed = self._poll(CiAnswers(checks, [DONE_PR_RUN, cancelled_pr]), ["test"])
        self.assertEqual(rollup, "FAILURE")


if __name__ == "__main__":
    unittest.main()
