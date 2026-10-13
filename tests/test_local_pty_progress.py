"""Screen-based progress folding, including replay of recorded provider spinner cells."""

from __future__ import annotations

import gzip
import hashlib
import math
import re
import time
import unittest
from pathlib import Path
from unittest import mock

from ummanu.runtime.head.local_pty import protocol, supervisor as module
from ummanu.runtime.redact import scrub_secrets

FIXTURES = Path(__file__).parent / "fixtures" / "local_pty_recorded"
# Small clips taken inside the longest spinner-only frame runs in five production PTY tails.
# The capture starts mid-screen, so each clip is fed once to establish existing screen cells
# before a turn's progress records are counted.
RECORDED = (
    ("claude_93d44d57", 50, 10),
    ("claude_48881cb1", 40, 10),
    ("claude_20f18b2a", 50, 10),
    ("codex_155d07a8", 126, 15),
    ("codex_8425c32f", 235, 15),
)
FULL_RECORDED = (
    ("claude_93d44d57_full", 397, 10),
    ("claude_48881cb1_full", 101, 10),
    ("claude_20f18b2a_full", 204, 10),
    ("codex_155d07a8_full", 126, 15),
    ("codex_8425c32f_full", 264, 15),
)


def _frames(name: str) -> list[bytes]:
    delimiter = b"\x1b[?25h" if name.startswith("claude") else b"\x1b[?2026l"
    payload = gzip.decompress((FIXTURES / f"{name}.bin.gz").read_bytes())
    parts = payload.split(delimiter)
    assert parts[-1] == b""
    return [part + delimiter for part in parts[:-1]]


def _start_turn(supervisor: module.Supervisor) -> None:
    supervisor._turn_open = False
    delivery = module._Delivery(1, b"x", "unit", 1.0)
    delivery.written = 1
    supervisor._delivery = delivery
    supervisor._finish_delivery(protocol.DELIVERY_COMPLETE, "")


class ProgressLinesTests(unittest.TestCase):
    def test_normalization_table(self) -> None:
        first = ["✢ Thinking 12s 456 tokens"]
        for candidate, equal in (
            (["✶ Thinking 93s 999 tokens"], True),
            (["* Thinking 1s 2 tokens"], True),
            (["⠋ Thinking 7s 8 tokens"], True),
            (["• Thinking 1s 2 tokens"], True),
            (["◦ Thinking 1s 2 tokens"], True),
            (["Thinking 1s 2 tokens"], True),
            (["Planning 1s 2 tokens"], False),
            (["Thinking 1s 2 tokens", "New finding"], False),
        ):
            with self.subTest(candidate=candidate):
                self.assertEqual(module._progress_lines(first) == module._progress_lines(candidate), equal)
        self.assertEqual(module._progress_lines(["", "  ", "✢ · •"]), set())


class ProgressFoldTests(unittest.TestCase):
    def setUp(self) -> None:
        self.supervisor = module.Supervisor(
            run_dir=Path("/tmp"), run_id="unit", role="worker", task="unit", command="true"
        )
        self.addCleanup(self.supervisor._selector.close)
        self.events: list[tuple[str, dict]] = []
        self.supervisor._append = lambda kind, **fields: self.events.append((kind, fields))  # type: ignore[method-assign]
        _start_turn(self.supervisor)

    def _window(self, data: bytes) -> None:
        self.supervisor._record_output(data)
        self.supervisor._flush_progress()

    def _progress(self) -> list[dict]:
        return [fields for kind, fields in self.events if kind == module.PROVIDER_PROGRESSED]

    def test_fold_keeps_bytes_and_counts_windows_until_new_content(self) -> None:
        first = b"\x1b[1;1H\x1b[2K* Thinking 1s 2 tokens"
        folded_a = "\x1b[1;1H\x1b[2K✶ Thinking 3s 4 tokens".encode()
        folded_b = "\x1b[1;1H\x1b[2K⠋ Thinking 5s 6 tokens".encode()
        new = b"\x1b[1;1H\x1b[2KNew finding"
        for data in (first, folded_a, folded_b):
            self._window(data)
        self.assertEqual(len(self._progress()), 1)
        self._window(new)
        self.assertEqual(len(self._progress()), 2)
        self.assertEqual(self._progress()[-1]["folded_windows"], 2)
        self.assertEqual(self._progress()[-1]["output_bytes"], len(folded_a + folded_b + new))

    def test_folded_windows_are_reported_when_a_turn_ends(self) -> None:
        self._window(b"\x1b[1;1H\x1b[2KThinking 1s")
        self._window(b"\x1b[1;1H\x1b[2KThinking 2s")
        self.supervisor._last_output_at = time.monotonic() - 3
        self.supervisor._tick()
        finished = [fields for kind, fields in self.events if kind == module.TURN_FINISHED]
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0]["folded_windows"], 1)

    def test_head_exit_reports_pending_folds_and_resize_reaches_the_screen(self) -> None:
        self.supervisor.set_winsize(3, 8)
        self.assertEqual((self.supervisor._screen.rows, self.supervisor._screen.cols), (3, 8))
        self._window(b"\x1b[1;1H\x1b[2Kalpha")
        self._window(b"\x1b[1;1H\x1b[2Kalpha")
        self.supervisor._head_status = 0
        self.supervisor._finish()
        finished = [fields for kind, fields in self.events if kind == module.TURN_FINISHED]
        self.assertEqual(finished[0]["folded_windows"], 1)

    def test_seen_cap_and_turn_reset(self) -> None:
        with mock.patch.object(module, "PROGRESS_SEEN_LINES_MAX", 2):
            for line in (b"alpha", b"beta", b"gamma"):
                self._window(b"\x1b[1;1H\x1b[2K" + line)
            self.assertEqual(len(self.supervisor._progress_seen), 2)
            self.assertNotIn(
                hashlib.blake2b(b"alpha", digest_size=16).digest(), self.supervisor._progress_seen
            )
            self._window(b"\x1b[1;1H\x1b[2Kalpha")
            self.assertEqual(len(self._progress()), 4)
            _start_turn(self.supervisor)
            self.assertEqual(self.supervisor._progress_seen, {})
            self.assertEqual(self.supervisor._progress_visible, set())
            self._window(b"\x1b[1;1H\x1b[2Kalpha")
            self.assertEqual(len(self._progress()), 5)

    def test_visible_lines_over_cap_do_not_retrigger_on_spinner_frames(self) -> None:
        with mock.patch.object(module, "PROGRESS_SEEN_LINES_MAX", 3):
            for row, word in enumerate(("alpha", "beta", "gamma", "delta", "epsilon"), 1):
                self._window(f"\x1b[{row};1H\x1b[2Kfinding {word}".encode())
            before = len(self._progress())
            self.assertEqual(before, 5)
            for frame in range(120):
                glyph = "✢ ✶ ✻ ✽ *".split()[frame % 5]
                self._window(f"\x1b[6;1H\x1b[2K{glyph} Thinking {frame}s {frame * 3} tokens".encode())
                self.assertLessEqual(len(self.supervisor._progress_seen), 3)
            self.assertLessEqual(len(self._progress()) - before, 1)


