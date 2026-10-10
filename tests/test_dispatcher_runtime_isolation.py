"""Dispatcher boundary tests for workspace Python and production provenance."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.fakes.dispatcher import FakeHost
from ummanu.broad_check import load_receipt, receipt_path, run_broad_check
from ummanu.dispatch import gate_lifecycle
from ummanu.dispatch.cleanup import CleanupJournal, CleanupOwner
from ummanu.dispatch.gate import GateResult
from ummanu.dispatch.gate_receipt import AcceptedGreenGate, mint_gate_receipt
from ummanu.dispatch.host import CommandHostRuntime
from ummanu.dispatch.runtime_provenance import RuntimeProvenance
from ummanu.dispatch.state import DispatcherRecord
from ummanu.dispatch.types import HostError
from ummanu.runtime.head import HeadRun, HeadSpec, TaskRef
from ummanu.runtime.head_runtimes import LOCAL_PTY_RUNTIME


def _observation(classification: str = "valid") -> RuntimeProvenance:
    return RuntimeProvenance(
        classification,
        sys.executable,
        "/registered/ummanu",
        "/registered/ummanu/src/ummanu/__init__.py",
        (),
    )


def _record(workspace: str = "") -> DispatcherRecord:
    return DispatcherRecord(
        worker="worker",
        workspace=workspace,
        handle="pane",
        head="codex",
        review_head="codex-reviewer",
        attempt_id="attempt",
        comment_baseline=0,
        review_baseline=0,
        state="reviewing",
        claimed_at=0,
    )


def _git_workspace(path: Path) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Fixture"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "fixture@example.invalid"], check=True)
    (path / "tracked").write_text("candidate\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "tracked"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "fixture"], check=True)


class _Runtime:
    interpreter = sys.executable
    product_root = "/registered/ummanu"

    def __init__(self, answers: list[RuntimeProvenance]) -> None:
        self.answers = answers
        self.calls = 0

    def probe(self) -> RuntimeProvenance:
        answer = self.answers[self.calls]
        self.calls += 1
        return answer


class _Host(CommandHostRuntime):
    def __init__(self, root: Path, runtime: _Runtime) -> None:
        super().__init__(SimpleNamespace(), root, mode="real", production_runtime=runtime)  # type: ignore[arg-type]
        self.effects: list[str] = []
        self.environment_checks: list[str] = []

    def _decide_workspace_environment_ownership(self, workspace: str) -> str:
        self.environment_checks.append(workspace)
        return super()._decide_workspace_environment_ownership(workspace)

    def stop_workspace(self, record: DispatcherRecord) -> None:
        self.effects.append("stop")

    def _run_json(self, args: list[str]) -> dict:
        self.effects.append("remove")
        return {}


class DispatcherRuntimeIsolationTests(unittest.TestCase):
    def test_recording_host_observes_worker_vitality_during_gate_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            host = FakeHost(Path(tmp))
            runtime = SimpleNamespace(host=host)
            observation = {"pid_status": {"state": "live-match"}}
            with (
                mock.patch.object(host, "worker_status", return_value=observation) as status,
                mock.patch.object(gate_lifecycle, "_reduce_and_store_vitality_episode",
                                  return_value=mock.sentinel.episode) as reduce,
            ):
                record = _record()
                episode = gate_lifecycle._worker_vitality_for_gate(
                    runtime, {"ref": "sample-1"}, record, {}, {})
            self.assertIs(episode, mock.sentinel.episode)
            status.assert_called_once_with({"ref": "sample-1"}, record)
            self.assertIs(reduce.call_args.args[5], observation)

    def test_recording_host_requires_and_preserves_executed_gate_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            host = FakeHost(Path(tmp))
            host.commit = "a" * 40
            receipt = mint_gate_receipt(
                validated_sha=host.head_commit(_record()), base_sha="b" * 40,
                gate_mode="github", required_checks=[{"name": "test", "conclusion": "SUCCESS"}],
                check_set_identity="fixture complete gate",
            )
            accepted = AcceptedGreenGate.accept(
                receipt, current_sha=host.head_commit(_record()), gate_mode="github", noop=host.mode == "noop")
            self.assertTrue(accepted.valid)
            self.assertEqual(accepted.persisted_payload(), receipt)
            missing = AcceptedGreenGate.accept(
                None, current_sha=host.head_commit(_record()), gate_mode="github", noop=host.mode == "noop")
            self.assertFalse(missing.valid)

    def test_gate_is_fenced_before_and_after_backend_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _Runtime([_observation(), _observation()])
            host = _Host(Path(tmp), runtime)
            with mock.patch(
                "ummanu.dispatch.host._gate_check", return_value=GateResult("green", "fixture")
            ) as gate:
                result = host.gate_check({}, _record(str(Path(tmp) / "task")))
        self.assertEqual(result.status, "green")
        self.assertEqual(runtime.calls, 2)
        self.assertEqual(host.environment_checks, [str(Path(tmp) / "task")])
        gate.assert_called_once()

    def test_cleanup_refuses_after_stop_but_before_worktree_removal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _Runtime([_observation(), _observation("workspace_targeted_editable")])
            host = _Host(Path(tmp), runtime)
            record = _record(str(Path(tmp) / "task"))
            run = HeadRun(run_id="fixture-run", spec=HeadSpec(profile_id="fixture", adapter="unknown", runtime=LOCAL_PTY_RUNTIME),
                          workspace=record.workspace, task_ref=TaskRef.card("ummanu-1"))
            backend = SimpleNamespace(stop=lambda run, initiator: (host.effects.append("stop") or SimpleNamespace(
                ok=True, run=run.finishing(initiator).exited())))
            host.head_runtime_for = lambda run: backend
            owner = CleanupOwner(SimpleNamespace(data_dir=Path(tmp), host=host))
            intent = {"task": {"ref": "ummanu-1"}, "record": {"workspace": record.workspace}, "heads": [run.to_json()]}
            owner._stop(intent)
            with self.assertRaisesRegex(HostError, "workspace_targeted_editable"):
                owner._remove_workspace(intent, Path(tmp))
        self.assertEqual(host.effects, ["stop"])
        self.assertEqual(host.environment_checks, [str(Path(tmp) / "task")])

    def test_release_is_fenced_before_and_after_its_effect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _Runtime([_observation(), _observation()])
            host = _Host(Path(tmp), runtime)
            host.catalog = SimpleNamespace(integration_base=lambda project, override: "main")
            with (
                mock.patch("ummanu.dispatch.host._validation_ci", return_value="github"),
                mock.patch.object(
                    host, "_merge_github_pr", side_effect=lambda *args: host.effects.append("merge")
                ),
            ):
                host.complete_green(
                    {"ref": "ummanu-1", "project": "ummanu", "workspace": {}},
                    _record(str(Path(tmp) / "task")),
                )
        self.assertEqual(runtime.calls, 2)
        self.assertEqual(host.effects, ["merge"])
        self.assertEqual(host.environment_checks, [str(Path(tmp) / "task")])

    def test_release_refusal_cannot_reach_the_merge_effect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _Runtime([_observation("wrong_root")])
            host = _Host(Path(tmp), runtime)
            with (
                mock.patch.object(host, "_merge_github_pr") as merge,
                self.assertRaisesRegex(HostError, "wrong_root"),
            ):
                host.complete_green(
                    {"ref": "ummanu-1", "project": "ummanu", "workspace": {}},
                    _record(str(Path(tmp) / "task")),
                )
        merge.assert_not_called()

    def test_workspace_prepare_creates_an_owned_interpreter_and_reuses_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "task"
            _git_workspace(workspace)
            runtime = _Runtime([_observation()])
            host = _Host(Path(tmp), runtime)
            host._prepare_workspace_environment(str(workspace))
            python = workspace / ".ummanu-task-env" / "venv" / "bin" / "python3"
            first = python.stat().st_ino
            host._prepare_workspace_environment(str(workspace))
            self.assertEqual(python.stat().st_ino, first)
            self.assertTrue(os.access(python, os.X_OK))

    def test_clean_env_child_of_the_workspace_venv_writes_bytecode_only_into_the_namespace(self) -> None:
        """secretary-1922: a child given a from-scratch env still caches bytecode inside the namespace."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "task"
            _git_workspace(workspace)
            source = workspace / "src"
            for name in ("plain", "by_env", "by_option"):
                (source / name).mkdir(parents=True)
                (source / name / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
            host = _Host(Path(tmp), _Runtime([_observation()]))
            host._prepare_workspace_environment(str(workspace))
            environment = workspace / ".ummanu-task-env" / "venv"
            (startup,) = environment.glob("lib/python3*/site-packages/00-ummanu-task-pycache.pth")
            namespace_cache = workspace.resolve() / ".ummanu-task-env" / "pycache"
            python = str(environment / "bin" / "python3")
            elsewhere = Path(tmp) / "elsewhere"

            def child(module: str, env: dict[str, str], *options: str) -> None:
                subprocess.run([python, *options, "-c", f"import {module}"], cwd=tmp, env=env, check=True)

            child("plain", {"PYTHONPATH": str(source)})
            # An explicit prefix, from the environment or the command line, still takes precedence.
            child("by_env", {"PYTHONPATH": str(source), "PYTHONPYCACHEPREFIX": str(elsewhere / "env")})
            child("by_option", {"PYTHONPATH": str(source)}, "-X", f"pycache_prefix={elsewhere / 'option'}")

            self.assertIn("sys.pycache_prefix or", startup.read_text(encoding="utf-8"))
            self.assertEqual(list(source.rglob("*.pyc")), [])
            self.assertEqual([path.name for path in source.rglob("__pycache__")], [])
            self.assertTrue(any((namespace_cache / source.resolve().relative_to("/") / "plain").glob("*.pyc")))
            self.assertFalse(any(namespace_cache.rglob("by_*")))
            self.assertTrue(any((elsewhere / "env").rglob("by_env/__init__*.pyc")))
            self.assertTrue(any((elsewhere / "option").rglob("by_option/__init__*.pyc")))

    def test_ready_environment_without_the_bytecode_redirect_is_accepted_and_gains_it(self) -> None:
        """secretary-1922: a venv made ready before the startup file existed still brings up."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "task"
            _git_workspace(workspace)
            host = _Host(Path(tmp), _Runtime([_observation()]))
            host._prepare_workspace_environment(str(workspace))
            environment = workspace / ".ummanu-task-env" / "venv"
            (startup,) = environment.glob("lib/python3*/site-packages/00-ummanu-task-pycache.pth")
            body = startup.read_text(encoding="utf-8")
            startup.unlink()
            python = environment / "bin" / "python3"
            first = python.stat().st_ino

            host._require_workspace_environment(str(workspace))
            host._prepare_workspace_environment(str(workspace))

            self.assertEqual(python.stat().st_ino, first)
            self.assertEqual(startup.read_text(encoding="utf-8"), body)

    def test_dispatcher_environment_is_disjoint_from_adapter_owned_dot_venv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "task"
            _git_workspace(workspace)
            adapter_environment = workspace / ".venv"
            adapter_environment.mkdir(parents=True)
            sentinel = adapter_environment / "adapter-owned"
            sentinel.write_text("untouched\n", encoding="utf-8")
            runtime = _Runtime([_observation()])
            host = CommandHostRuntime(  # type: ignore[arg-type]
                SimpleNamespace(), Path(tmp), mode="real", production_runtime=runtime
            )

            host._prepare_workspace_environment(str(workspace))

            dispatcher_python = workspace / ".ummanu-task-env" / "venv" / "bin" / "python3"
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "untouched\n")
            self.assertFalse(any(adapter_environment.rglob("_ummanu_production_dependencies.pth")))
            imported = subprocess.run(
                [str(dispatcher_python), "-I", "-c", "import ummanu"],
                cwd=tmp,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(imported.returncode, 0, "production packages leaked into task venv")

    def test_unclaimed_reserved_environment_is_never_adopted_or_mutated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "task"
            _git_workspace(workspace)
            environment = workspace / ".ummanu-task-env" / "venv"
            environment.mkdir(parents=True)
            sentinel = environment / "foreign"
            sentinel.write_text("untouched\n", encoding="utf-8")
            host = CommandHostRuntime(  # type: ignore[arg-type]
                SimpleNamespace(), Path(tmp), mode="real", production_runtime=_Runtime([_observation()])
            )

            with self.assertRaisesRegex(HostError, "ownership is unavailable"):
                host._prepare_workspace_environment(str(workspace))

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "untouched\n")

    def test_adapter_setup_receives_neither_dispatcher_nor_production_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "task"
            workspace.mkdir()
            catalog = SimpleNamespace(
                adapter=lambda project: {
                    "setup": {"commands": ["uv sync --locked"]},
                    "smoke": {"command": ".venv/bin/python -m tests.smoke"},
                }
            )
            runtime = _Runtime([_observation()])
            runtime.interpreter = "/opt/ummanu/.venv/bin/python3"
            host = CommandHostRuntime(  # type: ignore[arg-type]
                catalog, Path(tmp), mode="real", production_runtime=runtime
            )
            commands: list[str] = []

            with (
                mock.patch.dict(
                    os.environ,
                    {"PATH": "/opt/ummanu/.venv/bin:/usr/local/bin:/usr/bin"},
                    clear=True,
                ),
                mock.patch.object(
                    host,
                    "_run_shell",
                    side_effect=lambda command, cwd, label: commands.append(command),
                ),
            ):
                host._run_setup("adapter-project", str(workspace))

            self.assertEqual(len(commands), 2)
            for command in commands:
                self.assertIn("PATH=/usr/local/bin:/usr/bin", command)
                self.assertIn("unset VIRTUAL_ENV", command)
                self.assertNotIn(".ummanu-task-env", command)
                self.assertNotIn("/opt/ummanu/.venv/bin", command)

    def test_adapter_default_runtime_is_populated_from_candidate_dev_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "task"
            _git_workspace(workspace)
            (workspace / "pyproject.toml").write_text("[project]\nname = 'other-project'\n", encoding="utf-8")
            catalog = SimpleNamespace(
                adapter=lambda project: {
                    "broad_check": {"module": "tests.broad", "import_package": "ummanu"}
                }
            )
            host = CommandHostRuntime(  # type: ignore[arg-type]
                catalog, Path(tmp), mode="real", production_runtime=_Runtime([_observation()])
            )
            original_run = host._run
            commands: list[list[str]] = []

            def run(args: list[str], label: str, *, cwd: Path | None = None):
                commands.append(args)
                if label == "workspace candidate dependencies":
                    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
                return original_run(args, label, cwd=cwd)

            with (
                mock.patch.object(host, "_run", run),
                mock.patch.object(host, "_require_workspace_environment"),
            ):
                host._prepare_workspace_environment(str(workspace), project="other-project")

            candidate_python = str(workspace / ".ummanu-task-env" / "venv" / "bin" / "python3")
            self.assertIn([candidate_python, "-m", "pip", "install", "-e", ".[dev]"], commands)

    def test_install_output_is_recorded_as_exact_generated_bytes_only_when_new(self) -> None:
        """secretary-1920: what the editable install creates is the dispatcher's, nothing else is."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "task"
            _git_workspace(workspace)
            (workspace / ".gitignore").write_text("*.egg-info/\n", encoding="utf-8")
            (workspace / "pyproject.toml").write_text("[project]\nname = 'sample'\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(workspace), "add", "."], check=True)
            subprocess.run(["git", "-C", str(workspace), "commit", "-qm", "project"], check=True)
            metadata = workspace / "src" / "sample.egg-info"
            metadata.mkdir(parents=True)
            (metadata / "top_level.txt").write_text("author kept\n", encoding="utf-8")
            (workspace / "notes.txt").write_text("author notes\n", encoding="utf-8")
            catalog = SimpleNamespace(
                adapter=lambda project: {"broad_check": {"module": "tests.broad", "import_package": "sample"}}
            )
            host = CommandHostRuntime(  # type: ignore[arg-type]
                catalog, Path(tmp) / "data", mode="real", production_runtime=_Runtime([_observation()])
            )
            original_run = host._run
            written = {"PKG-INFO": b"Metadata-Version: 2.1\nName: sample\n", "SOURCES.txt": b"\xffbinary\n",
                       "top_level.txt": b"sample\n"}

            def run(args: list[str], label: str, *, cwd: Path | None = None):
                if label == "workspace candidate dependencies":
                    # The install's effect on the source tree, including a rewrite of an existing file.
                    for name, body in written.items():
                        (metadata / name).write_bytes(body)
                    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
                return original_run(args, label, cwd=cwd)

            with (
                mock.patch.object(host, "_run", run),
                mock.patch.object(host, "_require_workspace_environment"),
            ):
                host._prepare_workspace_environment(str(workspace), project="sample")

            generated = CleanupJournal(Path(tmp) / "data").read()["generated"]
            root = workspace.resolve()
            self.assertEqual(
                generated,
                {
                    str(root / "src/sample.egg-info" / name): hashlib.sha256(written[name]).hexdigest()
                    for name in ("PKG-INFO", "SOURCES.txt")
                },
            )

    def test_reserved_environment_is_locally_excluded_and_cannot_be_staged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = Path(tmp) / "other-project"
            _git_workspace(repository)
            workspace = Path(tmp) / "task-worktree"
            subprocess.run(
                ["git", "-C", str(repository), "worktree", "add", "-qb", "task", str(workspace)],
                check=True,
            )
            host = CommandHostRuntime(  # type: ignore[arg-type]
                SimpleNamespace(), Path(tmp), mode="real", production_runtime=_Runtime([_observation()])
            )

            host._prepare_workspace_environment(str(workspace))

            exclude = Path(
                subprocess.run(
                    ["git", "-C", str(workspace), "rev-parse", "--git-path", "info/exclude"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
            )
            self.assertIn(".ummanu-task-env/", exclude.read_text(encoding="utf-8").splitlines())
            status = subprocess.run(
                ["git", "-C", str(workspace), "status", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(status.stdout, "")
            host.verify_worker_result({}, _record(str(workspace)))
            subprocess.run(["git", "-C", str(workspace), "add", "-A"], check=True)
            staged = subprocess.run(
                ["git", "-C", str(workspace), "diff", "--cached", "--name-only"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(staged.stdout, "")

    def _clean_project_worktree(self, tmp: str) -> tuple[Path, Path]:
        """A linked task worktree of a project whose committed `.gitignore` is empty."""
        repository = Path(tmp) / "clean-project"
        _git_workspace(repository)
        (repository / ".gitignore").write_text("", encoding="utf-8")
        subprocess.run(["git", "-C", str(repository), "add", ".gitignore"], check=True)
        subprocess.run(["git", "-C", str(repository), "commit", "-qm", "empty ignore"], check=True)
        workspace = Path(tmp) / "clean-task-worktree"
        subprocess.run(
            ["git", "-C", str(repository), "worktree", "add", "-qb", "task", str(workspace)],
            check=True,
        )
        return repository, workspace

    @staticmethod
    def _ignored(workspace: Path, path: str) -> bool:
        return (
            subprocess.run(["git", "-C", str(workspace), "check-ignore", "-q", path], check=False).returncode
            == 0
        )

    @staticmethod
    def _exclude_file(workspace: Path) -> Path:
        located = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "--git-path", "info/exclude"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        exclude = Path(located)
        return exclude if exclude.is_absolute() else workspace / exclude

    def test_pipeline_written_paths_are_excluded_in_a_project_with_a_clean_gitignore(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, workspace = self._clean_project_worktree(tmp)
            host = CommandHostRuntime(  # type: ignore[arg-type]
                SimpleNamespace(), Path(tmp), mode="real", production_runtime=_Runtime([_observation()])
            )

            host._prepare_workspace_environment(str(workspace))

            for path in ("state/checks/broad-x.json", "TASK.md", ".ummanu-task-env/owner.json"):
                self.assertTrue(self._ignored(workspace, path), f"{path} must be excluded")
            (workspace / "TASK.md").write_text("task\n", encoding="utf-8")
            (workspace / "state" / "checks").mkdir(parents=True)
            (workspace / "state" / "checks" / "broad-x.json").write_text("{}\n", encoding="utf-8")
            status = subprocess.run(
                ["git", "-C", str(workspace), "status", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(status.stdout, "")
            self.assertEqual((workspace / ".gitignore").read_text(encoding="utf-8"), "")

    def test_same_names_deeper_in_the_project_stay_candidate_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, workspace = self._clean_project_worktree(tmp)
            host = CommandHostRuntime(  # type: ignore[arg-type]
                SimpleNamespace(), Path(tmp), mode="real", production_runtime=_Runtime([_observation()])
            )

            host._prepare_workspace_environment(str(workspace))

            for path in ("docs/TASK.md", "docs/state/checks/broad-x.json"):
                self.assertFalse(self._ignored(workspace, path), f"{path} belongs to the project")
            (workspace / "docs").mkdir()
            (workspace / "docs" / "TASK.md").write_text("project doc\n", encoding="utf-8")
            status = subprocess.run(
                ["git", "-C", str(workspace), "status", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(status.stdout, "?? docs/\n")

    def test_broad_receipt_is_written_in_a_clean_project_workspace_after_bring_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, workspace = self._clean_project_worktree(tmp)
            host = CommandHostRuntime(  # type: ignore[arg-type]
                SimpleNamespace(), Path(tmp), mode="real", production_runtime=_Runtime([_observation()])
            )

            host._prepare_workspace_environment(str(workspace))
            code, _ = run_broad_check("true", root=workspace, stream=StringIO())

            self.assertEqual(code, 0)
            self.assertIsNotNone(load_receipt(receipt_path(workspace, "true")))
            status = subprocess.run(
                ["git", "-C", str(workspace), "status", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(status.stdout, "")

    def test_repeated_bring_up_appends_only_the_missing_exclude_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, workspace = self._clean_project_worktree(tmp)
            exclude = self._exclude_file(workspace)
            # A foreign rule, a set an earlier bring-up only partly wrote, and no final newline.
            exclude.write_text("foreign-rule\n.ummanu-task-env/", encoding="utf-8")
            host = CommandHostRuntime(  # type: ignore[arg-type]
                SimpleNamespace(), Path(tmp), mode="real", production_runtime=_Runtime([_observation()])
            )

            host._prepare_workspace_environment(str(workspace))
            first = exclude.read_text(encoding="utf-8")
            host._prepare_workspace_environment(str(workspace))

            self.assertEqual(first, "foreign-rule\n.ummanu-task-env/\n/TASK.md\n/state/checks/\n/.ummanu-report/\n")
            self.assertEqual(exclude.read_text(encoding="utf-8"), first)

    def test_rework_prepares_a_missing_pre_upgrade_environment_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "task"
            workspace.mkdir()
            host = CommandHostRuntime(  # type: ignore[arg-type]
                SimpleNamespace(integration_base=lambda project, override: "main"),
                Path(tmp),
                mode="real",
                production_runtime=_Runtime([_observation()]),
            )
            order: list[str] = []
            task = {"ref": "ummanu-1", "project": "ummanu", "workspace": {}, "routing": {}}
            record = _record(str(workspace))

            with (
                mock.patch.object(host, "_require_project_available"),
                mock.patch.object(
                    host,
                    "_prepare_workspace_environment",
                    side_effect=lambda *args, **kwargs: order.append("prepare"),
                ),
                mock.patch.object(
                    host,
                    "_require_workspace_environment",
                    side_effect=lambda *args: order.append("require"),
                ),
                mock.patch.object(host, "_clear_report_bodies"),
                mock.patch.object(host, "_worker_task_doc", return_value="task\n"),
                mock.patch.object(
                    host, "_launch", side_effect=lambda *args, **kwargs: order.append("launch")
                ),
            ):
                host.restart_worker(task, record)

            self.assertEqual(order, ["prepare", "require", "launch"])

    def test_retained_review_prepares_a_missing_pre_upgrade_environment_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "task"
            workspace.mkdir()
            host = CommandHostRuntime(  # type: ignore[arg-type]
                SimpleNamespace(adapter=lambda project: {}), Path(tmp), mode="real",
                production_runtime=_Runtime([_observation()]),
            )
            order: list[str] = []
            task = {"ref": "ummanu-1", "project": "ummanu", "routing": {}}
            record = _record(str(workspace))
            document = Path(tmp) / "review.md"

            def launch(*args, **kwargs):
                order.append("launch")
                raise HostError("launch reached")

            with (
                mock.patch.object(host, "_require_project_available"),
                mock.patch.object(
                    host,
                    "_prepare_workspace_environment",
                    side_effect=lambda *args, **kwargs: order.append("prepare"),
                ),
                mock.patch.object(
                    host,
                    "_require_workspace_environment",
                    side_effect=lambda *args: order.append("require"),
                ),
                mock.patch.object(host, "_clear_body_file"),
                mock.patch.object(host, "_review_document", return_value=(document, "review")),
                mock.patch.object(host, "_launch", side_effect=launch),
                self.assertRaisesRegex(HostError, "launch reached"),
            ):
                host.start_review(task, record)

            self.assertEqual(order, ["prepare", "require", "launch"])


if __name__ == "__main__":
    unittest.main()
