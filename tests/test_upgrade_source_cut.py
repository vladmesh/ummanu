"""Actual file-only materializer path counters, wire packets and invocation lifetime."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path

from tests.support.source_cut import MEMORY, PO, WEB, PayloadCounts, SourceFixture, capture
from ummanu.installation.upgrade import checkout, services, types

ROOT = Path(__file__).resolve().parents[1]
BASE_HASHES = {
    "upgrade.py": "57e0de16d38eb308c3a00c5d0a8ac5deb9d3ec88b4cb3542ac69fc05eca1b166",
    "services.py": "7165271131016775cd5b6885c11b8f7de4ffa03600f0156fd9687beb970eb6c1",
}


class UpgradeSourceCutTests(unittest.TestCase):
    def test_mixed_materializer_reads_each_payload_once_at_n_and_2n(self):
        packets = {}
        with tempfile.TemporaryDirectory() as tmp:
            for n in (8, 16):
                packet = capture(Path(tmp) / str(n), n)
                packets[str(n)] = packet
                self.assertEqual([s["status"] for s in packet["steps"]],
                                 ["unchanged", "changed", "unchanged", "changed", "unchanged"])
                counts = packet["counts"]
                source = {k: v for k, v in counts["reads"].items() if k.startswith("src/")}
                self.assertEqual(len(source), n)
                self.assertEqual(set(source.values()), {1})
                self.assertEqual({k: counts["opens"][k] for k in source}, source)
                self.assertEqual(sum(counts["bytes"][k] for k in source), packet["source_bytes"])
                for path in source:
                    self.assertEqual(counts["feeds"][path], 2 if "/schemas/" in path else 1)
                    self.assertGreater(counts["metadata"][path], 1)
                self.assertEqual(counts["reads"]["pyproject.toml"], 7)
                self.assertEqual(counts["reads"]["uv.lock"], 7)
                self.assertEqual(counts["reads"]["requirements.txt"], 7)
                self.assertEqual(counts["reads"]["packaging/systemd/ummanu-web.service"], 3)
                self.assertEqual(counts["reads"]["@data/heads.yaml"], 3)
                self.assertNotIn("src/untracked.py", source)
                self.assertNotIn("src/__pycache__/app.pyc", source)
                self.assertGreater(counts["git"]["ls-files -- src/"], 1)
                for body in packet["receipts"].values():
                    self.assertEqual(json.loads(body)["version"], 1)
                # No constant/hash-wrapper oracle: actual legacy wire helper reads dirty fixture bytes.
                fixture = SourceFixture(Path(tmp) / f"wire-{n}", n)
                with ExitStack() as stack:
                    fixture.install(stack)
                    inputs = json.loads(packet["receipts"]["web"])["inputs"]
                    self.assertEqual(inputs["product_sha256"], checkout._git_tracked_digest(fixture.product, types.PRODUCT_SOURCE_PATHS))
                    self.assertEqual(inputs["schemas_sha256"], checkout._git_tracked_digest(fixture.product, types.SCHEMA_PATHS))
                    self.assertEqual(inputs["dependency_sha256"], checkout._git_tracked_digest(fixture.product, types.DEPENDENCY_PATHS))
            base_dir = os.environ.get("UMMANU_SOURCE_CUT_BASE")
            if base_dir:
                base_dir = Path(base_dir).resolve()
                self.assertFalse(base_dir.is_relative_to(ROOT))
                owners = {}
                for name, sha in BASE_HASHES.items():
                    path = base_dir / name
                    self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), sha)
                    spec = importlib.util.spec_from_file_location("_source_cut_base_" + path.stem, path)
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    owners[name] = module
                for n in (8, 16):
                    old = capture(Path(tmp) / f"old-{n}", n, service_owner=owners["services.py"], entry=owners["upgrade.py"])
                    self.assertEqual(old["receipts"], packets[str(n)]["receipts"])
                    self.assertEqual(old["steps"], packets[str(n)]["steps"])
                    packets[str(n)]["base"] = old
            destination = os.environ.get("UMMANU_SOURCE_CUT_PACKET_OUT")
            if destination:
                Path(destination).write_text(json.dumps(packets, sort_keys=True, indent=2) + "\n")

    def test_second_invocation_same_context_and_another_root_are_fresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            for root in (Path(tmp) / "one", Path(tmp) / "two"):
                fixture = SourceFixture(root)
                with ExitStack() as stack:
                    fixture.install(stack)
                    fixture.bind_po()
                    first = fixture.run()
                    self.assertTrue(first.ok, first.render())
                    before = fixture.receipts()
                    counts = PayloadCounts(fixture)
                    counts.install(stack)
                    second = fixture.run()
                    self.assertTrue(second.ok, second.render())
                    self.assertEqual({s.status for s in second.steps}, {"unchanged"})
                    self.assertEqual(fixture.receipts(), before)
                    for path in fixture.sources:
                        self.assertEqual(counts.reads[path], 1)
                    fixture.schema.write_bytes(b'{"const":8}\n')
                    changed = fixture.run((services.step_memory, services.step_web))
                    self.assertTrue(changed.ok, changed.render())
                    self.assertEqual([s.status for s in changed.steps], ["changed", "changed"])
                    self.assertNotEqual(fixture.receipts()["web"], before["web"])
                    self.assertIsNone(fixture.context.source_cut)

    def test_no_consumers_and_early_failure_do_not_acquire(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            fixture = SourceFixture(Path(tmp))
            fixture.install(stack)
            fixture.report.host["components"] = {name: {"enabled": False} for name in ("memory", "web", "po")}
            counts = PayloadCounts(fixture)
            counts.install(stack)
            result = fixture.run()
            self.assertTrue(result.ok, result.render())
            failed = fixture.run((lambda ctx: types.StepResult("pull", "failed", "early"), services.step_memory))
            self.assertFalse(failed.ok)
            self.assertEqual(failed.steps[0].detail, "early")
            self.assertEqual(sum(counts.reads.values()), 0)
            self.assertEqual(counts.git, {})
            self.assertIsNone(fixture.context.source_cut)

    def test_replaced_context_shares_only_the_active_scope_and_exception_closes_it(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            fixture = SourceFixture(Path(tmp))
            fixture.install(stack)
            copies = []

            def first(ctx):
                services.memory_process_inputs(ctx, "ummanu-memory.service")
                copies.append(replace(ctx, dry_run=True))
                self.assertIs(copies[-1].source_cut, ctx.source_cut)
                return types.StepResult("first", "unchanged")

            def error(ctx):
                raise ValueError("fixture unexpected error")

            with self.assertRaisesRegex(ValueError, "fixture unexpected error"):
                fixture.run((first, error))
            self.assertIsNone(fixture.context.source_cut)
            with self.assertRaisesRegex(types.ReceiptError, "scope is closed"):
                services.memory_process_inputs(copies[0], "ummanu-memory.service")
            self.assertTrue(fixture.run((services.step_memory,)).ok)

    def test_standalone_po_startup_and_step_helpers_acquire_fresh_dirty_bytes(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            fixture = SourceFixture(Path(tmp))
            fixture.install(stack)
            counts = PayloadCounts(fixture)
            counts.install(stack)
            fixture.bind_po()
            before = fixture.receipts()["po"]
            fixture.schema.write_bytes(b'{"const":8}\n')
            fixture.bind_po()
            self.assertNotEqual(fixture.receipts()["po"], before)
            for path in fixture.sources:
                self.assertEqual(counts.reads[path], 4 if "/schemas/" in path else 2)
            self.assertTrue(services.step_memory(fixture.context).status == "changed")
            fixture.schema.write_bytes(b'{"const":7}\n')
            self.assertEqual(services.step_memory(fixture.context).status, "changed")
            self.assertIsNone(fixture.context.source_cut)
            self.assertIn(PO, fixture.units.active)

    def test_install_and_recover_materializers_reacquire(self):
        with tempfile.TemporaryDirectory() as tmp:
            for recovery in (False, True):
                packet = capture(Path(tmp) / str(recovery), 8, recovery=recovery)
                self.assertEqual({v for k, v in packet["counts"]["reads"].items() if k.startswith("src/")}, {1})
                self.assertEqual(packet["steps"][-1]["status"], "unchanged")

    def test_pull_precedes_lazy_capture(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            fixture = SourceFixture(Path(tmp))
            fixture.install(stack)

            def pulled(ctx):
                fixture.revision = "b" * 40
                fixture.schema.write_bytes(b'{"const":8}\n')
                return types.StepResult("pull", "changed")

            result = fixture.run((pulled, services.step_memory))
            self.assertTrue(result.ok, result.render())
            inputs = json.loads(fixture.receipts()["memory"])["inputs"]
            self.assertEqual(inputs["product_revision"], "b" * 40)

    def test_stopped_and_foreign_units_preserve_the_service_decisions(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            fixture = SourceFixture(Path(tmp))
            fixture.install(stack)
            fixture.units.active.clear()
            result = fixture.run((services.step_memory, services.step_po, services.step_web))
            self.assertTrue(result.ok, result.render())
            self.assertEqual([s.status for s in result.steps], ["changed", "changed", "skipped"])
            self.assertEqual(fixture.units.calls, [("restart", MEMORY), ("restart", PO)])
            fixture.report.host["foreign_units"] = [MEMORY, WEB, PO]
            # Catalogue spelling is exercised by the same product helper, not a patched decision.
            counts = PayloadCounts(fixture)
            counts.install(stack)
            result = fixture.run()
            self.assertTrue(result.ok, result.render())
            self.assertEqual([s.status for s in result.steps[:3]], ["skipped"] * 3)
            self.assertEqual(counts.reads, {})
