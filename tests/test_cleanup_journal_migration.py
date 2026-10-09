"""Released-v1 cleanup journal migration and crash recovery, exercised in recovery CI."""

from __future__ import annotations

import os
import shutil
import unittest
from unittest import mock

from tests.support import cleanup_journal as layout
from tests.support.cleanup_journal import STATUSES, LegacyFixture, stored_files
from ummanu.dispatch import cleanup
from ummanu.dispatch.cleanup import INTENT_FILE_LIMIT, CleanupJournal
from ummanu.dispatch.types import HostError


class MigrationTests(layout.JournalTestCase):
    def test_v1_journal_over_40_mb_migrates_without_loss_and_once(self) -> None:
        original, body = LegacyFixture.load()
        self.assertGreaterEqual(len(body), 40_000_000)
        self.assertEqual({value["status"] for value in original["intents"].values()}, set(STATUSES))
        self.write_legacy(body)
        # Unmigrated, every reader answers from the v1 document and writes nothing.
        summary = self.journal.summary()
        # One ref per status (each answer reads the whole journal).
        refs = [f"ummanu-{n}" for n in range(8)]
        refusals = {ref: self.journal.admission_refusal(ref) for ref in refs}
        self.assertEqual(sorted(set(refusals.values()))[0], "")
        self.assertIn("has not verified", "".join(refusals.values()))
        self.assertFalse(self.journal.path.exists())

        self.assertTrue(self.journal.migrate())
        self.assertFalse(self.journal.legacy.exists())
        self.assertEqual(self.journal.archive.read_bytes(), body)
        migrated = self.journal.read()
        self.assert_lossless(original, migrated)
        sizes = [path.stat().st_size for path in self.journal.path.rglob("*.json")]
        self.assertLessEqual(max(sizes), INTENT_FILE_LIMIT)
        self.assertEqual(self.journal.summary(), summary)
        self.assertEqual({ref: self.journal.admission_refusal(ref) for ref in refusals}, refusals)
        print(f"v1 {len(body)} bytes -> {len(sizes)} files, max {max(sizes)}, total {sum(sizes)} bytes; "
              f"{self.journal.writes}")

        files = stored_files(self.journal)
        self.assertFalse(CleanupJournal(self.data).migrate())
        self.assertEqual(stored_files(self.journal), files)

    def test_crash_while_copying_resumes_from_the_untouched_v1_journal(self) -> None:
        original, body = LegacyFixture.load()
        self.write_legacy(body)
        replace = cleanup._replace_file
        calls = []

        def crash(path, data):
            calls.append(path)
            if len(calls) == 150:
                raise OSError("power lost while copying")
            return replace(path, data)

        with mock.patch.object(cleanup, "_replace_file", side_effect=crash), self.assertRaises(OSError):
            self.journal.migrate()
        self.assertFalse(self.journal.path.exists())
        self.assertEqual(self.journal.legacy.read_bytes(), body)
        self.assertEqual(self.journal.read()["intents"], original["intents"])
        self.assertTrue(CleanupJournal(self.data).migrate())
        self.assert_lossless(original, self.journal.read())
        self.assertFalse(os.path.lexists(self.journal._staging))

    def test_crash_at_publication_resumes(self) -> None:
        original, body = LegacyFixture.load()
        self.write_legacy(body)
        with mock.patch.object(cleanup.os, "rename", side_effect=OSError("crash before rename")), \
                self.assertRaises(OSError):
            self.journal.migrate()
        self.assertTrue(self.journal._staging.is_dir())
        self.assertFalse(self.journal.path.exists())
        self.assertEqual(self.journal.legacy.read_bytes(), body)
        self.assertTrue(CleanupJournal(self.data).migrate())
        self.assert_lossless(original, self.journal.read())
        self.assertEqual(self.journal.archive.read_bytes(), body)

    def test_crash_before_archiving_never_reads_the_v1_journal_again(self) -> None:
        original, body = LegacyFixture.load()
        self.write_legacy(body)
        with mock.patch.object(CleanupJournal, "_archive_legacy", side_effect=OSError("crash before archive")), \
                self.assertRaises(OSError):
            self.journal.migrate()
        self.assertTrue(self.journal.path.is_dir())
        self.assertTrue(self.journal.legacy.exists())
        key = sorted(original["intents"])[0]
        with mock.patch.object(CleanupJournal, "_read_legacy", side_effect=AssertionError("v1 read after publication")):
            # The published layout is authoritative; progress made on it is never rolled back.
            progressed = self.journal.intent(key)
            progressed.update(status="completed", reason="settled after publication")
            self.journal.commit_intent(key, progressed)
            self.assertFalse(self.journal.legacy.exists())
            self.assertEqual(self.journal.archive.read_bytes(), body)
            # A v1 file that reappears later is archived beside the first, unread.
            self.write_legacy(body)
            self.assertFalse(CleanupJournal(self.data).migrate())
            self.assertFalse(self.journal.legacy.exists())
            self.assertTrue(self.journal.archive.with_name("cleanup.v1-archive.1.json").exists())
            self.assertEqual(self.journal.intent(key)["status"], "completed")
            self.assertEqual(len(self.journal.read()["intents"]), len(original["intents"]))

    def test_unreadable_v1_journal_is_never_migrated_or_replaced(self) -> None:
        self.write_legacy(b"{not json")
        with self.assertRaisesRegex(HostError, "unreadable"):
            self.journal.migrate()
        with self.assertRaisesRegex(HostError, "unreadable"):
            self.journal.remember({"ref": "x-1", "id": 1, "project": "p"}, {"attempt_id": "a"})
        self.assertFalse(self.journal.path.exists())
        self.assertEqual(self.journal.legacy.read_bytes(), b"{not json")
        shutil.rmtree(self.journal._staging, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
