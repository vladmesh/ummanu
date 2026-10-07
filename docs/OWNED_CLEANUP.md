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
bytes recorded by the prompt producer, and the existing exactly owned
`.ummanu-task-env` namespace, may be discarded. Author-modified prompts,
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
to finish a missing-directory registration. A generic missing path, changed mapping,
substitution or unreadable evidence remains pending. Registration and directory
absence precede ref/claim settlement. Environment deletion also retains its exact namespace
identity before effects, so an interrupted deletion of its ownership file remains
replayable without adopting a replacement namespace. Terminal stale matching claims are cleared only after
verified head settlement; pending failures keep their owner. `completed`,
`preserved` and `pending` are distinct dispositions. Delivery may reach Done with
a pending cleanup receipt; it never turns that receipt into clean settlement.
Every normal dispatcher tick replays a bounded, rotating batch so persistent
failures or preserved work cannot starve newer intents.

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
when the owner verified the retained disposition and settled heads and claims;
missing ownership reported as preservation cannot stand in for that proof.
A failed handoff or stop remains pending after board closure. Neither the closing
observer nor a code worker cleans live residue. Installation and final residue
proof belong to the subsequent authorized PO operation.

`tests.test_owned_cleanup` exercises disposable Git repositories, simulated
scope owners and the maintenance reader/replay entry. Backend systemd and board
integration contracts remain dispatcher exact-SHA CI responsibilities.
