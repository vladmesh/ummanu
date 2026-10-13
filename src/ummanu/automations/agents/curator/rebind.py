"""Rebind curator state after a known move of Claude project directories (issue:db32299c8).

The transition of sprint:1475 moved a fixed set of cwds (`curator.move_paths.claude_move_paths`):
the curator's role workspace among them, and with them the Claude project directories whose
transcript paths key the watermark.  A pending record bound to the retired workspace refuses every
tick, and a cursor left under the old key would let harvest read the moved transcript from zero.

`plan_rebind` reads state and decides; it writes nothing.  The CLI applies the plan under the
cursor-settlement lock.  Rules:

* identity: rebound only when the record's workspace is a known old path that no longer exists, and
  the rebound identity is this run's.  The batch's facts are kept; only its cursor keys follow the
  same move as the watermark, and its batch id is re-signed.
* cursors: a key under a moved directory goes to its new key when the old file is absent, the new
  file exists, and the new key holds no cursor or one equal to or behind the old.  A new key already
  ahead wins and the old key is dropped.  Everything else is left as it is.

The whole plan is refused, with a named reason, rather than half applied.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from ummanu.automations.agents.curator import move_paths

from . import harvest

#: The transition whose moves this rebind follows (`curator.move_paths.claude_move_paths`).
MOVE_SPRINT = "sprint:1475"
SKIP_REASONS = ("outside_moves", "old_exists", "new_missing", "incomparable")


class RebindRefused(harvest.PendingError):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass
class RebindPlan:
    identity: str = "absent"  # absent | current | rebound
    old_workspace: str | None = None
    new_workspace: str | None = None
    carried: list[tuple[str, str]] = field(default_factory=list)
    superseded: list[tuple[str, str]] = field(default_factory=list)
    pending_keys: list[tuple[str, str]] = field(default_factory=list)
    skipped: Counter = field(default_factory=Counter)
    watermark_text: str | None = None
    pending_text: str | None = None

    def digest(self, st) -> str:
        """`state_digest` of the files after this plan: planned text, or the current bytes it keeps."""
        def after(text: str | None, path: Path) -> bytes | None:
            if text is not None:
                return text.encode("utf-8")
            return path.read_bytes() if path.exists() else None

        return state_digest(after(self.pending_text, st.pending_file), after(self.watermark_text, st.watermark_file))

    @property
    def changed(self) -> bool:
        return self.watermark_text is not None or self.pending_text is not None

    def counts(self) -> dict[str, object]:
        return {
            "rebound": int(self.identity == "rebound"),
            "carried": len(self.carried),
            "superseded": len(self.superseded),
            "pending_keys": len(self.pending_keys),
            "skipped": {reason: self.skipped.get(reason, 0) for reason in SKIP_REASONS},
        }


def state_digest(pending: bytes | None, watermark: bytes | None) -> str:
    """The digest a `rebind` audit line carries: the bytes of `pending.json` and `watermark.json` as
    the rebind publishes them (None for an absent file), so a reader can match it to the state."""
    digest = hashlib.sha256()
    for name, data in (("pending.json", pending), ("watermark.json", watermark)):
        digest.update(name.encode() + b"\0")
        digest.update(b"absent\0" if data is None else str(len(data)).encode() + b"\0" + data)
    return digest.hexdigest()


def _position(cursor: object) -> tuple[str, float] | None:
    """How far a cursor is, comparable only with a cursor of the same kind."""
    if not isinstance(cursor, dict):
        return None
    for kind in ("offset", "lines", "last_id"):
        if kind in cursor:
            value = cursor[kind]
            return (kind, value) if isinstance(value, int) and not isinstance(value, bool) else None
    mtime = cursor.get("mtime")
    if isinstance(mtime, (int, float)) and not isinstance(mtime, bool):
        return ("mtime", mtime)
    return None


def _read_watermark(st) -> dict:
    if not st.watermark_file.exists():
        return {}
    try:
        mark = json.loads(st.watermark_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RebindRefused("watermark-unreadable", "curator watermark is unreadable") from exc
    if not isinstance(mark, dict) or not all(isinstance(key, str) for key in mark):
        raise RebindRefused("watermark-unreadable", "curator watermark is malformed")
    return mark


def _read_record(st) -> dict:
    try:
        record = json.loads(st.pending_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RebindRefused("pending-unreadable", "curator pending record is unreadable") from exc
    if not isinstance(record, dict) or record.get("version") != harvest.PENDING_VERSION:
        raise RebindRefused("pending-legacy", "curator pending record is legacy or unsupported")
    identity, batch, base = record.get("identity"), record.get("batch"), record.get("base")
    if (
        not isinstance(identity, dict)
        or not harvest._well_formed_batch(batch)
        or not isinstance(base, dict)
        or not isinstance(record.get("selector"), str)
    ):
        raise RebindRefused("pending-invalid", "curator pending record has an invalid shape")
    if record.get("batch_id") != harvest._batch_id(identity, batch, base, _project(record)):
        raise RebindRefused("pending-invalid", "curator pending record identity does not match its contents")
    return record


def _project(record: dict) -> str | None:
    selected = record["selector"]
    return None if selected == harvest.selector(None) else selected


def plan_rebind(st, *, home: Path, projects: Path, current: dict[str, str]) -> RebindPlan:
    """What a rebind of `st` would change, with the Claude projects root `projects` under `home`."""
    moves = move_paths.claude_move_paths(home, MOVE_SPRINT)
    prefixes = [
        (str(projects / move_paths.claude_key(old)), str(projects / move_paths.claude_key(new))) for old, new in moves
    ]

    def moved(key: str) -> tuple[str | None, str | None]:
        new = move_paths.swap_prefix(key, prefixes)
        if new == key:
            return None, "outside_moves"
        if os.path.lexists(key):
            return None, "old_exists"
        if not os.path.exists(new):
            return None, "new_missing"
        return new, None

    plan = RebindPlan()
    mark = _read_watermark(st)
    renames: dict[str, str] = {}
    updates: dict[str, object] = {}
    drops: set[str] = set()
    for key, cursor in mark.items():
        new, reason = moved(key)
        if new is None:
            plan.skipped[reason] += 1
            continue
        if new in mark:
            old_at, new_at = _position(cursor), _position(mark[new])
            if old_at is None or new_at is None or old_at[0] != new_at[0]:
                plan.skipped["incomparable"] += 1
                continue
            drops.add(key)
            if new_at[1] > old_at[1]:
                plan.superseded.append((key, new))
            else:
                updates[new] = cursor
                plan.carried.append((key, new))
            continue
        renames[key] = new
        plan.carried.append((key, new))
    result: dict = {}
    for key, cursor in mark.items():
        if key in drops:
            continue
        if key in renames:
            result[renames[key]] = cursor
        else:
            result[key] = updates.get(key, cursor)
    if plan.carried or plan.superseded:
        plan.watermark_text = json.dumps(result, indent=2, ensure_ascii=False)

    if not st.pending_file.exists():
        return plan
    record = _read_record(st)
    identity = record["identity"]
    new_identity = identity
    if identity == current:
        plan.identity = "current"
    else:
        workspace = identity.get("workspace")
        known = {str(old): str(new) for old, new in moves}
        if workspace not in known:
            raise RebindRefused(
                "identity-outside-moves",
                f"curator pending record is bound to {workspace}, which is not a known moved workspace",
            )
        if os.path.lexists(workspace):
            raise RebindRefused(
                "old-workspace-exists", f"curator pending record is bound to {workspace}, which still exists"
            )
        new_identity = {**identity, "workspace": known[workspace]}
        if new_identity != current:
            raise RebindRefused(
                "identity-not-current",
                f"the rebound workspace {known[workspace]} is not this run's identity {current}; "
                "run from the role workspace or set TA_CURATOR_WORKSPACE",
            )
        plan.identity = "rebound"
        plan.old_workspace, plan.new_workspace = workspace, known[workspace]

    base, batch = record["base"], record["batch"]
    key_map: dict[str, str] = {}
    for key in [*base, *batch["pending"]]:
        new, _ = moved(key)
        if new is not None:
            key_map[key] = new
    for old_key, new_key in key_map.items():
        if new_key in base or new_key in batch["pending"]:
            raise RebindRefused("pending-key-collision", f"curator pending record names both {old_key} and {new_key}")
        if result.get(new_key) != base.get(old_key):
            raise RebindRefused(
                "pending-base-diverged",
                f"curator pending record's starting cursor for {old_key} does not match the cursor at {new_key}",
            )
    plan.pending_keys = sorted(key_map.items())
    if plan.identity == "rebound" or key_map:
        new_base = {key_map.get(key, key): value for key, value in base.items()}
        new_batch = {**batch, "pending": {key_map.get(key, key): value for key, value in batch["pending"].items()}}
        rebound = {
            **record,
            "identity": new_identity,
            "base": new_base,
            "batch": new_batch,
            "batch_id": harvest._batch_id(new_identity, new_batch, new_base, _project(record)),
        }
        plan.pending_text = json.dumps(rebound, ensure_ascii=False)
    return plan


def render(plan: RebindPlan, *, dry_run: bool) -> list[str]:
    lines = []
    if plan.identity == "rebound":
        lines.append(f"identity: rebind {plan.old_workspace} -> {plan.new_workspace}")
    else:
        lines.append(f"identity: {'no pending record' if plan.identity == 'absent' else 'current'}")
    lines += [f"  carry {old} -> {new}" for old, new in plan.carried]
    lines += [f"  drop {old} ({new} is ahead)" for old, new in plan.superseded]
    lines += [f"  pending key {old} -> {new}" for old, new in plan.pending_keys]
    counts = plan.counts()
    skipped = ", ".join(f"{reason}={count}" for reason, count in counts["skipped"].items())
    verdict = "plan (dry run, nothing written)" if dry_run else ("rebound" if plan.changed else "nothing to rebind")
    lines.append(
        f"curator rebind: {verdict}; rebound={counts['rebound']} carried={counts['carried']} "
        f"superseded={counts['superseded']} pending_keys={counts['pending_keys']}; skipped: {skipped}"
    )
    return lines
