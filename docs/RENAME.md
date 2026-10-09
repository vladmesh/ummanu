# Rename: secretary → ummanu

The inventory of the old name and the transition design for sprint:1475. Owner decisions are in
issue:d627bac6d6fc02421939 and the instance knowledge note
`decisions/2026-10-01-sprint-1475-rename-ummanu-why.md`; they are not reopened here.

New names: package and CLI `ummanu`, env vars `UMMANU_*`, units `ummanu-*`, code in `~/ummanu`, data in
`~/ummanu-data`, GitHub `vladmesh/ummanu`, board product and project `ummanu`, new cards `ummanu-N`.

There is **no alias**. After the transition `import secretary` fails, `SECRETARY_*` variables are
ignored, and nothing falls back to an old path.

The old name may stay (`leave`) in exactly four classes. Every `leave` row below names one of them:

| Class | Covers |
|---|---|
| **H** Hermes | The Hermes Telegram agent `secretary`, its skill roots `~/.hermes/skills/secretary*`, its repos `~/secretary-agent` and `~/secretary-brain` (`vladmesh/secretary-agent`) |
| **I** instance repo | The repository name `secretary-instance`: path `/home/dev/secretary-instance`, remote `vladmesh/secretary-instance`, project id `secretary-instance`, its cards `secretary-instance-N` |
| **R** historical records | Audit events, requests, closed cards and sprints, closeouts, knowledge reports, existing card refs `secretary-N`, git history and branches, transcripts of finished sessions, old backup archives |
| **T** transition code | The one-shot migration module, its shell bootstrap and its tests, and this document (written for the transition, a historical record after it) |

Facts were collected on 2026-10-01 from tree `1e935dd9` and the live host (read-only). Counts move as
cards land; the rename card regenerates the baseline before it rewrites anything.

---

## Inventory

### 0. Grep baseline (product repository)

Command: `git grep -I -i -o secretary` over tracked files. `TASK.md` is untracked and not included.

| Measure | Value |
|---|---|
| Tracked files | 760 |
| Files whose content mentions the name | 653 |
| Occurrences, case-insensitive | 15 263 |
| Tracked paths containing the name | 396 (362 under `src/secretary/`, 18 `packaging/systemd/secretary-*`, 8 `packaging/memory/product-secretary/`, 5 `skills/roles/secretary/`, 2 `scripts/secretary-*.sh`, 1 test fixture) |
| Card refs `\bsecretary-[0-9]+\b` (class R) | 3 767 |
| `secretary-instance` (class I) | 102 |
| Hermes skill targets and roots (class H) | 4, all in `skills/manifest.toml` |
| `import`/`from secretary` lines | 3 611 in 504 files |
| `-m secretary` | 311 |
| `[A-Z_]*SECRETARY[A-Z0-9_]*` tokens | 955 |
| `secretary-data` | 375 |
| `vladmesh/secretary` (excluding `-instance`, `-supervisor`, `-agent`) | 40 |

| Top-level dir | Files mentioning | Occurrences |
|---|---|---|
| `tests/` | 281 | 10 010 |
| `src/` | 306 | 3 279 |
| `docs/` | 13 | 793 |
| `packaging/` | 28 | 182 |
| `skills/` | 9 | 116 |
| root (`README.md`, `CONTRIBUTING.md`, `SECURITY.md`, `pyproject.toml`) | 4 | 97 |
| `scripts/` | 8 | 95 |
| `examples/` | 3 | 9 |
| `.github/` | 1 | 2 |

In the instance repository (`/home/dev/secretary-instance`, 5 456 tracked files) the name appears in
4 162 files and 247 784 times. Almost all of that is `state/board` (3 625 files), `state/knowledge` (358)
and `state/memory` (150). The rest is in about 28 configuration files (§10).

### 1. Python package, module paths, console scripts, metadata

| Item | Found | Decision | Reason |
|---|---|---|---|
| Package dir `src/secretary/` | 362 tracked files | rename → `src/ummanu/` | Package name is the product name |
| Imports `from/import secretary…` | 3 611 lines / 504 files (src, tests, scripts) | rename | Follows the package |
| `-m secretary` in code, docs, skills, units, packets | 311 | rename → `-m ummanu` | CLI entry |
| `pyproject.toml` `[project] name`, `description` ("Portable secretary appliance…") | 1 + 1 | rename | Distribution metadata |
| `[project.scripts]` `secretary`, `secretary-memory-mcp`, `secretary-memory-po-bridge`, `secretary-memory-reindex` | 4 | rename → `ummanu`, `ummanu-memory-mcp`, `ummanu-memory-po-bridge`, `ummanu-memory-reindex` | Console scripts the units and MCP configs execute |
| `[tool.secretary] agent-specs = "src/secretary/automations/agents"` | 1 table | rename → `[tool.ummanu]` and the new path | Read by `upgrade.py:989` |
| `[tool.mypy] files` (65 paths), `[tool.coverage.run] source`, `[tool.setuptools.packages.find] include`, `[tool.setuptools.package-data]` keys | ~72 | rename | Package paths |
| `src/secretary.egg-info/` (untracked, in checkouts) | production and worktrees | rename (regenerated) | Build artefact. Deleted by the transition, recreated by `pip install -e` |
| `.venv/…/__editable__.secretary-0.1.0.pth` → `/home/dev/secretary/src`, `secretary-0.1.0.dist-info`, `.venv/bin/secretary*` with shebang `/home/dev/secretary/.venv/bin/python3` | production venv | rename (venv rebuilt) | A moved venv keeps old shebangs and paths, so it is rebuilt, not moved |
| `dispatch/runtime_preflight.py` `PACKAGE = "secretary"` | 1 | rename | The unit's preflight checks this package |
| `broad_check.py` `import_package` default, `check_commands.py` `CLI_DEFAULT_IMPORT_PACKAGE` | 2 | rename | Import-provenance check of the broad suite |
| `head_registry.py` `HEADS_RELATIVE = src/secretary/runtime/heads.toml`; `role_env.py` docker-bin path; `automations/runtime/{dispatch,health}.py` spec paths; `infra/github_credential.py` source-root probe; `upgrade.py:599,616` provenance names | 7 | rename | Paths into the package |
| `cli.py` `prog="secretary"`, `web/pages.py` `TITLE = "secretary"`, unit `Description=` lines | 3 + 18 | rename | User-visible product name |
| Test fixture `tests/fixtures/local_pty_journals/secretary_1727_9c6b884b.jsonl.gz` | 1 binary | leave (R) | Captured journal of card secretary-1727; the file name carries that card ref |

### 2. Protocol and identity strings in code

"Persisted" means the value is stored outside the code. A persisted value needs a step in the transition
(§T3), not only a code edit.

