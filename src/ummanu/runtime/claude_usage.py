"""Capture Claude Code's statusLine rate_limits without calling a provider or reading auth.

Run as a statusLine command. --forward preserves an existing trusted shell command's stdin and
stdout. Only validated usage windows and a local observation time enter the private atomic cache.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

CACHE_NAME = "ummanu-usage.json"
MAX_CACHE_BYTES = 4096
MAX_PAYLOAD_BYTES = 1024 * 1024


def finite_number(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def usage_limits(raw: Any) -> dict[str, dict[str, float]]:
    """Keep only the two documented windows; reject invalid percentages and unsafe reset dates."""
    if not isinstance(raw, dict):
        return {}
    result = {}
    for name in ("five_hour", "seven_day"):
        window = raw.get(name)
        if not isinstance(window, dict):
            continue
        used, reset = window.get("used_percentage"), window.get("resets_at")
        if not finite_number(used) or not 0 <= used <= 100:
            continue
        # Unix seconds; bounded so datetime conversion remains portable and cannot overflow.
        if not finite_number(reset) or not 0 < reset < 253402300800:
            continue
        result[name] = {"used_percentage": used, "resets_at": reset}
    return result


def capture(payload: bytes, home: Path, *, observed: float | None = None) -> bool:
    """Atomically replace a 0600 snapshot; an absent/malformed payload leaves the last reading intact."""
    if len(payload) > MAX_PAYLOAD_BYTES:
        return False
    try:
        raw = json.loads(payload)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return False
    limits = usage_limits(raw.get("rate_limits")) if isinstance(raw, dict) else {}
    if not limits:
        return False
    # A re-render is not a new provider observation. Stat only: never read transcript contents.
    if observed is None:
        transcript = raw.get("transcript_path")
        if not isinstance(transcript, str) or not Path(transcript).is_absolute():
            return False
        try:
            observed = Path(transcript).stat().st_mtime
        except OSError:
            return False
        if observed > time.time():
            return False
    if not finite_number(observed) or observed <= 0:
        return False
    directory = home / ".claude"
    temporary = None
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Serialize heads before comparing timestamps, so an old session cannot win a write race.
        fd = os.open(directory / ".ummanu-usage.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            previous = None
            try:
                with (directory / CACHE_NAME).open("rb") as stream:
                    previous = json.loads(stream.read(MAX_CACHE_BYTES + 1))
            except (OSError, ValueError, UnicodeDecodeError, RecursionError):
                previous = None
            if isinstance(previous, dict) and previous.get("version") == 1:
                stamp = previous.get("observed_at")
                if finite_number(stamp) and (stamp >= observed or previous.get("rate_limits") == limits):
                    return False
            fd, temporary = tempfile.mkstemp(prefix=".ummanu-usage-", dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"version": 1, "observed_at": observed, "rate_limits": limits}, stream)
            os.replace(temporary, directory / CACHE_NAME)
        return True
    except OSError:
        return False
    finally:
        if temporary is not None:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass  # Best-effort capture must not prevent the original statusLine from running.


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--forward", help="existing trusted statusLine shell command")
    args = parser.parse_args()
    payload = sys.stdin.buffer.read(MAX_PAYLOAD_BYTES + 1)
    try:
        capture(payload, Path.home())
    except Exception:  # noqa: BLE001, S110 -- collection must never break the configured statusLine
        pass
    if args.forward and len(payload) <= MAX_PAYLOAD_BYTES:
        # Exactly the configured command, never a shell fragment taken from the CLI payload.
        subprocess.run(args.forward, shell=True, input=payload, check=False)


if __name__ == "__main__":
    main()
