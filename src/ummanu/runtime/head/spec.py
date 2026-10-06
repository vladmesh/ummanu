"""`HeadSpec`: one head's launch shape, resolved once and then carried by value.

The adapter is required: a `HeadSpec` in hand proves the head is launchable. Per-profile launch
rules live in `command.validate_launch_shape`; the registry checks only cross-profile facts
(resource exists, fallbacks resolve).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..head_runtimes import DEFAULT_HEAD_RUNTIME, RECORD_RUNTIME_WHEN_ABSENT
from .command import (
    CODEX_TUI_MODE,
    PROMPT_AFTER_START_ADAPTERS,
    HeadCommandError,
    validate_launch_shape,
)
from .memory import DEFAULT_MEMORY_LIMIT_MIB

if TYPE_CHECKING:  # pragma: no cover - the registry is data this module is handed
    from ..heads import Registry

# The registry is imported lazily: it imports this package at module scope.


def _load_registry() -> Registry:
    from ..heads import load_registry

    return load_registry()


# Absent effort means the adapter's own default; an effort the adapter does not know is refused.
DEFAULT_EFFORT = "default"


class HeadSpecError(HeadCommandError):
    """A profile that is not a head: no adapter, or one this product cannot launch."""


@dataclass(frozen=True)
class HeadSpec:
    """What is needed to launch, address and stop one head.

    Frozen so spawn, nudge and stop can share one object safely.
    """

    profile_id: str
    adapter: str
    model: str | None = None
    effort: str = DEFAULT_EFFORT
    resource: str | None = None
    codex_mode: str | None = None
    fallback: tuple[str, ...] = ()
    #: The backend holding this head (orthogonal to `adapter`, which says what the head is). Taken
    #: from the profile (only `local-pty` is valid there) and carried on the durable run record, so a
    #: head is always observed and stopped through the backend it was raised under. A hand-built spec
    #: defaults to the record rule, a legacy Orca record no backend serves
    #: (`head_runtime_backends.is_legacy_record`); callers holding a live head pass `local-pty`.
    runtime: str = RECORD_RUNTIME_WHEN_ABSENT
    # Hand-built legacy/test specs have none; registry specs always do.
    memory_limit_mib: int | None = None

    @property
    def prompt_after_start(self) -> bool:
        """Whether this head's prompt is delivered into the session after it comes up."""
        return self.adapter in PROMPT_AFTER_START_ADAPTERS

    @classmethod
    def from_profile(cls, profile_id: str, profile: Any) -> HeadSpec:
        """The spec for one registry profile, or `HeadSpecError` naming that profile.

        Cross-profile fields (resource existence, fallbacks) are not checked here.
        """
        if not isinstance(profile, Mapping):
            raise HeadSpecError(f"head {profile_id!r} is not a profile table, got {type(profile).__name__}")
        try:
            validate_launch_shape(profile_id, profile)
        except HeadCommandError as exc:
            raise HeadSpecError(str(exc)) from None
        adapter = str(profile["adapter"])
        model = profile.get("model")
        resource = profile.get("resource")
        fallback = profile.get("fallback") or []
        return cls(
            profile_id=profile_id,
            adapter=adapter,
            model=str(model) if isinstance(model, str) and model else None,
            effort=str(profile.get("effort", DEFAULT_EFFORT)),
            resource=resource if isinstance(resource, str) and resource else None,
            codex_mode=(str(profile.get("codex_mode", CODEX_TUI_MODE)) if adapter == "codex" else None),
            fallback=tuple(str(fb) for fb in fallback) if isinstance(fallback, list) else (),
            runtime=str(profile.get("runtime", DEFAULT_HEAD_RUNTIME)),
            memory_limit_mib=int(profile.get("memory_limit_mib", DEFAULT_MEMORY_LIMIT_MIB)),
        )


def load_head_specs(registry: Registry | None = None) -> dict[str, HeadSpec]:
    """Every profile of the selected registry as a `HeadSpec`, keyed by profile id.

    One profile that fails to load fails the whole load by name; none is dropped.
    """
    reg = registry if registry is not None else _load_registry()
    return {pid: HeadSpec.from_profile(pid, prof) for pid, prof in reg.profiles.items()}


def head_spec(profile_id: str, registry: Registry | None = None) -> HeadSpec:
    """One head of the selected registry, resolved through its compatibility ids."""
    reg = registry if registry is not None else _load_registry()
    resolved = reg.resolve(profile_id)
    return HeadSpec.from_profile(resolved, reg.profile(resolved))
