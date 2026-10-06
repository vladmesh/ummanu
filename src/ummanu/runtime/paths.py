"""Home-relative fallbacks for an installation and its product checkout when nothing names them.

Overrides (``--instance``/``UMMANU_INSTANCE``, ``--product-root``/``UMMANU_REPO``) are read by
callers first; this module holds the single spelling of each fallback. A missing default live root
(``~/ummanu-data/instance``) is refused (:func:`resolve_instance_path`), never created: only
``install``, ``recover`` and ``bootstrap``, given an explicit target, create one.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

PRODUCT_ENV = "UMMANU_REPO"
INSTANCE_ENV = "UMMANU_INSTANCE"
DATA_DIRNAME = "ummanu-data"
INSTANCE_DIRNAME = "instance"
PRODUCT_DIRNAME = "ummanu"
INSTANCE_CONFIG_NAME = "instance.yaml"


class MissingDefaultInstance(RuntimeError):
    """Nothing named a live root and the default one does not exist."""

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(
            f"no live root at the default {path}: pass --instance or set {INSTANCE_ENV} to name the installation"
        )


def default_instance_path() -> Path:
    """The live root of a host that never configured one: a plain directory, not a Git work tree."""
    return Path.home() / DATA_DIRNAME / INSTANCE_DIRNAME


def resolve_instance_path(
    explicit: str | Path | None = None, environ: Mapping[str, str] | None = None
) -> Path:
    """The live root: ``--instance``, else ``UMMANU_INSTANCE``, else the default if it exists.

    A missing default raises :class:`MissingDefaultInstance` and creates nothing, so a command that
    lost ``UMMANU_INSTANCE`` (e.g. via ``sudo``) never runs against an empty path.
    """
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ if environ is None else environ
    configured = env.get(INSTANCE_ENV)
    if configured:
        return Path(configured).expanduser()
    default = default_instance_path()
    if not default.is_dir():
        raise MissingDefaultInstance(default)
    return default


#: Set on a parsed namespace whose ``--instance`` falls back to the environment and the default.
INSTANCE_FALLBACK_FLAG = "instance_fallback"


def add_instance_argument(parser: Any, *, help: str | None = None, type: Any = str) -> None:
    """``--instance`` for a command that may fall back: resolved by :func:`resolve_instance_argument`.

    The default stays ``None`` so the resolver decides, and refuses, after parsing.
    """
    parser.add_argument(
        "--instance",
        default=None,
        type=type,
        help=(help or "live root: an instance dir or instance.yaml")
        + f" (default: {INSTANCE_ENV}, else {default_instance_path()})",
    )
    parser.set_defaults(**{INSTANCE_FALLBACK_FLAG: True, "instance_value_type": type})


def resolve_instance_argument(args: Any, environ: Mapping[str, str] | None = None) -> None:
    """Fill a parsed ``--instance`` that fell back; raises :class:`MissingDefaultInstance`."""
    if getattr(args, "command", None) == "bootstrap" and not getattr(args, "empty", False):
        return
    if getattr(args, INSTANCE_FALLBACK_FLAG, False) and not getattr(args, "instance", None):
        args.instance = getattr(args, "instance_value_type", str)(resolve_instance_path(None, environ))


def default_product_root() -> Path:
    """The product checkout of a host that never configured one."""
    return Path.home() / PRODUCT_DIRNAME


def configured_product_root(environ: Mapping[str, str] | None = None) -> Path:
    """The product checkout this process was pointed at (``UMMANU_REPO``), or the home default.

    Never the checkout containing the running module: a candidate or rescue copy must not install itself.
    """
    env = os.environ if environ is None else environ
    configured = env.get(PRODUCT_ENV)
    return Path(configured).expanduser() if configured else default_product_root()


def instance_dir(path: Path | str) -> Path:
    """The instance directory for a path naming either the directory or its ``instance.yaml``."""
    resolved = Path(path).expanduser()
    return resolved.parent if resolved.name == INSTANCE_CONFIG_NAME else resolved


def component_enabled(host: dict[str, Any], component: str) -> bool:
    """Whether an installation wants a packaged component. An omitted entry means yes."""
    components = host.get("components") if isinstance(host, dict) else None
    if not isinstance(components, dict):
        return True
    entry = components.get(component)
    if not isinstance(entry, dict):
        return True
    return entry.get("enabled") is not False
