from __future__ import annotations

import contextlib
import getpass
import hashlib
import io
import json
import os
import pwd
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import venv
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.fakes.installation import (
    CARD,
    PRODUCT_ROOT,
    SPRINT,
    _checkpoint,
    _git,
    split_board,
    write_memory_metadata,
)
from tests.retired_board import RETIRED_STORE, STALE_FILE, legacy_runtime_lines, write_stale_leftovers
from ummanu import _proc, installation, restore_commands, secret_store, state_repo
from ummanu.checkpoint import CheckpointPusher
from ummanu.cli import main
from ummanu.config import InstanceReport
from ummanu.data import export_runs
from ummanu.host import CollectResult, HostInventory
from ummanu.host_apply import resolve_systemd_layout
from ummanu.installation import (
    InstallError,
    _clone_or_reuse,
    _ensure_installation_user,
    _product_root,
    check_prerequisites,
    install,
    materialize_checkpoint,
    materialize_pipeline_state,
    pipeline_state_path,
    provision_codex_home,
    provision_project_checkouts,
)
from ummanu.memory.config import MemoryConfig, index_matches, memory_config
from ummanu.projects.availability import ProjectAvailability
from ummanu.routing_journal import attempts
from ummanu.runtime_env import RuntimeEnvError
from ummanu.secret_words import RECOVERY_WORDS
from ummanu.upgrade import UpgradeResult, step_host


