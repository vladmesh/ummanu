"""Deterministic anomaly signals — what the steward's precheck gate and `/steward` skill both
read before anything judges.

Five signal kinds: new Blocked card, an unhealthy production dispatcher tick since the steward's
watermark, a card sitting in an active column past STALE_HOURS, a resource health flip, a
worker/reviewer workspace on disk with no in-flight card record. Any one is enough for precheck
to spawn the head; finding none costs a few board reads and a couple of file stats, no LLM.

Every signal dedupes against a persisted watermark (state/steward/watermark.json) keyed by
anomaly kind, so a condition that has not changed since the last run does not re-spawn the head
every hour. `scan` is read-only; `advance` folds the scanned state into the watermark, two-phase,
so a crash between the two re-scans instead of silently dropping a signal.
"""

from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, TypedDict
from ummanu.runtime.state import AgentState

from ...runtime import production_telemetry

STATE = AgentState("steward")

# Columns where a long dwell is itself worth a look. "Issues" (backlog, not yet triaged into Ready)
# and "Done" (terminal) are excluded — sitting there indefinitely is the expected shape, not an
# anomaly.
# "Assessment" is in for the opposite reason: it waits on the observer rather than on a head, so
# no watchdog times it out. If the observer never comes back to decide release, rework or reslice,
# this signal is the only thing that notices.
STALE_COLUMNS = ("Ready", "In progress", "Validate", "Assessment", "Blocked")
STALE_HOURS = float(os.environ.get("TA_STEWARD_STALE_HOURS", "24"))

def _workspaces_root() -> Path:
    """Card workspaces owned by the production dispatcher on this installation.

    Standing-role worktrees still live under ~/orca/workspaces; card worktrees moved to the
    installation data plane in A20. Resolve per scan so a rendered agent unit follows the same
    UMMANU_DATA_DIR / instance data_dir that production_telemetry already uses.
    """
    return production_telemetry.data_dir() / "workspaces"


class StewardSignalCard(TypedDict):
    """The only active-board fields needed by steward anomaly detection."""

    reference: str
    state: str
    column: str
    project: str
    date_moved: int | None
    steward_report: str


class StewardSignalReader(Protocol):
    def active_cards(
        self, *, states: set[str] | None = None, project: str | None = None
    ) -> list[StewardSignalCard]: ...


_STATE_COLUMNS = {
    "issues": "Issues",
    "ready": "Ready",
    "in_progress": "In progress",
    "validate": "Validate",
    "assessment": "Assessment",
    "blocked": "Blocked",
    "done": "Done",
}


def resolve_reader(reader: StewardSignalReader | None = None) -> StewardSignalReader:
    """Return the explicitly composed board reader.

    The generic steward helpers intentionally have no board implementation of
    their own.  Live automation supplies Ummanu's canonical adapter through
    ``ummanu.automations.composition``; a missing port is a wiring error,
    never an opportunity to bypass audit and sprint guards through the retired
    pipeline CLI.
    """
    if reader is None:
        raise RuntimeError("steward board reader must be supplied by the composition root")
    return reader


# Both pipeline signals — unhealthy ticks and resource flips — are reached across a process
# boundary through the production dispatcher's own data plane (runtime/production_telemetry.py),
# never through AgentState("pipeline"): that resolves STATE_ROOT (ummanu/runtime/state.py) per process,
# and the steward's systemd unit runs in its own worktree with its own environment. A path that
# can never exist there returns no hits, indistinguishable from "checked, nothing new" — the
# blindness of triggered-agents-253, which secretary-833 found again on a worktree copy of
# resource_health.json that the production dispatcher does not write.


def _empty_watermark() -> dict:
    return {
        # None, not 0: no baseline has been taken yet. See _pipeline_tick_signals.
        "pipeline_incident_total": None,
        "pipeline_recovery_total": None,
        # Which telemetry history the counters above were read from — "" while the dispatcher has
        # not stamped one. See _pipeline_tick_signals.
        "pipeline_telemetry_generation": "",
        "notified_blocked": [],
        "notified_stale": {},
        "notified_orphans": [],
        "resource_status": {},
    }


def load_watermark() -> dict:
    mark = _empty_watermark()
    mark.update(STATE.load_watermark())
    return mark


