"""Seed versus integration base: the two different git meanings a card's workspace block carries.

A card names at most two refs and they answer different questions:

  seed          — where the successor's checkout *starts*.  A reslice successor inherits the
                  predecessor's unreleased content, so its worktree is cut from that candidate:
                  a card branch, or the exact object id of the candidate that was assessed.
  integration   — where the increment *lands*.  It is the pull request's base, the range the
  base          candidate history is read over, the `base_sha` of the exact-SHA gate receipt and
                  the branch the merge writes to.  It is durable and shared, never a card branch.

Until issue:2c82ba8f5d1c3bf5b8cc one field, `workspace.base_branch`, carried both.  Pointing it at
a predecessor's `pipeline/*` branch opened the release pull request into that dead branch, where
the project's `pull_request` workflow does not trigger at all: GitHub created zero check-runs, the
empty rollup read as `pending`, and the card burned the gate's six-hour pending ceiling before
anybody learned anything.  Keeping the two apart is what this module is for; the rules live here
rather than in the dispatcher because the board refuses a bad card before a workspace exists.
"""

from __future__ import annotations

import re

# The namespace the dispatcher publishes one card's candidate under (`dispatch.helpers.
# _legacy_worker_branch`).  A branch in it belongs to a single card and dies with it, so it is a
# legitimate seed and never an integration target.
CARD_BRANCH_PREFIX = "pipeline/"

# Deliberately narrow: a ref name git accepts, or an object id.  It travels into `git fetch` and
# `orca worktree create --base-branch`, so a value that could be read as an option or a path
# traversal is refused here rather than quoted at every call site.
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_EXACT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# A remote serves an object only by its full id, so an abbreviated one cannot be fetched.
_ABBREVIATED_SHA_RE = re.compile(r"^[0-9a-f]{7,39}$")


class IntegrationBaseError(ValueError):
    """A card names an integration base the project cannot integrate into."""


def is_card_branch(ref: str) -> bool:
    """Is `ref` in the per-card branch namespace the dispatcher owns?"""
    return str(ref or "").startswith(CARD_BRANCH_PREFIX)


def is_exact_sha(ref: str) -> bool:
    return bool(_EXACT_SHA_RE.match(str(ref or "")))


def ref_shape_refusal(value: str, *, field: str) -> str:
    """Why `value` is not a usable git ref for `field`, or "" when it is."""
    if not _REF_RE.match(str(value or "")):
        return (
            f"{field} must be a git ref or object id matching [A-Za-z0-9][A-Za-z0-9._/-]{{0,199}}; "
            f"{value!r} is not"
        )
    if str(value).endswith("/") or "//" in str(value) or ".." in str(value):
        return f"{field} {value!r} is not a well-formed git ref"
    return ""


def integration_base_refusal(value: str) -> str:
    """Why `value` may not be a card's integration base, or "" when its shape is acceptable.

    Shape only: whether a well-formed branch name is one *this project* integrates into is a
    question about the project's binding, and `resolve_integration_base` answers that one.
    """
    shape = ref_shape_refusal(value, field="base branch")
    if shape:
        return shape
    if is_card_branch(value):
        return (
            f"base branch {value!r} is a card branch: it is another card's candidate, it dies with "
            "that card, and the project's pull_request CI does not trigger for it. A successor that "
            "needs the predecessor's content inherits it as a seed (--seed-ref) and integrates into "
            "the project's default branch"
        )
    if is_exact_sha(value):
        return (
            f"base branch {value!r} is an object id, not a branch: an increment is merged into a "
            "branch. An exact candidate belongs in --seed-ref"
        )
    return ""


def seed_ref_refusal(value: str) -> str:
    """Why `value` may not be a card's seed, or "" when it may."""
    if _ABBREVIATED_SHA_RE.match(str(value or "")):
        return (
            f"seed ref {value!r} looks like an abbreviated object id; the dispatcher fetches the seed "
            "from the project remote, which serves an object only by its full 40-character id"
        )
    return ref_shape_refusal(value, field="seed ref")


def resolve_integration_base(*, default_branch: str, declared: list[str] | None, override: str | None) -> str:
    """The branch this card integrates into, refusing an override the project cannot integrate into.

    `declared` is the binding's `integration_bases`: the long-lived branches, besides the default
    one, that this project actually integrates into and whose CI its workflows trigger for. An
    override outside that set is refused rather than accepted and discovered six hours later as an
    empty check rollup — including the `pipeline/*` case, which is refused by name because that is
    the mistake the field evidence records.
    """
    default = str(default_branch or "").strip() or "main"
    chosen = str(override or "").strip()
    if not chosen:
        return default
    refusal = integration_base_refusal(chosen)
    if refusal:
        raise IntegrationBaseError(refusal)
    allowed = {default} | {str(item).strip() for item in (declared or []) if str(item).strip()}
    if chosen not in allowed:
        raise IntegrationBaseError(
            f"base branch {chosen!r} is not an integration target of this project "
            f"(declared: {', '.join(sorted(allowed))}); a card cannot invent one, because the "
            "project's required CI only triggers for the branches it declares"
        )
    return chosen
