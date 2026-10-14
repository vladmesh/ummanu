"""Environment value parsing shared by runtime features."""

from __future__ import annotations

import os


def positive_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default
