"""The run directories, card history and web app the head-view suites share (secretary-1703).

A support module rather than a test module, so the hermetic suite and the supervised one can both
stand on it without one test module importing another.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tests.web_fakes import Recording
from ummanu.runtime.head import HeadRun, HeadSpec, TaskRef
from ummanu.runtime.head.local_pty.journal import (
    INPUT_ACCEPTED,
    RUN_STARTED,
    TURN_FINISHED,
    TURN_STARTED,
    JournalWriter,
)
from ummanu.runtime.head_runtimes import LOCAL_PTY_RUNTIME, ORCA_LEGACY_RUNTIME
from ummanu.web.app import WebApp
from ummanu.webproto import sources
from ummanu.webproto.errors import TaskNotFound
from ummanu.webproto.journal import CommittedAudit
from ummanu.webproto.reads import ReadLayer

REF = "ummanu-9"
SPRINT = "sprint:1490"
OTHER = "ummanu-8"
#: The shape `memory.access.issue_grant` hands a head: a grant id, a dot, `token_urlsafe(32)`.
SECRET = "Zq3xY7abCDefGhIJkLmNoPqRsTuVwXyZ012345678_9"
TOKEN = "0f3c6f1e2d4b4a5c9e8d7c6b5a4f3e2d." + SECRET
WORKER = "a" * 32
REVIEWER = "b" * 32
FOREIGN = "c" * 32
LEGACY = "d" * 32
FINISHED_EARLIER = "e" * 32
OBSERVER = "f" * 32


def _instance(root: Path) -> Path:
    instance_dir = root / "instance"
    (instance_dir / "projects").mkdir(parents=True)
    data_dir = root / "data"
    (data_dir / "dispatcher").mkdir(parents=True)
    (instance_dir / "instance.yaml").write_text(
        "version: 1\nname: test\n"
        f"data_dir: {data_dir}\n"
        "offsite:\n  instance_remote: git@example.invalid:x/y.git\n",
        encoding="utf-8",
    )
    (instance_dir / "projects" / "ummanu.yaml").write_text(
        "id: ummanu\nrepo: /projects/ummanu\nenabled: true\nadapter: ummanu\ndefault_branch: main\n",
        encoding="utf-8",
    )
    return instance_dir


class _Audit:
    """The card audit owner's one read, over records the test hands it."""

    def __init__(self, history: dict[str, list[dict[str, Any]]]) -> None:
        self.history = history

    def events(self, ref: str) -> list[dict[str, Any]]:
        return list(self.history.get(ref, []))


def _routing(ref: str, *heads: tuple[str, str, str]) -> dict[str, Any]:
    return {
        "kind": "routing",
        "ref": ref,
        "payload": {
            "heads": [{"role": role, "head": head, "launch_id": run_id} for run_id, role, head in heads]
        },
    }


def _run(ref: str, run_id: str, *, role: str, runtime: str) -> dict[str, Any]:
    return HeadRun(
        run_id=run_id,
        spec=HeadSpec(profile_id=f"claude-{runtime}", adapter="claude", runtime=runtime),
        workspace="/w",
        task_ref=TaskRef.sprint(ref) if ref.startswith("sprint:") else TaskRef.card(ref),
        role=role,
        leaf=f"leaf-{run_id}",
    ).to_json()


class HeadViewFixture(unittest.TestCase):
    """One instance, a fake card history, run directories on disk, and the web app over them."""

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.instance = _instance(self.tmp)
        self.data_dir = self.tmp / "data"
        self.root = self.data_dir / "heads"
        self.root.mkdir(parents=True)
        self.cards = {REF, OTHER}
        self.history: dict[str, list[dict[str, Any]]] = {
            REF: [
                _routing(REF, (FINISHED_EARLIER, "worker", "claude-local-pty")),
                _routing(REF, (WORKER, "worker", "claude-local-pty")),
            ],
            OTHER: [_routing(OTHER, (FOREIGN, "worker", "claude-local-pty"))],
        }
        self.records: dict[str, Any] = {
            REF: {
                "attempt_id": "attempt-1",
                "state": "validate",
                "head": "claude-local-pty",
                "review_head": "codex-legacy",
                "worker_pid_file": str(self.tmp / "worker.pid"),
                "worker_head_run": _run(REF, WORKER, role="worker", runtime=LOCAL_PTY_RUNTIME),
                "review_pid_file": str(self.tmp / "review.pid"),
                "review_head_run": _run(REF, LEGACY, role="reviewer", runtime=ORCA_LEGACY_RUNTIME),
            }
        }
        self.observers: dict[str, Any] = {}
        self.write_production()
        test = self

        def events(_layer: ReadLayer, _data_dir: Path) -> CommittedAudit:
            return CommittedAudit(_Audit(test.history))

        def card_exists(_layer: ReadLayer, ref: str) -> None:
            if ref not in test.cards:
                raise TaskNotFound(f"the board holds no card {ref!r}")

        def card(_layer: ReadLayer, ref: str, _data_dir: Path, *, now: float):
            card_exists(_layer, ref)
            return {"ref": ref, "state": "in_progress", "project": "ummanu"}, sources.available(now)

        self.enterContext(mock.patch.object(ReadLayer, "_events", events))
        self.enterContext(mock.patch.object(ReadLayer, "_card_exists", card_exists))
        self.enterContext(mock.patch.object(ReadLayer, "_card", card))

    def write_production(self) -> None:
        (self.data_dir / "dispatcher" / "production-state.json").write_text(
            json.dumps({"phase": "production", "records": self.records, "observers": self.observers}),
            encoding="utf-8",
        )

    def layer(self) -> ReadLayer:
        return ReadLayer(self.instance, status_reader=dict, clock=lambda: 1_790_000_000.0)

    def app(self) -> WebApp:
        return WebApp(
            self.layer(),
            Recording(card_runs={"items": []}),
            Recording(),
            Recording(),
            Recording(),
            Recording(),
            Recording(),
            Recording(),
        )

    def run_dir(
        self,
        run_id: str,
        *,
        ref: str = REF,
        tail: bytes | None = None,
        subject: str = "a nudge",
        task: str | None = None,
    ) -> Path:
        """A finished run directory, optionally with an old tail to prove the page ignores it."""
        directory = self.root / run_id
        directory.mkdir(parents=True)
        with JournalWriter(directory / "journal.jsonl", run_id) as journal:
            journal.append(
                RUN_STARTED,
                head_pid=1,
                command=f"env UMMANU_MEMORY_ACCESS_TOKEN={TOKEN} claude --dangerously",
                environment={"UMMANU_MEMORY_ACCESS_TOKEN": TOKEN},
                role="worker",
                task=task or f"card:{ref}",
            )
            journal.append(INPUT_ACCEPTED, bytes=12, subject=subject)
            journal.append(TURN_STARTED, turn=1)
            journal.append(TURN_FINISHED, turn=1, reason="quiet")
        (directory / "supervisor.lock").write_text("")
        if tail is not None:
            (directory / "output.tail").write_bytes(tail)
        return directory

    def get(self, path: str) -> tuple[int, str]:
        response = self.app().handle("GET", path)
        return response.status, response.body.decode("utf-8")
