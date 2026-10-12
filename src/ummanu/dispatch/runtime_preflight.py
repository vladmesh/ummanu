"""A stdlib-only fence before the production ``ummanu`` entry point.

This file is deliberately executable by pathname.  The production systemd unit starts the
configured virtualenv's interpreter with ``-I`` and this file, before the console entry point has
an opportunity to import ``ummanu``.  Keep imports in this module in the standard library: an
editable installation can point at a vanished workspace, so importing the package to diagnose
that fact is already too late.

``ProductionRuntime`` imports the data contract after the fence has admitted the interpreter, and
Doctor invokes this file in a child interpreter.  That makes the classification and JSON shape one
contract, instead of three similar-looking path checks.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

PREFLIGHT_REFUSAL_EXIT = 78
PACKAGE = "ummanu"
_UNHEALTHY_KEPT = 50
_ERRORS_KEPT = 5


def _resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _inside(path: str | Path, root: str | Path) -> bool:
    try:
        return _resolved(path).is_relative_to(_resolved(root))
    except (OSError, RuntimeError, ValueError):
        return False


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _counter(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class RuntimeProvenance:
    """The stable, secret-free result of an editable-install provenance observation."""

    classification: str
    interpreter: str
    product_root: str
    import_origin: str
    metadata_targets: tuple[tuple[str, str, str], ...]
    offending_target: str = ""
    metadata_source: str = ""
    package: str = PACKAGE

    @property
    def valid(self) -> bool:
        return self.classification == "valid"

    def as_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification,
            "interpreter": self.interpreter,
            "product_root": self.product_root,
            "package": self.package,
            "import_origin": self.import_origin,
            "metadata_targets": [
                {"kind": kind, "source": source, "target": target}
                for kind, source, target in self.metadata_targets
            ],
            "metadata_source": self.metadata_source,
            "offending_target": self.offending_target,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> RuntimeProvenance:
        raw_targets = payload.get("metadata_targets")
        targets: list[tuple[str, str, str]] = []
        if isinstance(raw_targets, list):
            for entry in raw_targets:
                if not isinstance(entry, Mapping):
                    continue
                kind = entry.get("kind")
                source = entry.get("source")
                target = entry.get("target")
                if all(isinstance(value, str) for value in (kind, source, target)):
                    targets.append((kind, source, target))
        return cls(
            classification=str(payload.get("classification") or "interpreter_unavailable"),
            interpreter=str(payload.get("interpreter") or ""),
            product_root=str(payload.get("product_root") or ""),
            import_origin=str(payload.get("import_origin") or ""),
            metadata_targets=tuple(targets),
            offending_target=str(payload.get("offending_target") or ""),
            metadata_source=str(payload.get("metadata_source") or ""),
            package=str(payload.get("package") or PACKAGE),
        )

    def refusal(self, boundary: str) -> str:
        detail = self.offending_target or self.import_origin or self.interpreter
        source = f"; metadata_source={self.metadata_source}" if self.metadata_source else ""
        return (
            f"production runtime provenance refused at {boundary}: {self.classification}; "
            f"interpreter={self.interpreter}; product_root={self.product_root}; observed={detail}{source}"
        )


def _distribution_name(path: Path) -> str:
    """Return a dist-info's normalized Name without importing its package."""
    metadata = path / "METADATA"
    try:
        for line in metadata.read_text(encoding="utf-8").splitlines():
            if line.lower().startswith("name:"):
                return line.partition(":")[2].strip().lower().replace("-", "_")
    except (OSError, UnicodeError):
        pass
    return ""


def _named_for_package(path: Path, package: str) -> bool:
    """Whether a setuptools editable metadata filename belongs to ``package``.

    A vanished target cannot be examined, so the metadata file name is intentionally part of the
    evidence.  Existing unusual files get a second chance through their target containing the
    requested package below, but a random editable project never becomes Ummanu evidence.
    """
    normalized = re.sub(r"[-_.]+", "_", path.stem.lower())
    return package.lower().replace("-", "_") in normalized


