"""Coherent small journal byte limits for hermetic retention fixtures."""

from __future__ import annotations

import unittest
from unittest import mock

from ummanu.runtime.head.local_pty import journal


def small_limits(test: unittest.TestCase) -> None:
    for name, value in {
        "JOURNAL_RECORD_BYTES": 4096,
        "JOURNAL_ANCHOR_BYTES": 24576,
        "JOURNAL_RECENT_BYTES": 4096,
        "JOURNAL_RETAIN_BYTES": 28672,
        "JOURNAL_MAX_BYTES": 32768,
        "JOURNAL_TRANSIENT_BYTES": 61440,
    }.items():
        test.enterContext(mock.patch.object(journal, name, value))
