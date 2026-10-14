"""DUP-W4 ownership and exact-source packets, without processes, boards or real locks."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support.request_index_packets import BASE_HASHES, BASE_SHA, base_modules, packets
from ummanu.webproto import ops, request_index, runs, sprint_ops, sprint_requests, store_io

PRODUCT = Path(__file__).resolve().parents[1] / "src/ummanu/webproto"


def index_owners(sources):
    """Recognize request-argument hashing, request reads and mismatch production in any helper."""
    owners = {"hash": set(), "read": set(), "match": set(), "exception": set()}
    for module, source in sources.items():
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "RequestMismatch":
                owners["exception"].add(module)
            if not isinstance(node, ast.FunctionDef):
                continue
            arguments = {a.arg for a in node.args.args}
            for item in ast.walk(node):
                if (
                    isinstance(item, ast.Raise)
                    and isinstance(item.exc, ast.Call)
                    and getattr(item.exc.func, "id", "") == "RequestMismatch"
                ):
                    owners["match"].add(module)
                if not isinstance(item, ast.Call) or not isinstance(item.func, ast.Attribute):
                    continue
                if item.func.attr == "read_text" and "request_id" in arguments:
                    owners["read"].add(module)
                if item.func.attr != "sha256" or not item.args:
                    continue
                encoded = item.args[0]
                if (
                    isinstance(encoded, ast.Call)
                    and isinstance(encoded.func, ast.Attribute)
                    and encoded.func.attr == "encode"
                    and isinstance(encoded.func.value, ast.Name)
                    and encoded.func.value.id in arguments
                ):
                    owners["hash"].add(module)
    return owners


class RequestIndexOwnershipTests(unittest.TestCase):
    def test_shared_implementations_have_one_owner_and_no_shadow_exception(self):
        sources = {
            name: (PRODUCT / f"{name}.py").read_text(encoding="utf-8")
            for name in ("runs", "sprint_requests", "request_index")
        }
        self.assertEqual(
            index_owners(sources),
            {kind: {"request_index"} for kind in ("hash", "read", "match", "exception")},
        )
        for source in sources.values():
            tree = ast.parse(source)
            self.assertFalse(
                any(isinstance(n, ast.FunctionDef) and n.name == "__getattr__" for n in ast.walk(tree))
            )
            self.assertFalse(
                any(
                    isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names)
                    for n in ast.walk(tree)
                )
            )

    def test_planted_renamed_hash_read_and_matching_copies_are_detected(self):
        duplicate = """
