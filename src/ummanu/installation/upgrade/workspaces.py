"""Client, registry, skill, workspace and worktree materialization."""

from __future__ import annotations

import os
import pwd
import stat
import tomllib
from pathlib import Path

from ummanu import role_skills, state_repo
from ummanu.head_registry import (
    HeadRegistryConfigError,
    canonical_heads,
    canonical_path,
    generated_pair,
    materialize_snapshot,
    record_source,
)
from ummanu.infra import git_worktree
from ummanu.installation.upgrade.checkout import (
    _git,
    fast_forward,
    upstream_target,
)
from ummanu.installation.upgrade.types import (
    GitError,
    StepResult,
    UpgradeContext,
    _data_dir,
)
from ummanu.memory.client_config import (
    CODEX_HOME_SEEDED_FILES,
    ClientConfigError,
    reconcile_clients,
    reconciled_codex_homes,
)
from ummanu.memory.pack import MemoryPackError, load_product_pack, materialize_product_pack
from ummanu.po import workspace as po_workspace
from ummanu.runtime import git_hooks, interactive_workspace
from ummanu.runtime.paths import env_value


def step_memory_clients(context: UpgradeContext) -> StepResult:
    """Materialize PO bridge entries without changing provider login state."""
    if not (context.product_root / ".venv").is_dir():
        return StepResult("memory-clients", "skipped", "no .venv in the product checkout")
    if context.runtime_home is None:
        return StepResult("memory-clients", "failed", "installation runtime home is unresolved")
    data_dir = context.report.data_dir
    if data_dir is None:
        return StepResult("memory-clients", "failed", "instance data directory is unresolved")
    try:
        result = reconcile_clients(
            context.product_root,
            context.runtime_home,
            data_dir,
            dry_run=context.dry_run,
        )
    except ClientConfigError as exc:
        return StepResult("memory-clients", "failed", str(exc))
    if not context.dry_run:
        data_homes = reconciled_codex_homes(data_dir)
        # A data-dir home may have been seeded here; the home itself and its login are left as found.
        seeded = (home / name for home in data_homes for name in CODEX_HOME_SEEDED_FILES)
        user_codex = context.runtime_home / ".codex" / "config.toml"
        claude = context.runtime_home / ".claude.json"
        try:
            _set_runtime_directory_owner(user_codex.parent, context.runtime_user)
            for path in (*seeded, user_codex, claude):
                _set_runtime_owner(path, context.runtime_user)
        except GitError as exc:
            return StepResult("memory-clients", "failed", str(exc))
    if not result.changed:
        return StepResult("memory-clients", "unchanged", "Claude and Codex PO bridge entries current")
    action = "would reconcile" if context.dry_run else "reconciled"
    return StepResult("memory-clients", "changed", f"{action} {result.changed} client config(s)")


def step_codex_home(context: UpgradeContext) -> StepResult:
    """Seed the non-secret Codex runtime files into `<data_dir>/codex-home`, the one CODEX_HOME the
    installation manages; the PO's `codex login` there is the only other step it needs."""
    if not (context.product_root / "packaging" / "codex-home").is_dir():
        return StepResult("codex-home", "skipped", "no packaging/codex-home in the product checkout")
    data_dir = _data_dir(context)
    if data_dir is None:
        return StepResult("codex-home", "failed", "instance data directory is unresolved")
    if context.dry_run:
        return StepResult("codex-home", "skipped", "--dry-run does not seed CODEX_HOME")
    # installation imports this module; the seeding lives there with install's own call of it.
    from ummanu.installation.models import InstallError
    from ummanu.installation.provision import provision_codex_home

    try:
        seeded = provision_codex_home(
            context.product_root,
            context.runtime_user,
            data_dir=data_dir,
        )
    except InstallError as exc:
        return StepResult("codex-home", "failed", str(exc))
    if not seeded:
        return StepResult("codex-home", "unchanged", "CODEX_HOME files current")
    return StepResult("codex-home", "changed", f"seeded {seeded} CODEX_HOME file(s)")


