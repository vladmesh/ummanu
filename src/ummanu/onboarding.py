"""Deterministic repository scanning and disabled onboarding draft publication."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from ummanu._fsutil import (
    file_lock,
    publish_pair_and_remove_atomic,
    publish_pair_atomic,
)
from ummanu.config import ConfigError, DataDirError, load_config, validate
from ummanu.runtime.onboarding_storage import (
    ADAPTER_STORAGE as ADAPTER_STORAGE,
    DRAFT_STORAGE as DRAFT_STORAGE,
    OnboardingStorage as OnboardingStorage,
)
from ummanu.runtime.paths import default_instance_path

# The installation of a host that configured none. One spelling of the fallback, shared with the
# runtime, so the CLI and a launched role cannot disagree about which installation they mean.
DEFAULT_INSTANCE = str(default_instance_path())
IDENTITY_FIELDS = ("id", "repo", "adapter", "default_branch")
REQUIRED_DECISIONS = [
    "setup.commands",
    "smoke.command",
    "validation.ci",
    "artifact_policy.write_project_files",
]


class ScannerError(RuntimeError):
    """The repository could not be inspected deterministically."""


def project_add(
    repo_value: str,
    instance_value: str,
    *,
    dry_run: bool,
    re_onboard: bool = False,
) -> tuple[int, dict[str, Any]]:
    """Scan ``repo_value`` and optionally publish a disabled binding and draft.

    ``re_onboard`` is the operator's explicit request to take an enabled binding back down to
    the disabled draft state. It is the only supported way to re-onboard a legacy project that
    carries ``enabled: true`` and a canonical adapter but no draft, provision run or gate result.
    """
    repo = Path(repo_value).expanduser().resolve(strict=False)
    project_id = _project_id(repo.name)
    instance = Path(instance_value).expanduser()
    instance_dir = instance.parent if instance.name == "instance.yaml" else instance
    binding_path = instance_dir / "projects" / f"{project_id}.yaml"
    try:
        storage = OnboardingStorage.for_instance(instance_dir)
    except DataDirError as exc:
        default_branch = _default_branch(repo) if repo.is_dir() else "main"
        artifact = _base_artifact(repo, project_id, default_branch, _safe_scan(repo, default_branch))
        return 1, _fail_draft(artifact, "draft.invalid", f"onboarding storage is unavailable: {exc}")
    draft_path = storage.draft(project_id)
    if dry_run:
        return _project_add_locked(
            repo,
            project_id,
            instance_dir,
            binding_path,
            draft_path,
            dry_run=True,
            re_onboard=re_onboard,
        )
    with file_lock(storage.lock(project_id)):
        return _project_add_locked(
            repo,
            project_id,
            instance_dir,
            binding_path,
            draft_path,
            dry_run=False,
            re_onboard=re_onboard,
        )


def _project_add_locked(
    repo: Path,
    project_id: str,
    instance_dir: Path,
    binding_path: Path,
    draft_path: Path,
    *,
    dry_run: bool,
    re_onboard: bool = False,
) -> tuple[int, dict[str, Any]]:
    existing_binding, error = _load_optional_mapping(binding_path)
    if error:
        default_branch = _default_branch(repo) if repo.is_dir() else "main"
        artifact = _base_artifact(repo, project_id, default_branch, _safe_scan(repo, default_branch))
        return 1, _fail_draft(artifact, "draft.invalid", error)

    default_branch = _default_branch(repo) if repo.is_dir() else "main"
    if existing_binding:
        default_branch = str(existing_binding.get("default_branch", default_branch))

    identity = _identity(repo, project_id, default_branch)
    # Taking an enabled binding down is what ``--re-onboard`` asks for, and only that. On an
    # already disabled binding the flag changes nothing, so repeating the request republishes
    # the same bytes instead of wiping a provision run the operator has since applied.
    taking_down = bool(re_onboard) and bool(existing_binding) and existing_binding.get("enabled") is True
    # A disabled binding on another adapter -- a Phase-2 inventory binding (`adapter:
    # inventory-only`) the project now gets its own adapter for -- is moved onto this project's
    # adapter instead of refused. Nothing executes a disabled binding, so the move itself is
    # safe; what must not survive it is evidence produced for the previous adapter, which is
    # reset below. An enabled binding keeps refusing: switching what it executes is a takedown.
    retargeting = (
        bool(existing_binding)
        and existing_binding.get("enabled") is False
        and existing_binding.get("adapter") != identity["adapter"]
    )
    if existing_binding:
        conflict = _binding_conflict(
            existing_binding,
            identity,
            allow_enabled=dry_run or re_onboard,
            allow_adapter=retargeting,
        )
        if conflict:
            artifact = _base_artifact(repo, project_id, default_branch, _safe_scan(repo, default_branch))
            return 1, _fail_draft(artifact, "draft.invalid", conflict)

    scanner = _safe_scan(repo, default_branch)
    artifact = _base_artifact(repo, project_id, default_branch, scanner)
    artifact["identity"] = identity
    if scanner["status"] == "failed":
        return 1, artifact

    existing_draft, error = _load_optional_mapping(draft_path)
    if error:
        return 1, _fail_draft(artifact, "draft.invalid", error)
    if existing_draft:
        errors = validate(existing_draft, "onboarding-contract", draft_path.name)
        if errors:
            return 1, _fail_draft(artifact, "draft.invalid", str(errors[0]))
        draft_identity = existing_draft.get("identity")
        accepted = {_identity_core(identity)}
        if retargeting:
            # The draft written for the previous adapter. The new identity stays accepted too:
            # an interrupted retarget has published the draft but not yet the binding.
            accepted.add(_identity_core({**identity, "adapter": existing_binding["adapter"]}))
        if not isinstance(draft_identity, dict) or _identity_core(draft_identity) not in accepted:
            return 1, _fail_draft(
                artifact,
                "draft.invalid",
                "existing draft identity does not match binding",
            )
        artifact = existing_draft
        artifact["identity"] = identity
        old_head = artifact.get("scanner", {}).get("repo", {}).get("head")
        artifact["scanner"] = scanner
        if old_head and scanner.get("repo", {}).get("head") != old_head:
            _reset_scanner_derived_state(artifact)
    if taking_down:
        # The adapter that was executing under the enable is not evidence for the new draft:
        # provisioning and the gate start from pending again, on this scanner head. On an
        # unchanged head the run ids would otherwise repeat, so the new cycle enters them and
        # the previous run's result and gate receipt can neither be reapplied nor supersede.
        _reset_scanner_derived_state(artifact)
        artifact["onboarding_cycle"] = onboarding_cycle(artifact) + 1
    if retargeting:
        # A provision drafted and a gate run for the previous adapter vouch for nothing on this
        # one. Both go back to pending, and the run ids derive from the adapter, so the previous
        # adapter's results answer as foreign. Its canonical file is not this project's to delete.
        _reset_scanner_derived_state(artifact)

    # Updating a binding is a merge over what is already there, not a rebuild from a list of
    # fields we happen to remember. Identity is recomputed and overwrites; everything else the
    # operator or an earlier card put into the binding survives, including schema fields this
    # code does not name.
    binding = dict(existing_binding) if existing_binding else {}
    binding.update(identity)
    # A new project gets no orca_binding: it runs on local-pty heads in git workspaces. The merge
    # above keeps a legacy one an earlier onboarding wrote, which is what orca-legacy heads read.
    binding["enabled"] = False
    binding_errors = validate(binding, "project-binding", binding_path.name)
    artifact_errors = validate(artifact, "onboarding-contract", draft_path.name)
    if binding_errors or artifact_errors:
        problem = (binding_errors + artifact_errors)[0]
        return 1, _fail_draft(artifact, "draft.invalid", str(problem))

    if dry_run:
        return 0, artifact

    adapter_path = instance_dir / "adapters" / f"{identity['adapter']}.yaml"
    remove_stale_adapter = adapter_path.exists() and (
        taking_down or artifact.get("provision", {}).get("status") != "drafted"
    )
    try:
        binding_text = yaml.safe_dump(binding, sort_keys=False, allow_unicode=True)
        draft_text = yaml.safe_dump(artifact, sort_keys=False, allow_unicode=True)
        # The draft is published before the binding. A refusal or an I/O error rolls the whole
        # transition back, but an interruption has no rollback, and the binding is what the next
        # run reads to decide whether a takedown is still owed: writing it last keeps an
        # unfinished re-onboarding visible as the enabled binding it started from, so a retry
        # carries it through instead of mistaking the leftovers for a finished disabled state.
        if remove_stale_adapter:
            publish_pair_and_remove_atomic(
                draft_path,
                draft_text,
                binding_path,
                binding_text,
                adapter_path,
            )
        else:
            publish_pair_atomic(draft_path, draft_text, binding_path, binding_text)
    except OSError as exc:
        message = f"publication failed: {exc.strerror or 'I/O error'}"
        return 1, _fail_draft(artifact, "draft.invalid", message)
    return 0, artifact


def render_artifact(artifact: dict[str, Any]) -> str:
    return json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _project_id(name: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return value or "project"


def _identity(repo: Path, project_id: str, default_branch: str) -> dict[str, Any]:
    return {
        "id": project_id,
        "repo": str(repo),
        "adapter": project_id,
        "default_branch": default_branch,
    }


def _binding_conflict(
    binding: dict[str, Any],
    identity: dict[str, Any],
    *,
    allow_enabled: bool = False,
    allow_adapter: bool = False,
) -> str | None:
    if binding.get("enabled") is True and not allow_enabled:
        return "existing binding is enabled"
    for field in IDENTITY_FIELDS:
        if field == "adapter" and allow_adapter:
            continue
        if binding.get(field) != identity[field]:
            return f"existing binding has conflicting {field}"
    errors = validate(binding, "project-binding", "existing binding")
    return str(errors[0]) if errors else None


def onboarding_cycle(draft: dict[str, Any]) -> int:
    """The draft's re-onboarding generation. An absent value is the first cycle."""
    value = draft.get("onboarding_cycle")
    return value if isinstance(value, int) and value >= 1 else 1


