"""Shared contract of head operations: refusals, the delivered pointer, and the returned run.

`spawn`, `nudge` and `stop` are `HeadRuntime` verbs (`local_pty_head`). Shared rules:

  * `HeadSpawnAborted` means a head may still be alive (keep the launch intent);
    `HeadSpawnFailed` means nothing survived. Confusing them kills live heads.
  * A head is sent a bounded `NudgePointer` to a document, not the task text (`prompt_document`).
  * A delivery cannot rewrite a launch: `post_delivery_run` is the one merge of the callback's run
    with operation-proved address facts. See docs/PROTOCOLS.md "Post-delivery HeadRun handoff".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ummanu.runtime.prompt_document import nudge_for

# Re-exported for callers that name head types through this module (`dispatch.launch`, fixtures).
from .run import HeadRun, StopInitiator
from .spec import HeadSpec
from .task_ref import TaskRef

__all__ = [
    "HeadNudgeFailed",
    "HeadOperationError",
    "HeadRun",
    "HeadSpawnAborted",
    "HeadSpawnFailed",
    "HeadSpec",
    "HeadStopFailed",
    "NudgePointer",
    "StopInitiator",
    "TaskRef",
    "post_delivery_run",
]


class HeadOperationError(RuntimeError):
    """Any refusal from one of the three operations, with the delivery evidence there was."""

    def __init__(
        self,
        message: str,
        *,
        evidence: Any = None,
        run: HeadRun | None = None,
    ) -> None:
        super().__init__(message)
        self.evidence = evidence
        # Once a delivery callback refined the run, every later failure must carry that run, or
        # launch-intent recovery would overwrite what it persisted.
        self.run = run


class HeadSpawnFailed(HeadOperationError):
    """A bring-up that left nothing running (confirmed gone); safe to treat as not launched."""


class HeadSpawnAborted(HeadOperationError):
    """A bring-up that may have left a live head; carries the run so a later tick can adopt or stop it."""

    def __init__(self, message: str, *, run: HeadRun, evidence: Any = None) -> None:
        super().__init__(message, evidence=evidence, run=run)


class HeadNudgeFailed(HeadOperationError):
    """A prompt into a live head that did not reach its confirmation."""


class HeadStopFailed(HeadOperationError):
    """A stop that could not be confirmed. The run travels in `finishing`, with its initiator."""

    def __init__(self, message: str, *, run: HeadRun) -> None:
        super().__init__(message)
        self.run = run


@dataclass(frozen=True)
class NudgePointer:
    """One bounded line a head is sent, and the document it points at, if any.

    Without a document the line itself is the message.
    """

    text: str
    document: str = ""

    @classmethod
    def at_document(cls, path: str, note: str = "") -> NudgePointer:
        """The nudge for a task document already on disk.

        `note` is a discriminating tail built through `nudge_for`, so the ceiling covers path and note.
        """
        return cls(text=nudge_for(path, note), document=str(path))

    @classmethod
    def line(cls, text: str) -> NudgePointer:
        """A short instruction that carries its own content."""
        return cls(text=text)


def post_delivery_run(before: HeadRun, after: HeadRun) -> HeadRun:
    """Merge the exact pre-send handoff with address facts this operation has proved.

    The callback owns its persisted source; `spawn`/`nudge` own only the address and provable
    lifecycle, so a stale launch result cannot erase a newer source binding.
    """
    if not isinstance(after, HeadRun) or not before.same_run(after):
        raise HeadNudgeFailed("post-delivery HeadRun does not match the launched head", run=before)
    if (
        before.spec != after.spec
        or before.workspace != after.workspace
        or before.task_ref != after.task_ref
        or before.role != after.role
        or before.pid_file != after.pid_file
    ):
        raise HeadNudgeFailed("post-delivery HeadRun changed its launch identity", run=before)
    if after.lifecycle != before.lifecycle or after.stopped_by != before.stopped_by:
        raise HeadNudgeFailed("pre-send delivery callback changed HeadRun lifecycle", run=before)
    # Reapplying the operation-owned facts also covers a callback that read the run before the
    # backend supplied its stable leaf; provider source fields cannot change.
    return after.rebound(before.handle, leaf=before.leaf)
