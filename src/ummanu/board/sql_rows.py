"""The row and time helpers the SQL board modules share, with nothing heavier than the stdlib.

`sql_cards`, `sql_sprints` and `sql_product_issues` read and write the same `timestamptz` columns
and group the same `(owner, value)` rows, so the helpers live once, here, where each of them can
import them without importing another.

Time has one written spelling: RFC 3339 UTC with a `Z`, to the second (`rfc3339`).  A field whose
stored order depends on sub-second precision asks for `microseconds=True` and keeps the same `Z`
form.  Older writers left `+00:00` and fractional seconds behind, so every reader goes through
`parse_rfc3339`, which accepts all of them.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any


def now() -> datetime:
    """The current moment, aware and in UTC, for a `timestamptz` column."""
    return datetime.now(UTC)


def rfc3339(value: datetime | None, *, microseconds: bool = False) -> str:
    """`value` as RFC 3339 UTC with a `Z`: to the second, or to the microsecond when asked.

    The empty string stands for no value, as the board vocabulary spells it.
    """
    if value is None:
        return ""
    moment = value.astimezone(UTC)
    if not microseconds:
        moment = moment.replace(microsecond=0)
    return moment.isoformat().replace("+00:00", "Z")


def parse_rfc3339(value: Any) -> datetime | None:
    """A written time in any form the board ever wrote, or None for no value or not a time.

    `Z`, `z` and `+00:00`, with or without fractional seconds, all read.  A time that names no
    zone comes back naive; whether that is UTC or a refusal is the caller's rule.
    """
    text = "" if value is None else str(value).strip()
    if not text:
        return None
    if text[-1] in "zZ":
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def epoch(value: datetime | None) -> str:
    """Whole seconds since the epoch as the board's `date_*` fields spell them, or ""."""
    return "" if value is None else str(int(value.timestamp()))


def grouped(rows: Iterable[tuple[Any, ...]]) -> dict[Any, list[Any]]:
    """Rows keyed by their first column, each group in the order the statement returned it.

    A group holds the bare second column when a row has two columns, else the remaining tuple.
    """
    groups: dict[Any, list[Any]] = {}
    for owner, *rest in rows:
        groups.setdefault(owner, []).append(rest[0] if len(rest) == 1 else tuple(rest))
    return groups


__all__ = ["epoch", "grouped", "now", "parse_rfc3339", "rfc3339"]