def step_git_hooks(context: UpgradeContext) -> StepResult:
    """Point the runtime user's global `core.hooksPath` at the hooks this product checkout ships."""
    if context.runtime_home is None:
        return StepResult("git-hooks", "failed", "installation runtime home is unresolved")
    target = git_hooks.hooks_dir(context.product_root)
    try:
        checkouts = [binding["repo"] for binding in context.report.bindings if isinstance(binding.get("repo"), str)]
        changed = git_hooks.install_hooks_path(
            context.runtime_home, context.product_root, checkouts, dry_run=context.dry_run
        )
        if changed and not context.dry_run:
            _set_runtime_owner(context.runtime_home / ".gitconfig", context.runtime_user)
    except (git_hooks.GitHooksError, GitError) as exc:
        return StepResult("git-hooks", "failed", str(exc))
    if not changed:
        return StepResult("git-hooks", "unchanged", f"global core.hooksPath is {target}")
    action = "would set" if context.dry_run else "set"
    return StepResult("git-hooks", "changed", f"{action} global core.hooksPath to {target}")


def step_po_workspace(context: UpgradeContext) -> StepResult:
    """Materialize the PO head's working directory; its notes file is never rewritten.

    Ownership is handed over by `step_po_workspace_owner`, after the skills are delivered into it.
    """
    if not (context.product_root / ".venv").is_dir():
        return StepResult("po-workspace", "skipped", "no .venv in the product checkout")
    data_dir = _data_dir(context)
    if data_dir is None:
        return StepResult("po-workspace", "failed", "instance data directory is unresolved")
    try:
        result = po_workspace.materialize(context.product_root, data_dir, dry_run=context.dry_run)
    except po_workspace.WorkspaceError as exc:
        return StepResult("po-workspace", "failed", str(exc))
    if not result.changed:
        return StepResult("po-workspace", "unchanged", f"{result.path} current")
    action = "would write" if context.dry_run else "wrote"
    return StepResult("po-workspace", "changed", f"{action} {', '.join(result.changed)} in {result.path}")


def step_po_workspace_owner(context: UpgradeContext) -> StepResult:
    """Hand the whole PO workspace to the runtime user once role-skills has delivered into it.

    Runs on every upgrade rather than on change: a root invoker creates the skill roots and their
    copies after `po-workspace`, and a tree left root-owned by an earlier run is repaired here.
    """
    data_dir = _data_dir(context)
    if data_dir is None:
        return StepResult("po-workspace-owner", "skipped", "instance data directory is unresolved")
    workspace = po_workspace.workspace_dir(data_dir)
    if context.dry_run or not workspace.is_dir():
        return StepResult("po-workspace-owner", "skipped", f"no {workspace} to hand over")
    if not context.runtime_user or os.geteuid() != 0:
        return StepResult(
            "po-workspace-owner", "skipped", "not a root invoker; files already belong to the caller"
        )
    try:
        _set_runtime_owner(workspace, context.runtime_user)
    except GitError as exc:
        return StepResult("po-workspace-owner", "failed", str(exc))
    return StepResult("po-workspace-owner", "unchanged", f"{workspace} owned by {context.runtime_user}")


