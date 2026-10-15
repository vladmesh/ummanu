"""Mechanical gate mode, candidate preflight and local/base orchestration.

GitHub API, publication and workflow trigger analysis have explicit lower owners.
Gate lifecycle state transitions remain separate from review and merge policy.
"""

from __future__ import annotations

import json
from pathlib import Path

from ummanu.candidate_history import Commit, ai_attributions, parse_shas, repair_message
from ummanu.dispatch import gate_pr, gate_workflows, github_ci
from ummanu.dispatch.gate_receipt import is_exact_sha, mint_gate_receipt
from ummanu.dispatch.gate_types import (
    GATE_INFRASTRUCTURE_RERUN_MAX_ATTEMPTS as GATE_INFRASTRUCTURE_RERUN_MAX_ATTEMPTS,
    GATE_PENDING_STALL_SECONDS as GATE_PENDING_STALL_SECONDS,
    GATE_TRANSPORT_MAX_ATTEMPTS as GATE_TRANSPORT_MAX_ATTEMPTS,
    GateResult as GateResult,
    _fingerprint,
)
from ummanu.dispatch.helpers import _legacy_worker_branch, _tail, safe_one_line
from ummanu.dispatch.state import DispatcherRecord
from ummanu.dispatch.types import HostError


def reset_infrastructure_reruns(record: DispatcherRecord) -> None:
    """Clear the per-SHA bounded infrastructure-rerun and rerun-transport state."""
    record.gate_infrastructure_reruns_sha = ""
    record.gate_infrastructure_reruns = 0
    record.gate_infrastructure_rerun_run_id = ""
    record.gate_infrastructure_rerun_reason = ""
    record.gate_rerun_transport_failures = 0
    record.gate_rerun_transport_error = ""


def gate_check(host, task: dict, record) -> GateResult:
    """Run the mechanical gate for this task in the worker checkout.

Raise GateTransportError for unanswered backend calls; determinate red
verdicts stay ordinary GateResult outcomes.
    """
    if getattr(host, "mode", "real") == "noop":
        # A noop proves no command ran.  It may preserve dispatcher control flow, but must never
        # look like reusable validation evidence.
        return GateResult("green", "noop gate")
    ci = github_ci._validation(host, task).get("ci") or "none"
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
        return _github_gate(host, task, record, workspace, base, github_ci._required_checks(host, task))
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
            f"{_tail(github_ci._answer(listing))}"
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
                f"{_tail(github_ci._answer(message))}"
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


def validation_ci(host, task: dict) -> str:
    """The project's declared mechanical-gate mode: "local" | "github" | "none". Read by the
    dispatcher's merge step to pick a github PR merge over the local fast-forward."""
    return github_ci._validation(host, task).get("ci") or "none"


def _recover_base(host, workspace: str, base: str, project: str) -> str:
    """Fast-forward the worker branch onto the latest base. Returns "clean" (already current),
    "recovered" (merged base in), or "conflict" (a textual conflict, aborted)."""
    fetch = github_ci._backend_call(
        host, ["git", "-C", workspace, "fetch", "origin", base], "gate base fetch", project=project
    )
    if fetch.returncode != 0:
        # An answered refusal (a base branch the remote does not have, a rejected credential) is
        # a determinate gate failure, exactly as it was before this call moved here.
        raise HostError(
            f"gate base fetch (git fetch) refused: {_tail(github_ci._answer(fetch))}"
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
    command = github_ci._validation(host, task).get("command")
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
        _tail(github_ci._answer(completed), github_ci.GATE_LOG_FRAGMENT_LINES) or "(no output)"
    )
    # Local output lacks Actions provenance and cannot classify infrastructure.
    return GateResult(
        "red", "local validation failed", tail, fingerprint=_fingerprint("local", tail), attestation=receipt
    )


def _github_gate(
    host, task: dict, record, workspace: str, base: str, required: list[str] | None = None
) -> GateResult:
    branch = _legacy_worker_branch(task["ref"])
    sha = _rev_parse(host, workspace, "HEAD", "gate head sha")
    refused = gate_pr._publish_branch(host, record, workspace, branch, sha, task["project"])
    if refused is not None:
        return refused
    gate_pr._ensure_pr(host, workspace, task, record, branch, base)
    repo = github_ci._name_with_owner(host, workspace)
    rerun_run_id = str(getattr(record, "gate_infrastructure_rerun_run_id", "") or "")
    rerun_sha = str(getattr(record, "gate_infrastructure_reruns_sha", "") or "")
    if rerun_run_id and rerun_sha == sha:
        status, _conclusion = github_ci._rerun_status(host, repo, rerun_run_id)
        if status != "COMPLETED":
            return GateResult(
                "pending",
                f"CI infrastructure rerun {rerun_run_id} is {status.lower()} for `{branch}` @ `{sha[:12]}`",
                failure_class="infrastructure",
                failure_reason=str(getattr(record, "gate_infrastructure_rerun_reason", "") or ""),
                failed_run_id=rerun_run_id,
                failed_run_repo=repo,
            )
    rollup, failed, checked = github_ci._poll_ci(host, repo, sha, required or [])
    receipt = mint_gate_receipt(
        validated_sha=sha,
        base_sha=_base_sha(host, workspace, base),
        gate_mode="github",
        required_checks=[github_ci._terminal_check(item) for item in checked],
        check_set_identity=json.dumps(
            {"required": sorted(required or [github_ci._check_name(item) for item in checked])},
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
    short = sha[:12] or sha
    if rollup == "SUCCESS":
        return GateResult("green", f"CI green for `{branch}` @ `{short}`", attestation=receipt)
    if rollup == "FAILURE":
        job = safe_one_line((failed or {}).get("name") or (failed or {}).get("context") or "?") or "?"
        if (failed or {}).get("conclusion") == github_ci.MISSING_CONCLUSION:
            return GateResult(
                "red",
                f"CI red: required check «{job}» was never posted on `{branch}` @ `{short}`"
                " and every workflow run for it has completed",
                f"candidate = {sha}\nrequired checks = {', '.join(required or [])}",
                fingerprint=_fingerprint("github-missing-check", job, sha),
                attestation=receipt,
            )
        fragment = github_ci._failed_log(host, repo, failed or {})
        where = f"job «{job}»"
        if fragment.step:
            where += f', step "{safe_one_line(fragment.step)}"'
        summary = f"CI red: {where} failed on `{branch}` @ `{short}`"
        if fragment.failure_class == "infrastructure":
            summary += f"; infrastructure failure: {fragment.failure_reason}"
        log = fragment.text if fragment.available else f"log unavailable: {fragment.reason}"
        cause = fragment.text if fragment.available else f"unavailable:{fragment.reason}"
        fingerprint = _fingerprint("github", job, fragment.step, cause)
        failed_run_id = github_ci._actions_run_id(failed or {})
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
        impossible = gate_workflows._impossible_trigger_reason(workspace, base)
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


def _rev_parse(host, workspace: str, rev: str, label: str) -> str:
    return host._run(["git", "-C", workspace, "rev-parse", rev], label).stdout.strip()


def _head_sha(host, workspace: str) -> str:
    if not workspace:
        return ""
    try:
        return _rev_parse(host, workspace, "HEAD", "gate attestation head")
    except HostError:
        return ""


def _base_sha(host, workspace: str, base: str) -> str:
    if not workspace or not base:
        return ""
    try:
        return _rev_parse(host, workspace, f"origin/{base}", "gate attestation base")
    except HostError:
        return ""
