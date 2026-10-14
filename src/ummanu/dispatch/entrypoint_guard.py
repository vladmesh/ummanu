"""Does a target commit keep the entrypoint the live units execute? One check for every advance.

The installed units start ``src/<PACKAGE>/dispatch/runtime_preflight.py`` by pathname and then the
``<PACKAGE>`` console script of the editable install, both out of the production checkout. A commit
without either cannot be activated by moving that checkout under the running code: the next tick,
web or PO restart would find nothing to execute. Such a commit may land on the remote, but the
production checkout and its role worktrees stay where they are until a deliberate installation operation
(`docs/OPERATIONS.md` §Entrypoint activation) activates the replacement entrypoints.

`PACKAGE` is the running code's own package (`runtime_preflight.PACKAGE`), never a literal, so after
a rename the same check guards the new name and ordinary releases advance again. The target is read
from the object store only; nothing here checks out or touches a working tree.

Both movers call :func:`require_entrypoint`: `dispatch.production_checkout.advance` (the release
path) and `upgrade.fast_forward` (the operator path), each with its own Git runner.
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable

from ummanu.dispatch.runtime_preflight import PACKAGE

ENTRYPOINT_MOVED = "entrypoint_moved"
RUNBOOK = "docs/OPERATIONS.md §Entrypoint activation"

#: Runs ``git <args>`` in the checkout being advanced; stdout, or None when git failed.
GitProbe = Callable[[list[str]], "str | None"]


class EntrypointMoved(Exception):
    """The target commit does not carry the entrypoint the live units execute."""

    code = ENTRYPOINT_MOVED
    reason = ENTRYPOINT_MOVED

    def __init__(self, *, target: str, package: str, missing: str) -> None:
        self.target = target
        self.package = package
        self.missing = missing
        super().__init__(
            f"{target[:12]} does not keep the running entrypoint of package {package!r}: {missing}; "
            f"activate it only through the installation operation guidance in {RUNBOOK}"
        )

    def facts(self) -> dict[str, object]:
        return {
            "code": self.code,
            "reason": self.reason,
            "target": self.target,
            "revision": None,
            "package": self.package,
            "missing": self.missing,
            "runbook": RUNBOOK,
            "message": str(self),
        }


def preflight_path(package: str = PACKAGE) -> str:
    return f"src/{package}/dispatch/runtime_preflight.py"


def entrypoint_refusal(git: GitProbe, target: str, *, package: str = PACKAGE) -> EntrypointMoved | None:
    """The refusal for `target`, or None when it keeps both halves of the running entrypoint."""
    preflight = preflight_path(package)
    if git(["cat-file", "-e", f"{target}:{preflight}"]) is None:
        return EntrypointMoved(target=target, package=package, missing=f"{preflight} is absent")
    manifest = git(["show", f"{target}:pyproject.toml"])
    if manifest is None:
        return EntrypointMoved(target=target, package=package, missing="pyproject.toml is absent")
    try:
        scripts = tomllib.loads(manifest).get("project", {}).get("scripts", {})
    except (tomllib.TOMLDecodeError, AttributeError):
        return EntrypointMoved(target=target, package=package, missing="pyproject.toml is unreadable")
    if not isinstance(scripts, dict) or package not in scripts:
        return EntrypointMoved(
            target=target, package=package, missing=f"pyproject.toml [project.scripts] has no {package!r} script"
        )
    return None


def require_entrypoint(git: GitProbe, target: str, *, package: str = PACKAGE) -> None:
    """Raise `EntrypointMoved` unless `target` keeps the running entrypoint."""
    refusal = entrypoint_refusal(git, target, package=package)
    if refusal is not None:
        raise refusal


__all__ = [
    "ENTRYPOINT_MOVED",
    "RUNBOOK",
    "EntrypointMoved",
    "entrypoint_refusal",
    "preflight_path",
    "require_entrypoint",
]
