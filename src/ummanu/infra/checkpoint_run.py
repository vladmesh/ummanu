"""Independent periodic checkpoint owner and upgrade-compatible durable records."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ummanu._fsutil import try_file_lock, write_json

CHECKPOINT_INTERVAL_SECONDS = 5 * 60


def load_checkpoint_state(data_dir: Path) -> dict[str, Any]:
    """Use legacy records only until the independent state file exists.

    An unreadable or malformed new file is authoritative empty state, so stale legacy success
    cannot hide a broken new producer. Missing cadence metadata forces a fresh first cut.
    """
    root = Path(data_dir) / "dispatcher"
    try:
        raw = json.loads((root / "checkpoint-state.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        try:
            raw = json.loads((root / "production-state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        key: value for key in ("checkpoint", "checkpoint_push") if isinstance(value := raw.get(key), dict)
    }


def run_checkpoint(runtime: Any) -> dict[str, Any]:
    """Run regardless of pause mode, without the tick or cleanup lock."""
    root = Path(runtime.data_dir) / "dispatcher"
    with try_file_lock(root / "checkpoint.lock") as acquired:
        if not acquired:
            return {
                "status": "skipped",
                "step": "checkpoint-run",
                "reason": "checkpoint singleton lock is held",
            }
        payload = load_checkpoint_state(runtime.data_dir)
        checkpoint, push = _coordinate_checkpoint(runtime, payload)
        write_json(root / "checkpoint-state.json", payload)
        failed = bool(checkpoint and checkpoint.get("status") in {"blocked", "failed"})
        failed = failed or bool(push and push.get("status") in {"failed", "diverged"})
        result: dict[str, Any] = {"status": "failed" if failed else "ok", "step": "checkpoint-run"}
        if checkpoint is not None:
            result["checkpoint"] = checkpoint
        if push is not None:
            result["checkpoint_push"] = push
        return result


def _coordinate_checkpoint(
    runtime: Any, payload: dict[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Prepare one current recovery snapshot when its local or remote window is due.

    This is the only periodic caller of ``CheckpointWriter``.  It uses the
    pusher's clock and due semantics so the five-minute preparation and the
    thirty-minute remote window do not stack.  A due push receives a snapshot
    prepared in this invocation or is explicitly withheld.
    """
    writer = getattr(runtime, "checkpoint", None)
    pusher = getattr(runtime, "checkpoint_push", None)
    previous = payload.get("checkpoint")
    write_state = dict(previous) if isinstance(previous, dict) else {}
    previous_push = payload.get("checkpoint_push")
    push_state = dict(previous_push) if isinstance(previous_push, dict) else {}
    now = _checkpoint_now(pusher)
    push_due = _checkpoint_push_due(pusher, push_state, now)
    checkpoint_due = _checkpoint_due(write_state, now)

    # A few constrained runtime tests deliberately have no checkpoint writer.
    # Preserve their former benign push-only behavior rather than fabricating a
    # blocked periodic checkpoint. Production constructs both dependencies.
    if writer is None:
        if pusher is None or not push_due:
            return None, None
        push = _push_checkpoint(runtime, push_state, now)
        payload["checkpoint_push"] = push
        return None, push
    if pusher is None and not checkpoint_due:
        checkpoint = _checkpoint_skipped(write_state, now)
        payload["checkpoint"] = checkpoint
        return checkpoint, None

    # A regular remote deadline needs a current preparation before delivery.
    # A remote already known to have diverged is deliberately rechecked by its
    # pusher on every tick, but cannot make the expensive board/run projection
    # run more often than its own five-minute deadline.
    preparation_due = checkpoint_due or _push_forces_preparation(push_due, push_state)
    if not preparation_due:
        checkpoint = _checkpoint_skipped(write_state, now)
        payload["checkpoint"] = checkpoint
        if not push_due or pusher is None:
            return checkpoint, None
        push = _push_checkpoint(runtime, push_state, now)
        payload["checkpoint_push"] = push
        return checkpoint, push

    checkpoint = _write_checkpoint(runtime, write_state, now)
    payload["checkpoint"] = checkpoint
    prepared = checkpoint.get("status") in {"committed", "unchanged"}
    if not push_due:
        return checkpoint, None
    if not prepared:
        push = _push_withheld_for_checkpoint(push_state, checkpoint, now)
        payload["checkpoint_push"] = push
        return checkpoint, push
    if pusher is None:
        return checkpoint, None
    push = _push_checkpoint(runtime, push_state, now)
    # This one-shot pusher retry means only "the next delivery needs a fresh
    # preparation". The preparation above satisfied that condition even when
    # the remote still fails or remains diverged, so it cannot pin the pusher
    # and this coordinator into a per-minute retry loop.
    if push_state.get("retry_pending"):
        push.pop("retry_pending", None)
    payload["checkpoint_push"] = push
    return checkpoint, push


