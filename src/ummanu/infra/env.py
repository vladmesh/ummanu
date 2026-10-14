"""Environment value parsing shared by runtime features."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import overload


def legacy_name(name: str) -> str:
    """The released TA spelling of a canonical UMMANU setting."""
    return "TA_" + name.removeprefix("UMMANU_")


@overload
def env_value(
    name: str, default: str, *, nonempty: bool = False, environ: Mapping[str, str] | None = None
) -> str: ...


@overload
def env_value(
    name: str, default: None = None, *, nonempty: bool = False, environ: Mapping[str, str] | None = None
) -> str | None: ...


def env_value(
    name: str,
    default: str | None = None,
    *,
    nonempty: bool = False,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Canonical, then legacy, then default; only nonempty lookups skip empty strings.

    Parsing belongs to the consumer. A malformed canonical value never borrows legacy data.
    This lookup neither mutates the environment nor grants authority to either spelling.
    """
    env = os.environ if environ is None else environ
    for key in (name, legacy_name(name)):
        value = env.get(key)
        if value is not None and (value or not nonempty):
            return value
    return default


def positive_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default
