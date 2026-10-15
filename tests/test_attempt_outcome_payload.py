"""Typed payload coverage for the closed ``attempt.outcome`` board-event schema."""

from __future__ import annotations

import ast
import inspect
import unittest
from collections import UserDict
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from unittest.mock import patch

from ummanu.board import attempt_outcome, models, terminal_taxonomy
from ummanu.board.attempt_outcome import (
    AttemptOutcomeCompleteness,
    AttemptOutcomeLineageRequired,
    AttemptOutcomePayload,
    AttemptOutcomeSourceEventIds,
    AttemptOutcomeTerminalState,
    AttemptOutcomeUsageCompleteness,
    AttemptOutcomeVerdict,
)
from ummanu.board.events import AttemptOutcomeOccurrence
from ummanu.board.models import Actor, EntityKind, Event, EventKind
from ummanu.board.roles import Role


def outcome_data(version=1):
    """Released wire fixture, independent of the payload serializer/field constants."""
    data = {
        "version": version, "attempt_id": "attempt-1", "attempt": 1,
        "report_generation": 2, "sprint_ref": None, "specification_revision": None,
        "terminal_state": "done", "verdict": "green", "disposition": "accept",
        "blocked_reason": None,
        "source_event_ids": dict.fromkeys(
            ("report", "verdict", "decision", "effect", "worker_usage", "review_usage")
        ),
        "usage_completeness": {"worker": "missing", "review": "legacy"},
    }
    if version == 2:
        data["lineage_required"] = dict.fromkeys(
            ("specification_revision", "report", "verdict", "decision", "effect",
             "worker_usage", "review_usage"), True
        )
    return data


def outcome_event(data, **changes):
    fields = {
        "event_id": "event-outcome", "kind": EventKind.ATTEMPT_OUTCOME,
        "entity_kind": EntityKind.CARD, "ref": "card-1", "actor": Actor(Role.DISPATCHER, "dispatcher"),
        "reason": "confirmed terminal lifecycle effect", "occurred_at": datetime(2026, 9, 17, tzinfo=UTC),
        "data": data,
    }
    fields.update(changes)
    return Event(**fields)


