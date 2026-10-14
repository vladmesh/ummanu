# Head runtime

A head runs on `local-pty`: `LocalPtyHeadRuntime` owns a supervisor, process group, PTY,
socket and journal. Profiles may set `runtime = "local-pty"` or omit the key. Old Orca records
remain readable as `orca-legacy`; they cannot launch a head or receive delivery.
The lifecycle boundary is in [Architecture](ARCHITECTURE.md#head-runtime-ownership), liveness in
[Head vitality](HEAD_VITALITY.md), and scope ownership in [Head scopes](HEAD_SCOPES.md).

## Worker and reviewer Docker guard

Reviewers inspect code and worker/CI evidence without running tests or any `ummanu check` form.
The role launcher binds role identity through `/proc` exec ancestry; candidate-local startup hooks
and a workspace startup import directory cover candidate and external Python. See
[Testing](TESTING.md) for admission, supported recovery and production activation preconditions.

The shared launcher puts a product-owned `docker` executable before native Docker in worker and
reviewer environments, including after login profiles run. Workspace Python and Ruff come from
`.ummanu-task-env/venv`; the guard uses the configured product's managed interpreter and source
with isolated startup. Candidate code, interpreters and inherited guard bindings cannot select
its implementation. Native Docker is resolved before that PATH prefix is installed. Missing or
unexecutable guard, interpreter or native backend refuses launch or command. PO and dispatcher
Docker paths retain their behavior.

The dispatcher validates the sprint's entire creation-only `local_run_exceptions` field, then
selects the card's project entries. Each durable launch intent retains one snapshot per head round;
packet and launch use that value, including an empty or unavailable result. The renderer carries
it through `role_env exec --local-run-policy`; inherited policy bindings, `runtime.env` and
candidate files supply no authority. Missing sprint/field, malformed declarations, read failures,
unbound and standing launches grant no exceptions. The immutable snapshot needs no refresh.

Heavy operations refuse before native execution with status 125 and a use-CI message unless the
snapshot grants the exact argv: `docker run|create|build`, `compose up|run|build`,
`container run|create`, `image build`, `builder build`, `buildx build` and `buildx b`.
Global flags, Compose options and `--` boundaries identify the operation; unknown or unresolved
options grant no execution. The matching vector is `["docker", *original_arguments]` before parsing
or translation. Executable spelling, order, flag placement, values and empty arguments must match
exactly, without shell evaluation, wildcard, prefix or path/alias normalization. Authorized
commands retain argv, output and status; cleanup ownership checks still apply. This parser covers
the listed hierarchy, not arbitrary plugins or additional heavy commands. Local broad tests
must not require Docker; report such a dependency on the card.

Explicit `docker rm|stop|kill` targets, including `docker container` aliases, must all carry
`ummanu.test-board=<positive decimal owner PID>` and no `ummanu.production-board` key.
PIDs use ASCII digits without sign, whitespace or leading zero. Any production marker protects
the container, even malformed or alongside a valid test label; the reserved value is `true`.
Names, images, Compose projects and missing/malformed labels confer no ownership. Unlabelled
production containers are protected. Label names are owned by `runtime.container_labels`.

Native flags, environment and selected context resolve the endpoint. The guard pins that host,
inspects the entire target batch before effects and substitutes inspected full IDs for names and
prefixes. Any unsafe or unresolved target refuses the invocation. Force/volumes, stop timeouts
and stop/kill signals are supported; unknown flags, combined short flags, ambiguous syntax and
link removal (`--link`, `-l`, attached/equals forms) refuse. A global short log level must precede
removal; `--log-level` is supported. Named contexts with stored TLS material or TLS verification
overrides refuse destructive operations; use explicit `--host` and TLS CLI settings. Read-only
calls keep native arguments, output and status; allowed cleanup keeps native output, status and
signal handling.

Compose `down|rm|stop|kill` and every prune form (container, system, volume, image, network,
builder, buildx) refuse regardless of force, filters or global options. Select owned test
containers with explicit IDs and `docker container rm|stop|kill`. Refusals exit 125 with
`docker-guard:`; launch-boundary refusals use `role-env:`. This guards accidental commands in
trusted roles: absolute Docker paths, raw sockets and deliberate environment/PATH evasion are
outside the threat model. An exception authorizes only its exact sprint vector under the CI-only
Docker/integration rule. Production label transitions, other fixture ownership and reaping remain
separate work. `tests.test_docker_guard` exercises the guard and role wrappers with a fake native
CLI and no daemon in component CI and local broad.

## Supervisor progress journal

The supervisor feeds PTY chunks into a bounded screen at the PTY's rows and columns. During an
open turn it examines 0.5-second windows and writes `provider.progressed` only for a normalized
line not seen in that turn. Printable text, cursor moves, erasure, scrolling and the alternate
screen affect the screen; colour and other non-text controls do not. Normalization masks digit
runs, removes Dingbat, Braille, Geometric Shapes, `•`, `·` and lone `*` spinner glyphs, collapses
whitespace and ignores empty lines.

At most 4096 hashes are retained in FIFO order, cleared at `turn.started`. Eviction does not make
a still-visible line progress again. Windows with no new line emit nothing; their bytes and
`folded_windows` accumulate in the next progress record. `turn.finished.output_bytes` counts
all turn output; `turn.finished.folded_windows` includes remaining folds. Quiet-turn timing is
unchanged. See `runtime/head/local_pty/journal.py` for the journal contract.

`JournalWriter`, under the existing exclusive supervisor lifecycle lock, owns bounded JSONL
retention. Schema 1, event kinds, run IDs and original increasing sequence numbers are unchanged.
An ordinary successful append is one complete `O_APPEND` write and fsync. Encoded records,
including their newline, may occupy up to 1 MiB. Commands and scope proofs exceed 256 bytes;
64 KiB is a tail-reader window, not a record-size assumption. The 1 MiB ceiling accommodates
the native single-argument command limit even with JSON escaping, the launch metadata and
scope binding, and the 64 KiB input / bounded request-envelope producers. An unusually large
record receives `journal_record_too_large` before disk or sequence mutation; no command, proof
or JSON is truncated, and the refusal contains no record contents.

The active `journal.jsonl` occupies at most 8 MiB after every successful append. Before an
append would cross that limit, the writer streams the old file into a retained set: the first
`run.started` for historical UI metadata, plus the current incarnation's original `scope.bound`,
`run.started`, `drain.requested`, `run.stopping` and `run.exited`, and at most 1 MiB of recent
complete records. The six anchors cost at most 6 MiB, the replacement at most 7 MiB; records
shared by prefix and suffix are written once, in original sequence order. A new scope binding
begins the next scoped incarnation before its start, so a pre-head crash keeps its authority.
Old optional progress, inputs and turn boundaries may be evicted. Current terminal outcomes,
including the typed memory-limit reason, and launch-time native attestation remain original
records, never reconstructions from mutable scope ownership.

There are no archives. During rotation the active file and one fixed `journal.jsonl.retaining`
staging file total at most 15 MiB (8 + 7), including an interrupted staging write. The writer
fsyncs staging, atomically replaces the canonical name, switches its append descriptor and
fsyncs the directory. It refuses substituted paths, symlinks, hard links and files outside
owner-only regular-file permissions. Any append, short-write, fsync, publication or reopen
failure closes the writer and returns no success. An uncertain fsync may leave a complete
record; reopen continues beyond its sequence rather than reusing it. A torn or malformed
canonical journal is refused, not trimmed or silently converted into clean history. Under the
same exclusive supervisor lock, reopen discards only the bounded owner-only fixed staging
file left by an interrupted publication, after validating the canonical journal. No backup
or separate scope-proof sidecar supplies authority.

An atomic replacement never removes the canonical name between generations. A reader holding
the old descriptor, including scope inventory's securely opened `/proc/self/fd` reader, sees
the original inode; the writer stops appending to it after publication. A reader opening the
name sees the old or new retained set; an in-progress ordinary append may be explicitly torn.
An external reader may keep an unlinked old inode alive until it closes its descriptor; the
15 MiB bound covers writer-owned active/staging files and descriptors, not arbitrary readers'
descriptor lifetimes. No bounded archive discovery or foreign-path fallback is needed.

Released unrotated schema-1 journals remain readable. Reopen streams a valid legacy input once
with bounded record, anchor and suffix memory, preserving exact original authority and last
sequence. If that input is already larger than 8 MiB, its one-time conversion begins with those
existing bytes plus at most 7 MiB staging; it never makes another full-size copy. It removes the
legacy excess before open returns and before the first successful append. Existing records
over 1 MiB receive the same named refusal without mutation. Untouched old running supervisors
continue their released append behavior until normal supported replacement or upgrade; this
card performs no live rewrite, deployment or migration and claims no retroactive cap.

`read_events`, `HeadHandle.events` and `head_run_journal_read` return the retained records and
report `partial_head` for either an evicted prefix or an interior sequence gap. The tuple-only
`head_run_journal` is retained history, not a completeness witness. `read_tail` and `tail_window`
keep their 64 KiB byte window and expose the same incompleteness. Torn, malformed and unordered
remain distinct damage readings. `head_view` exposes partial history. `head_run_first_record`
reads at most two maximum records (binding then start) and returns only task, role, run ID and
time, never command or proof. Reused directories retain the first start's historical metadata.
Terminal readers inspect the retained set and fence exits to the current incarnation; PO wait
also matches the launched head PID and internal handle's start sequence and refuses damaged
exit evidence. Handles constructed by released callers without that sequence retain their
PID-based fallback; new launches always capture the sequence.

Both launch owners, `client.spawn_head` (including failure cleanup) and
`ScopedHeadLifecycle.launch_until_started` / `started_or_exited`, use original sequence floors,
not list lengths. A retained start or exit above the captured floor remains a positive launch
witness even if intervening progress was evicted. Admission refuses partial journal history;
live supervisor status keeps its existing authority. Vitality resets its fold at every gap, so
an old retained turn or start cannot answer for newer unknown state. Handoff and journal delivery
recovery require every sequence above their floor, including interior gaps after an older
pinned anchor. Missing typed/submit evidence then means unavailable, never nothing landed or
permission to replay input. A complete recent suffix above a floor can still witness delivery.
Supervisor stderr logs, output tails and run-directory garbage collection are separate policies.

## Supported protocol and prompt contracts (CON-13 / CON-14)

CON-13 is intentionally retained. `SupervisorClient.attach`, `resize`, `next_event` and `stream`,
the attach/resize opcodes and pushed output/dropped/exited events remain supported protocol
surface. `LocalPtyHeadRuntime.attach` is the product consumer of attach; the public runtime
verb returns the connected stream. Resize and direct pushed-event iteration have native
supervisor/client coverage, but no additional current product caller. Push is the supervisor's
event stream, not a separate public client `push` method. `next_event` intentionally returns
`None` for both timeout and closed/unreadable stream. Consumers needing a lifecycle conclusion
use status, launch identity and journal evidence, not that ambiguity. Protected runtime attach
and request-drain behavior remains unchanged. The local-PTY path policy is conservatively
100 UTF-8 bytes (`protocol.SUN_PATH_MAX`), distinct from PO service's native 107-byte socket
bound; neither policy changes here. The old under-256-byte journal claim is corrected above.

CON-14 is intentionally retained at the current trusted-input boundary. Dispatcher worker,
reviewer and observer handoffs use `NudgePointer.at_document`: full prompts live in task
documents, and `prompt_document.nudge_for` bounds the document pointer plus note to 256 bytes.
`NudgePointer.line` also supports caller-supplied instructions and imposes no validation itself.
Local-PTY `_payload_of` sends `pointer.text + "\n"`; its composer path sends the line and then
Enter separately. Supervisor input admission applies the 64 KiB input and bounded frame rules.
These callers supply trusted product-generated text or authorized operator instructions; this
path does not promise control-character validation or bracketed-paste framing.

`agent_prompt_transport.prepare_agent_prompt` is a distinct policy API: it normalizes CR/CRLF,
rejects ESC and C0 except tab/newline, requires valid UTF-8, frames Codex bracketed paste,
and bounds the framed body to 64 KiB (Claude uses a plain body). Current source has no product
caller of that API; its dedicated tests establish the policy, not local-PTY prompt coverage.
No assertion that all prompts pass through it is supported. This disposition preserves the
trusted-input contract and existing size/transport rules; journal retention adds no prompt
validation, framing or protocol redesign.

## Production prompt handoffs

Prompt delivery types the line, waits for its echo turn to close, sends Enter alone and confirms
a turn took it. Interactive callers wait for completion. A production `advance_active` pass
shares one `HandoffBudget` of 4 seconds (`HANDOFF_WAIT_BUDGET_SECONDS`) across worker continuations
and reviewer launches; pending `PromptHandoff`s continue on later ticks.

- Each card gets the remaining allowance divided by the cards still to serve. An effect starts
  only with `HANDOFF_EFFECT_RESERVE_SECONDS` left. One operation deadline covers connection,
  sends, receives, status, admission, following delivery and fatal-close read-back. Actual elapsed
  time is charged; exhaustion grants no minimum or second offer. Admitted writes continue, and
  the next pass reads their result. A lost offer answer is resolved from supervisor status;
  a request frame cut off before admission has no effect.
- Before any effect, the caller persists the journal sequence floor and opening time. Worker
  continuations keep them in `WorkerContinuation.handoff`; reviewer launches use floor 0 and the
  launch-intent time. No floor means no wake or typing in that pass. Each pass reads status and
  journal evidence above that floor rather than remembering a stage. A typed line, accepted Enter
  or Enter still in flight is never resent. At most `SUBMIT_ATTEMPTS` submits occur; settle and
  confirmation bounds are measured from durable times. Spinner output proves no submission.
- A partial line closes delivery immediately. An unfinished drain remains owed, journalled above
  the floor and refused again on later passes; confirmed stop precedes replacement. A journal
  window missing the floor, a changed incarnation or duplicate line returns
  `prompt_handoff_unestablished`, for caller recovery rather than retyping. Expired confirmation
  returns `prompt_typed_but_no_turn_started` with the head busy.
- `status.output_idle_seconds` proves settle. Older supervisors are watched across ticks using
  supervisor/head PIDs, journal sequence and output count; all must stay unchanged. Dispatcher
  restart restarts that watch. A settle wait occurs only if quiet is due within the card's share.
  Unfinished stages return `HEAD_BUSY` with `handoff_stage=settle|typed|submitted`; pending spends
  no busy or launch attempt.
- Both callers use `dispatch/handoff_liveness.py`: only the exact-source provider cursor of the
  exact HeadRun proves progress. Heartbeats, advisory ingress, echo, spinners and accepted Enter
  do not. An unmoved cursor spends one attempt at each durable busy-schedule interval
  (30, 90, 210 seconds). Worker exhaustion reaches safe recovery then identity-fenced replacement;
  recorded terminal debt, unavailable or unanswered recovery goes to confirmed stop before
  delivery, without calling recovery twice. An exhaustion without a recorded rung still enters
  the ladder. The one return to delivery after recovery is persisted before the effect; progress
  resets only uncommitted state. Missing or foreign sources take existing terminal outcomes.
- Reviewer pending liveness is resolved before heartbeat grace, backoff, pending receipts or
  adoption can return. Before the first prompt an unavailable source uses `UnprovenSchedule`;
  exhaustion or a foreign source ends the undeliverable launch through confirmed stop and
  relaunch, without spending a delivery attempt. A report or verdict already on the card wins.
  Pending passes write only changed state.
- Reviewer `start` types nothing. `start_review` stops the worker or confirms a retained worker
  suspended before raising the launch; later delivery passes repeat that writer fence. A
  non-retained worker's stop is recorded on the launch intent.

Telemetry reports actual `handoff_ms` per card and bounded `{stage, subject, ms, allowed_ms,
outcome}` entries in `cards[].handoffs`; tick `handoff` carries allowance and elapsed cost.
Card `ms - handoff_ms` is non-handoff cost. `cards[].stages` accounts for the same total by effect
site, with any remainder `unclassified`; handoffs stay inside their stages. Release stages include
gate/e2e, runtime fence, merge, refresh/activation, commit read/watch, completion evidence, teardown
and terminal move. Old records without stage telemetry remain unknown. Implementation details
live in `runtime/head/handoff.py`, `runtime/head/local_pty/client.py` and `dispatch/tick_telemetry.py`.

## The runtime default

`runtime.head_runtimes` owns names and absent-key meanings; `runtime.head_runtime_backends`
alone turns a name into a backend.

`runtime.local_pty_head` keeps the concrete lifecycle class and consumed reader exports.
Its method mixins live in `local_pty_handoff` (prompt delivery and production budgets) and
`local_pty_delivery` (admission, socket/journal witnesses and outcomes). `local_pty_durable`
owns recovered state, `local_pty_journal` the journal fold and readers, `local_pty_inspection`
leases and cleanup fences, and `local_pty_values` shared values and refusal tokens.

- Profiles: `HEAD_RUNTIMES=("local-pty",)` and `DEFAULT_HEAD_RUNTIME="local-pty"`. An absent
  profile key selects it. Any other name, including `orca-legacy`, refuses at registry validation
  with the profile ID and the fix: set `runtime = "local-pty"` or drop the key. No profile is
  silently rewritten; the shipped registry omits the key.
- Records: `RECORD_RUNTIME_WHEN_ABSENT="orca-legacy"`. A durable HeadRun without a runtime,
  or explicitly naming that runtime, loads and round-trips as legacy. `is_legacy_record` is the
  shared predicate; `head-status` and the web show its legacy status. `build_head_runtime`
  raises `LegacyHeadRecordError` (an `UnknownHeadRuntimeError`) rather than launching or falling
  back. Unknown runtime names also refuse.
- Dispatcher ownership: cards and observers use owned git worktrees. A record with a legacy
  worker/reviewer/observer run, or a workspace under the Orca root which is not an owned git
  worktree, refuses launch, delivery, stop and teardown with `LegacyDispatcherRecord` before
  child execution or backend construction. The card is blocked with its record's reason; it is
  neither re-placed nor torn down through Orca. Legacy head status uses its PID heartbeat.
- Memory grants: legacy runtime records still load; that alone is not
  `runtime_identity_malformed`. Grants depend on process liveness, with missing/stale identities
  denied as `runtime_identity_unbound` or `runtime_identity_stale`. Old PO-bridge/health-probe
  grants remain usable while their process lives; new specs explicitly name `local-pty`.

A standing-agent tick without a usable `local-pty` profile starts no head and exits 1: registry,
role routing, HeadSpec or command failures and unsupported runtime names all fail closed.
It stops no head, creates/closes no report and leaves `head_run.json` and `active_report.json`
unchanged. An existing supervisor finishes its turn; a later usable tick finds that head and
busy-skips or brings up a replacement after it ends. A completed turn whose head stays idle is
stopped conditionally after `IDLE_HEAD_GRACE_SECONDS`, with `action="supervised-idle-stop"`.

Read the last `automation-state/<agent>/runs.jsonl` entry under `UMMANU_STATE` (normally
`~/ummanu-data/automation-state`): `action="no-supervised-head"`, `result="error"` and `error`
give the cause, also written to `journalctl -u ummanu-<agent>.service`. Any remaining
`terminal_handle.json`, even empty, unreadable or lacking a handle, refuses with
`action="supervised-owner-conflict"` and exit 1. The tick neither deletes it nor launches beside
it; remove it only after confirming the pane-era head it names is gone.
`TA_STATE` remains a compatibility alias; an explicitly present `UMMANU_STATE` wins, including
an empty value. Recovery guidance must name whichever alias the installation actually selected.

The generation and profile sidecar APIs are retired. `head_run.json` carries the current
head's resolved profile in its typed spec and records its ownership. Existing
`head_profile.json` and `terminal_generation.json` files remain historical evidence;
ticks neither read, rewrite nor remove them. The retained `terminal_handle.json` path
serves only the read-only existence fence, including when checking for an orphan report.
Legacy files receive no migration or automatic cleanup.

Host reconciliation's read-only scope projection, preserved runtime ownership and upgrade
evidence are in [Runtime scopes in host reconciliation](HEAD_SCOPES.md#runtime-scopes-in-host-reconciliation).

## Orca migration history

The Orca migration completed on 2026-09-25 (A20, secretary-1727; sprint:1459 and sprint:1461).
The `local-pty` parity criteria covered launch survival, prompt delivery, worktrees, retained heads, status and role environments.
The A20 exit checklist removed the Orca backend, dispatcher/agent paths, pane inventory, legacy launch-home fallback and host coupling.
`orca_binding` and old worktree roots remain only for curator routing; host-owned Orca cleanup was a separate PO action.
See git history at [5c8b7191](https://github.com/vladmesh/ummanu/commit/5c8b7191) for the criteria, checklist and evidence.
