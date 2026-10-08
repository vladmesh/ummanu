"""Read-only installation status snapshot used by operators and automation."""

from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ummanu import head_registry
from ummanu.board.backend import SPRINT, board_client
from ummanu.checkpoint import checkpoint_snapshot
from ummanu.dispatch.headless import headless_cards, headless_worker
from ummanu.dispatch.observer import observer_snapshot
from ummanu.dispatch.pause import ProductionPause
from ummanu.dispatch.review import command_terminal_status
from ummanu.dispatch.state import DispatcherRecord
from ummanu.dispatch.tick_telemetry import (
    card_details,
    counter_values,
    duration_ms,
    phase_ms,
    reconcile_ms,
    tick_statistics,
)
from ummanu.dispatch.types import HostError
from ummanu.host import (
    CollectResult,
    FixtureHostSource,
    LiveHostSource,
    build_doctor_expectations,
)
from ummanu.host_apply import resolve_installed_packaged, resolve_runtime_owner
from ummanu.infra.checkpoint_run import load_checkpoint_state
from ummanu.infra.recovery_inventory import collect_recovery_inventory
from ummanu.runtime import interactive_workspace
from ummanu.secret_store import store_health
from ummanu.sprints import SprintReader, budget_thresholds
from ummanu.tasks import TaskError

if TYPE_CHECKING:
    from ummanu.board.sql_cards import SqlCardClient

STATUS_SCHEMA_VERSION = 1


def collect_status(
    report,
    *,
    host_fixture: str | None = None,
    offline: bool = False,
    sprint_client: SqlCardClient | None = None,
    recovery: dict[str, Any] | None = None,
    sprints: bool = True,
    probe_panels: bool | None = None,
) -> dict[str, Any]:
    """Return a stable, non-mutating snapshot for one validated instance.

    `sprints=False` leaves `installation.sprints` as an explicitly skipped section instead of
    reading every sprint the board holds: a caller that already reads sprints through the sprint
    protocol (the web dashboard) would otherwise pay that whole pass -- and carry every sprint's
    full status, hundreds of kilobytes on a live installation -- on a read whose subject is the
    host. `probe_panels` overrides the default of probing each attempt's runtime panel, which is
    the other per-attempt cost this collector has; `None` keeps the released behaviour.
    """
    assert report.data_dir is not None
    data_dir = report.data_dir
    instance_dir = report.instance_path.parent
    production = _read_object(data_dir / "dispatcher" / "production-state.json")
    expected = build_doctor_expectations(
        report.instance,
        report.bindings,
        data_dir=data_dir,
        packaged=resolve_installed_packaged(
            report.instance,
            instance_path=instance_dir,
            data_dir=data_dir,
        ),
    )
    if offline:
        collected = CollectResult(expected_to_empty_inventory())
    else:
        source = (
            FixtureHostSource(Path(host_fixture))
            if host_fixture
            else LiveHostSource(resolve_runtime_owner(instance_dir)[0])
        )
        collected = source.collect(expected)
    checkpoint_state = load_checkpoint_state(data_dir)
    checkpoint = checkpoint_snapshot(
        report.instance_path.parent,
        write_state=_object(checkpoint_state.get("checkpoint")),
        push_state=_object(checkpoint_state.get("checkpoint_push")),
        data_dir=report.data_dir,
    )
    # Status is a pollable metadata snapshot. Provider-backed readiness is therefore cache-only;
    # doctor supplies an explicitly live inventory when its caller permits live inspection.
    recovery = recovery or collect_recovery_inventory(report, inspect_live=False, checkpoint=checkpoint)
    if probe_panels is None:
        probe_panels = not offline and host_fixture is None
    return {
        "schema_version": STATUS_SCHEMA_VERSION,
        "installation": {
            "name": report.name or None,
            "instance": str(report.instance_path),
            "projects": report.projects,
            "heads": _heads(report.instance),
            "head_registry": _head_registry(report.instance_path.parent, report.data_dir),
            "interactive_workspace": interactive_workspace.describe(data_dir),
            "cards": {
                "total": _card_count(data_dir),
                "active_attempts": len(_attempts(production, probe_panels=False)),
            },
            "sprints": _sprints(
                data_dir, report.instance_path.parent, report.instance, production, client=sprint_client
            )
            if sprints
            else {"items": [], "error": None, "skipped": "sprints were not read on this call"},
        },
        "host": {
            "units": _units(expected, collected, offline=offline),
            "schedules": _schedules(expected, collected, offline=offline),
            "inventory_errors": collected.errors,
            "runtime_scopes": list(collected.inventory.runtime_scopes.scopes.values())
            if collected.inventory.runtime_scopes is not None else [],
            "resources": _host_resources(data_dir),
        },
        "dispatcher": {
            "phase": _text(production.get("phase")) or "new",
            "active_attempts": _attempts(production, probe_panels=probe_panels),
            "observers": _observers(production),
            "pause": _pause_status(data_dir, production),
            "divergences": _divergences(production),
            "reconciliation": _reconciliation(production),
            "last_tick": _last_tick(production),
            "tick_statistics": tick_statistics(production),
        },
        "checkpoint": checkpoint,
        "memory": _memory_status(data_dir),
        "secret_store": store_health(report.instance_path.parent),
        "recovery": recovery,
    }


