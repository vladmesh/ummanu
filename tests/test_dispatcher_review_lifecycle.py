from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ummanu.checkpoint import CheckpointResult
from ummanu.dispatch import host as dispatcher_host_module
from ummanu.dispatch.heartbeat import heartbeat_identity, run_heartbeat_identity
from ummanu.dispatch.host import CommandHostRuntime
from ummanu.dispatch.review import (
    recover_review_launch,
)
from ummanu.dispatch.runtime import DispatcherRuntime
from ummanu.dispatch.state import (
    DispatcherRecord,
)
from ummanu.dispatch.types import (
    STOPPED_BY_REPLACEMENT,
    STOPPED_BY_REVIEW_FREEZE,
    STOPPED_BY_REVIEW_VERDICT,
    STOPPED_BY_WATCHDOG,
    DispatcherError,
    HostError,
)
from ummanu.infra.checkpoint_run import load_checkpoint_state, run_checkpoint

GITHUB_FAILED_LOG_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "github_actions_failed_logs"
from tests.dispatcher_fixtures import (
    PromptAfterStartCatalog,
    RecordingReviewHost,
    SupervisedBackend,
    clear_env as _clear_env,
    supervised_run,
    write_heartbeat,
)
from tests.fakes.dispatcher import (
    FakeCatalog,
    FakeCheckpoint,
    FakeHost,
    FakePusher,
    dispatcher_seed,
)
from tests.integration_setup import require_disposable_board_fixture
from tests.sql_backend_fixtures import PostgresBoard, card_store
from ummanu.dispatch.types import (
    HeadLaunchAborted,
    review_pane_label,
)
from ummanu.dispatch.watchdog import (
    WORKER_REPORT_STALL_DEFAULT,
    bind_head_heartbeat,
    head_process_status,
    initial_output_stall_seconds,
    pid_file_path,
)
from ummanu.dispatch.worker_lifecycle import (
    WorkerContinuation,
    WorkerContinuationStage,
)
from ummanu.runtime.head import (
    HEAD_BUSY,
    DeliverReceipt,
    operations as head_ops,
    with_pid_heartbeat,
)
from ummanu.runtime.prompt_document import (
    NUDGE_MAX_BYTES,
    PromptDocumentError,
)
from ummanu.tasks import TaskReader, TaskWriter, task_audit_for


def setUpModule() -> None:
    """Confirm this CI shard can build its disposable board seam before tests run."""
    require_disposable_board_fixture(PostgresBoard.shared)


