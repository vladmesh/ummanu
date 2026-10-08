"""Explicit local membership, checked before a runner imports any selected test."""

from __future__ import annotations

import importlib.util
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ummanu.broad_check import BroadCheckError


def validate_declaration(local: dict[str, Any], module: str, args: tuple[str, ...]) -> None:
    """Check the executable shape as well as the schema; flags cannot narrow a full round."""
    manifest = local.get("ci_manifest")
    expected = "tests.broad" if manifest else local["runner"]
    if module != expected or args:
        raise BroadCheckError(
            "invalid_local_check",
            f"broad_check.local requires module {expected!r} and no broad_check.args; "
            "the declared membership supplies the runner's test arguments",
        )
    if manifest and (local["runner"] != "unittest" or local["shards"] != ["unit", "component"]):
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
    manifest: bool = False

    @classmethod
    def load(cls, root: Path, declaration: dict[str, Any]) -> LocalProfile:
        try:
            if "ci_manifest" in declaration:
                grouped = _manifest(root, declaration["ci_manifest"])
                owners = {path: shard for shard, paths in grouped.items() for path in paths}
            else:
                owners = dict(declaration["modules"])
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
            return cls(declaration["runner"], owners, shards, "ci_manifest" in declaration)
        except (OSError, ValueError, UnicodeError, ImportError, AttributeError) as exc:
            raise BroadCheckError("invalid_local_check", f"cannot read broad_check.local: {exc}") from exc

    def full_args(self) -> list[str]:
        paths = [path for shard in self.shards for path, owner in self.owners.items() if owner == shard]
        if self.manifest:
            return []  # tests.broad uses the same validated manifest and its fixed two shards.
        return [self._dotted(path) for path in paths] if self.runner == "unittest" else paths

    @staticmethod
    def _dotted(path: str) -> str:
        return path.removesuffix(".py").replace("/", ".")

    def select(self, selector: str) -> tuple[str, str]:
        """Return canonical module and runner node-id, preserving pytest parameter text as argv."""
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
