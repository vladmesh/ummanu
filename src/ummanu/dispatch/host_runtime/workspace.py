"""Production host workspace implementation."""

from __future__ import annotations

import contextlib
import json
import os
import shlex
from pathlib import Path
from typing import Any

from ummanu._fsutil import write_text_atomic
from ummanu.dispatch.helpers import _legacy_worker_branch
from ummanu.dispatch.host_runtime.catalog import _same_repo
from ummanu.dispatch.launch import CAUSE_BASE_BRANCH_CONTRACT, CAUSE_WORKSPACE_CONTRACT
from ummanu.dispatch.types import HostError
from ummanu.infra import git_worktree
from ummanu.projects.integration_base import is_exact_sha as _is_exact_ref_sha
from ummanu.runtime.role_env import (
    WORKSPACE_ENV_DIR,
    WORKSPACE_EXCLUDES,
    WORKSPACE_NAMESPACE,
    WORKSPACE_PYCACHE_PTH,
    workspace_pycache_pth,
)

# One line per declared interpreter the workspace test guard left unguarded, in the namespace.
TEST_GUARD_SKIPS_FILE = "test-guard-skips"


class WorkspaceEnvironmentMixin:

    def _fetch_seed(self, repo: Path, seed: str, *, project: str) -> str:
        """Bring `seed` into the project checkout and return the start point a worktree is cut at."""
        if _is_exact_ref_sha(seed):
            self._remote_git_checked(project, repo, ["fetch", "origin"], "git fetch")
            try:
                self._run(["git", "-C", str(repo), "cat-file", "-e", f"{seed}^{{commit}}"], "git seed probe")
            except HostError:
                raise HostError(
                    f"seed commit {seed[:12]} is not on the project remote; the predecessor "
                    "candidate this card inherits was never published or has been removed",
                    bring_up_cause=CAUSE_BASE_BRANCH_CONTRACT,
                ) from None
            return seed
        self._remote_git_checked(project, repo, ["fetch", "origin", seed], "git fetch")
        return f"origin/{seed}"

    def _validate_resumable_workspace(self, task: dict[str, Any], workspace: str) -> None:
        """Accept only the registered project worktree on this card's worker branch."""
        if self.mode == "noop":
            return
        path = Path(workspace)
        if not path.is_dir():
            raise HostError("resume workspace is not a directory", bring_up_cause=CAUSE_WORKSPACE_CONTRACT)
        try:
            top_level = self._run(
                ["git", "-C", workspace, "rev-parse", "--show-toplevel"], "resume workspace git check"
            ).stdout.strip()
        except HostError as exc:
            raise HostError(
                "resume workspace is not a git worktree", bring_up_cause=CAUSE_WORKSPACE_CONTRACT
            ) from exc
        if not _same_repo(Path(top_level), path):
            raise HostError(
                "resume workspace git root does not match its expected path",
                bring_up_cause=CAUSE_WORKSPACE_CONTRACT,
            )
        branch = self._run(
            ["git", "-C", workspace, "branch", "--show-current"], "resume workspace branch check"
        ).stdout.strip()
        expected_branch = _legacy_worker_branch(task["ref"])
        if branch != expected_branch:
            raise HostError(
                f"resume workspace is on branch {branch or '(detached)'}, expected {expected_branch}",
                bring_up_cause=CAUSE_WORKSPACE_CONTRACT,
            )
        repo = Path(str(self.catalog.binding(task["project"])["repo"])).expanduser()
        if not repo.is_dir():
            raise HostError("resume project repo is unavailable")
        listing = self._run(
            ["git", "-C", str(repo), "worktree", "list", "--porcelain"], "resume workspace ownership check"
        ).stdout
        registered = {row["worktree"] for row in git_worktree.parse_list(listing) if "worktree" in row}
        if not any(_same_repo(Path(candidate), path) for candidate in registered):
            raise HostError(
                "resume workspace is not a registered worktree of the project repo",
                bring_up_cause=CAUSE_WORKSPACE_CONTRACT,
            )

    def _run_setup(self, project: str, workspace: str) -> None:
        if self.mode == "noop":
            return
        adapter = self.catalog.adapter(project)
        for command in adapter.get("setup", {}).get("commands", []):
            self._run_adapter_shell(str(command), Path(workspace), "setup command")
        smoke = adapter.get("smoke", {}).get("command")
        if smoke:
            self._run_adapter_shell(str(smoke), Path(workspace), "smoke command")

    @staticmethod
    def _workspace_environment(workspace: str | Path) -> Path:
        """The reserved dispatcher-owned environment; adapter-owned ``.venv`` is disjoint."""
        return Path(workspace) / WORKSPACE_ENV_DIR

    @classmethod
    def _workspace_environment_owner(cls, workspace: str | Path) -> Path:
        return cls._workspace_environment(workspace).parent / "owner.json"

    @classmethod
    def _workspace_environment_ready_file(cls, workspace: str | Path) -> Path:
        return cls._workspace_environment(workspace).parent / "ready"

    @classmethod
    def _workspace_environment_ready(cls, workspace: str | Path) -> bool:
        return cls._workspace_environment_ready_file(workspace).is_file()

    def _claim_workspace_environment(self, workspace: str) -> Path:
        """Determine dispatcher ownership before creating or populating its reserved environment."""
        root = Path(workspace).resolve(strict=True)
        self._exclude_workspace_environment(root)
        environment = self._workspace_environment(root)
        owner = self._workspace_environment_owner(root)
        expected = {"owner": "ummanu-dispatcher", "schema_version": 1, "workspace": str(root)}
        if self._decide_workspace_environment_ownership(workspace) == "dispatcher":
            return environment
        environment.parent.mkdir(parents=True)
        try:
            write_text_atomic(owner, json.dumps(expected, sort_keys=True) + "\n")
        except RuntimeError as exc:
            raise HostError(f"workspace Python environment ownership could not be written: {exc}") from None
        return environment

    def _exclude_workspace_environment(self, root: Path) -> None:
        """Keep everything the pipeline writes here out of this repository's candidate content."""
        located = self._run(
            ["git", "-C", str(root), "rev-parse", "--git-path", "info/exclude"],
            "workspace Git exclude",
            cwd=root,
        ).stdout.strip()
        if not located:
            raise HostError("workspace Git exclude path is unavailable")
        exclude = Path(located)
        if not exclude.is_absolute():
            exclude = root / exclude
        try:
            current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        except (OSError, UnicodeError) as exc:
            raise HostError(f"workspace Git exclude could not be read: {exc}") from None
        present = {line.strip() for line in current.splitlines()}
        missing = [pattern for pattern in WORKSPACE_EXCLUDES if pattern not in present]
        if not missing:
            return
        separator = "" if not current or current.endswith("\n") else "\n"
        try:
            write_text_atomic(exclude, current + separator + "".join(f"{pattern}\n" for pattern in missing))
        except RuntimeError as exc:
            raise HostError(f"workspace Git exclude could not be written: {exc}") from None

    def _candidate_environment_install_required(self, project: str) -> bool:
        """Use the adapter's existing default-interpreter choice, never a project-name heuristic."""
        if not project:
            return False
        broad_check = self.catalog.adapter(project).get("broad_check")
        return isinstance(broad_check, dict) and "interpreter" not in broad_check

    def _prepare_workspace_environment(self, workspace: str, *, project: str = "") -> None:
        """Create and populate only the environment the dispatcher has explicitly claimed."""
        if self.mode == "noop":
            return
        root = Path(workspace)
        environment = self._claim_workspace_environment(workspace)
        if self._workspace_environment_ready(workspace):
            self._require_workspace_environment(workspace)
            # A venv made ready before the startup file existed stays acceptable; it gains the file
            # here when its layout allows, and keeps working without it otherwise.
            self._install_workspace_pycache_prefix(root, environment, required=False)
            self._install_workspace_test_guards(root, project=project)
            return
        self._run(
            [self.production_runtime.interpreter, "-m", "venv", str(environment)],
            "workspace Python environment",
            cwd=root,
        )
        self._install_workspace_pycache_prefix(root, environment, required=True)
        if self._candidate_environment_install_required(project) and (root / "pyproject.toml").is_file():
            before = self._workspace_extra_files(root)
            self._run(
                [str(environment / "bin" / "python3"), "-m", "pip", "install", "-e", ".[dev,typecheck]"],
                "workspace candidate dependencies",
                cwd=root,
            )
            self._record_install_output(root, before)
        try:
            write_text_atomic(self._workspace_environment_ready_file(workspace), "ready\n")
        except RuntimeError as exc:
            raise HostError(f"workspace Python environment readiness could not be written: {exc}") from None
        self._require_workspace_environment(workspace)
        self._install_workspace_test_guards(root, project=project)

    def _install_workspace_test_guards(self, root: Path, *, project: str = "") -> None:
        """Guard the workspace venv, and the declared interpreter's venv when it is candidate-local.

        Each declared interpreter left unguarded gets one line in `TEST_GUARD_SKIPS_FILE`.
        """
        if self.mode == "noop":
            return
        from ummanu.runtime.test_guard import install_environment, install_workspace

        environments = [self._workspace_environment(root)]
        skipped: list[str] = []
        if project:
            declaration = self.catalog.adapter(project).get("broad_check", {})
            interpreter = declaration.get("interpreter") if isinstance(declaration, dict) else None
            if interpreter:
                prefix, reason = _optional_guard_prefix(root, str(interpreter))
                if prefix is not None:
                    environments.append(prefix)
                else:
                    skipped.append(f"optional interpreter {interpreter} not guarded: {reason}\n")
        try:
            install_workspace(root)
            for environment in dict.fromkeys(environments):
                install_environment(root, environment)
            write_text_atomic(root / WORKSPACE_NAMESPACE / TEST_GUARD_SKIPS_FILE, "".join(skipped))
        except (OSError, RuntimeError, ValueError) as exc:
            raise HostError(f"workspace test guard unavailable: {exc}") from None

    def _install_workspace_pycache_prefix(self, root: Path, environment: Path, *, required: bool) -> None:
        """Point every interpreter of the owned venv at the owned bytecode cache."""
        try:
            resolved = root.resolve(strict=True)
            namespace = (resolved / WORKSPACE_NAMESPACE).resolve(strict=True)
            found = [path for path in environment.glob("lib/python3*/site-packages") if path.is_dir()]
            inside = len(found) == 1 and found[0].resolve(strict=True).is_relative_to(namespace)
        except (OSError, RuntimeError, ValueError):
            inside = False
        if not inside:
            if required:
                raise HostError(f"workspace Python environment site-packages is unavailable at {environment}")
            return
        file = found[0] / WORKSPACE_PYCACHE_PTH
        body = workspace_pycache_pth(resolved)
        with contextlib.suppress(OSError, UnicodeError):
            if not file.is_symlink() and file.read_text(encoding="utf-8") == body:
                return
        try:
            write_text_atomic(file, body)
        except RuntimeError as exc:
            if required:
                raise HostError(f"workspace bytecode redirect could not be written: {exc}") from None

    def _workspace_extra_files(self, root: Path) -> set[str]:
        """Every untracked or ignored path in `root`'s source tree, outside the owned namespace."""
        status = self._run(
            ["git", "-C", str(root), "status", "--porcelain=v1", "--ignored", "--untracked-files=all", "-z",
             "--", ".", f":(exclude){WORKSPACE_NAMESPACE}"],
            "workspace generated files",
            cwd=root,
        ).stdout
        return {row[3:] for row in status.split("\0") if row[:2] in {"??", "!!"}}

    def _record_install_output(self, root: Path, before: set[str]) -> None:
        """Record what the editable install created in the source tree as exact generated bytes."""
        if self.mode != "real":
            return
        from ummanu.dispatch.cleanup import CleanupJournal
        journal = CleanupJournal(self.data_dir)
        for name in sorted(self._workspace_extra_files(root) - before):
            file = root.resolve() / name
            if file.is_file() and not file.is_symlink():
                journal.generated(file, file.read_bytes())

    @staticmethod
    def _workspace_python(workspace: str | Path) -> Path:
        return Path(workspace) / WORKSPACE_ENV_DIR / "bin" / "python3"

    def _decide_workspace_environment_ownership(self, workspace: str) -> str:
        """Classify the reserved namespace without requiring a pre-upgrade environment."""
        if self.mode == "noop":
            return "absent"
        root = Path(workspace).resolve(strict=False)
        environment = self._workspace_environment(root)
        namespace = environment.parent
        if not namespace.exists():
            return "absent"
        owner = self._workspace_environment_owner(root)
        expected = {"owner": "ummanu-dispatcher", "schema_version": 1, "workspace": str(root)}
        try:
            observed = json.loads(owner.read_text(encoding="utf-8"))
            inside = namespace.resolve(strict=True).is_relative_to(root)
        except (OSError, RuntimeError, UnicodeError, ValueError):
            raise HostError(f"workspace Python environment ownership is unavailable at {namespace}") from None
        if observed != expected or not inside:
            raise HostError(f"workspace Python environment ownership is invalid at {namespace}")
        return "dispatcher"

    def _require_workspace_environment(self, workspace: str) -> None:
        if self.mode == "noop":
            return
        environment = self._workspace_environment(workspace)
        python = self._workspace_python(workspace)
        try:
            root = Path(workspace).resolve(strict=True)
            inside = environment.resolve(strict=True).is_relative_to(root)
        except (OSError, RuntimeError, ValueError):
            inside = False
        if (
            self._decide_workspace_environment_ownership(workspace) != "dispatcher"
            or not inside
            or not self._workspace_environment_ready(workspace)
            or not os.access(python, os.X_OK)
        ):
            raise HostError(f"workspace Python environment is unavailable at {environment}")

    def _run_adapter_shell(self, command: str, cwd: Path, label: str) -> None:
        """Run adapter setup without lending it the production or dispatcher environment."""
        production_bin = Path(self.production_runtime.interpreter).parent.resolve(strict=False)
        safe_path = os.pathsep.join(
            entry
            for entry in os.environ.get("PATH", "").split(os.pathsep)
            if entry and Path(entry).resolve(strict=False) != production_bin
        )
        prefix = f"PATH={shlex.quote(safe_path)}; unset VIRTUAL_ENV; export PATH; "
        self._run_shell(prefix + command, cwd, label)