def step_interactive_workspace(context: UpgradeContext) -> StepResult:
    """Materialize the interactive head's working directory and hand it to the runtime user.

    `AGENTS.md` is the product's shared part followed by the live root's `persona/AGENTS.md`; recover
    runs this same step against the live root it extracted. Nothing else receives the persona.
    """
    source = interactive_workspace.shared_source(context.product_root)
    if not source.exists() and not source.is_symlink():
        return StepResult(
            "interactive-workspace",
            "skipped",
            f"no {interactive_workspace.SHARED_SOURCE_RELATIVE} in the product checkout",
        )
    data_dir = _data_dir(context)
    if data_dir is None:
        return StepResult("interactive-workspace", "failed", "instance data directory is unresolved")
    try:
        result = interactive_workspace.materialize(
            context.product_root, context.instance_path, data_dir, dry_run=context.dry_run
        )
    except interactive_workspace.WorkspaceError as exc:
        return StepResult("interactive-workspace", "failed", str(exc))
    if not context.dry_run:
        try:
            _set_runtime_owner(result.path, context.runtime_user)
        except GitError as exc:
            return StepResult("interactive-workspace", "failed", str(exc))
    sources = f"shared {result.shared}, personal {result.personal or 'absent'}"
    if not result.changed:
        return StepResult("interactive-workspace", "unchanged", f"{result.path} current ({sources})")
    action = "would write" if context.dry_run else "wrote"
    return StepResult(
        "interactive-workspace",
        "changed",
        f"{action} {', '.join(result.changed)} in {result.path} ({sources})",
    )


def _role_skills_manifest(context: UpgradeContext) -> Path:
    """The skill registry of the checkout being installed, which is not always the running one."""
    return role_skills.product_manifest_path(context.product_root)


def step_registries(context: UpgradeContext) -> StepResult:
    """Read every registry this upgrade materializes from, before anything is written.

    The steps that follow write in order — head snapshot, role worktrees, skills and entry points,
    host — and each reads operator-written configuration that can be malformed, so finding that out
    at the third write leaves a host half-moved. Parsing is not enough for the skill registry: a
    manifest whose declared ``SKILL.md`` is absent, whose target roots overlap, or whose entry point
    collides parses cleanly and is refused by `sync` after the head snapshot has been written, so the
    whole plan is decided here against the same manifests and home the later steps use.
    """
    manifest = _role_skills_manifest(context)
    try:
        registry = role_skills.load_registry(context.instance_path, product_manifest=manifest)
        data_dir = role_skills.resolve_data_dir(registry, context.instance_path, _data_dir(context))
        problems = role_skills.unmaterializable(registry, context.runtime_home, data_dir=data_dir)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        return StepResult("registries", "failed", f"skill registry: {exc}")
    if problems:
        return StepResult(
            "registries",
            "failed",
            f"skill registry: {problems[0]}",
        )
    try:
        canonical, _ = canonical_path(context.product_root, context.instance_path)
        canonical_heads(context.product_root, context.instance_path)
    except HeadRegistryConfigError as exc:
        return StepResult("registries", "failed", str(exc))
    try:
        context.memory_pack = load_product_pack(context.product_root)
    except MemoryPackError as exc:
        return StepResult("registries", "failed", f"memory pack: {exc}")
    sources = ", ".join(str(source.path) for source in registry.sources)
    return StepResult("registries", "unchanged", f"{sources}, {canonical}, and memory pack are readable")


def step_memory_pack(context: UpgradeContext) -> StepResult:
    """Materialize the shipped pack only after `registries` validated its source."""
    pack = context.memory_pack
    try:
        if pack is None:
            pack = load_product_pack(context.product_root)
        data_dir = context.report.data_dir
        if data_dir is None:
            return StepResult("memory-pack", "failed", "instance has no resolved data directory")
        result = materialize_product_pack(
            pack,
            instance_dir=context.instance_path,
            data_dir=data_dir,
            dry_run=context.dry_run,
            runtime_handoff=lambda path: _set_runtime_owner(path, context.runtime_user),
            runtime_export_check=lambda memory_dir: _assert_memory_export_readable(
                memory_dir, context.runtime_user
            ),
        )
    except MemoryPackError as exc:
        return StepResult("memory-pack", "failed", str(exc))
    context.memory_pack_changed = result.changed
    context.memory_pack_digest = pack.digest
    if not result.changed:
        return StepResult("memory-pack", "unchanged", "installed digest matches product pack")
    verb = "would reconcile" if context.dry_run else "reconciled"
    return StepResult(
        "memory-pack",
        "changed",
        f"{verb} {result.added} added, {result.updated} updated, {result.deleted} deleted, {result.retained} retained",
    )


