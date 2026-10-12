"""`--rollback`: put the old installation back from the journal (`docs/RENAME.md` §T3).

Each part acts only on what the journal or the rollback copies say the transition did, and each is
idempotent, so an interrupted rollback is rerun. The order matters: the new units and the new store
stop first, everything that moved comes back, the old unit files are restored, the old store
starts, and only then are the old units enabled. The pipeline stays frozen; the operator resumes it.

The code checkout comes back by `mv` and then `git reset --hard <pre-transition SHA>` on branch
`main`, never by a detached checkout: the next release fast-forwards `main` from there.

This module may run from the renamed checkout it is about to move, so it imports everything it
needs when it is loaded and starts no new import afterwards.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from . import board, rewrite, steps
from .context import Context, TransitionError, now
from .names import NEW, OLD


def _restore_copy(copy: Path, target: Path) -> bool:
    if not copy.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(copy, target)
    return True


def stop_new(ctx: Context) -> list[str]:
    layout, runner = ctx.layout, ctx.runner
    units = sorted(path.name for path in layout.units_dir.glob(f"{NEW.unit_prefix}*") if path.is_file())
    if units:
        runner.sudo("systemctl", "disable", "--now", *units, check=False)
    compose = layout.compose_path(NEW)
    if ctx.journal.fact("board_provisioned") and compose.exists():
        runner.run(board.compose_argv(compose, NEW.compose_project, layout.instance / board.STORE_FILE, "stop"),
                   check=False)
    return units


def restore_claude(ctx: Context) -> dict[str, Any]:
    layout = ctx.layout
    root = layout.claude_projects
    moved_back = []
    for old, new in reversed(ctx.journal.fact("claude_moved") or []):
        if rewrite.move_dir(root / new, root / old) == "moved":
            moved_back.append(old)
    dropped = rewrite.drop_claude_trust(layout.claude_json, ctx.journal.fact("claude_trust_added") or [])
    codex = _restore_copy(layout.state_dir / "home" / layout.codex_config.relative_to(layout.home),
                          layout.codex_config)
    return {"claude_moved_back": moved_back, "claude_trust_dropped": dropped, "codex_config": codex}


def restore_instance(ctx: Context) -> dict[str, Any]:
    layout, runner = ctx.layout, ctx.runner
    instance, copies = layout.instance, layout.state_dir / "instance"
    commit = ctx.journal.fact("instance_commit")
    action = "untouched"
    if commit:
        reverted = runner.git(instance, "log", "-1", "--format=%s")
        if not reverted.startswith(f'Revert "{steps.COMMIT_SUBJECT}"'):
            runner.git(instance, "revert", "--no-edit", commit)
        action = f"reverted {commit[:12]}"
    elif copies.exists() or (layout.state_dir / "secrets").exists():
        # Interrupted before its commit: put every tracked path back as HEAD has it, drop the new ones.
        present = [relative for relative in steps.INSTANCE_PATHS if (instance / relative).exists()]
        runner.git(instance, "reset", "-q", "--", *present, check=False)
        runner.git(instance, "checkout", "HEAD", "--", *present, check=False)
        for relative in (f"projects/{NEW.project_id}.yaml", f"adapters/{NEW.project_id}.yaml",
                         f"state/memory/facts/{NEW.memory_scope_dir}"):
            path = instance / relative
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        action = "restored from HEAD"
    # The ignored env files are not in the commit; their copies are.
    for name in ("runtime.env", board.STORE_FILE):
        _restore_copy(copies / name, instance / name)
    return {"instance": action}


def move_back(ctx: Context) -> dict[str, Any]:
    layout, runner = ctx.layout, ctx.runner
    # The provisioned definition leaves /opt before the moved one comes back to it.
    new_compose = layout.compose_path(NEW)
    old_compose = layout.state_dir / "board" / "postgres-compose.old.yml"
    if old_compose.exists():
        if new_compose.exists():
            runner.sudo("mv", str(new_compose), str(layout.state_dir / "board" / "postgres-compose.new.yml"))
        runner.sudo("mv", str(old_compose), str(new_compose))
    for label, link, privileged, _prefixes in steps.links(ctx):
        original = (ctx.journal.fact("link_targets") or {}).get(label)
        if not original:
            continue
        if privileged:
            runner.sudo("ln", "-sfn", original, str(link))
        else:
            temporary = link.with_name(f".{link.name}.transition")
            temporary.unlink(missing_ok=True)
            temporary.symlink_to(original)
            os.replace(temporary, link)
    moved = []
    for label, source, target, privileged in reversed(steps.moves(ctx)):
        if target.exists() and source.exists():
            raise TransitionError(f"both {source} and {target} exist; refusing to merge them back")
        if not target.exists():
            continue
        if privileged:
            runner.sudo("mv", "-T", str(target), str(source))
        else:
            os.rename(target, source)
        moved.append(label)
    return {"moved_back": moved}


def restore_checkout(ctx: Context) -> dict[str, Any]:
    """`main` reset to the journalled SHA, the old venv back, the old remote, the worktrees relinked."""
    layout, runner = ctx.layout, ctx.runner
    root = layout.product_root(OLD)
    sha = ctx.journal.fact("pre_transition_sha")
    if not sha or not root.exists():
        return {"checkout": "untouched"}
    old_venv = layout.state_dir / "old-venv"
    if old_venv.exists():
        shutil.rmtree(root / ".venv", ignore_errors=True)
        os.rename(old_venv, root / ".venv")
    shutil.rmtree(root / "src" / f"{NEW.package}.egg-info", ignore_errors=True)
    if runner.git_probe(root, "symbolic-ref", "--short", "HEAD") != "main":
        runner.git(root, "checkout", "-q", "main")
    runner.git(root, "reset", "-q", "--hard", sha)
    runner.git(root, "remote", "set-url", "origin", OLD.remote)
    runner.git(root, "worktree", "repair", check=False)
    reverse = [(new, old) for old, new in rewrite.data_plane_prefixes(layout.home)]
    for repository, worktrees in rewrite.worktree_links([layout.data_dir(OLD) / "workspaces"], reverse).items():
        if repository.is_dir():
            runner.run(["git", "-C", str(repository), "worktree", "repair", *map(str, worktrees)], check=False)
    return {"checkout": f"main reset to {sha[:12]}", "branch": runner.git(root, "symbolic-ref", "--short", "HEAD")}


def restore_data_files(ctx: Context) -> list[str]:
    layout = ctx.layout
    copies = layout.state_dir / "data"
    restored = []
    if copies.exists():
        for copy in sorted(path for path in copies.rglob("*") if path.is_file()):
            relative = copy.relative_to(copies)
            if _restore_copy(copy, layout.data_dir(OLD) / relative):
                restored.append(str(relative))
    return restored


def restore_units(ctx: Context) -> dict[str, Any]:
    layout, runner = ctx.layout, ctx.runner
    units: dict[str, dict[str, str]] = ctx.journal.fact("units") or {}
    copies = layout.state_dir / "units"
    restored = []
    for name in units:
        if not (layout.units_dir / name).exists() and (copies / name).exists():
            runner.sudo("cp", "-p", str(copies / name), str(layout.units_dir / name))
            restored.append(name)
    if units:
        runner.sudo("systemctl", "daemon-reload")
    # The old store first: the units it serves start against it.
    if layout.compose_path(OLD).exists():
        runner.run(board.compose_argv(layout.compose_path(OLD), OLD.compose_project,
                                      layout.instance / board.STORE_FILE, "start"))
    enabled = [name for name, state in units.items() if state.get("enabled") == "enabled"]
    if enabled:
        runner.sudo("systemctl", "enable", *enabled)
    active = [name for name, state in units.items() if state.get("active") == "active"]
    if active:
        runner.sudo("systemctl", "start", *active)
    return {"restored": restored, "enabled": enabled, "started": active}


def rollback(ctx: Context) -> dict[str, Any]:
    if not ctx.journal.exists:
        raise TransitionError(f"no journal at {ctx.layout.journal_path}: nothing to roll back")
    if ctx.journal.done("report"):
        raise TransitionError("the transition finished (step 12 reported); roll back by hand if at all")
    outcome: dict[str, Any] = {"stopped_new_units": stop_new(ctx)}
    outcome.update(restore_claude(ctx))
    outcome.update(restore_instance(ctx))
    outcome.update(move_back(ctx))
    outcome.update(restore_checkout(ctx))
    outcome["data_files"] = restore_data_files(ctx)
    outcome.update(restore_units(ctx))
    if ctx.journal.done("upgrade"):
        outcome["follow_up"] = (f"the old role worktrees were removed at step 10: run `{OLD.package} upgrade "
                                f"--no-pull` to recreate them")
    # Keep the journal and the copies together, out of the way of a fresh --apply.
    ctx.journal.data["rolled_back_at"] = now()
    ctx.journal.data["rollback"] = outcome
    ctx.journal.save()
    stamp = now().replace(":", "")
    archive = ctx.layout.home / f"{ctx.layout.state_dir.name}-rolled-back-{stamp}"
    ctx.layout.state_dir.mkdir(parents=True, exist_ok=True)
    os.replace(ctx.journal.path, ctx.layout.state_dir / ctx.journal.path.name)
    os.rename(ctx.layout.state_dir, archive)
    outcome["archived"] = str(archive)
    return outcome


__all__ = ["rollback"]