| String (where) | Persisted? | Decision | Migration |
|---|---|---|---|
| Dispatcher owner `secretary-production` (unit `SECRETARY_DISPATCHER_OWNER`, packaging unit; default `secretary-dispatcher` in `dispatch/commands.py:133`) | `production-state.json` `owner` (257 occurrences incl. records); board `board_events.actor_id`, request ids | rename → `ummanu-production` / `ummanu-dispatcher` | `owner` is overwritten each tick (`production.py:405`), with no lease compare. The transition requires an empty `records` map. Old events and requests: leave (R) |
| Product id `secretary` (`board/schema.py` comments; `memory/pack.py:255` `"product": "secretary"`) | board `products` row, `issues.product_id` (420), `sprints.product_id` (77), `product_projects` | rename → `ummanu` | New row plus translation of live refs (§T2) |
| Project id `secretary` (instance `projects/secretary.yaml`, `adapters/secretary.yaml`; defaults `SECRETARY_META_PROJECT`/`SECRETARY_ORCA_PROJECT` = `"secretary"` in `automations/runtime/dispatch.py:380`, `runtime/shared_state.py:9`) | `projects`, `tasks.project_id` (864 cards), `sprint_projects` (75), `repositories` | rename → `ummanu` | §T2 |
| Card ref prefix `secretary-` (computed `f"{project}-"` in `tasks.next_project_reference`) | `tasks.task_ref` | old refs: leave (R); new: `ummanu-N` | No counter table. The next ref is the board-wide max for the prefix plus one, so `ummanu-1` comes with no migration |
| Branch prefix `pipeline/<ref>`, workspace `<ref>-<slug>` under `workspaces/<project>/` | GitHub branches, workspace dirs | derived; new ones become `pipeline/ummanu-N`, `workspaces/ummanu/…` | Old branches and dirs: leave (R) |
| Memory scopes `product:secretary` (`memory/access.py:32`, `memory/pack.py:35`, `memory_service.py:313`), `project:secretary` (`memory/health.py:175`) | instance `state/memory/facts/product-secretary/` (6), `facts/secretary/` (117), `packs/product-secretary.json`, memory index sqlite, access grants | rename → `product:ummanu`, `project:ummanu` | Move fact dirs, rematerialize the pack, reindex (§T3) |
| Role `secretary` (`skills/manifest.toml [roles.secretary]`, `skills/roles/secretary/`, `memory_write.py` writer roles, `instance.schema.json` head role enum) | memory facts `source: secretary…` (43 facts) | rename → `operator` (proposed; it already holds the same memory rights) | Existing `source:` stamps: leave (R). The role name needs only to drop `secretary`; the observer may choose another |
| Unit prefix default `secretary-` (`automations/runtime/health.py:45`, `SECRETARY_UNIT_PREFIX`) and instance `host.unit_prefix: secretary-` | `/etc/systemd/system`, `host-managed.json` | rename → `ummanu-` | §4 |
| `host.py:239-276` `"managed_by": "secretary"` | `host-managed.json` | rename | Rewritten by the first `ummanu` reconcile |
| Transient scopes `secretary-head-<hash>.scope` (`runtime/head/memory.py:36`, checked by `local_pty/scope_bootstrap.py:45`) | live cgroups only | rename → `ummanu-head-` | None alive after the freeze |
| Pid files `secretary-{kind}-pid-<token>.pid` (`dispatch/watchdog.py:155`) | data dir | rename | Stale files of finished heads: leave (R) |
| Backup archive names `secretary-backup-<kind>-<stamp>.tar`, manifest `"tool": "secretary"` (`backup.py:106,249`) | `backups/`, offsite | rename | Old archives: leave (R). They restore with the pre-transition code only, which is the rollback |
| Secret store: `VERIFIER_AAD = b"secretary/installation-key/v1"`, `VALUE_KDF_INFO = "secretary/secret/v1"` (`secret_store.py:84,89`), formats `secretary.installation-key`, `secretary.secret-envelope` | instance `secrets/installation-key.json` and 4 `secrets/values/*.enc.json` (cryptographically bound) | rename | The transition re-wraps the key and re-encrypts the 4 values; old constants live only in the transition module (§T3) |
| Board store: `dbname="secretary"` (`board/store.py:141`), roles `secretary_owner/app/read` (`board/schema.py:1019-1021`, `store.py:142-146`), compose project `secretary-board-store` and `DEFAULT_COMPOSE_PATH=/opt/secretary/postgres-compose.yml` (`board/provision.py:22-24`), label `secretary.production-board` | live Postgres | rename | §6, §T2 |
| Web front cookie `__Host-secretary_front` (web front render) | owner's browser, `webfront/Caddyfile` | rename | The owner logs in once more |
| Data-plane schema tags `secretary.data`, `secretary.webproto`, `secretary.test-board`, `secretary.reference-repair-plan` | data-dir JSON files | rename | Rewrite live files in the transition, or have the new code recreate them; never read them under both names |
| Workspace env dir `.secretary-task-env/` (dispatcher-created, git-ignored) | each workspace | rename → `.ummanu-task-env/` | Old workspaces: leave (R) |
| Temp prefixes `.secretary-pg-restore-` etc. | none | rename | — |

### 3. Environment variables

`git grep` finds 93 distinct tokens matching `\b(TA_)?SECRETARY_[A-Z0-9_]*`: the env vars below, the unit
template placeholders, a few Python constants, and one retired name that a test bans from `src/` and
`docs/`. Every env var is renamed to `UMMANU_*`, and `TA_SECRETARY_REPO` becomes `TA_UMMANU_REPO`. None is kept as a fallback. Other `TA_*`
names (`TA_RUNTIME_ENV_FILE`, `TA_RUNTIME_PYTHONPATH`, `TA_CLAUDE_PROJECTS_DIR`, …) and `MEMORY_*` do not
contain the name and stay as they are.

| Group | Names | Where set |
|---|---|---|
| Installation identity | `SECRETARY_INSTANCE`, `SECRETARY_DATA_DIR`, `SECRETARY_RUNTIME_ENV_FILE`, `SECRETARY_UNIT_PREFIX`, `SECRETARY_BIN_DIR`, `SECRETARY_CHECKPOINT_INSTANCE`, `SECRETARY_GITHUB_BOOTSTRAP_FILE`, `SECRETARY_ROLE_SKILLS_MANIFEST`, `TA_SECRETARY_REPO` | 9 live units set `SECRETARY_INSTANCE` + `TA_SECRETARY_REPO`; 2 set `SECRETARY_DATA_DIR`; 1 sets `SECRETARY_RUNTIME_ENV_FILE`; CI sets `TA_SECRETARY_REPO` (`.github/workflows/ci.yml:13`); packets use `${TA_SECRETARY_REPO:-$HOME/secretary}` |
| Board store (`board-store.env`) | `SECRETARY_DB_HOST`, `_PORT`, `_NAME`, `_OWNER_USER`, `_OWNER_PASSWORD`, `_APP_USER`, `_APP_PASSWORD`, `_READ_USER`, `_READ_PASSWORD` | instance `board-store.env` (mode 0600, ignored); interpolated by `/opt/secretary/postgres-compose.yml` |
| Dispatcher | `SECRETARY_DISPATCHER_OWNER`, `_AUTOMERGE`, `_BODY_DIR`, `_HOST_MODE`, `_PROMPT_DIR`, `_REVIEW_COMMAND`, `_WORKER_COMMAND`, `_WORKSPACES_ROOT`, `SECRETARY_LEGACY_PAUSE_FILE`, `SECRETARY_LEGACY_PIPELINE_STATE_DIR`, `SECRETARY_META_PROJECT`, `SECRETARY_ORCA_PROJECT` | unit (owner), tests |
| Timeouts and limits | `SECRETARY_HEAD_IDLE_STALL_SECONDS`, `_HEAD_SUSPENSION_RESPONSE_SECONDS`, `_INITIAL_OUTPUT_STALL_SECONDS`, `_WORKER_REPORT_STALL_SECONDS`, `_REVIEW_VERDICT_STALL_SECONDS`, `_REVIEW_INFRA_RETRY_ATTEMPTS`, `_REVIEW_LAUNCH_ABORT_STUCK`, `_GATE_PENDING_STALL_SECONDS`, `_GATE_TRANSPORT_MAX_ATTEMPTS`, `_GATE_INFRASTRUCTURE_RERUN_MAX_ATTEMPTS`, `_GATE_LOG_FRAGMENT_LINES`, `_LAUNCH_DELIVERY_MAX_ATTEMPTS`, `_POST_MERGE_CI_CEILING_SECONDS`, `_RELEASE_MIGRATION_LOCK_TIMEOUT_SECONDS`, `_OBSERVER_ACK_DEADLINE_SECONDS`, `_OBSERVER_TURN_CEILING_SECONDS`, `_OBSERVER_UNPROVEN_TURN_CEILING_SECONDS`, `_OBSERVER_WAKE_MAX_ATTEMPTS`, `_PR_BODY_SECTION_CHARS` | instance `runtime.env` sets `SECRETARY_HEAD_IDLE_STALL_SECONDS` |
| Head and run context | `SECRETARY_ROLE`, `SECRETARY_RUN_ID`, `_RUN_REF`, `_RUN_ROLE`, `_RUN_RESULT`, `_RUN_WORKSPACE`, `SECRETARY_OBSERVER_SPRINT`, `_OBSERVER_GENERATION`, `SECRETARY_PO_SESSION`, `SECRETARY_PO_REQUEST`, `SECRETARY_UPGRADE_HANDOFF`, `SECRETARY_SCOPE_LAUNCH_BINDING`, `SECRETARY_OOM_STREAM_FD`, `SECRETARY_REPO_ENV` | written by the product into head environments |
| Docker shim | `SECRETARY_DOCKER_BACKEND`, `_DOCKER_LOCAL_RUN_POLICY`, `_DOCKER_PYTHON`, `_DOCKER_SOURCE` | `runtime/docker-bin/docker`, role env |
| Sessions | `SECRETARY_CLAUDE_PROJECTS`, `SECRETARY_CODEX_SESSIONS` | operator override |
| Memory | `SECRETARY_MEMORY_URL`, `_MEMORY_ACCESS_TOKEN`, `_MEMORY_TEST_PYTHON`; placeholders `{{SECRETARY_MEMORY_MODEL/DIM/THREADS}}` | `.mcp.json` of the PO workspace, `~/.codex/config.toml` MCP block, memory unit |
| Web | `SECRETARY_WEB_FRONT_PASSWORD` | instance `secrets/catalog.yaml:7` |
| E2E and benchmarks | `SECRETARY_E2E_IDENTIFY_SECONDS`, `SECRETARY_E2E_RECOVERY_SETTLE_SECONDS`, `SECRETARY_FULL_BULK_BENCHMARK` | tests, scripts |

