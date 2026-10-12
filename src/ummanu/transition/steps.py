"""The installation transition (`docs/RENAME.md` §T3): twelve steps, each a plan and an apply.

Every apply is idempotent and returns the facts its journal record keeps; a fact the rollback
needs is recorded *before* the action it describes, so an interrupted step still leaves it.
Steps 1-4 run in the pre-rename tree, step 5 is the shell bootstrap's (the Python half only
verifies it), and steps 6-12 run in the renamed tree.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import shutil
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import board, preconditions, rewrite, secret_rewrap
from .context import Context, TransitionError
from .names import COUNTS_MARKER, DONE_MARKER, INSTANCE_PROJECT, NEW, OLD, ROLES

RUNBOOK = "docs/RENAME.md §T3"
FREEZE_REASON = f"transition from {OLD.package} to {NEW.package} ({RUNBOOK} step 2)"
COMMIT_SUBJECT = f"Transition the installation from {OLD.package} to {NEW.package}"
DISPATCHER_UNIT = f"{OLD.unit_prefix}dispatcher-production"
#: The observer record state a refused freeze stop leaves (`dispatch.observer.STATE_PAUSE_STOP_PENDING`).
OBSERVER_STOP_PENDING = "pause-stop-pending"
#: Exits 0 only when importing the old package fails the way the DoD requires.
IMPORT_CHECK = (
    "import importlib, sys\n"
    "try:\n    importlib.import_module(sys.argv[1])\n"
    "except ModuleNotFoundError:\n    print('ModuleNotFoundError'); sys.exit(0)\n"
    "print('importable'); sys.exit(1)\n"
)


@dataclass(frozen=True)
class Step:
    number: int
    name: str
    title: str
    plan: Callable[[Context], list[str]]
    apply: Callable[[Context], dict[str, Any]]
    #: "old": runs in either tree before the checkout moves; "shell": performed by the bootstrap;
    #: "new": needs the renamed tree.
    phase: str = "new"


class BoardOps:
    """The board store half of steps 1, 3 and 8, separate so unit tests can stand it in."""

    def reads(self, ctx: Context) -> preconditions.BoardReads | None:
        try:
            config = board.read_store_env(ctx.layout.instance / board.STORE_FILE, OLD)
        except TransitionError:
            return None

        class Reads:
            def prepared_sprints(self) -> list[str]:
                return board.prepared_sprints(config, preconditions.BASELINE_MARKER)

            def markers(self, sprint: str) -> dict[str, int]:
                return board.sprint_markers(config, sprint, (preconditions.BASELINE_MARKER, COUNTS_MARKER))

        return Reads()

    def dump(self, ctx: Context) -> dict[str, Any]:
        config = board.read_store_env(ctx.layout.instance / board.STORE_FILE, OLD)
        return board.dump(config, ctx.layout.state_dir / "board")

    def stop_old(self, ctx: Context) -> None:
        ctx.runner.run(board.compose_argv(
            ctx.layout.compose_path(OLD), OLD.compose_project, ctx.layout.instance / board.STORE_FILE, "stop",
        ))

    def build_new(self, ctx: Context, metadata: dict[str, Any]) -> dict[str, Any]:
        layout = ctx.layout
        compose = layout.compose_path(NEW)
        board.require_renamed_tree(NEW, compose)
        config = board.read_store_env(layout.instance / board.STORE_FILE, NEW)
        actions = board.provision_new_store(layout.instance, compose)
        restored = board.restore(layout.state_dir / "board" / "postgres.dump", layout.instance, metadata, config)
        translated = board.translate(
            config, OLD, NEW,
            old_root=layout.product_root(OLD), new_root=layout.product_root(NEW),
            old_po=layout.data_dir(OLD) / "po", new_po=layout.data_dir(NEW) / "po",
        )
        after = board.table_counts(config)
        return {"provision": list(actions), "restore": restored, "translation": translated, "counts_after": after}


def _cli(ctx: Context, names: Any) -> str:
    return str(ctx.layout.cli(names))


def runtime_user(ctx: Context) -> str:
    try:
        import pwd

        return pwd.getpwuid(ctx.layout.home.stat().st_uid).pw_name
    except (KeyError, OSError):
        return getpass.getuser()


# -- step 1 --------------------------------------------------------------------------------------


def plan_preconditions(ctx: Context) -> list[str]:
    checks, _facts = preconditions.run(ctx, ctx.board.reads(ctx), fetch=False)
    lines = [f"  $ git -C {ctx.layout.code_root()} fetch origin   (--apply only; the plan reads the refs as they are)"]
    return lines + [check.line() for check in checks]


def apply_preconditions(ctx: Context) -> dict[str, Any]:
    checks, facts = preconditions.run(ctx, ctx.board.reads(ctx), fetch=True)
    for check in checks:
        ctx.say(check.line())
    failed = [check.name for check in checks if not check.ok]
    if failed:
        raise TransitionError("preconditions unmet: " + ", ".join(failed))
    ctx.sprint = str(facts.get("sprint") or ctx.sprint)
    ctx.journal.record("pre_transition_sha", facts["checkout_sha"])
    # The one commit step 5 may fast-forward to; origin/main moving later is a refusal there.
    ctx.journal.record("target_sha", facts["origin_main"])
    ctx.journal.record("sprint", ctx.sprint)
    return facts


# -- step 2 --------------------------------------------------------------------------------------


def foreign_units(instance: Path) -> set[str]:
    try:
        text = (instance / "instance.yaml").read_text(encoding="utf-8")
    except OSError:
        return set()
    match = re.search(r"^\s*foreign_units:\s*\n((?:\s+- .*\n?)*)", text, re.MULTILINE)
    return {line.strip()[2:].strip() for line in match.group(1).splitlines()} if match else set()


def old_units(ctx: Context) -> list[str]:
    """The installed `secretary-*` unit files, timers first, minus the instance's foreign units."""
    skip = foreign_units(ctx.layout.instance)
    names = [
        path.name for path in ctx.layout.units_dir.glob(f"{OLD.unit_prefix}*")
        if path.is_file() and path.name not in skip and path.suffix in (".service", ".timer")
    ]
    return sorted(names, key=lambda name: (not name.endswith(".timer"), name))


