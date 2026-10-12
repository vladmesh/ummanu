"""The post-merge CI watch: a release is finished when the base's CI for its merge has answered.

A release merge starts a CI run on the integration base. The dispatcher owns all waiting for CI, so
it records a watch the moment the merge has landed (card ref, project, base, the merge commit that
actually landed, start time) in its own production state, beside the card records and never among
them: a watched card is Done and holds no claim. Each tick reads the base's CI for that exact commit
with the gate's own machinery and resolves the watch to exactly one of four results:

- `green`: every selected check finished successfully;
- `red`: at least one failed, with the run ids, failed check names and the gate's
  infrastructure/product classification;
- `absent`: the project validates without GitHub CI, or no workflow triggers on a push to the base,
  decided when the watch is opened, without waiting;
- `timeout`: no terminal result within `UMMANU_POST_MERGE_CI_CEILING_SECONDS` (default 3600).

`read_merge_ci` is the one place the answers become green or red, and only from a reading that is
complete and exact: every answer well-formed, every check run and the status naming the full merge
commit, all check-run pages read up to `total_count`, every required check present. Anything else
(a transport error, an unreadable or malformed `gh` answer, a partial page set, a check for another
commit, a completed check with no conclusion, a run that has not started) is pending until the
ceiling, whatever the other answer says.

The result is written to the watch before anything is published, so a replayed tick publishes the
same fact under the same request ids: one card event (the observer wake, see
`ummanu.tasks.is_significant_card_event`) and, for a sprint card, one dispatcher comment on the
sprint. The watch is dropped only after both are on the board. A `green` watch of a project whose e2e
stage is declared `placement: after_merge` queues its card for that project's next after-merge e2e run
(`dispatch/e2e_after_merge.py`); no other result queues anything. Such a watch is dropped only once the
card is queued, or its project is established not to be `after_merge`: while the adapter or its
declaration cannot be read it is kept, marked `published` so its fact is not published again, and the
next pass retries the enqueue alone.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ummanu.dispatch.gate import (
    _actions_run_id,
    _backend_call,
    _check_name,
    _check_result,
    _failed_log,
    _name_with_owner,
    _required_checks,
    _rollup,
    _selected_checks,
    push_trigger_absent_reason,
)
from ummanu.dispatch.helpers import safe_one_line
from ummanu.dispatch.types import HostError, MergeLanding
from ummanu.infra.env import positive_int
from ummanu.tasks import POST_MERGE_CI_KEY, TaskError, post_merge_ci_fact

WATCHES_KEY = "post_merge_watches"
CEILING_ENV = "UMMANU_POST_MERGE_CI_CEILING_SECONDS"
DEFAULT_CEILING_SECONDS = 3600
#: How many failed runs have their logs read for the classification; the rest are listed only.
_CLASSIFIED_RUNS = 3
_SHA_RE = re.compile(r"[0-9a-f]{40}")


def ceiling_seconds() -> int:
    return positive_int(CEILING_ENV, DEFAULT_CEILING_SECONDS)


def watches(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The live watches in the dispatcher production state, created on first use."""
    raw = payload.get(WATCHES_KEY)
    if not isinstance(raw, dict):
        raw = {}
        payload[WATCHES_KEY] = raw
    return raw


def pr_merge_commit(run: Callable[..., Any], branch: str, cwd: Path) -> str:
    """The merge commit of `branch`'s pull request, or "" when it cannot be read.

    Only a full 40-hex commit is an answer; anything else is "not known yet".
    """
    if not branch:
        return ""
    try:
        completed = run(
            ["gh", "pr", "view", branch, "--json", "mergeCommit", "-q", ".mergeCommit.oid"],
            "post-merge commit",
            cwd=cwd,
        )
    except (HostError, OSError):
        return ""
    sha = str(getattr(completed, "stdout", "") or "").strip()
    return sha if _SHA_RE.fullmatch(sha) else ""


