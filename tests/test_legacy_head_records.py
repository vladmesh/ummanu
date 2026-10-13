"""A20 step 2: `orca-legacy` is no longer a head runtime, and the records written on Orca stay readable.

The fixtures are copied from live shapes (2026-09-24): `GRANT` is an access grant from
`memory/access-grants/` whose `head_run` a codex reviewer on an Orca pane wrote, trimmed to one
provider baseline entry; `PRE_RUNTIME_RUN` is the same run as it was written before `head_runtime`
existed beside the spec block.
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ummanu.memory import access as memory_access
from ummanu.runtime.head import HeadRun, HeadSpec, HeadSpecError, TaskRef
from ummanu.runtime.head.command import HeadCommandError, validate_launch_shape
from ummanu.runtime.head_runtime_backends import (
    LegacyHeadRecordError,
    UnknownHeadRuntimeError,
    build_head_runtime,
    is_legacy_record,
)
from ummanu.runtime.head_runtimes import (
    DEFAULT_HEAD_RUNTIME,
    HEAD_RUNTIMES,
    LOCAL_PTY_RUNTIME,
    ORCA_LEGACY_RUNTIME,
    RECORD_RUNTIME_WHEN_ABSENT,
)
from ummanu.runtime.local_pty_head import LocalPtyHeadRuntime

GRANT = {
    "expires_at": 1790296027,
    "grant_id": "f4aa66cfb5fb4023a8a243ea720707d0",
    "head_run": {
        "fanout_policy": {
            "binary_digest": "",
            "binary_path": "",
            "cli_version": "",
            "events": [],
            "model": "gpt-6-sol",
            "prompt_identity": {
                "path": "/home/dev/ummanu-data/artifacts/prompts/codegen-orchestrator-1363/review-4.md",
                "version": "sha256:01d23cf608aa1f4212e30c0118f4552875c3e93ca4e36deb215c7b8afa7d7c1d",
            },
            "provider_schema_verdict": "",
            "provider_source": {
                "baseline": [
                    (
                        "/home/dev/ummanu-data/codex-home/sessions/2026/09/24/"
                        "rollout-2026-09-24T08-40-14-01a0d292-8b60-78e0-bdc1-9dbe28652227.jsonl"
                    )
                ],
                "head_run_fingerprint": "172529ff471f9a656fb02b0cc6df1c07",
                "kind": "codex_session_event_jsonl",
                "role": "reviewer",
                "root": "/home/dev/ummanu-data/codex-home/sessions",
                "run_id": "96df8f9876f04c539e52f8624e32b2d1",
                "state": "unbound",
                "task_ref": {
                    "document": "/home/dev/ummanu-data/artifacts/prompts/codegen-orchestrator-1363/review-4.md",
                    "kind": "card",
                    "ref": "codegen-orchestrator-1363",
                },
                "version": 1,
                "workspace": "/home/dev/orca/workspaces/codegen_orchestrator/codegen-orchestrator-1363-deploy-cleanup-tolerant",
            },
            "provider_source_required": True,
            "reason": "no provider-schema attestation is attached to this Codex launch",
            "role": "reviewer",
            "run_id": "96df8f9876f04c539e52f8624e32b2d1",
            "state": "schema_absent",
            "terminal_state": "unknown",
            "tool_schema_digest": "",
            "version": 1,
        },
        "handle": "",
        "head_runtime": "orca-legacy",
        "leaf": "",
        "lifecycle": "spawned",
        "pid_file": "/tmp/ummanu-review-pid-codegen-orchestrator-1363.pid",
        "role": "reviewer",
        "run_id": "96df8f9876f04c539e52f8624e32b2d1",
        "spec": {
            "adapter": "codex",
            "codex_mode": "tui",
            "effort": "medium",
            "fallback": [],
            "model": "gpt-6-sol",
            "profile_id": "codex-sol-medium",
            "resource": "openai-sub",
        },
        "stopped_by": {},
        "task_ref": {
            "document": "/home/dev/ummanu-data/artifacts/prompts/codegen-orchestrator-1363/review-4.md",
            "kind": "card",
            "ref": "codegen-orchestrator-1363",
        },
        "workspace": "/home/dev/orca/workspaces/codegen_orchestrator/codegen-orchestrator-1363-deploy-cleanup-tolerant",
    },
    "issued_at": 1790252827,
    "subject": {"kind": "card", "project": "codegen-orchestrator", "ref": "codegen-orchestrator-1363"},
    "token_digest": "353e8943cfdbdb9857a99e966fbc60e04962135e9585e558b25188713eb57616",
    "version": 1,
}

LEGACY_RUN = GRANT["head_run"]
PRE_RUNTIME_RUN = {key: value for key, value in LEGACY_RUN.items() if key != "head_runtime"}


class VocabularyTests(unittest.TestCase):
    def test_the_vocabulary_is_one_name(self) -> None:
        self.assertEqual(HEAD_RUNTIMES, ("local-pty",))
        self.assertEqual(DEFAULT_HEAD_RUNTIME, LOCAL_PTY_RUNTIME)
        self.assertEqual(RECORD_RUNTIME_WHEN_ABSENT, ORCA_LEGACY_RUNTIME)

    def test_a_profile_naming_orca_legacy_is_refused_by_name_with_the_fix(self) -> None:
        for runtime in (ORCA_LEGACY_RUNTIME, "podman"):
            with self.subTest(runtime=runtime):
                with self.assertRaises(HeadCommandError) as caught:
                    validate_launch_shape("codex-sol-medium", {"adapter": "codex", "runtime": runtime})
                message = str(caught.exception)
                self.assertIn("'codex-sol-medium'", message)
                self.assertIn(repr(runtime), message)
                self.assertIn('set `runtime = "local-pty"` or drop the key', message)
                with self.assertRaises(HeadSpecError):
                    HeadSpec.from_profile("codex-sol-medium", {"adapter": "codex", "runtime": runtime})

    def test_a_keyless_profile_is_local_pty(self) -> None:
        self.assertEqual(HeadSpec.from_profile("head", {"adapter": "claude"}).runtime, LOCAL_PTY_RUNTIME)
        self.assertFalse(is_legacy_record(HeadSpec.from_profile("head", {"adapter": "claude"})))


class BackendTests(unittest.TestCase):
    def build(self, name: str):
        return build_head_runtime(
            name,
            local_pty_root=lambda: Path(tempfile.gettempdir()) / "heads",
            head_process_status=lambda *_args, **_kwargs: {},
        )

    def test_only_local_pty_is_built(self) -> None:
        self.assertIsInstance(self.build(LOCAL_PTY_RUNTIME), LocalPtyHeadRuntime)

    def test_a_legacy_record_is_refused_by_a_typed_error_and_never_falls_back(self) -> None:
        for name in (ORCA_LEGACY_RUNTIME, ""):
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(LegacyHeadRecordError, "legacy Orca record.*never launched"),
            ):
                self.build(name)
        self.assertTrue(issubclass(LegacyHeadRecordError, UnknownHeadRuntimeError))
        with self.assertRaises(UnknownHeadRuntimeError) as caught:
            self.build("podman")
        self.assertNotIsInstance(caught.exception, LegacyHeadRecordError)


class RecordTests(unittest.TestCase):
    def test_both_legacy_shapes_load_unchanged_and_are_legacy(self) -> None:
        for label, payload in (("explicit", LEGACY_RUN), ("absent", PRE_RUNTIME_RUN)):
            with self.subTest(shape=label):
                run = HeadRun.from_json(copy.deepcopy(payload))
                self.assertTrue(is_legacy_record(run))
                self.assertEqual(run.spec.runtime, ORCA_LEGACY_RUNTIME)
                self.assertEqual(
                    (run.run_id, run.role, run.workspace, run.pid_file, run.spec.profile_id, run.spec.adapter),
                    (
                        payload["run_id"],
                        payload["role"],
                        payload["workspace"],
                        payload["pid_file"],
                        payload["spec"]["profile_id"],
                        payload["spec"]["adapter"],
                    ),
                )
                written = run.to_json()
                # Every field but the runtime is written back exactly as it was read.
                self.assertEqual(
                    {key: value for key, value in written.items() if key != "head_runtime"},
                    {key: value for key, value in payload.items() if key != "head_runtime"},
                )
                # The runtime is never rewritten to local-pty: an absent one is written as what it meant.
                self.assertEqual(written["head_runtime"], ORCA_LEGACY_RUNTIME)
                self.assertTrue(is_legacy_record(HeadRun.from_json(written)))

    def test_an_explicit_legacy_record_round_trips_byte_for_byte(self) -> None:
        self.assertEqual(HeadRun.from_json(copy.deepcopy(LEGACY_RUN)).to_json(), LEGACY_RUN)

    def test_a_local_pty_record_is_not_legacy(self) -> None:
        payload = {**copy.deepcopy(LEGACY_RUN), "head_runtime": LOCAL_PTY_RUNTIME}
        self.assertFalse(is_legacy_record(HeadRun.from_json(payload)))

    def test_the_hand_built_bridge_and_probe_specs_name_local_pty(self) -> None:
        """They were live producers of `orca-legacy` grants only because a hand-built spec defaults to it.

        The probe's run is read where it is handed to the read; the bridge's, which needs the
        `memory` extra, in `test_memory_po_bridge`.
        """
        from ummanu.memory import health

        probed: list[HeadRun] = []
        with (
            tempfile.TemporaryDirectory() as data_dir,
            mock.patch.object(health, "_probe_read", side_effect=lambda run, *args, **kwargs: probed.append(run)),
        ):
            health.probe_memory(Path(data_dir))
        [run] = probed
        self.assertEqual(run.spec.runtime, LOCAL_PTY_RUNTIME)
        self.assertFalse(is_legacy_record(run))


class AccessGrantTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data_dir = Path(self.temporary.name)
        self.bindings = memory_access.bindings_dir(self.data_dir)
        self.bindings.mkdir(parents=True)

    def plant(self, head_run: dict) -> str:
        grant = copy.deepcopy(GRANT)
        grant["head_run"] = head_run
        (self.bindings / f"{grant['grant_id']}.json").write_text(json.dumps(grant), encoding="utf-8")
        return grant["grant_id"]

    def resolve(self, grant_id: str):
        return memory_access.resolve_grant_id(grant_id, data_dir=self.data_dir, now=GRANT["issued_at"] + 1)

    def test_a_legacy_grant_is_denied_as_stale_not_raised_and_not_malformed(self) -> None:
        for label, payload in (("explicit", LEGACY_RUN), ("absent", PRE_RUNTIME_RUN)):
            with self.subTest(shape=label):
                run = {**copy.deepcopy(payload), "pid_file": str(self.data_dir / "orca-pane.pid")}
                denial = self.resolve(self.plant(run))
                self.assertIsInstance(denial, memory_access.MemoryAccessDenial)
                self.assertEqual(denial.code, "runtime_identity_stale")

    def test_a_legacy_grant_with_no_pid_file_is_unbound(self) -> None:
        denial = self.resolve(self.plant({**copy.deepcopy(LEGACY_RUN), "pid_file": ""}))
        self.assertIsInstance(denial, memory_access.MemoryAccessDenial)
        self.assertEqual(denial.code, "runtime_identity_unbound")

    def test_an_ordinary_local_pty_grant_is_unaffected(self) -> None:
        run = HeadRun(
            run_id="run-1",
            spec=HeadSpec(profile_id="codex-sol-medium", adapter="codex", runtime=LOCAL_PTY_RUNTIME),
            workspace=str(self.data_dir),
            task_ref=TaskRef.card("card-1"),
            role="worker",
            pid_file=str(self.data_dir / "worker.pid"),
        )
        grant = memory_access.issue_grant(
            run, memory_access.card_subject("card-1", "alpha"), data_dir=self.data_dir, now=100
        )
        with mock.patch.object(memory_access, "head_process_status", return_value={"state": "live-match"}):
            resolved = memory_access.resolve_token(grant.token, data_dir=self.data_dir, now=101)
        self.assertIsInstance(resolved, memory_access.MemoryReadIdentity)
        with mock.patch.object(memory_access, "head_process_status", return_value={"state": "dead"}):
            denied = memory_access.resolve_token(grant.token, data_dir=self.data_dir, now=101)
        self.assertEqual(denied.code, "runtime_identity_stale")


class ShippedRegistryTests(unittest.TestCase):
    def test_the_shipped_registry_names_no_runtime_but_local_pty(self) -> None:
        from ummanu.runtime import heads

        shipped = heads.load_registry(heads.HEADS_TOML)
        self.assertTrue(shipped.profiles)
        for profile_id, profile in shipped.profiles.items():
            with self.subTest(profile=profile_id):
                self.assertEqual(profile.get("runtime", DEFAULT_HEAD_RUNTIME), LOCAL_PTY_RUNTIME)
                self.assertEqual(HeadSpec.from_profile(profile_id, profile).runtime, LOCAL_PTY_RUNTIME)
        self.assertNotIn('runtime = "orca-legacy"', heads.HEADS_TOML.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
