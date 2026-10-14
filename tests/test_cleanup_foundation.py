"""Shared cleanup ownership and journal contracts, without runtime effects or backend probes."""

from __future__ import annotations

import ast
import contextlib
import hashlib
import importlib
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests import config_dependencies, source_trees

ROOT = Path(__file__).resolve().parents[1]
PRODUCT = ROOT / "src" / "ummanu"
OWNERS = ("ummanu.dispatch.ownership", "ummanu.dispatch.cleanup_journal")
PRIMITIVE_CONSUMERS = (
    "tasks", "sprints", "board.sprint_protocol.lifecycle", "board.sprint_protocol.reservations",
    "board.sql_host", "board.reference_repair", "restore", "data", "task_restore",
)
FORBIDDEN = (
    "ummanu.dispatch.cleanup", "ummanu.dispatch.host", "ummanu.dispatch.runtime",
    "ummanu.dispatch.production", "ummanu.dispatch.watchdog", "ummanu.runtime.local_pty_head",
    "ummanu.tasks", "ummanu.sprints", "ummanu.board.sql_host", "ummanu.board.sql_cards",
    "ummanu.infra.git_worktree", "ummanu.dispatch.git_workspace", "ummanu.watchdog",
)
# The existing HeadRun parser's package/validation closure, including its nested registry reads.
# These are measured source edges, not a claim that cold compaction executes every capability.
PARSER_CLOSURE = set("""
ummanu ummanu._fsutil ummanu._proc ummanu.board ummanu.board.legacy_codec
ummanu.board.local_run ummanu.broad_check ummanu.config ummanu.dispatch
ummanu.dispatch.cleanup_journal ummanu.dispatch.ownership ummanu.dispatch.tick_telemetry
ummanu.dispatch.types ummanu.head_registry ummanu.infra ummanu.infra.export_allowlist
ummanu.kernel ummanu.kernel.sprint_policy ummanu.projects ummanu.projects.test_timing
ummanu.runtime ummanu.runtime.agent_prompt_transport ummanu.runtime.codex_preflight
ummanu.runtime.container_labels ummanu.runtime.docker_guard ummanu.runtime.head
ummanu.runtime.head.command ummanu.runtime.head.memory ummanu.runtime.head.operations
ummanu.runtime.head.run ummanu.runtime.head.runtime ummanu.runtime.head.spec
ummanu.runtime.head.task_ref ummanu.runtime.head_run_binding ummanu.runtime.head_runtimes
ummanu.runtime.heads ummanu.runtime.launch_prefix ummanu.runtime.onboarding_storage
ummanu.runtime.paths ummanu.runtime.procfs ummanu.runtime.prompt_document ummanu.runtime.role_env
ummanu.runtime.test_guard ummanu.runtime.tui_delivery ummanu.runtime_env ummanu.schemas
""".split())


def forbidden(names: set[str], prefixes: tuple[str, ...]) -> list[str]:
    return sorted(name for name in names if any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes))


