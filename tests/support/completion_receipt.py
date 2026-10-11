"""Deterministic completion artifact fixture; no interpreter or suite is executed."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from ummanu.broad_check import SCHEMA_VERSION, CheckSpec, ContentIdentity, _receipt_digest, receipt_path

SHA = "a" * 40
TREE = "b" * 40


def declared_receipt(root: Path, instance: Path, *, module: str = "shared"):
    root.mkdir(exist_ok=True)
    (root / "app").mkdir(exist_ok=True)
    (root / "app/__init__.py").touch()
    (instance / "projects").mkdir(parents=True, exist_ok=True)
    (instance / "adapters").mkdir(exist_ok=True)
    (instance / "projects/other.yaml").write_text(json.dumps(
        {"id": "other", "enabled": True, "repo": str(root), "adapter": "other"}))
    declaration = {"module": module, "args": ["checks"] if module == "pytest" else ["--host"],
                   "import_package": "app", "interpreter": sys.executable,
                   "local": {"membership": "runner", "selector_args": [] if module == "pytest" else ["--"]}}
    (instance / "adapters/other.yaml").write_text(json.dumps({
        "setup": {"commands": ["true"]}, "smoke": {"command": "true"},
        "validation": {"ci": "github"}, "artifact_policy": {"write_project_files": False},
        "broad_check": declaration}))
    spec = CheckSpec.for_module(module, declaration["args"], interpreter=sys.executable, import_package="app")
    receipt = {
        "schema_version": SCHEMA_VERSION, "command": spec.identity, "command_shape": spec.shape,
        "check_set": spec.check_set, "command_or_check_set_digest": spec.digest,
        "cwd": str(root), "content_identity": ContentIdentity(TREE).as_dict(),
        "project_provenance": {"origin": "check-process", "python": sys.executable,
                               "environment_prefix": sys.prefix, "cwd": str(root),
                               "imported_package": "app", "imported_project": str(root / "app/__init__.py"),
                               "inside_workspace": True, "import_roots": [str(root), str(root / "src")]},
        "started_at": "2026-10-10T00:00:00+00:00", "ended_at": "2026-10-10T00:00:01+00:00",
        "duration_seconds": 1, "exit_code": 0, "signal": 0, "status": "complete",
        "incomplete_reason": "", "verdict": "passed", "parsed": {"tests": 2}, "tail": "OK",
    }
    path = receipt_path(root, spec)
    path.parent.mkdir(parents=True, exist_ok=True)
    save_receipt(path, receipt)
    return spec, path, receipt


def save_receipt(path, receipt):
    receipt["receipt_digest"] = _receipt_digest(receipt)
    path.write_text(json.dumps(receipt))