def unit_environment(ctx: Context) -> dict[str, str]:
    """The dispatcher unit's environment, so the old CLI runs as the tick does."""
    environment = {key: value for key, value in os.environ.items()}
    copy = ctx.layout.state_dir / "units" / f"{DISPATCHER_UNIT}.service"
    source = copy if copy.exists() else ctx.layout.units_dir / f"{DISPATCHER_UNIT}.service"
    try:
        text = source.read_text(encoding="utf-8")
    except OSError:
        return environment
    for line in text.splitlines():
        if line.startswith("EnvironmentFile="):
            path = Path(line.split("=", 1)[1].lstrip("-"))
            try:
                for entry in path.read_text(encoding="utf-8").splitlines():
                    if "=" in entry and not entry.startswith("#"):
                        key, value = entry.split("=", 1)
                        environment[key] = value
            except OSError:
                continue
        elif line.startswith("Environment="):
            key, _, value = line.split("=", 1)[1].partition("=")
            environment[key] = value
    return environment


def _old_pipeline(ctx: Context, *arguments: str) -> list[str]:
    return [
        _cli(ctx, OLD), *arguments,
        "--instance", str(ctx.layout.instance),
        "--data-dir", str(ctx.layout.data_dir(OLD)),
        "--owner", OLD.dispatcher_owner,
        "--actor", "transition",
    ]


def plan_freeze(ctx: Context) -> list[str]:
    units = old_units(ctx)
    lines = [
        f"  $ sudo -n systemctl stop {DISPATCHER_UNIT}.timer {DISPATCHER_UNIT}.service",
        "  $ " + " ".join(_old_pipeline(ctx, "resume")) + "   (only from a drain: a drain cannot become a freeze)",
        "  $ " + " ".join(_old_pipeline(ctx, "pause", "freeze")) + f" --reason '{FREEZE_REASON}'",
        f"  copy {len(units)} unit files {ctx.layout.units_dir}/{OLD.unit_prefix}* -> {ctx.layout.state_dir / 'units'}/",
        "  $ sudo -n systemctl disable --now " + " ".join(units),
    ]
    return lines


def freeze_warnings(stdout: str) -> list[str]:
    """The warnings of the freeze's own answer (its last JSON line); an unreadable answer is one."""
    for line in reversed((stdout or "").strip().splitlines()):
        try:
            answer = json.loads(line)
        except ValueError:
            continue
        if isinstance(answer, dict):
            return [str(warning) for warning in answer.get("warnings") or []]
    return ["the freeze printed no answer to check"]


def pending_observer_stops(data_dir: Path) -> list[str]:
    """Observer records a freeze left as a pending stop: their heads may still run."""
    try:
        payload = json.loads((data_dir / "dispatcher" / "production-state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    observers = payload.get("observers") if isinstance(payload, dict) else None
    if not isinstance(observers, dict):
        return []
    return sorted(
        ref for ref, record in observers.items()
        if isinstance(record, dict) and record.get("state") == OBSERVER_STOP_PENDING
    )


def apply_freeze(ctx: Context) -> dict[str, Any]:
    layout, runner = ctx.layout, ctx.runner
    units = ctx.journal.fact("units")
    if units is None:
        names = old_units(ctx)
        units = {}
        for name in names:
            enabled = runner.run(["systemctl", "is-enabled", name], check=False).stdout.strip()
            active = runner.run(["systemctl", "is-active", name], check=False).stdout.strip()
            units[name] = {"enabled": enabled, "active": active}
        ctx.journal.record("units", units)
    copies = layout.state_dir / "units"
    copies.mkdir(parents=True, exist_ok=True)
    for name in units:
        source, target = layout.units_dir / name, copies / name
        if source.exists() and not target.exists():
            shutil.copy2(source, target)
    environment = unit_environment(ctx)
    runner.sudo("systemctl", "stop", f"{DISPATCHER_UNIT}.timer", f"{DISPATCHER_UNIT}.service")
    pause = preconditions.pipeline_checks(layout.data_dir(OLD))[1].get("pause_mode", "")
    if pause == "drain":
        runner.run(_old_pipeline(ctx, "resume"), env=environment)
    if pause != "freeze":
        result = runner.run([*_old_pipeline(ctx, "pause", "freeze"), "--reason", FREEZE_REASON], env=environment)
        warnings = freeze_warnings(result.stdout)
        if warnings:
            raise TransitionError("the freeze did not stop everything: " + "; ".join(warnings))
    pending = pending_observer_stops(layout.data_dir(OLD))
    if pending:
        raise TransitionError(
            "observer heads the freeze could not stop are still on the books: " + ", ".join(pending)
            + "; stop them, then rerun"
        )
    present = [name for name in units if (layout.units_dir / name).exists()]
    if present:
        runner.sudo("systemctl", "disable", "--now", *present)
    return {"units": sorted(units), "frozen_from": pause or "none"}


# -- step 3 --------------------------------------------------------------------------------------


def plan_dump(ctx: Context) -> list[str]:
    layout = ctx.layout
    return [
        (f"  dump {OLD.db_name}@{layout.instance / board.STORE_FILE} ({OLD.env_prefix}DB_*) -> "
        f"{layout.state_dir / 'board' / 'postgres.dump'} + dump.json (table_counts, alembic_head, source_endpoint_id)"),
        "  $ " + " ".join(board.compose_argv(layout.compose_path(OLD), OLD.compose_project,
                                             layout.instance / board.STORE_FILE, "stop"))
        + "   (the volume is kept for rollback)",
    ]


def apply_dump(ctx: Context) -> dict[str, Any]:
    metadata = ctx.board.dump(ctx)
    ctx.say("  dump counts: " + ", ".join(f"{name} {metadata['table_counts'].get(name)}" for name in
                                          ("tasks", "issues", "sprints", "board_events")))
    ctx.board.stop_old(ctx)
    return {"table_counts": metadata["table_counts"], "alembic_head": metadata.get("alembic_head"),
            "bytes": metadata.get("bytes")}


# -- step 4 --------------------------------------------------------------------------------------


def moves(ctx: Context) -> list[tuple[str, Path, Path, bool]]:
    """(label, source, target, privileged) for the four paths."""
    layout = ctx.layout
    return [
        ("checkout", layout.product_root(OLD), layout.product_root(NEW), False),
        ("data", layout.data_dir(OLD), layout.data_dir(NEW), False),
        ("opt", layout.opt_dir(OLD), layout.opt_dir(NEW), True),
        ("tools", layout.tools_dir(OLD), layout.tools_dir(NEW), False),
    ]


def links(ctx: Context) -> list[tuple[str, Path, bool, list[tuple[str, str]]]]:
    layout = ctx.layout
    return [
        ("ide-link", layout.orca_link, True, [(str(layout.opt_dir(OLD)), str(layout.opt_dir(NEW)))]),
        ("uv", layout.uv_link, False, [(str(layout.tools_dir(OLD)), str(layout.tools_dir(NEW)))]),
    ]


def venv_extras(root: Path) -> list[str]:
    """The extras to reinstall: `dev` plus every declared extra whose distributions the venv carries."""
    try:
        declared = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"][
            "optional-dependencies"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError):
        return ["dev"]

    def normal(name: str) -> str:
        return re.sub(r"[-_.]+", "-", name).lower()

    carried = {
        normal(path.name.split("-", 1)[0])
        for path in (root / ".venv" / "lib").glob("python*/site-packages/*.dist-info")
    }
    extras = {"dev"}
    for name, requirements in declared.items():
        wanted = {
            normal(match.group(1)) for requirement in requirements
            if isinstance(requirement, str) and (match := re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement))
        }
        if wanted and wanted <= carried:
            extras.add(normal(name))
    return sorted(extras)


