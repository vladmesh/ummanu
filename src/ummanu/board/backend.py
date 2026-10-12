"""The board client and the identity vocabulary every normalized record shares.

There is one board backend, the PostgreSQL board store (`docs/BOARD_STORE.md`), and nothing selects
it: `board_client` always builds the store's client for an installation.  What stays here is the
vocabulary every caller shares with that client -- the capabilities a call site asks for, the
integer key ranges of the transport, and the `<kind>_<store>_<n>` identities a row carries.
"""

from __future__ import annotations

import hashlib
import re
from enum import StrEnum
from pathlib import Path
from typing import Any

#: What a row's identity and `audit.backend.kind` name as the store that answered.
BOARD_STORE_KIND = "postgres"


class BoardIdentityError(RuntimeError):
    """A board identity or key is outside the vocabulary this build knows how to read."""


class BoardCapability(StrEnum):
    """A normalized board surface a caller requires from the board client."""

    CARD = "card"
    SPRINT = "sprint"
    PRODUCT_ISSUE = "product/issue"


#: Compatibility exports for existing callers.  They are `str`-compatible enum members.
CARD = BoardCapability.CARD
SPRINT = BoardCapability.SPRINT
PRODUCT_ISSUE = BoardCapability.PRODUCT_ISSUE

#: Which normalized surfaces the PostgreSQL implementation answers.
POSTGRES_SERVES: frozenset[BoardCapability] = frozenset({CARD, PRODUCT_ISSUE, SPRINT})


# Every normalized entity shares the integer-addressed board-client vocabulary.  These ranges
# are its one namespace: Cards use an immutable stored key in the positive range below two
# billion, while Sprint, Product and Issue keys are stored and indexed beside their string
# identity.  Sprint reserves two disjoint
# half-ranges so numbered references are reversible without colliding with custom references.
CARD_KEY_BASE = 1
CARD_KEY_LIMIT = 2_000_000_000
RECORD_KINDS = ("sprint", "product", "issue")
RECORD_KEY_BASES = {"sprint": 2_000_000_000, "product": 3_000_000_000, "issue": 4_000_000_000}
RECORD_KEY_SPAN = 1_000_000_000
SPRINT_NUMBER_KEY_SPAN = RECORD_KEY_SPAN // 2
_ASCII_NUMBERED_SPRINT_REF = re.compile(r"^sprint:([0-9]+)$")


def sprint_reference_number(identifier: object) -> int | None:
    """Return a canonical ASCII ``sprint:N`` number, or None for a custom Sprint ref."""
    text = str(identifier).strip()
    match = _ASCII_NUMBERED_SPRINT_REF.fullmatch(text)
    if match is None:
        return None
    number = int(match.group(1))
    if text != f"sprint:{number}":
        raise BoardIdentityError(
            f"a numbered Sprint reference must be canonical, not {text!r}"
        )
    return number


def record_key(kind: str, identifier: str) -> int:
    """Return the stable SQL transport key for one normalized non-Card identity."""
    if kind not in RECORD_KINDS:
        raise BoardIdentityError(f"a board record names one of {', '.join(RECORD_KINDS)}, not {kind!r}")
    text = str(identifier).strip()
    if not text:
        raise BoardIdentityError(f"a {kind} key needs an identifier")
    number = sprint_reference_number(text) if kind == "sprint" else None
    if number is not None:
        if number >= SPRINT_NUMBER_KEY_SPAN:
            raise BoardIdentityError(
                f"a numbered Sprint must be below {SPRINT_NUMBER_KEY_SPAN}, not {number}"
            )
        return RECORD_KEY_BASES[kind] + number
    digest = hashlib.sha256(f"{kind}:{text}".encode()).digest()
    offset = SPRINT_NUMBER_KEY_SPAN if kind == "sprint" else 0
    span = SPRINT_NUMBER_KEY_SPAN if kind == "sprint" else RECORD_KEY_SPAN
    return RECORD_KEY_BASES[kind] + offset + int.from_bytes(digest[:8], "big") % span


def record_key_kind(value: object) -> str | None:
    """Return the normalized non-Card kind for a transport key, or None for a Card key."""
    try:
        number = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None
    for kind in RECORD_KINDS:
        base = RECORD_KEY_BASES[kind]
        if base <= number < base + RECORD_KEY_SPAN:
            return kind
    return None


def card_transport_key(value: object) -> int | None:
    """Return a valid Card transport key, or ``None`` outside the Card namespace."""
    try:
        number = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None
    return number if CARD_KEY_BASE <= number < CARD_KEY_LIMIT else None


