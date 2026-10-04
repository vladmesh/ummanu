# Architecture

`ummanu` is a substrate for several interchangeable agent heads. It owns the task and memory
protocols, the dispatcher lifecycle, installation contracts and recovery. The actual work is done by
the providers' native CLIs.

Command and web contracts are in [Protocols](PROTOCOLS.md), runbooks in [Operations](OPERATIONS.md),
the PostgreSQL schema in [Board store](BOARD_STORE.md), checkpoint and restore in
[Recovery](RECOVERY.md), and the product goal in [Vision](VISION.md).

## Source layout and module boundaries

Product source uses a `src/` layout, so a test or command run from the repository root cannot import
an uninstalled checkout by accident. Packaging, scripts, docs, examples and tests stay at the root.

- `src/ummanu` is the product package. Its flat root is closed: `tests/test_architecture.py`
  holds the list of existing flat modules, and a new module must go into a feature package. Current
  packages: `automations`, `board`, `dispatch`, `infra`, `memory`, `po`, `projects`, `runtime`,
  `schemas`, `web`, `webfront`, `webproto`.
- `src/ummanu/automations` is the three background agents (curator, steward, retro), built on top
  of the rest of `ummanu`. `python3 -P -m ummanu automations <agent> <cmd>` is their one entry:
  `ummanu.cli` hands the argv untouched to the composition root
  (`ummanu.automations.composition`), which injects Ummanu's board ports and hands the rest to
  the mechanical-role driver and the agents' deterministic helpers. The package may import any
  `ummanu` module; no other `ummanu` module imports it, except the on-demand import behind the
  `automations` subcommand in `ummanu.cli`. `ummanu` finds the agents' shipped `automation.toml`
  specs through the product manifest (`[tool.ummanu] agent-specs` in `pyproject.toml`), not by the
  package name. `tests/test_architecture.py` holds the one direction and keeps the retired top-level
  `triggered_agents` package (historical name, removed in sprint:1459) from coming back.
- The packaged systemd timers are the only schedule owner of the background agents. Before
  sprint:1459 they ran as Orca automations; `upgrade` no longer creates, repoints or deletes any.
- Resource health has one writer and one vocabulary: `ummanu.head_health` runs each registry
  resource's `probe` command, classifies it (`ready`, `unknown`, `probe_broken`, `unauthenticated`,
  `exhausted`, `unavailable`) and caches it in `<data_dir>/dispatcher/resource_health.json` for
  300 s. The card dispatcher and the background agents' head resolution read that one cache; a
  head is launchable when its status is `ready` or `unknown`. The shipped registry's probes are `python3 -P -m ummanu.runtime.resource_probe --resource <id>` (`claude-sub`,
  `openai-sub`, `openrouter`): one cheap provider call, exit 0 healthy, 1 failed with one scrubbed
  reason line on stderr, 2 for an id it has no probe for. It writes nothing.
- `src/ummanu/runtime` holds the head-runtime utilities both the pipeline and the background
  agents use (`paths`, `references`, `prompt_document`, `launch_prefix`, `shared_state`,
  `claude_sessions`, `claude_env`, `state`, ...). New shared runtime code goes here, not into
  `ummanu.automations`.

The target package layout is feature-first. Modules move there one feature at a time, keeping
compatibility imports where an installed command depends on an old path:

```text
src/ummanu/
  cli/                 command parsing and rendering
  board/               board protocol, models and adapters
  tasks/               task lifecycle
  sprints/             sprint lifecycle and observation
  dispatch/            production dispatcher orchestration
  projects/            registered projects: their bindings and adapter-owned contracts
  runtime/             heads, sessions and prompt delivery
  automations/         curator, retro and steward services
  memory/              facts, journal, index and MCP service
  installation/        bootstrap, upgrade and host reconciliation
  backup/              checkpoint, backup and restore
  secrets/             secret storage and recovery
  infra/               filesystem, process, environment and path helpers
  schemas/             packaged data contracts
```

