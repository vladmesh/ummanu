"""The e2e check a project adapter declares, and the GitHub calls that dispatch and identify its run.

A project declares one e2e check in its adapter, beside its mechanical gate (secretary-1795):

    validation:
      ci: github
      e2e:
        workflow: e2e.yml         # the workflow file name, or its numeric id, in the project's repository
        inputs: {suite: mega}     # optional static `workflow_dispatch` inputs
        deadline: 6h              # optional; how long the run may take, 6h when absent
        candidate_input: sha      # optional; the input that receives the candidate SHA
        dispatch_id_input: sid    # optional; the input that receives the dispatch id
        placement: before_merge   # optional; `after_merge` runs it on main after the merge

`parse_e2e` is the one reading of it, and a malformed declaration raises
:class:`AdapterE2eDeclarationError`, a typed adapter error: the adapter read fails (`InstanceCatalog.
adapter`), so the card's gate fails with the reason, instead of the stage being skipped. An adapter
with no `e2e` key reads as None and nothing changes. `e2e` needs `ci: github`: only the github gate
publishes the candidate branch the workflow is dispatched on.

`placement` is `before_merge` (the default: the stage runs on the card's candidate, `dispatch/e2e_stage.py`)
or `after_merge` (secretary-1807): for a project whose e2e workflow can only run on a commit whose releases
the post-merge CI of `main` published, the card merges with no e2e, and the dispatcher runs the workflow on
`main` afterwards, once for every card merged since the last run (`dispatch/e2e_after_merge.py`). Such a run
is dispatched on a branch the dispatcher owns, `pipeline-e2e/<dispatch id>`, pointed at the exact target
SHA (:func:`create_ref`) and deleted when the run is over (:func:`delete_ref`).

The run is identified by **GitHub's own answer**: the dispatch is sent with `return_run_details: true`
(REST, explicitly, so it does not depend on the host's `gh` version), and the 200 answer names the
run (`workflow_run_id`, `html_url`). The workflow needs no contract for that.

A dispatcher that lost that answer (it died after the POST, or the POST got no answer) finds the run
again among the workflow's runs by all of: `event == workflow_dispatch`, branch `pipeline/<ref>`,
`head_sha ==` the candidate, and `created_at` at or after the intent less
:data:`E2E_CLOCK_MARGIN_SECONDS` (GitHub's clock against the dispatcher's). The stage looks only once
the window has settled (:data:`E2E_RECOVERY_SETTLE_SECONDS` after that), and takes a match only when it
is the only one; more than one is ambiguous and is never guessed (:func:`matching_runs` returns them
all). When the adapter declares `dispatch_id_input`, that input carries the dispatch id and recovery
takes only a run whose title carries it, which is exact for a workflow that puts the input in its
`run-name`. Without it no extra input is sent.

Everything here is host I/O through the gate's `_backend_call`/`_gh_api`, so a question that got no
answer is a `GateTransportError`, never a verdict. The stage (`dispatch/e2e_stage.py`) decides what
each answer does to the card.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ummanu.board.e2e_record import AFTER_MERGE, BEFORE_MERGE, PLACEMENTS
from ummanu.board.wait_card import WaitSpecError, parse_duration, parse_utc
from ummanu.dispatch.gate import _HTTP_STATUS_RE, _backend_call, _failed_log, _gh_api, _LogFragment
from ummanu.dispatch.helpers import _tail
from ummanu.dispatch.types import GateTransportError, HostError

DEFAULT_DEADLINE = "6h"
#: How far before the intent a recovered run's `created_at` may lie: GitHub's clock against ours.
E2E_CLOCK_MARGIN_SECONDS = 120
#: How long after the margin recovery waits before it takes a match, so a second run in the window
#: has appeared in the listing by then.
E2E_RECOVERY_SETTLE_SECONDS = max(
    0, int(os.environ.get("UMMANU_E2E_RECOVERY_SETTLE_SECONDS", str(3 * 60)))
)
#: How long after its intent a dispatched run may stay unidentified before the card is Blocked.
E2E_IDENTIFY_SECONDS = max(60, int(os.environ.get("UMMANU_E2E_IDENTIFY_SECONDS", str(15 * 60))))

_KEYS = frozenset({"workflow", "inputs", "deadline", "candidate_input", "dispatch_id_input", "placement"})
#: The prefix of the branch an after-merge run is dispatched on: `pipeline-e2e/<dispatch id>`.
AFTER_MERGE_REF_PREFIX = "pipeline-e2e/"
_WORKFLOW_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.ya?ml$")
_INPUT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,99}$")
#: Conclusions of a failed job whose steps and log are the evidence of a red run.
_FAILED_JOB_CONCLUSIONS = frozenset({"failure", "timed_out"})
_RUNS_JQ = "[.workflow_runs[] | {id, event, head_branch, head_sha, display_title, name, html_url, status, created_at}]"
_JOBS_JQ = (
    "[.jobs[] | {name, conclusion, html_url, "
    'steps: [(.steps // [])[] | select(.conclusion == "failure" or .conclusion == "timed_out") | .name]}]'
)


class AdapterE2eDeclarationError(HostError):
    """An adapter's `validation.e2e` is malformed; the message names the adapter and what is wrong."""

    def __init__(self, adapter: str, problem: str) -> None:
        super().__init__(f"adapter {adapter or '(unnamed)'} declares a malformed validation.e2e: {problem}")
        self.adapter = adapter
        self.problem = problem