def plan_moves(ctx: Context) -> list[str]:
    lines = []
    for _label, source, target, privileged in moves(ctx):
        lines.append(f"  $ {'sudo -n ' if privileged else ''}mv -T {source} {target}")
    for _label, link, privileged, prefixes in links(ctx):
        lines.append(f"  $ {'sudo -n ' if privileged else ''}ln -sfn <{prefixes[0][0]}… -> {prefixes[0][1]}…> {link}")
    lines.append(f"  record the venv extras of {ctx.layout.product_root(OLD) / '.venv'} for step 5")
    return lines


def apply_moves(ctx: Context) -> dict[str, Any]:
    layout = ctx.layout
    if ctx.journal.fact("venv_extras") is None and layout.product_root(OLD).exists():
        ctx.journal.record("venv_extras", venv_extras(layout.product_root(OLD)))
        layout.state_dir.mkdir(parents=True, exist_ok=True)
        (layout.state_dir / "venv-extras").write_text(",".join(ctx.journal.fact("venv_extras")) + "\n",
                                                      encoding="utf-8")
    done = []
    for label, source, target, privileged in moves(ctx):
        if source.exists() and target.exists():
            raise TransitionError(f"both {source} and {target} exist; refusing to merge them")
        if source.exists():
            if privileged:
                ctx.runner.sudo("mv", "-T", str(source), str(target))
            else:
                os.rename(source, target)
            done.append(label)
        elif not target.exists() and label in ("checkout", "data"):
            raise TransitionError(f"neither {source} nor {target} exists")
    targets = dict(ctx.journal.fact("link_targets") or {})
    for label, link, privileged, prefixes in links(ctx):
        if not link.is_symlink():
            continue
        current = os.readlink(link)
        replaced = rewrite.swap_prefix(current, prefixes)
        if replaced == current:
            continue
        targets.setdefault(label, current)
        ctx.journal.record("link_targets", targets)
        if privileged:
            ctx.runner.sudo("ln", "-sfn", replaced, str(link))
        else:
            temporary = link.with_name(f".{link.name}.transition")
            temporary.unlink(missing_ok=True)
            temporary.symlink_to(replaced)
            os.replace(temporary, link)
    return {"moved": done, "links": targets, "venv_extras": ctx.journal.fact("venv_extras")}


# -- step 5 --------------------------------------------------------------------------------------


def plan_checkout(ctx: Context) -> list[str]:
    root, state = ctx.layout.product_root(NEW), ctx.layout.state_dir
    target = ctx.journal.fact("target_sha") or "<origin/main as step 1 journals it>"
    return [
        f"  (scripts/transition-from-{OLD.package}.sh, before the new venv exists)",
        f"  $ git -C {root} fetch origin   (refuses if origin/main moved away from {target})",
        f"  $ git -C {root} merge --ff-only {target}   (on branch main)",
        f"  $ git -C {root} remote set-url origin {NEW.remote}",
        f"  $ rm -rf {root / 'src' / OLD.package} {root / 'src' / (OLD.package + '.egg-info')}",
        f"  $ mv {root / '.venv'} {state / 'old-venv'}   (kept for rollback: its scripts name the old path)",
        f"  $ python3 -m venv {root / '.venv'} && pip install -e '{root}[<extras recorded by step 4>]'",
        f"  check: {ctx.layout.python(NEW)} -P -c 'import {OLD.package}' raises ModuleNotFoundError",
        f"  $ exec {ctx.layout.cli(NEW)} transition from-{OLD.package} --instance {ctx.layout.instance} --apply",
    ]