The dispatcher is being split by lifecycle ownership. `dispatch.worker_continuation` owns
retained/red continuation: durable rework intent, replayable board move, delivery and its bounded
provider-progress recovery, and the confirmed-stop handoff to `dispatch.worker_launch`. Durable
models stay in `dispatch.worker_lifecycle`. In `_advance_worker`, red-transition replay runs before
report lookup; delivery recovery runs after `worker_report_marker` but before `handle_worker_report`.
This ordering lets a report prove a prior delivery without resending its prompt. Gate/review decisions
and Assessment parking stay outside this boundary. There are no implementation callbacks into the
dispatcher for the extracted continuation methods.

`dispatch.wait_vitality` owns the shared worker/reviewer wait state machine: vitality reduction,
recovery-policy rungs, suspension SIGCONT/operator escalation, guarded one-shot respawn, second-stall
blocking and the bounded unobservable-head escalation. `DispatcherRuntime` (`ummanu.dispatch.runtime`) calls its four package
entry points from worker/review/gate orchestration; the module calls back only for the existing
worker/reviewer confirmed-stop lifecycle boundaries and routing; terminal effects go directly through
`dispatch.attempt_accounting`. Mechanical gate
verdict, transport retry, bounded infrastructure rerun and pending-CI policy are package-owned by
`ummanu.dispatch.gate_lifecycle`. `dispatch.review_verdict` owns durable review-verdict
acceptance and Assessment parking: marker consumption, reviewer-stop handoff, green/red bookkeeping,
the no-observer red ceiling, pre-park gate handoff, and park intent/replay. `dispatch.assessment_decision`
owns Assessment decision intake/replay plus rework/reslice execution. `dispatch.release_lifecycle`
owns the shared merge-readiness check, research-report publication, release replay/gate re-check,
merge-path blocking, merge/teardown, completion-evidence enforcement and the final transition to
Done. `dispatch.attempt_accounting` owns the shared attempt-usage/outcome boundary: durable
round/source handoff, phase usage accounting and publication, terminal outcome obligations, and the
generic terminal board effect. Reviewer launch/wait, confirmed-stop/adoption, routing bookkeeping and
provider ingress remain separate runtime boundaries.

Dependency rules:

- Dependencies point inward, from entry points to feature APIs. CLI and automation modules call
  feature services. Orchestration calls task, sprint and runtime APIs. An adapter implements a
  protocol owned by the feature that consumes it.
- Feature code does not import CLI modules. Shared runtime does not import an application module to
  find configuration; configuration and paths are passed in.
- A board client is built only through `ummanu.board.backend.board_client`, and a card audit only
  through `ummanu.tasks.task_audit_for`. `tests/test_architecture.py` holds both rules.

## Storage boundary

```text
product repository    ~/ummanu: CLI, runtime, schemas, tests, generic skills
live root             ~/ummanu-data/instance: one installation's config and portable state; plain files, no Git
snapshot repository   <data>/backup/instance.git: bare; the exporter's commits of the live root
instance remote       one private repository per installation; the pusher publishes the snapshot branch there
data directory        ~/ummanu-data: local mutable and derived runtime data plane
board store           live cards, sprints, products, issues and their audit
```

The product repository holds no real project bindings, credentials, cards or host-local state. No
product path names a user or a checkout.

