"""The resumable position in a card's event history: a count of committed records.

The audit owner's traversal (`docs/BOARD_STORE.md` §7.3) only appends and never rewrites, so the
number of records before the next one neither loses nor replays an event. Not a timestamp (seconds
collide, clocks go back, ``occurred_at`` is writer-stamped) and not an index into a filtered list.

The cursor is opaque base64 of a versioned document with the count, the card's ref (a cursor from
another card is refused) and ``pos`` = :data:`POSITION_ORDINAL`. Not signed: tampering only yields
:class:`~ummanu.webproto.errors.InvalidCursor`. A document without that ``pos`` (e.g. a legacy
file-journal byte offset) is refused; the client reads a fresh task snapshot instead.
:meth:`~ummanu.webproto.command_reads.CommandReadLayer.command_history` pages the whole audit with
the same count and no ref.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass

from ummanu.webproto.errors import InvalidCursor

#: Bumped when the document changes shape; other versions are refused, since a misread position
#: silently skips events.
CURSOR_VERSION = 1

#: How many committed records stand before the next one, in the traversal the audit owner
#: publishes: the one position a cursor carries, and required in every cursor document.
POSITION_ORDINAL = "ordinal"


@dataclass(frozen=True, slots=True)
class Cursor:
    """A position in one card's history: how many of its committed records stand before the next."""

    ref: str
    offset: int

    def encode(self) -> str:
        document: dict[str, object] = {
            "v": CURSOR_VERSION,
            "ref": self.ref,
            "offset": self.offset,
            "pos": POSITION_ORDINAL,
        }
        encoded = json.dumps(document, sort_keys=True, separators=(",", ":"))
        return base64.urlsafe_b64encode(encoded.encode("utf-8")).decode("ascii").rstrip("=")


def decode(value: str, *, ref: str) -> Cursor:
    """Read a cursor this layer issued for ``ref``, or refuse it by name."""
    if not isinstance(value, str) or not value:
        raise InvalidCursor("a cursor is a non-empty string")
    padding = "=" * (-len(value) % 4)
    try:
        document = json.loads(base64.urlsafe_b64decode(value + padding).decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeError):
        raise InvalidCursor("this cursor was not issued by the task event reader") from None
    if not isinstance(document, dict) or document.get("v") != CURSOR_VERSION:
        raise InvalidCursor("this cursor uses an event cursor version this reader does not know")
    offset = document.get("offset")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise InvalidCursor("this cursor names no readable journal position")
    if document.get("ref") != ref:
        raise InvalidCursor(
            f"this cursor belongs to {document.get('ref')!r} and cannot be continued on {ref!r}"
        )
    if document.get("pos") != POSITION_ORDINAL:
        raise InvalidCursor(
            "this cursor names no position this reader can continue; "
            "read a fresh task snapshot for the cursor that continues here"
        )
    return Cursor(ref=ref, offset=offset)
