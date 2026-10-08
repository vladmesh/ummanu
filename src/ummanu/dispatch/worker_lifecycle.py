"""Durable state for handing a worker across mechanical validation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ummanu.runtime.head.handoff import PromptHandoff
from ummanu.runtime.head_run_binding import head_run_binding

BUSY_RETRY_INITIAL_SECONDS = 30
BUSY_RETRY_MAX_SECONDS = 5 * 60
# A readiness refusal deliberately is not a failure on its own.  It is nevertheless not allowed
# to keep one red continuation in the same backoff forever: after this many *unchanged provider*
# observations the dispatcher takes the one safe-recovery rung it knows about, or goes to the
# already identity-fenced replacement path.  This count is kept on the liveness record, not in a
# process-local retry loop, so a restart does not buy another episode.
CONTINUATION_NO_PROGRESS_BUSY_ATTEMPTS = 3
CONTINUATION_LIVENESS_VERSION = 1


class ContinuationLivenessState(StrEnum):
    """What the provider evidence says, never an inference from screen text."""

    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"
    BASELINE_PENDING = "baseline_pending"
    BASELINED = "baselined"
    STALLED = "stalled"
    PROGRESSED = "progressed"


class ContinuationProviderCondition(StrEnum):
    """Narrow provider-source conditions a retained-continuation policy may act on.

    Protocol classifications, not host error text: an unavailable transport, malformed evidence or a
    source that fails the HeadRun fence must not acquire this condition merely because its diagnostic
    resembles an unbound source.
    """

    LEGACY_UNBOUND_V1 = "legacy_unbound_v1"


class ContinuationRecoveryRung(StrEnum):
    """The durable, single-use no-progress ladder for one retained HeadRun."""

    NONE = "none"
    SAFE_RECOVERY_PENDING = "safe_recovery_pending"
    SAFE_RECOVERY_RESPONSE_WINDOW = "safe_recovery_response_window"
    SAFE_RECOVERY_RESUME_ONCE = "safe_recovery_resume_once"
    SAFE_RECOVERY_UNAVAILABLE = "safe_recovery_unavailable"
    TERMINAL = "terminal"


@dataclass
class WorkerContinuationLiveness:
    """Versioned provider-progress evidence for a retained red continuation.

    Separate from the delivery receipt: the receipt says whether the continuation prompt reached the
    pane, this answers whether the *same provider run* has made progress while readiness keeps saying
    busy. Only opaque cursors and classifications, never composer or prompt text.

    A record missing this value is never upgraded into an episode. A v1 episode is created
    exclusively when a new delivery boundary is written for an already retained HeadRun, which
    prevents a recovered same-workspace pane from being bound after the fact.
    """

    version: int = CONTINUATION_LIVENESS_VERSION
    state: ContinuationLivenessState = ContinuationLivenessState.UNKNOWN
    reason: str = "missing"
    head_run_id: str = ""
    head_run_fingerprint: str = ""
    # The first probe of this exact HeadRun episode.  It is distinct from busy timing and is
    # written for progressing, idle and unavailable evidence alike.
    first_observed_at: float = 0.0
    first_busy_at: float = 0.0
    last_provider_progress_at: float = 0.0
    last_provider_observed_at: float = 0.0
    provider_cursor: str = ""
    provider_source: str = ""
    provider_source_fingerprint: str = ""
    baseline_established: bool = False
    # A text-free explanation of why unchanged progress is not treated as a completed turn.
    # `residual_composer` requires equal composer and output fingerprints; everything else stays
    # active-or-unknown rather than guessing from a screen snapshot.
    no_progress_evidence: str = ""
    busy_attempts: int = 0
    recovery_rung: ContinuationRecoveryRung = ContinuationRecoveryRung.NONE
    recovery_attempted_at: float = 0.0
    recovery_response_deadline: float = 0.0
    recovery_attempts: int = 0
    recovery_resume_used: bool = False
    terminal_outcome: str = ""
    # A rejected source does not erase a real episode into a clean, re-baselinable value.  The
    # retained HeadRun, baseline and ladder stay as audit evidence while the state becomes typed
    # unknown, so a later tick cannot reinterpret an identity failure as a new first observation.
    source_rejected: bool = False
    # Kept solely to explain an historical record.  It is intentionally never copied into
    # ``busy_attempts`` and therefore cannot spend a recovery rung.
    legacy_busy_attempts: int = 0

    @classmethod
    def unknown(
        cls,
        reason: str = "missing",
        *,
        legacy_busy_attempts: int = 0,
    ) -> WorkerContinuationLiveness:
        return cls(
            state=ContinuationLivenessState.UNKNOWN,
            reason=reason[:240],
            legacy_busy_attempts=max(0, int(legacy_busy_attempts or 0)),
        )

    @classmethod
    def begin(cls, head_run: Any) -> WorkerContinuationLiveness:
        """Open the only kind of episode which is allowed to acquire a source baseline."""
        run_id, fingerprint = head_run_binding(head_run)
        if not run_id:
            return cls.unknown("retained HeadRun is unavailable or malformed")
        return cls(
            state=ContinuationLivenessState.BASELINE_PENDING,
            reason="awaiting exact run-bound provider source baseline",
            head_run_id=run_id,
            head_run_fingerprint=fingerprint,
        )

    @property
    def bound(self) -> bool:
        return bool(self.head_run_id and self.head_run_fingerprint)

    @property
    def admitted(self) -> bool:
        return (
            self.bound
            and self.baseline_established
            and self.state
            not in {
                ContinuationLivenessState.UNKNOWN,
                ContinuationLivenessState.UNAVAILABLE,
            }
        )

    @property
    def terminal(self) -> bool:
        return bool(self.terminal_outcome) or self.recovery_rung == ContinuationRecoveryRung.TERMINAL

    def observe_provider(self, evidence: Any, now: float, *, head_run: Any) -> str:
        """Apply the sole provider admission rule before any ladder decision.

        The host can report only an opaque cursor, but it must also attest the run and source that
        produced it. This method never invents that binding.
        """
        if self.source_rejected:
            # A card with a rejected source is sent to the identity-fenced blocked path.  In
            # particular, do not let a later, superficially well-formed reply re-admit it and
            # restart the preserved no-progress ladder.
            return "unknown"
        expected_run_id, expected_fingerprint = head_run_binding(head_run)
        if (
            not self.bound
            or not expected_run_id
            or self.head_run_id != expected_run_id
            or self.head_run_fingerprint != expected_fingerprint
        ):
            self._reject_as_unknown("continuation liveness episode is not bound to the retained HeadRun")
            return "unknown"
        if not self.first_observed_at:
            self.first_observed_at = now
        if not isinstance(evidence, dict):
            self.state = ContinuationLivenessState.UNAVAILABLE
            self.reason = "provider-progress transport returned an invalid shape"
            self.last_provider_observed_at = now
            return "unavailable"
        if (
            str(evidence.get("state") or "") == "unavailable"
            and str(evidence.get("source") or "") == "codex-session"
            and str(evidence.get("continuation_condition") or "")
            == ContinuationProviderCondition.LEGACY_UNBOUND_V1.value
        ):
            # The producer emits this only for a complete v1 preflight descriptor that still says
            # `unbound` for this exact run.  It is not a source admission and therefore cannot be
            # re-baselined here, but the dispatcher may safely take its already-fenced replacement
            # route instead of parking the card for an observer to move back to Ready.
            self.state = ContinuationLivenessState.UNAVAILABLE
            self.reason = "Codex provider source remains legacy-unbound for v1 progress"
            self.last_provider_observed_at = now
            return ContinuationProviderCondition.LEGACY_UNBOUND_V1.value
        if (
            str(evidence.get("state") or "") != "observed"
            or str(evidence.get("admission") or "") != "accepted"
            or str(evidence.get("head_run_id") or "") != self.head_run_id
            or str(evidence.get("head_run_fingerprint") or "") != self.head_run_fingerprint
        ):
            if str(evidence.get("state") or "") == "identity_mismatch":
                self._reject_as_unknown(
                    str(evidence.get("reason") or "provider progress names another HeadRun")
                )
                self.last_provider_observed_at = now
                return "unknown"
            self.state = ContinuationLivenessState.UNAVAILABLE
            self.reason = str(evidence.get("reason") or "provider source was not admitted")[:240]
            self.last_provider_observed_at = now
            return self.state.value
        source = str(evidence.get("source") or "")[:80]
        source_fingerprint = str(evidence.get("source_fingerprint") or "")[:64]
        cursor = str(evidence.get("cursor") or "")[:240]
        if not source or not _fingerprint(source_fingerprint) or not cursor:
            self.state = ContinuationLivenessState.UNAVAILABLE
            self.reason = "provider source admission is incomplete"
            self.last_provider_observed_at = now
            return "unavailable"
        if not self.baseline_established:
            if self.state != ContinuationLivenessState.BASELINE_PENDING:
                self._reject_as_unknown("continuation liveness baseline is incoherent")
                return "unknown"
            self.provider_source = source
            self.provider_source_fingerprint = source_fingerprint
            self.provider_cursor = cursor
            self.baseline_established = True
            self.state = ContinuationLivenessState.BASELINED
            self.reason = ""
            self.last_provider_observed_at = now
            return "baseline"
        if source != self.provider_source or source_fingerprint != self.provider_source_fingerprint:
            self._reject_as_unknown("provider source does not match the persisted v1 baseline")
            return "unknown"
        self.last_provider_observed_at = now
        if cursor == self.provider_cursor:
            self.state = ContinuationLivenessState.STALLED
            self.reason = "provider cursor has not advanced"
            return "stalled"
        response_window = self.recovery_rung == ContinuationRecoveryRung.SAFE_RECOVERY_RESPONSE_WINDOW
        self.provider_cursor = cursor
        self.state = ContinuationLivenessState.PROGRESSED
        self.reason = ""
        self.last_provider_progress_at = now
        self.busy_attempts = 0
        self.recovery_rung = (
            ContinuationRecoveryRung.SAFE_RECOVERY_RESPONSE_WINDOW
            if response_window
            else ContinuationRecoveryRung.NONE
        )
        self.recovery_attempted_at = 0.0
        self.recovery_response_deadline = 0.0
        self.recovery_attempts = 0
        self.recovery_resume_used = False
        self.terminal_outcome = ""
        self.no_progress_evidence = ""
        return "progressed"

    def note_busy(self, now: float) -> int:
        if not self.admitted or self.state != ContinuationLivenessState.STALLED:
            return self.busy_attempts
        if not self.first_busy_at:
            self.first_busy_at = now
        self.busy_attempts += 1
        return self.busy_attempts

    def begin_safe_recovery(self, now: float) -> None:
        self.recovery_rung = ContinuationRecoveryRung.SAFE_RECOVERY_PENDING
        self.recovery_attempted_at = now
        self.recovery_attempts += 1

    def safe_recovery_response_window(self, now: float, seconds: float) -> None:
        self.recovery_rung = ContinuationRecoveryRung.SAFE_RECOVERY_RESPONSE_WINDOW
        self.recovery_response_deadline = now + max(0.0, seconds)

    def allow_safe_recovery_resume_once(self) -> bool:
        if self.recovery_rung != ContinuationRecoveryRung.SAFE_RECOVERY_RESUME_ONCE:
            return False
        if self.recovery_resume_used:
            return False
        self.recovery_resume_used = True
        return True

    def terminalize(self, outcome: str, reason: str) -> None:
        self.recovery_rung = ContinuationRecoveryRung.TERMINAL
        self.terminal_outcome = outcome
        self.reason = reason[:240]

    def _reject_as_unknown(self, reason: str) -> None:
        """Fence a rejected episode without laundering its ladder into a fresh one."""
        self.state = ContinuationLivenessState.UNKNOWN
        self.reason = reason[:240]
        self.source_rejected = True

    def to_json(self) -> dict[str, Any]:
        return {
            "version": CONTINUATION_LIVENESS_VERSION,
            "state": self.state.value,
            "reason": self.reason,
            "head_run_id": self.head_run_id,
            "head_run_fingerprint": self.head_run_fingerprint,
            "first_observed_at": self.first_observed_at,
            "first_busy_at": self.first_busy_at,
            "last_provider_progress_at": self.last_provider_progress_at,
            "last_provider_observed_at": self.last_provider_observed_at,
            "provider_cursor": self.provider_cursor,
            "provider_source": self.provider_source,
            "provider_source_fingerprint": self.provider_source_fingerprint,
            "baseline_established": self.baseline_established,
            "no_progress_evidence": self.no_progress_evidence,
            "busy_attempts": self.busy_attempts,
            "recovery_rung": self.recovery_rung.value,
            "recovery_attempted_at": self.recovery_attempted_at,
            "recovery_response_deadline": self.recovery_response_deadline,
            "recovery_attempts": self.recovery_attempts,
            "recovery_resume_used": self.recovery_resume_used,
            "terminal_outcome": self.terminal_outcome,
            "source_rejected": self.source_rejected,
            "legacy_busy_attempts": self.legacy_busy_attempts,
        }

    @classmethod
    def from_json(cls, value: Any) -> WorkerContinuationLiveness:
        if value is None:
            return cls.unknown("missing")
        if not isinstance(value, dict):
            return cls.unknown("malformed")
        try:
            if int(value.get("version")) != CONTINUATION_LIVENESS_VERSION:
                return cls.unknown("unsupported version")
            state = ContinuationLivenessState(str(value.get("state") or ""))
            rung = ContinuationRecoveryRung(str(value.get("recovery_rung") or ""))
            run_id = value.get("head_run_id")
            fingerprint = value.get("head_run_fingerprint")
            if not isinstance(run_id, str) or not isinstance(fingerprint, str):
                return cls.unknown("malformed HeadRun binding")
            numeric = {
                name: float(value.get(name) or 0.0)
                for name in (
                    "first_observed_at",
                    "first_busy_at",
                    "last_provider_progress_at",
                    "last_provider_observed_at",
                    "recovery_attempted_at",
                    "recovery_response_deadline",
                )
            }
            if any(number < 0 for number in numeric.values()):
                return cls.unknown("malformed liveness timestamp")
            busy_attempts = int(value.get("busy_attempts") or 0)
            recovery_attempts = int(value.get("recovery_attempts") or 0)
            if busy_attempts < 0 or recovery_attempts < 0:
                return cls.unknown("malformed liveness attempts")
            legacy_busy_attempts = int(value.get("legacy_busy_attempts") or 0)
            if legacy_busy_attempts < 0:
                return cls.unknown("malformed legacy busy attempts")
            source_rejected = value.get("source_rejected", False)
            if not isinstance(source_rejected, bool):
                return cls.unknown("malformed source-rejection fence")
        except (TypeError, ValueError):
            return cls.unknown("malformed")
        source = str(value.get("provider_source") or "")[:80]
        source_fingerprint = str(value.get("provider_source_fingerprint") or "")[:64]
        cursor = str(value.get("provider_cursor") or "")[:240]
        baseline_established = value.get("baseline_established")
        outcome = str(value.get("terminal_outcome") or "")[:80]
        explicit_unknown = (
            state == ContinuationLivenessState.UNKNOWN
            and not run_id
            and not fingerprint
            and not source
            and not source_fingerprint
            and not cursor
            and baseline_established is False
            and busy_attempts == 0
            and rung == ContinuationRecoveryRung.NONE
            and not outcome
            and all(not numeric[name] for name in numeric)
        )
        if explicit_unknown and not source_rejected:
            return cls.unknown(
                str(value.get("reason") or "unknown"), legacy_busy_attempts=legacy_busy_attempts
            )
        if not run_id or not _fingerprint(fingerprint):
            return cls.unknown("malformed HeadRun binding", legacy_busy_attempts=legacy_busy_attempts)
        if not isinstance(baseline_established, bool):
            return cls.unknown(
                "historical v1 liveness lacks a baseline", legacy_busy_attempts=legacy_busy_attempts
            )
        if state == ContinuationLivenessState.UNKNOWN:
            # A foreign or changing source seals an otherwise exact episode.  Its retained
            # fields are audit-only: no retry may read them back as a new source baseline.
            if not source_rejected:
                return cls.unknown(
                    "unknown liveness episode lacks a source-rejection fence",
                    legacy_busy_attempts=legacy_busy_attempts,
                )
            if baseline_established:
                if not source or not cursor or not _fingerprint(source_fingerprint):
                    return cls.unknown(
                        "incoherent source-rejected liveness baseline",
                        legacy_busy_attempts=legacy_busy_attempts,
                    )
            elif (
                source
                or source_fingerprint
                or cursor
                or busy_attempts
                or rung != ContinuationRecoveryRung.NONE
                or outcome
                or numeric["first_busy_at"]
                or numeric["last_provider_progress_at"]
                or numeric["recovery_attempted_at"]
                or numeric["recovery_response_deadline"]
                or recovery_attempts
                or bool(value.get("recovery_resume_used", False))
            ):
                return cls.unknown(
                    "incoherent source-rejected pending baseline", legacy_busy_attempts=legacy_busy_attempts
                )
        elif state == ContinuationLivenessState.BASELINE_PENDING:
            if (
                baseline_established
                or source
                or source_fingerprint
                or cursor
                or busy_attempts
                or rung != ContinuationRecoveryRung.NONE
                or outcome
                or any(numeric.values())
                or recovery_attempts
                or bool(value.get("recovery_resume_used", False))
            ):
                return cls.unknown(
                    "incoherent pending liveness baseline", legacy_busy_attempts=legacy_busy_attempts
                )
        elif state in {
            ContinuationLivenessState.BASELINED,
            ContinuationLivenessState.STALLED,
            ContinuationLivenessState.PROGRESSED,
        }:
            if not baseline_established or not source or not cursor or not _fingerprint(source_fingerprint):
                return cls.unknown(
                    "incoherent bound liveness baseline", legacy_busy_attempts=legacy_busy_attempts
                )
        elif state == ContinuationLivenessState.UNAVAILABLE:
            # An unavailable probe is a durable observation of this already-bound episode, not an
            # unbound historical value.  In particular, preserve it across a reload so a missing
            # source cannot be re-baselined from a later workspace scan.  A prior admitted baseline
            # and its bounded ladder are audit evidence too: a later exact source may report real
            # progress, but it cannot start a fresh episode.
            if source_rejected:
                return cls.unknown(
                    "unavailable liveness carries a source-rejection fence",
                    legacy_busy_attempts=legacy_busy_attempts,
                )
            if baseline_established:
                if not source or not cursor or not _fingerprint(source_fingerprint):
                    return cls.unknown(
                        "incoherent unavailable liveness baseline",
                        legacy_busy_attempts=legacy_busy_attempts,
                    )
            elif (
                source
                or source_fingerprint
                or cursor
                or busy_attempts
                or rung not in {ContinuationRecoveryRung.NONE, ContinuationRecoveryRung.TERMINAL}
                or numeric["first_busy_at"]
                or numeric["last_provider_progress_at"]
                or numeric["recovery_attempted_at"]
                or numeric["recovery_response_deadline"]
                or recovery_attempts
                or bool(value.get("recovery_resume_used", False))
            ):
                return cls.unknown(
                    "incoherent unavailable pending baseline",
                    legacy_busy_attempts=legacy_busy_attempts,
                )
        else:
            return cls.unknown(
                "unavailable liveness episode is not safely recoverable",
                legacy_busy_attempts=legacy_busy_attempts,
            )
        if state == ContinuationLivenessState.UNKNOWN:
            return cls(
                version=CONTINUATION_LIVENESS_VERSION,
                state=state,
                reason=str(value.get("reason") or "")[:240],
                head_run_id=run_id,
                head_run_fingerprint=fingerprint,
                first_observed_at=numeric["first_observed_at"],
                first_busy_at=numeric["first_busy_at"],
                last_provider_progress_at=numeric["last_provider_progress_at"],
                last_provider_observed_at=numeric["last_provider_observed_at"],
                provider_cursor=cursor,
                provider_source=source,
                provider_source_fingerprint=source_fingerprint,
                baseline_established=baseline_established,
                no_progress_evidence=str(value.get("no_progress_evidence") or "")[:80],
                busy_attempts=busy_attempts,
                recovery_rung=rung,
                recovery_attempted_at=numeric["recovery_attempted_at"],
                recovery_response_deadline=numeric["recovery_response_deadline"],
                recovery_attempts=recovery_attempts,
                recovery_resume_used=bool(value.get("recovery_resume_used", False)),
                terminal_outcome=outcome,
                source_rejected=True,
                legacy_busy_attempts=legacy_busy_attempts,
            )
        if rung == ContinuationRecoveryRung.TERMINAL:
            if not outcome:
                return cls.unknown(
                    "terminal liveness outcome is missing", legacy_busy_attempts=legacy_busy_attempts
                )
        elif outcome:
            return cls.unknown(
                "non-terminal liveness carries a terminal outcome", legacy_busy_attempts=legacy_busy_attempts
            )
        elif rung != ContinuationRecoveryRung.NONE and state not in {
            ContinuationLivenessState.STALLED,
            ContinuationLivenessState.PROGRESSED,
            ContinuationLivenessState.UNAVAILABLE,
        }:
            return cls.unknown(
                "recovery rung lacks verified no-progress episode", legacy_busy_attempts=legacy_busy_attempts
            )
        if rung == ContinuationRecoveryRung.SAFE_RECOVERY_RESPONSE_WINDOW and (
            recovery_attempts != 1
            or numeric["recovery_response_deadline"] <= numeric["recovery_attempted_at"]
        ):
            return cls.unknown(
                "safe recovery response window is incoherent", legacy_busy_attempts=legacy_busy_attempts
            )
        if rung == ContinuationRecoveryRung.SAFE_RECOVERY_RESUME_ONCE and recovery_attempts != 1:
            return cls.unknown(
                "safe recovery resume is incoherent", legacy_busy_attempts=legacy_busy_attempts
            )
        return cls(
            version=CONTINUATION_LIVENESS_VERSION,
            state=state,
            reason=str(value.get("reason") or "")[:240],
            head_run_id=run_id,
            head_run_fingerprint=fingerprint,
            first_observed_at=numeric["first_observed_at"],
            first_busy_at=numeric["first_busy_at"],
            last_provider_progress_at=numeric["last_provider_progress_at"],
            last_provider_observed_at=numeric["last_provider_observed_at"],
            provider_cursor=cursor,
            provider_source=source,
            provider_source_fingerprint=source_fingerprint,
            baseline_established=baseline_established,
            no_progress_evidence=str(value.get("no_progress_evidence") or "")[:80],
            busy_attempts=busy_attempts,
            recovery_rung=rung,
            recovery_attempted_at=numeric["recovery_attempted_at"],
            recovery_response_deadline=numeric["recovery_response_deadline"],
            recovery_attempts=recovery_attempts,
            recovery_resume_used=bool(value.get("recovery_resume_used", False)),
            terminal_outcome=outcome,
            source_rejected=False,
            legacy_busy_attempts=legacy_busy_attempts,
        )


def _fingerprint(value: str) -> bool:
    return len(value) == 32 and all(character in "0123456789abcdef" for character in value.lower())


class ReportNudgeStage(StrEnum):
    NONE = "none"
    # The intent is on disk and nothing has been sent yet, or the tick that sent it died before it
    # could say so. The two are one stage on purpose: they are indistinguishable from the record.
    PENDING = "pending"
    DELIVERED = "delivered"


@dataclass
class WorkerReportNudge:
    """The one reminder a report round may spend on a live worker that has stopped working.

    Replacing a head that has finished the work and only missed the report throws that work away, so
    the round gets a single prompt at the confirmed-idle boundary before anything destructive happens.

    Bounded by `generation`, which is the report round: a second confirmed-idle episode in the same
    round finds the intent already spent and escalates, and a round that opens later gets its own
    prompt. That is also why nothing has to remember to clear this.

    `PENDING` is deliberately as spent as `DELIVERED`. A tick that died between the intent and its
    confirmation may or may not have typed into a live conversation, and re-entering the delivery to
    find out would be the second prompt this bound exists to prevent.
    """

    stage: ReportNudgeStage = ReportNudgeStage.NONE
    generation: int = 0
    sent_at: float = 0.0

    def spent(self, generation: int) -> bool:
        """Whether the round `generation` has already used its one prompt."""
        return self.stage != ReportNudgeStage.NONE and self.generation == int(generation)

    @property
    def unconfirmed(self) -> bool:
        """An intent whose delivery nobody ever confirmed, from any round."""
        return self.stage == ReportNudgeStage.PENDING

    def begin(self, generation: int, now: float) -> None:
        """Reserve this round's prompt, before anything is sent to the head."""
        if self.spent(generation):
            raise ValueError(f"report generation {generation} has already been prompted")
        self.stage = ReportNudgeStage.PENDING
        self.generation = int(generation)
        self.sent_at = now

    def confirm(self) -> None:
        if self.stage not in {ReportNudgeStage.PENDING, ReportNudgeStage.DELIVERED}:
            raise ValueError(f"cannot confirm a report prompt from {self.stage}")
        self.stage = ReportNudgeStage.DELIVERED

    # Deliberately no `clear()`, unlike the continuation below. Nothing ever has to reset this:
    # the generation invalidates a spent prompt on its own, and a mutator that could unspend one
    # is exactly how a round would end up prompting a live conversation twice.

    def to_json(self) -> dict[str, Any]:
        if self.stage == ReportNudgeStage.NONE:
            return {}
        return {
            "stage": self.stage.value,
            "generation": self.generation,
            "sent_at": self.sent_at,
        }

    @classmethod
    def from_json(cls, value: Any) -> WorkerReportNudge:
        if not isinstance(value, dict) or not value:
            return cls()
        return cls(
            stage=ReportNudgeStage(str(value.get("stage") or "none")),
            generation=int(value.get("generation") or 0),
            sent_at=float(value.get("sent_at") or 0.0),
        )