Also renamed: the unit-template placeholders `{{SECRETARY_PRODUCT_ROOT}}`, `{{SECRETARY_INSTANCE_PATH}}`,
`{{SECRETARY_DATA_DIR}}`, `{{SECRETARY_RUNTIME_USER}}`, `{{SECRETARY_RUNTIME_HOME}}`, and the `host.py:137`
check for unrendered `{{SECRETARY_`. These are not env vars but carry the name. Python constants carrying
the name are renamed too (`PRODUCT_SECRETARY_SCOPE`, `PROJECT_SECRETARY_SCOPE`, `DEFAULT_SECRETARY_INSTANCE`,
`SECRETARY_RUNTIME_ENV_FILE_ENV`, `SECRETARY_DB_READ_URL`, `SECRETARY_DB_EXTRA`, `SECRETARY_SOURCE_SHELL`), as are
names that only tests use (`SECRETARY_UNSET_COMMAND`, `SECRETARY_PO_MARK`, `SECRETARY_ORCA_EXECUTABLE`, and the
switches named after cards, `SECRETARY_1698_…` and `SECRETARY_1702_…`).

Live `runtime.env` holds two names (values not copied here): `SECRETARY_HEAD_IDLE_STALL_SECONDS` →
`UMMANU_HEAD_IDLE_STALL_SECONDS`, and `SECRETARY_CARD_BACKEND`, which no current code reads. Drop it.

### 4. systemd units and timers

Live `/etc/systemd/system/secretary-*`: 18 files, matching `packaging/systemd/` one for one.

| Unit | Executes (live) | Decision |
|---|---|---|
| `secretary-dispatcher-production.{service,timer}` (60 s) | `/home/dev/secretary/.venv/bin/python3 -I /home/dev/secretary/src/secretary/dispatch/runtime_preflight.py … -- /home/dev/secretary/.venv/bin/secretary dispatcher production-tick` | rename → `ummanu-dispatcher-production.*` |
| `secretary-doctor.{service,timer}` (≈60 s) | `.venv/bin/secretary doctor-record` | rename |
| `secretary-instance-maintenance.{service,timer}` (04:17) | `.venv/bin/secretary instance-maintenance` | rename → `ummanu-instance-maintenance.*`. The component name means "maintenance of the instance repo" and is not class I; only the prefix changes |
| `secretary-memory.service` | `.venv/bin/secretary-memory-mcp`, `MEMORY_*` paths under `/home/dev/secretary-data/memory` | rename |
| `secretary-po.service` | `.venv/bin/secretary po-serve` | rename → `ummanu-po.service` |
| `secretary-web.service` | `.venv/bin/secretary web-serve --port 8787` | rename |
| `secretary-web-front.service` | `caddy run --config /home/dev/secretary-data/webfront/Caddyfile` | rename |
| `secretary-curator.*` (hourly), `secretary-retro.*` (daily), `secretary-steward.*` (3 h), `secretary-steward-deep-sweep.*` (03:47) | `/home/dev/secretary/scripts/secretary-agent-gate.sh <role>`, cwd `~/orca/workspaces/secretary/<role>` | rename (script → `scripts/ummanu-agent-gate.sh`, cwd → `~/orca/workspaces/ummanu/<role>`) |

Generators: `packaging/systemd/*` (18 templates and `README.md`) rendered by `host_apply` during
`upgrade`/`reconcile apply` with `host.unit_prefix`. The reconciler enumerates only its own prefix, so
once the prefix is `ummanu-` it will neither see nor remove `secretary-*`. The transition removes them
explicitly (§T3).

| Instance config | Value | Decision |
|---|---|---|
| `instance.yaml` `name` | `vladmesh-secretary` | rename → `vladmesh-ummanu` |
| `instance.yaml` `description` | mentions secretary twice | rename |
| `instance.yaml` `data_dir` | `/home/dev/secretary-data` | rename → `/home/dev/ummanu-data` |
| `instance.yaml` `host.unit_prefix` | `secretary-` | rename → `ummanu-` |
| `instance.yaml` `host.foreign_units` | `secretary-supervisor.{service,timer}` (not installed on the host) | rename: drop both entries. They fall outside the new prefix, so the list has nothing left to protect |
| `scripts/secretary-start.sh`, `scripts/secretary-agent-gate.sh` | units and the operator shell | rename → `ummanu-*.sh` |

### 5. Paths

| Path | Contents keyed by the name | Decision | Notes |
|---|---|---|---|
| `/home/dev/secretary` | production checkout, `.venv`, `.git` with 7 worktrees | rename → `/home/dev/ummanu` | `mv` the checkout, rebuild the venv, `git worktree repair` |
| `/home/dev/secretary-data` (5.9 GB) | the whole data plane | rename → `/home/dev/ummanu-data` | One `mv` on the same filesystem. Sub-trees below move with it but have absolute paths inside |
| `…/workspaces/secretary/` | worktrees of `/home/dev/secretary` (2 live) | new cards: `workspaces/ummanu/`; existing dirs: leave (R) | `git -C ~/ummanu worktree repair` on the live ones |
| `…/workspaces/observers/sprint-1475` | worktree of `…/dispatcher/observer-root/observers` | moves with the data dir | `git -C ~/ummanu-data/dispatcher/observer-root/observers worktree repair` |
| `…/workspaces/codegen-orchestrator/*` (5 worktrees of `/home/dev/projects/codegen_orchestrator`) | absolute `gitdir` | move with the data dir | `git -C ~/projects/codegen_orchestrator worktree repair <new paths>` |
| `…/dispatcher/production-state.json` | `owner`, 38 `resume_workspaces` entries and record paths under `/home/dev/secretary-data` | rename | The transition rewrites the path prefix; `records` must be empty (§T1) |
| `…/dispatcher/pause.json` | the pause flag | moves | The legacy mirror `~/orca/workspaces/secretary/pipeline/state/pipeline/pause.json` becomes `…/ummanu/pipeline/…` |
| `…/po/` (`AGENTS.md`, `CLAUDE.md`, `NOTES.md`, `.mcp.json`, `.claude/skills`, `.agents/skills`, `.codex`) | `.mcp.json` names `/home/dev/secretary/.venv/bin/secretary-memory-po-bridge`, the access-grants path and `SECRETARY_MEMORY_URL` | rename (re-rendered by the `memory_clients` and `po_workspace` upgrade steps) | `NOTES.md` is the PO's own notes: its old commands are prose (R) |
| `…/po-service/` (socket, lock, receipt), `po-heads/`, `po-queue/`, `po-runs/`, `po-web-token` | runtime state | move | The socket is recreated by `ummanu-po.service` |
| `…/codex-home/` | Codex home of product heads: `config.toml` has `[projects."/home/dev/secretary"]`, `[projects."…/orca/workspaces/secretary/curator"]` and per-workspace keys; `sessions/` | move; rewrite the live project keys | Codex resumes by id, so the session files are path-independent |
| `…/heads/` (593 run dirs, sockets) | local-pty run records | move | No head alive after the freeze |
| `…/webfront/` (`Caddyfile`, `caddy/` storage with TLS state) | Caddyfile has `storage file_system /home/dev/secretary-data/webfront/caddy` and the cookie name | move; re-render with `ummanu web-front render` | |
| `…/memory/` (index sqlite, export, fastembed cache, access-grants) | paths in the memory unit and MCP configs | move; reindex after the scope rename | |
| `…/board/.create.lock`, `…/data-manifest.json` (`data_dir`), `…/host-managed.json` (23 mentions) | | move; rewrite `data_dir`; `host-managed.json` is rewritten by the first reconcile | |
| `~/orca/workspaces/secretary/{curator,pipeline,retro,steward}` | role worktrees of `/home/dev/secretary`; `PIPELINE_WORKTREE` (`data.py:44`), `cli.py:285`, `pause.py:284`, `recovery_inventory.py:434`, `upgrade.py:1016` | rename → `~/orca/workspaces/ummanu/…` | Remove the old worktrees; `step_worktrees` recreates them |
| `/opt/secretary/` (`postgres-compose.yml`, `orca/`, Orca AppImage; `/usr/local/bin/orca` symlink) | | rename → `/opt/ummanu/` | Repoint the symlink |
| `~/.secretary-tools/` (uv, poetry for adapters of other projects; `~/.local/bin/uv` points into it) | instance adapters `codegen-orchestrator.yaml`, `personal-site.yaml` | rename → `~/.ummanu-tools/` | The adapter setup reinstalls on its first run |
| Defaults in code: `Path.home()/"secretary-data"` (`runtime/references.py:87` and others), `PRODUCT_DIRNAME = "secretary"` (`runtime/paths.py:23`) | | rename | |