def _absolute_target(value: str, base: Path) -> str | None:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    try:
        return str(candidate.resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return None


def _finder_targets(finder: Path, package: str) -> list[str]:
    """Read setuptools' static ``MAPPING``/``NAMESPACES`` without executing a finder."""
    try:
        tree = ast.parse(finder.read_text(encoding="utf-8"), filename=str(finder))
    except (OSError, UnicodeError, SyntaxError):
        return []
    targets: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        assigned = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
        names = {
            target.id
            for target in assigned
            if isinstance(target, ast.Name) and target.id in {"MAPPING", "NAMESPACES"}
        }
        if not names or not isinstance(value, ast.Dict):
            continue
        for key, target in zip(value.keys, value.values, strict=True):
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                continue
            key_name = key.value.split(".", 1)[0]
            if key_name != package or not isinstance(target, ast.Constant) or not isinstance(target.value, str):
                continue
            resolved = _absolute_target(target.value, finder.parent)
            if resolved:
                targets.append(resolved)
    return targets


def _target_contains_package(target: str, package: str) -> bool:
    root = Path(target)
    return any(
        candidate.is_file()
        for candidate in (
            root / package / "__init__.py",
            root / "src" / package / "__init__.py",
            root / "__init__.py" if root.name == package else root / ".absent",
        )
    )


def _metadata_targets(package: str) -> list[tuple[str, str, str]]:
    """Inspect editable metadata in this interpreter's site directories, without importing it."""
    import site

    roots = _site_roots(site)

    targets: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    def add(kind: str, source: Path, target: str) -> None:
        item = (kind, str(source), target)
        if item not in seen:
            seen.add(item)
            targets.append(item)

    for root in roots:
        try:
            if not root.is_dir():
                continue
            pths = sorted(root.glob("*.pth"))
        except OSError:
            continue
        for pth in pths:
            named = _named_for_package(pth, package)
            try:
                lines = pth.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError):
                continue
            for line in lines:
                value = line.strip()
                if not value or value.startswith("#"):
                    continue
                if value.startswith("import "):
                    imports = re.findall(r"\bimport\s+([A-Za-z_][A-Za-z0-9_]*)", value)
                    for module in imports:
                        finder = root / f"{module}.py"
                        for target in _finder_targets(finder, package):
                            add("pth_finder", pth, target)
                    continue
                target = _absolute_target(value, pth.parent)
                if target and (named or _target_contains_package(target, package)):
                    add("pth", pth, target)
        try:
            direct_urls = sorted(root.glob("*.dist-info/direct_url.json"))
        except OSError:
            continue
        for direct in direct_urls:
            dist_info = direct.parent
            if not (_named_for_package(dist_info, package) or _distribution_name(dist_info) == package):
                continue
            try:
                body = json.loads(direct.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError):
                continue
            url = body.get("url") if isinstance(body, Mapping) else None
            if not isinstance(url, str) or not url.startswith("file://"):
                continue
            target = _absolute_target(unquote(urlsplit(url).path), direct.parent)
            if target:
                add("direct_url", direct, target)
    return targets


def _package_origin(target: str, package: str) -> str:
    root = Path(target)
    candidates = (root / package / "__init__.py", root / "src" / package / "__init__.py")
    if root.name == package:
        candidates = (root / "__init__.py", *candidates)
    for candidate in candidates:
        try:
            if candidate.is_file():
                return str(candidate.resolve(strict=False))
        except OSError:
            continue
    return ""


def _installed_package_origin(package: str) -> str:
    """Find a non-editable package origin without importing the package."""
    import site

    for root in _site_roots(site):
        origin = _package_origin(str(root), package)
        if origin:
            return origin
    return ""


def _site_roots(site_module: Any) -> list[Path]:
    """Site directories available in CPython's production interpreter.

    The packaged runtime requires CPython's :mod:`site` API. Treating an unavailable API as an
    empty site would turn an uninspectable installation into a misleading provenance verdict.
    """
    roots = [Path(raw) for raw in site_module.getsitepackages()]
    user = site_module.getusersitepackages()
    if isinstance(user, str):
        roots.append(Path(user))
    return roots


