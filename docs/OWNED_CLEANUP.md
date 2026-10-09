# Owned Git residue

`dispatch.cleanup.CleanupOwner` owns settlement. `CleanupJournal` is the single
producer of `data_dir/dispatcher/cleanup.json`; its fsynced intent retains the
card/sprint/project, claim and attempt, canonical repository/common directory,
linked-worktree registration and inode, exact branch/HEAD, every recorded head
run and scope generation, intended disposition and effect progress.

Dispatcher record saves retain ownership before a later terminal path can drop
the active record. Done teardown requests settlement through the same owner as
inactive reconciliation. Task archive, including supersession and sprint close
archive steps, requests settlement before closing the board row. Pending archive
recovery retains that request. Sprint close keeps its existing transaction and
step progress; it does not execute Git or stop its own observer inside a board
transaction. Its cleanup result reads the journal's actual dispositions.

Owner installation, record capture, tick replay and inactive settlement use the
real-mode `CommandHostRuntime` contract. Recording host doubles exercise real
gate/vitality policy while simulating their effects; declaring real policy mode
alone does not make a host a Git cleanup owner.

`dispatcher/cleanup.lock` covers only short cleanup-journal publications, dispatcher
ownership saves and live ownership admission/commit checks; ticks, Git proofs, head
setup, launch readiness, delivery and stop run outside it. `CleanupOwner.admission`
revalidates the primary key, state and claim before effects; per-reference lifecycle
and effect fences order ownership mutations against disposal, while the PostgreSQL
row lock covers only the short admission/commit check and is released before any
subprocess or filesystem disposal. Release admits `done` cleanup in Assessment;
heads and workspace are disposed there, and claim settlement waits for Done.

The shared `dispatcher/board-bulk.lock` lane comes before card effects, capacity,
SQL and `cleanup.lock`; independent shared holders do not serialize head commands.
Board import and resumable group-order repair take this lane exclusively once,
without per-card descriptors. Restore keeps `.restore.lock` and sprint admission
ahead of the lane. Disposal finishes before an exclusive restore starts; subsequent
admission revalidates restored ownership by key.

Claims count live capacity keys and revalidate each peer, independently of the tick
snapshot. A separate admission fence orders concurrent claims and moves into active
states; comment/report writes rely on their SQL transactions. Head settlement calls the runtime selected by each durable
HeadRun. Scoped runs still require `ScopedHeadLifecycle` generation matching and
recursive empty-scope proof. Genuinely deployed unscoped local-pty runs use their
existing launch identity fences. PID death never substitutes for scoped cleanup.
Other/newer recorded owners, unrecorded scopes, unreadable evidence and unknown
claims refuse effects. Legacy Orca records remain preserved.

`CommandHostRuntime.fence_cleanup_scopes` reaches the scope reader through
`runtime.local_pty_head.fence_cleanup_scopes`; cleanup never imports the PTY
substrate. Recorded scoped generations always use the selected runtime's stop,
including after a retained terminal flag. An unrecorded terminal scope needs the
existing runtime inventory's current native disappearance proof. Successful stop
receipts must settle the exact run, generation, spec, role, task and workspace;
the journal retains those receipts before proceeding. Deployed unscoped runs can
reuse their confirmed stop receipt only after their launch identity is fenced.

Missing Git proof does not erase independently recorded head ownership. The
owner settles that exact head through its lifecycle and reports the unproven
workspace as preserved. Failed or mismatched head settlement remains pending.

A live replacement card attempt admits only its predecessor's recorded heads:
the card id/ref/project, worker, recorded workspace identity and distinct nonempty
attempt ids must agree with the successor's dispatcher record and owned intent.
Replay uses the recorded-only scope fence, never shared role PID files or successor
scopes, and revalidates the specific successor before checkpointing stop proof.
Its preserved, verified terminal outcome `attempt-replaced` records both attempt ids in `progress.handoff`;
`claim_settled` completes only the predecessor's obligation and releases no current
claim. Workspace, ref, environment and green gate evidence stay with the successor.
Later replay cannot reclaim them even after the successor record disappears.
This clears only that old head obligation from launch admission, allowing the
existing Blocked-to-Validate reviewer retry over the retained candidate.

