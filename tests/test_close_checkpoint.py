"""Close-fact coverage, validation, bindings and parity without a backend."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from tests.close_checkpoint_fixtures import close_facts, write_close
from ummanu.board.close_checkpoint import (
    CloseCheckpointError,
    read_checkpoint_close_facts,
    require_close_parity,
    require_close_set_parity,
    scan_close_secrets,
    validate_close_facts,
    validate_close_merge,
)


class CloseCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.facts = close_facts()

    def test_mixed_subjects_confirmation_actuals_shared_request_and_ties_allow_local_ids(self):
        validate_close_facts(self.facts)
        actual = copy.deepcopy(self.facts)
        for index, row in enumerate(actual["decisions"]):
            row["decision_id"] = index + 50
        require_close_parity(self.facts, actual)

    def test_null_columns_and_declared_zero_are_valid(self):
        for reason, document in ((None, None), ("reason", None), (None, "knowledge/closeout.md")):
            validate_close_facts({"close_reason": reason, "closeout_document": document, "decisions": []})

    def test_confirmation_actual_vocabulary_and_relative_document_are_validated(self):
        for value in ("blocked", "", 3):
            facts = copy.deepcopy(self.facts)
            facts["decisions"][3]["actual"] = value
            with self.subTest(value=value), self.assertRaisesRegex(CloseCheckpointError, "actual"):
                validate_close_facts(facts)
        for document in ("../secret", "/private/document", "", "a/../b", "a\\b"):
            with self.subTest(document=document), self.assertRaisesRegex(CloseCheckpointError, "closeout_document"):
                validate_close_facts({**self.facts, "closeout_document": document})

    def test_global_tie_order_and_prewrite_conflicts_preserve_semantic_identity(self):
        other = close_facts("sprint:other")
        for row in other["decisions"]:
            row["decision_id"] += 100
        expected = {"sprint:closed": self.facts, "sprint:other": other}
        actual = copy.deepcopy(expected)
        for row in actual["sprint:closed"]["decisions"]:
            row["decision_id"] += 200
        with self.assertRaisesRegex(CloseCheckpointError, "global order"):
            require_close_set_parity(expected, actual)
        validate_close_merge(expected, {})
        for changed in (
            {"sprint:closed": {**self.facts, "close_reason": None}},
            {"sprint:closed": {**self.facts, "decisions": [dict(self.facts["decisions"][0], reason="changed")]}},
            {"sprint:extra": other},
        ):
            with self.assertRaises(CloseCheckpointError):
                validate_close_merge(expected, changed)

    def test_invalid_fields_subjects_verdicts_actuals_and_times_refuse_without_prose(self):
        for field, value in (
            ("decision_id", True), ("subject_kind", "other"), ("issue_id", None),
            ("task_ref", "private-value"), ("verdict", "done"), ("actual", "private-value"),
            ("reason", ""), ("reason", []), ("request_id", ""),
            ("decided_at", "2026-09-01T00:00:00"), ("decided_at", "bad"),
        ):
            with self.subTest(field=field, value=value):
                facts = copy.deepcopy(self.facts)
                facts["decisions"][0][field] = value
                with self.assertRaises(CloseCheckpointError) as raised:
                    validate_close_facts(facts)
                self.assertNotIn("private-value", str(raised.exception))
        for field in ("subject_kind", "actual"):
            facts = copy.deepcopy(self.facts)
            del facts["decisions"][0][field]
            with self.assertRaises(CloseCheckpointError):
                validate_close_facts(facts)

    def test_duplicate_scoped_subjects_and_reordered_identities_refuse(self):
        for row in (dict(self.facts["decisions"][0], decision_id=99), self.facts["decisions"][0]):
            facts = copy.deepcopy(self.facts)
            facts["decisions"].append(row)
            with self.assertRaises(CloseCheckpointError):
                validate_close_facts(facts)

    def test_parity_refuses_extra_missing_order_and_every_changed_canonical_field(self):
        variants = [self.facts["decisions"][:-1], self.facts["decisions"][::-1],
                    [*self.facts["decisions"], self.facts["decisions"][0]]]
        for rows in variants:
            with self.assertRaises(CloseCheckpointError):
                require_close_parity(self.facts, {**self.facts, "decisions": rows})
        for field in self.facts["decisions"][0]:
            if field == "decision_id":
                continue
            actual = copy.deepcopy(self.facts)
            actual["decisions"][0][field] = "private-value"
            with self.subTest(field=field), self.assertRaises(CloseCheckpointError):
                require_close_parity(self.facts, actual)

    def _source(self):
        sprints = [{"reference": "sprint:closed", "status": "open", "issues": ["issue:one", "issue:two"], "close_facts": self.facts},
                   {"reference": "sprint:empty", "close_facts": {"close_reason": None, "closeout_document": None, "decisions": []}}]
        cards = [{"reference": f"issue:{name}", "metadata": {"record_type": "issue"}} for name in ("one", "two")]
        cards += [{"reference": f"ummanu-{index}", "metadata": {"sprint_ref": "sprint:closed"}} for index in (1, 2)]
        history = [{"request_id": "close-request", "ref": "sprint:closed"}]
        return sprints, cards, history

    def test_declared_retained_open_facts_and_zero_sprint_bind_to_source(self):
        with tempfile.TemporaryDirectory() as raw:
            board = Path(raw)
            write_close(board, *self._source())
            restored = read_checkpoint_close_facts(board)
            self.assertEqual(restored["sprint:closed"], self.facts)
            self.assertEqual(restored["sprint:empty"]["decisions"], [])

    def test_wrong_membership_request_and_missing_source_refuse(self):
        for kind in ("issue", "card", "request", "source"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as raw:
                board = Path(raw)
                sprints, cards, history = self._source()
                if kind == "issue":
                    sprints[0]["issues"] = []
                elif kind == "card":
                    cards[-1]["metadata"]["sprint_ref"] = "sprint:empty"
                elif kind == "request":
                    history[0]["ref"] = "sprint:empty"
                else:
                    cards.pop(0)
                write_close(board, sprints, cards, history)
                with self.assertRaises(CloseCheckpointError):
                    read_checkpoint_close_facts(board)

    def test_missing_corrupt_unknown_coverage_duplicate_json_and_symlink_refuse(self):
        for damage in ("missing", "count", "digest", "version", "duplicate", "symlink", "undeclared", "json-parity"):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as raw:
                board = Path(raw)
                write_close(board, *self._source())
                path = board / "sprints.ndjson"
                summary_path = board / "export.json"
                summary = json.loads(summary_path.read_text())
                if damage == "missing":
                    path.unlink()
                elif damage in {"count", "digest", "version"}:
                    summary["sprint_close"][{"count": "decision_count", "digest": "sha256", "version": "version"}[damage]] = 99
                elif damage == "duplicate":
                    path.write_text(path.read_text().replace('"close_reason":', '"close_reason": null, "close_reason":', 1))
                elif damage == "symlink":
                    path.rename(board / "elsewhere")
                    path.symlink_to(board / "elsewhere")
                elif damage == "undeclared":
                    del summary["sprint_close"]
                else:
                    (board / "sprints.json").write_text('{"sprints": []}')
                summary_path.write_text(json.dumps(summary))
                with self.assertRaises(CloseCheckpointError):
                    read_checkpoint_close_facts(board)

    def test_legacy_is_unavailable_and_declared_empty_is_complete(self):
        with tempfile.TemporaryDirectory() as raw:
            board = Path(raw)
            (board / "export.json").write_text('{"version": 1}')
            self.assertIsNone(read_checkpoint_close_facts(board))
            write_close(board, [], [], [])
            self.assertEqual(read_checkpoint_close_facts(board), {})

    def test_escaped_known_secret_in_close_reason_and_decision_prose_is_detected(self):
        for location in ("close_reason", "decision"):
            facts = copy.deepcopy(self.facts)
            if location == "decision":
                facts["decisions"][0]["reason"] = "known-secret"
            else:
                facts[location] = "known-secret"
            text = json.dumps({"close_facts": facts}).replace("known-secret", "known-\\u0073ecret")
            with self.subTest(location=location), self.assertRaises(CloseCheckpointError) as raised:
                scan_close_secrets(text, lambda value: value.replace("known-secret", "redacted"))
            self.assertNotIn("known-secret", str(raised.exception))
