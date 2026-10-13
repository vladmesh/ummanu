"""Raw mapping, typed-view and restart contracts for all eight persisted wrappers."""

from __future__ import annotations

import contextlib
import json
import sys
import unittest
from collections import UserDict
from pathlib import Path
from unittest import mock

from tests.support.persisted_mapping import CONTRACTS, FIELDS, record, typed_values
from ummanu.dispatch import state
from ummanu.dispatch.types import DispatcherError

GOLDENS = json.loads((Path(__file__).parent / "support" / "persisted_mapping_goldens.json").read_text())


class PersistedMappingOwnerTests(unittest.TestCase):
    def test_all_eight_inherit_one_common_method_owner(self) -> None:
        base = getattr(state, "_PersistedMapping", None)
        self.assertIsNotNone(base, "eight independent wrappers must consolidate their shared methods")
        self.assertEqual(base.__slots__, ())
        for wrapper, *_ in CONTRACTS:
            with self.subTest(wrapper=wrapper.__name__):
                self.assertEqual(wrapper.__bases__, (base,))
                for name in ("__init__", "from_value", "to_json"):
                    self.assertNotIn(name, wrapper.__dict__)
                    self.assertIs(
                        getattr(wrapper, name).__func__ if name == "from_value" else getattr(wrapper, name),
                        base.__dict__[name].__func__ if name == "from_value" else base.__dict__[name],
                    )


