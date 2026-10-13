"""Scripted command answers: no subprocess, Git, network or dispatcher state storage."""

from __future__ import annotations

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
        "gate repo view": (0, "org/repo", ""),
        "gate attestation base": (0, "b" * 40, ""),
    })
    # The two API questions share a label; answers follow their endpoint.
    original = host._answer
    def answer(args, label):
        if label == "gate gh api":
            payload = ('[{"id":1,"name":"tests","status":"COMPLETED","conclusion":"SUCCESS",'
                       '"details_url":"https://github.com/org/repo/actions/runs/7"}]') if args[2].endswith("check-runs") else "[]"
            return CompletedProcess(args, 0, payload, "")
        return original(args, label)
    host._answer = answer
    return host, {"ref": "p-1", "project": "p", "title": "Gate"}, SimpleNamespace(), sha
