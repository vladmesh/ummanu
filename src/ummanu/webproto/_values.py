"""The leaf value helpers every webproto layer reads raw records and failures with.

Each answers the same way for the same input wherever it is called, so a document's bytes do not
depend on which layer built it.
"""

from __future__ import annotations

from typing import Any


def _text(value: Any) -> str:
    """A string field as it is, anything else as empty."""
    return value if isinstance(value, str) else ""


def _float(value: Any) -> float:
    """A number field as a float, anything else (a bool included) as zero."""
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _reason(exc: Exception) -> str:
    """What a failure says: a typed refusal's own message, else its text, else its type."""
    return getattr(exc, "message", None) or str(exc) or type(exc).__name__


def _typed_reason(exc: Exception) -> str:
    """A failure named by its type, then what it says: for a source's refusal, where the type is evidence."""
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