# The checkout these tests run out of, which is the one they have. Nothing resolves it for them:
# an install materializes the configured checkout or `~/ummanu`, and neither exists on a machine
# that only checked this branch out somewhere.
class InstallationTests(unittest.TestCase):
    def _recovery_divergence_fixture(self, root: Path) -> tuple[Path, Path, Path, str, str]:
        """A recovered checkout with one local commit while upstream advanced: diverged history."""
        source = root / "source"
        remote = root / "instance.git"
        target = root / "instance"
        source.mkdir()
        _git(source, "init", "-b", "main")
        _git(source, "config", "user.name", "Upstream fixture")
        _git(source, "config", "user.email", "upstream@example.invalid")
        (source / "upstream.txt").write_text("base\n", encoding="utf-8")
        _git(source, "add", ".")
        _git(source, "commit", "-m", "initial checkpoint")
        subprocess.run(
            ["git", "clone", "--bare", str(source), str(remote)],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            _clone_or_reuse(remote.as_uri(), target, recovery=True, dry_run=False),
            "cloned private instance remote",
        )
        (target / "local.txt").write_text("retained\n", encoding="utf-8")
        _git(target, "add", "local.txt")
        _git(
            target,
            "-c",
            f"user.name={state_repo.FALLBACK_IDENTITY[0]}",
            "-c",
            f"user.email={state_repo.FALLBACK_IDENTITY[1]}",
            "commit",
            "-m",
            "local commit",
        )
        local = state_repo.head(target)
        assert local is not None
        (source / "upstream.txt").write_text("upstream\n", encoding="utf-8")
        _git(source, "add", "upstream.txt")
        _git(source, "commit", "-m", "upstream checkpoint")
        _git(source, "push", str(remote), "main")
        upstream = subprocess.run(
            ["git", "--git-dir", str(remote), "rev-parse", "main"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return source, remote, target, local, upstream

    def test_recovery_reuse_is_fast_forward_only_and_never_merges(self):
        """No local history is merged with upstream any more: the head-registry pair is generated in
        the data directory and never committed, so recovery has no retained lineage to reconcile
        (ummanu-26). A diverged checkout is refused exactly as in a fresh install, both tips kept."""
        for recovery in (True, False):
            with self.subTest(recovery=recovery), tempfile.TemporaryDirectory() as temporary:
                _, remote, target, local, upstream = self._recovery_divergence_fixture(Path(temporary))
                if not recovery:
                    (target / ".ummanu-bootstrap").write_text("bootstrap\n", encoding="utf-8")
                    (target / ".git" / "info" / "exclude").write_text(".ummanu-bootstrap\n", encoding="utf-8")

                with self.assertRaisesRegex(InstallError, "fast-forward instance checkout"):
                    _clone_or_reuse(remote.as_uri(), target, recovery=recovery, dry_run=False)

                self.assertEqual(state_repo.head(target), local)
                self.assertEqual(
                    state_repo.git(target, ["rev-parse", "origin/main"], label="upstream").strip(), upstream
                )
                self.assertEqual(
                    subprocess.run(
                        ["git", "--git-dir", str(remote), "rev-parse", "main"],
                        check=True,
                        capture_output=True,
                        text=True,
                    ).stdout.strip(),
                    upstream,
                )
                for operation_state in ("MERGE_HEAD", "MERGE_MSG", "MERGE_MODE", "AUTO_MERGE"):
                    self.assertFalse((target / ".git" / operation_state).exists())

    def test_recovery_ownership_barrier_precedes_reused_checkpoint_git(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "instance"
            data = Path(temporary) / "data"
            key = secret_store.key_path(target)
            key.parent.mkdir(parents=True)
            key.write_text("not-secret-fixture-material\n", encoding="utf-8")
            key.chmod(0o600)
            report = SimpleNamespace(data_dir=data)
            events: list[str] = []
            args = SimpleNamespace(
                instance_dir=str(target),
                instance_remote="remote",
                installation_user=getpass.getuser(),
                recover=True,
                adopt=False,
                dry_run=False,
                runtime_env=None,
                product_root=str(PRODUCT_ROOT),
                bootstrap_credential_file=None,
                bootstrap_credential_stdin=False,
                recovery_phrase_file=None,
                recovery_phrase_stdin=False,
            )

            def barrier(*_args, **_kwargs):
                events.append("ownership")
                if events == ["ownership", "git", "ownership"]:
                    raise InstallError("cleanup ownership failed")

            def reuse(*_args, **_kwargs):
                events.append("git")
                raise InstallError("stop after ordering proof")

            with (
                mock.patch("ummanu.installation._ensure_installation_user"),
                mock.patch("ummanu.installation._validated_instance", return_value=report),
                mock.patch(
                    "ummanu.installation._establish_recovery_ownership_barrier",
                    side_effect=barrier,
                ),
                mock.patch("ummanu.installation._clone_or_reuse", side_effect=reuse),
            ):
                result = installation.install(args)

            self.assertEqual(events, ["ownership", "git", "ownership"])
            self.assertEqual(result.status, "failed")
            self.assertIn("stop after ordering proof", result.steps[-2].detail)
            self.assertIn("original failure is retained above", result.steps[-1].detail)

    def test_recovery_ownership_barrier_refuses_unsafe_key_shape_or_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance = root / "instance"
            data = root / "data"
            key = secret_store.key_path(instance)
            key.parent.mkdir(parents=True)
            key.write_text("fixture\n", encoding="utf-8")
            key.chmod(0o640)
            with self.assertRaisesRegex(InstallError, "mode 0600"):
                installation._establish_recovery_ownership_barrier(instance, data, None)
            key.unlink()
            target = root / "elsewhere"
            target.write_text("fixture\n", encoding="utf-8")
            key.symlink_to(target)
            with self.assertRaisesRegex(InstallError, "regular non-symlink"):
                installation._establish_recovery_ownership_barrier(instance, data, None)

    @unittest.skipUnless(os.geteuid() == 0, "requires a real root-to-runtime-user recovery fixture")
    def test_recovery_barrier_enables_real_child_git_and_key_loading(self):
        try:
            account = pwd.getpwnam("nobody")
        except KeyError:
            self.skipTest("fixture has no nobody user")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o755)
            instance = root / "instance"
            data = root / "data"
            instance.mkdir()
            data.mkdir()
            _git(instance, "init", "-b", "main")
            _git(instance, "config", "user.name", "Test")
            _git(instance, "config", "user.email", "test@example.invalid")
            (instance / "instance.yaml").write_text("version: 1\n", encoding="utf-8")
            _git(instance, "add", "instance.yaml")
            _git(instance, "commit", "-m", "fixture")
            phrase = " ".join(RECOVERY_WORDS[:16])
            secret_store.initialize_store(instance, phrase=phrase, actor="fixture")
            key = secret_store.key_path(instance)
            lock = state_repo._lock_path(instance)
            progress = data / installation.RECOVERY_PROGRESS_FILE
            progress.write_text('{"identity":"fixture"}\n', encoding="utf-8")
            run_state = root / "runtime" / "state"
            run_state.mkdir(parents=True)
            (run_state / "attempts.jsonl").write_text("{}\n", encoding="utf-8")
            os.chown(key, 0, 0)

            installation._establish_recovery_ownership_barrier(
                instance,
                data,
                account.pw_name,
                additional_paths=(run_state,),
            )

            identity = state_repo.git_child_identity(instance)
            self.assertEqual((identity.uid, identity.gid), (account.pw_uid, account.pw_gid))
            self.assertEqual(
                state_repo.git(
                    instance,
                    ["rev-parse", "--is-inside-work-tree"],
                    label="verify recovery child Git identity",
                ),
                "true\n",
            )
            child_source = root / "child-source"
            shutil.copytree(Path.cwd() / "src" / "ummanu", child_source / "ummanu")
            for staged in (child_source, *child_source.rglob("*")):
                mode = staged.stat().st_mode & 0o777
                staged.chmod(mode | (0o055 if staged.is_dir() else 0o044))
            completed = subprocess.run(
                [
                    "runuser",
                    "--user",
                    account.pw_name,
                    "--",
                    sys.executable,
                    "-c",
                    (
                        "import os,sys; from ummanu.secret_store import load_installation_key; "
                        "load_installation_key(sys.argv[1]); print(os.geteuid())"
                    ),
                    str(instance),
                ],
                cwd="/",
                env={**os.environ, "PYTHONPATH": str(child_source)},
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.stdout.strip(), str(account.pw_uid))
            info = key.lstat()
            self.assertEqual(
                (info.st_uid, info.st_gid, info.st_mode & 0o777),
                (account.pw_uid, account.pw_gid, 0o600),
            )
            for owned in (lock, progress, run_state, run_state / "attempts.jsonl"):
                info = owned.lstat()
                self.assertEqual((info.st_uid, info.st_gid), (account.pw_uid, account.pw_gid))

    def test_isolated_git_timeout_reaps_its_descendant_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            pid_file = Path(temporary) / "child.pid"
            script = (
                "import pathlib,subprocess,sys; "
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid)); child.wait()"
            )
            with self.assertRaises(subprocess.TimeoutExpired):
                _proc.run_isolated([sys.executable, "-c", script], timeout=0.2)
            child_pid = int(pid_file.read_text(encoding="utf-8"))
            for _ in range(100):
                if not Path(f"/proc/{child_pid}").exists():
                    break
                time.sleep(0.01)
            self.assertFalse(Path(f"/proc/{child_pid}").exists(), "timed-out clone descendant survived")
            print(f"timeout descendant cleanup: pid {child_pid} absent")

    def test_isolated_git_interrupt_reaps_its_descendant_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            pid_file = Path(temporary) / "child.pid"
            script = (
                "import pathlib,subprocess,sys; "
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid)); child.wait()"
            )
            interrupt = threading.Timer(0.2, os.kill, args=(os.getpid(), signal.SIGINT))
            interrupt.start()
            try:
                with self.assertRaises(KeyboardInterrupt):
                    _proc.run_isolated([sys.executable, "-c", script], timeout=10)
            finally:
                interrupt.cancel()
                interrupt.join()
            child_pid = int(pid_file.read_text(encoding="utf-8"))
            for _ in range(100):
                if not Path(f"/proc/{child_pid}").exists():
                    break
                time.sleep(0.01)
            self.assertFalse(Path(f"/proc/{child_pid}").exists(), "interrupted clone descendant survived")
            print(f"interrupt descendant cleanup: pid {child_pid} absent")

    def test_fresh_instance_clone_is_bounded_and_reuse_stays_shallow(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            remote = root / "instance.git"
            target = root / "instance"
            source.mkdir()
            _git(source, "init", "-b", "main")
            _git(source, "config", "user.name", "Test")
            _git(source, "config", "user.email", "test@example.invalid")
            payload = source / "historical.bin"
            for revision in range(8):
                payload.write_bytes(
                    b"".join(hashlib.sha256(f"{revision}:{block}".encode()).digest() for block in range(8192))
                )
                _git(source, "add", "historical.bin")
                _git(source, "commit", "-m", f"large history {revision}")
            payload.unlink()
            (source / "checkpoint").write_text("current\n", encoding="utf-8")
            _git(source, "add", "-A")
            _git(source, "commit", "-m", "current checkpoint")
            subprocess.run(
                ["git", "clone", "--bare", str(source), str(remote)],
                check=True,
                capture_output=True,
                text=True,
            )
            _git(remote, "gc")
            remote_bytes = sum(path.stat().st_size for path in (remote / "objects" / "pack").glob("*.pack"))
            remote_url = remote.as_uri()

            self.assertEqual(
                _clone_or_reuse(remote_url, target, recovery=True, dry_run=False),
                "cloned private instance remote",
            )

            def git(*args: str) -> str:
                return subprocess.run(
                    ["git", "-C", str(target), *args],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()

            first = git("rev-parse", "HEAD")
            self.assertEqual(git("rev-parse", "--is-shallow-repository"), "true")
            self.assertEqual(git("rev-list", "--count", "HEAD"), "1")
            self.assertEqual(git("rev-parse", "--abbrev-ref", "@{u}"), "origin/main")
            clone_bytes = sum(
                path.stat().st_size for path in (target / ".git" / "objects" / "pack").glob("*.pack")
            )
            self.assertLess(clone_bytes * 4, remote_bytes)

            (source / "checkpoint").write_text("advanced\n", encoding="utf-8")
            _git(source, "add", "checkpoint")
            _git(source, "commit", "-m", "advance checkpoint")
            _git(source, "push", str(remote), "main")
            self.assertEqual(
                _clone_or_reuse(remote_url, target, recovery=True, dry_run=False),
                "reused checkpoint checkout",
            )
            second = git("rev-parse", "HEAD")
            self.assertNotEqual(first, second)
            self.assertEqual(git("rev-parse", "--is-shallow-repository"), "true")
            self.assertEqual(git("rev-list", "--count", "HEAD"), "2")
            object_bytes = sum(
                path.stat().st_size for path in (target / ".git" / "objects").rglob("*") if path.is_file()
            )

            self.assertEqual(
                _clone_or_reuse(remote_url, target, recovery=True, dry_run=False),
                "reused checkpoint checkout",
            )
            self.assertEqual(git("rev-parse", "HEAD"), second)
            self.assertEqual(
                sum(
                    path.stat().st_size for path in (target / ".git" / "objects").rglob("*") if path.is_file()
                ),
                object_bytes,
            )
            print(
                "shallow recovery evidence: "
                f"remote_pack={remote_bytes} fresh_pack={clone_bytes} "
                f"fresh_commits=1 reused_commits=2 reused_objects={object_bytes} "
                "unchanged_objects=" + str(object_bytes)
            )

            _git(target, "config", "user.name", "Test")
            _git(target, "config", "user.email", "test@example.invalid")
            (target / "checkpoint").write_text("locally checkpointed\n", encoding="utf-8")
            _git(target, "add", "checkpoint")
            _git(target, "commit", "-m", "local checkpoint")
            pushed = CheckpointPusher(target, interval_seconds=0).push()
            self.assertEqual(pushed["status"], "pushed")
            self.assertEqual(
                subprocess.run(
                    ["git", "--git-dir", str(remote), "rev-parse", "main"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                git("rev-parse", "HEAD"),
            )

    def test_initial_clone_stages_validates_and_adopts_an_empty_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "instance"
            target.mkdir()

            def clone(_execution, staging, **kwargs):
                self.assertEqual(
                    kwargs["clone_args"],
                    ["--depth=1", "--single-branch", "--no-tags", "--no-local"],
                )
                self.assertEqual(staging.parent.stat().st_mode & 0o777, 0o700)
                staging.mkdir()

            with (
                mock.patch("ummanu.installation.RemoteExecution.run_clone", autospec=True, side_effect=clone),
                mock.patch("ummanu.installation._validate_initial_clone") as validate,
            ):
                installation._clone_instance("remote", target, bootstrap_credential=None)

            validate.assert_called_once()
            self.assertTrue(target.is_dir())
            self.assertEqual(list(root.glob(".instance.clone-*")), [])

    def test_initial_clone_failures_preserve_target_and_remove_staging(self):
        for failure in (InstallError("invalid clone"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                target = root / "instance"
                target.mkdir()

                def clone(_execution, staging, **_kwargs):
                    staging.mkdir()

                with (
                    mock.patch(
                        "ummanu.installation.RemoteExecution.run_clone", autospec=True, side_effect=clone
                    ),
                    mock.patch("ummanu.installation._validate_initial_clone", side_effect=failure),
                    self.assertRaises(type(failure)),
                ):
                    installation._clone_instance("remote", target, bootstrap_credential=None)
                self.assertTrue(target.is_dir())
                self.assertEqual(list(target.iterdir()), [])
                self.assertEqual(list(root.glob(".instance.clone-*")), [])

    def test_initial_clone_atomic_adoption_failure_preserves_empty_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "instance"
            target.mkdir()

            def clone(_execution, staging, **_kwargs):
                staging.mkdir()

            with (
                mock.patch("ummanu.installation.RemoteExecution.run_clone", autospec=True, side_effect=clone),
                mock.patch("ummanu.installation._validate_initial_clone"),
                mock.patch("ummanu.installation.os.replace", side_effect=OSError("fixture")),
                self.assertRaisesRegex(InstallError, "atomic replacement failed"),
            ):
                installation._clone_instance("remote", target, bootstrap_credential=None)
            self.assertTrue(target.is_dir())
            self.assertEqual(list(target.iterdir()), [])
            self.assertEqual(list(root.glob(".instance.clone-*")), [])

    def test_initial_clone_ownership_failure_prevents_adoption(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "instance"

            def clone(_execution, staging, **_kwargs):
                staging.mkdir()

            with (
                mock.patch("ummanu.installation.RemoteExecution.run_clone", autospec=True, side_effect=clone),
                mock.patch("ummanu.installation._validate_initial_clone"),
                mock.patch(
                    "ummanu.installation._set_installation_owner",
                    side_effect=InstallError("ownership handoff failed"),
                ),
                self.assertRaisesRegex(InstallError, "ownership handoff failed"),
            ):
                installation._clone_instance(
                    "remote", target, bootstrap_credential=None, installation_user="runtime"
                )
            self.assertFalse(target.exists())
            self.assertEqual(list(root.glob(".instance.clone-*")), [])

    def test_initial_clone_refuses_an_invalid_remote_default_branch_without_adoption(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            remote = root / "remote.git"
            target = root / "instance"
            source.mkdir()
            _git(source, "init", "-b", "main")
            _git(source, "config", "user.name", "Test")
            _git(source, "config", "user.email", "test@example.invalid")
            (source / "checkpoint").write_text("current\n", encoding="utf-8")
            _git(source, "add", "checkpoint")
            _git(source, "commit", "-m", "checkpoint")
            subprocess.run(
                ["git", "clone", "--bare", str(source), str(remote)],
                check=True,
                capture_output=True,
                text=True,
            )
            _git(remote, "symbolic-ref", "HEAD", "refs/heads/missing")

            with self.assertRaisesRegex(InstallError, "cloned branch"):
                installation._clone_instance(remote.as_uri(), target, bootstrap_credential=None)
            self.assertFalse(target.exists())
            self.assertEqual(list(root.glob(".instance.clone-*")), [])

    def test_existing_invalid_remote_and_dirty_checkout_are_untouched(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "instance"
            target.mkdir()
            (target / ".git").mkdir()
            with (
                mock.patch("ummanu.installation.state_repo.git", return_value="other\n"),
                self.assertRaisesRegex(InstallError, "different instance remote"),
            ):
                _clone_or_reuse("expected", target, recovery=True, dry_run=False)
            marker = target / "marker"
            marker.write_text("untouched", encoding="utf-8")
            with (
                mock.patch("ummanu.installation.state_repo.git", side_effect=("expected\n", " M marker\n")),
                self.assertRaisesRegex(InstallError, "local changes"),
            ):
                _clone_or_reuse("expected", target, recovery=True, dry_run=False)
            self.assertEqual(marker.read_text(encoding="utf-8"), "untouched")

    def test_prefixed_partial_git_directory_gets_cleanup_or_fresh_target_guidance(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "instance"
            (target / ".git" / "objects").mkdir(parents=True)
            marker = target / "partial-pack"
            marker.write_text("preserve", encoding="utf-8")

            with self.assertRaisesRegex(InstallError, "remove a failed partial target.*fresh"):
                _clone_or_reuse("remote", target, recovery=True, dry_run=False)
            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")

    def test_prerequisite_probe_reads_the_installation_s_board_store(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch("ummanu.installation.board_client") as selected,
            mock.patch("ummanu.installation.TaskReader") as reader,
        ):
            check_prerequisites(instance_dir=Path(tmp))

        self.assertEqual(selected.call_args.args, (Path(tmp),))
        self.assertNotIn("transport", selected.call_args.kwargs)
        self.assertIs(reader.call_args.args[0], selected.return_value)

    def test_only_an_absent_runtime_env_is_ignored_for_an_unlocked_store(self):
        target = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, target)
        args = SimpleNamespace(
            instance_dir=str(target),
            instance_remote="remote",
            installation_user=getpass.getuser(),
            recover=True,
            adopt=False,
            dry_run=True,
            runtime_env=None,
            product_root=str(PRODUCT_ROOT),
        )
        unlocked = installation.SecretRecovery(store_present=True, unlocked=True)
        with (
            mock.patch("ummanu.installation._ensure_installation_user"),
            mock.patch("ummanu.installation._clone_or_reuse", return_value="reused checkpoint checkout"),
            mock.patch("ummanu.installation._open_secret_store", return_value=unlocked),
            mock.patch("ummanu.installation.read_runtime_env", side_effect=RuntimeEnvError("unsafe mode")),
        ):
            result = install(args)

        self.assertFalse(result.ok)

    def test_recovery_materializes_pipeline_state_before_host_steps_can_start_units(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            events: list[str] = []

            def run(context, *, steps):
                events.append("post-host" if step_host in steps else "pre-host")
                return UpgradeResult()

            with (
                mock.patch("ummanu.installation.validate_instance", return_value=SimpleNamespace(ok=True)),
                mock.patch("ummanu.installation.check_product_runtime"),
                mock.patch(
                    "ummanu.installation.resolve_runtime_owner", return_value=("operator", root / "home")
                ),
                mock.patch("ummanu.installation.run_steps", side_effect=run),
            ):
                installation.materialize_host(
                    root / "instance", root / "product", before_host=lambda _context: events.append("restore")
                )

            self.assertEqual(events, ["pre-host", "restore", "post-host"])

    def test_pipeline_state_materialization_rebuilds_the_checkpointed_journal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance = root / "instance"
            source = instance / "state" / "runs"
            source.mkdir(parents=True)
            record = {"event": "claim", "reference": "ummanu-1"}
            (source / "runs.ndjson").write_text(
                json.dumps({"source": "runs.jsonl", "line": 1, "record": record}) + "\n",
                encoding="utf-8",
            )
            state_dir = root / "home" / "orca" / "workspaces" / "ummanu" / "pipeline" / "state" / "pipeline"

            first = materialize_pipeline_state(instance, state_dir)
            self.assertEqual((first.records, first.changed), (1, True))

            self.assertEqual(
                [
                    json.loads(line)
                    for line in (state_dir / "runs.jsonl").read_text(encoding="utf-8").splitlines()
                ],
                [record],
            )
            stamp = (state_dir / "runs.jsonl").stat().st_mtime_ns
            second = materialize_pipeline_state(instance, state_dir)
            self.assertEqual((second.records, second.changed), (1, False))
            self.assertEqual((state_dir / "runs.jsonl").stat().st_mtime_ns, stamp)

    def test_pipeline_state_path_honors_the_dispatcher_override(self):
        with tempfile.TemporaryDirectory() as temporary:
            override = Path(temporary) / "overridden-pipeline-state"
            with mock.patch.dict(os.environ, {"TA_PIPELINE_STATE_DIR": str(override)}):
                self.assertEqual(pipeline_state_path(Path(temporary) / "home"), override)

    def test_pipeline_state_materialization_refuses_to_overwrite_different_live_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance = root / "instance"
            source = instance / "state" / "runs"
            source.mkdir(parents=True)
            (source / "runs.ndjson").write_text(
                json.dumps({"source": "runs.jsonl", "line": 1, "record": {"event": "checkpoint"}}) + "\n",
                encoding="utf-8",
            )
            state_dir = root / "pipeline-state"
            state_dir.mkdir()
            journal = state_dir / "runs.jsonl"
            journal.write_text(json.dumps({"event": "live"}) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(InstallError, "does not extend the checkpoint"):
                materialize_pipeline_state(instance, state_dir)

            self.assertEqual(journal.read_text(encoding="utf-8"), json.dumps({"event": "live"}) + "\n")

    def test_pipeline_state_materialization_keeps_a_valid_live_append_and_ignores_blank_lines(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance = root / "instance"
            source = instance / "state" / "runs"
            source.mkdir(parents=True)
            canonical = [
                {"source": "runs.jsonl", "line": 1, "record": {"event": "claim"}},
                {"source": "runs.jsonl", "line": 3, "record": {"event": "review"}},
            ]
            (source / "runs.ndjson").write_text(
                "\n".join(json.dumps(record) for record in canonical) + "\n", encoding="utf-8"
            )
            state_dir = root / "pipeline-state"
            state_dir.mkdir()
            journal = state_dir / "runs.jsonl"
            journal.write_text(
                '{"event":"claim"}\n\n{"event":"review"}\n{"event":"release"}\n',
                encoding="utf-8",
            )

            result = materialize_pipeline_state(instance, state_dir)
            self.assertEqual((result.records, result.changed), (2, False))

            self.assertIn('{"event":"release"}', journal.read_text(encoding="utf-8"))

    def test_gapped_checkpoint_journal_round_trips_through_the_next_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance = root / "instance"
            source = instance / "state" / "runs"
            source.mkdir(parents=True)
            rows = [
                {"source": "runs.jsonl", "line": 1, "record": {"event": "claim"}},
                {"source": "runs.jsonl", "line": 3, "record": {"event": "review"}},
            ]
            canonical = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
            (source / "runs.ndjson").write_text(canonical, encoding="utf-8")
            state_dir = root / "pipeline-state"

            materialize_pipeline_state(instance, state_dir)
            export_runs(root / "data", state_dir=state_dir)

            self.assertEqual((state_dir / "runs.jsonl").read_text(encoding="utf-8").splitlines()[1], "")
            self.assertEqual((root / "data" / "runs" / "runs.ndjson").read_text(encoding="utf-8"), canonical)

    def test_missing_project_checkout_is_cloned_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            remote = root / "remote.git"
            target = root / "projects" / "demo"
            source.mkdir()
            _git(source, "init", "-b", "main")
            _git(source, "config", "user.name", "Test")
            _git(source, "config", "user.email", "test@example.invalid")
            (source / "README.md").write_text("demo\n", encoding="utf-8")
            _git(source, "add", ".")
            _git(source, "commit", "-m", "initial")
            subprocess.run(
                ["git", "clone", "--bare", str(source), str(remote)],
                check=True,
                capture_output=True,
                text=True,
            )
            binding = {
                "id": "demo",
                "repo": str(target),
                "remote": str(remote),
                "default_branch": "main",
            }

            first = provision_project_checkouts([binding], None, instance_dir=source)
            second = provision_project_checkouts([binding], None, instance_dir=source)
            self.assertEqual([row.outcome for row in first], ["cloned"])
            self.assertEqual([row.outcome for row in second], ["unchanged"])
            self.assertEqual((target / "README.md").read_text(encoding="utf-8"), "demo\n")

    def test_project_failures_are_isolated_and_rows_are_sanitized(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance = root / "instance"
            instance.mkdir()
            bindings = [
                {
                    "id": "first",
                    "repo": str(root / "first"),
                    "remote": "https://example.invalid/a",
                    "default_branch": "main",
                },
                {
                    "id": "middle",
                    "repo": str(root / "middle"),
                    "remote": "https://github.example/b",
                    "default_branch": "main",
                },
                {
                    "id": "healthy",
                    "repo": str(root / "healthy"),
                    "remote": str(root / "healthy.git"),
                    "default_branch": "main",
                },
            ]
            source = root / "source"
            source.mkdir()
            _git(source, "init", "-b", "main")
            _git(source, "config", "user.name", "Test")
            _git(source, "config", "user.email", "test@example.invalid")
            (source / "README").write_text("ok", encoding="utf-8")
            _git(source, "add", ".")
            _git(source, "commit", "-m", "initial")
            subprocess.run(
                ["git", "clone", "--bare", str(source), str(root / "healthy.git")],
                check=True,
                capture_output=True,
            )

            rows = provision_project_checkouts(bindings, None, instance_dir=instance)

            self.assertEqual([row.outcome for row in rows], ["failed", "failed", "cloned"])
            self.assertEqual([row.code for row in rows], ["unsupported-https", "unsupported-https", "cloned"])
            rendered = json.dumps([row.__dict__ for row in rows])
            self.assertNotIn("example.invalid/a", rendered)
            self.assertTrue((root / "healthy" / ".git").exists())

    def test_invalid_binding_target_and_collision_are_project_scoped(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance = root / "instance"
            instance.mkdir()
            collision = root / "collision"
            collision.write_text("leave untouched", encoding="utf-8")
            loop = root / "loop"
            loop.symlink_to(loop)
            rows = provision_project_checkouts(
                [
                    {"id": "binding"},
                    {
                        "id": "target",
                        "repo": str(loop / "checkout"),
                        "remote": str(root / "remote"),
                        "default_branch": "main",
                    },
                    {
                        "id": "collision",
                        "repo": str(collision),
                        "remote": str(root / "remote"),
                        "default_branch": "main",
                    },
                ],
                None,
                instance_dir=instance,
            )
            self.assertEqual(
                [row.code for row in rows], ["invalid-binding", "invalid-target", "target-collision"]
            )
            self.assertEqual(collision.read_text(encoding="utf-8"), "leave untouched")

    def test_project_timeout_removes_staging_and_propagates_operator_interrupt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance = root / "instance"
            instance.mkdir()
            binding = {
                "id": "slow",
                "repo": str(root / "slow"),
                "remote": str(root / "remote"),
                "default_branch": "main",
            }
            timeout = installation.CredentialError("clone: command timed out", code="timeout")
            with mock.patch("ummanu.installation.RemoteExecution.run_clone", side_effect=timeout):
                rows = provision_project_checkouts([binding], None, instance_dir=instance)
            self.assertEqual((rows[0].code, rows[0].retryable), ("timeout", True))
            self.assertFalse((root / "slow").exists())
            self.assertEqual(list(root.glob(".slow.clone-*")), [])
            with (
                mock.patch("ummanu.installation.RemoteExecution.run_clone", side_effect=KeyboardInterrupt),
                self.assertRaises(KeyboardInterrupt),
            ):
                provision_project_checkouts([binding], None, instance_dir=instance)

    def test_project_remote_failures_keep_typed_sanitized_codes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance = root / "instance"
            instance.mkdir()
            for code, retryable in (
                ("authentication", True),
                ("network", True),
                ("process", True),
                ("invalid-branch", False),
            ):
                binding = {
                    "id": code,
                    "repo": str(root / code),
                    "remote": "https://github.com/example/private.git",
                    "default_branch": "main",
                }
                failure = installation.CredentialError("contains-secret-value", code=code)
                with mock.patch("ummanu.installation.RemoteExecution.run_clone", side_effect=failure):
                    row = provision_project_checkouts([binding], None, instance_dir=instance)[0]
                self.assertEqual((row.code, row.retryable), (code, retryable))
                self.assertNotIn("contains-secret-value", row.reason)
                self.assertFalse((root / code).exists())

    def test_project_retry_only_clones_the_previous_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance = root / "instance"
            instance.mkdir()
            bindings = [
                {
                    "id": name,
                    "repo": str(root / name),
                    "remote": str(root / f"{name}.git"),
                    "default_branch": "main",
                }
                for name in ("one", "two", "three")
            ]
            calls: list[str] = []

            def clone(execution, target, **_kwargs):
                calls.append(target.parent.name)
                if target.parent.name.endswith("two.clone-fixture"):
                    raise installation.CredentialError("failed", code="network")
                target.mkdir()
                (target / ".git").mkdir()

            # Use deterministic staging names so the injected middle failure is clear.
            counter = iter(("one", "two", "three"))

            def staging(*_args, dir=None, **_kwargs):
                path = Path(dir) / f".{next(counter)}.clone-fixture"
                path.mkdir()
                return str(path)

            with (
                mock.patch("ummanu.installation.tempfile.mkdtemp", side_effect=staging),
                mock.patch("ummanu.installation.RemoteExecution.run_clone", autospec=True, side_effect=clone),
            ):
                first = provision_project_checkouts(bindings, None, instance_dir=instance)
            self.assertEqual([row.outcome for row in first], ["cloned", "failed", "cloned"])

            def repaired(execution, target, **_kwargs):
                calls.append(target.parent.name)
                target.mkdir()
                (target / ".git").mkdir()

            with mock.patch(
                "ummanu.installation.RemoteExecution.run_clone", autospec=True, side_effect=repaired
            ):
                second = provision_project_checkouts(bindings, None, instance_dir=instance)
            self.assertEqual([row.outcome for row in second], ["unchanged", "cloned", "unchanged"])

    def test_degraded_project_results_are_truthful_in_text_and_json(self):
        result = installation.InstallResult(
            projects=[
                installation.ProjectProvisionResult(
                    "missing",
                    "missing",
                    "github-https",
                    "failed",
                    "authentication",
                    "remote authentication failed",
                    True,
                )
            ]
        )
        result.add("status", "degraded", "core ready; rerun ummanu recover with the same inputs")
        self.assertEqual(result.status, "degraded")
        self.assertIn("status: degraded", result.render())
        args = SimpleNamespace(
            bootstrap_credential_stdin=False,
            recovery_phrase_stdin=False,
            json=True,
        )
        output = io.StringIO()
        with (
            mock.patch("ummanu.installation.install", return_value=result),
            contextlib.redirect_stdout(output),
        ):
            code = installation.run_install(args)
        payload = json.loads(output.getvalue())
        self.assertEqual((code, payload["status"]), (1, "degraded"))
        self.assertEqual(payload["projects"][0]["project_id"], "missing")

    def test_project_progress_persists_only_non_secret_outcomes_and_is_identity_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance = root / "instance"
            instance.mkdir()
            progress = root / "recovery-progress.json"
            secret = "credential-material"
            binding = {
                "id": "demo",
                "repo": str(root / "demo"),
                "remote": f"https://user:{secret}@github.com/example/private.git",
                "default_branch": "main",
            }
            rows = provision_project_checkouts(
                [binding],
                None,
                instance_dir=instance,
                progress_path=progress,
                recovery_identity="identity-one",
            )
            self.assertEqual(rows[0].code, "unsafe-remote")
            stored = progress.read_text(encoding="utf-8")
            self.assertNotIn(secret, stored)
            self.assertNotIn(str(root / "demo"), stored)
            self.assertEqual(
                installation._read_recovery_progress(progress, "identity-two"),
                {"version": 1, "identity": "identity-two"},
            )

    def test_recovery_identity_tracks_changed_added_and_removed_memory_facts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance = root / "instance"
            instance.mkdir()
            _checkpoint(instance, root / "data")
            facts = instance / "state" / "memory" / "facts"
            original = facts / "fact.md"
            progress = root / "recovery-progress.json"

            baseline = installation._recovery_identity(instance, [])
            self.assertEqual(installation._recovery_identity(instance, []), baseline)
            installation._write_recovery_progress(progress, baseline, memory="complete")
            self.assertEqual(
                installation._read_recovery_progress(progress, baseline)["memory"],
                "complete",
            )

            original.write_text("# changed fact\n", encoding="utf-8")
            changed = installation._recovery_identity(instance, [])
            self.assertNotEqual(changed, baseline)
            self.assertNotIn("memory", installation._read_recovery_progress(progress, changed))
            original.write_text("# recovered fact\n", encoding="utf-8")
            self.assertEqual(installation._recovery_identity(instance, []), baseline)

            added = facts / "nested" / "added.md"
            added.parent.mkdir()
            added.write_text("# added fact\n", encoding="utf-8")
            self.assertNotEqual(installation._recovery_identity(instance, []), baseline)
            added.unlink()
            added.parent.rmdir()
            self.assertEqual(installation._recovery_identity(instance, []), baseline)

            original.unlink()
            self.assertNotEqual(installation._recovery_identity(instance, []), baseline)

    def test_recovery_identity_length_delimits_fact_paths_types_and_contents(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance = root / "instance"
            instance.mkdir()
            _checkpoint(instance, root / "data")
            facts = instance / "state" / "memory" / "facts"
            (facts / "fact.md").unlink()
            first = facts / "a"
            first.write_bytes(b"b\0file\0Z")
            one_file = installation._recovery_identity(instance, [])

            first.write_bytes(b"")
            (facts / "b").write_bytes(b"Z")
            two_files = installation._recovery_identity(instance, [])

            self.assertNotEqual(one_file, two_files)

    def test_restore_reconcile_leaves_unavailable_checkout_explicitly_degraded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = SimpleNamespace(
                ok=True,
                data_dir=root / "data",
                instance_path=root / "instance" / "instance.yaml",
                instance={},
                host={},
                bindings=[
                    {
                        "id": "missing",
                        "repo": str(root / "missing-checkout"),
                        "enabled": True,
                        "orca_binding": "missing",
                    }
                ],
            )
            source = mock.Mock()
            source.collect.return_value = CollectResult(inventory=HostInventory())

            with (
                mock.patch.object(restore_commands, "validate_instance", return_value=report),
                mock.patch.object(restore_commands, "resolve_installed_packaged", return_value=[]),
                mock.patch.object(
                    restore_commands,
                    "_target",
                    return_value=(report.instance_path, report.data_dir, {}),
                ),
                mock.patch.object(restore_commands, "LiveHostSource", return_value=source),
                mock.patch.object(restore_commands, "_print_json") as emit,
                mock.patch.object(restore_commands, "mark_reconcile_applied") as mark_applied,
            ):
                code = restore_commands.run_restore_reconcile(
                    SimpleNamespace(instance=str(report.instance_path.parent))
                )

            self.assertEqual(code, 1)
            payload = emit.call_args.args[0]
            self.assertEqual(payload["status"], "degraded")
            self.assertEqual(payload["unavailable_projects"], ["missing"])
            self.assertIn("remains incomplete", payload["error"])
            mark_applied.assert_not_called()

    def test_materializer_preserves_desired_bindings_and_carries_availability(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = InstanceReport(
                instance_path=root / "instance" / "instance.yaml",
                name="test",
                projects=2,
                adapters=0,
                adapter_drafts=0,
                has_manifest=True,
                manifest_path=root / "manifest",
                errors=[],
                warnings=[],
                bindings=[{"id": "ready"}, {"id": "missing"}],
                host={},
                instance={},
                data_dir=root / "data",
            )

            def run(context, *, steps=installation.STEPS):
                self.assertEqual(context.report.bindings, [{"id": "ready"}, {"id": "missing"}])
                self.assertEqual(context.project_availability.unavailable, frozenset({"missing"}))
                return UpgradeResult()

            with (
                mock.patch("ummanu.installation.validate_instance", return_value=report),
                mock.patch("ummanu.installation.check_product_runtime"),
                mock.patch("ummanu.installation.resolve_runtime_owner", return_value=(None, root / "home")),
                mock.patch("ummanu.installation.run_steps", side_effect=run),
            ):
                installation.materialize_host(
                    root / "instance",
                    root / "product",
                    project_availability=ProjectAvailability(frozenset({"missing"})),
                )

    def test_degraded_recovery_finishes_host_pipeline_state_and_ownership(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "instance"
            data = root / "data"
            target.mkdir()
            (target / ".git").mkdir()
            _checkpoint(target, data)
            report = InstanceReport(
                instance_path=target / "instance.yaml",
                name="test",
                projects=1,
                adapters=0,
                adapter_drafts=0,
                has_manifest=True,
                manifest_path=data / "data-manifest.json",
                errors=[],
                warnings=[],
                bindings=[
                    {
                        "id": "missing",
                        "repo": str(root / "missing"),
                        "remote": "https://example.invalid/private.git",
                        "default_branch": "main",
                    }
                ],
                host={},
                instance={},
                data_dir=data,
            )
            args = SimpleNamespace(
                instance_dir=str(target),
                instance_remote="file:///instance.git",
                installation_user=getpass.getuser(),
                recover=True,
                adopt=False,
                dry_run=False,
                runtime_env=None,
                product_root=str(PRODUCT_ROOT),
                bootstrap_credential_file=None,
                bootstrap_credential_stdin=False,
                recovery_phrase_file=None,
                recovery_phrase_stdin=False,
                host_fixture=None,
            )
            host_result = SimpleNamespace(
                steps=[
                    SimpleNamespace(name="head-registry", status="changed", detail="regenerated"),
                    SimpleNamespace(name="host", status="changed", detail="completed"),
                ]
            )

            def host(*_args, before_host=None, **_kwargs):
                before_host(SimpleNamespace(runtime_home=root / "home"))
                return host_result

            with (
                mock.patch("ummanu.installation._ensure_installation_user"),
                mock.patch("ummanu.installation._clone_or_reuse", return_value="reused checkpoint checkout"),
                mock.patch(
                    "ummanu.installation._open_secret_store",
                    return_value=installation.SecretRecovery(store_present=True, unlocked=True),
                ),
                mock.patch("ummanu.installation.read_runtime_env", return_value={}),
                mock.patch("ummanu.installation.check_prerequisites"),
                mock.patch("ummanu.installation._validated_instance", return_value=report),
                mock.patch("ummanu.installation.import_normalized_board", return_value=1),
                mock.patch("ummanu.installation.rebuild_memory_index", return_value=1),
                mock.patch("ummanu.installation.provision_codex_home", return_value=0),
                mock.patch(
                    "ummanu.installation.materialize_pipeline_state",
                    return_value=installation.PipelineStateMaterialization(0, True),
                ),
                mock.patch("ummanu.installation.materialize_host", side_effect=host) as materialize,
                mock.patch("ummanu.installation.mark_reconcile_applied"),
                mock.patch("ummanu.installation.restore_findings", return_value=[]),
                mock.patch("ummanu.installation._set_installation_owner") as owner,
            ):
                result = installation.install(args)

            self.assertEqual(result.status, "degraded")
            self.assertEqual(result.projects[0].code, "unsupported-https")
            self.assertEqual(
                [step.name for step in result.steps if step.status == "degraded"],
                ["runtime", "status"],
            )
            # The regenerated head pair is never published, so there is no publication to degrade.
            self.assertNotIn("publication_policy", materialize.call_args.kwargs)
            self.assertTrue(any(step.name == "pipeline-state" for step in result.steps))
            self.assertEqual(
                materialize.call_args.kwargs["project_availability"].unavailable,
                frozenset({"missing"}),
            )
            self.assertIn(mock.call(target, getpass.getuser()), owner.call_args_list)

    def test_partial_materializer_failure_reaches_final_ownership_barrier(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "instance"
            data = root / "data"
            run_state = root / "runtime" / "state"
            target.mkdir()
            (target / ".git").mkdir()
            (target / ".git" / state_repo.STATE_LOCK_NAME).write_text("", encoding="utf-8")
            key = secret_store.key_path(target)
            key.parent.mkdir(parents=True)
            key.write_text("fixture-key-bytes\n", encoding="utf-8")
            key.chmod(0o600)
            _checkpoint(target, data)
            report = InstanceReport(
                instance_path=target / "instance.yaml",
                name="test",
                projects=0,
                adapters=0,
                adapter_drafts=0,
                has_manifest=True,
                manifest_path=data / "data-manifest.json",
                errors=[],
                warnings=[],
                bindings=[],
                host={},
                instance={},
                data_dir=data,
            )
            args = SimpleNamespace(
                instance_dir=str(target),
                instance_remote="file:///instance.git",
                installation_user=getpass.getuser(),
                recover=True,
                adopt=False,
                dry_run=False,
                runtime_env=None,
                product_root=str(PRODUCT_ROOT),
                bootstrap_credential_file=None,
                bootstrap_credential_stdin=False,
                recovery_phrase_file=None,
                recovery_phrase_stdin=False,
                host_fixture=None,
            )

            def restore_runs(*_args, **_kwargs):
                run_state.mkdir(parents=True)
                (run_state / "attempts.jsonl").write_text("{}\n", encoding="utf-8")
                return installation.PipelineStateMaterialization(1, True)

            def fail_after_pipeline_state(*_args, before_host=None, **_kwargs):
                before_host(SimpleNamespace(runtime_home=root / "home"))
                raise InstallError("safe materializer tail failed")

            with (
                mock.patch("ummanu.installation._ensure_installation_user"),
                mock.patch("ummanu.installation._clone_or_reuse", return_value="reused checkpoint checkout"),
                mock.patch(
                    "ummanu.installation._open_secret_store",
                    return_value=installation.SecretRecovery(store_present=True, unlocked=True),
                ),
                mock.patch("ummanu.installation.read_runtime_env", return_value={}),
                mock.patch("ummanu.installation.check_prerequisites"),
                mock.patch("ummanu.installation._validated_instance", return_value=report),
                mock.patch("ummanu.installation.import_normalized_board", return_value=0),
                mock.patch("ummanu.installation.rebuild_memory_index", return_value=0),
                mock.patch("ummanu.installation.provision_project_checkouts", return_value=[]),
                mock.patch("ummanu.installation.provision_codex_home", return_value=0),
                mock.patch("ummanu.installation.pipeline_state_path", return_value=run_state),
                mock.patch("ummanu.installation.materialize_pipeline_state", side_effect=restore_runs),
                mock.patch("ummanu.installation.materialize_host", side_effect=fail_after_pipeline_state),
                mock.patch("ummanu.installation._set_installation_owner") as owner,
                mock.patch(
                    "ummanu.installation._establish_recovery_ownership_barrier",
                    wraps=installation._establish_recovery_ownership_barrier,
                ) as barrier,
            ):
                result = installation.install(args)

            self.assertEqual(result.status, "failed")
            self.assertIn("safe materializer tail failed", result.steps[-1].detail)
            self.assertEqual(
                owner.call_args_list[-3:],
                [
                    mock.call(target, getpass.getuser()),
                    mock.call(data, getpass.getuser()),
                    mock.call(run_state.parent, getpass.getuser()),
                ],
            )
            self.assertEqual(barrier.call_args_list[-1].kwargs["additional_paths"], (run_state.parent,))
            self.assertTrue((data / installation.RECOVERY_PROGRESS_FILE).is_file())
            self.assertTrue((target / ".git" / state_repo.STATE_LOCK_NAME).is_file())
            self.assertTrue((run_state / "attempts.jsonl").is_file())

    def test_fatal_board_failure_does_not_enter_project_or_host_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "instance"
            data = root / "data"
            target.mkdir()
            (target / ".git").mkdir()
            _checkpoint(target, data)
            report = InstanceReport(
                target / "instance.yaml",
                "test",
                0,
                0,
                0,
                True,
                data / "data-manifest.json",
                [],
                [],
                [],
                {},
                {},
                data,
            )
            args = SimpleNamespace(
                instance_dir=str(target),
                instance_remote="file:///instance.git",
                installation_user=getpass.getuser(),
                recover=True,
                adopt=False,
                dry_run=False,
                runtime_env=None,
                product_root=str(PRODUCT_ROOT),
                bootstrap_credential_file=None,
                bootstrap_credential_stdin=False,
                recovery_phrase_file=None,
                recovery_phrase_stdin=False,
                host_fixture=None,
            )
            with (
                mock.patch("ummanu.installation._ensure_installation_user"),
                mock.patch("ummanu.installation._clone_or_reuse", return_value="reused checkpoint checkout"),
                mock.patch(
                    "ummanu.installation._open_secret_store",
                    return_value=installation.SecretRecovery(True, True),
                ),
                mock.patch("ummanu.installation.read_runtime_env", return_value={}),
                mock.patch("ummanu.installation.check_prerequisites"),
                mock.patch("ummanu.installation._validated_instance", return_value=report),
                mock.patch(
                    "ummanu.installation.import_normalized_board",
                    side_effect=installation.RestoreError("parity failed"),
                ),
                mock.patch("ummanu.installation.provision_project_checkouts") as projects,
                mock.patch("ummanu.installation.materialize_host") as host,
                mock.patch("ummanu.installation._set_installation_owner"),
            ):
                result = installation.install(args)
            self.assertEqual(result.status, "failed")
            projects.assert_not_called()
            host.assert_not_called()

    def test_codex_home_seeds_only_missing_non_secret_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            product = root / "product"
            source = product / "packaging" / "codex-home"
            source.mkdir(parents=True)
            (source / "AGENTS.md").write_text("agents\n", encoding="utf-8")
            (source / "config.toml").write_text("model = 'test'\n", encoding="utf-8")
            data_dir = root / "data"
            target = data_dir / "codex-home"
            with mock.patch("ummanu.installation._set_installation_owner"):
                # With no data dir there is no managed home to seed (secretary-1723).
                self.assertEqual(provision_codex_home(product, "dev"), 0)
                self.assertEqual(provision_codex_home(product, "dev", data_dir=data_dir), 2)
                (target / "config.toml").write_text("operator state\n", encoding="utf-8")
                self.assertEqual(provision_codex_home(product, "dev", data_dir=data_dir), 0)
            self.assertEqual((target / "config.toml").read_text(encoding="utf-8"), "operator state\n")

    def test_codex_home_upgrade_repairs_only_the_managed_memory_bearer_setting(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            product = root / "product"
            source = product / "packaging" / "codex-home"
            source.mkdir(parents=True)
            (source / "AGENTS.md").write_text("agents\n", encoding="utf-8")
            (source / "config.toml").write_text(
                'model = "test"\n\n[mcp_servers.memory]\nurl = "http://127.0.0.1:8077/mcp"\n'
                'bearer_token_env_var = "UMMANU_MEMORY_ACCESS_TOKEN"\n',
                encoding="utf-8",
            )
            data_dir = root / "data"
            target = data_dir / "codex-home"
            target.mkdir(parents=True)
            (target / "config.toml").write_text(
                'model = "operator-choice"\n\n[mcp_servers.memory]\nurl = "http://127.0.0.1:8077/mcp"\n',
                encoding="utf-8",
            )
            with mock.patch("ummanu.installation._set_installation_owner"):
                self.assertEqual(provision_codex_home(product, "dev", data_dir=data_dir), 2)

            rendered = (target / "config.toml").read_text(encoding="utf-8")
            self.assertIn('model = "operator-choice"', rendered)
            self.assertIn('bearer_token_env_var = "UMMANU_MEMORY_ACCESS_TOKEN"', rendered)

    def _codex_product(self, root: Path) -> Path:
        product = root / "product"
        source = product / "packaging" / "codex-home"
        source.mkdir(parents=True)
        (source / "AGENTS.md").write_text("agents\n", encoding="utf-8")
        (source / "config.toml").write_text(
            'model = "test"\n\n[mcp_servers.memory]\nurl = "http://127.0.0.1:8077/mcp"\n'
            'bearer_token_env_var = "UMMANU_MEMORY_ACCESS_TOKEN"\n',
            encoding="utf-8",
        )
        return product

    def test_codex_home_seeds_the_data_dir_home_copy_once_and_never_a_login(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            product = self._codex_product(root)
            data_dir = root / "data"
            data_home = data_dir / "codex-home"
            legacy = root / "home" / ".config" / "orca" / "codex-runtime-home" / "home"
            account = SimpleNamespace(pw_dir=str(root / "home"), pw_uid=os.getuid(), pw_gid=os.getgid())
            with (
                mock.patch("ummanu.installation.pwd.getpwnam", return_value=account),
                mock.patch("ummanu.installation._set_installation_owner"),
            ):
                # Only the data-dir home is seeded: the legacy one is not managed (secretary-1723).
                self.assertEqual(provision_codex_home(product, "dev", data_dir=data_dir), 2)
                self.assertEqual(
                    sorted(path.name for path in data_home.iterdir()), ["AGENTS.md", "config.toml"]
                )
                self.assertFalse(legacy.exists())
                self.assertEqual(stat.S_IMODE(data_home.stat().st_mode), 0o700)
                (data_home / "config.toml").write_text("operator state\n", encoding="utf-8")
                self.assertEqual(provision_codex_home(product, "dev", data_dir=data_dir), 0)
            self.assertEqual((data_home / "config.toml").read_text(encoding="utf-8"), "operator state\n")
            self.assertFalse((data_home / "auth.json").exists())
            self.assertFalse((legacy / "auth.json").exists())

    def test_codex_home_never_touches_the_legacy_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            product = self._codex_product(root)
            data_dir = root / "data"
            data_home = data_dir / "codex-home"
            legacy = root / "home" / ".config" / "orca" / "codex-runtime-home" / "home"
            unreconciled = (
                'model = "operator-choice"\n\n[mcp_servers.memory]\nurl = "http://127.0.0.1:8077/mcp"\n'
            )
            legacy.mkdir(parents=True)
            (legacy / "AGENTS.md").write_text("agents\n", encoding="utf-8")
            (legacy / "config.toml").write_text(unreconciled, encoding="utf-8")
            account = SimpleNamespace(pw_dir=str(root / "home"), pw_uid=os.getuid(), pw_gid=os.getgid())
            with (
                mock.patch("ummanu.installation.pwd.getpwnam", return_value=account),
                mock.patch("ummanu.installation._set_installation_owner"),
            ):
                # No login anywhere: the data-dir home is seeded and the legacy one is left as found
                # (A20 step 7, secretary-1723). It used to be reconciled while it was the active one.
                self.assertEqual(provision_codex_home(product, "dev", data_dir=data_dir), 2)
                self.assertEqual((legacy / "config.toml").read_text(encoding="utf-8"), unreconciled)
                # And after the PO's login, the same.
                login = '{"tokens": "fixture"}\n'
                (data_home / "auth.json").write_text(login, encoding="utf-8")
                (legacy / "AGENTS.md").unlink()
                self.assertEqual(provision_codex_home(product, "dev", data_dir=data_dir), 0)
            self.assertEqual((legacy / "config.toml").read_text(encoding="utf-8"), unreconciled)
            self.assertFalse((legacy / "AGENTS.md").exists())
            self.assertEqual((data_home / "auth.json").read_text(encoding="utf-8"), login)

    def test_prerequisites_need_no_orca_binary(self):
        """A20 step 9 (secretary-1726): recovery probes the board store and runs no `orca`."""
        with (
            mock.patch("ummanu.installation.os.geteuid", return_value=0),
            mock.patch("ummanu.installation.shutil.which", return_value=None) as which,
            mock.patch("ummanu.installation._run") as run,
            mock.patch("ummanu.installation.board_client"),
            mock.patch("ummanu.installation.TaskReader") as reader,
        ):
            check_prerequisites(Path("/tmp/instance"))

        which.assert_not_called()
        run.assert_not_called()
        reader.return_value.list.assert_called_once()

    def test_prerequisite_probe_requires_the_instance(self):
        with self.assertRaises(TypeError):
            check_prerequisites()  # type: ignore[call-arg]

    def test_existing_runtime_env_is_not_a_bootstrap_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "instance"
            (target / ".git").mkdir(parents=True)
            (target / "runtime.env").write_text("EXAMPLE_API_TOKEN=existing\n", encoding="utf-8")

            with (
                mock.patch("ummanu.installation.state_repo.git", return_value="remote\n"),
                self.assertRaisesRegex(InstallError, "choose --recover"),
            ):
                _clone_or_reuse("remote", target, recovery=False, dry_run=True)

            (target / ".ummanu-bootstrap").write_text("bootstrap\n", encoding="utf-8")
            with mock.patch("ummanu.installation.state_repo.git", side_effect=("remote\n", "")):
                self.assertEqual(
                    _clone_or_reuse("remote", target, recovery=False, dry_run=True),
                    "reused checkpoint checkout",
                )

    def test_reused_checkout_uses_the_owner_scoped_state_repository_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "instance"
            (target / ".git").mkdir(parents=True)
            with mock.patch("ummanu.installation.state_repo.git", side_effect=("remote\n", "")) as git:
                self.assertEqual(
                    _clone_or_reuse("remote", target, recovery=True, dry_run=True),
                    "reused checkpoint checkout",
                )

            self.assertEqual(
                [call.args[1] for call in git.call_args_list],
                [["remote", "get-url", "origin"], ["status", "--porcelain", "-z", "--untracked-files=all"]],
            )

    @unittest.skipUnless(os.geteuid() == 0, "requires a root clean-host fixture")
    def test_root_can_reuse_a_checkout_owned_by_installation_user(self):
        try:
            account = pwd.getpwnam("nobody")
        except KeyError:
            self.skipTest("fixture has no nobody user")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            remote = root / "remote.git"
            target = root / "instance"
            source.mkdir()
            _git(source, "init")
            _git(source, "config", "user.name", "Test")
            _git(source, "config", "user.email", "test@example.invalid")
            (source / "checkpoint").write_text("ok\n", encoding="utf-8")
            _git(source, "add", ".")
            _git(source, "commit", "-m", "checkpoint")
            subprocess.run(["git", "clone", "--bare", str(source), str(remote)], check=True)
            subprocess.run(["git", "clone", str(remote), str(target)], check=True)
            for path in (target, *target.rglob("*")):
                os.chown(path, account.pw_uid, account.pw_gid, follow_symlinks=False)

            self.assertEqual(
                _clone_or_reuse(str(remote), target, recovery=True, dry_run=True),
                "reused checkpoint checkout",
            )

    def test_existing_installation_user_requires_recover_or_adopt_choice(self):
        with self.assertRaisesRegex(InstallError, "choose --recover.*adopt"):
            _ensure_installation_user(getpass.getuser(), recovery=False, dry_run=False)

    def test_bootstrap_stamp_allows_the_existing_user_for_first_install(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "instance"
            target.mkdir()
            (target / ".ummanu-bootstrap").write_text("bootstrap\n", encoding="utf-8")
            args = SimpleNamespace(
                instance_dir=str(target),
                instance_remote="remote",
                installation_user=getpass.getuser(),
                recover=False,
                adopt=False,
                dry_run=False,
                runtime_env=None,
            )
            with (
                mock.patch("ummanu.installation._ensure_installation_user") as ensure_user,
                mock.patch("ummanu.installation._clone_or_reuse", return_value="reused checkpoint checkout"),
                mock.patch(
                    "ummanu.installation.read_runtime_env",
                    side_effect=RuntimeEnvError("stop after user check"),
                ),
            ):
                result = install(args)

            ensure_user.assert_called_once_with(getpass.getuser(), recovery=True, dry_run=False)
            self.assertFalse(result.ok)
            self.assertIn("stop after user check", result.steps[-1].detail)

    def test_checkpoint_materialization_builds_local_json_and_never_copies_derived_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data = root / "data"
            instance.mkdir()
            _checkpoint(instance, data)
            stale = instance / "state" / "memory" / "index.sqlite"
            stale.write_bytes(b"must not move")

            self.assertEqual(materialize_checkpoint(instance, data), (1, 0))

            cards = json.loads((data / "board" / "cards.json").read_text(encoding="utf-8"))
            self.assertEqual(cards["cards"], [CARD])
            self.assertFalse((data / "memory" / "index.sqlite").exists())
            self.assertFalse((data / "worktrees").exists())

    def test_checkpoint_materialization_rebuilds_the_sprint_export(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data = root / "data"
            instance.mkdir()
            _checkpoint(instance, data, sprints=[SPRINT])

            self.assertEqual(materialize_checkpoint(instance, data), (1, 0))

            sprints = json.loads((data / "board" / "sprints.json").read_text(encoding="utf-8"))
            self.assertEqual(sprints["sprints"], [SPRINT])
            self.assertTrue((data / "board" / "sprints.ndjson").is_file())

    def test_checkpoint_sprint_count_mismatch_is_refused(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data = root / "data"
            instance.mkdir()
            _checkpoint(instance, data, sprints=[SPRINT])
            (instance / "state" / "board" / "sprints.ndjson").write_text("", encoding="utf-8")

            with self.assertRaisesRegex(InstallError, "sprint count does not match"):
                materialize_checkpoint(instance, data)

    def test_checkpoint_predating_sprint_export_materializes_an_empty_set(self):
        """An instance repo whose last tick ran before sprints joined the export."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data = root / "data"
            instance.mkdir()
            _checkpoint(instance, data)

            self.assertEqual(materialize_checkpoint(instance, data), (1, 0))

            sprints = json.loads((data / "board" / "sprints.json").read_text(encoding="utf-8"))
            self.assertEqual(sprints["sprints"], [])

    def test_checkpoint_materialization_restores_the_routing_journal(self):
        """secretary-716: per-attempt head telemetry lives only in the journal, so a recovery that
        rebuilt the data plane without `events.ndjson` would lose every finished card's head pairs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data = root / "data"
            instance.mkdir()
            _checkpoint(instance, data)
            event = {
                "event_id": "evt_routing",
                "schema_version": 1,
                "kind": "routing",
                "occurred_at": "2026-07-24T00:00:00Z",
                "outcome": "success",
                "actor": {"role": "dispatcher", "id": "ummanu-dispatcher"},
                "task_id": f"task_{RETIRED_STORE}_1",
                "ref": "ummanu-1",
                "backend": {"kind": RETIRED_STORE, "task_id": 1, "revision": "updated_at:x"},
                "request_id": "routing-verdict",
                "payload": {
                    "attempt": 1,
                    "attempt_id": "attempt-1",
                    "phase": "verdict",
                    "outcome": "red",
                    "heads": [
                        {
                            "role": "worker",
                            "head": "codex",
                            "model": "gpt-5.6-terra",
                            "model_source": "profile",
                        },
                        {
                            "role": "reviewer",
                            "head": "claude-opus",
                            "model": "opus",
                            "model_source": "profile",
                        },
                    ],
                },
            }
            (instance / "state" / "board" / "events.ndjson").write_text(
                json.dumps(event) + "\n", encoding="utf-8"
            )

            materialize_checkpoint(instance, data)

            restored = [
                json.loads(line)
                for line in (data / "board" / "events.ndjson").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            history = attempts(restored, "ummanu-1")
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0].reviewer.head, "claude-opus")
            self.assertEqual(history[0].outcome, "red")

    def test_split_and_flat_checkpoints_materialize_the_same_local_board(self):
        """secretary-1656: one reader serves both layouts, so recovery restores the same bytes."""
        restored: dict[str, dict[str, bytes]] = {}
        identities: dict[str, str] = {}
        for layout in ("flat", "split"):
            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                instance = root / "instance"
                data = root / "data"
                instance.mkdir()
                _checkpoint(instance, data, sprints=[SPRINT])
                board = instance / "state" / "board"
                (board / "audit.ndjson").write_text('{"request_id": "r-1"}\n{"request_id": "r-2"}\n')
                (board / "events.ndjson").write_text('{"event_id": "e-1"}\n', encoding="utf-8")
                if layout == "split":
                    split_board(board)
                    self.assertTrue((board / "layout.json").is_file())
                    self.assertFalse((board / "cards.ndjson").exists())
                    self.assertTrue((board / "cards" / "0000" / "00000000.json").is_file())
                identities[layout] = installation._recovery_identity(instance, [])

                self.assertEqual(materialize_checkpoint(instance, data), (1, 0))

                restored[layout] = {
                    path.name: path.read_bytes() for path in (data / "board").iterdir() if path.is_file()
                }
                self.assertEqual(json.loads(restored[layout]["cards.json"])["cards"], [CARD])
                self.assertEqual(json.loads(restored[layout]["sprints.json"])["sprints"], [SPRINT])
        self.assertEqual(restored["split"], restored["flat"])
        self.assertEqual(identities["split"], identities["flat"])

    def test_broken_split_checkpoint_is_refused(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data = root / "data"
            instance.mkdir()
            _checkpoint(instance, data, layout="split")
            # A gap in the record sequence is a checkpoint nobody wrote; it must not restore silently.
            part = instance / "state" / "board" / "cards" / "0000" / "00000000.json"
            part.rename(part.with_name("00000001.json"))

            with self.assertRaisesRegex(InstallError, "out of sequence"):
                materialize_checkpoint(instance, data)

    def test_non_ummanu_data_target_is_refused_without_overwrite(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data = root / "data"
            instance.mkdir()
            data.mkdir()
            marker = data / "owned-by-operator"
            marker.write_text("keep", encoding="utf-8")
            _checkpoint(instance, data)

            with self.assertRaisesRegex(InstallError, "choose adopt"):
                materialize_checkpoint(instance, data)
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_clean_target_clones_then_resumes_recovery_idempotently(self):
        self._assert_clean_target_recovers(layout="flat")

    def test_clean_target_recovers_from_a_split_layout_checkpoint(self):
        """secretary-1656: `ummanu recover` restores the board from the split layout too."""
        self._assert_clean_target_recovers(layout="split")

    def _assert_clean_target_recovers(self, *, layout: str) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "source"
            remote = root / "instance.git"
            target = root / "target"
            data = root / "data"
            bootstrap = root / "bootstrap-token"
            bootstrap.write_text("fixture-bootstrap\n", encoding="utf-8")
            bootstrap.chmod(0o600)
            source.mkdir()
            _checkpoint(source, data, layout=layout)
            _git(source, "init")
            _git(source, "config", "user.name", "Test")
            _git(source, "config", "user.email", "test@example.invalid")
            _git(source, "add", ".")
            _git(source, "commit", "-m", "checkpoint")
            subprocess.run(
                ["git", "clone", "--bare", str(source), str(remote)],
                check=True,
                capture_output=True,
                text=True,
            )
            # The config identity is the clone URL the recovery command verifies.
            text = (source / "instance.yaml").read_text(encoding="utf-8")
            (source / "instance.yaml").write_text(text.replace("placeholder", str(remote)), encoding="utf-8")
            _git(source, "add", "instance.yaml")
            _git(source, "commit", "-m", "remote identity")
            _git(source, "push", str(remote), "HEAD:master")

            # An install materializes the checkout it is told to, and this one is not `~/ummanu`.
            base = [
                "--instance-remote",
                str(remote),
                "--instance-dir",
                str(target),
                "--installation-user",
                getpass.getuser(),
                "--product-root",
                str(PRODUCT_ROOT),
                "--bootstrap-credential-file",
                str(bootstrap),
            ]
            host = SimpleNamespace(steps=[SimpleNamespace(status="changed")])
            patches = (
                mock.patch(
                    "ummanu.installation.check_prerequisites",
                    side_effect=(InstallError("simulated interrupted recovery"), None, None),
                ),
                mock.patch("ummanu.installation.import_normalized_board", return_value=1),
                mock.patch("ummanu.installation.rebuild_memory_index", return_value=1),
                mock.patch("ummanu.installation.materialize_host", return_value=host),
                mock.patch("ummanu.installation.materialize_pipeline_state", return_value=0),
                mock.patch("ummanu.installation.restore_findings", return_value=[]),
            )
            with (
                patches[0],
                patches[1] as board_restore,
                patches[2],
                patches[3],
                patches[4],
                patches[5],
                mock.patch("ummanu.installation._set_installation_owner") as set_owner,
            ):
                with mock.patch("ummanu.installation._ensure_installation_user"):
                    first_code, first_output = self._cli(["install", *base])
                second_code, second_output = self._cli(["recover", *base])
                third_code, third_output = self._cli(["recover", *base])

            self.assertEqual(first_code, 1, first_output)
            self.assertIn("simulated interrupted recovery", first_output)
            self.assertIn(
                mock.call(target / ".gitignore", getpass.getuser()),
                set_owner.call_args_list,
            )
            self.assertTrue((target / ".git").exists())
            self.assertFalse((target / "runtime.env").exists())
            self.assertIn("skipped   runtime-env", second_output)
            self.assertEqual(second_code, 0, second_output)
            self.assertEqual(third_code, 0, third_output)
            self.assertIn("status: ok", second_output)
            board_restore.assert_called_once_with(data, instance=target)
            self.assertIn("unchanged board", third_output)
            self.assertEqual(
                json.loads((data / "board" / "cards.json").read_text(encoding="utf-8"))["cards"],
                [CARD],
            )
            self.assertFalse((target / "state" / "memory" / "index.sqlite").exists())

    def test_recover_dry_run_validates_checkpoint_without_materializing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "source"
            target = root / "target"
            data = root / "data"
            source.mkdir()
            _checkpoint(source, data)
            materialize_checkpoint(source, data)
            before = {path.relative_to(data): path.read_bytes() for path in data.rglob("*") if path.is_file()}
            _git(source, "init")
            _git(source, "config", "user.name", "Test")
            _git(source, "config", "user.email", "test@example.invalid")
            _git(source, "add", ".")
            _git(source, "commit", "-m", "checkpoint")
            subprocess.run(
                ["git", "clone", str(source), str(target)],
                check=True,
                capture_output=True,
                text=True,
            )
            runtime = target / "runtime.env"
            runtime.write_text(legacy_runtime_lines(), encoding="utf-8")
            runtime.chmod(0o600)

            with (
                mock.patch("ummanu.installation.check_prerequisites") as prerequisites,
                mock.patch("ummanu.installation.import_normalized_board") as board,
                mock.patch("ummanu.installation.rebuild_memory_index") as memory,
                mock.patch("ummanu.installation.materialize_host") as host,
                mock.patch("ummanu.installation.mark_reconcile_applied") as reconcile,
            ):
                code, output = self._cli(
                    [
                        "recover",
                        "--instance-remote",
                        str(source),
                        "--instance-dir",
                        str(target),
                        "--installation-user",
                        getpass.getuser(),
                        "--dry-run",
                    ]
                )

            self.assertEqual(code, 0, output)
            self.assertIn("would-change checkpoint", output)
            self.assertIn("preview made no recovery changes", output)
            self.assertEqual(Path(prerequisites.call_args.args[0]).resolve(), target.resolve())
            after = {path.relative_to(data): path.read_bytes() for path in data.rglob("*") if path.is_file()}
            self.assertEqual(after, before)
            board.assert_not_called()
            memory.assert_not_called()
            host.assert_not_called()
            reconcile.assert_not_called()

    def test_existing_checkout_requires_explicit_recovery_choice(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "source"
            target = root / "target"
            source.mkdir()
            _checkpoint(source, root / "data")
            _git(source, "init")
            _git(source, "config", "user.name", "Test")
            _git(source, "config", "user.email", "test@example.invalid")
            _git(source, "add", ".")
            _git(source, "commit", "-m", "checkpoint")
            subprocess.run(
                ["git", "clone", str(source), str(target)], check=True, capture_output=True, text=True
            )

            with mock.patch("ummanu.installation._ensure_installation_user"):
                code, output = self._cli(
                    [
                        "install",
                        "--instance-remote",
                        str(source),
                        "--instance-dir",
                        str(target),
                        "--installation-user",
                        getpass.getuser(),
                    ]
                )
            self.assertEqual(code, 1)
            self.assertIn("choose --recover", output)
            self.assertIn("adopt", output)

    def test_a_product_root_holding_no_product_is_refused_by_name(self):
        """The selected checkout is checked where it is selected.

        An empty or moved path would otherwise arrive as an ENOENT on a file inside it, several
        steps later, naming a directory the operator never meant to install from.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            empty = Path(tmpdir) / "not-a-checkout"
            empty.mkdir()
            args = SimpleNamespace(product_root=str(empty))

            with self.assertRaises(InstallError) as refusal:
                _product_root(args)

            self.assertIn(str(empty), str(refusal.exception))
            self.assertIn("--product-root", str(refusal.exception))

    def test_the_default_product_root_follows_the_configured_checkout(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            home.mkdir()
            args = SimpleNamespace(product_root=None)

            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
                os.environ.pop("UMMANU_REPO", None)
                with self.assertRaisesRegex(InstallError, str(home / "ummanu")):
                    _product_root(args)
                os.environ["UMMANU_REPO"] = str(PRODUCT_ROOT)
                self.assertEqual(_product_root(args), PRODUCT_ROOT)

    @staticmethod
    def _cli(argv: list[str]) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(argv)
        return code, output.getvalue()


class BootstrapCheckoutRecoveryTests(unittest.TestCase):
    """secretary-1666: recovery on the checkout bootstrap left, whose board is the PostgreSQL store."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = self.source = root / "source"
        self.remote = root / "instance.git"
        self.target = root / "instance"
        self.data = root / "data"
        source.mkdir()
        _checkpoint(source, self.data)
        _git(source, "init")
        _git(source, "config", "user.name", "Test")
        _git(source, "config", "user.email", "test@example.invalid")
        _git(source, "add", ".")
        _git(source, "commit", "-m", "checkpoint")
        subprocess.run(
            ["git", "clone", "--bare", str(source), str(self.remote)],
            check=True,
            capture_output=True,
            text=True,
        )
        text = (source / "instance.yaml").read_text(encoding="utf-8")
        (source / "instance.yaml").write_text(text.replace("placeholder", str(self.remote)), encoding="utf-8")
        _git(source, "add", "instance.yaml")
        _git(source, "commit", "-m", "remote identity")
        _git(source, "push", str(self.remote), "HEAD:master")
        # What bootstrap leaves before any install: the clone and its stamp.
        from ummanu.bootstrap import _mark_bootstrap_checkout

        self.assertEqual(
            _clone_or_reuse(str(self.remote), self.target, recovery=True, dry_run=False),
            "cloned private instance remote",
        )
        _mark_bootstrap_checkout(self.target)

    def _install(
        self, secrets: installation.SecretRecovery | None = None, *, rebuild=None
    ) -> tuple[installation.InstallResult, mock.Mock]:
        steps = mock.Mock()
        steps.import_normalized_board.return_value = 1
        steps.rebuild_memory_index.return_value = 1
        steps.rebuild_memory_index.side_effect = rebuild
        args = SimpleNamespace(
            instance_dir=str(self.target),
            instance_remote=str(self.remote),
            installation_user=getpass.getuser(),
            recover=True,
            adopt=False,
            dry_run=False,
            runtime_env=None,
            product_root=str(PRODUCT_ROOT),
            bootstrap_credential_file=None,
            bootstrap_credential_stdin=False,
            recovery_phrase_file=None,
            recovery_phrase_stdin=False,
            host_fixture=None,
        )
        store = secrets or installation.SecretRecovery(store_present=False, unlocked=False)
        with (
            mock.patch("ummanu.installation._ensure_installation_user"),
            mock.patch("ummanu.installation._set_installation_owner"),
            mock.patch("ummanu.installation._open_secret_store", return_value=store),
            mock.patch("ummanu.installation.check_prerequisites", steps.check_prerequisites),
            mock.patch("ummanu.installation.import_normalized_board", steps.import_normalized_board),
            mock.patch("ummanu.installation.rebuild_memory_index", steps.rebuild_memory_index),
            mock.patch("ummanu.installation.provision_project_checkouts", return_value=[]),
            mock.patch("ummanu.installation.provision_codex_home", return_value=0),
            mock.patch(
                "ummanu.installation.materialize_host",
                return_value=SimpleNamespace(steps=[SimpleNamespace(status="changed")]),
            ),
            mock.patch("ummanu.installation.materialize_pipeline_state", return_value=0),
            mock.patch("ummanu.installation.restore_findings", return_value=[]),
        ):
            return installation.install(args), steps

    def _configure_memory(self, **settings):
        path = self.source / "instance.yaml"
        config = installation.load_config(path)
        config["host"].update(settings)
        path.write_text(json.dumps(config), encoding="utf-8")
        _git(self.source, "add", "instance.yaml")
        _git(self.source, "commit", "-m", "memory configuration")
        _git(self.source, "push", str(self.remote), "HEAD:master")
        return config

    def _rebuild(
        self, data, _instance, *, model="intfloat/multilingual-e5-large", dim=1024, threads=1, isolated
    ):
        self.assertTrue(isolated)
        config = memory_config(installation.load_config(self.target / "instance.yaml")["host"])
        self.assertEqual((model, dim, threads), (config.model, config.dim, config.threads))
        _write_memory_metadata(data / "memory" / "index.sqlite", config)
        return 1

    def test_memory_configuration_reaches_recovery_reindex_and_service(self):
        config = self._configure_memory(memory_model="fixture/custom", memory_dim=384, memory_threads=2)
        result, steps = self._install(rebuild=self._rebuild)
        self.assertEqual(result.status, "ok", result.render())
        settings = steps.rebuild_memory_index.call_args.kwargs
        layout = resolve_systemd_layout(
            config,
            PRODUCT_ROOT / "packaging" / "systemd",
            instance_path=self.target / "instance.yaml",
            data_dir=self.data,
            runtime_user=getpass.getuser(),
        )
        self.assertEqual(
            (settings["model"], settings["dim"], settings["threads"]),
            (layout.memory_model, layout.memory_dim, layout.memory_threads),
        )
        with (
            mock.patch.object(restore_commands, "rebuild_memory_index", return_value=1) as explicit,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                restore_commands.run_memory_reindex(SimpleNamespace(instance=str(self.target))), 0
            )
        self.assertEqual(
            {key: explicit.call_args.kwargs[key] for key in ("model", "dim", "threads")},
            {key: settings[key] for key in ("model", "dim", "threads")},
        )
        again, retry = self._install(rebuild=self._rebuild)
        self.assertEqual(again.status, "ok", again.render())
        retry.rebuild_memory_index.assert_not_called()
        retry.import_normalized_board.assert_not_called()

    def test_changed_memory_settings_rebuild_only_memory_after_recovery(self):
        result, _ = self._install(rebuild=self._rebuild)
        self.assertEqual(result.status, "ok", result.render())
        identity = installation._recovery_identity(self.target, [])
        for settings in (
            {"memory_model": "fixture/custom"},
            {"memory_dim": 384},
        ):
            with self.subTest(settings=settings):
                self._configure_memory(**settings)
                result, steps = self._install(rebuild=self._rebuild)
                self.assertEqual(result.status, "ok", result.render())
                self.assertEqual(installation._recovery_identity(self.target, []), identity)
                steps.rebuild_memory_index.assert_called_once()
                steps.import_normalized_board.assert_not_called()

    def test_retry_rebuilds_missing_legacy_or_corrupt_index_without_reimporting_board(self):
        result, _ = self._install(rebuild=self._rebuild)
        self.assertEqual(result.status, "ok", result.render())
        index = self.data / "memory" / "index.sqlite"
        for shape in ("missing", "legacy", "corrupt"):
            with self.subTest(shape=shape):
                index.unlink()
                if shape == "legacy":
                    with sqlite3.connect(index) as conn:
                        conn.execute("CREATE TABLE legacy_facts(id TEXT)")
                elif shape == "corrupt":
                    index.write_bytes(b"not a sqlite database")
                result, steps = self._install(rebuild=self._rebuild)
                self.assertEqual(result.status, "ok", result.render())
                steps.rebuild_memory_index.assert_called_once()
                steps.import_normalized_board.assert_not_called()

    def test_interrupted_memory_completion_requires_a_compatible_index(self):
        result, _ = self._install(rebuild=self._rebuild)
        self.assertEqual(result.status, "ok", result.render())
        identity = installation._recovery_identity(self.target, [])
        for config in (MemoryConfig(), MemoryConfig(model="fixture/wrong")):
            with self.subTest(model=config.model):
                installation._write_recovery_progress(
                    self.data / installation.RECOVERY_PROGRESS_FILE, identity, memory="started"
                )
                (self.data / "restore-state.json").write_text('{"memory_index":"complete"}', encoding="utf-8")
                _write_memory_metadata(self.data / "memory" / "index.sqlite", config)
                result, steps = self._install(rebuild=self._rebuild)
                self.assertEqual(result.status, "ok", result.render())
                self.assertEqual(steps.rebuild_memory_index.call_count, int(config != MemoryConfig()))
                steps.import_normalized_board.assert_not_called()

    def test_locked_credentials_recovery_keeps_configured_memory_and_repairs_its_index(self):
        self._configure_memory(memory_model="fixture/custom", memory_dim=384, memory_threads=2)
        locked = installation.SecretRecovery(
            store_present=True,
            unlocked=False,
            locked=({"id": "example", "environment": "EXAMPLE_TOKEN", "target": "runtime-env"},),
        )
        for shape in ("missing", "compatible", "wrong-model"):
            with self.subTest(shape=shape):
                if shape == "wrong-model":
                    _write_memory_metadata(self.data / "memory" / "index.sqlite", MemoryConfig())
                result, steps = self._install(locked, rebuild=self._rebuild)
                self.assertEqual(result.status, "failed", result.render())
                self.assertIn("recovery is incomplete", result.render())
                self.assertEqual(steps.rebuild_memory_index.call_count, int(shape != "compatible"))
                steps.import_normalized_board.assert_not_called()

    def test_recovery_restores_into_the_store_with_no_transport_step(self) -> None:
        result, steps = self._install()

        self.assertEqual(result.status, "ok", result.steps)
        # The board-side sequence is the store's prerequisite read, then the restore into it:
        # no transport is materialized and no Pipeline board is made first.
        self.assertEqual(
            steps.mock_calls,
            [
                mock.call.check_prerequisites(self.target),
                mock.call.import_normalized_board(self.data, instance=self.target),
                mock.call.rebuild_memory_index(
                    self.data,
                    self.target,
                    model="intfloat/multilingual-e5-large",
                    dim=1024,
                    threads=1,
                    isolated=True,
                ),
            ],
        )
        board = {step.name: (step.status, step.detail) for step in result.steps}
        self.assertFalse([name for name in board if "transport" in name], board)
        self.assertEqual(board["board"], ("changed", "1 card(s) at parity"))
        self.assertFalse((self.target / STALE_FILE).exists())

    def test_recovery_leaves_stale_transport_leftovers_unread_and_unreported(self) -> None:
        runtime = self.target / "runtime.env"
        runtime.write_text("EXAMPLE_TOKEN=from-the-store\n" + legacy_runtime_lines(), encoding="utf-8")
        runtime.chmod(0o600)
        stale = write_stale_leftovers(self.target)
        # An older build ignored the file it wrote, so it is no local change of the checkout.
        with (self.target / ".git" / "info" / "exclude").open("a", encoding="utf-8") as exclude:
            exclude.write(f"/{STALE_FILE}\n")
        body = stale.read_bytes()

        result, _steps = self._install()

        self.assertEqual(result.status, "ok", result.steps)
        rendered = " ".join(f"{step.name} {step.detail}" for step in result.steps)
        self.assertNotIn(STALE_FILE, rendered)
        self.assertNotIn("board transport", rendered.lower())
        self.assertEqual(stale.read_bytes(), body)

    def test_a_store_written_runtime_env_is_left_as_the_store_wrote_it(self) -> None:
        # Nothing selects the board, so recovery adds no line to the file a store materialized.
        runtime = self.target / "runtime.env"
        runtime.write_text("EXAMPLE_TOKEN=from-the-store\n", encoding="utf-8")
        runtime.chmod(0o600)

        result, _steps = self._install()

        self.assertEqual(result.status, "ok", result.steps)
        self.assertEqual(runtime.read_text(encoding="utf-8"), "EXAMPLE_TOKEN=from-the-store\n")
        self.assertFalse((self.target / STALE_FILE).exists())

    def test_locked_runtime_secrets_still_block_although_a_runtime_file_exists(self) -> None:
        # A runtime.env carrying none of the store's variables does not say they arrived.
        runtime = self.target / "runtime.env"
        runtime.write_text("OTHER=value\n", encoding="utf-8")
        runtime.chmod(0o600)
        locked = installation.SecretRecovery(
            store_present=True,
            unlocked=False,
            locked=({"id": "example_token", "environment": "EXAMPLE_TOKEN", "target": "runtime-env"},),
        )

        result, steps = self._install(locked)

        self.assertEqual(result.status, "failed")
        failure = next(step.detail for step in result.steps if step.status == "failed")
        self.assertIn("recovery is incomplete", failure)
        self.assertIn("runtime.env lacks EXAMPLE_TOKEN", failure)
        steps.check_prerequisites.assert_not_called()
        steps.import_normalized_board.assert_not_called()


def _write_memory_metadata(path: Path, config: MemoryConfig) -> None:
    path.unlink(missing_ok=True)
    write_memory_metadata(path, model=config.model, dim=config.dim)


class MemoryIndexIdentityTests(unittest.TestCase):
    def test_malformed_duplicate_metadata_with_mixed_value_types_is_not_reusable(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "index.sqlite"
            with sqlite3.connect(path) as conn:
                conn.execute("CREATE TABLE index_metadata(key, value)")
                conn.executemany(
                    "INSERT INTO index_metadata VALUES (?, ?)",
                    [("model", "fixture/model"), ("model", b"invalid binary metadata")],
                )
            self.assertFalse(index_matches(path, MemoryConfig()))

    def test_matching_index_is_read_only_and_dimension_is_part_of_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "index with spaces.sqlite"
            config = memory_config({})
            self.assertEqual(config, MemoryConfig("intfloat/multilingual-e5-large", 1024, 1))
            _write_memory_metadata(path, config)
            before = path.read_bytes(), path.stat().st_mtime_ns
            self.assertTrue(index_matches(path, config))
            self.assertFalse(index_matches(path, MemoryConfig(dim=384)))
            self.assertFalse(index_matches(path, MemoryConfig(model="fixture/other")))
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)


class ProductRuntimeTests(unittest.TestCase):
    def _runtime(self, root: Path) -> tuple[Path, Path]:
        product = root / "product"
        source = product / "src" / "ummanu"
        source.mkdir(parents=True)
        (source / "__init__.py").write_text("raise AssertionError('preflight imported product')\n")
        venv.EnvBuilder(with_pip=False).create(product / ".venv")
        site = next((product / ".venv" / "lib").glob("python*/site-packages"))
        (site / "ummanu.pth").write_text(str(source.parent) + "\n", encoding="utf-8")
        metadata = site / "ummanu-0.1.0.dist-info"
        metadata.mkdir()
        (metadata / "direct_url.json").write_text(
            json.dumps({"url": product.as_uri(), "dir_info": {"editable": True}}), encoding="utf-8"
        )
        for name in ("ummanu", "ummanu-memory-mcp", "ummanu-memory-po-bridge"):
            script = product / ".venv" / "bin" / name
            script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            script.chmod(0o755)
        return product, metadata

    def test_materializer_refuses_absent_venv_before_any_host_step(self):
        with tempfile.TemporaryDirectory() as temporary:
            product = Path(temporary) / "product"
            with (
                mock.patch.object(installation, "validate_instance", return_value=SimpleNamespace(ok=True)),
                mock.patch.object(
                    installation, "resolve_runtime_owner", return_value=("fixture", Path(temporary))
                ),
                mock.patch.object(installation, "run_steps") as steps,
                self.assertRaisesRegex(InstallError, r"\.venv/bin/python3"),
            ):
                installation.materialize_host(Path(temporary) / "instance", product)
            steps.assert_not_called()
            self.assertFalse(product.exists())

    def test_preflight_accepts_own_editable_runtime_without_importing_product(self):
        with tempfile.TemporaryDirectory() as temporary:
            product, _ = self._runtime(Path(temporary))
            installation.check_product_runtime(product)

    def test_preflight_refuses_snapshot_install_or_missing_entry_point(self):
        with tempfile.TemporaryDirectory() as temporary:
            product, metadata = self._runtime(Path(temporary))
            for payload in ({"url": product.as_uri(), "dir_info": {}}, {"dir_info": None}, []):
                with self.subTest(metadata=payload):
                    (metadata / "direct_url.json").write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaisesRegex(InstallError, "not an editable install"):
                        installation.check_product_runtime(product)
            (product / ".venv" / "bin" / "ummanu-memory-mcp").unlink()
            with self.assertRaisesRegex(InstallError, r"\.venv/bin/ummanu-memory-mcp"):
                installation.check_product_runtime(product)

    def test_preflight_refuses_runtime_targeting_another_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            product, metadata = self._runtime(root)
            other = root / "other"
            other.mkdir()
            (metadata / "direct_url.json").write_text(
                json.dumps({"url": other.as_uri(), "dir_info": {"editable": True}}), encoding="utf-8"
            )
            with self.assertRaisesRegex(InstallError, "wrong_root"):
                installation.check_product_runtime(product)


if __name__ == "__main__":
    unittest.main()
