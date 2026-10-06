"""curator agent — deterministic helpers the curator skill drives via Bash.

Flow the agent follows each run:
  1. `python3 -P -m ummanu automations curator harvest [--project <canonical-id|unknown|review:po>]`  -> redacted batch (markdown) of new
                                     Claude/Hermes/Codex session turns and changed
                                     personal-memory files on stdout, and a fact-bearing,
                                     identity-bound pending batch cached on disk. Cursor-only
                                     scans advance before the command returns.
  2. agent extracts durable facts, dedups via memory_search, writes each accepted fact
     through `python3 -P -m ummanu automations curator memory-write`.
  3. `python3 -P -m ummanu automations curator advance [--project <canonical-id|unknown|review:po>]`  -> moves the watermark past step 1.

An operator can settle an already-reviewed project backlog without running the curator:
`backlog --project ID --json` emits a metadata-only cutoff identity, and `baseline` accepts that
identity or the `batch_id` of the one matching pending batch.  Baseline writes no facts or source text.

Two-phase so a crash before the memory commit re-harvests instead of dropping turns.
`harvest --json` emits the structured batch; `backlog [--project <canonical-id|unknown|review:po>] [--json]`
reports metadata only without changing state; `sessions` lists discovered sources;
`status` shows the watermark; `rebind [--dry-run]` carries a pending record and the transcript
cursors across a known role-workspace move (OPERATIONS "role route"); `precheck` exits PRECHECK_SKIP (100) when there is nothing new, so
the systemd gate can skip the run without spinning up a head.
"""

from __future__ import annotations

import fcntl
import json
import re
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from ummanu.runtime.paths import MissingDefaultInstance, add_instance_argument, resolve_instance_argument
from ummanu.runtime.redact import looks_like_credential, scrub_secrets
from ummanu.runtime.state import (
    PRECHECK_DEFERRED,
    PRECHECK_SKIP,
    AgentState,
    append_line_durable,
    publish_state_atomic,
)

from . import discover, harvest, rebind
from .memory_protocol import (
    MemoryProtocolError,
    MemoryWriteRequest,
    write_fact,
)

STATE = AgentState("curator")
BASELINE_AUDIT_VERSION = 1
_BASELINE_ID = re.compile(r"[0-9a-f]{64}\Z")
_BASELINE_ACTOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}\Z")


class SettlementDeferred(RuntimeError):
    """Another process owns the short curator cursor-settlement transaction."""


@contextmanager
def cursor_settlement_transaction(*, nonblocking: bool = False):
    """Serialize curator watermark/pending read-validate-write without owning a run lifecycle.

    flock ownership is released by Linux when the owning process exits, including a killed curator
    head.  The file is intentionally persistent: it names the transaction boundary, not its owner.
    """
    STATE.ensure_dir()
    lock_path = STATE.dir / "cursor-settlement.lock"
    handle = lock_path.open("a+", encoding="utf-8")
    locked = False
    try:
        flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
        try:
            fcntl.flock(handle.fileno(), flags)
        except BlockingIOError as exc:
            raise SettlementDeferred from exc
        locked = True
        yield
    finally:
        if locked:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _write_pending(record: dict) -> None:
    """Atomically publish the fact-bearing batch that a later advance consumes."""
    harvest.write_pending(STATE, record)


def _baseline_audit_file() -> Path:
    return STATE.dir / "baseline-audit.ndjson"


def _read_baseline_audit() -> str:
    """Return a validated existing journal without exposing it to command output."""
    path = _baseline_audit_file()
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise harvest.PendingError("curator baseline audit is unreadable") from exc
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise harvest.PendingError("curator baseline audit is malformed") from exc
        if not isinstance(record, dict) or record.get("version") != BASELINE_AUDIT_VERSION:
            raise harvest.PendingError("curator baseline audit is malformed")
    return text