def _optional_guard_prefix(root: Path, interpreter: str) -> tuple[Path | None, str]:
    """The candidate-local venv prefix a declared interpreter may guard, or why it may not."""
    path = Path(interpreter)
    path = path if path.is_absolute() else root / path
    # Setup may not have created the adapter-owned venv yet; worker preparation repeats this
    # step after setup, and retained environments take this same path.
    if not path.is_file():
        return None, "interpreter is absent"
    if path.parent.name != "bin":
        return None, "interpreter is not in a bin directory"
    # Resolve the bin directory, not the executable symlink: a local venv normally links Python
    # to its system binary. External declarations stay supported but never receive guard writes.
    try:
        prefix = path.parent.resolve(strict=True).parent
        if not prefix.is_relative_to(root.resolve(strict=True)):
            return None, "prefix is outside the candidate"
        if not (prefix / "pyvenv.cfg").is_file():
            return None, "prefix has no pyvenv.cfg"
        sites = [site for site in prefix.glob("lib/python3*/site-packages") if site.is_dir()]
        if len(sites) != 1:
            return None, "prefix has no single site-packages"
        if not sites[0].resolve(strict=True).is_relative_to(prefix):
            return None, "site-packages escapes the prefix"
    except (OSError, RuntimeError, ValueError) as exc:
        return None, f"prefix is unresolvable ({type(exc).__name__})"
    return prefix, ""
