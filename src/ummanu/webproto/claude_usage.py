"""Dependency-free values and validation for Claude statusLine usage documents."""

from __future__ import annotations

import math
from typing import Any

CACHE_NAME = "ummanu-usage.json"
MAX_CACHE_BYTES = 4096
MAX_PAYLOAD_BYTES = 1024 * 1024


def finite_number(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def usage_limits(raw: Any) -> dict[str, dict[str, float]]:
    """Keep only the two documented windows; reject invalid percentages and unsafe reset dates."""
    if not isinstance(raw, dict):
        return {}
    result = {}
    for name in ("five_hour", "seven_day"):
        window = raw.get(name)
        if not isinstance(window, dict):
            continue
        used, reset = window.get("used_percentage"), window.get("resets_at")
        if not finite_number(used) or not 0 <= used <= 100:
            continue
        # Unix seconds; bounded so datetime conversion remains portable and cannot overflow.
        if not finite_number(reset) or not 0 < reset < 253402300800:
            continue
        result[name] = {"used_percentage": used, "resets_at": reset}
    return result