class RecordedSpinnerReplayTests(unittest.TestCase):
    def test_recorded_fixtures_have_no_credentials_paths_or_conversation(self) -> None:
        for name, _frames_expected, _rate in RECORDED + FULL_RECORDED:
            with self.subTest(run=name):
                payload = gzip.decompress((FIXTURES / f"{name}.bin.gz").read_bytes())
                text = payload.decode("utf-8")
                self.assertEqual(scrub_secrets(text), text)
                self.assertIsNone(
                    re.search(
                        r"/home/|/tmp/|/etc/|UMMANU_|Bearer\s|sk-[A-Za-z0-9]|ghp_", text, re.IGNORECASE
                    )
                )
                self.assertIsNone(
                    re.search(
                        r"\b(?:assistant|user|system):\s|\b(?:apply_patch|git commit|task report)\b",
                        text,
                        re.IGNORECASE,
                    )
                )

    def test_recorded_spinner_replay_and_new_text(self) -> None:
        for name, expected_frames, frames_per_second in RECORDED:
            frames = _frames(name)
            self.assertEqual(len(frames), expected_frames)
            for size in (1, 5, 12):
                with self.subTest(run=name, frames_per_window=size):
                    supervisor = module.Supervisor(
                        run_dir=Path("/tmp"), run_id="replay", role="worker", task="unit", command="true"
                    )
                    try:
                        events: list[str] = []
                        supervisor._append = lambda kind, events=events, **fields: events.append(kind)  # type: ignore[method-assign]
                        # A kept tail begins mid-screen. Build its existing cells first, then
                        # count only the recorded spinner frames in an open turn.
                        for frame in frames:
                            supervisor._screen.feed(frame)
                        _start_turn(supervisor)
                        ten_minutes = frames_per_second * 600
                        passes = math.ceil(ten_minutes / len(frames))
                        for iteration in range(passes):
                            for start in range(0, len(frames), size):
                                supervisor._record_output(b"".join(frames[start : start + size]))
                                supervisor._flush_progress()
                            if iteration == 0:
                                self.assertLessEqual(events.count(module.PROVIDER_PROGRESSED), 1)
                        self.assertLessEqual(events.count(module.PROVIDER_PROGRESSED), 5)
                        before = events.count(module.PROVIDER_PROGRESSED)
                        supervisor._record_output(b"\x1b[12;1H\x1b[2KFound a new result")
                        supervisor._flush_progress()
                        self.assertEqual(events.count(module.PROVIDER_PROGRESSED), before + 1)
                    finally:
                        supervisor._selector.close()

    def test_full_longest_runs_stay_within_the_bound_at_recorded_cadence(self) -> None:
        for name, expected_frames, frames_per_second in FULL_RECORDED:
            with self.subTest(run=name):
                frames = _frames(name)
                self.assertEqual(len(frames), expected_frames)
                supervisor = module.Supervisor(
                    run_dir=Path("/tmp"), run_id="replay", role="worker", task="unit", command="true"
                )
                try:
                    events: list[str] = []
                    supervisor._append = lambda kind, events=events, **fields: events.append(kind)  # type: ignore[method-assign]
                    for frame in frames:
                        supervisor._screen.feed(frame)
                    _start_turn(supervisor)
                    for _ in range(math.ceil(frames_per_second * 600 / len(frames))):
                        for start in range(0, len(frames), 5):
                            supervisor._record_output(b"".join(frames[start : start + 5]))
                            supervisor._flush_progress()
                    self.assertLessEqual(events.count(module.PROVIDER_PROGRESSED), 5)
                finally:
                    supervisor._selector.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
