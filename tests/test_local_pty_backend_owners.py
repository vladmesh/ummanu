"""Backend ownership and operative bindings, without starting heads or observing native scopes."""

from __future__ import annotations

import inspect
import unittest
from pathlib import Path
from unittest import mock

from tests.support import local_pty_owners
from ummanu.runtime import (
    local_pty_delivery as delivery,
    local_pty_handoff as handoff,
    local_pty_head as runtime,
    local_pty_inspection as inspection,
    local_pty_journal as journal,
    local_pty_values as values,
)
from ummanu.runtime.head.memory import MemoryScopeError

ROOT = Path(__file__).resolve().parents[1]


class LocalPtyBackendOwnersTests(unittest.TestCase):
    def test_bounded_unique_owners_and_acyclic_dependencies(self):
        sources = local_pty_owners.sources(ROOT)
        self.assertEqual(local_pty_owners.oversized(sources), [])
        inventory = local_pty_owners.inventory(sources)
        for owner, names in (
            (
                "local_pty_head",
                (
                    "LocalPtyHeadRuntime",
                    "runtime.start",
                    "runtime.attach",
                    "runtime.request_drain",
                    "runtime.stop",
                    "runtime._rehydrate",
                ),
            ),
            ("local_pty_handoff", ("runtime.deliver", "runtime._handoff_within", "_handoff_progress")),
            ("local_pty_delivery", ("runtime._put", "runtime._follow", "DeliveryReport", "_delivery_report")),
            ("local_pty_durable", ("_journal_state", "_supervisor_state", "_DurableHead")),
            ("local_pty_journal", ("_replay_journal", "_JournalReplay", "head_run_turn_reading")),
            ("local_pty_inspection", ("head_run_supervisor_lease", "fence_cleanup_scopes")),
            ("local_pty_values", ("_Probe", "_Address", "_Refusal", "AttachedStream")),
        ):
            for name in names:
                self.assertEqual(inventory[name], owner, name)
        graph = {
            owner: {
                target.removeprefix("ummanu.runtime.")
                for target in local_pty_owners.dependencies(owner, source)
                if target.removeprefix("ummanu.runtime.") in sources
            }
            for owner, source in sources.items()
        }
        for owner in sources:
            pending = list(graph[owner])
            reached = set()
            while pending:
                target = pending.pop()
                self.assertNotEqual(target, owner, f"backend cycle from {owner}")
                if target not in reached:
                    reached.add(target)
                    pending.extend(graph[target])
            if owner != "local_pty_head":
                self.assertNotIn("local_pty_head", reached)

    def test_planted_size_duplicate_reverse_import_and_substrate_construction_sites(self):
        self.assertEqual(local_pty_owners.oversized({"planted": "\n" * 2001}), ["planted"])
        with self.assertRaisesRegex(AssertionError, "duplicate backend definition"):
            local_pty_owners.inventory(
                {"a": "def _delivery_report(): pass", "b": "def _delivery_report(): pass"}
            )
        for source in (
            "from .local_pty_head import LocalPtyHeadRuntime\n",
            "def f():\n from ummanu.runtime.local_pty_head import AttachedStream\n",
            "from importlib import import_module\nx = import_module('ummanu.runtime.local_pty_head')\n",
        ):
            self.assertIn(
                "ummanu.runtime.local_pty_head", local_pty_owners.dependencies("local_pty_delivery", source)
            )
        for source in (
            "from ummanu.runtime.head.local_pty import protocol\n",
            "from ummanu.runtime.head import local_pty\n",
            "import ummanu.runtime.head.local_pty.client\n",
        ):
            self.assertEqual(
                local_pty_owners.substrate_offenders({"src/ummanu/runtime/unowned.py": source}),
                ["src/ummanu/runtime/unowned.py"],
            )
            self.assertEqual(
                local_pty_owners.substrate_offenders(
                    {path: source for path in local_pty_owners.BACKEND_PATHS}
                ),
                [],
            )
        sites = local_pty_owners.construction_sites(
            {
                "src/ummanu/runtime/local_pty_handoff.py": "def accidental(): return LocalPtyHeadRuntime()\n",
                local_pty_owners.CLASS_OWNER: "def irrelevant(): return LocalPtyHeadRuntime()\n",
                "src/ummanu/runtime/head_runtime_backends.py": "def build_head_runtime(): return LocalPtyHeadRuntime()\n",
            }
        )
        self.assertEqual(
            sites, {"accidental": {"LocalPtyHeadRuntime"}, "build_head_runtime": {"LocalPtyHeadRuntime"}}
        )

    def test_real_consumer_reexports_and_class_signature_identity(self):
        for owner, names in (
            (
                inspection,
                (
                    "head_run_supervisor_lease",
                    "head_run_directory",
                    "head_run_pid_file",
                    "runtime_scope_inventory",
                    "fence_cleanup_scopes",
                ),
            ),
            (
                journal,
                (
                    "head_run_turn_reading",
                    "head_run_screen_lines",
                    "head_run_journal",
                    "head_run_journal_read",
                    "head_run_first_record",
                    "head_run_journal_tail",
                    "head_run_loss_reason",
                    "_has_exited",
                ),
            ),
            (handoff, ("_handoff_progress",)),
            (
                values,
                (
                    "AttachedStream",
                    "LocalPtyRuntimeError",
                    "JOURNAL_NAME",
                    "RUN_EXITED",
                    "DELIVERY_PENDING",
                    "DELIVER_NOT_SUBMITTED",
                    "SUBMIT_KEY",
                    "_declared_bound",
                ),
            ),
        ):
            for name in names:
                self.assertIs(getattr(runtime, name), getattr(owner, name), name)
        self.assertIs(runtime.MemoryScopeError, MemoryScopeError)
        self.assertEqual(runtime.LocalPtyHeadRuntime.__module__, "ummanu.runtime.local_pty_head")
        parameters = inspect.signature(runtime.LocalPtyHeadRuntime).parameters
        self.assertEqual(parameters["head_process_status"].kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertEqual(parameters["delivery_grace"].default, 5.0)
        self.assertEqual(parameters["stop_timeout"].default, 10.0)
        self.assertEqual(parameters["prompt_quiet"].default, None)

    def test_prompt_constant_patch_reaches_constructor_and_self_override_reaches_delivery(self):
        class Overridden(runtime.LocalPtyHeadRuntime):
            def _deliver_payload(self, *args, **kwargs):
                return "overridden"

        with mock.patch.object(runtime, "PROMPT_QUIET_SECONDS", 0.125):
            instance = Overridden("/unused", head_process_status=lambda *args, **kwargs: {})
        self.assertEqual(instance._prompt_quiet, 0.125)
        self.assertEqual(instance.deliver(mock.sentinel.run, mock.sentinel.pointer), "overridden")
        with mock.patch.object(instance, "_deliver_payload", return_value="patched"):
            self.assertEqual(instance.deliver(mock.sentinel.run, mock.sentinel.pointer), "patched")
        self.assertIs(runtime.LocalPtyHeadRuntime._follow, delivery.DeliveryMixin._follow)