class PersistedMappingContractTests(unittest.TestCase):
    def assert_json(self, actual, expected) -> None:
        self.assertEqual(list(actual), list(expected))
        self.assertEqual(json.dumps(actual), json.dumps(expected))

    def test_current_typed_goldens_and_identity(self) -> None:
        for wrapper, domain, view, parser, _, slot in CONTRACTS:
            typed = typed_values()[wrapper]
            with (
                self.subTest(wrapper=wrapper.__name__),
                mock.patch.object(domain, parser, wraps=getattr(domain, parser)) as parse,
            ):
                value = wrapper(typed)
                parse.assert_not_called()
                self.assert_json(value.to_json(), GOLDENS[wrapper.__name__])
                self.assertIs(wrapper.from_value(value), value)
                if slot:
                    self.assertIs(getattr(value, view), typed)
                    parse.assert_not_called()
                else:
                    self.assertEqual(getattr(value, view), typed)
                    self.assertEqual(parse.call_count, 1)

    def test_defaults_non_dict_and_mapping_only_inputs(self) -> None:
        for wrapper, domain, view, parser, _, slot in CONTRACTS:
            for source in (None, {}, [], "text", 1, UserDict({"role": "worker"})):
                with (
                    self.subTest(wrapper=wrapper.__name__, source=source),
                    mock.patch.object(domain, parser, return_value=None) as parse,
                ):
                    value = wrapper(source)
                    self.assert_json(value.to_json(), {})
                    parse.assert_not_called()
                    self.assertIsNone(getattr(value, view))
                    self.assertEqual(parse.call_count, 0 if slot else 1)
            self.assert_json(wrapper().to_json(), {})

    def test_raw_historical_order_shallow_copy_and_all_mapping_mutations(self) -> None:
        for wrapper, *_ in CONTRACTS:
            with self.subTest(wrapper=wrapper.__name__):
                nested = {"keep": [1]}
                source = {"z_unknown": nested, "role": "worker", "a_extra": None}
                value = wrapper(source)
                self.assertIsInstance(value, dict)
                self.assertEqual(value, source)
                self.assert_json(value.to_json(), source)
                self.assertIs(value["z_unknown"], nested)
                source["only_input"] = 3
                self.assertNotIn("only_input", value)
                projection = value.to_json()
                self.assertIsNot(projection, value)
                self.assertIs(projection["z_unknown"], nested)
                projection["only_projection"] = 4
                self.assertNotIn("only_projection", value)
                nested["keep"].append(2)
                self.assertEqual(value["z_unknown"]["keep"], [1, 2])
                value["new"] = 1
                value.update({"role": "review", "last": 2})
                self.assertEqual(value.pop("a_extra"), None)
                del value["new"]
                self.assert_json(value.to_json(), {"z_unknown": nested, "role": "review", "last": 2})
                value.clear()
                self.assert_json(value.to_json(), {})

    def test_slots_properties_and_subclass_classmethod_binding(self) -> None:
        for wrapper, _, view, _, _, slot in CONTRACTS:
            with self.subTest(wrapper=wrapper.__name__):
                value = wrapper()
                self.assertIsInstance(wrapper.__dict__[view], property)
                if slot:
                    self.assertEqual(wrapper.__slots__, (slot,))
                    self.assertFalse(hasattr(value, "__dict__"))
                    with self.assertRaises(AttributeError):
                        value.annotation = "no instance dict"
                else:
                    self.assertNotIn("__slots__", wrapper.__dict__)
                    value.annotation = "retained"
                    self.assertEqual(value.__dict__, {"annotation": "retained"})

                class Child(wrapper):
                    pass

                child = Child.from_value({"unknown": 1})
                self.assertIs(type(child), Child)
                self.assertIs(Child.from_value(child), child)
                self.assertIs(wrapper.from_value(child), child)
                self.assertIs(type(Child.from_value(value)), Child)
                self.assert_json(child.to_json(), {"unknown": 1})

    def test_cached_parser_exact_payload_arguments_refusal_and_identity(self) -> None:
        for wrapper, domain, view, parser, _, slot in CONTRACTS:
            if not slot:
                continue
            source = {"unknown": [1], "validated_sha": 73, "role": "worker"}
            for result in (object(), None):
                seen = []

                def parse(payload, _seen=seen, _result=result, **kwargs):
                    _seen.append((payload, kwargs))
                    # The stored raw mapping is initialized before parsing, even if the
                    # parser later mutates its independent top-level input.
                    payload["parser_added"] = True
                    return _result

                with (
                    self.subTest(wrapper=wrapper.__name__, result=result),
                    mock.patch.object(domain, parser, side_effect=parse) as patched,
                ):
                    value = wrapper(source)
                    patched.assert_called_once()
                    payload, kwargs = seen[0]
                    self.assertIsNot(payload, source)
                    self.assertIsNot(payload, value)
                    self.assertIs(payload["unknown"], source["unknown"])
                    self.assertEqual(kwargs, {"current_sha": "73"} if view == "receipt" else {})
                    self.assert_json(value.to_json(), source)
                    self.assertIs(getattr(value, view), result)
                    value.update({"role": "review"})
                    value.pop("unknown")
                    value.clear()
                    self.assertIs(getattr(value, view), result)
                    patched.assert_called_once()

    def test_parser_exception_policies_are_narrow(self) -> None:
        for wrapper, domain, view, parser, caught, slot in CONTRACTS:
            for error_type in (KeyError, RuntimeError, TypeError, ValueError, LookupError):
                failure = error_type(f"{view}-failure")
                with (
                    self.subTest(wrapper=wrapper.__name__, error=error_type),
                    mock.patch.object(domain, parser, side_effect=failure) as parse,
                ):
                    if not slot:
                        value = wrapper({"role": "worker"})
                        parse.assert_not_called()
                        with self.assertRaises(error_type) as raised:
                            getattr(value, view)
                        self.assertIs(raised.exception, failure)
                    elif error_type in caught:
                        self.assertIsNone(getattr(wrapper({"role": "worker"}), view))
                    else:
                        with self.assertRaises(error_type) as raised:
                            wrapper({"role": "worker"})
                        self.assertIs(raised.exception, failure)
                    parse.assert_called_once()

    def test_live_parsers_receive_current_self_on_each_access_and_fault(self) -> None:
        for wrapper, domain, view, parser, _, slot in CONTRACTS:
            if slot:
                continue
            first, second = object(), object()
            failure = RuntimeError(view)
            with (
                self.subTest(wrapper=wrapper.__name__),
                mock.patch.object(domain, parser, side_effect=[first, second, failure]) as parse,
            ):
                value = wrapper({"role": "worker"})
                parse.assert_not_called()
                self.assertIs(getattr(value, view), first)
                value["role"] = "review"
                self.assertIs(getattr(value, view), second)
                value.clear()
                with self.assertRaises(RuntimeError) as raised:
                    getattr(value, view)
                self.assertIs(raised.exception, failure)
                self.assertEqual(parse.call_count, 3)
                self.assertTrue(all(call.args[0] is value for call in parse.call_args_list))

    def test_real_live_values_follow_recovery_mutations(self) -> None:
        intent = state.PersistedLaunchIntent({"role": "worker", "round": 1})
        first = intent.intent
        intent.update({"role": "review", "round": 2})
        self.assertEqual((first.role, first.round_number), ("worker", 1))
        self.assertEqual((intent.intent.role, intent.intent.round_number), ("review", 2))
        self.assertIsNot(intent.intent, intent.intent)
        del intent["role"]
        self.assertIsNone(intent.intent)
        episode = state.PersistedHeadlessRecoveryEpisode({"since": 1.0})
        first = episode.episode
        episode["recovery_error"] = "refused"
        self.assertEqual(first.recovery_error, "")
        self.assertEqual(episode.episode.recovery_error, "refused")
        self.assertIsNot(episode.episode, episode.episode)
        episode.clear()
        self.assertIsNone(episode.episode)

    def test_gate_receipt_type_import_is_lazy_and_patch_seam_works(self) -> None:
        # A distinct class in the actual owning module distinguishes a lazy lookup
        # from an eager alias while exercising both raw and typed inputs.
        sentinel = object()

        class Receipt:
            accept = mock.Mock(return_value=sentinel)

            def as_dict(self):
                return {"z": 1, "validated_sha": "a" * 40}

        with mock.patch.dict(sys.modules, {"ummanu.dispatch.gate_receipt": mock.Mock(GateReceipt=Receipt)}):
            value = state.PersistedGateReceipt({"validated_sha": "a" * 40})
            self.assertIs(value.receipt, sentinel)
            Receipt.accept.assert_called_once_with({"validated_sha": "a" * 40}, current_sha="a" * 40)
            typed = Receipt()
            self.assertIs(state.PersistedGateReceipt(typed).receipt, typed)
            Receipt.accept.assert_called_once()