def old_package_import(ctx: Context) -> tuple[bool, str]:
    """Whether the new venv refuses `import <old package>` with ModuleNotFoundError, and what it said."""
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    result = ctx.runner.run(
        [str(ctx.layout.python(NEW)), "-P", "-c", IMPORT_CHECK, OLD.package],
        check=False, env=environment, timeout=120,
    )
    said = ((result.stdout or "") + (result.stderr or "")).strip().splitlines()
    return result.returncode == 0, said[-1] if said else f"exit {result.returncode}"


def apply_checkout(ctx: Context) -> dict[str, Any]:
    """The bootstrap did the work; this verifies it before anything runs from the new tree."""
    root = ctx.layout.product_root(NEW)
    target = ctx.journal.fact("target_sha")
    problems = []
    branch = ctx.runner.git_probe(root, "symbolic-ref", "--short", "HEAD")
    head = ctx.runner.git_probe(root, "rev-parse", "HEAD")
    remote = ctx.runner.git_probe(root, "remote", "get-url", "origin")
    if branch != "main":
        problems.append(f"{root} is on {branch or 'a detached HEAD'}, not main")
    if not target:
        problems.append("the journal has no target commit from step 1")
    elif head != target:
        problems.append(f"{root} is at {head}, not at {target}, the origin/main step 1 checked")
    if remote != NEW.remote:
        problems.append(f"origin is {remote}, not {NEW.remote}")
    if not ctx.layout.cli(NEW).exists():
        problems.append(f"{ctx.layout.cli(NEW)} is missing")
    for leftover in (root / "src" / OLD.package, root / "src" / f"{OLD.package}.egg-info"):
        if leftover.exists():
            problems.append(f"{leftover} is still there")
    if not problems:
        refused, said = old_package_import(ctx)
        if not refused:
            problems.append(f"the new venv imports {OLD.package} ({said})")
    if problems:
        raise TransitionError("the bootstrap's step 5 is not complete: " + "; ".join(problems))
    old_venv = ctx.layout.state_dir / "old-venv"
    return {"new_sha": head, "old_venv": str(old_venv) if old_venv.exists() else "",
            "old_import": "ModuleNotFoundError"}


# -- step 6 --------------------------------------------------------------------------------------


def _data_files(ctx: Context) -> list[tuple[str, Path]]:
    data = ctx.layout.data_dir(NEW)
    return [
        ("production-state.json", data / "dispatcher" / "production-state.json"),
        ("data-manifest.json", data / "data-manifest.json"),
        ("codex-home/config.toml", data / "codex-home" / "config.toml"),
    ]


def plan_data_plane(ctx: Context) -> list[str]:
    layout = ctx.layout
    return [
        f"  $ git -C {layout.product_root(NEW)} worktree repair   (the role worktrees follow the moved checkout)",
        (f"  $ git -C <repo> worktree repair <moved worktrees>   (every worktree under {layout.data_dir(NEW) / 'workspaces'}:"
        " checkout, observer root, codegen and instance worktrees)"),
        f"  $ git -C {layout.product_root(NEW)} worktree prune",
        (f"  rewrite {layout.data_dir(NEW) / 'dispatcher' / 'production-state.json'}: path prefixes, owner "
        f"{OLD.dispatcher_owner} -> {NEW.dispatcher_owner}"),
        f"  rewrite {layout.data_dir(NEW) / 'data-manifest.json'}: data_dir",
        f"  rewrite {layout.data_dir(NEW) / 'codex-home' / 'config.toml'}: live [projects.\"…\"] keys",
        f"  drop the [mcp_servers.*] table of {layout.codex_config} that runs {layout.product_root(OLD)}/…",
        f"  (copies of every rewritten file under {layout.state_dir / 'data'} and {layout.state_dir / 'home'})",
    ]


def apply_data_plane(ctx: Context) -> dict[str, Any]:
    layout, runner = ctx.layout, ctx.runner
    root = layout.product_root(NEW)
    runner.git(root, "worktree", "repair", check=False)
    prefixes = rewrite.data_plane_prefixes(layout.home)
    repaired = {}
    for repository, worktrees in rewrite.worktree_links([layout.data_dir(NEW) / "workspaces"], prefixes).items():
        if repository.is_dir():
            runner.run(["git", "-C", str(repository), "worktree", "repair", *map(str, worktrees)], check=False)
            repaired[str(repository)] = len(worktrees)
    runner.git(root, "worktree", "prune")
    for label, path in _data_files(ctx):
        rewrite.backup(path, layout.data_dir(NEW), layout.state_dir / "data")
        if label == "production-state.json":
            rewrite.rewrite_production_state(path, layout.home)
        elif label == "data-manifest.json":
            rewrite.rewrite_data_manifest(path, layout.home)
    codex_home = rewrite.rename_codex_projects(_data_files(ctx)[2][1], rewrite.live_trust_paths(layout.home))
    rewrite.backup(layout.codex_config, layout.home, layout.state_dir / "home")
    dropped = rewrite.drop_stale_mcp_servers(layout.codex_config, layout.product_root(OLD))
    return {"repaired": repaired, "codex_home_keys": codex_home, "mcp_dropped": dropped}


# -- step 7 --------------------------------------------------------------------------------------


INSTANCE_PATHS = ("instance.yaml", "projects", "adapters", "heads/heads.toml", "secrets", "state/memory")


