"""Producer goldens captured on main 780125e before the mechanical extraction.

The digest covers ordered outcome JSON, ordered collaborator calls (including record
snapshots at each call), and final persisted record content. No real host or board runs.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import signal
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import mock

from ummanu.dispatch import wait_vitality as wait
from ummanu.dispatch.head_vitality_episode import VitalityEpisode, VitalityVerdict
from ummanu.dispatch.head_vitality_guard import GuardDecision, GuardRefusal
from ummanu.dispatch.head_vitality_policy import RecoveryDecision, RecoveryIntent
from ummanu.dispatch.stale_round import WorkspaceReading
from ummanu.dispatch.state import DispatcherRecord
from ummanu.dispatch.watchdog import HEARTBEAT_LIVE_MATCH

CASES = (
    "paused", "identity", "provider-forward", "healthy", "healthy-degraded",
    "healthy-escalation", "suspected", "retained", "retained-degraded",
    "no-observation", "no-observation-degraded", "unobservable", "recovery-missing",
    "observe", "observe-escalated", "sigcont", "sigcont-foreign", "suspension-escalation",
    "guard-missing", "guard-foreign", "guard-allowed", "respawn", "respawn-degraded",
    "respawn-refused", "respawn-failed", "intent-unwritable", "terminal", "terminal-refused",
)
GOLDENS = {
    "worker/paused": "ed0e04ca00e714a62acb83a983e4c859aaa8900609a24d3608585e60a9e3580c",
    "worker/identity": "60421802f5bebee7393d59d2b3140a8fefdcefcb6952ec8cf8f69a07d48ad2c9",
    "worker/provider-forward": "f19668af5f7b501847b44f8c60e383409532175288ede65f86ae0a3fed04410a",
    "worker/healthy": "f7df7745940525992659a270b2ae73a8f0a197203f038e0d1b87d05024505213",
    "worker/healthy-degraded": "6e4efdbc5d0d2cbd38d35eb87d478a7a5519dc7d39096fa67d9e47e82546f3ea",
    "worker/healthy-escalation": "aaa0d963e6cca488892bed78dbf352dcea90d8156057ccce848508566ba73899",
    "worker/suspected": "35912b8c9631a599e29c2b2ed31c65a78c2f253f87de434e437cd197f0636379",
    "worker/retained": "c5b374e40f5ec8b6f0b74b736515b2b1f23894de61618f1c42c6f8b06d13621a",
    "worker/retained-degraded": "c432a21ac566796ab6a0e5f2cb851f8539170f869ebe9711129a9c5aaeca1c3e",
    "worker/no-observation": "4fc2bcc2fc52960b359a47c754ef00554a47aca1fdc0a29e2a3098c140e41e55",
    "worker/no-observation-degraded": "06632da3457d28da2936e17550f1c32739ff096a08142b48ac675ba49172a055",
    "worker/unobservable": "93815299e38867d1c082c30049fea12e11d4fbbb94cb845841e79b1e6d3c88ac",
    "worker/recovery-missing": "0fef2dce1c627e201a2763e60775a4ec41b8dc20103080d26e36ef66114655c0",
    "worker/observe": "ec01714a0d558abf4ad49afca0925453e1225ff455a79cafb12e39a79c0f009f",
    "worker/observe-escalated": "ab265c16da61889272046ad7d6c25eff5217e5759c5d57371e4d2cfb0f010fbf",
    "worker/sigcont": "9695aacc95354b6e9a30f45009a02346afbe193fe90487d1007371d6025f6000",
    "worker/sigcont-foreign": "131b7d0d523a6e2ef38dd3aa42a7419d07f9ed1b98bb797ff50dbd071538a442",
    "worker/suspension-escalation": "c2f789856d2c19305651445b4d95c0b42196e687369317f0de9eec9e60a9797b",
    "worker/guard-missing": "7f184aa5f2f839a65c1baa6152f3f7065df28faa1c16650a8d9a635841cf527a",
    "worker/guard-foreign": "3c38b15be4fa63003f26fcab8296c6302354d82653ed378fb939226cb1a97f20",
    "worker/guard-allowed": "51d8a4b2e30970b6d1bf42c26f6954763cdaa4859497b132bd444d2e6c4ed11f",
    "worker/respawn": "6f44cee68e72fdeac18f87ee0a8b4010caafb6ee7cd4a553f234192d1fb6f502",
    "worker/respawn-degraded": "fe3b7849cbeca71b78d9d00bab76c3ec55af17041cebb7afb2eddf607fff64c8",
    "worker/respawn-refused": "9f4e420e50585a7a9655ee994cb508e45f311fbc8b3fca893eca2ef0146ddf04",
    "worker/respawn-failed": "4916ac5504b1d6b3b18f95cb9200ee94b84e79f88e601d0fd24aa7750ae99362",
    "worker/intent-unwritable": "157a08c18a0ef7d658993ca0f1c149d1438b0cebe16a6f0c5b6c1404e2e3be52",
    "worker/terminal": "12da2a135c2156ccab5b940b1e1b6a40a50cde2923b444596041bab2f6d172cf",
    "worker/terminal-refused": "9f4e420e50585a7a9655ee994cb508e45f311fbc8b3fca893eca2ef0146ddf04",
    "review/paused": "324a8f978e994451045e94bc758fdb739ef756e3f9ea5cae38b7f730915f8a23",
    "review/identity": "6fb4b6b1fac12fd11bcb8541e62d215499a7342ad87e219b8755de7579ac37b0",
    "review/provider-forward": "c81670c1acf3fe55954e868a7d72aca42f40e01c8bd0bbe1ffd15920ca1eba2f",
    "review/healthy": "545a6c5f6a31886aaf3b34f506d86be45970540cc3ed05fdfe3844d0c1087682",
    "review/healthy-degraded": "08a4dc406589d3ab32cce534cde4901a920545e907513e7a6fe8285d48d6c5cb",
    "review/healthy-escalation": "1734eceee16e31817b2df77d0f58d883d4d15051646f345f8bf4c691fbb873de",
    "review/suspected": "a3e5f65b8b0ba6641af8dc8c661bd99f009a1b8843c7a9a4b7a039eab8273767",
    "review/retained": "4ad0ba8f430ddb0255642570246aa729f59a2b8cc1c9dec403c012f8ce4c7271",
    "review/retained-degraded": "c8c4cc5e24132ae879846981e6a7f698618b3e99707f524a2974729a0a451424",
    "review/no-observation": "d5d0ade4dd06d6a735fbf31614f42f176d2f4862e61608559faec455a71854a4",
    "review/no-observation-degraded": "e33e1772709bd59ab20733f17353a886dd0db3887cef28bf35f4cab15f6cf53a",
    "review/unobservable": "da850ae4069490d6690a7d2b7f3f0a7151e5342ee74f078c5fd2c4349f8a14dd",
    "review/recovery-missing": "15c1a2e6d79a7df06ff9e24618f8754e6e90370d53a0262385026e86b2299114",
    "review/observe": "5e5d46349feab90bf27d7bf91b138b1bc61364adda7660e4f0307cc729cda37d",
    "review/observe-escalated": "da0d33c9766788c0a6167d4a35396704bc14c7706105b7a7e883135ec47b97ce",
    "review/sigcont": "298d8b62122a7efd8f9d6b9175c180e30834f60cca3c58d3516c60a03d2ddddc",
    "review/sigcont-foreign": "643cc76a925ef0a76d360b9d78e97fbae9ca2fa84a6b9909540b761a130e6b3d",
    "review/suspension-escalation": "45c5c2546cc4109597d540bfdce2a12d1e974b7721c040355d23f98e86810419",
    "review/guard-missing": "56d3b8bbf4be3f838ff5dad01b0cf2500bd68f4ed4ac65fce6ad8fae28bcae27",
    "review/guard-foreign": "0e75159101abe4e61716a3a97e7f24882ae0187024962b0a7936e5e68a93c8d1",
    "review/guard-allowed": "0521301f0b34c2b149127d2a2881c25935b5fd178d8ee488e8382e2a82ab5931",
    "review/respawn": "ac02a8de6716f70940847b83389666e2c95c8ae0e652ee1e4579ba9c0b5bb486",
    "review/respawn-degraded": "ef5e0c9c0f6e5e0bcb8ae5326bc5e7a91c537e7cbe7400ffbcf19f9b9ddfe8d9",
    "review/respawn-refused": "4b19c4c20992d7e9a282a2b7f9429e87a8f2cc832eac0ffdac180ee9ced29162",
    "review/respawn-failed": "d2235f5cf248207e0c4950bb5cfaac7a09dd010097f04db64ecf977738917f15",
    "review/intent-unwritable": "ac02a8de6716f70940847b83389666e2c95c8ae0e652ee1e4579ba9c0b5bb486",
    "review/terminal": "4a30738a5e9adde41920a2aa58bbd8fddf8028d27ef41cd70e938613585f072d",
    "review/terminal-refused": "4b19c4c20992d7e9a282a2b7f9429e87a8f2cc832eac0ffdac180ee9ced29162",
}


class WaitOutcomeTests(unittest.TestCase):
    def scenario(self, kind, case):
        record = DispatcherRecord(
            worker="owner", workspace="/candidate", handle="worker-pane", head="codex",
            review_head="claude", attempt_id="record-attempt", comment_baseline=2,
            review_baseline=3, state="waiting", claimed_at=10.0, report_generation=4,
            review_handle="review-pane", worker_pid_file="worker.pid", review_pid_file="review.pid",
            worker_head_run={"run_id": "worker-run"}, review_head_run={"run_id": "review-run"},
        )
        setattr(record, f"{kind}_waiting_since", 900.0)
        records, payload, task = {"DUP-1": record}, {"marker": "payload"}, {"ref": "DUP-1"}
        runtime = mock.Mock(owner="owner")
        runtime.host.mode = "real"
        runtime.host.head_run_end.return_value = None
        runtime.reader.client = "board-client"
        events = []

        def normal(value):
            if value is runtime:
                return "runtime"
            if isinstance(value, (DispatcherRecord, VitalityEpisode, RecoveryDecision, GuardDecision)):
                return value.to_json()
            if isinstance(value, dict):
                return {key: normal(item) for key, item in value.items()}
            if isinstance(value, (tuple, list)):
                return [normal(item) for item in value]
            if isinstance(value, SimpleNamespace):
                return normal(vars(value))
            return value

        def capture(name, result=None, error=None):
            def called(*args, **kwargs):
                events.append([name, normal(args), normal(kwargs)])
                if error:
                    raise error
                return result
            return called

        verdict = VitalityVerdict.HEALTHY_ACTIVE
        if case.startswith("retained"):
            verdict = VitalityVerdict.RETAINED
        elif case == "suspected":
            verdict = VitalityVerdict.SUSPECTED_STALL
        episode = VitalityEpisode(run_id=f"{kind}-run", verdict=verdict, reason="quiet evidence")
        if case.startswith("no-observation") or case == "unobservable":
            episode = None
        intent = RecoveryIntent.OBSERVE
        if case.startswith("sigcont"):
            intent = RecoveryIntent.SIGCONT
        elif case in ("healthy-escalation", "suspension-escalation"):
            intent = RecoveryIntent.ESCALATE_OPERATOR
        decision = RecoveryDecision(
            intent, 4 if "escalat" in case else 2, reason="policy reason",
            detail={"span_started_at": 100.0, "response_window_seconds": 300.0,
                    "suspended_for_seconds": 600, "deterministic_class": "invalid_configuration",
                    "identical_refusals": 3},
        )
        forwarded = {"status": "degraded", "step": "caller-step", "reason": "forwarded"}
        status = {"known": True, "live": True}
        if case == "identity":
            status["identity_mismatch"] = True
        if case == "paused":
            setattr(record, f"paused_{'reviewer' if kind == 'review' else 'worker'}_at", 20.0)
        if case == "unobservable":
            setattr(record, f"{kind}_waiting_since", 100.0)
        refusal = GuardRefusal.FOREIGN_RUN if case == "guard-foreign" else GuardRefusal.MISSING_EPISODE
        guard = GuardDecision("respawn", case == "guard-allowed", refusal, "guard reason",
                              "Unverifiable", "foreign-run")
        identity_status = {"pid": 123, "process_alive": True}
        if case == "sigcont":
            identity_status["state"] = HEARTBEAT_LIVE_MATCH
        with ExitStack() as stack:
            def patch(owner, name, result=None, error=None):
                return stack.enter_context(mock.patch.object(
                    owner, name, side_effect=capture(name, result, error)))

            patch(wait.time, "time", 1000.0)
            patch(runtime, "save_records")
            comment = patch(runtime.writer, "comment")
            patch(runtime.host, "worker_status", status)
            patch(runtime.host, "review_status", status)
            patch(runtime.host, "round_workspace", WorkspaceReading())
            patch(wait, "_provider_failure_outcome", forwarded if case == "provider-forward" else None)
            patch(wait, "reduce_and_store_vitality_episode", episode)
            patch(wait, "_recovery_policy_decision", None if case == "recovery-missing" else (decision, episode))
            patch(wait, "_prompt_worker_report", (None, "spent prompt"))
            patch(wait, "_prompt_review_verdict", (None, "spent prompt"))
            patch(wait, "_stall_seconds", 300)
            patch(wait, "_vitality_guard_decision", guard)
            identity = patch(wait, "_guard_head_run_identity", identity_status,
                  wait._HeadRunIdentityMismatch("foreign identity") if case == "sigcont-foreign" else None)
            getpgid = patch(wait.os, "getpgid", 123)
            getpgrp = patch(wait.os, "getpgrp", 456)
            killpg = patch(wait.os, "killpg")
            kill = patch(wait.os, "kill")
            refused = forwarded if case in ("respawn-refused", "terminal-refused") else None
            patch(runtime, "_stop_worker_confirmed", refused)
            patch(runtime, "_end_review_pane_confirmed", refused)
            patch(wait, "_start_review", forwarded if case == "respawn-failed" else {"status": "ok"})
            patch(wait, "_write_worker_relaunch_intent", "disk refused" if case == "intent-unwritable" else None)
            patch(wait, "_launch_intent_unwritable", forwarded)
            patch(wait, "_bring_up_worker_head", (None, forwarded) if case == "respawn-failed"
                  else (SimpleNamespace(run={"run_id": "successor"}), None))
            patch(runtime, "record_worker_routing")
            patch(wait.attempt_accounting, "terminal_effect")
            args = (runtime, task, record, records, payload, "tick-attempt")
            if case.startswith("guard-"):
                proceed = mock.Mock(side_effect=capture("proceed", forwarded))
                outcome = wait._guard_or_wait(runtime, task, record, "tick-attempt", kind=kind,
                                             action="respawn", proceed=proceed, current_run_id="bound-run")
            elif case.startswith("respawn") or case == "intent-unwritable":
                outcome = wait._respawn_wait(*args, kind=kind, now=1000.0, trigger="stall trigger",
                                            degraded=case == "respawn-degraded")
            elif case.startswith("terminal"):
                outcome = wait._escalate_wait(*args, kind=kind, stall=300, trigger="stall trigger")
            elif case in ("recovery-missing", "observe", "observe-escalated", "sigcont",
                          "sigcont-foreign", "suspension-escalation"):
                outcome = wait.execute_recovery_intent(*args, episode=episode, kind=kind, now=1000.0)
            else:
                if case.endswith("degraded"):
                    getattr(runtime.host, f"{'review' if kind == 'review' else 'worker'}_status").side_effect = (
                        capture("status-unavailable", error=RuntimeError("provider offline")))
                outcome = wait.wait_watchdog(*args, kind=kind)
            if case in ("provider-forward", "guard-allowed", "respawn-refused", "respawn-failed",
                        "terminal-refused") or (case == "intent-unwritable" and kind == "worker"):
                self.assertIs(outcome, forwarded)
            if case == "terminal":
                self.assertEqual(records, {})
                self.assertEqual(list(outcome), ["status", "step", "pilot_ref", "attempt_id", "to"])
            if case in ("paused", "respawn"):
                self.assertNotIn("reason", outcome)
            if case == "respawn-degraded":
                self.assertEqual(outcome["reason"], "stall trigger")
            if case == "guard-allowed":
                proceed.assert_called_once_with()
            elif case.startswith("guard-"):
                proceed.assert_not_called()
            if case in ("sigcont", "sigcont-foreign"):
                identity.assert_called_once_with(
                    f"{'review' if kind == 'review' else 'worker'}.pid",
                    run={"run_id": f"{kind}-run"}, role=kind, task="card:DUP-1", leaf="",
                )
                comment.assert_called_once()
                expected_calls = ["_recovery_policy_decision", "save_records", "_guard_head_run_identity"]
                if case == "sigcont":
                    self.assertEqual(outcome["action"], f"{kind}-sigcont-sent")
                    getpgid.assert_called_once_with(123)
                    getpgrp.assert_called_once_with()
                    killpg.assert_called_once_with(123, signal.SIGCONT)
                    self.assertIn("Sent one identity-fenced SIGCONT", comment.call_args.kwargs["body"])
                    expected_calls.extend(["getpgid", "getpgrp", "killpg"])
                else:
                    self.assertEqual(outcome["action"], f"{kind}-sigcont-fenced")
                    for collaborator in (getpgid, getpgrp, killpg):
                        collaborator.assert_not_called()
                    self.assertIn("Could NOT verify the process identity", comment.call_args.kwargs["body"])
                kill.assert_not_called()
                self.assertEqual([event[0] for event in events], [*expected_calls, "comment"])
            snapshot = {"outcome": outcome, "events": events, "record": record.to_json(),
                        "records": normal(records)}
            return json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))

    def test_producer_goldens(self):
        for kind in ("worker", "review"):
            self.assertNotEqual(GOLDENS[f"{kind}/sigcont"], GOLDENS[f"{kind}/sigcont-foreign"])
        for kind in ("worker", "review"):
            for case in CASES:
                with self.subTest(kind=kind, case=case):
                    snapshot = self.scenario(kind, case)
                    self.assertEqual(hashlib.sha256(snapshot.encode()).hexdigest(), GOLDENS[f"{kind}/{case}"])

    def test_builder_inventory_and_explicit_step(self):
        source = ast.parse(inspect.getsource(wait))
        returns = [node.value for node in ast.walk(source) if isinstance(node, ast.Return)]
        owned = [node for node in returns if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == "_wait_outcome"]
        self.assertEqual(len(owned), 15)  # One walk: nested plain_wait counts once.
        bases = [node for node in returns if isinstance(node, ast.Dict)
                 and any(isinstance(key, ast.Constant) and key.value == "pilot_ref" for key in node.keys)]
        self.assertEqual(len(bases), 1)
        payload = {"intent": "observe"}
        for kind in ("worker", "review"):
            for step in ("known-step", ""):
                outcome = wait._wait_outcome(status="degraded", kind=kind, pilot_ref="DUP-1",
                                             attempt_id="tick", step=step, recovery=payload, action="caller-action")
                self.assertEqual(list(outcome.items()), [("status", "degraded"), ("step", step),
                                 ("pilot_ref", "DUP-1"), ("attempt_id", "tick"),
                                 ("recovery", payload), ("action", "caller-action")])
                self.assertIs(outcome["recovery"], payload)
        for kind in ("reviewer", "observer", "unknown"):
            with self.assertRaises(KeyError):
                wait._wait_outcome(status="ok", kind=kind, pilot_ref="DUP-1", attempt_id="tick")
