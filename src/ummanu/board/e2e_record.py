"""A code card's e2e record: every workflow run the dispatcher dispatched for it (secretary-1795).

A project adapter may declare an e2e check (`validation.e2e`, `dispatch/e2e.py`). The dispatcher then
dispatches that GitHub Actions workflow on a `code` card's candidate and waits for the run through a
`wait` card. What it knows about each run lives on the code card itself, in one typed field of its
extension bag (`extensions.extra`, docs/BOARD_STORE.md §8.2), JSON text, no column:

- `e2e`: `{"runs": [...]}`, one record per dispatch, oldest first. Only the dispatcher writes it
  (`TaskWriter.record_e2e_state`), and it rewrites it only when what it knows changed.

A run record is written as an **intent** (card, SHA, dispatch id) before the workflow is dispatched,
so a dispatcher that dies between the intent and the call looks the run up instead and never
dispatches a second one. It then gains the run (from GitHub's dispatch answer, or that lookup), the SHA
the run was checked to run on, the wait card that waits for it, the
wait's frozen result, and, when the result Blocked the card, the request id of that Blocked move
(`closing`): once the move is committed that run's pass is over, and a card brought back to the same
SHA after an unblock may spend a new run on it.

The count of runs dispatched for a card is the number of its records: a run counts when its intent is
persisted, whatever GitHub answered, as a sprint charges it. It is durable because the records are. A card of a sprint spends the sprint's e2e run budget, charged at
each intent (`board/e2e_budget.py`, secretary-1796); a card outside every sprint is bounded by its own
cap, :data:`E2E_RUN_CAP` plus every raise the owner authorized.

A card that reached the stage with the budget spent carries `budget_wait` (`{decision, generation,
scope, since}`): the decision card it waits on, and the budget (or cap) the runs were spent against.
`task show` says `e2e: budget spent, waiting on <decision>`.

**After merge** (secretary-1807, `dispatch/e2e_after_merge.py`). A project whose adapter declares
`placement: after_merge` runs its e2e workflow on `main` after the merge, once for every card merged
since the last run. Two more keys of the same field carry it:

- `after_merge`, on every card that merged with green post-merge CI (:class:`AfterMergeMark`): its merge
  SHA, where it stands (`pending`, `covered` by a run, `green`, `red` with the hotfix card, `budget_wait`
  on a decision, `declined`), the run that covers it, the card that carries that run, and the dispatch ids
  charged to this card's own cap when no open sprint paid (they count in :attr:`E2eState.dispatched`);
- `after_merge_runs`, on the newest card a run covers (its *carrier*): the run records, each an
  :class:`E2eRun` with `placement: after_merge`, the covered cards with their merge SHAs, and the
  dispatcher-owned branch the run was dispatched on.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from ummanu.board import e2e_budget
from ummanu.board.extension_bag import EXTENSION_BAG

E2E_FIELD = "e2e"

#: The bound on runs one card outside every sprint may dispatch across all its SHAs, before a raise.
E2E_RUN_CAP = e2e_budget.CARD_E2E_CAP

#: A run record's dispatch status: the intent is on the card and the call was not confirmed (it may
#: or may not have reached GitHub), GitHub accepted it, or GitHub refused it.
INTENT = "intent"
SENT = "sent"
REFUSED = "refused"
_DISPATCH_STATES = (INTENT, SENT, REFUSED)

#: The one run conclusion that lets the card proceed, and the one that returns it to rework.
SUCCESS = "success"
FAILURE = "failure"

#: Where a project's e2e stage runs: on the candidate before the merge (the default), or on `main`
#: after it (secretary-1807).
BEFORE_MERGE = "before_merge"
AFTER_MERGE = "after_merge"
PLACEMENTS = (BEFORE_MERGE, AFTER_MERGE)

#: Where a card stands in its project's after-merge e2e.
AM_PENDING = "pending"
AM_COVERED = "covered"
AM_GREEN = "green"
AM_RED = "red"
AM_BUDGET_WAIT = "budget_wait"
AM_DECLINED = "declined"

#: The resolution of an after-merge run once its result was acted on: green, red (a hotfix card), a
#: conclusion or wait outcome that is neither (`requeued`), or a run that was never attached to the
#: covered cards (`blocked`: refused, unidentified, ambiguous, or run on another SHA). The last two send
#: planned waits return to the pending set; uncovered uncertain outcomes wait on PO disposition.
AM_REQUEUED = "requeued"
AM_BLOCKED = "blocked"
_AM_STATES = (AM_PENDING, AM_COVERED, AM_GREEN, AM_RED, AM_BUDGET_WAIT, AM_DECLINED, AM_BLOCKED)

#: The request-id prefix of the one `code` card the dispatcher may create: the hotfix of a red
#: after-merge run, `dispatcher-e2e-am-hotfix-<dispatch id>` (`dispatch/e2e_after_merge.py`).
AFTER_MERGE_HOTFIX_REQUEST_PREFIX = "dispatcher-e2e-am-hotfix-"


@dataclass
class E2eRun:
    """One dispatch of the declared workflow for one candidate SHA."""

    dispatch_id: str
    sha: str
    repo: str
    branch: str
    workflow: str
    intent_at: str
    # The adapter's deadline as it read at the intent: the wait card's create repeats it unchanged.
    deadline: str = ""
    dispatch: str = INTENT
    dispatch_detail: str = ""
    run_id: int = 0
    run_url: str = ""
    # The SHA GitHub says the run ran on, once checked against `sha`; empty until then.
    head_sha: str = ""
    # How the run was named: `answer` (GitHub's dispatch answer) or `recovery` (the lookup after the
    # answer was lost), and for `recovery` the rule it matched.
    identified_by: str = ""
    recovery_rule: str = ""
    wait_ref: str = ""
    # {outcome, conclusion, summary, evidence, key}: the wait card's frozen result, copied once.
    result: dict[str, str] | None = None
    # The request id of the Blocked move this run's result (or its dispatch) ended in, and its reason,
    # written before the move so a repeat after a crash moves with the same id and the same words.
    closing: str = ""
    closing_reason: str = ""
    # The result was acted on (the card proceeded, went to rework or was Blocked on it).
    acted: bool = False
    # Base-only moves this green run was carried across (`reconcile_reviewed_base_move`'s record).
    reconciled: list[dict[str, Any]] = field(default_factory=list)
    # After merge only (secretary-1807): `after_merge`; the covered cards `{ref, merge_sha}`; the
    # dispatcher-owned branch the run was dispatched on and whether it is `created` or `deleted`; what
    # paid for it (a sprint, or `cards`: each covered card's own cap); how its result was acted on
    # (`green`, `red`, `requeued`) and the hotfix card a red one created.
    placement: str = ""
    covered: list[dict[str, str]] = field(default_factory=list)
    git_ref: str = ""
    git_ref_state: str = ""
    charged_to: str = ""
    resolution: str = ""
    hotfix: str = ""
    # The PO card owning an unresolved result/return-route question, distinct from the code hotfix.
    disposition: str = ""
    # Applied native completion receipt. Marks and this receipt commit together;
    # retry pending recovery reads the marks, even after the project queue vanished.
    disposition_result: dict[str, str] | None = None

    @property
    def conclusion(self) -> str:
        return str((self.result or {}).get("conclusion") or "")

    @property
    def green(self) -> bool:
        return (
            self.result is not None
            and self.result.get("outcome") == "target_reached"
            and (self.conclusion == SUCCESS)
        )

    def status(self) -> str:
        """One word for `task show`."""
        if self.dispatch == REFUSED:
            return "dispatch_refused"
        if self.result is not None:
            outcome = str(self.result.get("outcome") or "")
            return (self.conclusion or "no_conclusion") if outcome == "target_reached" else outcome
        if not self.run_id or not self.head_sha:
            return "identifying" if self.dispatch == SENT or self.run_id else "dispatching"
        return "waiting" if self.wait_ref else "wait_card_pending"

    @classmethod
    def from_json(cls, payload: Any) -> E2eRun | None:
        if not isinstance(payload, Mapping):
            return None
        texts = {
            name: str(payload.get(name) or "")
            for name in (
                "dispatch_id",
                "sha",
                "repo",
                "branch",
                "workflow",
                "intent_at",
                "deadline",
                "dispatch",
                "dispatch_detail",
                "run_url",
                "head_sha",
                "identified_by",
                "recovery_rule",
                "wait_ref",
                "closing",
                "closing_reason",
                "placement",
                "git_ref",
                "git_ref_state",
                "charged_to",
                "resolution",
                "hotfix",
                "disposition",
            )
        }
        if not (texts["dispatch_id"] and texts["sha"]) or texts["dispatch"] not in _DISPATCH_STATES:
            return None
        run_id = payload.get("run_id")
        result = payload.get("result")
        reconciled = payload.get("reconciled")
        covered = payload.get("covered")
        return cls(
            **texts,
            acted=payload.get("acted") is True,
            covered=[
                {"ref": str(item.get("ref") or ""), "merge_sha": str(item.get("merge_sha") or "")}
                for item in covered
                if isinstance(item, Mapping) and item.get("ref")
            ]
            if isinstance(covered, list)
            else [],
            reconciled=[dict(item) for item in reconciled if isinstance(item, Mapping)]
            if isinstance(reconciled, list)
            else [],
            run_id=run_id if isinstance(run_id, int) and not isinstance(run_id, bool) and run_id > 0 else 0,
            result=(
                {str(key): str(value) for key, value in result.items()}
                if isinstance(result, Mapping) and result.get("outcome")
                else None
            ),
            disposition_result=(dict(payload["disposition_result"])
                                if isinstance(payload.get("disposition_result"), Mapping) else None),
        )


@dataclass
class BudgetWait:
    """A card waiting on the decision its spent e2e budget needs."""

    decision: str
    # The budget (a sprint's) or the cap (a card's) the runs were spent against: its decision's key.
    generation: int
    # `sprint` or `card`: whose budget is spent.
    scope: str
    since: str

    @property
    def mark(self) -> str:
        return f"e2e: budget spent, waiting on {self.decision}"

    @classmethod
    def from_json(cls, payload: Any) -> BudgetWait | None:
        if not isinstance(payload, Mapping) or not payload.get("decision"):
            return None
        generation = payload.get("generation")
        if isinstance(generation, bool) or not isinstance(generation, int):
            return None
        return cls(
            decision=str(payload["decision"]),
            generation=generation,
            scope=str(payload.get("scope") or ""),
            since=str(payload.get("since") or ""),
        )


@dataclass
class AfterMergeMark:
    """Where one merged card stands in its project's after-merge e2e (secretary-1807)."""

    merge_sha: str
    state: str = AM_PENDING
    # The run that covers (or covered) it, and the card that carries that run's record.
    dispatch_id: str = ""
    run_url: str = ""
    carrier: str = ""
    hotfix: str = ""
    # The budget decision it waits on (`budget_wait`), or the one that declined it.
    decision: str = ""
    # The last outcome that sent it back to the pending set, or why it was declined.
    note: str = ""
    # Dispatch ids charged to this card's own cap (no open sprint paid for them).
    charged: list[str] = field(default_factory=list)
    # None reads the released decision-as-holder format. Empty explicitly means
    # historical decision only; otherwise the live operation/follow-up holder.
    holder: str | None = None

    def label(self) -> str:
        """The one line `task show` gives: pending, covered by <run>, green, red -> <hotfix card>."""
        run = self.run_url or self.dispatch_id
        if self.state == AM_COVERED:
            return f"covered by {run}"
        if self.state == AM_RED:
            return f"red -> {self.hotfix or '(hotfix card pending)'}"
        if self.state == AM_BUDGET_WAIT:
            return f"e2e: budget spent, waiting on {self.decision}"
        return self.state

    @classmethod
    def from_json(cls, payload: Any) -> AfterMergeMark | None:
        if not isinstance(payload, Mapping) or not payload.get("merge_sha"):
            return None
        state = str(payload.get("state") or AM_PENDING)
        charged = payload.get("charged")
        return cls(
            merge_sha=str(payload["merge_sha"]),
            state=state if state in _AM_STATES else AM_PENDING,
            **{
                name: str(payload.get(name) or "")
                for name in ("dispatch_id", "run_url", "carrier", "hotfix", "decision", "note")
            },
            charged=[str(item) for item in charged if str(item)] if isinstance(charged, list) else [],
            holder=str(payload["holder"]) if payload.get("holder") is not None else None,
        )