def another(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
def lookup(request_id):
    return json.loads(path.read_text(encoding="utf-8"))
def refuse(wanted, owned):
    if wanted and owned and wanted != owned:
        raise RequestMismatch("different operation")
class RequestMismatch(RunStoreError): pass
"""
        self.assertEqual(
            index_owners({"planted": duplicate}),
            {kind: {"planted"} for kind in ("hash", "read", "match", "exception")},
        )

    def test_actual_consumers_share_the_exception_identity_and_base_class(self):
        self.assertIs(runs.RequestMismatch, request_index.RequestMismatch)
        self.assertIs(ops.RequestMismatch, request_index.RequestMismatch)
        self.assertIs(sprint_ops.RequestMismatch, request_index.RequestMismatch)
        self.assertTrue(issubclass(request_index.RequestMismatch, store_io.RunStoreError))
        self.assertIs(runs.RunStoreError, store_io.RunStoreError)

    def test_both_stores_actually_use_all_shared_steps_and_the_atomic_owner(self):
        for store_type in (runs.RunStore, sprint_requests.SprintRequestStore):
            with tempfile.TemporaryDirectory() as directory:
                store = store_type(directory)
                self.assertIs(type(store.index), request_index.RequestIndex)
                with (
                    mock.patch.object(store.index, "path", wraps=store.index.path) as path,
                    mock.patch.object(store.index, "read", wraps=store.index.read) as read,
                    mock.patch.object(store.index, "write", wraps=store.index.write) as write,
                    mock.patch.object(store.index, "match", wraps=store.index.match) as match,
                    mock.patch.object(
                        store_io, "write_text_atomic", wraps=store_io.write_text_atomic
                    ) as atomic,
                    mock.patch.object(runs, "file_lock", return_value=mock.MagicMock()),
                    mock.patch.object(sprint_requests, "file_lock", return_value=mock.MagicMock()),
                ):
                    if store_type is runs.RunStore:
                        build = mock.Mock(
                            side_effect=lambda rid: runs.ProductRun(rid, "id", "r", "p", "worker", "x")
                        )
                        record, claimed = store.claim("id", build, operation="op", fingerprint="fp")
                        self.assertTrue(claimed)
                        self.assertEqual(
                            store.claim("id", build, operation="op", fingerprint="fp"), (record, False)
                        )
                        with self.assertRaises(request_index.RequestMismatch):
                            store.claim("id", build, operation="other", fingerprint="other")
                        self.assertEqual(build.call_count, 1)
                    else:
                        store.claim("id", operation="op", fingerprint="fp")
                        store.record_reference("id", "sprint:1")
                    self.assertIsNotNone(store.by_request("id", operation="op", fingerprint="fp"))
                    for seam in (path, read, write, match, atomic):
                        self.assertGreater(seam.call_count, 0)
                    with (
                        mock.patch.object(store_io, "write_text_atomic", side_effect=RuntimeError("refused")),
                        self.assertRaisesRegex(store_io.RunStoreError, "refused"),
                    ):
                        store.index.write("id", {})


class RequestIndexPacketTests(unittest.TestCase):
    def test_real_store_packets_and_optional_exact_base_source_comparison(self):
        candidate = packets()
        baseline = None
        if directory := os.environ.get("UMMANU_INDEX_BASE_SOURCE"):
            baseline = packets(base_modules(directory))
        if output := os.environ.get("UMMANU_INDEX_PACKET_OUT"):
            packet = {
                "base_sha": BASE_SHA,
                "base_source_sha256": BASE_HASHES,
                "candidate_source_sha256": {
                    name: hashlib.sha256((PRODUCT / f"{name}.py").read_bytes()).hexdigest()
                    for name in ("runs", "sprint_requests", "request_index")
                },
                "fixture_source_sha256": hashlib.sha256(
                    (Path(__file__).parent / "support/request_index_packets.py").read_bytes()
                ).hexdigest(),
                "candidate": candidate,
                "base": baseline,
            }
            Path(output).write_text(json.dumps(packet, sort_keys=True, indent=2), encoding="utf-8")
        if baseline is not None:
            self.assertEqual(set(candidate), set(baseline))
            for name, actual in candidate.items():
                with self.subTest(packet=name):
                    self.assertEqual(actual, baseline[name])
        for kind in ("run", "sprint"):
            prefix = f"{kind}/id-'request/雪'/"
            missing, claimed, repeat = (candidate[prefix + str(i)] for i in range(3))
            self.assertEqual(missing["type"], "NoneType")
            self.assertFalse(missing["root_exists"])
            self.assertEqual(missing["files"], {})
            self.assertTrue(claimed["claimed"])
            self.assertFalse(repeat["claimed"])
            self.assertEqual(claimed["files"], repeat["files"])
            self.assertEqual(
                [e for e in repeat["events"] if e[0] == "build"],
                [["build", "pr-one"]] if kind == "run" else [],
            )
            self.assertIn("cannot be reused for other", candidate[prefix + "3"]["message"])
            self.assertIn("different inputs", candidate[prefix + "4"]["message"])
            for number in range(5):
                malformed = candidate[f"{kind}/json-{number}-{'missing' if kind == 'run' else 'none'}/0"]
                self.assertEqual(malformed["error"], "RunStoreError")
                self.assertIsNone(malformed["cause"])
            index = next(
                bytes.fromhex(content).decode()
                for path, content in claimed["files"].items()
                if path.endswith(hashlib.sha256("request/雪".encode()).hexdigest() + ".json")
            )
            self.assertEqual("\n" in index.rstrip("\n"), kind == "sprint")
        final = candidate["sprint/reference-failure/5"]
        self.assertEqual(final["value"]["reference"], "sprint:7")
        self.assertFalse(candidate["sprint/reference-failure/3"]["claimed"])
        self.assertEqual(candidate["run/write-2-RuntimeError/0"]["error"], "RunStoreError")
        files = candidate["run/write-2-RuntimeError/0"]["files"]
        # The canonical record still precedes the failing request-index write. Card membership
        # now adds derived files, without changing the canonical/request-index byte contract.
        canonical = [path for path in files if Path(path).parent.as_posix() == "<data>/webproto/runs"]
        self.assertEqual(len(canonical), 1)