def expected_to_empty_inventory():
    """Avoid probing the host in --offline mode.

    Empty inventory and no errors deliberately mean host facts are unavailable;
    status represents each expected resource with null presence instead.
    """
    from ummanu.host import HostInventory

    return HostInventory()


def _heads(instance: dict[str, Any]) -> list[dict[str, str]]:
    heads = instance.get("heads") if isinstance(instance, dict) else None
    if not isinstance(heads, list):
        return []
    return [
        {"role": item["role"], "model": item.get("model", "")}
        for item in heads
        if isinstance(item, dict) and isinstance(item.get("role"), str)
    ]


def _head_registry(instance_dir: Path, data_dir: Path | None = None) -> dict[str, Any]:
    """Where the live head registry came from: the snapshot file and the pin next to it.

    The dispatcher runs off the snapshot, so neither the file it was generated from nor the
    checkout that generated it is derivable from anything else an operator can see. `canonical`
    answers the first — an installation may own its registry, in which case the product revision
    alone would credit the wrong file, and `canonical_owner` says which side owns it. An
    installation upgraded before the pin existed reads back with null source and an error naming
    what to run. Nothing here consults a checkout: the snapshot is validated on its own.
    """
    record: dict[str, Any] = {
        "snapshot": "",
        "canonical": None,
        "canonical_owner": None,
        "product_root": None,
        "revision": None,
        "error": None,
    }
    try:
        pair = head_registry.installed_pair(instance_dir, data_dir)
    except head_registry.HeadRegistryConfigError as exc:
        record["error"] = str(exc)
        return record
    record["snapshot"] = str(pair.snapshot)
    try:
        source = head_registry.read_source(instance_dir, pair)
        head_registry.installed_heads(instance_dir, pair)
    except head_registry.HeadRegistryConfigError as exc:
        record["error"] = str(exc)
        return record
    if source is None:
        record["error"] = f"no canon source recorded; run `ummanu upgrade --instance {instance_dir}`"
        return record
    record["canonical"] = _text(source.get("canonical")) or None
    record["canonical_owner"] = _text(source.get("canonical_owner")) or None
    record["product_root"] = _text(source.get("product_root")) or None
    record["revision"] = _text(source.get("revision")) or None
    return record


def _sprints(
    data_dir: Path,
    instance_dir: Path,
    instance: dict[str, Any],
    production: dict[str, Any],
    *,
    client: SqlCardClient | None = None,
) -> dict[str, Any]:
    """Read the sprint entity and live board without consulting observer context."""
    try:
        reader = SprintReader(
            client if client is not None else board_client(instance_dir, serves=(SPRINT,)),
            data_dir=data_dir,
            thresholds=budget_thresholds(instance),
        )
        observers = {row["sprint"]: row for row in observer_snapshot(production)}
        return {
            "items": reader.statuses(observers=observers, headless=headless_cards(production), create=False),
            "error": None,
        }
    except TaskError as exc:
        return {"items": [], "error": {"code": exc.code, "message": exc.message}}


