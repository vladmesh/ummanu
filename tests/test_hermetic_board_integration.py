"""An explicit disposable SQL board supplies sprint content to offline status."""

import tempfile
import unittest
from pathlib import Path

from tests.fakes.sprints import sprint_store, status_seed
from tests.support.instance import status_instance
from ummanu.status import collect_status


class HermeticBoardIntegrationTests(unittest.TestCase):
    def test_a_test_can_still_opt_in_to_a_real_sprint_boards_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = status_instance(root)
            board = sprint_store(self, status_seed())
            board.add_sprint("sprint:1")
            snapshot = collect_status(report, offline=True, sprint_client=board)

        self.assertIsNone(snapshot["installation"]["sprints"]["error"])
        self.assertEqual(len(snapshot["installation"]["sprints"]["items"]), 1)
