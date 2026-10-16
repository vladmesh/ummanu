"""Shared GitHub checks, statuses, Actions logs/reruns and backend transport.

E2e, post-merge and wait consumers use this owner directly. All gate backend
questions pass through the single _backend_call implementation here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from ummanu.dispatch.gate_types import GATE_LOG_FRAGMENT_LINES, GateResult
from ummanu.dispatch.helpers import _tail, safe_one_line
from ummanu.dispatch.types import GateTransportError, HostError, ProjectGitAccessError

_FAIL_CONCLUSIONS = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STALE", "STARTUP_FAILURE"}
_PASS_CONCLUSIONS = {"SUCCESS", "NEUTRAL", "SKIPPED"}
#: The conclusion `_rollup` gives a required check that was never posted once every run has settled.
MISSING_CONCLUSION = "MISSING"
_RUN_URL_RE = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/actions/runs/(\d+)")
# `gh run view --log[-failed]` emits one line per log entry as `<job>\t<step>\t<content>`.
_ERROR_MARK_RE = re.compile(r"##\[error\]")
_RUNNER_BOILERPLATE_RE = re.compile(r"(?i)process completed with exit code \d+")
# CI-service signatures distinguish infrastructure from code/test failures.
# Only explicit CI service unavailable/5xx signatures classify infrastructure reds.
_ACTION_DOWNLOAD_5XX_RE = re.compile(
    # The runner prints its preparation notice and the failed download on separate entries.  The
    # fragment, not a source line, is the unit of adjacency because `_failed_log` joins entries
    # with newlines.  Requiring the action-download wording on the 5xx entry keeps an unrelated
    # setup failure from borrowing the preparation notice above it.
    r"(?im)\bgetting action download info\b(?:[^\n]*\n){0,3}[^\n]*"
    r"(?:failed to download action|response status code does not indicate success)[^\n]*\b5\d\d\b"
)
_REGISTRY_UNAVAILABLE_RE = re.compile(
    # A named registry plus a service-unavailable response is explicit evidence.  Docker/Buildx
    # daemon messages are explicit too even when they omit the registry name: they name an HTTP
    # boundary and a 5xx themselves.  The caller still binds this to one of the container steps.
    r"(?im)(?:"
    r"(?:registry(?:[./][\w./:-]+)?|ghcr\.io|docker\.io|quay\.io|image manifest|pulling fs layer)"
    r"[^\n]{0,240}?(?:http(?:/\d(?:\.\d)?)?\s*(?:status\s*)?[: ]?5\d\d|"
    r"(?:service|temporarily) unavailable)"
    r"|(?:received unexpected http status|unexpected status(?: code)?)\s*:?\s*5\d\d"
    r")"
)
_RUNNER_UNAVAILABLE_RE = re.compile(
    # These are runner-service diagnostics, not arbitrary setup-script prose that happens to use
    # the word "runner".  Keep the vocabulary enumerable with the fixture corpus below.
    r"(?i)(?:the hosted runner failed to start|the runner has (?:received a shutdown signal|"
    r"lost communication with the server)|the job was cancelled because the runner was not started)"
)
# How a failed backend command says that an answer *did* arrive.
#
# The rule is positive and the default is silence: a question counts as answered only when the
# tool prints one of the shapes below, and anything else it prints is a question that got no
# answer. Guessing "no answer" costs a few retries; guessing "answer" costs an immediate wrong
# Blocked on a moment of bad network. The shapes were taken from the binaries this gate runs
# (gh 2.45.0, git 2.43.0); the captures are in the tests next to them.
#
# 1. An HTTP status the tool quotes — only the backend can produce one. A 5xx is the exception:
#    the backend failed to serve an answer, and that counts as transport.
#      gh api        -> gh: Not Found (HTTP 404)
#      git over HTTPS-> fatal: unable to access '...': The requested URL returned error: 503
_HTTP_STATUS_RE = re.compile(r"(?i)\bhttp(?:/\d(?:\.\d)?)?[ /](\d{3})\b|returned error:\s*(\d{3})\b")
# 2. A GraphQL error: gh rendering an API response body it parsed. A raw response body reaching
#    the text counts for the same reason.
#      gh repo view  -> GraphQL: Could not resolve to a Repository with the name '...'
# 3. git's push report, which exists only because the remote answered: the per-ref status table
#    it prints from the server's report-status, and any line the server itself sent.
#      git push      ->  ! [rejected]        main -> main (fetch first)
#                        remote: policy: branch is protected
_ANSWERED_RE = re.compile(
    r"(?im)(\bgraphql:\s|^\s*remote:\s|!\s*\[(?:rejected|remote rejected|deleted|no match)\]"
    r'|^\s*\{\s*"(?:errors|message)")'
)
# 4. `git fetch` reporting that the requested remote ref does not exist. This is a terminal
# answer from the named remote, but git gives it neither an HTTP status nor the push report shape
# above. Keep it coupled to the fetch invocation: `fatal` by itself remains unclassified silence.
_GIT_FETCH_MISSING_REF_RE = re.compile(r"(?im)^fatal: couldn't find remote ref [^\r\n]+$")


def _backend_answered(text: str, args: list[str]) -> bool:
    """Whether the remote backend answered a failed command.