class CleanupFoundationBoundaryTests(unittest.TestCase):
    def test_primitive_consumers_do_not_import_the_effect_owner(self) -> None:
        violations = {}
        for relative in PRIMITIVE_CONSUMERS:
            name = "ummanu." + relative
            path = PRODUCT.joinpath(*relative.split(".")).with_suffix(".py")
            targets = config_dependencies.dependencies(name, path, path.read_text())
            found = forbidden(targets, ("ummanu.dispatch.cleanup",))
            if found:
                violations[name] = found
        self.assertEqual(violations, {})

    def test_shared_definitions_have_one_owner_outside_the_effect_module(self) -> None:
        expected = {"Allowance": OWNERS[0], "Deferred": OWNERS[0], "ownership_lock": OWNERS[0],
                    "reference_lock": OWNERS[0], "bulk_lane": OWNERS[0], "CleanupJournal": OWNERS[1],
                    "capacity_serialized": OWNERS[0], "ownership_recovery": OWNERS[0],
                    "journal_mutation": OWNERS[0], "owner_operation": OWNERS[0]}
        definitions = {name: [] for name in expected}
        for path, tree in source_trees.modules(PRODUCT, naming=tuple(expected)):
            module = ".".join(path.relative_to(ROOT / "src").with_suffix("").parts)
            for node in tree.body:
                if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in expected:
                    definitions[node.name].append(module)
        self.assertEqual(definitions, {name: [owner] for name, owner in expected.items()})

    def test_shared_owner_closures_exclude_runtime_effects(self) -> None:
        for owner in OWNERS:
            with self.subTest(owner=owner):
                reached = config_dependencies.closure(PRODUCT, owner)
                self.assertIn(owner, reached)
                self.assertEqual(forbidden(reached, FORBIDDEN), [])
                allowed = PARSER_CLOSURE if owner == OWNERS[1] else {
                    "ummanu", "ummanu.dispatch", OWNERS[0], "ummanu.dispatch.types",
                }
                self.assertEqual(sorted(reached - allowed), [])

    def test_shared_owners_cold_import_without_runtime_effects(self) -> None:
        with mock.patch.dict(sys.modules):
            for name in tuple(sys.modules):
                if name == "ummanu" or name.startswith("ummanu."):
                    del sys.modules[name]
            for owner in OWNERS:
                importlib.import_module(owner)
            # Compaction's existing parser is a lazy edge; exercise it in the cold process too.
            journal = importlib.import_module(OWNERS[1])
            journal._compact_run({"fanout_policy": {"state": "clean", "events": ["malformed"]}})
            self.assertEqual(forbidden(set(sys.modules), FORBIDDEN), [])

    def test_guard_resolves_planted_nested_qualified_relative_and_string_edges(self) -> None:
        path = PRODUCT / "dispatch" / "ownership.py"
        for source in (
            "def f():\n    from .cleanup import CleanupOwner\n",
            "from . import cleanup as effects\nf = effects.CleanupOwner\n",
            "import ummanu.dispatch as d\nf = d.cleanup.CleanupOwner\n",
            "import importlib as loader\nf = loader.import_module('ummanu.dispatch.cleanup')\n",
            "from importlib import import_module as load\nf = load('.cleanup', 'ummanu.dispatch')\n",
            "from importlib import import_module as load\nf = load('.cleanup', package='ummanu.dispatch')\n",
            "PATCH = 'ummanu.dispatch.cleanup._check'\n",
        ):
            with self.subTest(source=source):
                targets = config_dependencies.dependencies(OWNERS[0], path, source)
                self.assertTrue(forbidden(targets, FORBIDDEN))
        # The explicit package wins over the importing module's package.
        targets = config_dependencies.dependencies("ummanu.kernel.probe", PRODUCT / "kernel" / "probe.py",
            "from importlib import import_module as load\nload('.cleanup', package='ummanu.dispatch')\n")
        self.assertIn("ummanu.dispatch.cleanup", targets)
        with self.assertRaisesRegex(AssertionError, "nonliteral dynamic import package"):
            config_dependencies.dependencies(OWNERS[0], path,
                "from importlib import import_module as load\nload('.cleanup', package=target)\n")


class SharedOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        from ummanu.dispatch import cleanup, cleanup_journal, ownership

        self.effects, self.journal_module, self.owner = cleanup, cleanup_journal, ownership
        self.temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.data = Path(self.temporary)

    def test_facade_aliases_and_actual_callers_share_definitions_and_globals(self) -> None:
        from ummanu import tasks
        from ummanu.board import reference_repair, sql_host
        from ummanu.board.sprint_protocol import lifecycle as sprint_lifecycle

        for name in ("Allowance", "Deferred", "ownership_lock", "bulk_lane", "reference_lock",
                     "capacity_serialized", "ownership_recovery", "lifecycle", "owner_operation",
                     "serialized", "journal_mutation", "_locks", "_guard", "_held", "_ALLOWANCE"):
            self.assertIs(getattr(self.effects, name), getattr(self.owner, name), name)
        self.assertIs(self.effects.CleanupJournal, self.journal_module.CleanupJournal)
        self.assertIs(tasks.CleanupJournal, sprint_lifecycle.CleanupJournal)
        self.assertIs(tasks.CleanupJournal, self.journal_module.CleanupJournal)
        self.assertIs(tasks.reference_lock, sql_host.reference_lock)
        self.assertIs(tasks.bulk_lane, reference_repair.bulk_lane)
        self.assertIs(self.journal_module._check, self.owner._check)
        self.assertIs(self.effects._check, self.owner._check)
        for name in ("_replace_file", "_fsync_directory", "_compact_run", "_intent_key"):
            self.assertIs(getattr(self.effects, name), getattr(self.journal_module, name), name)

    def test_mixed_consumers_reenter_exact_resolved_paths_with_one_descriptor(self) -> None:
        handles = []

        def opened(path, mode):
            handle = mock.MagicMock(name=str(path))
            handle.__enter__.return_value = handle
            handles.append((str(path), handle))
            return handle

        with mock.patch("builtins.open", side_effect=opened), mock.patch.object(self.owner.fcntl, "flock") as flock:
            with self.effects.ownership_lock(self.data / ".." / self.data.name), self.owner.ownership_lock(self.data):
                pass
            with self.effects.reference_lock(self.data, "x-1"), self.owner.reference_lock(self.data, "x-1"):
                pass
            with self.owner.reference_lock(self.data, "x-1", lane="lifecycle"):
                pass
            with self.owner.reference_lock(self.data, "capacity", lane="admission"):
                pass
        root = self.data.resolve() / "dispatcher"
        token = hashlib.sha256(b"x-1").hexdigest() + ".lock"
        capacity = hashlib.sha256(b"capacity").hexdigest() + ".lock"
        self.assertEqual([path for path, _ in handles], [str(root / "cleanup.lock"), str(root / "board-bulk.lock"),
            str(root / "effects" / token), str(root / "lifecycle" / token), str(root / "admission" / capacity)])
        self.assertEqual(flock.call_count, 10)
        self.assertEqual([call.args[1] for call in flock.call_args_list], [self.owner.fcntl.LOCK_EX,
            self.owner.fcntl.LOCK_UN, self.owner.fcntl.LOCK_SH, self.owner.fcntl.LOCK_EX,
            self.owner.fcntl.LOCK_UN, self.owner.fcntl.LOCK_UN, self.owner.fcntl.LOCK_EX,
            self.owner.fcntl.LOCK_UN, self.owner.fcntl.LOCK_EX, self.owner.fcntl.LOCK_UN])

    def test_bulk_upgrade_refusal_exclusive_bypass_and_independent_shared_descriptors(self) -> None:
        handles = []

        def descriptor(*args):
            handle = mock.MagicMock()
            handle.__enter__.return_value = handle
            handles.append(handle)
            return handle

        opened = mock.Mock(side_effect=descriptor)
        with mock.patch("builtins.open", opened), mock.patch.object(self.owner.fcntl, "flock"):
            with self.owner.bulk_lane(self.data), self.assertRaisesRegex(self.effects.HostError, "cannot upgrade"), \
                    self.effects.bulk_lane(self.data, exclusive=True):
                self.fail("upgrade admitted")
            opened.reset_mock()
            with self.effects.bulk_lane(self.data, exclusive=True), self.owner.reference_lock(self.data, "x-1"), \
                    self.effects.reference_lock(self.data, "capacity", lane="admission"):
                pass
            self.assertEqual(opened.call_count, 1)
            opened.reset_mock()
            # Independent thread-local holders are simulated; real threads/processes belong to CI.
            with self.owner.bulk_lane(self.data), mock.patch.object(self.owner, "_held", threading.local()), \
                    self.effects.bulk_lane(self.data):
                pass
            self.assertEqual(opened.call_count, 2)
            self.assertIsNot(handles[-1], handles[-2])

    def test_capacity_and_recovery_decorators_acquire_reference_then_admission_then_mutation(self) -> None:
        events = []

        @contextlib.contextmanager
        def fence(data, reference, *, lane="effects"):
            events.append(("enter", reference, lane))
            yield
            events.append(("exit", reference, lane))

        @contextlib.contextmanager
        def mutation():
            events.append("mutation")
            yield

        instance = SimpleNamespace(data_dir=self.data, _mutation=mutation)
        with mock.patch.object(self.owner, "reference_lock", side_effect=fence):
            self.effects.capacity_serialized(lambda self, **kwargs: events.append("write"))(instance, reference="x-1")
            self.effects.ownership_recovery(lambda self, event: events.append("write"))(instance, {"ref": "x-1"})
        expected = [("enter", "x-1", "effects"), ("enter", "capacity", "admission"), "mutation", "write",
                    ("exit", "capacity", "admission"), ("exit", "x-1", "effects")]
        self.assertEqual(events, expected * 2)

    def test_busy_waits_and_publication_tail_use_the_shared_allowance_and_poll(self) -> None:
        elapsed = [0.0]
        budget = self.effects.Allowance(4.0, clock=lambda: elapsed[0])
        token = self.owner._ALLOWANCE.set(budget)
        self.addCleanup(self.owner._ALLOWANCE.reset, token)
        self.assertIs(self.effects._allowance(), budget)
        self.assertEqual((self.owner.REPLAY_ALLOWANCE, self.owner.PUBLICATION_RESERVE,
                          self.owner.ATTEMPT_FLOOR, self.owner.EFFECT_FLOOR, self.owner._LOCK_POLL),
                         (4.0, 0.5, 1.0, 0.5, 0.01))
        with self.assertRaisesRegex(self.effects.Deferred, "busy"):
            self.owner._wait("held", lambda block: False, blocking=False)
        self.assertEqual(budget.deferrals, 0)
        elapsed[0] = 3.5
        with self.assertRaises(self.effects.Deferred):
            self.journal_module._check("read")
        acquired = mock.Mock(side_effect=[False, False, True])
        with mock.patch.object(self.owner.time, "sleep", side_effect=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)) as sleep:
            self.effects._wait("publication", acquired, publication=True)
        self.assertEqual(acquired.call_args_list, [mock.call(False)] * 3)
        self.assertEqual(sleep.call_args_list, [mock.call(0.01)] * 2)
        elapsed[0] = 4.0
        with self.assertRaises(self.effects.Deferred):
            self.effects._wait("publication", lambda block: False, publication=True)

    def test_nested_replay_preserves_context_deadline_and_board_bounds(self) -> None:
        elapsed = [0.0]
        within = mock.MagicMock()
        client = SimpleNamespace(within=within)
        runtime = SimpleNamespace(data_dir=self.data, cleanup_monotonic=lambda: elapsed[0])
        outer, inner = self.effects.CleanupOwner(runtime), self.effects.CleanupOwner(runtime)
        observed = []

        def nested(limit, budget, report):
            observed.append((budget, self.owner._allowance()))
            elapsed[0] = 2.0
            return inner.replay(allowance=100.0)

        def finish(limit, budget, report):
            observed.append((budget, self.effects._ALLOWANCE.get()))
            self.assertEqual(budget.deadline, 4.0)
            self.assertEqual(budget.remaining(), 1.5)
            return []

        with mock.patch.object(outer, "_board_clients", return_value=[client]), \
                mock.patch.object(inner, "_board_clients", return_value=[]), \
                mock.patch.object(outer, "_replay_batch", side_effect=nested), \
                mock.patch.object(inner, "_replay_batch", side_effect=finish):
            outer.replay(allowance=4.0)
        self.assertTrue(all(budget is observed[0][0] and active is budget for budget, active in observed))
        self.assertIsNone(self.owner._allowance())
        self.assertEqual(within.call_args.args[0](), 1.5)
        self.assertEqual(outer.last_replay["allowance_ms"], 4000.0)
        self.assertEqual(inner.last_replay["allowance_ms"], 4000.0)

    def test_reservation_is_published_before_mocked_effects_and_survives_work_exhaustion(self) -> None:
        module = self.journal_module
        journal = module.CleanupJournal(self.data)
        task = {"id": 1, "ref": "x-1", "project": "p"}
        key = journal.remember(task, {"attempt_id": "a"}, disposition="done")
        initial = journal._intent_path(key).read_bytes()
        owner = self.effects.CleanupOwner(SimpleNamespace(data_dir=self.data, cleanup_clock=lambda: 100.0))
        with mock.patch.object(module, "_check", side_effect=self.effects.Deferred("read seam")) as check, \
                mock.patch.object(owner, "_replay") as refused_effect, \
                self.assertRaises(self.effects.Deferred):
            owner.replay_one(key)
        check.assert_called_once_with("attempt reservation")
        refused_effect.assert_not_called()
        self.assertEqual(journal._intent_path(key).read_bytes(), initial)
        elapsed = [0.0]
        budget = self.owner.Allowance(4.0, clock=lambda: elapsed[0])
        token = self.owner._ALLOWANCE.set(budget)
        self.addCleanup(self.owner._ALLOWANCE.reset, token)
        publications = []
        replace_file = module._replace_file

        def published(path, body):
            replace_file(path, body)
            if path.parent.name == "intents":
                publications.append(json.loads(body))

        def effect(value, selected):
            self.assertEqual(publications[-1]["retry"], {"last_attempt_at": 100.0, "next_attempt_at": 3700.0})
            self.assertEqual(journal.intent(key)["retry"], publications[-1]["retry"])
            elapsed[0] = 3.5
            with self.assertRaises(self.effects.Deferred):
                self.effects._check("effect")
            intent = value["intents"][selected]
            intent["reason"] = "work exhausted; retained"
            owner.journal.commit_intent(selected, intent)
            return intent

        with mock.patch.object(module, "_replace_file", side_effect=published), \
                mock.patch.object(owner, "_replay", side_effect=effect) as replay:
            owner.replay_one(key)
            replay.assert_called_once()
            owner.replay_one(key)
            replay.assert_called_once()  # cooldown prevents another effect or publication
        self.assertEqual(len(publications), 2)
        self.assertEqual(journal.intent(key)["reason"], "work exhausted; retained")

    def test_publication_fsync_order_and_tick_counts_survive_mixed_journal_consumers(self) -> None:
        import stat

        from ummanu.dispatch import tick_telemetry

        module = self.journal_module
        native_fsync, native_replace = module.os.fsync, module.os.replace
        events = []

        def fsync(descriptor):
            events.append("directory-fsync" if stat.S_ISDIR(module.os.fstat(descriptor).st_mode) else "file-fsync")
            native_fsync(descriptor)

        def replace(source, target):
            events.append("replace")
            native_replace(source, target)

        with mock.patch.object(module.os, "fsync", side_effect=fsync), \
                mock.patch.object(module.os, "replace", side_effect=replace):
            module._replace_file(self.data / "probe.json", b'{"proof": true}')
        self.assertEqual(events, ["file-fsync", "replace", "directory-fsync"])
        self.assertEqual((self.data / "probe.json").read_bytes(), b'{"proof": true}')
        with tick_telemetry.tick_counting() as counters:
            journal = self.effects.CleanupJournal(self.data)
            task = {"id": 1, "ref": "x-1", "project": "p"}
            key = journal.remember(task, {"attempt_id": "a"})
            counted = dict(counters)
            journal.remember(task, {"attempt_id": "a"})
            journal.read()
            self.assertEqual(counters, counted)
            lower = module.CleanupJournal(self.data)
            lower.request(task, "done", {"attempt_id": "a"})
            self.assertEqual(counters["cleanup_intent_writes"], 2)
            self.assertEqual(counters["cleanup_bytes_written"], journal.writes["bytes"] + lower.writes["bytes"])
            self.assertEqual(journal.intent(key)["status"], "pending")

    def test_compaction_keeps_exact_stop_identity_and_never_cleans_malformed_evidence(self) -> None:
        from tests.support.cleanup_journal import head
        from ummanu.runtime.head import HeadRun

        raw = head("x-1", 0, baseline=2, events=10, exited=True)
        compact = self.journal_module._compact_run(raw)
        for field in ("run_id", "scope_generation", "spec", "workspace", "task_ref", "role", "lifecycle", "stopped_by"):
            self.assertEqual(compact[field], raw[field])
        self.assertEqual(compact["lifecycle"], "exited")
        self.assertEqual(len(compact["fanout_policy"]["events"]), 8)
        self.assertFalse(HeadRun.from_json(compact).fanout_clean)
        for policy in ({"version": 1, "state": "clean", "events": ["bad"]},
                       {"version": 1, "state": "clean", "events": [], "run_id": "foreign"},
                       {"version": 1, "state": "clean", "events": [], "provider_source_required": True}):
            with self.subTest(policy=policy):
                malformed = {**raw, "fanout_policy": policy}
                projected = self.journal_module._compact_run(malformed)
                self.assertFalse(HeadRun.from_json(projected).fanout_clean)
                self.assertEqual(self.journal_module._compact_run(projected), projected)


class CleanupFoundationGoldenTests(unittest.TestCase):
    def test_persisted_state_migration_and_crash_match_released_bytes(self) -> None:
        from tests.support.cleanup_foundation import snapshots
        from ummanu.dispatch import cleanup_journal

        expected = json.loads((ROOT / "tests" / "fixtures" / "cleanup_foundation.json").read_text())
        self.assertEqual(snapshots(cleanup_journal), expected)
