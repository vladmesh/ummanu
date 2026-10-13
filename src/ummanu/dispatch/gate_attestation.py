"""Immutable comment selection after fresh gate admission, including deployed legacy effects.

This is a caller-local effect fence, not an audit repair path. The board supplies original
bytes, the committed request supplies ownership, and GateReceipt supplies semantic evidence.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime
from typing import Any

from ummanu.dispatch.gate_receipt import GateReceipt, is_exact_sha
from ummanu.dispatch.state import DispatcherRecord, attempt_request_id
from ummanu.kernel.errors import TaskError

ATTESTATION_FAILURE_LIMIT = 3


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def delivery_context(
    record: DispatcherRecord, *, ref: str, owner: str, attempt_id: str, stage: str,
    e2e_reconciliation: dict[str, Any] | None,
) -> dict[str, Any]:
    return copy.deepcopy({
        "ref": ref, "actor": owner, "attempt_id": record.attempt_id or attempt_id,
        "stage": stage, "review_baseline": record.review_baseline,
        "comment_baseline": record.comment_baseline,
        "report_generation": record.report_generation or record.review_baseline,
        "review_commit": record.review_commit,
        "review_reconciliation": record.review_reconciliation,
        "e2e_reconciliation": e2e_reconciliation,
    })


def semantic_identity(receipt: GateReceipt, context: dict[str, Any]) -> str:
    facts = receipt.as_dict()
    del facts["completed_at"]
    return digest(json.dumps({"receipt": facts, "delivery": context}, sort_keys=True,
                             separators=(",", ":"), ensure_ascii=True, allow_nan=False))


def render_body(receipt: GateReceipt, context: dict[str, Any]) -> str:
    stage = context["stage"]
    label = "Assessment delivery" if stage == "assessment" else "release audit"
    body = f"## Mechanical gate attestation — {label}\n\n" + receipt.render()
    review = context["review_reconciliation"]
    if stage == "release" and review is not None:
        body += (
            "\n\nReview/base reconciliation: "
            f"reviewed SHA `{review['reviewed_sha']}`; HEAD `{review['head_sha']}`; "
            f"base SHA `{review['base_sha']}`; {review['reviewed_paths']} reviewed paths; "
            "reviewed paths unchanged."
        )
    e2e = context["e2e_reconciliation"]
    if e2e is not None:
        body += (
            "\n\nE2E/base reconciliation: "
            f"e2e-green SHA `{e2e['reviewed_sha']}`; HEAD `{e2e['head_sha']}`; "
            f"base SHA `{e2e['base_sha']}`; {e2e['reviewed_paths']} card paths; "
            "card paths unchanged, so the e2e result carries to this SHA."
        )
    closing = (
        "The observer consumes this fresh receipt, the worker report and the reviewer "
        "verdict before opening code or running any check."
        if stage == "assessment" else
        "Exact-SHA pre-merge gate receipt is valid; merge follows as a separate effect."
    )
    return body + f"\n\n{closing}"


def effect_request(context: dict[str, Any], identity: str) -> str:
    return attempt_request_id(context["attempt_id"], f"gate-attestation-{context['stage']}",
                              context["ref"], f"v2-{identity}")


def legacy_request(receipt: GateReceipt, context: dict[str, Any]) -> str:
    suffix = receipt.command_or_check_set_digest[:12]
    if context["stage"] == "assessment":
        suffix = f"{context['review_baseline']}-{suffix}"
    return attempt_request_id(context["attempt_id"], f"gate-attestation-{context['stage']}",
                              context["ref"], suffix)


class AttestationRefusal(Exception):
    """Only bounded operator metadata crosses the caller's failure boundary."""

    def __init__(self, request_id: str, reason: str, committed: Any = None) -> None:
        super().__init__(reason)
        self.request_id = request_id
        self.reason = reason
        event = committed if isinstance(committed, dict) else {}
        self.committed = {key: str(event.get(key) or "") for key in ("kind", "ref", "event_id")}