def observe(
    *,
    interpreter: str | Path,
    product_root: str | Path,
    package: str = PACKAGE,
    workspaces_root: str | Path = "",
    git_workspaces_root: str | Path = "",
) -> RuntimeProvenance:
    """Classify the current interpreter's static package provenance.

    The supplied interpreter is an identity to report.  Callers run this source file under that
    interpreter, so the observation never starts a candidate package import merely to discover it
    was foreign.

    An editable target inside either workspaces root is workspace-targeted: the Orca root
    (`workspaces_root`) and `<data_dir>/workspaces` (`git_workspaces_root`), where git-managed card
    and observer worktrees live. Both are card checkouts that are removed when their card ends.
    """
    workspace_roots = [str(candidate) for candidate in (workspaces_root, git_workspaces_root) if candidate]
    expected_python = Path(interpreter).expanduser().absolute()
    root = _resolved(product_root)
    if not expected_python.is_file() or not os.access(expected_python, os.X_OK):
        return RuntimeProvenance("interpreter_unavailable", str(expected_python), str(root), "", ())
    targets = _metadata_targets(package)
    for _kind, source, target in targets:
        if any(_inside(target, candidate) for candidate in workspace_roots):
            return RuntimeProvenance(
                "workspace_targeted_editable",
                str(expected_python),
                str(root),
                _package_origin(target, package),
                tuple(targets),
                target,
                source,
                package,
            )
        if not _inside(target, root):
            return RuntimeProvenance(
                "wrong_root",
                str(expected_python),
                str(root),
                _package_origin(target, package),
                tuple(targets),
                target,
                source,
                package,
            )
    origins = [_package_origin(target, package) for _kind, _source, target in targets]
    origin = next((candidate for candidate in origins if candidate), "") or _installed_package_origin(package)
    if not origin:
        return RuntimeProvenance("missing_import", str(expected_python), str(root), "", tuple(targets), package=package)
    if not _inside(origin, root):
        return RuntimeProvenance(
            "wrong_root",
            str(expected_python),
            str(root),
            origin,
            tuple(targets),
            origin,
            package=package,
        )
    return RuntimeProvenance("valid", str(expected_python), str(root), origin, tuple(targets), package=package)


