# Head scope ownership and memory exits

Every profile-backed observer, worker, reviewer and PO turn uses `ScopedHeadLifecycle`.
`memory_limit_mib` accepts an integer from 1 through 1048576, excluding booleans.
The default is 8192 MiB; shipped high profiles use 12288 MiB. The launch sets
`MemoryMax` to that many bytes and `MemorySwapMax=0`. Scope registration or limit
verification failure refuses the launch.

`spawn_head` writes and fsyncs `scope-owner.json` before starting a launcher. The
record holds the run ID, derived unit name, launch generation and admission gate.
The launcher starts a child behind a pipe barrier, writes its PID, boot ID and
process start ticks to the owner, then releases it to exec sudo/systemd-run.
Death before release closes the pipe and creates no scope work. Cleanup closes
admission under the same owner lock, terminates the recorded launch group with
identity fences, stops the unit and reads recursive `cgroup.events` membership.
The same nonblocking owner lock remains held from generation verification through
backend termination and durable empty proof. Admission and replacement use that
lock too, so a delayed old stop cannot target a same-run replacement. Contention
refuses without changing ownership and can be retried. Recording the proof does
not reacquire the lock.
An absent cgroup after admission closes and launch work ends, or `populated 0`,
is the empty proof. A missing membership file in an existing cgroup is an error.
Unreadable state, a failed command or interrupted cleanup leaves the owner for
retry. A new generation cannot replace it until its empty proof is durable;
late launchers and stale cleanup calls cannot act on the replacement generation.

`run.exited` says the head exited. It does not say the scope is empty. Runtime
stop, forgetting a scoped head, replacement spawn, PO settlement and PO retry
all go through this owner. Detached descendants remain in the cgroup even when
they create another process group. The dispatcher also checks settled HeadRuns
before removing their workspace: historical PID-based settlement is insufficient
for a scope owner.

Scoped HeadHandles and HeadRuns carry the launch generation, including retained
pre-start failures. Runtime stop consumes that matching owner's proof while its
lock is held. A failed launch that never published a heartbeat or journal can
therefore settle after cleanup succeeds. Missing head records alone cannot settle
a run; malformed owners, generation mismatches and foreign/reused process
identities still refuse. A missing owner refuses unless the run has ended on present
evidence: its journal holds `run.exited` after its last `run.started`, its launch
identity is absent or dead, and its cgroup is absent or `populated 0`. A missing
journal is never that evidence. Genuinely unscoped runs keep their identity-based check.

Product runs assign and save that generation in their write-ahead HeadRun before
calling start. The runtime and launcher preserve it, so a crash before saving the
returned handle still leaves a record that can stop the matching scope. A start
carrying a write-ahead generation cannot replace an existing scope owner. Product
recovery treats a scope owner as a launch trace even before any heartbeat or journal
exists; failed cleanup retains the unresolved run until matching proof succeeds.
Dispatcher observer/worker/reviewer intents use their newly allocated launch ID
as the generation, so both preflight calls preserve the original durable binding.
Provider/lifecycle handoff merges preserve it and refuse conflicting generations.
An ordinary runtime replacement that has no new write-ahead admission still gets
a fresh generation after proof of the previous owner's empty scope.

PO turns keep a symlink to the canonical supervisor run directory, not another
copy of the owner. Failed launch, failed waiter start, completion and owner stop
all reach the same terminal operation. It verifies the row is still running,
records/selects terminal intent, proves empty, and commits the selected outcome
to the store with the owner lock held throughout. Owner interruption overrides
uncommitted completion or failure. A stop after committed completion sees no
running turn and changes nothing. Recovery consumes that same intent and operation,
retaining a successful answer or interruption rather than rerunning the input.
The store's conditional running-row transition makes feed publication and failure
callbacks idempotent. Waiter exit retains its existing wake callback for bounded
recovery even when cleanup fails. The service schedules its existing bounded recovery
backoff after newly orphaned claims and waiter completion. A pending owner stop
is revisited even while its waiter remains blocked. Running rows fence their
session's next input and restart admission; other sessions can continue.

## Prepared environment across privilege acquisition

Observer, worker and reviewer start through `LocalPtyHeadRuntime.start` and
`spawn_head`; PO turns reach the same `spawn_head` through `PoRunner._scoped_launch`.
`_supervisor_environment` retains its existing merge of inherited environment and
explicit overrides, with the importing product source first on `PYTHONPATH`.
PO's `turn_environment` puts its runtime interpreter directory first on `PATH`;
`session_environment` supplies session/request bindings. The shared launcher
transfers this complete prepared environment, including supplied empty strings
and home/configuration/auth bindings, across sudo's environment reset.