@dataclass
class HotfixRoute:
    """The actual hotfix's copy of its carrier's native disposition receipt."""

    carrier: str
    run: str
    result: dict[str, str]

    @classmethod
    def from_json(cls, value: Any) -> HotfixRoute | None:
        if not isinstance(value, Mapping) or set(value) != {"carrier", "run", "result"}:
            return None
        result = value.get("result")
        if (not all(isinstance(value.get(key), str) and value[key] for key in ("carrier", "run"))
                or not isinstance(result, Mapping)
                or not {"operation", "status", "action", "holder", "reason"} <= set(result)
                or set(result) - {"operation", "status", "action", "holder", "reason", "completion"}
                or any(not isinstance(item, str) for item in result.values())
                or result.get("status") not in {"waiting", "neutral", "follow_up", "settled"}
                or result.get("action") not in {"", "retry", "decline", "follow_up"}
                or not result.get("operation") or not result.get("reason")):
            return None
        return cls(value["carrier"], value["run"], dict(result))


@dataclass
class E2eState:
    """Every run record of one card, as its `e2e` field holds them."""

    runs: list[E2eRun] = field(default_factory=list)
    budget_wait: BudgetWait | None = None
    after_merge: AfterMergeMark | None = None
    after_merge_runs: list[E2eRun] = field(default_factory=list)
    # Decision identity and exact Blocked occurrence; cleared by every other transition.
    budget_decline: dict[str, str] | None = None
    hotfix_route: HotfixRoute | None = None

    @property
    def dispatched(self) -> int:
        """Runs dispatched for this card, across all its SHAs: every persisted intent, whatever GitHub
        answered. A run counts when its intent is written, before the POST, as a sprint charges it
        (secretary-1796); the per-card cap and `task show` read this count. An after-merge run that no
        open sprint paid for counts on every card it covers (secretary-1807)."""
        return len(self.runs) + (len(self.after_merge.charged) if self.after_merge is not None else 0)

    def after_merge_run(self, dispatch_id: str) -> E2eRun | None:
        """The after-merge run this card carries under that dispatch id, or None."""
        return next((run for run in self.after_merge_runs if run.dispatch_id == dispatch_id), None)

    def latest(self, sha: str) -> E2eRun | None:
        """The newest record for this SHA, or None."""
        return next((run for run in reversed(self.runs) if run.sha == sha), None)

    def green(self, sha: str) -> E2eRun | None:
        """A run that concluded `success` on exactly this SHA, or None."""
        return next((run for run in reversed(self.runs) if run.sha == sha and run.green), None)

    def last_green(self) -> E2eRun | None:
        """The newest run that concluded `success`, on whatever SHA, or None."""
        return next((run for run in reversed(self.runs) if run.green), None)

    def to_json(self) -> dict[str, Any]:
        document: dict[str, Any] = {"runs": [asdict(run) for run in self.runs]}
        if self.budget_decline is not None:
            document["budget_decline"] = dict(self.budget_decline)
        if self.hotfix_route is not None:
            document["hotfix_route"] = asdict(self.hotfix_route)
        if self.budget_wait is not None:
            document["budget_wait"] = asdict(self.budget_wait)
        if self.after_merge is not None:
            document["after_merge"] = asdict(self.after_merge)
        if self.after_merge_runs:
            document["after_merge_runs"] = [asdict(run) for run in self.after_merge_runs]
        return document

    def text(self) -> str:
        return json.dumps(self.to_json(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, payload: Any) -> E2eState:
        runs = payload.get("runs") if isinstance(payload, Mapping) else None
        parsed = [E2eRun.from_json(run) for run in runs] if isinstance(runs, list) else []
        mapping = payload if isinstance(payload, Mapping) else {}
        waiting = BudgetWait.from_json(mapping.get("budget_wait"))
        mark = AfterMergeMark.from_json(mapping.get("after_merge"))
        raw_after = mapping.get("after_merge_runs")
        after = [E2eRun.from_json(run) for run in raw_after] if isinstance(raw_after, list) else []
        decline = mapping.get("budget_decline")
        if not isinstance(decline, Mapping) or set(decline) != {"decision", "request_id"} or any(
            not isinstance(value, str) or not value.strip() for value in decline.values()
        ):
            decline = None
        return cls(
            [run for run in parsed if run is not None],
            waiting,
            mark,
            [run for run in after if run is not None],
            dict(decline) if decline is not None else None,
            HotfixRoute.from_json(mapping.get("hotfix_route")),
        )


def _json_field(task: Mapping[str, Any]) -> Any:
    extensions = task.get("extensions")
    bag = extensions.get(EXTENSION_BAG) if isinstance(extensions, Mapping) else None
    raw = bag.get(E2E_FIELD) if isinstance(bag, Mapping) else None
    if isinstance(raw, Mapping):
        return raw
    try:
        return json.loads(str(raw or ""))
    except ValueError:
        return None


def e2e_state(task: Mapping[str, Any]) -> E2eState:
    """The card's run records; a field that does not parse reads as none."""
    return E2eState.from_json(_json_field(task))


def e2e_view(task: Mapping[str, Any]) -> dict[str, Any] | None:
    """The `e2e` block `task show` carries, or None for a card that never reached the stage.

    A card of a sprint spends the sprint's budget (`budget: <sprint>`, no `run_cap`); a card outside
    every sprint has its own cap. A card waiting on a budget decision carries `mark`.
    """
    state = e2e_state(task)
    if (
        not state.runs
        and state.budget_wait is None
        and state.after_merge is None
        and not state.after_merge_runs
        and state.budget_decline is None
        and state.hotfix_route is None
    ):
        return None
    sprint = str(task.get("sprint") or "")
    after_merge = _after_merge_view(state)
    return {
        **after_merge,
        "runs_dispatched": state.dispatched,
        "run_cap": None if sprint else e2e_budget.card_cap(task),
        "budget": sprint or None,
        **({"hotfix_route": asdict(state.hotfix_route)} if state.hotfix_route else {}),
        **({"declined_by": state.budget_decline["decision"]} if state.budget_decline else {}),
        **(
            {"mark": state.budget_wait.mark, "waiting_on": state.budget_wait.decision}
            if state.budget_wait is not None
            else {}
        ),
        "runs": [
            {
                "sha": run.sha,
                "dispatch_id": run.dispatch_id,
                "workflow": run.workflow,
                "state": run.status(),
                "run": run.run_url or None,
                "identified_by": run.identified_by or None,
                **({"recovery_rule": run.recovery_rule} if run.recovery_rule else {}),
                **(
                    {"reconciled_to": [item.get("head_sha") for item in run.reconciled]}
                    if run.reconciled
                    else {}
                ),
                "wait_card": run.wait_ref or None,
                "dispatched_at": run.intent_at,
                "result": (
                    {key: run.result.get(key) for key in ("outcome", "conclusion", "summary")}
                    if run.result is not None
                    else None
                ),
                **({"detail": run.dispatch_detail} if run.dispatch_detail else {}),
            }
            for run in state.runs
        ],
    }


def _after_merge_view(state: E2eState) -> dict[str, Any]:
    """The after-merge half of the `e2e` block: `placement`, where the card stands, and the run link."""
    mark = state.after_merge
    if mark is None and not state.after_merge_runs:
        return {}
    view: dict[str, Any] = {"placement": AFTER_MERGE}
    if mark is not None:
        view.update(
            {
                "state": mark.label(),
                "merge_sha": mark.merge_sha,
                "run": mark.run_url or None,
                "covered_by": mark.dispatch_id or None,
                "carrier": mark.carrier or None,
                **({"hotfix": mark.hotfix} if mark.hotfix else {}),
                **({"decision": mark.decision} if mark.decision else {}),
                **({"note": mark.note} if mark.note else {}),
                **(
                    {"mark": mark.note or mark.label(), "waiting_on": mark.holder if mark.holder is not None else mark.decision}
                    if (mark.holder if mark.holder is not None else mark.decision)
                    and mark.state in {AM_BUDGET_WAIT, AM_RED, AM_BLOCKED}
                    else {}
                ),
            }
        )
    if state.after_merge_runs:
        view["after_merge_runs"] = [
            {
                "dispatch_id": run.dispatch_id,
                "sha": run.sha,
                "workflow": run.workflow,
                "ref": run.git_ref,
                "ref_state": run.git_ref_state or None,
                "covered": [dict(item) for item in run.covered],
                "charged_to": run.charged_to or None,
                "state": run.resolution or run.status(),
                "run": run.run_url or None,
                "wait_card": run.wait_ref or None,
                "dispatched_at": run.intent_at,
                **({"hotfix": run.hotfix} if run.hotfix else {}),
                **({"disposition": run.disposition} if run.disposition else {}),
                **({"disposition_result": dict(run.disposition_result)} if run.disposition_result is not None else {}),
                **({"reason": run.closing_reason} if run.closing_reason else {}),
                "result": (
                    {key: run.result.get(key) for key in ("outcome", "conclusion", "summary")}
                    if run.result is not None
                    else None
                ),
            }
            for run in state.after_merge_runs
        ]
    return view


__all__ = [
    "AFTER_MERGE",
    "AFTER_MERGE_HOTFIX_REQUEST_PREFIX",
    "AM_BLOCKED",
    "AM_BUDGET_WAIT",
    "AM_COVERED",
    "AM_DECLINED",
    "AM_GREEN",
    "AM_PENDING",
    "AM_RED",
    "AM_REQUEUED",
    "BEFORE_MERGE",
    "E2E_FIELD",
    "E2E_RUN_CAP",
    "FAILURE",
    "INTENT",
    "PLACEMENTS",
    "REFUSED",
    "SENT",
    "SUCCESS",
    "AfterMergeMark",
    "BudgetWait",
    "E2eRun",
    "E2eState",
    "e2e_state",
    "e2e_view",
]
