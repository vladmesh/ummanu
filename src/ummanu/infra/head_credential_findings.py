"""Doctor findings for a role head env through which gh or git reaches the owner's GitHub login."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path

from ummanu import _proc
from ummanu.infra.recovery_inventory import GIT_BYPASS_KEY_PATTERN, git_bypass_entries
from ummanu.runtime.role_env import GH_CONFIG_DIR_ENV, RoleEnvError, runtime_env

HEAD_CREDENTIAL_FINDING = "head_env_github_login"
INSPECTED_ROLES = ("worker", "reviewer")
# gh's answer when the config dir it reads holds no login for the host.
_GH_NO_TOKEN = "no oauth token found"


def head_env_credential_findings(role: str, env: Mapping[str, str]) -> list[dict[str, object]]:
    """One finding per gh login or git credential helper or URL rewrite that `env` resolves."""
    findings: list[dict[str, object]] = []
    hosts = str(Path(env[GH_CONFIG_DIR_ENV]) / "hosts.yml")
    # `gh auth token` resolves the login offline; its stdout is the token and is never kept.
    try:
        gh = _proc.run(["gh", "auth", "token", "--hostname", "github.com"], env=env, cwd="/", timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        findings.append(_finding(role, hosts, f"cannot run gh auth token: {exc}"))
    else:
        if gh.returncode == 0:
            findings.append(_finding(role, hosts, "gh resolves a GitHub login"))
        elif _GH_NO_TOKEN not in str(gh.stderr):
            findings.append(_finding(role, hosts, f"gh auth token exited {gh.returncode}: {str(gh.stderr).strip()}"))
    # The root directory keeps a repository's local config out of the answer.
    try:
        git = _proc.run(
            ["git", "config", "--show-origin", "--get-regexp", GIT_BYPASS_KEY_PATTERN],
            env=env, cwd="/", timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        findings.append(_finding(role, "git config", f"cannot run git config: {exc}"))
        return findings
    if git.returncode not in (0, 1):
        findings.append(_finding(role, "git config", f"git config exited {git.returncode}"))
        return findings
    for origin, kind, _value in git_bypass_entries(str(git.stdout)):
        findings.append(_finding(role, origin.removeprefix("file:"), f"git configuration names a {kind}"))
    return findings


def role_head_credential_findings(
    *, base_env: dict[str, str] | None = None, env_file: Path | str | None = None
) -> list[dict[str, object]]:
    """The worker and reviewer envs the dispatcher launches heads with, built and inspected."""
    findings: list[dict[str, object]] = []
    for role in INSPECTED_ROLES:
        try:
            env = runtime_env(role, base_env=base_env, env_file=env_file)
        except RoleEnvError as exc:
            findings.append(_finding(role, "runtime env", f"cannot build the role env: {exc}"))
            continue
        findings.extend(head_env_credential_findings(role, env))
    return findings


def _finding(role: str, path: str, reason: str) -> dict[str, object]:
    return {
        "code": HEAD_CREDENTIAL_FINDING,
        "role": role,
        "path": path,
        "message": f"{role} head env: {reason} ({path})",
    }
