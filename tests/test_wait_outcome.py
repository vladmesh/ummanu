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
    "worker/paused": "390b45a3641a09bc418275ed069eb4f8c81524a190ff4744c2a793e5d6f853dc",
    "worker/identity": "5617a0253b444da1e59015265d62cc610a9144f539608801ed48c6ff5466c13a",
    "worker/provider-forward": "7d18cafb5a775bf6b9f47e70d63ba7cdee5a0ce50d2c0054053a3f21aaaf1ad5",
    "worker/healthy": "76884c7034a3cc5180e91cf0d87d389b5dbbfc3171c62743ce597eac62f8f94b",
    "worker/healthy-degraded": "bab7530cab398d4c5212e63e73d800eb4ad23c6ecc659654691a2dc125a6f5c4",
    "worker/healthy-escalation": "3c3921092de837f026d44ba431e1837afb59a21d92e419eb407dbb431a99babb",
    "worker/suspected": "3651a1476319ad8773093d8d2909c6205e90daab1d370fb66764a21ea907f8f6",
    "worker/retained": "4a22b49999005d088cd513698fa2a37fdd635715a5b7cd71c4a5d4252faa0ab6",
    "worker/retained-degraded": "8f4dc17f0f83883729760bf6b99c7e5eda0023732911c9510ead4d28506e897d",
    "worker/no-observation": "c73dc86cf6802fdbc430bc71eeb6023bf77ce539ac3ad9f73498daee5f79f607",
    "worker/no-observation-degraded": "ae942d0518cd76d787da202cedd02c81cbbab305e4cccb417356a9d11d09d887",
    "worker/unobservable": "928ebe2baeeb4a52d331de5e6a56126a4ae84cd77a6ff86185b0793b2e9fcb43",
    "worker/recovery-missing": "78093f19702d98c34271262535440d19967e9001eb685ca90423e02a2a73f21b",
    "worker/observe": "621cb42486f459fadbbcc8536dbe9252ca331f21974de9cf63aeff80fc40d934",
    "worker/observe-escalated": "3c1c35e06bf68e5fc03417944c688c1da7930338e761ba0b8f073ff3952d8115",
    "worker/sigcont": "fbfca549783e604294bedaa7aa2c8ed13a0b44de867bf2349feaa0358170a8a8",
    "worker/sigcont-foreign": "b2d37da50dde32be3c5a30b6aec40a4de3a49d07f6b161014e8a2f277ea29f3b",
    "worker/suspension-escalation": "ed6b874439528afad3416cd2aea5c064cd1bcd42239d40dfee6f1ad1c3ce44af",
    "worker/guard-missing": "a3a37456c842b0a49aa01a75de470b55e16eebf64a61f7ef717b71c99f956656",
    "worker/guard-foreign": "fe190a4211b27b9585e3640558c708b05222d7569f8fd688317d441a0e4bd8dd",
    "worker/guard-allowed": "d87153bf3d87edd4cdd1ffcf693059dcb0072b0286416ffd3af734134df2636d",
    "worker/respawn": "236ec89d11c001c368019a6ce0c3d0ea78b661d7913974eb900a6c663bb652c8",
    "worker/respawn-degraded": "4e884fa55523561c18ea0c67a5685ed495017cc2c6bda5301e3f5a9e4cd011a6",
    "worker/respawn-refused": "e2cdf89826809edd62f36e44742afd0a7aea227ba003340e75f1e732de342960",
    "worker/respawn-failed": "29b9cae88e0200ed9dc63ed519fe6377642593a8d319d3d602ab587681ecc089",
    "worker/intent-unwritable": "22179428f9ba859b19fadf2267945d1637f3df497c1937cdb044b4b0f105e8e1",
    "worker/terminal": "3bb73a3ae884358ceb5d77d9bd59153b059e3fc0bc4d388b6e8f87c11629e92e",
    "worker/terminal-refused": "e2cdf89826809edd62f36e44742afd0a7aea227ba003340e75f1e732de342960",
    "review/paused": "6de4e29601d01586369009dae46ff43de04bc5b4a684b4fb0954b5e705794aee",
    "review/identity": "7ecc39055d0bab0b72f6518ba4611357d670c643cfb145f484c63890bb92f524",
    "review/provider-forward": "889b0bbf8872da962b89ad9d15138e5d76f0a11ff99277209981360ce8e2e6d7",
    "review/healthy": "3d131f7c1c6597887df75d8d1f12954f0506ab2a91b5eb9118a5073a8b49b924",
    "review/healthy-degraded": "5c4e759d5fc86758a6ac7872769410f413d597f21fc714cbcdfcb4cecc787820",
    "review/healthy-escalation": "728e36053dd714b7637d8bf6156caddd1b92ff33e6e3d17ad0602902a9bbdfbe",
    "review/suspected": "3434e28faae96e95f9082456f3217376af127b1d37624c3267101bcc329ef242",
    "review/retained": "6863fbf43fd8191012273745c5ca7802719fe4a4e91e6ff1a1a4d2caabb1782b",
    "review/retained-degraded": "b3f16aa5c1400a7d232cbbe01337be50acc571f521395235938a8b8f7d6421d5",
    "review/no-observation": "40b2f2c47e2bf6d8d1417dacf1e9991536126b7ef84cc42d4e4eee492c190fcc",
    "review/no-observation-degraded": "01b27da9d66ffb358b69d2a19e6e87b4cfd09026c1a67502b5c43d5a241db6d2",
    "review/unobservable": "61ca3fbe20adfb462ff2f06d5d02a1486aae8c588b2175bf230206dc5510a5e2",
    "review/recovery-missing": "c3db6f0a056d36e66db925ccbf015fbbd700b97d6e2ed5bbf39d880120dda239",
    "review/observe": "620ee091d4ab1d6ae176273bc3b4deb4bfb6f20a2f648296f6618a639922cc38",
    "review/observe-escalated": "cb81c9ea57f2e6886df38a03ade90daa83ef3535d2c61160f368ba6dabb081c5",
    "review/sigcont": "b76f451169af4a9268e0a024817ee8d17ce403d5a1b2aa144d0ed71b4b3fd113",
    "review/sigcont-foreign": "bf835b54bef93b7406767405c997e2ee959e5970d3edf20c20f3e1ecc5dec1d7",
    "review/suspension-escalation": "bbdb5bf752ca42c65472a7a6b40e4767a41314298be89dc2480abc43e15f713a",
    "review/guard-missing": "2d5c9fde9f7abff2f2bcddce36ccc5ca8e9838524c37b99daf7766d2a06b8fd1",
    "review/guard-foreign": "2277fabd7b09ca374a82cac55d38cff05c9876a18012af44672b70d11d149cb6",
    "review/guard-allowed": "15817eb9aaf8444782d3c026ffbc518b69380c47a13d38b7af0c9ec746a3578e",
    "review/respawn": "ab6a986342d053d172f232efd64df68391069c0f8313188ece9886666547da34",
    "review/respawn-degraded": "2364d5d61fe4d9d3258e9037eee16a3443a28a772c0ff93d3e8957cf8bb61d20",
    "review/respawn-refused": "d560a891afbbf1df173ec01358567105f56a2b4169ede20af4b18021be819377",
    "review/respawn-failed": "2a9b4dc91830631cbb00374ccda2724471d02409b47cdf4931b0d9aad2e502fc",
    "review/intent-unwritable": "ab6a986342d053d172f232efd64df68391069c0f8313188ece9886666547da34",
    "review/terminal": "3b4848bd137147a4dd7ba862341e105ee07cb8e6e782f48e3b4e2722908cd585",
    "review/terminal-refused": "d560a891afbbf1df173ec01358567105f56a2b4169ede20af4b18021be819377",
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
