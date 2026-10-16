"""The host-wide Git hooks directory the product ships and the runtime user's global config names.

Its pre-push hook refuses a remote ref deletion or a non-fast-forward update unless the push sets
`REWRITE_OPT_OUT_ENV=1`.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable
from pathlib import Path

from ummanu import _proc

REWRITE_OPT_OUT_ENV = "UMMANU_ALLOW_REMOTE_REWRITE"
HOOKS_RELATIVE = Path("src") / "ummanu" / "runtime" / "git-hooks"
PRE_PUSH = "pre-push"
HOOKS_PATH_FINDING = "git_hooks_path"


class GitHooksError(RuntimeError):
    """The global hooks path could not be read or written."""


def hooks_dir(product_root: Path | str) -> Path:
    return Path(product_root).expanduser().resolve() / HOOKS_RELATIVE


def _git_env(**overrides: str) -> dict[str, str]:
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("GIT_CONFIG") and name not in {"GIT_DIR", "GIT_WORK_TREE", "XDG_CONFIG_HOME"}
    }
    env.update({"GIT_CONFIG_NOSYSTEM": "1", **overrides})
    return env


def _global_git(home: Path, args: list[str]):
    # The owner's own ~/.gitconfig, whatever global config the calling process was pointed at.
    env = _git_env(HOME=str(home))
    try:
        return _proc.run(["git", "config", "--global", *args], env=env, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitHooksError(f"git config --global failed: {exc}") from None


def configured_hooks_path(home: Path | str) -> str:
    """The global `core.hooksPath` under `home`, or "" when it is unset."""
    completed = _global_git(Path(home), ["--get", "core.hooksPath"])
    if completed.returncode == 1:
        return ""
    if completed.returncode != 0:
        raise GitHooksError(f"cannot read the global core.hooksPath: {completed.stderr.strip()}")
    return completed.stdout.strip()


def _checkout_git(checkout: Path, args: list[str], *, unset_ok: bool = False) -> str:
    # Repository configuration only: the global path under test must not answer for the checkout.
    command = ["git", "-c", "safe.directory=*", "-c", "core.fsmonitor=false", "-C", str(checkout), *args]
    try:
        completed = _proc.run(command, env=_git_env(GIT_CONFIG_GLOBAL=os.devnull), timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitHooksError(f"cannot inspect {checkout}: {exc}") from None
    if completed.returncode != 0 and not (unset_ok and completed.returncode == 1):
        raise GitHooksError(f"cannot inspect {checkout}: {completed.stderr.strip()}")
    return completed.stdout


def _worktrees(checkout: Path) -> list[Path]:
    listing = _checkout_git(checkout, ["worktree", "list", "--porcelain"])
    return [Path(line.removeprefix("worktree ")) for line in listing.splitlines() if line.startswith("worktree ")]


def shadowed_hooks(checkouts: Iterable[Path | str]) -> list[str]:
    """Executable hooks a global `core.hooksPath` would hide in these checkouts and their linked worktrees.

    A worktree whose repository or worktree config sets `core.hooksPath` keeps it; an absent checkout has none.
    """
    shadowed: set[str] = set()
    for checkout in checkouts:
        checkout = Path(checkout).expanduser()
        if not checkout.exists():
            continue
        for worktree in _worktrees(checkout):
            if not worktree.exists():
                continue
            if _checkout_git(worktree, ["config", "--get", "core.hooksPath"], unset_ok=True).strip():
                continue
            default = Path(
                _checkout_git(worktree, ["rev-parse", "--path-format=absolute", "--git-path", "hooks"]).strip()
            )
            if not default.is_dir():
                continue
            shadowed.update(
                str(entry)
                for entry in default.iterdir()
                if entry.is_file() and entry.suffix != ".sample" and os.access(entry, os.X_OK)
            )
    return sorted(shadowed)


def install_hooks_path(
    home: Path | str, product_root: Path | str, checkouts: Iterable[Path | str], *, dry_run: bool = False
) -> bool:
    """Point the global `core.hooksPath` under `home` at the shipped hooks; True when it changed.

    Refuses before writing when the change would hide a hook any of `checkouts` runs today.
    """
    target = hooks_dir(product_root)
    hook = target / PRE_PUSH
    if not os.access(hook, os.X_OK):
        raise GitHooksError(f"the product ships no executable {hook}")
    if configured_hooks_path(home) == str(target):
        return False
    shadowed = shadowed_hooks(checkouts)
    if shadowed:
        raise GitHooksError(
            "a global core.hooksPath would stop running these registered checkout hooks: "
            + ", ".join(shadowed)
            + "; give that repository its own core.hooksPath first"
        )
    if not dry_run:
        completed = _global_git(Path(home), ["core.hooksPath", str(target)])
        if completed.returncode != 0:
            raise GitHooksError(f"cannot set the global core.hooksPath: {completed.stderr.strip()}")
    return True


def hooks_path_finding(home: Path | str, product_root: Path | str) -> dict[str, object] | None:
    """A doctor finding when the shipped pre-push hook is missing or the global hooks path names elsewhere."""
    target = hooks_dir(product_root)
    hook = target / PRE_PUSH
    if not os.access(hook, os.X_OK):
        return {"code": HOOKS_PATH_FINDING, "message": f"the pre-push hook {hook} is missing or not executable"}
    try:
        configured = configured_hooks_path(home)
    except GitHooksError as exc:
        return {"code": HOOKS_PATH_FINDING, "message": str(exc)}
    if configured != str(target):
        return {
            "code": HOOKS_PATH_FINDING,
            "message": f"global core.hooksPath is {configured or '(unset)'}, not {target}; run ummanu upgrade",
        }
    return None
