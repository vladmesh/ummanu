#!/usr/bin/env python3
"""Audit and sync role-owned skills into shell-owned skill directories.

Two registries, layered. The product manifest is the portable one, read from whichever checkout
the caller names, because an upgrade can materialize a host from a checkout other than the one
running this module. An installation may own a second manifest at
``<instance>/skills/manifest.toml`` naming skills that belong to this host alone. A skill is
always read from the tree beside the manifest that declared it, so the two never have to agree
about where sources live.

A skill may also ship one command: an executable ``<skill>.sh`` beside its ``SKILL.md``, linked
into the operator's ``bin`` directory under the skill's own name.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ummanu.config import DataDirError, instance_data_dir
from ummanu.kernel.identifiers import is_machine_id, machine_id_refusal
from ummanu.po.workspace import workspace_dir
from ummanu.runtime.paths import (
    MissingDefaultInstance,
    add_instance_argument,
    configured_product_root,
    default_instance_path,
    resolve_instance_argument,
)

ROOT = Path(__file__).resolve().parents[2]
# Manifest shipped by this checkout; hosts use their configured product checkout instead.
MANIFEST = ROOT / "skills" / "manifest.toml"
# Where an installation keeps its own manifest, relative to the instance directory.
INSTANCE_MANIFEST_RELATIVE = Path("skills") / "manifest.toml"
# Product manifest path, separate from the instance overlay path.
PRODUCT_MANIFEST_RELATIVE = Path("skills") / "manifest.toml"
# Registry override selects both its manifest and adjacent roles tree.
MANIFEST_ENV = "UMMANU_ROLE_SKILLS_MANIFEST"
INSTANCE_ENV = "UMMANU_INSTANCE"
# Default command destination; callers may use an isolated destination.
BIN_DIR_ENV = "UMMANU_BIN_DIR"

PRODUCT_ORIGIN = "product"
INSTANCE_ORIGIN = "instance"

# Role and skill names are one path component to contain every root join.
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
# A target root inside the installation's PO workspace, which lives in the data directory.
PO_WORKSPACE_ROOT = "@po"
# Written into every copy sync delivers, so a later sync can tell its own copies from anyone else's.
OWNERSHIP_MARKER = ".ummanu-role-skill"
OWNERSHIP_MARKER_TEXT = (
    "Delivered by `ummanu role-skills sync`, which removes this directory once no manifest\n"
    "declares the skill for this root.\n"
)
# A SKILL.md a manifest's repository has shipped, as `git log --raw` names it.
SHIPPED_SKILL = re.compile(r"(?:^|/)roles/[^/]+/([^/]+)/SKILL\.md$")


class RegistryError(ValueError):
    """A manifest that cannot be read or does not have the shape a registry needs."""


def _absolute(value: Path | str) -> Path:
    """A path that means the same thing from anywhere.

    A symlink resolves its own text against the directory it sits in, not against the working
    directory of whoever ran the sync, so a relative ``--instance`` would materialize a command
    pointing at a path below ``bin``. Symlinks in the path itself are left alone.
    """
    return Path(os.path.abspath(os.path.expanduser(str(value))))


def manifest_path(product_manifest: Path | str | None = None) -> Path:
    """The product manifest: the one the caller named, the one under test, or the configured one.

    A named manifest wins over the environment because the caller that names one is installing a
    particular checkout. With neither, the answer is the configured product checkout —
    ``UMMANU_REPO``, else ``~/ummanu`` — and never the checkout containing this file, since a
    candidate checkout is a normal place to run ``role-skills`` from.
    """
    if product_manifest is not None:
        return _absolute(product_manifest)
    raw = os.environ.get(MANIFEST_ENV)
    if raw:
        return _absolute(raw)
    return product_manifest_path(configured_product_root())


def product_manifest_path(product_root: Path | str) -> Path:
    """The manifest of a named product checkout, which is not always the running one."""
    return _absolute(product_root) / PRODUCT_MANIFEST_RELATIVE


def instance_dir(value: Path | str) -> Path:
    """The private repo root. An instance is named by its directory or by its instance.yaml."""
    path = _absolute(value)
    return path.parent if path.suffix in (".yaml", ".yml") else path


def configured_instance_path() -> Path:
    return _absolute(os.environ.get(INSTANCE_ENV) or default_instance_path())


def _expand_home(value: Path | str, home: Path | str | None) -> Path:
    """A manifest path with ``~`` read as the installation's home, not the caller's.

    Which home that is belongs to the installation being materialized: an upgrade repairing another
    account's installation would otherwise deliver every skill under the invoking process's home
    while the units it renders name the owner's. With no home named, the process's own is right.
    """
    if home is None:
        return Path(os.path.expanduser(str(value)))
    text = str(value)
    if text == "~":
        return Path(home)
    if text.startswith("~/"):
        return Path(home) / text[2:]
    # Never expand another account's home directory.
    return Path(os.path.expanduser(text))


def _is_po_workspace_root(value: str) -> bool:
    return value == PO_WORKSPACE_ROOT or value.startswith(f"{PO_WORKSPACE_ROOT}/")


def resolve_data_dir(
    registry: SkillRegistry,
    instance_path: Path | str | None,
    data_dir: Path | str | None = None,
    *,
    role: str | None = None,
) -> Path | None:
    """The data directory `@po/` roots are read against, or None when there is no installation.

    A named data directory wins. Otherwise ``instance.yaml`` is read only when a target this
    question concerns (every target, or the ones carrying ``role``) has a `@po/` root, so a
    question about any other skill never depends on the instance file. An instance directory with
    no ``instance.yaml`` is a checkout with nothing installed, and its `@po/` targets are left out
    rather than guessed. An ``instance.yaml`` that cannot be read refuses.
    """
    if data_dir is not None:
        return _absolute(data_dir)
    if not any(
        _is_po_workspace_root(target["root"]) and (role is None or role in target["roles"])
        for target, _ in registry.targets.values()
    ):
        return None
    base = instance_path if instance_path is not None else configured_instance_path()
    path = _absolute(base)
    instance_file = path if path.suffix in (".yaml", ".yml") else path / "instance.yaml"
    if not instance_file.is_file():
        return None
    try:
        return instance_data_dir(instance_file)
    except DataDirError as exc:
        raise RegistryError(f"{instance_file}: data directory cannot be resolved: {exc}") from None


def _expand_root(value: str, home: Path | str | None, data_dir: Path | None) -> Path | None:
    """A target root as a path, or None for a PO workspace root with no data directory to name."""
    if not _is_po_workspace_root(value):
        return _expand_home(value, home)
    if data_dir is None:
        return None
    workspace = workspace_dir(data_dir)
    root = _absolute(workspace / value[len(PO_WORKSPACE_ROOT) :].lstrip("/"))
    if not _within(workspace, root):
        raise RegistryError(f"target root {value!r} resolves to {root}, outside {workspace}")
    return root


def bin_dir(home: Path | str | None = None) -> Path:
    """The directory on ``PATH`` that a skill's command is linked into."""
    raw = os.environ.get(BIN_DIR_ENV)
    if raw:
        return _absolute(raw)
    return _absolute(Path(home) / "bin") if home is not None else Path.home() / "bin"


