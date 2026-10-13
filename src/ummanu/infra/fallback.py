"""Cross-family diagnostics for doctor and live-root config checks."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from ummanu.config import SchemaError


def fallback_errors(instance_dir: Path, instance: Any) -> list[SchemaError]:
    """Every head and PO session must be able to fall over to the other subscription family.

    Read by `ummanu config check` and `ummanu doctor`, which fail on it naming the profile; not by
    `validate_instance`, so an installation with a gap still upgrades and runs (and is told).

    ummanu-108: a role's default profile is only the first one to try. The installation's own head
    registry (`heads/heads.toml`, when it has one; the product default otherwise, which passes) has
    to give every `claude`/`codex` profile a cross-family fallback at the same effort, and
    `po.models` has to offer a model on both CLIs, or on neither, so a PO session opened on one can
    continue on the other. A registry that cannot be parsed is reported by the reader that loads it.
    """
    from ummanu.head_registry import INSTANCE_HEADS_RELATIVE
    from ummanu.po.models import models_from_instance
    from ummanu.runtime.heads import HeadRegistryError, cross_family_gaps, validate_registry

    errors: list[SchemaError] = []
    path = instance_dir / INSTANCE_HEADS_RELATIVE
    if path.is_file():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            profiles = data.get("profiles") or {}
            validate_registry(data.get("resources") or {}, profiles)
        except (OSError, UnicodeError, ValueError, HeadRegistryError, AttributeError) as exc:
            errors.append(
                SchemaError(str(INSTANCE_HEADS_RELATIVE), "<file>", f"head registry is invalid: {exc}")
            )
        else:
            for gap in cross_family_gaps(profiles):
                errors.append(SchemaError(str(INSTANCE_HEADS_RELATIVE), "profiles.fallback", gap))
    if not isinstance(instance, dict):
        return errors
    try:
        models = models_from_instance(instance)
    except (AttributeError, TypeError):
        return errors
    offered = {cli: bool(values) for cli, values in models.items()}
    if any(offered.values()) and not all(offered.values()):
        lone = next(cli for cli, has in offered.items() if has)
        missing = ", ".join(cli for cli, has in offered.items() if not has)
        errors.append(
            SchemaError(
                "instance.yaml",
                "po.models",
                f"a {lone} PO session has no model on {missing} to fall over to when its subscription is down",
            )
        )
    return errors
