"""Mechanical validation gate between worker completion and review/merge.

An adapter selects local (validation.command), github (branch PR and required
checks), or none. PENDING/NONE is never treated as failed CI. Candidate history
is checked for forbidden attribution in every mode before publication, and
base freshness is resolved before local/github validation.

CommandHostRuntime.gate_check owns the host I/O; gate_lifecycle owns durable
state transitions, separate from reviewer and merge policy.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from ummanu.candidate_history import (
    Commit,
    ai_attributions,
    parse_shas,
    repair_message,
)
from ummanu.dispatch.gate_receipt import is_exact_sha, mint_gate_receipt
from ummanu.dispatch.helpers import (
    _last_marker_body,
    _legacy_worker_branch,
    _tail,
    safe_one_line,
    scrub_host_output,
)
from ummanu.dispatch.state import DispatcherRecord, GatePrAuthorship, GatePublishedRef
from ummanu.dispatch.types import GateTransportError, HostError, ProjectGitAccessError

# Pending CI has a bounded watchdog so missing checks cannot strand a card.
GATE_PENDING_STALL_SECONDS = int(os.environ.get("UMMANU_GATE_PENDING_STALL_SECONDS", str(6 * 3600)))

GATE_LOG_FRAGMENT_LINES = int(os.environ.get("UMMANU_GATE_LOG_FRAGMENT_LINES", "40"))

# Consecutive backend silence is bounded before human escalation.
GATE_TRANSPORT_MAX_ATTEMPTS = max(1, int(os.environ.get("UMMANU_GATE_TRANSPORT_MAX_ATTEMPTS", "5")))

# Enumerated CI-service failures rerun per SHA with a bounded cap.
GATE_INFRASTRUCTURE_RERUN_MAX_ATTEMPTS = max(
    1, int(os.environ.get("UMMANU_GATE_INFRASTRUCTURE_RERUN_MAX_ATTEMPTS", "2"))
)


def reset_infrastructure_reruns(record: DispatcherRecord) -> None:
    """Clear the per-SHA bounded infrastructure-rerun and rerun-transport state."""
    record.gate_infrastructure_reruns_sha = ""
    record.gate_infrastructure_reruns = 0
    record.gate_infrastructure_rerun_run_id = ""
    record.gate_infrastructure_rerun_reason = ""
    record.gate_rerun_transport_failures = 0
    record.gate_rerun_transport_error = ""

PR_BODY_SECTION_CHARS = int(os.environ.get("UMMANU_PR_BODY_SECTION_CHARS", "4000"))

_PR_BODY_FOOTER = (
    "Opened by the ummanu CI gate so that the `pull_request` workflow runs. "
    "Title and body are built from the card and the worker's done report."
)

_FAIL_CONCLUSIONS = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STALE", "STARTUP_FAILURE"}
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


def _backend_call(host, args: list[str], label: str, *, cwd: Path | None = None, project: str = ""):
    """Invoke the gate backend and classify unanswered remote calls.

Keep a determinate non-zero response as a response. Raise GateTransportError
only for failures to reach the backend. Project Git access refusals propagate
as ProjectGitAccessError rather than retryable silence.
    """
    try:
        if project:
            if args[:2] != ["git", "-C"] or len(args) < 4:
                raise HostError(f"{label}: a project remote Git operation must name its checkout")
            completed = host.remote_git(project, args[2], args[3:], label)
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
    text = _tail((completed.stderr or completed.stdout or "").strip())
    if _backend_answered(text, args):
        return completed
    raise GateTransportError(f"{label} got no answer: {text or '(no output)'}")


@dataclass
class GateResult:
    status: str  # "green" | "red" | "pending"
    summary: str
    log: str = ""
    fingerprint: str = ""
    # Structured terminal reds decide whether the exact SHA may rerun.
    # "substantive" | "infrastructure" | "publication" | "topology"
    failure_class: str = "substantive"
    failure_reason: str = ""
    # The Actions run which supplied an infrastructure-classified red.  Only this concrete run
    # can be rerun; an external status or a log without a run URL remains a red verdict.
    failed_run_id: str = ""
    failed_run_repo: str = ""
    # Reusable green evidence names the exact tree and terminal checks.
    attestation: dict[str, object] | None = None


@dataclass
class _LogFragment:
    """What `_failed_log` recovered from a failed CI job, or why it couldn't."""

    available: bool
    step: str = ""
    text: str = ""
    failure_class: str = "substantive"
    failure_reason: str = ""
    reason: str = ""


def _fingerprint(*parts: str) -> str:
    """Stable short digest of supplied fields, used to compare repeated failures.

Include the head SHA in the caller's detail so different candidates do not
collapse to one apparent repeat.
    """
    return hashlib.sha256("\x1f".join(parts).encode("utf-8", "surrogateescape")).hexdigest()[:16]


