"""Card launch spelling, independent of runtime identity and other role domains.

Dispatcher intents, record prefixes and pid paths use ``review``; launched heads
use ``reviewer``. Normalize caller input here, never a persisted HeadRun identity.
"""

WORKER_ROLE = "worker"
REVIEW_ROLE = "review"

_HEAD_ROLES = {WORKER_ROLE: WORKER_ROLE, REVIEW_ROLE: "reviewer"}
_CARD_KINDS = {head_role: kind for kind, head_role in _HEAD_ROLES.items()}
_CARD_KINDS.update({kind: kind for kind in _HEAD_ROLES})


def card_kind(role: str) -> str:
    """The card's field/path/intent kind; other domains cannot select worker state."""
    try:
        return _CARD_KINDS[role]
    except KeyError:
        raise ValueError(f"not a card head role: {role!r}") from None


def head_role(role: str) -> str:
    """The launch/heartbeat role, preserving standing and unknown role spellings."""
    kind = _CARD_KINDS.get(role)
    return _HEAD_ROLES[kind] if kind is not None else role