class WorkerContinuationStage(StrEnum):
    NONE = "none"
    VALIDATION_MOVE_PENDING = "validation_move_pending"
    RETAINED = "retained"
    # A substantive reviewer verdict, before and after the board move that parks the card in
    # Assessment. The two are separate stages for the same reason the red transition splits its
    # move: the intent has to be on disk before anything observable happens, and recovery has to
    # be able to tell "the move may not have landed" from "the card is parked and waiting".
    ASSESSMENT_PENDING = "assessment_pending"
    ASSESSMENT_PARKED = "assessment_parked"
    RED_TRANSITION_PENDING = "red_transition_pending"
    DELIVERY_PENDING = "delivery_pending"
    DELIVERY_CONFIRMED = "delivery_confirmed"


@dataclass
class WorkerContinuation:
    stage: WorkerContinuationStage = WorkerContinuationStage.NONE
    phase: str = ""
    retained_at: float = 0.0
    sent_at: float = 0.0
    report_baseline: int = 0
    reserved_generation: int = 0
    """The report generation this transition opens, reserved before the board is moved.

    The transition is finished by whichever tick finds it, first attempt or recovery, so the round
    it opens cannot be a number computed at completion time: a completion that ran twice would hand
    one rework round two generations and leave a worker holding a document nobody is waiting on.
    Reserving it with the intent makes the retry idempotent, the same way the move is.
    """
    move_reason: str = ""
    """The comment the red move carries, kept so recovery finishes the move it opened.

    A gate verdict is not reconstructible from the record: the CI summary and its tail live in the
    tick that read them. Recovery that re-derived a reason would either move the card with a
    different body or have to re-run the gate, which is exactly the replay this stage exists to
    prevent.
    """
    verdict_outcome: str = ""
    decision: str = ""
    """The observer decision this transition is performing, empty when nobody decided anything.

    A red mechanical gate opens its transition with no decision behind it; a rework decision on a
    parked card opens the same transition and must carry the decision into the board move, which
    refuses to take a card out of Assessment without one.
    """
    decision_body: str = ""
    """What the observer actually instructed, frozen here with the generation this round opens.

    The worker of the round has to follow the decision that opened it, so the text cannot be
    re-read at document-build time: "the most recent decision comment" is a different question, and
    a decision recorded while the round is already running would answer it and silently replace the
    instruction the round was opened on. Reserved with `reserved_generation`, in the same immutable
    transition, so the document and the round always name the same adjudication.
    """
    decision_protocol_prerequisites: tuple[str, ...] = ()
    """The structured prerequisites validated with ``decision_body`` and this round."""
    session_held: bool = False
    """Whether a suspended session of this round is still there to be resumed.

    Separate from the stage because a red transition outlives the session it was opened over: the
    intent has to be durable whether or not anything can take the continuation, and a confirmed
    stop in the middle of one drops the session without dropping the transition.
    """
    # A readiness timeout is evidence that the owned head is working, not a failed continuation.
    # This bounded retry clock keeps that distinction durable across dispatcher restarts. It never
    # becomes evidence that the head died or that the prompt was acknowledged.
    busy_attempts: int = 0
    busy_next_at: float = 0.0
    handoff: PromptHandoff | None = None
    """The production prompt handoff this delivery opened, made durable before the worker is woken.

    Its floor is the worker journal's sequence before anything of this continuation was written, so
    every later tick reads from that journal how far the handoff got (typed, submitted, taken)
    instead of typing it again. None outside the production dispatcher and for a delivery opened by
    a dispatcher that predates it, which opens one on its next attempt.
    """

    @property
    def retained(self) -> bool:
        return self.session_held and self.stage != WorkerContinuationStage.NONE

    @property
    def validation_move_pending(self) -> bool:
        return self.stage == WorkerContinuationStage.VALIDATION_MOVE_PENDING

    @property
    def assessment_pending(self) -> bool:
        return self.stage == WorkerContinuationStage.ASSESSMENT_PENDING

    @property
    def parked(self) -> bool:
        """The card is held by a verdict nobody has acted on, move landed or not."""
        return self.stage in {
            WorkerContinuationStage.ASSESSMENT_PENDING,
            WorkerContinuationStage.ASSESSMENT_PARKED,
        }

    @property
    def red_transition_pending(self) -> bool:
        return self.stage == WorkerContinuationStage.RED_TRANSITION_PENDING

    @property
    def delivery_pending(self) -> bool:
        return self.stage == WorkerContinuationStage.DELIVERY_PENDING

    @property
    def delivery_confirmed(self) -> bool:
        return self.stage == WorkerContinuationStage.DELIVERY_CONFIRMED

    def begin_retention(self, now: float) -> None:
        self.stage = WorkerContinuationStage.VALIDATION_MOVE_PENDING
        self.phase = ""
        self.retained_at = now
        self.sent_at = 0.0
        self.session_held = True

    def confirm_validation_move(self) -> None:
        if self.stage == WorkerContinuationStage.RETAINED:
            return
        if self.stage != WorkerContinuationStage.VALIDATION_MOVE_PENDING:
            raise ValueError(f"cannot confirm validation move from {self.stage}")
        self.stage = WorkerContinuationStage.RETAINED

    def begin_park(self, phase: str, report_baseline: int, move_reason: str, verdict_outcome: str) -> None:
        """Record the reviewer's verdict before the card is parked in Assessment.

        The verdict is durable here, and nothing has yet merged, resumed a worker or moved the board. A
        tick that dies between this write and the move is recovered by finishing the move, never by
        re-deciding what the verdict meant. Re-entry from `ASSESSMENT_PENDING` is allowed.
        """
        if self.stage not in {
            WorkerContinuationStage.NONE,
            WorkerContinuationStage.VALIDATION_MOVE_PENDING,
            WorkerContinuationStage.RETAINED,
            WorkerContinuationStage.ASSESSMENT_PENDING,
        }:
            raise ValueError(f"cannot park from {self.stage}")
        self.stage = WorkerContinuationStage.ASSESSMENT_PENDING
        self.phase = phase
        self.report_baseline = int(report_baseline)
        self.move_reason = move_reason
        self.verdict_outcome = verdict_outcome

    def confirm_park(self) -> None:
        """The card is in Assessment. From here only a recorded decision moves it."""
        if self.stage == WorkerContinuationStage.ASSESSMENT_PARKED:
            return
        if self.stage != WorkerContinuationStage.ASSESSMENT_PENDING:
            raise ValueError(f"cannot confirm a park from {self.stage}")
        self.stage = WorkerContinuationStage.ASSESSMENT_PARKED

    def begin_red_transition(
        self,
        phase: str,
        report_baseline: int,
        move_reason: str,
        verdict_outcome: str,
        decision: str = "",
        reserved_generation: int = 0,
        decision_body: str = "",
        decision_protocol_prerequisites: tuple[str, ...] = (),
    ) -> None:
        """Record the red verdict before the board is moved.

        Without this the record of a crashed tick still names the report that closed the previous round,
        and recovery reads that report as a new completion instead of finishing the red transition.

        Opening one over a round that holds no session is the same transition, not a lesser case. An open
        transition is immutable: it carries everything its own completion needs, so a tick that finds one
        finishes it rather than opening a second one over a freshly read verdict.
        """
        if self.stage not in {
            WorkerContinuationStage.NONE,
            WorkerContinuationStage.VALIDATION_MOVE_PENDING,
            WorkerContinuationStage.RETAINED,
            # A rework decision on a parked card opens the transition the park was holding back.
            WorkerContinuationStage.ASSESSMENT_PARKED,
        }:
            raise ValueError(f"cannot open a red transition from {self.stage}")
        self.stage = WorkerContinuationStage.RED_TRANSITION_PENDING
        self.phase = phase
        self.report_baseline = int(report_baseline)
        self.move_reason = move_reason
        self.verdict_outcome = verdict_outcome
        self.decision = decision
        self.reserved_generation = int(reserved_generation)
        self.decision_body = decision_body
        self.decision_protocol_prerequisites = decision_protocol_prerequisites

    def begin_delivery(self, phase: str, now: float) -> None:
        # `DELIVERY_PENDING` is allowed back in: a tick that died between the send and its
        # checkpoint is recovered by re-entering the same delivery, not by a different path.
        if self.stage not in {
            WorkerContinuationStage.RETAINED,
            WorkerContinuationStage.RED_TRANSITION_PENDING,
            WorkerContinuationStage.DELIVERY_PENDING,
        }:
            raise ValueError(f"cannot resume worker from {self.stage}")
        if self.stage != WorkerContinuationStage.DELIVERY_PENDING:
            # The earliest send is what a turn is looked for after. Moving this forward on a retry
            # would hide a turn the first send already started and cost the worker a second prompt.
            self.sent_at = now
        self.stage = WorkerContinuationStage.DELIVERY_PENDING
        self.phase = phase

    def open_handoff(self, floor: int, now: float) -> None:
        """Fix where this delivery's handoff starts; a handoff already open is never moved."""
        if self.stage != WorkerContinuationStage.DELIVERY_PENDING:
            raise ValueError(f"cannot open a continuation handoff from {self.stage}")
        if self.handoff is None:
            self.handoff = PromptHandoff(floor=int(floor), began_at=float(now))

    def confirm_delivery(self) -> None:
        if self.stage not in {
            WorkerContinuationStage.DELIVERY_PENDING,
            WorkerContinuationStage.DELIVERY_CONFIRMED,
        }:
            raise ValueError(f"cannot confirm worker delivery from {self.stage}")
        self.stage = WorkerContinuationStage.DELIVERY_CONFIRMED
        self.busy_attempts = 0
        self.busy_next_at = 0.0

    def defer_busy(self, now: float) -> int:
        """Defer a readiness-busy retry while retaining the same pending delivery."""
        self.busy_attempts += 1
        delay = min(
            BUSY_RETRY_INITIAL_SECONDS * (2 ** min(self.busy_attempts - 1, 4)),
            BUSY_RETRY_MAX_SECONDS,
        )
        self.busy_next_at = now + delay
        return delay

    def busy_retry_due(self, now: float) -> bool:
        return not self.busy_next_at or now >= self.busy_next_at

    def drop_session(self) -> None:
        """The session is gone, by a confirmed stop or by its own death.

        A retention that has nothing else pending disappears with it; a park and a red transition do not.
        Losing the red transition's intent here is what would let a later tick read the closed round's
        report as a new completion.
        """
        self.session_held = False
        if self.stage in {
            WorkerContinuationStage.VALIDATION_MOVE_PENDING,
            WorkerContinuationStage.RETAINED,
        }:
            self.clear()

    def clear(self) -> None:
        self.stage = WorkerContinuationStage.NONE
        self.phase = ""
        self.retained_at = 0.0
        self.sent_at = 0.0
        self.report_baseline = 0
        self.reserved_generation = 0
        self.move_reason = ""
        self.verdict_outcome = ""
        self.decision = ""
        self.decision_body = ""
        self.decision_protocol_prerequisites = ()
        self.session_held = False
        self.busy_attempts = 0
        self.busy_next_at = 0.0
        self.handoff = None

    def to_json(self) -> dict[str, Any]:
        if self.stage == WorkerContinuationStage.NONE:
            return {}
        return {
            "stage": self.stage.value,
            "phase": self.phase,
            "retained_at": self.retained_at,
            "sent_at": self.sent_at,
            "report_baseline": self.report_baseline,
            "reserved_generation": self.reserved_generation,
            "move_reason": self.move_reason,
            "verdict_outcome": self.verdict_outcome,
            "decision": self.decision,
            "decision_body": self.decision_body,
            "decision_protocol_prerequisites": list(self.decision_protocol_prerequisites),
            "session_held": self.session_held,
            "busy_attempts": self.busy_attempts,
            "busy_next_at": self.busy_next_at,
            **({"handoff": self.handoff.to_json()} if self.handoff is not None else {}),
        }

    @classmethod
    def from_json(cls, value: Any) -> WorkerContinuation:
        if not isinstance(value, dict) or not value:
            return cls()
        stage = WorkerContinuationStage(str(value.get("stage") or "none"))
        return cls(
            stage=stage,
            phase=str(value.get("phase") or ""),
            retained_at=float(value.get("retained_at") or 0.0),
            sent_at=float(value.get("sent_at") or 0.0),
            report_baseline=int(value.get("report_baseline") or 0),
            # 0 for a transition written before the reservation existed. Its completion falls back
            # to advancing the record's own generation, which is what it did when it was written.
            reserved_generation=int(value.get("reserved_generation") or 0),
            move_reason=str(value.get("move_reason") or ""),
            verdict_outcome=str(value.get("verdict_outcome") or ""),
            decision=str(value.get("decision") or ""),
            # A transition written before the decision text was frozen carries none. Its round then
            # renders the way it did when it was written: reviewer findings only.
            decision_body=str(value.get("decision_body") or ""),
            decision_protocol_prerequisites=tuple(
                item
                for item in value.get("decision_protocol_prerequisites", ())
                if isinstance(item, str) and item
            ),
            # Records written before the session flag existed only ever reached a stage by
            # retaining one.
            session_held=bool(value.get("session_held", stage != WorkerContinuationStage.NONE)),
            busy_attempts=int(value.get("busy_attempts") or 0),
            busy_next_at=float(value.get("busy_next_at") or 0.0),
            handoff=PromptHandoff.from_json(value.get("handoff")),
        )
