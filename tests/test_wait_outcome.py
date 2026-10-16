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
    "worker/paused": "5d365db8b22799f7af7756bd0be7d2175ab2bd4c1df7486012258b9dcbf49623",
    "worker/identity": "b2cefcc41830d742a47b4d400760b20c79f70f7ac39896b8cebc15d7262dd891",
    "worker/provider-forward": "0830a26c5e730912bf815d7049cfae1439122d093db0f6cd4c36c9a09703c877",
    "worker/healthy": "5807fcffb61b8e0480ebca6f305b4bc5a13b154da7e8b2dfd56b390094704177",
    "worker/healthy-degraded": "6e28a56e0291a96d674d27ea9b0217634455dbcdbeddb3ef8c94552d8810cd39",
    "worker/healthy-escalation": "a2b7b8744071eac9e4b9ce70999493dfa2e8167df5d2ad12cad1b45d46893444",
    "worker/suspected": "a1901bf0e1d9456ce3dab3884cac27ba4e43aa6b76f70864c8b33e362f3e5a76",
    "worker/retained": "87dbfcfed45f2c7004d22f9f232f613b65232f98d57350aaeea51e0ccb68ab4d",
    "worker/retained-degraded": "efabeb6f8cdfbbaa359471b738280c99feccc2e5951460c7f77f1e898e90de8d",
    "worker/no-observation": "218db49f8c281f867e9b7849ccd4f90820cd53fe4489243d77692dbf4e5bef66",
    "worker/no-observation-degraded": "dbc968b06292ebb4cf4cf190c1662db675f57eb2889ff315bac0d9bd0546a78b",
    "worker/unobservable": "b30ec27c1cc4494b80d5cdbdff1fe257ccecdfff9bd99b260ae21565f0fb14fe",
    "worker/recovery-missing": "453b83867053564de26807a799ea6ea18a6a0014dd6f68d522c02e520836be2c",
    "worker/observe": "ce9d091bdcc28be06bc533e1c64e0648fce231ce05e76fcbbda4b3098e5f4667",
    "worker/observe-escalated": "47b68d77cf964035da6af330e5ba916f767814d0d070c9b978f3dd9887533656",
    "worker/sigcont": "562d59b5d0d170fb75c8cfd708af31c712c33f804a78e314965f969915544257",
    "worker/sigcont-foreign": "cb71d0c51bd91c33b2539ae84b1a084d192f668889ffed34c0092367d2534737",
    "worker/suspension-escalation": "af69f3d91560cf4e9736f4b0bdca41cbdf173e94ab463745a87a902ba8612fc6",
    "worker/guard-missing": "e48c526d33cb07814194c5b36e799f5c9e0f49b04e5cde5ee1ef2a8009d14b31",
    "worker/guard-foreign": "012aab644c65ff54dac7c8134d1e71bad05b00b5f6f69bf5ea696ac3dceeb985",
    "worker/guard-allowed": "756bdce312299d5e6a737c7bfebbb223271b6b036244ae347907c8f22696f7be",
    "worker/respawn": "51397dffd94fc4bd7aee4b244a2d358fcd96dbbc82be8b72adcd43cb6b981f97",
    "worker/respawn-degraded": "c0a70a6d1b4c5b0c44f737beef3094a7a5ae51bd63aa1f4f941a9538497b0cb8",
    "worker/respawn-refused": "8ad56cd77f9e4923e842616d66cbc95ecbd090f5577ac4a88f80d38c7f016d50",
    "worker/respawn-failed": "4ef044faaf5213f97dfad9aa6f5c5046f3c1ae2355e2520ab0218de76ff08fa7",
    "worker/intent-unwritable": "be87db25bc329708aa3c53d42b616c8ec69c4a098a7f5be2a958cbfd7e795d24",
    "worker/terminal": "0a007a9b8d9637a25e957c8fbb5e19fa83b0014f29e9fb7b6e49ea8a6a3a0e77",
    "worker/terminal-refused": "8ad56cd77f9e4923e842616d66cbc95ecbd090f5577ac4a88f80d38c7f016d50",
    "review/paused": "7615fad559c15f0445749a573990474751ea82bf7b4d49fdf3a50262e70460f9",
    "review/identity": "7e1d8871b6494e119cc64846a4c0677d5c892d4844389f15749787ef0ccda19f",
    "review/provider-forward": "69a5fd114aec59e4cf5265cbbf981ee1242cb05f56b72201317bcd3f9e556092",
    "review/healthy": "acf5f39d42a040f688fc01409fc1874b84a19cff5c544f03a6e5f486b869bc5a",
    "review/healthy-degraded": "eb912d0b8019c21efb6ae2da09a8cf5b4141e8b64b32a2c1d43c05e382663fd9",
    "review/healthy-escalation": "403b6fb6a9e0f0934b1478f728b959feadf98ab32542f9a77beb43252d2e1262",
    "review/suspected": "092c70ec99679c7a40a7e30e6195d24ab5719a80d1eb53f9e00666bb3afd1c0e",
    "review/retained": "2be306cd81779801797539cddf376fd459852cc3c9bf204d3984c539eeafc9f8",
    "review/retained-degraded": "165d3d58181ef3766f0237afc5da2eaaf23ac4b86abd22d7390cca59f5899653",
    "review/no-observation": "46c2f06412912b66f77f0416354dcb88e0d369f9c83d9f58bc257b87585736ba",
    "review/no-observation-degraded": "098a9c3d7757f9d3bd5da64777d3cf8877b25587db56c9f4452949c818517f77",
    "review/unobservable": "f3627107b42def5a9338dbae8cc33acd7b0de68e4de87086f6a371afd8bf09d3",
    "review/recovery-missing": "c4ae7605d971f8c557ad5e56d1a50c538814c353b49e11415bdbb1d7fbc9c4d0",
    "review/observe": "3d754ec0d2d281c3ba6ab02ef132da67be6ce0229120c57e3259ea36402f02e9",
    "review/observe-escalated": "5e0c1b29c03ab5c54017836e451add9c79eb7710a7e2c5b7a28562036766c5e9",
    "review/sigcont": "24fb840206a8dfe8b68478eda2abf9f15f04b82a3d8775bf5ae46454213283e9",
    "review/sigcont-foreign": "78d51db19c57e8c059da0c878fc03e884a4f3ea9868e44ee183050ccd7d9c4c7",
    "review/suspension-escalation": "140db74a2f9b24c495f8acce4c77b7af5402a41a1cd2f06fac0e0d8f7021759b",
    "review/guard-missing": "cdb23e52eaff6636622022256797d5d3d0b30b9b941e763974f3b7457c38c666",
    "review/guard-foreign": "fda962409f625423a9dd6913f3d640ae25908b748d3fbc194056541616c7529a",
    "review/guard-allowed": "cf47fc657a949625e380b4482d53dc559bf7a5cd19270ab190dbc92167ff2367",
    "review/respawn": "7dc93d1023fbedd0961370a881aa9fb7c9478c2a550fdcfdfbdb6dd7ad0dec67",
    "review/respawn-degraded": "725cb5ced95715039042a0e9db1e4bb6e765a83bd3f63f2ad4674046d5a90763",
    "review/respawn-refused": "5183fcad69f6b68ac67f49d932ababfb454a99cf76bb1af2f3d0fe70516dae93",
    "review/respawn-failed": "b9fb372f2094b796023870efca6be42c20f8e433376019abfab5c09a1029cfd2",
    "review/intent-unwritable": "7dc93d1023fbedd0961370a881aa9fb7c9478c2a550fdcfdfbdb6dd7ad0dec67",
    "review/terminal": "a9333f6982a3adbf985a53e97a468294bbb36de3a5809947b3dc17185ca44c3b",
    "review/terminal-refused": "5183fcad69f6b68ac67f49d932ababfb454a99cf76bb1af2f3d0fe70516dae93",
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