A 5xx HTTP answer may still signal transport failure; a determinate backend
rejection must remain distinct from an unanswered request.
    """
    text = (text or "").strip()
    if not text:
        return False
    status = _HTTP_STATUS_RE.search(text)
    if status:
        code = status.group(1) or status.group(2)
        return not code.startswith("5")
    if args[:1] == ["git"] and "fetch" in args and _GIT_FETCH_MISSING_REF_RE.search(text):
        return True
    return bool(_ANSWERED_RE.search(text))


def _answer(completed) -> str:
    """What a finished command said: its stderr, else its stdout, stripped."""
    return (completed.stderr or completed.stdout or "").strip()


def _backend_call(
    host,
    args: list[str],
    label: str,
    *,
    cwd: Path | None = None,
    project: str = "",
    extra_env: dict[str, str] | None = None,
):
    """Invoke the gate backend and classify unanswered remote calls.

Keep a determinate non-zero response as a response. Raise GateTransportError
only for failures to reach the backend. Project Git access refusals propagate
as ProjectGitAccessError rather than retryable silence. `extra_env` reaches only
a project remote Git child.
    """
    try:
        if project:
            if args[:2] != ["git", "-C"] or len(args) < 4:
                raise HostError(f"{label}: a project remote Git operation must name its checkout")
            completed = host.remote_git(project, args[2], args[3:], label, extra_env=extra_env)
        elif extra_env:
            raise HostError(f"{label}: an environment overlay needs a project remote Git operation")
        else:
            completed = host.run_capture(args, label, cwd=cwd)
    except ProjectGitAccessError:
        raise
    except HostError as exc:
        # The command never finished: it timed out waiting for the backend, or could not be run
        # at all. Either way no answer exists, and the tool's own text travels with it.
        raise GateTransportError(f"{label} got no answer: {exc}") from None
    if completed.returncode == 0:
        return completed
    text = _tail(_answer(completed))
    if _backend_answered(text, args):
        return completed
    raise GateTransportError(f"{label} got no answer: {text or '(no output)'}")


@dataclass
class _LogFragment:
    """What `_failed_log` recovered from a failed CI job, or why it couldn't."""

    available: bool
    step: str = ""
    text: str = ""
    failure_class: str = "substantive"
    failure_reason: str = ""
    reason: str = ""


def _validation(host, task: dict) -> dict:
    adapter_fn = getattr(host.catalog, "adapter", None)
    adapter = adapter_fn(task["project"]) if callable(adapter_fn) else None
    validation = adapter.get("validation") if isinstance(adapter, dict) else None
    return validation if isinstance(validation, dict) else {}


def _required_checks(host, task: dict) -> list[str]:
    """Declared names of the checks the github gate judges by (`validation.required_checks`).
    Empty list means the adapter has not migrated yet: the gate then judges by every check on the
    sha, as it did before secretary-841."""
    declared = _validation(host, task).get("required_checks")
    if not isinstance(declared, list):
        return []
    return [name.strip() for name in declared if isinstance(name, str) and name.strip()]