def _baseline_inputs(
    project: str | None,
    actor: str,
    reason: str,
    *,
    cutoff_id: str | None,
    batch_id: str | None,
) -> tuple[str, str, str, str, str]:
    project = harvest.validate_project(project)
    if project is None:
        raise harvest.PendingError("curator baseline requires one project, unknown, or review:po selector")
    if not isinstance(actor, str) or not _BASELINE_ACTOR.fullmatch(actor) or looks_like_credential(actor):
        raise harvest.PendingError("curator baseline actor is malformed")
    if (
        not isinstance(reason, str)
        or not reason.strip()
        or len(reason) > 280
        or any(ord(char) < 32 for char in reason)
    ):
        raise harvest.PendingError("curator baseline reason is malformed")
    if (cutoff_id is None) == (batch_id is None):
        raise harvest.PendingError("curator baseline requires exactly one evidence identity")
    evidence_kind, evidence_id = ("cutoff", cutoff_id) if cutoff_id is not None else ("batch", batch_id)
    if not isinstance(evidence_id, str) or not _BASELINE_ID.fullmatch(evidence_id):
        raise harvest.PendingError("curator baseline evidence identity is malformed")
    return project, actor, scrub_secrets(reason.strip()), evidence_kind, evidence_id


def _baseline_cursor_ids(pending: dict) -> list[str]:
    """Stable cursor identities for audit, without retaining paths or cursor values."""
    import hashlib

    return [
        hashlib.sha256(
            json.dumps({"source": source, "cursor": cursor}, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        for source, cursor in sorted(pending.items())
    ]


def baseline_settlement(
    *,
    project: str | None,
    actor: str,
    reason: str,
    cutoff_id: str | None = None,
    batch_id: str | None = None,
) -> dict:
    """Settle one operator-approved project cutoff or pending batch under the cursor lock.

    This API never harvests, invokes a curator head, or writes memory facts.  Callers must
    present exactly one opaque identity produced by `backlog --json` (cutoff) or `harvest --json`
    (batch).  The returned record is the redacted audit event that was durably published.
    """
    project, actor, reason, evidence_kind, evidence_id = _baseline_inputs(
        project, actor, reason, cutoff_id=cutoff_id, batch_id=batch_id
    )
    with cursor_settlement_transaction():
        if evidence_kind == "batch":
            plan = harvest.baseline_pending(STATE, harvest.current_identity(), project, evidence_id)
            remove_pending = True
        else:
            if STATE.pending_file.exists():
                # Validate, rather than skipping over, a stale, foreign, or malformed pending
                # record.  A cutoff must never leapfrog replayable curator input.
                harvest.read_pending(STATE, harvest.current_identity(), project)
                raise harvest.PendingError("curator baseline requires the existing pending batch evidence")
            cutoff = harvest.baseline_cutoff(STATE, project)
            if not cutoff["pending"] or cutoff["cutoff_id"] != evidence_id:
                raise harvest.PendingError("curator baseline cutoff is stale or has no cursors")
            plan = {
                "project": project,
                "base": cutoff["base"],
                "pending": cutoff["pending"],
                "cutoff_id": cutoff["cutoff_id"],
            }
            remove_pending = False

        audit = {
            "version": BASELINE_AUDIT_VERSION,
            "event": "curator_baseline",
            "time": datetime.now(UTC).isoformat(),
            "project": project,
            "actor": actor,
            "reason": reason,
            "evidence": {"kind": evidence_kind, "id": evidence_id},
            "affected_cursor_count": len(plan["pending"]),
            "affected_cursor_ids": _baseline_cursor_ids(plan["pending"]),
            "outcome": "settled",
        }
        audit_text = _read_baseline_audit()
        if audit_text and not audit_text.endswith("\n"):
            audit_text += "\n"
        audit_text += json.dumps(audit, ensure_ascii=False, sort_keys=True) + "\n"

        mark = harvest._baseline_watermark(STATE)
        # The batch path already compared every starting cursor.  This shared guard keeps the
        # cutoff path fail-closed if state changed between proof construction and publication.
        if any(mark.get(source) != expected for source, expected in plan["base"].items()):
            raise harvest.PendingError("curator baseline cursor state is stale")
        next_mark = {**mark, **plan["pending"]}
        writes = [
            (STATE.watermark_file, json.dumps(next_mark, indent=2, ensure_ascii=False)),
            (_baseline_audit_file(), audit_text),
        ]
        publish_state_atomic(writes, removes=[STATE.pending_file] if remove_pending else [])
    return audit


def cmd_baseline(
    *,
    project: str | None,
    actor: str,
    reason: str,
    cutoff_id: str | None = None,
    batch_id: str | None = None,
) -> int:
    try:
        audit = baseline_settlement(
            project=project,
            actor=actor,
            reason=reason,
            cutoff_id=cutoff_id,
            batch_id=batch_id,
        )
    except (harvest.PendingError, OSError, RuntimeError):
        # Inputs can contain source paths, transcript snippets, and credentials.  This command
        # intentionally reports only the refusal, leaving precise evidence in no output channel.
        print("curator: baseline refused; cursor state unchanged", file=sys.stderr)
        return 1
    print(f"curator: baseline settled {audit['project']} ({audit['affected_cursor_count']} cursor(s))")
    return 0


def _prepare_batch(*, nonblocking: bool = False, project: str | None = None) -> dict:
    """Replay fact work or atomically settle a fresh bounded scan in one transaction.

    Every complete source record the scan classified leaves this function either in an
    identity-bound fact batch or in the watermark.  A cursor-only pending record is not
    a supported batch because no curator step can consume it, so it fails closed.
    """
    with cursor_settlement_transaction(nonblocking=nonblocking):
        project = harvest.validate_project(project)
        identity = harvest.current_identity()
        if STATE.pending_file.is_file():
            record = harvest.read_pending(STATE, identity, project)
            # A stale record must win over new discovery, but cannot be replayed or
            # advanced as though it belonged to the current watermark.
            for source, expected in record["base"].items():
                if STATE.load_watermark().get(source) != expected:
                    raise harvest.PendingError(f"curator pending record is stale for {source}")
            return {**record["batch"], "batch_id": record["batch_id"]}

        batch = harvest.harvest(STATE, identity, project=project)
        if batch["sessions"] or batch["memory"]:
            base = {key: STATE.load_watermark().get(key) for key in batch["pending"]}
            _write_pending(harvest.pending_record(batch, identity, base, project))
        elif batch["pending"]:
            # These are complete non-emitting records.  Persist their precise scan
            # cursor now, not as a pending batch that the zero-input skill will exit
            # without advancing.
            base = {key: STATE.load_watermark().get(key) for key in batch["pending"]}
            harvest.advance(STATE, harvest.pending_record(batch, identity, base, project), identity, project)
        return batch


def cmd_harvest(as_json: bool, project: str | None = None) -> int:
    try:
        batch = _prepare_batch(project=project)
    except harvest.PendingError as exc:
        print(f"curator: {exc}", file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps(batch, ensure_ascii=False, indent=2))
    else:
        print(harvest.render_markdown(batch))
    return 0


def cmd_advance(project: str | None = None) -> int:
    try:
        with cursor_settlement_transaction():
            project = harvest.validate_project(project)
            if not STATE.pending_file.is_file():
                raise harvest.PendingError("nothing to advance (run harvest first)")
            identity = harvest.current_identity()
            pending = harvest.read_pending(STATE, identity, project)
            harvest.advance(STATE, pending, identity, project)
            STATE.pending_file.unlink()
    except harvest.PendingError as exc:
        print(f"curator: {exc}", file=sys.stderr)
        return 1
    STATE.log_run("advance")
    print(f"curator: watermark advanced for {len(pending['batch']['pending'])} source(s)")
    return 0


def cmd_precheck() -> int:
    """Return work, clean-skip, or an unclaimed settlement defer for the systemd gate."""
    try:
        batch = _prepare_batch(nonblocking=True)
    except SettlementDeferred:
        print("curator: cursor settlement is busy; tick deferred", file=sys.stderr)
        return PRECHECK_DEFERRED
    except harvest.PendingError as exc:
        print(f"curator: {exc}", file=sys.stderr)
        return 1
    if batch["sessions"] or batch["memory"]:
        STATE.log_run("precheck", result="change")
        return 0
    if batch.get("partial_sources"):
        STATE.log_run("precheck", result="source-local-partial")
        print("curator: source-local partial JSONL tail; safe cursors settled", file=sys.stderr)
        return PRECHECK_SKIP
    STATE.log_run("precheck", result="no-change")
    print("curator: no new turns since watermark", file=sys.stderr)
    return PRECHECK_SKIP


def cmd_rebind(dry_run: bool) -> int:
    """Rebind the pending identity and carry moved transcript cursors; audited, idempotent.

    `runs.jsonl` is append-only here: the `rebind` line is appended durably *before* the state
    changes and carries the digest of the state it is about to publish.  A failed publication is
    rolled back and followed by a best-effort `rebind-failed` line with the same digest.
    """
    def plan() -> rebind.RebindPlan:
        return rebind.plan_rebind(
            STATE, home=Path.home(), projects=discover.CLAUDE_PROJECTS, current=harvest.current_identity()
        )

    journal = STATE.dir / "runs.jsonl"
    try:
        if dry_run:
            result = plan()
        else:
            with cursor_settlement_transaction():
                result = plan()
                if result.changed:
                    digest = result.digest(STATE)
                    event = {
                        "ts": datetime.now(UTC).isoformat(),
                        "event": "rebind",
                        **result.counts(),
                        "from_workspace": result.old_workspace,
                        "to_workspace": result.new_workspace,
                        "state_digest": digest,
                    }
                    append_line_durable(journal, json.dumps(event, ensure_ascii=False))
                    writes = [
                        (path, text)
                        for path, text in (
                            (STATE.watermark_file, result.watermark_text),
                            (STATE.pending_file, result.pending_text),
                        )
                        if text is not None
                    ]
                    try:
                        publish_state_atomic(writes)
                    except OSError as exc:
                        failed = {
                            "ts": datetime.now(UTC).isoformat(),
                            "event": "rebind-failed",
                            "state_digest": digest,
                            "error": str(exc),
                        }
                        try:
                            append_line_durable(journal, json.dumps(failed, ensure_ascii=False))
                        except OSError:
                            pass
                        raise
    except rebind.RebindRefused as exc:
        print(f"curator: rebind refused ({exc.reason}): {exc}; state unchanged", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"curator: rebind failed: {exc}; state unchanged", file=sys.stderr)
        return 1
    for line in rebind.render(result, dry_run=dry_run):
        print(line)
    return 0


def cmd_sessions() -> int:
    for s in discover.all_sessions():
        print(f"{s['head']:8} {s['session_id']}  {s['cwd']}  {s['path']}")
    return 0


def cmd_backlog(as_json: bool, project: str | None = None) -> int:
    try:
        summary = harvest.backlog(STATE, project)
    except harvest.PendingError as exc:
        print(f"curator: {exc}", file=sys.stderr)
        return 1
    if as_json and project is not None and not STATE.pending_file.exists():
        try:
            cutoff = harvest.baseline_cutoff(STATE, project)
        except harvest.PendingError:
            # Backlog remains the released read-only diagnostic on legacy or partial state;
            # only the opt-in settlement proof is unavailable until that state is resolved.
            pass
        else:
            summary["cutoff"] = {"id": cutoff["cutoff_id"], "cursor_count": len(cutoff["pending"])}
    if as_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    if not summary["groups"]:
        print(f"curator: no unadvanced backlog for {summary['project']}")
        return 0
    print("project head sessions signal-turns memory-files oldest newest")
    for group in summary["groups"]:
        print(
            f"{group['project']} {group['head']} {group['session_count']} {group['signal_turn_count']} "
            f"{group['memory_file_count']} {group['oldest'] or '-'} {group['newest'] or '-'}"
        )
    return 0


def cmd_status() -> int:
    mark = STATE.load_watermark()
    print(f"watermark: {len(mark)} source(s) tracked; state={STATE.dir}")
    for src, v in mark.items():
        if "lines" in v:
            print(f"  {v['lines']:>6} lines  {Path(src).name}")
        elif "offset" in v:
            print(f"  {v['offset']:>6} bytes  {Path(src).name}")
        else:
            print(f"  {v.get('size', 0):>6} bytes  {Path(src).name}")
    return 0


def cmd_memory_write(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python3 -P -m ummanu automations curator memory-write")
    add_instance_argument(parser)
    parser.add_argument("--data-dir")
    parser.add_argument("--actor", required=True)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--file", required=True)
    parser.add_argument("--source")
    parser.add_argument("--tags", default="")
    parser.add_argument("--pinned", action="store_true")
    parser.add_argument("--supersedes", default="")
    parser.add_argument("--ummanu-repo")
    ns = parser.parse_args(argv)
    try:
        resolve_instance_argument(ns)
    except MissingDefaultInstance as exc:
        parser.error(str(exc))

    request = MemoryWriteRequest(
        instance=Path(ns.instance),
        actor=ns.actor,
        scope=ns.scope,
        slug=ns.slug,
        fact_file=Path(ns.file),
        source=ns.source,
        tags=ns.tags,
        pinned=ns.pinned,
        supersedes=ns.supersedes,
        data_dir=Path(ns.data_dir) if ns.data_dir else None,
        ummanu_repo=Path(ns.ummanu_repo) if ns.ummanu_repo else None,
    )
    try:
        result = write_fact(STATE, request)
    except MemoryProtocolError as exc:
        STATE.log_run(
            "memory_write",
            result="error",
            actor=ns.actor,
            scope=ns.scope,
            slug=ns.slug,
            error=exc.payload.get("error"),
            commit=exc.payload.get("commit"),
        )
        print(json.dumps(exc.payload, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 1
    STATE.log_run(
        "memory_write",
        result="ok",
        actor=ns.actor,
        scope=ns.scope,
        slug=ns.slug,
        commit=result.get("commit"),
        fact=result.get("fact"),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def main(argv=None) -> int:
    argv = list(argv or [])
    cmd = argv[0] if argv else "help"
    if cmd in {"harvest", "advance", "backlog"}:
        import argparse

        parser = argparse.ArgumentParser(prog=f"python3 -P -m ummanu automations curator {cmd}")
        parser.add_argument("--project")
        if cmd in {"harvest", "backlog"}:
            parser.add_argument("--json", action="store_true")
        ns = parser.parse_args(argv[1:])
        if cmd == "harvest":
            return cmd_harvest(ns.json, ns.project)
        if cmd == "advance":
            return cmd_advance(ns.project)
        return cmd_backlog(ns.json, ns.project)
    if cmd == "baseline":
        import argparse

        parser = argparse.ArgumentParser(prog="python3 -P -m ummanu automations curator baseline")
        parser.add_argument("--project", required=True)
        parser.add_argument("--actor", required=True)
        parser.add_argument("--reason", required=True)
        evidence = parser.add_mutually_exclusive_group(required=True)
        evidence.add_argument("--cutoff-id")
        evidence.add_argument("--batch-id")
        ns = parser.parse_args(argv[1:])
        return cmd_baseline(
            project=ns.project,
            actor=ns.actor,
            reason=ns.reason,
            cutoff_id=ns.cutoff_id,
            batch_id=ns.batch_id,
        )
    if cmd == "rebind":
        import argparse

        parser = argparse.ArgumentParser(prog="python3 -P -m ummanu automations curator rebind")
        parser.add_argument("--dry-run", action="store_true")
        ns = parser.parse_args(argv[1:])
        return cmd_rebind(ns.dry_run)
    if cmd == "precheck":
        return cmd_precheck()
    if cmd == "sessions":
        return cmd_sessions()
    if cmd == "status":
        return cmd_status()
    if cmd == "memory-write":
        return cmd_memory_write(argv[1:])
    print(__doc__)
    return 0 if cmd in ("help", "-h", "--help") else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
