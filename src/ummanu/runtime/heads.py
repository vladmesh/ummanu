"""Head registry: which heads an installation has, and which one each role runs on.

Owns the `[resources.*]`, `[profiles.*]` and `[role_defaults]` data only; a profile is rendered into
a command by `ummanu.runtime.head.command`, which never imports this module. An installation reads
its generated snapshot (`ummanu.head_registry.installed_pair`); the shipped `heads.toml` is the
portable default. Both share one validator. No board, orca or subprocess.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]  # no PyYAML stubs in the typecheck extra

from .head.command import (
    HeadCommandError,
    validate_launch_shape,
)

HEADS_TOML = Path(__file__).with_name("heads.toml")
# Only an explicitly configured instance counts, so a checkout on a host with an installation
# keeps reading the product default.
INSTANCE_ENV = "UMMANU_INSTANCE"
# Points one process at another registry without moving its installation (tests, diffing).
REGISTRY_ENV = "TA_HEADS_REGISTRY"


def installed_registry_path() -> Path | None:
    """The configured installation's snapshot, or None when no installation is selected.

    Existence is not checked: a missing snapshot of a selected instance must fail the load rather
    than fall back to the mutable product checkout.
    """
    configured = os.environ.get(INSTANCE_ENV)
    if not configured:
        return None
    from ummanu.head_registry import HeadRegistryConfigError, installed_pair

    try:
        return installed_pair(Path(configured).expanduser()).snapshot
    except HeadRegistryConfigError as exc:
        raise HeadRegistryError(str(exc)) from None


def registry_path() -> Path:
    """The registry this process reads: the installation's snapshot, else the product default.

    Resolved per call, not at import.
    """
    override = os.environ.get(REGISTRY_ENV)
    if override:
        return Path(override).expanduser()
    return installed_registry_path() or HEADS_TOML


class HeadRegistryError(HeadCommandError):
    """The registry is missing/malformed, or names an unknown profile/resource/adapter/runtime/fallback."""


def resolve_head_id(profile_id: str, profiles: Mapping[str, Any]) -> str:
    """`profile_id` itself if the registry defines it, else HeadRegistryError.

    No alias table: an unknown id fails closed by name. Only launch resolution is strict; records
    carrying such an id still load and display it.
    """
    if isinstance(profiles, Mapping) and profile_id in profiles:
        return profile_id
    known = ", ".join(sorted(profiles)) if isinstance(profiles, Mapping) and profiles else "(none)"
    raise HeadRegistryError(f"unknown head {profile_id!r} (known: {known})")


def required_role_default(role_defaults: Any, role: str) -> str:
    """The head `[role_defaults]` routes `role` to, or HeadRegistryError naming the missing key.

    There is no product-side fallback head.
    """
    head = role_defaults.get(role) if isinstance(role_defaults, Mapping) else None
    if not head or not isinstance(head, str):
        raise HeadRegistryError(f"head registry has no role_defaults.{role}")
    return head


class Registry:
    def __init__(self, resources: dict, profiles: dict, role_defaults: dict | None = None):
        self.resources = resources
        self.profiles = profiles
        self.role_defaults = role_defaults or {}

    def role_default(self, role: str) -> str | None:
        """The head this registry routes `role` to, or None when it routes it nowhere."""
        head = self.role_defaults.get(role)
        return str(head) if head else None

    def resolve(self, profile_id: str) -> str:
        """The profile id that actually serves `profile_id` here. See `resolve_head_id`."""
        return resolve_head_id(profile_id, self.profiles)

    def profile(self, profile_id: str) -> dict:
        """The profile dict, or HeadRegistryError listing the known ids (surfaced verbatim)."""
        prof = self.profiles.get(profile_id)
        if prof is None:
            known = ", ".join(sorted(self.profiles)) or "(none)"
            raise HeadRegistryError(f"unknown head {profile_id!r} (known: {known})")
        return prof

    def known(self) -> list[str]:
        return sorted(self.profiles)


def role_head(role: str, registry: Registry | None = None) -> str:
    """The head the selected registry routes `role` to; HeadRegistryError if none."""
    reg = registry or load_registry()
    return required_role_default(reg.role_defaults, role)


def default_head(registry: Registry | None = None) -> str:
    """The head a card that names none of its own runs on."""
    return role_head("new_card", registry)


def reviewer_head(registry: Registry | None = None) -> str:
    """The head a card that names no reviewer is reviewed by; `TA_REVIEWER_HEAD` overrides."""
    override = os.environ.get("TA_REVIEWER_HEAD")
    if override:
        return override
    return role_head("reviewer", registry)


def profile_info(profile_id: str, registry: Registry | None = None) -> dict:
    """Display-facing profile facts. Unknown profiles return a marked record instead of raising."""
    reg = registry or load_registry()
    try:
        prof = reg.profile(profile_id)
    except HeadRegistryError:
        return {
            "profile": profile_id,
            "known": False,
            "adapter": "unknown",
            "model": "unknown",
            "effort": "unknown",
        }
    adapter = prof.get("adapter") or "unknown"
    effort = prof.get("effort", "default") if adapter in {"claude", "codex"} else "n/a"
    return {
        "profile": profile_id,
        "known": True,
        "adapter": adapter,
        "model": prof.get("model") or "default",
        "effort": effort,
    }


def _named(value: object, what: str) -> str:
    """`value` as a name, or HeadRegistryError.

    Checked before membership tests: an unhashable value would raise TypeError there.
    """
    if not isinstance(value, str):
        raise HeadRegistryError(f"{what} must be a name, got {type(value).__name__}")
    return value


def validate_registry(resources: dict, profiles: dict) -> None:
    """Structural check shared by the product default and the installation snapshot.

    Every malformed shape must surface as HeadRegistryError, not an AttributeError in a consumer.
    """
    if not isinstance(resources, dict):
        raise HeadRegistryError(f"[resources] must be a table, got {type(resources).__name__}")
    if not isinstance(profiles, dict):
        raise HeadRegistryError(f"[profiles] must be a table, got {type(profiles).__name__}")
    for rid, res in resources.items():
        if not isinstance(res, dict):
            raise HeadRegistryError(f"resource {rid!r} must be a table, got {type(res).__name__}")
    for pid, prof in profiles.items():
        if not isinstance(prof, dict):
            raise HeadRegistryError(f"profile {pid!r} must be a table, got {type(prof).__name__}")
        resource = _named(prof.get("resource"), f"profile {pid!r} resource")
        if resource not in resources:
            raise HeadRegistryError(f"profile {pid!r} references unknown resource {resource!r}")
        # Adapter, effort, Codex launch mode and backend runtime are validated by the renderer only,
        # so the registry cannot accept a shape that bring-up rejects (e.g. the retired Codex `exec`).
        try:
            validate_launch_shape(pid, prof)
        except HeadCommandError as exc:
            raise HeadRegistryError(str(exc)) from None
        fallback = prof.get("fallback") or []
        if not isinstance(fallback, list):
            raise HeadRegistryError(f"profile {pid!r} fallback must be a list, got {type(fallback).__name__}")
        for fb in fallback:
            fb = _named(fb, f"profile {pid!r} fallback entry")
            if fb not in profiles:
                raise HeadRegistryError(f"profile {pid!r} fallback references unknown profile {fb!r}")


def validate_role_defaults(role_defaults: dict, profiles: dict) -> None:
    """A role routed to a head nobody defined is a routing hole, not a stale line to ignore."""
    if not isinstance(role_defaults, dict):
        raise HeadRegistryError(f"[role_defaults] must be a table, got {type(role_defaults).__name__}")
    for role, head in role_defaults.items():
        head = _named(head, f"role {role!r} head")
        if head not in profiles:
            raise HeadRegistryError(
                f"role {role!r} routes to unknown head {head!r} "
                f"(known: {', '.join(sorted(profiles)) or '(none)'})"
            )


#: The two subscription families a role runs on; each must be able to fall over to the other.
SUBSCRIPTION_FAMILIES = ("claude", "codex")


def cross_family_gaps(profiles: Mapping[str, Any]) -> list[str]:
    """One line per profile that cannot fall over to the other subscription family (ummanu-108).

    Any `claude` or `codex` profile can be reached (a role default, a card's or sprint's explicit
    head, a chain), so each needs a chain that reaches a profile of the other family on another
    resource, and the first such profile has to be at the same effort: a fallback is the closest
    tier, not whatever is left. Profiles of other adapters (the hermes last resort) are not a
    subscription family and are not required to have one. Assumes `validate_registry` passed.
    """
    gaps: list[str] = []
    for pid, prof in profiles.items():
        family = str(prof.get("adapter") or "")
        if family not in SUBSCRIPTION_FAMILIES:
            continue
        resource = str(prof.get("resource") or "")
        seen = {pid}
        queue = list(prof.get("fallback") or [])
        found = ""
        while queue and not found:
            candidate = str(queue.pop(0))
            if candidate in seen or candidate not in profiles:
                continue
            seen.add(candidate)
            other = profiles[candidate]
            other_family = str(other.get("adapter") or "")
            if (
                other_family in SUBSCRIPTION_FAMILIES
                and other_family != family
                and str(other.get("resource") or "") != resource
            ):
                found = candidate
                break
            queue.extend(other.get("fallback") or [])
        if not found:
            gaps.append(
                f"profile {pid!r} ({family} on {resource}) has no cross-family fallback: its chain reaches "
                f"no {' or '.join(f for f in SUBSCRIPTION_FAMILIES if f != family)} profile on another resource"
            )
            continue
        effort, other_effort = prof.get("effort"), profiles[found].get("effort")
        if effort and other_effort and str(effort) != str(other_effort):
            gaps.append(
                f"profile {pid!r} falls over to {found!r} at effort {other_effort}, not {effort}: the "
                "cross-family fallback must be the closest tier"
            )
    return gaps


def _parse_registry(path: Path) -> dict:
    """The registry document, whichever of its two shapes is on disk."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        # A snapshot is generated, never hand-written: name what generates it.
        hint = "; run `ummanu upgrade` to generate it" if path.suffix in {".yaml", ".yml"} else ""
        raise HeadRegistryError(f"head registry missing: {path}{hint}") from e
    except (OSError, UnicodeError) as e:
        raise HeadRegistryError(f"cannot read head registry {path}: {e}") from e
    if path.suffix in {".yaml", ".yml"}:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as e:
            raise HeadRegistryError(f"head registry {path} is not valid YAML: {e}") from e
    else:
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            raise HeadRegistryError(f"head registry {path} is not valid TOML: {e}") from e
    if not isinstance(data, dict):
        raise HeadRegistryError(f"head registry {path} has an unsupported shape")
    return data


def load_registry(path: Path | None = None) -> Registry:
    """The resolved registry, parsed and validated. See `_load_registry`."""
    return _load_registry(path if path is not None else registry_path())


@cache
def _load_registry(path: Path) -> Registry:
    """The registry file, parsed and validated, cached per (process, path).

    Each dispatcher tick is a fresh process, so the cache only dedupes reads within a tick.
    HeadRegistryError is not cached, so a fixed registry recovers without a restart.
    """
    data = _parse_registry(path)
    resources = data.get("resources") or {}
    profiles = data.get("profiles") or {}
    role_defaults = data.get("role_defaults") or {}
    validate_registry(resources, profiles)
    validate_role_defaults(role_defaults, profiles)
    return Registry(resources=resources, profiles=profiles, role_defaults=role_defaults)