@dataclass(frozen=True)
class E2eDeclaration:
    """A well-formed `validation.e2e`."""

    workflow: str
    inputs: tuple[tuple[str, str], ...] = ()
    deadline: str = DEFAULT_DEADLINE
    candidate_input: str = ""
    dispatch_id_input: str = ""
    placement: str = BEFORE_MERGE

    @property
    def after_merge(self) -> bool:
        return self.placement == AFTER_MERGE

    def dispatch_inputs(self, dispatch_id: str, sha: str) -> dict[str, str]:
        """Every input one dispatch sends: the static ones, and the SHA and the id where declared."""
        inputs = dict(self.inputs)
        if self.candidate_input:
            inputs[self.candidate_input] = sha
        if self.dispatch_id_input:
            inputs[self.dispatch_id_input] = dispatch_id
        return inputs


def _input_value(name: str, value: Any, adapter: str) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str | int | float):
        return str(value)
    raise AdapterE2eDeclarationError(
        adapter,
        f"inputs.{name} is a {type(value).__name__}; a workflow_dispatch input is a string, number or boolean",
    )


def _input_name(value: Any, what: str, adapter: str, reserved: str = "") -> str:
    if not isinstance(value, str) or not _INPUT_NAME_RE.match(value):
        raise AdapterE2eDeclarationError(adapter, f"{what} {value!r} is not a workflow input name")
    if reserved and value == reserved:
        raise AdapterE2eDeclarationError(
            adapter, f"{what} {value!r} is the dispatch id input the dispatcher sets itself"
        )
    return value


