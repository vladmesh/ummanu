"""One checkout-availability value shared by recovery, host planning and dispatch."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def binding_disabled(binding: dict[str, Any]) -> bool:
    """A retired or not-yet-onboarded binding (`enabled: false`): nothing runs against it.

    Recovery does not clone its checkout, and the host expectations neither require its checkout
    nor call one that is there unmanaged. One predicate, so the two cannot disagree (ummanu-53 P13).
    """
    return isinstance(binding, dict) and binding.get("enabled") is False


@dataclass(frozen=True)
class ProjectAvailability:
    """Projects whose configured checkout cannot currently support activation."""

    unavailable: frozenset[str] = frozenset()

    def allows(self, project_id: str) -> bool:
        return project_id not in self.unavailable

    @classmethod
    def inspect(cls, bindings: Iterable[dict[str, Any]]) -> ProjectAvailability:
        unavailable: set[str] = set()
        for binding in bindings:
            project_id = binding.get("id") if isinstance(binding, dict) else None
            if not isinstance(project_id, str) or not project_id:
                continue
            raw_repo = binding.get("repo")
            if not isinstance(raw_repo, str) or not raw_repo:
                unavailable.add(project_id)
                continue
            try:
                repo = Path(raw_repo).expanduser()
                ready = repo.is_dir() and (repo / ".git").exists()
            except (OSError, RuntimeError):
                ready = False
            if not ready:
                unavailable.add(project_id)
        return cls(frozenset(unavailable))