def _pipeline_tick_signals(mark: dict) -> tuple[list[dict], dict]:
    """(pipeline incidents/recoveries since the watermark, the counters to fold into it).

    The source is the production dispatcher's durable tick telemetry — the record written by the
    timer that actually moves cards on this host. A log written by anything else is a file whose
    silence cannot be told apart from a healthy pipeline.

    The unit reported here is the INCIDENT, not the tick, so two counters are read:

          * `incident_total` — one `pipeline-tick-unhealthy` per incident, carrying the tick that
            opened it, so the cause is in the hit. Further failing ticks of the same incident add
            nothing.
          * `recovery_total` — one `pipeline-tick-recovered` per closed incident, naming the incident
            it closes, its cause and the healthy tick that ended it.

    Both are monotonic within a generation, so an ordinary tick in between cannot consume an event
    the steward has not looked at yet.

    A first scan with no counters in the watermark takes the current ones as a BASELINE and reports
    nothing; otherwise a cold start would replay whatever the state file happens to hold as new. The
    counters are only meaningful within one telemetry history, so the watermark stores the
    `generation` they were read from and reports the open incident again the moment that changes — a
    restored state file starts counting again and can land on numbers the watermark already holds. A
    backwards counter is a reset on its own; a host that stamps no generation leaves "cannot tell",
    not "changed".

    Telemetry that cannot be read is NOT "nothing to report": that silence is indistinguishable from
    a healthy pipeline, so it is reported as a synthetic warn hit AND logged into the steward's own
    runs.jsonl, where the gap stays durably visible outside one scan's in-memory batch.
    """
    telemetry = production_telemetry.read()
    if not telemetry.available:
        STATE.log_run(telemetry.unavailable, level="warn", path=str(telemetry.path))
        # The watermark does not move over a source that could not be read.
        return (
            [{"event": telemetry.unavailable, "level": "warn", "path": str(telemetry.path)}],
            {
                "pipeline_incident_total": mark["pipeline_incident_total"],
                "pipeline_recovery_total": mark["pipeline_recovery_total"],
                "pipeline_telemetry_generation": mark["pipeline_telemetry_generation"],
            },
        )
    generation = telemetry.generation
    pending = {
        "pipeline_incident_total": telemetry.incident_total,
        "pipeline_recovery_total": telemetry.recovery_total,
        "pipeline_telemetry_generation": generation,
    }
    seen_incidents = mark["pipeline_incident_total"]
    if seen_incidents is None:
        return [], pending
    seen_recoveries = mark["pipeline_recovery_total"] or 0
    seen_generation = str(mark["pipeline_telemetry_generation"] or "")
    replaced = bool(generation and seen_generation and generation != seen_generation)
    hits: list[dict] = []
    if replaced or telemetry.incident_total < seen_incidents or telemetry.recovery_total < seen_recoveries:
        # The history restarted: the state file was replaced (a restore, a rebuilt installation, a
        # hand edit), either under a new generation or with a counter that went BACKWARDS. Report
        # the reset itself and treat whatever the new history holds as unseen.
        STATE.log_run(
            "pipeline-telemetry-reset",
            level="warn",
            path=str(telemetry.path),
            cursor=seen_incidents,
            incident_total=telemetry.incident_total,
            recovery_total=telemetry.recovery_total,
            generation=generation,
            seen_generation=seen_generation,
        )
        hits.append(
            {
                "event": "pipeline-telemetry-reset",
                "level": "warn",
                "path": str(telemetry.path),
                "cursor": seen_incidents,
                "incident_total": telemetry.incident_total,
                "recovery_total": telemetry.recovery_total,
                "generation": generation,
                "seen_generation": seen_generation,
            }
        )
        seen_incidents = 0
        seen_recoveries = 0
    new_incidents = telemetry.incident_total - seen_incidents
    new_recoveries = telemetry.recovery_total - seen_recoveries
    if new_incidents > 0:
        # The open incident, or — if it has already been closed since the last scan — the closed
        # one the recovery record carries. Either way the failure is reported before its recovery,
        # so the steward never hears that something recovered without hearing what broke.
        incident = telemetry.incident or telemetry.recovery
        if incident:
            hits.append(_incident_hit(incident, new_incidents, telemetry.unhealthy))
    if new_recoveries > 0 and telemetry.recovery:
        hits.append(_recovery_hit(telemetry.recovery, new_recoveries))
    return hits, pending