After the existing generation admission barrier releases, `scope_launcher` creates
one sealed anonymous memfd containing the environment and installs it as stdin.
The synchronous `sudo -n /usr/bin/systemd-run --system --scope` preserves stdin.
No environment payload is written to argv, systemd properties, journals or disk.
The gated process owns the descriptor until exec; kernel descriptor lifetime owns
cleanup on refusal, cancellation and crashes. No durable payload or replay owner
is added to the scope lifecycle.

Privilege acquisition uses absolute native binaries and a minimal fixed environment.
The privileged bootstrap uses the launcher's product interpreter with `-I` and an
absolute product script that inserts only its own source root. Caller PATH,
PYTHONPATH, PYTHONHOME, loader settings and home directories cannot select privileged
code or tools. It still verifies cgroup membership, installs the OOM contract and
opens the kernel stream, then uses absolute `setpriv` with the original native
uid/gid/supplementary groups. After that drop, an isolated runtime reader requires
a sealed regular descriptor owned by that uid, an initial offset of zero, and a
valid environment of at most 1 MiB encoded JSON. It replaces stdin with `/dev/null`
and execs the supervisor with the prepared snapshot. The OOM descriptor is the
bootstrap's own binding; the supervisor consumes it before launching the head.
Transfer failures emit fixed diagnostics and refuse the scoped launch. Existing
generation locks, cleanup proof and terminal settlement still own recovery.

The persistent PO deployed at `64c42d735bd99740bba94c65788d34f7f354bee0`
already executes a separate launcher from current product source with this environment
and the existing argv contract. The new launcher handles that actual emitter's
`sudo -E`/`env PYTHONPATH` prefix by selecting its own trusted bootstrap and carrying
the snapshot on stdin. The old prefix supplies no privileged code/environment
authority. Native identity flags must match the launcher. This permits the next
authorized PO refresh after delivery without an observer/PO restart, owner migration,
receipt edit or parallel compatibility subsystem. Scope records do not change.
Disposable tests load the emitter function directly from that delivered Git object;
CI runs it through real sudo, system scope, bootstrap, privilege drop, supervisor
and PO head with a fake executable found only on prepared PATH. It also verifies
`python3 -P -m ummanu --help` resolves through the prepared product runtime.
Local broad tests instrument the privileged effects; the real system scope, OOM,
descendant and PO recovery proofs remain dispatcher-owned CI evidence.

## Provider identity and scope ownership

`runtime.head_run_binding.head_run_binding` owns the provider/continuation digest.
Codex v1 fixes the input to `run_id`, the original workspace spelling, complete
serialized `task_ref`, role, and seven spec fields: `profile_id`, `adapter`,
`model`, `effort`, `resource`, `codex_mode`, `fallback`. The encoding is ASCII
JSON with sorted keys and compact separators, SHA-256 truncated to 32 hex digits.
The source descriptor separately carries the resolved workspace path. Addresses,
lifecycle, runtime, memory limit and scope generation do not enter this digest.
Full spec equality and scope generation remain fences in launch handoff; scope
admission and cleanup verify their canonical owner under its lock.

Preflight's `codex_provider_source_descriptor` calls the runtime implementation;
provider-event ingress verifies against that descriptor. The dispatch
`worker_lifecycle.head_run_binding` import exposes that same implementation to
provider cursor reads, retained-continuation liveness, observer/reviewer progress
and recovery. The provider-error reader shares the cursor reader's
`_codex_bound_source` verification of descriptor, journal root, session and initial
range. No reader discovers a replacement journal or changes a persisted digest.
Non-Codex sources retain their existing serialized-spec digest in the same runtime
implementation, preserving deployed Claude descriptors and continuation episodes.

Deployed Codex v1 descriptors retain their exact hash, including profile-derived
8192/12288 MiB runs. Old and new preflight producers therefore agree with the
corrected reader without migration. An old reader still rejects memory-bearing
Codex descriptors until it is refreshed. A persisted Codex continuation episode
carrying the divergent serialized-spec digest fails closed at episode admission;
it is never rewritten or tried as an alternate identity. Supported external runtime
refresh and settlement remain the observer's responsibility. Missing, malformed or
unsupported descriptors still refuse; this repair grants no recovery or rebind
capability.

## Causal OOM producer and consumer

