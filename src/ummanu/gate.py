"""Clean-worktree onboarding gate and the sole binding enable transition."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import yaml

from ummanu._fsutil import file_lock, publish_state_atomic
from ummanu.config import ConfigError, DataDirError, load_config, validate
from ummanu.onboarding import (
    IDENTITY_FIELDS,
    OnboardingStorage,
    ScannerError,
    scan_repo,
)
from ummanu.provision import _instance_dir, _load_inputs, _run_id

_SECRET = re.compile(
    r"(?is)(-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----"
    r"|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
    r"|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9_]+"
    r"|(?:token|password|secret|api[_-]?key)\s*[=:]\s*\S+)"
)
_TAIL = 4000
_COMMAND_TIMEOUT = 300
_GIT_TIMEOUT = 60


def run_gate(instance_value: str, project_id: str) -> tuple[int, dict[str, Any]]:
    instance = _instance_dir(instance_value)
    try:
        storage = OnboardingStorage.for_instance(instance)
    except DataDirError as exc:
        return 1, {
            "status": "storage_unavailable",
            "finding": _redact(f"onboarding storage is unavailable: {exc}"),
        }
    with file_lock(storage.lock(project_id)):
        return _run_gate_locked(instance, storage, project_id)


def _run_gate_locked(
    instance: Path, storage: OnboardingStorage, project_id: str
) -> tuple[int, dict[str, Any]]:
    binding_path = instance / "projects" / f"{project_id}.yaml"
    draft_path = storage.draft(project_id)
    try:
        existing_binding = load_config(binding_path)
        existing_draft = load_config(draft_path)
    except ConfigError:
        existing_binding = existing_draft = None
    if isinstance(existing_binding, dict) and existing_binding.get("enabled") is True:
        if isinstance(existing_draft, dict) and existing_draft.get("gate", {}).get("status") == "passed":
            adapter_path = instance / "adapters" / f"{existing_binding['adapter']}.yaml"
            try:
                current_head = scan_repo(Path(existing_binding["repo"]), existing_binding["default_branch"])[
                    "repo"
                ]["head"]
            except (OSError, KeyError, ScannerError, subprocess.TimeoutExpired):
                current_head = "unavailable"
            expected_head = existing_draft["scanner"]["repo"]["head"]
            if current_head != expected_head:
                return _disable_stale_enabled(
                    instance, storage, project_id, existing_binding, existing_draft, None
                )
            try:
                current_digest = "sha256:" + hashlib.sha256(adapter_path.read_bytes()).hexdigest()
            except OSError as exc:
                return 1, {
                    "status": "conflict",
                    "finding": _redact(exc.strerror or "canonical adapter is unavailable"),
                }
            provision_run = _run_id(existing_draft)
            expected_run = _gate_run_id(project_id, expected_head, provision_run, current_digest)
            expected_path = storage.gate_runs(project_id) / expected_run / "result.json"
            if expected_path.exists():
                try:
                    previous = _load_result(expected_path)
                except ConfigError:
                    return 1, {"status": "conflict", "finding": "current gate result is corrupt"}
                if previous.get("status") == "passed" and previous.get("adapter_digest") == current_digest:
                    return 0, previous
            for candidate_path in storage.gate_runs(project_id).glob("*/result.json"):
                try:
                    candidate = _load_result(candidate_path)
                except ConfigError:
                    continue
                revision = candidate.get("input_revision", {})
                if (
                    candidate.get("status") == "passed"
                    and revision.get("scanner_head") == expected_head
                    and revision.get("provision_run_id") == provision_run
                ):
                    return _disable_stale_enabled(
                        instance, storage, project_id, existing_binding, existing_draft, candidate
                    )
            return 1, {
                "status": "conflict",
                "finding": "enabled binding has no passed result for its current inputs",
            }
        return 1, {"status": "conflict", "finding": "enabled binding has no matching passed gate result"}
    loaded = _load_inputs(instance, storage, project_id)
    if loaded["status"] != "ok":
        return 1, loaded
    draft, binding = loaded["draft"], loaded["binding"]
    if draft["provision"]["status"] != "drafted":
        return 1, {"status": "conflict", "finding": "provision is not drafted"}
    adapter_path = instance / "adapters" / f"{draft['identity']['adapter']}.yaml"
    try:
        adapter_bytes = adapter_path.read_bytes()
        adapter = load_config(adapter_path)
    except (OSError, ConfigError) as exc:
        return 1, {"status": "conflict", "finding": _redact(str(exc))}
    errors = validate(adapter, "adapter", adapter_path.name)
    if errors:
        return 1, {"status": "conflict", "finding": "canonical adapter is invalid"}
    digest = "sha256:" + hashlib.sha256(adapter_bytes).hexdigest()
    provision_run = _run_id(draft)
    run_id = _gate_run_id(project_id, draft["scanner"]["repo"]["head"], provision_run, digest)
    result_path = storage.gate_runs(project_id) / run_id / "result.json"
    if result_path.exists():
        try:
            previous = _load_result(result_path)
        except ConfigError:
            return 1, _conflict_result(draft, run_id, provision_run, digest, "current gate result is corrupt")
        if previous.get("status") == "passed" and binding.get("enabled") is True:
            return 0, previous
        if previous.get("status") == "passed":
            return 1, _conflict_result(draft, run_id, provision_run, digest, "passed result is superseded")

    result = _base_result(draft, run_id, provision_run, digest)
    repo = Path(draft["identity"]["repo"])
    head = draft["scanner"]["repo"]["head"]
    try:
        current_head = scan_repo(repo, draft["identity"]["default_branch"])["repo"]["head"]
    except (OSError, KeyError, ScannerError, subprocess.TimeoutExpired):
        current_head = "unavailable"
    if current_head != head:
        return _publish_stale(result_path, result, "scanner HEAD changed")
    with tempfile.TemporaryDirectory(prefix="ummanu-gate-") as temp:
        worktree = Path(temp) / "worktree"
        try:
            add = subprocess.run(
                ["git", "-C", str(repo), "worktree", "add", "--detach", str(worktree), head],
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            add = _timed_out(exc)
        if add.returncode:
            return _publish_failure(result_path, result, "clean_worktree", add)
        try:
            result["checks"]["clean_worktree"] = {"status": "passed"}
            for command in adapter["setup"]["commands"]:
                outcome = _command(command, worktree)
                if outcome.returncode:
                    return _publish_failure(result_path, result, "setup", outcome)
            result["checks"]["setup"] = {"status": "passed"}
            outcome = _command(adapter["smoke"]["command"], worktree)
            if outcome.returncode:
                return _publish_failure(result_path, result, "smoke", outcome)
            result["checks"]["smoke"] = {"status": "passed"}
            validation = adapter["validation"]
            if validation["ci"] == "none":
                result["checks"]["validation"] = {"status": "declared-missing"}
                result["missing_coverage"] = list(validation["missing"])
            else:
                command = validation.get("command", "git diff --check HEAD")
                outcome = _command(command, worktree)
                if outcome.returncode:
                    return _publish_failure(result_path, result, "validation", outcome)
                result["checks"]["validation"] = {"status": "passed"}
            result["checks"]["artifact_policy"] = {"status": "passed"}
        finally:
            try:
                subprocess.run(
                    ["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)],
                    capture_output=True,
                    timeout=_GIT_TIMEOUT,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                pass

    latest = _load_inputs(instance, storage, project_id)
    if latest["status"] != "ok" or latest["draft"]["scanner"]["repo"]["head"] != head:
        return _publish_stale(result_path, result, "scanner or provision state changed")
    try:
        current_digest = "sha256:" + hashlib.sha256(adapter_path.read_bytes()).hexdigest()
    except OSError:
        current_digest = "unavailable"
    if current_digest != digest:
        return _publish_stale(result_path, result, "canonical adapter changed")

    updated = copy.deepcopy(draft)
    updated["gate"] = {
        "owner": "onboarding-gate",
        "status": "passed",
        "checks": {key: value["status"] for key, value in result["checks"].items()},
        "binding": {"enabled": True},
        "findings": [],
    }
    enabled = copy.deepcopy(binding)
    enabled["enabled"] = True
    result["status"] = "passed"
    contract_errors = validate(updated, "onboarding-contract", draft_path.name)
    result_errors = validate(result, "gate-result", result_path.name)
    if contract_errors or result_errors:
        result["status"] = "failed"
        result["findings"] = [{"code": "gate.failed", "message": "gate output is invalid"}]
        return _publish_result(result_path, result)
    writes = [
        (result_path, json.dumps(result, indent=2, sort_keys=True) + "\n"),
        (draft_path, yaml.safe_dump(updated, sort_keys=False)),
        (instance / "projects" / f"{project_id}.yaml", yaml.safe_dump(enabled, sort_keys=False)),
    ]
    try:
        publish_state_atomic(writes)
    except OSError as exc:
        result["status"] = "failed"
        result["findings"] = [{"code": "publication.failed", "message": _redact(exc.strerror or "I/O error")}]
        return 1, result
    return 0, result


def _base_result(draft: dict[str, Any], run_id: str, provision_run: str, digest: str) -> dict[str, Any]:
    return {
        "version": 1,
        "run_id": run_id,
        "identity": {field: copy.deepcopy(draft["identity"][field]) for field in IDENTITY_FIELDS},
        "input_revision": {
            "scanner_head": draft["scanner"]["repo"]["head"],
            "provision_run_id": provision_run,
        },
        "adapter_digest": digest,
        "status": "failed",
        "checks": {
            name: {"status": "not-run"}
            for name in ("clean_worktree", "setup", "smoke", "validation", "artifact_policy")
        },
        "findings": [],
    }


def _command(command: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            shell=True,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=_COMMAND_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return _timed_out(exc)


def _publish_failure(
    path: Path, result: dict[str, Any], stage: str, outcome: subprocess.CompletedProcess[str]
) -> tuple[int, dict[str, Any]]:
    result["checks"][stage] = {"status": "failed"}
    result["findings"] = [
        {
            "code": f"{stage}.failed",
            "message": f"{stage} command failed",
            "log_tail": _redact((outcome.stdout + outcome.stderr)[-_TAIL:]),
        }
    ]
    return _publish_result(path, result)


def _publish_stale(path: Path, result: dict[str, Any], message: str) -> tuple[int, dict[str, Any]]:
    result["status"] = "stale"
    result["findings"] = [{"code": "stale.input", "message": message}]
    return _publish_result(path, result)


def _publish_result(path: Path, result: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    try:
        publish_state_atomic([(path, json.dumps(result, indent=2, sort_keys=True) + "\n")])
    except OSError as exc:
        result["status"] = "failed"
        result["findings"] = [
            {
                "code": "publication.failed",
                "message": _redact(exc.strerror or "I/O error"),
            }
        ]
    return 1, result


def _timed_out(exc: subprocess.TimeoutExpired) -> subprocess.CompletedProcess[str]:
    stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout or ""
    stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr or ""
    return subprocess.CompletedProcess(exc.cmd, 124, stdout, stderr + "\ncommand timed out")


def _gate_run_id(project_id: str, head: str, provision_run: str, digest: str) -> str:
    payload = f"{project_id}\0{head}\0{provision_run}\0{digest}".encode()
    return "gate-" + hashlib.sha256(payload).hexdigest()[:20]


def _load_result(path: Path) -> dict[str, Any]:
    result = load_config(path)
    if not isinstance(result, dict) or not isinstance(result.get("input_revision", {}), dict):
        raise ConfigError("gate result and its input revision must be objects")
    return result


def _conflict_result(
    draft: dict[str, Any], run_id: str, provision_run: str, digest: str, message: str
) -> dict[str, Any]:
    result = _base_result(draft, run_id, provision_run, digest)
    result["status"] = "conflict"
    result["findings"] = [{"code": "result.conflict", "message": message}]
    return result


def _redact(value: str) -> str:
    return _SECRET.sub("[REDACTED]", value)


def _disable_stale_enabled(
    instance: Path,
    storage: OnboardingStorage,
    project_id: str,
    binding: dict[str, Any],
    draft: dict[str, Any],
    previous: dict[str, Any] | None,
) -> tuple[int, dict[str, Any]]:
    disabled = copy.deepcopy(binding)
    disabled["enabled"] = False
    updated = copy.deepcopy(draft)
    updated["gate"] = {
        "owner": "onboarding-gate",
        "status": "failed",
        "checks": {
            name: "not-run" for name in ("clean_worktree", "setup", "smoke", "validation", "artifact_policy")
        },
        "binding": {"enabled": False},
        "findings": [{"code": "stale.input", "severity": "error", "message": "enabled gate inputs changed"}],
    }
    stale = copy.deepcopy(previous) if previous is not None else {"status": "stale", "findings": []}
    stale["status"] = "stale"
    stale["findings"] = [{"code": "stale.input", "message": "enabled gate inputs changed"}]
    try:
        publish_state_atomic(
            [
                (instance / "projects" / f"{project_id}.yaml", yaml.safe_dump(disabled, sort_keys=False)),
                (storage.draft(project_id), yaml.safe_dump(updated, sort_keys=False)),
            ],
        )
    except OSError as exc:
        return 1, {"status": "publication_failed", "finding": _redact(exc.strerror or "I/O error")}
    return 1, stale
