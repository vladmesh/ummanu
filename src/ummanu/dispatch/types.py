"""Shared dispatcher exceptions and selectors."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class DispatcherError(Exception):
    def __init__(self, code: str, message: str, exit_code: int = 2) -> None:
        self.code = code
        self.message = message
        self.exit_code = exit_code
        super().__init__(message)


class HostError(Exception):
    """A host operation the dispatcher asked for did not happen.

    `bring_up_cause` is the one optional thing a raiser may say about a failure beyond its message:
    the enumerated bring-up cause from `ummanu.dispatch.launch`, when the raise site — and only the raise
    site — knows which one it is. Everything else about a bring-up failure is classified from the
    exception's type by the one classifier, so nothing here decides a class: the vocabulary and the
    cause-to-class mapping live in `ummanu.dispatch.launch.classify_bring_up_failure`, and a cause this
    field carries that the classifier does not know is ignored rather than trusted.
    """

    def __init__(self, *args: Any, bring_up_cause: str = "") -> None:
        super().__init__(*args)
        self.bring_up_cause = bring_up_cause


class OwnershipChanged(HostError):
    """An unlocked preparation lost admission; preserve the newer board owner."""


#: `ummanu.dispatch.launch.CAUSE_WORKSPACE_CONTRACT`, spelled here because `launch` imports this
#: module: a legacy record is this card's own contract failing, never a host to retry.
_LEGACY_RECORD_BRING_UP_CAUSE = "workspace_contract"


class LegacyDispatcherRecord(HostError):
    """A dispatcher record written while heads were Orca panes reached a verb that would act on it.

    One typed refusal for every verb — launch, deliver, stop and tear down — against a record whose
    workspace is an Orca worktree rather than a git worktree the host owns, or whose head run is a
    legacy record (`head_runtime_backends.is_legacy_record`). The record stays readable; nothing is
    launched into it, delivered to it, stopped through a pane or re-placed. The message names the
    record, so the card that carries it goes Blocked with a reason a human can act on.
    """

    def __init__(self, subject: str, reason: str, *, verb: str) -> None:
        super().__init__(
            f"legacy dispatcher record: refused to {verb} {subject}: {reason}; it was written while "
            "heads ran in Orca panes, which this dispatcher no longer drives",
            bring_up_cause=_LEGACY_RECORD_BRING_UP_CAUSE,
        )
        self.subject = subject
        self.reason = reason
        self.verb = verb


class GateTransportError(HostError):
    """The gate could not reach its backend, so no verdict was received at all.

    A question that never got an answer is not a red gate (secretary-1164). A TLS handshake
    timeout, a DNS failure, a dropped connection or a 5xx from GitHub itself says nothing about
    the code under validation, and treating the absence of an answer as a negative one blocked a
    card whose required check was in fact green. The dispatcher keeps such a card exactly where it
    is and asks again on the next tick, bounded, instead of deciding on silence.
    """


class ProjectGitAccessError(HostError):
    """A registered project's remote Git access was refused by name, before or by the remote.

    A missing, locked or rejected managed GitHub credential, an unsupported HTTPS host or an
    unreadable origin is a determinate answer about access. It is deliberately not a
    `GateTransportError`: asking again on the next tick cannot change it, so it never enters the
    bounded transport retry. `code` is one of `github_credential.PROJECT_ACCESS_REFUSALS` and
    `reason` is fixed, secret-free vocabulary.
    """

    def __init__(self, project: str, code: str, reason: str) -> None:
        super().__init__(f"project {project!r} Git access refused ({code}): {reason}")
        self.project = project
        self.code = code
        self.reason = reason


class HeadLaunchAborted(HostError):
    """A worker or reviewer bring-up that failed after its terminal was already created.

    The same ambiguity `ObserverLaunchAborted` covers for the observer, and it is answered the same
    way. The bring-up did not finish, but something of it may still be running, so the failure
    carries the pane it opened and the heartbeat that head writes. The caller keeps the launch
    intent instead of blocking the card on it: the next tick reads the heartbeat and either adopts
    the head or stops what is left of it. Clearing the intent and dropping the record here would
    leave a live head with nothing pointing at it, which is the second head this contour exists to
    prevent.
    """

    def __init__(
        self,
        message: str,
        *,
        handle: str = "",
        leaf: str = "",
        workspace: str = "",
        pid_file: str = "",
        evidence: dict[str, Any] | None = None,
        head_run: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.handle = handle
        self.leaf = leaf
        self.workspace = workspace
        self.pid_file = pid_file
        # The run of the head this bring-up did start, where it got that far (secretary-1414). An
        # abort is the case where the pane may be live, so the identity of what is in it travels
        # with the failure: the intent keeps it, and the adoption continues that run.
        self.head_run = dict(head_run or {})
        # What the shared delivery boundary saw, when this bring-up failed delivering a prompt.
        # It travels with the ambiguity rather than being read off a pane nobody may touch: the
        # caller persists it before it decides anything about the head that may still be running.
        self.evidence = dict(evidence or {})


@dataclass(frozen=True)
class ReviewLaunch:
    """What a reviewer bring-up hands back to the runtime: the pane the reviewer runs in and the
    commit its checkout was pinned at once the worker head was shut down."""

    handle: str
    leaf: str = ""
    commit: str = ""
    # The launch configuration of the reviewer head this bring-up started, snapshotted by the
    # launcher itself (secretary-716). The runtime writes it to the routing journal as-is.
    run: dict[str, Any] = field(default_factory=dict)
    # The reviewer's own head run, as the three head operations keep it (secretary-1414). Distinct
    # from `run` above, which is the routing snapshot of the configuration this head launched with:
    # this is the state of the head itself — the identity a later stop addresses, and the lifecycle
    # that stop moves. The caller writes it onto the record, which is where it becomes durable.
    head_run: dict[str, Any] = field(default_factory=dict)
    delivery_evidence: dict[str, Any] = field(default_factory=dict)
    # A successful standalone recovery after a recognised split refusal.
    fallback_reason: str = ""


@dataclass(frozen=True)
class MergeLanding:
    """What a release merge actually landed, as `complete_green` hands it back.

    `sha` is the commit now on the integration base: the PR's merge commit, or the pushed branch
    head. It can be empty only on the GitHub path, when the merge went through but its commit could
    not be read back; `branch` then lets the post-merge watch read it from the PR later. `ci` is the
    project's declared validation mode, which decides whether the base has a CI run to wait for.
    A `complete_green` that merged nothing returns None, never a landing.
    """

    sha: str
    base: str
    path: str  # "github-pr" | "push"
    ci: str = "none"
    branch: str = ""


def review_pane_label(reference: str) -> str:
    """Stable human-readable label for the reviewer pane. Carries the card reference and the role
    so an operator can tell the two panes of one worktree apart in the Orca client. Lifecycle
    checks key off the persisted handle, not this label: a head overwrites the terminal title with
    its own OSC sequence seconds after launch, and a title-only check would then read the reviewer
    as gone (or as the worker)."""
    return f"{reference} reviewer"


# Who a head's stop was initiated by, as the head run records it (secretary-1412). These live here
# rather than beside the runtime because every module that performs a stop has to name one, and the
# runtime imports those modules. A stop with no initiator is impossible at the operation's
# signature; naming them here is what keeps the names from drifting per call site.
STOPPED_BY_DISPATCHER = "dispatcher"
STOPPED_BY_REVIEW_FREEZE = "review-freeze"
STOPPED_BY_REPLACEMENT = "replacement"
STOPPED_BY_OPERATOR = "operator"
STOPPED_BY_RECONCILIATION = "reconciliation"
STOPPED_BY_LAUNCH_RECOVERY = "launch-recovery"
# The reviewer's round is over: a red verdict handing the checkout back, a green one parking it,
# a parked round released for rework. All of them are the verdict ending that head, which is the
# distinction an operator reads this field for — a reviewer that finished is not a reviewer that
# was killed (secretary-1414).
STOPPED_BY_REVIEW_VERDICT = "review-verdict"
# The wait watchdog ending a head that stopped answering: the respawn of a silent reviewer and the
# escalation that follows the second stall. The head may well still be running, which is exactly
# why the record has to name who decided it should not be.
STOPPED_BY_WATCHDOG = "watchdog"
# A head whose first turn ended on a provider error, ended so its role can move to the next head of
# its fallback chain (secretary-1799). Not the watchdog: nothing waited for it to fall silent.
STOPPED_BY_PROVIDER_FAILURE = "provider-failure"
