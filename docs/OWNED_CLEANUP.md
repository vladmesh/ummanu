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

Before removal, the owner verifies canonical paths, catalog binding, common-dir,
registration, Git admin path/file, inode, branch and exact HEAD. It preserves
tracked, untracked and ignored author work and unpublished commits. Only prompt
bytes recorded by the prompt producer, the existing exactly owned
`.ummanu-task-env` namespace and, for the exact workspace of a card the replay's
admissions see Done, Git-ignored (`!!`) entries strictly inside a `.pytest_cache`,
`__pycache__` or `.venv` directory (nested ones included) may be discarded. Every
component down to such an entry is a real directory; a symlinked cache or parent,
a tracked or untracked (`??`) cache name, any other ignored file, an observer
workspace and a closed card that is not Done keep the workspace. The planner, the
inventory and execution read one predicate; under the removal admission it is
read again and each entry is unlinked through no-follow directory descriptors
before the ordinary no-force `git worktree remove`. Author-modified prompts,
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
slot of the batch; the cursor rotates over due intents only.

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
