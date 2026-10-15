"""The production-tick command's exit status, which the oneshot dispatcher unit records."""

from __future__ import annotations

import argparse
import contextlib
import io
import tempfile
import unittest
from unittest import mock

from ummanu.dispatch import commands as dispatch_commands


class _Runtime:
    def __init__(self, data_dir: str, status: str) -> None:
        self.data_dir = data_dir
        self.status = status

    def production_tick(self) -> dict:
        return {"status": self.status, "step": "production-tick"}

    def production_probe(self) -> dict:
        return {"status": self.status, "step": "production-probe"}


def _exit_status(status: str, *, probe: bool = False) -> int:
    with tempfile.TemporaryDirectory() as data_dir:
        args = argparse.Namespace(
            instance=None, data_dir=data_dir, host_mode="noop", owner="test", probe=probe
        )
        with (
            mock.patch("ummanu.dispatch.commands.runtime_from_args", return_value=_Runtime(data_dir, status)),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            return dispatch_commands.run_dispatcher_production_tick(args)


class ProductionTickExitTests(unittest.TestCase):
    def test_a_degraded_tick_exits_zero(self) -> None:
        self.assertEqual(_exit_status("degraded"), 0)

    def test_a_blocked_tick_exits_three(self) -> None:
        self.assertEqual(_exit_status("blocked"), 3)

    def test_a_degraded_probe_still_exits_three(self) -> None:
        self.assertEqual(_exit_status("degraded", probe=True), 3)


if __name__ == "__main__":
    unittest.main()
