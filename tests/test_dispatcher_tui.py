from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.dispatcher_fixtures import SupervisedBackend
from tests.fanout_fixtures import accepted_transport_run
from ummanu.dispatch.host import CommandHostRuntime
from ummanu.dispatch.tui import (
    DELIVERY_RECEIPT_ACCEPTED,
    DELIVERY_RECEIPT_REFUSED,
    DELIVERY_RECEIPT_UNOBSERVED,
    READINESS_BUSY,
    bind_claude_provider_progress_source,
    claude_project_dir_name,
    delivery_receipt_state,
    latest_claude_user_turn_for,
    latest_user_turn_for,
    prepare_claude_provider_progress_source,
    provider_progress_for_run,
    provider_turn_started,
)
from ummanu.dispatch.worker_lifecycle import ContinuationProviderCondition
from ummanu.runtime.codex_preflight import codex_provider_source_descriptor
from ummanu.runtime.head import HeadCommand, HeadRun, HeadSpec, TaskRef
from ummanu.runtime.tui_delivery import (
    DeliveryEvidence,
)


class DispatcherTuiLaunchTests(unittest.TestCase):
    def test_claude_binding_retries_the_exact_run_until_its_late_session_id_arrives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            root = Path(tmp) / "claude-projects"
            with mock.patch.dict(os.environ, {"UMMANU_CLAUDE_PROJECTS": str(root)}):
                run = prepare_claude_provider_progress_source(
                    HeadRun(
                        run_id="claude-late",
                        spec=HeadSpec(profile_id="claude", adapter="claude"),
                        workspace=str(workspace),
                        task_ref=TaskRef.card("ummanu-1517"),
                        role="worker",
                    )
                )
                run = bind_claude_provider_progress_source(run)
                self.assertEqual(
                    run.fanout_policy["provider_progress_source"]["state"], "awaiting_transcript"
                )

                transcript = root / claude_project_dir_name(str(workspace)) / "own.jsonl"
                transcript.parent.mkdir(parents=True)
                transcript.write_text('{"type":"assistant"}\n', encoding="utf-8")
                run = bind_claude_provider_progress_source(run)
                self.assertEqual(
                    run.fanout_policy["provider_progress_source"]["state"], "awaiting_session_id"
                )

                # The selected path is fenced by its device/inode. A later same-workspace file
                # cannot become this run's conversation while Claude finishes its own header.
                foreign = transcript.with_name("foreign.jsonl")
                foreign.write_text('{"type":"assistant","sessionId":"foreign-session"}\n', encoding="utf-8")
                transcript.write_text(
                    '{"type":"assistant"}\n{"type":"assistant","sessionId":"late-own-session"}\n',
                    encoding="utf-8",
                )
                run = bind_claude_provider_progress_source(run)

            source = run.fanout_policy["provider_progress_source"]
            self.assertEqual(source["state"], "bound")
            self.assertEqual(source["session_id"], "late-own-session")
            self.assertEqual(source["path"], str(transcript.resolve()))

    def test_provider_progress_uses_only_text_free_run_bound_cursors(self) -> None:
        """Both provider shapes reject competing workspace files and expose only opaque cursors."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            claude_root = Path(tmp) / "claude-projects"
            with mock.patch.dict(os.environ, {"UMMANU_CLAUDE_PROJECTS": str(claude_root)}):
                claude_run = prepare_claude_provider_progress_source(
                    HeadRun(
                        run_id="claude-bound",
                        spec=HeadSpec(profile_id="claude", adapter="claude"),
                        workspace=str(workspace),
                        task_ref=TaskRef.card("ummanu-1429"),
                        role="worker",
                    )
                )
                transcript = claude_root / claude_project_dir_name(str(workspace)) / "session.jsonl"
                foreign = claude_root / claude_project_dir_name(str(workspace)) / "foreign.jsonl"
                transcript.parent.mkdir(parents=True)
                transcript.write_text(
                    '{"type":"assistant","sessionId":"claude-session-1","message":"secret"}\n',
                    encoding="utf-8",
                )
                claude_run = bind_claude_provider_progress_source(claude_run)
                foreign.write_text('{"type":"assistant","message":"foreign"}\n', encoding="utf-8")
                claude = provider_progress_for_run(claude_run)
            self.assertEqual(claude["state"], "observed")
            self.assertEqual(claude["source"], "claude-session")
            self.assertIn(":", claude["cursor"])
            self.assertNotIn("secret", str(claude))
            self.assertNotIn("foreign", str(claude))
            self.assertEqual(
                claude_run.fanout_policy["provider_progress_source"]["session_id"], "claude-session-1"
            )

            codex_root = Path(tmp) / "codex-sessions"
            codex_path = codex_root / "session.jsonl"
            codex_root.mkdir()
            codex_path.write_text(
                '{"type":"session_meta","payload":{"session_id":"own","cwd":"' + str(workspace) + '"}}\n'
                '{"type":"event_msg","payload":{"type":"thread.started","thread_id":"parent"}}\n',
                encoding="utf-8",
            )
            from ummanu.codex_provider_events import _range_digest, _read_source
            from ummanu.dispatch.worker_lifecycle import head_run_binding

            parsed = _read_source(codex_path)
            self.assertIsNotNone(parsed)
            _meta, lines = parsed
            _, run_fingerprint = head_run_binding(
                HeadRun(
                    run_id="codex-bound",
                    spec=HeadSpec(profile_id="codex", adapter="codex"),
                    workspace=str(workspace),
                    task_ref=TaskRef.card("ummanu-1429"),
                    role="worker",
                ).to_json()
            )
            codex_run = HeadRun(
                run_id="codex-bound",
                spec=HeadSpec(profile_id="codex", adapter="codex"),
                workspace=str(workspace),
                task_ref=TaskRef.card("ummanu-1429"),
                role="worker",
            )
            source = {
                "version": 1,
                "kind": "codex_session_event_jsonl",
                "state": "bound",
                "run_id": codex_run.run_id,
                "head_run_fingerprint": run_fingerprint,
                "workspace": str(workspace.resolve()),
                "role": "worker",
                "task_ref": codex_run.task_ref.to_json(),
                "root": str(codex_root.resolve()),
                "path": str(codex_path.resolve()),
                "session_id": "own",
                "parent_thread_id": "parent",
                "initial_range": {
                    "first": {"line": 1, "digest": lines[0].digest},
                    "root": {"line": 2, "digest": lines[1].digest},
                    "last": {"line": 2, "digest": lines[1].digest},
                    "digest": _range_digest(lines),
                },
                "cursor": {"line": 2, "digest": lines[1].digest},
                "bound_at": "2026-08-13T00:00:00Z",
            }
            codex_run = codex_run.with_fanout_policy(
                {
                    **codex_run.fanout_policy,
                    "provider_source": source,
                }
            )
            foreign_codex = codex_root / "foreign.jsonl"
            foreign_codex.write_text(
                '{"type":"session_meta","payload":{"session_id":"foreign"}}\n', encoding="utf-8"
            )
            codex = provider_progress_for_run(codex_run)
            self.assertEqual(codex["state"], "observed")
            self.assertEqual(codex["source"], "codex-session")
            self.assertNotIn("foreign", str(codex))

    def test_provider_progress_keeps_an_unavailable_source_distinct_from_busy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            unavailable = provider_progress_for_run(
                HeadRun(
                    run_id="claude-unbound",
                    spec=HeadSpec(profile_id="claude", adapter="claude"),
                    workspace=str(Path(tmp) / "workspace"),
                    task_ref=TaskRef.card("ummanu-1429"),
                    role="worker",
                )
            )
        self.assertEqual(unavailable["state"], "unavailable")
        self.assertNotEqual(unavailable["state"], READINESS_BUSY)

    def test_provider_progress_types_only_a_complete_legacy_unbound_codex_v1_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            run = HeadRun(
                run_id="codex-unbound",
                spec=HeadSpec(profile_id="codex", adapter="codex", model="gpt-5.6-terra"),
                workspace=str(workspace),
                task_ref=TaskRef.card("ummanu-1435"),
                role="worker",
            )
            source = {
                "version": 1,
                "kind": "codex_session_event_jsonl",
                "state": "unbound",
                **codex_provider_source_descriptor(run),
                "root": str((Path(tmp) / "sessions").resolve()),
                "baseline": [],
            }
            policy = {
                "version": 1,
                "state": "allowed",
                "terminal_state": "clean",
                "run_id": run.run_id,
                "role": run.role,
                "model": run.spec.model or "",
                "binary_path": "/test/codex",
                "binary_digest": "0" * 64,
                "cli_version": "test-codex",
                "tool_schema_digest": "0" * 64,
                "provider_schema_verdict": "no_callable_child_spawn_surface",
                "events": [],
                "provider_source_required": True,
                "provider_source": source,
            }

            legacy = provider_progress_for_run(run.with_fanout_policy(policy))
            foreign = provider_progress_for_run(
                run.with_fanout_policy(
                    {
                        **policy,
                        "provider_source": {**source, "workspace": "/foreign"},
                    }
                )
            )
            malformed = provider_progress_for_run(
                run.with_fanout_policy(
                    {
                        **policy,
                        "provider_source": {**source, "baseline": [1]},
                    }
                )
            )
            relative_root = provider_progress_for_run(
                run.with_fanout_policy(
                    {
                        **policy,
                        "provider_source": {**source, "root": "relative-session-root"},
                    }
                )
            )
            relative_baseline = provider_progress_for_run(
                run.with_fanout_policy(
                    {
                        **policy,
                        "provider_source": {**source, "baseline": ["relative-old-session.jsonl"]},
                    }
                )
            )
            noncanonical_root = provider_progress_for_run(
                run.with_fanout_policy(
                    {
                        **policy,
                        "provider_source": {
                            **source,
                            "root": str(Path(tmp) / "sessions" / ".." / "sessions"),
                        },
                    }
                )
            )
            outside_baseline = provider_progress_for_run(
                run.with_fanout_policy(
                    {
                        **policy,
                        "provider_source": {
                            **source,
                            "baseline": [str((Path(tmp) / "outside.jsonl").resolve())],
                        },
                    }
                )
            )

        self.assertEqual(legacy["state"], "unavailable")
        self.assertEqual(
            legacy["continuation_condition"],
            ContinuationProviderCondition.LEGACY_UNBOUND_V1.value,
        )
        self.assertNotIn("continuation_condition", foreign)
        self.assertNotIn("continuation_condition", malformed)
        self.assertNotIn("continuation_condition", relative_root)
        self.assertNotIn("continuation_condition", relative_baseline)
        self.assertNotIn("continuation_condition", noncanonical_root)
        self.assertNotIn("continuation_condition", outside_baseline)

    def test_a_card_still_carrying_exec_is_launched_and_prompted_the_same_way(self) -> None:
        """The route a restored or long-lived card takes: legacy `codex_launch_mode` on the card.

        It reaches the bring-up exactly as it is stored and changes nothing. The head is started
        and handed its prompt, which is the one Codex bring-up there is.
        """
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "TASK.md").write_text("Read TASK.md\n", encoding="utf-8")
            host = RecordingTuiHost(workspace)

            host._launch(
                str(workspace),
                "title",
                "codex",
                "TASK.md",
                role="worker",
                env_name="UMMANU_DISPATCHER_WORKER_COMMAND",
                task={"ref": "ummanu-1173", "routing": {"codex_launch_mode": "exec"}},
            )

        [start] = host.backend.starts
        self.assertNotIn("codex exec", str(start["command"]))
        self.assertIsNotNone(start["pointer"], "the prompt is delivered after the head is up")
        self.assertEqual(len(host.backend.deliveries), 1)
        # Nothing selected the interactive shape: the launcher was asked for a Codex head and
        # there is no other kind to ask for.
        self.assertEqual(host.catalog.heads, ["codex"])

    def test_tui_launch_delivers_short_pointer_not_task_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "TASK.md").write_text(
                "full spec body that must not be delivered\n", encoding="utf-8"
            )
            host = RecordingTuiHost(workspace)

            host._launch(
                str(workspace),
                "title",
                "codex",
                "TASK.md",
                role="worker",
                env_name="UMMANU_DISPATCHER_WORKER_COMMAND",
                launch_prompt="The full task is in TASK.md. Read it first.",
            )

        [(_run, pointer, _subject)] = host.backend.deliveries
        self.assertEqual(pointer.text, "The full task is in TASK.md. Read it first.")
        self.assertNotIn("full spec body", pointer.text)


class CodexUserTurnRecordTests(unittest.TestCase):
    """What Codex writes down when a prompt is submitted, in both shapes it writes it.

    The records below are the two real ones. `codex exec` persists `event_msg`/`user_message`;
    the interactive `codex-tui` of cli 0.147.0 persists `response_item`/`message` with
    `role: "user"` and never the first shape at all — checked against the eleven observer rollouts
    of 2026-08-15, in which `user_message` appears zero times and the user-role message 5 to 19
    times per session. Reading only the first shape made the durable half of every interactive
    delivery proof answer "no turn", for launches as much as for wakes.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()

    def write_session(self, *records: dict, name: str = "rollout.jsonl") -> None:
        lines = [
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {"cwd": str(self.workspace.resolve()), "originator": "codex-tui"},
                }
            )
        ]
        lines += [json.dumps(record) for record in records]
        (self.sessions / name).write_text("\n".join(lines), encoding="utf-8")

    @staticmethod
    def record(kind: str, payload: dict, *, at: str = "2099-01-02T03:04:05Z") -> dict:
        return {"type": kind, "timestamp": at, "payload": payload}

    def turn_after(self, since: float) -> float | None:
        return latest_user_turn_for(str(self.workspace), since, session_root=self.sessions)

    def test_both_shapes_of_a_submitted_prompt_are_a_user_turn(self) -> None:
        for kind, payload in (
            ("event_msg", {"type": "user_message", "message": "wake"}),
            ("response_item", {"type": "message", "role": "user", "content": [{"text": "wake"}]}),
        ):
            with self.subTest(kind=kind):
                self.write_session(self.record(kind, payload))
                self.assertIsNotNone(self.turn_after(0.0))

    def test_what_the_provider_writes_without_a_prompt_is_not_a_turn(self) -> None:
        """Everything a session holds that is not somebody submitting something."""
        self.write_session(
            self.record(
                "response_item", {"type": "message", "role": "developer", "content": [{"text": "skills"}]}
            ),
            self.record(
                "response_item", {"type": "message", "role": "assistant", "content": [{"text": "done"}]}
            ),
            self.record("response_item", {"type": "reasoning"}),
            self.record("event_msg", {"type": "task_started"}),
            self.record("event_msg", {"type": "token_count"}),
        )

        self.assertIsNone(self.turn_after(0.0))

    def test_a_turn_before_the_send_is_not_a_turn_after_it(self) -> None:
        """The window is what makes this a delivery proof rather than a session history."""
        self.write_session(
            self.record(
                "response_item",
                {"type": "message", "role": "user", "content": [{"text": "earlier"}]},
            )
        )
        recorded = self.turn_after(0.0)

        self.assertIsNotNone(recorded)
        self.assertIsNone(self.turn_after(recorded + 1))

    def test_the_journal_answers_yes_no_or_nothing_and_the_three_stay_apart(self) -> None:
        """A journal that says "not yet" is not a journal that is not there.

        The difference decides who gets to answer: only the absent journal leaves a caller with
        the screen, and the screen says yes for every pane that has ever worked.
        """
        self.write_session(
            self.record(
                "response_item",
                {"type": "message", "role": "user", "content": [{"text": "wake"}]},
            )
        )
        recorded = latest_user_turn_for(str(self.workspace), 0.0, session_root=self.sessions)

        def answer(workspace: Path, since: float) -> bool | None:
            return provider_turn_started(str(workspace), since, adapter="codex", session_root=self.sessions)

        self.assertIs(answer(self.workspace, recorded - 1), True)
        self.assertIs(answer(self.workspace, recorded + 1), False)
        self.assertIsNone(answer(self.root / "elsewhere", 1.0))
        self.assertIsNone(
            provider_turn_started(str(self.workspace), 1.0, adapter="shell", session_root=self.sessions)
        )


