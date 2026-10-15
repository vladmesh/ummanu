"""Git change planning and guarded product checkout updates."""

from __future__ import annotations

import hashlib
import re
import stat
import subprocess
from pathlib import Path

from ummanu import _proc, state_repo
from ummanu.dispatch import entrypoint_guard
from ummanu.dispatch.entrypoint_guard import EntrypointMoved
from ummanu.infra.process_receipts import ReceiptError
from ummanu.installation.upgrade.types import (
    MEMORY_CODE_PATHS,
    PACKAGED_UNIT_ROOT,
    SCHEMA_PATHS,
    EntrypointRefused,
    GitError,
    StepResult,
    UpgradeContext,
    WebProcessReceiptError,
)


def _git(root: Path, args: list[str], timeout: int = 120) -> str:
    try:
        # Scrub inherited Git state so `-C root` selects this checkout.
        result = _proc.run(
            ["git", "-c", f"safe.directory={root}", "-C", str(root), *args],
            timeout=timeout,
            env=state_repo.git_env(),
        )
    except FileNotFoundError:
        raise GitError("git not found") from None
    except subprocess.TimeoutExpired:
        raise GitError(f"git {args[0]} timed out") from None
    except OSError:
        raise GitError(f"git {args[0]} could not run") from None
    if result.returncode != 0:
        raise GitError(f"git {args[0]}: {_git_failure_reason(result.stderr or result.stdout)}")
    return (result.stdout or "").strip()


def _git_failure_reason(output: str) -> str:
    """Git's `fatal:` line, else its last line: the first is often progress (`Preparing worktree`)."""
    lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
    fatal = next((line for line in lines if line.startswith("fatal:")), None)
    return fatal or (lines[-1] if lines else "failed")


def require_entrypoint(root: Path, target: str) -> None:
    """Raise `EntrypointRefused` when moving `root` to `target` would remove the running entrypoint.

    The same check the release path runs (`dispatch.entrypoint_guard`), through this module's Git.
    """

    def probe(args: list[str]) -> str | None:
        try:
            return _git(root, args)
        except GitError:
            return None

    try:
        entrypoint_guard.require_entrypoint(probe, target)
    except EntrypointMoved as exc:
        raise EntrypointRefused(root, exc) from exc


def upstream_target(root: Path, base_branch: str) -> tuple[str, str]:
    """Fetch `base_branch` and pin it. Returns ``(head, target)``; refuses a target that moved the entrypoint."""
    head = _git(root, ["rev-parse", "HEAD"])
    _git(root, ["fetch", "--quiet", "origin", base_branch])
    target = _git(root, ["rev-parse", "--verify", f"origin/{base_branch}^{{commit}}"])
    if target != head:
        require_entrypoint(root, target)
    return head, target


def fast_forward(root: Path, base_branch: str) -> tuple[str, str]:
    """Fetch and fast-forward one checkout. Returns ``(before, after)``.

    Strictly ``--ff-only``: a checkout with local commits or a diverged history is left exactly as
    found and the caller hears why. Nothing in an upgrade may discard work that is only on this host.
    The upstream commit is pinned and checked first: a target without the entrypoint the live units
    execute raises `EntrypointRefused` before anything moves (`docs/OPERATIONS.md` §Entrypoint activation).
    """
    before, target = upstream_target(root, base_branch)
    _git(root, ["merge", "--ff-only", target])
    return before, _git(root, ["rev-parse", "HEAD"])


def _changed_paths(root: Path, before: str, after: str) -> tuple[str, ...]:
    if before == after:
        return ()
    return tuple(_git(root, ["diff", "--name-only", f"{before}..{after}"]).splitlines())


def _touches(changed: tuple[str, ...], prefixes: tuple[str, ...]) -> bool:
    return any(path.startswith(prefix) for path in changed for prefix in prefixes)


def planned_unit_names(changed: tuple[str, ...], name_prefix: str) -> tuple[str, ...]:
    """The shipped units under ``name_prefix`` a revision move edits, by unit file name.

    `step_host` answers the same question from the live host, and on the apply path the two agree,
    because by then the checkout has moved and reconcile has seen the new bytes. Under `--dry-run`
    only this one can answer it: the files are still the old ones on disk, and the change exists
    only in the diff against the target revision.
    """
    return tuple(
        name
        for path in changed
        if path.startswith(PACKAGED_UNIT_ROOT)
        for name in (path[len(PACKAGED_UNIT_ROOT) :],)
        if "/" not in name and name.startswith(name_prefix)
    )


