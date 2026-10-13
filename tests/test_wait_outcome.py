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
    "worker/paused": "defc78541b67a158f93aa772f212583b9efdce0716c9fce6ffe954b6174d74f6",
    "worker/identity": "125410068ce726d4a30309b851b4122ce4072822494e78bdd42b926ca6ba0be7",
    "worker/provider-forward": "8d1d747e1f8388078a1ee5a8927a64f9004912d40a1511932c68c0293ae28d5a",
    "worker/healthy": "258db0c9bd0658675871e412c1509ee5b4c13d13a6d970f55e91d7ee5eae31f6",
    "worker/healthy-degraded": "1925059bdddc1817cffd45c39f6f99cefa4faf8f03b30f3dd54c0a16d1525d37",
    "worker/healthy-escalation": "a063e18444b2e75a47de613d54951d0cc49a6054680dbd7f0ea9dc456055dbe8",
    "worker/suspected": "a83c381a5912dbc2ac1ae6fc390bfb44e432aa1bcbd5296801dd4307d2d647e4",
    "worker/retained": "349c610f5c84edc64bdf320c64d5bde5975c22df3f25c13b3fe638a28cec2fbb",
    "worker/retained-degraded": "3784c31046ec691d1437e31254c107b5a1fd944b685d6cde70adb415c329227e",
    "worker/no-observation": "4a3682afa1c9cfcaa0d94632870e880e64b024ca6a1b8ee93d914747b9513670",
    "worker/no-observation-degraded": "da9565a5fd81e6623757a6092efe0642d4b673637412fc4aa6c461bf26723498",
    "worker/unobservable": "46215ab954508e937fde84c2c06c312ef5688a4d278f0fc940ad3f91d248dd15",
    "worker/recovery-missing": "917d513537685df2c9fb179ab40b5be476cfc2efce3bc70f83a1665e531334a5",
    "worker/observe": "c48ffdc1c80ffd27f1d36f1528842d2b7979c54fad1bbc7ab2404bd911c85e41",
    "worker/observe-escalated": "bd0faddc798ad80089e129bd0d4c1f0e698fa49c9c1ad0e60796b2b0ba983dda",
    "worker/sigcont": "d291ba20853c9e9dab1cf254893491ee5bc9cad23e42f1db2256902dfca90cb3",
    "worker/sigcont-foreign": "c11df95e369b6d702e9265df4d176485a348160df278b2320191aa881caec305",
    "worker/suspension-escalation": "9dd2280787b1b7302ac928efaa9be38b2c74778ee6b51967283c57d3d5cf82f7",
    "worker/guard-missing": "804e6e1d3d6ac53bf5898890aa54d3a5757df526383b3150063efbfef1245499",
    "worker/guard-foreign": "0d6494378493c181003ee8cceec76804249b94cbb269619496fa8491d859ea2f",
    "worker/guard-allowed": "65411a4cd5b1992f45fee46e80f878dc409cf6e80cc517c011cea9970b746b25",
    "worker/respawn": "dc914b532e17a56fb8d2eb49735172b78c5843dc3eae1b81e2908c0f527ace0e",
    "worker/respawn-degraded": "c876c67dd2f87f04ef5b2ab3770a4007319aad5e878ffb628f5b65612905d248",
    "worker/respawn-refused": "ee81b8b140475088c42e45e8b7f3b7dc922dc840c00fe04c2f37dafe03415ae9",
    "worker/respawn-failed": "092196c5bdbd083ff27b20f5b3f373245c96426eadde778339c4ab22126b9d25",
    "worker/intent-unwritable": "5c0ad8403b27aae5660603d1bd425f9749fc5d18d328432bef63000bd11d4613",
    "worker/terminal": "4b1575446d073fd513d6328a99c4661c4e761913a642e2cc254d262533295c29",
    "worker/terminal-refused": "ee81b8b140475088c42e45e8b7f3b7dc922dc840c00fe04c2f37dafe03415ae9",
    "review/paused": "97591e9b502e7703b0b5726662ba6b11d29c29d6de8623b203a2509bb3ff763f",
    "review/identity": "40423e873d55fe3e99cb89e879298ba99d7334f671ebff009abfe030e25955cb",
    "review/provider-forward": "a877ead9065e21509a322445a79ae23c5a557b40d074a0c03fb58619a1e7cf38",
    "review/healthy": "4b86fc5a4e43c732806169c6253ffadafdb88696704fe61853380db8dfa3dc10",
    "review/healthy-degraded": "ee193823938b6135bc63114131a8c02862bc0ac0905cd27a3c4abc3a7ba8d98a",
    "review/healthy-escalation": "fc2e9de0934dfada6b317bf3ce2d0009bcc9c470959b2c9871ed77a61488d81e",
    "review/suspected": "1ee2a8926e66f2770ada943acc1451fe9613f243017b2f56a380f80727adfebc",
    "review/retained": "8d5136a475fa46d3f54818f221b8b008170d187fb98abc356bca429196f88815",
    "review/retained-degraded": "ed8b3a10970bbb78e093ef3f2548796126641de7c80df94dd6f402c9cf84a2f1",
    "review/no-observation": "e9e9e626177853e479d8b6fe9dfbc89b297cdc1061b0c51a4c7cd7ab3160b13b",
    "review/no-observation-degraded": "51f92a9466ec045475c48be3b879d300ffb8b5b75715cb10648e8b74deede6dd",
    "review/unobservable": "733a8f91f8e4d7501c8b800f20a3442d561ff4be0a0672c16b44001e2a8abb22",
    "review/recovery-missing": "97fe51e06bc5d787f6c71f69ab3558acd4e98e3f81b76a623693a9aa6dc5a195",
    "review/observe": "72e7488efd9ac10b3edf84369e95295c7bcbed542ab76d3ad19f75c20ad57a2f",
    "review/observe-escalated": "00f7aff1b89d3231583dff83db203554f34b033294f54f149d4d7bce231215cb",
    "review/sigcont": "16fc4bd00effc4efbbd519885bc28771cb4394987456d61ba038717c8e1a08dd",
    "review/sigcont-foreign": "5d76afd7f5865d54aa450c4e3662afea2f32c69c0fdb0be38aafafd63baec0ad",
    "review/suspension-escalation": "a9dbd32852e82e05fc3542e2ccbab4ff2724f1214b48b8d4b77060087c5e12ce",
    "review/guard-missing": "507a536da0b371805117591921a43a7e0451f4535e36c332312d01d1942037ba",
    "review/guard-foreign": "73697126f2de399fd766f6f706549dc00d097937aaf83e4c654597833f03f224",
    "review/guard-allowed": "50b501fcb0d5681177f525de8e51d7420d717fbc9144ea6172c0d8ff13977460",
    "review/respawn": "ab615b885a4b0338d056bd2bbeaef1d178b3b50851a0ba692484ac27797dd23f",
    "review/respawn-degraded": "bfc438ba50588823c5207ac058aef478f394a73bd6c416df436106a6f5a1b88e",
    "review/respawn-refused": "13064f269fbcfc6a15a66bdc8c9f45bee20ce7f152262602a99a18de628dead0",
    "review/respawn-failed": "68487050678edba760730488e2103b31a131475a6c2fd6d9be9a879637a20d70",
    "review/intent-unwritable": "ab615b885a4b0338d056bd2bbeaef1d178b3b50851a0ba692484ac27797dd23f",
    "review/terminal": "9fb6bc057b15f3c5a76ee9ffa742812986138841000c8ba0585dbd6e5b7a78b6",
    "review/terminal-refused": "13064f269fbcfc6a15a66bdc8c9f45bee20ce7f152262602a99a18de628dead0",
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
            patch(wait, "_provider_failure_outcome", forwarded if case == "provider-forward" else None)
            patch(wait, "reduce_and_store_vitality_episode", episode)
            patch(wait, "_recovery_policy_decision", None if case == "recovery-missing" else (decision, episode))
            patch(wait, "_prompt_worker_report", (None, "spent prompt"))
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
            patch(wait.owner_events, "record")
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
