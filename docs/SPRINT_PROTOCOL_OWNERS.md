# Sprint protocol owners

`ummanu.sprints` retains the released `SprintReader` and `SprintWriter` constructors and
the imports used by CLI, PO, dispatcher, web, export/restore and task guards. The writer
inherits one cohesive close/reopen capability from
`board/sprint_protocol/lifecycle.py`. It contains 26 methods, including staging, planning,
closeout, issue/card dispositions, replay checks, reopen and observer preimage capture.
The released source has 81 writer, 13 reader and two audit traversal methods; those
counts are unchanged. The proposed seam's estimate of 27 methods was one too high.

`board/sprint_protocol/shared.py` owns `_sql_atomic`, the three lifecycle event kinds,
and the derived close request identifiers. `reservations.py` owns the existing guard
index and admission locks, shared by core and lifecycle. Neither owner imports the
upper facade. The package initializer exports no implementations.

The public writer MRO is `SprintWriter`, `SprintLifecycleMixin`, `object`. Its constructor
stays in the facade; inherited methods continue calling collaborators through `self`.
There are no `super`, private name-mangling or concrete writer-class references in the
moved methods. `TaskReader` and `TaskWriter` in lifecycle retain the identities from
`ummanu.tasks`. Method defaults, annotations, decorators, errors and operation ordering
are retained. The shared decorator changes only its internal `wrapped(self: SprintWriter)`
annotation to `Any`; `functools.wraps` still copies the decorated method's public
annotations and signature. Its thread-local nesting path, complete SQL transaction,
advisory lock 1600, rollback/drop order and exception mapping are unchanged.

The two intentional behavior changes are `SprintReader.list(create=False)` as the
default, and `show` using `_sprint_board(create=False)` with the existing
`TaskError("not_found", "sprint was not found", 2)` when the board is absent. Constructors
do not initialize anything. Explicit `list(create=True)` and writer `_finish_create`'s
`ensure_sprint_board` remain initialization paths. Failed lookups retain their errors;
released malformed/falsy response classifications are retained. Default show still
fetches and renders comments when cards are requested, even with
`include_comments=False`; this cut does not alter that released output.

The four close-path `ummanu.sprints.TaskReader/TaskWriter` patches in
`tests/test_observer_voice.py` now target `ummanu.board.sprint_protocol.lifecycle`.
Core reader `TaskReader`, `task_audit_for`, `_now`, create/admission/reference and guard
patches remain in the facade. Public-class and instance method patches still reach
inherited methods. Core `update_active_sprint_projects` producers are patchable in
`ummanu.sprints`; lifecycle producers are patchable in the lifecycle module. Reservation
helper internals execute in `reservations`, without facade aliases that broadcast patches.
The cleanup foundation identity guard follows `CleanupJournal` to its actual lifecycle
caller, and its primitive-consumer scan includes both new consumers. The identity
assertion remains strict; no facade alias is retained solely for that old guard.

For ummanu-265, the card-scoped source inventory compares the fetched base
`405e06f214ca909f50e6a5fa23d4a133e24c1d1e` with the candidate. Of 137 function/method
definitions, 134 have identical ASTs, one differs only by the annotation above, and two
are the intentional reader deltas. All 39 moved definitions and all moved constant
values are accounted for. Imports are compared separately, including executing global
bindings and explicit patch retargets. Historical AST inventories stay in the worker's
evidence artifacts, rather than becoming permanent source pins. Current native guards
check unique ownership, the 3,000-line boundary, MRO and reverse imports, including
planted duplicate, oversized, nested, relative, dynamic and string-reference cases.

Validation belongs to the declared unit/component profile and exact-candidate CI.
The missing-board protocol probe rejects every write and demonstrates the old fallback
to `createProject`; it is not a supported alternative Sprint store. PostgreSQL always
exposes a virtual sprint-board sentinel, so native SQL CI probes instead trap mutating
RPC/SQL and compare persisted project/sprint/audit rows and guard-index bytes around
fresh and established default reads, including linked cards and freshness. Positive
writer create/restore probes retain initialization and idempotent request replay.
Existing PostgreSQL close/reopen fault, restore, reservation, staged intent and golden
tests retain their manifest owners and assertions. The fixture storage/count guard is
unchanged because its four counted suites were not edited.

Native result/request/key-order goldens record the released source contract; they do
not imply an independently executed before-run. The two missing-board regressions
were actually run through the allowed adapter subset before changing production and
both failed at `createProject`. The original frozen uncut coupling scanner runs only
in the existing graph CI workflow, now also triggered by sprint owners and changed
consumers. Wider SQL consolidation, CON-12, audit closure and deployment remain
separate work.