def _retained_unhealthy_summary(unhealthy: tuple[dict, ...]) -> dict:
    """Classify the diagnostics that are still readable in the unhealthy ring.

    The writer bounds each tick's diagnostics, so this deliberately counts only retained
    diagnostic items. The per-tick ``*_count`` fields can prove that more items existed, but
    cannot safely assign an unseen item to a class or ref. A malformed item is ignored without
    preventing the remaining items from contributing to their classes.
    """
    degradation_counts: defaultdict[tuple[str, str], int] = defaultdict(int)
    degradation_refs: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
    error_counts: defaultdict[str, int] = defaultdict(int)
    error_refs: defaultdict[str, set[str]] = defaultdict(set)
    for tick in unhealthy:
        if not isinstance(tick, dict):
            continue
        items = tick.get("degradations")
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                step = item.get("step")
                action = item.get("action")
                if not isinstance(step, str) or not step or not isinstance(action, str) or not action:
                    continue
                group = (step, action)
                degradation_counts[group] += 1
                ref = item.get("ref")
                if isinstance(ref, str) and ref:
                    degradation_refs[group].add(ref)
        items = tick.get("errors")
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                code = item.get("code")
                if not isinstance(code, str) or not code:
                    continue
                error_counts[code] += 1
                ref = item.get("ref")
                if isinstance(ref, str) and ref:
                    error_refs[code].add(ref)

    return {
        "degradations": [
            {
                "step": step,
                "action": action,
                "count": degradation_counts[(step, action)],
                "refs": sorted(degradation_refs[(step, action)]),
            }
            for step, action in sorted(degradation_counts)
        ],
        "errors": [
            {"code": code, "count": error_counts[code], "refs": sorted(error_refs[code])}
            for code in sorted(error_counts)
        ],
    }


def _incident_hit(incident: dict, incidents: int, unhealthy: tuple[dict, ...]) -> dict:
    opened = incident.get("opened")
    return {
        # The tick that opened the incident, spread whole: status, step, reason, errors and
        # degradations are what an operator reads to tell a board outage from a product bug.
        **(opened if isinstance(opened, dict) else {}),
        "event": "pipeline-tick-unhealthy",
        "level": "warn",
        "ts": incident.get("opened_at", ""),
        "incident": incident.get("id", ""),
        "opened_seq": incident.get("opened_seq"),
        "unhealthy_ticks": incident.get("unhealthy_ticks"),
        "cause": production_telemetry.describe_incident(incident),
        # More than one incident opened between two scans: the newest one is described here, and
        # the count says the steward is not looking at all of them.
        "incidents": incidents,
        # Unlike the opening tick above, this preserves the full currently retained picture:
        # distinct failures later in the ring cannot be hidden by a newer incident's cause.
        "retained_window": _retained_unhealthy_summary(unhealthy),
    }


def _recovery_hit(recovery: dict, recoveries: int) -> dict:
    return {
        "event": "pipeline-tick-recovered",
        "level": "info",
        "ts": recovery.get("recovered_at", ""),
        "incident": recovery.get("id", ""),
        "opened_at": recovery.get("opened_at", ""),
        "opened_seq": recovery.get("opened_seq"),
        "unhealthy_ticks": recovery.get("unhealthy_ticks"),
        "recovered_seq": recovery.get("recovered_seq"),
        "recovered_at": recovery.get("recovered_at", ""),
        "recovered_status": recovery.get("recovered_status", ""),
        # What the incident was, on the recovery hit itself: the steward reads one record and
        # knows both what broke and that it is over.
        "cause": production_telemetry.describe_incident(recovery),
        "recoveries": recoveries,
    }


def ensure_pipeline_baseline(batch: dict) -> None:
    """Persist the pipeline counter baseline a scan just took, before anything else can consume it.

    The baseline only ever reached the watermark through `advance`, which runs after a steward head
    was dispatched — and a normal hour has no signal at all, so precheck exits PRECHECK_SKIP and
    writes nothing. The next unhealthy tick would then meet an unset counter, be read as a first-ever
    scan and be suppressed again, forever.

    So the baseline is written the moment it is taken, by both entry points that take one. Only the
    pipeline fields are touched and only while still unset; the per-kind dedup state stays on the
    two-phase scan/advance contract. A contended write is skipped rather than waited on.
    """
    baseline = batch["pending"]["pipeline_incident_total"]
    if baseline is None:
        return  # unreadable telemetry: nothing was measured, so there is no baseline to keep
    try:
        with STATE.lock():
            stored = STATE.load_watermark()
            if stored.get("pipeline_incident_total") is not None:
                return
            stored["pipeline_incident_total"] = baseline
            # Incidents and recoveries are one dedup state, taken from one read: a baseline that
            # kept only half of it would replay the recovery of an incident it never reported.
            stored["pipeline_recovery_total"] = batch["pending"].get("pipeline_recovery_total", 0)
            # The counters only mean anything next to the history they were taken from, so the
            # generation is written with them and never after them.
            stored["pipeline_telemetry_generation"] = batch["pending"].get(
                "pipeline_telemetry_generation", ""
            )
            STATE.save_watermark(stored)
    except SystemExit:
        return


