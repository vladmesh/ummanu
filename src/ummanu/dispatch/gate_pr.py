"""Candidate branch publication leases and PR text/authorship ownership.

Remote questions resolve through github_ci; publication records are committed
through the host before a future tick can reuse their ownership.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from ummanu.candidate_history import without_ai_attribution
from ummanu.dispatch import github_ci
from ummanu.dispatch.gate_receipt import is_exact_sha
from ummanu.dispatch.gate_types import PR_BODY_SECTION_CHARS, GateResult, _fingerprint
from ummanu.dispatch.helpers import _last_marker_body, _tail, safe_one_line, scrub_host_output
from ummanu.dispatch.state import GatePrAuthorship, GatePublishedRef
from ummanu.dispatch.types import GateTransportError, HostError

_PR_BODY_FOOTER = (
    "Opened by the ummanu CI gate so that the `pull_request` workflow runs. "
    "Title and body are built from the card and the worker's done report."
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
    listing = github_ci._backend_call(
        host,
        ["git", "-C", workspace, "ls-remote", "origin", f"refs/heads/{branch}"],
        "gate remote branch sha",
        project=project,
    )
    if listing.returncode != 0:
        raise HostError(
            "gate could not read the remote branch: "
            f"{_tail(github_ci._answer(listing))}"
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
    return github_ci._backend_call(
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
        text = _tail(github_ci._answer(push))
        if not _LEASE_REFUSED_RE.search(text):
            raise HostError(f"gate publish branch failed: {text}")
        observed = _remote_branch_sha(host, workspace, branch, project)
        if leased and _contained_in_candidate(host, workspace, observed, sha):
            push = _push_leased(host, workspace, branch, observed, project)
            text = _tail(github_ci._answer(push))
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
        completed = github_ci._backend_call(
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

Rewrite an owned PR whose remote text still matches the recorded digest; use
the sent digest to avoid duplicate updates. In any other PR change only AI
attribution lines, which are removed. A failed prose update does not
invalidate already-green CI.
    """
    entry = _pr_authorship(record)
    if entry is not None and entry.number == int(number) and _pr_digest(title, body) == entry.sent:
        return
    current = _pr_view(host, workspace, number)
    if current is None:
        return
    current_body = str(current.get("body") or "")
    if not _gate_owns_pr(record, number, str(current.get("title") or ""), current_body):
        cleaned = without_ai_attribution(current_body)
        if cleaned != current_body:
            _edit_pr(host, workspace, number, ["--body", cleaned])
        return
    if _edit_pr(host, workspace, number, ["--title", title, "--body", body]):
        _remember_pr(host, workspace, record, number, title, body)


def _edit_pr(host, workspace: str, number: int, fields: list[str]) -> bool:
    """Whether GitHub accepted the edit; a refusal leaves the record as it is for the next tick."""
    try:
        completed = github_ci._backend_call(
            host,
            ["gh", "pr", "edit", str(number), *fields],
            "gate pr edit",
            cwd=Path(workspace),
        )
    except GateTransportError:
        return False
    return completed.returncode == 0


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
    created = github_ci._backend_call(
        host,
        ["gh", "pr", "create", "--base", base, "--head", branch, "--title", title, "--body", body],
        "gate pr create",
        cwd=Path(workspace),
    )
    if created.returncode == 0:
        _remember_created_pr(host, workspace, record, branch, title, body)
        return
    text = github_ci._answer(created)
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
        completed = github_ci._backend_call(host, args, label, cwd=Path(workspace))
        if completed.returncode != 0:
            raise HostError(
                f"{label} failed for PR #{number} (base {observed!r} -> {base!r}): "
                f"{_tail(github_ci._answer(completed))}"
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
    completed = github_ci._backend_call(
        host,
        ["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "number,baseRefName"],
        "gate pr list",
        cwd=Path(workspace),
    )
    if completed.returncode != 0:
        raise HostError(f"gate pr list failed: {_tail(github_ci._answer(completed))}")
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