def _checkpoint_now(pusher: Any) -> float:
    """Use the pusher's established controllable clock for both windows."""
    clock = getattr(pusher, "_clock", None)
    if callable(clock):
        try:
            return float(clock())
        except (TypeError, ValueError):
            pass
    return time.time()


def _checkpoint_due(state: dict[str, Any], now: float) -> bool:
    """Whether a fresh board/run preparation is due, fail-closed on old state."""
    if state.get("retry_pending"):
        return True
    successful = _checkpoint_success_epoch(state)
    # Existing payloads did not carry cadence metadata.  Their first upgraded
    # tick must make one fresh cut, rather than treating an old result as one.
    if successful is None:
        return True
    # A rollback is due now.  Otherwise clamp is not enough: a future marker
    # would park recovery forever.
    return now < successful or now - successful >= CHECKPOINT_INTERVAL_SECONDS


def _checkpoint_push_due(pusher: Any, state: dict[str, Any], now: float) -> bool:
    if pusher is None:
        return False
    due = getattr(pusher, "due", None)
    if not callable(due):
        return False
    try:
        return bool(due(state, now=now))
    except Exception:  # noqa: BLE001 - one step's failure is recorded, never ends the tick
        # A pusher that cannot answer its own public window contract gets a
        # fresh preparation and then records its own delivery failure. It must
        # not turn a failed preflight into permission to send an old snapshot.
        return True


def _push_forces_preparation(push_due: bool, state: dict[str, Any]) -> bool:
    """Whether this due push is an ordinary deadline, not a sticky recheck."""
    return push_due and not bool(state.get("remote_diverged"))


def _checkpoint_skipped(state: dict[str, Any], now: float) -> dict[str, Any]:
    """Record an inexpensive not-yet-due decision without reclassifying success."""
    started = time.perf_counter()
    result = dict(state)
    successful = _checkpoint_success_epoch(result)
    result.update(
        {
            "status": "skipped",
            "reason": "not due",
            # This outcome inherits the previous run's fields, so its duration is restated rather
            # than left behind: a skip that reported the last committed run's milliseconds would
            # read as an expensive checkpoint nobody ran.
            "duration_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "at": _checkpoint_rfc3339(now),
            "skip_epoch": now,
            "skip_at": _checkpoint_rfc3339(now),
            "next_due_epoch": successful + CHECKPOINT_INTERVAL_SECONDS if successful is not None else now,
            "next_due_at": _checkpoint_rfc3339(
                successful + CHECKPOINT_INTERVAL_SECONDS if successful is not None else now
            ),
        }
    )
    return result