def _blocked_signals(mark: dict, reader: StewardSignalReader | None = None) -> tuple[list[str], list[str]]:
    """(new Blocked refs since the watermark, every ref currently Blocked)."""
    # A steward report card may intentionally end in Blocked when the run found items that need
    # a human. That report is an accounting artifact, not a fresh anomaly for the next hourly
    # sweep. Stale still catches report cards left in In progress after a dead head.
    blocked = [
        c["reference"]
        for c in resolve_reader(reader).active_cards(states={"blocked"})
        if c["steward_report"] != "1"
    ]
    seen = set(mark["notified_blocked"])
    new = [r for r in blocked if r not in seen]
    return new, blocked


def _stale_signals(mark: dict, reader: StewardSignalReader | None = None) -> tuple[list[dict], dict]:
    """Cards past STALE_HOURS in their current column, excluding ones already notified at their current
    date_moved — a card that moves again re-arms the check; one that just sits still, already flagged
    once, does not re-fire every hour. (new stale hits, {ref: date_moved} for every card ACTUALLY
    reported stale this scan or a previous one at its still-current date_moved — the next watermark).

    notified_stale must only ever hold refs that crossed the threshold, never a fresh card: a scan
    runs on ANY signal, so a not-yet-stale card's date_moved written here would match the next scan's
    dedup check and immunize it permanently, since date_moved never changes while it sits still.
    """
    now = time.time()
    threshold = STALE_HOURS * 3600
    notified = mark["notified_stale"]
    hits = []
    next_notified = {}
    cards = resolve_reader(reader).active_cards(
        states={state for state, column in _STATE_COLUMNS.items() if column in STALE_COLUMNS}
    )
    # Keep the historical JSON/markdown order: stale columns in their documented
    # order, and cards inside each column in the reader's stable board order.
    for column in STALE_COLUMNS:
        for card in cards:
            if card["column"] != column:
                continue
            moved = card["date_moved"]
            if not moved:
                continue
            ref = card["reference"]
            if now - moved < threshold:
                continue  # not stale yet — never recorded, so it can still fire once it is
            if notified.get(ref) == moved:
                next_notified[ref] = moved  # already reported at this exact dwell — stay silent
                continue
            hits.append({"reference": ref, "column": column, "since": moved})
            next_notified[ref] = moved  # just reported — suppress until it moves again
    return hits, next_notified


def _resource_signals(mark: dict) -> tuple[dict, dict]:
    """(resources whose status differs from the watermark — a red->green recovery counts the same as a
    fresh red — current status map for every resource). A resource with no prior entry is a new
    baseline, not a flip, so a cold-start scan does not "flip" every currently-green resource.

    Reads the resource_health.json cache the PRODUCTION dispatcher writes next to its own state
    instead of running a fresh probe: refresh() executes real probes that cost tokens on a TTL, and a
    second call from the steward's worktree would describe a probe the dispatcher never saw. The file
    is the one the running dispatcher writes — the pipeline worktree holds a same-named cache only
    the legacy agent path fills.

    A missing, unreadable or malformed cache keeps the PREVIOUS baseline rather than resetting to {},
    which would silently erase whatever flip happened on the very next real read.
    """
    current = production_telemetry.read_resource_status()
    if current is None:
        current = dict(mark["resource_status"])
    prev = mark["resource_status"]
    changed = {r: s for r, s in current.items() if r in prev and prev[r] != s}
    return changed, current


