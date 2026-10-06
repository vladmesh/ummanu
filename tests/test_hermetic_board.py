"""Offline status refuses ambient database credentials before any network access."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support.instance import status_instance
from ummanu.status import collect_status


class HermeticBoardTests(unittest.TestCase):
    def test_default_never_dials_out_even_with_live_looking_credentials(self):
        # No injected board here.  Even with live-looking libpq credentials, the
        # temporary instance has no board-store.env and must fail before any
        # network request; urlopen makes an accidental dial-out loud.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = status_instance(root)
            with (
                mock.patch.dict(
                    "os.environ",
                    {
                        "DATABASE_URL": "postgresql://svc:secret@board.invalid/board",
                        "PGHOST": "board.invalid",
                        "PGUSER": "svc",
                        "PGPASSWORD": "secret",
                    },
                ),
                mock.patch(
                    "urllib.request.urlopen",
                    side_effect=AssertionError("unit test reached a real board network call"),
                ),
            ):
                snapshot = collect_status(report, offline=True)

        self.assertEqual(snapshot["installation"]["sprints"]["error"]["code"], "backend_unavailable")
        self.assertEqual(snapshot["installation"]["sprints"]["items"], [])



if __name__ == "__main__":
    unittest.main()