def gate_check(host, task: dict, record) -> GateResult:
    """Run the mechanical gate for this task in the worker checkout.

Raise GateTransportError for unanswered backend calls; determinate red
verdicts stay ordinary GateResult outcomes.
    """
    if getattr(host, "mode", "real") == "noop":
        # A noop proves no command ran.  It may preserve dispatcher control flow, but must never
        # look like reusable validation evidence.
        return GateResult("green", "noop gate")
    ci = _validation(host, task).get("ci") or "none"
    workspace = record.workspace
    # Every mode, including none, needs a readable checkout for candidate preflight.
    if not workspace or not Path(workspace).is_dir():
        raise HostError("gate workspace is missing")
    base = host.catalog.integration_base(task["project"], task.get("workspace", {}).get("base_branch"))
    if ci != "none" and _recover_base(host, workspace, base, task["project"]) == "conflict":
        return GateResult(
            "red",
            f"branch fell behind base {base!r} and the merge conflicts — resolve it in the "
            f"workspace and report done again",
            fingerprint=_fingerprint("base-conflict", base),
        )
    # Candidate trailer preflight precedes every gate mode and publication.
    history = _candidate_history_gate(host, workspace, base)
    if history is not None:
        return history
    if ci == "none":
        # `none` is an explicit absence of mechanical validation, not an all-green check set.
        return GateResult("green", "ci none: mechanical gate skipped")
    if ci == "local":
        return _local_gate(host, task, workspace)
    if ci == "github":
        return _github_gate(host, task, record, workspace, base, _required_checks(host, task))
    raise HostError(f"unsupported validation ci mode {ci!r}; expected local, github or none")


def _candidate_history_gate(host, workspace: str, base: str) -> GateResult | None:
    """Reject forbidden AI attribution in commit messages within base..HEAD.

Check before publishing even for the none gate. Read commits individually
to prevent untrusted message framing; unreadable history fails closed.
    """
    try:
        listing = host.run_capture(
            ["git", "-C", workspace, "log", "--format=%H", _history_range(host, workspace, base)],
            "gate candidate history",
        )
    except UnicodeError as exc:
        raise HostError("gate candidate history could not be decoded") from exc
    if listing.returncode != 0:
        raise HostError(
            "gate candidate history could not be read: "
            f"{_tail((listing.stderr or listing.stdout or '').strip())}"
        )
    try:
        shas = parse_shas(listing.stdout or "")
    except ValueError as exc:
        raise HostError(f"gate candidate history could not be read: {exc}") from None
    commits = []
    for sha in shas:
        try:
            message = host.run_capture(
                ["git", "-C", workspace, "log", "-1", "--format=%B", sha],
                "gate candidate history message",
            )
        except UnicodeError as exc:
            raise HostError(f"gate candidate history could not be decoded for {sha[:12]}") from exc
        if message.returncode != 0:
            raise HostError(
                f"gate candidate history could not be read for {sha[:12]}: "
                f"{_tail((message.stderr or message.stdout or '').strip())}"
            )
        commits.append(Commit(sha=sha, message=message.stdout or ""))
    attributions = ai_attributions(commits)
    if not attributions:
        return None
    return GateResult(
        "red",
        "candidate history carries forbidden AI attribution; nothing was published",
        repair_message(attributions, base),
        fingerprint=_fingerprint("ai-attribution", *(item.sha for item in attributions)),
    )