def _write_checkpoint(runtime: Any, state: dict[str, Any], now: float) -> dict[str, Any]:
    """Prepare a fresh checkpoint and retain success/failure history separately."""
    started = time.perf_counter()
    writer = getattr(runtime, "checkpoint", None)
    if writer is None:
        raw = {"status": "blocked", "reason": "checkpoint writer is unavailable"}
    else:
        try:
            raw = writer.write().to_json()
        except Exception as exc:  # noqa: BLE001 - one step's failure is recorded, never ends the tick
            raw = {"status": "blocked", "reason": f"{type(exc).__name__}: {exc}"}
    result = dict(state)
    status = str(raw.get("status") or "blocked")
    reason = str(raw.get("reason") or "")
    result.update(raw)
    result.update(
        {
            "status": status,
            "reason": reason,
            # The writer times its own run and reports it in `raw`; a run that never reached the
            # writer, or one that died before it could return a result, is timed from out here so
            # that every outcome this coordinator records carries a duration of its own.
            "duration_ms": _number(raw.get("duration_ms"))
            or round((time.perf_counter() - started) * 1000.0, 3),
            "at": _checkpoint_rfc3339(now),
            "attempted_epoch": now,
            "attempted_at": _checkpoint_rfc3339(now),
        }
    )
    if status in {"committed", "unchanged"}:
        result.update(
            {
                "last_success_epoch": now,
                "last_success_at": _checkpoint_rfc3339(now),
                "last_success_status": status,
                "last_success_commit": str(raw.get("commit") or ""),
                "next_due_epoch": now + CHECKPOINT_INTERVAL_SECONDS,
                "next_due_at": _checkpoint_rfc3339(now + CHECKPOINT_INTERVAL_SECONDS),
                "retry_pending": False,
            }
        )
        result.pop("last_failure_epoch", None)
        result.pop("last_failure_at", None)
        result.pop("last_failure_reason", None)
        result.pop("failing_since_epoch", None)
        result.pop("failing_since_at", None)
    else:
        # `last_failure_*` moves with every failed run; `failing_since_*` keeps the first failure
        # after the last success, so doctor can say since when the checkpoint has not published.
        since = _number(state.get("failing_since_epoch")) if state.get("last_failure_reason") else 0.0
        since = since or now
        result.update(
            {
                "last_failure_epoch": now,
                "last_failure_at": _checkpoint_rfc3339(now),
                "last_failure_reason": reason or "checkpoint preparation failed",
                "failing_since_epoch": since,
                "failing_since_at": _checkpoint_rfc3339(since),
                "retry_pending": True,
            }
        )
    return result


def _push_withheld_for_checkpoint(
    state: dict[str, Any], checkpoint: dict[str, Any], now: float
) -> dict[str, Any]:
    reason = str(checkpoint.get("last_failure_reason") or checkpoint.get("reason") or "preparation failed")
    result = dict(state)
    result.update(
        {
            "status": "skipped",
            "reason": f"fresh checkpoint preparation failed; remote push withheld: {reason}",
            "attempted_epoch": now,
            "attempted_at": _checkpoint_rfc3339(now),
            "retry_pending": True,
        }
    )
    return result


def _checkpoint_rfc3339(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _checkpoint_success_epoch(state: dict[str, Any]) -> float | None:
    """A valid zero timestamp is a real successful preparation, not old state."""
    value = state.get("last_success_epoch")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _push_checkpoint(runtime: Any, state: dict[str, Any], now: float) -> dict[str, Any]:
    """Run a due remote window only after this tick's preparation succeeded."""
    pusher = getattr(runtime, "checkpoint_push", None)
    assert pusher is not None
    try:
        result = pusher.push(state, now=now)
    except Exception as exc:  # noqa: BLE001 - one step's failure is recorded, never ends the tick
        return _failed_push(state, exc, now)
    result = dict(result)
    # ``CheckpointPusher`` records this itself. Keep a pre-cadence compatible
    # pusher from being retried every minute merely because it omitted the
    # durable window marker from an otherwise successful result.
    if _number(result.get("attempted_epoch")) <= 0:
        result["attempted_epoch"] = now
        result["attempted_at"] = _checkpoint_rfc3339(now)
    return result


def _failed_push(state: dict[str, Any], exc: Exception, now: float) -> dict[str, Any]:
    result = dict(state)
    result.update(
        {
            "status": "failed",
            "reason": f"{type(exc).__name__}: {exc}",
            "attempted_epoch": now,
            "attempted_at": _checkpoint_rfc3339(now),
            "failures": int(result.get("failures") or 0) + 1,
        }
    )
    return result
