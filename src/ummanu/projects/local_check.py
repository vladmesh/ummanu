"""Manifest membership checks and selector handoff to project-owned broad runners."""

from __future__ import annotations

import importlib.util
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ummanu.broad_check import BroadCheckError


def validate_pytest_path(root: Path, args: tuple[str, ...], selector: str) -> None:
    """Keep appended pytest paths inside the declared collection roots.

    Markers, deselection and node validation remain pytest's. These are the separate-value
    options used by the installed adapters; equals-form options never look like a path.
    """
    value_options = {"-c", "--rootdir", "-m", "-k", "-p", "-o", "--override-ini"}
    paths = []
    expecting_value = False
    for arg in args:
        if expecting_value:
            expecting_value = False
        elif arg.startswith("-"):
            expecting_value = arg in value_options
        else:
            paths.append((root / arg.partition("::")[0]).resolve())
    candidate_root = root.resolve()
    selected = (root / selector.partition("::")[0]).resolve()
    allowed = paths or [candidate_root]
    if not selected.is_relative_to(candidate_root) or not any(
        selected == path or (path.is_dir() and selected.is_relative_to(path)) for path in allowed
    ):
        raise BroadCheckError(
            "outside_local_profile",
            f"{selector.partition('::')[0]}: outside declared pytest paths; execution only in CI",
        )


def validate_declaration(local: dict[str, Any], module: str, args: tuple[str, ...]) -> None:
    """Check the executable shape as well as the schema; flags cannot narrow a full round."""
    if not module:
        raise BroadCheckError("invalid_local_check", "broad_check.local needs broad_check.module")
    if local.get("membership") == "runner":
        return
    options = {"-v", "--verbose", "-q", "--quiet", "-b", "--buffer", "-c", "--catch", "-f", "--failfast"}
    if module != "tests.broad" or any(arg not in options for arg in args):
        raise BroadCheckError(
            "invalid_local_check",
            "manifest membership requires tests.broad and only reporting/control arguments; "
            "test names and filters cannot define a full-profile receipt",
        )
    if local["runner"] != "unittest" or local["shards"] != ["unit", "component"]:
        raise BroadCheckError(
            "invalid_local_check", "the Ummanu CI manifest local profile is exactly unit + component"
        )


def _manifest(root: Path, relative: str) -> dict[str, list[str]]:
    """Use the candidate's existing CI validator, never a second manifest parser."""
    path = root / "scripts" / "ci_test_shards.py"
    spec = importlib.util.spec_from_file_location("ummanu_local_ci_shards", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"CI manifest validator is unavailable at {path}")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(spec.name)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        return module.load_manifest(root, root / relative)
    finally:
        if previous is None:
            sys.modules.pop(spec.name, None)
        else:
            sys.modules[spec.name] = previous


@dataclass(frozen=True)
class LocalProfile:
    runner: str
    owners: dict[str, str]
    shards: tuple[str, ...]
    runner_owned: bool = False
    selector_args: tuple[str, ...] = ()

    @classmethod
    def load(cls, root: Path, declaration: dict[str, Any]) -> LocalProfile:
        if declaration.get("membership") == "runner":
            return cls("", {}, (), runner_owned=True, selector_args=tuple(declaration["selector_args"]))
        try:
            grouped = _manifest(root, declaration["ci_manifest"])
            owners = {path: shard for shard, paths in grouped.items() for path in paths}
            shards = tuple(declaration["shards"])
            if not owners or any(shard not in owners.values() for shard in shards):
                raise ValueError("local membership or a declared local shard is empty")
            for relative in owners:
                path = Path(relative)
                if (
                    relative.startswith("-")
                    or path.is_absolute()
                    or ".." in path.parts
                    or path.as_posix() != relative
                    or not relative.endswith(".py")
                    or not (root / path).resolve().is_relative_to(root.resolve())
                ):
                    raise ValueError(f"invalid candidate test module path {relative!r}")
                if declaration["runner"] == "unittest" and not re.fullmatch(
                    r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*", cls._dotted(relative)
                ):
                    raise ValueError(f"invalid unittest module path {relative!r}")
                with (root / path).open("rb") as handle:
                    handle.read(1)
            return cls(declaration["runner"], owners, shards)
        except (OSError, ValueError, UnicodeError, ImportError, AttributeError) as exc:
            raise BroadCheckError("invalid_local_check", f"cannot read broad_check.local: {exc}") from exc

    def full_args(self) -> list[str]:
        paths = [path for shard in self.shards for path, owner in self.owners.items() if owner == shard]
        return [self._dotted(path) for path in paths] if self.runner == "unittest" else paths

    @staticmethod
    def _dotted(path: str) -> str:
        return path.removesuffix(".py").replace("/", ".")

    def select(self, selector: str) -> tuple[str, str]:
        """Return canonical module and runner node-id, preserving pytest parameter text as argv."""
        if self.runner_owned:
            if selector.startswith("-"):
                raise BroadCheckError("invalid_selector", "a selector cannot be a runner option")
            return selector.partition("::")[0], selector
        path, separator, node = selector.partition("::")
        if self.runner == "unittest" and not separator and path not in self.owners:
            matches = [
                entry
                for entry in self.owners
                if selector == self._dotted(entry) or selector.startswith(self._dotted(entry) + ".")
            ]
            if matches:
                path = max(matches, key=len)
                node = selector[len(self._dotted(path)) :].removeprefix(".")
        owner = self.owners.get(path)
        if owner is None:
            raise BroadCheckError("unknown_local_module", f"{path}: unknown module in broad_check.local")
        if owner not in self.shards:
            raise BroadCheckError("ci_only_module", f"{path}: shard {owner}; execution only in CI")
        if separator and not node:
            raise BroadCheckError("invalid_selector", f"{selector!r}: empty test node-id")
        if self.runner == "unittest":
            names = node.replace("::", ".")
            if names and not re.fullmatch(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*", names):
                raise BroadCheckError("invalid_selector", f"{selector!r}: invalid unittest node-id")
            target = self._dotted(path) + ("." + names if names else "")
        else:
            target = selector
        return path, target
