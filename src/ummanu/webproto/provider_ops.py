"""The owner's one write on a provider: spend a Codex rate-limit reset credit.

The account holds few credits -- one, on 2026-09-26 -- so the operation is built around never
spending one twice. Whether a reset applies right now is left to the provider: its
`applicable_available_count` follows a rule it does not document, and its own client offers the
reset on any available credit and shows the provider's answer. Everything is decided in
:meth:`ProviderOperationLayer.codex_reset_limit`, in this order:

1. **A committed record for the request id is the answer.** A repeat, a retry and a reload of the
   same press read what the first one recorded and ask the provider nothing.
2. **The live reading decides whether there is anything to spend.** The Codex usage is read again
   past the bar's five-minute cache. No available credit is refused -- recorded, and no consume.
3. **Otherwise the press is staged, consume is sent, and the mapped outcome is committed.** The
   request id is the provider's `redeem_request_id`, so even a consume sent twice under it spends at
   most one credit.

**The record is the board audit's own**: a generic, record-only `requests` row (`backend.revision`
`not_written`, as the sprint guard's records are), so `/history` lists it and `GET
/api/history/<request_id>` answers for it with no second journal and no schema change. It names no
entity: `ref` is empty, which `requests.ref` allows and the history renders as `—`. A press that was
staged and never committed -- the process stopped while the provider was being asked -- carries the
outcome `unknown`, and the audit's stale-row settlement commits it as that rather than guessing.

No token reaches the record, the answer or an error: the provider layer puts it in a header and
nowhere else, and the outcome it hands back is a closed code with a reason it wrote itself.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ummanu.board.backend import BOARD_STORE_KIND, card_client
from ummanu.runtime.codex_home import installation_codex_dir
from ummanu.tasks import TaskError, task_audit_for
from ummanu.webproto import sources
from ummanu.webproto.boundary import ProtocolBoundary
from ummanu.webproto.errors import OwnerConflict, RuntimeUnavailable, ValidationRefused

SCHEMA_VERSION = 1

#: The audit record's `kind`, and the action `/history` shows for it.
CODEX_RESET_KIND = "codex_reset_limit"

#: The one refusal the live reading can force, as the history and the page show it. Whether a reset
#: applies is not judged here: the provider's `applicable_available_count` follows a rule it does not
#: document, and its own client offers the reset on any available credit, so the consume is sent and
#: the provider's answer (`nothing_to_reset` among them) is recorded as the outcome.
NO_CREDIT = "no Codex reset credit"

#: The outcome a staged press carries until the provider's answer replaces it. Seen only when the
#: process stopped between the two, which is exactly when nobody knows what the provider did.
STAGED_OUTCOME = "unknown"
STAGED_REASON = (
    "the consume call was sent and its answer was never recorded; the provider deduplicates on "
    "this request id, so no second credit can be spent under it"
)

_CODES: dict[str, Any] = {"validation": ValidationRefused}


class ProviderOperationLayer(ProtocolBoundary):
    """One installation's owner-side provider writes, with no knowledge of who is asking.

    Construction does no I/O. `usage` is the provider layer the bar reads -- the same object, so a
    reset clears the cache the next render reads -- and `board_client` and `clock` are the seams a
    test supplies directly.
    """

    def __init__(
        self,
        instance: str | Path,
        *,
        usage: Any,
        board_client: Any | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.instance = Path(instance)
        self.usage = usage
        self._board_client = board_client
        self._clock = clock
        # Two presses inside this process -- a double click that got past the page -- are one after
        # the other, so the second finds the first one's committed record.
        self._lock = threading.Lock()

    def codex_reset_limit(self, *, request_id: str, actor: str, role: str = "po") -> dict[str, Any]:
        """Spend one Codex reset credit under `request_id`, or say why not; every answer is recorded."""
        identifier = str(request_id or "").strip()
        if not identifier:
            raise ValidationRefused("a Codex reset names the request it is made under")
        with self._lock:
            try:
                return self._reset(identifier, actor=actor, role=role)
            except TaskError as exc:
                raise _CODES.get(exc.code, RuntimeUnavailable)(exc.message) from None

    # -- plumbing ---------------------------------------------------------------------------

    def _reset(self, request_id: str, *, actor: str, role: str) -> dict[str, Any]:
        audit = task_audit_for(self._client())
        committed = audit.committed_event(request_id)
        if committed is not None:
            if str(committed.get("kind") or "") != CODEX_RESET_KIND:
                raise OwnerConflict("this request id belongs to another operation")
            return self._document(committed, replayed=True)
        pending = audit.pending_event(request_id)
        if pending is not None and str(pending.get("kind") or "") != CODEX_RESET_KIND:
            raise OwnerConflict("this request id belongs to another operation")
        credits = self.usage.codex_live().get("reset_credits")
        credits = credits if isinstance(credits, dict) else {}
        available = _count(credits.get("available"))
        applicable = _count(credits.get("applicable"))
        seen = {"available": available, "applicable": applicable}
        refusal = NO_CREDIT if not available else None
        if refusal is not None:
            if pending is not None:
                refusal += "; an earlier consume under this request id was sent and its answer never recorded"
            record = self._record(
                request_id, actor=actor, role=role, outcome="refused", reason=refusal, seen=seen
            )
            audit.stage(request_id, record)
            audit.append(request_id, record)
            return self._document(record, replayed=False)
        record = self._record(
            request_id, actor=actor, role=role, outcome=STAGED_OUTCOME, reason=STAGED_REASON, seen=seen
        )
        audit.stage(request_id, record)
        outcome, reason = self.usage.consume_codex_reset(request_id)
        record = {
            **record,
            "occurred_at": sources.isoformat(self._clock()),
            "outcome": outcome,
            "reason": reason,
        }
        # A generic staged record is replaced by its own settled form, then committed as that.
        audit.stage(request_id, record)
        audit.append(request_id, record)
        if outcome == "reset":
            self.usage.invalidate()
        return self._document(record, replayed=False)

    def _record(
        self,
        request_id: str,
        *,
        actor: str,
        role: str,
        outcome: str,
        reason: str | None,
        seen: dict[str, int | None],
    ) -> dict[str, Any]:
        return {
            "event_id": "evt_" + uuid.uuid4().hex,
            "schema_version": 1,
            "occurred_at": sources.isoformat(self._clock()),
            "actor": {"role": role, "id": actor},
            "kind": CODEX_RESET_KIND,
            "outcome": outcome,
            "reason": reason,
            "task_id": "",
            "ref": "",
            "backend": {"kind": BOARD_STORE_KIND, "task_id": None, "revision": "not_written"},
            "request_id": request_id,
            "payload": {"provider": "codex", "redeem_request_id": request_id, "credits": seen},
        }

    def _document(self, record: dict[str, Any], *, replayed: bool) -> dict[str, Any]:
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": CODEX_RESET_KIND,
            "observed_at": sources.isoformat(self._clock()),
            "request_id": str(record.get("request_id") or ""),
            "event_id": str(record.get("event_id") or "") or None,
            "outcome": str(record.get("outcome") or ""),
            "reason": record.get("reason"),
            "credits": payload.get("credits"),
            "replayed": replayed,
        }

    def _client(self) -> Any:
        return self._board_client or card_client(
            self.instance.parent if self.instance.is_file() else self.instance
        )


def _count(value: Any) -> int | None:
    """A count the provider layer already normalised (a whole number or `None`), read defensively."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def codex_usage_home(data_dir: str | None) -> Callable[[], Path | None]:
    """The web usage layer's Codex home: the CODEX_HOME this installation's heads run on.

    Resolved on every read (`installation_codex_dir`), so a re-login or a data-dir move is seen
    without a restart; None, and so `~/.codex`, only when the installation holds no Codex login.
    """
    return lambda: installation_codex_dir(data_dir)