def _active_card_prefixes(project: str, reader: StewardSignalReader | None = None) -> set[str]:
    """Workspace-name prefixes for every active card of `project`, including Blocked.

    Git-managed card workspaces are named from the full board reference
    (`<project>-<id>-<slug>`), not the numeric-only Orca-era id. A review workspace keeps the
    same reference behind `review-`. The board remains the source of truth: Blocked cards keep
    their workspaces for human inspection and therefore must still suppress an orphan signal.
    """
    prefixes: set[str] = set()
    for card in resolve_reader(reader).active_cards(project=project):
        reference = str(card["reference"])
        prefixes.add(f"{reference}-")
        prefixes.add(f"review-{reference}-")
    return prefixes


def _orphan_signals(mark: dict, reader: StewardSignalReader | None = None) -> tuple[list[str], list[str]]:
    """(new orphan workspace paths, every orphan path found this scan).

    The production namespace is `<data_dir>/workspaces/<project>/<worker>`; unlike the legacy Orca
    root it is dispatcher-owned, so every child worktree of a project must belong to an active card.
    `workspaces/observers` is a separate sprint-observer namespace and is deliberately excluded.
    """
    root = _workspaces_root()
    if not root.is_dir():
        return [], []
    orphans = []
    for project_dir in sorted(root.iterdir()):
        if not project_dir.is_dir() or project_dir.name == "observers":
            continue
        prefixes = _active_card_prefixes(project_dir.name, reader)
        for ws in sorted(project_dir.iterdir()):
            if ws.is_dir() and not any(ws.name.startswith(prefix) for prefix in prefixes):
                orphans.append(str(ws))
    notified = set(mark["notified_orphans"])
    new = [o for o in orphans if o not in notified]
    return new, orphans

def scan(reader: StewardSignalReader | None = None) -> dict:
    """Everything precheck/the skill need: signals since the watermark, plus the raw state to
    fold into the watermark on advance(). Read-only — never touches the watermark file itself."""
    resolved_reader = resolve_reader(reader)
    mark = load_watermark()
    tick_hits, tick_pending = _pipeline_tick_signals(mark)
    new_blocked, all_blocked = _blocked_signals(mark, resolved_reader)
    stale_hits, stale_current = _stale_signals(mark, resolved_reader)
    changed_resources, resource_current = _resource_signals(mark)
    new_orphans, all_orphans = _orphan_signals(mark, resolved_reader)
    return {
        "signals": {
            "pipeline_ticks": tick_hits,
            "new_blocked": new_blocked,
            "stale": stale_hits,
            "resource_flip": changed_resources,
            "new_orphan_workspaces": new_orphans,
        },
        "pending": {
            **tick_pending,
            "notified_blocked": all_blocked,
            "notified_stale": stale_current,
            "notified_orphans": all_orphans,
            "resource_status": resource_current,
        },
    }


def has_signal(batch: dict) -> bool:
    s = batch["signals"]
    return bool(
        s["pipeline_ticks"]
        or s["new_blocked"]
        or s["stale"]
        or s["resource_flip"]
        or s["new_orphan_workspaces"]
    )


def render_markdown(batch: dict) -> str:
    s = batch["signals"]
    if not has_signal(batch):
        return "steward: no signals since the previous watermark.\n"
    lines = ["# steward: anomaly signals", ""]
    if s["new_blocked"]:
        lines.append(f"## New Blocked ({len(s['new_blocked'])})")
        lines += [f"- {ref}" for ref in s["new_blocked"]]
        lines.append("")
    if s["pipeline_ticks"]:
        lines.append(f"## Tick incidents of the production dispatcher ({len(s['pipeline_ticks'])})")
        lines += [
            f"- {rec.get('ts', '?')} [{rec.get('event', '?')}] {json.dumps(rec, ensure_ascii=False)}"
            for rec in s["pipeline_ticks"]
        ]
        lines.append("")
    if s["stale"]:
        lines.append(f"## Stuck in a column longer than {STALE_HOURS:g}h ({len(s['stale'])})")
        for hit in s["stale"]:
            since = datetime.fromtimestamp(hit["since"], tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
            lines.append(f"- {hit['reference']} in {hit['column']!r} since {since}")
        lines.append("")
    if s["resource_flip"]:
        lines.append(f"## Resource health flip ({len(s['resource_flip'])})")
        lines += [f"- {r}: -> {status}" for r, status in s["resource_flip"].items()]
        lines.append("")
    if s["new_orphan_workspaces"]:
        lines.append(f"## Workspace with no card in flight ({len(s['new_orphan_workspaces'])})")
        lines += [f"- {p}" for p in s["new_orphan_workspaces"]]
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"