def step_role_skills(context: UpgradeContext) -> StepResult:
    """Materialize the product skills of the checkout being installed, plus this installation's."""
    manifest = _role_skills_manifest(context)
    try:
        before = role_skills.audit(
            instance_path=context.instance_path,
            product_manifest=manifest,
            home=context.runtime_home,
            data_dir=_data_dir(context),
        )
    except (OSError, ValueError) as exc:
        return StepResult("role-skills", "failed", str(exc))
    if before["ok"]:
        if not context.dry_run:
            try:
                _hand_role_skill_dirs_to_runtime_user(context, manifest)
            except (GitError, OSError, ValueError) as exc:
                return StepResult("role-skills", "failed", str(exc))
        return StepResult("role-skills", "unchanged", f"{len(before['targets'])} targets in sync")
    pending = len(before["missing"]) + len(before["drift"]) + len(before["entry_points"])
    pending += len(before.get("retired", []))
    if before["config_errors"] or before["source_missing"]:
        return StepResult(
            "role-skills", "failed", "manifest is not usable: overlapping roots or a missing source skill"
        )
    if context.dry_run:
        return StepResult("role-skills", "changed", f"would sync {pending} skill copies")
    try:
        after = role_skills.sync(
            instance_path=context.instance_path,
            product_manifest=manifest,
            home=context.runtime_home,
            data_dir=_data_dir(context),
        )
    except (OSError, ValueError) as exc:
        return StepResult("role-skills", "failed", str(exc))
    if not after["after"]["ok"]:
        return StepResult("role-skills", "failed", "sync ran but the audit is still red")
    try:
        _hand_role_skill_dirs_to_runtime_user(context, manifest)
    except (GitError, OSError, ValueError) as exc:
        return StepResult("role-skills", "failed", str(exc))
    return StepResult("role-skills", "changed", f"synced {pending} skill copies")


def _hand_role_skill_dirs_to_runtime_user(context: UpgradeContext, manifest: Path) -> None:
    """Give the runtime user every skill root, and the directories above it a root sync created.

    Under sudo, `sync` creates `~/.claude/skills`, `~/.hermes/skills`, the Codex runtime home and the
    entry points' directory as root. Each root is handed over whole; its ancestors one by one, up to
    the home or data directory that contains them and never that directory itself.
    """
    if not context.runtime_user or os.geteuid() != 0:
        return
    registry = role_skills.load_registry(context.instance_path, product_manifest=manifest)
    data_dir = role_skills.resolve_data_dir(registry, context.instance_path, _data_dir(context))
    home = context.runtime_home or Path.home()
    bounds = tuple(Path(bound) for bound in (home, data_dir) if bound is not None)
    roots = {
        root for root in role_skills.target_roots(registry, context.runtime_home, data_dir).values() if root
    }
    for root in sorted(roots):
        _set_runtime_owner(root, context.runtime_user)
        _set_runtime_ancestors_owner(root, bounds, context.runtime_user)
    for command in role_skills.iter_expected_commands(registry, context.runtime_home):
        _set_runtime_ancestors_owner(command.dest, bounds, context.runtime_user)


def _set_runtime_ancestors_owner(path: Path, bounds: tuple[Path, ...], runtime_user: str) -> None:
    """Assign the real directories between `path` and the innermost bound containing it."""
    bound = max(
        (bound for bound in bounds if bound in path.parents), key=lambda b: len(b.parts), default=None
    )
    if bound is None:
        return
    for parent in path.parents:
        if parent == bound:
            return
        if parent.is_symlink() or not parent.is_dir():
            continue
        _set_runtime_directory_owner(parent, runtime_user)