def boundary_vectors():
    """Expected messages are observed contracts, not an implementation oracle."""
    vectors = []

    def add(name, changes, raw=None, typed=None, version=1):
        data = outcome_data(version)
        data.update(changes)
        vectors.append((name, data, raw, typed if typed is not None else raw))

    for version in (1, 2, True, False, 1.0, 2.0, 3, None, "1"):
        error = None if version in (1, 2) else "unsupported attempt outcome version"
        typed = "invalid attempt outcome payload: unsupported attempt outcome version" if version is True else error
        add(f"version-{version!r}", {}, error, typed, version)
    for version in (1, 2):
        for field in tuple(outcome_data(version)):
            data = outcome_data(version)
            del data[field]
            error = "unsupported attempt outcome version" if field == "version" else f"attempt outcome v{version} has an unsupported field set"
            vectors.append((f"v{version}-missing-{field}", data, error, error))
        add(f"v{version}-extra", {"future": 1}, f"attempt outcome v{version} has an unsupported field set", version=version)
    add("v1-lineage", {"lineage_required": {}}, "attempt outcome v1 has an unsupported field set")
    for field in ("attempt_id", "attempt", "report_generation", "sprint_ref", "specification_revision"):
        values = (None, "", " ", 7) if field in ("attempt_id", "sprint_ref", "specification_revision") else (None, 0, -1, True, 1.0, "1")
        for value in values:
            error = (f"attempt outcome requires a positive {field}" if field in ("attempt", "report_generation")
                     else "attempt outcome requires a non-empty attempt_id" if field == "attempt_id"
                     else f"attempt outcome {field} must be a non-empty string or null")
            if value is None and field in ("sprint_ref", "specification_revision"):
                error = None
            add(f"{field}-{value!r}", {field: value}, error,
                f"invalid attempt outcome payload: {error}" if error else None)
    for field, enum, raw in (
        ("terminal_state", "AttemptOutcomeTerminalState", "terminal state must be done, blocked or in_progress"),
        ("verdict", "AttemptOutcomeVerdict", "verdict is unsupported"),
        ("disposition", None, "disposition is unsupported"),
        ("blocked_reason", None, "blocked reason is unsupported"),
    ):
        for value in (None, "", "unknown"):
            if field == "blocked_reason" and value is None:
                continue
            message = f"attempt outcome {raw}"
            detail = f"{value!r} is not a valid {enum}" if enum else message
            add(f"{field}-{value!r}", {field: value}, message, f"invalid attempt outcome payload: {detail}")
    for changes in ({"disposition": "blocked"}, {"blocked_reason": "other"}):
        error = "attempt outcome blocked reason is present exactly for blocked disposition"
        add("blocked-consistency", changes, error, f"invalid attempt outcome payload: {error}")
    for field, noun in (("source_event_ids", "source_event_ids"), ("usage_completeness", "usage completeness"), ("lineage_required", "lineage requiredness")):
        version = 2 if field == "lineage_required" else 1
        error = f"attempt outcome {noun} {'is' if field == 'usage_completeness' else 'are' if field == 'source_event_ids' else 'is'} incomplete"
        for value in (None, [], {}, {"extra": None}):
            add(f"{field}-shape-{value!r}", {field: value}, error, f"invalid attempt outcome payload: {error}", version)
        mapping = UserDict(outcome_data(version)[field])
        add(f"{field}-Mapping", {field: mapping}, error, "", version)
        for missing in outcome_data(version)[field]:
            nested = outcome_data(version)[field]
            del nested[missing]
            add(f"{field}-missing-{missing}", {field: nested}, error, f"invalid attempt outcome payload: {error}", version)
    for field, values in (
        ("terminal_state", ("done", "blocked", "in_progress")),
        ("verdict", ("green", "red", "blocked", "missing", "legacy")),
        ("disposition", ("accept", "rework", "reslice", "drop", "operator_stop")),
        ("blocked_reason", ("implementation", "review", "task_contract", "gate", "provider", "infrastructure", "operator", "other")),
    ):
        for value in values:
            changes = {field: value}
            if field == "blocked_reason":
                changes["disposition"] = "blocked"
            add(f"valid-{field}-{value}", changes)
    add("whitespace-preserved", {"attempt_id": " attempt ", "sprint_ref": " sprint:1 ", "specification_revision": " spec ", "source_event_ids": {**outcome_data()["source_event_ids"], "report": " report "}})
    add("lineage-all-optional", {"lineage_required": dict.fromkeys(outcome_data(2)["lineage_required"], False)}, version=2)
    for name in outcome_data()["source_event_ids"]:
        for value in ("", " ", 9, False):
            refs = outcome_data()["source_event_ids"]
            refs[name] = value
            error = f"attempt outcome source event {name} must be a non-empty string or null"
            add(f"ref-{name}-{value!r}", {"source_event_ids": refs}, error, f"invalid attempt outcome payload: {error}")
    for role in ("worker", "review"):
        for state in ("collected", "degraded", "missing", "legacy", "unknown", None):
            for ref in (None, "usage-1"):
                refs = outcome_data()["source_event_ids"]
                refs[f"{role}_usage"] = ref
                usage = outcome_data()["usage_completeness"]
                usage[role] = state
                error = None
                if state not in ("collected", "degraded", "missing", "legacy"):
                    error = f"attempt outcome {role} completeness is unsupported"
                    typed = f"invalid attempt outcome payload: attempt outcome completeness is unsupported: {state!r} is not a valid AttemptOutcomeCompleteness"
                else:
                    if state in ("collected", "degraded") and ref is None:
                        error = f"attempt outcome {role} completeness requires its usage event ref"
                    if state in ("missing", "legacy") and ref is not None:
                        error = f"attempt outcome {role} missingness carries no usage event ref"
                    typed = f"invalid attempt outcome payload: {error}" if error else None
                add(f"usage-{role}-{state}-{ref}", {"source_event_ids": refs, "usage_completeness": usage}, error, typed)
    for name in outcome_data(2)["lineage_required"]:
        lineage = outcome_data(2)["lineage_required"]
        lineage[name] = 1
        error = "attempt outcome lineage requiredness must be boolean"
        add(f"lineage-{name}", {"lineage_required": lineage}, error, f"invalid attempt outcome payload: {error}", 2)
    add("scalar-before-nested", {"attempt": True, "source_event_ids": None},
        "attempt outcome requires a positive attempt", "invalid attempt outcome payload: attempt outcome source_event_ids are incomplete")
    add("scalar-before-enum", {"attempt_id": "", "verdict": "unknown"},
        "attempt outcome requires a non-empty attempt_id", "invalid attempt outcome payload: 'unknown' is not a valid AttemptOutcomeVerdict")
    add("scalar-before-lineage", {"attempt": 0, "lineage_required": None},
        "attempt outcome requires a positive attempt", "invalid attempt outcome payload: attempt outcome lineage requiredness is incomplete", 2)
    add("worker-link-before-review-state", {"usage_completeness": {"worker": "collected", "review": "unknown"}},
        "attempt outcome worker completeness requires its usage event ref",
        "invalid attempt outcome payload: attempt outcome completeness is unsupported: 'unknown' is not a valid AttemptOutcomeCompleteness")
    refs = dict(reversed(tuple(outcome_data()["source_event_ids"].items())))
    refs.update(report="", review_usage="")
    add("ref-input-order", {"source_event_ids": refs},
        "attempt outcome source event review_usage must be a non-empty string or null",
        "invalid attempt outcome payload: attempt outcome source event report must be a non-empty string or null")
    return vectors