def record_change_plan(context: UpgradeContext, changed: tuple[str, ...]) -> None:
    """Turn one revision move into the facts every later step reads. The only place that happens.

    Three entry points reach the same schedule — a fresh `pull`, the re-executed pulled schedule
    arriving through its handoff marker, and `--dry-run` planning against the upstream target — and
    each of them used to derive its own subset of these facts. `--dry-run` derived none at all, so
    it returned before it knew anything and no later step could name a pending action; the handoff
    derived `code_changed` and silently left `schemas_changed` false. Both were one defect with
    three spellings, so there is now one recorder and the entry points only supply the path set.
    """
    context.changed_paths = changed
    context.code_changed = _touches(changed, MEMORY_CODE_PATHS)
    context.schemas_changed = _touches(changed, SCHEMA_PATHS)


DIRTY_PATHS_SHOWN = 5


def _dirty_paths(porcelain: str) -> str:
    """The first `DIRTY_PATHS_SHOWN` paths of `git status --porcelain` output, and how many more."""
    paths = [line.split(None, 1)[-1] for line in porcelain.splitlines() if line.strip()]
    shown = ", ".join(paths[:DIRTY_PATHS_SHOWN])
    hidden = len(paths) - DIRTY_PATHS_SHOWN
    return f"{shown} (+{hidden} more)" if hidden > 0 else shown


def step_pull(context: UpgradeContext) -> StepResult:
    if context.pull_result is not None:
        return context.pull_result
    if not context.pull:
        if context.handoff_before and context.handoff_after:
            return StepResult(
                "pull",
                "changed",
                f"{context.handoff_before[:12]} -> {context.handoff_after[:12]}; running pulled schedule",
            )
        return StepResult("pull", "skipped", "--no-pull")
    try:
        dirty = _git(context.product_root, ["status", "--porcelain"])
        if dirty:
            return StepResult("pull", "failed", f"product checkout has uncommitted changes: {_dirty_paths(dirty)}")
        if context.dry_run:
            # Plan against the target revision and write nothing: `fetch` and `diff` read, and the
            # checkout is left exactly where it was. This is what makes every later step's
            # `would-change` line about the revision an operator is about to apply rather than
            # about the one already installed.
            head, target = upstream_target(context.product_root, context.base_branch)
            record_change_plan(context, _changed_paths(context.product_root, head, target))
            if head == target:
                return StepResult("pull", "unchanged", head[:12])
            return StepResult("pull", "changed", f"{head[:12]} -> {target[:12]} (not applied)")
        before, after = fast_forward(context.product_root, context.base_branch)
        changed = _changed_paths(context.product_root, before, after)
    except GitError as exc:
        return StepResult("pull", "failed", str(exc))
    if before == after:
        return StepResult("pull", "unchanged", after[:12])
    record_change_plan(context, changed)
    context.pulled_before = before
    context.pulled_after = after
    return StepResult("pull", "changed", f"{before[:12]} -> {after[:12]}")


def _git_tracked_digest(product_root: Path, paths: tuple[str, ...]) -> str:
    """Hash selected tracked product inputs, including their checkout-relative names.

    A working process may create bytecode below ``src/``.  Git's tracked list intentionally leaves
    that runtime by-product out while still including local edits to a tracked input.  The revision
    is recorded separately, so a moved checkout and a dirty tracked file are both visible evidence.
    """
    try:
        listed = _git(product_root, ["ls-files", "--", *paths])
    except GitError as exc:
        raise WebProcessReceiptError(f"cannot list product inputs: {exc}") from None
    digest = hashlib.sha256()
    digest.update(b"ummanu-web-input-v1\0")
    digest.update("\0".join(paths).encode("utf-8"))
    for relative in filter(None, listed.splitlines()):
        path = product_root / relative
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise OSError("not a regular file")
            contents = path.read_bytes()
        except OSError as exc:
            raise WebProcessReceiptError(f"cannot read product input {relative}: {exc}") from None
        digest.update(b"\0file\0")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(contents)
    return digest.hexdigest()


def _product_revision(product_root: Path, error: type[ReceiptError]) -> str:
    try:
        revision = _git(product_root, ["rev-parse", "HEAD"])
    except GitError as exc:
        raise error(f"cannot read product revision: {exc}") from None
    if not re.fullmatch(r"[0-9a-f]{40,64}", revision):
        raise error("cannot read an exact product revision")
    return revision