Before removal, the owner verifies canonical paths, catalog binding, common-dir,
registration, Git admin path/file, inode, branch and exact HEAD. It preserves
tracked, untracked and ignored author work and unpublished commits. Only prompt
bytes recorded by the prompt producer, the existing exactly owned
`.ummanu-task-env` namespace and, for the exact workspace of a card the replay's
admissions see Done, Git-ignored (`!!`) regular files strictly inside a `.pytest_cache`,
`__pycache__` or `.venv` directory (nested ones included) may be discarded. Every
component down to such a file is a real directory and the file itself is regular:
any symlink path (a symlinked cache, parent or leaf such as `.venv/bin/python`), a
tracked or untracked (`??`) cache name, any other ignored file, an observer workspace
and a closed card that is not Done keep the workspace. A real `.venv` therefore still
keeps a Done workspace through its interpreter symlinks. The planner, the inventory
and execution read one predicate, always through the recorded workspace directory
pinned by a descriptor opened from `/` without following any component and matched
to the recorded device/inode; Git status runs inside that descriptor. Under the
removal admission the exact identity is revalidated, the root pinned again, the
predicate read again, and each file is unlinked relative to the pinned root before
the ordinary no-force `git worktree remove`. A substituted ancestor or workspace is
refused before the first unlink. Author-modified prompts,
check receipts, reports and other ignored files remain work unless their existing
consumer has already settled them. Git removal uses no force, recursive directory
fallback or broad metadata pruning, and confirms both directory and registration
absence. Failed creation/adoption preserves its unadmitted residue.

Detached HEADs require the same commit retention proof. A remote-tracking ref
containing the exact HEAD proves publication and retention. The existing observer
producer's empty, parentless root commit is disposable only while its named
`observers` branch still retains that exact commit in the owned observer root.
User commits, arbitrary local refs, reflogs and a SHA in the journal do not grant
that authority. Detached unpublished user commits retain their checkout and
registration even when Git status is clean. The intent records the retaining refs
before admitting removal; cleanup never creates a preservation ref retroactively.

A published clean unmerged checkout can be removed while its exact local ref is
retained. Ref deletion requires the exact card's local pipeline namespace, no
registered checkout or active owner, publication and ancestry into the registered
integration/default branch. Git's ref transaction verifies and locks both the
candidate tip and the integration witness during deletion. Remote refs and other
namespaces are untouched. Git configuration and remote retention are outside
this settlement.

Effect-start progress is durable before removal and ref deletion. Replay can
finish after a crash between removal and claim/ref settlement, without adopting
a replacement directory. Git admission follows exact registration, author-work
and commit proof. If Git removes the directory before its admin entry, the owner
revalidates the retained admin inode and `HEAD`/`gitdir`/`commondir`, common directory,
path, registration and ref against that admission at the shared primitive's native
removal boundary. Only that proof permits ordinary targeted `git worktree remove`
to finish a missing-directory registration. A changed mapping, substitution or
unreadable evidence remains pending. Registration and directory
absence precede ref/claim settlement. Environment deletion also retains its exact namespace
identity before effects, so an interrupted deletion of its ownership file remains
replayable without adopting a replacement namespace. Terminal stale matching claims are cleared only after
verified head settlement; pending failures keep their owner. `completed`,
`preserved` and `pending` are distinct dispositions. Delivery may reach Done with
a pending cleanup receipt; it never turns that receipt into clean settlement.

Some refusals are dead ends no retry can change. After the exact heads are stopped
through the proof path above, they end as a terminal outcome: status `preserved`,
`progress.terminal = {kind, reason}`, `preservation_verified` false and no removal,
ref or admission flag. The kinds, each decided from facts read in that attempt:
`workspace-disappeared` (no directory, registration or admin entry, and no
admitted or shared removal proof), `registration-without-directory` (the directory
is gone, its exact recorded registration, admin entry and ref remain and stay
untouched; never disposed or pruned), `project-unregistered` (the catalog's
registration table does not name the project; no repository is read) and `follows`
(an empty-attempt duplicate whose attempt owners all completed or ended terminally).
Both Git kinds also need a retention witness for the recorded tip, recorded as
`commit_proof`: a remote-tracking ref containing it, the owned observer root, or the
attempt's candidate ref still containing it. Without one (a unique unpublished commit
whose last ref is gone) the obligation stays pending with its claim. The registration
check also covers an attempt whose workspace identity was never captured, after the
owner and head fences.
A registered but disabled project, an unreadable catalog, a changed or substituted
admin, unreadable Git or board evidence, a live or unknown head, another active
owner or a changed claim stay pending. The claim of a terminal outcome is settled
as for any preservation with stopped heads. Summary and inventory rows name the
kind in `terminal`; the residue and its recorded identity stay visible, and no
entrypoint replays a terminal intent again. A later attempt of the card is a new key.