def open_watch(
    task: dict[str, Any],
    payload: dict[str, Any],
    landing: MergeLanding,
    *,
    workspace: str,
    now: float | None = None,
) -> dict[str, Any]:
    """Record the watch for a merge that has just landed; a replay keeps the first one.

    The absence of any base CI is decided here, from the merged checkout, while it still exists:
    the checkout is torn down before Done.
    """
    ref = task["ref"]
    live = watches(payload)
    existing = live.get(ref)
    if isinstance(existing, dict) and not existing.get("result"):
        if not existing.get("merge_sha") and landing.sha:
            existing["merge_sha"] = landing.sha
        return existing
    if landing.ci != "github":
        absent = (
            f"the project validates with `validation.ci = {landing.ci}`, not GitHub CI, so the "
            "merge starts no CI run the dispatcher could read"
        )
    else:
        absent = push_trigger_absent_reason(workspace, landing.base) if workspace else ""
    started = time.time() if now is None else now
    watch = {
        "version": 1,
        "id": uuid.uuid4().hex[:16],
        "ref": ref,
        "project": str(task.get("project") or ""),
        "sprint": str(task.get("sprint") or ""),
        "base": landing.base,
        "merge_sha": landing.sha,
        "merge_path": landing.path,
        "branch": landing.branch,
        "ci": landing.ci,
        "started_at": started,
        "absent_reason": absent,
        "repo": "",
        "last_state": "",
        "result": None,
    }
    live[ref] = watch
    return watch


def release_merge_marker(watch: dict[str, Any]) -> dict[str, Any]:
    """The Done transition's `release_merge` data: what landed, and that its CI result follows."""
    return {
        "base": str(watch.get("base") or ""),
        "merge_sha": str(watch.get("merge_sha") or ""),
        "path": str(watch.get("merge_path") or ""),
    }


