# Typed board double

`tests.fakes.board` owns `FakeBoardHost` and its `MemoryAudit`. These simulate typed entity
reads/listing, create/replace/lifecycle mutations, request replay and event confirmation using
the production models, transitions, `BoardEventCanon` and `MutationEventTransaction`. They are
test support, with intentional policy differences from `SqlBoardHost`/`SqlTaskAudit`:

| Contract | Fake policy | SQL policy / evidence required |
|---|---|---|
| Surface | No `marker_comment`, `recover_marker_comment`, `recover_transition`, `recover_sprint` or `recover_product_issue`; transition has no `finish` hook. | Marker grammar, occurrence witnesses, confirmation and recovery have dedicated SQL paths. |
| Authority and configuration | Uses the generic transition registry and model validation; no role authority, configured directories, registered projects, capacity/admission locks or SQL-backed validation. | Card transitions use role-authorized edges and reference/admission locks; writers retain their admission rules. Product/Issue and Sprint operations require configuration and their own validation. |
| Create/replace | Any entity kind can be created; replace checks existence and unchanged lifecycle state. Description-operation evidence is not interpreted. | Create supports Product/Issue; replace supports Issue priority and explicit description operations. Other kinds refuse with `migration_pending`; Issue replacement has additional state/payload restrictions. |
| Related refs | Keeps exactly the caller's refs, including Sprint `request_related_refs` in event data. | Adds Card sprint, Sprint product/issues/cards and Issue product refs on the applicable paths. |
| Issue closure | Generic transition preserves the existing `close_reason`. | Stores the operation reason as `close_reason` and confirms closure metadata/comments. |
| Time | UTC event time truncated to whole seconds. | Host events use `datetime.now(UTC)` with microseconds; persistence encoding belongs to the SQL path. |
| Event identity and payload | Shared `board.events.event_id` hashes request, kind, entity kind/ref, actor, reason, related refs, source/target and supplement/data. Create/replace data is empty; transitions include Sprint supplements and explicit data. | Also uses the shared hash, with operation-specific payloads: normalized Product/Issue and description evidence, marker occurrence and recovery evidence. IDs and shapes are not interchangeable. |
| Replay and errors | Host operation mismatch raises `ValueError` in both modes. Canonical audit claim/append/discard ownership mismatch raises `TaskError("validation", ..., 2)`; no-audit request-map collision raises `ValueError`. Missing entities and invalid edges use the existing board errors. | Host replay checks can also raise `ValueError`; SqlTaskAudit ownership refusals use the kernel TaskError vocabulary. Backend/configuration/migration errors have their own shapes. There is no blanket exception equivalence. |
| Canonical audit | Passing `MemoryAudit` enables staged/committed canon transactions, shared-audit hosts, deep-copy snapshots and committed-then-pending projection records through the shared lazy `_projection_slice`. Records are protected by one `RLock`. | Claims/events persist in `requests`/`board_events`; database transactions, unique ownership, advisory locks, SQL ordering/filtering/pagination, stale settlement and origin-return outbox belong to SQL. MemoryAudit has no generic `stage`, outbox or SQL pagination/filter interface. |
| No-audit mode | Per-host entity/event/request maps; no staged history. Neither mode persists or atomically locks entity mutations with audit records. | Canon is configured through the SQL host's data directory and audit/client. Supported mutation paths require it; disabling it is not equivalent to the fake's compact mode. |

Relocation preserves these policies; it does not make the fake interchangeable with SQL. The
old CON-12 audit phrase “event-id without payload” is stale: `_event_id` includes operation
payload. The three other `MemoryAudit` classes in `po_handover_fakes.py`, `test_web_codex_reset.py`
and `test_tick_board_snapshot.py` have different interfaces and policies and remain independent.

`tests.fakes.marker_audit.MarkerAudit` extends `MemoryAudit` with a board directory, generic staging and kind-filtered reads
for in-process writer tests. Marker locks are no-ops and pending marker arbitration returns no owner;
it supplies no SQL persistence, concurrent marker reservation or cross-process locking evidence.

The existing consumers keep their manifest owners: `test_board_rows` and
`test_dispatcher_worker_continuation` are local unit evidence; `test_board` is integration-board,
`test_attempt_outcome` integration-heads, and `test_narrow_cleanup_lock` integration-dispatcher.
Those CI-owned modules include existing interrupted-effect, pending-retry, collision and audit
failure cases; pure-fake assertions establish in-process behavior only. PostgreSQL persistence,
transaction/locking, SQL validity, recovery, backend/process and real Git/network behavior need
their native exact-SHA CI evidence. The frozen original coupling scanner runs in graph CI;
its candidate measurement is separate from behavioral evidence and final-main closeout.