def instance_manifest_path(instance_path: Path | str | None = None) -> Path:
    """Where this installation's own manifest would be, whether or not it exists."""
    base = instance_path if instance_path is not None else configured_instance_path()
    return instance_dir(base) / INSTANCE_MANIFEST_RELATIVE


@dataclass(frozen=True)
class ManifestSource:
    """One manifest file and the ``roles/`` tree that belongs to it."""

    origin: str
    path: Path

    @property
    def roles_root(self) -> Path:
        return self.path.parent / "roles"


@dataclass(frozen=True)
class ExpectedSkill:
    target: str
    shell: str
    role: str
    skill: str
    source: Path
    dest: Path
    origin: str = PRODUCT_ORIGIN
    manifest: Path = MANIFEST


@dataclass(frozen=True)
class ExpectedCommand:
    """The one executable a skill ships, and the link that makes it runnable by name."""

    name: str
    role: str
    skill: str
    source: Path
    dest: Path
    origin: str
    manifest: Path


@dataclass(frozen=True)
class SkillRegistry:
    """The product manifest with an optional instance manifest layered over it."""

    sources: tuple[ManifestSource, ...]
    # role -> ordered (skill name, declaring source)
    roles: dict[str, list[tuple[str, ManifestSource]]] = field(default_factory=dict)
    # target name -> (target table, declaring source)
    targets: dict[str, tuple[dict[str, Any], ManifestSource]] = field(default_factory=dict)
    # (role, skill) -> the role whose tree the skill is read from, for `<role>/<skill>` entries
    references: dict[tuple[str, str], str] = field(default_factory=dict)

    def source_role(self, role: str, skill: str) -> str:
        return self.references.get((role, skill), role)

    def describe_sources(self) -> list[dict[str, str]]:
        return [{"origin": source.origin, "path": str(source.path)} for source in self.sources]

    @property
    def owned_roots(self) -> tuple[Path, ...]:
        """Every ``roles/`` tree this registry reads skills from."""
        return tuple(source.roles_root for source in self.sources)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest(path: Path | None = None) -> dict[str, Any]:
    """One manifest file, parsed. Defaults to the product manifest."""
    target = path or manifest_path()
    try:
        return tomllib.loads(target.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise RegistryError(f"{target}: not valid TOML: {exc}") from None
    except (OSError, UnicodeError) as exc:
        raise RegistryError(f"{target}: could not be read: {exc}") from None


def manifest_sources(
    instance_path: Path | str | None = None,
    *,
    product_manifest: Path | str | None = None,
) -> list[ManifestSource]:
    """The manifests that make up this installation's registry, product first.

    A missing instance manifest is not an error: a portable installation has nothing to layer.
    Something at that path that is not a readable file is the opposite case and refuses.
    """
    sources = [ManifestSource(PRODUCT_ORIGIN, manifest_path(product_manifest))]
    overlay = instance_manifest_path(instance_path)
    if overlay.is_file():
        sources.append(ManifestSource(INSTANCE_ORIGIN, overlay))
    elif overlay.is_symlink() or overlay.exists():
        raise RegistryError(f"{overlay}: exists but is not a readable manifest file")
    return sources


def _table(data: dict[str, Any], key: str, source: ManifestSource) -> dict[str, Any]:
    value = data.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RegistryError(f"{source.path}: [{key}] must be a table")
    return value


def _string(table: dict[str, Any], key: str, where: str, source: ManifestSource) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value:
        raise RegistryError(f"{source.path}: {where}.{key} must be a non-empty string")
    return value


def _string_list(table: dict[str, Any], key: str, where: str, source: ManifestSource) -> list[str]:
    value = table.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise RegistryError(f"{source.path}: {where}.{key} must be a list of non-empty strings")
    return value


def _identifier(value: str, where: str, source: ManifestSource) -> str:
    """One path component, or a refusal naming the manifest that has to be edited."""
    if not IDENTIFIER.fullmatch(value):
        raise RegistryError(
            f"{source.path}: {where} must be a single directory name matching "
            f"{IDENTIFIER.pattern}, got {value!r}"
        )
    return value


def _role_name(value: str, where: str, source: ManifestSource) -> str:
    """A role name in the machine identifier grammar, or a refusal naming the manifest to edit."""
    if not is_machine_id(value):
        raise RegistryError(f"{source.path}: {machine_id_refusal(where, value)}")
    return value


def _within(root: Path, path: Path) -> bool:
    root_abs = _absolute(root)
    path_abs = _absolute(path)
    return path_abs == root_abs or root_abs in path_abs.parents


def load_registry(
    instance_path: Path | str | None = None,
    *,
    product_manifest: Path | str | None = None,
) -> SkillRegistry:
    """Read every manifest and layer them in order.

    Roles accumulate: an instance adds skills to a product role rather than replacing the role. A
    target is replaced whole by a later manifest, because a target is one shell root. A
    `<role>/<skill>` entry is read from wherever that role declares the skill, so it has to.
    """
    sources = manifest_sources(instance_path, product_manifest=product_manifest)
    roles: dict[str, list[tuple[str, ManifestSource]]] = {}
    targets: dict[str, tuple[dict[str, Any], ManifestSource]] = {}
    references: dict[tuple[str, str], tuple[str, ManifestSource]] = {}
    for source in sources:
        data = load_manifest(source.path)
        if not isinstance(data, dict):
            raise RegistryError(f"{source.path}: manifest must be a table")
        for role_name, role in _table(data, "roles", source).items():
            if not isinstance(role, dict):
                raise RegistryError(f"{source.path}: [roles.{role_name}] must be a table")
            _role_name(role_name, f"[roles.{role_name}] role name", source)
            declared = roles.setdefault(role_name, [])
            seen = {skill for skill, _ in declared}
            for entry in _string_list(role, "skills", f"roles.{role_name}", source):
                owner, _, skill = entry.rpartition("/")
                if owner:
                    _role_name(owner, f"roles.{role_name}.skills entry {entry!r} role", source)
                _identifier(skill, f"roles.{role_name}.skills entry", source)
                if skill not in seen:
                    declared.append((skill, source))
                    seen.add(skill)
                    if owner and owner != role_name:
                        references[(role_name, skill)] = (owner, source)
        for target_name, target in _table(data, "targets", source).items():
            if not isinstance(target, dict):
                raise RegistryError(f"{source.path}: [targets.{target_name}] must be a table")
            where = f"targets.{target_name}"
            _string(target, "shell", where, source)
            _string(target, "root", where, source)
            _string_list(target, "roles", where, source)
            targets[target_name] = (target, source)

    for target_name, (target, source) in sorted(targets.items()):
        for role_name in target["roles"]:
            if role_name not in roles:
                raise RegistryError(
                    f"{source.path}: targets.{target_name}.roles names the unknown role {role_name!r}"
                )
    owners: dict[tuple[str, str], str] = {}
    for (role_name, skill), (owner, source) in references.items():
        declared = dict(roles.get(owner, []))
        if skill not in declared or (owner, skill) in references:
            raise RegistryError(
                f"{source.path}: roles.{role_name}.skills names {owner}/{skill}, which the {owner} "
                "role does not declare itself"
            )
        # Read from the manifest whose tree holds the owner's copy.
        roles[role_name] = [
            (name, declared[skill] if name == skill else origin) for name, origin in roles[role_name]
        ]
        owners[(role_name, skill)] = owner
    return SkillRegistry(sources=tuple(sources), roles=roles, targets=targets, references=owners)


def target_roots(
    registry: SkillRegistry, home: Path | str | None = None, data_dir: Path | str | None = None
) -> dict[str, Path | None]:
    """Every target's root; None for a PO workspace root when no data directory is known."""
    resolved = _absolute(data_dir) if data_dir is not None else None
    return {
        name: _expand_root(target["root"], home, resolved)
        for name, (target, _) in sorted(registry.targets.items())
    }


def find_overlapping_target_roots(
    registry: SkillRegistry, home: Path | str | None = None, data_dir: Path | str | None = None
) -> list[dict[str, str]]:
    """Reject nested roots for one shell: recursive discovery mixes their namespaces."""
    errors: list[dict[str, str]] = []
    roots = target_roots(registry, home, data_dir)
    items = [
        (name, target["shell"], root.resolve())
        for name, (target, _) in sorted(registry.targets.items())
        if (root := roots[name]) is not None
    ]
    for index, (left_name, left_shell, left_root) in enumerate(items):
        for right_name, right_shell, right_root in items[index + 1 :]:
            if left_shell != right_shell or left_root == right_root:
                continue
            if left_root in right_root.parents or right_root in left_root.parents:
                errors.append(
                    {
                        "shell": left_shell,
                        "left_target": left_name,
                        "left_root": str(left_root),
                        "right_target": right_name,
                        "right_root": str(right_root),
                    }
                )
    return errors


def iter_expected(
    registry: SkillRegistry, home: Path | str | None = None, data_dir: Path | str | None = None
) -> list[ExpectedSkill]:
    """Every skill the registry expects in a shell, with both ends of the copy checked.

    A target whose root is in the PO workspace is left out when no data directory is known.
    """
    expected: list[ExpectedSkill] = []
    roots = target_roots(registry, home, data_dir)
    for target_name, (target, source_of_target) in sorted(registry.targets.items()):
        root = roots[target_name]
        if root is None:
            continue
        for role_name in target["roles"]:
            for skill, source in registry.roles.get(role_name, []):
                item = ExpectedSkill(
                    target=target_name,
                    shell=target["shell"],
                    role=role_name,
                    skill=skill,
                    source=source.roles_root / registry.source_role(role_name, skill) / skill,
                    dest=root / skill,
                    origin=source.origin,
                    manifest=source.path,
                )
                if not _within(source.roles_root, item.source):
                    raise RegistryError(
                        f"{source.path}: {role_name}/{skill} resolves to {item.source}, outside "
                        f"{source.roles_root}"
                    )
                if not _within(root, item.dest):
                    raise RegistryError(
                        f"{source_of_target.path}: targets.{target_name} would write "
                        f"{item.dest}, outside {root}"
                    )
                expected.append(item)
    return expected


def find_conflicting_destinations(expected: list[ExpectedSkill]) -> list[dict[str, str]]:
    """Two different sources copied into one directory: the second silently buries the first.

    Skill directories are flat under a shell root, so a product skill and an installation skill of
    the same name land on the same path. The pair is reported instead of resolved.
    """
    first_by_dest: dict[Path, ExpectedSkill] = {}
    conflicts: list[dict[str, str]] = []
    for item in expected:
        first = first_by_dest.setdefault(item.dest, item)
        if first is item or first.source == item.source:
            continue
        conflicts.append(
            {
                "dest": str(item.dest),
                "shell": item.shell,
                "left_target": first.target,
                "left_skill": f"{first.role}/{first.skill}",
                "left_source": str(first.source),
                "left_manifest": str(first.manifest),
                "right_target": item.target,
                "right_skill": f"{item.role}/{item.skill}",
                "right_source": str(item.source),
                "right_manifest": str(item.manifest),
            }
        )
    return conflicts


def _git_blob_id(path: Path) -> str:
    data = path.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _shipped_skill_blobs(roles_root: Path) -> set[tuple[str, str]]:
    """(skill name, blob id) of every SKILL.md the repository holding a roles tree ever had.

    No repository, or git failing, is an empty answer: it only means fewer copies can be proven ours.
    """
    if not roles_root.is_dir():
        return set()
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(roles_root),
                "log",
                "--all",
                "--no-renames",
                "--raw",
                "--no-abbrev",
                "--format=",
                "--",
                ".",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    if completed.returncode != 0:
        return set()
    shipped: set[tuple[str, str]] = set()
    for line in completed.stdout.splitlines():
        meta, separator, path = line.partition("\t")
        fields = meta.split()
        match = SHIPPED_SKILL.search(path)
        if not separator or not meta.startswith(":") or len(fields) < 4 or match is None:
            continue
        shipped.update((match.group(1), blob) for blob in fields[2:4] if blob.strip("0"))
    return shipped


def find_retired(
    registry: SkillRegistry,
    home: Path | str | None = None,
    data_dir: Path | str | None = None,
    target_filter: set[str] | None = None,
) -> list[dict[str, str]]:
    """Skill copies sync delivered into a target root that no manifest declares for that root anymore.

    Only copies that are provably sync's are named: one carrying the marker sync writes into every
    copy, or one delivered before the marker existed whose SKILL.md is byte for byte a version the
    repository of one of the manifests shipped under that name. Anything else in a shell root is
    somebody else's and is never named. A root shared by several targets keeps what any of them
    declares.
    """
    roots = target_roots(registry, home, data_dir)
    declared: dict[Path, set[str]] = {}
    for item in iter_expected(registry, home, data_dir):
        declared.setdefault(_absolute(item.dest.parent), set()).add(item.skill)
    scanned = sorted(
        {
            _absolute(root)
            for name, root in roots.items()
            if root is not None and (not target_filter or name in target_filter)
        }
    )
    shipped: set[tuple[str, str]] | None = None
    retired: list[dict[str, str]] = []
    for root in scanned:
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if child.is_symlink() or not child.is_dir() or child.name in declared.get(root, set()):
                continue
            if (child / OWNERSHIP_MARKER).is_file():
                evidence = f"carries {OWNERSHIP_MARKER}"
            else:
                skill_file = child / "SKILL.md"
                if skill_file.is_symlink() or not skill_file.is_file():
                    continue
                if shipped is None:
                    shipped = set().union(*(_shipped_skill_blobs(s.roles_root) for s in registry.sources))
                if (child.name, _git_blob_id(skill_file)) not in shipped:
                    continue
                evidence = "SKILL.md is a version a manifest repository shipped"
            retired.append({"root": str(root), "skill": child.name, "dest": str(child), "evidence": evidence})
    return retired


def command_script(role: str, skill: str, source: ManifestSource) -> Path | None:
    """The one command a skill may ship: ``<skill>.sh`` beside its ``SKILL.md``.

    Discovered from the tree rather than declared in the manifest, so a skill carries its entry point
    with it when it moves out of the product and into an installation.
    """
    script = source.roles_root / role / skill / f"{skill}.sh"
    return script if script.is_file() else None


def iter_expected_commands(registry: SkillRegistry, home: Path | str | None = None) -> list[ExpectedCommand]:
    """Every command the registry ships, keyed by the name the operator types.

    A command belongs to the skill, not to a shell: however many targets a skill is copied into, the
    link points at the canonical script. Two skills of the same name would want the same link, and
    that is refused here, before an audit reads the filesystem or a sync writes to it.
    """
    root = bin_dir(home)
    by_name: dict[str, ExpectedCommand] = {}
    for role in sorted(registry.roles):
        for skill, source in registry.roles[role]:
            if (role, skill) in registry.references:
                # The owning role links it.
                continue
            script = command_script(role, skill, source)
            if script is None:
                continue
            command = ExpectedCommand(
                name=skill,
                role=role,
                skill=skill,
                source=script,
                dest=root / skill,
                origin=source.origin,
                manifest=source.path,
            )
            clash = by_name.get(skill)
            if clash is not None:
                raise RegistryError(
                    f"two skills ship the command {command.dest}: {clash.role}/{clash.skill} "
                    f"from {clash.manifest} and {command.role}/{command.skill} from "
                    f"{command.manifest}"
                )
            by_name[skill] = command
    return [by_name[name] for name in sorted(by_name)]


def _link_target(link: Path) -> Path:
    """What a symlink points at, as a path that means the same from anywhere."""
    raw = Path(os.readlink(link))
    return raw if raw.is_absolute() else _absolute(link.parent / raw)


def _entry_point_is_owned(link_target: Path, owned_roots: tuple[Path, ...]) -> bool:
    """Whether a link we did not write is one of ours to repoint, or somebody else's.

    Ownership is a location this registry reads skills from, not a path that looks like one. That the
    target no longer exists says nothing either way, so the tree decides and existence is not
    consulted. Everything else stays somebody else's, however much its path resembles a skill source.
    """
    return any(link_target == root or root in link_target.parents for root in owned_roots)


def _entry_point_state(command: ExpectedCommand, owned_roots: tuple[Path, ...]) -> dict[str, str]:
    """What sync would do with one entry point, decided before anything is written."""
    base = {
        "command": command.name,
        "role": command.role,
        "skill": command.skill,
        "source": str(command.source),
        "dest": str(command.dest),
        "origin": command.origin,
        "manifest": str(command.manifest),
    }
    if command.dest.is_symlink():
        link_target = _link_target(command.dest)
        if link_target == command.source:
            return base | {"status": "ok", "reason": ""}
        if _entry_point_is_owned(link_target, owned_roots):
            return base | {
                "status": "stale",
                "reason": f"points at {link_target}, which is no longer where the skill lives",
            }
        return base | {
            "status": "conflict",
            "reason": f"{command.dest} is a link to {link_target}, which this registry does not own",
        }
    if command.dest.exists():
        return base | {
            "status": "conflict",
            "reason": f"{command.dest} is an existing file",
        }
    return base | {"status": "missing", "reason": f"{command.dest} does not exist"}


def _write_entry_point(command: ExpectedCommand, state: dict[str, str]) -> None:
    """Point the link at the source and make what it points at executable.

    An entry point that is already right is left exactly as it is. A symlink has no mode of its own,
    so the exec bit has to be on the script itself.
    """
    if state["status"] != "ok":
        command.dest.parent.mkdir(parents=True, exist_ok=True)
        if state["status"] == "stale":
            command.dest.unlink()
        command.dest.symlink_to(command.source)
    mode = command.source.stat().st_mode
    wanted = mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    if mode != wanted:
        command.source.chmod(wanted)


def _named_manifests(
    instance_path: Path | str | None,
    product_manifest: Path | str | None = None,
) -> str:
    """Every manifest a refusal was decided from — an overlay can be the file at fault."""
    product = manifest_path(product_manifest)
    overlay = instance_manifest_path(instance_path)
    return f"{product} + {overlay}" if overlay.is_file() else str(product)


def skill_delivery(
    role: str,
    skill: str,
    shell: str,
    *,
    instance_path: Path | str | None = None,
) -> dict[str, Any]:
    """Whether one role skill is materialized in one shell, and where it is expected.

    A head is launched into a shell, not into this repository. Both `delivered` and `reason` are
    filled for a manifest that cannot be read at all, because an unreadable registry is not evidence
    that the skill is there.
    """
    result: dict[str, Any] = {
        "role": role,
        "skill": skill,
        "shell": shell,
        "manifest": str(manifest_path()),
        "manifests": [],
        "delivered": False,
        "paths": [],
        "reason": "",
    }
    try:
        registry = load_registry(instance_path)
        result["manifests"] = registry.describe_sources()
        expected = [
            item
            for item in iter_expected(registry, data_dir=resolve_data_dir(registry, instance_path, role=role))
            if item.role == role and item.skill == skill and item.shell == shell
        ]
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        result["reason"] = f"skill registry {_named_manifests(instance_path)} could not be read: {exc}"
        return result
    if not expected:
        result["reason"] = f"no {shell} target in {_named_manifests(instance_path)} carries the {role} role"
        return result
    result["paths"] = [str(item.dest / "SKILL.md") for item in expected]
    missing = [path for path in result["paths"] if not Path(path).is_file()]
    if missing:
        result["reason"] = (
            f"{role}/{skill} is not in the {shell} skill directory ({', '.join(missing)}); "
            "run `ummanu role-skills sync`"
        )
        return result
    result["delivered"] = True
    return result


def audit(
    target_filter: set[str] | None = None,
    *,
    instance_path: Path | str | None = None,
    product_manifest: Path | str | None = None,
    home: Path | str | None = None,
    data_dir: Path | str | None = None,
) -> dict[str, Any]:
    registry = load_registry(instance_path, product_manifest=product_manifest)
    data_dir = resolve_data_dir(registry, instance_path, data_dir)
    config_errors = find_overlapping_target_roots(registry, home, data_dir)
    expected = iter_expected(registry, home, data_dir)
    if target_filter:
        expected = [item for item in expected if item.target in target_filter]
    destination_conflicts = find_conflicting_destinations(expected)
    retired = find_retired(registry, home, data_dir, target_filter)
    unresolved = [
        name
        for name, root in target_roots(registry, home, data_dir).items()
        if root is None and (not target_filter or name in target_filter)
    ]

    missing: list[dict[str, str]] = []
    drift: list[dict[str, str]] = []
    source_missing: list[dict[str, str]] = []

    for item in expected:
        source_skill = item.source / "SKILL.md"
        dest_skill = item.dest / "SKILL.md"
        base = {
            "target": item.target,
            "shell": item.shell,
            "role": item.role,
            "skill": item.skill,
            "source": str(item.source),
            "dest": str(item.dest),
            "origin": item.origin,
            "manifest": str(item.manifest),
        }
        if not source_skill.is_file():
            source_missing.append(base)
            continue
        if not dest_skill.is_file():
            missing.append(base)
            continue
        source_hash = _sha256(source_skill)
        dest_hash = _sha256(dest_skill)
        if source_hash != dest_hash:
            drift.append(base | {"source_hash": source_hash, "dest_hash": dest_hash})

    by_target: dict[str, dict[str, int | str]] = {}
    for item in expected:
        target = by_target.setdefault(
            item.target,
            {"shell": item.shell, "expected": 0, "missing": 0, "drift": 0, "source_missing": 0},
        )
        target["expected"] = int(target["expected"]) + 1
    for bucket_name, bucket in (("missing", missing), ("drift", drift), ("source_missing", source_missing)):
        for item in bucket:
            by_target[item["target"]][bucket_name] = int(by_target[item["target"]][bucket_name]) + 1

    # Filtered audits cover named shell targets, not commands.
    entry_points = (
        []
        if target_filter
        else [
            _entry_point_state(command, registry.owned_roots)
            for command in iter_expected_commands(registry, home)
        ]
    )
    entry_point_problems = [item for item in entry_points if item["status"] != "ok"]

    ok = not missing and not drift and not source_missing and not config_errors
    ok = ok and not entry_point_problems and not destination_conflicts and not retired
    return {
        "ok": ok,
        "manifest": str(manifest_path(product_manifest)),
        "manifests": registry.describe_sources(),
        "targets": by_target,
        "missing": missing,
        "drift": drift,
        "source_missing": source_missing,
        "entry_points": entry_point_problems,
        "config_errors": config_errors,
        "destination_conflicts": destination_conflicts,
        "retired": retired,
        # Targets in the PO workspace of an installation this audit could not name.
        "unresolved_targets": unresolved,
    }


def unmaterializable(
    registry: SkillRegistry,
    home: Path | str | None = None,
    target_filter: set[str] | None = None,
    data_dir: Path | str | None = None,
) -> list[str]:
    """Every reason this registry cannot be delivered as written, decided without writing.

    Nothing here reads a destination's contents, so the answer is the same before and after a sync.
    `sync` asks the same question, so the two cannot come to different conclusions.
    """
    problems = [
        f"overlapping skill target roots: {error}"
        for error in find_overlapping_target_roots(registry, home, data_dir)
    ]
    expected = iter_expected(registry, home, data_dir)
    if target_filter:
        expected = [item for item in expected if item.target in target_filter]
    problems += [
        f"two skills claim the skill directory {conflict['dest']}: "
        f"{conflict['left_skill']} from {conflict['left_manifest']} and "
        f"{conflict['right_skill']} from {conflict['right_manifest']}"
        for conflict in find_conflicting_destinations(expected)
    ]
    problems += [
        f"missing canonical skill: {item.source}/SKILL.md (declared by {item.manifest})"
        for item in expected
        if not (item.source / "SKILL.md").is_file()
    ]
    commands = [] if target_filter else iter_expected_commands(registry, home)
    problems += [
        f"command entry point {state['command']} from {state['manifest']} "
        f"cannot be materialized: {state['reason']}"
        for state in (_entry_point_state(command, registry.owned_roots) for command in commands)
        if state["status"] == "conflict"
    ]
    return problems


def sync(
    target_filter: set[str] | None = None,
    *,
    instance_path: Path | str | None = None,
    product_manifest: Path | str | None = None,
    home: Path | str | None = None,
    data_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Deliver every expected skill and entry point, or refuse before writing anything.

    A registry that is half applied is worse than one that was not applied at all, because the next
    audit cannot tell the two apart. Copies of skills no manifest declares for their root anymore
    are removed, but only the ones `find_retired` proves this sync delivered.
    """
    registry = load_registry(instance_path, product_manifest=product_manifest)
    data_dir = resolve_data_dir(registry, instance_path, data_dir)
    problems = unmaterializable(registry, home, target_filter, data_dir)
    if problems:
        raise RegistryError(problems[0])
    expected = iter_expected(registry, home, data_dir)
    if target_filter:
        expected = [item for item in expected if item.target in target_filter]
    commands = [] if target_filter else iter_expected_commands(registry, home)
    states = [_entry_point_state(command, registry.owned_roots) for command in commands]
    retired = find_retired(registry, home, data_dir, target_filter)

    copied: list[dict[str, str]] = []
    for item in expected:
        item.dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(item.source, item.dest, dirs_exist_ok=True)
        marker = item.dest / OWNERSHIP_MARKER
        if not marker.is_file() or marker.read_text(encoding="utf-8") != OWNERSHIP_MARKER_TEXT:
            marker.write_text(OWNERSHIP_MARKER_TEXT, encoding="utf-8")
        copied.append(
            {
                "target": item.target,
                "shell": item.shell,
                "role": item.role,
                "skill": item.skill,
                "dest": str(item.dest),
                "origin": item.origin,
            }
        )

    linked: list[dict[str, str]] = []
    for command, state in zip(commands, states):
        _write_entry_point(command, state)
        linked.append(
            {
                "command": command.name,
                "role": command.role,
                "skill": command.skill,
                "dest": str(command.dest),
                "source": str(command.source),
                "origin": command.origin,
                "manifest": str(command.manifest),
                "was": state["status"],
            }
        )

    for item in retired:
        shutil.rmtree(item["dest"])
    return {
        "ok": True,
        "copied": copied,
        "linked": linked,
        "removed": retired,
        "after": audit(
            target_filter,
            instance_path=instance_path,
            product_manifest=product_manifest,
            home=home,
            data_dir=data_dir,
        ),
    }


def render_markdown(result: dict[str, Any]) -> str:
    lines = [f"role skills: {'ok' if result['ok'] else 'drift'}", ""]
    if result.get("manifests"):
        for source in result["manifests"]:
            lines.append(f"- manifest ({source['origin']}): {source['path']}")
        lines.append("")
    for target, stats in sorted(result["targets"].items()):
        lines.append(
            f"- {target} ({stats['shell']}): expected={stats['expected']}, "
            f"missing={stats['missing']}, drift={stats['drift']}, source_missing={stats['source_missing']}"
        )
    for title, key in (("Missing", "missing"), ("Drift", "drift"), ("Source missing", "source_missing")):
        if result[key]:
            lines.extend(["", f"{title}:"])
            for item in result[key]:
                # Identify the manifest to edit, not just its layer.
                lines.append(
                    f"- {item['target']} {item['role']}/{item['skill']} "
                    f"[{item.get('origin', PRODUCT_ORIGIN)} {item.get('manifest', manifest_path())}]"
                    f" -> {item['dest']}"
                )
    if result.get("entry_points"):
        lines.extend(["", "Command entry points:"])
        for item in result["entry_points"]:
            lines.append(
                f"- {item['command']} [{item['origin']} {item['manifest']}] "
                f"{item['status']}: {item['reason']}"
            )
    if result.get("retired"):
        lines.extend(["", "Retired copies (removed by sync):"])
        for item in result["retired"]:
            lines.append(f"- {item['dest']}: {item['evidence']}")
    if result.get("unresolved_targets"):
        lines.extend(
            ["", "Not checked, no installation data directory: " + ", ".join(result["unresolved_targets"])]
        )
    if result.get("destination_conflicts"):
        lines.extend(["", "Destination conflicts:"])
        for item in result["destination_conflicts"]:
            lines.append(
                f"- {item['dest']}: {item['left_skill']} from {item['left_manifest']} "
                f"and {item['right_skill']} from {item['right_manifest']}"
            )
    if result["config_errors"]:
        lines.extend(["", "Configuration errors:"])
        for item in result["config_errors"]:
            lines.append(
                f"- {item['shell']}: nested target roots {item['left_target']}={item['left_root']} "
                f"and {item['right_target']}={item['right_root']}"
            )
    return "\n".join(lines)


def parse_targets(value: str | None) -> set[str] | None:
    if not value:
        return None
    return {part.strip() for part in value.split(",") if part.strip()}


def run_role_skills(args) -> int:
    targets = parse_targets(args.targets)
    instance = getattr(args, "instance", None)
    product_root = getattr(args, "product_root", None)
    product = product_manifest_path(product_root) if product_root else None
    data_dir = getattr(args, "data_dir", None)
    if args.role_skills_command == "audit":
        try:
            result = audit(targets, instance_path=instance, product_manifest=product, data_dir=data_dir)
        except (OSError, ValueError) as exc:
            print(f"ummanu role-skills audit: {exc}")
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render_markdown(result))
        return 1 if args.check and not result["ok"] else 0
    try:
        result = sync(targets, instance_path=instance, product_manifest=product, data_dir=data_dir)
    except (OSError, ValueError) as exc:
        print(f"ummanu role-skills sync: {exc}")
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render_markdown(result["after"]))
    return 0


def _add_common_arguments(parser, name: str) -> None:
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--targets", help="comma-separated target names from skills/manifest.toml")
    add_instance_argument(
        parser,
        help="instance dir or instance.yaml whose skills/manifest.toml is layered over the product "
        f"manifest (default: {INSTANCE_ENV}, else {default_instance_path()})",
    )
    parser.add_argument(
        "--product-root",
        help="product checkout whose skills/manifest.toml is the product layer "
        f"(default: {MANIFEST_ENV}, else the configured product checkout)",
    )
    parser.add_argument(
        "--data-dir",
        help=f"data directory whose PO workspace the {PO_WORKSPACE_ROOT}/ roots name "
        "(default: data_dir of the instance)",
    )
    if name == "audit":
        parser.add_argument("--check", action="store_true", help="exit 1 when missing or drift exists")


def add_role_skills_subcommands(subparsers) -> None:
    """Expose the audit as a health interface and the sync as a materializer."""
    command = subparsers.add_parser(
        "role-skills", help="audit or sync role-owned skills into shell skill directories"
    )
    commands = command.add_subparsers(dest="role_skills_command", required=True)
    for name in ("audit", "sync"):
        sub = commands.add_parser(name)
        _add_common_arguments(sub, name)
        sub.set_defaults(handler=run_role_skills, check=False)
    command.set_defaults(handler=run_role_skills)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="role_skills_command", required=True)
    for name in ("audit", "sync"):
        p = sub.add_parser(name)
        _add_common_arguments(p, name)
    args = parser.parse_args(argv)
    args.check = getattr(args, "check", False)
    try:
        resolve_instance_argument(args)
    except MissingDefaultInstance as exc:
        print(f"ummanu role-skills: {exc}", file=sys.stderr)
        return 2
    return run_role_skills(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