def plan_instance(ctx: Context) -> list[str]:
    instance = ctx.layout.instance
    return [
        (f"  edit {instance / 'instance.yaml'}: name, description, data_dir, host.unit_prefix, drop "
        f"{OLD.unit_prefix}* foreign_units"),
        (f"  $ git mv projects/{OLD.project_id}.yaml projects/{NEW.project_id}.yaml   (id, repo, remote, orca_binding, "
        "adapter; drop curator_roots)"),
        f"  $ git mv adapters/{OLD.project_id}.yaml adapters/{NEW.project_id}.yaml   (import_package: {NEW.package})",
        f"  edit adapters/*.yaml: $HOME/{OLD.tools_dir} -> $HOME/{NEW.tools_dir}",
        f"  edit heads/heads.toml: -m {OLD.package}.runtime.resource_probe -> -m {NEW.package}.runtime.resource_probe",
        f"  edit secrets/catalog.yaml: {OLD.env_prefix}* environment, {ctx.layout.data_dir(OLD)}/ paths",
        (f"  re-wrap secrets/installation-key.json and re-seal secrets/values/*.enc.json "
        f"({OLD.value_kdf_info} -> {NEW.value_kdf_info}); old files to {ctx.layout.state_dir / 'secrets'}"),
        (f"  $ git mv state/memory/facts/{OLD.memory_scope_dir} state/memory/facts/{NEW.memory_scope_dir}; "
        f"git rm -r state/memory/facts/{OLD.product_memory} state/memory/packs/{OLD.product_memory}.json"),
        (f"  rewrite runtime.env ({OLD.env_prefix}* -> {NEW.env_prefix}*, drop {', '.join(rewrite.DROPPED_RUNTIME_KEYS)})"
        f" and board-store.env ({NEW.env_prefix}DB_*, db {NEW.db_name}, users {NEW.db_owner}/{NEW.db_app}/"
        f"{NEW.db_read}, same passwords); both are ignored by git, copies under {ctx.layout.state_dir / 'instance'}"),
        f"  $ git -C {instance} commit -m '{COMMIT_SUBJECT}'   (one commit)",
    ]


def _git_mv(ctx: Context, instance: Path, source: str, target: str) -> None:
    if (instance / source).exists() and not (instance / target).exists():
        ctx.runner.git(instance, "mv", source, target)


def apply_instance(ctx: Context) -> dict[str, Any]:
    layout, runner = ctx.layout, ctx.runner
    instance, copies = layout.instance, layout.state_dir / "instance"
    committed = ctx.journal.fact("instance_commit")
    if committed:
        return {"commit": committed}
    if not ctx.journal.fact("instance_rewrite_started"):
        # The commit is the transition's alone: nothing staged, nothing pending in the paths it rewrites.
        if runner.run(["git", "-C", str(instance), "diff", "--cached", "--quiet"], check=False).returncode:
            raise TransitionError(f"{instance} has staged changes; the transition commits alone")
        dirty = runner.git(instance, "status", "--porcelain", "--untracked-files=all", "--", *INSTANCE_PATHS)
        if dirty:
            raise TransitionError(
                f"{instance} has uncommitted changes in the paths step 7 rewrites; commit or discard them first: "
                + "; ".join(line.strip() for line in dirty.splitlines()[:10])
            )
        ctx.journal.record("instance_rewrite_started", True)
    for relative in ("instance.yaml", f"projects/{OLD.project_id}.yaml", f"adapters/{OLD.project_id}.yaml",
                     "heads/heads.toml", "secrets/catalog.yaml", "runtime.env", board.STORE_FILE):
        rewrite.backup(instance / relative, instance, copies)
    for adapter in sorted((instance / "adapters").glob("*.yaml")):
        rewrite.backup(adapter, instance, copies)

    path = instance / "instance.yaml"
    rewrite.write_text(path, rewrite.edit_instance_yaml(path.read_text(encoding="utf-8"), layout.home))
    _git_mv(ctx, instance, f"projects/{OLD.project_id}.yaml", f"projects/{NEW.project_id}.yaml")
    path = instance / "projects" / f"{NEW.project_id}.yaml"
    if path.exists():
        rewrite.write_text(path, rewrite.edit_project_yaml(path.read_text(encoding="utf-8"), layout.home))
    _git_mv(ctx, instance, f"adapters/{OLD.project_id}.yaml", f"adapters/{NEW.project_id}.yaml")
    path = instance / "adapters" / f"{NEW.project_id}.yaml"
    if path.exists():
        rewrite.write_text(path, rewrite.edit_adapter_yaml(path.read_text(encoding="utf-8")))
    for adapter in sorted((instance / "adapters").glob("*.yaml")):
        rewrite.write_text(adapter, rewrite.edit_tools_paths(adapter.read_text(encoding="utf-8")))
    path = instance / "heads" / "heads.toml"
    if path.exists():
        rewrite.write_text(path, rewrite.edit_heads_toml(path.read_text(encoding="utf-8")))
    path = instance / "secrets" / "catalog.yaml"
    if path.exists():
        rewrite.write_text(path, rewrite.edit_catalog(path.read_text(encoding="utf-8"), layout.home))
    rewrapped = secret_rewrap.rewrap_store(instance / "secrets", layout.state_dir / "secrets", OLD, NEW)
    verified = (secret_rewrap.verify_store(instance / "secrets", NEW)
                if (instance / "secrets" / secret_rewrap.KEY_PARAMS_NAME).exists() else [])
    ctx.say(f"  secrets: {len(verified)} value(s) open under {NEW.value_kdf_info}")
    facts = instance / "state" / "memory" / "facts"
    _git_mv(ctx, instance, f"state/memory/facts/{OLD.memory_scope_dir}", f"state/memory/facts/{NEW.memory_scope_dir}")
    for stale in (facts / OLD.product_memory, instance / "state" / "memory" / "packs" / f"{OLD.product_memory}.json"):
        if stale.exists():
            runner.git(instance, "rm", "-r", "-q", str(stale.relative_to(instance)))

    path = instance / "runtime.env"
    if path.exists():
        rewrite.write_text(path, rewrite.edit_runtime_env(path.read_text(encoding="utf-8")))
    path = instance / board.STORE_FILE
    try:
        old_config = board.read_store_env(path, OLD)
    except TransitionError:
        board.read_store_env(path, NEW)  # already rewritten, or refuse an unreadable file
    else:
        board.write_store_env(path, board.store_env_text(board.renamed_config(old_config, NEW), NEW))

    # Exactly the files the rewriters write; the moves and removals above staged themselves.
    written = [
        path for path in (
            instance / "instance.yaml",
            instance / "projects" / f"{NEW.project_id}.yaml",
            *sorted((instance / "adapters").glob("*.yaml")),
            instance / "heads" / "heads.toml",
            instance / "secrets" / "catalog.yaml",
            instance / "secrets" / secret_rewrap.KEY_PARAMS_NAME,
            *sorted((instance / "secrets" / "values").glob(f"*{secret_rewrap.VALUE_SUFFIX}")),
        )
        if path.is_file()
    ]
    runner.git(instance, "add", "--", *(str(path.relative_to(instance)) for path in written))
    if runner.run(["git", "-C", str(instance), "diff", "--cached", "--quiet"], check=False).returncode:
        runner.git(instance, "commit", "-q", "-m", COMMIT_SUBJECT, "-m",
                   f"One-shot rewrite by the transition command ({RUNBOOK} step 7). The repository keeps its name.")
        commit = runner.git(instance, "rev-parse", "HEAD")
    else:
        last = runner.git(instance, "log", "-1", "--format=%H%x00%s")
        sha, _, subject = last.partition("\0")
        if subject != COMMIT_SUBJECT:
            raise TransitionError(f"{instance} has nothing to commit and its last commit is not the transition's")
        commit = sha
    ctx.journal.record("instance_commit", commit)
    return {"commit": commit, "secrets_verified": verified, "secrets_already_new": rewrapped.already}


