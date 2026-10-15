"""Stale worker round: facts read from the round's checkout and one record per round.

While the dispatcher waits for a worker report it computes the round's facts every tick. The first
tick that finds the round stale writes one `Stale round:` card comment, keyed on the attempt and the
report generation. Nothing else changes: no move, stop, interrupt, charge or owner event.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ummanu.board.estimate import estimate_view
from ummanu.dispatch.head_vitality_episode import VitalityVerdict
from ummanu.dispatch.state import DispatcherRecord, attempt_request_id
from ummanu.dispatch.types import HostError
from ummanu.dispatch.worker_lifecycle import StaleRound
from ummanu.infra.env import positive_int
from ummanu.runtime.role_env import WORKSPACE_NAMESPACE

ESTIMATE_FACTOR = 2
QUIET_MINUTES_ENV = "UMMANU_STALE_ROUND_QUIET_MINUTES"
DEFAULT_QUIET_MINUTES = 30
RULE_ESTIMATE = "estimate"
RULE_QUIET = "quiet"
# Verdicts under which the pane is alive: the quiet rule asks why a working head changes nothing.
ALIVE_VERDICTS = frozenset(
    {VitalityVerdict.HEALTHY_ACTIVE, VitalityVerdict.HEALTHY_QUIET, VitalityVerdict.SUSPECTED_STALL}
)
# A linked worktree has a `.git` file, not a directory, at its root.
_SKIPPED_NAMES = frozenset({".git", WORKSPACE_NAMESPACE})


@dataclass(frozen=True)
class WorkspaceReading:
    """The checkout against its base, or the reason it could not be read (`unknown`)."""

    commits: int = 0
    newest_commit_at: float = 0.0
    files_changed: int = 0
    lines_added: int = 0
    lines_removed: int = 0
    newest_file_at: float = 0.0
    unknown: str = ""


@dataclass(frozen=True)
class RoundFacts:
    """One tick's facts about the current worker round; `unknown` names an unread workspace."""

    elapsed_seconds: float
    commits: int
    newest_commit_at: float
    files_changed: int
    lines_added: int
    lines_removed: int
    last_change_at: float
    quiet_seconds: float
    unknown: str


def quiet_seconds() -> int:
    return 60 * positive_int(QUIET_MINUTES_ENV, DEFAULT_QUIET_MINUTES)


def round_facts(record: DispatcherRecord, reading: WorkspaceReading, now: float) -> RoundFacts:
    """The round's facts from its record, its workspace reading and the clock.

    Quiet is measured from the newest change, never from before the round opened.
    """
    started_at = record.worker_stale_round.started_at
    last_change_at = max(reading.newest_commit_at, reading.newest_file_at)
    return RoundFacts(
        elapsed_seconds=max(0.0, now - started_at),
        commits=reading.commits,
        newest_commit_at=reading.newest_commit_at,
        files_changed=reading.files_changed,
        lines_added=reading.lines_added,
        lines_removed=reading.lines_removed,
        last_change_at=last_change_at,
        quiet_seconds=max(0.0, now - max(last_change_at, started_at)),
        unknown=reading.unknown,
    )


def stale_rule(
    facts: RoundFacts,
    *,
    estimate_minutes: int | None,
    verdict: VitalityVerdict | None,
    quiet_after: float,
) -> str:
    """The rule that makes this round stale, or "" while it is not."""
    if estimate_minutes is not None and facts.elapsed_seconds > ESTIMATE_FACTOR * estimate_minutes * 60:
        return RULE_ESTIMATE
    if not facts.unknown and verdict in ALIVE_VERDICTS and facts.quiet_seconds >= quiet_after:
        return RULE_QUIET
    return ""