**External locations keyed by those paths.** Claude names a project directory after the absolute path of
its cwd, or of the main repository when the cwd is a worktree (`runtime/claude_sessions.py`). Of 543
entries in `~/.claude/projects`, 328 (1.6 GB) contain the name.

| Directory | Holds | Decision |
|---|---|---|
| `-home-dev-secretary-data-po` | **PO memory** (29 files incl. `MEMORY.md`), 126 PO sessions | **move** → `-home-dev-ummanu-data-po`. Without it the new PO session starts with no `MEMORY.md` |
| `-home-dev-secretary-data-po-handoffs` | 3 entries | move → `-home-dev-ummanu-data-po-handoffs` |
| `-home-dev-secretary-data-dispatcher-observer-root-observers` | **observer memory** (28 files), shared by every sprint observer worktree | **move** → `-home-dev-ummanu-data-dispatcher-observer-root-observers` |
| `-home-dev-secretary-data-workspaces-observers-sprint-1475` | session of this sprint's observer | move |
| `-home-dev-secretary` | **interactive and worker memory** (45 files; worker worktrees resolve to the main checkout), 3 164 entries | **move** → `-home-dev-ummanu` |
| `-home-dev-orca-workspaces-secretary-{retro,steward}` (and `curator` if present) | role-agent sessions | move → `…-ummanu-…` |
| `-home-dev-secretary-instance` | | leave (I) |
| `-home-dev-secretary-data-workspaces-<project>-<ref>-*` (72 for project secretary, plus codegen and instance cards), `-home-dev-secretary-data-webproto-*`, `-tmp-*secretary*` | transcripts of finished cards and runs | leave (R) |
| `~/.claude.json` `projects` map | 1 106 of 6 027 keys contain the name (trust and per-path settings) | copy the entries for `/home/dev/secretary` and `…/observer-root/observers` to the new paths; the rest: leave (R) |
| `~/.codex/config.toml` | trust entries for `/home/dev/secretary` and `/home/dev/secretary-data/po`; MCP server `command = /home/dev/secretary/.venv/bin/secretary-memory-po-bridge` with `SECRETARY_MEMORY_URL` | rename (re-rendered by `reconcile_clients`; the transition deletes the stale block) |
| `~/.config/orca/codex-runtime-home/home/skills`, `~/.claude/skills` | role skills whose text calls `secretary` | rename (rewritten by `ummanu role-skills sync`) |
| `~/.hermes/skills/secretary`, `~/.hermes/skills/secretary-roles` | Hermes skill roots | leave (H). Their *contents* are re-delivered by the sync and then call `ummanu` |

### 6. Board store

Live store: container `secretary-board-store-postgres-1` (`postgres:16`), compose project
`secretary-board-store`, file `/opt/secretary/postgres-compose.yml` (env file
`/home/dev/secretary-instance/board-store.env`), volume `secretary-board-store_board-db`, network
`secretary-board-store_default`, port `127.0.0.1:5432`, database `secretary`, roles `secretary_owner`
(bootstrap superuser), `secretary_app`, `secretary_read`. The unrelated container `po-checkpoint-1428-db`
belongs to codegen and is out of scope.

| Item | Decision | How |
|---|---|---|
| Database `secretary` | rename → `ummanu` | Copy into a new store (§T2). Docker has no volume rename, and the bootstrap superuser cannot rename itself |
| Roles `secretary_owner/app/read` | rename → `ummanu_owner/app/read` | Created by migration 0001 from the renamed constants |
| Compose project, file, volume, network, container, label | rename → `ummanu-board-store`, `/opt/ummanu/postgres-compose.yml`, `ummanu-board-store_board-db`, `ummanu-board-store-postgres-1`, `ummanu.production-board` | `board/provision.py` |
| `board-store.env` keys `SECRETARY_DB_*` | rename → `UMMANU_DB_*` | The transition rewrites the keys and keeps the passwords |
| Old volume and container | leave (R) until the sprint closes | The rollback copy; removed by the owner after DoD |

Board identity rows (live counts):

| Row | Live | Decision |
|---|---|---|
| `products` `secretary` ("Secretary") | 1 | add `ummanu` ("Ummanu"); set `secretary` to `archived` (R) |
| `projects` `secretary` (plane `orchestrator`, adapter/binding `secretary`) | 1 | add `ummanu`; set `secretary` to `enabled=false, registry_present=false` (R) |
| `repositories` | `/home/dev/secretary` (primary, remote `vladmesh/secretary`) plus 3 `curator_root` rows for directories that no longer exist; stray rows with paths `/home/dev/secretary/secretary`, `/tmp/secretary`, `secretary` and no project | add `/home/dev/ummanu` (project `ummanu`, remote `vladmesh/ummanu`); old rows: leave (R), referenced by closed sprints |
| `product_projects` | `(secretary, secretary)`, `(secretary, secretary-instance)`, `(cutover-d9821c3499daaf9d, secretary)` | add `(ummanu, ummanu)`, `(ummanu, secretary-instance)`; old: leave (R) |
| Cards on project `secretary` | 864: 716 done, 92 `issues` column, 52 ready, 2 blocked, 2 in progress. 13 not archived and not done: secretary-1472/1473/1476/1482/1495/1518–1522 (`issues`), 1890, 1915 (blocked), 1927 (this card) | refs and project_id: leave (R). Parked cards are re-cut by the PO as `ummanu-N` with `supersedes` if still wanted |
| Ref counter | none; `next_reference` scans the board | nothing to migrate; first new card is `ummanu-1` |
| `sprints.product_id = secretary` | 77 (1 open: sprint:1475) | translate the open sprint only |
| `sprints.allowed_productions ∋ secretary` | 8 (1 open) | translate the open sprint only (`array_replace`) |
| `sprint_projects.project_id = secretary` | 75 (open: sprint:1475 reserved) | translate sprint:1475's reservation |
| `sprint_repositories` → `/home/dev/secretary` | sprint:1475 and closed sprints | repoint sprint:1475 only |
| `issues.product_id = secretary` | 420 (188 open, 232 closed) | translate the 188 open; closed: leave (R) |
| `po_sessions.cwd = /home/dev/secretary-data/po` | 64 (2 open) | translate the 2 open; closed: leave (R) |
| `tasks.extensions.extra.swimlane = secretary` | 316 | leave (R) |
| `board_events` (12 968 total; 7 190 mention the name), `requests` (48 465; 21 491 mention it), comments, resumes, `owner_events` | | leave (R) |

### 7. Docs, README, skills, packaging, tests

