"""How many cards with a head run at once: per installation and, for code cards, per project."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

DEFAULT_HEAD_CAPACITY = 3
DEFAULT_CODE_CONCURRENCY = 1


def head_capacity(config: Mapping[str, Any]) -> int:
    """The installation's `head_capacity`: active code, research and infra cards across all projects."""
    raw = config.get("head_capacity", DEFAULT_HEAD_CAPACITY)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise ValueError(f"head_capacity must be a positive integer, got {raw!r}")
    return raw


def code_concurrency(binding: Mapping[str, Any]) -> int:
    """A project's `policy.code_concurrency`: its active code cards at once; 0 claims none."""
    policy = binding.get("policy") or {}
    raw = policy.get("code_concurrency", DEFAULT_CODE_CONCURRENCY)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise ValueError(f"policy.code_concurrency must be a non-negative integer, got {raw!r}")
    return raw


def code_slot_wait(project: str, cap: int, active: list[str]) -> str:
    """Why a code card of `project` waits: its code slots are held by `active`."""
    holders = ", ".join(active) if active else "none allowed"
    return f"waiting for a code slot of project {project} (code_concurrency {cap}): {holders}"


def head_slot_wait(cap: int, active: list[str]) -> str:
    """Why a card with a head waits: every installation slot is held by `active`."""
    return f"waiting for a slot (head_capacity {cap}): {', '.join(active)}"
