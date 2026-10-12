"""Provision task/result handling for Phase 6 onboarding."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from ummanu._fsutil import file_lock, publish_pair_atomic, publish_state_atomic
from ummanu.config import ConfigError, DataDirError, load_config, validate
from ummanu.onboarding import (
    ADAPTER_STORAGE,
    IDENTITY_FIELDS,
    OnboardingStorage,
    onboarding_cycle,
    scan_repo,
)
from ummanu.runtime.paths import instance_dir as _instance_dir

ENVIRONMENT_SUMMARIES = {
    "dependency-missing": "required dependency is missing",
    "tool-unavailable": "required tool is unavailable",
    "permission-denied": "environment permission denied",
    "runtime-error": "provision runtime failed",
}


def start_provision(instance_value: str, project_id: str) -> tuple[int, dict[str, Any]]:
    instance = _instance_dir(instance_value)
    try:
        storage = OnboardingStorage.for_instance(instance)
    except DataDirError as exc:
        return 1, _storage_unavailable(exc)
    return _start_provision(instance, storage, project_id)


def _start_provision(
    instance: Path, storage: OnboardingStorage, project_id: str
) -> tuple[int, dict[str, Any]]:
    loaded = _load_inputs(instance, storage, project_id)
    if loaded["status"] != "ok":
        return 1, loaded
    draft = loaded["draft"]
    stale = _stale_reason(draft)
    if stale:
        return 1, _status("stale_input", run_id=_run_id(draft), **stale)
    task = _task_document(draft, loaded["binding"])
    errors = validate(task, "provision-task", "provision-task")
    if errors:
        return 1, _status("invalid_task", run_id=task["run_id"], errors=[str(e) for e in errors])
    task_path = storage.provision_runs(project_id) / task["run_id"] / "task.yaml"
    if not task_path.exists():
        task_path.parent.mkdir(parents=True, exist_ok=True)
        task_path.write_text(yaml.safe_dump(task, sort_keys=False), encoding="utf-8")
    return 0, {"status": "task_ready", "task_path": str(task_path), "task": task}


def apply_provision_result(
    instance_value: str,
    project_id: str,
    result_value: str | None = None,
) -> tuple[int, dict[str, Any]]:
    instance = _instance_dir(instance_value)
    try:
        storage = OnboardingStorage.for_instance(instance)
    except DataDirError as exc:
        return 1, _storage_unavailable(exc)
    with file_lock(storage.lock(project_id)):
        return _apply_provision_result_locked(instance, storage, project_id, result_value)


def _apply_provision_result_locked(
    instance: Path,
    storage: OnboardingStorage,
    project_id: str,
    result_value: str | None,
) -> tuple[int, dict[str, Any]]:
    loaded = _load_inputs(instance, storage, project_id)
    if loaded["status"] != "ok":
        return 1, loaded
    draft = loaded["draft"]
    run_id = _run_id(draft)
    task_path = storage.provision_runs(project_id) / run_id / "task.yaml"
    if not task_path.exists():
        code, started = _start_provision(instance, storage, project_id)
        if code:
            return code, started
        loaded = _load_inputs(instance, storage, project_id)
        if loaded["status"] != "ok":
            return 1, loaded
        draft = loaded["draft"]
        run_id = _run_id(draft)
    result_path = (
        Path(result_value) if result_value else storage.provision_runs(project_id) / run_id / "result.yaml"
    )
    try:
        result = load_config(result_path)
    except ConfigError as exc:
        return 1, _status("result_invalid", run_id=run_id, errors=[str(exc)])
    if not isinstance(result, dict):
        return 1, _status("result_invalid", run_id=run_id, errors=["result is not a mapping"])
    outcome = _validate_result(result, draft, run_id)
    if outcome and outcome["status"] not in {"environment_failed", "stale_input"}:
        return 1, outcome
    stale = _stale_reason(draft)
    if stale and (not outcome or outcome["status"] != "stale_input"):
        return 1, _status("stale_input", run_id=run_id, **stale)
    if outcome:
        if outcome["status"] == "environment_failed":
            if draft.get("provision", {}).get("status") == "drafted":
                return 1, _status(
                    "transition_conflict",
                    run_id=run_id,
                    current_status="drafted",
                    attempted_status="environment_failed",
                )
            failure = _record_provision_failure(
                storage,
                project_id,
                draft,
                "environment.failed",
                outcome["environment"]["summary"],
            )
            if failure:
                return 1, failure
        return 1, outcome
    adapter = result["adapter"]
    project_local = result.get("project_local_adapter", {})
    if project_local.get("proposed") and loaded["binding"].get("plane") != "project":
        return 1, _status(
            "result_invalid",
            run_id=run_id,
            errors=["project-local adapter proposal is only available for project plane"],
        )
    if project_local.get("proposed") and not project_local.get("requires_opt_in"):
        return 1, _status("result_invalid", run_id=run_id, errors=["project-local adapter requires opt-in"])
    if adapter.get("artifact_policy", {}).get("write_project_files"):
        return 1, _status("result_invalid", run_id=run_id, errors=["project file writes are out of scope"])

    updated = _draft_with_adapter(draft, adapter)
    adapter_path = instance / "adapters" / f"{draft['identity']['adapter']}.yaml"
    draft_path = storage.draft(project_id)
    latest = _load_inputs(instance, storage, project_id)
    if latest["status"] != "ok":
        return 1, latest
    if latest["draft"]["scanner"]["repo"]["head"] != draft["scanner"]["repo"]["head"]:
        return 1, _status(
            "stale_input",
            run_id=run_id,
            expected_scanner_head=draft["scanner"]["repo"]["head"],
            actual_scanner_head=latest["draft"]["scanner"]["repo"]["head"],
        )
    errors = validate(updated, "onboarding-contract", draft_path.name)
    if errors:
        return 1, _status("canonical_invalid", run_id=run_id, errors=[str(e) for e in errors])
    try:
        publish_pair_atomic(
            adapter_path,
            yaml.safe_dump(adapter, sort_keys=False),
            draft_path,
            yaml.safe_dump(updated, sort_keys=False),
        )
    except OSError as exc:
        return 1, _status("publication_failed", run_id=run_id, errors=[exc.strerror or "I/O error"])
    return 0, {
        "status": "drafted",
        "run_id": run_id,
        "adapter_path": str(adapter_path),
        "draft_path": str(draft_path),
        "binding_enabled": False,
    }


def render_result(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _storage_unavailable(exc: DataDirError) -> dict[str, Any]:
    return _status("storage_unavailable", errors=[f"onboarding storage is unavailable: {exc}"])


def _load_inputs(instance: Path, storage: OnboardingStorage, project_id: str) -> dict[str, Any]:
    draft_path = storage.draft(project_id)
    binding_path = instance / "projects" / f"{project_id}.yaml"
    try:
        draft = load_config(draft_path)
        binding = load_config(binding_path)
    except ConfigError as exc:
        status = "draft_missing" if not draft_path.exists() or not binding_path.exists() else "draft_invalid"
        return _status(status, errors=[str(exc)])
    if not isinstance(draft, dict) or not isinstance(binding, dict):
        return _status("draft_invalid", errors=["draft and binding must be mappings"])
    errors = validate(draft, "onboarding-contract", draft_path.name)
    errors += validate(binding, "project-binding", binding_path.name)
    if errors:
        return _status("draft_invalid", errors=[str(e) for e in errors])
    identity = draft.get("identity", {})
    if binding.get("enabled") is not False:
        return _status("draft_invalid", errors=["binding must remain disabled"])
    for field in IDENTITY_FIELDS:
        if binding.get(field) != identity.get(field):
            return _status("draft_invalid", errors=[f"binding {field} differs from draft identity"])
    if draft.get("scanner", {}).get("status") != "ok":
        return _status("draft_invalid", errors=["scanner did not complete"])
    if "head" not in draft.get("scanner", {}).get("repo", {}):
        return _status("draft_invalid", errors=["scanner head missing"])
    return {"status": "ok", "draft": draft, "binding": binding}


def _stale_reason(draft: dict[str, Any]) -> dict[str, Any] | None:
    identity = draft["identity"]
    expected = draft["scanner"]["repo"]["head"]
    try:
        current = scan_repo(Path(identity["repo"]), identity["default_branch"])["repo"]["head"]
    except Exception:  # noqa: BLE001
        return {"expected_scanner_head": expected, "actual_scanner_head": "unavailable"}
    if current != expected:
        return {"expected_scanner_head": expected, "actual_scanner_head": current}
    return None


def _run_id(draft: dict[str, Any]) -> str:
    identity = draft["identity"]
    scanner_head = draft["scanner"]["repo"]["head"]
    material = f"{identity['id']}\0{identity['repo']}\0{identity['adapter']}\0{identity['default_branch']}\0{scanner_head}"
    cycle = onboarding_cycle(draft)
    if cycle > 1:
        # The first cycle keeps its historical run ids: a draft published before re-onboarding
        # existed carries no cycle, and its published runs must stay addressable.
        material = f"{material}\0cycle={cycle}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return f"provision-{identity['id']}-{digest}"


def _task_document(draft: dict[str, Any], binding: dict[str, Any]) -> dict[str, Any]:
    identity = {key: draft["identity"][key] for key in IDENTITY_FIELDS}
    constraints: dict[str, Any] = {
        "binding_enabled": False,
        "adapter_storage": "external",
        "project_local_requires_opt_in": True,
        "project_local_allowed": binding.get("plane") == "project",
    }
    for field in ("plane", "policy"):
        if field in binding:
            constraints[field] = binding[field]
    adapter = draft["draft"]["adapter"]
    unresolved = adapter.get("required_decisions", []) if isinstance(adapter, dict) else []
    return {
        "version": 1,
        "run_id": _run_id(draft),
        "identity": identity,
        "input_revision": {"scanner_head": draft["scanner"]["repo"]["head"]},
        "scanner": {
            "facts": draft["scanner"]["facts"],
            "findings": draft["scanner"].get("findings", []),
        },
        "unresolved": list(unresolved),
        "constraints": constraints,
    }


def _validate_result(result: dict[str, Any], draft: dict[str, Any], run_id: str) -> dict[str, Any] | None:
    errors = validate(result, "provision-result", "provision-result")
    if errors:
        return _status("result_invalid", run_id=run_id, errors=[str(e) for e in errors])
    if result["run_id"] != run_id:
        return _status("result_foreign", run_id=run_id, errors=["run id does not match input"])
    if result["identity"] != {"id": draft["identity"]["id"], "adapter": draft["identity"]["adapter"]}:
        return _status("result_foreign", run_id=run_id, errors=["identity does not match input"])
    expected_head = draft["scanner"]["repo"]["head"]
    actual_head = result["input_revision"]["scanner_head"]
    if actual_head != expected_head:
        return _status(
            "stale_input",
            run_id=run_id,
            expected_scanner_head=expected_head,
            actual_scanner_head=actual_head,
        )
    if result["status"] == "environment_failed":
        if result["environment"]["run_id"] != run_id:
            return _status("result_foreign", run_id=run_id, errors=["environment run id does not match"])
        environment = dict(result["environment"])
        environment["summary"] = ENVIRONMENT_SUMMARIES[environment["code"]]
        return _status(
            "environment_failed",
            run_id=run_id,
            environment=environment,
        )
    if result["status"] == "stale_input":
        return _status("stale_input", run_id=run_id, **result["stale_input"])
    adapter = result.get("adapter", {})
    adapter_errors = validate(adapter, "adapter", "adapter")
    if adapter_errors:
        missing_ci = any(error.path == "validation" and "ci" in error.message for error in adapter_errors)
        status = "ci_undeclared" if missing_ci else "adapter_invalid"
        return _status(status, run_id=run_id, errors=[str(e) for e in adapter_errors])
    return None


def _draft_with_adapter(draft: dict[str, Any], adapter: dict[str, Any]) -> dict[str, Any]:
    updated = copy.deepcopy(draft)
    updated["provision"] = {
        "owner": "provision-agent",
        "status": "drafted",
        "binding": {"enabled": False},
        "adapter": copy.deepcopy(adapter),
        "findings": [],
    }
    updated["ownership"]["adapter"]["storage"] = ADAPTER_STORAGE
    return updated


def _record_provision_failure(
    storage: OnboardingStorage,
    project_id: str,
    draft: dict[str, Any],
    code: str,
    message: str,
) -> dict[str, Any] | None:
    updated = copy.deepcopy(draft)
    updated["provision"] = {
        "owner": "provision-agent",
        "status": "failed",
        "binding": {"enabled": False},
        "adapter": {
            "status": "unresolved",
            "required_decisions": [
                "setup.commands",
                "smoke.command",
                "validation.ci",
                "artifact_policy.write_project_files",
            ],
        },
        "findings": [{"code": code, "severity": "error", "message": message}],
    }
    if validate(updated, "onboarding-contract", "failure"):
        return _status(
            "canonical_invalid", run_id=_run_id(draft), errors=["environment failure draft is invalid"]
        )
    path = storage.draft(project_id)
    try:
        publish_state_atomic([(path, yaml.safe_dump(updated, sort_keys=False))])
    except OSError as exc:
        return _status("publication_failed", run_id=_run_id(draft), errors=[exc.strerror or "I/O error"])
    return None


def _status(status: str, **fields: Any) -> dict[str, Any]:
    data = {"status": status}
    data.update(fields)
    return data