class AttemptOutcomeBoundaryTests(unittest.TestCase):
    def assert_boundary(self, call, expected, prefix=""):
        if not expected:
            return call()
        with self.assertRaises(ValueError) as caught:
            call()
        self.assertIs(type(caught.exception), ValueError)
        self.assertEqual(str(caught.exception), prefix + expected)
        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual(caught.exception.__suppress_context__, bool(prefix) or expected.startswith("invalid attempt outcome payload:"))

    def test_json_container_refusals(self):
        record = outcome_event(outcome_data()).to_record("request-1")
        for value in ([], {}):
            for field in ("version", "terminal_state"):
                data = {**outcome_data(), field: value}
                if field == "version":
                    self.assert_boundary(lambda data=data: outcome_event(data), "unsupported attempt outcome version")
                    self.assert_boundary(lambda data=data: AttemptOutcomePayload.from_data(data), "unsupported attempt outcome version")
                else:
                    for call, exception, prefix in (
                        (lambda data=data: outcome_event(data), TypeError, ""),
                        (lambda data=data: Event.from_record({**record, "data": data}), ValueError, "invalid board event record: "),
                    ):
                        with self.assertRaises(exception) as caught:
                            call()
                        self.assertIs(type(caught.exception), exception)
                        self.assertEqual(str(caught.exception), prefix + f"unhashable type: '{type(value).__name__}'")
                        self.assertIsNone(caught.exception.__cause__)
                        self.assertEqual(caught.exception.__suppress_context__, bool(prefix))
                    self.assert_boundary(lambda data=data: AttemptOutcomePayload.from_data(data), f"invalid attempt outcome payload: {value!r} is not a valid AttemptOutcomeTerminalState")

    def test_wire_boundary_characterization(self):
        record = outcome_event(outcome_data()).to_record("request-1")
        print(f"attempt outcome characterization: {len(boundary_vectors())} wire vectors, three boundaries")
        for name, data, raw_error, typed_error in boundary_vectors():
            before = deepcopy(data)
            for boundary in ("event", "record", "payload"):
                with self.subTest(vector=name, boundary=boundary):
                    if boundary == "event":
                        result = self.assert_boundary(lambda data=data: outcome_event(data), raw_error)
                    elif boundary == "record":
                        result = self.assert_boundary(lambda data=data: Event.from_record({**record, "data": data}), raw_error, "invalid board event record: ")
                    else:
                        result = self.assert_boundary(lambda data=data: AttemptOutcomePayload.from_data(data), typed_error)
                    if result is not None:
                        output = result.to_data() if boundary == "payload" else result.data
                        self.assertEqual(output, data)
                        self.assertIs(type(output["version"]), type(data["version"]))
                        if boundary != "payload":
                            self.assertIs(output, data)
                    self.assertEqual(data, before)

    def test_direct_payload_and_nested_boundaries(self):
        payload = AttemptOutcomePayload.from_data(outcome_data())
        cases = (
            ({"version": True}, "unsupported attempt outcome version"),
            ({"version": False}, "unsupported attempt outcome version"),
            ({"version": 3}, "unsupported attempt outcome version"),
            ({"attempt_id": ""}, "attempt outcome requires a non-empty attempt_id"),
            ({"attempt": True}, "attempt outcome requires a positive attempt"),
            ({"report_generation": False}, "attempt outcome requires a positive report_generation"),
            ({"terminal_state": "done"}, "attempt outcome terminal state must be done, blocked or in_progress"),
            ({"verdict": "green"}, "attempt outcome verdict is unsupported"),
            ({"verdict": AttemptOutcomeTerminalState.BLOCKED}, "attempt outcome verdict is unsupported"),
            ({"source_event_ids": outcome_data()["source_event_ids"]}, "attempt outcome source_event_ids are incomplete"),
            ({"usage_completeness": outcome_data()["usage_completeness"]}, "attempt outcome usage completeness is incomplete"),
            ({"lineage_required": AttemptOutcomeLineageRequired(**outcome_data(2)["lineage_required"])}, "attempt outcome v1 has an unsupported field set"),
            ({"version": 2}, "attempt outcome lineage requiredness is incomplete"),
            ({"version": 2, "lineage_required": outcome_data(2)["lineage_required"]}, "attempt outcome lineage requiredness is incomplete"),
            ({"attempt": 0, "verdict": "unknown"}, "attempt outcome requires a positive attempt"),
        )
        for changes, error in cases:
            with self.subTest(changes=changes):
                self.assert_boundary(lambda changes=changes: replace(payload, **changes), error)
        for version in (1.0, 2.0):
            parsed = AttemptOutcomePayload.from_data(outcome_data(version))
            self.assertIs(type(replace(parsed).version), float)
        for role in ("worker", "review"):
            usage = {"worker": AttemptOutcomeCompleteness.MISSING, "review": AttemptOutcomeCompleteness.LEGACY}
            usage[role] = "missing"
            self.assert_boundary(lambda usage=usage: AttemptOutcomeUsageCompleteness(**usage), f"attempt outcome {role} completeness is unsupported")
        for name in outcome_data()["source_event_ids"]:
            refs = outcome_data()["source_event_ids"]
            refs[name] = " "
            self.assert_boundary(lambda refs=refs: AttemptOutcomeSourceEventIds(**refs), f"attempt outcome source event {name} must be a non-empty string or null")
        for name in outcome_data(2)["lineage_required"]:
            lineage = outcome_data(2)["lineage_required"]
            lineage[name] = None
            self.assert_boundary(lambda lineage=lineage: AttemptOutcomeLineageRequired(**lineage), "attempt outcome lineage requiredness must be boolean")
        self.assert_boundary(lambda: payload.natural_key(" "), "attempt outcome card ref must be non-empty")

    def test_round_trip_order_identity_and_guards(self):
        for version in (1, 2):
            data = outcome_data(version)
            payload = AttemptOutcomePayload.from_data(UserDict(data))
            self.assertEqual(payload.to_data(), data)
            self.assertEqual(payload, replace(payload))
            self.assertFalse(hasattr(payload, "__dict__"))
            with self.assertRaises(FrozenInstanceError):
                payload.attempt = 8
            self.assertEqual(payload.natural_key("card-1"), ("card-1", "attempt-1", 2))
            self.assertEqual(payload.lineage_missing(), () if version == 1 else (
                "specification_revision", "report", "verdict", "decision", "effect", "worker_usage", "review_usage"))
            for key in ("source_event_ids", "usage_completeness", *( ("lineage_required",) if version == 2 else ())):
                self.assertEqual(list(payload.to_data()[key]), list(data[key]))
            self.assertEqual(list(payload.to_data()), list(data))
            event = outcome_event(data)
            record = event.to_record("request-1")
            self.assertEqual(Event.from_record(record), event)
            self.assertEqual(Event.from_record(record).to_record("request-1"), record)
            self.assertIs(record["data"], data)
        self.assert_boundary(lambda: outcome_event({}, entity_kind=EntityKind.PRODUCT), "attempt outcome events require a Card subject")
        self.assertEqual(outcome_event({}, kind=EventKind.ENTITY_UPDATED).data, {})


