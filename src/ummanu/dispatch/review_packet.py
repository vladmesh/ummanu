"""Read-only projection of existing round evidence; never admits or routes a verdict."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from ummanu.board.audit_contract import is_protocol_event
from ummanu.dispatch.state import DispatcherRecord, attempt_request_id
from ummanu.tasks import assessment_resolution, specification_revision

BLOCKER = r"BLOCKER-[A-Za-z0-9][A-Za-z0-9_-]*"


def data_block(body: str) -> list[str]:
    """Keep arbitrary Markdown and all IDs as data without a truncation boundary."""
    body = "".join(c if c in "\n\t" or 32 <= ord(c) != 127 else f"\\x{ord(c):02x}" for c in body)
    fence = "`" * max(3, 1 + max((len(m[0]) for m in re.finditer(r"`+", body)), default=0))
    return [fence + "text", body, fence, ""]


def payload(event: dict[str, Any]) -> dict[str, Any]:
    data = event.get("data") if is_protocol_event(event) else event.get("payload")
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
    verdict: str = ""


def resolve_review_evidence(
    task: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    attempt: str,
    generation: int,
    decision: str,
    previous: str = "",
    decision_id: str = "",
    review_id: str | None = None,
    review_round: int | None = None,
) -> ReviewEvidence:
    """Use the frozen decision and exact report request, not the latest card comment.

    Resolve the canonical Assessment instruction before independent review/report
    supporting data. Every selected marker must have its spec binding and board comment.
    Missing supporting evidence cannot revoke a valid instruction. Legacy text stays
    visible without supplying authority or invented dispositions.
    """
    description = str(task.get("description") or "")
    revision = specification_revision(events, description)
    digest = hashlib.sha256(description.encode()).hexdigest()

    def bound(event: dict[str, Any], marker: str) -> bool:
        data = payload(event)
        body, occurrence = data.get("body"), data.get("marker_occurrence")
        return bool(
            revision
            and event.get("kind")
            == (
                {
                    "decision:rework": "card.decided",
                    "review:red": "card.verdict",
                    "review:green": "card.verdict",
                    "report:done": "card.reported",
                }[marker]
                if is_protocol_event(event)
                else {
                    "decision:rework": "decided", "review:red": "verdict",
                    "review:green": "verdict", "report:done": "reported",
                }[marker]
            )
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
    # The first exact report bounds this frozen worker round even if report support
    # is duplicated. Ambiguous claims remain unavailable without losing its instruction.
    report_index = reports[0][0] if reports else len(events)
    report = reports[0][1] if len(reports) == 1 else {}
    selected_decision: dict[str, Any] = {}
    boundary = report_index
    if decision.strip():
        decisions = [
            (i, e) for i, e in enumerate(events[:boundary])
            if decision_id and e.get("event_id") == decision_id
        ]
        # An exact persisted source fixes the visit even before a report exists, or
        # when a later visit repeats the same instruction. It must still be canonical.
        decision_boundary = decisions[0][0] + 1 if len(decisions) == 1 else boundary
        visit, canonical = assessment_resolution(events[:decision_boundary])
        if (
            canonical is None
            or (decision_id and (len(decisions) != 1 or canonical.get("event_id") != decision_id))
            or not bound(canonical, "decision:rework")
            or payload(canonical)["body"].strip() != decision.strip()
            or sum(e.get("event_id") == canonical.get("event_id") for e in events[:boundary]) != 1
        ):
            return ReviewEvidence(
                historical=previous, diagnostic="unknown/unresolved: missing/ambiguous frozen decision"
            )
        if payload(canonical).get("assessment_visit") != visit:
            return ReviewEvidence(
                historical=previous, diagnostic="unknown/unresolved: decision visit mismatch"
            )
        selected_decision = canonical
        boundary = events.index(canonical)
        # Review evidence belongs before the park of this decision's visit, never
        # to a verdict injected between the park and its canonical instruction.
        parks = [
            i for i, e in enumerate(events[:boundary])
            if str(e.get("event_id") or e.get("request_id") or "") == visit
        ]
        if len(parks) != 1:
            return ReviewEvidence(
                decision=payload(canonical)["body"],
                decision_id=str(canonical.get("event_id") or ""), historical=previous,
                diagnostic="unknown/unresolved: ambiguous Assessment visit",
            )
        boundary = parks[0]
    prefixes = {
        marker: attempt_request_id(attempt, "review-" + marker.split(":")[1], task["ref"]) + "-"
        for marker in ("review:red", "review:green")
    }
    # No review from an earlier Assessment round can become this visit's predecessor.
    prior_park, _ = assessment_resolution(events[:boundary])
    start = next((
        i + 1 for i, e in enumerate(events[:boundary])
        if prior_park and str(e.get("event_id") or e.get("request_id") or "") == prior_park
    ), 0)
    suffix = re.escape(str(review_round)) if review_round is not None else r"\d+"
    reviews = [
        (i, e)
        for i, e in enumerate(events[:boundary])
        if i >= start and any(
            re.fullmatch(re.escape(prefix) + suffix, str(e.get("request_id") or ""))
            and bound(e, marker)
            for marker, prefix in prefixes.items()
        )
    ]
    diagnostic = []
    review: dict[str, Any] = {}
    if not reviews:
        diagnostic.append("no applicable prior review")
    else:
        review_index, candidate = reviews[-1]
        # A foreign/intervening verdict does not supply adjudicated findings, but
        # cannot erase the independently canonical instruction selected above.
        later = events[review_index + 1 : boundary]
        if review_id is not None and candidate.get("event_id") != review_id:
            diagnostic.append("frozen prior review mismatch")
        elif sum(
            e.get("request_id") == candidate.get("request_id")
            or e.get("event_id") == candidate.get("event_id") for e in events
        ) != 1:
            diagnostic.append("ambiguous prior review binding")
        elif len({
            str(e.get("request_id")).rsplit("-", 1)[-1]
            for _, e in reviews
        }) < len(reviews):
            diagnostic.append("conflicting prior verdicts")
        elif any(
            payload(e).get("marker") in {"review:red", "review:green", "decision:rework"}
            and isinstance(payload(e).get("body"), str)
            and payload(e)["body"].strip()
            and payload(e).get("marker_occurrence")
            for e in later
        ):
            diagnostic.append("intervening round evidence")
        else:
            review = candidate
    if len(reports) > 1:
        diagnostic.append("ambiguous report binding")
    elif not report:
        diagnostic.append("current report missing")
    return ReviewEvidence(
        findings=payload(review).get("body", ""),
        decision=payload(selected_decision).get("body", ""),
        report=payload(report).get("body", ""),
        decision_id=str(selected_decision.get("event_id") or ""),
        review_id=str(review.get("event_id") or ""),
        report_id=str(report.get("event_id") or ""),
        historical=previous if not review else "",
        verdict=payload(review).get("marker", "").removeprefix("review:"),
        diagnostic="unknown/unresolved: " + "; ".join(diagnostic)
        if diagnostic
        else "applicable round/spec evidence",
    )


def retain_rework_review(
    task: dict[str, Any], events: list[dict[str, Any]], record: DispatcherRecord, decision: str
) -> None:
    """Freeze predecessor/source identities with the existing rework transition write.

    The accepted park owns the SHA. Audit evidence supplies supporting data, never
    a worktree pin. Empty released fields recover through the canonical current visit.
    """
    evidence = resolve_review_evidence(
        task, events, attempt=record.attempt_id, generation=record.report_generation + 1,
        decision=decision, previous=record.previous_blockers,
        review_round=record.review_baseline,
    )
    outcome = record.worker_continuation.verdict_outcome
    applicable = bool(
        evidence.review_id and evidence.verdict == outcome and outcome in {"green", "red"}
    )
    # A confirmed stop may have saved the capture before the transition's own
    # write failed. Only that same bound capture can replace the now-cleared pin.
    pinned = record.review_commit or (
        record.previous_reviewed_sha
        if record.previous_review_id == evidence.review_id
        and record.report_decision_id == evidence.decision_id else ""
    )
    record.previous_reviewed_sha = pinned if applicable else ""
    record.previous_review_id = evidence.review_id if applicable else ""
    record.previous_blockers = evidence.findings or evidence.historical
    # Even an unsupported legacy decision source must not be replaced by a newer
    # identical instruction. Its id freezes the source without granting authority.
    _visit, canonical = assessment_resolution(events)
    record.report_decision_id = evidence.decision_id or (
        str(canonical.get("event_id") or "") if canonical is not None else ""
    )


def dispositions(evidence: ReviewEvidence) -> list[tuple[str, str]]:
    """Standard report lines describe claims, with exact observer quotations for exceptions."""
    supported_ids = set(re.findall(BLOCKER, evidence.findings + "\n" + evidence.decision))
    ids = list(dict.fromkeys(re.findall(
        BLOCKER, (evidence.findings or evidence.historical) + "\n" + evidence.decision
    )))
    result = []
    for blocker in ids:
        report = evidence.report if blocker in supported_ids else ""
        claims = []
        invalid = False
        for line in report.splitlines():
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
            report,
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
    reviewer_ids = set(re.findall(BLOCKER, evidence.findings))
    observer_ids = set(re.findall(BLOCKER, evidence.decision))
    for blocker, status in statuses:
        sources = []
        if blocker in reviewer_ids:
            sources.append(f"review:{evidence.verdict}; source_event: {evidence.review_id}")
        if blocker in observer_ids:
            sources.append(f"observer decision; source_event: {evidence.decision_id}")
        lines += data_block(
            f"{blocker}: {status}\nfinding_sources: "
            + ("; ".join(sources) if sources else "historical unbound data without authority")
        )
    if not statuses:
        lines += ["unknown/unresolved: legacy verdict has no recoverable stable blocker IDs", ""]
    for label, identity, body in (
        (f"Applicable prior findings, supporting data (review:{evidence.verdict or 'unknown/unresolved'})", evidence.review_id, evidence.findings),
        ("Applicable observer decision for this round", evidence.decision_id, evidence.decision),
        ("Current worker report, supporting data", evidence.report_id, evidence.report),
        ("Historical prior findings, unbound data without authority", "", evidence.historical),
    ):
        lines += [f"### {label}", f"source_event: {identity or 'unknown/unresolved'}", ""]
        lines += data_block(body) if body else ["unknown/unresolved: evidence unavailable", ""]
    return lines
