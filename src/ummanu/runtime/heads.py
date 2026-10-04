"""Head registry — which heads an installation has, and which one each role runs on.

A worker/reviewer head is data (`[resources.*]`, `[profiles.*]`, `[role_defaults]`), not a
hardcoded `claude` invocation. This module owns that data and nothing else: a profile is looked
up here and handed to `ummanu.runtime.head.command`, which is the one place a profile
becomes a shell command. The dependency runs one way — this module imports the renderer, never
the reverse — which keeps a head operation runnable with no registry.

Which heads exist is installation configuration, not product code, so an upgraded installation
reads its own generated snapshot (located by `ummanu.head_registry.installed_pair`) and the
shipped `heads.toml` is the portable default. Both go through the same validator.

Pure and I/O-light (`load_registry` caches its read per process): no board, no orca, no
subprocess.
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
# The installation whose registry this process runs off, and where that registry sits inside it.
# Only an explicitly configured instance counts: a checkout on a host that happens to have an
# installation must keep reading the product default, or every test about the shipped registry
# would silently assert against the developer's own heads.
INSTANCE_ENV = "UMMANU_INSTANCE"
# Point one process at another registry without moving its installation. Tests use it; so does an
# operator diffing a candidate registry against the live one.
REGISTRY_ENV = "TA_HEADS_REGISTRY"


def installed_registry_path() -> Path | None:
    """The configured installation's own snapshot, or None when there is no installation here.

    Where it sits is `ummanu.head_registry`'s answer, `<data>/heads/heads.yaml`; a live root's own
    `heads/heads.yaml` is never read. Whether that snapshot exists is otherwise not
    asked: a missing, unreadable or dangling snapshot is a broken installation and the load below
    fails by that path. Answering "no installation" instead would route a selected non-default
    instance off a mutable product checkout.
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
    """The registry this process reads: the installation's own snapshot, else the product default.

    The product default is for a checkout with no installation selected at all, not for a selected
    installation whose snapshot is unusable. Resolved per call rather than at import.
    """
    override = os.environ.get(REGISTRY_ENV)
    if override:
        return Path(override).expanduser()
    return installed_registry_path() or HEADS_TOML


class HeadRegistryError(HeadCommandError):
    """heads.toml is missing/malformed, or a profile/resource/adapter/runtime/fallback it names is
    unknown.
    """


def resolve_head_id(profile_id: str, profiles: Mapping[str, Any]) -> str:
    """The profile id that serves `profile_id` in a registry's `profiles` table: the id itself.

    There is no alias table any more. A head id written down before the installation renamed its
    profiles — a card override, a dispatcher record, an agent's automation.toml — is launched under
    its own name or not at all: an unknown id fails closed by name here rather than being routed to
    whatever profile happens to look closest. Only launch resolution is strict; the records that
    carry such an id still load, and read paths display it as the string it is.
    """
    if isinstance(profiles, Mapping) and profile_id in profiles:
        return profile_id
    known = ", ".join(sorted(profiles)) if isinstance(profiles, Mapping) and profiles else "(none)"
    raise HeadRegistryError(f"unknown head {profile_id!r} (known: {known})")


def required_role_default(role_defaults: Any, role: str) -> str:
    """The head `[role_defaults]` routes `role` to, or HeadRegistryError naming the missing key.

    The product has no head id of its own to fall back on: which heads exist is the installation's
    registry, so a registry that routes a role nowhere is refused by that key rather than handed a
    product-chosen id it may not define.
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
        """The profile dict for `profile_id`, or HeadRegistryError with the known ids — the text
        a claim guard or a create/update validation surfaces verbatim to whoever reads it."""
        prof = self.profiles.get(profile_id)
        if prof is None:
            known = ", ".join(sorted(self.profiles)) or "(none)"
            raise HeadRegistryError(f"unknown head {profile_id!r} (known: {known})")
        return prof

    def known(self) -> list[str]:
        return sorted(self.profiles)


def role_head(role: str, registry: Registry | None = None) -> str:
    """The head the selected registry routes `role` to.

    An unreadable registry, or one with no `role_defaults.<role>`, raises HeadRegistryError: there
    is no product-side head id left to launch instead.
    """
    reg = registry or load_registry()
    return required_role_default(reg.role_defaults, role)


def default_head(registry: Registry | None = None) -> str:
    """The head a card that names none of its own runs on."""
    return role_head("new_card", registry)


def reviewer_head(registry: Registry | None = None) -> str:
    """The head a card that names no reviewer of its own is reviewed by.

    ``TA_REVIEWER_HEAD`` still wins: it is the one-tick override an operator sets to try a reviewer
    without editing the installation's registry.
    """
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
    """A registry field that has to be a plain name before anything can be looked up by it.

    Checked before the membership tests below rather than left to them: a list where a name belongs
    is unhashable, so `value not in table` would raise TypeError past every caller.
    """
    if not isinstance(value, str):
        raise HeadRegistryError(f"{what} must be a name, got {type(value).__name__}")
    return value


def validate_registry(resources: dict, profiles: dict) -> None:
    """Structural check every consumer of the registry shares: the product canon at load time and the
    installation snapshot the dispatcher runs off.

    Shapes are checked alongside names, because a registry is hand-written TOML: every malformed
    entry has to come back as a HeadRegistryError here rather than as an AttributeError down in a
    consumer that assumed a mapping.
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
        # Adapter, effort, Codex launch mode and the backend runtime are the renderer's rules,
        # checked by the renderer:
        # what a registry may name is exactly what something can be launched from, and a table
        # validated against a second copy of that list is a table that can pass here and fail at
        # bring-up. An absent Codex mode is the interactive one, and a registry that still pins the
        # retired `exec` is refused there rather than launched as a shape nothing produces.
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


def _parse_registry(path: Path) -> dict:
    """The registry document, whichever of its two shapes is on disk."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        # An installation's snapshot is generated, never written by hand: name what generates it.
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
    """The registry this installation runs off, resolved then parsed. See ``_load_registry``."""
    return _load_registry(path if path is not None else registry_path())


@cache
def _load_registry(path: Path) -> Registry:
    """The registry file, parsed and validated. Cached per (process, path) — every dispatcher tick
    is a fresh production-dispatcher process, so this only dedupes the 2+
    reads a single tick already does (claim's `_check_head`, then the bring-up's own lookup),
    never a long-lived process going stale against an edited file on disk. A raised
    HeadRegistryError is not cached — the next call re-reads, so a fixed-then-retried registry
    recovers without a process restart."""
    data = _parse_registry(path)
    resources = data.get("resources") or {}
    profiles = data.get("profiles") or {}
    role_defaults = data.get("role_defaults") or {}
    validate_registry(resources, profiles)
    validate_role_defaults(role_defaults, profiles)
    return Registry(resources=resources, profiles=profiles, role_defaults=role_defaults)