class AttemptOutcomeOwnershipTests(unittest.TestCase):
    def test_owner_imports_and_compatibility_exports(self):
        owner = ast.parse(inspect.getsource(attempt_outcome))
        product_imports = [
            node.module
            for node in ast.walk(owner)
            if isinstance(node, ast.ImportFrom) and node.module.startswith("ummanu")
        ]
        product_imports.extend(alias.name for node in ast.walk(owner) if isinstance(node, ast.Import)
                               for alias in node.names if alias.name.startswith("ummanu"))
        self.assertEqual(product_imports, ["ummanu.board.terminal_taxonomy"])
        for name in (
            "ATTEMPT_OUTCOME_VERDICTS",
            "ATTEMPT_OUTCOME_COMPLETENESS",
            "ATTEMPT_OUTCOME_DISPOSITIONS",
            "ATTEMPT_OUTCOME_BLOCKED_REASONS",
        ):
            self.assertIs(getattr(models, name), getattr(attempt_outcome, name))
        self.assertIs(attempt_outcome.ATTEMPT_OUTCOME_DISPOSITIONS, terminal_taxonomy.TERMINAL_DISPOSITIONS)
        self.assertIs(attempt_outcome.ATTEMPT_OUTCOME_BLOCKED_REASONS, terminal_taxonomy.BLOCKED_REASONS)

    def test_model_adapter_only_guards_subject_and_delegates(self):
        wrapper = ast.parse(inspect.getsource(models._validate_attempt_outcome_event))
        self.assertFalse(
            any(
                isinstance(node, (ast.Dict, ast.Set, ast.Subscript, ast.For, ast.Try))
                for statement in wrapper.body[0].body for node in ast.walk(statement)
            )
        )
        calls = [node for node in ast.walk(wrapper) if isinstance(node, ast.Call)]
        self.assertEqual(
            {node.func.id for node in calls if isinstance(node.func, ast.Name)},
            {"ValueError", "validate_attempt_outcome_data"},
        )
        with patch.object(models, "validate_attempt_outcome_data") as validate:
            models._validate_attempt_outcome_event(EventKind.ENTITY_UPDATED, EntityKind.CARD, {})
            self.assert_boundary_subject(validate)
            data = outcome_data()
            models._validate_attempt_outcome_event(EventKind.ATTEMPT_OUTCOME, EntityKind.CARD, data)
            validate.assert_called_once_with(data)
            self.assertIs(validate.call_args.args[0], data)
        self.assertIs(models.validate_attempt_outcome_data, attempt_outcome.validate_attempt_outcome_data)

    def assert_boundary_subject(self, validate):
        with self.assertRaisesRegex(ValueError, "^attempt outcome events require a Card subject$"):
            models._validate_attempt_outcome_event(EventKind.ATTEMPT_OUTCOME, EntityKind.PRODUCT, {})
        validate.assert_not_called()

    def test_raw_and_typed_adapters_execute_shared_facts(self):
        data = outcome_data(2)
        payload = AttemptOutcomePayload.from_data(data)
        for name in (
            "_validate_scalars",
            "_validate_disposition",
            "_validate_usage_link",
            "_optional_non_empty",
            "_validate_lineage_values",
            "_validate_field_set",
        ):
            with (
                self.subTest(fact=name),
                patch.object(attempt_outcome, name, wraps=getattr(attempt_outcome, name)) as fact,
            ):
                outcome_event(data)
                self.assertTrue(fact.called)
                fact.reset_mock()
                AttemptOutcomePayload.from_data(data)
                self.assertTrue(fact.called)
                if name in ("_validate_scalars", "_validate_disposition", "_validate_usage_link"):
                    fact.reset_mock()
                    replace(payload)
                    self.assertTrue(fact.called)


