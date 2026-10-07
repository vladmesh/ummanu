# Git-centric recovery

The private instance remote is the recovery contract. Its branch tip is a snapshot of the
installation: the configuration and portable state the snapshot exporter cut from the live root,
plus a manifest. Moving to a new machine needs the product, access to that remote, the recovery
phrase and the credentials the snapshot does not hold. No bundle or object-store transport is part
of the main path.

## Topology

```text
product repository    public template: product, CLI, runtime, schemas, generic skills
live root             one installation's config and portable state; a plain directory, no Git
snapshot repository   bare and local; the exporter's commits of the live root, the only commit target
instance remote       one private repository per owner; the pusher publishes the snapshot branch there
data directory        local runtime data plane, rebuilt from the snapshot; not canonical
```

## Names on the host

A recovery recreates these names; nothing reads an older spelling of them.

| What | Name |
|---|---|
| Product checkout and venv | `~/ummanu`, `~/ummanu/.venv` (console scripts `ummanu`, `ummanu-memory-*`) |
| Data plane | `~/ummanu-data` unless `instance.yaml` `data_dir` says otherwise |
| Live root | `~/ummanu-data/instance`, a plain directory with no `.git`; the default of `--instance` and `UMMANU_INSTANCE` (`runtime.paths.default_instance_path`) |
| Snapshot repository | `<data>/backup/instance.git` unless `offsite.snapshot_repo` says otherwise; bare, branch `main`, written only by the exporter (and by a recovery that lays it out) |
| Instance remote | `offsite.instance_remote`; the private remote keeps its name. The pusher publishes the snapshot branch to it; nothing runs from a checkout of it |
| Old live root | `~/secretary-instance`, the instance repository's work tree the live root was before the cutover. A cut-over host keeps it read-only (`chmod -R a-w`) for rollback and reference; nothing reads or writes it, and `live_root.old_path` (below) flags anything that still names it |
| systemd units | `ummanu-*` from `packaging/systemd/` (`host.unit_prefix: ummanu-`), e.g. `ummanu-dispatcher-production.timer`, `ummanu-po.service`, `ummanu-instance-maintenance.timer` |
| Role worktrees | `~/orca/workspaces/ummanu/<role>` |
| Environment | `UMMANU_INSTANCE`, `UMMANU_DATA_DIR`, `UMMANU_REPO`, `UMMANU_RUNTIME_ENV_FILE`, and every other `UMMANU_*` key |
| Board store | Compose project `ummanu-board-store`, file `/opt/ummanu/postgres-compose.yml`, container `ummanu-board-store-postgres-1`, volume `ummanu-board-store_board-db`, label `ummanu.production-board`; database `ummanu`, roles `ummanu_owner`, `ummanu_app`, `ummanu_read`; `board-store.env` keys `UMMANU_DB_*` |
| Cold archives | `<data_dir>/backups/ummanu-backup-<kind>-<stamp>.tar`, manifest `"tool": "ummanu"` |

An installation from before the product rename is moved onto these names once, by the transition in
`docs/RENAME.md` §T3; archives taken before it restore only with the pre-transition code.

**Default and refusal.** A command given neither `--instance` nor `UMMANU_INSTANCE` uses the default
live root and refuses, naming the path, when that directory does not exist
(`runtime.paths.resolve_instance_path`, `MissingDefaultInstance`); it never creates it. Only
`install`, `recover` and `bootstrap`, with their explicit target, bring a live root into being.
Packaged units and dispatcher-launched heads set `UMMANU_INSTANCE` explicitly.

**Doctor findings.** `ummanu doctor` reports two red findings for a live root still in its old shape
(`infra.live_root_findings`):

- `live_root.git_work_tree`: the configured live root holds `.git`;
- `live_root.old_path`: an installed `ummanu-*` unit file, the live root's `runtime.env`, or the role
  env of a process bound to this live root (`UMMANU_INSTANCE`, `UMMANU_RUNTIME_ENV_FILE`,
  `TA_RUNTIME_ENV_FILE`) names the old live-root path, `~/secretary-instance` (the spelling lives
  only in `ummanu.transition.names`).

