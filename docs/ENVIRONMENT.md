# Environment names

Product settings formerly named `TA_*` use `UMMANU_*`. Released units and external
overrides using only the listed TA names remain supported. No runtime.env rewrite,
secret migration or process-global normalization is required. New managed service
output, launcher assignments and CI fixture pins use canonical names.

`runtime.paths.env_value` owns Python alias lookup. Shared Codex preflight retains
its existing import boundary and uses the equivalent explicit nonempty lookup.
Each row below names a canonical
suffix: prepend `UMMANU_` for its canonical name and `TA_` for its explicit legacy
name. For example, `RUNTIME_ENV_FILE` means `UMMANU_RUNTIME_ENV_FILE` and
`TA_RUNTIME_ENV_FILE`. This inventory covers actual reads and writes, including
the dynamic timeout suffix, rather than counting textual mentions.

Policy N skips missing and empty strings: first nonempty canonical, then nonempty
legacy, then the existing fallback. Whitespace is nonempty. Policy R preserves
raw `get(default)` semantics: canonical present, including empty, then legacy
present, including empty, then default. Parsing follows selection. Malformed
canonical values never borrow valid legacy values. Validators, paths, streams,
exit codes and numeric fallback policies otherwise retain their existing behavior.

| Suffix (canonical / legacy as above) | Actual readers | Timing; policy; fallback |
| --- | --- | --- |
| RUNTIME_ENV_FILE | runtime.role_env; automations.runtime.production_telemetry; recovery_inventory; live_root_findings | per call N; role default instance/runtime.env; telemetry no pin means its default instance; recovery only uses bound installation env; findings inspect both spellings |
| RUNTIME_PYTHONPATH | runtime.role_env; ummanu-agent-gate.sh | per call/start N; then UMMANU_REPO; Python's own importable checkout or shell's HOME/ummanu |
| HEADS_REGISTRY | runtime.heads; web.commands; webproto.commands | per call N for registry, parser construction R; installed registry then product registry; parser unset is None |
| REVIEWER_HEAD | runtime.heads | per call N; required registry role_defaults.reviewer |
| WORKSPACES_ROOT | runtime.shared_state; upgrade; recovery_inventory; dispatch.pause; curator.discover | shared_state import N, others per call N; HOME/orca/workspaces; upgrade's explicit owner home governs fallback; recovery ignores unbound env |
| PIPELINE_STATE_DIR | runtime.shared_state; recovery_inventory; dispatch.pause | per call N; workspace/project/pipeline/state/pipeline; recovery audits conflict against declared workspace layout |
| STATE | runtime.state; health/signals through AgentState | import R; HOME/ummanu-data/automation-state |
| CODEX_HOME | runtime.codex_preflight and session_roots readers | per call N; profile codex_home first, then env, then data-dir/codex-home with nonempty auth.json; source labels profile/env/data-dir |
| CODEX_SESSIONS | dispatch.tui; pipeline.codex_sessions | per call N; all installation/live Codex homes via session_roots |
| CODEX_SESSIONS_DIR | curator.discover | import N; unset scans installation/live homes; distinct from CODEX_SESSIONS |
| CLAUDE_PROJECTS | dispatch.tui | per call N; HOME/.claude/projects |
| CLAUDE_PROJECTS_DIR | curator.discover | import R; HOME/.claude/projects; distinct from CLAUDE_PROJECTS |
| HERMES_HOME_DIR | curator.discover | import R; HOME/.hermes |
| CLAUDE_JSON | automations.runtime.dispatch; webproto.lifecycle; dispatch.launcher | first two import R, launcher per call R; HOME/.claude.json; explicit config argument wins |
| WORKSPACE | automations.runtime.dispatch | per call N; HOME/orca/workspaces/ummanu/agent |
| CURATOR_WORKSPACE | curator.discover; curator.harvest; rebind identity via harvest | per call N; cwd resolved without requiring existence |
| CURATOR_SESSION_ID | curator.discover; curator.harvest | discover per call R default empty; harvest per call N omitted when empty |
| CURATOR_RUN_ID | curator.harvest | per call N; omitted when empty |
| CURATOR_MAX_TURNS | curator.harvest.Limits | per call R; positive integer (errors name selected spelling), default 200 |
| CURATOR_MAX_INPUT_BYTES | curator.harvest.Limits | per call R; positive integer (errors name selected spelling), default 262144 |
| CURATOR_MAX_SOURCES | curator.harvest.Limits | per call R; positive integer (errors name selected spelling), default 32 |
| CURATOR_MAX_ROWS_PER_SOURCE | curator.harvest.Limits | per call R; positive integer (errors name selected spelling), default 512 |
| CURATOR_MAX_RECORD_BYTES | curator.harvest.Limits | per call R; positive integer (errors name selected spelling), default 65536 |
| CURATOR_MAX_MEMORY_BYTES | curator.harvest.Limits | per call R; positive integer (errors name selected spelling), default 65536 |
| SEARCH_LOG | retro.search_log | import R; HOME/ummanu-data/memory/search-log.jsonl |
| STEWARD_STALE_HOURS | steward.signals | import R; raw float, default 24; existing acceptance of nonfinite floats unchanged |
| HEALTH_MAX_AGE_S | automations.runtime.health | import N; positive integer when supplied, otherwise spec health.max_age_s or cadence; diagnostics name the selected spelling |
| PRODUCTION_STATE | production_telemetry | per call N; data-dir/dispatcher/production-state.json |
| PRODUCTION_RESOURCE_HEALTH | production_telemetry | per call N; data-dir/dispatcher/resource_health.json |
| PROBE_TIMEOUT_S | runtime.resource_probe | import R raw int default 20; per-call R positive seconds parser for resources without a specific default |
| PROBE_TIMEOUT_S_<RESOURCE> | resource_probe; head_health via probe_timeout_s | per call R; uppercase resource id, non-alphanumeric characters become underscores; selected int(float(raw)) must be positive; invalid/empty goes to resource default, then global timeout, then 20 |
| PROBE_REASON_TEXT_LIMIT | runtime.resource_probe | import R raw int default 400 |
| OPENROUTER_ENV_FILE | runtime.resource_probe | import R; HOME/.hermes/.env |
| OPENROUTER_ENV_KEY | runtime.resource_probe | import N; file key names OPENROUTER_API_KEY then open_router_key |
| OPENROUTER_KEY | runtime.resource_probe | per call N; then selected env file/key; sensitive under both spellings |
| HARD_PAUSE_AUTO_RESUME_TTL_S | dispatch.pause | per call R; raw int, ValueError uses existing TTL default; negative clamps to zero |
| HARD_PAUSE_AUTO_RESUME_ACTORS | dispatch.pause | per call R; comma-separated stripped nonempty names; existing AUTO_RESUME_ACTORS_DEFAULT |
| GATE_BOARD_ATTEMPTS | ummanu-agent-gate.sh | start N; default 5; existing shell integer comparison errors remain errors, never use legacy to evade malformed canonical |
| GATE_BOARD_WAIT | ummanu-agent-gate.sh | start N; default 120; existing sleep parsing/errors unchanged |