def board_client(
    instance_dir: str | Path,
    *,
    serves: tuple[BoardCapability, ...] = (CARD,),
    role: str = "app",
) -> Any:
    """The board client for one installation: the PostgreSQL board store's.

    This is the single construction path: reader, writer, audit owner and host adapter all follow
    the client that comes back.  Nothing here probes for `board-store.env`; a store with no
    configuration refuses with the store's own reason, which is the diagnosis an operator needs.

    `serves` is what the call site needs from the client it is asking for.  Every surface is served
    by the store today, so a surface outside `POSTGRES_SERVES` is a programming error refused by name.
    """
    from ummanu.tasks import TaskError

    unknown = tuple(entity for entity in serves if entity not in POSTGRES_SERVES)
    if unknown:
        raise TaskError(
            "backend_error",
            f"the board store serves {', '.join(sorted(POSTGRES_SERVES))} only, not {', '.join(unknown)}",
            1,
        )
    from ummanu.board.sql_cards import SqlCardClient
    from ummanu.board.store import BoardStoreError, resolve_role

    try:
        credentials = resolve_role(instance_dir, role)
    except BoardStoreError as exc:
        raise _store_refusal(exc) from None
    return SqlCardClient(credentials, instance_dir)


def card_client(instance_dir: str | Path, *, role: str = "app") -> Any:
    """`board_client` for a call site that needs cards only."""
    return board_client(instance_dir, serves=(CARD,), role=role)


def _store_refusal(exc: Exception) -> Exception:
    """A store refusal, in the vocabulary `run_task_command` already prints (`TaskError`).

    `BoardStoreError` is a `RuntimeError`, and a `RuntimeError` reaching a CLI handler is a
    traceback.  Every command that reads or writes cards already renders `TaskError` as a named
    refusal with an exit status, so a `board-store.env` that is missing, malformed or tracked by
    git leaves here as `backend_unavailable` carrying the store's own sentence.
    """
    from ummanu.tasks import TaskError

    return TaskError("backend_unavailable", f"board store is not usable: {exc}", 1)


ENTITY_KINDS = ("task", "sprint")

#: `<kind>_<store word>_<n>`.  The store word is any lowercase word, so identities an earlier store
#: minted before the cutover (`docs/BOARD_STORE.md` §9) still read back to their number.
_ENTITY_IDENTITY = re.compile(r"^(?P<kind>[a-z]+)_[a-z]+_(?P<number>[0-9]+)$")


def entity_id(kind: str, number: int) -> str:
    """Mint `<kind>_postgres_<n>`: the one identity a normalized row carries.

    `kind` says whether the row is a card or a sprint and `number` is the store's own number for it,
    its `board_key`.  It is minted here and nowhere else, and `entity_number` reads it back.
    """
    if kind not in ENTITY_KINDS:
        raise BoardIdentityError(f"an entity identity names one of {', '.join(ENTITY_KINDS)}, not {kind!r}")
    return f"{kind}_{BOARD_STORE_KIND}_{int(number)}"


def entity_number(kind: str, value: object) -> int | None:
    """Read the number back out of a `<kind>_<store word>_<n>` identity, or out of a bare number.

    `None` means the value is not an identity of this kind, and every caller turns that into its
    own refusal.  The store word is not checked against today's: history recorded before the
    cutover carries another one, and those identities must keep resolving to the same number.  A
    bare number is still accepted, because the file journal holds records written before the
    identity carried a store at all.
    """
    if kind not in ENTITY_KINDS:
        raise BoardIdentityError(f"an entity identity names one of {', '.join(ENTITY_KINDS)}, not {kind!r}")
    text = "" if value is None else str(value).strip()
    match = _ENTITY_IDENTITY.fullmatch(text)
    if match is not None:
        if match.group("kind") != kind:
            return None
        text = match.group("number")
    if not text.isdigit():
        return None
    number = int(text)
    return number if number > 0 else None


__all__ = [
    "BOARD_STORE_KIND",
    "CARD",
    "CARD_KEY_BASE",
    "CARD_KEY_LIMIT",
    "ENTITY_KINDS",
    "PRODUCT_ISSUE",
    "RECORD_KEY_BASES",
    "RECORD_KEY_SPAN",
    "RECORD_KINDS",
    "SPRINT",
    "BoardCapability",
    "BoardIdentityError",
    "board_client",
    "card_client",
    "card_transport_key",
    "entity_id",
    "entity_number",
    "record_key",
    "record_key_kind",
    "sprint_reference_number",
]