def _actions_run_id(item: dict) -> str:
    match = _RUN_URL_RE.search(
        str(
            item.get("details_url")
            or item.get("html_url")
            or item.get("targetUrl")
            or item.get("target_url")
            or ""
        )
    )
    return match.group(2) if match else ""


def rerun_failed_ci(host, result: GateResult) -> None:
    """Ask Actions to rerun the failed jobs of the exact run that produced an infra red."""
    run_id = result.failed_run_id
    repo = result.failed_run_repo
    if not run_id or not repo:
        raise HostError("infrastructure gate red cannot be rerun: failed Actions run is unavailable")
    completed = _backend_call(
        host, ["gh", "run", "rerun", "--failed", run_id, "-R", repo], "gate rerun failed CI"
    )
    if completed.returncode != 0:
        raise HostError(
            f"gate rerun failed CI: {_tail(_answer(completed))}"
        )


def _rerun_status(host, repo: str, run_id: str) -> tuple[str, str]:
    """Read the rerun's current attempt, not the terminal check-runs from its prior attempt."""
    completed = _backend_call(
        host,
        ["gh", "run", "view", run_id, "-R", repo, "--json", "status,conclusion"],
        "gate infrastructure rerun status",
    )
    if completed.returncode != 0:
        raise HostError(
            f"gate infrastructure rerun status failed: {_tail(_answer(completed))}"
        )
    try:
        payload = json.loads((completed.stdout or "").strip())
    except ValueError:
        payload = None
    if not isinstance(payload, dict) or not str(payload.get("status") or ""):
        raise HostError("gate infrastructure rerun status returned no run state")
    return str(payload.get("status") or "").upper(), str(payload.get("conclusion") or "").upper()


def _name_with_owner(host, workspace: str) -> str:
    completed = _backend_call(
        host,
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        "gate repo view",
        cwd=Path(workspace),
    )
    name = (completed.stdout or "").strip()
    if completed.returncode != 0 or not name:
        # The answer travels with the failure: a message that drops the tool's own words leaves
        # nothing behind to say what the backend said.
        detail = _tail(_answer(completed)) or "(no output)"
        raise HostError(f"gate could not resolve the repository name: {detail}")
    return name


def _poll_ci(
    host, repo: str, sha: str, required: list[str] | None = None
) -> tuple[str, dict | None, list[dict]]:
    """Combined CI rollup for `sha` over every page of check-runs and commit statuses, narrowed to
    `required`. A cancelled `workflow_dispatch` run is auxiliary, so its check runs are left out."""
    runs = _gh_api_pages(host, f"repos/{repo}/actions/runs?head_sha={sha}", "workflow_runs")
    auxiliary = {
        str(run.get("id"))
        for run in runs
        if run.get("event") == "workflow_dispatch" and run.get("conclusion") == "cancelled"
    }
    items = [
        item
        for item in _gh_api_pages(host, f"repos/{repo}/commits/{sha}/check-runs", "check_runs")
        if _actions_run_id(item) not in auxiliary
    ]
    items.extend(_gh_api_pages(host, f"repos/{repo}/commits/{sha}/status", "statuses"))
    settled = bool(runs) and all(run.get("status") == "completed" for run in runs)
    rollup, failed = _rollup(items, required, runs_settled=settled)
    return rollup, failed, _selected_checks(items, required or [])


def _selected_checks(items: list[dict], required: list[str]) -> list[dict]:
    """The exact checks the gate judged, in stable order, for an attestation receipt."""
    selected = [item for item in items if not required or _check_name(item) in required]
    return sorted(
        selected, key=lambda item: (_check_name(item), str(item.get("id") or item.get("context") or ""))
    )


def _terminal_check(item: dict) -> dict[str, str]:
    conclusion = str(item.get("conclusion") or item.get("state") or item.get("status") or "").upper()
    return {
        "name": safe_one_line(_check_name(item)),
        "conclusion": conclusion,
        "url": safe_one_line(
            item.get("details_url")
            or item.get("html_url")
            or item.get("target_url")
            or item.get("targetUrl")
            or ""
        ),
    }


