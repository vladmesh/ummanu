"""Synthetic layers for the web preview: a busy installation that exists nowhere.

Every project, card, sprint, head and path is invented. The clock is fixed at `NOW`, so two runs
render the same pages. The layers answer the read operations `ummanu.web.app.WebApp` calls and
refuse every mutation; documents follow the shapes the real read layers publish, and the DoD and
board columns come from the product's own projections (`sprint_dod.section`, `board_document`).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ummanu.board import sprint_dod
from ummanu.board.audit_contract import PROTOCOL_EVENT_RECORD_TYPE
from ummanu.board.task_protocol.rows import RELEASE_MERGE_KEY
from ummanu.webproto import board_view, sources
from ummanu.webproto.errors import HeadRunNotFound, RuntimeUnavailable, TaskNotFound
from ummanu.webproto.reads import health_summary
from ummanu.webproto.section import Reading

NOW_DT = datetime(2026, 11, 20, 9, 30, tzinfo=UTC)
NOW = NOW_DT.timestamp()

#: What every refused mutation says.
READ_ONLY = "this is the web preview: its data is synthetic and it changes nothing, so every write is refused"

SOURCE = sources.available(NOW).to_json()

PROJECTS = ("acme-api", "nimbus-cli", "orbit-web")

OPEN_SPRINTS = ("sprint:41", "sprint:42")
SPRINT_PROJECT = {
    "sprint:38": "acme-api",
    "sprint:39": "orbit-web",
    "sprint:40": "nimbus-cli",
    "sprint:41": "acme-api",
    "sprint:42": "orbit-web",
}

#: The card the owner's screenshot is modelled on: every tab and panel of the card page is full.
SHOWCASE = "acme-api-231"


def at(minutes: float, *, micro: int = 0) -> str:
    """The moment `minutes` before `NOW`, in the journal's spelling (microseconds when given)."""
    moment = NOW_DT - timedelta(minutes=minutes)
    if micro:
        return moment.replace(microsecond=micro).isoformat().replace("+00:00", "Z")
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ahead(minutes: float) -> str:
    return (NOW_DT + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def section(name: str, **fields: Any) -> dict[str, Any]:
    """A rendered read section: its named source, then its fields."""
    return {"source": {**SOURCE, "name": name}, **fields}


# -- heads -----------------------------------------------------------------------------------------

HEADS = {
    "codex-high": {"profile": "codex-high", "model": "gpt-6-sol", "effort": "high", "adapter": "codex"},
    "codex-xhigh": {"profile": "codex-xhigh", "model": "gpt-6.1-sol", "effort": "xhigh", "adapter": "codex"},
    "opus-max": {"profile": "opus-max", "model": "claude-opus-5-5", "effort": "max", "adapter": "claude"},
    "sonnet-medium": {"profile": "sonnet-medium", "model": "claude-sonnet-5-5", "effort": "medium", "adapter": "claude"},
    "observer-opus": {"profile": "observer-opus", "model": "claude-opus-5-5", "effort": "high", "adapter": "claude"},
}


# -- cards -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Card:
    ref: str
    project: str
    state: str
    title: str
    minutes: float
    sprint: str | None = None
    type: str = "code"
    complexity: str = "standard"
    head: str | None = None
    review_head: str | None = None
    blocked: str | None = None
    owner: str | None = None
    ci: str = ""
    skip: str | None = None
    dod_item: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


CARDS: tuple[Card, ...] = (
    # Issues
    Card("acme-api-240", "acme-api", "issues", "Rate-limit the public search endpoint per API key", 1900, "sprint:41", dod_item="d8"),
    Card("orbit-web-118", "orbit-web", "issues", "Dark mode for the settings pages", 4200, complexity="small"),
    Card("nimbus-cli-77", "nimbus-cli", "issues", "Decide whether `nimbus sync --watch` should follow symlinked directories by default on Linux", 7300, type="decision", complexity="small"),
    # Ready
    Card("acme-api-236", "acme-api", "ready", "Backfill missing invoice currency codes for accounts created before the multi-currency launch", 340, "sprint:41", dod_item="d8", skip="the project acme-api is held by another attempt"),
    Card("orbit-web-121", "orbit-web", "ready", "Replace the date picker with the accessible combobox pattern", 95, "sprint:42"),
    Card("orbit-web-122", "orbit-web", "ready", "Investigate first-load bundle size regression", 50, "sprint:42", type="research", complexity="small"),
    Card("nimbus-cli-80", "nimbus-cli", "ready", "Ship shell completions for zsh and fish", 22, complexity="small"),
    # In progress
    Card("acme-api-233", "acme-api", "in_progress", "Idempotency keys for POST /v2/payments", 48, "sprint:41", head="codex-xhigh", dod_item="d2"),
    Card("orbit-web-119", "orbit-web", "in_progress", "Optimistic updates for the kanban drag and drop, with rollback when the server refuses a move", 131, "sprint:42", head="opus-max"),
    Card("nimbus-cli-79", "nimbus-cli", "in_progress", "Retry uploads on 429 with jittered backoff", 12, head="codex-high", complexity="small"),
    # Validate
    Card("acme-api-234", "acme-api", "validate", "Tighten JSON schema of the webhook delivery log", 6, "sprint:41", head="sonnet-medium", review_head="opus-max", dod_item="d2"),
    # CI
    Card(SHOWCASE, "acme-api", "ci", "Stream large export payloads instead of buffering the whole report in memory before upload", 9, "sprint:41", head="codex-xhigh", review_head="opus-max", ci="pending", dod_item="d5"),
    Card("orbit-web-117", "orbit-web", "ci", "Move feature flags to the edge config", 31, "sprint:42", head="codex-high", ci="waiting"),
    # Review
    Card("acme-api-229", "acme-api", "review", "Drop the legacy XML export route and its feature flag", 74, "sprint:41", head="codex-high", review_head="opus-max", ci="green", dod_item="d5"),
    Card("nimbus-cli-76", "nimbus-cli", "review", "Config file discovery walks up to the repository root", 205, head="sonnet-medium", review_head="codex-xhigh", ci="green"),
    # Assessment: none.
    # Post-merge CI
    Card("orbit-web-115", "orbit-web", "post_merge_ci", "Lazy-load the analytics dashboard charts", 17, "sprint:42", head="opus-max", ci="green"),
    # Blocked
    Card("acme-api-226", "acme-api", "blocked", "Migrate the ledger tables to partitioned storage", 1440, "sprint:41", head="codex-xhigh", dod_item="d7",
         blocked="The migration needs a maintenance window the owner has to approve; staging rehearsal took 47 minutes."),
    Card("orbit-web-120", "orbit-web", "blocked", "Pick the payment provider for the EU storefront", 380, "sprint:42", type="decision", complexity="small",
         blocked="Waiting for the owner: two providers fit, the choice is commercial.", owner="Choose between Provider A (lower fees) and Provider B (better SCA flow); the PO cannot decide pricing."),
    Card("nimbus-cli-74", "nimbus-cli", "blocked", "Sign release binaries for macOS notarization", 2880, head="codex-high",
         blocked="The signing certificate in the secret store expired; a new one has to be issued."),
    # Done
    Card("acme-api-225", "acme-api", "done", "Request ids in every structured log line", 300, "sprint:41", head="codex-high", review_head="opus-max", ci="green", dod_item="d1"),
    Card("acme-api-227", "acme-api", "done", "Health endpoint reports database pool saturation", 520, "sprint:41", head="sonnet-medium", review_head="opus-max", ci="green", dod_item="d1"),
    Card("acme-api-228", "acme-api", "done", "Pagination cursors are opaque and signed", 880, "sprint:41", head="codex-xhigh", review_head="opus-max", ci="green", dod_item="d4"),
    Card("acme-api-230", "acme-api", "done", "Document the export API limits", 160, "sprint:41", type="operation", head="sonnet-medium", dod_item="d3"),
    Card("orbit-web-110", "orbit-web", "done", "Remove the unused carousel component", 2100, "sprint:42", head="codex-high", review_head="opus-max", ci="green"),
    Card("orbit-web-111", "orbit-web", "done", "Keyboard shortcuts overlay", 1700, "sprint:42", head="opus-max", review_head="codex-xhigh", ci="green"),
    Card("orbit-web-112", "orbit-web", "done", "Audit colour contrast on the marketing pages", 1300, "sprint:42", type="research", head="sonnet-medium"),
    Card("orbit-web-113", "orbit-web", "done", "Translate the onboarding checklist into German and French", 960, "sprint:42", head="codex-high", review_head="opus-max", ci="green"),
    Card("orbit-web-114", "orbit-web", "done", "Cache-busting for service worker assets", 610, "sprint:42", head="codex-xhigh", review_head="opus-max", ci="green"),
    Card("orbit-web-116", "orbit-web", "done", "Fix layout shift on the pricing table", 45, "sprint:42", head="codex-high", review_head="opus-max", ci="green"),
    Card("nimbus-cli-75", "nimbus-cli", "done", "`nimbus login` stores the token in the OS keychain", 1500, head="codex-high", review_head="opus-max", ci="green"),
    Card("nimbus-cli-78", "nimbus-cli", "done", "Wait for the nightly e2e run on the release branch", 400, type="wait", complexity="small"),
    Card("nimbus-cli-81", "nimbus-cli", "done", "Progress bar for multi-gigabyte uploads", 90, head="opus-max", review_head="codex-xhigh", ci="green"),
)

BY_REF = {card.ref: card for card in CARDS}

#: The order a card walks its columns in; a blocked card left `in_progress`.
WALK = ("issues", "ready", "in_progress", "validate", "ci", "review", "post_merge_ci", "done")


def _walk(card: Card) -> list[str]:
    if card.state == "blocked":
        return ["issues", "ready", "in_progress", "blocked"]
    if card.type != "code" and card.state == "done":
        return ["issues", "ready", "in_progress", "done"]
    return list(WALK[: WALK.index(card.state) + 1])


def _created_minutes(card: Card) -> float:
    return card.minutes + 60 * len(_walk(card)) + 37


# -- dispatcher-side facts per card ----------------------------------------------------------------

def pipeline(card: Card) -> dict[str, Any]:
    """The stage chip the board and the card page draw, as `pipeline_summary` words it."""
    titles = dict((state, title) for state, title in board_view.COLUMNS)
    label, tone = titles.get(card.state, "Stage unknown"), ""
    ci = {"status": "unknown", "sha": None, "reason": None}
    previous = None
    if card.type != "code":
        ci["status"] = "not_applicable"
    elif card.state == "ci":
        ci["status"] = card.ci or "waiting"
        label, tone = ("CI pending" if card.ci == "pending" else "CI waiting"), "warn"
    elif card.state in {"review", "post_merge_ci", "done"}:
        ci.update(status="green", sha=_sha(card.ref, 0))
        if card.state == "review":
            label, tone = "Review; CI passed", "accent"
    elif card.state == "in_progress" and card.ref == "acme-api-233":
        previous = {
            "status": "red",
            "sha": _sha(card.ref, 1),
            "failure_class": "substantive",
            "reason": "tests/test_payments.py::test_replay_returns_first_response failed",
            "evidence": [],
        }
        ci["reason"] = "The worker is preparing a new report"
    return {
        "stage": card.state,
        "label": label,
        "tone": tone,
        "ci": ci,
        "previous": previous,
        "source": SOURCE,
    }


def _sha(ref: str, salt: int) -> str:
    """A stable 40-character hex commit id derived from the card ref."""
    seed = sum((index + 7) * ord(char) for index, char in enumerate(f"{ref}:{salt}"))
    digits = ""
    value = seed
    while len(digits) < 40:
        value = (value * 1103515245 + 12345) % (2**31)
        digits += f"{value:08x}"
    return digits[:40]


# -- the board -------------------------------------------------------------------------------------


class _BoardClient:
    """The one statement `board_view.board_document` issues, answered from `CARDS`."""

    def board_cards(self, **narrowed: Any) -> list[dict[str, Any]]:
        project, sprint, no_sprint = narrowed["project"], narrowed["sprint"], narrowed["no_sprint"]
        rows = []
        for card in CARDS:
            if project and card.project != project:
                continue
            if no_sprint and card.sprint:
                continue
            if sprint and card.sprint != sprint:
                continue
            rows.append(
                {
                    "ref": card.ref,
                    "title": card.title,
                    "type": card.type,
                    "project": card.project,
                    "sprint": card.sprint,
                    "state": card.state,
                    "moved": NOW - card.minutes * 60,
                    "head_override": None,
                    "worker_head": card.head,
                    "review_head_override": None,
                    "review_head": card.review_head,
                    "blocking_comment": f"[move:observer]\n{card.blocked}" if card.blocked else None,
                    "worker_report": None,
                }
            )
        return rows


def board_document(project: str | None, sprint: str | None) -> dict[str, Any]:
    skips = Reading("ready_skips", sources.available(NOW), {
        card.ref: {"since": at(card.minutes - 5), "reason": card.skip} for card in CARDS if card.skip
    })
    records = Reading("records", sources.available(NOW), {})
    document = board_view.board_document(
        _BoardClient(), now=NOW, project=project, sprint=sprint, skips=skips, records=records
    )
    for column in document["columns"]:
        for tile in column["cards"]:
            card = BY_REF[tile["ref"]]
            tile["pipeline"] = pipeline(card)
            tile["waiting_owner"] = _waiting_owner(card)
    return document


def _waiting_owner(card: Card) -> dict[str, str] | None:
    if not card.owner:
        return None
    return {"since": at(card.minutes - 10), "reason": card.owner, "by": "po"}


# -- the card page ---------------------------------------------------------------------------------

DESCRIPTIONS = {
    SHOWCASE: """\
## Goal

`GET /v2/exports/{id}/download` builds the whole report in memory before it starts the upload to
object storage. Exports above ~400 MB push the worker pods over their memory limit and the job is
killed without a useful error. Stream the payload instead.

## Scope

- Write rows to a temporary spooled file and upload it with **multipart upload** once it passes the
  threshold (`EXPORT_MULTIPART_THRESHOLD`, default 64 MiB).
- Keep the CSV and NDJSON encoders as they are; only the transport changes.
- Report progress through the existing `export_jobs.progress` column.

## Out of scope

1. The XLSX encoder (it has its own card).
2. Changing retention of finished exports.

## Done when

- An export of 2 GB finishes on a worker limited to 512 MiB.
- `tests/exports/test_streaming.py` covers threshold, retry of one part and abort on failure.
- The runbook `docs/runbooks/exports.md` names the new setting.

```text
acme-api export run --account 4471 --format ndjson --rows 25000000
```
""",
}

REPORT_ROUND_ONE = """\
## What changed
- `exports/transport.py`: new `SpooledUpload` that switches to multipart above the threshold.
- `exports/jobs.py`: the job writes through `SpooledUpload` and reports progress every 5 000 rows.
- Settings: `EXPORT_MULTIPART_THRESHOLD` (bytes, default `67108864`).

## Verification
- `pytest tests/exports -q`: 48 passed.
- Manual run with `--rows 25000000` on a 512 MiB container: finished in 6m41s, peak RSS 212 MiB.

## Not done
- Abort of the multipart upload on encoder failure is not covered yet.
"""

REPORT_ROUND_TWO = """\
## What changed
- `exports/transport.py`: `SpooledUpload.abort()` is called from the job's failure path, so a
  failed export leaves no orphaned multipart upload behind.
- `tests/exports/test_streaming.py`: covers the threshold, retry of a single part (`429` then
  `200`), and abort when the encoder raises halfway.
- `docs/runbooks/exports.md`: documents `EXPORT_MULTIPART_THRESHOLD` and how to find stuck uploads.

## Verification
- `pytest tests/exports -q`: 53 passed, 0 failed.
- `ruff check exports tests/exports`: clean.
- 2 GB export on a worker limited to 512 MiB: finished, peak RSS 231 MiB.

## Commits
- `{sha}` Stream export payloads through a spooled multipart upload
- `{sha2}` Abort the multipart upload when an export fails

## Risks
- Object storage lifecycle rules still delete incomplete uploads after 7 days; with `abort()` this
  is only a safety net now.
"""

VERDICT = """\
## Verdict: changes requested

1. `SpooledUpload` never calls `abort_multipart_upload` when the encoder raises, so a failed export
   leaves an incomplete upload that is billed until the lifecycle rule removes it.
2. The retry test asserts on the log line, not on the uploaded parts; assert the part numbers.

Everything else reads well: the threshold default and the progress reporting are fine.
"""

DECISION = """\
Keep the card in this sprint. The review findings are small and inside the card's scope; send it
back to the same worker profile with the verdict as context.
"""


def _event(
    ref: str,
    number: int,
    kind: str,
    minutes: float,
    *,
    role: str,
    actor: str,
    reason: str | None = None,
    outcome: str | None = None,
    transition: tuple[str, str] | None = None,
    data: dict[str, Any] | None = None,
    micro: int = 0,
) -> dict[str, Any]:
    """One journal item in the layer's event shape (`ummanu.webproto.journal`)."""
    return {
        "cursor": f"c{number:04d}",
        "typed": True,
        "kind": kind,
        "event_id": f"evt-{ref}-{number:03d}",
        "request_id": None,
        "ref": ref,
        "occurred_at": at(minutes, micro=micro),
        "actor": {"role": role, "id": actor},
        "reason": reason,
        "outcome": outcome,
        "transition": {"source": transition[0], "target": transition[1]} if transition else None,
        "related_refs": [],
        "data": data or {},
    }


def _showcase_events() -> list[dict[str, Any]]:
    ref = SHOWCASE
    sha, sha2 = _sha(ref, 2), _sha(ref, 3)
    report_two = REPORT_ROUND_TWO.format(sha=sha, sha2=sha2)
    rows = [
        ("created", 312, "observer", "observer-acme", "Cut for DoD item d5 (exports must not run out of memory).", "success", None, {}),
        ("routing", 311.5, "dispatcher", "dispatcher", "complexity standard: worker codex-xhigh, reviewer opus-max", "success", None, {}),
        ("card.readied", 311, "observer", "observer-acme", "Scope is clear and the project is free.", "success", ("issues", "ready"), {}),
        ("card.started", 296, "dispatcher", "dispatcher", None, "success", ("ready", "in_progress"), {}),
        ("outcome_round_context", 295.8, "dispatcher", "dispatcher", "round 1: no previous verdict", "success", None, {}),
        ("commented", 251, "owner", "owner", "Please keep the multipart threshold configurable per environment, staging uses a smaller bucket quota.", "success", None, {}),
        ("card.reported", 198, "worker", "codex-xhigh", REPORT_ROUND_ONE, "success", ("in_progress", "validate"),
         {"marker": "report:done", "body": REPORT_ROUND_ONE}),
        ("card.validated", 196, "dispatcher", "dispatcher", "report schema and branch checks passed", "success", ("validate", "review"), {}),
        ("card.verdict", 160, "reviewer", "opus-max", VERDICT, "success", None, {"marker": "review:changes_requested", "body": VERDICT}),
        ("card.decided", 151, "observer", "observer-acme", DECISION, "success", ("review", "in_progress"),
         {"decision": "decision:rework", "body": DECISION}),
        ("outcome_round_context", 150.5, "dispatcher", "dispatcher", "round 2: verdict review:changes_requested carried to the worker", "success", None, {}),
        ("head_changed", 121, "dispatcher", "dispatcher", None, "success", None, {
            "role": "worker", "change": "replaced", "cause": "provider_failover",
            "reason": "the codex resource answered 429 for 6 minutes; the run continues on the same profile after the window reset",
            "before": {"profile": "codex-xhigh", "run_id": "run-w-5c21e0"},
            "after": {"profile": "codex-xhigh", "run_id": "run-w-8d04a7"},
        }),
        ("commented", 64, "worker", "codex-xhigh", "Abort path implemented; running the 2 GB export on the constrained worker now.", "success", None, {}),
        ("card.reported", 14, "worker", "codex-xhigh", report_two, "success", ("in_progress", "validate"),
         {"marker": "report:done", "body": report_two}),
        ("card.validated", 9.3, "dispatcher", "dispatcher", "report schema and branch checks passed", "success", ("validate", "ci"), {}),
        ("gate.started", 9, "dispatcher", "dispatcher", f"exact-SHA gate requested for {sha2}", "pending", None, {"sha": sha2}),
    ]
    return [
        _event(ref, number, kind, minutes, role=role, actor=actor, reason=reason, outcome=outcome,
               transition=transition, data=data, micro=(number * 104729) % 1_000_000 or 1)
        for number, (kind, minutes, role, actor, reason, outcome, transition, data) in enumerate(rows, start=1)
    ]


def _card_events(card: Card) -> list[dict[str, Any]]:
    if card.ref == SHOWCASE:
        return _showcase_events()
    walk = _walk(card)
    created = _created_minutes(card)
    observer = f"observer-{card.project.split('-')[0]}" if card.sprint else "po"
    events = [_event(card.ref, 1, "created", created, role="observer" if card.sprint else "po", actor=observer,
                     reason="cut from the sprint goal" if card.sprint else "filed from the PO session", outcome="success")]
    step = (created - card.minutes) / max(len(walk), 1)
    for index, (source, target) in enumerate(zip(walk, walk[1:], strict=False), start=1):
        minutes = created - step * index if target != card.state else card.minutes
        kind, role, actor, reason = "card.moved", "dispatcher", "dispatcher", None
        data: dict[str, Any] = {}
        if target == "ready":
            kind, role, actor, reason = "card.readied", "observer", observer, "scope is clear"
        elif target == "in_progress":
            kind = "card.started"
        elif source == "in_progress" and target != "blocked":
            kind, role, actor = "card.reported", "worker", card.head or "codex-high"
            reason = f"## What changed\n- {card.title}.\n\n## Verification\n- tests pass."
            data = {"marker": "report:done", "body": reason}
        elif target == "blocked":
            kind, role, actor, reason = "card.blocked", "observer", observer, card.blocked
        elif target == "done" and source == "review":
            kind, role, actor, reason = "card.verdict", "reviewer", card.review_head or "opus-max", "approved"
            data = {"marker": "review:approved", "body": "Approved: the change does what the card asks."}
        events.append(_event(card.ref, index + 1, kind, minutes, role=role, actor=actor, reason=reason,
                             outcome="success", transition=(source, target), data=data))
    return events


def _runs_of(card: Card) -> list[dict[str, Any]]:
    """The head runs the card recorded (`heads.items`), oldest first."""
    walk = _walk(card)
    runs = []
    if "in_progress" in walk and card.head:
        profile = HEADS[card.head]
        if card.ref == SHOWCASE:
            for attempt, run_id, state, resolved in (
                (1, "run-w-1a9f02", "stopped", "gpt-6.1-sol-2026-10-14"),
                (2, "run-w-5c21e0", "stopped", "gpt-6.1-sol-2026-10-14"),
            ):
                runs.append({"role": "worker", "run_id": run_id, "attempt": attempt, "state": state,
                             "head": card.head, "model": profile["model"], "effort": profile["effort"],
                             "resolved_model": resolved, "local_pty": True, "current": False})
        current = card.state in {"in_progress", "validate", "ci"}
        runs.append({
            "role": "worker", "run_id": f"run-w-{_sha(card.ref, 9)[:6]}" if card.ref != SHOWCASE else "run-w-8d04a7",
            "attempt": 2 if card.ref == SHOWCASE else 1,
            "state": "running" if current else "stopped", "head": card.head, "model": profile["model"],
            "effort": profile["effort"], "resolved_model": f"{profile['model']}-2026-10-14" if profile["adapter"] == "codex" else profile["model"],
            "adapter": profile["adapter"], "local_pty": True, "current": current,
        })
    if card.review_head and (card.state in {"review", "post_merge_ci", "done"} or card.ref == SHOWCASE):
        profile = HEADS[card.review_head]
        runs.append({
            "role": "reviewer", "run_id": f"run-r-{_sha(card.ref, 8)[:6]}", "attempt": 1,
            "state": "running" if card.state == "review" else "stopped", "head": card.review_head,
            "model": profile["model"], "effort": profile["effort"], "resolved_model": profile["model"],
            "adapter": profile["adapter"], "local_pty": True, "current": card.state == "review",
            "reason": "paused by the owner while CI runs" if card.ref == SHOWCASE else None,
        })
    return runs


def _work(card: Card, events: list[dict[str, Any]]) -> dict[str, Any]:
    found: dict[str, dict[str, Any]] = {}
    latest = None
    for event in events:
        data = event["data"]
        marker = data.get("marker") or data.get("decision")
        if not marker:
            continue
        family, _, value = marker.partition(":")
        slot = {"report": "worker_report", "review": "review_verdict", "decision": "decision"}[family]
        found[slot] = {"marker": marker, "value": value, "at": event["occurred_at"], "body": data["body"], "classification": None}
        latest = {"kind": family, "value": value, "at": event["occurred_at"], "terminal": card.state == "done"}
    if card.state == "blocked":
        found["worker_report"] = {"marker": "report:blocked", "value": "blocked", "at": at(card.minutes + 2),
                                  "body": f"classification: external\n\n{card.blocked}", "classification": "external"}
        latest = {"kind": "report", "value": "blocked", "at": at(card.minutes + 2), "terminal": False}
    return {"worker_report": None, "review_verdict": None, "decision": None, **found, "outcome": latest}


def _attempt(card: Card) -> dict[str, Any] | None:
    if card.state in {"issues", "ready", "done"} or not card.head:
        return None
    attempt_id = f"att-{_sha(card.ref, 4)[:10]}"
    return {
        "state": {"ci": "gate_pending", "blocked": "parked"}.get(card.state, "running"),
        "attempt_id": attempt_id,
        "attempt_round": 2 if card.ref == SHOWCASE else 1,
        "report_generation": 2 if card.ref == SHOWCASE else 1,
        "gate_state": "pending" if card.state == "ci" else ("green" if card.state == "review" else None),
        "workspace": f"/srv/ummanu-preview/workspaces/{card.project}/cards/{card.ref}/{attempt_id}/checkout",
        "head": card.head,
        "review_head": card.review_head,
        "paused": {"worker": False, "reviewer": card.ref == SHOWCASE},
    }


def _description(card: Card) -> str:
    if card.ref in DESCRIPTIONS:
        return DESCRIPTIONS[card.ref]
    return (
        f"## Goal\n\n{card.title}.\n\n## Done when\n\n- The change is covered by tests in `{card.project}`.\n"
        "- The behaviour is described in the project's docs.\n"
    )


def task_snapshot(ref: str) -> dict[str, Any]:
    card = BY_REF.get(ref)
    if card is None:
        raise TaskNotFound(f"the board holds no card {ref!r}")
    events = _card_events(card)
    runs = _runs_of(card)
    attempt = _attempt(card)
    created = at(_created_minutes(card))
    value = {
        "ref": card.ref,
        "title": card.title,
        "state": card.state,
        "project": card.project,
        "sprint": card.sprint,
        "type": card.type,
        "blocked_by": None,
        "claimed_by": card.head if card.state not in {"issues", "ready"} else None,
        "created_at": created,
        "updated_at": at(card.minutes),
        "description": _description(card),
        "closed": card.state == "done",
        "workspace": attempt["workspace"] if attempt else None,
        "routing": {"complexity": card.complexity, "sprint": card.sprint, "dod_item": card.dod_item},
        "waiting_owner": _waiting_owner(card),
        "touches_production": "none" if card.type == "operation" else None,
        "origin": None,
        "wait": None,
        "e2e": None,
        "po_execution": None,
    }
    if card.ref == "orbit-web-120":
        value["origin"] = {"po_session": "po-7a1c55e2", "current_session": "po-7a1c55e2", "request_id": "req-3f81c0",
                           "returns": [{"state": "blocked", "status": "delivered", "delivered_at": at(370), "session": "po-7a1c55e2"}]}
    if card.ref == "nimbus-cli-78":
        value["wait"] = {"state": "delivered", "target": {"kind": "github_run", "repo": "nimbus/nimbus-cli", "run_id": 991204,
                         "link": "https://ci.example.invalid/nimbus/nimbus-cli/runs/991204"},
                         "waiting_since": at(900), "deadline": at(300), "return_to": ["po-session:po-7a1c55e2"],
                         "deliveries": {"po-session:po-7a1c55e2": "delivered"},
                         "result": {"summary": "nightly e2e green on release/2.4 (212 passed)"}}
    agents = [
        {"run_id": run["run_id"], "role": run["role"], "state": "running", "project": card.project}
        for run in runs if run.get("current")
    ]
    return {
        "schema_version": 1,
        "kind": "task",
        "observed_at": at(0),
        "ref": card.ref,
        "card": {"source": SOURCE, "value": value},
        "project": {"id": card.project, "registered": True, "binding": _binding(card.project)},
        "attempt": {"source": SOURCE, "value": attempt},
        "pipeline": pipeline(card),
        "agents": {"source": SOURCE, "items": agents},
        "heads": {"source": SOURCE, "items": runs},
        "work": _work(card, events),
        "events": {"source": SOURCE, "items": events, "next_cursor": f"c{len(events):04d}"},
    }


def card_runs(ref: str) -> dict[str, Any]:
    card = BY_REF.get(ref)
    if card is None:
        raise TaskNotFound(f"the board holds no card {ref!r}")
    if ref != SHOWCASE:
        return {"items": []}
    return {"items": [
        {"run": {"run_id": "prun-e2e-41c2", "role": "e2e", "profile": "acme-staging", "phase": "verify"},
         "state": {"value": "finished", "ended": True, "reason": "the run's process exited",
                   "result": {"present": True, "value": {"summary": "2 GB export finished, peak RSS 231 MiB"}, "verdict": "pass"},
                   "exit": {"code": 0}}},
        {"run": {"run_id": "prun-e2e-41c3", "role": "e2e", "profile": "acme-staging", "phase": "verify"},
         "state": {"value": "running", "ended": False, "reason": "the run's process is alive", "result": {}, "exit": {}}},
    ]}


def head_view(ref: str, run_id: str) -> dict[str, Any]:
    card = BY_REF.get(ref)
    if card is None:
        raise TaskNotFound(f"the board holds no card {ref!r}")
    run = next((item for item in _runs_of(card) if item["run_id"] == run_id), None)
    if run is None:
        raise HeadRunNotFound(f"card {ref} recorded no head run {run_id[:80]!r}")
    return _head_document(ref, run_id, "task", run["role"], run["state"])


def _head_document(ref: str, run_id: str, kind: str, role: str, state: str) -> dict[str, Any]:
    start = NOW - 50 * 60
    kinds = ("session.started", "turn.started", "output", "output", "turn.finished", "turn.started", "output", "fold", "output")
    tail = [
        {"seq": seq, "kind": name, "at": start + seq * 211, "turn": 1 + seq // 5,
         "bytes": 1800 + seq * 97 if name == "output" else None,
         "output_bytes": 52_000 + seq * 4100 if name == "output" else None,
         "folded_windows": 2 if name == "fold" else None,
         "reason": "context window folded" if name == "fold" else None}
        for seq, name in enumerate(kinds, start=1)
    ]
    return {
        "ref": ref,
        "run_id": run_id,
        "task_kind": kind,
        "observed_at": at(0),
        "read_only": "read-only: this page shows the head's journal and never types into its terminal",
        "head": {"role": role, "runtime": "local-pty", "state": state, "reason": "the supervisor holds a live process" if state == "running" else "the process exited"},
        "journal": {"answered": True, "reason": None, "tail": tail},
    }


# -- installation ----------------------------------------------------------------------------------


def _binding(project: str) -> dict[str, Any]:
    return {"repo": f"https://git.example.invalid/{project.split('-')[0]}/{project}", "adapter": "python-uv" if project != "orbit-web" else "node-pnpm", "default_branch": "main"}


def _projects() -> dict[str, Any]:
    return {
        "source": SOURCE,
        "items": [
            {"id": project, "repo": _binding(project)["repo"], "remote": "origin", "adapter": _binding(project)["adapter"],
             "default_branch": "main", "plane": None, "enabled": True}
            for project in PROJECTS
        ],
    }


STATUS = {
    "installation": {"cards": {"total": len(CARDS), "open": sum(card.state != "done" for card in CARDS)}},
    "host": {
        "units": [
            {"name": "ummanu-dispatcher.service", "kind": "service", "present": True, "enabled": True, "active": "active"},
            {"name": "ummanu-web.service", "kind": "service", "present": True, "enabled": True, "active": "active"},
            {"name": "ummanu-checkpoint.timer", "kind": "timer", "present": True, "enabled": True, "active": "active"},
        ],
        "resources": {"disk_free_bytes": 182 * 1024**3, "memory_available_bytes": 9.4 * 1024**3, "load_average": [1.72, 1.41, 1.33]},
    },
    "dispatcher": {
        "phase": "ticking",
        "pause": {"paused": False},
        "divergences": {"open_count": 1},
        "active_attempts": ["a1", "a2", "a3", "a4", "a5", "a6"],
        "observers": ["sprint:41", "sprint:42"],
        "reconciliation": {"last_tick_finished_at": at(0.4)},
    },
    "checkpoint": {"checkpoint_status": "ok", "lag_minutes": 4, "lag_commits": 2},
    "memory": {"index_present": True, "fact_count": 1284},
    "recovery": {"resources": [
        {"resource": "claude", "state": "ready"},
        {"resource": "codex", "state": "ready"},
    ]},
}


def system_snapshot() -> dict[str, Any]:
    current = [card for card in CARDS if card.state != "done"]
    return {
        "schema_version": 1,
        "kind": "system",
        "observed_at": at(0),
        "installation": {
            "instance": "/srv/ummanu-preview/instance",
            "name": "preview",
            "data_dir": "/srv/ummanu-preview/data",
            "health": {"source": SOURCE, "status": health_summary(copy.deepcopy(STATUS))},
        },
        "projects": _projects(),
        "tasks": {"source": SOURCE, "items": [
            {"ref": card.ref, "title": card.title, "state": card.state, "project": card.project, "sprint": card.sprint,
             "type": card.type, "blocked_by": None, "claimed_by": card.head, "created_at": at(_created_minutes(card)),
             "updated_at": at(card.minutes)}
            for card in current
        ]},
        "agents": {"source": SOURCE, "items": [
            {"run_id": run["run_id"], "role": run["role"], "state": "running", "ref": card.ref, "project": card.project}
            for card in current for run in _runs_of(card) if run.get("current")
        ]},
    }


def doctor_snapshot() -> dict[str, Any]:
    problems = [
        {"code": "dispatcher.divergences_open", "message": "1 dispatcher divergence(s) are open", "severity": "yellow", "source": "status"},
        {"code": "memory.index_stale", "message": "the memory index was rebuilt 26 hours ago", "severity": "yellow", "source": "doctor",
         "accepted": True, "acceptance_reason": "reindex runs nightly; accepted until the next run", "age_hours": 26},
    ]
    return {
        "kind": "doctor",
        "observed_at": at(0),
        "readable": True,
        "reason": None,
        "colour": "yellow",
        "problems": problems,
        "doctor": {"state": "yellow", "mode": "periodic", "run_at": at(14), "completed_at": at(13), "exit_code": 0},
        "doctor_run_at": at(14),
        "source": SOURCE,
    }


def pause_state() -> dict[str, Any]:
    return {
        "kind": "pause",
        "observed_at": at(0),
        "state": {"paused": False, "mode": None, "source": SOURCE},
        "dispatcher": {"phase": "ticking", "tracked_cards": 9},
    }


def usage_snapshot() -> dict[str, Any]:
    return {
        "observed_at": at(1),
        "providers": [
            {"id": "claude", "label": "Claude", "status": "available", "age_seconds": 40, "windows": [
                {"name": "5h", "remaining_percent": 62, "resets_at": ahead(133), "window_minutes": 300},
                {"name": "week", "remaining_percent": 41.5, "resets_at": ahead(3 * 1440 + 250), "window_minutes": 10080},
            ]},
            {"id": "codex", "label": "Codex", "status": "available", "age_seconds": 40, "windows": [
                {"name": "5h", "remaining_percent": 18, "resets_at": ahead(47), "window_minutes": 300},
                {"name": "week", "remaining_percent": 73, "resets_at": ahead(5 * 1440 + 30), "window_minutes": 10080},
            ], "reset_credits": {"available": 1, "applicable": 0, "next_expires_at": ahead(9 * 1440)}},
        ],
    }


COMMANDS = (
    (2, "owner", "owner", "task_comment", SHOWCASE, "accepted", "the comment is on the card"),
    (9, "dispatcher", "dispatcher", "gate_request", SHOWCASE, "accepted", "exact-SHA gate requested"),
    (17, "dispatcher", "dispatcher", "card_move", "orbit-web-115", "accepted", "review approved; merged to main"),
    (31, "observer", "observer-orbit", "card_create", "orbit-web-122", "accepted", "cut from the sprint goal"),
    (48, "dispatcher", "dispatcher", "run_start", "acme-api-233", "accepted", "worker codex-xhigh raised"),
    (75, "po", "web", "sprint_comment", "sprint:42", "accepted", "delivered to the observer"),
    (96, "observer", "observer-acme", "card_move", "acme-api-236", "refused",
     "the project acme-api is held by another attempt; the card stays in Ready until it is free"),
    (130, "owner", "owner", "pause_drain", "", "refused", "a reason is required"),
    (205, "dispatcher", "dispatcher", "run_review", "nimbus-cli-76", "accepted", "reviewer codex-xhigh raised"),
    (380, "po", "po", "task_handover", "orbit-web-120", "accepted", "handed to the owner: a commercial choice"),
    (1440, "observer", "observer-acme", "card_move", "acme-api-226", "accepted", "blocked: needs a maintenance window"),
    (1500, "po", "web", "sprint_create", "sprint:42", "accepted", "sprint opened with observer observer-opus"),
)


def command_history() -> dict[str, Any]:
    return {
        "kind": "command_history",
        "observed_at": at(0),
        "limit": 25,
        "commands": {"source": SOURCE, "has_more": True, "next_cursor": "h0012", "items": [
            {"occurred_at": at(minutes), "actor": {"role": role, "id": actor}, "action": action,
             "entity": {"kind": "sprint" if ref.startswith("sprint:") else "card", "ref": ref},
             "result": {"outcome": outcome, "reason": reason}}
            for minutes, role, actor, action, ref, outcome, reason in COMMANDS
        ]},
    }


def owner_event_list(*, unread_only: bool) -> dict[str, Any]:
    events = [
        {"id": 31, "class": "needs_owner", "kind": "card_handed_to_owner", "subject_ref": "orbit-web-120", "unread": True,
         "pinned": True, "held": True, "text": BY_REF["orbit-web-120"].owner, "created_at": at(370)},
        {"id": 30, "class": "notice", "kind": "sprint_item_met", "subject_ref": "sprint:41", "unread": True,
         "pinned": False, "held": False, "text": "DoD item d9 of sprint:41 is met", "created_at": at(860)},
        {"id": 27, "class": "notice", "kind": "card_done", "subject_ref": "nimbus-cli-81", "unread": False,
         "pinned": False, "held": False, "text": "nimbus-cli-81 is done", "created_at": at(95), "read_at": at(60)},
    ]
    shown = [event for event in events if event["unread"] or not unread_only]
    return {"kind": "owner_events", "observed_at": at(0), "source": SOURCE, "unread_only": unread_only,
            "events": shown, "unread": 2, "notice_count": 1}


# -- sprints ---------------------------------------------------------------------------------------

BY = {"role": "observer", "actor": "observer-acme", "at": at(600)}

#: The open sprint's items, deliberately interleaved so met, in-main, open and descoped alternate.
DOD_41 = (
    ("d1", "met", 3, "Every API log line carries the request id", False),
    ("d2", "open", 5, "Payments are idempotent under client retries and webhook deliveries are schema-checked", False),
    ("d3", "open", 2, "The export API limits are documented for customers", False),
    ("d4", "met", 8, "Pagination cursors cannot be forged", False),
    ("d5", "open", 3, "Exports of any size finish within the worker memory limit", False),
    ("d6", "met", 1, "The legacy XML export route is gone", True),
    ("d7", "descoped", 2, "Ledger tables are partitioned", False),
    ("d8", "open", 13, "Public endpoints and billing data are launch-ready: search rate-limited per API key, invoice currency known for every account", False),
    ("d9", "met", 5, "Webhook delivery log validates against a published schema", True),
)

DOD_42 = (
    ("d1", "met", None, "The kanban board updates optimistically and rolls back on refusal", False),
    ("d2", "open", 3, "First load of the app stays under 180 KB of JavaScript", False),
    ("d3", "met", 2, "Feature flags are served from the edge", True),
    ("d4", "open", None, "Checkout supports the EU storefront's payment provider", False),
)


def _stored(rows: tuple[tuple[str, str, int | None, str, bool], ...]) -> list[dict[str, Any]]:
    items = []
    for identifier, state, weight, text, in_main in rows:
        item: dict[str, Any] = {
            "id": identifier, "text": text, "state": state, "weight": weight,
            "weight_reason": f"sized by the observer: {weight} of the scale" if weight else None,
            "evidence": f"https://git.example.invalid/pulls/{300 + int(identifier[1:]) * 7} merged, verified on staging" if state == "met" else None,
            "met_by": dict(BY) if state == "met" else None,
        }
        if state == "descoped":
            item["descoped"] = {**BY, "reason": "needs a maintenance window outside this sprint; filed as a new issue"}
        if in_main:
            item["in_main_manual"] = {**BY, "evidence": f"merged as {_sha(identifier, 5)[:12]} on main"}
        items.append(item)
    return sprint_dod.stored_items(items)


def _dod(sprint: str) -> dict[str, Any]:
    rows = {"sprint:41": DOD_41, "sprint:42": DOD_42}.get(sprint)
    if rows is None:
        return sprint_dod.section(None)
    cards = [{"ref": card.ref, "dod_item": card.dod_item} for card in CARDS if card.sprint == sprint and card.dod_item]
    linked = {card["dod_item"] for card in cards}
    if any(item[0] in linked and item[4] for item in rows):
        raise ValueError(f"{sprint}: a manual in-main mark is for an item no card links")
    releases = [
        {"record_type": PROTOCOL_EVENT_RECORD_TYPE, "ref": card.ref, "event_id": f"evt-{card.ref}-release",
         "kind": "card.moved", "outcome": "success", "actor": {"role": "dispatcher", "id": "dispatcher"},
         "transition": {"source": "review", "target": "done"},
         "data": {RELEASE_MERGE_KEY: {"base": "main", "merge_sha": _sha(card.ref, 7)}}}
        for card in CARDS
        if card.sprint == sprint and card.state == "done"
    ]
    return sprint_dod.section(_stored(rows), cards=cards, events=releases)


SPRINTS = {
    "sprint:41": {
        "status": "open",
        "goal": "Make acme-api safe for the enterprise launch: idempotent payments, bounded exports, forged-cursor proof pagination and traceable logs.",
        "current": SHOWCASE,
        "observer": "observer-opus",
        "worker": "codex-xhigh",
        "reviewer": "opus-max",
        "budget": {"total": 11, "thresholds": {"signal": 12, "hard": 16}, "by_type": {"code": 9, "operation": 1, "decision": 1},
                   "signal_reached": False, "hard_reached": False},
        "issues": ["issue:acme-api#88", "issue:acme-api#91", "issue:acme-api#97"],
        "waiting": {"state": "waiting", "reason": f"CI is running the exact-SHA gate for {SHOWCASE}", "card": SHOWCASE},
        "attention": True,
    },
    "sprint:42": {
        "status": "open",
        "goal": "orbit-web storefront refresh: faster first load, optimistic kanban, EU checkout.",
        "current": "orbit-web-119",
        "observer": "observer-opus",
        "worker": "opus-max",
        "reviewer": "codex-xhigh",
        "budget": {"total": 13, "thresholds": {"signal": 12, "hard": 14}, "by_type": {"code": 9, "research": 2, "decision": 2},
                   "signal_reached": True, "hard_reached": False},
        "issues": ["issue:orbit-web#140"],
        "waiting": {"state": "working", "reason": "the worker is on orbit-web-119"},
        "attention": False,
    },
    "sprint:40": {"status": "closed", "goal": "nimbus-cli 2.4: keychain login, upload progress and completions.", "current": "nimbus-cli-81",
                  "observer": "observer-opus", "worker": "codex-high", "reviewer": "opus-max", "budget": None, "issues": [], "waiting": {"state": "ended", "reason": "the sprint is closed"}, "attention": False},
    "sprint:39": {"status": "closed", "goal": "orbit-web accessibility pass over the marketing pages and the onboarding flow.", "current": "orbit-web-112",
                  "observer": "observer-opus", "worker": "sonnet-medium", "reviewer": "opus-max", "budget": None, "issues": [], "waiting": {"state": "ended", "reason": "the sprint is closed"}, "attention": False},
    "sprint:38": {"status": "stopped", "goal": "acme-api reporting v1 (stopped: superseded by the enterprise launch sprint).", "current": None,
                  "observer": "observer-opus", "worker": "codex-high", "reviewer": "opus-max", "budget": None, "issues": [], "waiting": {"state": "ended", "reason": "the sprint was stopped by the owner"}, "attention": False},
}


def _decision(ref: str) -> dict[str, Any]:
    if ref == "sprint:41":
        entry = {
            "selected_step": f"Wait for the CI gate on {SHOWCASE}, then send it to review with opus-max.",
            "selected_why": "Round 2 addressed both review findings; the gate is the only open step.",
            "rejected_alternatives": "Start acme-api-236 in parallel: refused, the project is reserved by the running attempt.",
            "next_safe_step": "If the gate is red, return the card to the worker with the failing test names.",
            "recorded_at": at(8),
        }
    elif ref == "sprint:42":
        entry = {
            "selected_step": "Keep the worker on orbit-web-119; the payment decision stays with the owner.",
            "selected_why": "orbit-web-120 is a commercial choice the PO handed over.",
            "next_safe_step": "Cut the checkout card once the owner picks a provider.",
            "recorded_at": at(40),
        }
    else:
        return section("decision", entry=None, freshness={"value": {"fresh": True}})
    return section("decision", entry=entry, freshness={"value": {"fresh": True}})


def _sprint_work(ref: str) -> dict[str, Any]:
    spec = SPRINTS[ref]
    current = BY_REF.get(spec["current"]) if spec["current"] else None
    states: dict[str, list[str]] = {}
    for card in CARDS:
        if card.sprint == ref:
            states.setdefault(card.state, []).append(card.ref)
    is_open = spec["status"] == "open"
    gate = {"ci": "pending", "review": "green", "done": "green"}.get(current.state if current else "", "pending")
    waiting_on = []
    if ref == "sprint:41":
        waiting_on = [
            {"kind": "run", "card": SHOWCASE, "detail": "exact-SHA gate https://ci.example.invalid/acme/acme-api/runs/55120"},
            {"kind": "dependency", "card": "acme-api-236", "detail": "waits for the project reservation", "holder": SHOWCASE},
        ]
    if ref == "sprint:42":
        waiting_on = [{"kind": "owner", "card": "orbit-web-120", "detail": BY_REF["orbit-web-120"].owner}]
    return {
        "current_task": section("current_task", ref=current.ref if current else None, live=is_open,
                                reason=None if current else "the observer has cut no card for this sprint yet"),
        "current_card_state": section("current_card_state", state=current.state, since=at(current.minutes),
                                      age_seconds=current.minutes * 60, transition="recorded", title=current.title)
        if current else section("current_card_state", state=None, transition="not_applicable", reason="no current card"),
        "decision": _decision(ref),
        "cards": section("cards", states=states),
        "degraded_cards": section("degraded_cards", items=[]),
        "ready_skips": section("ready_skips", items=[]),
        "checks": section("checks", state="not_green" if gate != "green" else "green", gate={"state": gate},
                          reason=f"gate {gate} for {current.ref}" if current else None) if is_open else section("checks", state="not_applicable", gate={}),
        "waiting": section("waiting", **spec["waiting"]),
        "waiting_on": waiting_on,
        "attention": {"state": "waiting" if spec["attention"] else "none", "event_ids": [30] if spec["attention"] else [], "reason": None},
        "head_profiles": section("head_profiles", **{
            role: {**HEADS[spec[role]], "profile": spec[role]} for role in ("observer", "worker", "reviewer")
        }),
    }


def _observer(ref: str) -> dict[str, Any]:
    spec = SPRINTS[ref]
    running = spec["status"] == "open"
    return {
        "declared": section("declared", state="declared", profile=spec["observer"], value={"kind": "profile", "profile": spec["observer"]}),
        "launch": section(
            "launch",
            state="running" if running else "stopped",
            reason="the dispatcher holds a live observer head" if running else "the sprint ended and its observer was stopped",
            record={"head": spec["observer"], "heartbeat_state": "fresh" if running else "stale",
                    "run_id": f"run-o-{_sha(ref, 6)[:6]}"} if running else {},
        ),
    }


def _sprint_identity(ref: str) -> dict[str, Any]:
    spec = SPRINTS[ref]
    project = SPRINT_PROJECT[ref]
    return {
        "ref": ref,
        "goal": spec["goal"],
        "status": spec["status"],
        "product": project,
        "issues": spec["issues"],
        "reservations": [project],
        "repositories": [_binding(project)["repo"]],
        "executors": {"worker": {"state": "pinned", "profile": spec["worker"]}, "reviewer": {"state": "unpinned"}},
        "po_session": "po-7a1c55e2",
        "allowed_productions": [],
        "owner_decisions": [
            {"id": "od-1", "scope": "sprint", "kind": "constraint", "value": {"max_parallel_cards": 1},
             "quotation": "One card at a time on acme-api until the launch, please.", "recorded_by": {"role": "po", "actor": "po"}},
        ] if ref == "sprint:41" else [],
        "local_run_exceptions": [],
        "dod": _dod(ref),
        "budget": spec["budget"],
    }


def sprint_list(statuses: list[str] | None) -> dict[str, Any]:
    wanted = set(statuses or [])
    items = [
        {**_sprint_identity(ref), **_sprint_work(ref), "observer": _observer(ref)}
        for ref in sorted(SPRINTS, key=lambda name: -int(name.split(":")[1]))
        if not wanted or SPRINTS[ref]["status"] in wanted
    ]
    return {"schema_version": 1, "kind": "sprint_list", "observed_at": at(0), "filter": {"statuses": sorted(wanted)},
            "sprints": section("sprints", items=items)}


DOD_TEXT = {
    "sprint:41": "The enterprise launch checklist from issue:acme-api#88, split into the items above.\n\n"
                 "- Each item is verified on **staging** by the observer, not only merged.\n"
                 "- Weights follow the shared scale (1, 2, 3, 5, 8, 13).\n",
    "sprint:42": "Storefront refresh as agreed with the owner in the PO session.\n",
}


def sprint_state(ref: str) -> dict[str, Any]:
    if ref not in SPRINTS:
        raise TaskNotFound(f"the board holds no sprint {ref!r}")
    identity = _sprint_identity(ref)
    spec = SPRINTS[ref]
    value = {
        **identity,
        "definition_of_done": DOD_TEXT.get(ref, "Free-text Definition of Done: the release notes list every shipped change."),
        "current_task": spec["current"],
        "e2e": None,
        "resume": {
            "selected_step": "see the observer's call",
            "state_of_play": "4 of 8 in-scope items met; d4 is in CI, d2 is in progress, d8 not started.",
            "open_questions": "Does d5 need a data migration window? Asked the PO.",
            "risks": "d8 is the heaviest item (13) and has not started.",
        } if spec["status"] == "open" else {"state_of_play": "closed with every in-scope item met"},
        "audit": {"created_at": at(3000), "updated_at": at(8)},
    }
    return {
        "status": spec["status"],
        "schema_version": 1,
        "kind": "sprint",
        "observed_at": at(0),
        "ref": ref,
        "sprint": section("sprint", value=value),
        "observer": _observer(ref),
        "work": _sprint_work(ref),
    }


def sprint_head_view(ref: str, run_id: str) -> dict[str, Any]:
    if ref not in SPRINTS:
        raise TaskNotFound(f"the board holds no sprint {ref!r}")
    if SPRINTS[ref]["status"] != "open" or run_id != f"run-o-{_sha(ref, 6)[:6]}":
        raise HeadRunNotFound(f"sprint {ref} holds no observer head run {run_id[:80]!r}")
    return _head_document(ref, run_id, "sprint", "observer", "running")


def sprint_options() -> dict[str, Any]:
    return {
        "products": {"source": SOURCE, "items": [{"id": project} for project in PROJECTS]},
        "projects": {"source": SOURCE, "items": [
            {"id": project, "reserved_by": next((ref for ref in OPEN_SPRINTS if SPRINT_PROJECT[ref] == project), None)}
            for project in PROJECTS
        ]},
        "issues": {"source": SOURCE, "items": [
            {"ref": "issue:nimbus-cli#52", "product": "nimbus-cli", "title": "Windows installer"},
            {"ref": "issue:nimbus-cli#57", "product": "nimbus-cli", "title": "Proxy support"},
        ]},
        "heads": {"source": SOURCE, "items": [
            {"id": "observer-opus", "observer": True, "role": "observer", "label": "Observer (Opus)", "model": "claude-opus-5-5", "effort": "high"},
            *({"id": name, "role": "worker", "label": name, "model": spec["model"], "effort": spec["effort"]}
              for name, spec in HEADS.items() if name != "observer-opus"),
        ]},
    }


# -- PO --------------------------------------------------------------------------------------------

PO_SESSIONS = (
    {"session_id": "po-7a1c55e2", "title": "orbit-web storefront refresh", "cli": "claude", "model": "claude-opus-5-5",
     "effort": "max", "state": "open", "created_at": at(1600), "running": True,
     "first_message": "Let's plan the storefront refresh for the EU launch."},
    {"session_id": "po-31be09d4", "title": "acme-api enterprise launch", "cli": "codex", "model": "gpt-6.1-sol",
     "effort": "xhigh", "state": "open", "created_at": at(3100), "running": False,
     "first_message": "What has to be true before the first enterprise customer goes live?"},
)


def po_overview(*, closed: bool = False) -> dict[str, Any]:
    return {
        "models": {"claude": ["claude-opus-5-5", "claude-sonnet-5-5"], "codex": ["gpt-6-sol", "gpt-6.1-sol"]},
        "efforts": {"claude": ["default", "high", "max"], "codex": ["none", "low", "high", "xhigh"]},
        "sessions": [] if closed else [dict(item) for item in PO_SESSIONS],
        "closed": closed,
        "running": 1,
    }


def po_session(session_id: str) -> dict[str, Any]:
    session = next((dict(item) for item in PO_SESSIONS if item["session_id"] == session_id), None)
    if session is None:
        raise TaskNotFound(f"the PO store holds no session {session_id!r}")
    feed = [
        {"turn_seq": 1, "role": "owner", "text": session["first_message"], "metadata": {"source": "web"}},
        {"turn_seq": 1, "role": "agent", "text": "Here is a plan in three cards:\n\n1. **Kanban** optimistic updates\n2. First-load budget\n3. EU checkout (needs your pick of a payment provider)", "metadata": {}},
        {"turn_seq": 2, "role": "owner", "text": "Go ahead with 1 and 2; I will decide the provider tomorrow.", "metadata": {"source": "web"}},
        {"turn_seq": 2, "role": "agent", "text": "Opened sprint:42 with those cards and handed orbit-web-120 to you.", "metadata": {}},
    ]
    return {
        "session": session,
        "turns": [{"seq": 1, "state": "completed"}, {"seq": 2, "state": "running" if session["running"] else "completed"}],
        "running": session["running"],
        "running_seq": 2,
        "efforts": po_overview()["efforts"],
        "feed": feed,
        "queued": [],
    }


def po_delegated(session_id: str) -> dict[str, Any]:
    return {"kind": "po_delegated", "session": session_id, "source": SOURCE, "items": [
        {"ref": "orbit-web-120", "title": BY_REF["orbit-web-120"].title, "type": "decision", "state": "blocked", "relation": "assigned", "last_return": None},
        {"ref": "nimbus-cli-78", "title": BY_REF["nimbus-cli-78"].title, "type": "wait", "state": "done", "relation": "delegated",
         "last_return": {"status": "delivered"}},
    ]}


# -- the layers ------------------------------------------------------------------------------------


class _Refusing:
    """A layer whose every operation not defined on it is a write, and refused."""

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)

        def refuse(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeUnavailable(READ_ONLY)

        return refuse


class PreviewReads(_Refusing):
    def system_snapshot(self) -> dict[str, Any]:
        return system_snapshot()

    def task_snapshot(self, ref: str, *, events: int = 200) -> dict[str, Any]:
        snapshot = task_snapshot(ref)
        snapshot["events"]["items"] = snapshot["events"]["items"][-events:]
        return snapshot

    def task_events(self, ref: str, cursor: str | None = None, *, limit: int = 50) -> dict[str, Any]:
        task_snapshot(ref)
        return {"kind": "task_events", "observed_at": at(0), "ref": ref, "source": SOURCE, "cursor": cursor,
                "next_cursor": cursor or "c0000", "has_more": False, "items": []}

    def board_view(self, *, project: str | None = None, sprint: str | None = None) -> dict[str, Any]:
        return board_document(project, sprint)

    def board_filters(self) -> dict[str, Any]:
        return {"kind": "board_filters", "observed_at": at(0),
                "projects": {"source": SOURCE, "items": list(PROJECTS)},
                "sprints": {"source": SOURCE, "items": list(OPEN_SPRINTS)}}

    def head_view(self, ref: str, run_id: str) -> dict[str, Any]:
        return head_view(ref, run_id)

    def sprint_head_view(self, ref: str, run_id: str) -> dict[str, Any]:
        return sprint_head_view(ref, run_id)

    def po_delegated(self, session_id: str) -> dict[str, Any]:
        return po_delegated(session_id)


class PreviewOps(_Refusing):
    def card_runs(self, ref: str) -> dict[str, Any]:
        return card_runs(ref)

    def run_list(self, ref: str) -> dict[str, Any]:
        return card_runs(ref)

    def run_state(self, run_id: str) -> dict[str, Any]:
        raise TaskNotFound(f"no run {run_id[:80]!r} in the preview")


class PreviewSprintReads(_Refusing):
    def sprint_list(self, *, statuses: list[str] | None = None) -> dict[str, Any]:
        return sprint_list(statuses)

    def sprint_state(self, ref: str) -> dict[str, Any]:
        return sprint_state(ref)

    def sprint_options(self) -> dict[str, Any]:
        return sprint_options()


class PreviewPauseReads(_Refusing):
    def pause_state(self) -> dict[str, Any]:
        return pause_state()

    def pause_scope(self) -> dict[str, Any]:
        return {"kind": "pause_scope", "observed_at": at(0), "source": SOURCE, "items": []}


class PreviewCommandReads(_Refusing):
    def command_history(self, cursor: str | None = None, *, limit: int = 25) -> dict[str, Any]:
        return command_history()

    def command_request(self, request_id: str) -> dict[str, Any]:
        raise TaskNotFound(f"no request {request_id[:80]!r} in the preview")


class PreviewProviderUsage:
    def usage_snapshot(self) -> dict[str, Any]:
        return usage_snapshot()


class PreviewDoctor:
    def doctor_snapshot(self) -> dict[str, Any]:
        return doctor_snapshot()


class PreviewPo(_Refusing):
    def po_overview(self, *, closed: bool = False) -> dict[str, Any]:
        return po_overview(closed=closed)

    def po_session(self, session_id: str) -> dict[str, Any]:
        return po_session(session_id)

    def po_running_count(self) -> dict[str, Any]:
        return {"running": 1}

    def po_session_titles(self, named: list[str]) -> dict[str, Any]:
        titles = {item["session_id"]: {"title": item["title"]} for item in PO_SESSIONS}
        return {"sessions": {name: titles[name] for name in named if name in titles}}


class PreviewOwnerEvents(_Refusing):
    def owner_event_list(self, *, unread_only: bool = True) -> dict[str, Any]:
        return owner_event_list(unread_only=unread_only)

    def unread_count(self) -> dict[str, Any]:
        return {"state": "available", "reason": None, "count": 2}


#: Every GET page the preview is meant to show, in the order a person would visit them.
PAGES: tuple[str, ...] = (
    "/",
    "/board",
    "/board?project=acme-api",
    "/board?sprint=sprint:41",
    "/sprints",
    "/sprints?view=archive",
    "/sprints/sprint:41",
    "/sprints/sprint:42",
    "/sprints/sprint:40",
    "/sprints/new",
    f"/tasks/{SHOWCASE}",
    f"/tasks/{SHOWCASE}/heads/run-w-8d04a7",
    "/tasks/acme-api-226",
    "/tasks/orbit-web-120",
    "/tasks/nimbus-cli-78",
    "/tasks/orbit-web-110",
    "/projects",
    "/projects/acme-api",
    "/history",
    "/doctor",
    "/owner-events",
    "/po",
    "/po/sessions/po-7a1c55e2",
)