def _history_range(host, workspace: str, base: str) -> str:
    """Resolve the candidate's base..HEAD using a locally available base ref.

Prefer the fetched remote-tracking ref when present; reject ranges with
unresolvable provenance rather than silently checking a different base.
    """
    for ref in (f"origin/{base}", base):
        resolved = host.run_capture(
            ["git", "-C", workspace, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            "gate candidate history base",
        )
        if resolved.returncode == 0 and (resolved.stdout or "").strip():
            return f"{ref}..HEAD"
    raise HostError(
        f"gate candidate history could not resolve base {base!r} in the worker workspace; "
        "the candidate's own commits cannot be identified"
    )


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


def validation_ci(host, task: dict) -> str:
    """The project's declared mechanical-gate mode: "local" | "github" | "none". Read by the
    dispatcher's merge step to pick a github PR merge over the local fast-forward."""
    return _validation(host, task).get("ci") or "none"


def _recover_base(host, workspace: str, base: str, project: str) -> str:
    """Fast-forward the worker branch onto the latest base. Returns "clean" (already current),
    "recovered" (merged base in), or "conflict" (a textual conflict, aborted)."""
    fetch = _backend_call(
        host, ["git", "-C", workspace, "fetch", "origin", base], "gate base fetch", project=project
    )
    if fetch.returncode != 0:
        # An answered refusal (a base branch the remote does not have, a rejected credential) is
        # a determinate gate failure, exactly as it was before this call moved here.
        raise HostError(
            f"gate base fetch (git fetch) refused: {_tail((fetch.stderr or fetch.stdout or '').strip())}"
        )
    behind = host._run(
        ["git", "-C", workspace, "rev-list", "--count", f"HEAD..origin/{base}"],
        "gate base compare",
    ).stdout.strip()
    if behind in ("", "0"):
        return "clean"
    merge = host.run_capture(
        ["git", "-C", workspace, "merge", "--no-edit", f"origin/{base}"], "gate base merge"
    )
    if merge.returncode != 0:
        host.run_capture(["git", "-C", workspace, "merge", "--abort"], "gate base merge abort")
        return "conflict"
    return "recovered"


def _local_gate(host, task: dict, workspace: str) -> GateResult:
    command = _validation(host, task).get("command")
    if not isinstance(command, str) or not command.strip():
        raise HostError("local validation has no command")
    pre_run_sha = _head_sha(host, workspace)
    if not is_exact_sha(pre_run_sha):
        raise HostError("local gate could not capture a full pre-run HEAD object id")
    completed = host.run_capture(["bash", "-lc", command], "local gate", cwd=Path(workspace))
    post_run_sha = _head_sha(host, workspace)
    if not is_exact_sha(post_run_sha) or post_run_sha != pre_run_sha:
        detail = (
            f"HEAD changed while local validation ran ({pre_run_sha} -> {post_run_sha or '(unavailable)'})"
        )
        return GateResult(
            "red",
            "local validation did not preserve the validated HEAD",
            detail,
            fingerprint=_fingerprint("local-head-mutated", pre_run_sha, post_run_sha),
        )
    receipt = mint_gate_receipt(
        validated_sha=pre_run_sha,
        base_sha=_base_sha(
            host,
            workspace,
            host.catalog.integration_base(task["project"], task.get("workspace", {}).get("base_branch")),
        ),
        gate_mode="local",
        required_checks=[
            {
                "name": "local validation",
                "conclusion": "SUCCESS" if completed.returncode == 0 else "FAILURE",
                "url": "",
            }
        ],
        check_set_identity=command,
    )
    if completed.returncode == 0:
        return GateResult("green", "local validation passed", attestation=receipt)
    tail = (
        _tail((completed.stderr or completed.stdout or "").strip(), GATE_LOG_FRAGMENT_LINES) or "(no output)"
    )
    # Local output lacks Actions provenance and cannot classify infrastructure.
    return GateResult(
        "red", "local validation failed", tail, fingerprint=_fingerprint("local", tail), attestation=receipt
    )


# A ref update the remote refused because the branch is not where the push expected it to be.
# Under `--force-with-lease` every divergence collapses into `stale info`; the other two shapes
# are what the same divergence reads as when no lease was in play. Anything else a remote refuses
# — a protected branch, a pre-receive hook, a permission — is a determinate failure of this gate
# and stays a HostError, because it is not a statement about where the branch points.
_LEASE_REFUSED_RE = re.compile(
    r"(?im)^\s*!\s*\[(?:remote )?rejected\][^\n]*\((?:stale info|fetch first|non-fast-forward)\)\s*$"
)


def _published_ref_entry(record, branch: str) -> GatePublishedRef | None:
    """What the dispatcher last published to `branch`, or None when it published nothing there."""
    entry = getattr(record, "gate_published_ref", None)
    published = getattr(entry, "published_ref", None)
    if published is None:
        published = GatePublishedRef.from_json(entry)
    if published is None or published.branch != branch:
        return None
    return published


def _remember_published_ref(host, record, branch: str, sha: str) -> None:
    """Record the object id this gate just put on the remote branch, before another tick pushes.

    This is the lease the next publication is fenced against, and it is a durable record rather
    than a read taken at push time on purpose: a read taken now authorises whatever a foreign
    push already landed, which is precisely the thing the fence exists to refuse.
    """
    entry = GatePublishedRef(branch=branch, sha=sha)
    commit = getattr(host, "commit_gate_published_ref", None)
    if callable(commit):
        commit(record, entry)
    else:
        # Focused gate hosts own no dispatcher state file, but need the same tick-to-tick identity.
        record.gate_published_ref = entry.to_json()


def _remote_branch_sha(host, workspace: str, branch: str, project: str) -> str:
    """The object id `origin/<branch>` carries right now, or "" when the remote has no such ref."""
    listing = _backend_call(
        host,
        ["git", "-C", workspace, "ls-remote", "origin", f"refs/heads/{branch}"],
        "gate remote branch sha",
        project=project,
    )
    if listing.returncode != 0:
        raise HostError(
            "gate could not read the remote branch: "
            f"{_tail((listing.stderr or listing.stdout or '').strip())}"
        )
    first = (listing.stdout or "").strip().splitlines()
    sha = first[0].split()[0].strip() if first and first[0].split() else ""
    return sha if is_exact_sha(sha) else ""


def _contained_in_candidate(host, workspace: str, sha: str, head: str) -> bool:
    """Is `sha` already reachable from the candidate head, so publishing over it loses nothing?

    An unreadable answer is not containment: the gate refuses rather than guessing that a commit
    it cannot place is one it already has.
    """
    if not is_exact_sha(sha) or not is_exact_sha(head):
        return False
    contained = host.run_capture(
        ["git", "-C", workspace, "merge-base", "--is-ancestor", sha, head],
        "gate remote branch containment",
    )
    return contained.returncode == 0


def _push_leased(host, workspace: str, branch: str, expected: str, project: str):
    """Push the candidate under a lease on `expected` — the empty string meaning "must not exist"."""
    return _backend_call(
        host,
        [
            "git",
            "-C",
            workspace,
            "push",
            f"--force-with-lease=refs/heads/{branch}:{expected}",
            "origin",
            f"{branch}:refs/heads/{branch}",
        ],
        "gate publish branch",
        project=project,
    )


def _publish_branch(host, record, workspace: str, branch: str, sha: str, project: str) -> GateResult | None:
    """Publish a candidate branch under the last recorded object-id lease.

A rebased worker may rewrite only history previously published by this
dispatcher. Seed a missing lease from a remote read; a remote already
contained in HEAD may be re-leased once. Reject unrelated remote movement
instead of overwriting another writer.
    """
    published = _published_ref_entry(record, branch)
    expected = published.sha if published is not None else ""
    leased = bool(expected)
    if not leased:
        expected = _remote_branch_sha(host, workspace, branch, project)
    push = _push_leased(host, workspace, branch, expected, project)
    if push.returncode != 0:
        text = _tail((push.stderr or push.stdout or "").strip())
        if not _LEASE_REFUSED_RE.search(text):
            raise HostError(f"gate publish branch failed: {text}")
        observed = _remote_branch_sha(host, workspace, branch, project)
        if leased and _contained_in_candidate(host, workspace, observed, sha):
            push = _push_leased(host, workspace, branch, observed, project)
            text = _tail((push.stderr or push.stdout or "").strip())
        if push.returncode != 0:
            if not _LEASE_REFUSED_RE.search(text):
                raise HostError(f"gate publish branch failed: {text}")
            return _publication_refused(branch, sha, expected, observed)
    _remember_published_ref(host, record, branch, sha)
    return None


def _publication_refused(branch: str, sha: str, expected: str, observed: str) -> GateResult:
    """Return a typed red verdict for a remote branch that moved unexpectedly.

This is a lease/publication refusal before CI, not a failed build.
    """
    return GateResult(
        "red",
        f"branch `{branch}` was not published: `origin/{branch}` is at "
        f"`{(observed or '(absent)')[:12]}`, not the `{(expected or '(absent)')[:12]}` this "
        "dispatcher last published — someone else pushed to the card branch, so the candidate "
        f"`{sha[:12]}` was not written over it",
        f"expected origin/{branch} = {expected or '(absent)'}\n"
        f"observed origin/{branch} = {observed or '(absent)'}\n"
        f"candidate HEAD = {sha or '(unavailable)'}",
        fingerprint=_fingerprint("publish-lease", branch, expected, observed),
        failure_class="publication",
        failure_reason="remote-branch-moved",
    )


def _github_gate(
    host, task: dict, record, workspace: str, base: str, required: list[str] | None = None
) -> GateResult:
    branch = _legacy_worker_branch(task["ref"])
    sha = host._run(["git", "-C", workspace, "rev-parse", "HEAD"], "gate head sha").stdout.strip()
    refused = _publish_branch(host, record, workspace, branch, sha, task["project"])
    if refused is not None:
        return refused
    _ensure_pr(host, workspace, task, record, branch, base)
    repo = _name_with_owner(host, workspace)
    rerun_run_id = str(getattr(record, "gate_infrastructure_rerun_run_id", "") or "")
    rerun_sha = str(getattr(record, "gate_infrastructure_reruns_sha", "") or "")
    if rerun_run_id and rerun_sha == sha:
        status, _conclusion = _rerun_status(host, repo, rerun_run_id)
        if status != "COMPLETED":
            return GateResult(
                "pending",
                f"CI infrastructure rerun {rerun_run_id} is {status.lower()} for `{branch}` @ `{sha[:12]}`",
                failure_class="infrastructure",
                failure_reason=str(getattr(record, "gate_infrastructure_rerun_reason", "") or ""),
                failed_run_id=rerun_run_id,
                failed_run_repo=repo,
            )
    rollup, failed, checked = _poll_ci(host, repo, sha, required or [])
    receipt = mint_gate_receipt(
        validated_sha=sha,
        base_sha=_base_sha(host, workspace, base),
        gate_mode="github",
        required_checks=[_terminal_check(item) for item in checked],
        check_set_identity=json.dumps(
            {"required": sorted(required or [_check_name(item) for item in checked])},
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
    short = sha[:12] or sha
    if rollup == "SUCCESS":
        return GateResult("green", f"CI green for `{branch}` @ `{short}`", attestation=receipt)
    if rollup == "FAILURE":
        job = safe_one_line((failed or {}).get("name") or (failed or {}).get("context") or "?") or "?"
        fragment = _failed_log(host, repo, failed or {})
        where = f"job «{job}»"
        if fragment.step:
            where += f', step "{safe_one_line(fragment.step)}"'
        summary = f"CI red: {where} failed on `{branch}` @ `{short}`"
        if fragment.failure_class == "infrastructure":
            summary += f"; infrastructure failure: {fragment.failure_reason}"
        log = fragment.text if fragment.available else f"log unavailable: {fragment.reason}"
        cause = fragment.text if fragment.available else f"unavailable:{fragment.reason}"
        fingerprint = _fingerprint("github", job, fragment.step, cause)
        failed_run_id = _actions_run_id(failed or {})
        return GateResult(
            "red",
            summary,
            log,
            fingerprint=fingerprint,
            failure_class=fragment.failure_class,
            failure_reason=fragment.failure_reason,
            failed_run_id=failed_run_id,
            failed_run_repo=repo,
            attestation=receipt,
        )
    if not checked:
        # Zero checks on the candidate. Either they have not appeared yet, or nothing can ever
        # post them for this base — and those two must not read the same on the card, because the
        # second one otherwise burns the whole pending ceiling before anybody learns anything
        # (secretary-1541, issue:a858c044707de792a10f and issue:2c82ba8f5d1c3bf5b8cc).
        impossible = _impossible_trigger_reason(workspace, base)
        if impossible:
            return GateResult(
                "red",
                f"CI cannot run for `{branch}` @ `{short}`: {impossible}",
                f"candidate = {sha}\npull request base = {base}",
                fingerprint=_fingerprint("ci-trigger-impossible", base, branch),
                failure_class="topology",
                failure_reason="ci-trigger-impossible",
            )
    return GateResult(
        "pending",
        f"CI {rollup.lower()} for `{branch}` @ `{short}` — no terminal result yet",
        attestation=receipt,
    )


# Workflow trigger analysis: the difference between "checks have not appeared yet" and "no workflow
# can ever post them for this base".
_WORKFLOW_DIR = Path(".github") / "workflows"
_PR_EVENTS = ("pull_request", "pull_request_target")
# The event a release merge raises on the integration base, read by the post-merge CI watch.
_PUSH_EVENTS = ("push",)


def _branch_pattern_matches(pattern: str, branch: str) -> bool:
    """GitHub's branch filter glob, where `*` stops at a `/` and `**` does not."""
    parts = re.split(r"(\*\*|\*|\?)", pattern)
    expression = ""
    for part in parts:
        if part == "**":
            expression += ".*"
        elif part == "*":
            expression += "[^/]*"
        elif part == "?":
            expression += "[^/]"
        else:
            expression += re.escape(part)
    return re.fullmatch(expression, branch) is not None


def _event_triggers_base(spec: object, base: str, *, event: str = "pull_request") -> bool:
    """Would this `on: <event>:` block fire for `base` (the PR's base, or the pushed branch)?"""
    if not isinstance(spec, dict):
        # `pull_request:` or `push:` with no filter at all fires for every base.
        return True
    include = spec.get("branches")
    exclude = spec.get("branches-ignore")
    if event == "push" and include is None and exclude is None and ("tags" in spec or "tags-ignore" in spec):
        # A push filter that names only tags never fires for a branch push.
        return False
    if isinstance(exclude, list) and any(_branch_pattern_matches(str(item), base) for item in exclude):
        return False
    if isinstance(include, list):
        return any(_branch_pattern_matches(str(item), base) for item in include)
    return True


def _workflow_triggers_base(document: object, base: str, events: tuple[str, ...] = _PR_EVENTS) -> bool:
    """Does one parsed workflow declare a trigger among `events` that admits `base`?

    YAML 1.1 reads a bare `on:` key as the boolean true, which is why both spellings are looked up.
    """
    if not isinstance(document, dict):
        return False
    triggers = document.get("on", document.get(True))
    if isinstance(triggers, str):
        return triggers in events
    if isinstance(triggers, list):
        return any(str(item) in events for item in triggers)
    if isinstance(triggers, dict):
        for event in events:
            if event in triggers and _event_triggers_base(triggers[event], base, event=event):
                return True
    return False


def _workflows_missing_base(workspace: str, base: str, events: tuple[str, ...]) -> list[str] | None:
    """The workflow files read when it is established that none of them fires on `events` for
    `base`, or None when that cannot be established (no directory, no files, an unparsable file, or
    one workflow that does fire)."""
    root = Path(workspace) / _WORKFLOW_DIR
    try:
        files = sorted(path for path in root.iterdir() if path.suffix in (".yml", ".yaml") and path.is_file())
    except OSError:
        return None
    if not files:
        return None
    names = []
    for path in files:
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError):
            return None
        if _workflow_triggers_base(document, base, events):
            return None
        names.append(path.name)
    return names


def _listed_workflows(names: list[str]) -> str:
    return ", ".join(names[:6]) + (", …" if len(names) > 6 else "")


def _impossible_trigger_reason(workspace: str, base: str) -> str:
    """Explain when this candidate has no workflow capable of checking a PR to base.

Only conclude impossibility if every workflow parses and none matches.
Missing, invalid or admitting workflows remain pending: external checks
may also supply evidence.
    """
    names = _workflows_missing_base(workspace, base, _PR_EVENTS)
    if names is None:
        return ""
    return (
        f"no workflow in this candidate triggers on a pull request into `{base}` "
        f"({len(names)} workflow file(s) read: {_listed_workflows(names)}), so GitHub will never create a check-run "
        "for it: this rollup is empty because nothing can fill it, not because CI is still starting"
    )


def push_trigger_absent_reason(workspace: str, base: str) -> str:
    """Why a push to `base` starts no workflow of this checkout, or "".

    The same trigger analysis as `_impossible_trigger_reason`, asked about the `push` event a
    release merge raises on the integration base, with the same rule: only a positive answer counts,
    and anything unreadable leaves the post-merge watch waiting for a run.
    """
    names = _workflows_missing_base(workspace, base, _PUSH_EVENTS)
    if names is None:
        return ""
    return (
        f"no workflow triggers on a push to `{base}` ({len(names)} workflow file(s) read: "
        f"{_listed_workflows(names)}), so the merge starts no CI run on the base"
    )


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
            f"gate rerun failed CI: {_tail((completed.stderr or completed.stdout or '').strip())}"
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
            f"gate infrastructure rerun status failed: {_tail((completed.stderr or completed.stdout or '').strip())}"
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
        detail = _tail((completed.stderr or completed.stdout or "").strip()) or "(no output)"
        raise HostError(f"gate could not resolve the repository name: {detail}")
    return name


def _pr_title(task: dict, branch: str) -> str:
    """`<ref>: <card title>` — the one line a reader of `main`'s history gets."""
    ref = safe_one_line(task.get("ref") or "", limit=80) or branch
    title = safe_one_line(task.get("title") or "", limit=180)
    # GitHub refuses a title longer than 256 characters, and a card title is free text.
    return (f"{ref}: {title}" if title else ref)[:240].rstrip()


def _pr_section(text: object) -> str:
    """One board-sourced block on its way into a PR body: scrubbed and bounded, or empty."""
    cleaned = scrub_host_output(str(text or "")).replace("\r\n", "\n").strip()
    if len(cleaned) <= PR_BODY_SECTION_CHARS:
        return cleaned
    return cleaned[:PR_BODY_SECTION_CHARS].rstrip() + "\n\n… (truncated; the full text is on the card)"


def _pr_digest(title: str, body: str) -> str:
    """Digest a PR title and body as exact bytes.

Do not canonicalize either: authorship comparisons use the GitHub
read-back identity and must detect human edits.
    """
    payload = f"{title}\n\n{body}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _pr_body(task: dict, branch: str, base: str) -> str:
    """The PR description, built deterministically from the card and the worker's own report.

    Every source is optional and a missing one is omitted; no model is asked anything, so the same
    inputs always render the same body and `_refresh_pr` can compare its way to a no-op. Nothing
    marks the text as the gate's: a marker in a body proves nothing about who wrote the body
    around it, so authorship is kept outside the pull request, in `record.gate_pr_authorship`.
    """
    ref = safe_one_line(task.get("ref") or "", limit=80) or branch
    card_title = safe_one_line(task.get("title") or "", limit=180)
    heading = f"**Card:** `{ref}`" + (f" — {card_title}" if card_title else "")
    parts = [heading, f"**Branch:** `{branch}` → `{base}`"]
    statement = _pr_section(task.get("description"))
    if statement:
        parts += ["", "## What the card asks for", "", statement]
    report = _pr_section(_last_marker_body(task, "report:done") or "")
    if report:
        parts += ["", "## What the worker reports", "", report]
    parts += ["", "---", "", _PR_BODY_FOOTER]
    return "\n".join(parts)


def _pr_authorship(record) -> GatePrAuthorship | None:
    """What the gate durably recorded about the last pull request it wrote for this card."""
    entry = getattr(record, "gate_pr_authorship", None)
    authorship = getattr(entry, "authorship", None)
    return authorship if authorship is not None else GatePrAuthorship.from_json(entry)


def _remember_pr(host, workspace: str, record, number: int, title: str, body: str) -> None:
    """Persist ownership of a gate-authored PR after confirming its remote write.

Ownership digest hashes GitHub's read-back title/body; sent digest hashes
what the gate attempted to publish. If read-back fails, do not claim ownership.
    """
    stored = _pr_view(host, workspace, number)
    if stored is None:
        return
    host.commit_gate_pr_authorship(
        record,
        GatePrAuthorship(
            number=int(number),
            digest=_pr_digest(str(stored.get("title") or ""), str(stored.get("body") or "")),
            sent=_pr_digest(title, body),
        ),
    )


def _gate_owns_pr(record, number: int, title: str, body: str) -> bool:
    """Whether the gate may replace this PR's title and body.

Require a matching durable authorship record for the same PR and exact remote
text. Missing, different or stale ownership means leave human prose untouched.
    """
    entry = _pr_authorship(record)
    if entry is None or entry.number != int(number):
        return False
    return entry.digest == _pr_digest(title, body)


def _pr_view(host, workspace: str, number: int) -> dict | None:
    """Current title and body of PR `number`, or None when the backend would not say.

    None is deliberately not distinguished from "unreadable": a PR whose current text the gate
    cannot read is one it leaves alone.
    """
    try:
        completed = _backend_call(
            host,
            ["gh", "pr", "view", str(number), "--json", "title,body"],
            "gate pr view",
            cwd=Path(workspace),
        )
    except GateTransportError:
        return None
    if completed.returncode != 0:
        return None
    try:
        data = json.loads((completed.stdout or "").strip() or "{}")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _refresh_pr(host, workspace: str, record, number: int, title: str, body: str) -> None:
    """Update an open gate-authored PR's title and body when necessary.

Never read or change an unowned PR. Leave human-edited prose alone when
its digest differs. Use the sent digest to avoid duplicate updates.
A failed prose update does not invalidate already-green CI.
    """
    entry = _pr_authorship(record)
    if entry is None or entry.number != int(number):
        return
    if _pr_digest(title, body) == entry.sent:
        return
    current = _pr_view(host, workspace, number)
    if current is None:
        return
    if not _gate_owns_pr(record, number, str(current.get("title") or ""), str(current.get("body") or "")):
        return
    try:
        completed = _backend_call(
            host,
            ["gh", "pr", "edit", str(number), "--title", title, "--body", body],
            "gate pr edit",
            cwd=Path(workspace),
        )
    except GateTransportError:
        return
    if completed.returncode != 0:
        # GitHub refused the edit, so the pull request still carries the recorded text and the
        # record still describes it: the next tick tries again rather than disowning the PR.
        return
    _remember_pr(host, workspace, record, number, title, body)


def _ensure_pr(host, workspace: str, task: dict, record, branch: str, base: str) -> None:
    """Ensure an open candidate PR into the project integration base.

Its pull_request CI needs an open PR, not merely a feature-branch push.
Never treat a failed creation as a successful validation launch.
    """
    title = _pr_title(task, branch)
    body = _pr_body(task, branch, base)
    open_pr = _open_pr(host, workspace, branch)
    if open_pr is not None:
        number = int(open_pr["number"])
        _retarget_pr(host, workspace, number, str(open_pr["base"]), base)
        _refresh_pr(host, workspace, record, number, title, body)
        return
    created = _backend_call(
        host,
        ["gh", "pr", "create", "--base", base, "--head", branch, "--title", title, "--body", body],
        "gate pr create",
        cwd=Path(workspace),
    )
    if created.returncode == 0:
        _remember_created_pr(host, workspace, record, branch, title, body)
        return
    text = (created.stderr or created.stdout or "").strip()
    try:
        # gh refusing to duplicate a PR, or a concurrent tick that opened one first: the create
        # answered "no", and it is only tolerated when the backend also answers that a PR is open.
        if _open_pr_number(host, workspace, branch) is not None:
            return
    except HostError as exc:
        if isinstance(exc, GateTransportError):
            raise
        raise HostError(
            f"gate could not open a PR for {branch!r}: {_tail(text)}; and the open-PR probe failed too: {exc}"
        ) from None
    raise HostError(f"gate could not open a PR for {branch!r}: {_tail(text)}")


def _retarget_pr(host, workspace: str, number: int, observed: str, base: str) -> None:
    """Repair a gate-owned pipeline PR's integration base and retrigger its CI.

For a misbased pipeline/<ref> PR, change only the base then close/reopen to
emit the reopened event; changing base alone does not rerun pull_request CI.
This topology fix does not edit the candidate, re-run the worker or overwrite
human PR prose. Determinate failures to edit/reopen are gate failures.
    """
    if observed == base:
        return
    for args, label in (
        (["gh", "pr", "edit", str(number), "--base", base], "gate pr retarget"),
        (["gh", "pr", "close", str(number)], "gate pr close"),
        (["gh", "pr", "reopen", str(number)], "gate pr reopen"),
    ):
        completed = _backend_call(host, args, label, cwd=Path(workspace))
        if completed.returncode != 0:
            raise HostError(
                f"{label} failed for PR #{number} (base {observed!r} -> {base!r}): "
                f"{_tail((completed.stderr or completed.stdout or '').strip())}"
            )


def _remember_created_pr(host, workspace: str, record, branch: str, title: str, body: str) -> None:
    """Record the PR number actually created by the gate.

Read the result from the backend rather than assuming creation succeeded
or deriving identity from the card.
    """
    try:
        number = _open_pr_number(host, workspace, branch)
    except HostError:
        return
    if number is None:
        return
    _remember_pr(host, workspace, record, number, title, body)


def _open_pr(host, workspace: str, branch: str) -> dict | None:
    """Find an open PR by candidate branch; return its number and base.

Read both from one PR listing without inspecting its prose. Empty results
mean no PR; a failed backend call must not be mistaken for an empty result.
    """
    completed = _backend_call(
        host,
        ["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "number,baseRefName"],
        "gate pr list",
        cwd=Path(workspace),
    )
    if completed.returncode != 0:
        raise HostError(f"gate pr list failed: {_tail((completed.stderr or completed.stdout or '').strip())}")
    text = (completed.stdout or "").strip()
    if not text:
        return None
    try:
        rows = json.loads(text)
    except ValueError:
        raise HostError("gate pr list returned invalid JSON") from None
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        return None
    try:
        number = int(rows[0].get("number"))
    except (TypeError, ValueError):
        return None
    return {"number": number, "base": str(rows[0].get("baseRefName") or "")}


def _open_pr_number(host, workspace: str, branch: str) -> int | None:
    """Number of the open PR whose head is `branch`, or None when the backend answered none is."""
    entry = _open_pr(host, workspace, branch)
    return entry["number"] if entry else None


def _poll_ci(
    host, repo: str, sha: str, required: list[str] | None = None
) -> tuple[str, dict | None, list[dict]]:
    """Combined CI rollup for `sha`: GitHub-Actions check-runs plus legacy commit statuses,
    narrowed to `required` when the adapter declares a required set."""
    items: list[dict] = []
    runs = _gh_api(host, f"repos/{repo}/commits/{sha}/check-runs", jq=".check_runs")
    if isinstance(runs, list):
        items.extend(item for item in runs if isinstance(item, dict))
    statuses = _gh_api(host, f"repos/{repo}/commits/{sha}/status", jq=".statuses")
    if isinstance(statuses, list):
        items.extend(item for item in statuses if isinstance(item, dict))
    rollup, failed = _rollup(items, required)
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


def _head_sha(host, workspace: str) -> str:
    if not workspace:
        return ""
    try:
        return host._run(
            ["git", "-C", workspace, "rev-parse", "HEAD"], "gate attestation head"
        ).stdout.strip()
    except HostError:
        return ""


def _base_sha(host, workspace: str, base: str) -> str:
    if not workspace or not base:
        return ""
    try:
        return host._run(
            ["git", "-C", workspace, "rev-parse", f"origin/{base}"], "gate attestation base"
        ).stdout.strip()
    except HostError:
        return ""


def _gh_api(host, path: str, *, jq: str):
    completed = _backend_call(host, ["gh", "api", path, "--jq", jq], "gate gh api")
    if completed.returncode != 0:
        raise HostError(f"gate gh api failed: {_tail((completed.stderr or completed.stdout or '').strip())}")
    text = (completed.stdout or "").strip()
    if not text:
        return []
    try:
        return json.loads(text)
    except ValueError:
        return []


def _check_result(item: dict) -> str:
    """One rollup entry -> 'pass' | 'fail' | 'pending'. Handles both a GitHub-Actions check-run
    (status/conclusion) and a legacy commit status (state)."""
    if "state" in item and "status" not in item:
        state = str(item.get("state", "")).upper()
        if state in ("FAILURE", "ERROR"):
            return "fail"
        if state == "SUCCESS":
            return "pass"
        return "pending"
    if str(item.get("status", "")).upper() != "COMPLETED":
        return "pending"
    return "fail" if str(item.get("conclusion", "")).upper() in _FAIL_CONCLUSIONS else "pass"


def _check_name(item: dict) -> str:
    """Declared-name key of a rollup entry: the Actions check-run `name`, or the legacy commit
    status `context`."""
    return str(item.get("name") or item.get("context") or "").strip()


def _rollup(items: list[dict], required: list[str] | None = None) -> tuple[str, dict | None]:
    """Combine CI checks into an overall status.

Keep PENDING while any required job runs, even beside an early failure.
Only after all settle should the aggregate report FAILURE or SUCCESS.
    """
    if required:
        by_name = {name: [] for name in required}
        for item in items:
            bucket = by_name.get(_check_name(item))
            if bucket is not None:
                bucket.append(item)
        items = [item for name in required for item in by_name[name]]
        if any(not bucket for bucket in by_name.values()):
            # a required check has not been posted for this sha yet
            return "PENDING", None
    if not items:
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
    match = _RUN_URL_RE.search(
        str(
            item.get("details_url")
            or item.get("html_url")
            or item.get("targetUrl")
            or item.get("target_url")
            or ""
        )
    )
    if not match:
        return _LogFragment(available=False, reason="the entry is not an Actions run (no run link)")
    run_id = match.group(2)
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