class AttemptOutcomePayloadTests(unittest.TestCase):
    def _v1_payload(self) -> AttemptOutcomePayload:
        return AttemptOutcomePayload(
            version=1,
            attempt_id="attempt-2",
            attempt=2,
            report_generation=3,
            sprint_ref="sprint:1",
            specification_revision=None,
            terminal_state=AttemptOutcomeTerminalState.IN_PROGRESS,
            verdict=AttemptOutcomeVerdict.RED,
            disposition="rework",
            blocked_reason=None,
            source_event_ids=AttemptOutcomeSourceEventIds(
                report=None,
                verdict=None,
                decision=None,
                effect="effect-1",
                worker_usage=None,
                review_usage=None,
            ),
            usage_completeness=AttemptOutcomeUsageCompleteness(
                worker=AttemptOutcomeCompleteness.MISSING,
                review=AttemptOutcomeCompleteness.MISSING,
            ),
        )

    def _v2_payload(self) -> AttemptOutcomePayload:
        return AttemptOutcomePayload(
            version=2,
            attempt_id="attempt-3",
            attempt=3,
            report_generation=4,
            sprint_ref="sprint:2",
            specification_revision="spec-1",
            terminal_state=AttemptOutcomeTerminalState.DONE,
            verdict=AttemptOutcomeVerdict.GREEN,
            disposition="accept",
            blocked_reason=None,
            source_event_ids=AttemptOutcomeSourceEventIds(
                report="report-1",
                verdict="verdict-1",
                decision=None,
                effect="effect-2",
                worker_usage="usage-worker",
                review_usage="usage-review",
            ),
            usage_completeness=AttemptOutcomeUsageCompleteness(
                worker=AttemptOutcomeCompleteness.COLLECTED,
                review=AttemptOutcomeCompleteness.DEGRADED,
            ),
            lineage_required=AttemptOutcomeLineageRequired(
                specification_revision=True,
                report=True,
                verdict=True,
                decision=True,
                effect=True,
                worker_usage=True,
                review_usage=True,
            ),
        )

    def test_v1_round_trip_preserves_the_released_dictionary_exactly(self) -> None:
        payload = self._v1_payload()
        data = payload.to_data()

        self.assertEqual(AttemptOutcomePayload.from_data(data), payload)
        self.assertEqual(AttemptOutcomePayload.from_data(data).to_data(), data)
        self.assertNotIn("lineage_required", data)
        self.assertEqual(payload.natural_key("card-1"), ("card-1", "attempt-2", 3))

    def test_v2_round_trip_keeps_typed_lineage_and_reports_missing_required_evidence(self) -> None:
        payload = self._v2_payload()
        data = payload.to_data()

        self.assertEqual(AttemptOutcomePayload.from_data(data), payload)
        self.assertEqual(data["lineage_required"]["decision"], True)
        self.assertEqual(payload.lineage_missing(), ("decision",))

    def test_usage_completeness_requires_its_usage_event_reference(self) -> None:
        with self.assertRaisesRegex(ValueError, "worker completeness requires its usage event ref"):
            AttemptOutcomePayload(
                version=1,
                attempt_id="attempt-1",
                attempt=1,
                report_generation=1,
                sprint_ref=None,
                specification_revision=None,
                terminal_state=AttemptOutcomeTerminalState.DONE,
                verdict=AttemptOutcomeVerdict.GREEN,
                disposition="accept",
                blocked_reason=None,
                source_event_ids=AttemptOutcomeSourceEventIds(
                    report=None,
                    verdict=None,
                    decision=None,
                    effect="effect-1",
                    worker_usage=None,
                    review_usage=None,
                ),
                usage_completeness=AttemptOutcomeUsageCompleteness(
                    worker=AttemptOutcomeCompleteness.COLLECTED,
                    review=AttemptOutcomeCompleteness.MISSING,
                ),
            )

    def test_occurrence_exposes_the_same_typed_payload_from_legacy_event_data(self) -> None:
        payload = self._v1_payload()
        event = Event(
            event_id="event-outcome",
            kind=EventKind.ATTEMPT_OUTCOME,
            entity_kind=EntityKind.CARD,
            ref="card-1",
            actor=Actor(Role.DISPATCHER, "dispatcher"),
            reason="confirmed terminal lifecycle effect",
            occurred_at=datetime(2026, 9, 17, tzinfo=UTC),
            data=payload.to_data(),
        )
        occurrence = AttemptOutcomeOccurrence("request-1", event, False)

        self.assertEqual(occurrence.payload, payload)

    def test_unknown_fields_fail_closed(self) -> None:
        data = self._v2_payload().to_data()
        data["future"] = "value"

        with self.assertRaisesRegex(ValueError, "unsupported field set"):
            AttemptOutcomePayload.from_data(data)


if __name__ == "__main__":
    unittest.main()
