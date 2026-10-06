# Operations

Operator runbooks for a running installation. Install and restore are in [Recovery](RECOVERY.md);
command and route contracts are in [Protocols](PROTOCOLS.md). The state of a particular installation
comes from `ummanu status` and `ummanu doctor`, not from this file.

- [installation and host requirements](#install-and-check-the-code);
- [data, status and checkpoint operation](#data-plane);
- [connecting a project](#connecting-a-project-gate-and-stale-input-recovery);
- [sprints and observer heads](#starting-a-sprint);
- [recovery and the optional cold archive](#recovery);
- [dispatcher operation and watchdogs](#auto-merging-green-cards);
- [background roles, web service and units](#background-role-telemetry);
- [upgrade and runtime health](#upgrade).

## Install and check the code

From the product checkout, owned by the installation user, prepare the environment explicitly
([Recovery](RECOVERY.md#fresh-install-and-recovery) has the full host sequence). Ubuntu 24.04 needs
`python3-venv` first:

```bash
sudo apt-get install --yes python3-venv
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[memory,dev]'
.venv/bin/python -m tests.broad
```

The extras install the memory runtime and pinned linter. The
install is editable: the runtime reads deployment assets from the checkout, and `upgrade` treats a
non-editable (snapshot) install as drift and reinstalls. The shipped units run
`PRODUCT_ROOT/.venv/bin/…`. Install and recovery refuse a missing or foreign environment before
materializing the host. Run root commands by the absolute `PRODUCT_ROOT/.venv/bin/ummanu` path and
name `--product-root PRODUCT_ROOT` for install/recover; shell activation does not preserve a venv's
PATH through `sudo`. `ruff`
is pinned in `pyproject.toml` and any other version refuses to run; run it only on changed Python paths
with the command in [Testing](TESTING.md#changed-python-lint). Host bootstrap supports Ubuntu 24.04,
installs Docker and Compose from the distribution and provisions the board store; `ummanu install` or
`ummanu recover` then applies the instance ([Recovery](RECOVERY.md)).

`ummanu status --instance <dir>` summarizes an installation; `--json` is a structured snapshot and
writes no state. `doctor` reports broken invariants (`--json` for structured findings). Changing the
host requires `reconcile plan` and a separate confirmed apply.

### Pointing a command at a live root

A command reads its live root from `--instance <dir>` (the directory or its `instance.yaml`), else
from `UMMANU_INSTANCE`, else from the default `~/ummanu-data/instance`. To reach a live root
anywhere else, name it:

```bash
ummanu doctor --instance /srv/other/instance        # one command
export UMMANU_INSTANCE=/srv/other/instance           # every command of this shell
sudo --preserve-env=UMMANU_INSTANCE ummanu upgrade   # sudo drops the variable otherwise
```

With neither given and no directory at the default, the command exits non-zero, names the default
path and asks for `--instance` or `UMMANU_INSTANCE`; it creates nothing. The packaged units and every
head the dispatcher launches set `UMMANU_INSTANCE` explicitly, so they never fall back.

## Runtime secrets

### Installation secrets

Installation secrets live in the recoverable store (`ummanu secret init/set/import`, the `secrets/`
directory of the private repository) and are materialised into env files. The store contract is in
[Recovery](RECOVERY.md#secrets). `runtime.env` next to `instance.yaml` can be a materialisation target;
whether it is shows under `secret_store.materialize` in `ummanu status --json`. The product does
not migrate it on its own. Either way the file is `0600`, outside the export allowlist and in no checkpoint or archive.
`ummanu shell` ([the interactive head](#the-interactive-head-and-its-workspace)) receives the whole file; dispatcher-launched workers and reviewers receive
non-secret runtime switches through the role-environment wrapper.

Migrate an existing `<instance>/runtime.env` with the CLI, never by copying values through a shell or
an argument list:

```bash
python3 -P -m ummanu secret init --instance INSTANCE
python3 -P -m ummanu secret import --instance INSTANCE --file INSTANCE/runtime.env \
  --scope installation --purpose runtime --materialize runtime-env
python3 -P -m ummanu secret materialize --instance INSTANCE --target runtime-env
```

`secret init` is interactive and shows the recovery phrase once. `runtime-env` is a named target
resolving to the installation's canonical runtime-env path (including the supported override); do
not pass `--materialize-path` with it — that flag is only for the `file` target. `reconcile` never
decrypts the store.

### PostgreSQL board store

A fresh `ummanu bootstrap` creates `/opt/ummanu/postgres-compose.yml` (or checks the one root
installed), the `ummanu-board-store_board-db` volume and `<instance>/board-store.env`, runs Alembic to the
shipped head and verifies the owner/app/read logins and privilege boundary. PostgreSQL is the only
container on this path and publishes only `127.0.0.1:5432`. Schema and roles are in
[Board store](BOARD_STORE.md).

`board-store.env` holds nine keys and three independent passwords, mode 0600, outside the export allowlist. Do not
print it, put its values on an argument list or commit it. An upgrade with no file reports PostgreSQL
as not provisioned and continues. Once the file exists, an invalid file, unreachable server,
unexpected image/volume/port, role drift or migration failure stops the upgrade before consumers
restart: preserve the file, Compose definition and volume, repair the named cause and rerun the same
command. A rerun keeps the volume and credentials and applies only owed migrations.

No credential rotation command is shipped. Editing `POSTGRES_PASSWORD` does not rotate an owner in a
non-empty volume. Rotation is one explicit manual operation: `ALTER ROLE`, an atomic whole-file rewrite
of `board-store.env`, and a restart of the web and dispatcher consumers together.

### Checkpoint and project GitHub access

One managed token, `github.checkpoint-token`, serves the checkpoint push and every dispatcher Git
operation on registered GitHub HTTPS projects. Set or rotate it with `secret checkpoint-github set
--instance INSTANCE --stdin` (or a caller-owned mode-0600 `--file`); it needs read and write access to
the instance remote and every such project. Transport classification, recovery bootstrap credentials
and readiness rows are in [Recovery](RECOVERY.md#github-checkpoint-credential). A manual
`~/.git-credentials` entry is not checkpoint readiness; read `checkpoint.credential` in `status --json`.

Before claiming a Ready card the dispatcher runs a bounded `ls-remote` preflight. A missing, locked or
rejected credential blocks the card with `step: git-access-preflight` and a refusal code
(`credential-missing`, `credential-locked`, `credential-rejected`, `unsupported-https`,
`unsupported-transport`, `unsafe-remote`, `remote-unresolved`); no workspace or head is created and the
block is not retried. A preflight with no answer leaves the card in Ready. A credential refused later,
at the gate, blocks with `git_access_refusal`. To recover: read the `project-git:<project>` rows of
`ummanu doctor --instance INSTANCE`, rotate or unlock the token, return the card to Ready.

## Codex provider-internal fan-out policy

The policy and the telemetry it records are in
[Protocols](PROTOCOLS.md#codex-provider-internal-fan-out-policy). Fan-out observations are telemetry
only: do not stop or replace a head, block work or refuse delivery because of one.

Rerun the capability matrix only when an approved disposable-auth probe is warranted (a Codex
binary/model change or a candidate provider control). Use `scripts/codex_capability_matrix.py` with a
freshly isolated empty git worktree and `CODEX_HOME`; never point it at a production home. The
harness copies an explicitly approved auth source only into the temporary home, deletes the copy
before the next row, and outputs only raw-stream digests and typed event summaries.

## System requirements

The memory runtime loads a local embedding model and is the dominant memory consumer; an index
rebuild is its peak. No supported minimum is declared: size the host from `ummanu status --json`
resource figures.

The model cache is `DATA_DIR/memory/fastembed-cache`, never `/tmp`. `host.memory_threads` sets the
ONNX Runtime inference limit (default `1`). `ummanu doctor` prints the cache path and warns when
`data_dir` puts it under a temporary directory.

`host.memory_model` and `host.memory_dim` select the embedding model and its matching dimension
(defaults `intfloat/multilingual-e5-large` and `1024`). Install, recover, explicit memory reindex and
the memory service resolve the same settings. A recovery retry rebuilds an incompatible, legacy or
unreadable index without reimporting an already-restored board.

The legacy `host.memory_reindex_python` and `host.memory_reindex_script` overrides affect only
explicit `ummanu memory reindex --instance INSTANCE`. With neither set, it uses the product indexer.
Setting either requires both: an executable Python file and an existing script; an incomplete pair
is refused. The script receives `--canon`, `--export`, `--target-db`, `--model` and `--dim`, with
`MEMORY_THREADS` and `MEMORY_CACHE_DIR` in its environment. Automatic install and recover use the
product indexer and ignore both overrides.

Heads run on `local-pty`, which ships with the product: no host-owned head runtime is installed,
ordered after or reported (A20 step 9, [Head runtime](HEAD_RUNTIME.md#a20-exit-checklist)).

## Data plane

```bash
python3 -P -m ummanu data init --instance INSTANCE
python3 -P -m ummanu data export --instance INSTANCE [--copy-transcripts]
```

`data init` creates the local layout and manifest. The memory-fact canon is
`INSTANCE/state/memory/facts`; the data directory keeps its export and index. `data export` writes
normalised board, memory, run and transcript exports; without `--copy-transcripts` only a transcript
inventory is kept.

## Checkpoint writer

The snapshot exporter, its cadence and validation gate are in [Recovery](RECOVERY.md#writers).
Operationally: card reconciliation runs every one-minute tick, while checkpoint export and the cut
run at most once per five-minute window (a due push forces a fresh preparation). The gate is
fail-closed: pending task audit, an `export.json` counter mismatch or a detected secret blocks the
commit, the reason goes into the dispatcher's checkpoint state, and the next tick retries.

The shipped memory pack `packaging/memory/product-ummanu` is materialized into the memory canon
during install and upgrade, with its ownership and digest record in
`INSTANCE/state/memory/packs/product-ummanu.json`. It publishes facts under `product:ummanu`. A
local fact at a shipped id stops the upgrade. `ummanu upgrade --no-pull` still compares the
checkout's pack digest to the ledger and restarts the memory service when reconciliation changed it.
The ledger stays `pending` until the export is published and readable by the service user; the next
upgrade retries.

### Memory access from Claude and Codex

Install and upgrade reconcile an installation-owned `po_memory` stdio MCP entry in the installation
user's `~/.claude.json`, `~/.codex/config.toml` and `DATA_DIR/codex-home`, preserving login state
and unrelated entries. The command is `PRODUCT_ROOT/.venv/bin/ummanu-memory-po-bridge`; its
environment names only the grant directory and loopback Memory URL, and no bearer is stored.

The entry applies to Claude or Codex processes started afterwards; restart existing sessions to get
`po_memory`. `ummanu shell` and dispatcher-launched heads use the direct HTTP Memory endpoint with
a role-bound capability instead; a worker or reviewer gets scope `project:<card-project> +
product:ummanu`.

Inspect without exposing credentials:

```bash
ummanu upgrade --dry-run --no-pull --instance INSTANCE
rg -n 'po_memory|ummanu-memory-po-bridge' \
  ~/.claude.json ~/.codex/config.toml \
  DATA_DIR/codex-home/config.toml
```

### Codex home (`CODEX_HOME`)

Every Codex head launched by the dispatcher, and the `openai-sub` resource probe, runs with one
`CODEX_HOME`, resolved at each launch in this order:

1. the profile's `codex_home`;
2. `TA_CODEX_HOME`;
3. `DATA_DIR/codex-home`, when it holds a login (a non-empty `auth.json`).

With none of them the launch fails closed: no Codex head starts, and the refusal names the fix, "log
in under `DATA_DIR/codex-home` (`CODEX_HOME=DATA_DIR/codex-home codex login`), or copy an `auth.json`
there". There is no fallback to the legacy Orca home (`~/.config/orca/...`); A20 step 7 removed it
(secretary-1723) once every live Codex head was proven to run on the data-dir login.

Rung 3 needs to know the data dir. It comes from `UMMANU_DATA_DIR`. The production dispatcher
tick, the background agents and `ummanu shell` set that variable for their own run from the
selected instance, and the web unit sets it in its unit file. A process with no data dir, no profile
`codex_home` and no `TA_CODEX_HOME` is refused.

Install and upgrade (the `codex-home` step) copy `AGENTS.md` and `config.toml` into
`DATA_DIR/codex-home` if they are missing. It is the only CODEX_HOME the installation manages: the
legacy Orca home is neither seeded nor reconciled, and is not moved or deleted either (that is a PO
action, if ever). Neither step copies or writes `auth.json`.

The managed home must hold the full `[mcp_servers.po_memory]` bridge entry (`command`, `args`,
`env`). Each head is launched with `-c mcp_servers.po_memory.enabled=false`, and on a home without
the entry that override creates a table with no `command` or `url`: Codex then refuses every command
with `invalid transport in mcp_servers.po_memory`. The packaged `config.toml` therefore carries no
such table. Seeding writes the entry into each `config.toml` it creates, in the same step. The
upgrade's `memory-clients` step reconciles it in `DATA_DIR/codex-home` whenever that directory
exists, since a login alone is enough for heads to select it: a file the home lacks is seeded first
through the same copy-once path, so the packaged defaults are never skipped, whichever of the
`memory-clients` and `codex-home` steps reaches the home first.

Check the login as the installation user:

```bash
ummanu doctor --offline --instance INSTANCE | grep 'codex home'
#   error: codex home: no Codex login for this installation: log in under DATA_DIR/codex-home ...
CODEX_HOME=DATA_DIR/codex-home codex login
ummanu doctor --offline --instance INSTANCE | grep 'codex home'
#   codex home: DATA_DIR/codex-home (data-dir home)
```

`doctor --json` returns the same answer in `codex_home` (`path`, `kind`, `data_dir_home`,
`login_missing`, `codex_required`). A missing login is a red finding (`codex_home_login_missing`,
with the fix text) whenever an installed head profile runs on the `codex` adapter; an installation
with no Codex profile only prints it.

Every session reader scans the `sessions/` of the current home and of the data-dir home, whichever
exist, and, read-only, the legacy Orca home's `sessions/`: on 2026-09-24 the curator had not yet
ingested 468 of the 3217 rollouts there (`runtime/codex_home.py`, `_LEGACY_SESSIONS`, which goes once
the curator's watermark names every one). No head is launched there. This covers the watchdog's
activity signal, the delivery confirmation for service heads, the dispatcher's continuation recovery
proof and the curator. Each reader counts a session only once. An explicit sessions override
(`TA_CODEX_SESSIONS`, `UMMANU_CODEX_SESSIONS`, `TA_CODEX_SESSIONS_DIR`) is still the only root
while it is set.

### The PO workspace

The product owner head runs with `DATA_DIR/po` as its working directory (`DATA_DIR` is `data_dir`
of `instance.yaml`). Install and upgrade materialize it in the `po-workspace` step; after
`role-skills` has delivered into it, `po-workspace-owner` hands the whole tree to the runtime user on
every root-invoked upgrade (symlinks and hardlinked files are not followed), so a skill root left
root-owned by an earlier run is repaired:

| Path | Content | On upgrade |
| --- | --- | --- |
| `AGENTS.md` | copy of `packaging/po-workspace/AGENTS.md` from the product checkout | rewritten |
| `CLAUDE.md` | the single line `@AGENTS.md` | rewritten |
| `NOTES.md` | the PO's local notes | created if absent, never rewritten |
| `.mcp.json` | Claude project MCP entry `po_memory` | only that entry reconciled |
| `.codex/config.toml` | Codex `[mcp_servers.po_memory]` | only that section reconciled |
| `.claude/skills/` | PO skills for Claude | `role-skills sync` |
| `.agents/skills/` | PO skills for Codex 0.154, which reads this root in its cwd | `role-skills sync` |

The MCP entries are the same stdio bridge as the user-scoped ones above, written by the same
writers. The skills are the `po` role of `skills/manifest.toml`: `open-sprint`, `open-issue`,
`grilling`, `knowledge-doc`, delivered through targets whose root starts with `@po/`. A skill entry
`ummanu/open-sprint` reads the skill from the ummanu role's tree, so shared skills have one
source. `role-skills audit|sync` resolves `@po/` from the instance's `data_dir`, or `--data-dir`;
with no `instance.yaml` those targets are listed as unresolved and skipped.

`role-skills sync` removes a skill copy from a target root once no manifest declares that skill for
that root, but only a copy it can prove it delivered: one carrying the `.ummanu-role-skill`
marker it writes into every copy, or, for copies delivered before the marker existed, one whose
`SKILL.md` is byte for byte a version the manifest's repository shipped under that name. Other
directories in a shell root are never touched. `audit` lists pending removals under `retired`.

Check:

```bash
ls -la DATA_DIR/po DATA_DIR/po/.claude/skills DATA_DIR/po/.agents/skills
ummanu role-skills audit --instance INSTANCE
```

### The interactive head and its workspace

The owner's interactive head starts with `ummanu shell` and no alias:

```bash
ummanu shell                     # the registry's role_defaults.new_card head
ummanu shell --head codex        # any adapter or heads.toml profile id
ummanu shell --print             # cd DATA_DIR/interactive && <launch command>; starts nothing
ummanu shell --workspace DIR     # another cwd and Codex trust directory, without the persona
```

Without `--workspace` the head's cwd and its Codex trust directory are `DATA_DIR/interactive`, the
data dir of the selected installation (`UMMANU_DATA_DIR`, `UMMANU_INSTANCE`, the `--env-file`'s
instance, else the default instance). An explicit `--workspace` wins. The shell never materializes
the workspace: a missing one is refused, exit 2, with a message that names the path and
`ummanu upgrade`.

Install, upgrade and recover materialize it in the `interactive-workspace` step and hand it to the
runtime user on a root-invoked run ([The persona boundary](ARCHITECTURE.md#the-persona-boundary)):

| Path | Content | On upgrade |
| --- | --- | --- |
| `AGENTS.md` | the shared part `packaging/interactive-workspace/AGENTS.md` from the product checkout, a separator, then the personal part `INSTANCE/persona/AGENTS.md` byte for byte (the shared part alone when that file is absent) | rewritten |
| `CLAUDE.md` | the single line `@AGENTS.md` | rewritten |
| `.sources.json` | the digests of both parts and of `AGENTS.md` | rewritten |

The persona lives in two places, and neither is this directory: the shared role contract in the
product (changed by a product card), the owner's part in the live root's `persona/AGENTS.md` (changed
by a PO operation and `ummanu config check`, exported with the snapshot). Edits made here are
overwritten by the next upgrade, which reports `changed` whenever either part changed and
`unchanged` otherwise. No other head's workspace and nothing under `~/.claude` receives the persona.

The legacy `instance.yaml` fields `persona.name` and `persona.style` still validate but are ignored;
they do not affect these instructions. `host.orca_repos` also remains accepted and ignored for
compatibility: it does not create, check or remove Orca registrations.

`ummanu doctor` and `ummanu status` print one line for it, and `status --json` carries it under
`installation.interactive_workspace`:

```text
interactive workspace: DATA_DIR/interactive (shared sha256:<12 hex>, personal sha256:<12 hex>|absent)
```

A workspace not yet materialized reads `(absent; ...)`, one edited by hand since reads
`AGENTS.md differs from its sources`; both name `ummanu upgrade` as the repair.

### PO head sessions and turns

`ummanu.po.runner` runs the PO head headless, without a head runtime. A **session** is one
conversation with one CLI (`claude` or `codex`), one model and one reasoning effort, with `DATA_DIR/po` as cwd. A **turn**
is one CLI process with full permissions (`--dangerously-skip-permissions`,
`--dangerously-bypass-approvals-and-sandbox`), its own process group, and the owner's message on stdin:

| CLI | turn 1 | later turns | final answer |
| --- | --- | --- | --- |
| Claude | `claude -p --output-format json --session-id UUID` (UUID chosen at session creation) | `--resume UUID` once a turn completed; before that `--session-id UUID` again | `result` of the JSON result object |
| Codex | `codex exec --json -C DATA_DIR/po -` | `codex exec resume THREAD_ID -` (`thread_id` from turn 1's event stream) | the `-o` file |

**Effort.** Every turn carries the session's effort: Claude `--effort LEVEL`, Codex
`-c model_reasoning_effort=LEVEL` (both checked on Claude Code 2.1.280 and Codex 0.155.1). A new session's
effort is always explicit (`po/models.py::require_explicit_effort`, used by the web, the PO service and
the runner). `default` survives only as the stored value of sessions opened before that rule (every
session from before efforts existed, and later ones opened on `default`): they still resume with no flag,
so the CLI runs with its own configured effort, and the pages say their effort is `not set`.

**Resolved model.** Each settled turn records the model the CLI actually ran (`po_turns.resolved_model`),
e.g. `claude-opus-5-5` for `opus`. Claude: the first key of `modelUsage` in its JSON result — the
session's own model; subagent models follow it (`claude-opus-5-5[1m]` is the long-context variant, kept
as reported). Codex: its `--json` event stream names no model, so it is the `model` of the last
`turn_context` in the thread's rollout, `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*-THREAD_ID.jsonl`
(`CODEX_HOME` of the turn environment, else `~/.codex`). A turn that reported nothing (no result object,
no rollout) keeps `null`; the session reads show the latest turn's non-null value.
**Environment.** A turn gets the `po-serve` environment (HOME, auth, `UMMANU_*`, `TA_*` kept) with
the directory of the interpreter running `po-serve` (the product runtime, `/home/dev/ummanu/.venv/bin`
on prod) first on `PATH` and the product source it imports first on `PYTHONPATH`. So `python3 -P -m
ummanu ...` and `ummanu ...` from the PO workspace run the product, not the system Python.
`PoRunner.turn_environment()` computes it for every turn and re-run; an explicit `env=` replaces it.
Check, as the runtime user from `DATA_DIR/po` (prints the service interpreter, then `ok`):

```bash
BIN=$(dirname "$(tr '\0' '\n' </proc/$(systemctl show -p MainPID --value ummanu-po.service)/cmdline | head -1)"); echo "$BIN"; env PATH="$BIN:$PATH" sh -c 'python3 -P -m ummanu --help >/dev/null && ummanu --help >/dev/null && echo ok'
```

Board store tables (revisions `0008_po_sessions`, `0009_po_requests`, `0010_po_session_close`,
`0015_po_effort_resolved_model`):

| Table | Holds |
| --- | --- |
| `po_sessions` | id, cli, model, cwd, created_at, state (`open`/`closed`), the CLI's session id, `closed_at` and `closed_by` (set exactly when closed), `effort` (always an explicit offered one for a new session; `default` only on sessions opened before that rule, which resume with no effort flag and read `not set`) |
| `po_turns` | session, seq, started/finished, `running`/`completed`/`failed`/`interrupted`, stdout path, pid, process identity, reason (why it failed or was interrupted; on a re-run turn, why it was re-run), `resolved_model` |
| `po_feed` | the owner's messages and the agent's final answers only; no tool calls, no reasoning |
| `po_requests` | each /po form request id: operation (`po_session_create`, `po_send`, or `po_sprint_session` for a resolver's session, since 0016), fingerprint of its inputs, the session and, for a send, the turn it made |

A partial unique index allows at most one `running` turn per session; the PO service keeps a second
message queued until the running turn ends ([The PO service](#the-po-service)). Different sessions run
turns in parallel.

**Close.** `PoStore.close_session(session, actor)` locks the session row and, in one transaction, sets
`state = 'closed'`, `closed_at = now()` and `closed_by`; the CHECK `po_session_closed_iff_audited`
refuses a closed row without both. A running turn refuses the close (`TurnInProgress`) and writes nothing;
closing a closed session answers it unchanged with its first `closed_at`/`closed_by`. A send into a closed
session is refused in the transaction that would create the turn (`SessionClosed`): no turn, feed entry,
request row or process. A replay of a send made before the close still answers its turn. Nothing is
deleted and a closed session is not reopened; its feed and raw output stay. Check:

```bash
psql "$UMMANU_DB_READ_URL" -c "SELECT session_id, state, closed_at, closed_by FROM po_sessions ORDER BY created_at DESC LIMIT 10"
```

Raw output of a turn is in `DATA_DIR/po-runs/SESSION/turn-NNNN.{prompt,stdout,stderr,last-message}`,
outside the workspace. A non-zero exit, or no final answer, makes the turn `failed`; `reason` names the
exit status and quotes the stderr tail.

**Stop** kills the turn's process group and marks it `interrupted`. The session continues with
resume. A Codex turn stopped before its event stream named a `thread_id` leaves no id, and the next
turn starts a new Codex thread. A Claude session whose turns were all stopped or failed may or may not
have a saved conversation: the next turn uses `--session-id`, and if Claude answers
`Session ID UUID is already in use` the same turn is relaunched with `--resume`. Both attempts' output
is appended to the turn's files.

If the process of a turn starts but cannot be recorded in the store, its process group is killed and
reaped and the turn is `failed`. If the store does not answer at all, the row stays `running` with no
pid until the PO service's next start re-runs it.

### The PO service

`ummanu-po.service` (`ummanu po-serve --instance INSTANCE`, `Type=simple`, `Restart=always`, the
runtime user, the web unit's environment) is the one owner of PO turns: it holds the installation's only
`PoRunner`, and every turn process is a child in its control group. It has no `PartOf=`/`BindsTo=` tie to
`ummanu-web.service`, so **a web restart touches no turn**: a running turn finishes and its answer
reaches the feed. `reconcile apply` and `upgrade` install and enable it with the other catalogue units,
doctor lists it, and `host.components.po.enabled: false` opts out (then no PO turn runs anywhere). The
service takes an exclusive lock, `DATA_DIR/po-service/service.lock`; a second `po-serve` on the same data
dir refuses to start.

**Queue.** A message is one file in `DATA_DIR/po-queue/` (`<time_ns>-<pid>-<n>.json`: `session_id`,
`text`, `request_id`, `source` — `web`, `dispatcher` or `po-service` — and `queued_at`, plus the
`card` facts of a dispatcher input and, for an operation card, the service's production rights `note`
that the turn's prompt ends with), written to a temporary name, fsynced and
renamed before the submitter gets an answer. The service takes inputs oldest first per session and runs
one turn per session at a time; a message for a busy session waits in the queue, neither refused nor lost,
and sessions run in parallel. An input leaves the queue only after its turn row exists (`claim_turn`
under the input's request id), so a crash between the claim and the removal is answered by the same turn
on the next hand-over and creates nothing. An input that can never become a turn (session gone or closed,
request id taken by something else) moves to `DATA_DIR/po-queue/refused/` with its `reason`. A session
with queued messages refuses a close (409). Queued messages survive any restart of either service. A
`backup create` archive, core and full, carries `po-queue/` with `refused/` (the in-flight temporary dot
files stay out), and `restore` puts them back. When the runner starts a dispatcher input's turn it keeps
that input's card facts beside the turn (`DATA_DIR/po-runs/<session>/turn-NNNN.card.json`), so a turn that
ends `failed` names its card in the `po_turn_failed` owner event.

**Request ids.** One check, `PoService._reserve`, decides every request id, for `submit` and
`create_session` alike (and any later operation that takes one), under the lock that serializes them. An
id belongs to one operation with fixed inputs from the moment it is acknowledged, wherever it is recorded:
a `po_requests` row, a message pending in `po-queue/`, or a message set aside in `po-queue/refused/`. The
same operation with the same inputs is a replay; anything else is 409 `request_conflict`. So a session
create can never take the id of a message still waiting in the queue.

**Endpoint.** The Unix socket `DATA_DIR/po-service/po.sock` (mode 0600 in a 0700 directory): one JSON
request line, one JSON answer line, ops `submit`, `create_session`, `sprint_session`, `stop_turn`,
`close_session`, `rename_session`, `status` and `restart` (`ummanu.po.client`). The web is only a client: it reads the
store and the queue directory and sends every write here. The service runs a request only once its
whole line arrived.

**Accepted is accepted.** A message is accepted when its queue file is written, a session when its
`claim_session` commits. From then on the service answers "accepted" from what it knew at that moment —
the message queued under its id, or the session — even if a later step (the hand-over to a turn, the
lookup of the turn it became) fails; that lookup only adds detail. A failure *during* the accepting write,
which may have landed, is `outcome_unknown`. One wrapper in the service (`PoService._answer_id_operation`)
applies this to every operation that takes a request id.

**A refused form keeps its request id**, so sending it again is a replay of whatever the first attempt
did, never a second message or session. Only refusals marked, where they are raised, as having written
nothing (`data.nothing_written`) get a fresh id:

- nothing reached the service (no socket, connection refused, the send failed before the line ended):
  503, `the PO service is not running ... nothing was sent or written`;
- the input was refused before its id was reserved (empty text, a model or effort not offered, an unknown
  CLI): 400;
- the id already belongs to another request: 409 `request_conflict`;
- the session is unknown or closed: 404, 409 `session_closed`.

Everything else keeps the id: a lost or late answer (`the PO service may have accepted this message;
sending again with the same form is safe`, `data.reason = outcome_unknown`), a store that failed, any
unexpected error. A lost answer to a stop or a close says repeating it is safe (both are idempotent).
The web never starts a turn itself.

**Turn environment.** Every turn process gets `UMMANU_PO_SESSION=<session_id>` beside the product
runtime's `PATH`/`PYTHONPATH`, on its first launch, a re-run and a relaunch alike. `sprint create` inside
a turn takes it as the default of `--po-session`, so the sprint records the session that opened it.
Beside it, `UMMANU_PO_REQUEST=<request id>` names the input the turn answers (read from `po_requests`
for that turn, so a re-run names the same one; unset when the input carried no request id, and never
inherited from the service's own environment). `task create --role po` inside a turn records both as
the card's origin, and the card's result comes back to that session when it settles
([Protocols](PROTOCOLS.md#po-delegation)).

**Session title.** `po_sessions.title` (0019) is a readable name, null for an untitled session. The owner
sets it from the session page (`POST /po/sessions/ID/title`, field `title`); the PO sets its own with
`ummanu po rename --instance I --title TEXT [--session ID]`, whose `--session` defaults to
`$UMMANU_PO_SESSION`. Both go through the service's `rename_session` to `PoStore.set_title`, the one
rule: trimmed, one line (no C0/C1 control character), at most 120 characters; empty clears it. It carries
no request id and writes no `po_requests` row: a repeat sets the same value. A closed session may be
renamed. The resolver titles a sprint's new session with the sprint's ref (`sprint:<N>`). The CLI prints
`{"kind": "po_session_renamed", "session_id", "title"}`; a refused title or an unknown session exits 2, a
service that did not answer 1, as `web-run` does.

**A sprint's session (the resolver).** `sprint_session(sprint_ref, request_id)` answers the live PO
session of a sprint ([Protocols](PROTOCOLS.md#the-sprints-po-session-and-productions)). The session the
sprint recorded, while it exists and is open, is the answer and nothing is written. Otherwise the service
**re-seeds**, once: it opens a fresh session (the old one's CLI, model and effort, or the new-session
form's defaults when the old row is gone), queues as its first input a seeding message (the sprint, why
the session was opened, the text of the sprint's why-document from `state/knowledge/decisions/`, and the
instruction to read `NOTES.md` in the workspace), comments on the sprint as `po-service` (`the PO session
<old or none> no longer exists; opened <new> seeded with ... and NOTES.md`) and records the new id as the
sprint's `po_session`, in that order. The seed input is queued with `source: po-service`. Resolves run
under the service lock, so concurrent resolves of one sprint open one session; a repeat of the same
request id opens nothing and finishes whatever a failed attempt left. Check:

```bash
ummanu sprint show --ref sprint:ID | jq '{po_session, allowed_productions}'
ummanu sprint show --ref sprint:ID | jq '.comments[] | select(.body | contains("no longer exists"))'
```

**The dispatcher, a second source.** The dispatcher submits `decision` and `operation` cards
([Protocols](PROTOCOLS.md#decision-and-operation-cards)) through the same socket: one `sprint_session`
and one `submit` with `source: dispatcher` per claimed card, under request ids derived from the card
ref and the claim attempt. It repeats an unanswered request with the same id on its next tick and
never starts a turn itself. After the submit it reads only the store (`po_requests` for the turn its
input became, `po_turns` for that turn's state) and the queue directory (an input set aside in
`po-queue/refused/` Blocks the card), never the service. The input is queued like any other, so it
waits behind the sprint session's seed or a running owner turn. A card the PO handed to the owner
(`task handover`) waits instead of Blocking; each owner comment on it becomes one follow-up `submit`
(`owner_request_id`, `owner_submitted` on the record). Check what it did for a card:

```bash
jq '.records["REF"].po_submission | del(.text, .owner_text)' DATA_DIR/dispatcher/production-state.json
```

**Production rights.** Each dispatcher `submit` carries the card's facts (`card`: `card_ref`, `kind`,
`touches_production`, `sprint_ref`, `input`), and the service evaluates an operation card's own input
against its sprint's `allowed_productions` when it queues it ([Protocols](PROTOCOLS.md#production-rights)).
This is the only place the rule is evaluated, and it refuses nothing: every operation is queued as a
normal PO turn, with the service's `## Production rights (the PO service)` section after the card's text
(the `note` of its queue file; the turn's prompt and the `/po` feed show it). An operation touching `none`
or an allowed production says the sprint allows it and runs with no confirmation. Any other production
goes to the PO to decide under the owner's standing rule (ummanu production by default; others only as
agreed at sprint planning). The journal says:

```text
ummanu po: <card> queued for the PO to decide: touches production <p>; sprint <ref> allows [<list>]
```

The PO then either records the allowance (`sprint allow-production --role po`, audit kind
`production_allowed`) and runs the operation in the same turn, or hands the card to the owner with `task
handover`, like any card it may not decide. The service never hands a card over itself. The owner's answer
on a handed-over card (`input: owner_answer`) is queued without a rights section: the owner decided. Missing
facts, an operation naming no production and a sprint the service cannot read are refused `unavailable`:
nothing is queued, the dispatcher repeats the submit each tick (`po-service-unanswered`), and the journal
says `ummanu po: dispatcher input <id> not queued: <what is missing>` or `ummanu po: <card> not
queued: sprint <ref> cannot be read for its allowed productions (...)`. Check a card and its sprint:

```bash
ummanu task show --ref REF | jq '{touches_production, waiting_owner}'
ummanu sprint show --ref sprint:ID | jq '.allowed_productions'
journalctl -u ummanu-po.service | grep -e 'queued for the PO to decide' -e 'not queued'
```

**Service start.** Every turn left `running` is looked at once:

- its recorded process still alive (same PID and identity, not a zombie): killed first, then as below;
- no re-run recorded yet: **re-run once**. The reason `re-run: the PO service restarted while this turn was
  running` is written on the running row (`PoStore.mark_rerun`) and stays on it when it completes; the
  same prompt goes to the same conversation: Claude `--resume` once a turn of the session completed, else
  `--session-id` and, when Claude says the id is in use, `--resume`; Codex `exec resume THREAD_ID` when the
  interrupted attempt named its thread, else a new `exec`. Its output is appended to the same turn files;
- already a re-run (the reason is set): settled `interrupted` with `the PO service restarted again during
  this turn's re-run; not re-run a second time (it was re-run because: ...)`.

A turn the owner stopped was settled `interrupted`/`stopped by the owner` by the stop and is never re-run.
The re-run allowance is spent (`mark_rerun`) only after everything the re-run needs is loaded — the
owner's message from the feed, the session, the prompt file; a store that fails before that leaves the row
untouched for the next pass. A re-run that cannot be prepared for good (no owner message) is settled
`failed` with the reason; one whose launch fails after the allowance is settled `failed` (`the re-run did
not start: ...`), by the next pass if the store did not answer the first time. Recovery counts as done
only when every `running` row has a process under this service or is settled; until then it is retried,
first after a second and then doubling up to 30 s (journal: `still running without a process; recovery
retries in Ns`), and the sessions of those rows take no queued input while every other session goes on.
So a restart of the PO service costs at most the turn it interrupted, re-run.

**Upgrades never kill a running turn.** The PO itself runs `ummanu upgrade` inside a turn. The `po`
step of `ummanu upgrade` asks for a restart when the service's process inputs moved (product code or
dependencies, bundled schemas, the unit file) or its **process receipt** does not match the checkout,
and the service decides by one rule
(`PoService.request_restart`): idle, it exits at once and `Restart=always` starts the new code, which the
step waits for (`restarted ummanu-po.service while idle`); busy, it starts no queued
turn (new messages keep queueing) and exits by itself as soon as its running turns settle — the step reports `PO service restart
deferred: N turn(s) running` and does not fail. The request is the marker `DATA_DIR/po-service/restart-pending`;
the next process removes it at start. A stopped service is started.

**Process receipt.** This run's pull delta alone cannot tell whether the service runs current code: the
dispatcher's release fast-forwards the checkout before the PO runs `upgrade`, so the pull is empty
(secretary-1759). So, like the web and memory services, the service is bound to what it was started
on. Each process writes `DATA_DIR/po-service/process-receipt.json` at start, before it removes the
restart marker and takes the queue: its pid, kernel start ticks and systemd `INVOCATION_ID`, and the
checkout it runs from — product revision and the sha256 of the tracked product source, dependencies and
bundled schemas, computed as for the web and memory receipts. It is one private (0600) atomic
replacement, so a new process generation replaces the previous one's; no backup carries it. The `po`
step asks for a restart when the receipt is missing (`the PO process receipt is missing`), belongs to
another generation (`... belongs to a different process generation`), or names another revision or
digest (`the PO process receipt is stale: product revision A -> B; ...`); with a matching receipt and no
other reason it answers `unchanged` with `PO process receipt verified: pid N, revision R, ...`. A process
not started by systemd writes none (journal: `no process receipt was written`). The `verify` step
reports the receipt too: a current one is named, a non-current one fails the upgrade unless a restart is
pending (`PO service restart pending: ...`, the deferred case), and an uninstalled or stopped unit is
named as not checked. Check:

```bash
systemctl status ummanu-po.service
journalctl -u ummanu-po.service | grep 'ummanu po'   # recovery, re-runs, set-aside inputs, deferred restarts
ls -l DATA_DIR/po-queue/ DATA_DIR/po-queue/refused/ DATA_DIR/po-service/
jq . DATA_DIR/po-service/process-receipt.json                # compare .process.pid with MainPID
cat DATA_DIR/po-queue/*.json                               # what waits, oldest first by name
psql "$UMMANU_DB_READ_URL" -c "SELECT session_id, seq, state, reason FROM po_turns WHERE state <> 'completed' OR reason IS NOT NULL ORDER BY started_at DESC LIMIT 10"
```

### The PO head in the dashboard

`/po` is a client of the PO service ([The PO service](#the-po-service)): its pages read the board store
and the queue, and every create, send, stop and close goes over the service's socket. `ummanu
web-serve` builds no runner and recovers no turn. A PO head is a shell on this host, so `/po` has its own token on top of the
front's password.

**The token** is `DATA_DIR/po-web-token`: one line, mode 0600, owned by the runtime user, outside
`DATA_DIR/po`. The `po-token` step of install and upgrade creates it from `secrets` (256 bits) when it
is absent and never rewrites an existing one. Read it on the host:

```bash
sudo -u RUNTIME_USER cat DATA_DIR/po-web-token
```

**Rotate** by deleting the file and running the step again (`ummanu upgrade`, or install). Every
browser cookie issued under the old token stops working on the next request; nothing needs a restart.

**Logging in.** Open `/po`, enter the token. The form posts to `/po/login`, which compares it with
`hmac.compare_digest` and sets cookie `ummanu_po`: `HttpOnly; SameSite=Strict; Path=/po`, 30 days,
plus `Secure` when the request came through the TLS front (the front sets `X-Forwarded-Proto: https`).
The cookie value is an HMAC keyed by the token, never the token. Without a valid cookie every `/po`
route answers 401 (a page with the login form, or JSON `po_token_required`) before the PO service or the
board store is touched; a missing token file answers 503. Routes: [Protocols](PROTOCOLS.md#routes).

**The page.** `/po` lists open sessions, newest activity first, and opens a new one with a CLI, a model
from the list below and a reasoning effort (the CLI's first offered one preselected; there is no
`CLI default` option). A create with no effort or `default` is refused on the form with the offered list,
and nothing is opened. A row's link is the start of the session's first owner message (whitespace
collapsed, at most 80 characters with `…` when cut, plain text; `no message yet` before the first
message), then its last activity (the latest of creation, any turn's start or finish, and any feed
entry), the model — the one its last turn reported (`claude-opus-5-5`, said `Opus 5.5`), else the one it
was opened with, the CLI and the exact id under it — its effort (bars and the word; `not set` for a session
stored with `default`), state, whether a turn runs, the short session id and a `close` button. The panel
links to `closed sessions (N)`, `/po?closed=1`, which lists closed sessions the same way with their
`closed_at` instead of state and turn, and links back to the open ones. A session page names its model
and effort beside its title and shows the owner's messages, the PO
head's final answers and each turn's state (`running`, `completed`, `failed` or `interrupted` with its
reason), newest first, under a message box (Enter sends, Shift+Enter inserts a newline; the form goes
out once until the page reloads). **Every control is one row under that box, never at the end of the
feed**, which the newest-first order puts behind the whole scroll: `send` opens the row, and at its far
end, set apart from `send`, come `stop turn` while a turn runs, `close` while none does, and `new
session` always. `close` and `new session` are forms of their own and HTML has no nested form, so they
stand beside the message form and `send` reaches it by `form="po-send"`. `close` posts to
`/po/sessions/ID/close` as actor `owner` and returns to `/po`; pressed while a turn runs (from a list row)
it renders the session refused (409) and closes nothing. `new session` posts the `/po` form's own route,
`POST /po/sessions`, with the CLI, the model and the effort of the session being read and the page's request id
suffixed `-new-session` (one id belongs to one operation, so the create never takes the id the message
box carries); it opens a second session and leaves this one exactly as it was — sessions run side by
side, and nothing here closes one. A pair the installation no longer offers is refused the way the `/po`
form's create is, on `/po`, with the list of what it does offer. A closed session's page stays readable, says
`closed AT by owner`, and has no message box, no `send` and no `close` — only `new session`; a message
posted to it anyway is refused (409 `session_closed`). The running-turn count counts turns, whatever their session's state. The PO head's answers are rendered
server-side as a safe Markdown subset (headings, emphasis, code, lists, quotes, rules, `http(s)`/`mailto`
links; the text is escaped first, so raw HTML shows as text); the owner's messages are shown as typed.
While a turn runs or a message is queued the page
polls `/po/api/sessions/ID` every 3 seconds and reloads when the turn ends or the queued message starts. A
message sent while a turn runs is queued: it shows at the top of the feed marked `queued` and becomes the
next turn when the running one ends. Each form carries a request id minted when the page was served. It belongs to one
operation with fixed inputs, installation-wide: `po_requests` binds it to a session create (CLI,
model and effort; a `default` effort is left out of the fingerprint, so an id recorded before efforts
existed still binds the same inputs) or a send (session and the exact text), and is written in the transaction that creates the
session or the turn. Sending the same form again answers with what it made — the session, the message still
queued, or the turn running, completed, or failed with its reason even when its CLI never started — and
writes and launches nothing. The same id reused for anything else is refused (409 `request_conflict`). A queued message
has no request row until its turn is claimed; its id is bound by the queue file meanwhile. The dashboard's pipeline strip shows only the number of running PO turns
with a link to `/po`; it needs no token, and a PO store that does not answer hides the number.

**Models and efforts.** `instance.yaml`:

```yaml
po:
  models:
    claude: [fable, claude-opus-5-5]
    codex: [gpt-6-astra, gpt-6-sol, gpt-5.6-terra]
  efforts:
    claude: [high, low, medium, xhigh, max]
    codex: [high, low, medium, xhigh]
```

Without `po.models` or `po.efforts` the product default is exactly that list. The first entry per CLI is preselected in
the new-session form: `fable` and `high` when Claude is chosen, `gpt-6-astra` and `high` when Codex is; a model
id is shown as people say it (`claude-opus-5-5` is `Opus 5.5`), the id stays the posted value. A CLI left
out keeps its default; an empty list offers that CLI nothing. Creating a session with a CLI, model or
effort outside the list is refused (400), and so is a create with no effort or `default`: a listed
`default` is dropped from the offered efforts. A sprint's session opened by the PO service reuses the
previous session's CLI, model and effort, taking the CLI's first offered effort instead when that one is
`default` (or no longer offered), and with no previous session opens at the form's preselection. `new
session` on a session page sends that session's effort, or for a `default` session the CLI's first offered
one, and says so beside the button. The lists are read on every request, so an edit needs no restart.

### Read-only checkpoint and quiet-tick check

Observe an ordinary, already-authorized board transition; do not create a card change, invoke
`production-tick`, change a timer or force a push for this check. Before and after the next scheduled
tick:

```bash
ummanu status --json --instance INSTANCE
ummanu dispatcher production-observe --instance INSTANCE
ummanu doctor --instance INSTANCE
journalctl --user -u ummanu-dispatcher-production.service --since "TIME"
```

A changed normalized board or run export gives one local checkpoint commit at the end of that tick
(not an immediate push), visible under `checkpoint` in `status` and `production-observe`; `doctor`
reports a blocked gate, push failure, lag or divergence.

A quiet tick shows no card transition, `unchanged` checkpoint evidence, and for every
`observer-reconcile` result only `observer-live`, `observer-waiting` or `observer-idle` (or none). Any
other observer action — delivery, launch, relaunch or adoption, or an unrecognized one — fails the
observation.

## Status and doctor

`ummanu status --json --instance INSTANCE` is the read-only operational snapshot and safe to poll.
It reports managed services and timers, projects and heads, active dispatcher attempts (workspace,
watchdog liveness, progress, respawn state), sprint observers, pause state, checkpoint freshness,
memory index state, and host disk, memory and load. A live run reads each head's liveness the way the
dispatcher's watchdog does (`command_terminal_status`: the pid heartbeat and the exact-run provider
cursor) into the watchdog's `panel` field; `--offline` reports it as `not-probed`.

`ummanu doctor --json --instance INSTANCE` evaluates invariants over the same snapshot and exits
non-zero for a broken or unavailable host. Use `status` for what is running and `doctor` for what
needs repair.

Doctor measures free bytes on the filesystem containing the configured `data_dir` (or its nearest
existing ancestor). Below 10 GiB it reports `root_disk_low` with `free_bytes` and
`threshold_bytes`; a failed or malformed probe reports `root_disk_unavailable` and exits as
unavailable. The threshold and the seven-day build-cache age are defined once in
`ummanu.infra.host_space_policy`. Offline doctor skips the host probe; dry-run remains read-only.

The `recovery` object is shared with doctor:

- `resources` lists every resource in the installed head registry. `source` separates a fresh
  `dispatcher-cache` verdict, a `live-read-only-probe` and an unavailable observation; `freshness`,
  `observed_at`, `age_seconds` and `observed_state` expose staleness. Status never probes; doctor runs
  bounded read-only probes unless `--offline`. Neither writes the dispatcher cache.
- `credential_consumers` lists the managed checkpoint consumer (provider CLI logins stay unmanaged)
  and one `project-git:<project>` row per registered project with a checkout: `transport`,
  `managed_readiness`, `state`, `source`, `supported_next_action`. GitHub HTTPS rows take the managed
  state; local rows are `not-applicable`, SSH and other non-HTTPS `ambient/manual-bypass`, other HTTPS
  hosts `refused`. Consumer readiness does not borrow the last push outcome, and a locked store is
  `locked/unverifiable`. While any project uses an unmanaged HTTPS origin, doctor says to keep an
  ambient credential helper.
- `paths`, `materializations`, `catalog_envelope_divergences` and `bypasses` are metadata-only; every
  unsupported row carries `supported_next_action`.

`doctor` raises secret-store findings when catalog and values diverge, when the key is missing or
unusable with a non-empty catalog, or when the key is wider than `0600`.

With the production dispatcher enabled, the observer-root repository under the data directory belongs
to the installation. It is created on the first observer launch, so its absence on a fresh
installation is no finding. `reconcile` neither creates nor deletes it.

Timer-started oneshot units are neither required enabled nor active; their state is still
reported. No unit outside the installation's own is expected or probed; before A20 step 9 the
host-owned Orca unit was ([Head runtime](HEAD_RUNTIME.md)).

Doctor is also the writer of the owner's `provider_red` events ([Owner events](#owner-events)): a
resource whose probe verdict is `unauthenticated`, `exhausted`, `unavailable` or `probe_broken` puts one
notice on the bell per resource, verdict and UTC day. `--dry-run` writes none; nothing else of doctor
writes anything.

## Owner events

The bell in the dashboard's header counts the owner events nobody has read; `/owner-events` lists them
([Protocols](PROTOCOLS.md#owner-events-and-the-bell) has the entity, the producers and the rules). The
same list from a terminal, read-only through the board store's read role (it marks nothing read):

```bash
python3 -P -m ummanu owner-events list --instance INSTANCE            # open needs-the-owner first, then newest first
python3 -P -m ummanu owner-events list --instance INSTANCE --unread   # only what nobody read
python3 -P -m ummanu owner-events list --instance INSTANCE --json     # {unread, events: [...]}
```

Each line is `*` for unread, `#id`, the moment, the class, the kind and the subject, then the text.
Exit status `1` means the board store did not answer or has no `owner_events` table yet (migration
`0018` not applied: `the board store has no owner_events table yet`).

What each kind means:

| kind | class | means | what to do |
| --- | --- | --- | --- |
| `card_handed_to_owner` | needs the owner | the PO handed a `decision`/`operation` card to you with a reason | answer on the card page (the comment form posts as the owner) or in the sprint's PO session; it clears when the PO completes the card |
| `card_waits_for_person` | needs the owner | an unresolved sprint card is Blocked awaiting a decision, or a decision/operation card is with the PO | decide or complete the card; the event stays unread while that wait holds and settles when the card leaves it |
| `steward_needs_human` | needs the owner | the steward's report card went Blocked with a "Needs a human" section | read the report card; mark the event read once handled |
| `e2e_budget_spent` | needs the owner | a code card outside every sprint, cut by nobody's PO session, spent its 3 e2e runs and was Blocked (or, for an after-merge project, was left out of the after-merge run) | re-cut the work in a sprint with an e2e budget, or through the PO; mark the event read once handled |
| `e2e_after_merge` | needs the owner | an after-merge e2e run on `main` ended with no verdict (cancelled, timed out, deadline passed, unreadable, refused, not identified, ran on another SHA), and its cards are pending again; or a red one's hotfix card had no open sprint and no PO session to go to and is Blocked | read the run and the comment on the cards; for a Blocked hotfix, decide who fixes it (unblock it into a sprint, or hand it to the PO); mark the event read once handled |
| `sprint_closed` | notice | a sprint was closed | read its closeout on the sprint page |
| `sprint_stopped` | notice | a sprint's budget reached the hard limit and it was stopped | decide whether to reopen it |
| `budget_signal` | notice | a sprint's budget reached its signal threshold | look at why its cards keep going round |
| `observer_dead` | notice | a sprint's observer head is dead and the tick did not relaunch it (backoff, drain, a failed bring-up) | `ummanu status`; the dispatcher retries after the backoff |
| `head_dead` | notice | a worker or reviewer head died or stalled again after its one respawn, or its respawn failed; the card is Blocked | read the card's Blocked reason |
| `po_turn_failed` | notice | a PO turn ended `failed` (its subject is the card a dispatcher input was about, else `po-session:<id>`) | read the turn on `/po`; for a handed-over card, a new owner comment sends the answer again |
| `provider_red` | notice | doctor found a provider's key expired or login missing, its quota spent, its provider down, or its probe broken | fix the login or wait for the quota |

A notice is marked read by its button or by "Mark all notices read"; an event that needs the owner is
read by a click only when its card does not wait for the owner, and otherwise stays unread until the
card leaves `waiting_owner`. Advisory notice producers log a failed event write and go on.
Authoritative card waits require the event store: creation, replacement or settlement failure
refuses the enclosing mutation with a typed backend cause. PostgreSQL rolls back the card,
metadata, comments and occurrence together; restore the store and retry the same request ID.

The dashboard's sprint attention chip takes scoped open `needs_owner` event IDs from the same
SQL statement snapshot as the bell count and list. A card subject is joined through its sprint
foreign key; a superseded or archived card cannot fabricate a human wait. Page responses pin this
reading, so a settlement during rendering cannot split the chip from the bell. GETs write no events.
Routine observer, CI and wait-card waits are neutral. An unavailable event/card source and an
unknown sprint state do not mean attention.

`TaskWriter._transition_card` publishes `card_waits_for_person` in the card transition transaction,
with a dedup key containing the transition request ID. The same path settles this kind on leaving
the wait. PO handover inserts the established `card_handed_to_owner` event, settles the PO wait,
and writes the mark/comment in one transaction with its handover occurrence;
completion clears the handover mark and settles its events in the transition transaction. Other
open needs_owner events retain their existing click/settlement behavior. Bell count includes genuine
unread notices as before, but notices cannot cause a sprint chip.

Revision `0025_card_waits_for_person` adds only this constrained kind and backfills existing unresolved
open sprint waits without an open needs_owner event. It preserves old events and skips archived,
superseded and machine-wait cards. It declares `release_safety = "additive"`; the previous 0024 runtime
can still read the newer schema, load the added kind as an OwnerEvent, and write its existing kinds
and card records. Applying/releasing it and restarting production services are PO/dispatcher work.

## How long things take

Three durations are recorded on every running installation, and one command measures the dashboard
against the thresholds a sprint is judged on.

### Where the durations are

| what | where it lands | how to read it |
| --- | --- | --- |
| one web request | `journalctl -u ummanu-web.service` | `127.0.0.1 GET /sprints 200 4612.3ms` — client, verb, request target, HTTP status, and the milliseconds the application spent on it. One line per answered request, including a HEAD, a refusal and a contained 500. |
| one dispatcher tick | the dispatcher journal, and `ummanu status` | `dispatcher.last_tick` carries `duration_ms` beside the outcome already recorded for that tick (`seq`, `at`, `status`, `healthy`, `actions`). The human `ummanu status` prints it as `last tick: #12 ok at ... in 4322 ms`. |
| one checkpoint run | `ummanu status` | `checkpoint.checkpoint_duration_ms`, printed by `ummanu status` and by `ummanu doctor` as `checkpoint: committed in 2100 ms`. Every outcome carries its own number, including an unchanged run and a blocked one — a no-change checkpoint still regenerated the whole projection. |

The web duration is the application's part of the answer — reading the body, handling the request,
and writing the headers and body back — not the whole socket lifetime. The tick duration is the
wall clock of `production_tick` up to the moment its outcome became durable.

### Measuring the dashboard

```bash
python3 scripts/measure_dashboard.py            # against http://127.0.0.1:8787
python3 scripts/measure_dashboard.py --json     # the same facts, as one document
```

Run it from a checkout, on the host the installation runs on, as the runtime user. It reads only,
and only from the installation named by `--base-url`: three dashboard pages and, to reproduce what
an operator's browser is doing, the `/po` overview and one session's JSON. It makes no POST and
starts no head. It also cannot be sent anywhere else: its one opener follows no redirect — a 3xx
from any route ends the run instead — and reads no `http_proxy`/`https_proxy` variable, because
either would time a different installation under this route's name and hand it this installation's
PO cookie.

It prints, with the Definition of Done threshold beside each number and whether that number meets it:

- **warm sequential** `GET /`, `GET /sprints` and `GET /projects` — one discarded warm-up request,
  then twenty timed ones per route. The judged number is the **p95 by nearest rank**, which over
  twenty samples is the second-slowest of the twenty: one request that actually happened, so two
  runs compare the same way every time. Min, median and max are printed beside it, and the warm-up
  is discarded because the first request after a deploy pays for import and cache warming that no
  later request pays again. Threshold: 1.0 s.
- **four concurrent** `GET /`, all four released together, while a `/po/api/sessions/{session}`
  poll runs on the three-second cadence the `/po` page itself uses. The scenario is repeated
  **three rounds**; every round's four durations are printed, and the threshold is judged on the
  **worst round** — the one holding the slowest single request. One round is not a measurement:
  the same unchanged installation produced 17 s, 27 s and 38 s on three runs of the first version
  of this script, and the DoD says *each* of the four answers within 2.0 s, so a scenario that
  breaches that in one round of three has not met it. Threshold: 2.0 s for each request of the
  worst round.

The session it polls is not just any open one. The `/po` session page installs its three-second
poll only while a turn is running and clears it when the turn ends, so the command picks the first
session the overview lists **whose own JSON reports `running: true`**, read at the route that would
be polled. An installation where nothing is running is one where no page is polling, and the
concurrent half cannot be reproduced on it at all — see the exit statuses below.

### The cadence, and how a round is scheduled against it

The concurrent number is only the DoD's number if the four requests ran *while* a session was being
polled every three seconds. Two things in the output say whether that happened, and the command
refuses to judge anything if either of them says no.

- **The cadence is an independent schedule, and it is observed.** The page polls with
  `setInterval(async () => { await fetch(...) }, 3000)`, which starts a read every three seconds
  whether or not the previous one has answered. The command does the same: poll *k* starts at
  `anchor + 3k` s on a thread of its own, so a slow session read neither delays the next start nor
  stretches an interval, and slow reads overlap each other as they would under the page. Every
  poll's actual start is compared with its scheduled one, both ways, and the run is refused if any
  is more than 100 ms off. The output prints the spacing and the furthest offset
  (`start against schedule: furthest +1.2 ms …`) and says `the poll started every 3 s` only where
  those numbers show it.
- **Every round is launched by a poll that fell due, while it is in flight.** The four requests
  wait for the next poll on the cadence; when that poll's clock starts they are released, together
  with the poll's own request, so all five go out at once. A round therefore costs up to three
  seconds of waiting before its first clock starts — that wait is in no number — and the ordering
  holds at any installation speed, because it is the order one thread does two things in. A round
  that no poll was issued for is refused.
- **Every request starts under a poll.** For each of the four requests the command records how many
  polls were in flight when it started. A round counts only when every request had at least one; a
  round that did not is printed as `not counted` and taken again, up to nine attempts for the three
  rounds, and a run that cannot collect three is refused.

Each round's line reports both facts:

```
round 2: 2102, 1976, 2034, 2004 ms [launched by a due poll; polls in flight at each start: 1, 1, 1, 1; 1 during the round]
```

`--no-poll` takes the same three rounds of four concurrent `GET /` with no poll beside them, and
labels every number `no poll`: the baseline an under-the-poll run is compared against, so the
difference the poll makes can be read off two runs of one command.

### The load is never lighter than the page's

The concurrent poll is held to a one-sided standard: **it is a load no lighter than an open `/po`
page on a running turn, so a number that meets its threshold under it is sound, and one that
exceeds it may be conservative.** The command prints that sentence with the poll it chose. It
need not be an exact copy of the page — a heavier load can at worst turn a pass into a
conservative fail, never a fail into a false pass.

That standard is why a turn that **ends during the run** is reported and not refused. The page
clears its timer when a poll answers `running: false`, which is zero load from then on; the command
keeps polling on schedule, which is more. It reads `running` from every poll and prints one line
naming where it happened — `the turn ended during round 2 (a poll answered running: false);
polling continued on schedule, at least as heavy as the page, which stops polling there` — and the
run stands. The session still has to be running when it is selected: that is what makes it a
session a real page is polling at the start.

### What it refuses to report

The rule the whole script is built around: **no number is judged MEETS unless it came from exactly
the scenario the DoD names.** So the exit statuses are

- `0` — every request answered 2xx, the whole specified scenario ran, and every judged number is at
  or under its threshold;
- `1` — the same, except that a judged number is over its threshold. A red number is still a real
  number, and the full table is printed;
- `2` — everything else.

Everything else includes: the installation is unreachable; any request on any route answers
non-2xx, including a 3xx (a missing route, a refusal, a redirect, or the transport's contained 500
under the load being measured); the data directory or the PO token cannot be resolved or read; a
session's JSON cannot be read for whether it is running, at selection or in any later poll; a
poll started more than 100 ms off its three-second schedule, either way; a round ran with no poll
issued for it; the installation has **no open PO session**; and
the installation has open sessions but **no turn running in any of them**.

Those last two are not faults of the installation — `/po` answered, and simply lists nothing, or
lists only sessions no page is polling. The concurrent half of the DoD cannot be reproduced without
a running session, and polling an idle one instead would put a request no page makes beside the
four and report it under the same heading. In every case the command prints the numbers it did
take, marks every one of them `NOT JUDGED`, says in one line what could not be measured, and exits
2. For the two PO cases, run it again while a PO turn is running.

When no turn is running and a number is still wanted, `--poll-idle-session` measures beside a
stand-in: of the open sessions, the one whose JSON is largest — the heaviest read the page's poll
makes on this installation — polled on the same cadence. The output names it as a `SUBSTITUTE`
and every concurrent label carries `substitute poll`, so such a run is never read as the DoD
scenario itself. An installation with no open session at all still exits 2.

The PO poll needs this installation's PO token (`DATA_DIR/po-web-token`, mode 0600), so run the
command as the runtime user. The data directory is resolved the way the product resolves it:
`--data-dir`, then `UMMANU_DATA_DIR`, then `UMMANU_INSTANCE`, then the instance the CLI
itself defaults to (`ummanu.onboarding.DEFAULT_INSTANCE`, read through
`ummanu.config.instance_data_dir`). That last step is what makes the one command above work in
an ordinary checkout shell, which does not inherit the service unit's environment. The output names
the directory it resolved and which of those four rules chose it.

## Record reconciliation and controlled divergences

Before advancing cards, every production tick walks the dispatcher records whose card is not among
the active (In progress / Validate) cards, re-reads that card from the board immediately before
acting, and drops the record if the card really is out of the cycle (`record-removed` with the
reference and state). It touches bookkeeping only: the workspace and terminal stay, and dealing with
them is the PO's decision. If the board is unavailable the record is left for the next tick.

A controlled divergence records a board answer the dispatcher did not expect (a claim mismatch and
similar) with expected and actual values. While its card stays active it is open: `status --json` and
`doctor --json` list it and `doctor` raises an unresolved-controlled-divergence finding, including
under `--offline`. The same reconciliation pass closes it, with time and reason, once the card leaves
the active cycle.

`dispatcher.divergences` carries the open count, total and open items (reference, reason, open time).
`dispatcher.reconciliation` carries the tracked record count, the last tick finish time and the last
reconciliation pass time (`null` until a pass has run).

## Connecting a project: gate and stale-input recovery

Stage contract, identity fields and re-onboarding semantics are in
[Protocols](PROTOCOLS.md#connecting-a-project). This is the order of work.

### Identity and mutable binding fields

Identity is `id`, `repo`, `adapter`, `default_branch`, repeated verbatim by the draft, the provision
task and the gate result; the provision result carries only `id` and `adapter`, and provisioning
rejects a mismatch as foreign. `plane`, `policy.code_concurrency` and the other mutable fields carry
over on a repeat `project add`, so refreshing a draft does not reset routing.

`project add` writes no `orca_binding`: a new project runs on local-pty heads in git workspaces.
`orca_binding` is optional legacy (A20 step 8). An existing one is kept; only curator routing reads
it, and card placement never does (secretary-1722).

### Stale input or an invalid schema

Validity is checked first, freshness second.

A schema-invalid input never mentions HEAD: `project add` answers `draft.invalid`, and
`provision-*` and `gate` answer `draft_invalid` and publish nothing. Errors name a schema path. A
failed `project add` prints diagnostics but writes nothing; fix the source the errors name.

Stale input means the default branch gained a commit after the draft was written.
`provision-start` and `provision-apply` answer stale with the expected and actual scanner heads; the
gate publishes a stale result with a `stale.input` finding. The gate reports a conflict for other
desyncs: provisioning not drafted, an unreadable or invalid canonical adapter, or an enabled binding
with no matching passed result.

To tell them apart, compare the scanner head in `<data>/onboarding/adapter-drafts/<id>.yaml` with the tip of the
default branch. Different: stale, recover below. Equal and still refused: the named artifact is at
fault; do not loosen the guard, schema or policy.

Five `not-run` checks appear only when the gate on a fresh disabled draft saw HEAD move before building
its worktree; a stale result published mid-run keeps completed checks. A stale result on an enabled
binding exists only in command output.

### Refreshing a disabled draft

Do not edit instance files by hand; each stage rewrites its own artifacts.

```bash
python3 -P -m ummanu project add PROJECT_PATH --instance "$INSTANCE"
python3 -P -m ummanu project provision-start PROJECT_ID --instance "$INSTANCE"
# the provision agent writes result.yaml next to task.yaml, taking run_id and scanner head from the task
python3 -P -m ummanu project provision-apply PROJECT_ID --instance "$INSTANCE"
python3 -P -m ummanu project gate PROJECT_ID --instance "$INSTANCE"
```

A clean run: `project add` prints an ok scanner status and pending provision; `provision-start`
answers `task_ready` with the `task.yaml` path; `provision-apply` answers `drafted` with the binding
disabled; `gate` answers `passed`. Exit 0 only for success; any refusal exits 1.

There is no commit step. The binding (`projects/<id>.yaml`) and the canonical adapter
(`adapters/<id>.yaml`) are written into the live root, and the next snapshot exporter window carries
them ([Recovery](RECOVERY.md#writers)).

- `project add` rescans. If HEAD changed, provision and gate state reset to pending and the stale
  canonical adapter is deleted in the same transition. Uncommitted project changes are not read.
- `provision-start` is idempotent per run id (a digest of identity, scanner head and onboarding
  cycle).
- `provision-apply` reads `--result PATH` or the default path, rejects a foreign run id or scanner
  head, publishes the canonical adapter and keeps the binding disabled.
- `project gate` builds a temporary worktree at the recorded head, runs setup, smoke and validation,
  and is the only stage that enables the binding.

`project add` on an enabled binding refuses ("existing binding is enabled"). Run `project gate` on it
first: a stale input clears the enable and returns the project to the disabled state recovery starts
from.

A disabled binding on another adapter (typically `adapter: inventory-only`) is moved onto the
project's adapter by `project add` and stays disabled; its provision and gate state reset, a stale
`adapters/<id>.yaml` is deleted. An enabled binding on another adapter refuses, with `--re-onboard`
too ("existing binding has conflicting adapter").

### Re-onboarding an enabled legacy project

A project with `enabled: true`, a canonical adapter and no draft, provision run or gate result makes
the gate refuse with "enabled binding has no matching passed gate result". `--re-onboard` is the only
supported way out; do not edit the binding or adapter by hand:

```bash
python3 -P -m ummanu project add PROJECT_PATH --re-onboard --instance "$INSTANCE"
python3 -P -m ummanu project provision-start PROJECT_ID --instance "$INSTANCE"
python3 -P -m ummanu project provision-apply PROJECT_ID --instance "$INSTANCE"
python3 -P -m ummanu project gate PROJECT_ID --instance "$INSTANCE"
```

Identity must still match and the binding must validate; otherwise the command writes nothing and the
enable stays. `plane`, `policy`, `remote` and `orca_binding` carry over. On an already disabled
binding the flag does nothing.

A refusal or I/O error restores every touched file. After a crash or kill, rerun:

- killed after the draft, before the binding: the enable is still in place; rerun `project add
  --re-onboard`;
- killed after the binding, before the adapter delete: the binding is disabled; a plain `project add`
  deletes the stale adapter.

### Verifying the result

Read the three artifacts. Drafts, provision runs and gate runs are generated state under the data
directory (`$DATA`, `instance.yaml` `data_dir`); the binding is configuration in the instance:

```bash
cat "$DATA/onboarding/gate-runs/<project>/<run-id>/result.json"
cat "$INSTANCE/projects/<project>.yaml"
cat "$DATA/onboarding/adapter-drafts/<project>.yaml"
```

A passed result has empty findings, the four identity fields, scanner head and provision run id, the
adapter digest, and five passed checks: `clean_worktree`, `setup`, `smoke`, `validation`,
`artifact_policy`. The binding holds the identity and `enabled: true`; the draft's gate block is
passed with the same checks.

Verify by reading, not by running `project gate`: on an enabled binding it can change state.

- HEAD and adapter digest unchanged: returns the published passed result, exit 0, no change.
- HEAD moved, or the adapter was rewritten: clears the enable, prints a stale result, exit 1.

A clearing rewrites only the binding (disabled) and the draft (gate block failed, `stale.input`, five
`not-run`); the older passed result file stays on disk and the printed stale object may still show
passed checks. A result file describes its run, not current freshness.

### What this lifecycle does not prove

The gate does not check forge configuration. Without an explicit validation command it runs `git diff
--check HEAD`; a passed validation does not mean branch protection exists.

The mechanical gate reads `validation.required_checks` from the adapter:

- set: only those names (Actions check-run name or legacy status context) colour the card; missing
  or unfinished leaves it pending for the pending watchdog, a failed one makes it red, all successful
  make it green. Other checks do not matter;
- unset: every check on the SHA counts and any failure makes it red.

## Changing installation config

Installation config is the live root's exported files: `instance.yaml`, `projects/`, `adapters/`,
`heads/heads.toml`, `persona/`, `skills/manifest.toml` and the curated `state/knowledge` and
`state/memory`. The live root is not a code project. No card branch lands in it: a card whose project
repository resolves to the live root is refused at admission ("project 'ID' names the live root PATH
as its repository"), before any workspace or head exists, and a release refuses it again (`nothing
was merged: ...`). Project registration has its own commands
([Connecting a project](#connecting-a-project-gate-and-stale-input-recovery)).

Any other config change is an operation card, executed in place, then:

```bash
python3 -P -m ummanu config check --instance "$INSTANCE"
```

It needs no Git and reads the live root the same with or without `.git`. Two checks:

- schema validation: `instance.yaml`, every binding, adapter and onboarding draft, and the data
  manifest, the same read the dispatcher makes every tick;
- the old-name guard ([Rename](RENAME.md#t5-guard)): every exported path and text file, as the next
  cut copies it. The old product name passes only where an allowlist row covers it: the Hermes agent's
  own spellings, `secretary-instance`, old card refs, the `source:`/`supersedes:` lines of memory
  facts, and whole-file history (`state/knowledge`, except undated runbooks and `plans/current-*`).

Files outside the export allowlist (`runtime.env`, `board-store.env`, `secrets/installation.key`,
generated heads and onboarding state) are never opened. Each finding is one line on stdout and any
finding exits 1:

| finding | meaning |
| --- | --- |
| `schema: <file>: <field>: <message>` | the dispatcher would refuse this config; fix it first |
| `<path>:<line>: '<match>' in '<text>'` | the old name in an exported file, outside the allowlist |
| `<path>: path carries '<match>'` | the old name in an exported path |
| `export: ...` | a symlink or non-regular entry at an exported path; the exporter would block on it too |

A clean root prints `ummanu config check: ok (N exported file(s) in PATH)` on stderr and exits 0.
There is nothing to commit: the next exporter window carries the change.

## Starting a sprint

A person starts a sprint through an interactive ummanu session using the ummanu role skill
`open-sprint` (delivered by `ummanu role-skills sync`, in both Claude and Codex targets). The skill
gathers live context, checks that no other open sprint holds the needed repositories, interviews on
unresolved product forks and fixes a checkable Definition of Done. The goal is the person's choice.
A sprint needs its Product, at least one open Issue and at least one reserved registered project; an
installation holds one open sprint unless [two open sprints](#the-two-sprint-pilot) are enabled, and a
project another open sprint reserves is a resource conflict.

```bash
python3 -P -m ummanu sprint create --role po --actor <actor> \
  --goal "<one sentence>" --dod-file DOD.md \
  --product <product-id> --issue issue:<ID> --project <project-id> \
  --observer <head-profile|none> \
  --repository <repo> [--repository <repo>]
python3 -P -m ummanu sprint show --ref sprint:<ID>
python3 -P -m ummanu sprint status --ref sprint:<ID>
```

After that the sprint is not driven by hand: the production tick launches the observer head,
intervention goes through [comments on the entity](#a-po-comment-on-a-running-sprint), and status is
read with `sprint status`, `sprint list` and `task list --sprint`. The sprint entity is checkpointed
and restored with the cards ([Recovery](RECOVERY.md#what-the-checkpoint-contains)).

Goal, Definition of Done, repositories, status, budget, current card and resume are entity fields; a
knowledge document holds only the "why" and a pointer to the sprint reference.

## What is running right now

```bash
python3 -P -m ummanu sprint list                      # every sprint, with what each is doing
python3 -P -m ummanu sprint list --status open        # only the ones that are open
python3 -P -m ummanu sprint status --ref sprint:1431  # one sprint, plus its own fields
```

Both are reads over the same protocol operations and print one JSON document; the listing's
`sprints.items` entry and the watched sprint's `work` are the same object. Read it in this order:

1. **`status` and `current_task`.** `sprint status` prints the sprint's `status` as its first key.
   A closed sprint has no current card: `current_task.ref` is null (as it is in `sprint show`). A
   stopped sprint keeps the card it stopped on with `live: false`.
2. **`waiting.state`** (`working`, `waiting`, `blocked`, `ended`, `unknown`) with `waiting.reason` and
   `waiting.source`. `unknown` means the card is in an active column and whether a head is behind it
   could not be established; the reason still names the column.
3. **`checks`** — the recorded mechanical gate for the current card: `green` (with
   `gate.attested_sha`), `not_green` with reason, `unknown`, or `not_applicable`. Nothing is re-run.
4. **`decision`** — the observer's last resume `entry` and its `freshness`.

### Reading an answer that is only partly available

Every section carries a `source` (`available` with read time, or `unavailable` with reason and
evidence age) and `source.name`. `unavailable` means "nobody could say", never "nothing". The top-level
`cards.source`, `journal.source`, `liveness.source` and `installation.source` say which sources
answered. Which section each source can take away is in
[Protocols](PROTOCOLS.md#one-place-says-which-source-answered) and
[Protocols](PROTOCOLS.md#what-a-sprint-is-doing).

- `checks.state: unknown` with an `unavailable` source: production state unreadable; with an
  `available` source: no dispatcher record for the card yet. It never means the gate failed.
- With an invalid config, an explicit `--data-dir` and a reachable board still answer; without
  `--data-dir` the command exits `1` with `backend_unavailable`.

Exit statuses: unknown sprint or malformed filter `2` (`not_found` / `validation`), a refusing source
`1` (`backend_unavailable`). `ummanu sprint show --ref` reads the entity record, comments included.

### A card waiting on its e2e run

A code card of a project that declares `validation.e2e` waits for its e2e run after green CI and its
review ([Protocols](PROTOCOLS.md#the-e2e-stage)). While it does:

- the code card stays in **Validate** (in **Assessment** when a release decision is waiting for a run);
  its heads are not re-launched and its gate is not re-read;
- `task show --ref <card>` carries `e2e`: `runs_dispatched`, the sprint whose e2e budget it spends
  (`budget`) or, outside every sprint, its own `run_cap` (3 plus the owner's raises), and per run the
  SHA, the dispatch id, `state` (`dispatching`, `identifying`, `waiting`, then the conclusion or the
  wait outcome), the run link and the wait card;
- a **wait card** titled `E2E run <workflow> for <card> @ <sha>` sits in Ready, then In progress, in the
  card's sprint; `task show` of it carries `wait` with the run link, the deadline, `last_observation`
  (`run <repo>#<id> is in_progress`) and `last_error`;
- the dispatcher's tick outcome for the card is `e2e-identifying` (the run is not named or its SHA not
  checked yet; at most 15 minutes; when GitHub's answer was lost, `recovery_settles_at` says when the
  lookup may attach a run) or `e2e-waiting`, with `run`, `wait_card`, `deadline`, `observation` and `runs_dispatched`.

When the run concludes, the wait card goes Done (or Blocked for a missed deadline, an unreachable run
or a cancel) and comments `[wait:<outcome>]` on the code card, and the code card moves on the next
tick: Assessment or the release on `success` (with a `## E2E — green` comment), In progress for rework
on `failure`, Blocked on anything else. A newer base merged in the meantime does not cost a new run
when only base history came in and the card's own paths are unchanged: the attestation then carries an
`E2E/base reconciliation` line. To give up on a run, `task cancel` its wait card; the code card
is then Blocked. A Blocked card brought back on the same SHA dispatches a new run, charged like any
other.

`.github/workflows/e2e-synthetic.yml` is a synthetic e2e workflow kept for the sprint:1469 live proof:
it runs only on `workflow_dispatch`, checks out the dispatched ref, sleeps `minutes` minutes (1–110,
default 61) and concludes as its `outcome` input says. An adapter would declare it as
`validation.e2e: {workflow: e2e-synthetic.yml, inputs: {minutes: "61", outcome: success}, candidate_input: candidate, deadline: 3h}`.
Declaring it makes every code card of that project wait about an hour and spend one budgeted run.

### Gate attestation replay

Assessment delivery and release audit share `dispatch/gate_lifecycle.py:accept_green_gate`.
Each delivery first admits a fresh `AcceptedGreenGate` against current HEAD and the declared
gate mode. The latest observation remains in `gate_attestation`; reviewer packets, candidate
selection and sprint gate reads consume that receipt. Comment effects in
`gate_attestation_effects` preserve their original receipt, delivery context, request and body.
Their SHA-256 identity covers candidate, base, mode, every check name/result/URL, the complete
check-set digest, attempt/ref/actor/stage, report and review baselines, reviewed commit and
review/e2e reconciliation facts. Observation time alone does not select another effect.

The dispatcher saves the frozen effect before `TaskWriter.comment`, whose global audit claim
remains strict. After the Assessment attestation commits, the normal exact-generation reviewer
stop must succeed before the write-ahead park intent and its Assessment move. Stop refusal keeps
Validate and the retained worker. A crash before the park intent re-admits the gate and replays
the saved comment bytes. A crash after park intent, or after the move before state save, finishes
the existing park and move identity. Release similarly replays its comment before the separate
merge effect. A frozen comment never supplies current gate admission.

Released old-format comments, including secretary-1883's overwritten observation timestamp,
are supported through authoritative `audit.committed_event` and `TaskReader.show` reads. Adoption
requires successful commented kind, exact request/ref/dispatcher actor/marker, event id, one
original board body with the immutable audit digest, the canonical receipt/reconciliation
grammar and matching delivery context and fresh receipt semantics. The old Assessment key binds
the review baseline; a legacy release comment predating the current review baseline cannot be
adopted for that delivery. An independently admitted new receipt or reconciliation selects a
distinct v2 semantic request. Missing, unreadable, ambiguous, staged or unrelated legacy evidence
refuses delivery. Recovery does not rewrite audit facts or infer bytes from a current timestamp.
New additive dispatcher fields default empty when an older record is loaded; the body grammar
and latest receipt remain compatible with existing reviewers and observer consumers. Refresh
the ummanu dispatcher through its supported upgrade route before continuation; an older
dispatcher does not understand the v2 effect fence and must not resume its mutable writer path.

The caller uses the existing durable refusal/escalation convention: every failure records ref,
attestation stage, exact request, committed kind/ref/event and an operator-action reason in the
card record and degraded tick outcome. Three consecutive identical failures, persisted across
restarts, produce one idempotent operator comment and `gate-attestation-stalled`. The count caps
at three; later ticks create no additional comments or moves. Successful strict replay clears the
episode. Payload bodies, credentials and transcripts are excluded from diagnostics. Restore
authoritative evidence or investigate the conflicting ownership through supported audit reads;
changing a request's persisted audit identity is never a recovery action.

### When the e2e run budget is spent

Every e2e run pays for stands, so each sprint has an e2e run budget: 3 runs unless `sprint create
--e2e-budget N` said otherwise (sprints opened before it have 3). `sprint show --ref sprint:<N>` and
`sprint status --ref sprint:<N>` carry `e2e`, whose `summary` reads `e2e: <used> of <budget>`, with the
cards that spent the runs (`cards`) and each run (`charges`). Contract in
[Protocols](PROTOCOLS.md#the-e2e-run-budget).

When a card of the sprint needs a run and none is left, nothing is dispatched and the card is not
Blocked. What you see:

- a `decision` card titled `E2E budget spent: sprint:<N> — more runs? (money decision for the owner)`
  in the sprint, listing the card waiting for e2e, every run spent with its link and result, and the
  question: raise the budget by how many runs, or no. The PO hands it to you (`task handover`): it
  shows up in the bell as `card_handed_to_owner`;
- the waiting card stays in Validate (or Assessment), and `task show` of it says `e2e: budget spent,
  waiting on <decision card>`. Cards that need a run later join the same decision with a comment on
  it; there is one decision per spent budget.

What you do: answer on the decision card as the owner (the card page's comment form, or `ummanu task
comment --ref <decision card> --role owner --body-file ANSWER.md`) holding exactly one of these two
lines, in any case, with anything else you want to say around it:

```text
e2e budget: raise <N>
e2e budget: no
```

A comment with neither line, with both, or with two raise lines is not an answer. Your comment reaches
the PO session with its event id, and:

- on `e2e budget: raise <N>` the PO runs `ummanu sprint e2e-budget --ref sprint:<N> --role po
  --authorized-by <your comment's event id>` and completes the decision card. The raise is your N and
  nothing else: an `--add` other than it is refused. The waiting cards dispatch on the next tick. The
  PO cannot raise the budget without your comment: the command refuses any authorization but your
  raise line on that sprint's budget decision card after its handover, and each of your comments
  raises once. `sprint show` then reads `e2e: <used> of <budget + N>`;
- on `e2e budget: no` the PO completes the decision card without a raise, and every card waiting on it goes to
  Blocked with the PO's completion text (not charged to the card as a code defect).

A card outside every sprint has its own cap of 3 runs; every dispatch attempt counts, one GitHub
refused included. If a PO session cut it, the same decision card goes to that session, answered the
same way, and the raise is `ummanu task e2e-budget --ref <card> --role po --authorized-by <event
id>`. If nobody's PO session cut it, the card is Blocked with `e2e run cap
reached (3)` and the bell shows `e2e_budget_spent`.

### E2E after the merge

A project whose e2e workflow can only run on `main` (the Codegen mega waits for the releases of its exact
SHA, which only the post-merge CI of `main` publishes) declares `placement: after_merge` in
`validation.e2e`. Contract in [Protocols](PROTOCOLS.md#after-merge). What you see:

- its cards do **not** wait for e2e before the merge: they go through review, Assessment and release as
  before, with no `e2e` wait;
- once a card's merge commit shows **Post-merge CI GREEN**, `task show --ref <card>` carries `e2e` with
  `placement: after_merge` and `state: pending`;
- at most one run per project is in flight. When none is, the dispatcher starts one on the newest green
  merge SHA, covering every pending card merged up to it; each covered card then says `state: covered by
  <run link>`, and the newest of them (the *carrier*) lists the run under `after_merge_runs`, with the
  branch `pipeline-e2e/<dispatch id>` it was dispatched on and the cards it covers. Cards merged while it
  runs wait for the next run, which covers all of them at once;
- a **wait card** titled `E2E after merge: <workflow> on <project> @ <sha>` waits for the run, in the
  carrier's sprint while it is open;
- the run is charged to the carrier's open sprint (`sprint status` counts it in `e2e: <used> of <budget>
  (<n> after merge)`), otherwise to every covered card's own cap of 3, all or none. With nothing left, no
  run starts: one decision card names every covered card (outside a sprint, with each card's cap and which
  ones need a raise, and the `task e2e-budget` command for each), and each covered card says `e2e: budget
  spent, waiting on <decision>` ([below](#when-the-e2e-run-budget-is-spent)). The owner's raise runs the
  whole batch on the next pass; a decision completed with no raise declines every card of the batch;
- `production observe` lists every project's queue under `e2e_after_merge`: `pending`, `in_flight`,
  `covered`, `budget_waits` and `refs_to_delete`.

When the run ends:

- **green**: every covered card gets `## E2E after merge — green` with the run link, the SHA and the
  covered cards, and says `state: green`;
- **red**: one `code` card titled `Hotfix: after-merge e2e red on main @ <sha> (<workflow>)`, with the
  run, the failed jobs and steps, the log fragment, the SHA and every covered card with its merge SHA.
  It goes to the carrier's sprint while that is open (its observer wakes on it), else to the carrier's
  PO session, else it is Blocked at once (`after-merge e2e red, no sprint or origin owns it`) and the
  bell shows `e2e_after_merge`. Each covered card gets `## E2E after merge — red` and says `state: red ->
  <hotfix card>`;
- **anything else** (cancelled, timed out, deadline passed, the run unreadable, refused, not identified,
  or run on another SHA): no hotfix. Each covered card gets `## E2E after merge — requeued` (or `—
  blocked` when the run was never attached), the bell shows `e2e_after_merge`, and the cards are pending
  again: the next run, charged as usual, starts on the next tick. To stop that, pause the pipeline or
  let the budget decision stop it.

The dispatcher deletes the `pipeline-e2e/...` branch once the run was acted on. A branch whose delete
GitHub did not confirm (no answer, or a refusal while the branch still exists) stays under
`refs_to_delete` and is tried again each tick until it is gone; a branch stuck there is one GitHub keeps
refusing to delete, so look at its repository's branch protection.

## What was commanded, and what became of a request

Both are reads: they write nothing and never re-send, retry or repair. Contract in
[Protocols](PROTOCOLS.md#what-has-been-commanded-and-what-became-of-a-request).

### The last commands, across everything

```bash
python3 -P -m ummanu web-read commands --instance INSTANCE
python3 -P -m ummanu web-read commands --instance INSTANCE --limit 20
python3 -P -m ummanu web-read commands --instance INSTANCE --json
```

One line per command, newest first, across cards, sprints, products and issues: who, action, entity,
result. Pass a page's `next_cursor` as `--cursor` for older commands; `(more)` (`has_more`) appears only
when the limit cut the page. `commands: unavailable (...)` with `items: null` means the audit could not
be read; an empty history is `items: []`.

### What happened to a request id

```bash
python3 -P -m ummanu web-read request --instance INSTANCE --request-id ID
```

Use the `web-read request` form when a command failed, timed out or was interrupted, instead of
running it again to find out:

| answer | what to do |
| --- | --- |
| `committed` | nothing. If `staged` is true beside it, run `ummanu task reconcile-audit`; the operation itself is done |
| `pending` | repeat the operation **with the same request id**; a new id starts a second operation |
| `not_found` | the installation never saw it; safe to send |
| `unknown` | the audit could not be read; repair the journal and ask again. Not `not_found` |

`--json` carries the operation-identity table ([Protocols](PROTOCOLS.md#operation-identity-in-one-place)).
Both commands exit `2` with `validation` for an invalid config, a foreign cursor or a missing request
id, `1` with `backend_unavailable` if the layer could not run.

## A PO comment on a running sprint

A PO intervenes in a running sprint with a comment on the entity, not by editing its cards.

```bash
python3 -P -m ummanu sprint comment --ref sprint:1431 --role po --actor <actor> \
  --request-id po-2026-09-06-slow-down --body-file NOTE.md
```

Keep `comment_id` from the output: the read below takes it. `saved: false` means this request id's
comment already existed and nothing new was written or woken. Keep `--request-id` for retries; without
one each run is a new comment. Reusing an id with a different body, sprint, role or actor is refused
with `validation`, exit `2`. A closed or stopped sprint accepts a comment too
([Commenting after the close](#commenting-after-the-close)).

### Reading what happened to that comment

```bash
python3 -P -m ummanu sprint comment-delivery --ref sprint:1431 --comment-id evt_<...>
```

It only reads; redelivery belongs to the production tick. States are defined in
[Protocols](PROTOCOLS.md#what-happened-to-a-comment).

| `delivery.state` | what to do |
| --- | --- |
| `saved` | wait for the tick |
| `waiting` | wait; `batch.stage` says which stage |
| `handed_over` | nothing |
| `error` | the dispatcher retries; investigate the head if `batch` failure counts keep climbing |
| `not_deliverable` | nothing; the sprint has ended |
| `unknown` | read `delivery.source` and `delivery.reason` before concluding anything |

`handed_over` means the batch covering the comment was acknowledged, not that the observer read or
acted on it; `acceptance.established` is always `false`. To judge that, read the next resume entry
(`ummanu sprint status --ref sprint:ID`, `decision.entry`). Only a `po` comment wakes the observer;
other roles' comments ride along with a later significant event.

## Closing a sprint

Write two files first.

**The decisions file** states what became of every declared issue and every card outside Done; a
close missing one is refused before anything is written. Shape and vocabulary:
[Protocols](PROTOCOLS.md#the-decisions-a-close-carries).

```yaml
issues:
  - ref: issue:1445748c5a2508769ef5
    verdict: resolved
    reason: the sprint's own cards landed it
  - ref: issue:9eee1d8ee505bc4ecdc2
    verdict: open
    reason: not reached before the sprint ended
cards:
  - ref: secretary-1573
    verdict: drop
    reason: superseded by secretary-1577
```

**The closeout file** is your prose account of what was achieved, what is unfinished and what you
decided about the remainder. The close writes it into `state/knowledge` and links it to the sprint.

```bash
python3 -P -m ummanu sprint close --role po --actor <actor> --ref sprint:1431 \
  --request-id close-2026-09-07-1431 \
  --reason "the goal is reached far enough to cut the next sprint; the rest is deferred" \
  --decisions-file DECISIONS.yaml --closeout-file CLOSEOUT.md
```

`result.close` carries issue verdicts, card dispositions and the closeout path and commit;
`result.reservations` shows `released` projects (`held` should be empty); `result.sprint` the new
status; keep `event_id`.

**A close is not a completed Definition of Done.** Whether the goal was reached is what your decisions
and closeout say.

**`--request-id` is the retry handle.** A part-done close exits `4` telling you to repeat this request
id: run the identical command. It resumes without repeating committed steps or writing a second
closeout. A new id would start a second close. A repeat with other decisions, reason or closeout is
refused with `validation`, exit `2`.

| exit | code | what to do |
| --- | --- | --- |
| `2` | `validation` | fix the file the message names and rerun |
| `3` | `owner_conflict` (`live_work`) | settle the head still running on the disposed card, then repeat |
| `3` | `owner_conflict` (`close_conflict`) | amend exactly those entries to `already_closed` / `already_moved` with `actual`, repeat the same request id |
| `4` | pending | repeat the same request id |

The close stops no head. The next production tick stops the observer of a sprint that is no longer
open and drops its record; after one tick confirm with `ummanu sprint status --ref sprint:ID` that
the observer reads `ended`.

```bash
python3 -P -m ummanu sprint close-result --ref sprint:1431 --event-id evt_<...>
```

### Commenting after the close

`ummanu sprint comment` on a closed or stopped sprint saves and audits the comment and does nothing
else: no reopen, no reservation, no head. `sprint comment-delivery` answers `not_deliverable`.

## The two-sprint pilot

The default is one open sprint per installation. A second is enabled by an instance setting. Admission
rules are in [Protocols](PROTOCOLS.md#the-open-sprint-limit): in practice the second sprint must touch
nothing the first touches. Each sprint declares its own observer.

Role `po` operations `close`, `reopen` and `record_budget` take the sprint reference as an argument and
do not check the observer binding, so with two open sprints an observer head declaring `--role po` can
close the other sprint; the audit records `role=po` with the observer's actor id.

### Enabling it

Add to `instance.yaml` like any config change ([Changing installation config](#changing-installation-config)):

```yaml
open_sprint_limit: 2
```

Only `1` and `2` are accepted. Anything else keeps the limit at one and `ummanu doctor` reports an
`open_sprint_limit` finding. The value is read at each admission; nothing restarts.

### Verifying it took effect

A clean `doctor` does not distinguish `2` from absent. Read the effective limit (config read only):

```bash
python3 -c 'import sys; from pathlib import Path; from ummanu.sprints import instance_open_sprint_limit; print(instance_open_sprint_limit(Path(sys.argv[1])))' <instance>
```

`1` after writing `2` means a different file was read or the value was refused. The count refusal reads
`installation already has an open sprint` at limit one and `installation already holds its limit of 2
open sprints` at two.

### Reading a refusal

A refused `sprint create` writes nothing; fix the argument and repeat.

| refusal | what it says | what to do |
| --- | --- | --- |
| `resource_conflict` | `project(s) already reserved by an open sprint: <project> held by sprint:ID` | give the new sprint different projects, or close the holder |
| `resource_conflict` | `product <id> is already the product of open sprint sprint:ID; ...` | sequence the sprints, or use another Product |
| `resource_conflict` | `... declares no product, so it cannot be proven disjoint ...` | close the product-less sprint and open one that declares its Product |
| `resource_conflict` | `repository root <a> overlaps <b>, held by open sprint sprint:ID` | narrow the roots, or sequence the sprints |
| `resource_conflict` | `declares repository root '<value>', which is not an absolute path` | close that row or correct its `sprint_repositories` metadata |
| `sprint_conflict` | `installation already holds its limit of 2 open sprints: ...` | close one of the named sprints |

### What the pilot does not isolate

- **`pause drain` and `pause freeze` stop both sprints.** There is no per-sprint pause.
- **One production tick serves both sprints.** A bad tick or a stopped dispatcher is an outage of
  both, and the health line does not say which sprint caused it.
- **A tick that cannot read the sprint store fences the sprint-held work of both sprints.** It moves
  nothing identified as sprint work: projects the last successful pass recorded as reserved (a
  snapshot in production state) and cards whose metadata names a sprint. It reports
  `sprint_board_unavailable` as critical, naming the fenced sprints and projects, and clears when the
  store answers. Cards of no sprint keep running. Gap: a sprint admitted after the last successful pass
  is not in the snapshot, so an unlinked card already in a project it newly reserved can move during
  the outage. If the sprint store fails right after opening a sprint, `pause freeze` covers it; a
  `drain` covers only claims.

Per sprint: the declared observer and its bound writes, the observer fence when the store is readable,
the budget counter and hard stop, and the claim suppression a blocked card causes.

A sprint opened with `--observer none` has no observer at all: no resume entries, no parking; its
cards are bounded by the [no-observer ceiling](PROTOCOLS.md#the-no-observer-ceiling). Plan it as work a
person checks on.

### Rolling back to one open sprint

Lowering the limit closes nothing: an installation over its limit keeps both sprints ticking and
refuses every new `create` and `reopen`. A checkpoint taken with two open sprints cannot be restored
onto a limit-one installation (`restored open sprints are not admissible on this installation`).

1. Close the second sprint ([Closing a sprint](#closing-a-sprint)).
2. Confirm `python3 -P -m ummanu sprint list --status open` shows exactly one.
3. Set `open_sprint_limit: 1` in `instance.yaml` (or delete the key) and run `ummanu config check`.
4. Verify the effective limit is `1` with the read-back command.
5. Let one production tick write and push the checkpoint.

If the limit must drop before the close, lower it first and close afterwards; until then new admissions
and a restore of that window's archive are refused. Keep that window short.

## Dispatcher task Python isolation

Every new card workspace gets the dispatcher-owned `.ummanu-task-env/venv`, separate from the
adapter-owned `.venv`. Before creating it the dispatcher appends any missing lines to the repository's
`info/exclude`: `.ummanu-task-env/`, `/TASK.md` and `/state/checks/`. Projects need no `.gitignore`
entries; linked worktrees share the file, and the entries stay after cleanup.

Everything else the pipeline generates in a card workspace is owned too, so a settled Done workspace is
removable under the unchanged cleanup dirtiness rule. Worker and reviewer heads run with
`PYTHONPYCACHEPREFIX`, `RUFF_CACHE_DIR` and `MYPY_CACHE_DIR` pointing into `.ummanu-task-env/`; the
broad receipt lands there; and files the editable install creates in the source tree (for example
`src/*.egg-info/`) are recorded with their exact digests in the cleanup journal's `generated` map right
after the install. A file that existed before the install, or that a head later rewrites, stays author work.
Tests often start child interpreters with an environment built from scratch, which drops
`PYTHONPYCACHEPREFIX`, so the venv's site-packages also holds `00-ummanu-task-pycache.pth`: it sets
`sys.pycache_prefix` to `.ummanu-task-env/pycache` for every interpreter of that venv unless an explicit
prefix is already set. A venv made ready before this file existed is still accepted and gains the file on
its next bring-up. Children started with an interpreter outside the workspace venv are not covered.

When the adapter declares `broad_check` without `broad_check.interpreter`, the candidate's `.[dev]` is
installed into this environment, so worker and reviewer tools and the inner broad suite resolve there.
That install may need package-index access; an unavailable index is a bring-up failure, never
permission to install into the production virtualenv. An adapter with no `broad_check` gets a bare
environment. Adapter setup runs with neither virtualenv active and the production venv off `PATH`. A
retained older workspace gets the environment on its next rework or review launch. Never run candidate
installs against `PRODUCT_ROOT/.venv`, and never use `PYTHONPATH` to hide or repair an editable install
that points elsewhere.

Gate, release and cleanup accept an absent namespace and fail closed on an existing unowned one. The
owner record is written atomically before population; a valid owner without a `ready` marker is
resumed by the next prepare. A namespace with no valid owner is not adopted: if it holds no operator or
adapter data, remove only that worktree's `.ummanu-task-env/` and retry bring-up; if uncertain, keep
the worktree and escalate. Never fabricate an owner record.

At prepare, launch, gate, release and before removal the dispatcher probes the production interpreter.
A failure (`interpreter_unavailable`, `missing_import`, `wrong_root`, `workspace_targeted_editable`)
blocks the card and retains its workspace. Inspect the reported interpreter, root, import origin and
metadata target, then repair from the registered production checkout only:

    PRODUCT_ROOT/.venv/bin/python3 -m pip install --no-deps -e PRODUCT_ROOT

Substitute the exact registered root for both occurrences. Do not restart or kill heads, rewrite task
metadata or delete the retained checkout as part of this repair.

`workspace_targeted_editable` covers both workspaces roots: the Orca workspaces root of A20 steps 8
and 11 (`UMMANU_DISPATCHER_WORKSPACES_ROOT`, default `~/orca/workspaces`) and `DATA_DIR/workspaces`,
where git-managed card and observer worktrees live. A workspace's owner is read from its path, so the
real dispatcher refuses to start (`workspace_roots_overlap`, naming both paths) when the two roots
are equal or one is inside the other. Point `UMMANU_DISPATCHER_WORKSPACES_ROOT` or the instance
`data_dir` elsewhere so the two are disjoint.

## Git residue: read the manifest, then replay exact targets

Card and observer Git residue (worktrees and `pipeline/*` refs) is settled by the cleanup owner. The
tick replays journaled obligations by itself. An operator cleans older residue in two steps. There is
no global batch: `--residue-replay` without `--project` and at least one `--target` is refused as a
usage error, and nothing is written.

1. Read the project's manifest. This step performs no effect and writes nothing, not even the journal
   or its replay cursor:

       ummanu instance-maintenance --instance INSTANCE --residue-inventory --project PROJECT

   Only that binding's repository, audit and intents are read. An unregistered project is refused
   before any read. Without `--project` the command is a read-only report over every project.

   `manifest` has one entry for every residue row and every journaled intent of the project:
   - `target`: the intent key, `ref@tip` for branch-only residue, or `worktree:PATH` for a foreign
     worktree. A row with recorded owners of this project names their intent keys in `targets`. A
     row whose recorded owners conflict gets a `ref@tip` refusal entry, and is never replayable.
   - `effects`, in execution order: `request-settlement` (an owned attempt of a Done or archived
     card), `stop-head` (run ids), `remove-worktree` (path, identity, dirty-check result),
     `delete-ref` (ref and tip, base ref and base tip, merged and published proof), `settle-claim`.
   - `outcome`: `eligible`, `preserved`, `pending` or `completed`, with the exact preservation or
     pending `reason`. Preserved targets still list the head stops and claim settlement a replay
     would do.
   - `digest`: over the target, its effects, outcome and inputs.

   Branch-only residue counts as owned when all of the following hold:
   - the card's project is this binding;
   - the card is Done or archived;
   - the board audit has `card.started` by the `dispatcher` role (older `claimed` forms are still
     accepted; `card.started` by any other role is not);
   - no worktree is registered for the ref.

   A ref whose card belongs to another project is reported as foreign residue, and that project's
   audit is never read. The replay deletes the ref only through the exact-tip transaction, so an
   unmerged or unpublished ref stays preserved. Git is read without optional locks, so the inventory
   never refreshes an index.

2. Replay the targets you chose, each with the digest you read, in the same order (at most 20):

       ummanu instance-maintenance --instance INSTANCE --residue-replay --project PROJECT \
           --target TARGET --manifest DIGEST [--target TARGET --manifest DIGEST ...]

   Before any effect the target's manifest is recomputed. A different digest, an unknown target or
   another project's target is `refused` with nothing written. A branch-only target that is not
   `eligible` reports its outcome and is not adopted. An eligible one is adopted with the reviewed
   identity only: if the ref or base tip has moved since, it is `refused` and nothing is journaled.
   The ref transaction verifies the reviewed tip and base tip, so a later advance leaves the target
   `pending` and deletes nothing. Every save writes back only the target's own intent, so other
   intents and the replay cursor stay byte for byte as they were. `replay` reports each target's `status`, `reason` and `progress`. If a target is
   refused, read the manifest again before you retry.

Code never chooses the targets. Protected candidates and audit branches stay until an operator
names them.

## Sprint observer heads

The production tick keeps one observer head per open sprint. Observers claim no cards and use no
project slot. Observer fence and declared-observer contracts are in
[Protocols](PROTOCOLS.md#the-observer-fence) and
[Protocols](PROTOCOLS.md#the-declared-observer); vitality policy is in [Head vitality](HEAD_VITALITY.md).

While a sprint is open its observer is the only writer of the sprint's cards on its reserved projects. To
intervene on such a card, the PO passes `--sprint-override` and a non-empty `--sprint-override-reason-file`
to `ummanu task create`, `move` or `edit`; the reason goes to the audit. A PO card linked to no sprint
needs no override; on a reserved project the dispatcher admits it only as `research` or `infra`, and
blocks a `code` card at admission with `sprint-reservation-blocked` naming the reserving sprint
([Protocols](PROTOCOLS.md#cards-outside-a-sprint)). Refusals: `sprint_write_forbidden` (names the
sprint), `sprint_guard_unavailable` (the sprint store could not be checked), `observer_sprint_mismatch`,
`observer_identity_unbound`. A running observer without a sprint binding (`bound: false` in
`status --json`) is stopped by the tick with `observer head predates the sprint binding` and relaunched
bound on the next tick; no operator step.

At the budget signal threshold the observer prompt carries a note to reconsider the plan. At the hard
threshold the sprint becomes `stopped`: the head is stopped, newly linked Ready cards are skipped, active
cards finish their cycle. `ummanu status --json` shows each sprint under `installation.sprints.items`
(status, hard-stop reason, budget, resume freshness, observer state) and an unreadable board under
`installation.sprints.error`. Only `ummanu sprint reopen --role po` continues a stopped sprint.

The observer profile comes only from the sprint's `sprint_observer` field (or `none`); a profile the
registry lacks is fenced, never launched on a default. The [head readiness](#head-readiness) gate runs
first. The head is launched through the role-environment wrapper in its own registered worktree cut
from a separate observer repository the dispatcher creates under the data directory; do not delete it.
An unknown directory at the workspace path is removed and recreated. Stopping a head kills the
workspace's terminals and removes the worktree registration; an already unregistered worktree counts as
stopped.

### Tick actions

Each sprint's decision appears under the `observer-reconcile` step:

- `observer-launched` — an open sprint without a record got a head;
- `observer-live` — alive, nothing done;
- `observer-waiting` — working, no durable event needs a turn;
- `observer-idle` — ready for input, nothing owed;
- `observer-nudged` — a committed linked-card event woke an idle observer (after a release that merged,
  the post-merge CI result is that event, not the Done);
- `observer-wake-pending` — a sent batch awaits acknowledgement;
- `observer-wake-waiting` — an event arrived while working; the next tick with a ready observer nudges
  unless exact provider progress shows the run advancing. `admission` says what the provider source
  answered; an unadmitted source is held to the unproven turn ceiling;
- `observer-wake-progressing` — the admitted provider cursor advanced; nothing is sent or stopped;
- `observer-wake-no-progress` — the admitted cursor is unchanged while busy; the three-observation
  ladder advances, no raw input is sent;
- `observer-redelivered` — a batch was sent again (observer ready without acknowledgement, or the
  acknowledgement deadline ran out); the original batch is kept;
- `observer-wake-deferred` — the wake failed; after `UMMANU_OBSERVER_WAKE_MAX_ATTEMPTS` (3) failures
  the head is replaced (`observer-relaunched`);
- `observer-relaunched` — the head was replaced (dead pid, exhausted wake retries, or the no-progress
  ladder). A replacement over a quiet queue sets a launch cooldown;
- `observer-stopped` — the sprint closed or vanished; head stopped, record dropped;
- `observer-stop-failed` — the host rejected the stop or returned no terminal list; the record stays
  `stop-pending` and the next tick retries;
- `observer-launch-deferred` — resource not ready, role skill not delivered, bring-up failed, or an old
  terminal could not be closed; the reason is on the record and the next tick retries;
- `observer-adopted` — a launch intent from a dead tick names a live pid; that head is accepted;
- `observer-launch-pending` — a launch intent is still inside its pid-wait window;
- `observer-launch-skipped` — a drain is in progress; a deferred record is created so the sprint is
  visible, and the head launches after `resume`;
- `sprint-board-unavailable` — the sprint store could not be read; no live head is stopped.

The fence runs before these, as the `observer-fence` step with action `observer-fenced`. Its status:

| state | status | tick |
| --- | --- | --- |
| drained, the sprint's observer not launched yet (`observer_not_launched`) or deferred by the drain (`observer_launch_deferred`, reason `pipeline is draining`) | `deferred` | `ok`, exit 0 |
| anything else: a due launch that failed (a deferral with any other reason), a dead or mismatched head, an abandoned bring-up, a corrupt declaration; and every fence outside a pause | `critical` | `degraded`, exit 3 |

Either way the sprint's cards stay fenced. A recovered host with an open sprint, drained before its
first tick, therefore reports a green drained tick. On the first tick after `resume` the fence is
`critical` until the relaunched observer writes its pid.

Timers:

- `UMMANU_OBSERVER_ACK_DEADLINE_SECONDS` (30 minutes) — how long one sent batch may stay
  unacknowledged before redelivery, measured from the send.
- `UMMANU_OBSERVER_UNPROVEN_TURN_CEILING_SECONDS` (15 minutes) — for a record whose provider source
  never got admitted (unbound, foreign, unreadable). Past it the delivery takes the wake retries and then
  replacement, carrying the batch into the replacement's launch.
- `UMMANU_OBSERVER_TURN_CEILING_SECONDS` (3 hours) — for records with no provider-progress source.
- A head with an admitted cursor has no ceiling; its no-progress ladder decides. An unbound Codex source
  is retried for binding on every poll under the launch-time rules.

An observer is ready only when its supervisor reports no turn open and no delivery in flight, and idle
only when it is ready and its last-output time (the supervisor journal's last event) is readable. A
delivery left in `delivery-intent` by a dispatcher that died mid-send waits for the deadline rather than
being sent twice. A card in Ready, In progress or Validate is never by itself an idle observer. Never
clear a composer with Ctrl-C, Escape, a key chord or raw terminal input.

### The observer role skill

The `observer` role's `observe-sprint` skill is delivered by `ummanu role-skills sync` (the
`role-skills` step of `ummanu upgrade`) and checked by `ummanu role-skills audit --check`. If the
skill is not in the head's shell, the launch is deferred with a reason like:

```
observer role skill is not available to this head: observer/observe-sprint is not in the codex
skill directory (<root>/observe-sprint/SKILL.md); run `ummanu role-skills sync`
```

The same reason appears when `skills/manifest.toml` has no `observer` target for that shell or is
unreadable. It shows in `ummanu status --json`, `ummanu sprint status` and `ummanu dispatcher
production-observe`. Fix:

```bash
ummanu role-skills audit --check
ummanu role-skills sync
```

Both commands read the product manifest plus the optional `<instance>/skills/manifest.toml` of the
installation named by `--instance` (default `UMMANU_INSTANCE`). A skill may ship one executable
`<skill>.sh`, linked into the operator's bin directory as `<skill>` (see `skills/README.md`).

Liveness uses the versioned launch-identity heartbeat. A missing file counts as alive during the
initial-output window. A live file whose identity does not match the record is
`heartbeat-identity-mismatch`: not adopted, not stopped, no replacement beside it.

### Audit and launch intent

Lifecycle events are staged before the host call and committed after it, keyed by sprint reference,
record generation and launch counter.

- `observer-launch-deferred` with a staging reason, or `observer-stop-failed` mentioning staging —
  storage failed first; nothing happened; the next tick retries.
- An outcome with a pending audit field (degraded) — the action happened but its event is pending:

```bash
ummanu task verify-audit --instance INSTANCE     # .pending, .backend
ummanu task reconcile-audit --instance INSTANCE  # repaired/unresolved
```

Both read the card audit, the `requests` table ([Board store](BOARD_STORE.md) §7.3). `reconcile-audit`
answers `0/0`; a staged row is resolved by repeating its own request id.

The launch intent is written to production state before the host call:

- `observer-launch-deferred` with an intent-not-persisted reason — state is not writable; fix the disk
  or permissions;
- a record in a launching state with a pending launch — the tick died mid-launch. The next tick resolves
  it from the pid file (`observer-adopted`, `observer-launch-pending`, or close terminals and relaunch).
  Nothing to do by hand.

### Worker and reviewer launch intent

Card heads use the same intent on every launch path (claim, rework, watchdog respawn, relaunch on
resume). Delivery contracts are in
[Protocols](PROTOCOLS.md#a-settled-head-is-not-a-delivered-prompt) and
[Protocols](PROTOCOLS.md#a-live-head-is-not-a-delivered-pointer).

- Launch-intent-unwritable (degraded) — no head was launched; fix disk or permissions.
- A non-empty intent after a dead tick — the next tick resolves it from the heartbeat: launch-adopted,
  launch-pending, or drop and relaunch into the reserved round. A live identity mismatch stays degraded
  and untouched.
- Launch-aborted (degraded) — the terminal exists but the launch failed; the card is not blocked and the
  intent with its handle is resolved next tick.
- `worker-launch-undelivered` / `review-launch-undelivered` (degraded) — the pointer was not accepted
  (`busy`, `blocked`, `update-modal`, `starting`, `unknown-dialog`, or `refused` when found in the
  composer). The launch is not adopted as a claim, whatever the pid. After
  `UMMANU_LAUNCH_DELIVERY_MAX_ATTEMPTS` (5) the head is stopped and relaunched
  (`*-launch-undeliverable`). A stop the host will not confirm reports `*-stop-unconfirmed` and keeps the
  intent; nothing is opened beside an unstopped head. A report of `pre-delivery-starting` after bytes were
  written is the normal path for a head still starting.
- Codex update prompt: preflight sets `dismissed_version` in the runtime `CODEX_HOME` `version.json`,
  best effort, before the head starts. No delivery ever upgrades Codex.
- The reviewer starts as a second supervised process in the worker's git worktree.
- A record written while heads were Orca panes (its workspace an Orca worktree, or a head run on
  `orca-legacy`) is refused by every launch, delivery, stop and teardown with a
  `legacy dispatcher record` reason, and the card goes Blocked naming the record. Nothing is torn down;
  clear that checkout by hand once its heads are confirmed gone.
- A stop the host did not confirm is not a stop: no replacement, no Blocked move, no freeze listing until
  confirmed. Check the head with [head-status](#head-status-in-a-live-workspace): the stop is refused
  or the process ignores the signal.

State without reading a transcript:

```bash
ummanu status --json --instance INSTANCE                    # .dispatcher.observers
ummanu dispatcher production-observe --instance INSTANCE    # .observers
ummanu pause-status --instance INSTANCE                     # .heads.observers, .state.stopped_observer
```

An observer row carries sprint, profile, state (`running`, `waiting`, `idle-grace`, `wake-deferred`,
`launching`, `deferred`, `stop-pending`, `pause-stop-pending`, `stopped-by-pause`, `pending`), pid
liveness, launch count, workspace, handle flags, last action, deferred reason, a delivery object, and
`wake_liveness`.

### An infrastructure bring-up outcome

A card blocked because a head never came up says so. Vocabulary:
[Bring-up outcomes](PROTOCOLS.md#bring-up-outcomes).

- On the card: the Blocked reason ends in `[bring-up outcome: class=infrastructure,
  cause=host_unavailable, stage=claim, head=worker, attempt=ATTEMPT_ID]`. Infrastructure causes:
  `launch_aborted`, `host_unavailable`; task causes: `workspace_contract`,
  `base_branch_contract`.
- In the tick: `failure_class`, `failure_cause`, `failure_reason`, `bring_up`, and `contract_refusal`
  for a broad-check contract preflight refusal.
- In the audit: the request id ends in `-infrastructure-blocked`.
- In the sprint: `budget.uncharged.infrastructure_blocked`; infrastructure outcomes charge no threshold.

`cause=workspace_contract` means the requeued checkout is gone or not the claimed worktree and branch;
`cause=base_branch_contract` means an integration base the project cannot integrate into or a seed the
remote lacks. Neither is fixed by relaunching.

Repair what the cause names (head, resource, adapter, checkout), then move the card out of Blocked with a
reason. The dispatcher schedules no retry; the observer decides, and a returned card is claimed under a
fresh attempt id. Before concluding a head is missing, ask [head-status](#head-status-in-a-live-workspace).

## Checkpoint push

The push runs every 30 minutes, fast-forward only, never forced. Contract in
[Recovery](RECOVERY.md#failure-and-divergence). A push failure does not stop work; the next window
retries.

`remote diverged` stops the push and raises the alarm. Only the pusher publishes to the instance
remote, so a remote holding history the snapshot branch lacks was pushed from elsewhere. Do not merge
it into the snapshot repository: it is bare, and only the exporter commits there (any other commit is
a red `snapshot.foreign_commit`). Preserve both tips, find out who pushed, and decide with the owner;
never force-push or rewrite history. Once the remote tip is an ancestor of the snapshot branch again,
the next window pushes and the alarm clears. On an installation not yet cut over, whose live root is
still a work tree, merge the remote commits into that checkout by hand (`git -C INSTANCE fetch
origin`, then `git -C INSTANCE merge --no-edit FETCH_HEAD`).

`dispatcher production-observe` (`checkpoint`) and `doctor` show last commit, last push, lag in commits
and minutes (age of the oldest unpushed commit), blocked-gate reason and divergence. `doctor` raises a
finding on divergence, a blocked gate, or lag above 60 minutes.

### A checkpoint blocked by a Product/Issue transaction

The checkpoint gate and board export refuse while a Product or Issue write is staged. Find and repair it
through the CLI; never move files under `board/product-issue-transactions/` or `board/pending-audit/`:

```bash
ummanu product transaction list --data-dir DATA_DIR
ummanu product transaction retry --request-id REQUEST_ID --data-dir DATA_DIR
ummanu product transaction discard --request-id REQUEST_ID --data-dir DATA_DIR
```

`retry` first: it resumes the operation and commits its event. `discard` is for a released transaction
the backend never accepted; it refuses with `live_write` if the row or comment exists, and always refuses
typed pending events. A document already outside the released journal comes back with `ummanu product
transaction adopt --path FILE`.

### A checkpoint blocked by duplicate card references

`board export is not restorable: ... duplicate references` stopped publication before touching the prior
pair or the Git index. Use the preview and exact-ID apply commands in
[Recovery](RECOVERY.md#repairing-historical-duplicate-card-references). Do not pick a row with `task
show`, and do not edit normalized files or board storage. After apply, retry the checkpoint and verify
its remote SHA before any recovery drill.

## An export whose sprint rows carry no observer

Restore validates the whole exported sprint set before its first write and refuses, by name, a row
without an observer value (damaged or taken before the field existed). Add the value to each named row
in the export's `state/board/sprints.json` and restore again:

```json
"observer": {"kind": "head", "profile": "<profile>"}
"observer": {"kind": "none"}
```

Use `none` for a row that ran without an observer. A closed row whose head is unknown takes
`{"kind": "historical", "profile": null, "source": "migration_unknown"}`; an open row may not. An open row
naming a profile the registry lacks is refused and repaired the same way. Forms:
[Protocols](PROTOCOLS.md#the-declared-observer).

## Recovery

The checkpoint and the full sequence are in [Recovery](RECOVERY.md#fresh-install-and-recovery). On a
clean replacement host:

```bash
sudo ummanu bootstrap --instance-remote REMOTE --instance-dir INSTANCE --installation-user INSTALL_USER
sudo ummanu recover --instance-remote REMOTE --instance-dir INSTANCE --installation-user INSTALL_USER \
  --recovery-phrase-file PHRASE_FILE
```

The recovery command is `recover`, not `install`. Operator rules for a recovery that does not finish
cleanly:

- Rerun the identical `ummanu recover` after fixing the reported external cause. Completed board and
  memory phases are skipped, existing repositories are untouched, and only missing projects and their
  host state are retried. Do not edit `recovery-progress.json`, project registry files or Git credential
  files.
- A non-empty partial target from an older release: inspect it, then remove it or choose a fresh
  `--instance-dir`. Existing repositories are never reset or replaced.
- `checkpoint-publication` degraded: the local commit is retained. Repair only the destination or
  credential and rerun. Never reset, rebase, force-push, delete progress, create an empty commit or use an
  ambient credential helper.
- Unsupported local divergence, no trustworthy merge base, contract mismatch or conflict cleanup: preserve
  the checkout and stop. Do not deepen, resolve with `ours`/`theirs`, reset, rebase, delete or publish from
  it.
- `restore-board` reporting an uncertain card batch: rerun the same command without deleting backend
  rows, pending audit, restore state or the request namespace. An oversized `create`, `metadata/state` or
  `closure` payload is a pre-write refusal: fix the named record first. A duplicate reference or
  conflicting content is evidence to preserve and investigate.
- `failed` project rows make the result `degraded` and non-zero while everything else completes; dispatch
  refuses those bindings before any head or worktree.

`bootstrap --empty`, `restore-board`, `memory reindex`, `reconcile apply` and `restore-reconcile` are
diagnostic primitives, not the runbook. `restore-reconcile` exits non-zero `degraded` while a project
checkout is unavailable; repair it through `recover`.

## Optional cold archive

`backup create` and `backup verify` are a manual tool with no timer, offsite transfer or `doctor` gate;
the archive contract is in [Recovery](RECOVERY.md#backend-aware-cold-archives).

```bash
python3 -P -m ummanu backup create --instance INSTANCE --kind both
python3 -P -m ummanu backup verify ARCHIVE.tar [--strict]
```

`create` writes an unencrypted tar into `backups/` (`core`, `full` or `both`). `board-store.env` and the
memory model cache are never included; staging files are `0600` and no password reaches argv or logs.
`verify` returns `0` on success, `1` for findings or strict warnings, `2` for an unreadable archive.

Legacy extraction is `ummanu restore ARCHIVE.tar`. A `full` archive restores into a separately
provisioned, migrated, empty target of the same instance with a different database endpoint:

```bash
python3 -P -m ummanu restore-postgres ARCHIVE.tar --instance TARGET
```

Neither command reconciles or starts processes.

## Auto-merging green cards

A green verdict on a card whose sprint declares an observer parks the card in Assessment
([Tasks](PROTOCOLS.md#tasks)) once the mechanical gate is green; the merge runs on the tick that performs
a recorded `release`. Red or pending gates resolve in Validate. A card with no observer merges on the
verdict's tick. Gate receipt rules are in [Receipt names](PROTOCOLS.md#receipt-names).

A release the dispatcher cannot carry out takes the card to Blocked with the failure; it never sends the
card back for rework.

On release the dispatcher:

1. Pushes the worker branch to the default branch, fast-forward only; a diverged default branch is
   rejected, never forced or resolved.
2. Fast-forwards the project's local checkout onto the new tip (for the product repository this deploys
   the checkout it runs from). A card based on another card's branch lands on that base; the checkout is
   refreshed only from the default branch, and a failed refresh there does not send the card back.
   The production checkout the dispatcher runs from moves only after the board store is at the new
   tip's schema: the tip is pinned by commit id, its own owed migrations (only those declaring
   `release_safety = "additive"`) are applied under the migration lock with a bounded wait, and then the
   checkout fast-forwards to that commit ([Board store §7.4](BOARD_STORE.md#74-schema-versioning-and-migrations)).
   A refused migration keeps the checkout on its old commit, on both paths. The dispatcher retains
   the original `release_schema_refused` facts and delivered remote merge in its release record,
   then creates one `operation` for the sprint's PO with recovery (`ummanu upgrade` once the
   cause is fixed) and verification. If creation is refused, ordinary ticks retry the persisted
   request, including after restart; the source stays unsettled and activation is not retried.
   Once the operation commits, the canonical typed reason references it, the source goes to Blocked,
   and its release record is removed after durable settlement.
3. Stops the worktree's terminals and removes the worktree.

Nothing is ever merged into the live root. A card whose project repository is the live root is refused
at admission; one claimed before that refusal existed is refused here (`nothing was merged: ...`)
and goes to Blocked ([Changing installation config](#changing-installation-config)).

Teardown happens only on this path; parked and rework cards keep their workspace and branch.

A merge that landed opens a post-merge CI watch before the card reaches Done, and the observer is woken
on its result (`green`, `red`, `absent` or `timeout`), not on the Done
([Protocols](PROTOCOLS.md#post-merge-ci)). `ummanu dispatcher production-observe` lists open watches
under `post_merge_watches`; each resolution is a `post-merge-ci` tick action, and the result is a
dispatcher comment on the card and on its sprint. `UMMANU_POST_MERGE_CI_CEILING_SECONDS` (3600)
bounds the wait.

Kill switch: `UMMANU_DISPATCHER_AUTOMERGE=off` disables push and fast-forward. The card still reaches
done and needs a manual merge. Default on.

## Pausing the pipeline

Pause contract: [Protocols](PROTOCOLS.md#pause).

```bash
python3 -P -m ummanu pause-scope  --instance INSTANCE                  # what a pause would reach
python3 -P -m ummanu pause drain  --instance INSTANCE --reason "why"
python3 -P -m ummanu pause freeze --instance INSTANCE --reason "why"
python3 -P -m ummanu resume       --instance INSTANCE
python3 -P -m ummanu pause-status --instance INSTANCE
```

`drain` stops claiming Ready cards, dispatching background roles and launching observers for new sprints;
in-flight work, running heads and live observers continue. Use it to stop inflow.

`freeze` also stops live worker, reviewer and observer heads and the tick advances nothing. Workspaces and
uncommitted work are untouched. Use it when the host must be free now (backup, reboot, incident).

`resume` lifts the pause, relaunches frozen worker and reviewer heads in their workspaces with fresh
watchdog windows, and leaves a card that reported during the freeze to the next tick. Observers come back
through the next tick's reconciliation.

If the host refused to stop an observer during a freeze, the `pause` response warns with the sprints and
the record stays `pause-stop-pending`; the frozen tick retries and goes degraded if the host refuses
again.

Switching mode requires `resume` first; a repeat in the same mode is a `noop`. The flag is
`<data_dir>/dispatcher/pause.json`; background roles read a legacy mirror that `resume` removes only if
the pause wrote it.

### Read the scope first, then decide

```bash
python3 -P -m ummanu pause-scope --instance INSTANCE
```

It writes nothing and reports: `extent` (pipeline-wide, no per-sprint pause), `target` (the flag,
production state and legacy mirror), `sprints`, `cards` (every board card with its holding sprint or
`null`), `heads`, and `modes` (what drain and freeze each stop).

Then decide and read the result:

```bash
python3 -P -m ummanu pause drain --instance INSTANCE --reason "why"
python3 -P -m ummanu pause-status --instance INSTANCE
```

The response carries `action` (`paused`, `noop`, `resumed`), `changed` and the pause state. A drain while
frozen is refused with exit 3. After a drain, `pause-status` shows every `stopped_*` list empty and live
heads `running`; `resume` after a drain restores nothing and says so.

If production state is corrupt, a pause that took still answers with its `action`, the complaint under
`warnings` and the unreadable section as `unavailable`; read `<data_dir>/dispatcher/pause.json` directly
and repair production state. A command that really failed exits non-zero and leaves the flag untouched.
For a resume of a freeze in that state, `restored` lists are `null`, not empty.

`action` is decided under the tick lock, so trust it over comparing `pause-status` before and after.

### Pause or breakage

`pause-status` carries `state` (mode, actor, time), `target`, and per-head lines in `heads.cards` and
`heads.observers`, each with its source:

- `running` — alive;
- `stopped-by-pause` — stopped by the pause, workspace intact, `resume` brings it back;
- `not-running` — no head and not stopped by the pause: not reached yet, or a break
  ([Waiting watchdogs](#waiting-watchdogs)).

A frozen tick answers `skipped` with the reason and a pause snapshot; the health probe answers ok. It
keeps the checkpoint cadence and due-push coordination.

### A freeze that lifts itself

A freeze by an allowlisted automation actor expires after the configured TTL (45 minutes by default) and
the next tick resumes it. A freeze by any other actor never expires. TTL zero disables auto-resume.
`state.auto_resume` in `pause-status` is `fresh`, `manual-or-unknown-actor` or `disabled`; a tick that
lifted a pause reports its age and the heads it brought back.

## Waiting watchdogs

The dispatcher waits for a worker report (In progress) and a review verdict (Validate). Vitality
observation and the recovery ladder are in [Head vitality](HEAD_VITALITY.md); the headless-card contract
is in [Protocols](PROTOCOLS.md#a-card-in-an-active-state-with-no-worker).

Each waiting tick reads the head as `head-status` does (`command_terminal_status`): its launch-identity
pid heartbeat, the exact-run provider cursor and its child processes, reduced to one vitality verdict
([Head vitality](HEAD_VITALITY.md#decision-path)). A heartbeat naming a gone process takes the stall
path: one respawn in the same workspace, then Blocked. A runtime that answers unavailable is not a dead
head; the waiting ceiling still runs as a fallback.

The launch-identity heartbeat, written by the launcher before `exec`, lives under
`UMMANU_DISPATCHER_BODY_DIR` (default `/tmp`) with its leaf handoff; respawn deletes both first. A
matching live heartbeat is positive liveness; a dead one takes the stall path; missing or unreadable
keeps the output fallback; a live mismatch is degraded and never authorizes a close, stop, signal,
adoption or replacement. The raw command override has no heartbeat and stays on output checks.

Every fresh progress signal restarts the waiting window, so a ceiling measures silence, not task age. A
head that printed nothing since launch gets the short first-output window; TUI heads on an alternate
screen also count the session rollout file's modification time. First breach: one respawn; second:
Blocked.

A worker whose episode suspects or confirms a stall, with nothing landed for the round, first gets one
reminder per report round, delivered through its supervisor, to run the report command from its
`TASK.md` (`worker-report-prompted`, degraded). A suspicion never destroys: once the reminder is spent
the tick reports `worker-stall-suspected` (degraded) and waits. Under a confirmed stall, a spent
reminder, a head that cannot take one (exited, suspended or not addressable) or an unconfirmed send
takes respawn then Blocked. A head nobody could observe is never replaced on the clock: past the long
ceiling the tick escalates to the operator. The respawned worker gets the same `TASK.md`, commands and
generation.

This idle bounce is a degraded tick and turns `ummanu automations health` red until a healthy tick follows.
The Blocked move after it is not degraded; the steward reports it as `new_blocked`. Every respawn writes a
board comment.

### A card in an active column with no worker at all

Before waiting, the tick checks there is a head to wait for. A card moved into an active column with
nothing running (typically a raw move out of Blocked) is settled in that tick:

- `headless-worker-replacement-launched` (ok) — a replacement runs on the retained checkout; the outcome
  and a card comment name workspace, branch, candidate SHA and dirty flag. Nothing was reset.
- `headless-worker-recovery-refused` (blocked) — back to Blocked with `recovery_error`:
  `workspace_missing`, `workspace_unbindable`, `workspace_unreadable`, `candidate_unknown` (repair on
  disk), or `round_already_answered` (the round already has an accepted report; moving the card back was
  the wrong move).
- `orphan-worker-heartbeat-unbound` (degraded) — a live heartbeat at this card's worker pid path cannot be
  bound. Nothing is launched or signalled; find out whose process it is first.

Returning the same card again gets a fresh answer. While unresolved, `ummanu status` marks the attempt
`degraded` with `headless` details, and `ummanu sprint status` lists it under
`work.degraded_cards.items`. A card sitting in In progress is not on its own evidence that anything is running.

### Watchdog settings

- `UMMANU_INITIAL_OUTPUT_STALL_SECONDS` — first-output window, default 180.
- `UMMANU_REVIEW_VERDICT_STALL_SECONDS` — verdict ceiling after first output, default 5400.
- `UMMANU_WORKER_REPORT_STALL_SECONDS` — report ceiling after first output, default 21600.
- `UMMANU_HEAD_IDLE_STALL_SECONDS` — no production effect since the wait tick moved onto the vitality
  verdict; the vitality thresholds do not read it (see `docs/HEAD_VITALITY.md`, Thresholds).
- `UMMANU_LAUNCH_DELIVERY_MAX_ATTEMPTS` — ticks a head may hold an unaccepted pointer before relaunch,
  default 5.

The stall settings are read at check time; garbage or zero falls back to the default.

### Reports and verdicts

Heads write bodies to `/tmp/ummanu-report-<ref>-<round>.md` and `/tmp/ummanu-verdict-<ref>-<round>.md`
(directory from `UMMANU_DISPATCHER_BODY_DIR`); files are left in place. The round is part of the
verdict request id.

A worker round ends only with a report under the request id the dispatcher issued, taken from the hidden
`<!-- report-round generation=N ids=... -->` line at the end of `TASK.md`. Do not edit that line. To report
for a worker by hand, copy the command from its `TASK.md`, ids included; a report under any other id is
written to the card and moves nothing.

`ummanu task report` answering `audit_pending` means the comment landed but the audit did not: rerun
the same command unchanged (answers `replayed`) or run `ummanu task reconcile-audit`.

## Background-role telemetry

```bash
python3 -P -m ummanu automations health
```

One line per role: timer state and freshness of the last healthy tick. Expected state comes from
`host.components` of the bound `instance.yaml`; an explicit `enabled: false` prints `DISABLED` and is
neutral. An unreadable config prints an error. Non-zero exit: an enabled role is red or the config is
unavailable.

- `scripts/ummanu-agent-gate.sh` runs every role through one environment and exit-code protocol
  (every role through `python3 -P -m ummanu automations`, which injects the board ports steward and retro need). It
  resolves the checkout as `TA_RUNTIME_PYTHONPATH`, then `UMMANU_REPO`, then `$HOME/ummanu`, and
  uses only that checkout's `.venv/bin/python3`. A `configuration error` naming the checkout means its
  source tree or interpreter is missing, non-executable or another venv's; it fails before precheck.
  Inspect the rendered units or `ummanu doctor`, then repair as the owner from a healthy installed
  command:

  ```bash
  ummanu upgrade --no-pull --product-root /absolute/path/to/selected/checkout
  ```

  Do not use system-wide `pip`, copy site-packages or point `PYTHONPATH` at another checkout.

- curator, steward and retro run logs live under `$TA_STATE/<agent>/`, or the data directory when unset.
  Healthy is the last event whose result is neither `error` nor `board-unreachable`.
- Curator harvest limits (`TA_CURATOR_MAX_TURNS`, `TA_CURATOR_MAX_INPUT_BYTES`, `TA_CURATOR_MAX_SOURCES`),
  routing and pending-batch rules are in [Protocols](PROTOCOLS.md#memory). Precheck exit 102 is a
  successful deferred tick. A lock file never needs stale-PID repair. An unversioned, stale, foreign,
  corrupt or cursor-only pending file is refused and left untouched.
- the `pipeline` line comes from the production dispatcher's tick telemetry: time, healthy or degraded,
  diagnostics. A degraded tick colours the line immediately; a Blocked card does not. A tick that died with
  an exception writes a failed record. A tick that never reached its state (lock taken, guard refused)
  writes nothing and shows up as missing freshness. A freeze is healthy unless the frozen tick failed again
  to stop an observer.

Readers resolve dispatcher state like the dispatcher: `--data-dir`, else `UMMANU_DATA_DIR`, else
`data_dir` from the instance (a relative value resolves from `instance.yaml`, `~` is expanded). Setting
`UMMANU_DATA_DIR` in `runtime.env` moves both writer and readers.

A continuous run of unhealthy ticks is one incident; the steward reports one unhealthy event (opening
reason, failed tick count, `retained_window` grouped by step/action and error code) and one recovery.
Deduplication uses monotonic incident/recovery counters and a telemetry `generation`; a changed
generation or a counter that moved backwards gives a telemetry-reset hit. The resource-flip signal reads
the dispatcher's readiness cache; an unreadable cache keeps the previous baseline.

## The local web transport

`ummanu web-serve` serves the dashboard and card pages over the `web-read` and `web-run` operations.
It answers on loopback only. Routes and codes: [Protocols](PROTOCOLS.md#serving-the-pipeline-locally).

> **It is never published directly.** A non-loopback bind is refused in code with: "this service has no
> password, no TLS and no authorisation, and its routes start real heads on this installation, so it
> binds a loopback address only. External access is published by the guarded front instead (`ummanu
> web-front`, DoD 5), which terminates TLS, checks a password and proxies here; this refusal is what makes
> that front the only way in". Do not weaken it and do not forward the port. Outside access is
> [the published web front](#the-published-web-front).

The packaged `ummanu-web.service` runs it on `127.0.0.1:8787`. To run another by hand:

```bash
# start it in the foreground; Ctrl-C stops it
python3 -P -m ummanu web-serve --instance INSTANCE

# a second one beside the first, or a different data plane
python3 -P -m ummanu web-serve --instance INSTANCE --port 8788 --data-dir DIR

# head profiles from a registry other than the installation's own
python3 -P -m ummanu web-serve --instance INSTANCE --heads-registry REGISTRY
```

| flag | default | what it is |
| --- | --- | --- |
| `--instance` | required | instance directory or `instance.yaml` |
| `--data-dir` | the instance's own | override the data plane, or `UMMANU_DATA_DIR` |
| `--host` | `127.0.0.1` | bind address; refused unless every resolved address is loopback |
| `--port` | `8787` | bind port |
| `--heads-registry` | the installation's own | where `--profile` values resolve, or `TA_HEADS_REGISTRY` |
| `--offline` | off | collect installation health without inspecting the live host |

**Stopping it** loses nothing: cursors belong to browsers. It does not stop heads its runs raised; a run
is ended by `ummanu web-run state --run-id RUN` when its result arrives or its deadline passes.

**Diagnosing it.** It logs one line per request on stderr. Direct reads:

```bash
curl -s localhost:8787/api/system | python3 -m json.tool | head -40      # the dashboard's document
curl -s -o /dev/null -w '%{http_code}\n' localhost:8787/api/tasks/REF   # 200, 404, 503 …
python3 -P -m ummanu web-read system --instance INSTANCE              # the same read, no HTTP
```

An unavailable source renders as a marked block with reason and age, never an empty list; an unreadable
record under `<data-dir>/webproto/runs/` marks that section and the JSON route answers 503
`backend_unavailable` naming the file. If the document is right and the page wrong, the transport is at
fault; if both agree, the source is. A port in use fails the bind naming it; a non-loopback `--host` exits
2 with `validation`.

### The operator's screen

Every fact is drawn once. What the bottom bar carries on every page — the provider windows and the
doctor lamp — has no panel of its own anywhere, and a page repeats nothing its header already says.
Long text (a goal, a decision, a report body) is in the page once, in the reading face: held to two
lines and grown in place by `show more`, never a one-line preview followed by the whole text again.

The dashboard (`/`) has three parts that fail apart:

1. **pipeline** — running, drained or frozen (since when, by whom), the dispatcher phase, the number of
   running PO turns, and the opposite control (`Drain…` opens a reason and the button; `resume`).
2. **attention** — while installation health reports a problem, a banner names the first one and links
   to `/doctor`; health that could not be read is the marked block every unreadable source gets. The
   facts behind health (checkpoint, disk, memory, load, cards, attempts, last tick, failed units) are one
   collapsed `Installation` panel.
3. **open sprints** — one card per sprint: goal (two lines), the card in hand with its state, age, gate
   and title, the heads (observer, worker, reviewer) with model and effort, and the card budget as a thin
   secondary line — a spend, not progress.

A sprint page (`/sprints/{ref}`) has a `Now` panel (the card in hand, the gate, what the observer is
doing, the heads, the budget line), the observer's call (decision in prose, its reasons and rejected
alternatives behind a disclosure, the next step), and tabs for the cards by state, the Definition of Done
rendered from its Markdown, the rest of the last resume, and the issues. The side panel says what no chip
does: projects, repositories, the declared observer and whether it is up (the held head is named only when
it is not the declared one), and the worker and reviewer pins. Closing an open sprint is under `More
actions`.

A card page (`/tasks/{ref}`) is headed by the card's title and opens on its **Task** tab: the card's full
description, rendered from its Markdown. Beside it are the work (report, verdict, decision, result), the
timeline of transitions with the records that made them, and the raw event tail. The side panel's `Heads`
lists every run the card recorded, by role: the latest run of each role is a chip with the model that ran
it — the exact id the run reported (`claude-opus-5-5`, shown as `Opus 5.5`), else the configured one
marked as configured — its effort (five bars and a word; hollow bars when no effort flag was passed and
the CLI default applies) and whether its process is alive; the runs before it are one line each, with
their own model, effort and attempt. A run a local-pty supervisor held links to its read-only view: the
tail of its terminal and its journal.

Card pages carry a comment and a move with reason (a second reason past the sprint's reservation); sprint
pages carry a comment and, when open, the close. All post to the routes in
[Protocols](PROTOCOLS.md#routes) as role `po`, actor `web`. There is no browser `decide`. Every page reloads
every 30 seconds unless a field has focus or holds typed text — a half-written `/po` message is never
discarded by it, and a password field counts, so the `/po` token being typed in is never cleared by a
tick; the checkbox on the bottom bar turns the reload off. A page rendered as the answer to a POST — a refusal, normally — carries no reload at
all, because reloading one is the browser offering to send the submission again.

### The bottom status bar

Every HTML page the transport serves — the dashboard, sprints, projects, cards, `/history`, the sprint
form, `/po` and its sessions, and refusal pages too — ends in one bar fixed to the bottom of the viewport.
It shows what each provider subscription has left: for Claude and for Codex, every usage window the
provider reported, its remaining percentage and **how long is left until it resets** — `2d 12h left`,
`6h 45m left`, `42m left` or `less than a minute left`. A reset moment that has already gone by is
rolled forward by whole window lengths to the next one ahead, and the percentage is then drawn as
`stale`, because the reading predates that reset; a past moment with no usable window length, or a
reading with no moment, says `no reset time recorded` rather than showing a dash. One provider is one group and reads as one: its name is the group's heading, a
rule separates it from the next provider, each window is a chip of its own (`5-hour 73% · 1h 6m left`),
and the percentage follows the window's name at the chip's own gap. A percentage is drawn as the layer
rounded it, with a trailing `.0` dropped: `73%`, and `95.4%` when the reading really is fractional.
The exact moment is not lost: it is the hover title of that element, as the ISO
UTC string the reading carried. The time left is counted from when the page was drawn, not from when
the reading was taken, so a reading served from the cache does not overstate what is left. The bar is
the one place the windows are drawn. A provider whose reading is stale or
unavailable is shown with that word, the reason, and no percentage at all: on a bar a number is read as
what is left *now*, so no reading is drawn as words rather than as a figure. An available reading taken a
while ago says how old it is. The bar's height is reserved under the page rather than overlaid, so it
covers nothing, the `/po` composer included.

Its data is the cached provider layer (`ummanu.web.provider_usage`, a five-minute in-process cache).
**Rendering a page never adds a provider
call**: the transport hands the bar the cached read, so a hundred page loads inside one cache window ask
each provider once, and JSON routes, which render no page, ask nothing. A process built without the
provider layer, and a read that refuses, both still serve every page; the bar then carries the reason
where the numbers would be.

#### The doctor lamp, and the page behind it

The bar also carries a doctor lamp, at its left, on every page. Its state follows recorded findings:

- **red** — the installation cannot be trusted to run work, or its health is unknown. Any of
  `unit.failed`, `unit.missing`, `checkpoint.blocked`, `checkpoint.last_failed`,
  `secret_store.key_unusable`, or
  `health.unreadable` or `doctor.collection_stuck`.
- **yellow** — it runs, but somebody should look. Any of `pipeline.paused`,
  `dispatcher.divergences_open`, `host.inventory_unreadable`,
  `memory.index_missing`. A problem whose code nobody has classified is yellow too — never green.
- **green** — health was read, and it reports no unaccepted problem.
- **unknown**: recorded doctor is not yet collected and status reports no problems. This initial
  state uses a neutral grey lamp. A real status finding still makes it yellow or red.

Each problem carries that stable code beside the sentence a person reads, and the **code**, not the
wording, is what the colour is decided from (`ummanu.webproto.reads.PROBLEM_SEVERITY`). Red wins
over yellow, and yellow over green: one red problem is a red lamp however many yellow ones there are.

**Health that could not be read is red and never green.** A reading that did not happen — an instance
that does not validate, a collector that refused — is reported as the problem `health.unreadable`,
with the reason, and the lamp goes red. An empty list is never drawn as a clean installation.

The lamp is a link to `/doctor` from every page. That page lists the current problems, each with its
code and its message, grouped by the severity that decides the colour, with the red group first; when
there are none it says so plainly; when health could not be read it says that, with the reason.

Refreshing the lamp combines status health with the latest recorded real doctor findings. The
dashboard's attention banner, Installation panel, lamp and `/doctor` share `DoctorLayer`'s one-minute
cache over `ReadLayer.health_snapshot`: `collect_status` with no sprints or panel probes, plus a local
read of `DATA_DIR/doctor/latest.json`. Page reads never launch doctor, provider or SSH probes. Status
severity remains unchanged; any unaccepted doctor finding, including an unknown future code, makes the lamp
non-green. Both sources retain their problems. `/doctor` shows finding code/message and identity/details,
doctor run/completion/exit and the web reading time separately; lamp hover text includes doctor run time.
Malformed, unreadable, stale, wrong-installation, wrong-mode and failed records remain explicit red
doctor problems. Missing first results and an ordinary first collection say `unknown / not yet
collected`, with no `health.unreadable` finding. A process built without the layer remains red.

#### Known doctor findings and foreign project directories

Edit the selected installation's `instance.yaml` through the normal instance configuration
workflow. Obtain the current complete row from `ummanu doctor --instance INSTANCE --json` under
`findings`, then copy that object as `finding` and give its acceptance a nonblank reason. For example,
if doctor emits `{"code": "restore_problem", "message": "memory index has not been rebuilt"}`:

```yaml
doctor:
  accepted_findings:
    - finding:
        code: restore_problem
        message: memory index has not been rebuilt
      reason: Rebuild is scheduled during the maintenance window.
host:
  projects_root: /srv/projects
  foreign_projects:
    - unrelated-checkout
```

Copy every field and value, including severity, target and measurements when present. For an
already accepted JSON row, omit only the added `accepted` and `acceptance_reason` fields. Object key
order does not matter; changed content or another target with the same code requires explicit
acceptance again. There are no code selectors, wildcards, regular expressions or prose matching.
Acceptance belongs to this instance. Remove its entry to restore ordinary classification on the
next doctor collection. Checks still run, and accepted rows remain visible with their reason in
text, JSON, the dashboard Installation panel and the doctor page. Only unaccepted doctor rows
affect the lamp; status faults and unavailable, failed, stale, wrong-installation, wrong-mode or
stuck diagnostic reads retain their normal classification.

Foreign projects are literal immediate child directory names, not registry IDs or paths. Empty
or whitespace-only names, `.`, `..`, separators, absolute paths and wildcard syntax are refused.
Doctor excludes present foreign paths from unmanaged-project findings. It does not require absent
foreign children, and a registered project with the same path still receives its normal
missing/present checks. This declaration does not create ownership or authorize cleanup.

The periodic recording below picks up configuration edits on its next collection. The shared web
cache then refreshes within one minute; refreshing a page alone does not run doctor. To collect
immediately, use the supported `ummanu doctor-record --instance INSTANCE` command. A direct
`ummanu doctor` run displays the new disposition but does not publish the lamp's recorded result.

#### Periodic doctor recording

The catalog materializer owns `ummanu-doctor.service` and `ummanu-doctor.timer`. The timer starts
30 seconds after boot and 60 seconds after the previous oneshot becomes inactive, with one-second
accuracy. It uses the installation runtime user, home, runtime.env and installed product venv. The
timer must be enabled/active; its triggered oneshot need not remain active. Component disabled/foreign
declarations retain their existing ownership boundary. Daily instance Git maintenance is independent.

The rendered command is `PRODUCT_ROOT/.venv/bin/ummanu doctor-record --instance INSTANCE --data-dir DATA_DIR`.
It calls this installed product's `python -P -m ummanu doctor --instance INSTANCE --json`, whose
`run_doctor_json`/`collect_doctor_inspection` remain the sole diagnostic evaluator. Manual fixture/offline
runs can pass `--host-fixture DIR`/`--offline`; records retain their mode, and a live web reader refuses
to treat those modes as a live diagnostic success. No scheduler runs in dispatcher ticks or page requests.

`DATA_DIR/doctor/latest.json` is a bounded document, schema version 2. Both parts share the installation
identity. `completed` holds the last completed attempt (including failures), or null before the first
completion. `collecting` holds only the current run's start and mode, or null after completion:

```json
{
  "schema_version": 2,
  "installation": {"instance": "/srv/instance/instance.yaml", "data_dir": "/srv/data"},
  "completed": {
    "run_at": "2026-09-29T00:00:00Z",
    "completed_at": "2026-09-29T00:00:02Z",
    "mode": "live",
    "outcome": "result",
    "exit_code": 1,
    "reason": null,
    "result": {"schema_version": 1, "ok": false, "findings": [{"code": "recovery_bypass", "message": "ambient credential configuration", "capability": "checkpoint-git-authentication"}]}
  },
  "collecting": {"run_at": "2026-09-29T00:01:02Z", "mode": "live"}
}
```

The recording command takes a nonblocking `doctor/record.lock`, atomically retains the validated
completed attempt alongside a new collection marker, then evaluates in a child process group with a 40-second
deadline and a 2 MiB output bound. The unit's outer deadline is 50 seconds and kills its control group.
Timeout kills the collector group, including its probes. Exit 1 with findings is a recorded diagnostic
result: the recording command succeeds. Exit 2 is `unavailable` and preserves diagnostic findings;
process/parse failures publish `failed`, without raw stdout/stderr or exception text. No secrets or
environments are stored. Publication uses a private sibling, file fsync, atomic replace and directory
fsync. Completion atomically replaces `completed` and clears `collecting`. A killed attempt leaves
the previous completed attempt readable with its original timestamps, or an honest initial unknown.
A write refusal fails the unit/journal and never refreshes the prior timestamp; the prior result
expires normally. No result history is stored.

Precedence is shared by all consumers: completed findings and freshness come from `completed` alone.
Collection metadata is current only when its start is strictly later than that completion; an older
or equal marker cannot make the completed run look stuck or override its mode. Producer UTC timestamps
retain microsecond precision so sequential invocations within the same second remain distinguishable;
released whole-second timestamps remain readable. Both parts are
validated under the same installation identity. An ordinary current marker adds no problem and
the dashboard and `/doctor` show `run in progress since <run_at>` beside the completed reading.
After more than 60 seconds it adds the separate red `doctor.collection_stuck` finding, with elapsed
time, threshold and a service/journal inspection hint. Exactly 60 seconds does not trigger it.
This allows 20 seconds beyond the normal 40-second child deadline (and 10 beyond the unit's outer
deadline) for termination and publication. An interruption can leave a marker behind; a subsequent
successful collection clears it. Genuine completed-result staleness remains red during collection.

Released schema-version-1 completed records remain readable and are retained by the next producer
run. A version-1 `collecting` record already erased its predecessor: it reads unknown/not yet
collected until completion, and its start still supplies the same stuck-run threshold. No historical
result is fabricated.

Freshness is 180 seconds from the completed attempt's UTC run start, not from current collection,
web collection time or file mtime. Future/invalid
times are malformed. An expired result retains its findings and says stale. Cache reuse can delay a
new result or expiry by at most another 60 seconds; each response uses one pinned reading throughout.

The dashboard's banner and `Installation` panel read that same cached reading, not a collection of their
own: one cache, one window, so they and the lamp cannot disagree, and they are up to one minute stale
exactly as the lamp is. A cache refresh is one collection however many requests arrive during it (they wait
for it and share it), and one response draws its panel and its lamp from one reading even when the
window expires mid-request. A warm dashboard render starts no subprocess and opens no `board/*.ndjson`. For the same
reason `web-serve` runs the board store's git-exclusion guard (`board-store.env` untracked and ignored)
once at start-up instead of on every request; a refusal it finds there holds for the life of the
process, and a store repaired or created later is picked up by restarting `ummanu-web.service`.

### Running a card through the installed service

The two POST routes, through the front, as `curl`. `~/.ummanu-owner.curlrc` is a mode-0600 file with
`user = "owner:..."` and `cacert = "DATA_DIR/webfront/caddy/pki/authorities/local/root.crt"`, so the password
never reaches a command line or history.

```bash
F=https://HOST
K=~/.ummanu-owner.curlrc

# 1. a card this installation may run: a registered project, no open sprint reserving it, Issues
curl -sS -K $K "$F/api/tasks/REF" | python3 -m json.tool | head -30

# 2. raise the worker. `request_id` is the client's, and it is what makes a retry safe
curl -sS -K $K -H 'Content-Type: application/json' "$F/api/runs/start" \
  -d '{"ref":"REF","request_id":"ID","profile":"WORKER_PROFILE","instruction":"..."}'

# 3. watch it. A reload of /tasks/REF resumes; so does the same GET from a kept cursor
curl -sS -K $K "$F/api/runs/RUN" | python3 -m json.tool

# 4. review it, by its result, once it has ended
curl -sS -K $K -H 'Content-Type: application/json' "$F/api/runs/review" \
  -d '{"request_id":"ID2","profile":"REVIEWER_PROFILE","worker_run_id":"RUN"}'
```

Profiles come from the installation's head registry and must declare `runtime = "local-pty"`; others are
refused by name. After a registry change, see [Updating the service](#updating-the-service).

Repeating a request with the same `request_id` and inputs returns the existing run (same run id, pid,
workspace); different inputs are refused. That is the recovery for a reload, reconnect or retry.

On a card page the state column is what the process did (`running`, `finished`, `process_failed`,
`source_unavailable`, `unknown`, with `(open)` or `(over)`); the outcome column is what it produced
(verdict, result summary, exit status).

### Opening a sprint from the browser

Routes `GET /sprints/new`, `POST /sprints` and `GET /sprints/{ref}`; contract in
[Protocols](PROTOCOLS.md#opening-a-sprint-from-a-browser). The form offers this installation's products,
open issues, registered projects and head profiles. Fill in goal and Definition of Done, tick at least one
issue and one project, choose the observer, and leave worker and reviewer on "the observer chooses" unless
a role must be pinned. It calls the same `sprint_create` operation as the CLI, as role `po`, actor `web`.
The browser does not offer `none`; use `ummanu sprint create --observer none` for that.

"Start this sprint" is the create; the tick raises the observer. The sprint page says:

| what the page says | what to do |
| --- | --- |
| saved — no observer is up for it yet | wait for the next tick; `ummanu sprint status --ref REF` agrees |
| running — an observer head is up | nothing |
| stopped — an observer was raised for it and is not alive | look at the dispatcher; do not resubmit |
| no observer — this sprint declared none | nothing |
| not established — this could not be read at all | production state unreadable; the sprint fields are still true |

`saved` long after a tick is a dispatcher question, not a create problem.

The form keeps one request id while open, so double submits reach one sprint. If it says the sprint exists
and its request did not finish, submit **the same form again** without reloading; a reloaded form is a new
request id and can create a second sprint. A refusal returns the form with your values, the reason in the
board's words, and a fresh request id (except the part-done case, which keeps id and values); the block at
the top says which.

A POST from another site is refused with 403 based on `Origin`. Clients sending none (`curl`, `ummanu
web-run`, the diagnostics above) are unaffected; to imitate a browser send `-H "Origin: https://HOST"`.

### Updating the service

Code: `ummanu upgrade` moves the checkout and its `web` step restarts and probes the transport
([Updating the published application to `main`](#updating-the-published-application-to-main)).

Head profiles: edit the canonical registry, then materialize:

```bash
$EDITOR INSTANCE/heads/heads.toml
cd ~/ummanu && python3 -P -m ummanu upgrade --instance INSTANCE --no-pull
```

Never edit `<data>/heads/heads.yaml`: it is a generated snapshot pinned by `<data>/heads/source.yaml`, and an edited one
is rejected by the tick. The web process caches the registry, so a regenerated snapshot is a `web` step
restart reason (`the head registry snapshot changed`). If the upgrade stopped before its `web` step, restart
by hand:

```bash
sudo systemctl restart ummanu-web.service            # the front is PartOf= and comes with it
curl -s -o /dev/null -w '%{http_code}\n' localhost:8787/api/system   # 200
```

A `validation` refusal saying a profile "is not launchable" right after a registry change usually means
the process was not restarted.

## The published web front

`ummanu-web-front.service` is Caddy (Ubuntu archive) terminating TLS and checking a password with
`basicauth`, proxying to the loopback transport. The bcrypt hash comes from the secret store. Commands and
guard contract: [Protocols](PROTOCOLS.md#publishing-the-pipeline-the-guarded-front).

**The address** is `https://HOST/` for each rendered `--site`; the account is `owner`. Plain `http://`
redirects.

**First visit.** The certificate comes from Caddy's internal CA, so browsers show a full-page warning
(Firefox *Warning: Potential Security Risk Ahead*; Chrome *Your connection is not private*,
`NET::ERR_CERT_AUTHORITY_INVALID`). The connection is TLS either way; accept the warning (*Advanced* →
*Accept the risk* / *Proceed*) and the password prompt follows.

**Trusting the root.** The root is `DATA_DIR/webfront/caddy/pki/authorities/local/root.crt` on the host.
The front never installs it anywhere. Copy it to the browser's machine:

```bash
# copy it to the machine the browser runs on
scp USER@HOST:ummanu-data/webfront/caddy/pki/authorities/local/root.crt ummanu-root.crt
```

Import it as a trusted **certificate authority**: Firefox *Settings → Privacy & Security → Certificates →
View Certificates → Authorities → Import* with *Trust this CA to identify websites*; macOS Keychain Access
(*System* keychain, *Always Trust*); Chrome on Linux *Settings → Privacy and security → Security → Manage
certificates → Authorities*. Skipping this is fine: only the warning changes.

### Setting or reading the password

Values never travel through argv:

```bash
# the owner types their own, and it is read from stdin
python3 -P -m ummanu web-front set-password --instance INSTANCE --stdin

# or the product generates one from `secrets` and stores it
python3 -P -m ummanu web-front set-password --instance INSTANCE --generate
```

Read the current one back (writes a mode-0600 env file outside the repository):

```bash
python3 -P -m ummanu secret materialize --instance INSTANCE --target file
cat ~/ummanu-data/webfront/owner-password.env      # UMMANU_WEB_FRONT_PASSWORD=...
```

A new password takes effect after render and restart:

```bash
python3 -P -m ummanu web-front render --instance INSTANCE \
  --site https://HOST [--site https://ADDRESS ...]
sudo systemctl restart ummanu-web-front.service
```

### Web front sites

The addresses the front answers on are instance config, `host.web_front.sites` in `instance.yaml`:

```yaml
host:
  web_front:
    sites:
      - "https://HOST"
      - "https://ADDRESS"
      - "https://[IPV6-ADDRESS]"
```

The rendered Caddyfile is data-directory state, outside every checkpoint and snapshot, because it
holds the hash; `instance.yaml` travels with the live root. So `install`, `recover` and `upgrade`
render `DATA_DIR/webfront/Caddyfile` from this list in their `web-front-config` step, before the
`host` step enables and starts the front. The step runs `ummanu web-front render` as the owner of the
installation key (as root it crosses to that account, the way instance Git does), so the file is
written by the account whose Caddy reads it. A file already holding the same text is left alone
(`unchanged web-front-config: … current`); a rewrite makes the `web` step restart the pair.

**Changing them.** Edit the list in `instance.yaml`, as any other instance config, then:

```bash
ummanu upgrade --instance INSTANCE --no-pull     # renders, then restarts the pair
```

`ummanu web-front render … --site …` by hand still works, but the next install, recover or upgrade
renders the configured list again.

**Without the setting.** An enabled front with no `host.web_front.sites`:

- `upgrade` keeps an existing Caddyfile byte for byte and renders nothing
  (`unchanged web-front-config: … kept as it is: host.web_front.sites is not set`); with no file
  either, the step is `skipped` with the same advice, and the `host` step's `caddy validate` fails
  on the missing file as it always did;
- `install` and `recover` refuse at prerequisites (`web-front prerequisite failed: …`), naming the
  setting and the render command, before any live write.

To move an installation that rendered its file by hand onto the setting, copy the site line of the
current file into the list (each comma-separated address becomes one entry) and run the upgrade
above; the same addresses render the same file:

```bash
grep -E '^https://' ~/ummanu-data/webfront/Caddyfile     # e.g. https://HOST, https://ADDRESS {
```

### Starting, updating and stopping

```bash
sudo systemctl status ummanu-web.service ummanu-web-front.service
sudo systemctl restart ummanu-web-front.service       # after a render
sudo systemctl stop ummanu-web-front.service          # off the public interfaces, now
python3 -P -m ummanu status --instance INSTANCE   # both units, enabled and active
```

The front is `PartOf=ummanu-web.service`: restarting or stopping the transport does the same to the
front. Both are `Restart=always` with a three-second delay. Units roll out through `ummanu reconcile
apply`; the Caddyfile does not, because it holds the hash — `web-front render` writes it under
`~/ummanu-data/webfront/`, mode 0600, untracked, from the [configured sites](#web-front-sites). `ExecStartPre` runs `caddy validate`, so a broken
render fails the start instead of taking down a running front.

### Updating the published application to `main`

`ummanu-web.service` runs the product from the configured editable checkout. A running process keeps
the code it imported at start but reads bundled schemas and other lazy files from the checkout as it is
now, so a checkout that moves under a running process can stop it answering. The supported update is one
command:

```bash
ummanu upgrade --instance INSTANCE      # `pull` fast-forwards ~/ummanu onto main
```

Its `web` step runs after `pull`, `dependencies`, `head-registry` and `host` succeed, restarts
`ummanu-web.service` (the front follows) and probes it. A failure in an earlier step stops the run before
the restart. Run the upgrade as the installation owner from the installed checkout
(`/home/dev/ummanu/.venv/bin/ummanu`), never from a task workspace: without `--product-root` it
materializes the configured checkout.

The `host` step uses the complete `packaging/systemd` catalogue of the selected target checkout,
including steward, retro and deep sweep. Component opt-outs and `host.foreign_units` exclude units
from its desired state. The existing managed manifest authorizes writes and runtime repairs;
an installed name alone does not confer ownership. An unchanged owned timer that is enabled but
inactive needs `start`; a disabled required unit needs `enable --now`. Healthy repeated runs leave
both the files and runtime unchanged. `--dry-run` names these repairs without applying them.

Upgrade verify and doctor use `host.unit_runtime_expectations` and `host.assess_unit_runtime`.
Required timers and long-running services must be enabled and active. Timer-triggered oneshot
services remain installed and owned but need neither persistent state. Verify collects fresh
runtime state after materialization, so unchanged bytes and a current manifest cannot hide an
inactive timer or a missing catalogue file. Doctor uses the installation-pinned checkout's
catalogue; upgrade uses its explicit target. Neither selects units from the caller's working tree.

Packaged units use the system manager in `/etc/systemd/system`, with services running as the
resolved installation owner. `infra.systemd.SystemdObservation` supplies the shared bounded probes
through `systemctl --system`, independently of root or another shell account. An execution or
manager/bus connection failure is explicitly unavailable and suppresses inventory comparisons;
a native nonzero status carrying `inactive` is an observed inactive state. The historical
unreachable operator user bus does not establish a user-unit installation or Orca schedule owner.
Fixture and offline diagnostics retain their no-live-probe behavior. Web, memory and PO process
receipts still bind the same PID, kernel start ticks, invocation id and process inputs; their
format and the PO's deferred-until-idle restart are unchanged.

Restart reasons, from repository-relative changed paths:

| reason | what moved |
| --- | --- |
| `product code or dependencies changed` | `src/` (`ummanu`, the background agents' `ummanu.automations` included), `pyproject.toml`/`uv.lock`/`requirements.txt`, or a reinstall by `dependencies` |
| `bundled schemas changed` | `src/ummanu/schemas/` |
| `a web unit file changed` | `ummanu-web.service` or the front unit |
| `the head registry snapshot changed` | `<data>/heads/heads.yaml` regenerated |
| `the web front configuration was rendered` | `web-front-config` rewrote `<data>/webfront/Caddyfile` |

| line | meaning |
| --- | --- |
| `changed   web: restarted ummanu-web.service and probed http://127.0.0.1:8787/api/system -> 200; wrote web process receipt: ...` | replaced, answered, receipt written |
| `unchanged web: web process receipt verified: ...` | the active process generation matches the receipt for this revision and inputs |
| `skipped   web: ummanu-web.service is not installed …` / `… is installed but not active; an upgrade does not start it` | the web receipt step has no active transport to reconcile; `host` starts enabled canonical services and `verify` checks their required state |
| `failed    web: …` | see below |

An empty pull does not prove the process is current: a checkout the dispatcher advanced, or a missing
receipt, makes `--no-pull` restart once. `DATA_DIR/web/process-receipt.json` (mode 0600) is runtime
evidence of the process generation, revision and input hashes, excluded from backups. A missing or
mismatched receipt is stale, never `unchanged`; do not copy or edit it.

`--dry-run` compares `HEAD` with `origin/<branch>`, names the actions the target revision would cause
(`would restart ummanu-web.service and probe ...`), and writes nothing.

`upgrade` restarts onto the dependency set `dependencies` left. That step reinstalls when the venv does
not match the checkout by its receipt ([Upgrade](#upgrade)); it does not inspect a venv edited by hand
behind a matching receipt.

#### When the restart or the probe fails

*The restart failed* (`restarting ummanu-web.service failed: …`): nothing was probed; the old process may
or may not run.

```bash
systemctl is-active ummanu-web.service
sudo journalctl -u ummanu-web.service -n 50 --no-pager
```

*The probe failed* (`ummanu-web.service restarted but the loopback probe failed:
http://127.0.0.1:8787/api/system did not answer 200 within 20s …`): the new process cannot serve. The
message gives the last answer (`HTTP 500` or a socket error). An unexpected failure is a `500` carrying a
reference; the journal line under it names the exception and call site:

```bash
sudo journalctl -u ummanu-web.service -n 50 --no-pager     # the reference, the class, the frames
curl -s -o /dev/null -w '%{http_code}\n' localhost:8787/api/system
```

**Rollback** is the checkout plus `upgrade --no-pull` (`upgrade` is `--ff-only`):

```bash
git -C ~/ummanu log --oneline -3                  # the revision to go back to
git -C ~/ummanu switch --detach <previous-sha>
ummanu upgrade --instance INSTANCE --no-pull
```

`git switch` alone leaves the head-registry pin, units, dependencies and the memory and web processes as
the upgrade put them. `upgrade --no-pull` realigns them against the moved checkout: the venv and the memory
service through their receipts, the web transport through its own (the front is `PartOf=` and comes with
it). See [Taking the slice down, and rolling the application back a
revision](#taking-the-slice-down-and-rolling-the-application-back-a-revision).
For a target revision older than `bb43b5f` (secretary-1743), that revision's `upgrade` has no
dependency or memory receipts. After `ummanu upgrade --no-pull`, also run
`"$HOME/ummanu/.venv/bin/pip" install -e "$HOME/ummanu[dev,memory]"` and restart
`ummanu-memory.service`.

When an upgrade did not finish, or a service was restarted by hand, check whether the process is newer than
the checkout:

```bash
git -C ~/ummanu rev-parse --short HEAD                        # which revision is checked out
git -C ~/ummanu reflog show --date=iso -1 HEAD                # when that checkout last moved
systemctl show -p ExecMainStartTimestamp ummanu-web.service   # when the process started
```

The process start (UTC) must be later than the reflog time (local time with offset). If the reflog has no
entry, ask the running process for a route only the expected code has, for example
`curl -s -o /dev/null -w '%{http_code}\n' localhost:8787/sprints/new`. The head-registry pin in
`<data>/heads/source.yaml` (printed by `ummanu status`) is written early in the upgrade and never proves the
upgrade finished or the process was replaced; a pin ahead of the checkout indicates a `git switch` rollback
without `upgrade`.

A request in flight during the restart fails; a reload a moment later reaches the new code.

### Taking the slice down, and rolling the application back a revision

**Taking the slice down** is `sudo systemctl stop ummanu-web-front.service`; the transport and pipeline
keep running ([Rolling back to before this front existed](#rolling-back-to-before-this-front-existed)).

**Rolling the application back a revision** while staying published: move the tree, then run
`upgrade --no-pull` against it (the upgrade last):

```bash
git -C ~/ummanu log --oneline -10     # `git -C ~/ummanu reflog` says what was installed when
git -C ~/ummanu switch --detach <revision>
ummanu upgrade --instance INSTANCE --no-pull
```

- `upgrade --no-pull` compares the venv and the memory service with the moved checkout through the
  dependency and memory process receipts ([Upgrade](#upgrade)), and reconciles both: it reinstalls the
  product with every extra the installation uses when the dependency manifests differ, and restarts the
  memory service when its revision, code, dependencies, model or pack differ. The `web` step restarts the
  transport through its own receipt. Do not install `[dev]` or restart units by hand.
- An `upgrade` that ends `status: failed` did only the steps printed before the failure; it rolls nothing
  back.

For a target revision older than `bb43b5f` (secretary-1743), that revision's `upgrade` has no
dependency or memory receipts. After `ummanu upgrade --no-pull`, also run
`"$HOME/ummanu/.venv/bin/pip" install -e "$HOME/ummanu[dev,memory]"` and restart
`ummanu-memory.service`.

A detached checkout makes the next upgrade's `pull` refuse by name. Return explicitly, the same way:

```bash
git -C ~/ummanu switch main
ummanu upgrade --instance INSTANCE --no-pull
```

### A snapshot of the whole thing, in one go

No password, nothing written to the installation:

```bash
{
  date -Is
  git -C ~/ummanu rev-parse HEAD
  git -C ~/ummanu status --porcelain
  systemctl show -p ActiveState -p SubState -p ExecMainStartTimestamp \
    ummanu-web.service ummanu-web-front.service
  ss -ltnp '( sport = :8787 or sport = :443 )'
  ummanu status --instance INSTANCE
  ummanu web-front check --instance INSTANCE
  for path in / /api/system /api/tasks/secretary-1/events; do
    printf '%s ' "$path"
    curl -sk -o /dev/null -w '%{http_code} %{size_download}\n' "https://HOST$path"
  done
} 2>&1 | tee ~/ummanu-data/webfront/snapshot-$(date -u +%Y%m%dT%H%M%SZ).txt
```

It shows the served revision and tree cleanliness, both units, that the transport is on `127.0.0.1:8787`
and only Caddy on `443`, the installation view, unguarded routes, and what an unauthorised client gets.

### Auditing what is exposed

```bash
python3 -P -m ummanu web-front check --instance INSTANCE
```

It parses the running configuration against every published route and prints `"unguarded": []`, or exits
3 naming the routes.

Over the wire, an unauthorised client must get 401 and no body:

```bash
for path in / /tasks/secretary-1 /api/system /api/tasks/secretary-1 \
            /api/tasks/secretary-1/events /api/runs/x; do
  printf '%s ' "$path"
  curl -sk -o /dev/null -w '%{http_code} %{size_download}\n' "https://HOST$path"
done
curl -sk -o /dev/null -w '%{http_code}\n' -X POST -d '{}' https://HOST/api/runs/start
```

Every line must read `401 0`. A `200` is an incident: `sudo systemctl stop ummanu-web-front.service`
removes the public listener at once and leaves the pipeline running; then investigate.

### Rolling back to before this front existed

Nothing in the pipeline depends on either unit. In increasing permanence:

```bash
# 1. off the public interfaces, this second; the transport and the pipeline keep running
sudo systemctl stop ummanu-web-front.service

# 2. rehearse or run guarded on loopback only — the same file, one line different
python3 -P -m ummanu web-front render --instance INSTANCE \
  --site https://HOST --bind 127.0.0.1
sudo systemctl restart ummanu-web-front.service

# 3. permanently: disable both halves, then let reconcile remove the units
sudo systemctl disable --now ummanu-web-front.service ummanu-web.service
```

For 3, set `host.components.web.enabled: false` and `host.components.web-front.enabled: false` in
`instance.yaml` and run `ummanu reconcile apply`. Delete `~/ummanu-data/webfront/` if wanted; drop the
password and hash with `ummanu secret remove --id web-front-password` and `--id
web-front-password-hash`. Remove Caddy with `sudo apt-get remove caddy`; `caddy.service` is masked so the
package never starts an unconfigured listener (`sudo systemctl unmask caddy.service` to undo).

### When it is unreachable

| symptom | what it means | what to do |
| --- | --- | --- |
| connection refused / times out from outside | the front is not listening, or the network is in the way | `ss -ltn '( sport = :443 )'`; `sudo systemctl status ummanu-web-front.service` |
| the unit is `activating (auto-restart)` | `caddy validate` refused the configuration | `journalctl -u ummanu-web-front.service -n 50`; re-render |
| `permission denied` binding 443 | the capability is not in effect | `systemctl cat ummanu-web-front.service` must show `AmbientCapabilities=CAP_NET_BIND_SERVICE` |
| a certificate warning that will not go away | no trusted root | see *Trusting the root*; not a failure |
| 401 with the right password | the running configuration is older than the store | re-render and restart; `web-front check` prints the file the unit reads |
| 502 after the password | the loopback transport is down | `sudo systemctl status ummanu-web.service`, then `curl -s localhost:8787/api/system` |
| a section is marked unavailable | a source below the transport refused | see *Diagnosing it* |
| every route answers `500` with `reference: <id>` | an unexpected failure escaped; a stale process against a moved checkout is the known cause | grep the reference in `journalctl -u ummanu-web.service`, then *Updating the published application to `main`* |

SSH is unaffected by the front; if it is wedged, SSH in and stop it.

## Units

Templates are documented in [packaging/systemd/README.md](../packaging/systemd/README.md). Units are rolled
out by `ummanu reconcile apply`; manual installation is neither needed nor a source of ownership. The
production dispatcher timer runs a one-shot tick. Memory, curator, steward and retro each have exactly one
scheduler owner.

### Production interpreter provenance

The dispatcher unit runs an isolated preflight before the `ummanu` entry point, catching an editable
install that points at a task workspace (even a vanished one). A refusal exits non-zero before importing
candidate code, records its classification and metadata target in tick telemetry, turns `triggered-agents
health` red, and gives the steward one incident.

`ummanu doctor --instance INSTANCE` reports `production_runtime_provenance` with the interpreter, product
root and offending target. The only supported repair is the command it prints:

```bash
PRODUCT_ROOT/.venv/bin/python3 -m pip install --no-deps -e PRODUCT_ROOT
```

Use Doctor's product root. Do not run `uv sync`, delete a task workspace, rewrite editable metadata, add
`PYTHONPATH`, attempt an automatic repair, or patch the unit by hand. The next valid tick closes the
incident.

## Upgrade

`ummanu upgrade --instance <dir>` pulls a new product version and re-materialises the installation. It is
idempotent once materialized state, including an active web process receipt, is current.

```bash
ummanu upgrade --instance INSTANCE --dry-run   # decide everything, write nothing
ummanu upgrade --instance INSTANCE
```

Each step prints `changed`, `unchanged`, `skipped` or `failed`; the first failure stops the run:

| step | what it does |
| --- | --- |
| `pull` | `git fetch` plus `merge --ff-only`; a dirty checkout is refused |
| `registries` | read the skill manifest, instance overlay, head canon and memory pack; an unreadable or undeliverable registry stops the run before any write |
| `memory-pack` | materialize the shipped memory pack into the memory canon |
| `runtime-owner` | after a root-run upgrade, give `runtime.env`, `.gitignore` and `.git` back to the runtime user; a malformed `runtime.env` fails here |
| `dependencies` | compare the venv with the checkout through the dependency receipt (tracked-manifest digest, extras, venv path); on a mismatch, a snapshot install or a wrong Ruff pin, `pip install -e <root>[dev,…]` with every extra this installation uses, then write the receipt |
| `dependency-provenance` | import `ummanu`, psycopg, SQLAlchemy and Alembic with `-P` from the selected root and venv |
| `board-store-provision` | no-op before provisioning; otherwise verify/start the pinned `postgres:16` service and volume without rotating credentials |
| `board-store` | connect as owner and apply Alembic to the shipped head |
| `board-store-roles` | verify owner/app/read credentials, attributes and privilege boundaries |
| `memory-clients` | reconcile the `po_memory` MCP entries (Claude, `~/.codex`, the legacy Codex home and an existing `DATA_DIR/codex-home`, seeding what it lacks) without touching provider login state |
| `codex-home` | seed `AGENTS.md` and `config.toml` copy-once into `DATA_DIR/codex-home`; never `auth.json`, never the legacy Orca home ([Codex home](#codex-home-codex_home)) |
| `po-workspace` | materialize the PO head's working directory; its notes file is never rewritten |
| `interactive-workspace` | compose `DATA_DIR/interactive/AGENTS.md` from the product's shared part and the live root's `persona/AGENTS.md`, write `CLAUDE.md`, hand the tree to the runtime user ([The interactive head](#the-interactive-head-and-its-workspace)) |
| `head-registry` | generate `<data>/heads/heads.yaml` and `<data>/heads/source.yaml` from the canon; no Git call |
| `instance-packing` | on a live root that is still a Git work tree, keep its local Git packing controls bounded, with implicit `gc --auto` off (`gc.auto=0`, `maintenance.auto=false`); `skipped` on a plain live root ([Recovery](RECOVERY.md#local-git-packing-controls)) |
| `role-worktrees` | fast-forward role worktrees onto the base branch |
| `pipeline-state` | restore the dispatcher's untracked run journals (`state/pipeline/`) from the instance checkpoint; a live journal that does not extend the checkpoint fails the step and is never overwritten |
| `role-skills` | `role_skills sync` into shell skill directories |
| `po-workspace-owner` | hand the whole PO workspace, delivered skills included, to the runtime user |
| `po-token` | create `DATA_DIR/po-web-token` (0600, runtime user) if absent; an existing token is never rewritten |
| `web-front-config` | render `DATA_DIR/webfront/Caddyfile` from `host.web_front.sites` before `host` starts the front; with no sites in instance config the existing file is kept byte for byte |
| `host` | `reconcile apply`: units from `packaging/systemd` |
| `memory` | start a stopped memory service; restart an active one whose process receipt is missing, belongs to another process or binds another revision, source, dependency digest, `MEMORY_MODEL` or pack digest (or whose code, unit or pack this run changed); then a bounded `memory_list` read and a new receipt |
| `po` | start a stopped `ummanu-po.service`; restart an active one whose process receipt is missing, of another process or bound to another revision or digest, without killing a running PO turn (idle, it restarts now; busy, the restart is reported deferred); skipped when the component is opted out |
| `web` | for an active transport, verify its process receipt or restart, probe loopback, write the receipt after 200 |
| `verify` | repeat dry run; the second rollout must be a no-op |

Flags: `--no-pull`, `--base-branch`, `--product-root`, `--runtime-user`, `--json`.

`dependencies` and `memory` decide by installed state, not by what this run's `pull` moved, so a checkout
moved outside `upgrade` (a manual reset or pull, `--no-pull`, a recreated checkout) is caught by the next
run. Each line names what it compared, for example `unchanged dependencies: venv matches checkout (deps
sha256 1a2b3c4d5e6f, extras dev,memory)` or `changed dependencies: …: deps sha256 1a2b3c4d5e6f ->
4d5e6f7a8b9c`. The required extras are `dev`, plus `memory` when the memory unit is installed or active,
plus any declared extra whose distributions the venv already carries. The receipts are
`DATA_DIR/upgrade/dependency-receipt.json` and `DATA_DIR/upgrade/memory-process-receipt.json` (mode 0600,
excluded from backups). A missing, unreadable or malformed receipt means the work is done, never
`unchanged`; do not copy or edit them.

**First run after this change.** An installation has no receipts yet, so its first upgrade (the final
upgrade included) reinstalls the product into the venv once and restarts the memory service once, then
writes both receipts. The next run reports both steps `unchanged`.

The packaged systemd timers are the only schedule owner of the background roles (curator, retro,
steward). Before sprint:1459 they ran as Orca automations; upgrade no longer creates, repoints or
deletes any, and `doctor` does not report them.

When `pull` advances the checkout, the process re-executes `python -P -m ummanu` from the pulled checkout
with the same arguments and changed paths, so steps new in that revision run in the same upgrade.
`--no-pull` runs the current schedule once; `--dry-run` fetches and reports without moving anything.

If `host` reports `unowned names in our namespace`, resolve it as in
[Ownership and fail-closed behaviour](#ownership-and-fail-closed-behaviour).

### Upgrading from another checkout

`--product-root` names the checkout to install; every step reads only it (skill manifest and roles,
`packaging/systemd`, agent specs, role worktrees, and its head canon when the installation owns none).
`ummanu role-skills audit|sync --product-root <checkout>` delivers skills alone.

Without `--product-root`, install and upgrade materialize the configured checkout (`UMMANU_REPO`, else
`$HOME/ummanu`), not the directory the command runs in. `install` and `recover` refuse a path with no
product. A first install from a checkout other than `~/ummanu` names it with `--product-root`.

The selected checkout is written into the dispatcher unit as `UMMANU_REPO` and rendered into every
launched head's command line, so heads import the installed product.

### Path precedence

No absolute product path is shipped. First hit wins:

| what | order |
| --- | --- |
| the installation | `--instance` / `UMMANU_INSTANCE`, else `~/ummanu-data/instance` (refused when absent) |
| the product checkout a head imports | `UMMANU_REPO`, else `$HOME/ummanu` |
| the checkout an install or upgrade materializes | `--product-root`, else `UMMANU_REPO`, else `$HOME/ummanu` |
| the product skill manifest | `--product-root`, else `UMMANU_ROLE_SKILLS_MANIFEST`, else the configured checkout's |
| the checkout a launcher starts a role out of | `TA_RUNTIME_PYTHONPATH`, else `UMMANU_REPO`, else `$HOME/ummanu` |
| the packaged units a plan or a doctor run compares against | the checkout named by the command, else the one `<data>/heads/source.yaml` recorded, else `UMMANU_REPO`, else `$HOME/ummanu` |
| the account an upgrade materializes for | `--runtime-user`, else the owner of the instance directory |
| a skill's shell root | the manifest's `root`, expanded against the installation owner's home |
| a skill's command link | `UMMANU_BIN_DIR`, else `<owner home>/bin` |
| a role worktree | `TA_WORKSPACES_ROOT`, else `<owner home>/orca/workspaces` |
| the role runtime env file | `UMMANU_RUNTIME_ENV_FILE`, else `TA_RUNTIME_ENV_FILE`, else `<instance>/runtime.env` |
| the head registry a tick reads | `TA_HEADS_REGISTRY`, else the selected instance's `<data>/heads/heads.yaml` (a missing one is an error naming `ummanu upgrade`), else, with no instance selected, the running checkout's default |

`~` in a shipped manifest and `$HOME` in a shipped entry point mean the installation owner's home, resolved
once per upgrade, so a repair run as root writes under the owner rather than `/root`. Skill sources resolve
beside their manifest. `ummanu role-skills sync` run by hand uses the caller's home. Nothing falls back
to the checkout the running module was imported from; an offline `doctor` compares against the checkout
recorded in `<data>/heads/source.yaml`.

### The installation's head registry

A live tick reads only the installation's `<data>/heads/heads.yaml` and matching `source.yaml` (canon,
checkout, revision, snapshot digest); a stale or incomplete pair fails before routing. Only `ummanu
upgrade` (and `recover`) writes that pair, as generated state in the data directory that is never
committed or pushed, so editing a product checkout's canon does not affect a running installation.

A live root's own `heads/heads.yaml` and `heads/source.yaml` are never read. When
`<data>/heads/heads.yaml` is absent, every reader fails with an error that names `ummanu upgrade
--instance LIVE_ROOT`, which generates the pair ([Recovery](RECOVERY.md#fresh-install-and-recovery)).

An installation owns its registry by keeping `heads/heads.toml`; otherwise it materialises from the
product's small shipped default (a Claude and an OpenAI subscription, cross-family fallbacks, one default
per role, no installation policy). A present but unusable `heads/heads.toml` fails the upgrade by name.

The old top-level `heads` array in `instance.yaml` remains accepted but is ignored; it does not select
models or create systemd services. Existing `systemd:head:*` ownership records and their unit files
are retained. If a packaged unit needs the same name, reconcile refuses until the operator explicitly
resolves that legacy ownership, even when the old unit file is missing.

`ummanu status --json` returns `installation.head_registry` (`snapshot`, `canonical`, `canonical_owner`
`instance`/`product`, `product_root`, `revision`, `error`). `error` is set when the pin was never written on
this version or the snapshot is broken.

`[role_defaults]` routes worker and reviewer heads and the curator, retro and steward heads; it does not
route observers (`role_defaults.observer` only labels an observer record with no sprint to read). An
`automation.toml` `head` is a last resort. Packaged role units export `UMMANU_INSTANCE` and their
`runtime.env` path; dispatcher-launched heads get both on their command line. `UMMANU_INSTANCE` in a
`runtime.env` never overrides them.

### Manual curator routing in an instance canon

This is a deferred, manual operator procedure for an installation whose private
`INSTANCE/heads/heads.toml` already declares the Terra tier `profiles.codex-terra-high`. It changes only that instance
canon. Do not add the curator's profile choice, its model, or its account policy to
`src/ummanu/runtime/heads.toml`: the product file remains the portable fallback for an installation
with no canon of its own.

Before changing the role default, record the current `role_defaults.curator` as `PREVIOUS_PROFILE`. Inspect the
existing `profiles.codex-terra-high` without editing it: it must remain a Codex profile with
`model = "gpt-5.6-terra"` and `effort = "high"`, and its declared `fallback` sequence must name existing profiles.
The fallback is instance policy. Record its current order and do not invent, delete, or reorder it as part of this
routing change. If the profile is missing, malformed, has a different model or effort, or has an invalid fallback,
stop. That is a separate canon-policy decision, not a reason to edit the portable registry or make a replacement
profile here.

Change the existing instance table only as follows:

```toml
[role_defaults]
curator = "codex-terra-high"
```

`ummanu-curator.timer` is the sole scheduler owner when the curator component is enabled. The Orca curator
automation must remain disabled: it is a leftover of the schedule before sprint:1459, and removing it is a PO action
(A20 step 10). This installation's curator component must remain disabled for this deferred route change. Verify the latter read-only against the selected installation:

```bash
UMMANU_INSTANCE=INSTANCE python3 -P -m ummanu automations health
```

The output must retain the `DISABLED curator` line. Do not change `host.components.curator`, run `systemctl`, start or stop a service or timer, invoke the curator, run a production
baseline/backfill, write or delete a fact, reindex, or run a canary. Routing a role authorizes none of those actions.

After a separately approved instance-canon edit, materialize it manually with the normal instance rollout, for
example `ummanu upgrade --no-pull --instance INSTANCE --product-root PRODUCT_ROOT`. Confirm with
`ummanu status --json --instance INSTANCE` that the head-registry canonical owner is `instance` and that the new
snapshot was written. The routing assignment has no automatic rollout, shim, migration, or dependency step. The
routing change takes effect only for a later eligible scheduled run; it does not justify a
manual invocation. To roll back, restore `role_defaults.curator = "PREVIOUS_PROFILE"` in the same private canon,
leave `profiles.codex-terra-high` and its fallback untouched, repeat that same manual materialization, and confirm the
resulting instance snapshot. Do not delete the profile or alter scheduler ownership during rollback.

The role route does not widen the curator protocol. A fact-bearing pending batch remains bound to its curator workspace, run and
session identity, its selected-project or all-backlog selector, and its starting cursors. Replay or advance with a
different identity or selector fails closed.

A role-workspace move is the one supported resolution of a foreign identity: run `ummanu automations curator rebind
--dry-run`, read the plan, then `ummanu automations curator rebind` with the curator's identity (from the role
workspace, or with `TA_CURATOR_WORKSPACE` set to it). Run it after a move of the curator's workspace and Claude
project directories, such as the sprint:1475 rename, and before the next tick, which otherwise refuses with
`belongs to a different run identity` and names this verb. It follows only the known moves
(`transition.rewrite.claude_move_paths`), never a string replace, and runs under the cursor-settlement lock:

- the pending identity is rebound only when its workspace is a known old path that no longer exists and the rebound
  identity is the current run's; the facts in the batch are kept, its cursor keys follow the same moves and its batch
  id is re-signed. A workspace outside the moves, an old workspace that still exists, or a pending starting cursor
  that disagrees with the carried cursor is refused with a named reason, and nothing is written;
- a watermark cursor under a moved Claude project directory goes to its new key only when the old file is absent,
  the new file exists, and the new key holds no cursor or one equal to or behind it. A new key already ahead wins and
  the old key is dropped; a cursor never moves backwards. Keys outside the moves, keys whose old file still exists
  and cursors of different kinds are left as they are. A carried transcript is read from its cursor, so only its new
  turns are harvested;
- a run that changes state first appends one `rebind` line to `runs.jsonl` (append-only: never read or replaced)
  with the counts (rebound, carried, superseded, pending keys, skipped by reason), the from/to workspace and
  `state_digest`, the digest of the `pending.json` and `watermark.json` it is about to publish; only then does it
  publish them. A failed append changes nothing. A failed publication is rolled back and followed by a `rebind-failed`
  line with the same digest; the retry appends a new `rebind` line, and the applied attempt is the one whose digest
  matches the published state. A second run finds nothing and writes nothing. `--dry-run` writes nothing.

`ummanu doctor` reports `automation_busy_without_advance` for the curator when its supervised head has been answered
`supervised-busy-skip` for longer than `BUSY_WITHOUT_ADVANCE_HOURS` (6 h) with no `advance` or successful
`memory_write` in `runs.jsonl` and no head started since. The head is stuck: inspect and stop it rather than wait.
A head that only finished its turn and sat idle (a Codex TUI keeps its composer open after the turn) is
not this finding any more: the next tick ends it with `stop_if_quiescent` once its supervisor shows no turn
open, no delivery in flight and no output for `IDLE_HEAD_GRACE_SECONDS` (10 min), logs
`supervised-idle-stop` with the retired run id, and raises a fresh head in the same tick.

A later intentional baseline is a separate manual operation. It requires one registered canonical project
or the reserved `review:po` selector,
an explicit actor, a non-empty reason, and exactly one current opaque cutoff or pending-batch identity. It cannot
bypass a pending record or use all-backlog mode. The baseline audit records the project, actor, redacted reason,
evidence identity, outcome, and hashed cursor identities/count only. Legacy line watermarks remain readable only through the released conversion
path; unversioned, stale, foreign, corrupt or cursor-only pending state, a changed source, an incomplete tail, or a
failed write is refused and left for manual resolution rather than guessed forward. The detailed protocol is in
[Memory](PROTOCOLS.md#memory) and [Project baseline settlement](PROTOCOLS.md#project-baseline-settlement).

A broken snapshot stops the tick and names the reason (missing table, wrong shape, unknown resource or
adapter, a role default naming a missing head). A process given `UMMANU_INSTANCE` whose snapshot is
missing or unreadable fails on that path; the shipped registry is only for a checkout with no installation
selected. The dispatcher answers `invalid_heads`; the fix is `ummanu upgrade`.

### Ownership and fail-closed behaviour

`reconcile apply` writes only what the managed manifest confirms. A name under the unit prefix that is in
neither the plan nor the manifest is a conflict, and any conflict aborts the run before the first write.
Resolve it:

- the unit is ours and matches the packaged file byte for byte:
  `ummanu reconcile adopt --instance <dir> --logical-id systemd:unit:<name> --yes`;
- the name belongs to something else: list it in `host.foreign_units` in `instance.yaml`.

A differing unit is not adopted: remove it and let `apply` install the canonical one, or find out why the
host diverged. An `orca` record an older reconcile left in the managed manifest is kept, untouched
(A20 step 8).

Switch off a component in config, not by removing its unit:

```yaml
host:
  components:
    curator:
      enabled: false
      reason: "load shedding"
```

A disabled component whose unit is installed and owned is stopped and removed.

### Health suite

A deterministic gate before and after an upgrade:

```bash
ummanu doctor --instance <dir>
ummanu role-skills audit --check
ummanu dispatcher production-tick --instance <dir> --probe
python3 -m tests.broad
```

The `ummanu` commands check the installation; `python3 -m tests.broad` runs the `unit` and `component`
suites of the code it runs, not repository-wide discovery. This is an operator gate, not the test contract:
that is the dispatcher-owned exact-SHA CI run ([Testing](TESTING.md)). When an upgrade touches packaging,
recovery, memory, the local-PTY runtime or the board seam, run that suite directly, for example `python3
scripts/ci_test_shards.py packaging`.

`--probe` is a real dry tick: same lock, guards, card scan and decisions, but the first write aborts and is
reported as what the next tick would do.

### Worker-local broad receipt

Receipt ownership and travel are in [Receipt names](PROTOCOLS.md#receipt-names); broad-check handling in
[Protocols](PROTOCOLS.md#broad-check-handling).

```bash
python3 -m ummanu check broad --module tests.broad
python3 -m ummanu check show --module tests.broad
```

When the registered project's adapter declares a `broad_check` module, `ummanu check broad --reuse` and
`ummanu check show` run it with no flag; `--module` overrides. A project that declares none and passes
none is refused as `no_broad_check_module`. Task-packet commands use the registered production source and
interpreter with `-P`; when a contract omits `broad_check.interpreter`, the inner suite uses
`.ummanu-task-env/venv/bin/python3`.

`check broad` streams output to stderr, exits with the check's status (`128+N` for a signal), and writes one
receipt under `state/checks/` in the workspace (ignored, never committed): check set and digest, working
directory, import provenance, timing, exit code, parsed verdict and counts, bounded output tail. In a workspace
whose `.ummanu-task-env/` the dispatcher owns, writer, `check show` and `--reuse` all use
`.ummanu-task-env/checks/` instead, so owned cleanup removes the receipt with the namespace. A raw exit
code that disagrees with the runner's result is refused as `receipt_status_mismatch`.

Two shapes:

- `--module unittest` (with `--module-arg`) records working directory, interpreter and project package
  import. An adapter sets `broad_check.interpreter` (relative to the workspace unless absolute) and
  `broad_check.import_package`. Every registered project that gets cards must declare `broad_check`;
  otherwise `broad_check_not_declared`, here and at the dispatcher preflight. A checkout matching no
  registered project uses the CLI default (`module_contract.source: cli_default`, reason
  `no_project_binding` or `project_binding_disabled`). Adding `broad_check` changes the adapter digest, so
  run `project gate` again. An interpreter that cannot start gives `interpreter_start_failed`, exit 2, no
  receipt.
- `--command '<shell>'` records `origin: unobservable`, claims no import and is never reused.

The dispatcher's preflight refuses an unavailable or invalid adapter, a missing or incomplete `broad_check`,
and an absolute interpreter that cannot start, before any workspace or head, with the infrastructure class
([Bring-up outcomes](PROTOCOLS.md#bring-up-outcomes)). A relative interpreter is resolved later in the
workspace, which is why relative spelling is recommended.

A receipt may replace a run only when the check imported the configured package from this workspace. A
missing or unreadable record, an empty or unresolvable path, a path outside the candidate (for example via
`PYTHONPATH`), or an import from the interpreter's own environment such as `.venv/.../site-packages` is
refused for reuse.

A check is identified by its structured check set, not its rendering: `--module-arg 'one two'` and
`--module-arg one --module-arg two` are different checks.

`check show` runs nothing. It compares the recorded git tree id (tracked edits and untracked files included)
with the current one and exits non-zero when they differ. A truncated or edited receipt, a killed or
timed-out run, an unresolvable checkout, an import from outside the candidate, or a shape that attests no
import is "not usable". `load_receipt` also refuses result combinations no run could produce. `check broad
--reuse` skips the run exactly when `check show` would call the receipt usable.

### Head readiness

Before a worker, reviewer or observer launch the dispatcher probes the profile's resource from
the installed `heads.yaml`. Verdicts are cached in the data directory for 300 seconds:

```bash
ummanu dispatcher resource-health --instance <dir>
```

- `ready` allows a launch.
- `unauthenticated`, `unavailable`, `exhausted` forbid it. A repeat worker launch on a taken card blocks it;
  an observer launch is deferred.
- `unknown` (unclassifiable or timed out) does not forbid a launch.
- `probe_broken` (command, interpreter or import missing) forbids a launch. Probes run with the
  dispatcher's interpreter directory first on `PATH`.

`ummanu doctor` reports every resource's probe and names broken probes as findings, reusing a fresh
dispatcher verdict; `--offline` reports only what is recorded.

For a card in Ready, a forbidden verdict walks the registry's fallback chain to the first head whose resource
allows a launch; the tick, a card comment and the reviewer's document name the substitution. No launchable
head, or a fallback that would make worker and reviewer the same head, leaves the card in Ready with the
reason under `skipped_ready`, and the scan continues with the next card.

- `unauthenticated`: re-authenticate that runtime's CLI in the profile's runtime home, then wait for the TTL.
- `unavailable`: do not restart cards; check provider status and re-read after the TTL.
- `exhausted`: wait for the quota; cards with a fallback already moved.
- `probe_broken`: run the registry's probe string by hand under the dispatcher's environment and repair
  what it names; `doctor` prints the failing line.

### Head status in a live workspace

Whether a workspace that looks empty has a head:

```bash
ummanu head-status --instance <dir> --workspace <path>
```

It prints one row per dispatcher-held head (worker and reviewer apart) with an actionable `summary`. Exit 0
for an answer, 3 for degraded (no workspace path, or a host in `noop` mode). No held head means no rows.

- `head` — `alive`, `absent` or `unproven`, from the vitality snapshot only and bound to the head's
  `run_id`. `alive`: heartbeat process running or suspended, or an advancing provider cursor bound to the
  run. `absent`: from the heartbeat alone. Anything else is `unproven`, with `unavailable_sources` and
  per-source `evidence`.
- `episode` — the persisted vitality conclusion: `quiet_seconds`, `dark_progress_sources`,
  `missing_progress_sources`, `last_progress`, and `next_recovery_deadline` (or `null` with
  `deadline_note`). Ladder semantics: [Head vitality](HEAD_VITALITY.md).

A head on the `local-pty` runtime (worker, reviewer or sprint observer; `kind` names which) is read from
its own supervisor (`runtime: local-pty`, the backend its recorded run names): `process` and `heartbeat`
from its launch identity (state, pid), `supervisor` from the supervisor's `status` (`alive`,
`turn_open`, `turn`, `draining`, `stopping`), `lease` from the kernel's lock table (`held` with
`holder_pid`, or `free`), and `journal.tail`, the last eight journal records; a journal with skipped,
torn or untimed lines is `degraded`, with the reason. A source that did not answer is listed in
`unavailable_sources`, never read as a gone head. A legacy record (a run on `orca-legacy`, or a head
identity with no durable run) says `runtime: orca-legacy` and `legacy_record: true`, and is read through
its pid heartbeat alone (secretary-1723 removed the pane inventory, A20 step 5).

Readings are advisory. No disconnected or unreadable source is evidence that a head is
absent; never drop the claim, kill the workspace or restart the card on that basis. The command only reads:
no lifecycle call, no rebinding, no harder probing.

The web card page lists the card's heads under **Heads** (role, run id, state); each local-pty one links to
`/tasks/<ref>/heads/<run_id>` (JSON: `/api/tasks/...`), a read-only view with no input or control and the
journal tail. The page does not render the head's PTY output.