def reconcile_post_merge_watches(
    runtime: Any,
    payload: dict[str, Any],
    records: dict[str, Any],
    *,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Resolve and publish every watch that has an answer this tick."""
    live = watches(payload)
    outcomes: list[dict[str, Any]] = []
    for ref in sorted(live):
        watch = live[ref]
        if not isinstance(watch, dict):
            del live[ref]
            continue
        if not watch.get("result"):
            result = resolve(runtime, watch, now=now)
            if result is None:
                continue
            # Durable before it is published: a replay publishes this fact, never a re-read one.
            watch["result"] = result
            runtime.save_records(payload, records)
        if not watch.get("published"):
            try:
                publish(runtime, watch)
            except (TaskError, HostError, OSError, ValueError, TypeError) as exc:
                outcomes.append(
                    {
                        "status": "degraded",
                        "step": "post-merge-ci",
                        "action": "post-merge-ci-publish-failed",
                        "pilot_ref": ref,
                        "reason": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            watch["published"] = True
        # A green merge of a project whose e2e runs after the merge joins that project's pending set
        # (secretary-1807). The watch outlives an enqueue that could not establish the project's
        # placement: it is kept, published, and the next pass retries the enqueue alone.
        from ummanu.dispatch.e2e_after_merge import enqueue

        queued = enqueue(runtime, payload, watch)
        if queued.outcome is not None:
            outcomes.append(queued.outcome)
        # The queued card is saved before the watch is dropped; a replay of a save lost in between
        # queues it once (`enqueue` is idempotent per card and merge SHA).
        runtime.save_records(payload, records)
        if not queued.settled:
            continue
        del live[ref]
        runtime.save_records(payload, records)
        outcomes.append(
            {
                "status": "ok",
                "step": "post-merge-ci",
                "pilot_ref": ref,
                "result": watch["result"]["result"],
                "merge_sha": watch["result"].get("merge_sha", ""),
            }
        )
    return outcomes


def resolve(runtime: Any, watch: dict[str, Any], *, now: float | None = None) -> dict[str, Any] | None:
    """The watch's result, or None while it is pending."""
    current = time.time() if now is None else now
    waited = max(0, int(current - float(watch.get("started_at") or current)))
    if watch.get("absent_reason"):
        return _fact(watch, "absent", waited, reason=str(watch["absent_reason"]))
    read = _read_ci(runtime, watch)
    if read is not None:
        return {**read, **_fact(watch, read["result"], waited)}
    ceiling = ceiling_seconds()
    if waited >= ceiling:
        return _fact(
            watch,
            "timeout",
            waited,
            reason=(
                f"no terminal CI result for the merge within {ceiling // 60} min "
                f"(last seen: {watch.get('last_state') or 'nothing'})"
            ),
            runs=list(watch.get("last_runs") or []),
        )
    return None


def _fact(
    watch: dict[str, Any],
    result: str,
    waited: int,
    *,
    reason: str = "",
    runs: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    fact: dict[str, Any] = {
        "result": result,
        "card": str(watch.get("ref") or ""),
        "base": str(watch.get("base") or ""),
        "merge_sha": str(watch.get("merge_sha") or ""),
        "waited_seconds": waited,
    }
    if reason:
        fact["reason"] = safe_one_line(reason)
    if runs is not None:
        fact["runs"] = runs
    return fact


def _repo_dir(runtime: Any, project: str) -> Path | None:
    try:
        return Path(str(runtime.catalog.binding(project)["repo"])).expanduser()
    except (KeyError, TypeError, ValueError, AttributeError, HostError):
        return None


def _read_ci(runtime: Any, watch: dict[str, Any]) -> dict[str, Any] | None:
    """A terminal green/red reading of the base CI for the merge commit, or None (pending).

    This only asks: the answers are judged by `read_merge_ci` and nowhere else. A question that
    cannot even be put (no merge commit yet, no repository name) is pending too.
    """
    host = runtime.host
    project = str(watch.get("project") or "")
    repo_dir = _repo_dir(runtime, project)
    try:
        if not watch.get("merge_sha"):
            sha = pr_merge_commit(host._run, str(watch.get("branch") or ""), repo_dir or Path("."))
            if not sha:
                watch["last_state"] = "the merge commit is not readable yet"
                return None
            watch["merge_sha"] = sha
        sha = str(watch["merge_sha"])
        if not watch.get("repo"):
            if repo_dir is None:
                watch["last_state"] = "the project checkout is not registered"
                return None
            watch["repo"] = _name_with_owner(host, str(repo_dir))
        repo = str(watch["repo"])
        required = _required_checks(host, {"project": project})
        pages, status = _fetch_answers(host, repo, sha)
    except (HostError, OSError, ValueError, TypeError, AttributeError) as exc:
        watch["last_state"] = safe_one_line(f"CI unreadable: {exc}", limit=300)
        return None
    reading = read_merge_ci(pages, status, sha, required)
    runs = _runs(repo, list(reading.checks))
    if runs:
        watch["last_runs"] = runs
    watch["last_state"] = reading.reason or reading.result
    if reading.result == "pending":
        return None
    if reading.result == "red":
        classification, reasons = _classify(host, repo, list(reading.failed))
        return {
            "result": reading.result,
            "runs": runs,
            "failed_checks": sorted({safe_one_line(_check_name(item)) or "?" for item in reading.failed}),
            "classification": classification,
            **({"classification_reason": ", ".join(reasons)} if reasons else {}),
        }
    return {"result": reading.result, "runs": runs}


#: Check-runs are read in pages of this size, at most this many pages; a set larger than that
#: never reads complete and times out rather than being judged on a part.
_PAGE_SIZE = 100
_MAX_PAGES = 20


def _answer(host: Any, path: str) -> str | None:
    """One raw `gh api` answer, or None when no answer came (transport, 5xx, 404, any non-zero)."""
    try:
        completed = _backend_call(host, ["gh", "api", path], "post-merge gh api")
    except HostError:
        return None
    if completed.returncode != 0:
        return None
    return str(completed.stdout or "")


def _json_object(text: str | None) -> dict[str, Any] | None:
    if text is None:
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _fetch_answers(host: Any, repo: str, sha: str) -> tuple[list[str | None], str | None]:
    """Every check-runs page up to the answer's own `total_count`, and the combined status.

    Only the paging is decided here: a page that is not a usable object stops the reading, and
    `read_merge_ci` then finds it short.
    """
    pages: list[str | None] = []
    read = 0
    for page in range(1, _MAX_PAGES + 1):
        text = _answer(host, f"repos/{repo}/commits/{sha}/check-runs?per_page={_PAGE_SIZE}&page={page}")
        pages.append(text)
        parsed = _json_object(text)
        items = parsed.get("check_runs") if parsed is not None else None
        total = _count(parsed.get("total_count")) if parsed is not None else None
        if not isinstance(items, list) or total is None or not items:
            break
        read += len(items)
        if read >= total:
            break
    status = _answer(host, f"repos/{repo}/commits/{sha}/status?per_page={_PAGE_SIZE}")
    return pages, status


@dataclass(frozen=True)
class CiReading:
    """What `read_merge_ci` made of one tick's answers."""

    result: str  # "green" | "red" | "pending"
    reason: str = ""
    # Every well-formed check seen, for the run list; the selected ones on a terminal reading.
    checks: tuple[dict[str, Any], ...] = ()
    failed: tuple[dict[str, Any], ...] = ()


_STATUS_STATES = {"success", "failure", "error", "pending"}


def read_merge_ci(
    check_run_pages: list[str | None], status_answer: str | None, sha: str, required: list[str]
) -> CiReading:
    """The one place the post-merge watch turns raw `gh` answers for the merge commit into a result.

    `green` or `red` only from a reading that is complete and exact:

    - every check-runs page and the combined status answered with a JSON object of the expected
      shape (`total_count` and `check_runs`; `sha`, `total_count` and `statuses`);
    - every check run (`head_sha`) and the status object (`sha`) name exactly the full merge commit;
    - the check runs read across all pages add up to the answer's `total_count`, and so do the
      statuses;
    - every declared required check is present.

    If any one of these fails the whole reading is pending, whatever the other answer says. Only
    then is the gate's `_rollup` asked, and only here: `SUCCESS` is green, `FAILURE` red, anything
    else pending.
    """

    def pending(reason: str, seen: list[dict[str, Any]] | None = None) -> CiReading:
        return CiReading("pending", reason, tuple(seen or ()))

    if not _SHA_RE.fullmatch(sha):
        return pending("the merge commit is not a full commit id")
    if not check_run_pages:
        return pending("no check-runs answer")
    runs: list[dict[str, Any]] = []
    total: int | None = None
    for number, text in enumerate(check_run_pages, start=1):
        page = _json_object(text)
        items = page.get("check_runs") if page is not None else None
        count = _count(page.get("total_count")) if page is not None else None
        if page is None or not isinstance(items, list) or count is None:
            return pending(f"check-runs page {number} is not a readable answer", runs)
        if total is not None and count != total:
            return pending("check-runs total_count changed between pages", runs)
        total = count
        for item in items:
            if not isinstance(item, dict):
                return pending("a check run is not an object", runs)
            runs.append(item)
    if len(runs) != total:
        return pending(f"read {len(runs)} of {total} check run(s)", runs)
    for item in runs:
        if item.get("head_sha") != sha:
            return pending("a check run does not name the merge commit", runs)
        status = item.get("status")
        if not isinstance(item.get("name"), str) or not item["name"].strip() or not isinstance(status, str):
            return pending("a check run has no name or status", runs)
        conclusion = item.get("conclusion")
        if status.lower() == "completed" and (not isinstance(conclusion, str) or not conclusion):
            return pending(f"check run {safe_one_line(item['name'])} completed without a conclusion", runs)
    combined = _json_object(status_answer)
    statuses = combined.get("statuses") if combined is not None else None
    count = _count(combined.get("total_count")) if combined is not None else None
    if combined is None or not isinstance(statuses, list) or count is None:
        return pending("the combined status is not a readable answer", runs)
    if combined.get("sha") != sha:
        return pending("the combined status does not name the merge commit", runs)
    if count != len(statuses):
        return pending(f"read {len(statuses)} of {count} commit status(es)", runs)
    for item in statuses:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("context"), str)
            or not item["context"].strip()
            or str(item.get("state")) not in _STATUS_STATES
        ):
            return pending("a commit status has no context or state", runs)
    checks = runs + statuses
    present = {_check_name(item) for item in checks}
    missing = [name for name in required if name not in present]
    if missing:
        return pending("required check(s) not posted: " + ", ".join(missing), checks)
    rollup, _first = _rollup(checks, required)
    selected = _selected_checks(checks, required)
    if rollup == "SUCCESS":
        return CiReading("green", "", tuple(selected))
    if rollup == "FAILURE":
        failed = [item for item in selected if _check_result(item) == "fail"]
        return CiReading("red", "", tuple(selected), tuple(failed))
    return pending(f"{rollup.lower()} ({len(selected)} check(s))", selected)


