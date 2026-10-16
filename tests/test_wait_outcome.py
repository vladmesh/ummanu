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
    "worker/paused": "bb35b479f0f431882e86a41d4cf9c94f3643546f216605c6cb50d2570bb9864e",
    "worker/identity": "06100ddb7acd9c084fcf7eb32f9cd928138033a9527cf31de5718078cdf442ee",
    "worker/provider-forward": "86ccda2df8fb257fd14b7477b3813e1d721645fa7dcb92a358de7e44db088736",
    "worker/healthy": "185e05d2ddaf722dcac86a2dfdb21ecc1f32013ef975338ddfef521784b0699b",
    "worker/healthy-degraded": "290b574162fe24a77b479ff531c2fa17c0b6009cc3cce1aadab108be290acfdd",
    "worker/healthy-escalation": "057df91ef64f4e6189b462ad5987c56256bd782ddc0821c0de45bdcf860dc0fd",
    "worker/suspected": "f5644c525ae8c355d56005dc3c381a70d22f6d9521b50738cbcb7b3ed8f50487",
    "worker/retained": "ef488ae5b64cb122127ea164b67d6085b3099df203dc3db7b395cba5e7caa373",
    "worker/retained-degraded": "8e58ac1996b773bfcf791dcd688fb14656042ff2bf5add45fae5e2f755223a4e",
    "worker/no-observation": "a5f8882bd5c43634a36b0ae44d849d845e0cc4dc0c3f0b75667b98f4e58de1af",
    "worker/no-observation-degraded": "8c11d4cbfdcbdfadd4915bcdaf90bc9f8f6b0514761355f8f9f9e12e32362e76",
    "worker/unobservable": "8d21967886c3bd2043848d98b4b4aef9b7e3cbffa91be6273a2429703140aab2",
    "worker/recovery-missing": "0cc5ce7e31b0c74be9263927b9d84ae9407f304cf7b65919e9d29903aa309fb7",
    "worker/observe": "0ccbcfaf42278e60564569b8169d474d43eda7199ab36dd84ad80ee5b2b74ad9",
    "worker/observe-escalated": "b5302925991ba77381231efaecc2fcbdb15561a0e386804d409d33d453af185c",
    "worker/sigcont": "beadaf590a79a262341cb7808298d1e4d5668fb385632c84e188daad83f27583",
    "worker/sigcont-foreign": "4945fbf3503b6796ead3dc188fc7c0df4579ff304af46c112201f5262544a625",
    "worker/suspension-escalation": "306f95563dc159db33f19cc58795cd29654da0af094bbbc4f96e43be106310b0",
    "worker/guard-missing": "c5a1703ad2799d576bfec14c71d85f30a778fe273e56a3e7467673bd65bece50",
    "worker/guard-foreign": "64e70ca136c86d46d7923fdaa59024ee168f8b186e3fbe79806160a8c834b20c",
    "worker/guard-allowed": "0e2e1a99e6b9050cb9f42434e3cf06bf8ea66b44508ee64af3ba9907ed68bef7",
    "worker/respawn": "dfae83472071b7f4b4adc5a6415b378b4c9a4b07e660ecd0bc9efb27f88368bd",
    "worker/respawn-degraded": "98986a9a5396494a31addc32812b69ab8a4f0553f32a9c2d92c6792d1eb137f1",
    "worker/respawn-refused": "265c1d069e4a967411380eaf70322cb6af412a462759d587c9c518161df1468a",
    "worker/respawn-failed": "dd2200c75b88767c891bb485dc75576840282fbbfa75fca0d064a65e5366995a",
    "worker/intent-unwritable": "aec526e34eb2b80c8d84799a55b3abf9c35235564e528c41e8982f2e3c7d6906",
    "worker/terminal": "c61fa424aa85ad1c4c0922704189de214c3b6464f3d3c9fd560c168b0d8a05ce",
    "worker/terminal-refused": "265c1d069e4a967411380eaf70322cb6af412a462759d587c9c518161df1468a",
    "review/paused": "96c3a419bf54251710843145a5a038120f25dd237862ceeb325b194084444751",
    "review/identity": "5c471dfacc34ba85f98861f4ee9a3b99841d0e3eff167b40b9d22abeb1aad680",
    "review/provider-forward": "6f80da6b2b3bd818d111342452ace182c945efab9274a125ca243ce358b52676",
    "review/healthy": "faa4f7426083bed189e83bf0dd1ad2c472a827953e362e811a9ef6057a7b8df3",
    "review/healthy-degraded": "dc0145fafe7fa03e1356dc47e1475f52ff2bc350bb630465d8b943d357af6aec",
    "review/healthy-escalation": "5fc920deacf7a895969faaa5b48a16e42d4952ba8a6f4326a284f4f7a60ecebf",
    "review/suspected": "ec48d1b241d7c495acb93732d82296160f55af002a79a250b584f68d644c2ca7",
    "review/retained": "94cc5ffc073a3e8e6499760c5df22becd25358f7129392a6bfef04b22f408bc1",
    "review/retained-degraded": "75e1d258370fc828172b84528f816af60b6aa1dc94a77e41c4539a0e46dce369",
    "review/no-observation": "b9d01131a35b398df67e07652748a2185396dfa0f956cf7701d8a957c9082c5b",
    "review/no-observation-degraded": "25245f1c860bb5bbad018504ee5b2eb0ce04dc06e6a505ff9b6e4627459d8560",
    "review/unobservable": "6006d2a86ef3a1ffa304dffc330c4f341d09c39e6c394975fd1b1ff09ee4441a",
    "review/recovery-missing": "d96daa3ad3e8d393f75bef33cfff6ba35c17fbcc423644a085e45413f65ac865",
    "review/observe": "1fd0ff8eb3caa6750fefff7e3e5d6e9d12a1fe4c5cb4ae1406b4ee71aa96c864",
    "review/observe-escalated": "cc5021cb1f8d0988626cdfe53275ffd2baa238ddac87a41236005310faf41187",
    "review/sigcont": "9c2210791202627b39e146996781bdfcfc33e0f9327e0f5edc317abb8b5cb4be",
    "review/sigcont-foreign": "31758051b0033f6089499bf6cdc1796768323b5ef5f83c8a74daab1d137a8929",
    "review/suspension-escalation": "bbfd609cab05e0e245cf12638e1ef3fb58fdcbc3a65584190afce93b8e2da003",
    "review/guard-missing": "124fff1bd7ad847235cc55c276d668751f2d5412d2ecaabead6c56fcf925e2ad",
    "review/guard-foreign": "09a9f40f92af239f4bf77165cfd46496c426421cc46a8486ca3114f9cf6653eb",
    "review/guard-allowed": "139f1fc370e90a80e86808a35cbc8cc4df30f40c06d78f29b4917a694234ffb4",
    "review/respawn": "f39b122389effc7f32b15c03587c5435f8ea311413f06b3cb9c29fe53725c958",
    "review/respawn-degraded": "c8b002d69dad4c4e903e92a94e3643eb3de79877b2217dc73ed49535842dc4bf",
    "review/respawn-refused": "19cd8735bdce1e026057eff2d81e681098b4f19feb5a334b81f1ade5fd36f62c",
    "review/respawn-failed": "febb1675afa4e644bc246330b7fd64e5fd9f264a4bec7099d2edb413a85a53c3",
    "review/intent-unwritable": "f39b122389effc7f32b15c03587c5435f8ea311413f06b3cb9c29fe53725c958",
    "review/terminal": "c89f21ec2ab717d05e0162bf5738ecbacb65f80db687e3bd9002e34eed87cfe0",
    "review/terminal-refused": "19cd8735bdce1e026057eff2d81e681098b4f19feb5a334b81f1ade5fd36f62c",
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
