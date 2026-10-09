"""Manifest membership checks and selector handoff to project-owned broad runners."""

from __future__ import annotations

import importlib.util
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ummanu.broad_check import BroadCheckError

# Built-in pytest options with a separate value. Unknown plugin options require explicit roots;
# equals-form options are self-contained and never consume a following collection argument.
_PYTEST_VALUES = frozenset({
    "-m", "--markexpr", "-k", "-p", "-c", "-o", "--override-ini", "-W", "--pythonwarnings",
    "--rootdir", "--confcutdir", "--basetemp", "--junitxml", "--junit-xml", "--junitprefix",
    "--junit-prefix", "--durations", "--durations-min", "--maxfail", "--ignore", "--ignore-glob",
    "--deselect", "--import-mode", "--assert", "--capture", "--tb", "--show-capture", "--color",
    "--code-highlight", "--verbosity", "--log-level", "--log-format", "--log-date-format",
    "--log-cli-level", "--log-cli-format", "--log-cli-date-format", "--log-file",
    "--log-file-mode", "--log-file-level", "--log-file-format", "--log-file-date-format",
    "--log-auto-indent", "--log-disable", "--debug", "--pastebin",
    "--lfnf", "--last-failed-no-failures", "--pdbcls", "--doctest-report",
    "--doctest-glob", "--doctest-resolution", "--doctest-optionflags", "--cache-show", "-r",
})
_PYTEST_OPTIONAL_VALUES = frozenset({"--debug", "--cache-show", "-r"})
_PYTEST_FLAGS = frozenset({
    "-v", "--verbose", "-q", "--quiet", "-x", "--exitfirst", "-s", "--runxfail", "--lf",
    "--last-failed", "--ff", "--failed-first", "--nf", "--new-first",
    "--cache-clear", "--stepwise", "--sw", "--stepwise-skip", "--sw-skip", "--stepwise-reset",
    "--sw-reset", "--fixtures", "--funcargs", "--fixtures-per-test", "--pdb", "--trace",
    "--disable-warnings", "--disable-pytest-warnings", "-l", "--showlocals", "--no-showlocals",
    "--full-trace", "--collect-only", "--co", "--pyargs", "--noconftest", "--keep-duplicates",
    "--continue-on-collection-errors", "--strict-config", "--strict-markers", "--strict",
    "--doctest-modules", "--doctest-ignore-import-errors", "--doctest-continue-on-failure",
    "--help", "-h", "--version", "-V", "--markers", "--trace-config", "--setup-only",
    "--setup-show", "--setup-plan", "--no-header", "--no-summary", "--force-short-summary",
    "--no-fold-skipped", "--disable-plugin-autoload",
})


def _relative_path(value: str) -> Path:
    path = Path(value.partition("::")[0])
    if not value.partition("::")[0] or not str(path) or value.startswith("-") or path.is_absolute() or ".." in path.parts:
        raise ValueError("expected a relative collection path without '..' or a leading '-'")
    return path


@dataclass(frozen=True)
class PytestSelection:
    """Resolve declaration roles, membership and replacement without filesystem discovery.

    Explicit roots identify exact argv tokens, each occurring once. All other tokens are
    preserved without inferring plugin option arity. Resolution happens only for subsets.
    """

    args: tuple[str, ...]
    roots: tuple[str, ...]
    positions: tuple[int, ...]

    @classmethod
    def resolve(cls, args: tuple[str, ...], explicit: tuple[str, ...] | None = None) -> PytestSelection:
        def ambiguous(reason: str) -> BroadCheckError:
            return BroadCheckError(
                "ambiguous_pytest_declaration",
                f"{reason}; declare broad_check.collection_roots explicitly; execution only in CI",
            )

        if explicit is not None:
            if not explicit or len(set(explicit)) != len(explicit):
                raise ambiguous("collection_roots must be nonempty and unique")
            positions = []
            for entry in explicit:
                if args.count(entry) != 1:
                    raise ambiguous(f"collection root {entry!r} must occur exactly once in broad_check.args")
                position = args.index(entry)
                if position and args[position - 1] in _PYTEST_VALUES:
                    raise ambiguous(f"collection root {entry!r} is an option value")
                positions.append(position)
            positions.sort()
        else:
            positions = []
            index = 0
            positional_only = False
            while index < len(args):
                arg = args[index]
                if positional_only or not arg.startswith("-"):
                    positions.append(index)
                elif arg == "--":
                    positional_only = True
                elif "=" in arg:
                    pass
                elif arg in _PYTEST_VALUES:
                    has_value = index + 1 < len(args) and not args[index + 1].startswith("-")
                    if has_value:
                        index += 1
                    elif arg not in _PYTEST_OPTIONAL_VALUES:
                        raise ambiguous(f"option {arg!r} lacks an unambiguous separate value")
                elif arg in _PYTEST_FLAGS or re.fullmatch(r"-[vq]+", arg):
                    pass
                elif any(arg.startswith(option) and len(arg) > len(option)
                         for option in ("-m", "-k", "-p", "-c", "-o", "-W", "-r")):
                    pass
                else:
                    raise ambiguous(f"unknown option {arg!r}")
                index += 1
        roots = tuple(args[position] for position in positions)
        if not roots:
            raise ambiguous("pytest declaration has no positional collection roots")
        try:
            for entry in roots:
                _relative_path(entry)
                if "::" in entry:
                    raise ValueError("collection roots cannot contain node-ids")
        except ValueError as exc:
            raise ambiguous(f"invalid collection_roots: {exc}") from exc
        return cls(args, roots, tuple(positions))

    def select(self, checkout: Path, selectors: tuple[str, ...]) -> tuple[str, ...]:
        root = checkout.resolve()
        allowed = tuple((root / entry).resolve() for entry in self.roots)
        for selector in selectors:
            try:
                path = _relative_path(selector)
                selected = (root / path).resolve()
                if not selected.is_relative_to(root) or not any(
                    selected.is_relative_to(entry) and entry.is_relative_to(root) for entry in allowed
                ):
                    raise ValueError("outside roots")
            except ValueError as exc:
                raise BroadCheckError(
                    "outside_local_profile",
                    f"{selector!r}: outside declared pytest roots {list(self.roots)!r}; execution only in CI",
                ) from exc
        result = []
        for position, arg in enumerate(self.args):
            if position == self.positions[0]:
                result.extend(selectors)
            if position not in self.positions:
                result.append(arg)
        return tuple(result)

    def marker(self) -> str:
        marker = ""
        for index, arg in enumerate(self.args):
            if arg in {"-m", "--markexpr"} and index + 1 < len(self.args):
                marker = self.args[index + 1]
            elif arg.startswith("--markexpr="):
                marker = arg.partition("=")[2]
            elif arg.startswith("-m") and arg != "-m":
                marker = arg[2:]
        return marker


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