**Live root.** The installation's own files sit in the live root, a plain directory that is never a
Git work tree ([Recovery](RECOVERY.md#layout)):

- config: `instance.yaml` (the data directory, the host ownership boundary, the sprint budget's
  `signal` and `hard` limits, the open-sprint limit, the PO head and the offsite settings), the
  project registry `projects/`, `adapters/`, the head canon `heads/heads.toml`, `persona/` and
  `skills/manifest.toml`. `projects/` is the one structured registry of connected projects.
  `persona/AGENTS.md` is the personal part of the interactive head's persona; see
  [The persona boundary](#the-persona-boundary);
- `state/memory` and `state/knowledge`: the curated memory canon and the knowledge documents;
- `secrets/`: a plain metadata catalog and sealed values. The raw installation key and the recovery
  phrase are never exported ([Recovery](RECOVERY.md#secrets)). The host `runtime.env` is a separate
  `0600` file, outside the export allowlist and every snapshot and archive; a value registered in the
  store makes the file a materialised copy.

Each live-root path has one writer, and none of them uses Git: the memory, knowledge and secret
writers and onboarding write files under their locks; any other config change is an operation card
checked with `ummanu config check`. No card branch lands in the live root; a card whose project
repository is the live root is refused at admission ([Recovery](RECOVERY.md#writers)).

**Snapshot exporter.** The dispatcher tick runs `checkpoint.SnapshotExporter`, the only code that
commits anything about the installation. Each changed window it cuts the live root's allowlisted
files, the board and run exports and a `snapshot-manifest.json` into one commit of the bare snapshot
repository, built with Git plumbing so the snapshot never has a work tree; the pusher publishes the
branch to the instance remote, fast-forward only, every 30 minutes. That remote tip is the recovery
checkpoint ([Recovery](RECOVERY.md#writers)). A live root that still has `.git` is an installation not
yet cut over: the tick keeps the legacy commit into that work tree, and `doctor` reports it red
(`live_root.git_work_tree`).

The data directory holds dispatcher state, the board and run exports, the head-registry pair,
onboarding drafts, derived memory exports and indexes, search logs, raw dumps, transcripts and
artifacts. The SQLite and vector index, worktrees, terminals and generated host resources are derived
and never exported; recovery rebuilds them from the snapshot.

The live board is the PostgreSQL board store, the one implementation of `TaskReader`/`TaskWriter`.
Its schema, transactions and migrations are in [Board store](BOARD_STORE.md). A sprint is a board
entity there, written only through `ummanu sprint`; its budget is derived from the audit of linked
cards, and knowledge keeps only its "why" document. Backup components are normalised board and
process state plus the `postgres_dump` of the store. Backup code does not read ORM rows. It asks the
board client for normalised state and the `board-store.env` resolver for the owner connection;
dump/restore runs in `board/postgres_recovery.py`.

Product configuration reaches an installation one way. `ummanu upgrade` generates
`<data>/heads/heads.yaml` from the installation's head canon (its own `heads/heads.toml`, else the
product default) and writes `<data>/heads/source.yaml` recording the canon, its owner, checkout,
revision and snapshot digest. The pair is generated state in the data directory: neither file is
exported, and `recover` regenerates both. A live tick reads only that installed pair, through
`head_registry.installed_pair`, so editing a product working tree changes nothing until the next
`upgrade`. The gap shows in `ummanu status` under `installation.head_registry`. A live root's own
`heads/heads.yaml` is never read; a missing `<data>/heads/heads.yaml` is an error that names `ummanu
upgrade` ([Recovery](RECOVERY.md#fresh-install-and-recovery)).

An installation is named by `--instance` or `UMMANU_INSTANCE`, else the default live root when it
exists, the checkout by `--product-root`. Every other path (skill targets, shell entry points, role
worktrees, runtime env file) hangs off the home of the account that owns the installation: the owner
of the instance directory, or `--runtime-user`. A run as root therefore writes the paths the units
name, not `/root`. Full order: [Operations](OPERATIONS.md#path-precedence).

Recovery reads the remote tip: an exporter snapshot is validated against its manifest and laid out as
the plain live root plus the snapshot repository; a checkpoint without a manifest, from before the
cutover, takes the legacy path and becomes a shallow Git checkout. Details:
[Operations](OPERATIONS.md#recovery) and [Recovery](RECOVERY.md#fresh-install-and-recovery). A manual
cold archive is optional and plays no part in recovery readiness.

## Runtime flow

```text
operator / automation
          │
          ▼
  ummanu task and sprint protocol ────────> board backend
          │                            ▲
          ▼                            │
 production dispatcher ──> HeadRuntime ──> runtime backend ──> native agent CLI
          │
          └──── run/audit state ──────┘

agent heads ── Bearer grant + HeadRun heartbeat ──> memory MCP/index <── facts journal <── curator
```

Every supported board write goes through `ummanu task` or `ummanu sprint`, which apply role
guards, transitions and append-only audit ([Protocols](PROTOCOLS.md#tasks)). Cards and sprints are
exported to the checkpoint and restored from it as separate sets
([Recovery](RECOVERY.md#what-the-checkpoint-contains)). Card references are recovery identities, not
backend row ids. Export and restore share one board validator, so the checkpoint never holds canon
that restore would refuse.

The dispatcher resolves routing, drives the worker and reviewer lifecycle, and checks board,
workspace, report and review state before each transition. A substantive reviewer verdict parks the
card in Assessment with the reviewer stopped and the worker held; the merge or next round runs only on
a tick that carries out a recorded observer decision. Mechanical outcomes resolve in Validate.

Every head is interactive. A Codex profile runs its TUI on `local-pty` like a Claude one; a registry
that still pins the retired `exec` launch mode is refused when it is loaded (`runtime.heads`), and a
card restored from an older checkpoint loses that mode at the write boundary. The shipped registry
puts the reviewer in the other model family from `new_card`, so a card is not reviewed by the model
that wrote it; a fallback that would make worker and reviewer the same head leaves the card in Ready
([Operations](OPERATIONS.md#head-readiness)).

Sprint budget comes from the durable audit of linked cards, not from the observer. The dispatcher
writes one budget event per source event. At the hard limit the sprint becomes `stopped` and a
`budget_hard_stopped` event is written. Observer reconciliation reads only open sprints, so that stop
removes the live head without touching claimed cards. The sprint's resume entry is structured
metadata; its freshness is computed against card audit.

Standing agents: curator, steward and retro all enter `python3 -P -m ummanu automations`. Its
composition root supplies task-backed ports for steward signals and reports and for retro Done
retention; curator needs none. The generic triggered-agent runtime owns only the port interfaces.
Each tick raises the role's head on `local-pty` or fails closed with a recorded reason and exit 1
(see [Head runtime](HEAD_RUNTIME.md#the-runtime-default)).

### Head runtime ownership

`HeadRuntime` is the lifecycle boundary for the dispatcher and the mechanical-role driver. Its verbs
(start, deliver, observe, request drain, stop, conditional stop) return typed receipts; callers do not
infer success from a socket write or process existence. There is one head runtime,
`local-pty`, and `ummanu.runtime.head_runtime_backends` is the only place a name becomes a
backend. A durable record written while heads were Orca panes (`orca-legacy`, or no runtime) still
loads and is shown as a legacy record, but no backend is built for it, so it is never launched or
delivered to. A dispatcher record from that time, one whose workspace is an Orca worktree or whose
head run is a legacy record, is refused by every host verb (launch, delivery, stop, teardown) with
one typed error, `LegacyDispatcherRecord`, before anything runs; the card goes Blocked with a reason
naming the record. What an absent key means, the `local-pty` parity criteria and the A20 exit
checklist are in [Head runtime](HEAD_RUNTIME.md).

- `LocalPtyHeadRuntime` runs a per-run supervisor that owns the process group, PTY, Unix socket and
  a versioned append-only journal. Delivery, drain and stop share one lock. The supervisor's status
  frame is the live source for turn, admission and journal sequence; a bounded 64 KiB journal tail is
  the fallback after it exits. Uncertainty closes admission; confirmed process death never creates a
  permanent lease. A prompt the dispatcher hands over with its transport is an agent's prompt: the
  runtime waits for the head to settle, types the line, sends Enter as a separate delivery and
  reports `ok` only once the head's output shows a turn started. The dispatcher addresses a
  supervised observer by its run.

Mechanical scheduler units are `Type=oneshot` ticks with `KillMode=process`, so a supervised head
outlives the tick. The next tick controls it through the runtime's identity-fenced drain/stop. The
role's `AgentState` keeps the `HeadRun`; starting over a matching live run is refused, while a run
confirmed dead can be replaced. Every supervised process has a run directory, identity record and
socket.

Head liveness and recovery rules are in [Head vitality](HEAD_VITALITY.md).

### Card workspaces

Every card is placed in a plain `git worktree` on its card branch, cut from the card's seed at
`<data_dir>/workspaces/<project id>/<worker>` (`dispatch.git_workspace.GitWorkspaceManager`).
Placement asks no runtime and reads no profile. The launch intent records that path before the
worktree is cut, and a worktree anywhere else is refused. A returned worktree is accepted only if it
passes the resumable-workspace check (this project's repository, this card's branch); otherwise it
is removed, branch included, before bring-up fails. Resume validation, discard, stop and teardown
read ownership from the path. The worker and the reviewer are two supervised processes in the same
worktree. When review starts, the worker's head is stopped and its commit recorded, and the merge
gate refuses a green verdict if the checkout has moved since. Teardown removes the worktree with
`git worktree remove --force` and `prune`, only after the heads were confirmed stopped. Head
rendering and delivery are adapter-specific, but not a stable plugin API.

A record whose workspace is under the Orca workspaces root (`UMMANU_DISPATCHER_WORKSPACES_ROOT`,
else `~/orca/workspaces`) and not a git worktree the host owns was placed by Orca before A20. It is a
legacy record: the host never resumes, re-places or tears it down (see
[Head runtime ownership](#head-runtime-ownership)).

A sprint observer's workspace is a detached `git worktree` of the observer repo at
`<data_dir>/workspaces/observers/<token>`, recorded by the launch intent before it is cut. Stop,
respawn and removal read it from the recorded path; a recorded path anywhere else is a legacy
record.

The dispatcher owns only `.ummanu-task-env/venv` in a card worktree; `.venv` belongs to the
project adapter. It claims the environment with an owner record, adds its workspace paths to Git's
`info/exclude`, and never writes production package paths into either environment. One immutable
`ProductionRuntime` value binds the production interpreter, product root and `ummanu` import at
workspace creation, launch, gate, release and teardown; a mismatch keeps the worktree and blocks the
card. Head-visible Ummanu commands name the absolute production interpreter with `-P`. Details:
[Operations](OPERATIONS.md#dispatcher-task-python-isolation).

Before any interactive Codex head launches (worker, reviewer, observer or service agent), one
preflight answers the CLI's first-run questions: it marks the workspace trusted, appends only missing
entries, and stops bring-up with a reason if a path is held at a different trust level. Then the head
is started, readiness awaited, the prompt delivered and the turn confirmed. A workspace that cannot be
prepared fails before anything starts. Operator `ummanu shell` sessions skip the preflight.

## The read layer

`ummanu.webproto` answers the operator's questions (what the system is doing, what a card is
doing, what happened to it) for every transport: the `web-read` CLI and the web dashboard. A
transport only maps typed errors to its own codes and renders snapshots.

The layer collects no facts of its own. Health is `collect_status` (what `ummanu status --json`
prints); projects are the validated bindings; cards come from `TaskReader`; history is the board's
append-only audit, so a cursor is a position in that audit; agents are dispatcher production state
plus launch heartbeats.

Constraints, enforced by tests:

- nothing under `webproto` imports HTTP, sockets, a framework or a template engine;
- read operations never write the board, dispatcher state, audit or installation, and take no actor;
- liveness is process state, never terminal or window state.

Protocol, schema, states and cursors: [Protocols](PROTOCOLS.md#reading-the-pipeline).

## The product runtime

The other half of `ummanu.webproto` raises a real worker head for a card and a reviewer head from
its result, and owns their workspace, process, pid, logs and outcome.

It reuses existing parts: `LocalPtyHeadRuntime` through
`head_runtime_backends.build_head_runtime`, the watchdog's launch-identity heartbeat for liveness,
the supervisor journal for exit status, the head registry and `head.command.render_head_command` for
the command, `codex_preflight` and `claude_env` for first-run preparation, and the board's audit
for run events. New parts: a `git worktree` workspace, a durable run record and one admission gate.

Invariants:

- Run phases `claimed → raising → raised → settled` move in one function, and every spawn and close
  goes through it. The record that can find and stop a head is durable before the spawn. The record
  alone is enough to stop the head. An unconfirmed cleanup is recorded as unresolved, and admission
  refuses a second run beside an unresolved one.
- "The run is over" is a boolean set by the lifecycle: the process is confirmed gone, or none was
  spawned. It is the only thing admission checks. The run's outcome is derived separately and may be
  `source_unavailable`.
- Raising a head and publishing its start, and settling a run and publishing its end, are separate
  durable writes. Every repeat and every read of a settled run republishes what it owes. Events are
  pure functions of the run record. A request id owns an operation and its inputs.
- `webproto.admission.admit` is the one ownership check: a product run is never a second owner of a
  dispatcher attempt or a card in a sprint's reserved projects.

Operations, idempotency, ownership and outcomes: [Protocols](PROTOCOLS.md#running-the-pipeline).

## The web transport

`ummanu.web` serves the dashboard, card, sprint, project and history pages and a JSON API over
HTTP, using the standard-library `http.server`. It is a transport like `web-read`/`web-run`: each
entry of `ummanu.web.app.ROUTES` is one `ummanu.webproto` operation, and one table
(`ummanu.web.statuses`) maps protocol codes to HTTP status. It holds no snapshot, state derivation,
liveness rule or mutation of its own. A missing fact is added to the layer, not to a page.

Constraints:

- `ummanu.web` imports nothing from the product except `ummanu.webproto`.
- No session state. Cursors belong to the client, and repeated POSTs carry the client's request id,
  so a retry never raises a second head.
- Every public `webproto` operation is wrapped by `webproto.boundary.ProtocolBoundary`, which turns an
  implementation failure into `backend_unavailable`. One unreadable source marks its page section
  unavailable instead of failing the page.
- Loopback only. The service has no authentication and its POST routes start heads and change
  state. A non-loopback bind is refused before a socket exists, after resolving the name and
  checking every resulting address.
- A run is shown as two facts: what its process did (outcome and whether it is over) and what it
  produced (result document, verdict, exit status).

`ummanu-web.service` runs it on `127.0.0.1:8787`. A product run started through it must name a
head-registry profile that declares `local-pty`.

Routes, status table, cursors: [Protocols](PROTOCOLS.md#serving-the-pipeline-locally). Running it:
[Operations](OPERATIONS.md#the-local-web-transport).

## The guarded front

External access is TLS plus one password, and the product contains no authentication code.
`ummanu.webfront` renders a Caddy configuration (Caddy from the Ubuntu archive) that terminates
TLS, checks the owner's password with `basicauth *` against a bcrypt hash, and proxies to loopback.

- The front is the only public listener; the application cannot bind anywhere else, and pages call
  the read layer in-process, so there is no internal HTTP surface.
- `ummanu.webfront.guard` parses the rendered file and reports every entry of
  `ummanu.web.app.ROUTES` that would be answered before the password check.
- The password and its hash live in the installation secret store. The rendered file is `0600` state
  under the data directory; rotation is `set-password`, `render`, restart, with no commit.

Commands: [Protocols](PROTOCOLS.md#publishing-the-pipeline-the-guarded-front). Runbook:
[Operations](OPERATIONS.md#the-published-web-front).

## The sprint observer head

Each open sprint gets its own observer head. It never claims cards, never appears in card records
and never takes the per-project claim gate; the dispatcher runs the sprint's cards independently.
Operator view and states: [Operations](OPERATIONS.md#sprint-observer-heads). Sprint contract, the
declared observer and the observer fence: [Protocols](PROTOCOLS.md#sprints).

The production tick runs an observer reconciliation pass against the sprints board:

- open sprint, no live head: launch one;
- open sprint, live head: nothing (one head per sprint);
- open sprint whose head's launch identity positively shows a dead process: launch a replacement, on
  the persisted launch backoff. A missing or unreadable identity is not death;
- closed or vanished sprint: stop the head and drop the record;
- unreadable sprints board: change nothing.

A stop the host rejected is not a stop: the record stays `stop-pending` with its handle, and a
relaunch waits until the old terminal is closed.

The head profile is the sprint's declared `sprint_observer`, resolved against the installed
`heads.yaml` without fallback to another profile; an unknown profile fences the sprint's projects.
`role_defaults.observer` never chooses it. The profile is interactive (one session per sprint). Launch
passes the same resource-readiness gate as a card claim, uses the ordinary head-command renderer and
role environment wrapper, and gets only role-scoped environment, not the whole `runtime.env`.

The observer workspace is cut from a dispatcher-owned empty repository without a remote
(`<data>/dispatcher/observer-root/observers`, created on first use), as a detached worktree under
`<data>/workspaces/observers/` (`CommandHostRuntime.observer_workspace`). An observer reads the board
and the sprint entity and owns no branch, so it never gets a project checkout. Reconciliation neither creates
nor deletes this repository, and `doctor` accepts its registration only at that path. Stopping ends
the confirmed head first, then removes the git worktree.

The launch prompt is rendered from the live sprint entity and points to the `observe-sprint` role
skill by path without repeating it. The skill lives in `skills/` of this repository, is registered as
the `observer` role in `skills/manifest.toml` and is delivered by `ummanu role-skills sync` to the
shell of every profile a sprint may declare. If the skill is missing from the target shell, launch is
deferred with a reason naming the file.

### Wakes and delivery

Liveness uses the same pid heartbeat as workers and reviewers. Readiness is the head runtime's own
observation of the head (`observe`), read by its run. A head that cannot be addressed or observed
goes into the bounded failure path, not a wait.

A committed, significant event on a linked card opens a durable delivery batch with an immutable
high-water mark, written before a nudge or replacement. Only an observer resume carrying that
delivery's audit marker acknowledges the batch. An active card alone does not create a turn. A batch
is redelivered when the head is seen ready without acknowledging it, or when its acknowledgement
deadline passes; a head never ready for input is bounded by a turn ceiling. Failed wakes retry a
bounded number of times on the live head, after which the head is replaced and the same delivery
marker is carried into the new launch. Cumulative wake and launch-delivery counts and the last
failure's bounded evidence (never prompt text) stay on the observer record while the sprint is open.

All interactive heads share one delivery path, whichever provider or role:

- The head receives one short line with the absolute path of a task document (the worker's
  `TASK.md`, the reviewer's private review document under run artifacts). Nothing from a card
  description reaches the head's input.
- The head runtime delivers it: it waits for the head to settle, types the line, sends Enter as a
  separate delivery and reports `ok` only once the head's output shows a turn started. The
  dispatcher's pre-send step (a retained worker's `SIGCONT`, a Codex head's provider-source binding)
  runs after admission and before the first byte. Refusals surface as delivery failures with
  evidence.
- A turn is confirmed from the provider's own local session record (Claude or Codex). The status line
  may confirm a turn but never refute one.
- An unconfirmed delivery does not stop the head. Bring-up returns an abort with evidence and keeps
  the launch intent; the next tick adopts the head or stops it through its retained identity.
- Reviewer bring-up failures go through one recorder in `start_review`, which stores
  `review_delivery_failures` and `review_delivery_evidence` on the card without changing routing.

### Durability

Lifecycle events go to the durable audit keyed by sprint reference and are deduplicated by request id,
which includes the record generation. As with card writes, the event is staged, the host is called,
then the event is committed. A staging failure cancels the action. A commit failure keeps the effect,
leaves the event pending for `ummanu task reconcile-audit`, and reports a pending audit.

The launch intent (sprint, generation, profile, attempt, workspace, pid file) is flushed to production
state before the host call; unwritable state means no launch. A tick that dies after the host call
leaves the intent, and the next tick adopts a live pid, waits out the startup window, or closes the
workspace terminals and relaunches.

A freeze stops observer heads and records the reason; resume brings them back through reconciliation.
A drain leaves live heads alone and launches none, but a sprint opened during a drain still gets a
deferred record.

## Memory plane

Facts are markdown records under `state/memory/facts` in the live root. The curator writes
through `ummanu memory propose/commit/supersede`, which writes only `state/memory`, as files and
without Git, all or nothing under the shared live-root writer lock
([Recovery](RECOVERY.md#writers)). The butler may only `propose`; `commit` and `supersede` belong
to the curator, ummanu and operator roles ([Protocols](PROTOCOLS.md#memory)). Other heads read
through MCP. The NDJSON export and the SQLite/vector index in the data directory are rebuilt from the
canon, and one index writer publishes at a time.

Unresolved cross-project conclusions sit under `state/memory/facts/po-review` as scope `review:po`.
The interactive PO, curator and retro see them; worker, reviewer, observer and steward grants do not.
A reviewed item is superseded into its final scope.

The shipped `packaging/memory/product-ummanu` pack feeds the same canon at
`state/memory/facts/product-ummanu` (scope `product:ummanu`). Its manifest, paths and SHA-256
digests are verified before it is materialised. `state/memory/packs/product-ummanu.json` records
the installed digest and owned fact ids; a local fact with a shipped id is refused. Incremental index
reconciliation reuses embeddings with unchanged id and digest. The ledger is `pending` until the
export is handed to the memory daemon's runtime user, then `ready`, so a failed handoff is retried by
the next upgrade.

The embedding model runs locally and is the appliance's main memory consumer
([Operations](OPERATIONS.md#system-requirements)).

## Knowledge planes

Where a record goes depends on its length and purpose:

- the Pipeline board holds executable work: cards, specs, states;
- curated memory (`state/memory/facts`) holds the short current conclusion a head receives through
  `memory_search`;
- knowledge (`state/knowledge`) holds the long reasoning behind it: brainstorms, decision logs,
  incident write-ups.

Sections directly under `state/knowledge` belong to the installation. A connected project's documents
live under `state/knowledge/projects/<project id>/<section>/`, with the id from `projects/`. A product
repository carries contracts and code; the reasoning behind its development is installation state.

Knowledge is not indexed, not returned by `memory_search` and never loaded wholesale into a head's
context. Format is free markdown. Writes go through `ummanu knowledge write`, which owns only
`state/knowledge`, takes the shared live-root writer lock, starts no Git child and refuses documents
containing secrets ([Protocols](PROTOCOLS.md#knowledge)).

### The persona boundary

The interactive head (`ummanu shell`) is the one head with a persona, and it receives it through its
own workspace, `<data>/interactive` (decision on ummanu-33). `upgrade` and `recover` compose its
`AGENTS.md` in one function, `ummanu.runtime.interactive_workspace.materialize`:

- the **shared part**, the role contract, shipped in the product as
  `packaging/interactive-workspace/AGENTS.md`; it carries nothing owner-personal;
- a separator, then the **personal part**, byte for byte from the live root's `persona/AGENTS.md`
  (in the snapshot allowlist, so it is exported and recovered with the configuration). Without that
  file the workspace holds the shared part alone.

`CLAUDE.md` there is `@AGENTS.md`; Codex reads `AGENTS.md` from its cwd. Nothing else receives the
persona: not the PO workspace, observer, worker or reviewer workspaces, and not the owner's global
`~/.claude/CLAUDE.md`, which every Claude head on the host loads and the product never writes.
Hermes is a separate product and keeps its own persona. `tests/test_interactive_workspace.py` holds
the boundary.

## Ownership and security

- The security profile assumes one trusted host owner. Agents are not isolated as untrusted tenants.
- `doctor` reads config, data and host inventory and never changes the host. `status` and `doctor`
  share one recovery projection; it does not decrypt values, update the probe cache or launch heads.
- `reconcile plan` computes desired state. A matching name or prefix confers no ownership without a
  managed manifest or a product-written marker. Its kinds are project checkouts and systemd units.
  An `orca` record an older reconcile left in the managed manifest is kept, untouched (A20 step 8).
  A binding's `orca_binding` is optional legacy, read only by curator routing
  ([Head runtime](HEAD_RUNTIME.md#a20-exit-checklist)); new projects have none. Card placement
  never reads it.
- Store-registered secrets reach the snapshot and the instance remote only as encrypted envelopes.
  The raw installation key, the recovery phrase and `runtime.env` are never exported. Facts, exports and diagnostics carry no
  secrets.
- Private instance-remote and project Git go through one product-owned remote-execution boundary. HTTPS
  children clear ambient credential helpers and use explicit bootstrap input or the managed envelope;
  checkpoint probes and pushes use only the managed envelope. Local/file remotes are plain Git, SSH is
  manual bypass, and HTTPS hosts other than `github.com` are refused.
- Root-run recovery hands the instance and data roots to the installation account at one named
  ownership barrier, before that account's first secret-consuming Git child, and verifies the restored
  key is a regular `0600` file owned by it.
- Head-registry materialisation writes `<data>/heads/` and makes no Git call. The snapshot pusher
  publishes fast-forward only and stops on divergence. There is no reset, rebase, force-push or
  ambient credential fallback.
- Recovery isolates one binding's provisioning failure; core failures and interruption are not
  isolated. `ProjectAvailability` carries unavailable checkouts into host planning and dispatch; an
  unavailable binding gates its own worker and reviewer, never observers. The installation stays
  degraded until every binding is available and the recovery checkpoint is published.
- Task audit and pending writes fail closed: an unfinished board mutation blocks export and the
  checkpoint.
- Restoring a normalised board writes through the card client in one enclosing store transaction.
  Each card or comment has a durable `restored_bulk` obligation in the audit before any write, and
  replay mutates nothing that is already proved. Test coverage:
  [Testing](TESTING.md#normalized-board-bulk-recovery).
