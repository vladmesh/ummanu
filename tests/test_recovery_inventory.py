from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.head_registry import write_installed_pair
from tests.retired_board import LEGACY_ENV, LEGACY_SECRET_IDS, LEGACY_VALUES
from ummanu.checkpoint import _credential_snapshot
from ummanu.cli import main
from ummanu.config import validate_instance
from ummanu.head_health import HeadReadiness
from ummanu.host import CollectResult, HostInventory
from ummanu.infra.recovery_inventory import collect_recovery_inventory
from ummanu.secret_store import initialize_store, set_secret
from ummanu.secret_words import RECOVERY_WORDS
from ummanu.status import collect_status

REGISTRY = """resources:
  never-used:
    account: spare
    probe: 'false'
  used:
    account: primary
    probe: 'true'
profiles:
  worker:
    resource: used
    adapter: codex
    fallback: []
role_defaults:
  new_card: worker
"""


def git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)


class RecoveryInventoryTests(unittest.TestCase):
    def fixture(self, root: Path, recorded: dict | None = None) -> tuple[Path, object]:
        instance = root / "instance"
        data = root / "data"
        instance.mkdir()
        (instance / "instance.yaml").write_text(
            f"version: 1\nname: test\ndata_dir: {data}\noffsite:\n  instance_remote: https://github.com/example/instance.git\n",
            encoding="utf-8",
        )
        git(instance, "init", "--quiet", "--initial-branch", "main")
        git(instance, "config", "user.name", "operator")
        git(instance, "config", "user.email", "operator@example.invalid")
        git(instance, "add", "instance.yaml")
        git(instance, "commit", "--quiet", "-m", "fixture")
        git(instance, "remote", "add", "origin", "https://github.com/example/instance.git")
        write_installed_pair(instance, REGISTRY)
        if recorded is not None:
            health = data / "dispatcher" / "resource_health.json"
            health.parent.mkdir(parents=True)
            health.write_text(json.dumps(recorded), encoding="utf-8")
        return instance, validate_instance(instance)

    def test_every_installed_resource_is_visible_offline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, report = self.fixture(Path(tmp))
            snapshot = collect_recovery_inventory(report, inspect_live=False, checkpoint={})

        self.assertEqual([row["resource"] for row in snapshot["resources"]], ["never-used", "used"])
        never = snapshot["resources"][0]
        self.assertEqual(never["profiles"], [])
        self.assertEqual(never["state"], "unknown")
        self.assertEqual(never["source"], "unavailable")

    def test_fresh_cache_is_reused_and_stale_offline_is_explicit(self) -> None:
        now = time.time()
        recorded = {
            "used": {"status": "ready", "reason": "cached green", "checked_at": now},
            "never-used": {"status": "ready", "reason": "old green", "checked_at": now - 1000},
        }
        with tempfile.TemporaryDirectory() as tmp:
            _, report = self.fixture(Path(tmp), recorded)
            with mock.patch("ummanu.infra.recovery_inventory.run_probe") as probe:
                snapshot = collect_recovery_inventory(report, inspect_live=False, now=now, checkpoint={})

        probe.assert_not_called()
        rows = {row["resource"]: row for row in snapshot["resources"]}
        self.assertEqual(rows["used"]["source"], "dispatcher-cache")
        self.assertEqual(rows["used"]["state"], "ready")
        self.assertEqual(rows["never-used"]["state"], "stale")
        self.assertEqual(rows["never-used"]["observed_state"], "ready")

    def test_stale_cache_is_reprobed_online_without_writing_cache(self) -> None:
        now = time.time()
        recorded = {"used": {"status": "ready", "reason": "old", "checked_at": now - 1000}}
        with tempfile.TemporaryDirectory() as tmp:
            instance, report = self.fixture(Path(tmp), recorded)
            health = report.data_dir / "dispatcher" / "resource_health.json"
            before = health.read_bytes()
            with mock.patch(
                "ummanu.infra.recovery_inventory.run_probe",
                side_effect=lambda resource, probe, stamp: HeadReadiness(
                    resource, "unauthenticated", "login required", stamp
                ),
            ):
                snapshot = collect_recovery_inventory(report, inspect_live=True, now=now, checkpoint={})
            self.assertEqual(before, health.read_bytes())
            self.assertFalse((instance / "state").exists())

        rows = {row["resource"]: row for row in snapshot["resources"]}
        self.assertEqual(rows["used"]["state"], "unauthenticated")
        self.assertEqual(rows["used"]["source"], "live-read-only-probe")

    def test_online_status_keeps_resource_readiness_metadata_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, report = self.fixture(Path(tmp))
            inventory = HostInventory(units=set(), unit_states={})
            with (
                mock.patch("ummanu.status.LiveHostSource.collect", return_value=CollectResult(inventory)),
                mock.patch("ummanu.infra.recovery_inventory.run_probe") as probe,
            ):
                snapshot = collect_status(report)

        probe.assert_not_called()
        self.assertTrue(snapshot["recovery"]["resources"])
        self.assertTrue(all(row["source"] == "unavailable" for row in snapshot["recovery"]["resources"]))

    def test_managed_readiness_and_old_push_failure_remain_independent(self) -> None:
        checkpoint = {
            "push_status": "failed",
            "push_reason": "earlier credential was missing",
            "last_push_at": "2026-09-04T20:00:00Z",
            "credential": {
                "state": "managed-ready",
                "source": "encrypted-store",
                "reason": "",
                "last_verified_at": "2026-09-04T23:30:54Z",
                "last_verified_age_minutes": 3,
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            _, report = self.fixture(Path(tmp))
            snapshot = collect_recovery_inventory(report, inspect_live=False, checkpoint=checkpoint)

        consumer = snapshot["credential_consumers"][0]
        self.assertEqual(consumer["state"], "managed-ready")
        self.assertEqual(consumer["reason"], "")
        self.assertEqual(checkpoint["push_status"], "failed")
        self.assertNotIn("earlier credential was missing", consumer["reason"])

    def test_absent_consumer_reason_is_distinct_from_a_healthy_empty_reason(self) -> None:
        checkpoint = {"credential": {"state": "unknown"}}
        now = time.time()
        recorded = {"used": {"status": "ready", "reason": "", "checked_at": now}}
        with tempfile.TemporaryDirectory() as tmp:
            _, report = self.fixture(Path(tmp), recorded)
            snapshot = collect_recovery_inventory(report, inspect_live=False, now=now, checkpoint=checkpoint)

        consumers = {row["consumer"]: row for row in snapshot["credential_consumers"]}
        self.assertEqual(
            consumers["checkpoint-github"]["reason"],
            "checkpoint credential has not been inspected",
        )
        self.assertEqual(consumers["provider-login:used"]["reason"], "")

    def test_git_bypass_is_metadata_only_and_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance, report = self.fixture(Path(tmp))
            git(
                instance, "config", "url.https://credential@example.invalid/.insteadOf", "https://github.com/"
            )
            snapshot = collect_recovery_inventory(report, inspect_live=False, checkpoint={})

        row = next(row for row in snapshot["bypasses"] if row["kind"] == "insteadOf")
        self.assertEqual(row["configuration_key"], "url.*.insteadOf")
        self.assertNotIn("credential", json.dumps(row))
        self.assertFalse(row["supported"])
        self.assertTrue(row["supported_next_action"])

    def test_local_checkpoint_remote_is_not_an_authentication_bypass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance, report = self.fixture(Path(tmp))
            git(instance, "remote", "set-url", "origin", str(Path(tmp) / "checkpoint.git"))
            snapshot = collect_recovery_inventory(report, inspect_live=False, checkpoint={})

        self.assertFalse(any(row.get("kind") == "manual-transport" for row in snapshot["bypasses"]))

    def test_applicable_rewrite_is_visible_for_a_local_checkpoint_remote(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance, report = self.fixture(Path(tmp))
            local = str(Path(tmp) / "checkpoint.git")
            git(instance, "remote", "set-url", "origin", local)
            git(instance, "config", "url.https://example.invalid/.insteadOf", local)
            snapshot = collect_recovery_inventory(report, inspect_live=False, checkpoint={})

        self.assertTrue(any(row.get("kind") == "insteadOf" for row in snapshot["bypasses"]))
        self.assertFalse(any(row.get("kind") == "manual-transport" for row in snapshot["bypasses"]))

    def test_instead_of_bypass_does_not_replace_managed_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance, _ = self.fixture(Path(tmp))
            initialize_store(instance, phrase=" ".join(RECOVERY_WORDS[:16]), actor="tester")
            set_secret(
                instance,
                secret_id="github.checkpoint-token",
                value=b"fixture-checkpoint-token",
                scope="installation",
                purpose="checkpoint",
                actor="tester",
            )
            git(instance, "config", "url.ssh://git@example.invalid/.insteadOf", "https://github.com/")
            credential = _credential_snapshot(instance, {}, time.time())

        self.assertEqual(credential["state"], "managed-ready")
        self.assertEqual(credential["source"], "encrypted-store")

    def test_matching_and_conflicting_runtime_overrides_are_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance, report = self.fixture(Path(tmp))
            with mock.patch.dict(
                os.environ,
                {
                    "UMMANU_INSTANCE": str(instance),
                    "UMMANU_RUNTIME_ENV_FILE": str(instance / "runtime.env"),
                },
                clear=False,
            ):
                matching = collect_recovery_inventory(report, inspect_live=False, checkpoint={})
            with mock.patch.dict(
                os.environ,
                {
                    "UMMANU_INSTANCE": str(instance),
                    "UMMANU_RUNTIME_ENV_FILE": str(instance / "elsewhere.env"),
                },
                clear=False,
            ):
                conflicting = collect_recovery_inventory(report, inspect_live=False, checkpoint={})

        match = next(row for row in matching["paths"] if row["capability"] == "runtime-env")
        conflict = next(row for row in conflicting["paths"] if row["capability"] == "runtime-env")
        self.assertEqual(match["state"], "supported")
        self.assertEqual(conflict["state"], "conflicting-override")

    def test_instance_and_pipeline_run_state_path_provenance_is_truthful(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance, report = self.fixture(root)
            workspaces = root / "workspaces"
            canonical = workspaces / "ummanu" / "pipeline" / "state" / "pipeline"
            bound = {"UMMANU_INSTANCE": str(instance), "UMMANU_WORKSPACES_ROOT": str(workspaces)}
            with mock.patch.dict(os.environ, bound, clear=True):
                declared = collect_recovery_inventory(report, inspect_live=False, checkpoint={})
            with mock.patch.dict(os.environ, {**bound, "UMMANU_PIPELINE_STATE_DIR": str(canonical)}, clear=True):
                matching = collect_recovery_inventory(report, inspect_live=False, checkpoint={})
            with mock.patch.dict(
                os.environ,
                {**bound, "UMMANU_PIPELINE_STATE_DIR": str(root / "other-state")},
                clear=True,
            ):
                conflicting = collect_recovery_inventory(report, inspect_live=False, checkpoint={})

        instance_row = next(row for row in declared["paths"] if row["capability"] == "instance")
        self.assertEqual(instance_row["source"], "declared-installation")
        run_declared = next(row for row in declared["paths"] if row["capability"] == "pipeline-run-state")
        run_matching = next(row for row in matching["paths"] if row["capability"] == "pipeline-run-state")
        run_conflicting = next(
            row for row in conflicting["paths"] if row["capability"] == "pipeline-run-state"
        )
        self.assertEqual(run_declared["source"], "declared-installation")
        self.assertEqual(run_matching["state"], "supported")
        self.assertEqual(run_matching["source"], "environment-override")
        self.assertEqual(run_conflicting["state"], "conflicting-override")

    def test_pipeline_run_state_action_names_the_variable_that_was_selected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance, report = self.fixture(root)
            bound = {"UMMANU_INSTANCE": str(instance), "UMMANU_WORKSPACES_ROOT": str(root / "workspaces")}
            actions = {}
            for name in ("UMMANU_PIPELINE_STATE_DIR", "TA_PIPELINE_STATE_DIR"):
                with mock.patch.dict(os.environ, {**bound, name: str(root / "other-state")}, clear=True):
                    inventory = collect_recovery_inventory(report, inspect_live=False, checkpoint={})
                row = next(row for row in inventory["paths"] if row["capability"] == "pipeline-run-state")
                actions[name] = row["supported_next_action"]

        self.assertEqual(
            actions,
            {
                name: f"remove {name} or make it match the declared workspaces-root contract"
                for name in ("UMMANU_PIPELINE_STATE_DIR", "TA_PIPELINE_STATE_DIR")
            },
        )

    def test_status_and_doctor_leave_recovery_inputs_unchanged_and_launch_no_heads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance, report = self.fixture(root)
            data = report.data_dir
            assert data is not None
            initialize_store(
                instance,
                phrase=" ".join(RECOVERY_WORDS[:16]),
                actor="read-only fixture",
            )
            dispatcher = data / "dispatcher"
            dispatcher.mkdir(parents=True, exist_ok=True)
            (dispatcher / "production-state.json").write_text(
                json.dumps({"records": {}, "checkpoint_push": {"status": "failed"}}),
                encoding="utf-8",
            )
            tasks = data / "tasks"
            tasks.mkdir(parents=True)
            (tasks / "audit.jsonl").write_text('{"event":"fixture"}\n', encoding="utf-8")
            runtime = instance / "runtime.env"
            runtime.write_text("FIXTURE=value\n", encoding="utf-8")
            git(instance, "config", "fixture.read-only", "yes")

            watched = [instance, dispatcher, tasks]

            def snapshot() -> dict[str, bytes]:
                files: dict[str, bytes] = {}
                for target in watched:
                    paths = (
                        [target] if target.is_file() else sorted(p for p in target.rglob("*") if p.is_file())
                    )
                    for path in paths:
                        files[str(path)] = path.read_bytes()
                return files

            before_files = snapshot()
            bound_env = {
                "HOME": str(root / "home"),
                "UMMANU_INSTANCE": str(instance),
                "UMMANU_RUNTIME_ENV_FILE": str(runtime),
                "UMMANU_PIPELINE_STATE_DIR": str(root / "run-state"),
            }
            with (
                mock.patch.dict(os.environ, bound_env, clear=True),
                mock.patch("ummanu.infra.recovery_inventory.run_probe") as probe,
                mock.patch("ummanu.dispatch.host.CommandHostRuntime.prepare_worker") as worker,
                mock.patch("ummanu.dispatch.host.CommandHostRuntime.start_review") as reviewer,
                mock.patch("ummanu.dispatch.observer._launch_observer") as observer,
                mock.patch("builtins.print"),
            ):
                before_env = dict(os.environ)
                self.assertIn(main(["status", "--offline", "--instance", str(instance)]), (0, 1))
                self.assertIn(main(["doctor", "--offline", "--instance", str(instance)]), (0, 1))
                self.assertEqual(dict(os.environ), before_env)
                probe.assert_not_called()
                worker.assert_not_called()
                reviewer.assert_not_called()
                observer.assert_not_called()

            self.assertEqual(snapshot(), before_files)

    def test_missing_materialization_and_locked_store_are_not_claimed_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance, report = self.fixture(Path(tmp))
            initialize_store(instance, phrase=" ".join(RECOVERY_WORDS[:16]), actor="tester")
            set_secret(
                instance,
                secret_id="provider.token",
                value=b"fixture-provider-value",
                scope="installation",
                purpose="provider login",
                actor="tester",
                environment="PROVIDER_TOKEN",
                materialize={"target": "runtime-env"},
            )
            checkpoint = {
                "credential": {
                    "state": "locked/unverifiable",
                    "source": "none",
                    "reason": "installation key is unavailable",
                }
            }
            snapshot = collect_recovery_inventory(report, inspect_live=False, checkpoint=checkpoint)
            runtime = instance / "runtime.env"
            runtime.write_text("PROVIDER_TOKEN=fixture\n", encoding="utf-8")
            runtime.chmod(0o644)
            drifted = collect_recovery_inventory(report, inspect_live=False, checkpoint=checkpoint)

        self.assertEqual(snapshot["materializations"][0]["state"], "missing")
        self.assertEqual(drifted["materializations"][0]["state"], "drifted")
        self.assertEqual(snapshot["credential_consumers"][0]["state"], "locked/unverifiable")
        self.assertNotIn("equal", snapshot["credential_consumers"][0]["reason"])

    def test_legacy_board_catalog_entries_are_ordinary_entries_with_no_transport_row(self) -> None:
        """Nothing names the retired transport's ids: they add no bypass and no transport capability."""
        with tempfile.TemporaryDirectory() as tmp:
            instance, report = self.fixture(Path(tmp))
            initialize_store(instance, phrase=" ".join(RECOVERY_WORDS[:16]), actor="tester")
            for secret_id, environment, value in zip(LEGACY_SECRET_IDS, LEGACY_ENV, LEGACY_VALUES):
                set_secret(
                    instance,
                    secret_id=secret_id,
                    value=value.encode(),
                    scope="installation",
                    purpose="historic board configuration",
                    actor="tester",
                    environment=environment,
                )
            snapshot = collect_recovery_inventory(report, inspect_live=False, checkpoint={})

        text = json.dumps(snapshot)
        self.assertNotIn("transport", text)
        self.assertFalse([row for row in snapshot["bypasses"] if row.get("entry") in LEGACY_SECRET_IDS])

    def test_repeated_inventory_does_not_change_repository_or_dispatcher_cache(self) -> None:
        now = time.time()
        recorded = {"used": {"status": "ready", "reason": "cached", "checked_at": now}}
        with tempfile.TemporaryDirectory() as tmp:
            instance, report = self.fixture(Path(tmp), recorded)
            cache = report.data_dir / "dispatcher" / "resource_health.json"
            before_cache = cache.read_bytes()
            before_status = subprocess.run(
                ["git", "-C", str(instance), "status", "--porcelain=v1"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            first = collect_recovery_inventory(report, inspect_live=False, now=now, checkpoint={})
            second = collect_recovery_inventory(report, inspect_live=False, now=now, checkpoint={})
            after_cache = cache.read_bytes()
            after_status = subprocess.run(
                ["git", "-C", str(instance), "status", "--porcelain=v1"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout

        self.assertEqual(first, second)
        self.assertEqual(before_cache, after_cache)
        self.assertEqual(before_status, after_status)


if __name__ == "__main__":
    unittest.main()