One eligibility policy decides whether an open (pending or non-terminal preserved)
intent is due: the tick's bounded rotating batch, `replay_one`, targeted replay and
the close, archive and teardown callers all reach effects only through it. Before any
Git, host or board effect the owner reserves the attempt under `cleanup.lock`, writing
`retry = {last_attempt_at, next_attempt_at}` (one cooldown, 3600 s, later) into the
intent with the attempt's begin state; of concurrent owners only one reserves. A
crash after the reservation waits out the cooldown. An intent with no stored due time
(legacy) is due at once; a missing, non-numeric or non-finite due time, or one more
than a cooldown ahead of the clock (it moved back, or the value is hostile), is
distrusted and due at once, and the attempt re-anchors it. A repeated request keeps
the schedule. A not-due intent is neither read for effects nor written, and takes no
slot of the batch. A due snapshot is not a reservation: a reservation lost to a
concurrent owner takes no slot, the next due intent is tried, and the cursor moves
only to an attempt this owner reserved. A due time that is not a finite number in
float range (`10**400` included) is malformed and due at once.

The production tick's cleanup phase is the one automatic replay, and it runs on one
monotonic deadline (`REPLAY_ALLOWANCE`, 4 s, under the sprint's 5 s phase bound) from
before its selection until it returns. Its last `PUBLICATION_RESERVE` (0.5 s) is kept
for the journal publications that record outcomes and the cursor; no work, wait or
effect is admitted into it and nothing extends the deadline. Nested paths read what is
left and never restart it:

- lock waits (`cleanup.lock`, the bulk and effect lanes, the board writer's fences, and
  the owner's own operation mutex that an inventory or targeted replay may hold) poll for
  what is left; a card whose lifecycle lane another owner holds is skipped at once;
- the board store client (`SqlCardClient.within`) cuts its pool wait, the turn of
  `transaction()`, and every exchange with the server: each connection's own driver wait
  (`psycopg` `Connection.wait`, behind every query, setting, schema read, commit, rollback
  and unpin) gets what is left as its `timeout`, an argument the driver takes from 3.3.6,
  the declared floor (BOARD_STORE.md §5.8). An exchange cut short closes its connection
  (never reused) and is outcome-unknown: a COMMIT so cut may have committed, and the next
  attempt reads the board again before any effect; nothing is disposed from it. A new
  connection is made attempt by attempt (`conninfo_attempts`, one per resolved address),
  each with what is left as its `connect_timeout` (whole seconds, at least 2). The server
  also gets `statement_timeout` and `lock_timeout` `LOCAL` before each statement, so it
  cancels a long statement itself; the settings end with their transaction;
- every cleanup Git child runs in its own process group (`_proc.run_isolated`) within
  what is left: one still running when only `_TERMINATE_SECONDS` remain gets `SIGTERM`
  to its whole group, so Git removes the lock files of an unfinished ref transaction
  itself and a hook or helper it ran ends with it, then the group is killed, and the
  drain and reap get only what is left. Nothing it started can act after the call;
- the head stop receives `remaining` (runtime lock, supervisor exchange, scope
  termination, exit confirmation), and so does the scope fence (each run directory read
  and each native disappearance proof);
- every directory enumeration (intent selection, the whole-journal reads behind owner,
  shared-removal, observer and empty-attempt proofs, generated digests, the scope fence and
  runtime scope inventory) reads its native iterator entry by entry with the allowance
  checked before each; a scan cut short raises, so no partial listing ever stands for the
  whole directory or proves an absence. Loads, status classification, cache and
  generated-file unlinks and the environment namespace walk (removing entries as it reads
  them) check it before each entry too;
- the runtime provenance probe before stop, removal and ref deletion is a child process
  run in its own group within what is left.

A due intent is reserved only while `ATTEMPT_FLOOR` (1 s) of work allowance is left, and
no workspace, Git removal or ref stage starts with less than `EFFECT_FLOOR` (0.5 s). An
exhausted allowance raises `Deferred`, an ordinary refusal: the attempt ends `pending`
with its durable progress and reserved cooldown, never completed, verified or terminal.
A skipped intent was never reserved and keeps its due time. Selection reads intents in
rotation order from the cursor, one at a time and only as far as the attempts go, so
slow reads never starve the attempts; the cursor then advances past everything it observed,
attempted, lost, skipped at the floor, refused by a busy lane or owner, or not due, so a
persistently slow first read never holds later intents back; a due intent passed this way
was never reserved and is reached again on the next rotation. After a selection of the
whole journal it rests on the last attempt. It is written once, only with due work or a
selection stopped short, and the telemetry says whether it advanced, stayed or could not
be written. A Git or stop effect ended at
its bound is re-proved by the next attempt as after a crash (`removal_started`,
`environment_removal_started`, `ref_delete_admitted`, retained stop receipts). A Git
leader that outlives `SIGTERM` (an uninterruptible call) may leave a lock file; cleanup
never removes one, and the ref transaction's refusal names it. Teardown, inactive
reconciliation, observer stop/close and the targeted maintenance replay pass no
allowance and keep their waits. The tick records the invocation under `cleanup` in its
telemetry entry: allowance and spent milliseconds, the due, attempted, deferred,
skipped, busy, lost and unread counts, where it was cut, the cursor's fate, and its
intent, meta and generated publications and bytes.

Supported maintenance surfaces:

```
ummanu instance-maintenance --instance INSTANCE --residue-inventory [--project PROJECT]
ummanu instance-maintenance --instance INSTANCE --residue-replay --project PROJECT \
    --target TARGET --manifest DIGEST [--target TARGET --manifest DIGEST ...]
```

Inventory reads registered repositories/worktrees and local pipeline refs,
including archived cards absent from active records. It reports exact tips,
publication, ancestry and ownership/preservation reasons without adopting by glob
or touching Git. Replay settles only the named targets of one project (1..20),
each at the manifest digest the inventory reported, and adopts branch-only
archived/Done residue only with matching project/card and audited dispatcher
claim; the runbook is in [Operations](OPERATIONS.md#git-residue-read-the-manifest-then-replay-exact-targets).
Old workspaces missing exact attempt/head evidence,
foreign/detached worktrees and legacy Orca placements remain preserved and
reported. All repository types use the same contract, including instance-local
pipeline branches. A local remote-tracking ref is publication evidence; replay
does not fetch or operate on remote refs.

Recorded repository/ref/card/attempt/tip ownership precedes enumeration. Inventory
reports those owners and reuses their obligations; it cannot admit a new synthetic
attempt over a conflicting or unproven owner, even if the replacement tip is merged
and published. A ref present after completed cleanup retains its provenance and is
not re-adopted. Historical claim evidence does not override an exact recorded tip.

Close stages its observer handoff in the cleanup journal using the original
generation and launch count. The external dispatcher enriches that obligation
from the exact observer registration, waits for the existing close transaction
to finish and for card cleanup/verified preservation, then processes the observer
last. Retained observer user work appears as preservation in the same inventory.
Pending Git removal, registration, ref or claim settlement blocks observer stop,
including after exact card heads are stopped. Preservation permits handoff only
when the owner verified the retained disposition, or ended it terminally, and
settled heads and claims; missing ownership reported as preservation cannot stand
in for that proof.
A failed handoff or stop remains pending after board closure. Neither the closing
observer nor a code worker cleans live residue. Installation and final residue
proof belong to the subsequent authorized PO operation.

`tests.test_owned_cleanup` exercises disposable Git repositories, simulated
scope owners and the maintenance reader/replay entry. Backend systemd and board
integration contracts remain dispatcher exact-SHA CI responsibilities.