def read_committed(runtime: Any, request_id: str) -> dict[str, Any] | None:
    try:
        return runtime.audit.committed_event(request_id)
    except Exception:  # noqa: BLE001 - any unreadable audit refuses before an effect
        raise AttestationRefusal(request_id, "restore authoritative audit readability and retry") from None


def verify_claim(event: dict[str, Any], request_id: str, context: dict[str, Any], body: str) -> None:
    if (event.get("request_id") != request_id or not event.get("event_id")
            or event.get("outcome") != "success"
            or event.get("actor") != {"role": "dispatcher", "id": context["actor"]}):
        raise AttestationRefusal(request_id, "inspect conflicting committed ownership; audit facts must remain immutable", event)
    from ummanu.board.audit_contract import require_claim

    try:
        require_claim(event, kind="commented", reference=context["ref"],
                      identity={"marker": "dispatcher", "body_sha256": digest(body)})
    except TaskError:
        raise AttestationRefusal(request_id, "inspect conflicting committed payload; audit facts must remain immutable", event) from None


def _legacy_receipt(body: str) -> GateReceipt | None:
    """Read only the canonical released receipt grammar, then demand full round-trip bytes."""
    lines = body.split("\n\n")[1].splitlines() if "\n\n" in body else []
    if len(lines) < 7 or lines[3] != "- required terminal checks:":
        return None
    fields = {}
    for index, name in ((0, "validated_sha"), (1, "base_sha"), (2, "gate_mode"),
                        (-2, "completed_at"), (-1, "command_or_check_set_digest")):
        prefix = f"- {name}: "
        if not lines[index].startswith(prefix):
            return None
        fields[name] = lines[index][len(prefix):]
    checks = []
    for line in lines[4:-2]:
        match = re.fullmatch(r"  - (.*): (SUCCESS|NEUTRAL|SKIPPED)(?: \((.*)\))?", line)
        if match is None:
            return None
        checks.append({"name": match[1], "conclusion": match[2], "url": match[3] or ""})
    payload: dict[str, Any] = {**fields, "required_checks": checks}
    return GateReceipt.accept(payload, current_sha=fields["validated_sha"])


def _legacy_context(body: str, context: dict[str, Any]) -> dict[str, Any] | None:
    """Recover only reconciliation facts that the deployed renderer actually emitted."""
    original = copy.deepcopy(context)
    paragraphs = body.split("\n\n")[2:-1]
    original["e2e_reconciliation"] = None
    if context["stage"] == "release":
        original["review_reconciliation"] = None
    patterns = {
        "review_reconciliation": (
            r"Review/base reconciliation: reviewed SHA `([^`]+)`; HEAD `([^`]+)`; "
            r"base SHA `([^`]+)`; ([0-9]+) reviewed paths; reviewed paths unchanged\."
        ),
        "e2e_reconciliation": (
            r"E2E/base reconciliation: e2e-green SHA `([^`]+)`; HEAD `([^`]+)`; "
            r"base SHA `([^`]+)`; ([0-9]+) card paths; "
            r"card paths unchanged, so the e2e result carries to this SHA\."
        ),
    }
    seen = set()
    for paragraph in paragraphs:
        for key, pattern in patterns.items():
            match = re.fullmatch(pattern, paragraph)
            if match is None:
                continue
            if key in seen or (key == "review_reconciliation" and context["stage"] != "release"):
                return None
            if not all(is_exact_sha(match[index]) for index in (1, 2, 3)):
                return None
            seen.add(key)
            original[key] = {"reviewed_sha": match[1], "head_sha": match[2],
                             "base_sha": match[3], "reviewed_paths": int(match[4])}
            break
        else:
            return None
    return original


