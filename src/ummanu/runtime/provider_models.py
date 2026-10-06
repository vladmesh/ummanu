"""The model and reasoning effort a provider session actually ran, read from its own journal.

* Claude: per assistant message, ``message.model`` (full id) and the record's ``effort``; CLI-
  synthesized messages carry model ``<synthetic>`` and name no model.
* Codex: per ``turn_context`` record, payload ``model`` and ``effort`` (``reasoning_effort`` in
  older rollouts).

Sessions can switch models, so every distinct model is kept in order of last use; the last is the
model the session ended on. Unrecognised records name nothing and never raise.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "ProviderModels",
    "claude_session_models",
    "codex_rollout_path",
    "codex_session_models",
]

# Claude Code's model id on a message it wrote itself rather than received from a model.
CLAUDE_SYNTHETIC_MODEL = "<synthetic>"


@dataclass(frozen=True)
class ProviderModels:
    """Every model a session ran, oldest last use first, and the effort it last ran with."""

    models: tuple[str, ...] = ()
    effort: str = ""

    @property
    def model(self) -> str:
        """The model the session ended on, or an empty string when the journal named none."""
        return self.models[-1] if self.models else ""


def claude_session_models(records: Iterable[Any]) -> ProviderModels:
    """The resolved models and effort of a Claude session journal."""
    used: dict[str, None] = {}
    effort = ""
    for record in records:
        if not isinstance(record, Mapping) or record.get("type") != "assistant":
            continue
        message = record.get("message")
        model = _text(message.get("model")) if isinstance(message, Mapping) else ""
        if model and model != CLAUDE_SYNTHETIC_MODEL:
            used.pop(model, None)
            used[model] = None
        effort = _text(record.get("effort")) or effort
    return ProviderModels(tuple(used), effort)


def codex_session_models(records: Iterable[Any]) -> ProviderModels:
    """The resolved models and effort of a Codex rollout."""
    used: dict[str, None] = {}
    effort = ""
    for record in records:
        if not isinstance(record, Mapping) or record.get("type") != "turn_context":
            continue
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            continue
        model = _text(payload.get("model"))
        if model:
            used.pop(model, None)
            used[model] = None
        effort = _text(payload.get("effort")) or _text(payload.get("reasoning_effort")) or effort
    return ProviderModels(tuple(used), effort)


def codex_rollout_path(codex_home: Path | str, thread_id: str) -> Path | None:
    """The rollout Codex keeps for one thread, ``sessions/YYYY/MM/DD/rollout-<time>-<thread>.jsonl``.

    ``None`` when no file or more than one matches.
    """
    if not thread_id or "/" in thread_id or "*" in thread_id:
        return None
    found = sorted((Path(codex_home) / "sessions").glob(f"*/*/*/rollout-*-{thread_id}.jsonl"))
    return found[0] if len(found) == 1 else None


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""
