"""Selected venv dependencies, receipts and import provenance."""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any

from ummanu import _proc
from ummanu.board.store import store_path
from ummanu.infra import process_receipts
from ummanu.infra.process_receipts import ReceiptError
from ummanu.installation.upgrade.checkout import (
    _git_tracked_digest,
    _touches,
)
from ummanu.installation.upgrade.types import (
    DEPENDENCY_PATHS,
    MEMORY_COMPONENT,
    UPGRADE_RECEIPT_ROOT,
    StepResult,
    UpgradeContext,
    _memory_unit_prefix,
    _upgrade_receipt_path,
)

RUFF_VERSION_RE = re.compile(r"^ruff==([^;\s]+)$")


def _snapshot_install(venv_python: Path) -> bool:
    """Is the product installed into this venv as a copy rather than as the checkout itself?

    A snapshot install is a silent liability: nothing that follows moves it, so the venv keeps
    answering with whatever the code looked like when it was taken. An editable install cannot drift
    that way, so finding a snapshot is itself a reason to reinstall, whether or not a dependency
    manifest moved.
    """
    for dist_info in (venv_python.parent.parent / "lib").glob("python*/site-packages/ummanu-*.dist-info"):
        try:
            direct_url = json.loads((dist_info / "direct_url.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return True
        return not bool(direct_url.get("dir_info", {}).get("editable"))
    return True


def _declared_ruff_version(product_root: Path) -> str:
    """Read the exact Ruff version the product's ``dev`` extra promises."""
    try:
        pyproject = tomllib.loads((product_root / "pyproject.toml").read_text(encoding="utf-8"))
        dependencies = pyproject["project"]["optional-dependencies"]["dev"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read the dev Ruff pin: {exc}") from None
    if not isinstance(dependencies, list):
        raise TypeError("the dev extra is not a dependency list")
    for dependency in dependencies:
        if isinstance(dependency, str) and (match := RUFF_VERSION_RE.fullmatch(dependency)):
            return match.group(1)
    raise ValueError("the dev extra declares no exact Ruff pin")


def _ruff_runtime_problem(product_root: Path, venv_python: Path) -> str:
    """Why this venv cannot run the Ruff version its checkout declares, if any."""
    try:
        expected = _declared_ruff_version(product_root)
    except (TypeError, ValueError) as exc:
        return str(exc)
    executable = venv_python.parent / "ruff"
    if not executable.is_file():
        return f"the pinned Ruff {expected} is missing"
    try:
        result = _proc.run([str(executable), "--version"], timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return f"the pinned Ruff {expected} cannot run"
    if result.returncode != 0:
        return f"the pinned Ruff {expected} cannot run"
    actual = (result.stdout or result.stderr).strip()
    if actual != f"ruff {expected}":
        return f"the installed Ruff reports {actual or 'no version'}, not {expected}"
    return ""


DEPENDENCY_RECEIPT_RELATIVE = UPGRADE_RECEIPT_ROOT / "dependency-receipt.json"

DEPENDENCY_RECEIPT_VERSION = 1

SHA256_RE = re.compile(r"[0-9a-f]{64}")

EXTRA_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")

REQUIREMENT_NAME_RE = re.compile(r"\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")


def _normalized_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _declared_extras(product_root: Path) -> dict[str, frozenset[str]]:
    """The checkout's optional extras, each as the distribution names it requires."""
    try:
        pyproject = tomllib.loads((product_root / "pyproject.toml").read_text(encoding="utf-8"))
        declared = pyproject["project"]["optional-dependencies"]
    except (OSError, UnicodeError, KeyError, TypeError, tomllib.TOMLDecodeError):
        return {}
    if not isinstance(declared, dict):
        return {}
    extras = {}
    for name, requirements in declared.items():
        if not isinstance(name, str) or not isinstance(requirements, list):
            continue
        extras[_normalized_name(name)] = frozenset(
            _normalized_name(match.group(1))
            for requirement in requirements
            if isinstance(requirement, str) and (match := REQUIREMENT_NAME_RE.match(requirement))
        )
    return extras


def _venv_distributions(venv: Path) -> frozenset[str]:
    """The distribution names installed into ``venv``, read from its ``*.dist-info`` directories."""
    return frozenset(
        _normalized_name(dist_info.name.split("-", 1)[0])
        for dist_info in (venv / "lib").glob("python*/site-packages/*.dist-info")
    )


def required_extras(context: UpgradeContext, venv: Path) -> tuple[str, ...]:
    """The extras `dependencies` installs: ``dev`` plus every extra this installation uses.

    Which extras an installation uses is a property of the installation, not of the manifest, and
    this is the one place that rule lives. ``memory`` is used when the memory unit is installed or
    active, because `ummanu-memory-mcp` runs from this venv and its pins are that extra's. Any
    declared extra is used when the venv already carries all of its distributions, so a reinstall
    never drops what an operator added. An extra the checkout does not declare is never asked for.
    """
    declared = _declared_extras(context.product_root)
    extras = {"dev"}
    unit = f"{_memory_unit_prefix(context.report)}service"
    if MEMORY_COMPONENT in declared and (
        context.units.installed(unit) is not None or context.units.is_active(unit)
    ):
        extras.add(MEMORY_COMPONENT)
    carried = _venv_distributions(venv)
    extras.update(
        name for name, distributions in declared.items() if distributions and distributions <= carried
    )
    return tuple(sorted(extras))


def _read_dependency_receipt(context: UpgradeContext, venv: Path) -> tuple[dict[str, Any] | None, str]:
    try:
        path = _upgrade_receipt_path(context, DEPENDENCY_RECEIPT_RELATIVE)
    except ReceiptError as exc:
        return None, f"the dependency receipt cannot be located: {exc}"
    payload, reason = process_receipts.load_receipt(path, "dependency receipt")
    if payload is None:
        return None, reason
    malformed = "the dependency receipt is malformed"
    if set(payload) != {"version", "venv", "dependency_sha256", "extras"}:
        return None, malformed
    version = payload["version"]
    digest = payload["dependency_sha256"]
    extras = payload["extras"]
    if type(version) is not int or version != DEPENDENCY_RECEIPT_VERSION:
        return None, malformed
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        return None, malformed
    if (
        not isinstance(extras, list)
        or len(extras) > 64
        or not all(isinstance(extra, str) and EXTRA_NAME_RE.fullmatch(extra) for extra in extras)
        or len(set(extras)) != len(extras)
    ):
        return None, malformed
    if not isinstance(payload["venv"], str):
        return None, malformed
    if payload["venv"] != str(venv.resolve()):
        return None, "the dependency receipt names another venv"
    return {"dependency_sha256": digest, "extras": tuple(sorted(extras))}, ""


def _write_dependency_receipt(
    context: UpgradeContext, venv: Path, digest: str, extras: tuple[str, ...]
) -> None:
    path = _upgrade_receipt_path(context, DEPENDENCY_RECEIPT_RELATIVE)
    payload = {
        "version": DEPENDENCY_RECEIPT_VERSION,
        "venv": str(venv.resolve()),
        "dependency_sha256": digest,
        "extras": list(extras),
    }
    process_receipts.write_private_receipt(
        path, payload, context.runtime_user, ReceiptError, "dependency receipt"
    )


def step_dependencies(context: UpgradeContext) -> StepResult:
    """Install the product into its venv when the venv does not match the checkout.

    The question is the venv's state, not this run's pull delta: a checkout moved by hand, by the
    dispatcher or by `--no-pull` has an empty delta and a stale venv all the same. So the step
    compares the dependency receipt the last successful install wrote — the tracked dependency
    manifests' digest, the extras and the venv — with the checkout and this installation's extras.
    A missing or unreadable receipt is a reason to install, never `unchanged`.
    """
    venv = context.product_root / ".venv"
    venv_python = venv / "bin" / "python"
    if not venv_python.is_file():
        return StepResult("dependencies", "skipped", "no .venv in the product checkout")
    try:
        digest = _git_tracked_digest(context.product_root, DEPENDENCY_PATHS)
    except ReceiptError as exc:
        return StepResult("dependencies", "failed", f"cannot compare the venv with the checkout: {exc}")
    extras = required_extras(context, venv)
    listed = ",".join(extras)
    compared = f"deps sha256 {digest[:12]}, extras {listed}"
    reasons = []
    receipt, receipt_reason = _read_dependency_receipt(context, venv)
    if receipt is None:
        reasons.append(receipt_reason)
    else:
        if receipt["dependency_sha256"] != digest:
            reasons.append(f"deps sha256 {receipt['dependency_sha256'][:12]} -> {digest[:12]}")
        if receipt["extras"] != extras:
            reasons.append(f"extras {','.join(receipt['extras']) or 'none'} -> {listed}")
    if _snapshot_install(venv_python):
        reasons.append("the venv holds a snapshot install")
    # Under `--dry-run` the checkout has not moved yet, so the digest cannot see a pending manifest
    # change; the plan can.
    if _touches(context.changed_paths, DEPENDENCY_PATHS):
        reasons.append("a dependency manifest moved")
    ruff_problem = _ruff_runtime_problem(context.product_root, venv_python)
    if ruff_problem:
        reasons.append(ruff_problem)
    if not reasons:
        return StepResult("dependencies", "unchanged", f"venv matches checkout ({compared})")
    reason = "; ".join(reasons)
    if context.dry_run:
        return StepResult(
            "dependencies", "changed", f"would reinstall the product into .venv ({compared}): {reason}"
        )
    try:
        _proc.run(
            [
                str(venv_python),
                "-m",
                "pip",
                "install",
                "--quiet",
                "-e",
                f"{context.product_root}[{listed}]",
            ],
            timeout=900,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        return StepResult("dependencies", "failed", f"pip install exited {exc.returncode}")
    except (OSError, subprocess.TimeoutExpired):
        return StepResult("dependencies", "failed", "pip install could not run")
    context.code_changed = True
    try:
        _write_dependency_receipt(context, venv, digest, extras)
    except ReceiptError as exc:
        return StepResult("dependencies", "failed", f"installed the product into .venv but {exc}")
    return StepResult(
        "dependencies",
        "changed",
        f"installed the product into .venv ({compared}) and wrote the dependency receipt: {reason}",
    )


def step_dependency_provenance(context: UpgradeContext) -> StepResult:
    """Prove core imports come from the selected production installation environment."""
    venv = context.product_root / ".venv"
    python = venv / "bin" / "python"
    if not python.is_file():
        path = store_path(context.instance_path)
        if path.exists() or path.is_symlink():
            return StepResult(
                "dependency-provenance",
                "failed",
                "a configured board store requires the selected product checkout's production venv",
            )
        return StepResult("dependency-provenance", "skipped", "no .venv in the product checkout")
    program = (
        "import importlib.util,json,pathlib,sys;"
        "names=('ummanu','psycopg','sqlalchemy','alembic');"
        "print(json.dumps({'prefix':sys.prefix,'origins':{n:str(pathlib.Path(importlib.util.find_spec(n).origin).resolve()) for n in names}}))"
    )
    try:
        completed = _proc.run([str(python), "-P", "-c", program], timeout=60)
        if completed.returncode:
            raise ValueError("the import probe failed")
        evidence = json.loads(completed.stdout)
        origins = evidence["origins"]
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return StepResult(
            "dependency-provenance",
            "failed",
            "could not import ummanu, psycopg, SQLAlchemy and Alembic from the production venv",
        )
    root = context.product_root.resolve()
    try:
        Path(origins["ummanu"]).resolve().relative_to(root)
        for name in ("psycopg", "sqlalchemy", "alembic"):
            Path(origins[name]).resolve().relative_to(venv.resolve())
    except (ValueError, TypeError):
        return StepResult(
            "dependency-provenance",
            "failed",
            "production imports escaped the selected product root or its venv",
        )
    detail = ", ".join(f"{name}={origins[name]}" for name in origins)
    return StepResult("dependency-provenance", "unchanged", detail)