class PersistedRecordContractTests(unittest.TestCase):
    def test_full_typed_record_golden_restart_and_all_field_assignment_identity(self) -> None:
        typed = typed_values()
        current = record(**{field: typed[wrapper] for field, wrapper in FIELDS.items()})
        self.assertEqual(json.dumps(current.to_json()), json.dumps(GOLDENS["DispatcherRecord"]))
        self.assertEqual(list(current.to_json()), list(GOLDENS["DispatcherRecord"]))
        restarted = state.DispatcherRecord.from_json(json.loads(json.dumps(current.to_json())))
        self.assertEqual(json.dumps(restarted.to_json()), json.dumps(current.to_json()))
        for field, wrapper in FIELDS.items():
            with self.subTest(field=field):
                self.assertIs(type(getattr(current, field)), wrapper)
                self.assertIs(type(getattr(restarted, field)), wrapper)
                wrapped = wrapper(typed[wrapper])
                setattr(current, field, wrapped)
                self.assertIs(getattr(current, field), wrapped)
                setattr(current, field, {"z_unknown": [1], "role": "review", "a_extra": 2})
                value = getattr(current, field)
                self.assertIs(type(value), wrapper)
                self.assertEqual(
                    json.dumps(value.to_json()), '{"z_unknown": [1], "role": "review", "a_extra": 2}'
                )

    def test_historical_partial_record_defaults_and_conditional_keys(self) -> None:
        historical = {field: {"z_unknown": 1, "role": "worker", "a_extra": None} for field in FIELDS}
        current = record(**historical)
        restarted = state.DispatcherRecord.from_json(current.to_json())
        for field in FIELDS:
            with self.subTest(field=field):
                self.assertEqual(list(getattr(restarted, field)), ["z_unknown", "role", "a_extra"])
                self.assertEqual(json.dumps(getattr(restarted, field)), json.dumps(historical[field]))
        sparse = record().to_json()
        for field in FIELDS:
            del sparse[field]
        minimal = state.DispatcherRecord.from_json(sparse)
        self.assertEqual(json.dumps(minimal.to_json()), json.dumps(GOLDENS["DispatcherRecordEmpty"]))
        for field in FIELDS:
            self.assertEqual(getattr(minimal, field), {})
        payload = minimal.to_json()
        self.assertNotIn("review_provider_hold", payload)
        self.assertNotIn("po_submission", payload)
        minimal.review_provider_hold = "provider-unavailable"
        minimal.po_submission = state.PoSubmission(kind="decision")
        payload = minimal.to_json()
        keys = list(payload)
        self.assertEqual(
            keys[keys.index("review_provider_hold") - 1 : keys.index("review_provider_hold") + 2],
            ["review_infra_error", "review_provider_hold", "review_delivery_failures"],
        )
        self.assertEqual(keys[-1], "po_submission")
        self.assertEqual(json.dumps(state.DispatcherRecord.from_json(payload).to_json()), json.dumps(payload))

    def test_restart_parser_count_order_and_no_eager_live_parse(self) -> None:
        calls = []
        with contextlib.ExitStack() as stack:
            for wrapper, domain, view, parser, _, slot in CONTRACTS:

                def parse(payload, _view=view, **kwargs):
                    calls.append((_view, list(payload), kwargs))
                    return None

                stack.enter_context(mock.patch.object(domain, parser, side_effect=parse))
            payload = record().to_json()
            calls.clear()
            payload.update({field: {"z": field, "a": 1} for field in FIELDS})
            state.DispatcherRecord.from_json(payload)
        self.assertEqual(
            [view for view, _, _ in calls],
            [
                "run",
                "run",
                "snapshot",
                "snapshot",
                "receipt",
                "authorship",
                "published_ref",
                "evidence",
                "evidence",
            ],
        )
        self.assertTrue(all(keys == ["z", "a"] for _, keys, _ in calls))
        self.assertEqual(
            [kwargs for _, _, kwargs in calls], [{}, {}, {}, {}, {"current_sha": ""}, {}, {}, {}, {}]
        )

    def test_unsupported_legacy_record_is_rejected_before_wrapper_parsing(self) -> None:
        for field in (
            "worker_retained_at",
            "worker_resume_delivery",
            "worker_resume_phase",
            "worker_resume_sent_at",
        ):
            with self.subTest(field=field), mock.patch.object(CONTRACTS[0][1], "accept") as parse:
                with self.assertRaises(DispatcherError) as raised:
                    state.DispatcherRecord.from_json(
                        {field: 1, "gate_attestation": {"validated_sha": "a" * 40}}
                    )
                self.assertEqual(raised.exception.code, "unsupported_legacy_record")
                parse.assert_not_called()