def _units(expected, collected: CollectResult, *, offline: bool) -> list[dict[str, Any]]:
    rows = []
    for name in sorted(expected.units):
        enabled, active = collected.inventory.unit_states.get(name, (None, None))
        rows.append(
            {
                "name": name,
                "kind": "timer" if name.endswith(".timer") else "service",
                "present": None
                if offline or "units" in collected.errors
                else name in collected.inventory.units,
                "enabled": enabled,
                "active": active,
            }
        )
    return rows


def _schedules(expected, collected: CollectResult, *, offline: bool) -> list[dict[str, Any]]:
    """Timers, each with systemd's last trigger: the evidence that the schedule actually ran."""
    triggers = collected.inventory.timer_triggers
    return [
        {**row, "last_trigger": None if offline else triggers.get(row["name"])}
        for row in _units(expected, collected, offline=offline)
        if row["kind"] == "timer"
    ]


def _attempts(production: dict[str, Any], *, probe_panels: bool) -> list[dict[str, Any]]:
    records = production.get("records")
    if not isinstance(records, dict):
        return []
    attempts = []
    for reference, record in sorted(records.items()):
        if not isinstance(reference, str) or not isinstance(record, dict):
            continue
        worker = _watchdog(record, reference, "worker", probe_panels)
        reviewer = _watchdog(record, reference, "review", probe_panels)
        headless = headless_worker(record)
        attempts.append(
            {
                "reference": reference,
                "attempt_id": _text(record.get("attempt_id")) or None,
                "state": _text(record.get("state")) or None,
                "worker": _text(record.get("worker")) or None,
                "head": _text(record.get("head")) or None,
                "review_head": _text(record.get("review_head")) or None,
                "workspace": _text(record.get("workspace")) or None,
                "watchdogs": {"worker": worker, "reviewer": reviewer},
                # An In progress column is not evidence of active work: a card whose worker the
                # dispatcher cannot name reads as degraded here, with what the recovery is holding
                # (secretary-1544).
                "headless": headless,
                "degraded": headless is not None,
                "paused": {
                    "worker": _float(record.get("paused_worker_at")) > 0,
                    "reviewer": _float(record.get("paused_reviewer_at")) > 0,
                },
            }
        )
    return attempts


def _watchdog(record: dict[str, Any], reference: str, kind: str, probe_panels: bool) -> dict[str, Any]:
    prefix = "review" if kind == "review" else "worker"
    panel: dict[str, Any] = {"known": False, "live": None, "reason": "not-probed"}
    if probe_panels:
        try:
            panel = command_terminal_status(
                _StatusWatchdogHost(), {"ref": reference}, DispatcherRecord.from_json(record), kind=kind
            )
        except (HostError, OSError, OverflowError, TypeError, ValueError) as exc:
            panel = {"known": False, "live": None, "reason": str(exc)}
    return {
        "panel": panel,
        "last_progress_at": _epoch(_float(record.get(f"{prefix}_progress_at"))),
        "waiting_since": _epoch(_float(record.get(f"{prefix}_waiting_since"))),
        "respawns": int(_float(record.get(f"{prefix}_respawns"))),
    }


class _StatusWatchdogHost:
    """Read-only host for the liveness probe the dispatcher watchdog makes (its pid heartbeat).

    It carries no transport: `command_terminal_status` reads no pane since secretary-1723.
    """

    mode = "real"


