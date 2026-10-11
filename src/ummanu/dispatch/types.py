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

    Only the raiser identifies bring_up_cause; the shared classifier interprets known causes.
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

    Refuse legacy launch/deliver/stop without destroying the record.
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

    No backend answer is not a red verdict; retry boundedly without moving the card.
    """


class ProjectGitAccessError(HostError):
    """A registered project's remote Git access was refused by name, before or by the remote.

    Determinate Git access refusal is not retriable gate transport failure.
    """

    def __init__(self, project: str, code: str, reason: str) -> None:
        super().__init__(f"project {project!r} Git access refused ({code}): {reason}")
        self.project = project
        self.code = code
        self.reason = reason


class HeadLaunchAborted(HostError):
    """A worker or reviewer bring-up that failed after its terminal was already created.

    Preserve pane and heartbeat evidence for adoption or confirmed stop.
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

    sha is the integration-base commit; None means no landing.
    """

    sha: str
    base: str
    path: str  # "github-pr" | "push"
    ci: str = "none"
    branch: str = ""


def review_pane_label(reference: str) -> str:
    """Stable human-readable label for the reviewer pane.

    Include both card reference and head role.
    """
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
