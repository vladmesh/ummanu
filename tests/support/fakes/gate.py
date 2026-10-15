"""Scripted command answers: no subprocess, Git, network or dispatcher state storage."""

from __future__ import annotations

import json
from subprocess import CompletedProcess
from types import SimpleNamespace


class ScriptedGateHost:
    def __init__(self, answers):
        self.answers = dict(answers)
        self.calls = []
        self.events = []

    def run_capture(self, args, label, *, cwd=None):
        self.calls.append((args, label, cwd, ""))
        self.events.append(label)
        return self._answer(args, label)

    def _run(self, args, label):
        return self.run_capture(args, label)

    def remote_git(self, project, workspace, args, label):
        full = ["git", "-C", workspace, *args]
        self.calls.append((full, label, None, project))
        self.events.append(label)
        return self._answer(full, label)

    def _answer(self, args, label):
        answer = self.answers[label]
        if isinstance(answer, Exception):
            raise answer
        code, stdout, stderr = answer
        return CompletedProcess(args, code, stdout, stderr)

    def commit_gate_published_ref(self, record, entry):
        self.events.append("commit publication")
        record.gate_published_ref = entry.to_json()

    def commit_gate_pr_authorship(self, record, entry):
        self.events.append("commit authorship")
        record.gate_pr_authorship = entry.to_json()


def github_fixture():
    sha = "a" * 40
    host = ScriptedGateHost({
        "gate head sha": (0, sha, ""),
        "gate remote branch sha": (0, "", ""),
        "gate publish branch": (0, "", ""),
        "gate pr list": (0, '[{"number":7,"baseRefName":"main"}]', ""),
        "gate pr view": (0, '{"title":"p-1: Gate","body":"A person wrote this."}', ""),
        "gate repo view": (0, "org/repo", ""),
        "gate attestation base": (0, "b" * 40, ""),
    })
    # The API questions share a label; answers follow their endpoint.
    original = host._answer
    def answer(args, label):
        if label == "gate gh api":
            if args[2].endswith("check-runs"):
                payload = ('{"total_count":1,"check_runs":[{"id":1,"name":"tests","status":"COMPLETED",'
                           '"conclusion":"SUCCESS","details_url":"https://github.com/org/repo/actions/runs/7"}]}')
            elif args[2].endswith("status"):
                payload = '{"total_count":0,"statuses":[]}'
            else:
                payload = ('{"total_count":1,"workflow_runs":[{"id":7,"event":"pull_request",'
                           '"status":"completed","conclusion":"success"}]}')
            return CompletedProcess(args, 0, payload, "")
        return original(args, label)
    host._answer = answer
    return host, {"ref": "p-1", "project": "p", "title": "Gate"}, SimpleNamespace(), sha


class CiAnswers(ScriptedGateHost):
    """`gh api` CI answers by endpoint: the check-runs, statuses and workflow runs of one sha.

    A string in `raw` replaces the whole stdout of the endpoint whose path ends with its key.
    """

    def __init__(self, check_runs=(), workflow_runs=(), statuses=(), raw=None):
        super().__init__({})
        self.lists = {"check-runs": ("check_runs", list(check_runs)), "status": ("statuses", list(statuses))}
        self.workflow_runs = list(workflow_runs)
        self.raw = dict(raw or {})

    def _answer(self, args, label):
        path = args[2].split("?", 1)[0]
        for suffix, text in self.raw.items():
            if path.endswith(suffix):
                return CompletedProcess(args, 0, text, "")
        key, items = next(
            (value for suffix, value in self.lists.items() if path.endswith(suffix)),
            ("workflow_runs", self.workflow_runs),
        )
        return CompletedProcess(args, 0, json.dumps({"total_count": len(items), key: items}), "")
