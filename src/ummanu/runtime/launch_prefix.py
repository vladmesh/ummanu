"""Shell prefixes that make the provisioned ummanu source importable to a head."""

from __future__ import annotations

import os
import shlex
from pathlib import Path

from .paths import PRODUCT_DIRNAME, configured_product_root

UMMANU_REPO_ENV = "UMMANU_REPO"
# Shell-expression fallback: the prefix is rendered into card text and run later in the head's own
# shell, so `$HOME` must be the head's, not this process's.
UMMANU_SOURCE_SHELL = (
    f'"${{{UMMANU_REPO_ENV}:-$HOME/{PRODUCT_DIRNAME}}}/src${{PYTHONPATH:+:$PYTHONPATH}}"'
)


def ummanu_repo(environ: dict[str, str] | None = None) -> Path:
    """The checkout this process imports the product from, resolved per call (never at import)."""
    return configured_product_root(os.environ if environ is None else environ)


def pythonpath_prefix(environ: dict[str, str] | None = None) -> str:
    """The PYTHONPATH assignment that makes the provisioned ummanu source importable.

    By default a shell expression, resolved later by the head's shell. A caller building the launch
    command passes its environment and gets the checkout written out, because whether a same-command
    ``UMMANU_REPO=<root>`` assignment is visible to later words is unspecified in the shell.
    """
    if environ is None:
        return f"PYTHONPATH={UMMANU_SOURCE_SHELL}"
    return f'PYTHONPATH={shlex.quote(str(ummanu_repo(environ) / "src"))}"${{PYTHONPATH:+:$PYTHONPATH}}"'