# -- step 8 --------------------------------------------------------------------------------------


def plan_board(ctx: Context) -> list[str]:
    layout = ctx.layout
    old_compose = layout.compose_path(NEW)
    return [
        (f"  $ sudo -n mv {old_compose} {layout.state_dir / 'board' / 'postgres-compose.old.yml'}"
        f"   (the moved {OLD.env_prefix}DB_* definition)"),
        (f"  provision {NEW.compose_project} at {old_compose} ({NEW.env_prefix}DB_*, db {NEW.db_name}, fresh volume "
        f"{NEW.compose_project}_board-db, same port)"),
        "  restore_dump the step-3 dump (refuses any table-count mismatch, refuses the old endpoint)",
        (f"  translate in one transaction: product {NEW.product_id} (old archived), project {NEW.project_id} (old "
        f"disabled), repository {layout.product_root(NEW)}, product_projects ({NEW.product_id}, {NEW.project_id}) and "
        f"({NEW.product_id}, {INSTANCE_PROJECT}); open sprints' product/allowed_productions/reservations/repositories; "
        "open issues; open PO sessions' cwd"),
        "  counts after == dump counts except products +1, projects +1, repositories +1, product_projects +2",
    ]


def apply_board(ctx: Context) -> dict[str, Any]:
    layout = ctx.layout
    compose = layout.compose_path(NEW)
    aside = layout.state_dir / "board" / "postgres-compose.old.yml"
    if compose.exists() and not aside.exists():
        text = ctx.runner.sudo("cat", str(compose)).stdout
        if f"{OLD.env_prefix}DB_" in text:
            aside.parent.mkdir(parents=True, exist_ok=True)
            ctx.runner.sudo("mv", str(compose), str(aside))
    dump = layout.state_dir / "board" / "dump.json"
    metadata = json.loads(dump.read_text(encoding="utf-8"))
    ctx.journal.record("board_provisioned", True)
    outcome = ctx.board.build_new(ctx, metadata)
    before = {str(name): int(count) for name, count in metadata["table_counts"].items()}
    after = {str(name): int(count) for name, count in outcome["counts_after"].items()}
    for table in ("tasks", "issues", "sprints", "board_events", "products", "projects", "repositories",
                  "product_projects"):
        ctx.say(f"  {table}: before {before.get(table)}, after {after.get(table)}")
    problems = board.verify_counts(before, after)
    if problems:
        raise TransitionError("board counts after the translation differ: " + "; ".join(problems))
    return {**outcome, "counts_before": before}


# -- step 9 --------------------------------------------------------------------------------------


def plan_claude(ctx: Context) -> list[str]:
    root = ctx.layout.claude_projects
    lines = [f"  $ mv -T {root / old} {root / new}   (refuses a non-empty target)"
             for old, new in rewrite.claude_moves(ctx.layout.home, ctx.sprint or "<sprint>")
             if (root / old).exists() or ctx.journal.exists]
    lines.append(f"  copy {ctx.layout.claude_json} projects entries of the checkout and observer root to the new keys")
    lines.append(f"  rename the live [projects.\"…\"] trust keys of {ctx.layout.codex_config}")
    return lines