def _gh_api(host, path: str, *, jq: str):
    """The parsed JSON `gh api` printed; an empty or unparseable answer is GateTransportError."""
    completed = _backend_call(host, ["gh", "api", path, "--jq", jq], "gate gh api")
    if completed.returncode != 0:
        raise HostError(f"gate gh api failed: {_tail(_answer(completed))}")
    text = (completed.stdout or "").strip()
    if not text:
        raise GateTransportError(f"gate gh api {path} answered with no output")
    try:
        return json.loads(text)
    except ValueError:
        raise GateTransportError(f"gate gh api {path} answered with unreadable JSON: {_tail(text, 3)}") from None


def _gh_api_pages(host, path: str, key: str) -> list[dict]:
    """Every `key` item across all pages of a GitHub list answer that carries `total_count`.

    An empty, unreadable or short page set is GateTransportError, never a shorter list.
    """
    completed = _backend_call(
        host, ["gh", "api", path, "--paginate", "--jq", f"{{total_count, {key}}}"], "gate gh api"
    )
    if completed.returncode != 0:
        raise HostError(f"gate gh api failed: {_tail(_answer(completed))}")
    text = (completed.stdout or "").strip()
    if not text:
        raise GateTransportError(f"gate gh api {path} answered with no output")
    decoder = json.JSONDecoder()
    items: list[dict] = []
    totals: set[int] = set()
    position = 0
    while position < len(text):
        try:
            page, position = decoder.raw_decode(text, position)
        except ValueError:
            raise GateTransportError(f"gate gh api {path} answered with unreadable JSON: {_tail(text, 3)}") from None
        while position < len(text) and text[position].isspace():
            position += 1
        listed = page.get(key) if isinstance(page, dict) else None
        total = page.get("total_count") if isinstance(page, dict) else None
        if not isinstance(listed, list) or not isinstance(total, int) or isinstance(total, bool):
            raise GateTransportError(f"gate gh api {path} answered a page without total_count and {key}")
        if not all(isinstance(item, dict) for item in listed):
            raise GateTransportError(f"gate gh api {path} answered a {key} entry that is not an object")
        totals.add(total)
        items.extend(listed)
    if len(totals) != 1 or len(items) != totals.pop():
        raise GateTransportError(f"gate gh api {path} answered {len(items)} {key} against its total_count")
    return items


def _check_result(item: dict) -> str:
    """One check run or commit status -> 'pass' | 'fail' | 'pending'. A check run is terminal once it
    has a conclusion and is `completed` or has `completed_at`; an unknown conclusion stays pending."""
    if "state" in item and "status" not in item:
        state = str(item.get("state", "")).upper()
        if state in ("FAILURE", "ERROR"):
            return "fail"
        if state == "SUCCESS":
            return "pass"
        return "pending"
    conclusion = str(item.get("conclusion") or "").upper()
    finished = str(item.get("status", "")).upper() == "COMPLETED" or bool(item.get("completed_at"))
    if not finished or not conclusion:
        return "pending"
    if conclusion in _FAIL_CONCLUSIONS:
        return "fail"
    if conclusion in _PASS_CONCLUSIONS:
        return "pass"
    return "pending"


def _check_name(item: dict) -> str:
    """Declared-name key of a rollup entry: the Actions check-run `name`, or the legacy commit
    status `context`."""
    return str(item.get("name") or item.get("context") or "").strip()


def _rollup(
    items: list[dict], required: list[str] | None = None, *, runs_settled: bool = False
) -> tuple[str, dict | None]:
    """Combine CI checks into an overall status.

PENDING while any selected check runs. Once every posted check is terminal a failure is FAILURE
even if a required check was never posted; a required check still absent is PENDING, or a
FAILURE with conclusion MISSING_CONCLUSION when `runs_settled` says every workflow run completed.
    """
    missing: list[str] = []
    if required:
        by_name: dict[str, list[dict]] = {name: [] for name in required}
        for item in items:
            bucket = by_name.get(_check_name(item))
            if bucket is not None:
                bucket.append(item)
        items = [item for name in required for item in by_name[name]]
        missing = [name for name in required if not by_name[name]]
    if not items and not missing:
        return "NONE", None
    first_fail = None
    pending = False
    for item in items:
        result = _check_result(item)
        if result == "fail" and first_fail is None:
            first_fail = item
        elif result == "pending":
            pending = True
    if pending:
        return "PENDING", None
    if first_fail is not None:
        return "FAILURE", first_fail
    if missing:
        if runs_settled:
            return "FAILURE", {"name": missing[0], "status": "completed", "conclusion": MISSING_CONCLUSION}
        return "PENDING", None
    return "SUCCESS", None


