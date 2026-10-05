"""Instance repository maintenance runs on its own timer, never inside a tick (secretary-1657)."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from ummanu import state_repo
from ummanu.checkpoint import CheckpointWriter
from ummanu.cli import main
from ummanu.config import validate_instance
from ummanu.host import (
    Expectations,
    LiveHostSource,
    SystemdLayout,
    _CmdResult,
    build_doctor_expectations,
    build_plan,
    load_packaged_units,
    packaging_root,
)
from ummanu.host_apply import resolve_packaged
from ummanu.infra import instance_maintenance
from ummanu.runtime.container_labels import PRODUCTION_BOARD_LABEL, TEST_BOARD_LABEL

REPO_ROOT = Path(__file__).resolve().parents[1]
UNITS = REPO_ROOT / "packaging" / "systemd"
SERVICE = "ummanu-instance-maintenance.service"
TIMER = "ummanu-instance-maintenance.timer"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout


def _instance_repo(root: Path) -> Path:
    """A repository shaped like the checkpoint's target, with its canon tracked."""
    instance = root / "instance"
    (instance / "state" / "board").mkdir(parents=True)
    (instance / "state" / "runs").mkdir(parents=True)
    (instance / "state" / "board" / "cards.ndjson").write_text("", encoding="utf-8")
    (instance / "state" / "runs" / "runs.ndjson").write_text("", encoding="utf-8")
    _git(instance, "init", "--quiet", "--initial-branch", "main")
    _git(instance, "config", "user.name", "operator")
    _git(instance, "config", "user.email", "operator@example.invalid")
    _git(instance, "config", "commit.gpgsign", "false")
    # A detached gc would race the assertions below; the control must finish before they read.
    _git(instance, "config", "gc.autoDetach", "false")
    _git(instance, "add", "state")
    _git(instance, "commit", "--quiet", "-m", "canon")
    return instance


def _plain_live_root(root: Path) -> tuple[Path, Path]:
    """A live root without `.git` (the exporter layout) and the bare snapshot repository it names."""
    live = root / "instance"
    live.mkdir()
    data_dir = root / "ummanu-data"
    snapshot = data_dir / "backup" / "snapshot.git"
    (live / "instance.yaml").write_text(
        "version: 1\n"
        "name: test-home\n"
        f"data_dir: {data_dir}\n"
        "offsite:\n"
        "  instance_remote: git@example.invalid:owner/instance.git\n"
        f"  snapshot_repo: {snapshot}\n",
        encoding="utf-8",
    )
    return live, snapshot


def _bare_snapshot(snapshot: Path) -> None:
    snapshot.mkdir(parents=True)
    _git(snapshot, "init", "--quiet", "--bare", "--initial-branch", "main")
    _git(snapshot, "config", "gc.autoDetach", "false")
    # One reachable cut, as the exporter commits it with plumbing: gc packs reachable objects.
    blob = subprocess.run(
        ["git", "--git-dir", str(snapshot), "hash-object", "-w", "--stdin"],
        input="cut\n", check=True, capture_output=True, text=True,
    ).stdout.strip()
    tree = subprocess.run(
        ["git", "--git-dir", str(snapshot), "mktree"],
        input=f"100644 blob {blob}\tsnapshot-manifest.json\n", check=True, capture_output=True, text=True,
    ).stdout.strip()
    commit = _git(
        snapshot, "-c", "user.name=exporter", "-c", "user.email=exporter@example.invalid",
        "commit-tree", tree, "-m", "cut",
    ).strip()
    _git(snapshot, "update-ref", "refs/heads/main", commit)


def _cross_the_auto_gc_threshold(instance: Path, objects: Path | None = None) -> None:
    """Write loose objects until Git's `gc --auto` estimate passes the stock 6,700.

    Git estimates the loose count from the `objects/17` fan-out directory alone, so only blobs
    landing there are written: 28 of them read as more than 6,700 objects.
    """
    target = (objects or instance / ".git" / "objects") / "17"
    target.mkdir(parents=True, exist_ok=True)
    written = 0
    index = 0
    while written < 40:
        body = f"loose object {index}\n".encode()
        index += 1
        raw = b"blob %d\0" % len(body) + body
        digest = hashlib.sha1(raw).hexdigest()
        if not digest.startswith("17"):
            continue
        (target / digest[2:]).write_bytes(zlib.compress(raw))
        written += 1