The privileged scope bootstrap opens a read-only, nonblocking `/dev/kmsg` stream
and passes that descriptor to the trusted supervisor before dropping privileges.
Missing access refuses launch. The head child closes the descriptor, reserves
its new PID behind a barrier while still OOM-protected, and waits. The supervisor
seeks the stream to its end after fork, then releases the child to clear inherited
protection and exec. This excludes records for earlier uses of the PID or run.
The supervisor alone remains at `oom_score_adj=-1000`; `memory.oom.group=1`
includes the ordinary head, its children and threads in cgroup OOM termination.

On head exit the supervisor calls `waitid(WEXITED|WNOHANG|WNOWAIT)`, consumes
kernel records, then calls waitpid. It accepts only a kernel-facility
`Memory cgroup out of memory: Killed process <head PID>` record. Linux's
`__oom_kill_process` emits that record after sending SIGKILL while holding the
victim's task lock, before the victim can finish exit_mm. Thus it is available
before waitid reports completed exit. The zombie reserves the PID until reading
finishes. Observation can be delayed past a surviving child's subsequent OOM:
that kill record names the child, and cannot name this exited head. Operator
stop suppresses classification. Global OOM, victim-selection summaries,
oom_reaper messages, group counters and userspace kmsg records are insufficient.
Unreadable, overwritten or excessively large streams leave attribution unproved.
Positive evidence is journalled with the kernel sequence, boot timestamp and
head PID under this run's journal identity. The existing head-loss reader and
PO waiter consume `memory_limit` through normal recovery. A new `run.started`
ends the reader's search for an earlier incarnation's exit reason.