def step_head_registry(context: UpgradeContext) -> StepResult:
    """Keep the installation snapshot derived from whichever registry is this host's canon.

    The installation's own ``heads/heads.toml`` when it owns one, else the product's portable
    default. The pin next to the snapshot records which of the two won, plus the checkout and
    revision, and the live tick validates the pin against the snapshot. Both are generated state in
    ``<data>/heads/``: written here, never committed or pushed (docs/RECOVERY.md).
    """
    try:
        pair = generated_pair(context.instance_path, _data_dir(context))
        canonical, _ = canonical_path(context.product_root, context.instance_path)
        changed = materialize_snapshot(
            context.instance_path,
            context.product_root,
            dry_run=context.dry_run,
            data_dir=_data_dir(context),
        )
        repinned = record_source(
            context.instance_path,
            context.product_root,
            dry_run=context.dry_run,
            data_dir=_data_dir(context),
        )
        if not context.dry_run:
            # A root-run upgrade may have created the directory: the installation account reads it.
            _set_runtime_owner(pair.snapshot.parent, context.runtime_user)
    except (HeadRegistryConfigError, GitError) as exc:
        return StepResult("head-registry", "failed", str(exc))
    target = pair.snapshot
    # The snapshot, not the pin: the pin records which canon won and where the checkout is, while
    # `heads.yaml` is the file a running process actually read and cached.
    context.head_registry_changed = bool(changed)
    if not changed and not repinned:
        return StepResult("head-registry", "unchanged", f"{target} matches {canonical}")
    verb = "would regenerate" if context.dry_run else "regenerated"
    what = target if changed else pair.source
    if changed and repinned:
        what = f"{target} and {pair.source}"
    return StepResult("head-registry", "changed", f"{verb} {what}")


def step_instance_packing(context: UpgradeContext) -> StepResult:
    """Keep the private instance repo's packing controls bounded and local."""
    from ummanu.checkpoint import live_root_is_work_tree

    if not live_root_is_work_tree(context.instance_path):
        # An exporter-mode live root has no repository to pack; the snapshot repository is bare.
        return StepResult("instance-packing", "skipped", "the live root is not a Git work tree")
    try:
        drifted = state_repo.configure_packing_controls(context.instance_path, dry_run=context.dry_run)
    except state_repo.StateRepoError as exc:
        return StepResult("instance-packing", "failed", str(exc))
    if not drifted:
        return StepResult("instance-packing", "unchanged", "local Git packing controls match")
    action = "would set" if context.dry_run else "set"
    return StepResult(
        "instance-packing",
        "changed",
        f"{action} local Git packing controls: {', '.join(drifted)}",
    )


class AgentSpecsError(RuntimeError):
    """The product's declaration of its background agents' specs is broken."""


# The product declares where its background agents' specs live in its own `pyproject.toml`:
# the agents are a package on top of `ummanu`, so `ummanu` reads their location from the
# product's manifest instead of naming the package (sprint:1455, secretary-1689).
AGENT_SPECS_KEY = "agent-specs"


def agents_root(product_root: Path) -> Path | None:
    """The directory of ``<agent>/automation.toml`` specs this product declares, or None.

    A product tree without a manifest, or whose manifest declares no specs, ships no agents —
    as a tree without the directory always did. A manifest that cannot be parsed, or declares a
    path outside the product, is a broken product rather than an empty one.
    """
    manifest = product_root / "pyproject.toml"
    try:
        raw = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise AgentSpecsError(
            f"{manifest}: product manifest is unreadable: {exc.__class__.__name__}"
        ) from None
    tool = raw.get("tool")
    table = tool.get("ummanu") if isinstance(tool, dict) else None
    declared = table.get(AGENT_SPECS_KEY) if isinstance(table, dict) else None
    if declared is None:
        return None
    relative = Path(declared) if isinstance(declared, str) else None
    if relative is None or not declared or relative.is_absolute() or ".." in relative.parts:
        raise AgentSpecsError(
            f"{manifest}: [tool.ummanu] {AGENT_SPECS_KEY} must be a path inside the product"
        )
    return product_root / relative