def _started_commands(trace: Path) -> list[list[str]]:
    """Every Git process argv the trace2 event stream saw start, the parent included."""
    commands: list[list[str]] = []
    if not trace.exists():
        return commands
    for line in trace.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event.get("event") in {"start", "child_start"} and isinstance(event.get("argv"), list):
            commands.append([str(argument) for argument in event["argv"]])
    return commands


def _packing(commands: list[list[str]]) -> list[list[str]]:
    words = {"gc", "pack-objects", "repack", "maintenance"}
    return [argv for argv in commands if words & set(argv)]


class _WithoutSuiteGitConfig(unittest.TestCase):
    """The suite turns implicit gc off for every Git child (`tests/__init__.py`); these tests are
    about what the instance repository's own configuration does, so they run without that."""

    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        count = int(os.environ.pop("GIT_CONFIG_COUNT", "0") or 0)
        for index in range(count):
            os.environ.pop(f"GIT_CONFIG_KEY_{index}", None)
            os.environ.pop(f"GIT_CONFIG_VALUE_{index}", None)


class NoInTickGcTests(_WithoutSuiteGitConfig):
    """Acceptance 1: a checkpoint commit above the threshold starts no gc or pack-objects."""

    def _checkpoint_commit(self, root: Path, instance: Path) -> list[list[str]]:
        (instance / "state" / "board" / "cards.ndjson").write_text('{"ref": "x"}\n', encoding="utf-8")
        trace = root / "trace.json"
        trace.unlink(missing_ok=True)
        writer = CheckpointWriter(root / "data", instance)
        with mock.patch.dict(os.environ, {"GIT_TRACE2_EVENT": str(trace)}):
            result = writer._commit(board_cards=1, run_records=0)
        self.assertEqual(result.status, "committed")
        return _started_commands(trace)

    def test_without_the_product_controls_the_same_commit_would_gc(self):
        # The control: the fixture really is above Git's threshold, so the assertion below is
        # about the configuration and not about a repository too small to pack.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = _instance_repo(root)
            _cross_the_auto_gc_threshold(instance)

            commands = self._checkpoint_commit(root, instance)

        self.assertTrue(_packing(commands), commands)

    def test_a_checkpoint_commit_over_the_threshold_starts_no_gc_or_pack_objects(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = _instance_repo(root)
            _cross_the_auto_gc_threshold(instance)
            state_repo.configure_packing_controls(instance)
            loose_before = instance_maintenance.count_objects(instance)["count"]

            commands = self._checkpoint_commit(root, instance)
            loose_after = instance_maintenance.count_objects(instance)["count"]

        self.assertTrue(commands, "trace2 recorded no Git process at all")
        self.assertEqual(_packing(commands), [])
        self.assertGreater(loose_after, loose_before)

    def test_the_lifecycle_sets_implicit_gc_off_locally(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _instance_repo(Path(tmp))
            state_repo.configure_packing_controls(instance)

            self.assertEqual(_git(instance, "config", "--local", "--get", "gc.auto").strip(), "0")
            self.assertEqual(
                _git(instance, "config", "--local", "--get", "maintenance.auto").strip(), "false"
            )


class MaintenanceRunTests(_WithoutSuiteGitConfig):
    def test_a_run_above_the_threshold_packs_the_loose_objects(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _instance_repo(Path(tmp))
            _cross_the_auto_gc_threshold(instance)
            state_repo.configure_packing_controls(instance)

            result = instance_maintenance.run(instance)
            gc_auto = _git(instance, "config", "--local", "--get", "gc.auto").strip()

        self.assertGreaterEqual(result["loose_objects"]["before"], 40)
        self.assertLess(result["loose_objects"]["after"], result["loose_objects"]["before"])
        self.assertGreaterEqual(result["packs"]["after"], 1)
        # The run restates the thresholds on its command line; it never re-enables in-tick gc.
        self.assertEqual(gc_auto, "0")

    def test_a_quiet_repository_is_left_as_it_is(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _instance_repo(Path(tmp))
            state_repo.configure_packing_controls(instance)

            result = instance_maintenance.run(instance)

        self.assertEqual(result["packs"], {"before": 0, "after": 0})
        self.assertEqual(result["loose_objects"]["before"], result["loose_objects"]["after"])

    def test_a_plain_live_root_packs_the_exporters_snapshot_repository(self):
        # Since the exporter cutover the live root has no `.git`; the bare snapshot repository is
        # where checkpoints accumulate objects, so that is what the timer packs.
        with tempfile.TemporaryDirectory() as tmp:
            live, snapshot = _plain_live_root(Path(tmp))
            _bare_snapshot(snapshot)
            _cross_the_auto_gc_threshold(snapshot, objects=snapshot / "objects")

            result = instance_maintenance.run(live)

            self.assertFalse((live / ".git").exists())
            self.assertEqual(result["instance"], str(live.resolve()))
            self.assertEqual(result["repository"], str(snapshot.resolve()))
            self.assertGreaterEqual(result["loose_objects"]["before"], 40)
            self.assertLess(result["loose_objects"]["after"], result["loose_objects"]["before"])
            self.assertGreaterEqual(result["packs"]["after"], 1)

    def test_a_plain_live_root_before_the_first_cut_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            live, snapshot = _plain_live_root(Path(tmp))

            result = instance_maintenance.run(live)

            self.assertEqual(result["skipped"], "snapshot repository absent")
            self.assertFalse(snapshot.exists())
            self.assertFalse((live / ".git").exists())

    def test_the_cli_succeeds_on_a_plain_live_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            live, snapshot = _plain_live_root(Path(tmp))
            _bare_snapshot(snapshot)
            output = io.StringIO()
            clean = {"containers": {}, "anonymous_volumes": {}, "build_cache": {}, "findings": []}
            with mock.patch.object(instance_maintenance, "cleanup_docker", return_value=clean), \
                    contextlib.redirect_stdout(output):
                code = main(["instance-maintenance", "--instance", str(live)])

        self.assertEqual(code, 0, output.getvalue())
        report = json.loads(output.getvalue())
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["repository"], str(snapshot.resolve()))

    def test_packing_runs_outside_the_state_repo_lock_and_touches_no_ref(self):
        held = {"lock": False}
        seen: list[tuple[list[str], bool]] = []

        @contextlib.contextmanager
        def lock(_instance):
            held["lock"] = True
            try:
                yield
            finally:
                held["lock"] = False

        def git(_instance, args, *, label, timeout=120):
            seen.append((list(args), held["lock"]))
            return "count: 0\npacks: 0\n" if "count-objects" in args else ""

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(state_repo, "state_repo_lock", lock),
            mock.patch.object(state_repo, "git", git),
        ):
            instance = _instance_repo(Path(tmp))
            instance_maintenance.run(instance)

        gc = next((args, locked) for args, locked in seen if "gc" in args)
        self.assertFalse(gc[1], "the packing step must not hold the state-repo lock")
        for setting in ("gc.packRefs=false", "gc.reflogExpire=never", "gc.reflogExpireUnreachable=never"):
            self.assertIn(setting, gc[0])
        self.assertIn("gc.autoDetach=false", gc[0])
        self.assertEqual(gc[0][-3:], ["gc", "--auto", "--quiet"])
        reflog = next((args, locked) for args, locked in seen if "reflog" in args)
        self.assertTrue(reflog[1], "reflog expiry writes refs and belongs under the lock")

    def test_the_cli_reports_a_run_as_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _instance_repo(Path(tmp))
            output = io.StringIO()
            # The Git CLI contract is local; Docker cleanup is exercised with a fake below.
            with contextlib.redirect_stdout(output), mock.patch.object(
                instance_maintenance, "cleanup_docker", return_value={"findings": []}
            ):
                code = main(["instance-maintenance", "--instance", str(instance)])

        self.assertEqual(code, 0, output.getvalue())
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "ok")
        self.assertIn("loose_objects", payload)
        self.assertEqual(payload["cleanup"], {"findings": []})

    def test_the_cli_exposes_cleanup_failure_without_raw_docker_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _instance_repo(Path(tmp))
            output = io.StringIO()
            with contextlib.redirect_stdout(output), mock.patch.object(
                instance_maintenance, "cleanup_docker",
                return_value={"findings": ["Docker command failed or exceeded output bound"]},
            ):
                code = main(["instance-maintenance", "--instance", str(instance)])
        self.assertEqual(code, 1)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["cleanup"]["findings"], ["Docker command failed or exceeded output bound"])

    def test_the_cli_fails_on_a_directory_that_is_no_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["instance-maintenance", "--instance", tmp])

        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())["status"], "failed")


