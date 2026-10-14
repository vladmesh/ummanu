"""Server-rendered presentation: values."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from typing import Any

#: The bar's read, per request: a `{"available", "reason", "document"}` section, or `None` when
#: the provider usage layer was not built.
_LIMITS_SOURCE: ContextVar[Callable[[], dict[str, Any] | None] | None] = ContextVar(
    "ummanu.web.limits_source", default=None
)


#: The lamp's doctor read, per request: a section like the bar's, or `None` without that layer.
_DOCTOR_SOURCE: ContextVar[Callable[[], dict[str, Any] | None] | None] = ContextVar(
    "ummanu.web.doctor_source", default=None
)


#: The bell's read, per request: a `{"state", "reason", "count"}` document, or `None` without it.
_BELL_SOURCE: ContextVar[Callable[[], dict[str, Any] | None] | None] = ContextVar(
    "ummanu.web.bell_source", default=None
)


#: The countdown clock, per render; unset (every real request) means wall clock, tests set it.
_RENDER_CLOCK: ContextVar[Callable[[], datetime] | None] = ContextVar(
    "ummanu.web.render_clock", default=None
)


#: Whether this request answers a POST; such a page must not reload (the browser would resubmit).
_FROM_POST: ContextVar[bool] = ContextVar("ummanu.web.from_post", default=False)


@contextmanager
def limits_source(read: Callable[[], dict[str, Any] | None] | None) -> Iterator[None]:
    """Feed the bottom bar for the span of one request."""
    with _bind(_LIMITS_SOURCE, read):
        yield


@contextmanager
def doctor_source(read: Callable[[], dict[str, Any] | None] | None) -> Iterator[None]:
    """Feed the doctor lamp for the span of one request."""
    with _bind(_DOCTOR_SOURCE, read):
        yield


@contextmanager
def bell_source(read: Callable[[], dict[str, Any] | None] | None) -> Iterator[None]:
    """Feed the header bell for the span of one request."""
    with _bind(_BELL_SOURCE, read):
        yield


@contextmanager
def render_clock(now: Callable[[], datetime] | None) -> Iterator[None]:
    """Fix this render's countdown clock, for deterministic tests."""
    with _bind(_RENDER_CLOCK, now):
        yield


@contextmanager
def from_post(value: bool) -> Iterator[None]:
    """Mark one request as answering a POST, so its pages carry no auto-reload."""
    with _bind(_FROM_POST, value):
        yield


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}



@contextmanager
def _bind(variable: ContextVar, value: Any) -> Iterator[None]:
    """Bind one request value, restoring its token even when rendering fails."""
    token = variable.set(value)
    try:
        yield
    finally:
        variable.reset(token)

