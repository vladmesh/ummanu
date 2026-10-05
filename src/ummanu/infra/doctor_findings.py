"""Exact, instance-local acceptance of raw doctor rows; checks still emit every finding."""

from __future__ import annotations

from typing import Any


def accepted(finding: dict[str, Any]) -> bool:
    reason = finding.get("acceptance_reason")
    return finding.get("accepted") is True and isinstance(reason, str) and bool(reason.strip())


def active_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [finding for finding in findings if not accepted(finding)]


def apply_acceptance(findings: list[dict[str, Any]], instance: dict[str, Any]) -> list[dict[str, Any]]:
    """Compare complete raw objects before adding display annotations. Key order is immaterial."""
    declarations = instance.get("doctor", {}).get("accepted_findings", [])
    result = []
    for finding in findings:
        declaration = next((entry for entry in declarations if entry["finding"] == finding), None)
        result.append({**finding, "accepted": True, "acceptance_reason": declaration["reason"]}
                      if declaration is not None else finding)
    return result
