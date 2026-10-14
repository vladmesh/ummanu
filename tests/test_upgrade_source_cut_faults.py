"""Real file mutations and injected acquisition evidence; no runtime service operations."""

from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.support.source_cut import MEMORY, PO, WEB, PayloadCounts, SourceFixture
from ummanu.installation.upgrade import fingerprints, services, types


class SourceCutFaultTests(unittest.TestCase):
    def fixture(self, stack):
        tmp = stack.enter_context(tempfile.TemporaryDirectory())
        fixture = SourceFixture(Path(tmp))
        fixture.install(stack)
        fixture.bind_po()
        established = fixture.run()
        self.assertTrue(established.ok, established.render())
        return fixture

    def mutate(self, fixture, kind):
        path = fixture.schema
        if kind == "same-size":
            info = path.stat()
            path.write_bytes(b'{"const":8}\n')
            os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        elif kind == "replace":
            replacement = path.with_suffix(".new")
            replacement.write_bytes(b'{"const":9}\n')
            replacement.replace(path)
        elif kind == "delete":
            path.unlink()
        elif kind == "symlink":
            path.unlink()
            path.symlink_to(fixture.product / "src/app000.py")
        elif kind == "directory":
            path.unlink()
            path.mkdir()
        elif kind == "fifo":
            path.unlink()
            os.mkfifo(path)
        elif kind == "unreadable":
            path.chmod(0)
        elif kind == "touch":
            info = path.stat()
            os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))
        elif kind == "head":
            fixture.revision = "b" * 40
        elif kind == "membership":
            fixture.tracked.remove("src/app000.py")
        elif kind == "root":
            fixture.context.product_root = fixture.product.parent
        elif kind == "parent-symlink":
            parent = path.parent
            target = parent.with_name("other-schemas")
            parent.rename(target)
            parent.symlink_to(target, target_is_directory=True)
        else:
            raise AssertionError(kind)

    def test_probe_mutations_refuse_receipts_and_leave_old_bytes_intact(self):
        for family, probe in (("memory", "probe_memory"), ("web", "probe_web")):
            for kind in ("same-size", "replace", "delete", "symlink", "directory", "fifo", "unreadable",
                         "touch", "head", "membership", "root", "parent-symlink"):
                with self.subTest(family=family, kind=kind), ExitStack() as stack:
                    fixture = self.fixture(stack)
                    before = fixture.receipts()
                    fixture.context.code_changed = True
                    counts = PayloadCounts(fixture)
                    counts.install(stack)
                    stack.enter_context(mock.patch.object(services, probe, side_effect=lambda *a, fixture=fixture, kind=kind, **kw: self.mutate(fixture, kind) or 200))
                    result = fixture.run((getattr(services, "step_" + family),))
                    self.assertFalse(result.ok, result.render())
                    self.assertIn("source cut invalidated", result.steps[-1].detail)
                    self.assertEqual(fixture.receipts(), before)
                    self.assertIsNone(fixture.context.source_cut)
                    self.assertEqual({counts.reads[name] for name in fixture.sources}, {1})

    def test_acquisition_edit_and_descriptor_replacement_poison_without_retry(self):
        for when in ("before-open", "during-read", "after-earlier-read"):
            with self.subTest(when=when), ExitStack() as stack:
                fixture = self.fixture(stack)
                before = fixture.receipts()
                counts = PayloadCounts(fixture)
                counts.install(stack)
                real_open, real_fdopen = os.open, os.fdopen
                injected = False

                def opened(path, flags, *a, when=when, fixture=fixture, real_open=real_open, **kw):
                    nonlocal injected
                    if when == "before-open" and Path(path) == fixture.schema and not injected:
                        injected = True
                        self.mutate(fixture, "replace")
                    return real_open(path, flags, *a, **kw)

                class Stream:
                    def __init__(self, stream):
                        self.stream = stream

                    def __enter__(self):
                        self.stream.__enter__()
                        return self

                    def __exit__(self, *args):
                        return self.stream.__exit__(*args)

                    def __getattr__(self, key):
                        return getattr(self.stream, key)

                    def read(inner, counts=counts, when=when, fixture=fixture):
                        nonlocal injected
                        contents = inner.stream.read()
                        relative = counts.descriptors[inner.stream.fileno()]
                        if not injected and (
                            when == "during-read" or (when == "after-earlier-read" and relative.endswith("000.json"))
                        ):
                            injected = True
                            target = fixture.product / (relative if when == "during-read" else "src/app000.py")
                            target.write_bytes(b"changed!!!\n")
                        return contents

                stack.enter_context(mock.patch.object(os, "open", side_effect=opened))
                stack.enter_context(mock.patch.object(os, "fdopen", side_effect=lambda *a, stream_type=Stream, real_fdopen=real_fdopen, **kw: stream_type(real_fdopen(*a, **kw))))
                result = fixture.run((services.step_memory,))
                self.assertTrue(injected)
                self.assertFalse(result.ok, result.render())
                self.assertIn("source cut acquisition failed", result.steps[-1].detail)
                self.assertEqual(fixture.receipts(), before)
                self.assertLessEqual(max(counts.reads.values()), 1)

    def test_unknown_stat_and_git_evidence_fail_typed_and_stay_poisoned(self):
        for kind in ("stat", "precision", "git"):
            with self.subTest(kind=kind), ExitStack() as stack:
                fixture = self.fixture(stack)
                cut = fingerprints.SourceCut()
                cut.digest(fixture.product, types.PRODUCT_SOURCE_PATHS)
                with ExitStack() as fault:
                    real_lstat = Path.lstat

                    def lstat(path, *args, kind=kind, fixture=fixture, real_lstat=real_lstat, **kw):
                        info = real_lstat(path, *args, **kw)
                        if path != fixture.schema:
                            return info
                        if kind == "stat":
                            raise PermissionError("unknown stat")
                        return SimpleNamespace(**{name: getattr(info, name) for name in (
                            "st_mode", "st_dev", "st_ino", "st_nlink", "st_uid", "st_gid", "st_size", "st_ctime_ns")},
                            st_mtime_ns=None)

                    if kind == "git":
                        fault.enter_context(mock.patch.object(fingerprints, "_git", side_effect=types.GitError("unknown tracked set")))
                    else:
                        fault.enter_context(mock.patch.object(Path, "lstat", lstat))
                    with self.assertRaises(fingerprints.SourceCutInvalidated):
                        cut.check(fixture.product)
                with self.assertRaises(fingerprints.SourceCutInvalidated):
                    cut.digest(fixture.product, types.PRODUCT_SOURCE_PATHS)
                self.assertTrue(fixture.run((services.step_memory,)).ok)

    def test_revision_move_between_input_selection_and_capture_is_not_current(self):
        with ExitStack() as stack:
            fixture = self.fixture(stack)
            before = fixture.receipts()
            git = fixture.git
            selected = False

            def moved(root, args):
                nonlocal selected
                revision = git(root, args)
                if args == ["rev-parse", "HEAD"] and not selected:
                    selected = True
                    fixture.revision = "b" * 40
                return revision

            from ummanu.installation.upgrade import checkout
            stack.enter_context(mock.patch.object(checkout, "_git", side_effect=moved))
            result = fixture.run((services.step_memory,))
            self.assertFalse(result.ok, result.render())
            self.assertIn("HEAD changed during input selection", result.steps[-1].detail)
            self.assertEqual(fixture.receipts(), before)

    def test_changes_between_steps_and_at_verify_cannot_return_current(self):
        for phase in ("step", "verify", "current"):
            with self.subTest(phase=phase), ExitStack() as stack:
                fixture = self.fixture(stack)
                before = fixture.receipts()
                if phase == "step":
                    def edit(ctx, fixture=fixture):
                        self.mutate(fixture, "same-size")
                        return types.StepResult("edit", "unchanged")
                    steps = (services.step_memory, edit, services.step_web)
                elif phase == "verify":
                    stack.enter_context(mock.patch.object(services.role_skills, "audit", side_effect=lambda fixture=fixture, **kw: self.mutate(fixture, "same-size") or {"ok": True}))
                    steps = (services.step_memory, services.step_verify)
                else:
                    evidence = services._memory_receipt_evidence
                    def changed(*args, evidence=evidence, fixture=fixture):
                        result = evidence(*args)
                        self.mutate(fixture, "same-size")
                        return result
                    stack.enter_context(mock.patch.object(services, "_memory_receipt_evidence", side_effect=changed))
                    steps = (services.step_memory,)
                result = fixture.run(steps)
                self.assertFalse(result.ok, result.render())
                self.assertIn("source cut invalidated", result.steps[-1].detail)
                self.assertEqual(fixture.receipts(), before)
                self.assertIsNone(fixture.context.source_cut)

    def test_runtime_inputs_and_generation_remain_fresh_and_failure_precedence_survives(self):
        for family, unit in (("memory", MEMORY), ("web", WEB)):
            for kind in (("model", "pack", "generation") if family == "memory" else ("installed-unit", "shipped-unit", "registry", "generation")):
                with self.subTest(family=family, kind=kind), ExitStack() as stack:
                    fixture = self.fixture(stack)
                    before = fixture.receipts()
                    fixture.context.code_changed = True

                    def change(*a, kind=kind, fixture=fixture, unit=unit, family=family, **kw):
                        if kind in ("model", "installed-unit"):
                            fixture.units.files[unit] += b"Environment=MEMORY_MODEL=model/v2\n"
                        elif kind == "pack":
                            fixture.context.memory_pack_digest = "b" * 64
                        elif kind == "shipped-unit":
                            (fixture.product / "packaging/systemd/ummanu-web.service").write_bytes(b"other\n")
                        elif kind == "registry":
                            fixture.snapshot.write_bytes(b"other\n")
                        else:
                            fixture.units.identities[unit] = type(fixture.units.identities[unit])(999, 888, "changed")
                            # Web generation remains the earlier failure even if source also changed.
                            if family == "web":
                                self.mutate(fixture, "same-size")
                        return 200

                    stack.enter_context(mock.patch.object(services, "probe_" + family, side_effect=change))
                    result = fixture.run((getattr(services, "step_" + family),))
                    self.assertFalse(result.ok, result.render())
                    self.assertIn("changed process generation" if kind == "generation" else "inputs changed", result.steps[-1].detail)
                    self.assertEqual(fixture.receipts(), before)

    def test_pending_po_restart_does_not_excuse_changed_source_evidence(self):
        with ExitStack() as stack:
            fixture = self.fixture(stack)
            fixture.units.identities[PO] = type(fixture.units.identities[PO])(999, 888, "other")
            marker = services.po_client.restart_marker_path(fixture.data)
            marker.write_text("pending\n")
            result = fixture.run((services.step_memory, services.step_verify))
            self.assertTrue(result.ok, result.render())
            self.assertIn("restart pending", result.steps[-1].detail)
            stack.enter_context(mock.patch.object(services.role_skills, "audit", side_effect=lambda fixture=fixture, **kw: self.mutate(fixture, "same-size") or {"ok": True}))
            result = fixture.run((services.step_memory, services.step_verify))
            self.assertFalse(result.ok, result.render())
            self.assertIn("source cut invalidated", result.steps[-1].detail)

    def test_po_idle_busy_and_unanswered_restart_keep_their_decisions(self):
        for outcome in ("deferred", "unanswered", "idle"):
            with self.subTest(outcome=outcome), ExitStack() as stack:
                fixture = self.fixture(stack)
                before = fixture.receipts()
                fixture.context.code_changed = True

                def request(data_dir, reason, fixture=fixture, outcome=outcome):
                    self.assertEqual(data_dir, fixture.data)
                    self.assertIn("product code", reason)
                    if outcome == "idle":
                        fixture.units.identities[PO] = type(fixture.units.identities[PO])(999, 888, "new")
                    return SimpleNamespace(outcome=outcome, running=1, detail="fixture")

                restart = stack.enter_context(mock.patch.object(services.po_client, "request_restart", side_effect=request))
                result = fixture.run((services.step_po,))
                self.assertTrue(result.ok, result.render())
                self.assertEqual(result.steps[-1].status, "changed")
                restart.assert_called_once()
                self.assertEqual(fixture.receipts(), before)
                self.assertIn({"deferred": "restart deferred", "unanswered": "not acknowledged", "idle": "while idle"}[outcome], result.steps[-1].detail)