def parse_e2e(validation: Any, *, adapter: str = "") -> E2eDeclaration | None:
    """The adapter's e2e declaration, None when `validation` declares none, or a typed error."""
    if not isinstance(validation, Mapping) or "e2e" not in validation:
        return None
    raw = validation["e2e"]
    if not isinstance(raw, Mapping):
        raise AdapterE2eDeclarationError(adapter, "it is not a mapping with a workflow")
    unknown = sorted(str(key) for key in raw if key not in _KEYS)
    if unknown:
        raise AdapterE2eDeclarationError(
            adapter, f"unknown key(s) {', '.join(unknown)} (known: {', '.join(sorted(_KEYS))})"
        )
    if str(validation.get("ci") or "none") != "github":
        raise AdapterE2eDeclarationError(
            adapter,
            f"it needs ci: github, not {validation.get('ci') or 'none'!r}: only the github gate publishes "
            "the candidate branch the workflow is dispatched on",
        )
    workflow = raw.get("workflow")
    if isinstance(workflow, int) and not isinstance(workflow, bool) and workflow > 0:
        workflow = str(workflow)
    if not isinstance(workflow, str) or not (workflow.isdigit() or _WORKFLOW_FILE_RE.match(workflow)):
        raise AdapterE2eDeclarationError(
            adapter, f"workflow {workflow!r} is neither a workflow file name like e2e.yml nor a workflow id"
        )
    inputs_raw = raw.get("inputs", {})
    if inputs_raw is None:
        inputs_raw = {}
    if not isinstance(inputs_raw, Mapping):
        raise AdapterE2eDeclarationError(adapter, "inputs is not a mapping of input names to values")
    dispatch_raw = raw.get("dispatch_id_input")
    dispatch_id_input = (
        "" if dispatch_raw is None else _input_name(dispatch_raw, "dispatch_id_input", adapter)
    )
    inputs = tuple(
        (_input_name(name, "input", adapter, dispatch_id_input), _input_value(str(name), value, adapter))
        for name, value in inputs_raw.items()
    )
    deadline = raw.get("deadline", DEFAULT_DEADLINE)
    try:
        parse_duration(str(deadline), "deadline")
    except WaitSpecError as exc:
        raise AdapterE2eDeclarationError(adapter, str(exc)) from None
    candidate = raw.get("candidate_input")
    candidate_input = (
        "" if candidate is None else _input_name(candidate, "candidate_input", adapter, dispatch_id_input)
    )
    if candidate_input and candidate_input in dict(inputs):
        raise AdapterE2eDeclarationError(
            adapter, f"candidate_input {candidate_input!r} is also a static input; it takes the candidate SHA"
        )
    placement = raw.get("placement", BEFORE_MERGE)
    if placement is None:
        placement = BEFORE_MERGE
    if not isinstance(placement, str) or placement not in PLACEMENTS:
        raise AdapterE2eDeclarationError(
            adapter, f"placement {placement!r} is neither {' nor '.join(PLACEMENTS)}"
        )
    return E2eDeclaration(
        workflow, inputs, str(deadline).strip(), candidate_input, dispatch_id_input, placement
    )


def declared_e2e(host: Any, project: str) -> E2eDeclaration | None:
    """This project's e2e declaration as its adapter is read now."""
    adapter_fn = getattr(host.catalog, "adapter", None)
    adapter = adapter_fn(project) if callable(adapter_fn) else None
    if not isinstance(adapter, Mapping):
        return None
    return parse_e2e(adapter.get("validation"), adapter=project)


class DispatchRefused(HostError):
    """GitHub answered the dispatch and refused it: no workflow, no `workflow_dispatch`, no access."""


@dataclass(frozen=True)
class DispatchedRun:
    """The run GitHub's dispatch answer names; `run_id` 0 when the answer named none."""

    run_id: int = 0


def dispatch_workflow(
    host: Any, repo: str, declaration: E2eDeclaration, *, branch: str, dispatch_id: str, sha: str
) -> DispatchedRun:
    """`POST .../workflows/{workflow}/dispatches` on the candidate branch, asking for the run it starts.

    `return_run_details: true` is sent explicitly, so the answer is the same whatever `gh` the host
    runs. Returns the run GitHub named, or an empty :class:`DispatchedRun` when it accepted the dispatch
    without naming one (the run is then looked up as after a crash). :class:`DispatchRefused` when
    GitHub answered with a refusal; `GateTransportError` when no answer came back (which says nothing
    about whether it was accepted), and for a rate limit, which is GitHub declining to answer now rather
    than refusing the workflow.
    """
    args = [
        "gh",
        "api",
        "--method",
        "POST",
        f"repos/{repo}/actions/workflows/{declaration.workflow}/dispatches",
        "-f",
        f"ref={branch}",
        "-F",
        "return_run_details=true",
    ]
    for name, value in declaration.dispatch_inputs(dispatch_id, sha).items():
        args += ["-f", f"inputs[{name}]={value}"]
    completed = _backend_call(host, args, "e2e workflow dispatch")
    if completed.returncode == 0:
        try:
            answer = json.loads((completed.stdout or "").strip() or "{}")
        except ValueError:
            answer = {}
        run_id = answer.get("workflow_run_id") if isinstance(answer, dict) else None
        if isinstance(run_id, int) and not isinstance(run_id, bool) and run_id > 0:
            return DispatchedRun(run_id)
        return DispatchedRun()
    code, text = _gh_status(completed)
    if code == "429" or "rate limit" in text.lower():
        raise GateTransportError(f"e2e workflow dispatch was rate limited: {text}")
    raise DispatchRefused(f"GitHub refused the dispatch of {declaration.workflow} on {branch}: {text}")