| Item | Found | Decision |
|---|---|---|
| `docs/*.md` | OPERATIONS 402, PROTOCOLS 240, HEAD_RUNTIME 78, ARCHITECTURE 57, BOARD_STORE 56, RECOVERY 30, HEAD_VITALITY 23, TESTING 11, ROADMAP 4, VISION 3, REQUESTS_GROWTH 3, OWNED_CLEANUP 3, HEAD_SCOPES 1 | rename (commands, paths, names); card refs: leave (R); this file: leave (T) |
| `README.md`, `CONTRIBUTING.md`, `SECURITY.md` | 97 with `pyproject.toml` | rename |
| `skills/manifest.toml` | `[roles.secretary]`, `secretary/<skill>` entries, targets `claude-secretary-global`, `codex-orca-secretary`, `claude-secretary-{curator,retro,steward,observer}`, `codex-secretary-roles`, roots `~/orca/workspaces/secretary/*` | rename (role → `operator`, targets → `*-ummanu-*`/`*-operator-*`, roots → `…/ummanu/…`) |
| `skills/manifest.toml` `hermes-secretary`, `hermes-secretary-roles`, roots `~/.hermes/skills/secretary*` | 4 | leave (H) |
| `skills/roles/secretary/*` (5 skills), `skills/roles/{observer,steward,curator,retro,po}` | 116 occurrences, mostly CLI calls | rename (dir → `skills/roles/operator/`; calls → `ummanu`) |
| `packaging/po-workspace/AGENTS.md` (11), `packaging/codex-home/{AGENTS.md,config.toml}` | | rename |
| `packaging/memory/product-secretary/` (8 files, `manifest.yaml` namespace) | | rename → `packaging/memory/product-ummanu/`, namespace `product:ummanu` |
| `packaging/systemd/*` | 18 + README | rename (§4) |
| `examples/instance/instance.yaml` (7) | | rename |
| `tests/` | 281 files, 10 010 occurrences; fixtures build fake installs named `secretary`, `secretary-data` | rename, except card refs (R), `secretary-instance` (I) and the binary fixture (R) |
| `scripts/*.py` (`check_memory_mcp_restore_e2e.py` 29, `measure_dashboard.py` 16, `repro_local_pty_retained_continuation.py` 13, others) | | rename |

### 8. GitHub

| Item | Decision | Notes |
|---|---|---|
| Repo `vladmesh/secretary` | rename → `vladmesh/ummanu` (owner: `gh repo rename ummanu -R vladmesh/secretary`) | GitHub redirects the old URL for git and API until a new repo takes the old name. Nobody may create `vladmesh/secretary` again during the sprint |
| `origin` of `/home/dev/secretary` (`https://github.com/vladmesh/secretary.git`); worktrees share that config | rename | `git -C ~/ummanu remote set-url origin https://github.com/vladmesh/ummanu.git` |
| Instance `projects/secretary.yaml` `remote:` | rename | §10 |
| Board `repositories.remote` | new row gets the new remote | §6 |
| Text URLs in code and docs (40 `vladmesh/secretary`) | rename | |
| CI workflows `ci`, `e2e-synthetic` | names unchanged; `ci.yml` comment and `TA_SECRETARY_REPO` env: rename | Branch protection and required checks follow the repo rename |
| Remote branches `pipeline/secretary-N`, PR history | leave (R) | |
| `offsite.instance_remote: …/secretary-instance.git` | leave (I) | |

### 9. Known external breakage (out of scope, fixed after the sprint)

No alias, so these break at the transition. They are fixed later by research and infra cards on their
own projects. These are not regressions of this sprint.

| Repository | Files mentioning `secretary` |
|---|---|
| ai-safety-job-search | 61 |
| secretary-supervisor (also a board project and an instance adapter/project file) | 9 |
| review-value-research | 8 |
| dnd-simulator | 7 |
| codegen_orchestrator | 6 |
| butler | 6 |
| explee-ai-native-test | 5 |
| ai-risk-decision-lab, risk-decision-lab | 2 each |
| codegen-product-kit, public_profile, ru-it-stt-eval, service-template | 1 each |

### 10. `secretary-instance` content

| File | Mentions | Decision |
|---|---|---|
| `instance.yaml` | name, description, `data_dir`, `unit_prefix`, `foreign_units` | rename (§4); `offsite.instance_remote` leave (I) |
| `projects/secretary.yaml` | id, repo `/home/dev/secretary`, remote, `orca_binding`, `adapter`, 3 dead `curator_roots` | rename → `projects/ummanu.yaml` (id `ummanu`, `/home/dev/ummanu`, `vladmesh/ummanu`); drop dead roots |
| `adapters/secretary.yaml` | `broad_check.import_package: secretary` | rename → `adapters/ummanu.yaml`, `import_package: ummanu` |
| `projects/secretary-instance.yaml`, `adapters/secretary-instance.yaml`, `policies/concurrency.yaml` | id/repo of the instance repo | leave (I). Historical for `policies/concurrency.yaml`: no code reads `policies/`, and the sprint:1476 cutover (ummanu-61) left it out of the live root |
| `projects/secretary-supervisor.yaml`, `adapters/secretary-supervisor.yaml` | other product | external (§9) |
| `adapters/codegen-orchestrator.yaml`, `adapters/personal-site.yaml` | `$HOME/.secretary-tools` | rename → `.ummanu-tools`; `(secretary-1805)` card refs leave (R) |
| `heads/heads.toml` | 3 probes `python3 -P -m secretary.runtime.resource_probe`, prose, `no-such-provider-secretary-1566` | rename probes and prose; card refs leave (R) |
| `heads/heads.yaml`, `heads/source.yaml` | generated by `upgrade` (`product_root: /home/dev/secretary`) | rename (regenerated) |
| `persona/AGENTS.md` (9), `persona/README.md` (3), `CONTEXT.md` (15), `README.md` (4), `heads/README.md`, `policies/README.md` | CLI calls and product name | rename. Historical for `policies/README.md`: dead configuration, left out of the live root at the sprint:1476 cutover (ummanu-61) |
| `skills/manifest.toml` | `[roles.secretary]`, prose | rename (→ `operator`) |
| `tests/test_instance_config.py` | `from secretary.config import validate_instance` | rename |
| `secrets/installation-key.json`, `secrets/values/*.enc.json` (4) | formats and KDF info (§2) | rename by re-encryption |
| `secrets/catalog.yaml` | `SECRETARY_WEB_FRONT_PASSWORD`, path under `secretary-data` | rename |
| `runtime.env`, `board-store.env` (ignored) | §3 | rename keys |
| `state/memory/facts/secretary/` (117), `facts/product-secretary/` (6), `packs/product-secretary.json` | scopes | rename (move dir, rematerialize the pack). Rewrite CLI calls in fact bodies; `source:` stamps leave (R) |
| `state/board/**` (3 625), `state/knowledge/**` (358), `state/runs/**`, `gate-runs/`, `provision-runs/` | checkpoints, closeouts, decisions, reports | leave (R) |
| `state/memory/facts/secretary-instance/` | | leave (I) |

---

## Transition design

### T1. The merge problem