def _identity_core(identity: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(identity.get(field) for field in IDENTITY_FIELDS)


def _load_optional_mapping(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.exists():
        return None, None
    try:
        value = load_config(path)
    except ConfigError as exc:
        return None, str(exc)
    if not isinstance(value, dict):
        return None, f"{path.name}: expected a mapping"
    return value, None


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.fsmonitor=false",
            "-C",
            str(repo),
            *args,
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=10,
    )
    if result.returncode:
        raise ScannerError("git inspection failed")
    return result.stdout.strip()


def _default_branch(repo: Path) -> str:
    if not repo.is_dir():
        return "main"
    try:
        remote = _git(
            repo,
            "symbolic-ref",
            "--quiet",
            "--short",
            "refs/remotes/origin/HEAD",
        )
        if remote:
            return remote.split("/", 1)[-1]
    except (OSError, ScannerError, subprocess.TimeoutExpired):
        pass
    try:
        branch = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
        return branch or "main"
    except (OSError, ScannerError, subprocess.TimeoutExpired):
        return "main"


def _safe_scan(repo: Path, branch: str) -> dict[str, Any]:
    if not repo.exists() or not repo.is_dir():
        return _failed_scanner("repo.missing")
    try:
        return scan_repo(repo, branch)
    except (OSError, ScannerError, subprocess.TimeoutExpired):
        return _failed_scanner("scanner.failed")


def scan_repo(repo: Path, branch: str) -> dict[str, Any]:
    """Return facts visible from the named local Git revision without running repo code."""
    ref = f"refs/heads/{branch}"
    try:
        head = _git(repo, "rev-parse", "--verify", ref)
    except ScannerError:
        head = _git(repo, "rev-parse", "--verify", "HEAD")
        current = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
        if current != branch:
            raise ScannerError("default branch is not available locally")
        ref = "HEAD"
    files = sorted(filter(None, _git(repo, "ls-tree", "-r", "--name-only", ref).splitlines()))
    clean = not bool(
        _git(
            repo,
            "status",
            "--porcelain",
            "--untracked-files=normal",
            "--ignore-submodules=all",
        )
    )
    facts = _facts(files)
    findings: list[dict[str, str]] = []
    if not facts["test_files"]:
        findings.append(
            {
                "code": "tests.not-observed",
                "severity": "warning",
                "message": "no test files observed",
            }
        )
    if not facts["ci_files"]:
        findings.append(
            {
                "code": "ci.not-observed",
                "severity": "warning",
                "message": "no CI files observed",
            }
        )
    return {
        "version": 1,
        "owner": "deterministic-scanner",
        "status": "ok",
        "repo": {"exists": True, "head": head, "worktree_clean": clean},
        "facts": facts,
        "findings": findings,
    }


def _facts(files: list[str]) -> dict[str, Any]:
    suffix_languages = {
        ".py": "python",
        ".js": "javascript",
        ".jsx": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".go": "go",
        ".rs": "rust",
        ".rb": "ruby",
        ".java": "java",
        ".kt": "kotlin",
        ".c": "c",
        ".h": "c",
        ".cc": "cpp",
        ".cpp": "cpp",
        ".sh": "shell",
        ".php": "php",
    }
    package_markers = {
        "pyproject.toml": "pip",
        "requirements.txt": "pip",
        "poetry.lock": "poetry",
        "package-lock.json": "npm",
        "package.json": "npm",
        "pnpm-lock.yaml": "pnpm",
        "yarn.lock": "yarn",
        "Cargo.toml": "cargo",
        "go.mod": "go",
        "Gemfile": "bundler",
        "pom.xml": "maven",
        "build.gradle": "gradle",
    }
    languages = sorted(
        {
            suffix_languages[PurePosixPath(name).suffix.lower()]
            for name in files
            if PurePosixPath(name).suffix.lower() in suffix_languages
        }
    )
    basenames = {PurePosixPath(name).name for name in files}
    managers = sorted({manager for marker, manager in package_markers.items() if marker in basenames})
    ci_files = [name for name in files if _is_ci(name)]
    test_files = [name for name in files if _is_test(name)]
    docs_files = [name for name in files if _is_doc(name)]
    setup_files = [
        name
        for name in files
        if PurePosixPath(name).name in package_markers
        or PurePosixPath(name).name in {"Makefile", "Dockerfile", "compose.yaml", "docker-compose.yml"}
    ]
    local_adapter = any(name in {".ummanu/adapter.yaml", "ummanu-adapter.yaml"} for name in files)
    return {
        "languages": languages,
        "package_managers": managers,
        "ci_files": ci_files,
        "test_files": test_files,
        "docs_files": docs_files,
        "setup_files": setup_files,
        "project_local_adapter": local_adapter,
    }


def _is_ci(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        name.startswith(".github/workflows/")
        or name in {".gitlab-ci.yml", ".circleci/config.yml", "Jenkinsfile", "azure-pipelines.yml"}
        or path.name == "buildkite.yml"
    )


def _is_test(name: str) -> bool:
    path = PurePosixPath(name)
    lowered = name.lower()
    return (
        any(part in {"test", "tests", "spec", "specs"} for part in path.parts[:-1])
        or bool(re.search(r"(^|[._-])(test|spec)([._-]|$)", path.name.lower()))
        or lowered.endswith("_test.go")
    )


def _is_doc(name: str) -> bool:
    path = PurePosixPath(name)
    return path.name.lower().startswith(("readme", "contributing", "changelog")) or (
        path.parts and path.parts[0].lower() in {"doc", "docs"}
    )


def _failed_scanner(code: str) -> dict[str, Any]:
    return {
        "version": 1,
        "owner": "deterministic-scanner",
        "status": "failed",
        "repo": {"exists": code != "repo.missing"},
        "facts": {
            "languages": [],
            "package_managers": [],
            "ci_files": [],
            "test_files": [],
            "docs_files": [],
            "setup_files": [],
            "project_local_adapter": False,
        },
        "findings": [{"code": code, "severity": "error", "message": "repository scan did not complete"}],
    }


def _unresolved_adapter() -> dict[str, Any]:
    return {"status": "unresolved", "required_decisions": list(REQUIRED_DECISIONS)}


def _base_artifact(repo: Path, project_id: str, branch: str, scanner: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": 1,
        "identity": _identity(repo, project_id, branch),
        "scanner": scanner,
        "draft": {
            "owner": "project-add",
            "binding": {"enabled": False},
            "adapter": _unresolved_adapter(),
            "findings": [],
        },
        "provision": {
            "owner": "provision-agent",
            "status": "pending",
            "binding": {"enabled": False},
            "adapter": _unresolved_adapter(),
            "findings": [],
        },
        "gate": {
            "owner": "onboarding-gate",
            "status": "pending",
            "checks": {
                "clean_worktree": "not-run",
                "setup": "not-run",
                "smoke": "not-run",
                "validation": "not-run",
                "artifact_policy": "not-run",
            },
            "binding": {"enabled": False},
            "findings": [],
        },
        "ownership": {
            "binding": {
                "draft_owner": "project-add",
                "enable_owner": "onboarding-gate",
                "initial_enabled": False,
            },
            "adapter": {
                "draft_owner": "project-add",
                "provision_owner": "provision-agent",
                "storage": DRAFT_STORAGE,
            },
            "enable_transition": {
                "only_when": "gate.status == passed",
                "forbidden_owners": ["deterministic-scanner", "project-add", "provision-agent"],
            },
        },
    }


def _reset_scanner_derived_state(artifact: dict[str, Any]) -> None:
    artifact["provision"] = {
        "owner": "provision-agent",
        "status": "pending",
        "binding": {"enabled": False},
        "adapter": _unresolved_adapter(),
        "findings": [],
    }
    artifact["gate"] = {
        "owner": "onboarding-gate",
        "status": "pending",
        "checks": {
            "clean_worktree": "not-run",
            "setup": "not-run",
            "smoke": "not-run",
            "validation": "not-run",
            "artifact_policy": "not-run",
        },
        "binding": {"enabled": False},
        "findings": [],
    }
    artifact["ownership"]["adapter"]["storage"] = DRAFT_STORAGE


def _fail_draft(artifact: dict[str, Any], code: str, message: str) -> dict[str, Any]:
    failed = json.loads(json.dumps(artifact))
    failed["draft"].setdefault("findings", []).append({"code": code, "severity": "error", "message": message})
    return failed