def apply_claude(ctx: Context) -> dict[str, Any]:
    layout = ctx.layout
    root = layout.claude_projects
    moved = list(ctx.journal.fact("claude_moved") or [])
    for old, new in rewrite.claude_moves(layout.home, ctx.sprint):
        if not (root / old).exists():
            continue
        if [old, new] not in moved:
            moved.append([old, new])
            ctx.journal.record("claude_moved", moved)
        rewrite.move_dir(root / old, root / new)
    pairs = [(old, new) for old, new in rewrite.live_trust_paths(layout.home)[:3]]
    trust = list(ctx.journal.fact("claude_trust_added") or [])
    added = rewrite.copy_claude_trust(layout.claude_json, [pairs[0], pairs[2]])
    if added:
        trust += added
        ctx.journal.record("claude_trust_added", trust)
    rewrite.backup(layout.codex_config, layout.home, layout.state_dir / "home")
    codex = rewrite.rename_codex_projects(layout.codex_config, rewrite.live_trust_paths(layout.home))
    return {"claude_moved": moved, "claude_trust_added": trust, "codex_keys": codex}


# -- step 10 -------------------------------------------------------------------------------------


def caddy_sites(path: Path) -> list[str]:
    """The site addresses of the rendered Caddyfile: every top-level block header but the global one."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    sites: list[str] = []
    for line in text.splitlines():
        if line and not line[0].isspace() and line.rstrip().endswith("{") and line.strip() != "{":
            sites += [site.strip() for site in line.rstrip()[:-1].split(",") if site.strip()]
    return sites


def _upgrade_argv(ctx: Context) -> list[str]:
    layout = ctx.layout
    return [
        "sudo", "-n", str(layout.python(NEW)), "-P", "-m", NEW.package, "upgrade",
        "--instance", str(layout.instance), "--no-pull",
        "--product-root", str(layout.product_root(NEW)), "--runtime-user", runtime_user(ctx),
    ]


def _web_front_argv(ctx: Context, verb: str, sites: list[str]) -> list[str]:
    argv = [_cli(ctx, NEW), "web-front", verb, "--instance", str(ctx.layout.instance),
            "--data-dir", str(ctx.layout.data_dir(NEW))]
    for site in sites if verb == "render" else []:
        argv += ["--site", site]
    return argv


def plan_upgrade(ctx: Context) -> list[str]:
    caddyfile = ctx.layout.live_data_dir() / "webfront" / "Caddyfile"
    sites = caddy_sites(caddyfile) or ["<sites of the Caddyfile>"]
    return [
        (f"  $ sudo -n rm -rf {ctx.layout.role_workspaces(OLD)}/{{{','.join(ROLES)}}} && git worktree prune   "
        "(the old role worktrees; upgrade creates the new ones)"),
        "  $ " + " ".join(_upgrade_argv(ctx)),
        "  $ " + " ".join(_web_front_argv(ctx, "render", sites)),
        "  $ " + " ".join(_web_front_argv(ctx, "check", [])),
        f"  $ sudo -n systemctl restart {NEW.unit_prefix}web-front.service",
        f"  $ {ctx.layout.cli(NEW)} memory reindex --instance {ctx.layout.instance}",
    ]


def apply_upgrade(ctx: Context) -> dict[str, Any]:
    layout, runner = ctx.layout, ctx.runner
    removed = []
    for role in ROLES:
        worktree = layout.role_workspaces(OLD) / role
        if worktree.exists():
            runner.sudo("rm", "-rf", "--", str(worktree))
            removed.append(role)
    if removed:
        runner.git(layout.product_root(NEW), "worktree", "prune")
    result = runner.run(_upgrade_argv(ctx), check=False, timeout=3600)
    tail = (result.stdout or result.stderr or "").strip().splitlines()[-15:]
    for line in tail:
        ctx.say(f"  upgrade: {line}")
    if result.returncode:
        raise TransitionError(f"upgrade exited {result.returncode}")
    sites = caddy_sites(layout.data_dir(NEW) / "webfront" / "Caddyfile")
    if sites:
        runner.run(_web_front_argv(ctx, "render", sites))
        runner.run(_web_front_argv(ctx, "check", []))
        runner.sudo("systemctl", "restart", f"{NEW.unit_prefix}web-front.service")
    runner.run([_cli(ctx, NEW), "memory", "reindex", "--instance", str(layout.instance)], timeout=3600)
    return {"removed_role_worktrees": removed, "web_front_sites": sites, "upgrade_tail": tail}


# -- step 11 -------------------------------------------------------------------------------------


def plan_old_units(ctx: Context) -> list[str]:
    units = list(ctx.journal.fact("units") or {}) or old_units(ctx)
    return [
        "  $ sudo -n rm -f " + " ".join(str(ctx.layout.units_dir / name) for name in units),
        "  $ sudo -n systemctl daemon-reload",
        f"  (the copies stay in {ctx.layout.state_dir / 'units'})",
    ]


def apply_old_units(ctx: Context) -> dict[str, Any]:
    present = [ctx.layout.units_dir / name for name in (ctx.journal.fact("units") or {})
               if (ctx.layout.units_dir / name).exists()]
    if present:
        ctx.runner.sudo("rm", "-f", "--", *map(str, present))
    ctx.runner.sudo("systemctl", "daemon-reload")
    # Against step 2's list: step 7 dropped the foreign units from instance.yaml, and they stay.
    left = [name for name in (ctx.journal.fact("units") or {}) if (ctx.layout.units_dir / name).exists()]
    if left:
        raise TransitionError("old unit files are still installed: " + ", ".join(left))
    return {"removed": [path.name for path in present]}


# -- step 12 -------------------------------------------------------------------------------------


def plan_report(ctx: Context) -> list[str]:
    cli = ctx.layout.cli(NEW)
    return [
        (f"  $ {cli} resume --instance {ctx.layout.instance} --data-dir {ctx.layout.data_dir(NEW)} "
        f"--owner {NEW.dispatcher_owner} --actor transition"),
        f"  $ {cli} doctor --instance {ctx.layout.instance}",
        f"  write {ctx.layout.state_dir / 'report.md'} (counts before/after, units, doctor, SHAs, secrets, dirs)",
        (f"  $ {cli} sprint comment --ref {ctx.sprint or '<sprint>'} --role po --actor transition "
        f"--body-file {ctx.layout.state_dir / 'report.md'}   (marked {DONE_MARKER})"),
    ]


def report_text(ctx: Context, doctor: tuple[int, list[str]], units: list[str],
                old_import: tuple[bool, str] = (False, "not checked")) -> str:
    steps = ctx.journal.data["steps"]
    board_facts = steps.get("board", {})
    before, after = board_facts.get("counts_before", {}), board_facts.get("counts_after", {})
    rows = "\n".join(
        f"| {table} | {before.get(table)} | {after.get(table)} |"
        for table in sorted(set(before) | set(after))
    )
    moved = "\n".join(f"- `{old}` -> `{new}`" for old, new in steps.get("claude", {}).get("claude_moved", []))
    secrets = ", ".join(steps.get("instance", {}).get("secrets_verified", [])) or "none"
    lines = [
        f"{DONE_MARKER} {OLD.package} -> {NEW.package}",
        "",
        "## What was done",
        "",
        (f"- Old checkout SHA `{ctx.journal.fact('pre_transition_sha')}`, new checkout SHA "
        f"`{steps.get('checkout', {}).get('new_sha')}` ({ctx.layout.product_root(NEW)})."),
        f"- Instance commit `{ctx.journal.fact('instance_commit')}` in {ctx.layout.instance}.",
        f"- Secrets re-sealed under `{NEW.value_kdf_info}` and verified: {secrets}.",
        f"- Board restored into `{NEW.db_name}` and translated: {board_facts.get('translation')}.",
        f"- Units now installed: {', '.join(units) or 'none'}. Old unit copies: {ctx.layout.state_dir / 'units'}.",
        "- Claude project directories moved:",
        moved or "- (none)",
        "",
        "| table | before | after |",
        "|---|---|---|",
        rows,
        "",
        "## How to verify",
        "",
        f"- `{NEW.package} doctor --instance {ctx.layout.instance}` exited {doctor[0]}:",
        "",
        "```",
        *doctor[1],
        "```",
        f"- `python -P -c 'import {OLD.package}'` in the new venv: "
        + ("ModuleNotFoundError, as required." if old_import[0] else f"**did not fail** ({old_import[1]})."),
        f"- `{NEW.package} task show --ref {OLD.project_id}-1915` opens; the web front answers behind the password.",
        f"- `ls {ctx.layout.units_dir} | grep {OLD.unit_prefix}` prints nothing.",
        f"- Rollback copies stay in {ctx.layout.state_dir} until the owner removes them after the DoD.",
        "",
    ]
    return "\n".join(lines)


def apply_report(ctx: Context) -> dict[str, Any]:
    layout, runner = ctx.layout, ctx.runner
    cli = _cli(ctx, NEW)
    runner.run([cli, "resume", "--instance", str(layout.instance), "--data-dir", str(layout.data_dir(NEW)),
                "--owner", NEW.dispatcher_owner, "--actor", "transition"])
    doctor = runner.run([cli, "doctor", "--instance", str(layout.instance)], check=False)
    doctor_tail = ((doctor.stdout or "") + (doctor.stderr or "")).strip().splitlines()[-30:]
    units = sorted(path.name for path in layout.units_dir.glob(f"{NEW.unit_prefix}*") if path.is_file())
    refused, said = old_package_import(ctx)
    report = layout.state_dir / "report.md"
    report.write_text(report_text(ctx, (doctor.returncode, doctor_tail), units, (refused, said)), encoding="utf-8")
    sprint = ctx.sprint or str(ctx.journal.fact("sprint") or "")
    if sprint:
        runner.run([cli, "sprint", "comment", "--ref", sprint, "--role", "po", "--actor", "transition",
                    "--request-id", f"transition-done-{sprint.replace(':', '-')}",
                    "--body-file", str(report), "--instance", str(layout.instance),
                    "--data-dir", str(layout.data_dir(NEW))])
    return {"report": str(report), "doctor_exit": doctor.returncode, "units": units, "sprint": sprint,
            "old_import_refused": refused}


STEPS: tuple[Step, ...] = (
    Step(1, "preconditions", "Preconditions", plan_preconditions, apply_preconditions, "old"),
    Step(2, "freeze", "Freeze the pipeline, stop and disable the old units", plan_freeze, apply_freeze, "old"),
    Step(3, "dump", "Dump the board, stop the old store container", plan_dump, apply_dump, "old"),
    Step(4, "move", "Move the checkout, data dir, /opt and tools", plan_moves, apply_moves, "old"),
    Step(5, "checkout", "Fast-forward the checkout, rebuild the venv (shell bootstrap)", plan_checkout,
         apply_checkout, "shell"),
    Step(6, "data-plane", "Data-plane fix-ups", plan_data_plane, apply_data_plane),
    Step(7, "instance", "Rewrite the instance (one commit) and the secret store", plan_instance, apply_instance),
    Step(8, "board", "Provision, restore and translate the board", plan_board, apply_board),
    Step(9, "claude", "Claude and Codex continuity", plan_claude, apply_claude),
    Step(10, "upgrade", "Materialize the new installation", plan_upgrade, apply_upgrade),
    Step(11, "old-units", "Remove the old unit files", plan_old_units, apply_old_units),
    Step(12, "report", "Resume and report", plan_report, apply_report),
)


def step_named(name: str) -> Step:
    for step in STEPS:
        if step.name == name or str(step.number) == name:
            return step
    raise TransitionError(f"no step {name!r}; one of {', '.join(step.name for step in STEPS)}")


__all__ = ["STEPS", "BoardOps", "Step", "caddy_sites", "old_units", "step_named"]
