"""Deterministic values for the dispatcher mapping persistence contract.

The adjacent goldens were captured from base 8abf2a9c's serializers, before the
consolidation. Capture alone is source/fixture evidence, not a baseline test run.
"""

from __future__ import annotations

from typing import Any

from ummanu.dispatch import state
from ummanu.dispatch.gate_receipt import GateReceipt, TerminalCheck
from ummanu.runtime.head import HeadRun, HeadSpec, TaskRef

# wrapper, domain type, property, parser, caught errors, slot
CONTRACTS = (
    (state.PersistedGateReceipt, GateReceipt, "receipt", "accept", (), "_receipt"),
    (state.PersistedGatePrAuthorship, state.GatePrAuthorship, "authorship", "from_json", (), "_authorship"),
    (
        state.PersistedGatePublishedRef,
        state.GatePublishedRef,
        "published_ref",
        "from_json",
        (),
        "_published_ref",
    ),
    (state.PersistedDeliveryEvidence, state.DeliveryEvidence, "evidence", "from_json", (), "_evidence"),
    (state.PersistedLaunchIntent, state.LaunchIntent, "intent", "from_json", (), None),
    (state.PersistedHeadlessRecoveryEpisode, state.HeadlessRecoveryEpisode, "episode", "from_json", (), None),
    (
        state.PersistedHeadRun,
        HeadRun,
        "run",
        "from_json",
        (KeyError, RuntimeError, TypeError, ValueError),
        "_run",
    ),
    (
        state.PersistedRoutingHeadSnapshot,
        state.RoutingHeadSnapshot,
        "snapshot",
        "from_json",
        (KeyError, TypeError, ValueError),
        "_snapshot",
    ),
)

FIELDS = {
    "gate_attestation": state.PersistedGateReceipt,
    "gate_pr_authorship": state.PersistedGatePrAuthorship,
    "gate_published_ref": state.PersistedGatePublishedRef,
    "worker_delivery_evidence": state.PersistedDeliveryEvidence,
    "review_delivery_evidence": state.PersistedDeliveryEvidence,
    "launch_intent": state.PersistedLaunchIntent,
    "worker_headless": state.PersistedHeadlessRecoveryEpisode,
    "worker_head_run": state.PersistedHeadRun,
    "review_head_run": state.PersistedHeadRun,
    "worker_run": state.PersistedRoutingHeadSnapshot,
    "review_run": state.PersistedRoutingHeadSnapshot,
}


def typed_values() -> dict[type, Any]:
    run = HeadRun(
        run_id="run-263",
        spec=HeadSpec(profile_id="worker", adapter="codex", runtime="local-pty"),
        workspace="/tmp/card-263",
        task_ref=TaskRef.card("ummanu-263"),
        role="worker",
    )
    snapshot = state.RoutingHeadSnapshot(role="worker", head="codex", launch_id="run-263")
    evidence = state.DeliveryEvidence(handle="pane-263", subject="launch", turn_confirmed=True)
    return {
        state.PersistedGateReceipt: GateReceipt(
            "a" * 40,
            "b" * 40,
            "github",
            (TerminalCheck("test", "SUCCESS"),),
            "2026-10-13T00:00:00Z",
            "c" * 64,
        ),
        state.PersistedGatePrAuthorship: state.GatePrAuthorship(263, "digest-263", "sent-263"),
        state.PersistedGatePublishedRef: state.GatePublishedRef("pipeline/ummanu-263", "a" * 40),
        state.PersistedDeliveryEvidence: evidence,
        state.PersistedLaunchIntent: state.LaunchIntent(
            role="review",
            task="ummanu-263",
            round_number=2,
            opens_round=True,
            routing_run=snapshot,
            head_run=run,
            delivery=state.LaunchDelivery(evidence=evidence),
            handle="pane-263",
            leaf="leaf-263",
            launched=True,
            aborted=True,
        ),
        state.PersistedHeadlessRecoveryEpisode: state.HeadlessRecoveryEpisode(
            since=1.0,
            dirty=False,
            recovery_error="refused",
            report_generation=2,
        ),
        state.PersistedHeadRun: run,
        state.PersistedRoutingHeadSnapshot: snapshot,
    }


def record(**changes: Any) -> state.DispatcherRecord:
    values = {
        "worker": "worker-263",
        "workspace": "/tmp/card-263",
        "handle": "pane-263",
        "head": "codex",
        "review_head": "claude",
        "attempt_id": "attempt-263",
        "comment_baseline": 0,
        "review_baseline": 0,
        "state": "claimed",
        "claimed_at": 1.0,
    }
    values.update(changes)
    return state.DispatcherRecord(**values)