class PidHeartbeatTests(unittest.TestCase):
    """secretary-751: the pid a head writes for itself before it execs, and how the watchdog
    reads it back. This is the signal that distinguishes a live silent head from a shell left
    behind after the head exits, without reading terminal text, title, or a generic running flag.
    """

    write_heartbeat = staticmethod(write_heartbeat)

    def test_heartbeat_writes_an_atomic_versioned_identity_then_execs_the_head(self) -> None:
        wrapped = with_pid_heartbeat(
            "codex exec --dangerously-bypass-approvals-and-sandbox",
            "/tmp/x.pid",
            identity=heartbeat_identity(run_id="run-1", role="worker", task="card:ummanu-751"),
        )

        self.assertIn("python3 -P -c", wrapped)
        self.assertIn("os.replace", wrapped)
        self.assertIn("exec env codex exec --dangerously-bypass-approvals-and-sandbox", wrapped)

    def test_heartbeat_survives_a_leading_environment_assignment(self) -> None:
        """secretary-751 review: catalog commands from `head_launch` start with `NAME=value`, which
        bare `exec` cannot run directly. Executed through a real `/bin/sh` (not just string
        comparison), the wrapped command must still exec successfully and the pid file must end up
        holding the pid of the process that was actually running when it exited."""
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = os.path.join(tmp, "x.pid")
            wrapped = with_pid_heartbeat(
                'FOO=bar python3 -c "import os; print(os.getpid())"',
                pid_file,
                identity=heartbeat_identity(run_id="run-1", role="worker", task="card:ummanu-751"),
            )

            result = subprocess.run(
                ["/bin/sh", "-lc", wrapped],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            reported_pid = result.stdout.strip()
            heartbeat = json.loads(Path(pid_file).read_text(encoding="utf-8"))
            self.assertEqual(reported_pid, str(heartbeat["pid"]))
            self.assertEqual(heartbeat["version"], 1)
            self.assertEqual(heartbeat["run_id"], "run-1")

    def test_heartbeat_quotes_a_pid_file_path_with_spaces(self) -> None:
        wrapped = with_pid_heartbeat(
            "codex exec",
            "/tmp/weird dir/x.pid",
            identity=heartbeat_identity(run_id="run-1", role="worker", task="card:ummanu-751"),
        )

        self.assertIn(shlex.quote("/tmp/weird dir/x.pid"), wrapped)

    def test_pid_file_path_is_keyed_on_kind_and_reference_only(self) -> None:
        """A respawn in the same workspace must land on the same path as the launch before it, so
        clearing the file before a fresh launch actually removes the predecessor's pid."""
        self.assertEqual(
            pid_file_path("worker", "ummanu-751"),
            pid_file_path("worker", "ummanu-751"),
        )
        self.assertNotEqual(
            pid_file_path("worker", "ummanu-751"),
            pid_file_path("review", "ummanu-751"),
        )

    def test_pid_file_path_honours_the_body_dir_override(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"UMMANU_DISPATCHER_BODY_DIR": tmp}),
        ):
            self.assertTrue(pid_file_path("worker", "ummanu-751").startswith(tmp))

    def test_a_process_that_has_exited_is_not_alive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "head.pid"
            proc = subprocess.Popen(["true"])
            self.write_heartbeat(pid_file, proc.pid)
            proc.wait()

            status = head_process_status(str(pid_file))

            self.assertEqual(status["state"], "dead")

    def test_a_running_process_is_alive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "head.pid"
            proc = subprocess.Popen(["sleep", "5"])
            self.addCleanup(proc.wait)
            self.addCleanup(proc.terminate)
            self.write_heartbeat(pid_file, proc.pid)

            status = head_process_status(str(pid_file))

            self.assertEqual(status["state"], "live-match")
            self.assertFalse(status["stopped"])

    def test_a_stopped_matching_process_is_live_but_marked_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "head.pid"
            proc = subprocess.Popen(["sleep", "5"])
            self.addCleanup(proc.wait)
            self.addCleanup(proc.terminate)
            identity = heartbeat_identity(run_id="stopped-run", role="worker", task="card:ummanu-751")
            self.write_heartbeat(pid_file, proc.pid, identity=identity)
            os.kill(proc.pid, signal.SIGSTOP)
            try:
                # SIGSTOP is asynchronous from this test process.  Wait for the kernel state so
                # the assertion does not race the scheduler, and always resume before cleanup:
                # a stopped process cannot act on the cleanup SIGTERM.
                status = {}
                for _ in range(50):
                    status = head_process_status(str(pid_file), expected=identity)
                    if status.get("stopped"):
                        break
                    time.sleep(0.01)
                self.assertEqual(status["state"], "live-match")
                self.assertTrue(status["stopped"])
            finally:
                os.kill(proc.pid, signal.SIGCONT)

    def test_a_live_process_with_a_stale_start_or_run_is_an_identity_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "head.pid"
            proc = subprocess.Popen(["sleep", "5"])
            self.addCleanup(proc.wait)
            self.addCleanup(proc.terminate)
            identity = heartbeat_identity(
                run_id="run-a", role="worker", task="card:ummanu-751", leaf="leaf-a"
            )
            self.write_heartbeat(pid_file, proc.pid, identity=identity)
            raw = json.loads(pid_file.read_text(encoding="utf-8"))
            raw["proc_starttime_ticks"] = "0"
            pid_file.write_text(json.dumps(raw), encoding="utf-8")

            stale = head_process_status(str(pid_file), expected=identity)
            self.write_heartbeat(pid_file, proc.pid, identity=identity)
            foreign_run = head_process_status(
                str(pid_file),
                expected=heartbeat_identity(
                    run_id="run-b", role="worker", task="card:ummanu-751", leaf="leaf-a"
                ),
            )

            self.assertEqual(stale["state"], "identity-mismatch")
            self.assertEqual(foreign_run["state"], "identity-mismatch")

    def test_the_pane_leaf_is_bound_by_a_second_atomic_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "head.pid"
            proc = subprocess.Popen(["sleep", "5"])
            self.addCleanup(proc.wait)
            self.addCleanup(proc.terminate)
            identity = heartbeat_identity(run_id="bind-run", role="worker", task="card:ummanu-751")
            self.write_heartbeat(pid_file, proc.pid, identity=identity)

            self.assertTrue(bind_head_heartbeat(str(pid_file), expected=identity, leaf="leaf-a"))
            bound = head_process_status(str(pid_file), expected={**identity, "leaf": "leaf-a"})

            self.assertEqual(bound["state"], "live-match")
            self.assertEqual(bound["record"]["leaf"], "leaf-a")

    def test_a_leaf_handoff_before_the_writer_binds_each_dispatcher_role(self) -> None:
        """Terminal create may return before the shell reaches the heartbeat preamble.

        Worker, reviewer and observer share the writer, but their HeadRun bindings differ.  The
        handoff must make the first durable base record carry the returned leaf for all three.
        """
        roles = (
            ("worker", "card:ummanu-1424", "leaf-worker"),
            ("reviewer", "card:ummanu-1424", "leaf-reviewer"),
            ("observer", "sprint:ummanu-1424", "leaf-observer"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            for role, task, leaf in roles:
                with self.subTest(role=role):
                    pid_file = Path(tmp) / f"{role}.pid"
                    identity = heartbeat_identity(run_id=f"{role}-race", role=role, task=task)
                    # This is the create-return / writer-not-yet-observable ordering.  The bind
                    # cannot see a base record, but leaves a durable handoff for the shell.
                    self.assertTrue(bind_head_heartbeat(str(pid_file), expected=identity, leaf=leaf))
                    wrapped = with_pid_heartbeat(
                        "python3 -c 'import time; time.sleep(5)'",
                        str(pid_file),
                        identity=identity,
                    )
                    proc = subprocess.Popen(["/bin/sh", "-lc", wrapped])
                    try:
                        deadline = time.monotonic() + 2
                        status: dict[str, object] = {}
                        while time.monotonic() < deadline:
                            status = head_process_status(str(pid_file), expected={**identity, "leaf": leaf})
                            if status.get("state") == "live-match":
                                break
                            time.sleep(0.01)
                        self.assertEqual(status.get("state"), "live-match")
                        self.assertEqual(status["record"]["leaf"], leaf)  # type: ignore[index]
                    finally:
                        proc.terminate()
                        proc.wait(timeout=5)

    def test_a_pid_file_that_has_not_been_written_yet_is_not_known(self) -> None:
        """A fresh launch has not run its `echo $$` yet, and a raw
        `UMMANU_DISPATCHER_*_COMMAND` override never will. Neither is evidence of death."""
        with tempfile.TemporaryDirectory() as tmp:
            status = head_process_status(str(Path(tmp) / "never-written.pid"))

        self.assertEqual(status["state"], "not-yet-written")

    def test_garbage_pid_file_contents_are_not_known(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "head.pid"
            pid_file.write_text("not-a-pid\n", encoding="utf-8")

            status = head_process_status(str(pid_file))

        self.assertEqual(status["state"], "unreadable")


class NudgingReviewHost(RecordingReviewHost):
    """A bring-up whose heads take their prompt after they are up, so the launch delivery runs.

    Either role's launch runs through it. Both are nudged at a task document — the reviewer at its
    review, the worker at the TASK.md in its checkout — and the rule under test is the same rule.
    """

    def __init__(self, root: Path, **kwargs) -> None:
        super().__init__(root, catalog=PromptAfterStartCatalog(), **kwargs)


class ReviewNudgeDeliveryTests(unittest.TestCase):
    """secretary-1409: the reviewer is nudged at a task document, never handed the review itself.

    A ~12 KiB review typed into a Codex pane is what produced 24 consecutive
    `payload-left-in-composer` failures on `codegen-orchestrator-1165` and stopped two products.
    The rule that replaces it: the input channel carries only bounded pointers, and content lives in
    a file. What the head's backend is handed is the pointer (`SupervisedBackend` records it).
    """

    # An ESC, a bracketed-paste terminator and the CRLF the board's web form submits — all of it
    # arriving the way it really does, inside the card description the review prompt renders.
    HOSTILE_DESCRIPTION = "spec\r\n\x1b[201~ terminator\r\n\x1b[200~ opener\r\n\x1b]0;retitle\x07\r\n"

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.card_audit = task_audit_for(card_store(self, dispatcher_seed()))
        _clear_env(self, "UMMANU_DISPATCHER_REVIEW_COMMAND")
        _clear_env(self, "UMMANU_DISPATCHER_PROMPT_DIR")
        os.environ["UMMANU_DISPATCHER_BODY_DIR"] = str(self.root)
        # No provider session file may name this workspace, so the confirmation falls back to the
        # screen the host paints rather than reading the developer's own codex sessions.
        os.environ["UMMANU_CODEX_SESSIONS"] = str(self.root / "sessions")
        self.addCleanup(os.environ.pop, "UMMANU_CODEX_SESSIONS", None)
        self.task = {
            "ref": "ummanu-1409",
            "project": "ummanu",
            "description": self.HOSTILE_DESCRIPTION,
            "workspace": {"base_branch": "main"},
            "routing": {},
        }

    def _record(self) -> DispatcherRecord:
        return DispatcherRecord(
            worker="ummanu-1409-w",
            workspace=str(self.workspace),
            handle="run:worker-1409",
            head="codex",
            review_head="codex-reviewer",
            attempt_id="attempt-1",
            comment_baseline=0,
            review_baseline=0,
            state="review_starting",
            claimed_at=0.0,
            worker_head_run=supervised_run(
                "worker-1409", workspace=str(self.workspace), task_ref=head_ops.TaskRef.card("ummanu-1409")
            ),
        )

    def _document_of(self, host: NudgingReviewHost) -> Path:
        return host._prompt_document_path("review", self.task["ref"], 0)

    def _checkout_contents(self) -> dict[str, bytes]:
        return {
            str(path.relative_to(self.workspace)): path.read_bytes()
            for path in sorted(self.workspace.rglob("*"))
            if path.is_file() and not path.relative_to(self.workspace).is_relative_to(".ummanu-task-env")
        }

    def test_the_head_receives_a_bounded_pointer_and_never_the_review(self) -> None:
        host = NudgingReviewHost(self.root)
        host.audit = self.card_audit

        host.start_review(self.task, self._record())

        document = self._document_of(host)
        [nudge] = host.pointers()
        self.assertLessEqual(len(nudge.encode("utf-8")), NUDGE_MAX_BYTES)
        self.assertEqual(nudge.splitlines(), [nudge], "the head is given one line")
        self.assertNotIn("\x1b", nudge)
        self.assertIn(str(document), nudge)
        self.assertTrue(document.is_absolute())
        # The review itself never reaches the head's input, hostile bytes included.
        self.assertNotIn("\r", nudge)
        self.assertNotIn("BLOCKER-", nudge, "the review prompt's own text stayed on disk")
        self.assertNotIn("terminator", nudge)

    def test_the_document_holds_the_whole_review_outside_the_checkout(self) -> None:
        host = NudgingReviewHost(self.root)
        host.audit = self.card_audit

        host.start_review(self.task, self._record())

        document = self._document_of(host)
        body = document.read_text(encoding="utf-8")
        self.assertIn("# Review ummanu-1409", body)
        self.assertIn("\x1b[201~ terminator", body, "the description reaches the head unmodified")
        self.assertNotIn(
            str(self.workspace.resolve()),
            str(document.resolve()),
            "a prompt inside the checkout would move the identity receipts hash",
        )
        self.assertEqual(oct(document.stat().st_mode & 0o777), oct(0o600))
        self.assertFalse(
            (self.workspace / "REVIEW.md").exists(),
            "the review packet is the document, and it does not live in the worktree",
        )

    def test_the_bring_up_does_not_touch_the_candidate_checkout(self) -> None:
        """Preparing a prompt is not a licence to edit the candidate files under review.

        A `REVIEW.md` in the workspace can be a tracked part of a candidate as easily as a packet
        left by a dispatcher that predates this seam, and the nudge names an absolute path, so
        nothing needs deleting to be unambiguous. Removing it would be the same identity change the
        document-outside-the-worktree rule exists to prevent, made by the code enforcing that rule.
        """
        (self.workspace / "REVIEW.md").write_text("a candidate's own file\n", encoding="utf-8")
        (self.workspace / "src.py").write_text("print('candidate')\n", encoding="utf-8")
        before = self._checkout_contents()
        host = NudgingReviewHost(self.root)
        host.audit = self.card_audit

        host.start_review(self.task, self._record())

        self.assertEqual(self._checkout_contents(), before)
        self.assertTrue((self.workspace / ".ummanu-task-env" / "owner.json").is_file())

    def test_a_retry_rewrites_the_same_document_and_sends_a_fresh_nudge(self) -> None:
        """The pointer always names the round's current task, so a retry cannot review a stale one."""
        host = NudgingReviewHost(self.root)
        host.audit = self.card_audit
        host.start_review(self.task, self._record())
        first = list(host.pointers())

        self.task["description"] = "the card was edited between attempts"
        host.start_review(self.task, self._record())

        document = self._document_of(host)
        self.assertIn("the card was edited between attempts", document.read_text(encoding="utf-8"))
        self.assertEqual(
            host.pointers(),
            first * 2,
            "the same path is nudged again rather than a second document being written",
        )
        self.assertEqual(sorted(path.name for path in document.parent.iterdir()), ["review-0.md"])

    def test_a_second_round_gets_its_own_document(self) -> None:
        host = NudgingReviewHost(self.root)
        host.audit = self.card_audit
        record = self._record()
        record.review_baseline = 1

        host.start_review(self.task, record)

        self.assertTrue(host._prompt_document_path("review", self.task["ref"], 1).is_file())
        self.assertFalse(self._document_of(host).exists())

    def test_a_retained_reviewer_is_nudged_again_as_the_exact_run_its_intent_names(self) -> None:
        """A bring-up whose head is up and did not take its prompt keeps the head, and the next
        tick's retry is delivered to that same run at the same document, never to a replacement."""
        host = NudgingReviewHost(self.root)
        host.audit = self.card_audit
        host.backend.start_failure = head_ops.HeadSpawnAborted("the prompt did not start a turn", run=None)  # type: ignore[arg-type]

        with self.assertRaises(HeadLaunchAborted) as caught:
            host.start_review(self.task, self._record())
        self.assertEqual(host.backend.stops, [], "a head that is up is never stopped here")
        intent = {
            "role": "review",
            "workspace": str(self.workspace),
            "handle": caught.exception.handle,
            "leaf": caught.exception.leaf,
            "pid_file": caught.exception.pid_file,
            "head_run": dict(caught.exception.head_run),
        }

        retried = host.nudge_review_delivery(self.task, self._record(), intent)

        [(run, pointer, _subject)] = host.backend.deliveries
        self.assertEqual(run.run_id, caught.exception.head_run["run_id"])
        self.assertEqual(retried["head_run"]["run_id"], caught.exception.head_run["run_id"])
        self.assertIn(str(self._document_of(host)), pointer.text)
        self.assertEqual(host.backend.stops, [])

    def test_a_document_that_cannot_be_written_stops_the_bring_up_before_any_pane(self) -> None:
        """An unprompted reviewer would sit at its prompt forever; the caller's infrastructure
        retry is the right answer to a launch that never started."""
        host = NudgingReviewHost(self.root)
        host.audit = self.card_audit
        with (
            mock.patch.object(
                dispatcher_host_module,
                "_write_prompt_document",
                side_effect=PromptDocumentError("read-only artifacts directory"),
            ),
            self.assertRaises(HostError) as caught,
        ):
            host.start_review(self.task, self._record())

        self.assertIn("task document could not be prepared", str(caught.exception))
        self.assertEqual(host.backend.starts, [], "no head is started with nothing to read")


class WorkerNudgeDeliveryTests(unittest.TestCase):
    """secretary-1410: the same invariant, on the bring-up that was still killing its own heads.

    The worker's launch prompt has always been a pointer at the TASK.md written into its checkout —
    the reviewer's rule applied to the other role — but the bring-up was never told so, and answered
    an unconfirmed delivery by closing the pane. On 2026-08-11 that closed six consecutive live
    Claude workers on `codegen-orchestrator-1166`, each twelve seconds after it had started, taken
    its prompt and begun work: the transcripts they left behind are the proof they were healthy.
    What made the classification wrong is fixed elsewhere in this card; what this class fixes is
    that a wrong classification could carry that verdict at all.

    A bring-up whose head is up and did not take its prompt is handed back as `HeadLaunchAborted`
    (`tests.test_dispatcher_launch_intent`); what is held here is why that is safe.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.card_audit = task_audit_for(card_store(self, dispatcher_seed()))
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        _clear_env(self, "UMMANU_DISPATCHER_WORKER_COMMAND")
        os.environ["UMMANU_DISPATCHER_BODY_DIR"] = str(self.root)
        # Neither provider may have a session file naming this workspace, so the delivery falls
        # back to the screen the host paints instead of the developer's own transcripts.
        os.environ["UMMANU_CODEX_SESSIONS"] = str(self.root / "sessions")
        os.environ["UMMANU_CLAUDE_PROJECTS"] = str(self.root / "claude-projects")
        self.addCleanup(os.environ.pop, "UMMANU_CODEX_SESSIONS", None)
        self.addCleanup(os.environ.pop, "UMMANU_CLAUDE_PROJECTS", None)
        self.task = {
            "ref": "ummanu-1410",
            "project": "ummanu",
            "description": "a card with an \x1b[201~ terminator in it",
            "workspace": {"base_branch": "main"},
            "routing": {},
        }

    def _record(self) -> DispatcherRecord:
        return DispatcherRecord(
            worker="ummanu-1410-w",
            workspace=str(self.workspace),
            handle="term-worker",
            head="claude-opus",
            review_head="codex-reviewer",
            attempt_id="attempt-1",
            comment_baseline=0,
            review_baseline=0,
            state="claimed",
            claimed_at=0.0,
        )

    def test_the_task_the_head_was_pointed_at_is_on_disk_whatever_the_classification_said(
        self,
    ) -> None:
        """Why not stopping it is safe: the pointer named a file, and the file is there.

        A head that took the nudge has its whole task; a head that did not can be nudged again at
        the same path next tick. Nothing about the round depends on the head having answered.
        """
        host = NudgingReviewHost(self.root)
        host.audit = self.card_audit
        host.backend.start_failure = head_ops.HeadSpawnAborted("the prompt did not start a turn", run=None)  # type: ignore[arg-type]

        with self.assertRaises(HeadLaunchAborted) as caught:
            host.restart_worker(self.task, self._record())

        self.assertEqual(caught.exception.workspace, str(self.workspace))
        self.assertEqual(host.backend.stops, [], "the worker survives an unconfirmed nudge")
        body = (self.workspace / "TASK.md").read_text(encoding="utf-8")
        self.assertIn("ummanu-1410", body)
        self.assertIn("\x1b[201~ terminator", body, "the card reaches the head unmodified")
        [start] = host.backend.starts
        self.assertNotIn("terminator", start["pointer"].text, "the head got the pointer, not the card")
        self.assertEqual(start["pointer"].document, str(self.workspace / "TASK.md"))


class WorkerLifecycleTests(unittest.TestCase):
    """secretary-1412: the production worker path runs on `spawn` / `nudge` / `stop`.

    The head's backend owns its life (`local-pty` since secretary-1722), and the dispatcher's job is
    what only it can do: render the command, write the document, hand over the pointer, and say who
    is ending a head. What is asserted here is one run identity from bring-up to stop, and an
    initiator that is on the record afterwards and survives being written down.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.card_audit = task_audit_for(card_store(self, dispatcher_seed()))
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        _clear_env(self, "UMMANU_DISPATCHER_WORKER_COMMAND")
        os.environ["UMMANU_DISPATCHER_BODY_DIR"] = str(self.root)
        os.environ["UMMANU_CODEX_SESSIONS"] = str(self.root / "sessions")
        os.environ["UMMANU_CLAUDE_PROJECTS"] = str(self.root / "claude-projects")
        self.addCleanup(os.environ.pop, "UMMANU_CODEX_SESSIONS", None)
        self.addCleanup(os.environ.pop, "UMMANU_CLAUDE_PROJECTS", None)
        self.task = {
            "ref": "ummanu-1412",
            "project": "ummanu",
            "description": "a card",
            "workspace": {"base_branch": "main"},
            "routing": {},
        }

    def _record(self, **kwargs) -> DispatcherRecord:
        record = DispatcherRecord(
            worker="ummanu-1412-w",
            workspace=str(self.workspace),
            handle="term-worker",
            head="codex",
            review_head="codex-reviewer",
            attempt_id="attempt-1",
            comment_baseline=0,
            review_baseline=0,
            state="claimed",
            claimed_at=0.0,
        )
        for name, value in kwargs.items():
            setattr(record, name, value)
        return record

    def test_a_worker_bring_up_hands_back_the_run_that_head_is(self) -> None:
        host = NudgingReviewHost(self.root)
        host.audit = self.card_audit

        launched = host.restart_worker(self.task, self._record())

        run = launched.head_run
        self.assertTrue(run["run_id"], "the head has an identity of its own")
        self.assertEqual(run["lifecycle"], "working", "it was given its task")
        self.assertEqual(run["handle"], launched.handle)
        self.assertEqual(run["head_runtime"], "local-pty")
        self.assertEqual(
            run["task_ref"],
            {
                "kind": "card",
                "ref": "ummanu-1412",
                "document": str(self.workspace / "TASK.md"),
            },
        )
        self.assertEqual(run["spec"]["adapter"], "codex")
        [start] = host.backend.starts
        self.assertEqual(start["workspace"], str(self.workspace))

    def _running_worker(self, **fields) -> DispatcherRecord:
        """A worker the record holds a live `local-pty` run of, with its heartbeat answering."""
        record = self._record(
            worker_pid_file=str(self.root / "w.pid"),
            worker_run={"adapter": "codex", "codex_mode": "tui"},
            **fields,
        )
        record.worker_head_run = supervised_run(
            "worker-running-run",
            workspace=str(self.workspace),
            task_ref=head_ops.TaskRef.card(self.task["ref"]),
            handle=record.handle,
            pid_file=record.worker_pid_file,
        )
        PidHeartbeatTests.write_heartbeat(
            Path(record.worker_pid_file),
            os.getpid(),
            identity=run_heartbeat_identity(record.worker_head_run, role="worker"),
        )
        (self.workspace / "TASK.md").write_text("task\n", encoding="utf-8")
        return record

    def test_the_worker_report_prompt_goes_to_the_run_the_record_names(self) -> None:
        host = NudgingReviewHost(self.root)
        host.audit = self.card_audit
        record = self._running_worker(report_generation=2)

        host.prompt_worker_report(self.task, record)

        [(run, pointer, subject)] = host.backend.deliveries
        self.assertEqual(run.run_id, "worker-running-run")
        self.assertEqual(subject, "worker-report")
        self.assertIn("generation 2", pointer.text)
        self.assertEqual(record.worker_head_run["run_id"], "worker-running-run")
        self.assertEqual(record.worker_head_run["lifecycle"], "working")

    def test_a_busy_continuation_wait_does_not_signal_the_retained_worker(self) -> None:
        """The signal is the delivery's own pre-send step, which a busy head never reaches."""
        host = NudgingReviewHost(self.root)
        host.audit = self.card_audit
        host.backend.deliver_refusal = DeliverReceipt(
            status=HEAD_BUSY, reason="the head is in a turn", evidence={"readiness_state": "busy"}
        )
        record = self._running_worker(
            report_generation=2,
            worker_continuation=WorkerContinuation(
                stage=WorkerContinuationStage.DELIVERY_PENDING,
                phase="review",
                session_held=True,
                sent_at=time.time(),
            ),
        )

        with (
            mock.patch.object(
                host,
                "_head_status",
                return_value={
                    "known": True,
                    "alive": True,
                    "match": True,
                    "state": "live-match",
                    "stopped": True,
                },
            ),
            mock.patch.object(host, "_signal_head") as signal_head,
            self.assertRaises(HostError),
        ):
            host.resume_worker(self.task, record)

        signal_head.assert_not_called()
        self.assertEqual(host.backend.deliveries, [])
        self.assertEqual(record.worker_head_run["run_id"], "worker-running-run")

    def test_a_stopped_worker_records_who_stopped_it_and_that_survives_a_restart(self) -> None:
        host = NudgingReviewHost(self.root)
        host.audit = self.card_audit
        record = self._running_worker()

        host.stop_head(record, "worker", STOPPED_BY_REVIEW_FREEZE)

        self.assertEqual(host.backend.stops, [("worker-running-run", STOPPED_BY_REVIEW_FREEZE)])
        self.assertEqual(record.worker_head_run["lifecycle"], "exited")
        self.assertEqual(record.worker_head_run["stopped_by"]["actor"], STOPPED_BY_REVIEW_FREEZE)
        restarted = DispatcherRecord.from_json(json.loads(json.dumps(record.to_json())))
        self.assertEqual(restarted.worker_head_run["stopped_by"]["actor"], STOPPED_BY_REVIEW_FREEZE)

    def test_the_run_identity_is_the_same_one_from_bring_up_to_stop(self) -> None:
        host = NudgingReviewHost(self.root)
        host.audit = self.card_audit
        record = self._record()

        launched = host.restart_worker(self.task, record)
        record.worker_head_run = dict(launched.head_run)
        record.handle = launched.handle
        record.worker_leaf = launched.leaf

        host.stop_head(record, "worker", STOPPED_BY_REPLACEMENT)

        self.assertEqual(host.backend.stops, [(launched.head_run["run_id"], STOPPED_BY_REPLACEMENT)])
        self.assertEqual(record.worker_head_run["run_id"], launched.head_run["run_id"])
        self.assertEqual(record.worker_head_run["lifecycle"], "exited")


class ReviewerLifecycleTests(unittest.TestCase):
    """secretary-1414: the reviewer path runs on `spawn` / `nudge` / `stop`, like the worker's.

    The reviewer is the head this dispatcher stops from the most places, and until it had a durable
    run of its own every one of those stops left the same record behind: a reviewer that was simply
    gone. What is asserted here is the run — one identity from bring-up to stop, with its initiator
    written down — and the one thing the reviewer's stop must keep doing differently: ending the
    reviewer and nothing else in the worker's worktree.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.card_audit = task_audit_for(card_store(self, dispatcher_seed()))
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        _clear_env(self, "UMMANU_DISPATCHER_REVIEW_COMMAND")
        _clear_env(self, "UMMANU_DISPATCHER_PROMPT_DIR")
        os.environ["UMMANU_DISPATCHER_BODY_DIR"] = str(self.root)
        os.environ["UMMANU_CODEX_SESSIONS"] = str(self.root / "sessions")
        self.addCleanup(os.environ.pop, "UMMANU_CODEX_SESSIONS", None)
        self.task = {
            "ref": "ummanu-1414",
            "project": "ummanu",
            "description": "a card",
            "workspace": {"base_branch": "main"},
            "routing": {},
        }

    def _record(self, **fields) -> DispatcherRecord:
        record = DispatcherRecord(
            worker="ummanu-1414-w",
            workspace=str(self.workspace),
            handle="run:worker-1414",
            head="codex",
            review_head="codex-reviewer",
            attempt_id="attempt-1",
            comment_baseline=0,
            review_baseline=0,
            state="reviewing",
            claimed_at=0.0,
            worker_head_run=supervised_run(
                "worker-1414", workspace=str(self.workspace), task_ref=head_ops.TaskRef.card("ummanu-1414")
            ),
        )
        for name, value in fields.items():
            setattr(record, name, value)
        return record

    def _stored_run(self, **fields) -> dict:
        """A reviewer run as a previous tick wrote it down, before this one reads it back."""
        run = {
            "run_id": "run-reviewer-1",
            "spec": {"profile_id": "codex-reviewer", "adapter": "codex"},
            "head_runtime": "local-pty",
            "workspace": str(self.workspace),
            "task_ref": {"kind": "card", "ref": "ummanu-1414", "document": ""},
            "handle": "run:run-reviewer-1",
            "leaf": "",
            "pid_file": "",
            "lifecycle": "working",
            "stopped_by": {},
        }
        run.update(fields)
        return run

    def test_a_reviewer_bring_up_hands_back_the_run_that_head_is(self) -> None:
        host = NudgingReviewHost(self.root)
        host.audit = self.card_audit

        launch = host.start_review(self.task, self._record())

        run = launch.head_run
        self.assertTrue(run["run_id"], "the reviewer has an identity of its own")
        self.assertEqual(run["lifecycle"], "working", "it was given its review")
        self.assertEqual(run["handle"], launch.handle)
        self.assertEqual(run["leaf"], launch.leaf)
        self.assertEqual(run["spec"]["profile_id"], "codex-reviewer")
        self.assertEqual(run["task_ref"]["ref"], "ummanu-1414")
        # A second head in the worker's own checkout, never a workspace of its own.
        [start] = host.backend.starts
        self.assertEqual(start["workspace"], str(self.workspace))
        self.assertEqual(start["title"], review_pane_label("ummanu-1414"))
        self.assertFalse([call for call in host.calls if "worktree" in call and "add" in call])

    def test_a_stopped_reviewer_records_who_stopped_it_and_that_survives_a_restart(self) -> None:
        host = RecordingReviewHost(self.root)
        host.audit = self.card_audit
        record = self._record(review_handle="run:run-reviewer-1", review_head_run=self._stored_run())

        host.stop_review(record, STOPPED_BY_REVIEW_VERDICT)

        self.assertEqual(host.backend.stops, [("run-reviewer-1", STOPPED_BY_REVIEW_VERDICT)])
        self.assertEqual(record.review_head_run["lifecycle"], "exited")
        self.assertEqual(record.review_head_run["stopped_by"]["actor"], STOPPED_BY_REVIEW_VERDICT)
        restarted = DispatcherRecord.from_json(json.loads(json.dumps(record.to_json())))
        self.assertEqual(restarted.review_head_run["stopped_by"]["actor"], STOPPED_BY_REVIEW_VERDICT)

    def test_a_refused_reviewer_stop_reaches_the_caller_and_is_not_recorded_as_exited(self) -> None:
        host = RecordingReviewHost(self.root)
        host.audit = self.card_audit
        host.backend.stop_refusal = "the head's process outlived the stop it was sent"
        record = self._record(review_handle="run:run-reviewer-1", review_head_run=self._stored_run())

        with self.assertRaisesRegex(HostError, "outlived the stop"):
            host.stop_review(record, STOPPED_BY_WATCHDOG)

        self.assertEqual(host.backend.stops, [("run-reviewer-1", STOPPED_BY_WATCHDOG)])
        self.assertNotEqual(record.review_head_run["lifecycle"], "exited")

    def test_stopping_the_reviewer_leaves_the_workers_checkout_alone(self) -> None:
        """A red verdict hands the worktree back to the worker moments later. A reviewer stop that
        reached for the workspace would take the worker the card is about to resume with it."""
        host = RecordingReviewHost(self.root)
        worker_run = supervised_run(
            "run-worker-1", workspace=str(self.workspace), task_ref=head_ops.TaskRef.card("ummanu-1414")
        )
        record = self._record(
            handle="run:run-worker-1",
            worker_head_run=worker_run,
            review_handle="run:run-reviewer-1",
            review_head_run=self._stored_run(),
        )

        host.stop_review(record, STOPPED_BY_REVIEW_VERDICT)

        self.assertEqual(host.backend.stops, [("run-reviewer-1", STOPPED_BY_REVIEW_VERDICT)])
        self.assertEqual(record.worker_head_run, worker_run, "the worker's own run was not touched")
        self.assertFalse([call for call in host.calls if "worktree" in call], "the worktree was not touched")


class ReviewLivenessTests(unittest.TestCase):
    """Which pane counts as "the reviewer" for lifecycle checks."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.task = {"ref": "ummanu-651", "project": "ummanu", "routing": {}}
        _clear_env(self, "UMMANU_DISPATCHER_BODY_DIR")
        os.environ["UMMANU_DISPATCHER_BODY_DIR"] = str(self.root)

    def _dead_pid(self) -> int:
        proc = subprocess.Popen(["true"])
        proc.wait()
        return proc.pid

    def _live_pid(self) -> int:
        proc = subprocess.Popen(["sleep", "5"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.terminate)
        return proc.pid

    def _host(self, terminals: list[dict]) -> RecordingReviewHost:
        return RecordingReviewHost(self.root, terminals=terminals)

    def _record(self, **fields) -> DispatcherRecord:
        record = DispatcherRecord(
            worker="ummanu-651-w",
            workspace=str(self.workspace),
            handle="term-worker",
            head="codex",
            review_head="codex-reviewer",
            attempt_id="attempt-1",
            comment_baseline=0,
            review_baseline=0,
            state="reviewing",
            claimed_at=0.0,
        )
        for name, value in fields.items():
            setattr(record, name, value)
        record.worker_head_run = head_ops.HeadRun(
            run_id="worker-liveness-run",
            spec=head_ops.HeadSpec(profile_id=record.head, adapter="codex"),
            workspace=record.workspace,
            task_ref=head_ops.TaskRef.card(self.task["ref"]),
            handle=record.handle,
            leaf=record.worker_leaf,
            pid_file=pid_file_path("worker", self.task["ref"]),
        ).to_json()
        record.review_head_run = head_ops.HeadRun(
            run_id="review-liveness-run",
            spec=head_ops.HeadSpec(profile_id=record.review_head, adapter="codex"),
            workspace=record.workspace,
            task_ref=head_ops.TaskRef.card(self.task["ref"]),
            handle=record.review_handle,
            leaf=record.review_leaf,
            pid_file=pid_file_path("review", self.task["ref"]),
        ).to_json()
        return record

    def _write_heartbeat(self, kind: str, pid: int, record: DispatcherRecord | None = None) -> None:
        record = record or self._record()
        run = record.review_head_run if kind == "review" else record.worker_head_run
        leaf = record.review_leaf if kind == "review" else record.worker_leaf
        heartbeat = run_heartbeat_identity(run, role=kind, task=f"card:{self.task['ref']}", leaf=leaf)
        heartbeat.update({"version": 1, "pid": pid})
        stat_path = Path(f"/proc/{pid}/stat")
        if stat_path.exists():
            stat = stat_path.read_text(encoding="utf-8")
            heartbeat.update(
                {
                    "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip(),
                    "proc_starttime_ticks": stat[stat.rfind(")") + 2 :].split()[19],
                }
            )
        else:
            heartbeat.update({"boot_id": "dead-process", "proc_starttime_ticks": "0"})
        Path(pid_file_path(kind, self.task["ref"])).write_text(json.dumps(heartbeat), encoding="utf-8")

    def test_disconnected_reviewer_pane_is_not_running(self) -> None:
        host = self._host(
            [
                {"handle": "term-review", "leafId": "leaf-review", "connected": False},
            ]
        )

        status = host.review_status(self.task, self._record(review_handle="term-review"))
        self.assertFalse(status["live"])

    def test_disconnected_pane_preserves_a_foreign_heartbeat_fence_for_both_roles(self) -> None:
        """The shared status seam reads identity before an inventory result can authorize a
        replacement. A disconnected pane must not hide a live process from another HeadRun."""
        for kind, status_name, record_fields, terminal in (
            (
                "worker",
                "worker_status",
                {"worker_leaf": "leaf-worker"},
                {"handle": "term-worker", "leafId": "leaf-worker", "connected": False},
            ),
            (
                "review",
                "review_status",
                {"review_handle": "term-review", "review_leaf": "leaf-review"},
                {"handle": "term-review", "leafId": "leaf-review", "connected": False},
            ),
        ):
            with self.subTest(kind=kind):
                record = self._record(**record_fields)
                self._write_heartbeat(kind, self._live_pid(), record)
                path = Path(pid_file_path(kind, self.task["ref"]))
                heartbeat = json.loads(path.read_text(encoding="utf-8"))
                heartbeat["run_id"] = f"foreign-{kind}-run"
                path.write_text(json.dumps(heartbeat), encoding="utf-8")

                status = getattr(self._host([terminal]), status_name)(self.task, record)

                self.assertTrue(status["live"])
                self.assertTrue(status["identity_mismatch"])
                self.assertEqual(status["reason"], "heartbeat-identity-mismatch")

    def test_worker_pane_is_never_mistaken_for_the_reviewer(self) -> None:
        host = self._host(
            [
                {"handle": "term-worker", "leafId": "leaf-worker", "title": "codex", "connected": True},
            ]
        )

        status = host.review_status(self.task, self._record(review_handle="term-review"))
        self.assertFalse(status["live"])

    def test_foreign_reviewer_heartbeat_cannot_adopt_a_review_launch(self) -> None:
        """Recovery sees full status, so a live foreign PID is not a reviewing reviewer."""
        record = self._record(review_handle="term-review", review_leaf="leaf-review")
        record.state = "review_starting"
        record.review_launch_aborts = 2
        record.review_infra_failures = 3
        record.review_infra_error = "previous launch failure"
        foreign = self._live_pid()
        self._write_heartbeat("review", foreign, record)
        path = Path(pid_file_path("review", self.task["ref"]))
        heartbeat = json.loads(path.read_text(encoding="utf-8"))
        heartbeat["run_id"] = "foreign-reviewer-run"
        path.write_text(json.dumps(heartbeat), encoding="utf-8")
        host = self._host(
            [
                {"handle": "term-review", "leafId": "leaf-review", "connected": True},
            ]
        )
        runtime = mock.Mock()
        runtime.host = host

        status = host.review_status(self.task, record)
        outcome = recover_review_launch(
            runtime,
            self.task,
            {self.task["ref"]: record},
            record,
            "attempt-1",
            payload={},
        )

        self.assertTrue(status["live"])
        self.assertTrue(status["identity_mismatch"])
        self.assertFalse(status["live"] and not status.get("identity_mismatch"))
        self.assertEqual(outcome["action"], "review-heartbeat-identity-mismatch")
        self.assertEqual(record.state, "review_starting")
        self.assertEqual(record.review_launch_aborts, 2)
        self.assertEqual(record.review_infra_failures, 3)
        self.assertEqual(record.review_infra_error, "previous launch failure")
        os.kill(foreign, 0)
        runtime.save_records.assert_not_called()

    def test_disconnected_foreign_reviewer_heartbeat_cannot_adopt_a_review_launch(self) -> None:
        """A disconnected pane still preserves a live foreign PID's no-replacement fence."""
        record = self._record(review_handle="term-review", review_leaf="leaf-review")
        record.state = "review_starting"
        foreign = self._live_pid()
        self._write_heartbeat("review", foreign, record)
        path = Path(pid_file_path("review", self.task["ref"]))
        heartbeat = json.loads(path.read_text(encoding="utf-8"))
        heartbeat["run_id"] = "foreign-reviewer-run"
        path.write_text(json.dumps(heartbeat), encoding="utf-8")
        host = self._host(
            [
                {"handle": "term-review", "leafId": "leaf-review", "connected": False},
            ]
        )
        runtime = mock.Mock()
        runtime.host = host

        status = host.review_status(self.task, record)
        with mock.patch("ummanu.dispatch.review.start_review") as start_review:
            outcome = recover_review_launch(
                runtime,
                self.task,
                {self.task["ref"]: record},
                record,
                "attempt-1",
                payload={},
            )

        self.assertTrue(status["live"])
        self.assertTrue(status["identity_mismatch"])
        self.assertEqual(outcome["action"], "review-heartbeat-identity-mismatch")
        self.assertEqual(record.state, "review_starting")
        os.kill(foreign, 0)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["run_id"], "foreign-reviewer-run")
        start_review.assert_not_called()
        runtime.save_records.assert_not_called()

    def test_connected_pane_with_a_live_head_process_stays_live(self) -> None:
        self._write_heartbeat("worker", self._live_pid())
        host = self._host(
            [
                {"handle": "term-worker", "leafId": "leaf-worker", "connected": True},
            ]
        )

        status = host.worker_status(self.task, self._record())

        self.assertTrue(status["live"])

    def test_an_adopted_head_with_no_pane_identity_is_live_on_its_heartbeat(self) -> None:
        """secretary-820: a head adopted from a launch intent never had its handle persisted, so
        the inventory cannot name its pane. Its heartbeat can, and reading it as a missing terminal
        would respawn a working head: the second launch the intent exists to prevent."""
        record = self._record(handle="", worker_leaf="")
        self._write_heartbeat("worker", self._live_pid(), record)
        host = self._host([{"handle": "term-other", "leafId": "leaf-other", "connected": True}])

        status = host.worker_status(self.task, record)

        self.assertTrue(status["live"])
        self.assertEqual(status["reason"], "pid")
        self.assertTrue(status["pid_confirmed"])

    def test_a_record_with_no_pane_identity_and_a_dead_head_is_still_missing(self) -> None:
        record = self._record(handle="", worker_leaf="")
        self._write_heartbeat("worker", self._dead_pid(), record)
        host = self._host([{"handle": "term-other", "leafId": "leaf-other", "connected": True}])

        status = host.worker_status(self.task, record)

        self.assertFalse(status["live"])
        self.assertEqual(status["reason"], "missing-terminal")

    def test_a_persisted_handle_the_inventory_never_lists_is_live_on_its_heartbeat(self) -> None:
        """secretary-1158: `orca terminal create` can return a handle `terminal list` never lists
        back, and the leaf lookup that would have saved us keys on that same handle, so
        `worker_leaf` stays empty. A persisted-but-unmatchable identity used to make the heartbeat
        unreachable and killed three live heads in a row, 1-2 minutes into each round."""
        record = self._record(handle="term-worker", worker_leaf="")
        self._write_heartbeat("worker", self._live_pid(), record)
        host = self._host([{"handle": "term-alias", "leafId": "leaf-alias", "connected": True}])

        status = host.worker_status(self.task, record)

        self.assertTrue(status["live"])
        self.assertEqual(status["reason"], "pid")
        self.assertTrue(status["pid_confirmed"])

    def test_a_freshly_respawned_head_with_no_pid_file_yet_is_live_within_the_launch_grace_window(
        self,
    ) -> None:
        """secretary-1158: the dispatcher clears the pid file before a fresh launch and the new
        head has not written its own yet, so right after a respawn neither identity answers. A
        watchdog tick landing in that window used to read a live head as missing-terminal and,
        being the second one, escalated straight to Blocked without the head ever failing."""
        host = self._host([{"handle": "term-alias", "leafId": "leaf-alias", "connected": True}])

        status = host.worker_status(
            self.task,
            self._record(handle="term-worker", worker_leaf="", worker_started_at=time.time()),
        )

        self.assertTrue(status["live"])
        self.assertEqual(status["reason"], "pid-not-written-yet")
        self.assertFalse(status["pid_confirmed"])

    def test_a_reviewer_with_no_pid_file_yet_is_live_within_the_launch_grace_window(self) -> None:
        host = self._host([{"handle": "term-alias", "leafId": "leaf-alias", "connected": True}])

        status = host.review_status(
            self.task,
            self._record(review_handle="term-review", review_leaf="", review_started_at=time.time()),
        )

        self.assertTrue(status["live"])
        self.assertEqual(status["reason"], "pid-not-written-yet")

    def test_a_head_with_no_pid_file_past_the_launch_grace_window_is_missing(self) -> None:
        """The grace window is short and bounded: once it has passed, a still-unwritten pid file
        goes back to being read as a dead head, same as before this fix."""
        host = self._host([{"handle": "term-alias", "leafId": "leaf-alias", "connected": True}])

        status = host.worker_status(
            self.task,
            self._record(
                handle="term-worker",
                worker_leaf="",
                worker_started_at=time.time() - initial_output_stall_seconds() - 1,
            ),
        )

        self.assertFalse(status["live"])
        self.assertEqual(status["reason"], "missing-terminal")

    def test_a_persisted_handle_that_matches_nothing_with_a_dead_head_is_missing(self) -> None:
        """The heartbeat is evidence, not an amnesty: without it the verdict stays unchanged."""
        record = self._record(handle="term-worker", worker_leaf="")
        self._write_heartbeat("worker", self._dead_pid(), record)
        host = self._host([{"handle": "term-alias", "leafId": "leaf-alias", "connected": True}])

        status = host.worker_status(self.task, record)

        self.assertFalse(status["live"])
        self.assertEqual(status["reason"], "missing-terminal")

    def test_a_head_silent_since_launch_is_still_live_while_its_process_runs(self) -> None:
        """The pid signal must not read silence as death: a head that has said nothing since it
        started is a separate, pre-existing case (secretary-726's short initial-output window),
        not this one."""
        self._write_heartbeat("worker", self._live_pid())
        host = self._host(
            [
                {
                    "handle": "term-worker",
                    "leafId": "leaf-worker",
                    "connected": True,
                    "lastOutputAt": 1_000_000,
                },
            ]
        )

        status = host.worker_status(self.task, self._record())

        self.assertTrue(status["live"])

    def test_an_adopted_head_with_no_pane_identity_answers_no_work_state(self) -> None:
        """Nothing to probe, so the status says so instead of guessing: the caller falls back to
        its ceilings rather than treating an unprobed head as one that is working."""
        record = self._record(handle="", worker_leaf="")
        self._write_heartbeat("worker", self._live_pid(), record)
        host = self._host([{"handle": "term-other", "leafId": "leaf-other", "connected": True}])

        status = host.worker_status(self.task, record)

        self.assertTrue(status["pid_confirmed"])
        self.assertNotIn("idle", status)

    def test_a_refused_readiness_probe_answers_no_work_state(self) -> None:
        """A live pane whose binding the runtime has lost: `terminal list` still names it, and the
        readiness probe fails with `terminal_handle_stale`. That is not a busy head and not an idle
        one, so no work state is reported for it."""
        self._write_heartbeat("worker", self._live_pid())
        host = self._host(
            [
                {"handle": "term-worker", "leafId": "leaf-worker", "connected": True},
            ]
        )
        host.fail_ops = {"wait"}

        status = host.worker_status(self.task, self._record())

        self.assertTrue(status["pid_confirmed"])
        self.assertNotIn("idle", status)

    def test_readiness_is_not_probed_without_a_confirmed_head_process(self) -> None:
        """Without the heartbeat the ordinary ceilings still run, and a probe per waiting tick
        would buy nothing."""
        host = self._host(
            [
                {"handle": "term-worker", "leafId": "leaf-worker", "connected": True},
            ]
        )

        status = host.worker_status(self.task, self._record())

        self.assertNotIn("idle", status)
        self.assertNotIn("wait", [call[2] for call in host.calls if call[:2] == ["orca", "terminal"]])


class ProductionPauseTests(unittest.TestCase):
    """The pause the operator actually presses (secretary-731).

    The bug this covers: `pause` wrote a flag the production dispatcher never read, so the operator
    watched the pipeline claim new cards straight through a successful pause.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.data_dir = Path(self.tmpdir.name)
        # The legacy mirror is written next to the live pipeline worktree by default; keep every
        # test's copy inside its own tmpdir.
        env = mock.patch.dict(
            os.environ,
            {
                "UMMANU_LEGACY_PAUSE_FILE": str(self.data_dir / "legacy-pause.json"),
                "UMMANU_DISPATCHER_BODY_DIR": str(self.data_dir / "bodies"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.legacy_mirror = self.data_dir / "legacy-pause.json"
        self.board = card_store(self, dispatcher_seed(), instance_dir=self.data_dir)
        self.reader = TaskReader(self.board)  # type: ignore[arg-type]
        self.writer = TaskWriter(self.board, data_dir=self.data_dir, workspace=self.data_dir)  # type: ignore[arg-type]
        self.catalog = FakeCatalog(instance_dir=self.data_dir)
        self.host = FakeHost(self.data_dir / "workspaces", self.catalog)
        self.host.audit = task_audit_for(self.board)
        self.runtime = DispatcherRuntime(
            self.reader,
            self.writer,
            task_audit_for(self.board),
            self.data_dir,
            self.catalog,  # type: ignore[arg-type]
            self.host,  # type: ignore[arg-type]
            owner="ummanu-pilot",
        )
        self.ref = "ummanu-510"

    def pause(self, mode: str, **kwargs) -> dict:
        return self.runtime.pause_pipeline(mode=mode, actor="operator", reason="host maintenance", **kwargs)

    def report_done(self) -> None:
        """Report through the command in the checkout: that id is what names the round."""
        document = (Path(self.record().workspace) / "TASK.md").read_text(encoding="utf-8")
        line = next(line for line in document.splitlines() if "--kind done" in line)
        self.writer.report(
            role="worker",
            actor="worker",
            reference=self.ref,
            kind="done",
            body="ready for review",
            request_id=line.split("--request-id ", 1)[1].split()[0],
        )

    def drive_into_review(self) -> None:
        self.runtime.production_tick()
        self.report_done()
        self.runtime.production_tick()
        self.assertEqual(self.runtime.production_tick()["actions"][0]["action"], "review-started")

    def record(self) -> DispatcherRecord:
        payload = self.runtime.production_state.load()
        return self.runtime.production_state.records(payload)[self.ref]

    def test_drain_stops_new_claims(self) -> None:
        self.pause("drain")

        result = self.runtime.production_tick()

        self.assertEqual(result["pause"]["mode"], "drain")
        self.assertEqual(self.reader.show(self.ref)["state"], "ready")
        self.assertEqual(self.host.prepared, [])
        self.assertEqual(self.runtime.production_state.load().get("records") or {}, {})

    def test_unreadable_pause_file_freezes_dispatch(self) -> None:
        self.runtime.pause.path.parent.mkdir(parents=True, exist_ok=True)
        self.runtime.pause.path.write_text("{not-json", encoding="utf-8")

        result = self.runtime.production_tick()

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["pause"]["mode"], "freeze")
        self.assertIn("pause file is unreadable and read as frozen", result["pause"]["warnings"][0])
        self.assertEqual(self.reader.show(self.ref)["state"], "ready")

    def test_paused_tick_does_not_claim_a_new_card(self) -> None:
        """Regression for the reported bug: pause, tick, and the card must still be Ready."""
        for mode in ("drain", "freeze"):
            with self.subTest(mode=mode):
                self.pause(mode)
                self.runtime.production_tick()
                self.assertEqual(self.reader.show(self.ref)["state"], "ready")
                self.assertEqual(self.reader.show("ummanu-511")["state"], "ready")
                self.runtime.resume_pipeline(actor="operator")

    def test_drain_keeps_driving_the_card_already_in_flight(self) -> None:
        self.runtime.production_tick()
        self.report_done()
        self.pause("drain")

        result = self.runtime.production_tick()

        self.assertEqual(result["actions"][0]["to"], "validate")
        self.assertEqual(self.reader.show(self.ref)["state"], "validate")
        # ...and the Ready neighbour is still not claimed while the drain holds.
        self.assertEqual(self.reader.show("ummanu-511")["state"], "ready")

    def test_freeze_stops_the_worker_head_without_touching_the_workspace(self) -> None:
        self.runtime.production_tick()
        workspace = self.record().workspace

        status = self.pause("freeze")

        self.assertEqual(status["stopped_worker"], [self.ref])
        self.assertIn("stop_head:worker", self.host.calls)
        self.assertNotIn("stop_workspace", self.host.calls)
        self.assertEqual(self.host.torn_down, [])
        self.assertTrue(Path(workspace).is_dir())
        record = self.record()
        self.assertEqual(record.handle, "")
        self.assertEqual(record.workspace, workspace)
        self.assertGreater(record.paused_worker_at, 0)

    def test_freeze_stops_the_reviewer_head(self) -> None:
        self.drive_into_review()

        status = self.pause("freeze")

        self.assertEqual(status["stopped_reviewer"], [self.ref])
        self.assertEqual(self.host.stopped_reviews, [f"review:{self.ref}"])
        self.assertEqual(self.host.torn_down, [])
        record = self.record()
        self.assertEqual(record.review_handle, "")
        self.assertGreater(record.paused_reviewer_at, 0)

    def test_freeze_advances_nothing(self) -> None:
        self.runtime.production_tick()
        self.report_done()
        self.pause("freeze")

        result = self.runtime.production_tick()

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "pipeline is frozen by pause")
        self.assertEqual(self.reader.show(self.ref)["state"], "in_progress")

    def test_resume_relaunches_the_worker_in_the_same_workspace(self) -> None:
        self.runtime.production_tick()
        workspace = self.record().workspace
        self.pause("freeze")

        result = self.runtime.resume_pipeline(actor="operator")

        self.assertEqual(result["relaunched"], [f"{self.ref}:worker"])
        self.assertIn("restart_worker", self.host.calls)
        record = self.record()
        self.assertEqual(record.handle, f"rework:{self.ref}")
        self.assertEqual(record.workspace, workspace)
        self.assertEqual(record.paused_worker_at, 0.0)
        self.assertFalse(self.runtime.pause.path.exists())

    def test_resume_relaunches_the_reviewer(self) -> None:
        self.drive_into_review()
        self.pause("freeze")

        result = self.runtime.resume_pipeline(actor="operator")

        self.assertEqual(result["relaunched"], [f"{self.ref}:reviewer"])
        record = self.record()
        self.assertEqual(record.review_handle, f"review:{self.ref}")
        self.assertEqual(record.state, "reviewing")
        self.assertEqual(record.paused_reviewer_at, 0.0)

    def test_a_resume_relaunch_that_dies_mid_flight_is_adopted_by_the_next_tick(self) -> None:
        """secretary-820: the resume is a bring-up like any other, so it writes its intent first.

        A resume killed between the head coming up and the state write that records it would
        otherwise leave a live worker with no handle in the record, and the next tick's watchdog
        would respawn a head that is working.
        """
        self.runtime.production_tick()
        self.pause("freeze")
        real_save = self.runtime.production_state.save
        real_restart = self.host.restart_worker
        launched = {"yet": False}

        def save(payload: dict) -> None:
            if launched["yet"]:
                raise OSError("production state is not writable")
            real_save(payload)

        def restart(task: dict, record, **kwargs):
            result = real_restart(task, record, **kwargs)
            launched["yet"] = True
            return result

        with (
            mock.patch.object(self.runtime.production_state, "save", save),
            mock.patch.object(self.host, "restart_worker", restart),
            self.assertRaises(OSError),
        ):
            self.runtime.resume_pipeline(actor="operator")

        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        self.assertEqual(self.record().launch_intent["action"], "worker-resume")

        # The operator retries the resume. It parks the card rather than guessing at the head the
        # dead run may have left: the tick's recovery is the one place that decides.
        retried = self.runtime.resume_pipeline(actor="operator")

        self.assertEqual(retried["parked"], [f"{self.ref}:worker"])
        self.assertEqual(self.host.calls.count("restart_worker"), 1)

        adopted = self.runtime.production_tick()["actions"][0]

        self.assertEqual(adopted["action"], "worker-launch-adopted")
        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        self.assertEqual(self.record().state, "claimed")

    def test_resume_leaves_a_card_that_reported_during_the_freeze_to_the_tick(self) -> None:
        """A relaunched head would start a fresh turn on work that is already finished."""
        self.runtime.production_tick()
        self.pause("freeze")
        self.report_done()

        result = self.runtime.resume_pipeline(actor="operator")

        self.assertEqual(result["parked"], [f"{self.ref}:worker"])
        self.assertNotIn("restart_worker", self.host.calls)
        self.assertEqual(self.runtime.production_tick()["actions"][0]["to"], "validate")

    def test_resume_hands_the_wait_watchdog_a_fresh_window(self) -> None:
        """A freeze advances nothing, so its whole length would otherwise count as head silence."""
        self.runtime.production_tick()
        self.runtime.production_tick()
        payload = self.runtime.production_state.load()
        records = self.runtime.production_state.records(payload)
        stale = time.time() - (WORKER_REPORT_STALL_DEFAULT * 2)
        records[self.ref].worker_waiting_since = stale
        records[self.ref].worker_progress_at = stale
        # A head seen at its prompt before the freeze is given its idle window back too, or the
        # freeze itself reads as a head that stopped working and delivered nothing.
        records[self.ref].worker_idle_since = stale
        self.runtime.production_state.put_records(payload, records)
        self.runtime.production_state.save(payload)
        self.pause("freeze")

        for _ in range(3):
            self.assertEqual(self.runtime.production_tick()["status"], "skipped")
        paused = self.record()
        self.assertEqual(paused.worker_respawns, 0)
        self.assertEqual(self.reader.show(self.ref)["state"], "in_progress")

        self.runtime.resume_pipeline(actor="operator")

        self.assertGreater(self.record().worker_waiting_since, stale)
        self.assertEqual(self.record().worker_idle_since, 0.0)
        # The watchdog did not read the paused head as a stall: no respawn, no Blocked.
        self.assertEqual(self.runtime.production_tick()["actions"][0]["action"], "waiting-worker-report")
        self.assertEqual(self.reader.show(self.ref)["state"], "in_progress")

    def test_pause_status_reports_the_live_data_plane_and_stopped_heads(self) -> None:
        self.runtime.production_tick()
        self.pause("freeze")

        status = self.runtime.pause_status()

        self.assertTrue(status["paused"])
        self.assertEqual(status["mode"], "freeze")
        self.assertEqual(status["pause_file"], str(self.data_dir / "dispatcher" / "pause.json"))
        self.assertEqual(
            status["dispatcher"]["state_file"], str(self.data_dir / "dispatcher" / "production-state.json")
        )
        self.assertEqual(status["dispatcher"]["owner"], "ummanu-pilot")
        head = next(entry for entry in status["heads"] if entry["ref"] == self.ref)
        self.assertEqual(head["worker"], "stopped-by-pause")

    def test_a_head_that_was_never_up_is_not_reported_as_pause_stopped(self) -> None:
        self.runtime.production_tick()
        self.pause("freeze")

        head = next(entry for entry in self.runtime.pause_status()["heads"] if entry["ref"] == self.ref)

        self.assertEqual(head["worker"], "stopped-by-pause")
        self.assertEqual(head["reviewer"], "not-running")

    def test_repeated_pause_in_the_same_mode_is_a_noop(self) -> None:
        self.pause("drain")

        again = self.pause("drain")

        self.assertEqual(again["action"], "noop")
        self.assertEqual(again["mode"], "drain")

    def test_switching_mode_while_paused_is_refused(self) -> None:
        self.pause("drain")

        with self.assertRaisesRegex(DispatcherError, "already paused"):
            self.pause("freeze")

    def test_legacy_aliases_still_parse(self) -> None:
        self.assertEqual(self.pause("soft")["mode"], "drain")
        self.runtime.resume_pipeline(actor="operator")
        self.assertEqual(self.pause("hard")["mode"], "freeze")

    def test_unknown_mode_is_refused(self) -> None:
        with self.assertRaisesRegex(DispatcherError, "unknown pause mode"):
            self.pause("halt")

    def test_resume_without_a_pause_is_a_noop(self) -> None:
        result = self.runtime.resume_pipeline(actor="operator")

        self.assertEqual(result["action"], "noop")
        self.assertFalse(result["paused"])

    def test_pause_mirrors_the_flag_the_background_roles_read(self) -> None:
        """steward/curator/retro still read the legacy flag, so a pause has to reach it too."""
        self.pause("freeze")

        mirrored = json.loads(self.legacy_mirror.read_text(encoding="utf-8"))
        self.assertEqual(mirrored["mode"], "hard")
        self.assertEqual(mirrored["actor"], "operator")

        self.runtime.resume_pipeline(actor="operator")

        self.assertFalse(self.legacy_mirror.exists())

    def test_pause_never_takes_over_a_legacy_flag_it_did_not_write(self) -> None:
        self.legacy_mirror.write_text(json.dumps({"mode": "hard", "actor": "someone-else"}), encoding="utf-8")

        status = self.pause("freeze")

        self.assertFalse(status["legacy_mirror"]["written"])
        self.runtime.resume_pipeline(actor="operator")
        self.assertEqual(json.loads(self.legacy_mirror.read_text(encoding="utf-8"))["actor"], "someone-else")

    def test_freeze_leaves_an_excluded_workspace_running(self) -> None:
        """The backup worker freezes the pipeline from inside its own workspace."""
        self.runtime.production_tick()
        workspace = self.record().workspace

        status = self.pause("freeze", exclude_workspaces=[workspace])

        self.assertEqual(status["excluded_worker"], [self.ref])
        self.assertEqual(status["stopped_worker"], [])
        self.assertEqual(self.host.stopped, [])
        self.assertEqual(self.record().handle, f"term:{self.ref}-pilot")

    def test_probe_reports_a_freeze_instead_of_a_stuck_dispatcher(self) -> None:
        self.pause("freeze")

        result = self.runtime.production_probe()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["pause"]["mode"], "freeze")
        self.assertEqual(result["would"], [])

    def test_freeze_over_unreadable_state_sets_the_flag_without_touching_it(self) -> None:
        """An unreadable state file must not be replaced by an empty one on the way to a freeze."""
        self.runtime.production_state.path.parent.mkdir(parents=True, exist_ok=True)
        self.runtime.production_state.path.write_text("{ not json", encoding="utf-8")

        status = self.pause("freeze")

        self.assertTrue(status["paused"])
        self.assertIn("production state is unreadable", " ".join(status["warnings"]))
        self.assertEqual(self.runtime.production_state.path.read_text(encoding="utf-8"), "{ not json")
        self.assertEqual(self.runtime.production_tick()["status"], "skipped")

    def age_the_pause(self, seconds: int) -> None:
        state = self.runtime.pause.load()
        state["since"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds))
        self.runtime.pause.save(state)

    def test_an_expired_automation_freeze_is_resumed_by_the_next_tick(self) -> None:
        """A backup killed before its `finally` must not freeze the dispatcher forever."""
        self.runtime.production_tick()
        workspace = self.record().workspace
        self.runtime.pause_pipeline(mode="freeze", actor="ummanu-backup", reason="backup snapshot")
        self.age_the_pause(3600)

        result = self.runtime.production_tick()

        self.assertEqual(result["auto_resume"]["reason"], "stale-automation-freeze")
        self.assertEqual(result["auto_resume"]["relaunched"], [f"{self.ref}:worker"])
        self.assertFalse(self.runtime.pause.path.exists())
        self.assertNotEqual(result["status"], "skipped")
        record = self.record()
        self.assertEqual(record.handle, f"rework:{self.ref}")
        self.assertEqual(record.workspace, workspace)
        self.assertEqual(record.paused_worker_at, 0.0)

    def test_a_fresh_automation_freeze_is_left_alone(self) -> None:
        self.runtime.production_tick()
        self.runtime.pause_pipeline(mode="freeze", actor="ummanu-backup", reason="backup snapshot")

        result = self.runtime.production_tick()

        self.assertEqual(result["status"], "skipped")
        self.assertIsNone(result.get("auto_resume"))
        self.assertEqual(result["pause"]["auto_resume"]["reason"], "fresh")
        self.assertTrue(self.runtime.pause.path.exists())

    def test_an_operator_freeze_never_expires(self) -> None:
        """A person holding a maintenance window decides when it ends, however long it runs."""
        self.runtime.production_tick()
        self.pause("freeze")
        self.age_the_pause(3600 * 12)

        result = self.runtime.production_tick()

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["pause"]["auto_resume"]["reason"], "manual-or-unknown-actor")
        self.assertTrue(self.runtime.pause.path.exists())

    def test_auto_resume_honours_the_ttl_override(self) -> None:
        self.runtime.pause_pipeline(mode="freeze", actor="ummanu-backup", reason="backup")
        self.age_the_pause(3600)

        with mock.patch.dict(os.environ, {"TA_HARD_PAUSE_AUTO_RESUME_TTL_S": "0"}):
            self.assertEqual(self.runtime.production_tick()["status"], "skipped")

        self.assertEqual(self.runtime.production_tick()["auto_resume"]["resumed"], True)

    def test_a_failed_auto_resume_holds_the_freeze_and_says_why(self) -> None:
        self.runtime.production_tick()
        self.runtime.pause_pipeline(mode="freeze", actor="ummanu-backup", reason="backup")
        self.age_the_pause(3600)

        with mock.patch.object(self.runtime.pause, "clear", side_effect=OSError("read-only fs")):
            result = self.runtime.production_tick()

        self.assertEqual(result["status"], "skipped")
        self.assertFalse(result["auto_resume"]["resumed"])
        self.assertIn("read-only fs", result["auto_resume"]["error"])
        self.assertTrue(self.runtime.pause.path.exists())
        # The retry on the next tick does not launch a second head on top of the one it just put back.
        retry = self.runtime.production_tick()
        self.assertEqual(retry["auto_resume"]["parked"], [f"{self.ref}:worker"])

    def test_independent_checkpoint_still_writes_and_pushes_while_frozen(self) -> None:
        """Freeze stops cards moving, not durability: a long freeze must not be a snapshot hole."""
        self.runtime.checkpoint = FakeCheckpoint(
            CheckpointResult(status="committed", commit="abc123", board_cards=2)
        )
        self.runtime.checkpoint_push = FakePusher({"status": "pushed", "last_push_commit": "abc123"})
        self.pause("freeze")

        result = self.runtime.production_tick()

        self.assertEqual(result["status"], "skipped")
        self.assertNotIn("checkpoint", result)
        self.assertNotIn("checkpoint_push", result)
        result = run_checkpoint(self.runtime)
        self.assertEqual(result["checkpoint"]["commit"], "abc123")
        self.assertEqual(result["checkpoint_push"]["status"], "pushed")
        payload = load_checkpoint_state(self.runtime.data_dir)
        self.assertEqual(payload["checkpoint"]["commit"], "abc123")
        self.assertEqual(payload["checkpoint_push"]["last_push_commit"], "abc123")
        # ...and the frozen tick still moved nothing.
        self.assertEqual(self.reader.show(self.ref)["state"], "ready")

    def test_a_failing_independent_push_while_frozen_is_reported_not_raised(self) -> None:
        self.runtime.checkpoint = FakeCheckpoint(CheckpointResult(status="unchanged", board_cards=2))
        self.runtime.checkpoint_push = FakePusher(RuntimeError("ssh agent is gone"))
        self.pause("freeze")

        result = self.runtime.production_tick()

        self.assertEqual(result["status"], "skipped")
        self.assertNotIn("checkpoint", result)
        self.assertNotIn("checkpoint_push", result)
        result = run_checkpoint(self.runtime)
        self.assertEqual(result["checkpoint_push"]["status"], "failed")
        self.assertIn("ssh agent is gone", result["checkpoint_push"]["reason"])

    def test_the_mirror_lands_where_the_background_roles_look(self) -> None:
        """resolve_pipeline_state_dir's own order: a mirror written elsewhere sheds nothing."""
        state_dir = self.data_dir / "ta-state"
        with mock.patch.dict(os.environ, {"TA_PIPELINE_STATE_DIR": str(state_dir)}):
            os.environ.pop("UMMANU_LEGACY_PAUSE_FILE", None)
            self.pause("drain")

        self.assertTrue((state_dir / "pause.json").is_file())


class CommandHostStopWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.host = CommandHostRuntime(FakeCatalog(), self.root, mode="real")  # type: ignore[arg-type]
        self.backend = SupervisedBackend().install(self.host)
        self.record = DispatcherRecord(
            worker="ummanu-997-w1",
            workspace=str(self.root / "workspaces" / "ummanu" / "ummanu-997-w1"),
            handle="",
            head="head",
            review_head="review-head",
            attempt_id="attempt-1",
            comment_baseline=0,
            review_baseline=0,
            state="working",
            claimed_at=0.0,
        )

    def test_a_live_foreign_heartbeat_fences_a_workspace_before_its_first_stop(self) -> None:
        pid_file = self.root / "foreign-workspace.pid"
        self.record.worker_pid_file = str(pid_file)
        self.record.worker_leaf = "leaf-worker"
        self.record.worker_head_run = supervised_run(
            "workspace-owned-run",
            profile="head",
            adapter="unknown",
            workspace=self.record.workspace,
            task_ref=head_ops.TaskRef.card("ummanu-997"),
            leaf=self.record.worker_leaf,
            pid_file=str(pid_file),
        )
        foreign = subprocess.Popen(["sleep", "5"])

        def reap_foreign() -> None:
            if foreign.poll() is None:
                foreign.terminate()
            foreign.wait()

        self.addCleanup(reap_foreign)
        PidHeartbeatTests.write_heartbeat(
            pid_file,
            foreign.pid,
            identity=heartbeat_identity(
                run_id="foreign-workspace-run",
                role="worker",
                task="card:ummanu-997",
                leaf=self.record.worker_leaf,
            ),
        )

        with (
            mock.patch.object(self.host, "_signal_head") as signal_head,
            self.assertRaisesRegex(HostError, "mismatching launch identity"),
        ):
            self.host.stop_workspace(self.record)

        self.assertEqual(self.backend.stops, [], "the workspace stop is fenced before any head is stopped")
        signal_head.assert_not_called()
        self.assertIsNone(foreign.poll())


class ReviewPaneTests(unittest.TestCase):
    """secretary-651: the reviewer runs beside the worker, in the worker's own worktree."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.card_audit = task_audit_for(card_store(self, dispatcher_seed()))
        _clear_env(self, "UMMANU_DISPATCHER_REVIEW_COMMAND")
        os.environ["UMMANU_DISPATCHER_BODY_DIR"] = str(self.root)
        self.task = {
            "ref": "ummanu-651",
            "project": "ummanu",
            "description": "spec",
            "workspace": {"base_branch": "main"},
            "routing": {},
        }

    def _record(self) -> DispatcherRecord:
        record = DispatcherRecord(
            worker="ummanu-651-w",
            workspace=str(self.workspace),
            handle="run:worker-651",
            head="codex",
            review_head="codex-reviewer",
            attempt_id="attempt-1",
            comment_baseline=0,
            review_baseline=0,
            state="review_starting",
            claimed_at=0.0,
        )
        record.worker_head_run = supervised_run(
            "worker-651", workspace=str(self.workspace), task_ref=head_ops.TaskRef.card("ummanu-651")
        )
        return record

    def test_the_worker_is_shut_down_once_the_reviewer_is_up(self) -> None:
        """Nothing else stops the worker head from editing the checkout mid-review."""
        host = RecordingReviewHost(self.root)
        host.audit = self.card_audit

        launch = host.start_review(self.task, self._record())

        [start] = host.backend.starts
        self.assertEqual(start["role"], "reviewer")
        self.assertEqual(host.backend.stops, [("worker-651", STOPPED_BY_REVIEW_FREEZE)])
        self.assertEqual(launch.commit, "deadbeefcafe0000")

    def test_a_reviewer_that_did_not_come_up_leaves_the_worker_alone(self) -> None:
        host = RecordingReviewHost(self.root)
        host.audit = self.card_audit
        host.backend.start_failure = head_ops.HeadSpawnFailed("the reviewer never started")

        with self.assertRaises(HostError):
            host.start_review(self.task, self._record())

        self.assertEqual(host.backend.stops, [], "a failed reviewer must not kill the worker head")