The resource-specific default (75 seconds for openai-sub) precedes the global
timeout. Raw import-time timeout/reason constants still reject empty or malformed
integers at import. The per-call seconds parser still returns its normal fallback
for unusable numeric text. Empty canonical resource overrides suppress legacy
overrides, because these use R rather than N.

Telemetry keeps explicit UMMANU_INSTANCE ahead of a runtime-file-implied instance;
UMMANU_DATA_DIR precedes configured instance data_dir. An explicit
UMMANU_LEGACY_PAUSE_FILE, then UMMANU_LEGACY_PIPELINE_STATE_DIR, still wins the
pause mirror's setting before the PIPELINE_STATE_DIR alias pair. Other settings
that already use UMMANU names receive no new alias protocol.

The current writers are ten packaged systemd services (runtime-file pin), four
gate-driven standing services (runtime product-root pin), role_env.launch_binding
and role_env.runtime_env (canonical trusted bindings), and the suite/CI fixture
producers (CODEX_HOME and PIPELINE_STATE_DIR). Unit EnvironmentFile grammar and
the runtime.env parser are unchanged. Legacy producer vectors live in the tests;
the user's runtime.env and installed old units can retain their original names.

For fake precedence examples:

```sh
UMMANU_RUNTIME_ENV_FILE=/fake/new/runtime.env TA_RUNTIME_ENV_FILE=/fake/old/runtime.env
UMMANU_CODEX_SESSIONS= TA_CODEX_SESSIONS=/fake/legacy/sessions
UMMANU_PROBE_TIMEOUT_S_CUSTOM=oops TA_PROBE_TIMEOUT_S_CUSTOM=7
```

These select `/fake/new/runtime.env`, `/fake/legacy/sessions`, and the normal
resource/global timeout fallback respectively. They never select the last
legacy value to bypass canonical validation.

Lookup is not permission. Role allowlists are unchanged. Runtime.env cannot
inject launcher-only observer binding, actor, memory bearer, Docker-test grant,
or runtime-file/product-root pins through either alias. Trusted ambient unit
pins win file entries; only that binding seam emits normalized canonical pins.
Both provider spellings use the same sensitive-name filter and exact-value
redaction. Worker, reviewer and observer do not gain provider credentials.
BOARD_ROLE/BOARD_ACTOR, HOME/PATH/CODEX_HOME and third-party provider env/file
keys retain their separate protocols. Removed product-prefix names remain
negative fixtures and receive no compatibility fallback.

`test_environment_aliases` supplies deterministic unit consumer vectors;
existing role/launch, packaging, gate and curator suites retain their original
CI owners. Canonical fixture retargets preserve assertions about routing,
source labels, errors and trust. Cold composition retains its fresh-import
budget and forbidden families; no root value-taking option was added.
