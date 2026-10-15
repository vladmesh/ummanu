# Roadmap

Direction and open work. Milestones describe useful product states, not release dates. Current
behaviour is in [Architecture](ARCHITECTURE.md), [Protocols](PROTOCOLS.md) and
[Operations](OPERATIONS.md); known defects are prioritised issues on the board under the `ummanu`
product.

## Sprint programme

The sprint system builds its remaining programme through itself:

1. typed tasks: `code`, `research` and `operation`, with a versioned knowledge artifact for every
   research task and a structured state-evidence report for an operation;
2. abstract routing levels (`low`, `medium`, `high`, `frontier`) resolved by the dispatcher into
   explicit family, model and effort, with append-only per-round telemetry and a single-family
   degraded mode;
3. review convergence: a review-only budget with analysis at red review three, a hard decision at
   five, one exceptional sixth fix round, then a mechanical wait for the owner;
4. durable product decision requests and `awaiting_decision` when the Definition of Done proves
   impossible or materially incomplete;
5. more than two parallel sprints.

Reviewer verdict and accept decision stay distinct. At the budget boundary the observer may accept a
mechanically green, architecturally convergent increment with follow-up issues, reslice it, or use the
last fix round. It never rewrites a red review into green.

Raising the open-sprint limit past two (current pilot:
[Protocols](PROTOCOLS.md#the-open-sprint-limit), [Operations](OPERATIONS.md#the-two-sprint-pilot))
needs:

- pause and drain scoped to one sprint instead of the whole installation;
- a declaration a tick can check without the sprints board, so an unreadable board fences less than
  everything. It would also close the blind-fence gap where a sprint admitted since the last readable
  pass leaves an unlinked card in a reserved project unfenced.

The task model also lacks typed execution gates, a coded completeness check, per-project overlay
deviations, an in-house CI runner for private repositories, and cleanup by owner label.

## Milestone 1. Reliable fresh install

### Goal

A short bootstrap and install flow creates the appliance on a clean Ubuntu 24.04 VPS: the dedicated
OS user, the private instance repository, the local data plane, and the board, head runtime,
memory service, dispatcher, background roles and schedules. Agent heads and provider logins remain a
separate operator choice.

### User path

```text
install ummanu
  -> bootstrap host with installation user and private remote
  -> install a new instance or recover an existing one
  -> connect agent heads
  -> status
```

### Done when

- A clean-host end-to-end run passes on an Ubuntu 24.04 host with no pre-prepared home directory for
  the installation user, no checkouts, no board and no head-runtime state.
- Every host path and resource name is derived from the instance and the discovered host context.
- The installer installs and configures the bundled board without a pre-prepared runtime; the head
  runtime, `local-pty`, ships with the product.
- Memory, dispatcher, curator, steward, retro and the schedules come up through the materialiser with
  no hand-copied units and no editing of generated files.
- A repeat apply is idempotent, and an existing installation user triggers an explicit adopt/recover
  gate.
- An installation with no heads is a valid, observable state.

### Open questions

- Compatibility and upgrade policy for the pinned board and session-manager versions.
- Minimum CPU, RAM and disk for the production memory profile.

## Milestone 2. Git-backed recovery

Complete; see [Recovery](RECOVERY.md).

## Milestone 3. Head onboarding and explainable routing

### Goal

The owner adds heads after bootstrap. The system discovers an installed CLI or offers to install it,
runs the external auth flow, checks capabilities and creates runnable profiles. Routing picks a head
and an account without a neural model in the loop.

### User path

```text
ummanu head add
  -> discover or install runtime
  -> authenticate account
  -> create account pool and profile
  -> probe
```

### Done when

- The model distinguishes agent runtime, account, account pool and head profile; a runtime is not a
  model provider.
- Every task selects one abstract level (`low`, `medium`, `high` or `frontier`). The observer sees
  levels and model families, not concrete model names or effort values.
- The router applies overrides and hard availability constraints first, then weighs capability, quota,
  reset state and a preference for a different family for review.
- Worker and reviewer get the same level. The dispatcher resolves both to explicit profiles, models
  and efforts and may swap their families after a convergence signal.
- If one provider budget is exhausted, work continues in a visible single-family degraded mode.
  Predicting exhaustion and rotating accounts in advance are not part of the first step.
- An account in `unknown` state is available optimistically; quota, auth and transient failures move
  it into an explainable circuit-breaker state.
- Every run records the requested level and the resolved family, runtime, profile, model, effort,
  account, outcome and decision trace. Automatic model-quality scoring is a later task.

### Open questions

- Quota telemetry sources for each runtime.
- Automatic account rotation policy, once there is operational data.

## Milestone 4. Daily control plane

### Goal

The operator manages projects, settings and runtime through one product interface instead of
assembling low-level commands. The CLI stays the first interface; the board, the web dashboard and a
live terminal view cover work and observation.

### User path

```text
add project
  -> scan
  -> propose adapter
  -> provision
  -> gate
  -> smoke card
```

### Done when

- A high-level project workflow folds add, provision and gate into one resumable flow.
- `ummanu status` combines services, schedules, heads, quota state, projects, cards, memory and
  checkpoint freshness; `doctor` stays the strict invariant check.
- Install, start, stop, logs, upgrade and uninstall are available through the product CLI.
- Schedules and their single owner are configured centrally and applied idempotently.
- Settings change through validated operations, while instance-repository files stay canonical.

### Open questions

- How far the web dashboard grows into a full control plane.
- When Git-backed config should be replaced by a control-plane database.

## Milestone 5. Protocol runtime boundaries

### Goal

Dependencies on the board backend, the head runtime and specific CLIs sit behind checkable
contracts. This is not a public plugin API; it is the ability to replace a backend without rewriting
the task and agent lifecycle.

### Done when

- A board adapter implements the normalised task model, transitions, audit, and the export and import
  contract.
- A session protocol creates and lists durable sessions, starts processes, streams output, accepts
  input, reports exit state, terminates process trees and reconciles orphaned state.
- Head adapters implement discover, install, probe, launch, delivery and observe without task routing.
- Every contract has a backend-independent contract suite.
- A session-manager UI failure does not destroy task state or recovery semantics.

### Open questions

- Settled in A20 (sprint:1459 and sprint:1461): the minimal in-house backend, `local-pty`, replaced
  the session manager heads ran in before (Orca); see [Head runtime](HEAD_RUNTIME.md#orca-migration-history).
- Whether there is a real need for a public extension API.

## Milestone 6. First supported release

### Goal

A new user can install a versioned release, walk the main path and understand its boundaries without
knowing the project's history. The repository, licence and contribution path are already public; this
milestone makes a release supportable.

### Done when

- There is a versioned package, release notes, a compatibility matrix, schema and data migrations,
  and rollback.
- A clean-VM end-to-end run covers install, head onboarding, project add, a worker/reviewer task, the
  Git checkpoint and recovery on a second target.
- The trusted single-user security boundary, credential scopes and agent host access are documented.
- Minimum deployment requirements are published.
- Examples contain no private paths, accounts, projects or the author's historical repositories.

### Open questions

- What telemetry may be collected locally, opt-in only.

## Later directions

After the main delivery path: Telegram and voice as entry channels, a remote control plane for a
phone, richer model-quality metrics and more deployment profiles. Team work, a multi-tenant SaaS and
billing are not on the roadmap.
