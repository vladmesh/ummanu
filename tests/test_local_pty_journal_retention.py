"""Bounded supervisor journal, with files and injected syscalls, no live processes or sleeps.

The upper-bound exercise also runs against the exact c8bb5be journal source fixture. That is
fixture-on-base-byte evidence, not a pristine historical suite or a running old supervisor.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from tests.support.local_pty_journal import small_limits
from ummanu.runtime.head.local_pty import journal as j


def encoded(seq: int, kind: str = j.PROVIDER_PROGRESSED, **extra) -> bytes:
    return (json.dumps({"schema_version": 1, "seq": seq, "run_id": "run", "kind": kind,
                        "at": 1791975600.0, **extra}, separators=(",", ":")) + "\n").encode()


class ProducerRecordBudgetTests(unittest.TestCase):
    def test_native_sized_command_and_long_binding_fit_real_limits_and_metadata_read(self):
        from ummanu.runtime.local_pty_journal import head_run_first_record

        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = root / "journal.jsonl"
        command = "printf '%s' '" + "界" * 30000 + "'"
        with j.JournalWriter(path, "run") as writer:
            binding = writer.append(j.SCOPE_BOUND, binding={"invocation_id": "native",
                                     "admitted": {"workspace": "w" * 32000}})
            start = writer.append(j.RUN_STARTED, command=command, role="worker", task="task")
            self.assertGreater(path.stat().st_size, j.JOURNAL_TAIL_BYTES)
            self.assertLessEqual(path.stat().st_size, 2 * j.JOURNAL_RECORD_BYTES)
        with j.JournalWriter(path, "run") as writer:
            self.assertEqual(writer.seq, 2)
        self.assertEqual(j.read_events(path).events, (binding, start))
        metadata = head_run_first_record(root)
        self.assertEqual(metadata, {key: start.get(key) for key in ("task", "role", "run_id", "at")})
        self.assertNotIn(command, json.dumps(metadata))


class JournalRetentionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / "journal.jsonl"
        small_limits(self)

    def write(self, raw: bytes):
        self.path.write_bytes(raw)
        self.path.chmod(0o600)

    def aggregate(self) -> tuple[int, int]:
        files = list(self.root.iterdir())
        return sum(path.stat().st_size for path in files), len(files)

    def exercise_upper_bound(self, factory, count=500):
        with factory(self.path, "run") as writer:
            writer.append(j.RUN_STARTED, command="secret-command" * 100, head_pid=17)
            for _ in range(count):
                writer.append(j.PROVIDER_PROGRESSED, turn=1, output_bytes=200, padding="p" * 1100)
                size, files = self.aggregate()
                self.assertLessEqual(size, j.JOURNAL_MAX_BYTES, f"unbounded at seq {writer.seq}")
                self.assertLessEqual(files, 1)
                raw = self.path.read_bytes()
                self.assertTrue(raw.endswith(b"\n"))
                rows = [json.loads(line) for line in raw.splitlines()]
                self.assertEqual(rows[0]["kind"], j.RUN_STARTED)
                self.assertEqual(rows[-1]["seq"], writer.seq)
                self.assertTrue(all(a["seq"] < b["seq"] for a, b in zip(rows, rows[1:])))
        return writer.seq

    def test_many_rotations_bound_every_success_and_reopen_work(self):
        publications = []
        real_replace = os.replace

        def publish(*args, **kwargs):
            size, files = self.aggregate()
            publications.append(size)
            self.assertLessEqual(size, j.JOURNAL_TRANSIENT_BYTES)
            self.assertEqual(files, 2)
            return real_replace(*args, **kwargs)

        with mock.patch.object(j.os, "replace", side_effect=publish):
            last = self.exercise_upper_bound(j.JournalWriter)
        self.assertGreater(len(publications), 15)
        reading = j.read_events(self.path)
        self.assertTrue(reading.partial_head)
        self.assertTrue(j.read_tail(self.path, max_bytes=j.JOURNAL_MAX_BYTES).partial_head)
        self.assertTrue(j.tail_window(self.path, max_bytes=j.JOURNAL_MAX_BYTES)[1])
        self.assertFalse(reading.covers_since(10), "a pinned seq 1 cannot conceal the interior gap")
        self.assertTrue(reading.covers_since(last - 1))
        with j.JournalWriter(self.path, "run") as writer:
            self.assertEqual(writer.seq, last)
            self.assertEqual(writer.append(j.RUN_EXITED, head_pid=17, signal=9)["seq"], last + 1)
        self.assertLessEqual(self.path.stat().st_size, j.JOURNAL_MAX_BYTES)

    def test_same_upper_bound_fails_on_exact_base_source_fixture(self):
        fixture = Path(__file__).parent / "fixtures/local_pty_journal_c8bb5be.py.txt"
        source = fixture.read_bytes()
        self.assertEqual(hashlib.sha256(source).hexdigest(),
                         "3dc40c4c54fb0003851891e05175d40ea475b792d750b12af27e2c94918f534f")
        module = types.ModuleType("ummanu278_base_journal_fixture")
        self.enterContext(mock.patch.dict(sys.modules, {module.__name__: module}))
        exec(compile(source, str(fixture), "exec"), module.__dict__)
        with self.assertRaisesRegex(AssertionError, "unbounded at seq"):
            self.exercise_upper_bound(module.JournalWriter)
        self.assertGreater(self.path.stat().st_size, j.JOURNAL_MAX_BYTES)

    def test_scoped_authority_and_terminal_records_survive_many_rotations(self):
        with j.JournalWriter(self.path, "run") as writer:
            bound = writer.append(j.SCOPE_BOUND, binding={"invocation_id": "native", "proof": "s" * 2200})
            start = writer.append(j.RUN_STARTED, command="c" * 3000, head_pid=17)
            drain = writer.append(j.DRAIN_REQUESTED, initiator="worker")
            stopping = writer.append(j.RUN_STOPPING, initiator="worker")
            exit_record = writer.append(j.RUN_EXITED, head_pid=17, signal=9, head_loss_reason="memory_limit")
            for _ in range(500):
                writer.append(j.PROVIDER_PROGRESSED, turn=1, output_bytes=3, padding="p" * 1100)
                self.assertLessEqual(self.aggregate()[0], j.JOURNAL_MAX_BYTES)
        events = j.read_events(self.path).events
        for original in (bound, start, drain, stopping, exit_record):
            self.assertIn(original, events)

    def test_new_incarnation_keeps_first_metadata_and_current_authority(self):
        with j.JournalWriter(self.path, "run") as writer:
            first = writer.append(j.RUN_STARTED, head_pid=10, role="old", task="old")
            old_exit = writer.append(j.RUN_EXITED, head_pid=10, exit_code=0)
        with j.JournalWriter(self.path, "run") as writer:
            binding = writer.append(j.SCOPE_BOUND, binding={"invocation_id": "new"})
            current = writer.append(j.RUN_STARTED, head_pid=20, role="worker", task="new")
            for _ in range(180):
                writer.append(j.PROVIDER_PROGRESSED, turn=1, output_bytes=3, padding="p" * 1100)
        result = j.read_events(self.path)
        self.assertIn(first, result.events)
        self.assertIn(binding, result.events)
        self.assertIn(current, result.events)
        self.assertNotIn(old_exit, result.events)
        self.assertEqual(result.of_kind(j.SCOPE_BOUND), (binding,))

    def test_empty_and_large_legacy_input_enforce_cap_without_copying_whole_input(self):
        for empty in (b"", b"\n\n"):
            self.write(empty)
            with j.JournalWriter(self.path, "run") as writer:
                self.assertEqual(writer.seq, 0)
                writer.append(j.RUN_STARTED, command="c" * 3000)
        legacy = encoded(1, j.RUN_STARTED, command="legacy-secret" * 200)
        legacy += b"".join(encoded(seq, padding="x" * 2000) for seq in range(2, 2001))
        self.write(legacy)
        real_write = os.write
        staging_sizes = []

        def write(descriptor, raw):
            if descriptor != writer.fileno():
                staging_sizes.append(len(raw))
                self.assertLessEqual(len(raw), j.JOURNAL_RETAIN_BYTES)
            result = real_write(descriptor, raw)
            self.assertLessEqual(self.aggregate()[0], len(legacy) + j.JOURNAL_RETAIN_BYTES)
            return result

        writer = j.JournalWriter(self.path, "run")
        with mock.patch.object(j.os, "write", side_effect=write), writer:
            self.assertEqual(writer.seq, 2000)
            self.assertLessEqual(self.path.stat().st_size, j.JOURNAL_MAX_BYTES)
            self.assertEqual(writer.append(j.PROVIDER_PROGRESSED, turn=1)["seq"], 2001)
        self.assertEqual(len(staging_sizes), 1)
        self.assertTrue(j.read_events(self.path).partial_head)

    def test_oversized_refusal_precedes_bytes_sequence_and_rotation(self):
        with j.JournalWriter(self.path, "run") as writer:
            writer.append(j.RUN_STARTED, command="secret")
            before = self.path.read_bytes()
            with self.assertRaisesRegex(j.JournalError, "journal_record_too_large") as error:
                writer.append(j.RUN_STARTED, command="PRIVATE" * 1000)
            self.assertNotIn("PRIVATE", str(error.exception))
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(writer.seq, 1)
            self.assertEqual(writer.append(j.RUN_EXITED, head_pid=17)["seq"], 2)

    def test_reopen_reads_only_bounded_retained_input_and_bounded_lines(self):
        self.exercise_upper_bound(j.JournalWriter, count=100)
        real_fdopen = os.fdopen
        read_bytes = []

        class MeasuredStream:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                self.stream.__enter__()
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def readline(self, limit):
                self_test.assertLessEqual(limit, j.JOURNAL_RECORD_BYTES + 1)
                line = self.stream.readline(limit)
                read_bytes.append(len(line))
                return line

        self_test = self
        with (mock.patch.object(j.os, "fdopen", side_effect=lambda *args, **kwargs:
                                MeasuredStream(real_fdopen(*args, **kwargs))),
              j.JournalWriter(self.path, "run") as writer):
            self.assertEqual(writer.seq, 101)
        self.assertLessEqual(sum(read_bytes), j.JOURNAL_MAX_BYTES)

    def test_staging_substitution_before_publication_never_follows_foreign_path(self):
        writer = self.prepare_rotation()
        real_fsync = os.fsync
        foreign = self.root / "foreign"
        foreign.write_bytes(b"secret")
        staging = self.root / "journal.jsonl.retaining"

        def fsync(fd):
            real_fsync(fd)
            if fd not in (writer.fileno(), writer._directory):
                staging.unlink()
                staging.symlink_to(foreign)

        original = self.path.read_bytes()
        with (mock.patch.object(j.os, "fsync", side_effect=fsync),
              self.assertRaisesRegex(j.JournalError, "staging pathname was substituted")):
            writer.append(j.PROVIDER_PROGRESSED, turn=1, padding="x" * 3000)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(foreign.read_bytes(), b"secret")
        self.assertEqual(writer.fileno(), -1)

    def test_reader_holding_fd_sees_coherent_original_inode_at_publication(self):
        with j.JournalWriter(self.path, "run") as writer:
            writer.append(j.SCOPE_BOUND, binding={"invocation_id": "native"})
            writer.append(j.RUN_STARTED, head_pid=17)
            while self.path.stat().st_size < j.JOURNAL_MAX_BYTES - 1600:
                writer.append(j.PROVIDER_PROGRESSED, turn=1, padding="p" * 1100)
            with self.path.open("rb") as reader:
                old_raw = reader.read()
                real_replace = os.replace

                def replace(*args, **kwargs):
                    self.assertEqual(j.read_events(self.path).events[-1]["seq"], writer.seq)
                    result = real_replace(*args, **kwargs)
                    self.assertTrue(j.read_events(self.path).partial_head)
                    self.assertEqual(Path(f"/proc/self/fd/{reader.fileno()}").read_bytes(), old_raw)
                    return result

                with mock.patch.object(j.os, "replace", side_effect=replace):
                    writer.append(j.PROVIDER_PROGRESSED, turn=1, padding="p" * 3000)
                writer.append(j.PROVIDER_PROGRESSED, turn=1)
                self.assertEqual(Path(f"/proc/self/fd/{reader.fileno()}").read_bytes(), old_raw)
                self.assertEqual(os.fstat(writer.fileno()).st_ino, self.path.stat().st_ino)
                self.assertFalse(os.get_inheritable(writer.fileno()))

    def prepare_rotation(self):
        writer = j.JournalWriter(self.path, "run").open()
        writer.append(j.RUN_STARTED, command="c" * 1000)
        while self.path.stat().st_size < j.JOURNAL_MAX_BYTES - 1600:
            writer.append(j.PROVIDER_PROGRESSED, turn=1, padding="p" * 1100)
        return writer

    def test_rotation_faults_never_return_success_or_write_renamed_inode(self):
        for point in ("staging_write", "staging_fsync", "before_replace", "after_replace", "fd_switch", "directory_fsync"):
            with self.subTest(point=point):
                self.path.unlink(missing_ok=True)
                writer = self.prepare_rotation()
                floor = writer.seq
                original_fd = writer.fileno()
                real_write, real_fsync, real_replace = os.write, os.fsync, os.replace
                real_open = writer._open_file
                publication = {"done": False}

                def write(fd, raw, point=point, original_fd=original_fd, real_write=real_write):
                    if point == "staging_write" and fd != original_fd:
                        return real_write(fd, raw[:20])
                    return real_write(fd, raw)

                def fsync(fd, point=point, original_fd=original_fd, writer=writer, real_fsync=real_fsync):
                    if ((point == "staging_fsync" and fd not in (original_fd, writer._directory))
                            or (point == "directory_fsync" and fd == writer._directory)):
                        raise OSError("injected fsync failure")
                    return real_fsync(fd)

                def replace(*args, point=point, real_replace=real_replace, publication=publication, **kwargs):
                    if point == "before_replace":
                        raise OSError("injected publication failure")
                    result = real_replace(*args, **kwargs)
                    publication["done"] = True
                    if point == "after_replace":
                        raise OSError("injected postpublication failure")
                    return result

                def open_file(name, flags, point=point, publication=publication, real_open=real_open):
                    if point == "fd_switch" and publication["done"]:
                        raise OSError("injected reopen failure")
                    return real_open(name, flags)

                with (mock.patch.object(j.os, "write", side_effect=write),
                      mock.patch.object(j.os, "fsync", side_effect=fsync),
                      mock.patch.object(j.os, "replace", side_effect=replace),
                      mock.patch.object(writer, "_open_file", side_effect=open_file),
                      self.assertRaises((OSError, j.JournalError))):
                    writer.append(j.PROVIDER_PROGRESSED, turn=1, padding="p" * 3000)
                self.assertEqual(writer.seq, floor)
                self.assertEqual(writer.fileno(), -1)
                self.assertLessEqual(self.aggregate()[0], j.JOURNAL_TRANSIENT_BYTES)
                with self.assertRaises(j.JournalError):
                    writer.append(j.RUN_EXITED)
                with j.JournalWriter(self.path, "run") as recovered:
                    self.assertEqual(recovered.seq, floor)
                    self.assertEqual(recovered.append(j.RUN_EXITED)["seq"], floor + 1)
                self.assertFalse((self.root / "journal.jsonl.retaining").exists())

    def test_short_ordinary_write_and_uncertain_fsync_do_not_fabricate_success(self):
        for point in ("short_write", "fsync"):
            with self.subTest(point=point):
                self.path.unlink(missing_ok=True)
                writer = j.JournalWriter(self.path, "run").open()
                writer.append(j.RUN_STARTED)
                real_write = os.write
                patch = (mock.patch.object(j.os, "write", side_effect=lambda fd, raw, real_write=real_write: real_write(fd, raw[:20]))
                         if point == "short_write" else mock.patch.object(j.os, "fsync", side_effect=OSError("fsync")))
                with patch, self.assertRaises((OSError, j.JournalError)):
                    writer.append(j.RUN_EXITED)
                self.assertEqual(writer.seq, 1)
                self.assertEqual(writer.fileno(), -1)
                result = j.read_events(self.path)
                if point == "short_write":
                    self.assertTrue(result.truncated_tail)
                    before = self.path.read_bytes()
                    with self.assertRaises(j.JournalError):
                        j.JournalWriter(self.path, "run").open()
                    self.assertEqual(self.path.read_bytes(), before)
                else:
                    with j.JournalWriter(self.path, "run") as reopened:
                        self.assertEqual(reopened.seq, 2)
                        self.assertEqual(reopened.append(j.RUN_STARTED)["seq"], 3)

    def test_corruption_is_never_compacted_into_clean_history(self):
        for raw in (
            encoded(1, j.RUN_STARTED) + b"{torn",
            encoded(1, j.RUN_STARTED) + b"bad-json\n",
            encoded(2, j.RUN_STARTED) + encoded(1),
            encoded(1, j.RUN_STARTED).replace(b'"seq":1', b'"seq":1,"seq":1'),
            encoded(1, j.RUN_STARTED).replace(b'"seq":1', b'"seq":true'),
            encoded(1, j.RUN_STARTED).replace(b'"run"', b'"foreign"'),
            encoded(1, j.SCOPE_BOUND, binding={"invocation_id": "native"})
            + encoded(2, j.SCOPE_BOUND, binding={"invocation_id": "native"}),
            encoded(1, j.SCOPE_BOUND, binding={"invocation_id": "native"})
            + encoded(2, j.RUN_STARTED) + encoded(3, j.RUN_STARTED),
        ):
            with self.subTest(raw=raw[:30]):
                self.write(raw)
                with self.assertRaises(j.JournalError):
                    j.JournalWriter(self.path, "run").open()
                self.assertEqual(self.path.read_bytes(), raw)

    def test_path_permissions_and_staging_substitution_refuse(self):
        foreign = self.root / "foreign"
        foreign.write_bytes(b"private")
        foreign.chmod(0o600)
        self.path.symlink_to(foreign)
        with self.assertRaises(OSError):
            j.JournalWriter(self.path, "run").open()
        self.assertEqual(foreign.read_bytes(), b"private")
        self.path.unlink()
        self.write(encoded(1, j.RUN_STARTED))
        staging = self.root / "journal.jsonl.retaining"
        staging.symlink_to(foreign)
        with self.assertRaises(j.JournalError):
            j.JournalWriter(self.path, "run").open()
        self.assertTrue(staging.is_symlink())
        staging.unlink()
        self.path.chmod(0o644)
        with self.assertRaises(j.JournalError):
            j.JournalWriter(self.path, "run").open()
        self.path.chmod(0o600)
        with j.JournalWriter(self.path, "run") as writer:
            self.assertEqual(stat.S_IMODE(os.fstat(writer.fileno()).st_mode), 0o600)
            self.path.unlink()
            self.path.symlink_to(foreign)
            with self.assertRaises(j.JournalError):
                writer.append(j.RUN_EXITED)
        self.assertEqual(foreign.read_bytes(), b"private")