def _load_state(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def record_incident(telemetry: dict[str, Any], entry: dict[str, Any]) -> None:
    """Fold one tick into the open incident, closing it when the tick is healthy again.

    An incident is one continuous run of unhealthy ticks: the tick that finds none open opens one and
    bumps `incident_total`, every unhealthy tick after it extends that record, and the first healthy
    tick closes it into `recovery` and bumps `recovery_total`. Both counters are monotonic within a
    generation, because a reader dedupes on them and a counter that can go down would let a later
    event consume an earlier one the reader has not seen.

    The tick that opened the incident is kept whole under `opened`, so its cause survives into the
    recovery record.
    """
    incident = telemetry.get("incident")
    incident = dict(incident) if isinstance(incident, dict) else None
    if entry["healthy"]:
        if incident:
            telemetry["recovery"] = {
                **incident,
                "recovered_seq": entry["seq"],
                "recovered_at": entry["at"],
                "recovered_status": entry["status"],
            }
            telemetry["recovery_total"] = _counter(telemetry.get("recovery_total")) + 1
            incident = None
    elif incident:
        incident["unhealthy_ticks"] = _counter(incident.get("unhealthy_ticks")) + 1
        incident["last_seq"] = entry["seq"]
        incident["last_at"] = entry["at"]
    else:
        incident = {
            "id": uuid.uuid4().hex,
            "opened_seq": entry["seq"],
            "opened_at": entry["at"],
            "last_seq": entry["seq"],
            "last_at": entry["at"],
            "unhealthy_ticks": 1,
            "opened": entry,
        }
        telemetry["incident_total"] = _counter(telemetry.get("incident_total")) + 1
    telemetry["incident"] = incident
    telemetry.setdefault("recovery", None)
    telemetry.setdefault("incident_total", _counter(telemetry.get("incident_total")))
    telemetry.setdefault("recovery_total", _counter(telemetry.get("recovery_total")))


def _record_refusal_telemetry(payload: dict[str, Any], provenance: RuntimeProvenance) -> None:
    """Write the existing terminal-tick shape so health and steward stay the only readers."""
    telemetry = payload.get("tick_telemetry")
    telemetry = dict(telemetry) if isinstance(telemetry, dict) else {}
    telemetry.setdefault("generation", uuid.uuid4().hex)
    seq = _counter(telemetry.get("tick_seq")) + 1
    entry = {
        "seq": seq,
        "at": _now(),
        "status": "failed",
        "step": "production-runtime-preflight",
        "healthy": False,
        "reason": provenance.refusal("pre-import"),
        "actions": 0,
        "error_count": 1,
        "degraded_count": 0,
        "degradations": [],
        "errors": [
            {
                "ref": "",
                "code": "production_runtime_provenance",
                "message": provenance.refusal("pre-import"),
            }
        ][:_ERRORS_KEPT],
    }
    telemetry["tick_seq"] = seq
    telemetry["last"] = entry
    unhealthy = [entry for entry in telemetry.get("unhealthy", []) if isinstance(entry, dict)]
    unhealthy.append(entry)
    telemetry["unhealthy"] = unhealthy[-_UNHEALTHY_KEPT:]
    telemetry["unhealthy_total"] = _counter(telemetry.get("unhealthy_total")) + 1
    record_incident(telemetry, entry)
    payload["tick_telemetry"] = telemetry


def record_diagnostic(state_path: str | Path, provenance: RuntimeProvenance) -> None:
    """Atomically publish the one diagnostic an unimportable dispatcher can leave behind."""
    path = Path(state_path)
    payload = _load_state(path)
    previous = payload.get("runtime_provenance")
    if provenance.valid:
        # A successful preflight is not yet a successful tick.  It only supersedes an earlier
        # refusal; the command that follows records the healthy tick and closes its incident.
        if not isinstance(previous, Mapping) or previous.get("status") != "refused":
            return
        payload["runtime_provenance"] = {"status": "valid", "at": _now(), "observation": provenance.as_dict()}
    else:
        payload["runtime_provenance"] = {
            "status": "refused",
            "at": _now(),
            "observation": provenance.as_dict(),
        }
        _record_refusal_telemetry(payload, provenance)
    payload.setdefault("version", 1)
    payload.setdefault("mode", "production")
    payload.setdefault("phase", "new")
    _write_json_atomic(path, payload)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="check Ummanu production runtime provenance before import")
    parser.add_argument("--product-root", required=True)
    parser.add_argument("--interpreter", default=sys.executable)
    parser.add_argument("--package", default=PACKAGE)
    parser.add_argument(
        "--workspaces-root",
        help="the Orca workspaces root",
        default=os.environ.get("UMMANU_DISPATCHER_WORKSPACES_ROOT", str(Path.home() / "orca" / "workspaces")),
    )
    parser.add_argument(
        "--git-workspaces-root",
        help="the git workspaces root, <data_dir>/workspaces; derived from --data-dir when omitted",
    )
    parser.add_argument("--state-path")
    parser.add_argument("--data-dir")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # The dispatcher itself gives an explicit runtime.env override priority over the configured
    # data directory. Follow that same address rule or the health reader would watch one file
    # while the pre-import fence writes another, and the fence would guard another workspaces root.
    data_dir = Path(os.environ.get("UMMANU_DATA_DIR") or args.data_dir) if args.data_dir else None
    git_workspaces_root = args.git_workspaces_root or (str(data_dir / "workspaces") if data_dir else "")
    provenance = observe(
        interpreter=args.interpreter,
        product_root=args.product_root,
        package=args.package,
        workspaces_root=args.workspaces_root,
        git_workspaces_root=git_workspaces_root,
    )
    state_path = args.state_path
    if state_path is None and data_dir is not None:
        state_path = str(data_dir / "dispatcher" / "production-state.json")
    if state_path:
        record_diagnostic(state_path, provenance)
    if args.json or not args.command:
        print(json.dumps(provenance.as_dict(), sort_keys=True))
    if not provenance.valid:
        return PREFLIGHT_REFUSAL_EXIT
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if command:
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        os.execvpe(command[0], command, environment)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