A cut-over installation has neither. An installation that has not been cut over has both by design:
its live root is still the instance repository's work tree, and its units name it. The cutover
(final legacy checkpoint and push, a copy of the live root without `.git`, the
[seed](#writers), `upgrade --instance` onto the new root) clears both.

The instance remote is the only Git canon for the data plane: one remote, one branch, one RPO.

## Source of truth

The selected board backend (PostgreSQL in production) is the operational store. The remote branch
tip is the last confirmed recovery checkpoint. Between commits, live state runs ahead of the
checkpoint by the RPO; that gap is expected.

## What the checkpoint contains

The canon is the normalised minimum needed to resume work:

- instance config: `instance.yaml`, `persona/`, `projects/`, `adapters/`, `skills/manifest.toml` and
  `heads/heads.toml`, this installation's heads canon when it has one. The generated `heads.yaml` snapshot and its
  `source.yaml` pin are not canon: they live in `<data>/heads/`, and `upgrade` and `recover`
  regenerate them from the canon (see [Fresh install and recovery](#fresh-install-and-recovery));
- board export: the logical files `cards.ndjson`, `sprints.ndjson`, `events.ndjson`, `audit.ndjson`,
  `export.json` and the analytics seal `analytics-manifest.json`, stored in `state/board` in the
  split layout (see [Board checkpoint layout](#board-checkpoint-layout));
- run and audit state: `state/runs/runs.ndjson`, `claims.json`, `watermarks.json`, `export.json`;
- memory facts and pack ledgers: `state/memory/facts/**`, `state/memory/packs/*.json`;
- knowledge documents: `state/knowledge/**` (free-form markdown, see
  [Architecture](ARCHITECTURE.md#knowledge-planes));
- the secret store under `secrets/` (see [Secrets](#secrets)).

Board records are NDJSON for line-wise diffs. Derived JSON card and sprint duplicates are not
checkpointed.

`runs.ndjson` is a portable journal: each entry records its source path and line number. Recovery
materialises the entries back into the pipeline role worktree's live `state/pipeline/` sources, per
source, preserving line-number gaps, before dispatcher units are installed or started. A live journal
that is a valid append-only extension of the checkpoint is kept; a divergent or truncated prefix is
never overwritten. A checkpoint likewise refuses to publish a truncated or rewritten live export over
a non-empty canonical journal.

Cards (including Product and Issue records) and sprints are separate sets; the writer reads sprints
in their own pass. A sprint record carries its reference, goal, Definition of Done, repositories,
owning product, issues, reserved projects, status, budget by event type, current card, resume entry,
all entries and the source's audit metadata. A record without a product, issues or reservations
omits those keys rather than storing empty values. Derived values (budget totals, installation
thresholds, resume freshness) are recomputed, not stored.

Outside the canon, rebuilt or kept in an optional cold archive:

- raw board dumps;
- the vector index and derived memory exports;
- transcripts, artifacts, backups;
- terminals, worktrees and generated host state (systemd units from `packaging/systemd/`). The
  product is canonical for these: units are compiled from packaging templates, and those timers are
  the background roles' only schedule. `ummanu reconcile apply` and `ummanu upgrade`
  re-materialise them idempotently, so unit names stay stable. The Orca automations the background
  roles ran as before sprint:1459 are not recovered;
- generated installation state in the data directory: the head-registry pair in `<data>/heads/`,
  and onboarding's drafts, provision runs, gate runs and compatibility manifests in
  `<data>/onboarding/` with their locks in `<data>/locks/onboarding/`. Onboarding recreates its
  own; the pair is regenerated by `upgrade` and `recover`;
- the web front's rendered `<data>/webfront/Caddyfile` (it holds the password hash), rendered again
  by `install`, `recover` and `upgrade` from `host.web_front.sites` and the secret store.

## Layout

The live root is a plain directory. It is not a Git work tree, and nothing commits in it; each path
has one writer ([Writers](#writers)):

```text
<live root>/                                 ~/ummanu-data/instance by default
  instance.yaml, heads/heads.toml, persona/**,
  skills/manifest.toml                       config: an operation card plus `ummanu config check`
  projects/<id>.yaml, adapters/<id>.yaml     config: onboarding (project add, provision-apply, gate)
  state/
    memory/facts/**, memory/packs/*.json     memory writer
    knowledge/**                             knowledge writer: brainstorms, decision logs, incident write-ups
  secrets/
    catalog.yaml, installation-key.json,
    values/<id>.enc.json                     secret writer
    installation.key                         raw installation key, 0600, host-local
  runtime.env, board-store.env               host-local, 0600
  .ummanu-state-writer.lock                  the shared writer lock, host-local
```

The board and the run journals are not in the live root. The checkpoint service exports them from the backend into
the data directory, and every cut takes `state/board` and `state/runs` from that export, never from
the live root.

Host-local files are the ones the export allowlist does not match: `secrets/installation.key`,
`runtime.env`, `board-store.env`, the writer lock, a bootstrap stamp
([Local-file exclusion](#local-file-exclusion)).

What the installation generates lives in the data directory and is never exported:
`<data>/heads/heads.yaml` and `<data>/heads/source.yaml` (the head-registry pair, written by `upgrade`
and `recover`), `<data>/onboarding/{adapter-drafts,provision-runs,gate-runs,compatibility-manifests}/`
and `<data>/locks/onboarding/`. Copies an older version left in the live root (`heads/heads.yaml`,
`heads/source.yaml`, `adapter-drafts/`, `gate-runs/`, `provision-runs/`, `compatibility-manifests/`,
`.locks/`) are never read. `policies/` is dead configuration that no code reads; the cutover leaves it
out of the live root.

`data-manifest.json` is generated descriptive inventory, not a path-configuration file. Component
paths are relative to `data_dir`, except `memory.facts`: `state/memory/facts` is relative to the live
root. Older manifests remain readable; editing a manifest does not relocate data.

Memory facts use optional YAML mapping frontmatter between exact, unindented `---` lines (LF or CRLF;
the closing line may end at EOF). The writer and indexer report malformed frontmatter as a controlled error. Snapshot
export preserves the original fact text, including malformed facts, so a checkpoint does not discard
material that needs repair.

### Snapshot repository

The snapshot repository is the exporter's derived artifact, not a working tree: a **bare**
repository at `offsite.snapshot_repo` in `instance.yaml` (a relative value is rooted at the data
directory), by default `<data_dir>/backup/instance.git`, branch `main`. The exporter is the only code
that builds a commit there, and the pusher publishes that branch to `offsite.instance_remote`. The
snapshot tree keeps the live root's relative paths:

```text
<snapshot tree>/
  snapshot-manifest.json   format "ummanu.instance-snapshot", version 1, product_revision,
                           board_schema_head, files: {path: sha256} of every other file
  instance.yaml, projects/*.yaml, adapters/*.yaml, heads/heads.toml, persona/**,
  skills/manifest.toml, secrets/catalog.yaml, secrets/installation-key.json,
  secrets/values/*.enc.json, state/knowledge/**, state/memory/**     the export allowlist
  state/board/   layout.json, export.json, analytics-manifest.json,
                 cards/NNNN/NNNNNNNN.json, sprints/NNNN/NNNNNNNN.json      one record per file
                 audit/NNNN/NNNNNNNN.ndjson, events/NNNN/NNNNNNNN.ndjson   immutable segments
  state/runs/    runs.ndjson, claims.json, watermarks.json, export.json
```

The export allowlist is `checkpoint.SNAPSHOT_ALLOWLIST` (kept in `infra.export_allowlist`), a closed
set: `*` matches inside one path segment and a trailing `**` everything below a directory. Nothing
else in the live root is exported: not `heads/heads.yaml` or `heads/source.yaml`, `policies/`,
`tests/`, `README.md`, `CONTEXT.md`, `.gitignore`, onboarding, gate, provision and compatibility
directories, `.locks/`, `state/checks/`, the live root's own `state/board` and `state/runs`, the
writers' undo and swap areas, and never `secrets/installation.key`, `runtime.env` or
`board-store.env`. The manifest holds no clock value, so an unchanged state yields an unchanged
manifest; a new product revision or board schema head is a change.

A tree a legacy checkpoint committed (the instance repository's work tree, before the cutover) has the
same paths and blob ids for the same live state, the board's segments included; the manifest is the
only extra file, and the one a recovery tells the two shapes apart by
([Two remote shapes](#two-remote-shapes)).

**Takeover marker.** `refs/ummanu/snapshot-base` (`checkpoint.SNAPSHOT_BASE_REF`) names a blob of
one line: the parent of the exporter's first commit on this branch (a seeded legacy tip), or `root`
when that commit was a root commit. The exporter creates it once, in the same `update-ref --stdin`
transaction as that first commit, and only when the tip it builds on is not its own (empty, or
seeded). Apart from the exporter, only a snapshot recovery writes it: it points it at the recovered
tip (see [Snapshot recovery](#snapshot-recovery)). It is a local ref and is not pushed.

**Doctor `snapshot.foreign_commit` (red).** For every commit in `<base>..<tip>` of the snapshot
branch (the whole branch when the base is `root`), doctor (`checkpoint.snapshot_foreign_commits`)
checks the exporter's author and committer identity, the subject prefix, exactly one parent (the
exporter's root commit: none) and a `snapshot-manifest.json` in its tree. For the tip it also checks
that the manifest lists exactly the tree's other files and that every per-file digest matches the
blob. Any failure is one red finding naming the commits and why. So is a marker that no longer names
an ancestor of the tip, and exporter commits without any marker (a repository the exporter wrote
before the marker existed: recreate it or reseed it). On a live root that is still a work tree, and
while there is no snapshot repository, the finding is absent.

### Board checkpoint layout

The local export in the data directory stays flat. Only the copy a cut stages into `state/board` is
split, so a checkpoint's Git cost follows what changed instead of the size of the board
(secretary-1656):

- `layout.json` marks the split layout (`ummanu.board.checkpoint-layout`, version 2). A directory
  without it is the flat layout every earlier checkpoint used: one file per logical file.
- `cards.ndjson` and `sprints.ndjson` are stored one line per file, `<dir>/<index // 1000>/<index>`,
  in line order. Changing one card rewrites one small blob and the two trees above it; a checkpoint
  over an unchanged board writes no file and so creates no Git object.
- `audit.ndjson` and `events.ndjson` only grow, so they are stored as immutable segments. A
  checkpoint whose log extends the committed one adds one segment holding the appended bytes. A log
  that does not extend the committed one (the first split checkpoint after a flat one, or rewritten
  history) is replaced by a single segment.
- A logical file's bytes are the concatenation of its parts in index order; a gap in the sequence or
  an unexpected entry is a broken checkpoint, not a shorter one.

Every consumer of a committed checkpoint reads it through `board.checkpoint_layout.open_checkpoint_board`,
which understands both layouts and returns the same logical bytes for the same board. The first
checkpoint after the upgrade converts a flat checkpoint in place: it writes the split parts and
removes the flat files in the same commit. Earlier commits keep their flat files; history is never
rewritten.

Memory facts are stored flat in the live root's `state/memory/facts`; the memory writer writes
`commit`/`supersede` there as files, without Git, and the next cut carries them ([Writers](#writers)).
`state/memory/facts` is the only canon for every derived form of memory, so the instance directory
is a required argument on the export and index-rebuild paths: a missing argument fails instead of
pointing the export at another installation's memory.

## Cadence and RPO

- `ummanu-checkpoint.timer` activates `ummanu checkpoint-run` every 60 s, starting 30 s after boot.
  The exporter prepares a cut at most once per five-minute cadence window; a cut whose tree equals
  the tip's makes no commit. The minute divides that window, preserving five-minute steady-state
  cuts with roughly 50-second runs. Freeze and drain do not stop this independent timer.
- A remote push is attempted in its own 30-minute window, fast-forward only. A due push window forces
  one fresh, verified preparation in that checkpoint run before pushing.
- Durable RPO on machine loss is 30 minutes. Commits in the local snapshot repository give
  fine-grained history but do not survive the machine.

The pusher publishes only when the remote tip is an ancestor of the snapshot branch's tip; otherwise
it records the failure or divergence for the next window or operator action.

## Writers

The live root has no Git. Every writer of it writes files only and starts no Git child; the snapshot
exporter is the only code that commits, and only into the snapshot repository:

| Writer | Writes | When | Lock |
|---|---|---|---|
| snapshot exporter (`checkpoint.SnapshotExporter`, picked by `checkpoint.tick_checkpoint_writer`) | the snapshot repository's `main`; reads the live root, never writes it | the checkpoint service, at the [cadence](#cadence-and-rpo) | the checkpoint singleton lock, then the shared writer lock |
| memory writer (`memory_write`, `memory.canon`) | `state/memory` | `memory commit`/`supersede`, the memory pack of `upgrade` | `<data>/memory/.write.lock`, then the shared writer lock |
| knowledge writer (`knowledge_write`) | `state/knowledge` | `knowledge write`, the sprint-close closeout, the dispatcher's research-report transfer | the shared writer lock |
| secret writer (`secret_store`) | `secrets/` | `secret init/set/import/remove`, `secret checkpoint-github set`, every re-encryption of a value (`list` and `materialize` write no store file) | the shared writer lock |
| onboarding (`onboarding`, `provision`, `gate`) | `projects/<id>.yaml`, `adapters/<id>.yaml`; drafts, provision runs and gate receipts in `<data>/onboarding/` | `project add`, `provision-apply`, `gate` | `<data>/locks/onboarding/<id>.lock` |
| config edit | the other config files ([Layout](#layout)) | an operation card, then `ummanu config check` | none |

The shared writer lock is `state_repo.state_repo_lock`, the file `.ummanu-state-writer.lock` beside the
tree. Whoever holds it sees no other writer's half-written file, so a cut never copies half a fact,
document or store transaction.

**Config.** Nothing commits config. A change is an operation card executed in place, checked with
`ummanu config check --instance LIVE_ROOT` (schema validation and the old-name guard, without Git;
[Operations](OPERATIONS.md#changing-installation-config)), and the next exporter window carries it.
Onboarding replaces each stage's files atomically and has no commit step either; a registration
reaches the remote with the next cut.

**No card lands in the live root.** The live root is configuration, not a code project: a card whose
project repository resolves to the live root is refused at admission, naming it
(`dispatch.host`), and a release refuses it again.

**Generated state.** The head-registry pair is not a live-root path: `ummanu upgrade` and `recover`
write `heads.yaml` and `source.yaml` into `<data>/heads/` and make no Git call for them. No writer
maintains `.gitignore`: what leaves the host is decided by the export allowlist
([Local-file exclusion](#local-file-exclusion)).

### The exporter

Per window, under the checkpoint singleton lock and the shared writer lock, the exporter:

1. reads the tip of the snapshot branch, the base of the compare-and-swap below;
2. passes the [validation gate](#validation-gate) and stages `state/board` and `state/runs` from the
   export, the run-history check against the tip's `runs.ndjson`; the board's log segments continue
   the tip's;
3. copies the export allowlist byte for byte from the live root. A symlink or any non-regular file
   at an allowlisted path, or on the way to one, blocks the window by its path; nothing is followed;
4. writes `snapshot-manifest.json`;
5. runs the secret scan over every file of the cut, the manifest included; any hit blocks the window
   by path;
6. builds the tree with Git plumbing through a temporary index (`hash-object`, `update-index`,
   `write-tree`), so the snapshot never has a work tree. A tree equal to the tip's makes no commit
   (`unchanged`). Otherwise `commit-tree` makes one commit, a root commit on an empty repository and
   otherwise a child of the tip only, with the fixed identity `ummanu snapshot exporter
   <snapshot-exporter@ummanu.invalid>` and the subject prefix `snapshot(instance): `
   (`checkpoint.SNAPSHOT_AUTHOR_*`, `SNAPSHOT_SUBJECT_PREFIX`), and `update-ref` moves the branch
   only if it still points at the tip from step 1. The first commit on a tip the exporter did not
   make also creates the [takeover marker](#snapshot-repository) in the same ref transaction.

The cut is rebuilt from scratch every window, so a file deleted from the live root leaves the next
snapshot. The repository is created and initialised bare when absent; a non-empty directory there
that is not a bare repository blocks the window.

`ummanu data snapshot --instance INSTANCE --snapshot-repo PATH [--data-dir DIR] [--state-dir DIR]`
runs one exporter window into an explicit repository. It takes only the writer lock and never uses a
`.git` the live root may still have. It prints the result as JSON, exits 0 on `committed` or
`unchanged`, and never pushes. The stand comparison and the cutover use it.

**Push.** The checkpoint service's `CheckpointPusher` (`checkpoint.tick_checkpoint_pusher`) publishes
`refs/heads/main` of the snapshot repository: the 30-minute window, the fresh preparation a due
window forces, fast-forward only, the `diverged` stop on a remote tip the snapshot history does not
contain, the managed GitHub credential from the live root's secret store, and the shared lock. The
push state and doctor rows read the snapshot repository and name it as `snapshot repository:`.
Before each attempt the pusher sets the snapshot repository's `origin` URL from
`offsite.instance_remote` when it differs, so a changed value re-points the next push. No
`offsite.instance_remote`, no snapshot repository, or no commit yet is a `skipped` push with that
reason.

**Cutover seed.** `ummanu data snapshot --instance LIVE_ROOT --snapshot-repo PATH --seed-from
LEGACY_INSTANCE_DIR` runs no window. It fetches the legacy work tree's checked-out branch tip at
depth 1 into an empty snapshot repository and points `main` at it. Depth 1 is enough because the
remote already holds the legacy history, so a push of the exporter's child needs only the tip. The
legacy history (4.3 GiB) is never copied. An empty remote would refuse a push from this shallow
repository, so a fresh remote is not seeded this way. Rerunning it on a repository already at that
tip is a no-op (`unchanged`). A repository with exporter commits, or one seeded at another tip, is
refused (`blocked`, exit 1). The next window commits with the legacy tip as its parent and writes
the marker. The runbook order is: final legacy checkpoint and push, seed, switch the live root. The
first exporter push is then a fast-forward of the remote branch.

### Git-free writers

**Memory writer.** The canon is the files under `state/memory/facts` (and the pack ledgers under
`state/memory/packs`) of the live root. `memory commit`, `memory supersede` and the memory pack
write it under the memory lock and the shared writer lock. Each write first records the prior state
of every path it is about to replace or remove in the **undo area** `<data>/memory/.undo` (a copy of
the old bytes and mode, or "absent"), then replaces each file atomically or removes it, then retires
the undo area with one rename. A failure inside the write restores exactly the recorded set, so the
canon is byte-identical to before; an undo area a crashed writer left behind is restored by the
next writer (or `data export-memory`) under the same locks before it proceeds. The undo area is
outside `state/memory`, so it is never exported; `memory verify` reports one that is left behind.
Where a commit id used to be, the writer result (`commit`), the export manifest (`source.head`,
`journal.commit`) and the pack result carry the **content revision**: `sha256:` over the sorted fact
ids, each with the sha256 of its file's bytes (`memory.canon.content_revision`). The same canon gives
the same revision, and any changed byte changes it. `memory verify` compares the canon,
`export.ndjson` and `index.sqlite` by fact id set and per-fact content hash and names every missing,
extra or changed id, not their counts. The memory service reads the canon files when no export is
present.

**Knowledge writer.** `knowledge write --file` replaces one document under `state/knowledge` with one
atomic rename; `--dir` swaps one directory in whole through `state/.knowledge-swap` (outside the
allowlist). A failure at any step puts the previous directory back and removes any parent the write
created, so `state/knowledge` is byte-identical to before; a swap a crashed writer left behind is
finished by the next writer under the lock. Content equal to what is on disk writes nothing. Where a
commit id used to be, the result (`commit`), the sprint-close closeout step and the plan's
`mark_written` carry the **content revision** of what was written: `sha256:` over the sorted paths
below `state/knowledge`, each with the sha256 of its bytes (`_fsutil.content_revision`, the memory
writer's formula). The same content gives the same revision. A close whose plan was staged with a Git
commit id before this change keeps that value and completes.

**Secret writer.** Every store write holds the shared lock and runs as one undo-guarded transaction
over `secrets/` (the memory canon's transaction, its undo area in `secrets/.undo`): the prior bytes
and mode of each path are kept before it is replaced or removed, a failure restores exactly that set
and any directory the write created, so `secrets/` is byte-identical to before, and an undo a crashed
writer left is restored by the next store operation before it reads the catalog. The catalog and the
envelopes it names therefore never diverge. An envelope whose plaintext is unchanged is never
rewritten or re-encrypted, and a write whose catalog entry and value are both unchanged writes
nothing. Results carry the store's content revision in `commit`: the same formula over the exported
store files (`secrets/catalog.yaml`, `secrets/installation-key.json`, `secrets/values/*.enc.json`;
`secret_store.store_revision`), never over `installation.key`.

### Before the cutover

A live root that still has `.git` (an installation not cut over yet, red `live_root.git_work_tree`)
keeps the legacy writer: `checkpoint.CheckpointWriter` commits `state/board` and `state/runs` into the
work tree and, because the Git-free writers no longer commit, stages their files in the same commit
(`checkpoint.LEGACY_LIVE_PATHS`: `state/memory`, `state/knowledge` and the three exported secret-store
paths); the pusher publishes the work tree's branch. The dispatcher once also landed card branches
in that work tree (the instance-repository landing, retired in ummanu-32); that path is gone. A reader
of history from before the cutover finds these commits without a `snapshot-manifest.json`, and the
config commits operators and landed cards made beside them.

### Local-file exclusion

A local file that must never leave the host (`secrets/installation.key`, `runtime.env`,
`board-store.env`) is excluded by **not matching the export allowlist**, not by Git ignoring it. One
helper answers the question for every caller, `infra.export_allowlist.is_exported` (re-exported as
`checkpoint.is_exported`, beside `SNAPSHOT_ALLOWLIST`); it reads no file and starts no process, so it
answers the same on a live root with or without `.git`:

- the secret store refuses to initialise or write when `secrets/installation.key` would be exported,
  and `secret materialize` refuses a target inside the live root that would be;
- `runtime_env.read_runtime_env(..., require_ignored=True)` refuses a `runtime.env` inside the live
  root at an exported path and accepts one that is not (a file outside the live root is never
  exported);
- the board store refuses to materialise, resolve or migrate a `board-store.env` at an exported path,
  and `doctor` names it.

`.gitignore` is not exported, and no product code writes it.

## Checkpoint readers and freshness

`state/board` and `state/runs` are recovery and offline-analytics artifacts, not a live read model.
Their in-product readers are `installation.materialize_checkpoint` and the recovery identity during
recovery, `bootstrap` (checkpoint swimlanes) and `board.analytics.project_analytics_checkpoint`, which
first verifies the sealed manifest. All of them read the board through the one checkpoint reader
(see [Board checkpoint layout](#board-checkpoint-layout)); `restore.py` reads the materialised local
export, not the checkpoint. `status`
and `doctor` read Git and the dispatcher's production-state telemetry for freshness only.

Live card, sprint, audit and command reads use the selected backend through `TaskReader`, `TaskAudit`
and the web read layer. Dispatcher lifecycle state comes from `dispatcher/production-state.json` and
live run journals. Backend audit ownership is in [Board store](BOARD_STORE.md) §7.3.

## Local Git packing controls

These controls belong to a live root that is still a Git work tree, an installation before the
cutover. There, install, recover and upgrade idempotently set, with `git -C INSTANCE config --local
--replace-all`, only these settings in that repository (never global config, never a project repo):

```
pack.threads=1
pack.windowMemory=128m
pack.deltaCacheSize=64m
gc.auto=0
maintenance.auto=false
```

`doctor` names missing, drifted or duplicate values with the exact remediation. To roll back, run
`git -C INSTANCE config --local --unset-all` for each key. These settings constrain packing; they are
not a hard memory limit. On a plain live root `upgrade`'s `instance-packing` step reports `skipped`
and doctor checks nothing; the bare snapshot repository gets none of these settings.

`gc.auto=0` and `maintenance.auto=false` stop every `git commit` from starting Git's implicit
`gc --auto`, which would otherwise pack the repository inside whichever checkpoint run first crosses
the loose-object threshold. Packing runs from `ummanu-instance-maintenance.timer` instead (daily,
`Persistent=true`): its service runs `ummanu instance-maintenance`, which is `git gc --auto` with
Git's stock thresholds (6,700 loose objects, 50 packs) restated on the command line, so a quiet day
costs one object count. That packing step takes no state-repo lock: it touches objects only, and the
two ref-writing parts of `gc` (`pack-refs`, reflog expiry) are switched off for it. Reflogs are then
expired as a separate step under the state-repo lock, the only moment a checkpoint can wait on
maintenance, bounded at 60 seconds. `ummanu status` lists the timer under `host.schedules` with
`last_trigger`, and the service under `host.units` reads `failed` after a failed run; the run's
before/after object counts are in its journal. The command packs the live root's repository while
the live root is a work tree; on a plain live root it packs the exporter's bare snapshot repository
(`offsite.snapshot_repo`) instead, with the same overrides and the reflog step under the live root's
state-repo lock (`infra.instance_maintenance.run`), and reports which one under `repository`. Before
the exporter's first cut there is no snapshot repository and the step reports `skipped`. A live root
that is neither a work tree nor a valid `instance.yaml` exits 1 with `instance repo is not a git
repository` before the Docker cleanup below runs.

The same maintenance command also inventories only containers carrying `ummanu.test-board`
with a valid owner PID. It removes one by full ID only after two label checks and two definitive
dead-owner checks; a production marker protects the container. Docker's anonymous-only
`volume prune` requires an effective client API of 1.42 or newer and leaves named or in-use volumes
alone. The native `builder prune --all` keeps cache used within the last seven days and cache still
in use; no image-prune command runs. Docker calls pin the local `/var/run/docker.sock` endpoint and
ignore inherited API overrides. Its JSON journal result contains bounded counts, retained reasons, reclaimed cache
space and failure findings, without raw Docker output. A Docker failure makes the service fail and
does not widen any subsequent deletion scope.

The periodic owner atomically replaces `<data>/dispatcher/checkpoint-state.json` with its write and
push records under `<data>/dispatcher/checkpoint.lock`. Status, doctor and the first independent run
read the legacy production-state records only while this file is absent. Once present, even a
malformed or unreadable new file cannot revive legacy success. The production tick takes neither
checkpoint work nor its failures into its own outcome.

The board cut is one read-only REPEATABLE READ transaction over cards, Product/Issue rows, batched
metadata and comments, request/audit history and sprints on the same client. `board-bulk.lock` is
shared only for these reads, preventing overlap with bulk recovery; normal transactional board
writers can commit while the cut sees its earlier snapshot. Dispatcher state and run JSON files can
be replaced while exported: a read sees one complete version through atomic rename, and append-only
run history is checked against the preceding checkpoint. The cut need not coincide with a whole tick
boundary. Recovery treats run/claim state as inert bookkeeping and reconciles it against the coherent
board projection, so a completed board transaction or complete state-file version remains a valid
recovery point. The instance writer lock protects the live-root memory, knowledge and secret files
through publication and commit. Config edits remain operator-owned and must finish as complete
files before their next checkpoint window.

## Validation gate

Before each cut the state passes a fail-closed check. If any item fails, the checkpoint service
records the blocking reason in status and retries on its next timer activation:

- task audit is settled with no pending board mutation. The audit is the one the card client names
  (`tasks.task_audit_for`): staged `requests` rows. If the card client cannot be established the
  checkpoint is blocked by name, never checked against a file journal instead;
- `cards.ndjson` and `sprints.ndjson` are regenerated from the live backend, both counters in
  `export.json` match the line counts, the generated `cards.json`/`cards.ndjson` pair is identical and
  card references are unique, all before local export or canonical files are replaced;
- memory staging is empty;
- the secret scan is clean over every file of the cut, the manifest included (on a live root not yet
  cut over: over `state/` and every file the legacy writer commits beside board and runs). The memory
  and knowledge writers run the same scan over their own text before writing.

### Analytics checkpoint seal v2

`analytics-manifest.json` is `ummanu.board.analytics-checkpoint` version 2, the boundary for
offline analytics projection. Its object has exactly `schema`, `version`, `checkpoint_id` and `files`.
`files` has exactly one entry each for the logical `events.ndjson`, `cards.ndjson`, `sprints.ndjson`,
`audit.ndjson` and `export.json`, each with path, lowercase SHA-256 and byte count of the logical
bytes, whichever layout stores them; NDJSON entries
also record their non-blank line count. `checkpoint_id` is the SHA-256 of the canonical entries.
Version 1 seals (without `audit.ndjson`) stay readable; new exports are sealed as version 2.
`export.json` is a count summary, never proof of the cut.

The writer validates all five files flat in staging (synthesising an empty `events.ndjson` when
there are no events), validates the staged manifest, removes the prior manifest, writes the changed
split parts and renames the new manifest last. A copy taken mid-write has either no manifest or a
complete matching cut.

`verify_analytics_checkpoint(directory)` is read-only and reads only that directory. It rejects
unknown schemas, missing or extra files, duplicate entries, malformed metadata, digest or count
mismatches and stale summaries. A projection must call it before parsing rows. Unsealed checkpoints
remain valid recovery input but not analytics input; seals are not backfilled.

## Failure and divergence

A push failure (network, forge or auth) is fail-closed on the checkpoint, not on work: local commits
continue, the dispatcher keeps running, lag is visible in `status` and `doctor`, and the next window
retries.

On remote divergence (remote commits not present locally) the push stops and `status` raises
`remote diverged`. Force-push and history rewriting are forbidden; the operator resolves it
([Operations](OPERATIONS.md#checkpoint-push)).

## Secrets

The host `runtime.env` is mode `0600`, outside the export allowlist and the checkpoint, and may hold materialised
installation secrets. `board-store.env` is local connection material that bootstrap generates,
not restored from the secret store. Forge access and interactive head logins stay in the operator's password manager; the product
never copies them to the host.

The secret store (`ummanu/secret_store.py`, `secrets/` in the live root) is recoverable canon,
exported in every cut and pushed with it:

- `secrets/values/<id>.enc.json`: one versioned encrypted envelope per secret;
- `secrets/catalog.yaml`: plain metadata (id, scope, purpose, materialisation target). It is plain
  on purpose: a recovery without the phrase still lists what stayed locked or missing;
- `secrets/installation-key.json`: the public KDF parameters and a verifier, no key material.

The snapshot never contains the raw installation key (`secrets/installation.key`, `0600`, not
matched by the export allowlist, so it exists only on its host) or the recovery phrase, which
`secret init` shows once and the product stores nowhere. Documentation never reads or repeats a
secret's value. With the phrase the key is rebuilt and values return byte for byte; without it `recover`
prints a locked/missing report and writes nothing. Losing the phrase means reissuing secrets, not
losing the installation. Command contracts are in [Protocols](PROTOCOLS.md#secrets).

Security boundary: a trusted single-user host. Board and memory endpoints listen on loopback. External
tokens are protected by host access control, the export allowlist (a credential file is excluded by
not matching it, [Local-file exclusion](#local-file-exclusion)) and the checkpoint secret scan, not by
at-rest encryption on the host. The installation key belongs to the installation user; any process
that can read `runtime.env` can read the key and open every secret. There is no broker, grant or
per-worker isolation.

The checkpoint scan distinguishes configuration from credentials. It reads values whose runtime
variable names identify credentials (`*_TOKEN`, `*_PAT`, `*_IDENTITY`, `*_KEY`, `*_SECRET`, passwords,
credentials, auth, webhooks) and URLs with embedded userinfo. With the installation key present it also
reads catalog values under sensitive names or values matching a credential shape. `UMMANU_DATA_DIR`,
`UMMANU_REPO` or a board URL without userinfo are not secrets. A locked or incomplete store is a
`doctor` finding but does not halt checkpointing; runtime-file and pattern scans still run. Protocol
text is redacted by the same credential-specific redactor before it reaches the board or audit; known
token and webhook formats are a second fail-closed scan layer.

### GitHub checkpoint credential

The HTTPS `github.com` checkpoint remote and the dispatcher's registered-project GitHub HTTPS
operations use one managed credential, the encrypted `github.checkpoint-token` set by
`secret checkpoint-github set --stdin` (or `--file`). Setup, rotation, doctor rows and card preflight
are in [Operations](OPERATIONS.md#checkpoint-and-project-github-access). For recovery:

- A clean host cannot read the encrypted store before cloning it. Supply one external bootstrap
  credential with `--bootstrap-credential-file TOKEN_FILE` (mode `0600`, owned by the `sudo` caller or
  the effective user) or `--bootstrap-credential-stdin` (cannot share stdin with the recovery phrase).
- Ummanu copies it into a mode-`0600` operation-scoped capability owned by the installation-user
  Git child and removes it on success or failure. It is not retained and is not an ongoing checkpoint
  source.
- A rerun fetches into the existing snapshot repository (or legacy checkout) with the supplied
  bootstrap credential, or else the unlocked managed credential. With neither, recovery stops before
  contacting the remote. Ambient Git helpers are never used for `github.com` HTTPS; other HTTPS hosts
  are refused; local/file remotes are plain Git; SSH is explicit manual bypass.
- Under `sudo`, credential readiness is evaluated by the installation-user Git child, not by root.

For a pre-recovery inventory read `status.recovery` or text `doctor`. Rows come from the installed
registry and carry probe provenance and age; online doctor reuses a fresh dispatcher verdict or runs a
read-only probe, offline doctor reports `stale` or `unknown`. `probe_broken` (the probe itself failed)
is distinct from provider states `unauthenticated`, `exhausted`, `unavailable`. Credential consumers,
last checkpoint operation, path configuration, secret materialisation and manual Git bypasses are
separate rows; remediate each with its `supported_next_action`. An old push result proves nothing
about current credential health.

## Observability

`status` and `doctor` show checkpoint freshness (of the snapshot repository in exporter mode, named on
its own row): time and hash of the last commit, last successful
preparation, a not-yet-due skip, retry state, last failed preparation and reason, last successful
push, last operation attempt and its age, lag in minutes and commits, and `remote diverged`. A skip is
never reported as a fresh preparation. The attempt timestamp does not replace the last successful
push timestamp.

A blocked checkpoint records its failure independently of production tick telemetry; the checkpoint
service retries on the next timer activation. If the push window was due, the push is recorded as
withheld instead of sending an older snapshot. Doctor reports missing or failed checkpoint units as
`checkpoint.unit_unhealthy` and cuts older than 15 minutes as `checkpoint.cut_lag_exceeded`, both red.
The existing remote RPO finding remains `checkpoint.rpo_exceeded`.

Past the 30-minute RPO, `doctor` and the dashboard lamp report a red `checkpoint.rpo_exceeded`
finding that names what stopped publication. If preparation is failing, that is the gate's reason
and the time it began failing (`failing_since_at`); otherwise it is the push failure. A blocked gate
commits nothing, so the unpushed lag stays at zero while it is blocked. For that case the exposure
is counted from the last successful preparation. On PostgreSQL the gate first settles audit rows left
staged by a dead writer (`docs/BOARD_STORE.md` §3.9). A stale row therefore blocks for the grace
period plus one checkpoint activation, not until an operator repairs it.

`status --json` has a `secret_store` section: initialised or not, secret count, last catalog change,
whether a usable installation key exists, and a materialisation-target summary, with no value, key or
phrase. `doctor` raises a finding when catalog and values diverge, when the key is missing or unusable
while the catalog is non-empty, or when key permissions are wider than `0600`. A healthy store and an
absent store both produce no findings.

## Backend-aware cold archives

The Git checkpoint is the only recovery contract. `backup create`/`backup verify` are a manual cold
archive for raw material, with no timer, offsite transfer or `doctor` gate. Commands are in
[Operations](OPERATIONS.md#optional-cold-archive).

Every archive carries the normalized Product, Issue, Task and Sprint views, comments, request/audit
history, inert run/claim state and the PO service's input queue `po-queue/` (pending inputs and the
set-aside ones under `refused/`, which a restore puts back). A `core` archive holds only that
engine-independent set. A `full` archive is version 2 and adds
`engine/postgres.dump`, a custom-format data-only dump made and listed by the pinned `postgres:16`
client; its manifest records source Alembic head, server/client version, table counts and purpose. The
dump covers every board table, the owner events (`owner_events`) and the PO sessions, turns and feed
included, so `restore-postgres` brings them back. The
dump is taken after the pipeline pause, and its table counts are taken inside the same exported
snapshot the dump reads, so rows the pause itself writes are in both or neither. `backup verify`
checks the manifest's counts are well formed but does not decode the dump; `restore-postgres` is the
check that the restored rows match them. No
archive carries a database password, role secret, `board-store.env` or the memory model cache
`memory/fastembed-cache` (the index rebuild downloads the model again).

`ummanu restore-postgres ARCHIVE --instance TARGET` restores only
into a distinct disposable target whose `board-store.env`, container, database and owner/app/read roles
are managed by the board-store lifecycle. It verifies archive identity and checksums, refuses the
source endpoint, migrates the target to the recorded head, verifies roles, requires every application
table empty, restores in one `pg_restore` transaction as owner, and compares table counts and
normalized cards/sprints. A same-archive retry verifies the marker and parity and restores nothing. It
never starts a dispatcher, worker, reviewer or observer. An error leaves the archive and source
database untouched; repair or recreate only the target before retrying.

## Fresh install and recovery

A clean host is recovered with two commands, `bootstrap` then `recover`, whatever the remote's
[shape](#two-remote-shapes). The code is `bootstrap.bootstrap` and `installation.install` (`recover`
is `install` with `--recover`).

**Prepare the product first.** These commands assume Ubuntu 24.04 and a product checkout owned by
`USER`, the dedicated installation account. Create that account and checkout first if they do not
exist; bootstrap reuses the account. Keep the checkout and its `.venv` readable and executable by
`USER`, and create the environment as that user. Install the OS prerequisite with an administrator's
account, then run the environment setup from the product checkout:

```bash
sudo apt-get update
sudo apt-get install --yes python3-venv
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[memory,dev]'
ummanu_product_root=$(pwd -P)
```

The editable install preserves checkout-owned deployment assets. The memory extra supplies the
embedding runtime; dev supplies the pinned linter used by upgrade and project checks. The shipped
units always use `PRODUCT_ROOT/.venv/bin/…`. Root commands therefore use an absolute CLI path and
recovery names its product root explicitly; neither depends on `sudo` preserving PATH or HOME:

```bash
sudo "$ummanu_product_root/.venv/bin/ummanu" bootstrap \
  --instance-remote REMOTE --instance-dir INSTANCE --installation-user USER \
  --bootstrap-credential-file TOKEN_FILE
sudo "$ummanu_product_root/.venv/bin/ummanu" recover \
  --instance-remote REMOTE --instance-dir INSTANCE --installation-user USER \
  --product-root "$ummanu_product_root" \
  --bootstrap-credential-file TOKEN_FILE --recovery-phrase-file PHRASE_FILE
```

Both commands clone a private remote, so both take the same external
[bootstrap credential](#github-checkpoint-credential) (`--bootstrap-credential-file` or
`--bootstrap-credential-stdin`), checked the same way before the clone, with its operation copy
removed on success or failure; a local/file remote needs none.

`INSTANCE` is the live root to create, by default `~/ummanu-data/instance`. Heads run on local-pty,
which ships with the product; no session manager is installed. A new installation runs `install`
through the same absolute CLI path and with the same arguments instead of `recover` ([Guards](#bootstrap)).

**Memory configuration.** `host.memory_model`, `host.memory_dim` and `host.memory_threads` choose the
model, dimension and inference threads together. Their defaults are `intfloat/multilingual-e5-large`,
`1024` and `1`; a custom model must name its matching dimension. Both recovery paths, including
recovery with locked credentials, use these settings, as do explicit reindex and the rendered memory
service. A retry reuses only an index whose metadata matches the model and dimension. A missing,
legacy, corrupt or incompatible index is rebuilt; completed board recovery is retained.

The legacy `host.memory_reindex_python` and `host.memory_reindex_script` fields apply only to
explicit `ummanu memory reindex --instance INSTANCE`. With neither set, that command uses the
product indexer. Setting either requires both an executable Python and an existing script; a lone
override is refused. Install and recover ignore the pair and use the product indexer. The external
script's arguments and environment are described in [Operations](OPERATIONS.md#system-requirements).

Legacy `instance.yaml` `heads` arrays remain readable and are ignored. Configure actual profiles in
`heads/heads.toml`; existing managed head units retain their ownership until explicitly resolved
([Operations](OPERATIONS.md#the-installations-head-registry)).

The legacy `persona.name` and `persona.style` fields also remain readable and ignored. Personal
instructions come from `persona/AGENTS.md` in the live root. `host.orca_repos` is retained only for
compatibility and does not create, check or remove Orca registrations.

**Memory.** A host needs at least **4 GB of RAM** (8 GB recommended; production runs 8 GB with 6 GB
of swap). The embedding model is about 1.5-1.7 GB resident in the one process that holds it. Recover
builds the memory index in a short-lived child process, which exits with the model before the host
step starts `ummanu-memory-mcp`, so the model is resident in one process at a time, never in recover
and the memory service together (re-drill 3 on a 4 GB host without swap was OOM-killed when it was).
On a host near the minimum, keep 2-4 GB of swap for the board store, the web and the heads.

### Bootstrap

`sudo ummanu bootstrap` runs as root on Ubuntu 24.04 only (a `--dry-run` excepted) and is safe to
rerun. In order:

1. checks the bootstrap credential, when one is given, before anything else changes, and ensures
   `--installation-user`, reusing an existing one;
2. runs recovery's own clone step: an exporter snapshot is laid out as the plain live root and the
   snapshot repository with its takeover marker, exactly as `recover` lays them out
   ([Snapshot recovery](#snapshot-recovery) steps 1-7; the data directory is laid out when the
   repository or the live root is in it); a legacy checkpoint is cloned as a Git checkout
   ([Checkout](#checkout));
3. writes the stamp `.ummanu-bootstrap` into the live root. The allowlist does not match it, so it is
   host-local; in a legacy checkout it and `/runtime.env` are also added to `.git/info/exclude`;
4. installs Docker and Compose v2 from the distribution when absent. When the instance enables the
   web-front component (`installation.web_front_wanted`: a `host.unit_prefix`,
   `host.components.web-front` not disabled, the unit not in `host.foreign_units`), it also installs
   the distribution's `caddy`, which `ummanu-web-front.service` runs as `/usr/bin/caddy`. It masks
   `caddy.service` before the package exists, so the package never starts an unconfigured listener
   and the front unit is the only Caddy that runs;
5. provisions, migrates and role-verifies the PostgreSQL board store, with no recovery phrase or
   manual board credentials; `board-store.env` (0600) is written into the live root;
6. hands the live root, and for a snapshot the data directory, to `--installation-user`.

**Guards.** `install` and `recover` install no runtime. Before any live write they check that the
board store is reachable and, with the web front enabled, refuse naming caddy when `/usr/bin/caddy` is
absent (without that check the front crash-looped with 203/EXEC and recovery failed only at
materializer verify), and refuse naming `host.web_front.sites` and `ummanu web-front render` when no
sites are configured (`installation.check_prerequisites`). A fresh `install` refuses an existing
installation user or checkout and names the choice: `--recover` for the same installation, or the
separate adopt workflow for a live host. The first `install` of a bootstrapped target is the one
exception: it runs the [sequence](#sequence), removes `.ummanu-bootstrap` at the end, and after that
`install` refuses the target and names `--recover`.

Before host materialization starts, both commands check the chosen checkout's `.venv` entry points
and editable import provenance. A missing interpreter or entry point, snapshot install or environment
pointing at another checkout is refused with the setup instructions; no host materializer step runs.

**Web-front sites.** The front's Caddyfile (`<data>/webfront/Caddyfile`) holds the password hash, so
it is data-directory state that no snapshot carries. What survives is instance config: the https
addresses the front answers on are `host.web_front.sites` in `instance.yaml`
([Operations](OPERATIONS.md#web-front-sites)). The materializer's `web-front-config` step
(`upgrade.step_web_front_config`) renders the file from that list and the secret store's hash and
session secret, through `ummanu web-front render` run as the key's owner, before the host step
enables `ummanu-web-front.service` (whose `ExecStartPre` is `caddy validate` on that file).

### Two remote shapes

`recover` and `bootstrap` read the remote tip before they decide how to clone, whenever the
`--instance-dir` target is not a Git work tree (absent, empty, or a live root an earlier snapshot
recovery or bootstrap laid out); the first `install` of a bootstrapped live root reads it too
(`installation._reads_remote_shape`). It is one decision and one clone step for all three commands.
It clones the default branch depth 1, bare, into a private sibling staging directory and looks for
`snapshot-manifest.json` at the root of the tip's tree:

- **With a manifest** the tip is an exporter snapshot, and recovery takes the
  [snapshot path](#snapshot-recovery) (`installation._snapshot_checkout`).
- **Without one** the tip is a legacy checkpoint, a commit of the instance repository's work tree from
  before the cutover. The staging is removed and recovery takes the [checkout path](#checkout). That
  path stays for every checkpoint without a manifest.

A target that is already a Git work tree, a fresh `install` and a `--dry-run` against an absent
target do not read the shape and take the checkout path.

What `bootstrap` leaves and what `recover` then does, per shape:

| | Snapshot remote (tip with `snapshot-manifest.json`) | Legacy remote (tip without one) |
| --- | --- | --- |
| `bootstrap` | the plain live root with no `.git`, the snapshot repository at the tip with its marker, the data directory laid out, the stamp | a shallow Git checkout as the live root, the stamp |
| `recover` | finds the live root of the same tip (the stamp and `board-store.env` are host-local, not a divergence) and the repository at the tip, so nothing is cloned again; runs the [sequence](#sequence) on the extracted tree | fetches and fast-forwards the checkout; runs the [sequence](#sequence) on it |
| first checkpoint run | the live root is not a work tree, so the exporter commits on top of the recovered tip and the pusher publishes it | the live root is a work tree: the legacy writer, with both `live_root.*` findings red until the cutover |

A rerun of either command is idempotent: the same tip, no second board import, the store credentials
kept.

### Snapshot recovery

For a remote whose tip is an exporter snapshot, the clone step does this, in this order, and writes
nothing outside its staging until the checks have passed:

1. **Bare clone.** The staging clone is validated before anything is adopted: a bare repository,
   `origin` the given remote, the default branch resolvable and equal to `HEAD`, the history shallow.
   The branch must be `main`, the one the exporter publishes.
2. **Manifest check.** The manifest must be format `ummanu.instance-snapshot`, version 1, with a map
   of path to sha256, `board_schema_head` and `product_revision`; a malformed manifest or another
   version is refused by name. Every blob of the tree is extracted into the staging and hashed on
   the way: every file except the manifest must be listed with a matching digest, and nothing may be
   listed that the tree lacks. The manifest's `board_schema_head` must be in this product's migration
   lineage; a newer (or unknown) head is refused naming both heads.
3. **Layout.** The extracted `instance.yaml` names the data directory (a relative value is rooted
   at the live root) and `offsite.snapshot_repo`, resolved as the exporter resolves it, by default
   `<data>/backup/instance.git` (`config.recovered_instance_locations`). One layout check runs before
   anything is written (`installation._snapshot_layout`). It accepts exactly two shapes, and the
   snapshot repository lies outside the live root in both:
   - (a) the live root and the data directory are disjoint, neither containing the other;
   - (b) the live root is a direct child of the data directory, `<data>/<name>` (the default
     `~/ummanu-data/instance`, or `data_dir: ..`).
   Every other layout is refused with a message naming the paths. A non-empty data directory
   ummanu did not lay out is refused too. In shape (b), only the live root entry itself and the
   staging recovery created beside it are not counted as data-directory contents.
4. **Empty or same tip.** The `--instance-dir` must be absent, empty, or already the live root of
   this same tip: every exported path the tree's, byte for byte and with its executable bit, and no
   exported path extra. A file the allowlist does not match (`secrets/installation.key`,
   `runtime.env`, `board-store.env`, a bootstrap stamp) is the host's own and does not count. Any
   other non-empty live root is refused and nothing in it is overwritten.
5. **Snapshot repository.** The staging clone becomes the snapshot repository. An existing
   repository there is reused only if its tip is the remote tip or an ancestor of it (it is fetched
   and fast-forwarded); anything else is refused. `origin` is set to `offsite.instance_remote`, as
   the pusher expects.
6. **Takeover marker.** `refs/ummanu/snapshot-base` is set to the recovered tip, so doctor's
   `snapshot.foreign_commit` walk covers only commits made after the recovery.
7. **Live root.** Exactly the tree's paths the export allowlist matches are laid out in staging, with
   their bytes and modes, handed to `--installation-user` and moved into place atomically. The live
   root has no `.git`, no `state/board`, no `state/runs` and no manifest. Host-local files come only
   from bootstrap, the recovery phrase and the secret store, never from the tree.

The [sequence](#sequence) then runs on the plain live root. `state/board` and `state/runs` are read
from the extracted tree, not from the live root; the recovery identity hashes them from there and the
memory facts from the live root. `ummanu upgrade`'s `instance-packing` step skips a live root that is
not a work tree.

### Checkout

The legacy path. The clone takes only the current default-branch checkpoint: depth 1, one branch, no
tags. Git clones into a private sibling staging directory; recovery verifies origin, branch,
upstream, exact tip and shallow boundary, then atomically adopts the requested path. A timeout or
interruption kills the clone's whole process group, removes the credential capability and staging
directory, and leaves an absent or empty target as it was.

Later recovery of that checkout fetches the tracked branch without tags and merges `@{u}` with
`--ff-only`; the checkout stays shallow. An unchanged tip is a no-op. Recovery never shallows, resets
or replaces an existing checkout and never unshallows one.

A non-empty target that is not a valid instance repository is refused, not overwritten. Inspect and
preserve it, then remove it outside Ummanu or choose a fresh `--instance-dir`. A dirty checkout,
different origin, invalid repository or unsupported non-fast-forward is also left untouched and
refused. A clean tree alone never proves product ownership. An untracked file the allowlist does not
match (`secrets/installation.key`, `runtime.env`, `board-store.env`) is host-local and does not count
as a local change of the checkout; every other change, tracked or untracked, does.

A recovered checkout is a live root in the old shape; it moves to a plain live root with the same
cutover as any legacy installation ([Names on the host](#names-on-the-host)).

### Sequence

After the clone step, `recover` (`installation.install`) runs one sequence:

1. **Phrase.** Opens the secret store, if present, before reading `runtime.env`. With
   `--recovery-phrase-file`, `--recovery-phrase-stdin`, or a TTY prompt when the key is not on disk,
   it rebuilds the installation key and materialises values into the files the catalog names,
   `runtime.env` and every file target, one in the data directory included
   (`<data>/webfront/owner-password.env`). Before it writes, a file target already present in a data
   directory ummanu has not laid out (no data manifest) is refused by path and no secret is written;
   in a laid-out one it is refreshed. Without the phrase it writes nothing, reports locked/missing,
   and `runtime.env` stays as it is.
2. **Ownership barrier.** The live root, secrets, locks and declared data root are handed to
   `--installation-user` before that user's Git or remote child can consume a restored key. A
   present key must be a regular non-symlink mode-`0600` file owned by that user.
3. **Prerequisites.** Board reachability and, with the web front enabled, `/usr/bin/caddy` and
   `host.web_front.sites` ([Guards](#bootstrap)).
4. **Checkpoint.** Materialises `state/board` and `state/runs` (from the extracted snapshot tree, or
   from the checkout) into a new local data plane, builds derived JSON from the NDJSON and verifies
   counters before any live write (`installation.materialize_checkpoint`). The data target must be
   empty or laid out by ummanu: the files phase 1 created there, at paths absent before it ran, are
   this run's own and do not count, and every other entry of a data target ummanu did not lay out (a
   foreign file beside or inside one of them included) is refused by name. A legacy remote lays the
   data directory out here; a snapshot remote's clone step laid it out already.
5. **Head registry.** Generates the installed head snapshot and source pin into `<data>/heads/` with
   upgrade's own head-registry step, from the canon (the live root's `heads/heads.toml`, else the
   product default; `installation.materialize_head_registry`). The board import needs it: it
   validates every open sprint's observer head against this pair, and a clean host has none until
   this phase. It is idempotent and runs on every retry.
6. **Board.** Imports the board in foreign-key order, with card and sprint parity
   (`restore.import_normalized_board`, [Board import](#board-import)).
7. **Memory.** Rebuilds the memory index from `state/memory/facts` in a child process that takes the
   embedding model with it when it exits (`restore.rebuild_memory_index(..., isolated=True)`; one
   killed by the kernel is reported by its signal). Recover then publishes the memory export
   (`<data>/memory/export.ndjson`, `export.json`, `manifest.json`) from the same facts and hands it to
   `--installation-user` (`installation._publish_recovered_memory_export`), so `ummanu memory verify`
   is `ok` right after recovery. The pack step later finds the restored ledger current and writes
   nothing, so this is the export's only writer on a recovered host. A retry past a completed rebuild
   keeps the index and still publishes an export that is missing.
8. **Projects.** Attempts every missing project checkout from the registry through the same
   remote-execution boundary as the instance remote, and creates the non-secret managed runtime-home
   files for agent CLIs. A binding with `enabled: false` (retired, or not yet onboarded) is not
   cloned: its row has outcome `disabled` and does not count as unavailable. A parent directory
   recovery creates for a checkout (`~/projects`) is handed to `--installation-user`. Provider
   authentication stays manual.
9. **Materializer up to the host step** (`installation.materialize_host`, the `upgrade.STEPS`
   before `host`). Its head-registry step finds the pair phase 5 wrote current (the pair is
   generated, never read from the remote); `instance-packing` is skipped on a plain live root. It
   synchronises role skills and recreates role worktrees (owned by `--installation-user` under
   `sudo`, the skill roots and the directories above them in the home or data directory included). A
   role worktree still registered in the product's Git whose directory is gone (a lost workspace
   root, a host rebuilt from a backup) is added again over that registration; a locked one is
   refused, and the step reports git's `fatal:` line. Its **web-front config** step renders
   `<data>/webfront/Caddyfile` from `host.web_front.sites`; a file already current is not rewritten.
10. **Pipeline journal.** Rebuilds the pipeline worktree's live run journal from the checkpoint
    (`installation.materialize_pipeline_state`), before any dispatcher unit is installed or started.
11. **Host.** Applies host units, starts the memory service, the PO and the web, and verifies
    restore status. Dispatch refuses an unavailable binding before starting its worker, reviewer or
    project worktree. Observers use the dedicated observer repository and are unaffected by
    unavailable reserved projects. Heads are connected afterwards as a separate step.
12. **Ownership again.** Re-enters the ownership barrier on every partial or successful exit, handing
    root-created locks, recovery progress and restored dispatcher run-state to the installation
    user. A cleanup error is reported separately and does not replace an earlier failure.

`<data>/heads/source.yaml` is provenance for the installed heads snapshot and supports the read-only
host-packaging lookup. The product root to materialise comes from `--product-root` or the
configured/default root, not from the pin. Every reader of the pair (the dispatcher catalog,
`task --codex-mode`, the PO runner, web sprint reads, status, doctor and upgrade) reads
`<data>/heads/` only; a live root's own `heads/heads.yaml` is never consulted. A missing
`<data>/heads/heads.yaml` is an error naming `ummanu upgrade`, which (like `recover`) generates the
pair.

`ummanu recover --dry-run` checks checkout, credentials, runtime prerequisites and checkpoint
integrity and prints steps as `would-change`. It writes no data plane, does not touch the board and
runs neither the memory reindex nor the host materialiser.

### Board import

Card and sprint parity are checked separately and both fail closed: a mismatch leaves recovery
unfinished and visible in `doctor`. A live backend holding a sprint entity absent from the export
stops the restore.

The import restores sprint entities whole (goal, Definition of Done, repositories, product, issues,
reservations, status, budget, current card, resume, entries, source audit metadata). A restored entity
is a new board row: its own dates describe the restore; source dates are in its audit metadata.
Recovery does not apply sprint-opening validation. Parity compares whether `product`, `issues` and
reservations are present, not only their values; gaining an empty field the export lacked fails parity.
A checkpoint without a sprints file restores as an installation without sprints.

**Write order.** The import writes in the phases `src/ummanu/board/import_order.py` declares, and
nowhere else is the order stated: exported request/audit history first, then Products, Issues and
task cards, card comments (task, Issue and Product), closure, card order, sprints and sprint comments.
Each phase names the schema tables it writes. The rule is that for every immediate foreign key in
`src/ummanu/board/schema.py` between two tables the import writes, the parent's phase comes no later
than the child's (a `DEFERRABLE INITIALLY DEFERRED` key holds at commit, whatever the order). History
goes first because an Issue or Product comment's `[request-id:...]` stamp claims an exported request
(`issue_comment_claims_its_request`), and a history row references no board row. The card restore
sorts its create and initialize sweeps by the record's phase, so a Product precedes its Issue. A
writer's own staged claim precedes its effect inside the same call, and is not a phase ordering.
`tests/test_import_write_order.py` builds the FK graph from the schema metadata and fails on a table
with no place in the order or an edge the order violates.

**Refusals.** The import runs in one transaction, and PostgreSQL aborts it at the first refused
statement, so no read after it can prove anything. A refused swimlane, card create,
metadata/state, comment, closure or order batch therefore fails at once with the store's message
(`board store refused the restored <batch>: the board store refused to apply a write: <PostgreSQL
error>`). The import does not treat it as uncertain, and the whole import rolls back. Only
transport loss (`backend_unavailable`), where the outcome is unknown, takes the reconcile path
("… is uncertain …"), and the rerun resumes it.

Restore-only bulk boundaries, all idempotent on rerun:

- **Cards.** Task, Product and Issue creation validates the full plan and stages a deterministic
  per-card audit obligation before the first backend mutation. Every create, metadata/state and closure
  batch is reconciled against a fresh board inventory, and only individually proved rows commit. A
  retry writes only absent or incomplete obligations. Duplicate references, conflicting
  title/description, or a committed card that no longer matches fail closed. The metadata proof reads
  in the store's vocabulary, where an empty value is no value: `saveTaskMetadata` clears a bag key it
  is handed empty, so an exported `"model": ""` reads back absent and still proves; a non-empty value
  the row lacks does not.
- **History on retired ids.** A card's lane, column, position, closed state and project id are
  restored as exported, also when the project id is no longer in the registry (production holds closed
  `personal_site` cards beside the current `personal-site`). An exported lane is matched by its exact
  name before any look-alike. The registry is checked only for an open Product's project set; a task
  card's project id is not checked against it, open or closed.
- **Comments.** Card and sprint history is read in bounded batches and written in ordered waves with at
  most one next occurrence per entity per batch. Each occurrence is staged under a stable restore
  request id; a fresh history read proves applied occurrences before audit append, and only unproved
  ones are retried. Identical bodies keep multiplicity and order (body digest plus occurrence ordinal).
  Pending evidence carries the body only while the outcome is ambiguous.
- **Order.** Archived rows are closed in bounded batches only after their comments are proven. Then,
  from an authoritative snapshot, the relative order of active rows in each `(column, swimlane)` group
  is reconciled to normalized `(position, reference)` order. Only mismatched groups move; each owns one
  deterministic `restored_order` request and pending audit record, resumes at its first mismatch, and
  commits only after a final read proves the order. This repairs a preserved `board_parity=failed`
  target without clearing progress or repeating unrelated writes. Absolute positions of archived rows
  are not compared.

Restore is complete only after fresh card and sprint snapshots prove full content and order parity.
Released per-card create/restore events remain valid resume evidence. Recovery never clears
audit/progress or rotates a namespace still bound to the target.

### Degraded outcomes

Project failures are isolated after board and memory recovery. Output has one row per binding: project
id, target state, transport, outcome (`cloned`, `unchanged`, `failed`, `disabled`), sanitised reason
and retryability. If any row fails, recovery still completes safe host finalisation and the ownership
handoff, then exits non-zero with `status: degraded`. Invalid global configuration, board/sprint
parity, memory corruption, unsafe host materialisation and operator interruption stay fatal.

Recovery merges nothing into a reused snapshot repository or checkout: it is fast-forwarded or
refused. One with local-only history (a snapshot tip the remote does not extend; in a legacy checkout
an operator commit, or a head-registry checkpoint an older recovery kept) is refused with both tips
preserved; nothing is reset, rebased, merged or force-pushed.

### Retry

Rerunning the same `ummanu recover` command is the only supported retry. The checkout is
fast-forward only; a snapshot recovery interrupted after the bare clone reuses that repository and
lays the live root out, and one interrupted after the live root reuses both, by the live-root rule
above; completed board import and memory indexing are skipped while the board, run,
memory-fact and binding identity still matches; successful checkouts are untouched; only
missing/failed checkouts and their dependent host resources are retried. The identity length-delimits
every canonical path, entry type and content value before hashing.

`recovery-progress.json` in the data directory is non-secret derived state: identity, completed core
phases and sanitised project outcomes. Project rows are diagnostic only; filesystem checkout state is
the retry authority. Do not edit it, registry bindings, or managed-state files to force a retry.

A restored instance stays recoverable: its own restore audit goes into the next checkpoint, and a later
recovery into another empty backend writes events under a new namespace kept in the restore state
file, so retrying one recovery stays idempotent.

The low-level `restore-reconcile` diagnostic returns non-zero `degraded` while any configured checkout
is unavailable and does not mark reconcile complete.

Terminals, worktrees, the vector index, generated units and host caches are not copied from the
remote. No object store or separate backup host is required.

## Repairing historical duplicate card references

Cards created by releases before commit `d9e872b` may reuse an archived row's reference. The repair
keeps the older owner and reassigns the later row only when its backend ID equals the duplicated
numeric suffix, its exact `created` audit event binds that ID, both records are tasks with complete
metadata, and no active or ambiguous dependent state exists. A numeric coincidence without that
evidence is not authority.

Preview is read-only. It lists active and archived Pipeline rows with backend IDs, record type, state,
a bounded title, retention evidence, proposed collision-free references, refusals and a plan hash:

```bash
ummanu task repair-references-preview --instance INSTANCE --data-dir DATA_DIR
```

Apply names that plan and every proposed row by backend ID; put the non-secret reason in a file:

```bash
ummanu task repair-references-apply --role po --instance INSTANCE --data-dir DATA_DIR \
  --plan-id PLAN_ID --task-id BACKEND_ID --request-id REQUEST_ID --reason-file REASON_FILE
```

Repeat `--task-id` for every proposed row. Apply takes the allocation lock, compares the whole preview
before writing, stages all audit intents, then updates each row with `reference_repair` metadata and
an append-only `reference_repaired` event. A retry with the same request ID, plan, IDs and reason
resumes or proves the same effects. It never deletes, merges, reopens or moves a card; titles,
descriptions, comments, metadata, closed state and position are unchanged. A target taken by another
row, mixed record types, missing producer evidence, active work, reference-bearing dependents or a
concurrent board revision fail closed.

After a committed backend change, pending audit blocks export until the identical retry or
`task reconcile-audit` completes it. If another card claims a still-untouched row's target after an
interruption, either command keeps that card and reallocates under the allocation lock, recording each
superseded allocation; a row already changed is never reallocated. Rollback is a reviewed follow-up
after audit reconciliation, never a hand edit of checkpoint files, pending audit or board storage.
Never edit `cards.ndjson` as the source of truth.

## Not covered

- Moving configuration into a control-plane database.
- Automating provider credentials and head authorisation.
- A mandatory object-store transport, a full archive of transcripts and artifacts, a public plugin API.