def read_round_workspace(
    run: Callable[[list[str], str], subprocess.CompletedProcess[str]],
    workspace: str,
    base_ref: str,
) -> WorkspaceReading:
    """Commits, diff and newest change of `workspace` since its fork from `base_ref`.

    `run` raises HostError on a failed command; any failure is returned as `unknown`.
    """
    try:
        git = ["git", "-C", workspace]
        fork = run([*git, "merge-base", base_ref, "HEAD"], "stale round fork point").stdout.strip()
        commits = int(run([*git, "rev-list", "--count", f"{fork}..HEAD"], "stale round commits").stdout)
        newest = run([*git, "log", "-1", "--format=%ct", f"{fork}..HEAD"], "stale round newest commit")
        files = added = removed = 0
        for line in run([*git, "diff", "--numstat", fork], "stale round diff").stdout.splitlines():
            plus, minus, _path = line.split("\t", 2)
            files += 1
            added += int(plus) if plus.isdigit() else 0
            removed += int(minus) if minus.isdigit() else 0
        newest_file_at = _newest_mtime(workspace)
    except (HostError, OSError, ValueError) as exc:
        return WorkspaceReading(unknown=str(exc) or type(exc).__name__)
    return WorkspaceReading(
        commits=commits,
        newest_commit_at=float(newest.stdout.strip() or 0),
        files_changed=files,
        lines_added=added,
        lines_removed=removed,
        newest_file_at=newest_file_at,
    )


def _newest_mtime(workspace: str) -> float:
    def raise_error(exc: OSError) -> None:
        raise exc

    newest = 0.0
    for root, dirs, files in os.walk(workspace, onerror=raise_error):
        if root == workspace:
            dirs[:] = [name for name in dirs if name not in _SKIPPED_NAMES]
            files = [name for name in files if name not in _SKIPPED_NAMES]
        for name in files:
            newest = max(newest, os.lstat(os.path.join(root, name)).st_mtime)
    return newest


def record_stale_round(
    runtime: Any,
    task: Mapping[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    verdict: VitalityVerdict | None,
    *,
    now: float,
) -> None:
    """Write the round's one `Stale round:` comment on its first stale tick.

    The body is frozen on the record before the comment, so a replay sends the same bytes.
    """
    ref = str(task["ref"])
    marker = record.worker_stale_round
    if marker.generation != record.report_generation:
        marker = StaleRound(
            generation=record.report_generation, started_at=float(record.worker_started_at or now)
        )
        record.worker_stale_round = marker
        records[ref] = record
        runtime.save_records(payload, records)
    if marker.recorded:
        return
    if not marker.body:
        estimate = estimate_view(task)
        facts = round_facts(record, runtime.host.round_workspace(task, record), now)
        rule = stale_rule(
            facts,
            estimate_minutes=int(estimate["minutes"]) if estimate else None,
            verdict=verdict,
            quiet_after=quiet_seconds(),
        )
        if not rule:
            return
        marker.body = _stale_body(rule, facts, estimate, generation=record.report_generation, now=now)
        records[ref] = record
        runtime.save_records(payload, records)
    runtime.writer.comment(
        role="dispatcher",
        actor=runtime.owner,
        reference=ref,
        body=marker.body,
        request_id=attempt_request_id(
            record.attempt_id or "", "stale-round", ref, suffix=str(record.report_generation)
        ),
    )
    marker.recorded = True
    records[ref] = record
    runtime.save_records(payload, records)


def _stale_body(
    rule: str, facts: RoundFacts, estimate: Mapping[str, Any] | None, *, generation: int, now: float
) -> str:
    rule_text = (
        f"elapsed > {ESTIMATE_FACTOR}x estimate"
        if rule == RULE_ESTIMATE
        else f"no commit or file change for {round(facts.quiet_seconds / 60)} min while the head is alive"
    )
    estimate_text = f"{estimate['minutes']} min ({estimate['source']})" if estimate else "none"
    if facts.unknown:
        workspace_text = f"Workspace unknown: {facts.unknown}."
    else:
        workspace_text = (
            f"Commits since base: {facts.commits}"
            + (f", newest {_utc(facts.newest_commit_at)}" if facts.commits else "")
            + f". Diff against base: {facts.files_changed} files, +{facts.lines_added}/-{facts.lines_removed} lines."
            + f" Last change: {_utc(facts.last_change_at) if facts.last_change_at else 'none'}"
            + f", {round((now - facts.last_change_at) / 60) if facts.last_change_at else '-'} min ago."
        )
    return (
        f"Stale round: rule {rule} ({rule_text}), report generation {generation}."
        f" Elapsed {round(facts.elapsed_seconds / 60)} min; estimate {estimate_text}."
        f" {workspace_text} Recorded only: nothing was moved, stopped, interrupted or charged."
    )


def _utc(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))