def matching_runs(
    host: Any,
    repo: str,
    workflow: str,
    *,
    branch: str,
    sha: str,
    since: datetime,
    dispatch_id: str = "",
) -> list[dict[str, Any]]:
    """The workflow's runs that can be this dispatch's: every one of them, never a guess among them.

    A match is a `workflow_dispatch` run on `branch` at `head_sha == sha`, created at or after `since`
    less :data:`E2E_CLOCK_MARGIN_SECONDS`. `dispatch_id` (only when the adapter declares a
    `dispatch_id_input`) is required in the run's title too: a run without it is never a match.
    `GateTransportError` when GitHub did not answer; `HostError` when it answered with an error.
    """
    path = (
        f"repos/{repo}/actions/workflows/{workflow}/runs"
        f"?event=workflow_dispatch&branch={branch}&head_sha={sha}&per_page=100"
    )
    runs = _gh_api(host, path, jq=_RUNS_JQ)
    earliest = since - timedelta(seconds=E2E_CLOCK_MARGIN_SECONDS)
    found: list[dict[str, Any]] = []
    for run in runs if isinstance(runs, list) else []:
        if not isinstance(run, dict):
            continue
        run_id = run.get("id")
        if not (isinstance(run_id, int) and not isinstance(run_id, bool) and run_id > 0):
            continue
        if (run.get("event"), run.get("head_branch"), run.get("head_sha")) != (
            "workflow_dispatch",
            branch,
            sha,
        ):
            continue
        try:
            created = parse_utc(str(run.get("created_at") or ""), "created_at")
        except WaitSpecError:
            continue
        if created >= earliest:
            found.append(run)
    if dispatch_id:
        found = [
            run for run in found if dispatch_id in f"{run.get('display_title') or ''} {run.get('name') or ''}"
        ]
    return found


def run_head_sha(host: Any, repo: str, run_id: int) -> str:
    """The SHA a run ran on. `GateTransportError` for no answer, `HostError` for an answered error."""
    run = _gh_api(host, f"repos/{repo}/actions/runs/{run_id}", jq="{head_sha}")
    head = run.get("head_sha") if isinstance(run, dict) else None
    if not head:
        raise GateTransportError(f"GitHub answered run {repo}#{run_id} with no head_sha")
    return str(head)


def _gh_status(completed: Any) -> tuple[str, str]:
    """`(HTTP status, text)` of a failed `gh api` call."""
    text = _tail((completed.stderr or completed.stdout or "").strip()) or "(no output)"
    status = _HTTP_STATUS_RE.search(text)
    return ((status.group(1) or status.group(2)) if status else ""), text


def create_ref(host: Any, repo: str, name: str, sha: str) -> None:
    """Point the dispatcher-owned branch `name` at `sha` (`POST .../git/refs`).

    A branch that already exists is accepted only when it already points at `sha` (the same intent
    repeated after a crash); anything else is a :class:`HostError`. `GateTransportError` when GitHub did
    not answer.
    """
    completed = _backend_call(
        host,
        [
            "gh",
            "api",
            "--method",
            "POST",
            f"repos/{repo}/git/refs",
            "-f",
            f"ref=refs/heads/{name}",
            "-f",
            f"sha={sha}",
        ],
        "e2e after-merge ref",
    )
    if completed.returncode == 0:
        return
    code, text = _gh_status(completed)
    if code == "422":
        current = _gh_api(host, f"repos/{repo}/git/ref/heads/{name}", jq="{sha: .object.sha}")
        if isinstance(current, dict) and current.get("sha") == sha:
            return
        raise HostError(f"branch {name} already exists in {repo} and does not point at {sha}: {text}")
    if code == "429" or "rate limit" in text.lower() or not code:
        raise GateTransportError(f"e2e after-merge ref {name} was not created: {text}")
    raise HostError(f"GitHub refused to create branch {name} at {sha} in {repo}: {text}")


#: What :func:`delete_ref` answers: GitHub deleted the branch, or it is confirmed not to exist.
REF_DELETED = "deleted"
REF_ABSENT = "absent"