def _parse_job_log(text: str) -> list[tuple[str, str, str]]:
    """Split `gh run view --log[-failed]` output into (job, step, content) triples. A line that
    doesn't carry the tab-separated job/step prefix (unexpected gh output) still surfaces with
    empty job/step rather than being dropped."""
    entries = []
    for line in text.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            entries.append((parts[0], parts[1], parts[2]))
        else:
            entries.append(("", "", line))
    return entries


def _failed_log(host, repo: str, item: dict, lines: int = GATE_LOG_FRAGMENT_LINES) -> _LogFragment:
    """Extract a failed GitHub Actions job/step and its relevant log fragment.

gh run view --log-failed interleaves steps; keep the selected failure
bounded and attributed to the appropriate job and step.
    """
    run_id = _actions_run_id(item)
    if not run_id:
        return _LogFragment(available=False, reason="the entry is not an Actions run (no run link)")
    try:
        completed = _backend_call(
            host, ["gh", "run", "view", run_id, "-R", repo, "--log-failed"], "gate failed log"
        )
    except GateTransportError as exc:
        # Failed-log silence degrades the excerpt; it does not reopen an answered red verdict.
        return _LogFragment(available=False, reason=f"the log could not be fetched: {exc}")
    if completed.returncode != 0:
        return _LogFragment(available=False, reason="`gh run view --log-failed` returned an error")
    entries = _parse_job_log((completed.stdout or "").strip())
    if not entries:
        return _LogFragment(available=False, reason="the gate received an empty log")
    job_name = str(item.get("name") or item.get("context") or "")
    scoped = [entry for entry in entries if not job_name or entry[0] == job_name] or entries
    cause_idx = next(
        (
            i
            for i in range(len(scoped) - 1, -1, -1)
            if _ERROR_MARK_RE.search(scoped[i][2]) and not _RUNNER_BOILERPLATE_RE.search(scoped[i][2])
        ),
        len(scoped) - 1,
    )
    start = max(0, cause_idx - lines + 1)
    tail = scoped[start : cause_idx + 1]
    step = next((entry[1] for entry in reversed(tail) if entry[1]), "")
    text = "\n".join(entry[2] for entry in tail).strip()
    if not text:
        return _LogFragment(available=False, reason="the gate received an empty log")
    failure_class, failure_reason = _classify_failed_step(step, text)
    return _LogFragment(
        available=True,
        step=step,
        text=text,
        failure_class=failure_class,
        failure_reason=failure_reason,
    )


def _classify_failed_step(step: str, text: str) -> tuple[str, str]:
    """Classify an Actions red from its failed step and service evidence.

    This is intentionally an allowlist.  In particular, a failing ``Set up job`` does not become
    infra merely because of its name: the logged failure must show an action-download 5xx, or an
    unavailable runner.  The return value is persisted by the dispatcher before a card comment is
    rendered, so card prose cannot affect a later retry decision.
    """
    step = step.strip()
    lowered_step = step.casefold()
    if "set up job" in lowered_step and _ACTION_DOWNLOAD_5XX_RE.search(text):
        return "infrastructure", "action-download-http-5xx"
    if "set up docker buildx" in lowered_step and _REGISTRY_UNAVAILABLE_RE.search(text):
        return "infrastructure", "buildx-registry-unavailable"
    if any(
        token in lowered_step
        for token in (
            "initialize containers",
            "initialize container",
            "pull image",
            "pulling image",
            "docker pull",
            "load image",
        )
    ) and _REGISTRY_UNAVAILABLE_RE.search(text):
        return "infrastructure", "image-registry-unavailable"
    if "set up job" in lowered_step and _RUNNER_UNAVAILABLE_RE.search(text):
        return "infrastructure", "runner-unavailable"
    return "substantive", ""
