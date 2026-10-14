"""Released process receipts through the real upgrade producers and consumers, without services."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.fakes.upgrade import FakeUnitInstaller
from ummanu import upgrade
from ummanu.host_apply import UnitProcessIdentity
from ummanu.infra import process_receipts as receipts

IDENTITY = UnitProcessIdentity(123, 456, "invocation")
PROCESS = {"pid": 123, "start_ticks": 456, "invocation_id": "invocation"}
COMMON = {"product_revision": "a" * 40, "product_sha256": "b" * 64, "dependency_sha256": "c" * 64}
INPUTS = {
    "memory": {**COMMON, "memory_model": "model/v1", "memory_pack_sha256": None},
    "web": {**COMMON, "schemas_sha256": "d" * 64, "web_units_sha256": "e" * 64,
            "head_registry_sha256": "f" * 64},
    "PO": {**COMMON, "schemas_sha256": "d" * 64},
}


class ProcessReceiptFixture(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name)
        self.units = FakeUnitInstaller()
        self.context = upgrade.UpgradeContext(
            instance_path=self.data / "instance", product_root=self.data / "product",
            base_branch="main", dry_run=False, units=self.units,
            report=SimpleNamespace(data_dir=self.data, host={"unit_prefix": "ummanu-"}),
        )

    def path(self, family: str) -> Path:
        return self.data / {
            "memory": "upgrade/memory-process-receipt.json",
            "web": "web/process-receipt.json", "PO": "po-service/process-receipt.json",
        }[family]

    def payload(self, family: str) -> dict:
        value = {"version": 1, "process": PROCESS.copy(), "inputs": INPUTS[family].copy()}
        if family != "PO":
            value["unit"] = f"ummanu-{family}.service"
        return value

    def plant(self, family: str, payload: object) -> None:
        path = self.path(family)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def read(self, family: str):
        if family == "PO":
            return upgrade._read_po_process_receipt(self.data)
        reader = getattr(upgrade, f"_read_{family}_process_receipt")
        return reader(self.context, f"ummanu-{family}.service")

    def evidence(self, family: str, identity=IDENTITY, inputs=None):
        inputs = INPUTS[family] if inputs is None else inputs
        if family == "PO":
            return upgrade._po_receipt_evidence(self.data, identity, inputs)
        reader = upgrade._receipt_evidence if family == "web" else upgrade._memory_receipt_evidence
        return reader(self.context, f"ummanu-{family}.service", identity, inputs)


class ProcessReceiptContracts(ProcessReceiptFixture):
    def test_actual_producers_publish_released_bytes_and_probe_order(self) -> None:
        for family in INPUTS:
            with self.subTest(family=family):
                trace = []
                unit = f"ummanu-{family}.service"
                self.units.files[unit] = b"[Service]\nExecStart=/x --host 127.0.0.1 --port 8787\n"
                self.units.active.add(unit)
                with (
                    mock.patch.object(self.units, "restart", side_effect=lambda name, trace=trace: trace.append("restart")),
                    mock.patch.object(self.units, "process_identity", side_effect=lambda name, trace=trace: (
                        trace.append("identity") or IDENTITY)),
                    mock.patch.object(upgrade, f"{family.lower() if family != 'PO' else 'po'}_process_inputs",
                                      side_effect=lambda *args, trace=trace, family=family: trace.append("inputs") or INPUTS[family]),
                    mock.patch.object(upgrade, "probe_web", side_effect=lambda *a, trace=trace, **kw: trace.append("probe") or 200),
                    mock.patch.object(upgrade, "probe_memory", side_effect=lambda *a, trace=trace, **kw: trace.append("probe")),
                    mock.patch.object(upgrade.os, "getpid", return_value=123) as pid,
                    mock.patch.object(upgrade, "_process_start_ticks", return_value=456) as ticks,
                ):
                    if family == "PO":
                        line = upgrade.write_po_process_receipt(
                            self.data, self.context.product_root, environ={"INVOCATION_ID": "invocation"})
                        self.assertTrue(line.startswith(f"wrote the PO process receipt {self.path(family)}: pid 123;"))
                        pid.assert_called_with()
                        ticks.assert_called_once_with(123)
                        self.assertEqual(trace, ["inputs"])
                    else:
                        result = getattr(upgrade, f"step_{family}")(self.context)
                        self.assertEqual(result.status, "changed", result.detail)
                        self.assertEqual(trace, ["inputs", "identity", "restart", "identity", "probe", "identity", "inputs"])
                # The explicit envelope also fixes PO's absence of unit and compact sorted wire order.
                expected = (json.dumps(self.payload(family), sort_keys=True, separators=(",", ":")) + "\n").encode()
                self.assertEqual(self.path(family).read_bytes(), expected)
                self.assertEqual(stat.S_IMODE(self.path(family).stat().st_mode), 0o600)
                receipt, reason = self.read(family)
                self.assertEqual(reason, "")
                self.assertEqual(receipt, self.payload(family) if family == "web" else (IDENTITY, INPUTS[family]))
                summaries = {
                    "memory": "memory process receipt verified: pid 123; product revision aaaaaaaaaaaa, "
                    "product sha256 bbbbbbbbbbbb, deps sha256 cccccccccccc, model model/v1, pack sha256 none",
                    "PO": "PO process receipt verified: pid 123, revision aaaaaaaaaaaa, "
                    "product sha256 bbbbbbbbbbbb, deps sha256 cccccccccccc, schemas sha256 dddddddddddd",
                    "web": "web process receipt verified: pid 123, start ticks 456, invocation invocation; "
                    f"product revision {COMMON['product_revision']}; product {COMMON['product_sha256']}; "
                    f"dependencies {COMMON['dependency_sha256']}; schemas {'d' * 64}; web units {'e' * 64}; "
                    f"head registry {'f' * 64}",
                }
                self.assertEqual(self.evidence(family), (True, summaries[family]))
                self.path(family).unlink()

    def test_envelope_identity_and_input_faults_are_family_malformed_evidence(self) -> None:
        for family in INPUTS:
            valid = self.payload(family)
            faults = [None, [], {}, {**valid, "extra": 1}, {k: v for k, v in valid.items() if k != "inputs"},
                      {**valid, "version": "1"}, {**valid, "version": 2}]
            faults.append({**valid, "unit": "foreign.service"})
            for key, values in {"pid": (True, 0, -1, "123"), "start_ticks": (True, 0),
                                "invocation_id": (None, "", 1)}.items():
                faults.extend({**valid, "process": {**PROCESS, key: value}} for value in values)
            faults.extend({**valid, "process": value} for value in (None, {}, {**PROCESS, "extra": 1}))
            for key in INPUTS[family]:
                faults.append({**valid, "inputs": {k: v for k, v in INPUTS[family].items() if k != key}})
                faults.append({**valid, "inputs": {**INPUTS[family], key: True}})
                if key != "memory_pack_sha256":
                    faults.append({**valid, "inputs": {**INPUTS[family], key: None}})
            faults.append({**valid, "inputs": {**INPUTS[family], "extra": "b" * 64}})
            for payload in faults:
                with self.subTest(family=family, payload=payload):
                    self.plant(family, payload)
                    self.assertEqual(self.read(family), (None, f"the {family} process receipt is malformed"))

    def test_evidence_precedence_and_family_diagnostics(self) -> None:
        for family in INPUTS:
            with self.subTest(family=family):
                self.assertEqual(self.evidence(family, identity=None),
                                 (False, f"the active {family} process identity is unavailable"))
                self.assertEqual(self.evidence(family), (False, f"the {family} process receipt is missing"))
                self.plant(family, self.payload(family))
                moved = {**INPUTS[family], "product_revision": "d" * 40, "product_sha256": "e" * 64}
                self.assertEqual(self.evidence(family, UnitProcessIdentity(123, 457, "invocation"), moved),
                                 (False, f"the {family} process receipt belongs to a different process generation"))
                changes = "product revision aaaaaaaaaaaa -> dddddddddddd; product sha256 bbbbbbbbbbbb -> eeeeeeeeeeee"
                expected = {"web": "the web process receipt inputs do not match the materialized state",
                            "memory": changes, "PO": f"the PO process receipt is stale: {changes}"}[family]
                self.assertEqual(self.evidence(family, inputs=moved), (False, expected))
                self.path(family).unlink()


class WebReceiptHardening(ProcessReceiptFixture):
    def test_boolean_version_is_malformed(self) -> None:
        for family in INPUTS:
            self.plant(family, {**self.payload(family), "version": True})
            self.assertEqual(self.read(family), (None, f"the {family} process receipt is malformed"))

    def test_oversized_valid_web_json_is_malformed(self) -> None:
        self.plant("web", self.payload("web"))
        with self.path("web").open("ab") as output:
            output.write(b" " * 65536)
        self.assertEqual(self.read("web"), (None, "the web process receipt is malformed"))

    def test_replaced_final_symlink_is_malformed(self) -> None:
        self.plant("web", self.payload("web"))
        path = self.path("web")
        target = path.with_name("target.json")
        target.write_bytes(path.read_bytes())
        real_lstat = Path.lstat

        def swap(candidate):
            info = real_lstat(candidate)
            if candidate == path:
                path.unlink()
                path.symlink_to(target)
            return info

        with mock.patch.object(Path, "lstat", side_effect=swap, autospec=True):
            self.assertEqual(self.read("web"), (None, "the web process receipt is malformed"))


class SharedReceiptFaults(ProcessReceiptFixture):
    def test_po_service_public_entry_publishes_self_identity_and_catches_shared_error(self) -> None:
        from ummanu.po import service

        with (
            mock.patch.dict(os.environ, {"INVOCATION_ID": "invocation"}),
            mock.patch.object(upgrade.os, "getpid", return_value=123),
            mock.patch.object(upgrade, "_process_start_ticks", return_value=456) as ticks,
            mock.patch.object(upgrade, "po_process_inputs", return_value=INPUTS["PO"]) as inputs,
            mock.patch.object(receipts, "write_process_receipt", wraps=receipts.write_process_receipt) as write,
        ):
            line = service._write_process_receipt(self.data)
        self.assertTrue(line.startswith("ummanu po: wrote the PO process receipt"), line)
        inputs.assert_called_once_with(Path(service.__file__).resolve().parents[3])
        ticks.assert_called_once_with(123)
        write.assert_called_once_with(self.path("PO"), receipts.PO, IDENTITY, INPUTS["PO"], None, receipts.ReceiptError)
        self.assertEqual(self.read("PO"), ((IDENTITY, INPUTS["PO"]), ""))
        before = self.path("PO").read_bytes()
        with mock.patch.dict(os.environ, {"INVOCATION_ID": ""}):
            line = service._write_process_receipt(self.data)
        self.assertEqual(line, "ummanu po: no process receipt was written (no INVOCATION_ID: "
                         "the process was not started by systemd); an upgrade will ask for a restart")
        self.assertEqual(self.path("PO").read_bytes(), before)

    def test_probe_failure_or_changing_generation_or_inputs_never_publishes(self) -> None:
        for family in ("memory", "web"):
            unit = f"ummanu-{family}.service"
            self.units.files[unit] = b"[Service]\nExecStart=/x --host 127.0.0.1 --port 8787\n"
            self.units.active.add(unit)
            for fault in ("probe", "generation", "inputs"):
                self.plant(family, {**self.payload(family), "process": {**PROCESS, "pid": 999}})
                before = self.path(family).read_bytes()
                changed = UnitProcessIdentity(123, 789, "invocation")
                identities = [IDENTITY, IDENTITY, changed if fault == "generation" else IDENTITY]
                final = {**INPUTS[family], "product_revision": "d" * 40} if fault == "inputs" else INPUTS[family]
                error = upgrade.WebProbeError if family == "web" else upgrade.MemoryProbeError
                with (
                    self.subTest(family=family, fault=fault),
                    mock.patch.object(self.units, "process_identity", side_effect=identities),
                    mock.patch.object(upgrade, f"{family}_process_inputs", side_effect=[INPUTS[family], final]),
                    mock.patch.object(upgrade, f"probe_{family}", side_effect=error("fault") if fault == "probe" else None),
                    mock.patch.object(receipts, "write_process_receipt") as write,
                ):
                    result = getattr(upgrade, f"step_{family}")(self.context)
                self.assertEqual(result.status, "failed", result.detail)
                write.assert_not_called()
                self.assertEqual(self.path(family).read_bytes(), before)

    def test_actual_producers_and_consumers_call_the_shared_owner(self) -> None:
        with (
            mock.patch.object(receipts, "write_private_receipt", wraps=receipts.write_private_receipt) as write,
            mock.patch.object(receipts, "read_process_receipt", wraps=receipts.read_process_receipt) as read,
            mock.patch.object(receipts, "mismatch", wraps=receipts.mismatch) as compare,
        ):
            ProcessReceiptContracts.test_actual_producers_publish_released_bytes_and_probe_order(self)
        self.assertEqual([call.args[-1] for call in write.call_args_list],
                         ["memory process receipt", "web process receipt", "PO process receipt"])
        self.assertEqual({call.args[1] for call in read.call_args_list}, {receipts.MEMORY, receipts.WEB, receipts.PO})
        self.assertEqual([call.args[0] for call in compare.call_args_list], [receipts.MEMORY, receipts.WEB, receipts.PO])
        self.assertIs(upgrade.ReceiptError, receipts.ReceiptError)

    def test_input_boundaries_and_exact_family_keys_through_actual_readers(self) -> None:
        for family in INPUTS:
            for key in INPUTS[family]:
                if key == "product_revision":
                    accepted, rejected = ["a" * 40, "a" * 64], ["a" * 39, "a" * 65, "A" * 40, "g" * 40]
                elif key == "memory_model":
                    accepted, rejected = ["!", "~" * 256], ["", "x" * 257, "a b", "\x7f", "модель"]
                else:
                    accepted, rejected = ["0" * 64], ["a" * 63, "a" * 65, "A" * 64, "g" * 64, 123]
                    if key == "memory_pack_sha256":
                        accepted.append(None)
                for valid, values in ((True, accepted), (False, rejected)):
                    for value in values:
                        with self.subTest(family=family, key=key, value=value):
                            payload = self.payload(family)
                            payload["inputs"][key] = value
                            self.plant(family, payload)
                            result, reason = self.read(family)
                            self.assertEqual(result is not None, valid, reason)

    def test_hostile_files_and_json_are_total_for_all_actual_readers(self) -> None:
        for family in INPUTS:
            path = self.path(family)
            path.parent.mkdir(parents=True, exist_ok=True)
            for raw in (b"\xff", b"{", b"[" * 2000 + b"]" * 2000, b"null", b"[]", b"1", b" " * 65537):
                with self.subTest(family=family, raw=raw[:16]):
                    path.write_bytes(raw)
                    self.assertEqual(self.read(family), (None, f"the {family} process receipt is malformed"))
            path.unlink()
            path.mkdir()
            self.assertEqual(self.read(family), (None, f"the {family} process receipt is malformed"))
            path.rmdir()
            path.symlink_to(path.with_name("missing"))
            self.assertEqual(self.read(family), (None, f"the {family} process receipt is malformed"))
            path.unlink()
            with mock.patch.object(Path, "lstat", side_effect=PermissionError("denied")):
                self.assertEqual(self.read(family), (None, f"the {family} process receipt cannot be read: denied"))

    def test_descriptor_regularity_growth_and_open_failures_through_all_families(self) -> None:
        for family in INPUTS:
            self.plant(family, self.payload(family))
            raw = self.path(family).read_bytes()
            for mode, size, body, valid in (
                (stat.S_IFIFO, 0, raw, False), (stat.S_IFDIR, 0, raw, False),
                (stat.S_IFREG, 65537, raw, False),
                (stat.S_IFREG, len(raw), raw + b" " * (65537 - len(raw)), False),
                (stat.S_IFREG, len(raw), raw + b" " * (65536 - len(raw)), True),
            ):
                with self.subTest(family=family, mode=mode, size=size, read_size=len(body)):
                    source = mock.MagicMock()
                    source.__enter__.return_value = source
                    source.fileno.return_value = 42
                    source.read.return_value = body
                    with (
                        mock.patch.object(receipts.os, "open", return_value=42) as opened,
                        mock.patch.object(receipts.os, "fdopen", return_value=source),
                        mock.patch.object(receipts.os, "fstat", return_value=SimpleNamespace(st_mode=mode, st_size=size)),
                    ):
                        result, reason = self.read(family)
                    self.assertEqual(result is not None, valid, reason)
                    opened.assert_called_once_with(self.path(family), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    if mode == stat.S_IFREG and size <= 65536:
                        source.read.assert_called_once_with(65537)
                    else:
                        source.read.assert_not_called()
                    source.__exit__.assert_called_once()
            with mock.patch.object(receipts.os, "open", side_effect=OSError("swapped")) as opened:
                self.assertEqual(self.read(family), (None, f"the {family} process receipt is malformed"))
            opened.assert_called_once()

    def test_private_publication_ownership_sync_order_and_typed_failure_cleanup(self) -> None:
        path = self.data / "private" / "receipt.json"
        old, new = {"old": 1}, {"new": 2}
        receipts.write_private_receipt(path, old, None, receipts.ReceiptError, "fixture")
        old_bytes = path.read_bytes()
        for failure in ("fchown", "fchmod", "fdopen", "fsync", "replace"):
            with (
                self.subTest(failure=failure),
                mock.patch.object(receipts.os, "geteuid", return_value=0),
                mock.patch.object(receipts.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=123, pw_gid=456)),
                mock.patch.object(receipts.os, "chown"),
                mock.patch.object(receipts.os, "fchown"),
                mock.patch.object(receipts.os, failure, side_effect=OSError("fault")) as operation,
                mock.patch.object(receipts.os, "close", wraps=receipts.os.close) as close,
            ):
                with self.assertRaisesRegex(upgrade.WebProcessReceiptError, "could not write fixture .*: fault"):
                    receipts.write_private_receipt(path, new, "runtime", upgrade.WebProcessReceiptError, "fixture")
                operation.assert_called_once()
                if failure in ("fchown", "fchmod", "fdopen"):
                    close.assert_called_once()
            self.assertEqual(path.read_bytes(), old_bytes)
            self.assertEqual(list(path.parent.glob(".*.tmp")), [])
        trace = []
        real_replace = receipts.os.replace

        def sync_file(fd):
            self.assertEqual(path.read_bytes(), old_bytes)
            trace.append("file-sync")

        def publish(source, target):
            trace.append("replace")
            real_replace(source, target)

        with (
            mock.patch.object(receipts.os, "geteuid", return_value=0),
            mock.patch.object(receipts.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=123, pw_gid=456)) as account,
            mock.patch.object(receipts.os, "chown") as chown,
            mock.patch.object(receipts.os, "fchown") as fchown,
            mock.patch.object(receipts.os, "fsync", side_effect=sync_file) as fsync,
            mock.patch.object(receipts.os, "replace", side_effect=publish) as replace,
            mock.patch.object(receipts, "fsync_directory", side_effect=lambda path: trace.append("directory-sync")) as directory,
        ):
            receipts.write_private_receipt(path, new, "runtime", receipts.ReceiptError, "fixture")
            account.assert_called_once_with("runtime")
            chown.assert_called_once_with(path.parent, 123, 456, follow_symlinks=False)
            self.assertEqual(fchown.call_args.args[1:], (123, 456))
            fsync.assert_called_once()
            replace.assert_called_once()
            directory.assert_called_once_with(path.parent)
        self.assertEqual(trace, ["file-sync", "replace", "directory-sync"])
        self.assertEqual(path.read_bytes(), b'{"new":2}\n')
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        with (
            mock.patch.object(receipts, "fsync_directory", side_effect=OSError("directory fault")) as directory,
            self.assertRaisesRegex(receipts.ReceiptError, "directory fault"),
        ):
            receipts.write_private_receipt(path, old, None, receipts.ReceiptError, "fixture")
        directory.assert_called_once_with(path.parent)
        # Rename already happened; a failed directory sync still reports failure and leaves whole bytes.
        self.assertEqual(path.read_bytes(), old_bytes)
        self.assertEqual(list(path.parent.glob(".*.tmp")), [])

    def test_missing_runtime_owner_and_symlink_directory_are_typed_errors(self) -> None:
        path = self.data / "receipt.json"
        with (
            mock.patch.object(receipts.os, "geteuid", return_value=0),
            mock.patch.object(receipts.pwd, "getpwnam", side_effect=KeyError("missing")) as account,
            self.assertRaisesRegex(receipts.ReceiptError, "runtime user 'missing' does not exist"),
        ):
            receipts.write_private_receipt(path, {}, "missing", receipts.ReceiptError, "fixture")
        account.assert_called_once_with("missing")
        self.assertFalse(path.exists())
        alias = self.data / "alias"
        alias.symlink_to(self.data, target_is_directory=True)
        with self.assertRaisesRegex(receipts.ReceiptError, "receipt directory is not a real directory"):
            receipts.write_private_receipt(alias / "receipt.json", {}, None, receipts.ReceiptError, "fixture")
        self.assertFalse(path.exists())

    def test_nonroot_writer_does_not_resolve_or_change_ownership(self) -> None:
        with (
            mock.patch.object(receipts.os, "geteuid", return_value=1000),
            mock.patch.object(receipts.pwd, "getpwnam") as account,
            mock.patch.object(receipts.os, "chown") as chown,
            mock.patch.object(receipts.os, "fchown") as fchown,
        ):
            receipts.write_private_receipt(self.data / "receipt.json", {}, "runtime", receipts.ReceiptError, "fixture")
        account.assert_not_called()
        chown.assert_not_called()
        fchown.assert_not_called()

    def test_reader_fdopen_failure_closes_the_descriptor(self) -> None:
        self.plant("web", self.payload("web"))
        with (
            mock.patch.object(receipts.os, "open", return_value=42),
            mock.patch.object(receipts.os, "fdopen", side_effect=OSError("fault")) as fdopen,
            mock.patch.object(receipts.os, "close") as close,
        ):
            self.assertEqual(self.read("web"), (None, "the web process receipt is malformed"))
        fdopen.assert_called_once_with(42, "rb")
        close.assert_called_once_with(42)
