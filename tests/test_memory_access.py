"""Launch-bound memory-read policy, independent of the optional MCP dependencies."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ummanu.memory import access as memory_access, grant_env
from ummanu.runtime.head import HeadRun, HeadSpec, TaskRef


class MemoryAccessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def head_run(self, role: str, task: TaskRef) -> HeadRun:
        return HeadRun(
            run_id=f"{role}-{task.ref}",
            spec=HeadSpec(profile_id="test", adapter="codex"),
            workspace=str(self.data_dir / "workspace"),
            task_ref=task,
            role=role,
            pid_file=str(self.data_dir / f"{role}.pid"),
        )

    def resolve(self, grant: memory_access.MemoryAccessGrant, *, now: int = 101):
        with mock.patch.object(memory_access, "head_process_status", return_value={"state": "live-match"}):
            return memory_access.resolve_token(grant.token, data_dir=self.data_dir, now=now)

    def test_execution_heads_receive_only_their_card_project_and_product_ummanu(self) -> None:
        grant = memory_access.issue_grant(
            self.head_run("worker", TaskRef.card("card-1")),
            memory_access.card_subject("card-1", "other-product"),
            data_dir=self.data_dir,
            now=100,
        )
        resolved = self.resolve(grant)

        self.assertIsInstance(resolved, memory_access.MemoryReadIdentity)
        assert isinstance(resolved, memory_access.MemoryReadIdentity)
        self.assertEqual(resolved.scopes, frozenset({"project:other-product", "product:ummanu"}))
        self.assertIsInstance(memory_access.narrow(resolved, "project:foreign"), memory_access.MemoryAccessDenial)
        self.assertEqual(memory_access.narrow(resolved, "project:other-product").scopes, frozenset({"project:other-product"}))

    def test_ummanu_execution_gets_its_project_scope_but_not_installation_wide_read(self) -> None:
        grant = memory_access.issue_grant(
            self.head_run("reviewer", TaskRef.card("card-2")),
            memory_access.card_subject("card-2", "ummanu"),
            data_dir=self.data_dir,
            now=100,
        )
        resolved = self.resolve(grant)

        assert isinstance(resolved, memory_access.MemoryReadIdentity)
        self.assertEqual(resolved.scopes, frozenset({"project:ummanu", "product:ummanu"}))
        self.assertIsInstance(memory_access.narrow(resolved, "global"), memory_access.MemoryAccessDenial)

    def test_observer_receives_sprint_reservations_and_product_ummanu(self) -> None:
        grant = memory_access.issue_grant(
            self.head_run("observer", TaskRef.sprint("sprint:7")),
            memory_access.sprint_subject("sprint:7", ["alpha", "beta"]),
            data_dir=self.data_dir,
            now=100,
        )
        resolved = self.resolve(grant)

        assert isinstance(resolved, memory_access.MemoryReadIdentity)
        self.assertEqual(resolved.scopes, frozenset({"project:alpha", "project:beta", "product:ummanu"}))

    def test_interactive_po_is_installation_wide_but_that_capability_does_not_flow_to_worker(self) -> None:
        po = memory_access.issue_grant(
            self.head_run("po", TaskRef.standing("interactive")),
            memory_access.interactive_po_subject(),
            data_dir=self.data_dir,
            now=100,
        )
        worker = memory_access.issue_grant(
            self.head_run("worker", TaskRef.card("card-3")),
            memory_access.card_subject("card-3", "alpha"),
            data_dir=self.data_dir,
            now=100,
        )

        po_identity = self.resolve(po)
        worker_identity = self.resolve(worker)
        assert isinstance(po_identity, memory_access.MemoryReadIdentity)
        assert isinstance(worker_identity, memory_access.MemoryReadIdentity)
        self.assertIsNone(po_identity.scopes)
        self.assertNotIn("global", worker_identity.scopes)
        self.assertEqual(
            memory_access.narrow(po_identity, memory_access.PO_REVIEW_SCOPE).scopes,
            frozenset({memory_access.PO_REVIEW_SCOPE}),
        )
        self.assertIsInstance(
            memory_access.narrow(worker_identity, memory_access.PO_REVIEW_SCOPE),
            memory_access.MemoryAccessDenial,
        )

    def test_spoofed_token_hand_supplied_scope_and_stale_run_fail_closed(self) -> None:
        grant = memory_access.issue_grant(
            self.head_run("worker", TaskRef.card("card-4")),
            memory_access.card_subject("card-4", "alpha"),
            data_dir=self.data_dir,
            now=100,
        )
        spoofed = f"{grant.grant_id}.not-the-launch-token"
        self.assertEqual(
            memory_access.resolve_token(spoofed, data_dir=self.data_dir, now=101).code,
            "runtime_identity_mismatch",
        )
        resolved = self.resolve(grant)
        assert isinstance(resolved, memory_access.MemoryReadIdentity)
        self.assertEqual(memory_access.narrow(resolved, "project:foreign").code, "scope_not_permitted")
        with mock.patch.object(memory_access, "head_process_status", return_value={"state": "dead"}):
            self.assertEqual(
                memory_access.resolve_token(grant.token, data_dir=self.data_dir, now=101).code,
                "runtime_identity_stale",
            )

    def test_grant_never_persists_capability_material_and_unknown_roles_are_refused(self) -> None:
        grant = memory_access.issue_grant(
            self.head_run("worker", TaskRef.card("card-5")),
            memory_access.card_subject("card-5", "alpha"),
            data_dir=self.data_dir,
            now=100,
        )
        payload = json.loads((memory_access.bindings_dir(self.data_dir) / f"{grant.grant_id}.json").read_text())
        self.assertNotIn(grant.token, json.dumps(payload))
        with self.assertRaisesRegex(memory_access.MemoryAccessError, "does not match"):
            memory_access.issue_grant(
                self.head_run("steward", TaskRef.standing("steward")),
                {"kind": "standing", "ref": "different-duty"},
                data_dir=self.data_dir,
            )
        with self.assertRaisesRegex(memory_access.MemoryAccessError, "not permitted"):
            memory_access.issue_grant(
                self.head_run("other", TaskRef.standing("other")),
                memory_access.standing_subject("other"),
                data_dir=self.data_dir,
            )

    def test_a_revoked_grant_no_longer_resolves(self) -> None:
        grant = memory_access.issue_grant(
            self.head_run("worker", TaskRef.card("card-1")),
            memory_access.card_subject("card-1", "ummanu"),
            data_dir=self.data_dir,
            now=100,
        )
        memory_access.revoke_grant(grant.grant_id, data_dir=self.data_dir)
        for grant_id in (grant.grant_id, "../escape"):
            with self.subTest(grant_id=grant_id), self.assertRaises(memory_access.MemoryAccessError):
                memory_access.revoke_grant(grant_id, data_dir=self.data_dir)

        self.assertEqual(self.resolve(grant), memory_access.MemoryAccessDenial("runtime_identity_unknown"))

    def test_scheduled_roles_have_explicit_scopes(self) -> None:
        curator = memory_access.issue_grant(
            self.head_run("curator", TaskRef.standing("curator")),
            memory_access.standing_subject("curator"),
            data_dir=self.data_dir,
            now=100,
        )
        retro = memory_access.issue_grant(
            self.head_run("retro", TaskRef.standing("retro")),
            memory_access.standing_subject("retro"),
            data_dir=self.data_dir,
            now=100,
        )
        steward = memory_access.issue_grant(
            self.head_run("steward", TaskRef.standing("steward")),
            memory_access.standing_subject("steward"),
            data_dir=self.data_dir,
            now=100,
        )
        for grant in (curator, retro):
            resolved = self.resolve(grant)
            assert isinstance(resolved, memory_access.MemoryReadIdentity)
            self.assertIsNone(resolved.scopes)
        resolved = self.resolve(steward)
        assert isinstance(resolved, memory_access.MemoryReadIdentity)
        self.assertEqual(resolved.scopes, frozenset({"project:ummanu", "product:ummanu"}))

    def test_malformed_requested_scope_is_a_typed_denial_with_resolved_identity(self) -> None:
        grant = memory_access.issue_grant(
            self.head_run("worker", TaskRef.card("card-6")),
            memory_access.card_subject("card-6", "alpha"),
            data_dir=self.data_dir,
            now=100,
        )
        resolved = self.resolve(grant)
        assert isinstance(resolved, memory_access.MemoryReadIdentity)
        denial = memory_access.narrow(resolved, "pro ject")
        assert isinstance(denial, memory_access.MemoryAccessDenial)
        self.assertEqual(denial.response(), {"status": "denied", "error": "scope_malformed"})
        self.assertEqual(denial.identity, resolved)

    def test_scheduled_launch_helper_issues_a_heartbeat_bound_grant(self) -> None:
        run = self.head_run("curator", TaskRef.standing("curator"))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                grant_env.main(
                    [
                        "--head-run",
                        json.dumps(run.to_json()),
                        "--subject",
                        json.dumps(memory_access.standing_subject("curator")),
                        "--data-dir",
                        str(self.data_dir),
                    ]
                ),
                0,
            )
        token = output.getvalue().strip().split("=", 1)[1]
        with mock.patch.object(memory_access, "head_process_status", return_value={"state": "live-match"}):
            identity = memory_access.resolve_token(token, data_dir=self.data_dir)
        self.assertIsInstance(identity, memory_access.MemoryReadIdentity)


if __name__ == "__main__":
    unittest.main()