class ClaudeTranscriptPathTests(unittest.TestCase):
    """Fixed Claude-style folder names are independent of the implementation under test."""

    def test_the_project_folder_name_matches_fixed_claude_catalogue_pairs(self) -> None:
        pairs = [
            ("-home-dev-workspaces-codegen-orchestrator", "/home/dev/workspaces/codegen_orchestrator"),
            ("-srv-Sample-Project", "/srv/Sample_Project"),
            ("-srv-sample-project", "/srv/sample.project"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, cwd in pairs:
                folder = root / name
                folder.mkdir()
                (folder / "session.jsonl").write_text(
                    json.dumps({"type": "user", "cwd": cwd}) + "\n", encoding="utf-8",
                )
                self.assertEqual(_recorded_cwd(folder), cwd)
                self.assertEqual(claude_project_dir_name(cwd), name)
                self.assertNotEqual(str(Path(cwd).resolve()).replace("/", "-"), name)

    def test_an_underscore_workspace_is_confirmed_by_its_transcript_alone(self) -> None:
        """The delivery criterion, on the shape of workspace the incident was reported against.

        The user turn Claude persisted after the send is the proof, and it is read from the
        directory Claude actually writes. (Until secretary-1723 this went through the pane's
        `turn_started_confirm`; the provider journal half it proved is `provider_turn_started`.)
        """
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "codegen_orchestrator" / "codegen-orchestrator-1166"
            workspace.mkdir(parents=True)
            projects = Path(tmp) / "claude-projects"
            folder = projects / claude_project_dir_name(str(workspace))
            self.assertIn("-codegen-orchestrator-codegen-orchestrator-1166", str(folder))
            folder.mkdir(parents=True)
            (folder / "session.jsonl").write_text(
                json.dumps(
                    {
                        "type": "user",
                        "timestamp": "2099-01-02T03:04:05Z",
                        "cwd": str(workspace),
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            with mock.patch.dict(os.environ, {"UMMANU_CLAUDE_PROJECTS": str(projects)}):
                self.assertTrue(provider_turn_started(str(workspace), 1.0, adapter="claude"))
                # Everything the criterion says is about the boundary: a turn older than the send
                # is not this delivery's.
                self.assertFalse(provider_turn_started(str(workspace), 4102462000.0, adapter="claude"))

    def test_a_transcript_under_the_old_folder_name_is_not_read(self) -> None:
        """The path the reader used to look under is not a second place to look.

        It is a directory Claude Code never writes, so anything found there would be evidence
        somebody else planted. Keeping the old glob alive "just in case" would also keep the
        defect alive on any host where such a directory did exist.
        """
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "codegen_orchestrator" / "ws"
            workspace.mkdir(parents=True)
            projects = Path(tmp) / "claude-projects"
            stale = projects / str(workspace.resolve()).replace("/", "-")
            stale.mkdir(parents=True)
            (stale / "session.jsonl").write_text(
                json.dumps({"type": "user", "timestamp": "2099-01-02T03:04:05Z"}) + "\n",
                encoding="utf-8",
            )

            with mock.patch.dict(os.environ, {"UMMANU_CLAUDE_PROJECTS": str(projects)}):
                self.assertIsNone(latest_claude_user_turn_for(str(workspace), 0.0))


def _recorded_cwd(project: Path) -> str:
    """The workspace one Claude project directory was named after, as its own logs record it."""
    for log in sorted(project.glob("*.jsonl")):
        try:
            with log.open(encoding="utf-8", errors="replace") as source:
                for index, line in enumerate(source):
                    if index >= 200:
                        break
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    cwd = record.get("cwd") if isinstance(record, dict) else None
                    if isinstance(cwd, str) and cwd:
                        return cwd
        except OSError:
            continue
    return ""


class TuiCatalog:
    def __init__(self) -> None:
        self.heads: list[str] = []

    def prepare_head_workspace(self, head: str, workspace: str, *, role: str = "") -> None:
        return None

    def head_launch(
        self,
        head: str,
        prompt_file: str,
        *,
        workspace: str,
        role: str,
        launch_prompt: str | None = None,
        identity: dict[str, str] | None = None,
    ) -> HeadCommand:
        self.heads.append(head)
        return HeadCommand(
            "CODEX_HOME=/tmp/codex-home codex --dangerously-bypass-approvals-and-sandbox",
            prompt_after_start=True,
        )


class RecordingTuiHost(CommandHostRuntime):
    """A real host whose TUI heads are raised on a recording supervised backend."""

    def __init__(self, root: Path) -> None:
        self.catalog = TuiCatalog()
        super().__init__(self.catalog, root, mode="real")  # type: ignore[arg-type]
        self.preflight_codex_run = accepted_transport_run  # type: ignore[method-assign]
        self.backend = SupervisedBackend().install(self)

    def _require_workspace_environment(self, workspace: str) -> None:
        """TUI transport fixtures exercise delivery and run no candidate command."""
        return None


class DeliveryReceiptTests(unittest.TestCase):
    """Whether the composer accepted a pointer is asked of the persisted evidence alone."""

    def test_a_record_that_carried_the_derived_receipt_is_read_back_inertly(self) -> None:
        """`to_json` publishes a derivation; `from_json` restores only what is stored.

        The derived key travels as `delivery_receipt`, but a persisted payload that ever carried it
        under the property's own name must be ignored rather than raise on a read-only property.
        """
        record = DeliveryEvidence(stage="acknowledged", turn_confirmed=True).to_json()
        self.assertEqual(record["delivery_receipt"], DELIVERY_RECEIPT_ACCEPTED)
        restored = DeliveryEvidence.from_json({**record, "receipt": "refused"})
        self.assertEqual(restored.stage, "acknowledged")
        self.assertTrue(restored.turn_confirmed)
        self.assertEqual(restored.receipt, DELIVERY_RECEIPT_ACCEPTED)

    def test_the_receipt_is_asked_of_the_evidence_and_of_nothing_else(self) -> None:
        """`accepted`/`bytesWritten` and a stage are not a receipt; the composer's answer is."""
        written = {"stage": "payload_written", "send_accepted": True, "bytes_written": 1315}
        self.assertEqual(delivery_receipt_state(written), DELIVERY_RECEIPT_REFUSED)
        self.assertEqual(
            delivery_receipt_state({**written, "payload_left_in_composer": True}),
            DELIVERY_RECEIPT_REFUSED,
        )
        self.assertEqual(
            delivery_receipt_state({"stage": "acknowledged", "turn_confirmed": True}),
            DELIVERY_RECEIPT_ACCEPTED,
        )
        # A turn the provider recorded does not survive proof that the pointer is still sitting
        # in the composer: the direct, prompt-specific negative evidence wins.
        self.assertEqual(
            delivery_receipt_state(
                {"stage": "acknowledged", "turn_confirmed": True, "payload_left_in_composer": True}
            ),
            DELIVERY_RECEIPT_REFUSED,
        )
        # A bring-up that failed before a prompt existed observed no receipt either way.
        self.assertEqual(
            delivery_receipt_state({"subject": "worker-launch", "reason": "split refused"}),
            DELIVERY_RECEIPT_UNOBSERVED,
        )
        self.assertEqual(delivery_receipt_state(None), DELIVERY_RECEIPT_UNOBSERVED)
