"""Read-only projection of existing round evidence; never admits or routes a verdict."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from ummanu.dispatch.state import attempt_request_id
from ummanu.tasks import assessment_resolution, specification_revision

BLOCKER = r"BLOCKER-[A-Za-z0-9][A-Za-z0-9_-]*"


def data_block(body: str) -> list[str]:
    """Keep arbitrary Markdown and all IDs as data without a truncation boundary."""
    body = "".join(c if c in "\n\t" or 32 <= ord(c) != 127 else f"\\x{ord(c):02x}" for c in body)
    fence = "`" * max(3, 1 + max((len(m[0]) for m in re.finditer(r"`+", body)), default=0))
    return [fence + "text", body, fence, ""]


def payload(event: dict[str, Any]) -> dict[str, Any]:
    data = event.get("data") if isinstance(event.get("data"), dict) else event.get("payload")
    return data if isinstance(data, dict) else {}


@dataclass(frozen=True)
class ReviewEvidence:
    findings: str = ""
    decision: str = ""
    report: str = ""
    decision_id: str = ""
    review_id: str = ""
    report_id: str = ""
    diagnostic: str = "unknown/unresolved: missing round/spec evidence"
    historical: str = ""


def resolve_review_evidence(
    task: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    attempt: str,
    generation: int,
    decision: str,
    previous: str = "",
) -> ReviewEvidence:
    """Use the frozen decision and exact report request, not the latest card comment.

    A decision must be canonical for its Assessment visit and follow a RED from this
    attempt. Every selected marker must still have its spec binding and board comment.
    Legacy text stays visible when its authority cannot be recovered.
    """
    description = str(task.get("description") or "")
    revision = specification_revision(events, description)
    digest = hashlib.sha256(description.encode()).hexdigest()

    def bound(event: dict[str, Any], marker: str) -> bool:
        data = payload(event)
        body, occurrence = data.get("body"), data.get("marker_occurrence")
        return bool(
            revision
            and data.get("marker") == marker
            and data.get("specification_revision") == revision
            and data.get("description_sha256") == digest
            and isinstance(body, str)
            and body.strip()
            and isinstance(occurrence, int)
            and not isinstance(occurrence, bool)
            and occurrence > 0
            and sum(
                c.get("marker") == marker and c.get("body") == f"[{marker}]\n{body}"
                for c in task.get("comments") or []
                if isinstance(c, dict)
            )
            >= occurrence
        )

    report_request = attempt_request_id(attempt, "worker-report-done", task["ref"], str(generation))
    reports = [
        (i, e)
        for i, e in enumerate(events)
        if e.get("request_id") == report_request and bound(e, "report:done")
    ]
    if len(reports) > 1:
        return ReviewEvidence(historical=previous, diagnostic="unknown/unresolved: ambiguous report binding")
    report_index, report = reports[0] if reports else (len(events), {})
    selected_decision: dict[str, Any] = {}
    boundary = report_index
    if decision.strip():
        decisions = [
            (i, e)
            for i, e in enumerate(events[:boundary])
            if bound(e, "decision:rework") and payload(e)["body"].strip() == decision.strip()
        ]
        if len(decisions) != 1:
            return ReviewEvidence(
                historical=previous, diagnostic="unknown/unresolved: missing/ambiguous frozen decision"
            )
        boundary, selected_decision = decisions[0]
        visit, canonical = assessment_resolution(events[: boundary + 1])
        if canonical != selected_decision or payload(selected_decision).get("assessment_visit") != visit:
            return ReviewEvidence(
                historical=previous, diagnostic="unknown/unresolved: decision visit mismatch"
            )
    prefix = attempt_request_id(attempt, "review-red", task["ref"]) + "-"
    reviews = [
        e
        for e in events[:boundary]
        if re.fullmatch(re.escape(prefix) + r"\d+", str(e.get("request_id") or "")) and bound(e, "review:red")
    ]
    if not reviews:
        return ReviewEvidence(
            historical=previous, diagnostic="unknown/unresolved: no applicable prior review"
        )
    review = reviews[-1]
    # A foreign attempt's verdict between ours and this decision cannot be adjudicated as ours.
    later = events[events.index(review) + 1 : boundary]
    if any(
        payload(e).get("marker") in {"review:red", "review:green", "decision:rework"}
        and isinstance(payload(e).get("body"), str)
        and payload(e)["body"].strip()
        and payload(e).get("marker_occurrence")
        for e in later
    ):
        return ReviewEvidence(
            historical=previous, diagnostic="unknown/unresolved: intervening round evidence"
        )
    return ReviewEvidence(
        findings=payload(review)["body"],
        decision=payload(selected_decision).get("body", ""),
        report=payload(report).get("body", ""),
        decision_id=str(selected_decision.get("event_id") or ""),
        review_id=str(review.get("event_id") or ""),
        report_id=str(report.get("event_id") or ""),
        diagnostic="applicable round/spec evidence"
        if report
        else "unknown/unresolved: current report missing",
    )


def dispositions(evidence: ReviewEvidence) -> list[tuple[str, str]]:
    """Standard report lines describe claims, with exact observer quotations for exceptions."""
    ids = list(dict.fromkeys(re.findall(BLOCKER, evidence.findings or evidence.historical)))
    result = []
    for blocker in ids:
        claims = []
        invalid = False
        for line in evidence.report.splitlines():
            match = re.fullmatch(rf"\s*(?:- )?{re.escape(blocker)}: (.+)", line)
            if not match:
                continue
            value = match[1]
            fixed = re.fullmatch(r"fixed; commit: ([0-9a-f]{7,40})", value)
            exception = re.fullmatch(
                r"(observer-rejected|deferred); (?:issue: (issue:[a-z0-9]+); )?observer quote: (.+)", value
            )
            if fixed:
                claims.append(f"fixed (reported, verify independently); commit: {fixed[1]}")
            elif exception:
                status, issue, quote = exception.groups()
                if (
                    quote in evidence.decision
                    and blocker in re.findall(BLOCKER, quote)
                    and (status != "deferred" or issue in re.findall(r"issue:[a-z0-9]+", quote))
                ):
                    claims.append(value)
                else:
                    invalid = True
            else:
                invalid = True
        # Released prose reports remain readable. Only the explicit repair-commit assertion
        # supplies a legacy fixed claim; reviewer prose never supplies observer rejection.
        legacy = re.findall(
            rf"Repair commit `?([0-9a-f]{{7,40}})`? fixes {re.escape(blocker)}(?![A-Za-z0-9_-])",
            evidence.report,
        )
        claims.extend(f"fixed (reported, verify independently); commit: {sha}" for sha in legacy)
        distinct = list(dict.fromkeys(claims))
        reason = (
            "invalid/unresolved disposition evidence"
            if invalid
            else ("ambiguous claims" if distinct else "missing disposition evidence")
        )
        result.append(
            (blocker, distinct[0] if len(distinct) == 1 and not invalid else "unknown/unresolved: " + reason)
        )
    return result


def render_review_evidence(evidence: ReviewEvidence) -> list[str]:
    lines = [
        "Prior blocker dispositions (reported fixed status is not automatic GREEN):",
        evidence.diagnostic,
        "",
    ]
    statuses = dispositions(evidence)
    for blocker, status in statuses:
        lines += data_block(f"{blocker}: {status}")
    if not statuses:
        lines += ["unknown/unresolved: legacy verdict has no recoverable stable blocker IDs", ""]
    for label, identity, body in (
        ("Applicable prior findings, supporting data", evidence.review_id, evidence.findings),
        ("Applicable observer decision for this round", evidence.decision_id, evidence.decision),
        ("Current worker report, supporting data", evidence.report_id, evidence.report),
        ("Historical prior findings, unbound data without authority", "", evidence.historical),
    ):
        lines += [f"### {label}", f"source_event: {identity or 'unknown/unresolved'}", ""]
        lines += data_block(body) if body else ["unknown/unresolved: evidence unavailable", ""]
    return lines
