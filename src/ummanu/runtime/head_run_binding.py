"""Immutable provider/continuation identity, independent of lifecycle and pane address."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def head_run_binding(value: Any) -> tuple[str, str]:
    """Bind a serialized HeadRun using the deployed provider's immutable contract.

    Codex v1 hashes exactly seven spec fields and must never grow with the persisted spec (limits,
    runtime and scope generation are enforced by the lifecycle owner). Other adapters hash the
    serialized spec. Persisted digests are never rewritten or matched via an alternate hash. See
    docs/HEAD_SCOPES.md "Provider identity and scope ownership".
    """
    if not isinstance(value, dict):
        return "", ""
    run_id = value.get("run_id")
    workspace = value.get("workspace")
    task_ref = value.get("task_ref")
    spec = value.get("spec")
    if not isinstance(run_id, str) or not run_id or not isinstance(workspace, str) or not workspace:
        return "", ""
    if not isinstance(task_ref, dict) or not isinstance(spec, dict):
        return "", ""
    if spec.get("adapter") == "codex":
        spec = {
            "profile_id": spec.get("profile_id"),
            "adapter": spec["adapter"],
            "model": spec.get("model") or "",
            "effort": spec.get("effort", "default"),
            "resource": spec.get("resource") or "",
            "codex_mode": spec.get("codex_mode") or "",
            "fallback": spec.get("fallback", []),
        }
    stable = {
        "run_id": run_id,
        "workspace": workspace,
        "task_ref": task_ref,
        "role": str(value.get("role") or ""),
        "spec": spec,
    }
    encoded = json.dumps(stable, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return run_id, hashlib.sha256(encoded.encode("ascii")).hexdigest()[:32]
