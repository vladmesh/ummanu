"""Cut and take back a plain `git worktree`, for every workspace this product owns without Orca.

Two callers share it: a product run's detached workspace (`webproto.workspaces`) and a card's
branch workspace (`dispatch.git_workspace`). What differs between them is how a git call is run and
what a refusal is called, so the caller hands both in: `git(argv, cwd)` runs `git <argv>` in `cwd`
and returns the completed process without raising on a non-zero exit. `git` is the only executable
this module names, and it is never run through a shell.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

#: Runs `git <argv>` with `cwd` as its working directory and returns what git answered.
GitRunner = Callable[[list[str], Path], "subprocess.CompletedProcess[str]"]


def parse_list(listing: str) -> list[dict[str, str]]:
    """One `{attribute: value}` per worktree of a `git worktree list --porcelain [-z]` listing.

    Each record's lines (`worktree <path>`, `HEAD <sha>`, `branch <ref>`, `detached`, `bare`,
    `locked [<reason>]`, `prunable [<reason>]`) become keys; a bare label maps to its empty value.
    With `-z` lines end in NUL and records in an empty line; without it, newlines do the same.
    """
    rows: list[dict[str, str]] = []
    row: dict[str, str] = {}
    for line in listing.split("\0") if "\0" in listing else listing.splitlines():
        if not line:
            if row:
                rows.append(row)
                row = {}
        else:
            key, _, value = line.partition(" ")
            row[key] = value
    if row:
        rows.append(row)
    return rows


def add(
    git: GitRunner, repo: Path, target: Path, start: str, *, branch: str = ""
) -> subprocess.CompletedProcess[str]:
    """Cut a worktree of `repo` at `start` into `target`: on a new `branch`, or detached without one.

    A new branch is created with `-b`, never `-B`: a name some other checkout already owns is a
    refusal git reports, not a reference to move out from under it.
    """
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    placement = ["-b", branch] if branch else ["--detach"]
    return git(["worktree", "add", *placement, str(target), start], Path(repo))


def remove(git: GitRunner, repo: Path, target: Path, *,
           admitted_missing: Callable[[], None] | None = None) -> bool:
    """Remove an exact registered, clean linked worktree and prove both effects.

    Callers own admission and identity proof. This shared primitive never forces
    removal, recursively deletes a refused directory or prunes other registrations.
    Only an owner's durable admission and current exact registration proof can
    authorize finishing an interrupted removal whose directory is already gone.
    """
    target = Path(target)
    if target.absolute() != target.resolve() or target.resolve() == Path(repo).resolve():
        return False

    def listed() -> bool | None:
        result = git(["worktree", "list", "--porcelain", "-z"], Path(repo))
        if result.returncode:
            return None
        return any(row.get("worktree") == str(target.absolute()) for row in parse_list(result.stdout or ""))

    registered = listed()
    if registered is None:
        return False
    if not target.exists() and not target.is_symlink() and admitted_missing is not None:
        admitted_missing()  # Revalidate at the native effect, under the owner's lock.
        if not registered:
            return False
        result = git(["worktree", "remove", str(target)], Path(repo))
        return result.returncode == 0 and not target.exists() and not target.is_symlink() and listed() is False
    if not registered:
        return not target.exists() and not target.is_symlink()
    if not target.is_dir() or target.is_symlink():
        return False
    if not (target / ".git").is_file() or (target / ".git").is_symlink():
        return False
    common = git(["rev-parse", "--path-format=absolute", "--git-common-dir"], Path(repo))
    actual = git(["rev-parse", "--path-format=absolute", "--git-common-dir"], target)
    top = git(["rev-parse", "--show-toplevel"], target)
    if (common.returncode or actual.returncode or top.returncode or
            not (common.stdout or "").strip() or common.stdout.strip() != actual.stdout.strip() or
            top.stdout.strip() != str(target.absolute())):
        return False
    # Git permits ignored files during ordinary removal. They are author work
    # until a caller has removed its exactly owned generated files itself.
    dirt = git(["status", "--porcelain=v1", "--ignored", "--untracked-files=all"], target)
    if dirt.returncode or (dirt.stdout or "").strip():
        return False
    result = git(["worktree", "remove", str(target)], Path(repo))
    return result.returncode == 0 and not target.exists() and listed() is False


def head_of(git: GitRunner, workspace: Path) -> str:
    """The commit `workspace` is at, or "" when git will not say."""
    result = git(["rev-parse", "HEAD"], Path(workspace))
    return (result.stdout or "").strip() if result.returncode == 0 else ""