**Problem.** The dispatcher unit executes `src/secretary/dispatch/runtime_preflight.py` and
`.venv/bin/secretary` from `/home/dev/secretary`. Release lands the card (`gh pr merge`, since the
project's adapter says `validation.ci: github`) and then calls `CommandHostRuntime._advance_checkout`.
For the production checkout that is `dispatch/production_checkout.advance`: pin target → refuse a non-ff
→ board migrations → `git merge --ff-only`. A rename landing there removes `src/secretary/` under the
running tick. Then:

- the `release-after` runtime probe (`_require_production_runtime`) runs against a tree without the package, mid-release;
- the next timer start finds no `runtime_preflight.py`;
- the web and PO restart fail on `ModuleNotFoundError: secretary`, because the editable `.pth` points to
  `src/` and `src/secretary` is gone.

**Alternatives.**

1. *Let the dispatcher release it and repair by hand.* Rejected. The card dies between "merged" and
   "Done", the dispatcher is down until a human acts, and the release bookkeeping is left half-written.
2. *Kill switch `SECRETARY_DISPATCHER_AUTOMERGE=off` during the rename card, merge by hand in the
   transition.* Rejected as the primary path. It is global (every project), the card reaches Done without
   a merge and without a post-merge CI watch, and it relies on someone editing `runtime.env` at the right
   moment. Forgetting it means alternative 1.
3. *Blue/green: build `~/ummanu` beside the live checkout and switch units, then merge.* Not enough
   alone: nothing stops a release from advancing `~/secretary`. Its useful half (build the new tree and
   venv while the old one still runs) is kept inside the transition.
4. **Chosen: land but do not activate.** A small card before the rename adds an *entrypoint guard* to
   `production_checkout.advance`. The guard sits after the ff check and before migrations, and it is also
   called by `upgrade.fast_forward`. It refuses a target in which
   `git cat-file -e <target>:src/<runtime_preflight.PACKAGE>/dispatch/runtime_preflight.py` fails, or in
   which `[project.scripts]` no longer has the console script the unit runs. The refusal raises
   `ProductionActivationRefused` with a new reason `entrypoint_moved`.
   The existing recovery path (`dispatch/release_activation.py`) then:
   - keeps the PR merged on `main`;
   - leaves the production checkout on the last pre-rename commit;
   - blocks the card with a typed reason;
   - opens one PO operation card that names this runbook;
   - removes the release record.

   The live install keeps running the old code consistently. Every later advance of the secretary
   checkout is also refused, because no later `main` contains the old entrypoint. So no other card can
   activate in the window either. The `upgrade` pull refuses the same way, so an accidental
   `secretary upgrade` cannot ff into the rename.

   Landed in secretary-1929: `dispatch/entrypoint_guard.py`, called by both movers.

**How the dispatcher is kept from executing deleted code mid-release:** the checkout it executes never
moves to the renamed tree while it runs. Only the transition unit moves it, after the dispatcher is frozen
and its timer stopped (§T3, steps 1–2), and it moves it as `/home/dev/ummanu`, a path the old units never
execute.

The window between the rename merge and the transition is kept short. The observer drains the pipeline as
soon as the rename card is Blocked with `entrypoint_moved`.

**Gate of the rename card itself.** The worker and the GitHub CI test the candidate (`ummanu`), while the
production wrapper (`python -m secretary check broad`) still runs. The worker records its broad receipt
with `--command` (no import-provenance claim); `adapters/secretary.yaml` keeps `import_package: secretary`
until the transition. CI on the PR is the gate.

### T2. Board data

Done by the transition command (§T3) with every unit stopped:

1. **Snapshot.** Before the transition the observer posts a sprint comment with counts of `tasks`,
   `issues`, `sprints`, `board_events`, `requests`, `po_sessions`, `owner_events` (DoD). The transition
   repeats the count from the dump metadata (`table_counts` of every table).
2. **Dump** from the old store with the existing primitive (`board/postgres_recovery.py`: data-only
   custom-format dump plus `table_counts`, `alembic_head`, `source_endpoint_id`). The transition module
   reads the old `board-store.env` (`SECRETARY_DB_*`) itself.
3. **Stop the old container** (`docker compose -p secretary-board-store -f /opt/secretary/postgres-compose.yml stop`).
   Keep the volume.
4. **Provision the new store**: rewrite `board-store.env` to `UMMANU_DB_*` with db `ummanu`, users
   `ummanu_owner/app/read` and the same passwords. Then `board.provision.provision`: compose project
   `ummanu-board-store`, `/opt/ummanu/postgres-compose.yml`, fresh volume, same port 5432.
5. **Restore** with `postgres_recovery.restore_dump`. It migrates the empty store to head under the new
   role names, restores data-only in one transaction, and refuses unless the counts of every table equal
   the dump's. The restore refuses the old endpoint (`endpoint_identity` differs by dbname). The rename
   card adds **no** Alembic revision, so dump head equals shipped head.
6. **Translate live identity** in one owner transaction:

   ```sql
   INSERT INTO products (product_id, board_key, title, description, state, created_at, updated_at)
     SELECT 'ummanu', <new board_key>, 'Ummanu', description, 'active', now(), now() FROM products WHERE product_id = 'secretary';
   UPDATE products SET state = 'archived' WHERE product_id = 'secretary';
   INSERT INTO projects (project_id, plane, adapter, orca_binding) VALUES ('ummanu', 'orchestrator', 'ummanu', 'ummanu');
   UPDATE projects SET enabled = false, registry_present = false WHERE project_id = 'secretary';
   INSERT INTO repositories (project_id, path, remote, role) VALUES ('ummanu', '/home/dev/ummanu', 'https://github.com/vladmesh/ummanu.git', 'primary');
   INSERT INTO product_projects VALUES ('ummanu', 'ummanu'), ('ummanu', 'secretary-instance');
   -- open sprints only (today: sprint:1475)
   UPDATE sprints SET product_id = 'ummanu', allowed_productions = array_replace(allowed_productions, 'secretary', 'ummanu')
     WHERE status <> 'closed' AND (product_id = 'secretary' OR 'secretary' = ANY(allowed_productions));
   -- sprint_projects: replace (open sprint, 'secretary') by (open sprint, 'ummanu'), same reserved/reserved_at
   -- sprint_repositories: repoint the open sprint's /home/dev/secretary row to the new repository id
   UPDATE issues SET product_id = 'ummanu' WHERE product_id = 'secretary' AND state = 'open';
   UPDATE po_sessions SET cwd = '/home/dev/ummanu-data/po' WHERE state = 'open' AND cwd = '/home/dev/secretary-data/po';
   ```

   The exact statements are the transition card's. This list fixes *what* is translated: open sprints'
   product, `allowed_productions`, reservations and repositories; open issues; open PO sessions. Untouched
   (R): closed sprints, closed issues, every card (refs and `project_id`), events, requests, comments,
   resumes, extensions.
7. **After.** Counts of cards, issues, sprints and events equal the snapshot. The only added rows are
   1 product, 1 project, 1 repository, 2 `product_projects`, and the transition's own sprint comment.
   `ummanu task show --ref secretary-1915` and the web card page still open (reads go by `task_ref`, not
   by registry). The first card on project `ummanu` gets `ummanu-1`.

The 13 live cards on project `secretary` stay there. The project is disabled, so the dispatcher no longer
claims or launches on it. This card is Done before the transition. The rename card is Blocked
(`entrypoint_moved`) and moved to Done by the PO after the transition, since its merge landed. The other
parked cards are re-cut by the PO as `ummanu-N` with `supersedes` only if still wanted.

### T3. One-shot migration code

**Where.** `src/secretary/transition/` (renamed to `src/ummanu/transition/` by the rename card along with
everything else) and CLI `ummanu transition from-secretary --instance … (--plan | --apply | --rollback)`.
A shell bootstrap `scripts/transition-from-secretary.sh` runs the steps that must happen before
`~/ummanu/.venv` exists. Tests: `tests/test_transition_from_secretary.py` (unit, fixture installs) and
`tests/test_transition_board.py` (the board steps against PostgreSQL, CI integration shard; it takes every
name from the transition's table, so it is not class T and the rename card rewrites it like any other file).
After the sprint these are the **only** code that may contain `secretary` (class T). They are deleted in
the packaging sprint, after rollback stops mattering.

The module reaches the product only by relative imports of stable primitives (`board.postgres_recovery`,
`board.provision`, `board.store`, `board.migrate`, `board.backend`), so the rename card moves it without
rewriting it. Every old and new name (paths, unit and env prefixes, DB/role/Compose names, secret-store
formats and constants, Claude keys) is in one literal table, `transition/names.py`.

**Launched** by the PO from its session, the way NOTES.md describes `upgrade`:
`XDG_RUNTIME_DIR=/run/user/$(id -u) systemd-run --user --unit ummanu-transition --collect /bin/bash -c '/home/dev/secretary/scripts/transition-from-secretary.sh > /home/dev/transition.log 2>&1'`.
The log lives outside both data dirs. The PO session dies at step 2 and the log is read afterwards. Read
the plan first: `secretary transition from-secretary --plan --instance /home/dev/secretary-instance`.

**Steps.** Each step is idempotent and journals to `~/ummanu-transition.json`, so a rerun resumes at the
first unfinished step. `--plan` prints every step with the commands and paths it would touch and the
result of each precondition, and writes nothing (no journal, no lock, no board write, no fetch).
Rollback copies go to `~/ummanu-transition/`. Steps 1–4 run from the pre-rename tree (the bootstrap runs
them with `--through move`), step 5 is the bootstrap's own, steps 6–12 run from the renamed tree.

1. Preconditions, after `git fetch origin` in the checkout:
   - the checkout is on `main`, an ancestor of `origin/main` and behind it; a commit in between adds
     `src/ummanu/dispatch/runtime_preflight.py`, and `origin/main` has the `ummanu` console script and no
     `src/secretary/dispatch/runtime_preflight.py` (the rename card is Blocked `entrypoint_moved`);
   - no Alembic revision is added in the gap. The rename moves every revision file, so a path diff is
     never empty; the check compares the revision file *names* under `src/*/board/migrations/versions`;
   - the first-parent merges in the gap are the rename's merge only, unless `--allow-extra-merge <sha>`
     names each other one;
   - `pause.json` is a drain (or already a freeze); `production-state.json` has no records, no
     activation recovery and no post-merge watch; no worker or reviewer head socket of a record exists
     (observer heads are allowed: step 2 stops them);
   - the sprint (`--sprint`, or the one open sprint carrying it) has comments with the markers
     `[transition:baseline]` (doctor baseline) and `[transition:counts]` (board counts).
2. Stop the dispatcher timer and service, `secretary resume` if the pipeline is drained (a drain cannot
   become a freeze), `secretary pause freeze`, then `sudo -n systemctl disable --now` every `secretary-*`
   timer and service except the instance's `foreign_units` (this kills web, PO service and session).
   A freeze that answers with any warning, or leaves an observer record `pause-stop-pending`, refuses
   the step (it stays unfinished, the message names what failed). Their enabled/active states and copies of the unit files go to the journal and
   `~/ummanu-transition/units/` for rollback.
3. Dump the board (§T2.2), stop the old container (§T2.3).
4. Record the old venv's extras, then `mv /home/dev/secretary /home/dev/ummanu`,
   `mv /home/dev/secretary-data /home/dev/ummanu-data`, `sudo mv /opt/secretary /opt/ummanu` and repoint
   `/usr/local/bin/orca`, `mv ~/.secretary-tools ~/.ummanu-tools` and repoint `~/.local/bin/uv`.
5. Bootstrap, in `~/ummanu` on `main`: `git fetch`, refuse if `origin/main` moved away from the commit
   step 1 checked and journalled, then `git merge --ff-only <that commit>`; `git remote set-url
   origin https://github.com/vladmesh/ummanu.git` (the owner has renamed the repo by now; before the
   rename the old URL still works); delete `src/secretary` whole (ignored `__pycache__` leftovers would
   otherwise import as a namespace package) and `src/secretary.egg-info`; move the old `.venv` to
   `~/ummanu-transition/old-venv` (its scripts name the old path, so it is kept whole for rollback) and
   build a new one with the recorded extras, `pip install -e`; refuse unless `python -P -c 'import
   secretary'` in the new venv raises ModuleNotFoundError. Then the script execs
   `~/ummanu/.venv/bin/ummanu transition from-secretary --apply`, whose step 5 verifies all of it (HEAD is
   the journalled commit, no old package left, the import refused).
6. Data-plane fix-ups: `git worktree repair` in `~/ummanu` and, per repository, for every worktree that
   moved with the data dir (product, observer root, codegen, instance), then `git worktree prune`;
   `production-state.json` path prefixes and `owner`; `data-manifest.json` `data_dir`; the live
   `codex-home/config.toml` project keys; the `~/.codex/config.toml` MCP block that runs the old checkout.
7. Instance rewrite, committed to `secretary-instance` as one commit, refused if anything is staged or
   the paths it rewrites have uncommitted changes, and staging exactly the files it writes: `instance.yaml` (§4), rename
   `projects/secretary.yaml` and `adapters/secretary.yaml` to `ummanu.yaml`; `.secretary-tools` in
   adapters; probes in `heads/heads.toml`; `secrets/catalog.yaml`. Re-wrap `secrets/installation-key.json`
   and re-encrypt every value from the old AAD/KDF constants and formats to the new ones; each value is
   opened under the new ones before any file is replaced, and the old files are kept under
   `~/ummanu-transition/secrets/`. Move `state/memory/facts/secretary` to `facts/ummanu` and remove
   `facts/product-secretary` and `packs/product-secretary.json`. Also rewrite the git-ignored
   `runtime.env` keys and `board-store.env` (§T2.4 values), with copies kept for rollback.
8. Board: move the old Compose file (moved to `/opt/ummanu` with step 4) aside, then provision, restore,
   translate (§T2.4–6), print the before/after counts and refuse unless only the §T2.7 rows differ.
   Translation touches open sprints (`status = 'open'`) only.
9. Claude and Codex directories (§T4).
10. Remove the old role worktrees `~/orca/workspaces/secretary/*` (they stay usable until here, so a
    rollback before this step needs no recreation), then
    `sudo -n ~/ummanu/.venv/bin/python3 -P -m ummanu upgrade --instance /home/dev/secretary-instance
    --no-pull --product-root /home/dev/ummanu --runtime-user dev`. This is the existing materializer:
    registries, memory pack `product-ummanu`, memory clients, codex home, PO workspace, worktrees under
    `~/orca/workspaces/ummanu`, role skills, units `ummanu-*` from `packaging/systemd`, memory, PO, web,
    verify. Then `ummanu web-front render` with the sites of the current Caddyfile, `web-front check`,
    restart `ummanu-web-front.service`, then `ummanu memory reindex`.
11. Remove the old unit files from `/etc/systemd/system` and `daemon-reload`. The copies stay in
    `~/ummanu-transition/units/`.
12. `ummanu resume`; `ummanu doctor`; write `~/ummanu-transition/report.md` (counts before/after, unit
    list, doctor result, old and new checkout SHA, secrets verified, dirs moved, `import secretary`
    refused in the new venv) and post it as a sprint
    comment marked `[transition:done]`, with `## What was done` / `## How to verify` for the PO to complete
    the operation card the guard opened.

**Runbook notes** (from the secretary-1930 review):

- A step-2 refusal on `pause-stop-pending` does not clear by itself. It clears only after one manual
  dispatcher tick (`sudo systemctl start ummanu-dispatcher-production.service`, or the old-prefix unit
  `secretary-dispatcher-production.service` before step 2 completes), or after a rollback. Then rerun
  the transition.
- If step 5 fails between the fast-forward and a working new venv, `--rollback` cannot run yet: the
  bootstrap finds neither a working `ummanu` CLI in the renamed tree nor the old package to run it
  from. Recover forward (rerun `--apply`, which resumes at step 5), or put the checkout back with
  `git -C ~/ummanu reset --hard <pre_transition_sha>` on `main` (the SHA from the journal) and then
  run `--rollback`.

**Post-transition repairs** (ummanu-1, ummanu-5). Pieces of state the one-shot run left unreadable are
repaired afterwards, in this order:

1. Stop the heads still running from the old checkout.
2. `ummanu upgrade --instance <instance>`. Its `pipeline-state` step, right after `role-worktrees`,
   restores the untracked `state/pipeline/` journals that step 10 deleted with the old worktrees. It
   restores them from `<instance>/state/runs` and refuses a live journal that does not extend the
   checkpoint.
3. `ummanu transition from-secretary --repair-scope-owners --instance <instance>` lists the settled
   heads whose `scope-owner.json` still names a `secretary-head-*.scope`. Adding `--apply` renames
   each listed unit to `ummanu-head-*.scope`, and the original files are kept under
   `~/ummanu-transition/heads/`. Any other record that carries the old prefix is left as it is and
   reported, and the command exits non-zero.
4. (ummanu-5) `ummanu transition from-secretary --repair-products --instance <instance>` shows the open
   cutover canary `product:cutover-d9821c3499daaf9d`, still linked to the unregistered project
   `secretary` and so blocking the checkpoint; `--apply` archives it, as §T2 archived `product:secretary`.
   A closed Product's links are history and the checkpoint no longer checks them against the registry.

**Rollback**: `scripts/transition-from-secretary.sh --rollback` (or `… transition from-secretary --rollback`),
driven by the journal, before step 12 or if verify fails. It stops `ummanu-*` and the new store
container; moves the Claude directories back and drops the trust entries it added; reverts the instance
commit (or, if step 7 stopped before it, restores those paths from `HEAD`) and puts back `runtime.env` and
`board-store.env`; moves the Compose file and the four paths back and repoints the links; restores the
code checkout with `mv` back, the old venv back into place, then `git -C ~/secretary reset --hard <SHA>` on
branch `main` (the pre-transition SHA from the journal, never a detached checkout), the old `origin` URL
and `git worktree repair`; restores the rewritten data-plane files; restores the unit copies,
`docker compose -p secretary-board-store … start` (the old volume is untouched), and enables and starts
the units as they were. The pipeline stays frozen until the operator runs `secretary resume`. The journal
and the copies move to `~/ummanu-transition-rolled-back-<stamp>/`, so a later `--apply` starts fresh. The
GitHub rename needs no rollback: the old URL still redirects.

### T4. PO and head continuity

- `~/secretary-data/po` moves to `~/ummanu-data/po` with the data dir. `ummanu-po.service` (rendered from
  `packaging/systemd`) serves it, and `.mcp.json` and the skills are re-rendered by upgrade.
- Before the upgrade starts the PO service, step 9 moves the Claude project directories
  (`mv -T`, refusing a non-empty target):
  - `-home-dev-secretary-data-po` → `-home-dev-ummanu-data-po` (PO `MEMORY.md` and sessions);
  - `-home-dev-secretary-data-po-handoffs` → `-home-dev-ummanu-data-po-handoffs`;
  - `-home-dev-secretary-data-dispatcher-observer-root-observers` → `-home-dev-ummanu-data-dispatcher-observer-root-observers`
    (observer memory);
  - `-home-dev-secretary-data-workspaces-observers-sprint-1475` → `…-ummanu-…`;
  - `-home-dev-secretary` → `-home-dev-ummanu` (interactive and worker memory);
  - `-home-dev-orca-workspaces-secretary-{curator,pipeline,retro,steward}` → `…-ummanu-…` (the role
    agents only: card and `secretary-instance` worktrees under the same root are history, R/I).

  The two `~/.claude.json` trust entries are copied to their new keys. Session JSONL files keep their
  stale `cwd` lines (R). Claude finds a session by id in the directory of the current cwd.
- Codex: `codex-home` moves with the data dir, and its sessions resume by id. Only live trust keys are
  rewritten. All 9 Codex PO sessions are closed.
- The 2 open PO sessions get the new `cwd` (§T2). The sprint's `po_session` id is unchanged. Proof:
  a new PO turn in the web sees the previous `MEMORY.md`, and `po_memory` answers.
- **This sprint's observer head** is stopped by the freeze in step 2. Before asking for the transition it
  writes a `[sprint:resume]` naming the post-transition step (verify, then cut the instance card). After
  `ummanu resume`, the next tick launches a fresh observer in `~/ummanu-data/workspaces/observers/sprint-1475`.
  It recovers state from the sprint entity and reads the moved observer memory. No session resume is
  required.
- The interactive secretary session (cwd `/home/dev/secretary`) must reopen in `/home/dev/ummanu`. Its
  memory is the moved `-home-dev-ummanu`.

### T5. Guard

- **Product repo:** `tests/test_old_name_guard.py`, in the unit suite (so in `tests.broad` and CI), listed
  in `tests/ci-shards.txt`. It lands with the rename card. It walks `git ls-files`, checks every tracked
  path and every text file (binary skipped) for `(?i)secretary`, and fails on any match that is not
  allowlisted:
  - **H:** `hermes-secretary`, `hermes-secretary-roles`, `~/.hermes/skills/secretary`, `~/.hermes/skills/secretary-roles` (only in `skills/manifest.toml`).
  - **I:** `secretary-instance` (any file).
  - **R:** card refs `\bsecretary-\d+\b`; the fixture path `tests/fixtures/local_pty_journals/secretary_1727_*.jsonl.gz`.
    The standalone `opus_review.md` audit is also a class R record: it preserves quoted historical
    source and production unit names. Its exact path is allowlisted; this does not permit the same
    names in operational documentation or product source.
  - **T:** `src/ummanu/transition/**`, `scripts/transition-from-secretary.sh`, `tests/test_transition_from_secretary.py`, `docs/RENAME.md`, the guard test itself and its shared matcher `src/ummanu/infra/old_name_guard.py`.

  Nothing else, and no per-file exemptions outside these four classes.
- **Env:** `tests/test_env_prefix.py` sets `SECRETARY_DATA_DIR`, `SECRETARY_INSTANCE` and
  `TA_SECRETARY_REPO` to bogus values and asserts that resolution ignores them, then that the `UMMANU_*`
  equivalents take effect. A second assertion checks that `import secretary` raises `ModuleNotFoundError`
  in a clean interpreter on the installed tree.
- **Instance repo:** `tests/test_old_name_guard.py` in `secretary-instance` (post-transition instance
  card) with the same classes. R there covers `state/board/**`, `state/knowledge/**`, `state/runs/**`,
  `gate-runs/**`, `provision-runs/**` and `source:` lines of memory facts. External adapters/projects
  (`secretary-supervisor*`) stay out until §9 is fixed. Since ummanu-32 that suite is retired: its
  allowlist is ported into `src/ummanu/infra/old_name_guard.py` (`LIVE_ROOT_ALLOWLIST`, verbatim but
  for the receipt rows, whose paths are no longer exported; the
  matcher the product guard shares, listed under T), and `ummanu config check --instance LIVE_ROOT`
  runs it over the live root's exported files, without Git ([Operations](OPERATIONS.md#changing-installation-config)).
- **Live host:** the proof card checks `ls /etc/systemd/system | grep secretary` (empty),
  `docker ps -a --filter name=secretary-board-store` (only the stopped rollback copy until the owner
  removes it), `instance.yaml` name and prefix.

### T6. Card sequence

| # | Card | Repo / type | Scope |
|---|---|---|---|
| 1 | secretary-1927 (this) | secretary / code (docs) | This document |
| 2 | Entrypoint activation guard | secretary / code | `production_checkout.advance` and `upgrade.fast_forward` refuse a target missing `src/<PACKAGE>/dispatch/runtime_preflight.py` or the unit's console script; new reason `entrypoint_moved` through `release_activation`, with a PO operation text naming the transition. Tests with a fixture repo whose next commit renames the package. Released normally; must be live before #4 merges |
| 3 | Transition command | secretary / code | `secretary/transition/` + `scripts/transition-from-secretary.sh` + tests, under the old package. Implements §T3 steps 1–12 with `--plan`/`--apply`, journal, rollback notes and §T2 SQL. Tested against fixture installs and a fixture store (Postgres steps in the CI integration shard only). Released normally; inert until invoked |
| 4 | Package rename + guard | secretary / code | The mechanical rename across the tree per §1–§8, by a rewrite script whose rules are in the card. Skips the T, H, I and R matches; moves `src/secretary` → `src/ummanu` including `transition/`. Adds §T5 product guard and env test. Docs (OPERATIONS, RECOVERY, BOARD_STORE, …) describe new paths. No Alembic revision. Gate: GitHub CI. Expected outcome: merged, then **Blocked `entrypoint_moved`** (by design, see §T1) |
| 5 | Transition | owner/PO runbook (not a card) | Observer drains and posts the doctor baseline and counts. Owner renames the GitHub repo. PO launches the `systemd-run --user` unit (§T3) and the session dies. Owner or PO reads the log afterwards. Must be a runbook: it stops the dispatcher, every head (including any card's worker) and the PO session that would run it |
| 6 | Close #4 | PO action | Move the rename card to Done (merge landed). Re-cut parked `secretary-*` cards as `ummanu-N` if wanted |
| 7 | Instance prose + instance guard | secretary-instance / code (`secretary-instance-N`) | `persona/`, `CONTEXT.md`, `README.md`, `heads/README.md`, `policies/README.md`, `skills/manifest.toml`, CLI calls in memory facts → `ummanu`; instance guard test (§T5). Released normally on the renamed install; `ummanu role-skills sync` shows no drift. `policies/README.md` is historical here: the sprint:1475 inventory ran, and the sprint:1476 cutover left `policies/` out of the live root |
| 8 | Live proof | ummanu / code (`ummanu-1`) | A real small change taken Ready → Done (worker, review, merge, release) on project `ummanu`. Suggested: mark this document historical and record the transition SHAs in it. Evidence in the report: `ummanu doctor` equals the baseline; `ummanu backup create` + `ummanu backup verify` pass; `ummanu task show --ref secretary-1915` opens; the web front opens behind the password; no `secretary-*` units; the PO turn sees the previous `MEMORY.md` |

Owner/PO-only steps: the GitHub repo rename (#5, owner credentials); the transition unit (#5, kills the
session running it); closing #4 (#6); removing the old board volume, `/opt/ummanu` leftovers and the unit
copies after DoD (owner).