def workspaces_root(home: Path | str | None = None) -> Path:
    """Where role workspaces live: the configured root, else under the named home.

    ``home`` is the installation owner's, which is not the invoking process's when a repair runs
    as root or against another account's installation. Materializing root's workspace paths for
    units the owner then runs is how a workspace ends up somewhere nothing materialized.
    """
    configured = env_value("UMMANU_WORKSPACES_ROOT", nonempty=True)
    if configured:
        return Path(configured)
    return Path(home if home is not None else Path.home()) / "orca" / "workspaces"


def desired_role_worktrees(product_root: Path, home: Path | None = None) -> list[Path]:
    """Every derived role worktree shipped by this product, present or absent."""
    root = workspaces_root(home) / "ummanu"
    agents = agents_root(product_root)
    if agents is None:
        return []
    try:
        names = sorted(entry.name for entry in agents.iterdir() if (entry / "automation.toml").is_file())
    except OSError:
        return []
    return [root / name for name in names]


def _set_runtime_owner(path: Path, runtime_user: str | None) -> None:
    """Repair root-created runtime files without traversing links or hardlinks."""
    if not runtime_user or os.geteuid() != 0 or not path.exists():
        return
    try:
        account = pwd.getpwnam(runtime_user)
    except KeyError:
        raise GitError(f"runtime user {runtime_user!r} does not exist") from None

    def assign(candidate: Path) -> None:
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode) or (stat.S_ISREG(info.st_mode) and info.st_nlink > 1):
            return
        os.chown(candidate, account.pw_uid, account.pw_gid, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode):
            return
        with os.scandir(candidate) as children:
            for child in children:
                assign(Path(child.path))

    try:
        assign(path)
    except OSError as exc:
        raise GitError(f"could not assign {path} to runtime user {runtime_user}: {exc}") from None


def _assert_memory_export_readable(memory_dir: Path, runtime_user: str | None) -> None:
    """Require the daemon account to own readable export files before activation."""
    # Root publication must prove runtime-account ownership.
    if not runtime_user or os.geteuid() != 0:
        return
    try:
        account = pwd.getpwnam(runtime_user)
    except KeyError:
        raise GitError(f"runtime user {runtime_user!r} does not exist") from None
    for name in ("export.ndjson", "export.json", "manifest.json"):
        path = memory_dir / name
        try:
            info = path.lstat()
        except OSError as exc:
            raise GitError(f"could not inspect memory export {path}: {exc}") from None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise GitError(f"memory export is not a regular file: {path}")
        if info.st_uid != account.pw_uid or not info.st_mode & stat.S_IRUSR:
            raise GitError(f"memory export is not readable by runtime user {runtime_user}: {path}")


def _set_runtime_directory_owner(path: Path, runtime_user: str | None) -> None:
    """Make a created workspace ancestor traversable and writable without walking siblings."""
    if not runtime_user or os.geteuid() != 0 or not path.exists():
        return
    try:
        account = pwd.getpwnam(runtime_user)
    except KeyError:
        raise GitError(f"runtime user {runtime_user!r} does not exist") from None
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise GitError(f"workspace ancestor is not a real directory: {path}")
        os.chown(path, account.pw_uid, account.pw_gid, follow_symlinks=False)
    except OSError as exc:
        raise GitError(f"could not assign {path} to runtime user {runtime_user}: {exc}") from None


def _workspace_owner_dirs(worktree: Path) -> tuple[Path, ...]:
    """The exact parents this materializer can create for a role worktree."""
    ummanu_root = worktree.parent
    workspace_root = ummanu_root.parent
    roots = [workspace_root, ummanu_root]
    if workspace_root.name == "workspaces" and workspace_root.parent.name == "orca":
        roots.insert(0, workspace_root.parent)
    return tuple(roots)