def _runs(repo: str, items: list[dict[str, Any]]) -> list[dict[str, str]]:
    seen: list[str] = []
    for item in items:
        run_id = _actions_run_id(item)
        if run_id and run_id not in seen:
            seen.append(run_id)
    return [{"id": run_id, "url": f"https://github.com/{repo}/actions/runs/{run_id}"} for run_id in seen]


def _classify(host: Any, repo: str, failed: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """The gate's classification of a red: `infrastructure` only when every failed run read is.

    One failed job per run is read (`gate._failed_log`, `gate._classify_failed_step`), at most
    `_CLASSIFIED_RUNS` runs. A log that cannot be read classifies as product, as it does in the gate.
    """
    by_run: dict[str, dict[str, Any]] = {}
    for item in failed:
        by_run.setdefault(_actions_run_id(item), item)
    classes: list[str] = []
    reasons: list[str] = []
    for item in list(by_run.values())[:_CLASSIFIED_RUNS]:
        fragment = _failed_log(host, repo, item)
        classes.append(fragment.failure_class)
        if fragment.failure_reason:
            reasons.append(fragment.failure_reason)
    if classes and all(value == "infrastructure" for value in classes):
        return "infrastructure", reasons
    return "product", reasons


def render_fact(fact: dict[str, Any]) -> str:
    """One post-merge CI fact in words, for the card, the sprint comment and the observer wake."""
    result = str(fact.get("result") or "")
    card = str(fact.get("card") or "")
    base = str(fact.get("base") or "")
    sha = str(fact.get("merge_sha") or "")
    where = f"`{base}` @ `{sha}`" if sha else f"`{base}` (merge commit unknown)"
    raw_runs = fact.get("runs")
    runs: list[Any] = raw_runs if isinstance(raw_runs, list) else []
    run_text = ", ".join(
        f"{run.get('id')} ({run.get('url')})" for run in runs if isinstance(run, dict) and run.get("id")
    )
    lines = [f"Post-merge CI {result.upper()} for {card}: merge {where}."]
    if result == "green":
        lines.append("Every selected check on the merge commit finished successfully.")
    elif result == "red":
        lines.append(
            "The release merged, but the base's CI failed on the merge commit: this is not a plain Done."
        )
        raw_failed = fact.get("failed_checks")
        failed: list[Any] = raw_failed if isinstance(raw_failed, list) else []
        lines.append("- failed checks: " + (", ".join(str(name) for name in failed) or "(unnamed)"))
        classification = str(fact.get("classification") or "product")
        detail = str(fact.get("classification_reason") or "")
        lines.append(f"- classification: {classification}" + (f" ({detail})" if detail else ""))
    elif result == "absent":
        lines.append("No CI run validates the base after this merge.")
    elif result == "timeout":
        lines.append("The base's CI gave no terminal result for the merge commit within the ceiling.")
    lines.append("- run(s): " + (run_text or "none"))
    if fact.get("reason"):
        lines.append(f"- reason: {fact['reason']}")
    return "\n".join(lines)


def _request_id(watch: dict[str, Any], kind: str) -> str:
    return f"post-merge-ci-{kind}-{watch.get('id') or ''}-{watch.get('ref') or ''}"


def _sprint_writer(runtime: Any) -> Any:
    writer = getattr(runtime, "sprint_writer", None)
    if writer is not None:
        return writer
    from ummanu.sprints import SprintWriter, budget_thresholds

    instance = getattr(runtime.catalog, "instance", {})
    return SprintWriter(
        runtime.reader.client,
        data_dir=Path(getattr(runtime, "data_dir", None) or Path(runtime.audit.board_dir).parent),
        thresholds=budget_thresholds(instance if isinstance(instance, dict) else None),
    )


def publish(runtime: Any, watch: dict[str, Any]) -> None:
    """Write the recorded result: the card event, then the sprint comment. Both are idempotent."""
    fact = dict(watch["result"])
    body = render_fact(fact)
    runtime.writer.post_merge_ci(
        actor=runtime.owner,
        reference=str(watch["ref"]),
        body=body,
        fact=fact,
        request_id=_request_id(watch, "card"),
    )
    sprint = str(watch.get("sprint") or "")
    if sprint:
        _sprint_writer(runtime).comment(
            role="dispatcher",
            actor=runtime.owner,
            reference=sprint,
            body=body,
            request_id=_request_id(watch, "sprint"),
        )


def wake_facts(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The post-merge CI facts among a batch of observer wake events, in order."""
    facts = []
    for event in events:
        fact = post_merge_ci_fact(event)
        if fact is not None:
            facts.append(fact)
    return facts


__all__ = [
    "CEILING_ENV",
    "DEFAULT_CEILING_SECONDS",
    "POST_MERGE_CI_KEY",
    "WATCHES_KEY",
    "open_watch",
    "publish",
    "reconcile_post_merge_watches",
    "release_merge_marker",
    "render_fact",
    "resolve",
    "wake_facts",
]
