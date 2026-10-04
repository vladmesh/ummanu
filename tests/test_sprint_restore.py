"""Sprint entities through export, checkpoint and restore (secretary-819).

Recovery used to rebuild the Pipeline cards of a sprint and drop the sprint itself:
goal, Definition of Done, repositories, status, budget, current task, resume and every
record to the entity lived only on the live board. These pin the whole path: a filled
closed sprint is exported, restored into a separate empty backend, and compared field by
field against its source.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests.fakes.sprints import SprintBackendFixture, _write_project_registry
from tests.observer_identity import as_observer
from tests.sprint_close_fixtures import close_decisions
from ummanu.data import export_board, init_layout, normalize_sprint_entity
from ummanu.restore import (
    RestoreError,
    import_normalized_board,
    restore_findings,
    restore_state,
)
from ummanu.sprint_observer import head_choice, none_choice
from ummanu.sprints import (
    SprintReader,
    SprintWriter,
    sprint_admission_lock,
)
from ummanu.tasks import TaskReader, TaskWriter


def _root(name: str) -> str:
    """A declared repository root as the row stores it: canonical and absolute.

    Admission refuses to resolve a stored root itself, so an export standing in for rows
    this installation wrote carries the canonical form too.
    """
    return str(Path(name).resolve())


CARD_EXPORT = {
    "id": 13,
    "reference": "ummanu-13",
    "title": "Linked card",
    "description": "card body",
    "column": "Ready",
    "swimlane": "",
    "position": 1,
    "task_type": "code",
    "project": "ummanu",
    "metadata": {
        "record_type": "task",
        "complexity": "standard",
        "family_preference": "auto",
        "sprint_ref": "sprint:entity",
    },
    "comments": [],
}
RESUME = {
    "selected_step": "restore the entity",
    "selected_why": "the checkpoint carries it",
    "rejected_alternatives": "recreate it by hand",
    "current_task": "ummanu-12",
    "dod_state": "tests pending",
    "next_safe_step": "run the suite",
    "recorded_at": "2026-07-20T00:00:00Z",
}


class SprintRestoreTests(SprintBackendFixture, unittest.TestCase):
    """Reusable export/restore contract with one backend factory boundary."""

    def persisted_reference_count(self, client: object, reference: str) -> int:
        """Count a reference through the backend client without reading fake rows."""
        total = 0
        for name in ("Pipeline", "Ummanu sprints"):
            project = client.call("getProjectByName", name=name)  # type: ignore[attr-defined]
            if not isinstance(project, dict) or not project.get("id"):
                continue
            for status_id in (1, 0):
                rows = client.call(  # type: ignore[attr-defined]
                    "getAllTasks", project_id=int(project["id"]), status_id=status_id
                )
                total += sum(row.get("reference") == reference for row in rows)
        return total

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source_data = self.root / "source-data"
        self.target_data = self.root / "target-data"
        init_layout(self.source_data)
        init_layout(self.target_data)
        self.source = self.make_sprint_client()
        self.instance = _write_project_registry(self.root, "ummanu", "secretary-instance")
        self.ref = self._seed_closed_sprint()
        self._export()

    def test_sprint_comments_have_only_the_shared_restore_representation(self) -> None:
        self.assertFalse(hasattr(SprintWriter, "restore_comment"))

    def test_quoted_owner_decisions_and_paid_budget_roundtrip_without_reapplying_grants(self) -> None:
        from ummanu.board.owner_decisions import attributed

        path = self.target_data / "board" / "sprints.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        entries = attributed([
            {"id": "grant", "kind": "e2e_grant", "scope": "sprint", "value": 2, "quotation": "Two more runs."},
            {"id": "stop", "kind": "e2e_refusal", "scope": "sprint", "value": "no_more_e2e", "quotation": "No more e2e."},
        ], {"actor": {"role": "po", "id": "po"}, "event_id": "evt_source", "request_id": "source",
            "occurred_at": "2026-10-04T00:00:00Z"})
        e2e = {"budget": 5, "used": 1, "charges": [{"card": "ummanu-13", "dispatch_id": "paid", "at": "2026-10-04T00:00:00Z"}]}
        payload["sprints"][0].update(owner_decisions=entries, e2e=e2e)
        path.write_text(json.dumps(payload), encoding="utf-8")
        client, _count = self._restore()
        live = SprintReader(client, data_dir=self.target_data).show(self.ref)
        self.assertEqual(live["owner_decisions"], entries)
        self.assertEqual(normalize_sprint_entity(live)["e2e"], e2e)
        self.assertEqual(client.call("getSprintE2eBudget", sprint_ref=self.ref)["refusal"]["id"], "stop")
        self._restore(client)
        self.assertEqual(normalize_sprint_entity(SprintReader(client).show(self.ref))["e2e"], e2e)

    def test_local_run_vectors_restore_export_and_replay_with_parity(self) -> None:
        entries = [{"project": "ummanu", "argv": ["python3", "-m", "tests.probe", "two words", ""], "rationale": "owner's exact probe"}]
        path = self.target_data / "board" / "sprints.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["sprints"][0]["local_run_exceptions"] = entries
        path.write_text(json.dumps(payload), encoding="utf-8")
        client, _count = self._restore()
        live = SprintReader(client, data_dir=self.target_data).show(self.ref)
        self.assertEqual(live["local_run_exceptions"], entries)
        self.assertEqual(normalize_sprint_entity(live)["local_run_exceptions"], entries)
        self._restore(client)
        self.assertEqual(self.persisted_reference_count(client, self.ref), 1)
        self.assertEqual(restore_state(self.target_data)["sprint_parity"], "complete")

    def test_empty_local_run_default_restores_like_an_old_export(self) -> None:
        path = self.target_data / "board" / "sprints.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["sprints"][0]["local_run_exceptions"] = []
        path.write_text(json.dumps(payload), encoding="utf-8")
        client, _count = self._restore()
        self.assertEqual(SprintReader(client).show(self.ref)["local_run_exceptions"], [])
        self.assertEqual(restore_state(self.target_data)["sprint_parity"], "complete")

    def _seed_closed_sprint(self) -> str:
        writer = SprintWriter(  # type: ignore[arg-type]
            self.source,
            data_dir=self.source_data,
            instance=self.instance,
        )
        ref = writer.create(
            role="po",
            actor="operator",
            goal="Ship sprint entities into recovery",
            definition_of_done="restore rebuilds the entity",
            reference="sprint:entity",
            repositories=["ummanu", "secretary-instance"],
            product="ummanu",
            issues=["issue:open"],
            projects=["ummanu", "secretary-instance"],
            observer=head_choice("codex-observer"),
            request_id="seed-create",
        )["sprint"]["ref"]
        with as_observer(ref):
            card = TaskWriter(self.source, data_dir=self.source_data).create(  # type: ignore[arg-type]
                # The sprint holds `ummanu`, so its own observer is the writer of its cards.
                role="observer",
                actor="observer",
                project="ummanu",
                task_type="code",
                title="linked",
                target="ready",
                sprint=ref,
                request_id="seed-card",
            )["task"]
        writer.comment(
            role="po", actor="operator", reference=ref, body="first note", request_id="seed-comment"
        )
        writer.record_budget(
            role="po", actor="operator", reference=ref, event_type="red_ci", request_id="seed-budget"
        )
        writer.set_current_task(
            role="po",
            actor="operator",
            reference=ref,
            task_reference=card["ref"],
            request_id="seed-current",
        )
        writer.resume(role="po", actor="operator", reference=ref, entry=RESUME, request_id="seed-resume")
        writer.close(
            role="po",
            actor="operator",
            reference=ref,
            request_id="seed-close",
            decisions=close_decisions(writer, ref),
        )
        return ref

    def _card_reader(self, client: object = None) -> mock.Mock:
        """Canonical board-export seam; cards are not the subject of these tests.

        Its client is the board the export reads, whose card audit is the export's gate.
        """
        return mock.Mock(
            export=mock.Mock(return_value=[CARD_EXPORT]),
            client=client if client is not None else self.source,
        )

    def _export(self) -> None:
        export_board(
            self.source_data,
            instance_dir=self.instance,
            reader=self._card_reader(),  # type: ignore[arg-type]
            sprint_client=self.source,
        )
        # Only the normalized export travels; the target backend starts from nothing else.
        for name in ("cards.json", "sprints.json"):
            shutil.copy(self.source_data / "board" / name, self.target_data / "board" / name)

    def _exported_sprint(self) -> dict:
        payload = json.loads((self.target_data / "board" / "sprints.json").read_text(encoding="utf-8"))
        return payload["sprints"][0]

    def _restore(self, client: object | None = None) -> tuple[object, int]:
        client = client or self.make_target_client()
        return client, import_normalized_board(
            self.target_data,
            client=client,
            instance=self.instance,  # type: ignore[arg-type]
        )

    def test_export_carries_the_sprint_set_next_to_the_cards(self) -> None:
        summary = json.loads((self.source_data / "board" / "export.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["card_count"], 1)
        self.assertEqual(summary["sprint_count"], 1)
        lines = (self.source_data / "board" / "sprints.ndjson").read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(line)["reference"] for line in lines], [self.ref])
        exported = self._exported_sprint()
        self.assertEqual(exported["status"], "closed")
        self.assertEqual(
            exported["repositories"],
            [_root("ummanu"), _root("secretary-instance")],
        )
        self.assertEqual(exported["budget"]["by_type"]["red_ci"], 1)
        self.assertEqual(exported["current_task"], "ummanu-13")
        self.assertEqual(exported["resume"]["selected_step"], RESUME["selected_step"])
        self.assertEqual(
            [comment["text"] for comment in exported["comments"]],
            ["[po]\nfirst note", "[sprint:resume]\n" + RESUME["selected_step"]],
        )

    def test_closed_sprint_is_rebuilt_field_by_field_in_an_empty_backend(self) -> None:
        client, cards = self._restore()

        self.assertEqual(cards, 1)
        exported = self._exported_sprint()
        live = SprintReader(client, data_dir=self.target_data).show(self.ref)  # type: ignore[arg-type]
        self.assertEqual(live["goal"], exported["goal"])
        self.assertEqual(live["definition_of_done"], exported["definition_of_done"])
        self.assertEqual(live["repositories"], exported["repositories"])
        self.assertEqual(live["product"], "ummanu")
        self.assertEqual(live["issues"], exported["issues"])
        self.assertEqual(live["reservations"], exported["reservations"])
        self.assertEqual(live["status"], "closed")
        self.assertEqual(live["budget"]["by_type"], exported["budget"]["by_type"])
        self.assertEqual(live["budget"]["total"], 1)
        self.assertEqual(live["current_task"], exported["current_task"])
        self.assertEqual(live["resume"], exported["resume"])
        self.assertEqual(
            [comment["body"] for comment in live["comments"]],
            [comment["text"] for comment in exported["comments"]],
        )
        # The entity came back on a new row and the dates it was restored from stay readable
        # as explicit provenance. Timestamps are rendered to whole seconds, so two distinct
        # writes in the same second are allowed to have the same displayed value.
        self.assertEqual(live["audit"]["source"], exported["audit"])
        self.assertEqual(normalize_sprint_entity(live), exported)
        self.assertEqual(restore_state(self.target_data)["sprint_count"], 1)
        self.assertEqual(restore_state(self.target_data)["sprint_parity"], "complete")
        self.assertEqual(
            restore_findings(self.target_data),
            ["memory index has not been rebuilt", "managed reconcile has not been applied"],
        )

    def test_the_observer_declaration_survives_a_round_trip(self) -> None:
        client, _ = self._restore()

        live = SprintReader(client, data_dir=self.target_data).show(self.ref)  # type: ignore[arg-type]
        self.assertEqual(live["observer"], head_choice("codex-observer"))
        self.assertEqual(self._exported_sprint()["observer"], head_choice("codex-observer"))

    def test_an_invalid_observer_value_stops_the_restore_before_the_first_write(self) -> None:
        """Validated as a set, before the Pipeline cards, not at the sprint step that follows them.

        Sprint entities are written after every card, so validating them where they are written
        would leave a fully restored card board behind the refusal.
        """
        payload = json.loads((self.target_data / "board" / "sprints.json").read_text(encoding="utf-8"))
        payload["sprints"][0]["observer"] = {"kind": "default"}
        (self.target_data / "board" / "sprints.json").write_text(json.dumps(payload), encoding="utf-8")
        client = self.make_target_client()

        with self.assertRaisesRegex(RestoreError, "not one of the tagged forms"):
            import_normalized_board(self.target_data, client=client)  # type: ignore[arg-type]

        # The Pipeline card of the same export is untouched too: nothing of either set was written.
        self.assertEqual(self.persisted_record_count(client), 0)

    def test_an_export_missing_an_observer_is_refused(self) -> None:
        """A row without the field is refused, and the refusal names both ways it happens.

        A pre-migration export is not corrupt, only older than the field, and the archive carries
        nothing that tells the two apart. Naming only corruption would send the operator looking
        for damage that is not there, so the message names both and gives the one repair.
        """
        payload = json.loads((self.target_data / "board" / "sprints.json").read_text(encoding="utf-8"))
        payload["sprints"][0].pop("observer")
        (self.target_data / "board" / "sprints.json").write_text(json.dumps(payload), encoding="utf-8")
        client = self.make_target_client()

        with self.assertRaises(RestoreError) as caught:
            import_normalized_board(self.target_data, client=client)  # type: ignore[arg-type]

        message = str(caught.exception)
        self.assertIn("carries no observer field", message)
        self.assertIn("either corrupt or was taken before the observer migration", message)
        self.assertIn("state/board/sprints.json", message)
        # Diagnosis only: the refusal is still whole-set and nothing reached the backend.
        self.assertEqual(self.persisted_record_count(client), 0)

    def test_a_declared_head_the_registry_no_longer_has_is_refused(self) -> None:
        payload = json.loads((self.target_data / "board" / "sprints.json").read_text(encoding="utf-8"))
        payload["sprints"][0]["status"] = "open"
        payload["sprints"][0]["observer"] = {"kind": "head", "profile": "retired-observer"}
        (self.target_data / "board" / "sprints.json").write_text(json.dumps(payload), encoding="utf-8")
        client = self.make_target_client()

        with self.assertRaisesRegex(RestoreError, "not a profile of this installation"):
            import_normalized_board(  # type: ignore[arg-type]
                self.target_data,
                client=client,
                instance=self.instance,
            )

        self.assertEqual(self.persisted_record_count(client), 0)

    def test_a_second_disaster_keeps_the_declared_observer(self) -> None:
        """The checkpoint of a recovered installation recovers the same declared row again."""
        first, _ = self._restore()
        export_board(
            self.target_data,
            instance_dir=self.instance,
            reader=self._card_reader(first),  # type: ignore[arg-type]
            sprint_client=first,
        )
        second_data = self.root / "second-recovery"
        shutil.copytree(self.target_data, second_data)

        second = self.make_target_client()
        import_normalized_board(second_data, client=second)  # type: ignore[arg-type]

        live = SprintReader(second, data_dir=second_data).show(self.ref)  # type: ignore[arg-type]
        self.assertEqual(live["observer"], head_choice("codex-observer"))

    def test_an_open_row_is_refused_when_its_export_carries_provenance(self) -> None:
        payload = json.loads((self.target_data / "board" / "sprints.json").read_text(encoding="utf-8"))
        payload["sprints"][0]["status"] = "open"
        payload["sprints"][0]["observer"] = {
            "kind": "historical",
            "profile": None,
            "source": "migration_unknown",
        }
        (self.target_data / "board" / "sprints.json").write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaisesRegex(RestoreError, "may not carry migration provenance"):
            self._restore()

    def _two_open_rows(self, **overrides: object) -> dict:
        """An export carrying the seeded row open, plus a second open row beside it."""
        path = self.target_data / "board" / "sprints.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["sprints"][0]["status"] = "open"
        second = dict(payload["sprints"][0]) | {
            "reference": "sprint:collision",
            "current_task": "",
        } | overrides
        payload["sprints"].append(second)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    def _set_open_sprint_limit(self, value: int) -> None:
        # Set the one line and keep the rest: the installation's `data_dir` is where its generated
        # head registry lives (ummanu-26), so replacing the whole file would unplace it.
        instance_file = self.instance / "instance.yaml"
        kept = [
            line
            for line in instance_file.read_text(encoding="utf-8").splitlines()
            if not line.startswith("open_sprint_limit:")
        ]
        instance_file.write_text("\n".join([*kept, f"open_sprint_limit: {value}"]) + "\n", encoding="utf-8")

    def test_restore_refuses_an_export_of_open_sprints_admission_would_have_refused(self) -> None:
        """Restore reproduces rows one by one, so the set is judged once, before the first write.

        Otherwise an archive is the way around admission: two open sprints sharing a
        product, a reservation and a repository tree would come back exactly as the rules
        refuse to create them.
        """
        self._two_open_rows()
        client = self.make_target_client()

        with self.assertRaisesRegex(RestoreError, "not admissible"):
            import_normalized_board(
                self.target_data,
                client=client,
                instance=self.instance,  # type: ignore[arg-type]
            )

        self.assertEqual(self.persisted_record_count(client), 0)

    def test_restore_at_the_pilot_limit_judges_the_open_set_by_the_same_rules(self) -> None:
        """Two open rows come back only when they satisfy every rule `create` enforces."""
        self._set_open_sprint_limit(2)
        for name, overrides, message in (
            (
                "product",
                {
                    "product": "ummanu",
                    "reservations": ["other"],
                    "repositories": [_root("other")],
                    "observer": none_choice(),
                },
                "needs a different product",
            ),
            (
                "reservation",
                {"product": "other", "repositories": [_root("other")], "reservations": ["ummanu"]},
                "already reserved by an open sprint",
            ),
            (
                "repository",
                {
                    "product": "other",
                    "reservations": ["other"],
                    "repositories": [_root("ummanu/nested")],
                    "observer": none_choice(),
                },
                "overlaps",
            ),
        ):
            with self.subTest(collision=name):
                self.setUp()
                self._set_open_sprint_limit(2)
                self._two_open_rows(**overrides)
                client = self.make_target_client()

                with self.assertRaisesRegex(RestoreError, message):
                    import_normalized_board(
                        self.target_data,
                        client=client,
                        instance=self.instance,  # type: ignore[arg-type]
                    )

                self.assertEqual(self.persisted_record_count(client), 0)

        # Disjoint on everything the rules judge, and each row carrying its own observer
        # head: a declared head is no longer something the open set is refused for.
        self.setUp()
        self._set_open_sprint_limit(2)
        self._two_open_rows(
            product="other",
            reservations=["other"],
            repositories=[_root("other")],
            observer=head_choice("codex-observer"),
        )
        client, _ = self._restore()

        reader = SprintReader(client, data_dir=self.target_data)  # type: ignore[arg-type]
        self.assertEqual(
            sorted(sprint["ref"] for sprint in reader.list(statuses={"open"})),
            ["sprint:collision", "sprint:entity"],
        )

    def test_restore_refuses_an_open_row_whose_root_is_not_canonical(self) -> None:
        """An archive is not a way to publish an open row admission could not judge.

        A relative root names a different tree from every process that reads it, so the
        set check refuses it rather than resolving it against the working directory
        recovery happens to run in.
        """
        self._set_open_sprint_limit(2)
        self._two_open_rows(
            product="other",
            reservations=["other"],
            repositories=["../elsewhere"],
            observer=none_choice(),
        )
        client = self.make_target_client()

        with self.assertRaisesRegex(
            RestoreError,
            "repository root '../elsewhere', which is not an absolute path",
        ):
            import_normalized_board(
                self.target_data,
                client=client,
                instance=self.instance,  # type: ignore[arg-type]
            )

        self.assertEqual(self.persisted_record_count(client), 0)

    def test_restore_refuses_a_lone_open_row_whose_root_is_not_canonical(self) -> None:
        """One open row is the reachable shape: pre-fix creates could only make one.

        An export of an installation that ran before roots were canonicalized carries a
        single open sprint declaring `.`.  Judging it only against the other open sprints
        would inspect nothing at all here, and recovery would publish the row.
        """
        self._set_open_sprint_limit(2)
        path = self.target_data / "board" / "sprints.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["sprints"][0]["status"] = "open"
        payload["sprints"][0]["repositories"] = ["."]
        path.write_text(json.dumps(payload), encoding="utf-8")
        client = self.make_target_client()

        with self.assertRaisesRegex(
            RestoreError,
            "repository root '.', which is not an absolute path",
        ):
            import_normalized_board(
                self.target_data,
                client=client,
                instance=self.instance,  # type: ignore[arg-type]
            )

        self.assertEqual(self.persisted_record_count(client), 0)

    def _legacy_open_row_beside_the_seeded_one(self) -> None:
        """The seeded row open, plus an open row from before sprints owned a product.

        The legacy reference sorts after the seeded one, so it is the candidate the set
        check judges second: the order that used to let it through.
        """
        path = self.target_data / "board" / "sprints.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["sprints"][0]["status"] = "open"
        payload["sprints"][0]["observer"] = none_choice()
        legacy = dict(payload["sprints"][0]) | {
            "reference": "sprint:z-legacy",
            "repositories": ["separate-repository"],
            "current_task": "",
        }
        for field in ("product", "issues", "reservations"):
            legacy.pop(field, None)
        payload["sprints"].append(legacy)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_restore_refuses_a_second_open_row_that_declares_no_product(self) -> None:
        """Ownership absence is refused on whichever side of the comparison it lands.

        A pre-ownership row is a valid export, and nothing proves it disjoint from the
        sprint beside it. Judging only the already-admitted side made the answer depend
        on which reference sorted first, so the same pair was refused one way round and
        restored the other.
        """
        self._set_open_sprint_limit(2)
        self._legacy_open_row_beside_the_seeded_one()
        client = self.make_target_client()

        with self.assertRaisesRegex(RestoreError, "sprint:z-legacy: this sprint declares no product"):
            import_normalized_board(
                self.target_data,
                client=client,
                instance=self.instance,  # type: ignore[arg-type]
            )

        self.assertEqual(self.persisted_record_count(client), 0)

    def test_restore_holds_the_admission_lock_from_its_check_to_its_write(self) -> None:
        """Recovery publishes open sprints, so it admits a set and must serialize like one.

        A `create` that read the board between restore's check and its write would see no
        restored sprint, admit itself, and leave the installation holding both.
        """
        self._set_open_sprint_limit(2)
        self._two_open_rows(
            product="other",
            reservations=["other"],
            repositories=[_root("other")],
            observer=none_choice(),
        )
        blocked: list[str] = []
        contenders: list[threading.Thread] = []

        def contender_blocked() -> bool:
            """Whether a second admission on this data dir has to wait for the restore."""
            entered = threading.Event()

            def acquire() -> None:
                with sprint_admission_lock(self.target_data):
                    entered.set()

            thread = threading.Thread(target=acquire, daemon=True)
            contenders.append(thread)
            thread.start()
            self.addCleanup(thread.join, 5)
            return not entered.wait(0.2)

        import ummanu.restore as restore_module

        check = restore_module._check_restored_admission
        publish = restore_module._import_sprints

        def checked(*args: object, **kwargs: object) -> object:
            blocked.append(f"check:{contender_blocked()}")
            return check(*args, **kwargs)  # type: ignore[arg-type]

        def published(*args: object, **kwargs: object) -> object:
            blocked.append(f"publish:{contender_blocked()}")
            return publish(*args, **kwargs)  # type: ignore[arg-type]

        with (
            mock.patch.object(restore_module, "_check_restored_admission", checked),
            mock.patch.object(restore_module, "_import_sprints", published),
        ):
            client, _ = self._restore()

        self.assertEqual(blocked, ["check:True", "publish:True"])
        reader = SprintReader(client, data_dir=self.target_data)  # type: ignore[arg-type]
        self.assertEqual(
            sorted(sprint["ref"] for sprint in reader.list(statuses={"open"})),
            ["sprint:collision", "sprint:entity"],
        )
        # And it is released again: the installation is not left unable to admit anything.
        for thread in contenders:
            thread.join(5)
        self.assertFalse(contender_blocked())

    def test_pipeline_cards_still_restore_alongside_the_entities(self) -> None:
        client, cards = self._restore()

        self.assertEqual(cards, 1)
        self.assertEqual(self.persisted_reference_count(client, "ummanu-13"), 1)
        self.assertEqual(TaskReader(client).show("ummanu-13")["ref"], "ummanu-13")  # type: ignore[arg-type]
        self.assertEqual(restore_state(self.target_data)["board_parity"], "complete")

    def test_repeated_restore_creates_one_entity_and_no_duplicate_records(self) -> None:
        client, _ = self._restore()
        namespace = restore_state(self.target_data)["restore_namespace"]

        # The retry meets the durable audit its own first pass wrote, and stays on the
        # same namespace: the entities that audit names are the ones this backend holds.
        self._restore(client)

        self.assertEqual(restore_state(self.target_data)["restore_namespace"], namespace)
        self.assertEqual(self.persisted_reference_count(client, self.ref), 1)
        live = SprintReader(client, data_dir=self.target_data).show(self.ref)  # type: ignore[arg-type]
        self.assertEqual(
            [comment["body"] for comment in live["comments"]],
            [comment["text"] for comment in self._exported_sprint()["comments"]],
        )

    def test_second_disaster_restores_from_the_checkpoint_of_the_first_recovery(self) -> None:
        """A recovered instance is itself recoverable.

        The copied restore state may carry a namespace whose old events are foreign to this target,
        or no target-local events at all. In either case request ownership on the new backend cannot
        short-circuit the writes.
        """
        first, _ = self._restore()
        # The checkpoint of the recovered instance: its own export, its own audit.
        export_board(
            self.target_data,
            instance_dir=self.instance,
            reader=self._card_reader(first),  # type: ignore[arg-type]
            sprint_client=first,
        )
        second_data = self.root / "second-data"
        shutil.copytree(self.target_data, second_data)

        second = self.make_target_client()
        self.assertEqual(import_normalized_board(second_data, client=second), 1)  # type: ignore[arg-type]

        live = SprintReader(second, data_dir=second_data).show(self.ref)  # type: ignore[arg-type]
        exported = self._exported_sprint()
        self.assertEqual(live["goal"], exported["goal"])
        self.assertEqual(live["status"], "closed")
        self.assertEqual(live["repositories"], exported["repositories"])
        self.assertEqual(live["budget"]["by_type"], exported["budget"]["by_type"])
        self.assertEqual(live["current_task"], exported["current_task"])
        self.assertEqual(live["resume"], exported["resume"])
        self.assertEqual(
            [comment["body"] for comment in live["comments"]],
            [comment["text"] for comment in exported["comments"]],
        )
        self.assertEqual(normalize_sprint_entity(live), exported)
        self.assertEqual(restore_state(second_data)["sprint_parity"], "complete")
        self.assertEqual(
            [task["reference"] for task in second.tasks if task["reference"] == self.ref],
            [self.ref],  # type: ignore[attr-defined]
        )

    def test_recovery_interrupted_before_the_sprint_step_reports_it_unfinished(self) -> None:
        # Doctor treats a restore state with no sprint key as one that predates sprint
        # entities. A recovery that started under this build records the step from the
        # first live write, so an interruption cannot be read as nothing left to do.
        with (
            mock.patch("ummanu.restore._import_sprints", side_effect=RestoreError("stopped")),
            self.assertRaisesRegex(RestoreError, "stopped"),
        ):
            import_normalized_board(self.target_data, client=self.make_target_client())  # type: ignore[arg-type]

        self.assertEqual(restore_state(self.target_data)["sprints"], "pending")
        self.assertIn("sprint restore is incomplete", restore_findings(self.target_data))

    def test_foreign_sprint_on_the_target_board_stops_the_restore(self) -> None:
        (self.target_data / "board" / "cards.json").write_text(
            json.dumps({"version": 1, "cards": []}), encoding="utf-8"
        )
        path = self.target_data / "board" / "sprints.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["sprints"][0]["current_task"] = ""
        path.write_text(json.dumps(payload), encoding="utf-8")
        client = self.make_target_client()
        SprintWriter(client, data_dir=self.target_data).restore_create(  # type: ignore[arg-type]
            goal="someone else's sprint",
            reference="sprint:foreign",
            request_id="foreign",
        )

        with self.assertRaisesRegex(RestoreError, "sprint board is not empty"):
            import_normalized_board(self.target_data, client=client)  # type: ignore[arg-type]

    def test_invalid_sprint_export_is_refused_before_any_live_write(self) -> None:
        path = self.target_data / "board" / "sprints.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["sprints"][0]["status"] = "archived"
        path.write_text(json.dumps(payload), encoding="utf-8")
        client = self.make_target_client()

        with self.assertRaisesRegex(RestoreError, "invalid status"):
            import_normalized_board(self.target_data, client=client)  # type: ignore[arg-type]

        self.assertEqual(self.persisted_record_count(client), 0)

    def test_sql_restore_refuses_an_unlinked_current_task_before_writes(self) -> None:
        path = self.target_data / "board" / "sprints.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["sprints"][0]["current_task"] = "ummanu:not-in-export"
        path.write_text(json.dumps(payload), encoding="utf-8")
        client = self.make_target_client()

        with self.assertRaisesRegex(RestoreError, "not an included Card already linked"):
            import_normalized_board(self.target_data, client=client, instance=self.instance)
        self.assertEqual(self.persisted_record_count(client), 0)


if __name__ == "__main__":
    unittest.main()