def _observers(production: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per sprint the dispatcher tracks an observer head for.

    Enough to answer "is my sprint being watched, and if not, why" without opening a transcript:
    the head profile, whether its pid is alive right now, when the dispatcher last acted on it, and
    the reason a launch is parked.
    """
    return [
        {
            "sprint": row["sprint"],
            "head": row["head"] or None,
            "state": row["state"],
            "alive": row["alive"],
            "pid_known": row["pid_known"],
            "launches": row["launches"],
            # A live pid here belongs to a bring-up that failed with its terminal still up, not to
            # a working observer: without this flag `alive: true` would read as a watched sprint.
            "abandoned_handle": row["abandoned_handle"],
            # False while a head launched before the sprint binding is still up: it is alive and
            # cannot authenticate a single write, and the next tick retires it.
            "bound": row["bound"],
            # False for a head adopted from a launch intent: it is watching its sprint, but its
            # terminal handle died with the tick that opened it and its stop goes by workspace.
            "handle_known": row["handle_known"],
            "workspace": row["workspace"] or None,
            "last_action": row["last_action"] or None,
            "last_action_at": _epoch(row["last_action_at"]),
            "deferred_reason": row["deferred_reason"] or None,
            "stopped_reason": row["stopped_reason"] or None,
            "paused": row["paused"],
            "idle_since": _epoch(row["idle_since"]),
            "idle_reason": row["idle_reason"] or None,
            # Wakes this sprint's observer was owed and did not get, cumulative over the sprint
            # and separate from a reviewer that failed to come up on a card: a sprint whose head
            # was never reached must not read here as one whose deliveries all landed.
            "delivery_failures": _delivery_failures(row.get("delivery")),
            "delivery_last_failure": _delivery_last_failure(row.get("delivery")),
        }
        for row in observer_snapshot(production)
    ]


def _delivery_failures(delivery: Any) -> int:
    if not isinstance(delivery, dict):
        return 0
    return int(delivery.get("wake_failures") or 0) + int(delivery.get("launch_delivery_failures") or 0)


def _delivery_last_failure(delivery: Any) -> str | None:
    if not isinstance(delivery, dict):
        return None
    reason = str(delivery.get("last_failure_reason") or "")
    if not reason:
        return None
    return f"{delivery.get('last_failure_method') or 'observer-wake'}: {reason}"


def _last_tick(production: dict[str, Any]) -> dict[str, Any] | None:
    """How the last production tick ended and how long it took, or None if none has been recorded.

    `record_tick_telemetry` has folded the terminal outcome of every tick into
    `tick_telemetry.last` for as long as it has existed, and status carried nothing from it: an
    operator asking why the pipeline felt slow had to read the dispatcher's state file by hand.
    The whole entry is exposed rather than the duration alone, because a duration next to no
    outcome cannot be read — three seconds is healthy for a tick that launched a head and alarming
    for one that did nothing.
    """
    telemetry = production.get("tick_telemetry")
    entry = telemetry.get("last") if isinstance(telemetry, dict) else None
    if not isinstance(entry, dict):
        return None
    duration = entry.get("duration_ms")
    return {
        "seq": int(_float(entry.get("seq"))),
        "at": _text(entry.get("at")),
        "status": _text(entry.get("status")),
        "step": _text(entry.get("step")),
        "healthy": bool(entry.get("healthy")),
        "reason": _text(entry.get("reason")),
        "actions": int(_float(entry.get("actions"))),
        "error_count": int(_float(entry.get("error_count"))),
        "degraded_count": int(_float(entry.get("degraded_count"))),
        # Null, not zero, for a tick recorded before this field existed: a state file written by
        # the previous release has no duration, and 0 ms would be a measurement nobody made.
        "duration_ms": duration_ms(duration),
        "phases": (phases := phase_ms(entry.get("phases"))),
        # The aggregate the reconcile budget is judged by: its exclusive sub-phases summed.
        "reconcile_ms": reconcile_ms(phases),
        # The tick's successful writes, its own terminal save included, and the slowest cards' advance
        # (records handed in, flushes and writes); null before 131.
        "counters": counter_values(entry.get("counters")),
        "cards": card_details(entry.get("cards")),
    }


def _divergences(production: dict[str, Any]) -> dict[str, Any]:
    """Explicit counts and rows, never null, so a reader cannot mistake "we have not looked"

    for "there are none". A divergence closes once its card leaves the active dispatcher cycle
    (`dispatch.production._reconcile_production`); one still open is either tied to a card
    still in flight or is genuinely unresolved.
    """
    raw = production.get("controlled_divergences")
    items = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
    open_items = [item for item in items if item.get("status") != "closed"]
    return {
        "open_count": len(open_items),
        "total_count": len(items),
        "open": [
            {
                "id": _text(item.get("id")),
                "pilot_ref": _text(item.get("pilot_ref")),
                "step": _text(item.get("step")),
                "reason": _text(item.get("reason")),
                "opened_at": _text(item.get("at")),
            }
            for item in open_items
        ],
    }


def _reconciliation(production: dict[str, Any]) -> dict[str, Any]:
    """Evidence that the production tick has actually run its reconciliation pass.

    `last_tick_finished_at` predates reconciliation and is stamped by every tick regardless of
    dispatcher version, so it cannot tell a reconciled host from a pre-deployment one still
    running the old code. `last_reconciled_at` is only ever written by the reconciliation pass
    itself (`dispatch.production._reconcile_production`), so it stays null, honestly reporting
    "unknown", until a tick running the new code has actually completed one.
    """
    records = production.get("records")
    records = records if isinstance(records, dict) else {}
    return {
        "last_tick_finished_at": _text(production.get("last_tick_finished_at")) or None,
        "last_reconciled_at": _text(production.get("last_reconciled_at")) or None,
        "records_tracked": len(records),
    }


def _pause_status(data_dir: Path, production: dict[str, Any]) -> dict[str, Any]:
    pause = ProductionPause(data_dir).summary()
    paused = {
        ref: {
            "worker": _float(record.get("paused_worker_at")) > 0,
            "reviewer": _float(record.get("paused_reviewer_at")) > 0,
        }
        for ref, record in production.get("records", {}).items()
        if isinstance(ref, str) and isinstance(record, dict)
    }
    return {
        "paused": bool(pause.get("paused")),
        "mode": _text(pause.get("mode")) or None,
        "since": _text(pause.get("since")) or None,
        "actor": _text(pause.get("actor")) or None,
        "reason": _text(pause.get("reason")) or None,
        "auto_resume": pause.get("auto_resume") if isinstance(pause.get("auto_resume"), dict) else None,
        "cards": paused,
        "warnings": pause.get("warnings") if isinstance(pause.get("warnings"), list) else [],
    }


def _memory_status(data_dir: Path) -> dict[str, Any]:
    memory = data_dir / "memory"
    manifest = _read_object(memory / "manifest.json")
    journal = _object(manifest.get("journal")) or {}
    facts = journal.get("fact_count")
    if not isinstance(facts, int) or isinstance(facts, bool):
        facts = _export_fact_count(memory / "export.ndjson")
    index = memory / "index.sqlite"
    return {
        "fact_count": facts,
        "last_reindex_at": _mtime(index),
        "index_present": index.is_file(),
    }


def _host_resources(data_dir: Path) -> dict[str, Any]:
    return {
        "disk_free_bytes": disk_free_bytes(data_dir),
        "memory_available_bytes": _memory_available(),
        "load_average": _load_average(),
    }


def disk_free_bytes(data_dir: Path) -> int | None:
    """Free bytes on the configured data root's filesystem, or unknown on probe failure."""
    try:
        probe = data_dir
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        free = shutil.disk_usage(probe).free
        return free if type(free) is int and free >= 0 else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _memory_available() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _load_average() -> list[float] | None:
    try:
        return list(os.getloadavg())
    except OSError:
        return None


def _card_count(data_dir: Path) -> int | None:
    path = data_dir / "board" / "cards.ndjson"
    try:
        return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    except (OSError, UnicodeError):
        return None


def _export_fact_count(path: Path) -> int | None:
    try:
        return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    except (OSError, UnicodeError):
        return None


def _mtime(path: Path) -> str | None:
    try:
        return (
            datetime.fromtimestamp(path.stat().st_mtime, UTC)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except OSError:
        return None


def _read_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _object(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _float(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _epoch(value: float) -> str | None:
    if value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None
