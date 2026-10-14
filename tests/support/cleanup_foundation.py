"""Small deterministic persisted-output scenarios shared by the baseline and extracted journal."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
from pathlib import Path
from unittest import mock

from tests.support.cleanup_journal import head, record


def checkpoint(journal) -> dict:
    """Hash the actual persisted bytes, including JSON key order; omit inode/time/root identities."""
    return {
        "files": {path.relative_to(journal.path).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in sorted(journal.path.rglob("*.json"))},
        "archive": hashlib.sha256(journal.archive.read_bytes()).hexdigest() if journal.archive.exists() else None,
        "legacy": hashlib.sha256(journal.legacy.read_bytes()).hexdigest() if journal.legacy.exists() else None,
        "read": hashlib.sha256(json.dumps(journal.read(), sort_keys=True).encode()).hexdigest(),
        "summary": hashlib.sha256(json.dumps(journal.summary(), sort_keys=True).encode()).hexdigest(),
        "writes": dict(journal.writes),
    }


def snapshots(owner) -> dict:
    """Run identical publication, recovery and retained-evidence scenarios against either owner."""
    result = {}
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        journal = owner.CleanupJournal(root / "state")
        task = {"id": 1, "ref": "x-1", "project": "p", "state": "done", "sprint": "sprint:1"}
        raw = record("x-1", baseline=2, events=10)
        key = journal.remember(task, raw, disposition="done")
        result["remember"] = checkpoint(journal)
        journal.remember(task, raw, disposition="done")
        result["same-intent"] = checkpoint(journal)
        journal.generated(Path("/fixture/TASK.md"), "fixture body\n")
        journal.set_replay_cursor(key)
        reserved = journal.reserve_attempt(key, 100.0)
        result["reserved"] = checkpoint(journal)
        reserved["heads"] = [head("x-1", 0, baseline=2, events=10, exited=True), *reserved["heads"][1:]]
        journal.commit_intent(key, reserved)
        result["stop-receipt"] = checkpoint(journal)
        # A fresh owner gains admission identity while replay is unlocked; stale commit must refuse.
        old = journal.intent(key)
        identity = {"repo": "/fixture/repo", "workspace": raw["workspace"], "device": 1, "inode": 2}
        journal.remember(task, raw, identity=identity, disposition="done")
        try:
            journal.commit_intent(key, old)
        except owner.HostError as error:
            result["stale-error"] = str(error)
        else:
            raise AssertionError("stale publication was admitted")
        journal.defer_intent(key, old, "stale attempt retains matching exact stop receipt")
        result["stale-refusal"] = checkpoint(journal)
        # Malformed policy/binding must never compact into clean evidence.
        malformed = copy.deepcopy(raw)
        malformed["attempt_id"] = "malformed"
        malformed["worker_head_run"]["fanout_policy"] = {"version": 1, "state": "clean", "events": ["bad"]}
        journal.remember(task, malformed, disposition="done")
        result["malformed-policy"] = checkpoint(journal)
        # Source-state readers use the same current production-state record and observer generation.
        observer = {"id": 2, "ref": "sprint:2"}
        state = {"records": {"x-2": {"attempt_id": "source", "workspace": ""}},
                 "observers": {"sprint:2": {"generation": "g", "launches": 2, "workspace": ""}}}
        (journal.data_dir / "dispatcher" / "production-state.json").write_text(json.dumps(state))
        journal.request({"id": 3, "ref": "x-2", "project": "p"}, "archive")
        journal.observer_handoff(observer)
        result["source-state"] = checkpoint(journal)
        result["admission"] = {ref: journal.admission_refusal(ref) for ref in ("x-1", "x-2", "absent")}

        legacy = {**journal.read(), "version": 1}
        body = json.dumps(legacy).encode()
        for phase in ("migration", "copy-crash", "publish-crash", "archive-crash"):
            trial = owner.CleanupJournal(root / phase)
            trial.legacy.parent.mkdir(parents=True)
            trial.legacy.write_bytes(body)
            result[phase + ":read"] = checkpoint(trial)
            if phase == "copy-crash":
                replace = owner._replace_file
                calls = []

                def crash(path, data, calls=calls, replace=replace):
                    calls.append(path)
                    if len(calls) == 2:
                        raise OSError("copy interrupted")
                    replace(path, data)

                fault = mock.patch.object(owner, "_replace_file", side_effect=crash)
            elif phase == "publish-crash":
                fault = mock.patch.object(trial, "_publish", side_effect=OSError("publish interrupted"))
            elif phase == "archive-crash":
                fault = mock.patch.object(trial, "_archive_legacy", side_effect=OSError("archive interrupted"))
            else:
                fault = mock.patch.object(trial, "_published", wraps=trial._published)
            with fault:
                try:
                    trial.migrate()
                except OSError as error:
                    result[phase + ":error"] = str(error)
            result[phase + ":partial"] = checkpoint(trial)
            resumed = owner.CleanupJournal(trial.data_dir)
            resumed.migrate()
            result[phase + ":resumed"] = checkpoint(resumed)
            resumed.migrate()
            result[phase + ":idempotent"] = checkpoint(resumed)
    return result
