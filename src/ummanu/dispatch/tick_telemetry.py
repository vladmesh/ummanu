"""Bounded, read-only interpretation of the production tick's durable measurements."""

from __future__ import annotations

import math
from typing import Any

TICK_TELEMETRY_RECENT_KEPT = 100
TICK_P95_MIN_SAMPLES = 20
TICK_P95_THRESHOLD_MS = 300_000
# Reject corrupt measurements beyond a year, including integers too large to convert to float.
MAX_DURATION_MS = 366 * 24 * 60 * 60 * 1000


def duration_ms(value: Any) -> float | None:
    """Accept only finite, nonnegative JSON numbers in the measurement domain."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not 0 <= value <= MAX_DURATION_MS:
        return None
    return float(value)


def phase_ms(value: Any) -> dict[str, float] | None:
    if not isinstance(value, dict):
        return None
    return {
        name: measured
        for name, raw in value.items()
        if isinstance(name, str) and (measured := duration_ms(raw)) is not None
    }


def tick_statistics(production: dict[str, Any]) -> dict[str, Any]:
    """Nearest-rank p50/p95 over valid durations in the last 100 stored entries."""
    telemetry = production.get("tick_telemetry")
    recent = telemetry.get("recent") if isinstance(telemetry, dict) else None
    durations = sorted(
        measured
        for entry in (recent[-TICK_TELEMETRY_RECENT_KEPT:] if isinstance(recent, list) else [])
        if isinstance(entry, dict) and (measured := duration_ms(entry.get("duration_ms"))) is not None
    )
    count = len(durations)
    return {
        "sample_count": count,
        "p50_duration_ms": durations[math.ceil(count * 0.50) - 1] if count else None,
        "p95_duration_ms": durations[math.ceil(count * 0.95) - 1] if count else None,
    }


def tick_p95_finding(production: dict[str, Any]) -> dict[str, Any] | None:
    statistics = tick_statistics(production)
    count = statistics["sample_count"]
    p95 = statistics["p95_duration_ms"]
    if count < TICK_P95_MIN_SAMPLES or p95 <= TICK_P95_THRESHOLD_MS:
        return None
    return {
        "code": "dispatcher_tick_p95_slow",
        "severity": "red",
        "message": f"production tick p95 {p95:g} ms over {count} samples exceeds threshold {TICK_P95_THRESHOLD_MS} ms",
    }