Producer sources: [Linux v6.8 oom_kill.c](https://github.com/torvalds/linux/blob/v6.8/mm/oom_kill.c)
(`__oom_kill_process` and `oom_kill_memcg_member`),
[memcontrol.c](https://github.com/torvalds/linux/blob/v6.8/mm/memcontrol.c)
(`mem_cgroup_scan_tasks` uses process iteration), and
[cgroup v2 membership](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html).
The CI tiny-scope test proves a real head kill with children and threads. Required
backend cases also freeze the supervisor until an unrelated head SIGKILL and
subsequent child group OOM complete, exercise detached descendant stop/retry,
and exercise newly orphaned PO cleanup/recovery. Missing backend prerequisites
fail those CI cases; local broad checks exclude those integration cases.

## Existing state and upgrade boundary

Deployed main produced unscoped HeadRuns and PO process identities. Its readers
and stop paths remain for those genuine records. The three predecessor memory
candidates were never deployed, so their scope-owner formats are not supported
upgrade inputs; there is no fallback from a malformed scoped owner to PID-only
success. Supported PO upgrade requests idle restart, and the old service drains
its running turns before exiting. Systemd's service cgroup teardown covers its
descendants; the new service recovers persisted rows and new turns gain scopes.
Legacy dispatcher runs must drain/stop through their recorded runtime and identity
fences before replacement, under the installation's supported upgrade procedure.
An unscoped historical exit has no causal OOM evidence and gains no invented
`memory_limit` reason. This code does not retroactively manufacture scopes or
claim recursive scope proof for a legacy process-group stop. Installed drain,
limits and recovery evidence remain a separate authorized PO operation.

## Runtime scopes in host reconciliation

`runtime.local_pty_head.runtime_scope_inventory(data_dir, units)` is the supported read-only
projection of `ScopedHeadLifecycle` ownership. Upgrade's host step, reconcile plan/apply and
doctor/status consume it through host inventory. The selected installation's canonical `heads`,
`po-heads` and `webproto/heads` directories are the authority; PO turn symlinks are recovery
pointers and are never followed to discover ownership. There is no additional registry or owner
format. The lifecycle's `validate_owner` is shared with its existing `read_owner`.

`ScopedHeadLifecycle.persist` durably admits identity before the launcher exists. Under that same
owner lock, `launch_until_started` checks the caller's generation and sends the admitted identity
through its existing exec gate. EOF still prevents native work, and release still follows durable
launcher PID and boot/start recording. The gated child checks the actual unit, run directory,
run/role/task/workspace argv. It supplies a digest of that admitted identity as the native scope's
`Description`, and carries the identity itself through the existing sealed environment descriptor.
Inherited environment cannot supply or override this binding; no environment values enter the
native description. Bootstrap still establishes group OOM protection and drops privileges before
reading the sealed environment.

Before forking the head, the supervisor consumes that sealed identity and reacquires admission.
It compares the actual supervisor identity, canonical directory, current owner, live launcher,
native scope description and cgroup. It fsyncs `scope.bound` in the existing supervisor journal,
including the admitted generation/workspace/canonical launch root and native InvocationID, monotonic activation
and cgroup device/inode, then fsyncs the directory and forks while admission remains held. A crash
before the binding leaves inspection unavailable; a crash after binding but before `run.started`
leaves independently inspectable native ownership. Head and launcher death do not erase binding
while descendants keep the same native scope alive. There is no new journal file, owner, registry
or host recovery authority.

The projection anchors directories and regular owner/journal/heartbeat files without following
symlinks, reads under the existing owner lock, validates run-derived unit/directory identity and
compares every claimed identity field to the launch binding. Its recorded root and directory must
be exactly the discovered `heads`, `po-heads` or `webproto/heads` path under the selected data root;
no installation root is guessed from a basename. The journal's binding must also match
the native description digest, boot, InvocationID, activation and cgroup inode. A forged journal
and matching substituted owner cannot borrow the unchanged native digest. An old attestation
cannot lend identity to a new invocation. A subsequent `run.started` must have matching
run/role/task/socket and heartbeat boot/start evidence; duplicate starts for one binding refuse.
Sequential attested generations can reuse a journal, but only its latest binding can prove the
current incarnation. Missing, damaged, duplicate, unreadable or substituted evidence refuses
recognition. The complete observed name set survives collection. Every name, including a
previously disappeared ownerless name, is freshly inspected before effects; genuine native
absence is an observed disappearance, never a lifecycle cleanup receipt.

Apply revalidates the projection before effects and rejects changed generation, launch identity,
directory, cgroup inode or systemd invocation. Recognized scopes appear separately in status and
reconcile/upgrade diagnostics. They are excluded from packaged missing/unmanaged comparisons,
never added to desired state or the managed manifest, and never enabled, stopped, rendered or
deleted by host reconciliation. Closed admission with cleanup pending retains the same runtime
owner; lock contention or insufficient native evidence makes inspection unavailable. Admission,
termination, descendant cleanup and empty proof remain `ScopedHeadLifecycle` responsibilities.
Unknown scopes still conflict and explicit foreign unit handling is unchanged.
Removing a packaged service named in a preserved scope's native `BindsTo` dependency is also
refused before effects: disabling that service would otherwise stop the scope indirectly.
A lifecycle-completed scope with verified empty membership can remain visible until systemd
collects it. Its completed flag is reported truthfully; a completed owner with populated
membership is a refusal. Host inspection never records or clears the lifecycle's empty proof.

Released 1879/64c42d7/1902 launches have no generation/workspace attestation. Their journal and
heartbeat prove some launch fields but cannot prove the owner generation or retained workspace;
the live launcher argv also lacks generation. They therefore remain actionable unavailable even
when live, admission-closed or empty but still natively loaded. No retrospective attestation,
adoption, whitelist or owner rewrite establishes availability. Their existing lifecycle retains
termination, recovery and recursive empty-proof responsibility. After ordinary settlement and
fresh native absence, host inspection can omit that name without writing a receipt. Genuinely
unscoped old heads are outside scope inventory and retain their existing runtime contract.

The already-running 64c42d7 PO caller passes generation in its existing outer launcher argv. A
later PO operation executes the newly delivered launcher from product source, which preserves
that generation across the gate and produces new binding before persistent PO itself restarts.
This is the bootstrap: the upgrading PO remains live and attested throughout dry-run/apply. New
worker/reviewer/observer and other-project launches use the same crossing. Overlapping old scoped
heads lacking evidence require their own normal lifecycle settlement or handoff, not host stop
authority or a global all-heads drain. A genuinely unscoped old observer can remain live. Card
worker and reviewer settlement uses normal review/release ownership before the new operation. A remaining
uncertain scope causes refusal, not an invented compatibility exemption. No user data is deleted
to establish this boundary.

`tests/test_runtime_scope_inventory.py` exercises the supported host step and doctor inventory
with disposable ownership/native fixtures. The CI-only self-upgrade case in
`tests/test_head_scope_backend.py` runs those consumers inside a real PO-like system scope and
then proves ordinary owned lifecycle settlement. It uses the actual released scope argv emitter,
a newly executed launcher, a harmless head and temporary instance,
data and packaged units. It also reads native host inventory with a reconciled empty packaged
catalogue and the real systemd installer, so native dry-run and apply must preserve their only
live resource without any installer effect, with native doctor and status preservation.
Packaged-fixture effects are recorded separately. A second required native case exits both head
and launcher while a detached descendant remains, checks retained binding and substitution
refusals through the public inventory and host apply, then settles through the lifecycle.
