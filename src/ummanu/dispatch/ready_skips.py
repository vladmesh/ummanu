"""A Ready card the claim pass keeps skipping for one reason gets that reason on the card, once.

The tick outcome names every skip, but nobody reads a tick outcome for a card that sits in Ready.
After `READY_SKIP_NOTICE_SECONDS` of the same reason the card gets one comment with the reason
and, when a red resource names one, when it comes back. A changed reason starts a new streak.
"""

from __future__ import annotations

import time
from typing import Any

from ummanu.head_health import until_text

READY_SKIP_NOTICE_SECONDS = 900
#: The production-state key holding each skipped card's streak: `{ref: {reason, until, since, noticed}}`.
READY_SKIPS_KEY = "ready_skips"


def skip_until(outcome: dict[str, Any]) -> float:
    """The latest `until` of the red verdicts a claim-skip outcome names, 0.0 when none does."""
    verdicts = [outcome.get("readiness") or {}]
    for choice in (outcome.get("failover") or {}).values():
        verdicts.extend(choice.get("rejected") or ())
    return max((float(verdict.get("until") or 0.0) for verdict in verdicts), default=0.0)


def notice_long_ready_skips(
    runtime: Any, payload: dict[str, Any], outcome: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Track this pass's skips in `payload` and comment on each card whose streak just got long.

    A pass that ended on a claim did not read the cards after it, so their streaks are kept as
    they were; a pass that read every Ready card drops the streak of every card it did not skip.
    """
    skips = list((outcome or {}).get("skipped_ready") or ())
    complete = outcome is None or outcome.get("step") == "production-claim"
    claimed = "" if complete else str(outcome.get("pilot_ref") or "")
    previous = payload.get(READY_SKIPS_KEY) or {}
    now = time.time()
    streaks: dict[str, dict[str, Any]] = {}
    for skip in skips:
        ref, reason = str(skip["ref"]), str(skip["reason"])
        streak = previous.get(ref)
        if streak is None or streak["reason"] != reason:
            streak = {"reason": reason, "since": now, "noticed": False}
        streak["until"] = float(skip.get("until") or 0.0)
        streaks[ref] = streak
    if not complete:
        for ref, streak in previous.items():
            if ref not in streaks and ref != claimed:
                streaks[ref] = streak
    notices: list[dict[str, Any]] = []
    for ref, streak in streaks.items():
        if streak["noticed"] or now - streak["since"] < READY_SKIP_NOTICE_SECONDS:
            continue
        since = time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(streak["since"]))
        back = f" Expected back {until_text(streak['until'])}." if streak["until"] > now else ""
        runtime.writer.comment(
            role="dispatcher",
            actor=runtime.owner,
            reference=ref,
            body=f"Waiting in Ready since {since}, not claimed: {streak['reason']}.{back}",
            request_id=f"ready-skip-notice-{ref}-{int(streak['since'])}",
        )
        streak["noticed"] = True
        notices.append({"status": "ok", "step": "ready-skip-notice", "pilot_ref": ref, "reason": streak["reason"]})
    payload[READY_SKIPS_KEY] = streaks
    return notices