def recover_legacy(
    runtime: Any, receipt: GateReceipt, context: dict[str, Any],
) -> dict[str, Any] | None:
    request_id = legacy_request(receipt, context)
    event = read_committed(runtime, request_id)
    if event is None:
        # Pending old-format effects have no authoritative immutable completion to adopt.
        try:
            pending = runtime.audit.pending_event(request_id)
        except Exception:  # noqa: BLE001 - any unreadable pending audit refuses adoption
            raise AttestationRefusal(request_id, "restore authoritative pending audit readability and retry") from None
        if pending is not None:
            raise AttestationRefusal(request_id, "settle the staged legacy request through supported audit recovery", pending)
        return None
    try:
        card = runtime.reader.show(context["ref"])
        expected_digest = event.get("payload", {}).get("body_sha256")
        bodies = []
        for index, comment in enumerate(card["comments"]):
            body = comment["body"]
            # TaskReader preserves the board role prefix; sanitized readers may strip it.
            body = body.removeprefix("[dispatcher]\n")
            if comment.get("marker") == "dispatcher" and digest(body) == expected_digest:
                bodies.append((index, body))
    except Exception:  # noqa: BLE001 - any unreadable board evidence refuses adoption
        raise AttestationRefusal(request_id, "restore authoritative board comment readability and retry", event) from None
    if len(bodies) != 1:
        raise AttestationRefusal(request_id, "recover one unambiguous original board comment matching the immutable audit digest", event)
    index, body = bodies[0]
    verify_claim(event, request_id, context, body)
    stamps = re.findall(r"^- completed_at: (.+)$", body, re.MULTILINE)
    if len(stamps) != 1:
        raise AttestationRefusal(request_id, "inspect legacy receipt format and delivery context", event)
    try:
        stamp = datetime.fromisoformat(stamps[0])
        if stamp.tzinfo is None:
            raise ValueError("missing timezone")
    except ValueError:
        raise AttestationRefusal(request_id, "inspect invalid legacy observation timestamp", event) from None
    original = _legacy_receipt(body)
    original_context = _legacy_context(body, context)
    if (original is not None and legacy_request(original, context) == request_id
            and original_context is not None and render_body(original, original_context) == body):
        if (semantic_identity(original, original_context) != semantic_identity(receipt, context)
                or (context["stage"] == "release" and index < context["review_baseline"])):
            return None  # verified old effect; this admitted receipt needs its own identity
        return {"receipt": original.as_dict(), "context": context, "body": body,
                "request_id": request_id}
    raise AttestationRefusal(request_id, "legacy evidence or delivery context drifted; inspect original receipt before retry", event)


def select_effect(
    runtime: Any, record: DispatcherRecord, receipt: GateReceipt,
    context: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    identity = semantic_identity(receipt, context)
    request_id = effect_request(context, identity)
    if not isinstance(record.gate_attestation_effects, dict):
        raise AttestationRefusal(request_id, "inspect malformed frozen attestation effect collection")
    effect = record.gate_attestation_effects.get(identity)
    if effect is None:
        effect = recover_legacy(runtime, receipt, context)
        if effect is None:
            effect = {"receipt": receipt.as_dict(), "context": context,
                      "request_id": request_id, "body": render_body(receipt, context)}
    if not isinstance(effect, dict):
        raise AttestationRefusal(request_id, "inspect malformed frozen attestation effect")
    frozen = GateReceipt.accept(effect.get("receipt"), current_sha=receipt.validated_sha)
    if (frozen is None or effect.get("context") != context
            or semantic_identity(frozen, context) != identity
            or effect.get("request_id") not in {request_id, legacy_request(frozen, context)}
            or effect.get("body") != render_body(frozen, context)):
        raise AttestationRefusal(request_id, "inspect corrupt frozen attestation effect; do not rewrite audit facts")
    committed = read_committed(runtime, effect["request_id"])
    if committed is not None:
        verify_claim(committed, effect["request_id"], context, effect["body"])
    return identity, effect