def delete_ref(host: Any, repo: str, name: str) -> str:
    """Delete the dispatcher-owned branch `name`: :data:`REF_DELETED` or :data:`REF_ABSENT`, else raise.

    Three answers, and only two of them mean the branch is gone:

    - deleted: GitHub answered the `DELETE` with success (204);
    - absent: the `DELETE` answered 422 or 404, and a follow-up `GET git/ref/heads/<name>` answered 404.
      GitHub also answers 422 for a delete it refused (validation, a protected or default branch, spam
      limiting), so a 422 alone proves nothing;
    - not deleted: anything else, a 422 whose read still finds the branch or whose read failed included.
      `GateTransportError` when GitHub did not answer, `HostError` otherwise; the caller keeps the branch
      recorded and asks again.
    """
    completed = _backend_call(
        host,
        ["gh", "api", "--method", "DELETE", f"repos/{repo}/git/refs/heads/{name}"],
        "e2e after-merge ref",
    )
    if completed.returncode == 0:
        return REF_DELETED
    code, text = _gh_status(completed)
    if code in {"404", "422"}:
        read = _backend_call(host, ["gh", "api", f"repos/{repo}/git/ref/heads/{name}"], "e2e after-merge ref")
        if read.returncode == 0:
            raise HostError(f"branch {name} still exists in {repo} after its delete answered {code}: {text}")
        read_code, read_text = _gh_status(read)
        if read_code == "404":
            return REF_ABSENT
        raise HostError(
            f"branch {name} in {repo}: the delete answered {code} ({text}) and the read that would confirm it is "
            f"gone failed: {read_text}"
        )
    if code == "429" or "rate limit" in text.lower() or not code:
        raise GateTransportError(f"e2e after-merge ref {name} was not deleted: {text}")
    raise HostError(f"GitHub refused to delete branch {name} in {repo}: {text}")


def is_ancestor(host: Any, repo: str, ancestor: str, descendant: str) -> bool:
    """Whether `ancestor` is `descendant` or one of its ancestors, by GitHub's compare of the two."""
    if ancestor == descendant:
        return True
    answer = _gh_api(host, f"repos/{repo}/compare/{ancestor}...{descendant}", jq="{status}")
    status = answer.get("status") if isinstance(answer, dict) else None
    if not status:
        raise GateTransportError(
            f"GitHub answered the compare of {ancestor[:12]}...{descendant[:12]} with no status"
        )
    return str(status) in {"ahead", "identical"}


@dataclass(frozen=True)
class RedEvidence:
    """What a red run shows: its failed jobs with their failed steps, and one bounded log fragment."""

    jobs: tuple[tuple[str, tuple[str, ...]], ...]
    fragment: _LogFragment
    note: str = ""


def red_evidence(host: Any, repo: str, run_id: int, run_url: str) -> RedEvidence:
    """The failed jobs and steps of a concluded run, and the gate's `--log-failed` fragment of the first.

    Never raises: evidence that cannot be read degrades to a note, it does not undo the red verdict.
    """
    note = ""
    jobs: list[tuple[str, tuple[str, ...]]] = []
    try:
        listed = _gh_api(host, f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100", jq=_JOBS_JQ)
    except HostError as exc:
        listed, note = [], f"the run's jobs could not be read: {_tail(str(exc), 5)}"
    for job in listed if isinstance(listed, list) else []:
        if isinstance(job, dict) and str(job.get("conclusion") or "") in _FAILED_JOB_CONCLUSIONS:
            steps = tuple(str(step) for step in job.get("steps") or [] if str(step))
            jobs.append((str(job.get("name") or "?"), steps))
    first = jobs[0][0] if jobs else ""
    fragment = _failed_log(host, repo, {"name": first, "html_url": run_url})
    return RedEvidence(tuple(jobs), fragment, note)


__all__ = [
    "AFTER_MERGE_REF_PREFIX",
    "DEFAULT_DEADLINE",
    "E2E_CLOCK_MARGIN_SECONDS",
    "E2E_IDENTIFY_SECONDS",
    "E2E_RECOVERY_SETTLE_SECONDS",
    "REF_ABSENT",
    "REF_DELETED",
    "AdapterE2eDeclarationError",
    "DispatchRefused",
    "DispatchedRun",
    "E2eDeclaration",
    "RedEvidence",
    "create_ref",
    "declared_e2e",
    "delete_ref",
    "dispatch_workflow",
    "is_ancestor",
    "matching_runs",
    "parse_e2e",
    "red_evidence",
    "run_head_sha",
]
