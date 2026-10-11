"""Forward terminal taxonomy contracts independent of lifecycle authority."""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime

from ummanu.board.models import Actor, EntityKind, Event, EventKind
from ummanu.board.terminal_taxonomy import (
    TerminalTaxonomyValidationError,
    budget_event_type,
    normalize_terminal_taxonomy,
    read_terminal_taxonomy,
)


class TerminalTaxonomyTests(unittest.TestCase):
    def test_all_dispositions_are_independent_and_nonblocked_have_no_reason(self) -> None:
        for disposition in ("release", "rework", "reslice", "drop", "operator_stop"):
            with self.subTest(disposition=disposition):
                taxonomy = normalize_terminal_taxonomy(disposition=disposition)
                self.assertEqual((taxonomy.disposition, taxonomy.blocked_reason), (disposition, None))

    def test_forward_task_contract_mapping_keeps_the_worker_evidence(self) -> None:
        taxonomy = normalize_terminal_taxonomy(disposition="blocked", blocked_reason="wrong_task_definition")
        self.assertEqual(taxonomy.blocked_reason, "task_contract")
        self.assertEqual(taxonomy.source_evidence, "wrong_task_definition")

    def test_all_forward_blocked_reasons_are_typed(self) -> None:
        for reason in (
            "implementation",
            "review",
            "task_contract",
            "gate",
            "provider",
            "infrastructure",
            "operator",
            "other",
        ):
            with self.subTest(reason=reason):
                taxonomy = normalize_terminal_taxonomy(disposition="blocked", blocked_reason=reason)
                self.assertEqual((taxonomy.blocked_reason, taxonomy.source_evidence), (reason, reason))

    def test_external_fact_remains_evidence_without_invented_precision(self) -> None:
        taxonomy = normalize_terminal_taxonomy(disposition="blocked", blocked_reason="external_fact")
        self.assertEqual(taxonomy.blocked_reason, "other")
        self.assertEqual(taxonomy.source_evidence, "external_fact")

    def test_invalid_new_values_are_rejected_at_the_named_boundary(self) -> None:
        with self.assertRaisesRegex(TerminalTaxonomyValidationError, "disposition"):
            normalize_terminal_taxonomy(disposition="merge")
        with self.assertRaisesRegex(TerminalTaxonomyValidationError, "blocked reason"):
            normalize_terminal_taxonomy(disposition="blocked", blocked_reason="free-form prose")
        with self.assertRaisesRegex(TerminalTaxonomyValidationError, "exactly"):
            normalize_terminal_taxonomy(disposition="release", blocked_reason="operator")

    def test_missing_and_legacy_reads_are_explicitly_legacy_other(self) -> None:
        taxonomy = read_terminal_taxonomy({}, disposition="blocked")
        self.assertEqual(
            (taxonomy.blocked_reason, taxonomy.source_evidence, taxonomy.provenance),
            ("other", None, "legacy"),
        )

    def test_malformed_forward_record_fails_closed(self) -> None:
        with self.assertRaisesRegex(TerminalTaxonomyValidationError, "field set"):
            read_terminal_taxonomy(
                {"terminal_taxonomy": {"version": 1, "disposition": "blocked"}},
                disposition="blocked",
            )

    def test_supported_integer_versions_read_and_roundtrip(self) -> None:
        expected = normalize_terminal_taxonomy(
            disposition="blocked", blocked_reason="wrong_task_definition"
        )
        for version in (1, 2):
            with self.subTest(version=version):
                record = expected.to_record()
                record["version"] = version
                if version == 1:
                    del record["budget_class"]
                taxonomy = read_terminal_taxonomy(
                    {"terminal_taxonomy": json.loads(json.dumps(record))}, disposition="blocked"
                )
                self.assertEqual(taxonomy, expected)
                serialized = taxonomy.to_record()
                self.assertIs(type(serialized["version"]), int)
                self.assertEqual(serialized["version"], 2)
                self.assertEqual(
                    read_terminal_taxonomy({"terminal_taxonomy": serialized}, disposition="blocked"),
                    expected,
                )

    def test_malformed_json_versions_raise_typed_validation_with_complete_schemas(self) -> None:
        for schema_version in (1, 2):
            for version in (True, False, 1.0, 2.0, "1", "2", None, [], {}, 0, 3, -1):
                with self.subTest(schema_version=schema_version, version=version):
                    record = normalize_terminal_taxonomy(
                        disposition="blocked", blocked_reason="infrastructure"
                    ).to_record()
                    if schema_version == 1:
                        del record["budget_class"]
                    record["version"] = version
                    with self.assertRaisesRegex(TerminalTaxonomyValidationError, "version"):
                        read_terminal_taxonomy(
                            {"terminal_taxonomy": json.loads(json.dumps(record))}, disposition="blocked"
                        )

    def test_invalid_version_is_rejected_before_field_selection(self) -> None:
        for version in (2.0, [], {}, 3):
            with (
                self.subTest(version=version),
                self.assertRaisesRegex(TerminalTaxonomyValidationError, "version"),
            ):
                read_terminal_taxonomy({"terminal_taxonomy": {"version": version}}, disposition="blocked")

    def test_typed_event_boundary_rejects_unhashable_and_boolean_versions(self) -> None:
        for version in ([], False):
            with self.subTest(version=version):
                record = normalize_terminal_taxonomy(
                    disposition="blocked", blocked_reason="infrastructure"
                ).to_record()
                del record["budget_class"]
                record["version"] = version
                with self.assertRaisesRegex(TerminalTaxonomyValidationError, "version"):
                    Event(
                        event_id="evt-bad-version",
                        kind=EventKind.CARD_BLOCKED,
                        entity_kind=EntityKind.CARD,
                        ref="ummanu-211",
                        actor=Actor("dispatcher", "dispatcher"),
                        reason="blocked",
                        occurred_at=datetime(2026, 10, 11, tzinfo=UTC),
                        source_state="in_progress",
                        target_state="blocked",
                        data={"terminal_taxonomy": json.loads(json.dumps(record))},
                    )

    def test_forward_reslice_keeps_its_disposition_and_charges_blocked(self) -> None:
        taxonomy = read_terminal_taxonomy(
            {
                "terminal_taxonomy": normalize_terminal_taxonomy(disposition="reslice").to_record(),
            },
            disposition=None,
        )
        self.assertEqual(taxonomy.disposition, "reslice")
        self.assertEqual(budget_event_type(taxonomy), "blocked")

    def test_forward_record_persists_the_normalized_budget_class(self) -> None:
        taxonomy = normalize_terminal_taxonomy(disposition="blocked", blocked_reason="infrastructure")
        record = taxonomy.to_record()
        self.assertEqual(record["budget_class"], "infrastructure_blocked")
        self.assertEqual(
            read_terminal_taxonomy({"terminal_taxonomy": record}, disposition="blocked"), taxonomy
        )

        record["budget_class"] = "blocked"
        with self.assertRaisesRegex(TerminalTaxonomyValidationError, "budget class"):
            read_terminal_taxonomy({"terminal_taxonomy": record}, disposition="blocked")

    def test_typed_event_boundary_rejects_malformed_forward_taxonomy(self) -> None:
        with self.assertRaisesRegex(ValueError, "terminal taxonomy"):
            Event(
                event_id="evt-bad-taxonomy",
                kind=EventKind.CARD_BLOCKED,
                entity_kind=EntityKind.CARD,
                ref="ummanu-1533",
                actor=Actor("dispatcher", "dispatcher"),
                reason="blocked",
                occurred_at=datetime.now(UTC),
                source_state="in_progress",
                target_state="blocked",
                data={"terminal_taxonomy": {"version": 1}},
            )

    def test_budget_charge_uses_the_same_normalized_reason(self) -> None:
        infrastructure = normalize_terminal_taxonomy(disposition="blocked", blocked_reason="infrastructure")
        task_contract = normalize_terminal_taxonomy(
            disposition="blocked", blocked_reason="wrong_task_definition"
        )
        self.assertEqual(budget_event_type(infrastructure), "infrastructure_blocked")
        self.assertEqual(budget_event_type(task_contract), "blocked")

    def test_corrected_forward_terminal_causes_remain_charged(self) -> None:
        """Only classified head bring-up failures are uncharged infrastructure."""
        causes = {
            "worker-result": "implementation",
            "gate-exhaustion": "gate",
            "gate-rerun-unavailable": "gate",
            "adopt-head": "other",
            "merge": "implementation",
            "active-mismatch": "other",
        }
        for path, reason in causes.items():
            with self.subTest(path=path):
                taxonomy = normalize_terminal_taxonomy(disposition="blocked", blocked_reason=reason)
                self.assertEqual((taxonomy.blocked_reason, taxonomy.budget_class), (reason, "blocked"))

    def test_older_forward_record_still_wins_over_legacy_action_accounting(self) -> None:
        taxonomy = read_terminal_taxonomy(
            {
                "terminal_taxonomy": {
                    "version": 1,
                    "disposition": "blocked",
                    "blocked_reason": "infrastructure",
                    "source_evidence": "infrastructure",
                    "provenance": "forward",
                }
            },
            disposition="blocked",
        )
        self.assertEqual(
            (taxonomy.provenance, budget_event_type(taxonomy)), ("forward", "infrastructure_blocked")
        )