def _registered_worktree(product_root: Path, worktree: Path) -> bool:
    """Whether the product's Git already has a linked worktree registered at `worktree`."""
    listing = _git(product_root, ["worktree", "list", "--porcelain"])
    wanted = worktree.resolve(strict=False)
    return any(
        Path(row["worktree"]).resolve(strict=False) == wanted
        for row in git_worktree.parse_list(listing)
        if "worktree" in row
    )


def _worktree_git_dir(worktree: Path) -> Path | None:
    """Return the linked-worktree administrative directory named by its .git file."""
    try:
        line = (worktree / ".git").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    if not line.startswith("gitdir: "):
        return None
    return Path(line.removeprefix("gitdir: ")).expanduser().resolve()


def step_worktrees(context: UpgradeContext) -> StepResult:
    try:
        worktrees = desired_role_worktrees(context.product_root, context.runtime_home)
    except AgentSpecsError as exc:
        return StepResult("role-worktrees", "failed", str(exc))
    if not worktrees:
        return StepResult("role-worktrees", "skipped", "the product ships no role worktrees")
    created: list[str] = []
    moved: list[str] = []
    stuck: list[str] = []
    # Sudo may create worktree metadata the runtime user must own.
    try:
        _set_runtime_owner(worktrees[0].parent, context.runtime_user)
        for parent in _workspace_owner_dirs(worktrees[0]):
            _set_runtime_directory_owner(parent, context.runtime_user)
    except GitError as exc:
        return StepResult("role-worktrees", "failed", str(exc))
    for worktree in worktrees:
        try:
            _set_runtime_owner(worktree, context.runtime_user)
            admin = _worktree_git_dir(worktree)
            if admin is not None:
                _set_runtime_owner(admin.parent, context.runtime_user)
                _set_runtime_owner(admin, context.runtime_user)
        except GitError as exc:
            stuck.append(f"{worktree.name}: {exc}")
            continue
        if not (worktree / ".git").exists():
            if worktree.exists() and any(worktree.iterdir()):
                stuck.append(f"{worktree.name}: target exists and is not a managed worktree")
                continue
            if context.dry_run:
                created.append(worktree.name)
                continue
            try:
                worktree.parent.mkdir(parents=True, exist_ok=True)
                for parent in _workspace_owner_dirs(worktree):
                    _set_runtime_directory_owner(parent, context.runtime_user)
                # A registration whose directory is gone (a lost workspace root, a host rebuilt
                # from a backup) makes a plain add refuse; one `--force` replaces exactly that
                # registration. A locked one still refuses, and git's `fatal:` line says why.
                force = ["--force"] if _registered_worktree(context.product_root, worktree) else []
                _git(
                    context.product_root,
                    ["worktree", "add", *force, "--detach", str(worktree), "HEAD"],
                    timeout=300,
                )
                _set_runtime_owner(worktree, context.runtime_user)
                admin = _worktree_git_dir(worktree)
                if admin is None:
                    raise GitError(f"could not locate Git administration for {worktree}")
                _set_runtime_owner(worktree.parent, context.runtime_user)
                _set_runtime_owner(admin.parent, context.runtime_user)
                _set_runtime_owner(admin, context.runtime_user)
            except (GitError, OSError) as exc:
                stuck.append(f"{worktree.name}: {exc}")
                continue
            created.append(worktree.name)
            continue
        try:
            if context.dry_run:
                head, target = upstream_target(worktree, context.base_branch)
                if head != target:
                    moved.append(worktree.name)
                continue
            before, after = fast_forward(worktree, context.base_branch)
        except GitError as exc:
            stuck.append(f"{worktree.name}: {exc}")
            continue
        if before != after:
            moved.append(worktree.name)
    if stuck:
        return StepResult("role-worktrees", "failed", "; ".join(stuck))
    if not moved and not created:
        return StepResult("role-worktrees", "unchanged", f"{len(worktrees)} worktrees current")
    details = []
    if created:
        details.append("created " + ", ".join(created))
    if moved:
        details.append("updated " + ", ".join(moved))
    return StepResult("role-worktrees", "changed", "; ".join(details))