class DockerCleanupTests(unittest.TestCase):
    """A fake native boundary proves selection without touching a Docker daemon."""

    def test_build_cache_prune_accepts_docker_29_total(self):
        def docker(*args):
            if args[:2] == ("container", "ls"):
                return ""
            if args[0] == "version":
                return "1.51"
            if args[:2] == ("volume", "prune"):
                return "Total reclaimed space: 0B\n"
            if args[:2] == ("builder", "prune"):
                self.assertEqual(args[-1], "until=168h")
                return "CACHE ID\tCACHE TYPE\tSIZE\nTotal:\t6.1GB\n"
            self.fail(args)

        with mock.patch.object(instance_maintenance, "_docker", side_effect=docker):
            result = instance_maintenance.cleanup_docker()
        self.assertEqual(result["build_cache"]["reclaimed"], "6.1GB")
        self.assertEqual(result["findings"], [])

    def test_build_cache_prune_rejects_unrecognized_total(self):
        def docker(*args):
            if args[:2] == ("container", "ls"):
                return ""
            if args[0] == "version":
                return "1.51"
            if args[:2] == ("volume", "prune"):
                return "Total reclaimed space: 0B\n"
            if args[:2] == ("builder", "prune"):
                return "Total:\tunknown\n"
            self.fail(args)

        with mock.patch.object(instance_maintenance, "_docker", side_effect=docker):
            result = instance_maintenance.cleanup_docker()
        self.assertEqual(result["build_cache"]["reclaimed"], "unknown")
        self.assertEqual(result["findings"], ["build cache prune result malformed"])

    def test_native_command_pins_local_socket_and_current_api(self):
        with mock.patch.dict(os.environ, {"DOCKER_HOST": "tcp://remote:2375",
                                              "DOCKER_CONTEXT": "remote", "DOCKER_API_VERSION": "1.41"}), \
             mock.patch.object(instance_maintenance.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertEqual(instance_maintenance._docker("version"), "")
        environment = run.call_args.kwargs["env"]
        self.assertEqual(environment["DOCKER_HOST"], "unix:///var/run/docker.sock")
        self.assertNotIn("DOCKER_CONTEXT", environment)
        self.assertNotIn("DOCKER_API_VERSION", environment)

    def test_pid_absence_is_definitive_and_permission_failure_is_not(self):
        with mock.patch.object(instance_maintenance.os, "kill", side_effect=ProcessLookupError):
            self.assertTrue(instance_maintenance._owner_dead(101))
        with mock.patch.object(instance_maintenance.os, "kill", side_effect=PermissionError):
            self.assertFalse(instance_maintenance._owner_dead(101))
        self.assertFalse(instance_maintenance._owner_dead(os.getpid()))

    def test_volume_prune_uses_effective_client_api_and_accepts_docker_separator(self):
        calls = []

        def docker(*args):
            calls.append(args)
            if args[:2] == ("container", "ls"):
                return ""
            if args[0] == "version":
                self.assertEqual(args[-1], "{{.Client.APIVersion}}")
                return "1.41"
            if args[:2] == ("builder", "prune"):
                return "Total reclaimed space: 0B"
            self.fail(f"unsafe volume prune call: {args}")

        with mock.patch.object(instance_maintenance, "_docker", side_effect=docker):
            result = instance_maintenance.cleanup_docker()
        self.assertIsNone(result["anonymous_volumes"]["removed"])
        self.assertIn("Docker API below 1.42", result["findings"][0])
        self.assertEqual(instance_maintenance._deleted_volume_count(
            "Deleted Volumes:\nabc\n\nTotal reclaimed space: 1B\n"), 1)

    def test_dead_owner_only_and_native_prunes_are_bounded_and_idempotent(self):
        ids = [format(index, "064x") for index in range(1, 7)]
        labels = {
            ids[0]: {TEST_BOARD_LABEL: "101"},
            ids[1]: {TEST_BOARD_LABEL: "202"},
            ids[2]: {TEST_BOARD_LABEL: "101", PRODUCTION_BOARD_LABEL: "true"},
            ids[3]: {"other": "101"},  # A name/image decoy cannot establish ownership.
            ids[4]: {TEST_BOARD_LABEL: "01"},
            ids[5]: {TEST_BOARD_LABEL: "9" * 5000},
        }
        present = set(ids)
        calls = []
        volume_runs = 0

        def docker(*args):
            nonlocal volume_runs
            calls.append(args)
            if args[:2] == ("container", "ls"):
                self.assertEqual(args[-1], f"label={TEST_BOARD_LABEL}")
                return "\n".join(identifier for identifier in ids if identifier in present)
            if args[:2] == ("container", "inspect"):
                identifier = args[2]
                return json.dumps([{"Id": identifier, "Config": {"Labels": labels[identifier]}}])
            if args[:2] == ("container", "rm"):
                self.assertEqual(args[2], "--force")
                present.remove(args[3])
                return args[3]
            if args[0] == "version":
                return "1.45\n"
            if args[:2] == ("volume", "prune"):
                self.assertEqual(args, ("volume", "prune", "--force"))
                volume_runs += 1
                return ("Deleted Volumes:\n" + "a" * 64 + "\n\nTotal reclaimed space: 1B\n"
                        if volume_runs == 1 else "Total reclaimed space: 0B")
            if args[:2] == ("builder", "prune"):
                self.assertEqual(args[-1], "until=168h")
                self.assertIn("--all", args)
                return "Total reclaimed space: 4MB\n" if volume_runs == 1 else "Total reclaimed space: 0B\n"
            self.fail(args)

        with mock.patch.object(instance_maintenance, "_docker", side_effect=docker), \
             mock.patch.object(instance_maintenance, "_owner_dead", side_effect=lambda pid: pid == 101):
            first = instance_maintenance.cleanup_docker()
            second = instance_maintenance.cleanup_docker()
        self.assertEqual(first["containers"]["removed"], 1)
        self.assertEqual(first["containers"]["retained"],
                         {"owner_live_or_unknown": 1, "protected_label": 3, "invalid_owner": 1})
        self.assertEqual(first["anonymous_volumes"]["removed"], 1)
        self.assertEqual(first["build_cache"]["reclaimed"], "4MB")
        self.assertEqual(first["findings"], [])
        self.assertEqual(second["containers"]["removed"], 0)
        self.assertEqual(second["anonymous_volumes"]["removed"], 0)
        self.assertEqual([call for call in calls if call[:2] == ("container", "rm")],
                         [("container", "rm", "--force", ids[0])])

    def test_changed_owner_concurrent_removal_and_command_failure_fail_closed(self):
        identifier = "a" * 64
        inspections = 0

        def docker(*args):
            nonlocal inspections
            if args[:2] == ("container", "ls"):
                return identifier
            if args[:2] == ("container", "inspect"):
                inspections += 1
                owner = "101" if inspections == 1 else "202"
                return json.dumps([{"Id": identifier, "Config": {"Labels": {TEST_BOARD_LABEL: owner}}}])
            if args[0] == "version":
                raise instance_maintenance.CleanupError("version unavailable")
            if args[:2] == ("builder", "prune"):
                raise instance_maintenance.CleanupError("builder unavailable")
            self.fail(f"destructive call: {args}")

        with mock.patch.object(instance_maintenance, "_docker", side_effect=docker), \
             mock.patch.object(instance_maintenance, "_owner_dead", return_value=True):
            result = instance_maintenance.cleanup_docker()
        self.assertEqual(result["containers"], {"removed": 0, "retained": {"changed_or_live": 1}})
        self.assertEqual(result["findings"], ["version unavailable", "builder unavailable"])

    def test_empty_and_oversized_inventory(self):
        calls = []

        def docker(*args):
            calls.append(args)
            if args[:2] == ("container", "ls"):
                return ""
            if args[0] == "version":
                return "1.41"
            if args[:2] == ("builder", "prune"):
                return "Total reclaimed space: 0B"
            self.fail(args)

        with mock.patch.object(instance_maintenance, "_docker", side_effect=docker):
            result = instance_maintenance.cleanup_docker()
        self.assertEqual(result["containers"]["removed"], 0)
        self.assertTrue(result["findings"])
        self.assertIsNone(result["anonymous_volumes"]["removed"])
        self.assertFalse(any(call[:2] == ("volume", "prune") for call in calls))

        def oversized(*args):
            if args[:2] == ("container", "ls"):
                return "\n".join(format(index, "064x") for index in range(instance_maintenance.MAX_CONTAINERS + 1))
            if args[0] == "version":
                return "1.42"
            if args[:2] == ("volume", "prune"):
                return "Total reclaimed space: 0B"
            if args[:2] == ("builder", "prune"):
                return "Total reclaimed space: 0B"
            self.fail(f"container inspection/removal after oversized inventory: {args}")

        with mock.patch.object(instance_maintenance, "_docker", side_effect=oversized):
            result = instance_maintenance.cleanup_docker()
        self.assertEqual(result["containers"]["removed"], 0)
        self.assertIn("test container inventory exceeds bound or contains duplicates", result["findings"])

    def test_failed_removal_does_not_claim_success_or_expose_native_output(self):
        identifier = "f" * 64

        def docker(*args):
            if args[:2] == ("container", "ls"):
                return identifier
            if args[:2] == ("container", "inspect"):
                return json.dumps([{"Id": identifier, "Config": {"Labels": {TEST_BOARD_LABEL: "101"}}}])
            if args[:2] == ("container", "rm"):
                raise instance_maintenance.CleanupError("test container removal failed")
            if args[0] == "version":
                return "1.42"
            if args[:2] == ("volume", "prune"):
                return "Total reclaimed space: 0B"
            if args[:2] == ("builder", "prune"):
                return "Total reclaimed space: 0B"
            self.fail(args)

        with mock.patch.object(instance_maintenance, "_docker", side_effect=docker), \
             mock.patch.object(instance_maintenance, "_owner_dead", return_value=True):
            result = instance_maintenance.cleanup_docker()
        self.assertEqual(result["containers"], {"removed": 0, "retained": {"removal_failed": 1}})
        self.assertEqual(result["findings"], ["test container removal failed"])


class MaintenanceUnitTests(unittest.TestCase):
    """Acceptance 2: a product-owned timer, planned like every other shipped component."""

    def _rendered(self) -> dict[str, bytes]:
        units = load_packaged_units(
            UNITS,
            "ummanu-",
            SystemdLayout(
                REPO_ROOT, Path("/srv/instance"), Path("/srv/data"), "operator", Path("/home/operator")
            ),
        )
        return {unit.name: unit.content for unit in units}

    def test_the_service_runs_the_product_command_once_per_trigger(self):
        service = self._rendered()[SERVICE].decode()

        self.assertIn("Type=oneshot\n", service)
        self.assertIn("User=operator\n", service)
        self.assertIn(
            f"ExecStart={REPO_ROOT}/.venv/bin/ummanu instance-maintenance --instance /srv/instance\n",
            service,
        )
        self.assertIn("IOSchedulingClass=idle\n", service)
        # systemd's default 90 s start timeout must not cut the pack short: the unit's own bound
        # sits above the product's, with a margin, so the product's refusal fires first.
        timeouts = [
            line.partition("=")[2] for line in service.splitlines() if line.startswith("TimeoutStartSec=")
        ]
        self.assertEqual(len(timeouts), 1)
        product_bound = instance_maintenance.GC_TIMEOUT_SECONDS + instance_maintenance.REFLOG_TIMEOUT_SECONDS
        self.assertGreaterEqual(int(timeouts[0]), product_bound + 15 * 60)
        # Fired by its timer only: an [Install] section would let it start at boot.
        self.assertNotIn("[Install]", service)

    def test_the_timer_is_daily_catches_up_and_is_installable(self):
        timer = self._rendered()[TIMER].decode()

        self.assertIn("OnCalendar=04:17:00\n", timer)
        self.assertIn("Persistent=true\n", timer)
        self.assertIn(f"Unit={SERVICE}\n", timer)
        self.assertIn("WantedBy=timers.target\n", timer)

    def test_the_plan_owns_both_units_unless_the_component_is_opted_out(self):
        units = load_packaged_units(UNITS, "ummanu-")
        planned = {
            resource.name
            for resource in build_plan({"host": {"unit_prefix": "ummanu-"}}, [], packaged=units)
        }
        opted_out = {
            resource.name
            for resource in build_plan(
                {
                    "host": {
                        "unit_prefix": "ummanu-",
                        "components": {"instance-maintenance": {"enabled": False}},
                    }
                },
                [],
                packaged=units,
            )
        }

        self.assertLessEqual({SERVICE, TIMER}, planned)
        self.assertFalse({SERVICE, TIMER} & opted_out)


class MaintenanceStatusTests(unittest.TestCase):
    """The timer is listed under `host.schedules` with the evidence that it ran."""

    def test_status_lists_the_timer_with_its_last_trigger(self):
        instance = REPO_ROOT / "examples" / "instance"
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"UMMANU_REPO": str(REPO_ROOT)}),
        ):
            fixture = Path(tmp)
            report = validate_instance(instance)
            expected = build_doctor_expectations(
                report.instance,
                report.bindings,
                packaged=resolve_packaged(
                    report.instance,
                    packaging_root(REPO_ROOT),
                    product_root=REPO_ROOT,
                    instance_path=instance,
                    data_dir=report.data_dir,
                ),
            )
            (fixture / "units.txt").write_text("\n".join(sorted(expected.units)), encoding="utf-8")
            (fixture / "unit-states.txt").write_text(
                "\n".join(
                    [
                        *(f"{name} enabled active" for name in sorted(expected.units) if name != SERVICE),
                        f"{SERVICE} static inactive",
                    ]
                ),
                encoding="utf-8",
            )
            (fixture / "timer-triggers.txt").write_text(
                f"{TIMER} Mon 2026-09-21 04:21:09 UTC\n", encoding="utf-8"
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["status", "--json", "--host-fixture", str(fixture), "--instance", str(instance)])

        self.assertEqual(code, 0, output.getvalue())
        payload = json.loads(output.getvalue())
        self.assertIn(SERVICE, expected.units)
        schedule = next(row for row in payload["host"]["schedules"] if row["name"] == TIMER)
        self.assertEqual(schedule["enabled"], "enabled")
        self.assertEqual(schedule["active"], "active")
        self.assertEqual(schedule["last_trigger"], "Mon 2026-09-21 04:21:09 UTC")
        service = next(row for row in payload["host"]["units"] if row["name"] == SERVICE)
        self.assertEqual(service["active"], "inactive")
        others = [row for row in payload["host"]["schedules"] if row["name"] != TIMER]
        self.assertTrue(others)
        self.assertTrue(all(row["last_trigger"] is None for row in others))

    def test_the_live_probe_reads_last_trigger_for_timers_only(self):
        shown: list[str] = []

        class RuntimeHost(LiveHostSource):
            def _run(self, cmd):
                if cmd[1] == "list-unit-files":
                    return _CmdResult(True, 0, f"{TIMER} enabled enabled\n{SERVICE} static -\n", "")
                if cmd[1] == "is-enabled":
                    return _CmdResult(True, 0, "enabled\n", "")
                if cmd[1] == "is-active":
                    return _CmdResult(True, 0, "active\n", "")
                if cmd[1] == "show":
                    shown.append(cmd[-1])
                    return _CmdResult(True, 0, "Mon 2026-09-21 04:21:09 UTC\n", "")
                return _CmdResult(True, 0, "", "")

        expected = Expectations(
            units={TIMER, SERVICE},
            unit_prefix="ummanu-",
            unit_runtime={TIMER: (True, True), SERVICE: (False, False)},
        )
        result = RuntimeHost().collect(expected)

        self.assertEqual(result.errors, {})
        self.assertEqual(shown, [TIMER])
        self.assertEqual(result.inventory.timer_triggers, {TIMER: "Mon 2026-09-21 04:21:09 UTC"})


if __name__ == "__main__":
    unittest.main()
