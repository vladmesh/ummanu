# Head vitality

Head vitality answers "is the head working?" as three independent observation axes, fused over time
with hysteresis, then turned into a recovery intent. Modules:

- `src/ummanu/dispatch/head_vitality.py`: observation vocabulary and snapshots;
- `src/ummanu/dispatch/head_vitality_episode.py`: the episode reducer;
- `src/ummanu/dispatch/head_vitality_guard.py`: the destructive-step guard;
- `src/ummanu/dispatch/head_vitality_policy.py`: the recovery policy.

`HeadRuntime` owns the lifecycle boundary. The local-pty backend, the one head runtime, owns
delivery, turn lease, drain and stop atomically.

## Central invariant

Every external observation has a TOCTOU window: the head may start a new turn between the last reading
and any action. Agreeing channels reduce noise but never close the window.

> An observation reports facts. Fusion forms suspicion. Policy chooses intent. A runtime that
> atomically owns delivery decides whether an intervention is safe.

Local-pty performs the final admission/lease/epoch check inside its own lock against durable supervisor
evidence. Terminal output and filesystem fingerprints never grant a kill capability by themselves.

## Three independent axes

```text
Process  = Running | Suspended | Dead | Unknown      does a kernel process exist?
Turn     = Active  | Idle    | Unknown               is a turn in flight?
Progress = Advancing| Quiet  | Stagnant | Unknown    is the work moving?
```

A pid heartbeat answers only Process, a provider cursor only Progress. Turn was answered only by
pane readings before A20. Since secretary-1739 a `local-pty` head's Turn comes from its own
supervisor journal (`supervisor_journal`, below), which answers Turn and Progress. An axis a
snapshot cannot answer stays `Unknown`, and that survives serialisation: `Process=Running` with
`Turn=Unknown` is a valid state, and it is what an unreadable journal gives.

`Stagnant` is reserved for conclusions over time. No single observation produces it: one unchanged
cursor is `Quiet`.

## Snapshots

A `VitalitySnapshot` is one channel's reading of one head run at one instant.

- **Identity-bound.** Every snapshot carries the `HeadRun.run_id` it was proven against. A source whose
  attestation names another run degrades to `Unknown`/unavailable, never `Dead`. A snapshot never
  combines a new run's pid with an old run's cursor.
- **Unavailable is not no progress.** A missing pid file, unreadable supervisor status or journal
  yields `Unknown` axes, `availability=unavailable` and a bounded reason. A broken channel freezes
  knowledge; it never counts as stall evidence.
- **Pane readings were advisory (pane-era).** Before A20 they filled only `Turn`, stamped
  `source=pane_advisory`; readiness said whether a pane accepted input, never whether the head could
  be stopped. The source name stays so older episodes load.

| Source | Producer wrapped | Axes answered |
|---|---|---|
| `pid_heartbeat` | `dispatch.watchdog.head_process_status` | Process |
| `provider_cursor` | `dispatch.tui.provider_progress_for_run` | Progress |
| `pane_advisory` | pane-era: pane readiness (`{"idle": bool}`) before A20; no dispatcher status carries it since secretary-1723 | Turn |
| `execution_child` | `runtime.head.children.read_head_children` (the head's `/proc` descendants) | Progress |
| `supervisor_journal` | `runtime.local_pty_head.head_run_turn_reading` (the run's own `journal.jsonl`) | Turn, Progress |

Mappings:

- heartbeat `live-match` with `/proc` state `T` → `Suspended`; dead, zombie, or a pid reaped between the
  signal check and the `/proc` read → `Dead`; otherwise live → `Running`; missing, unreadable or
  mismatched → `Unknown` + unavailable;
- cursor moved since this run's previous snapshot → `Advancing`; unchanged → `Quiet`; unadmitted,
  foreign or unreadable → `Unknown` + unavailable; the first observation of a source records its cursor
  without a progress opinion;
- pane-era pane readiness, before A20: ready → `Turn=Idle`; busy → `Active`; unanswerable → `Unknown`;
- child processes (see [Child processes](#child-processes)): summed CPU or IO movement of the head's
  descendants since the previous reading past the noise floor → `Advancing`; less, or no descendant →
  `Quiet`; first reading → no opinion; `/proc` unreadable → the source is left out of the status;
- supervisor journal (see [Supervisor journal](#supervisor-journal)): an open turn → `Turn=Active`, a
  closed one → `Idle`, with `turn_since`; a new `provider.progressed` sequence since this run's
  previous reading → `Advancing`, the same one → `Quiet`, first reading → no opinion; anything the
  journal cannot account for → `Unknown` + unavailable. It never answers Process, so it is never
  `Dead`.

### Supervisor journal

`command_terminal_status` reads it for a supervised run whose heartbeat is a `live-match`, through
`host.supervisor_journal` → `head_run_turn_reading(heads_root, run_id)`. The reader reuses
`_journal_state`'s fold (`_replay_journal`) over the same bounded tail (`JOURNAL_TAIL_BYTES`, 64 KiB)
and returns `turn`, `turn_since`, `progress_seq`/`progress_at` and the run id it read. Since
secretary-1738 the supervisor writes `provider.progressed` only for a screen line not yet seen in the
turn, so the Progress half is real progress, not spinner redraws.

- `turn=active` while the last `turn.started` has no `turn.finished` after it; `turn_since` is that
  `turn.started`.
- `turn=idle` otherwise; `turn_since` is the latest of the last `turn.finished`, `input.accepted` and
  `turn.started`, so a delivery that reached the head restarts the clock even before its turn is
  folded.
- Bound to the run: only records whose `run_id` is the run's count, and a window that mixes in
  another run's records is refused. A reading naming another run is handed on as unavailable.
- Unavailable (`Turn=Unknown`, a bounded reason, never stall evidence, never `Dead`): the journal
  cannot be read; no record of this run; a torn last line; any record the strict validator refuses
  (below); `run.exited` (the heartbeat decides that one); no `turn.started` yet (bring-up, waiting for
  the prompt).
- **Strict validator** (`local_pty_head._strict_record`), applied to every line of the window before
  the replay, never coercing. The source reads the raw window (`local_pty.tail_window`), not
  `read_tail`'s records: `read_tail` coerces `seq` with `int()` for the admission reader, so a damaged
  `"12"` or `1e300` would reach the replay as an ordered integer. A line must be UTF-8 JSON with
  `schema_version` exactly `1`; `kind` a known string; `run_id` this run's string; `seq` a JSON
  integer (not bool, float or string) in `(0, 2^63)`, strictly increasing; `at` a finite int or float
  in `[2000-01-01, 2100-01-01)`; `turn` an integer in `(0, 2^63)` wherever present, and present on
  `turn.started`, `turn.finished` and `provider.progressed`, which the writer always numbers. One
  failing line makes the whole reading unavailable: no filtering, no coercion.
- A window that begins mid-history (`partial_head`) is the usual case, because a worker's journal
  outgrows 64 KiB within minutes (1727's run is 873 KB). It answers when it holds a `turn.started`,
  `turn.finished`, `run.started` or `run.exited` of this run, since the turn state after one of those
  depends on nothing older; without one it is unavailable. `_journal_state` still refuses every partial
  window, because admission must not conclude "no drain" from a window that may have lost one.
- One guard at the source: any exception while reading is `{"state": "unavailable"}`. The snapshot
  builder has one normaliser for every value it is handed (`head_vitality._journal_reading`): `bool` is never a number, a time must
  be finite, positive and no later than the observation plus 60 s, a sequence an int in `[0, 2^53]`.

The cursor is `j1:<progress_seq>`, kept on the episode like the other cursors. A sequence lower than
the previous reading's is unavailable, not quiet.

Snapshots are frozen dataclasses with `to_json`/`from_json`. A payload with an unknown version or axis
value raises instead of being normalised.

## Episodes

`VitalityEpisode` is the persisted hysteresis layer. `reduce_vitality(previous, snapshots, now,
thresholds, retained=..., answer_owed_since=...)` folds one tick's snapshots for a run into a durable
verdict. It is pure and deterministic: no I/O, no clock; the caller owns `now`.

```text
HealthyActive   a non-advisory source showed advancement now
HealthyQuiet    alive, no advancement yet, below every threshold
Suspended       /proc shows the process parked on a stop signal (not a stall)
Retained        the same parked process, held by the dispatcher's own retention
SuspectedStall  strong quiet outlived suspect_after
ConfirmedStall  strong quiet additionally outlived confirm_after
Dead            the heartbeat names a gone or unreaped process
Unverifiable    no strong channel answered; nothing may be concluded
```

Reducer rules (pinned in `tests/test_head_vitality_episode.py`):

- Snapshots naming another run are dropped and noted in `basis`. When all snapshots agree on a new run
  id, a fresh episode starts.
- `Dead` outranks everything. `Suspended` freezes the stall clocks: suspended time feeds no threshold,
  and quiet references shift past the frozen span on resume.
- With `retained=True` a parked process reduces to `Retained` instead of `Suspended`; the freeze is the
  same. `Dead` still outranks it, and a retained process that is running again is not `Retained`.
- Advancement from any non-advisory source ends a suspected or confirmed episode, resets phase
  timestamps, stamps `last_progress_at`/`last_progress_source` and bumps `activity_epoch`. Child
  process advancement is the bounded exception described under [Child processes](#child-processes).
- An unavailable source freezes its evidence and is tracked in `unavailable_since` until it answers.
  When every strong source (pid and provider) is dark the verdict is `Unverifiable`, except that an
  already confirmed episode stays confirmed.
- A caller-declared owed answer (`answer_owed_since`: the phase start or a rejected report, see
  [Idle-turn stall](#idle-turn-stall)), followed by an advisory (pane-era) turn seen to end, raises
  `HealthyQuiet` to `SuspectedStall` at once. Without an owed answer advisory readings carry no weight.
- Advisory Turn readings, a pane-era source, only corroborate in `basis` and never drive a stall
  verdict.
- The idle-turn stall, one rule (`_idle_turn_stall`): see [Idle-turn stall](#idle-turn-stall).
- Quiet is measured from `max(last_progress_at, quiet_since, child_progress_at)`, or `started_at` if
  none is set, not from the observing tick. The ladder climbs `HealthyQuiet` → `SuspectedStall` → `ConfirmedStall` as
  that age crosses `suspect_after` and then `suspect_after + confirm_after`.
- Confirmation is sticky. Only progress, suspension, death or identity change ends it.

### Child processes

A head running one long foreground command is silent in its terminal and provider journal until the
command returns (secretary-1665: two integration shards, respawned at 968 s of strong quiet with the
test child alive and on CPU). `command_terminal_status` therefore reads the descendants of the pid the
heartbeat proved (`live-match` only) through `host.head_children` → `read_head_children`: one scan of
`/proc/*/stat` builds the tree. Movement is measured over the **whole** tree: `total_cpu_ms` sums
`utime+stime+cutime+cstime` of every live descendant plus the head's own `cutime+cstime` (so children
already reaped, by a descendant or by the head, still count), and `total_io` sums `rchar+wchar` where
`/proc/<pid>/io` is readable. Only the described metadata is bounded: at most 16 descendants (half the
newest, half the most CPU) carry start time, counters, a command line redacted with
`runtime.redact.scrub_secrets`, flattened and bounded to 300 characters, and the output file when
`/proc/<pid>/fd/1` is a regular file.

The `execution_child` cursor is `c2:<uptime>:<total_cpu>:<total_io>;` plus `pid.start.cpu.io` for as
many described descendants as fit 240 characters, busiest first. A reading advances when the aggregate
grew by ≥ 500 ms CPU or ≥ 256 KiB IO since the previous reading (`CHILD_CPU_ADVANCE_MS`,
`CHILD_IO_ADVANCE_BYTES`); an aggregate that went down (a descendant died and nobody in the tree reaped
it) is no advancement for that reading. The per-process entries only name the mover.

The reducer fuses it separately from the head's own channels:

- child advancement makes the verdict `HealthyActive` (`basis` `advancing@execution_child`) and stamps
  `child_progress_at`, **only while** `now − child_activity_since < child_activity_ceiling`, where
  `child_activity_since` is the head's own quiet reference when the streak began. It never touches
  `last_progress_at`, `last_progress_source` or `activity_epoch`; the head's own advancement clears the
  streak;
- past the ceiling the child's movement is ignored (`basis` `child-ceiling:<n>s@execution_child`) and
  the ordinary ladder runs from the last accepted child progress: suspected at about ceiling + 5 min,
  confirmed at about ceiling + 15 min;
- a quiet child, no child, or no child reading leaves every other rule exactly as it was: the child
  source is excluded from the strong set that selects the quiet, pid-only and dark arms;
- child advancement does **not** hold a claude or codex head whose journal turn is idle while it owes
  its answer: for those TUIs a closed turn is a head at its prompt, not running the command its
  children are (`basis` `child-not-holding-idle-turn`). An open journal turn, and a head of any other
  adapter, keeps the hold, up to the ceiling.

The episode keeps the last reading's described descendant (`last_child_key`, `last_child_command`,
`last_child_output`, `last_child_at`). Any reading that saw a live descendant describes one: the
busiest measured mover of this reading (key `m:`), else the mover already on file while it lives, else
the youngest live descendant (key `y:`), which is the likeliest foreground tool command rather than a
helper started with the session. A head with only old helpers gets the youngest helper named; the line
is informational. A reading with no live descendant clears the fields; a tick without a child reading
keeps them.

On a wait-watchdog respawn (`_respawn_wait`, worker and reviewer) the successor's task document gets
one section, `## Interrupted command`, with the line `The previous head was stopped while running:
<command> (its output was redirected to <file>)` from the stopped run's own episode. The note rides
`DispatcherRecord.respawn_interrupted_command`, a transient field that is never serialised and is
cleared after the bring-up, so no rework, review or restart renders a stale one.

### Idle-turn stall

`reduce_vitality` decides it in one place, `_idle_turn_stall`, ranked after death, suspension and
advancement and before the child hold. It applies only when the run's adapter is in
`IDLE_TURN_ADAPTERS`, the `supervisor_journal` snapshot answered `Turn=Idle`, and the caller declares
an owed answer (`answer_owed_since > 0`).

- **Premise, and which heads it holds for.** Idle turn = 2 s pty quiet; verified to mean at-prompt
  only for the claude and codex TUIs, which animate during tool runs. The supervisor closes a turn on
  output quiet alone and cannot see the foreground command: a head whose child works without printing
  closes its turn too. The Claude Code and Codex TUIs redraw a working indicator while a tool runs, and
  the production journals show it: `f4b466341aa0437db48e46f7ae118254` (codex-sol-high worker,
  secretary-1738) kept each of four working turns open from delivery until 6–10 s after its report,
  through silent broad runs of about 220–300 s; `74bdb481750e45b496f71f2ec2ada0cb` (claude-opus-high
  worker, secretary-1739) kept one 32-minute turn open through two silent broad runs of about 180 s;
  `dc3dfbd19abd481fbfe488ced772d97f` (a codegen worker) kept its one turn open to the end of the work;
  and a sweep of the sixty most recent journals found no turn closed mid-work. So the rule is pinned
  to `IDLE_TURN_ADAPTERS = {"claude", "codex"}` (`head_vitality_episode`), read from
  `HeadRun.spec.adapter` and declared to the reducer as `adapter` by the wait tick
  (`wait_vitality._run_adapter`). A head of any other adapter (`hermes` today), or a run whose adapter
  cannot be read, is out of the rule: a silent-child head there keeps the child hold and the open-turn
  ladder.

- **Owed answer.** The wait tick declares it (`wait_vitality.answer_owed_since_for_wait`): a worker
  waiting for its report owes it since `worker_started_at` (every launch, continuation, rework and
  respawn stamps it) or a bounced report's `worker_answer_owed_since`, whichever is later; a reviewer
  waiting for its verdict owes it since `review_started_at`. The gate phase passes nothing: a finished
  worker at its prompt there is expected.
- **Clock.** Idle time runs from `max(turn_since, answer_owed_since, quiet_since)`, stored as
  `idle_turn_since`. The delivery that resumes a continued worker opens a turn, and its
  `input.accepted`/`turn.started` restarts the clock; a head is never charged with idleness from before
  it was asked; the report nudge's `quiet_since` restarts it too.
- **Ladder.** `SuspectedStall` at `idle_turn_since + idle_turn_suspect_after` (5 min),
  `ConfirmedStall` at `idle_turn_since + idle_turn_confirm_after` (10 min). `basis` says
  `idle-turn:<n>s@supervisor_journal`.
- **No laundering.** A stall the episode already holds (an open turn quiet long enough to be suspected,
  then closed) stands (`stall-stands`) unless the dispatcher restarted the head after it began: a new
  owed answer or a nudge.
- **Not decided here:** an open turn (quiet ladder and child hold as before), a retained or suspended
  head (the freeze stays), a head owing nothing, a journal that did not answer, and any tick on which a
  source advanced, including the provider cursor while the journal says idle.

The guard is unchanged: a confirmation earned while `provider_cursor` answers is acted on; with the
provider dark it waits for the outer ceiling like any other one-channel confirmation.

### How fast a stall is confirmed

| Shape | Suspected | Confirmed | Measured from |
|---|---|---|---|
| Journal turn **idle**, answer owed, claude/codex adapter | 5 min (`IDLE_TURN_SUSPECT_DEFAULT`) | 10 min (`IDLE_TURN_CONFIRM_DEFAULT`) | the idle turn's start (`idle_turn_since`) |
| Journal turn **open**, journal and provider quiet | 5 min (`suspect_after`) | 15 min (`suspect_after + confirm_after`) | last progress (`_quiet_reference`) |
| Open turn held by moving children | ceiling + 5 min | ceiling + 15 min | last accepted child progress, ceiling 45 min |

Ummanu-1727 round 2 (run `9c6b884b…`): its turn ended at 23:43:09Z; the episode reached
`suspected_stall` at 00:42:12Z. Replayed with the journal source and children moving on every tick, the
episode stamps suspicion from 23:48:09Z and confirmation from 23:53:09Z; at that night's 65 s tick
cadence the verdicts appear at 23:48:37Z and 23:54:02Z (`tests/test_local_pty_journal_turn.py`,
`Ummanu1727ReplayTests`).

### Pid-only evidence

- **No progress source has ever answered.** The pid is the only witness, and process existence is not
  liveness. Sustained "running, nothing else" ages to `SuspectedStall` at `suspect_after` and
  `ConfirmedStall` at `suspect_after + confirm_after`, measured from `started_at`. The absent provider
  is neither progress nor quiet.
- **A progress source this episode has witnessed** (it left a cursor) and is now dark keeps the freeze,
  bounded by `dark_ceiling`.

### Dark sources

A witnessed progress source is dark when it answers unavailable **or** produces no snapshot on a tick
(`basis` says `absent@<source>`). The wait tick's status can carry a live `pid_status` with no provider
channel, for example `reason: "pid"` (an exact live heartbeat) or `reason: "disconnected"`. A source
that never answered is not treated as dark; that is the pid-only case above.

`reason: "pid"` is a `local-pty` head's normal shape, and that shape carries its provider cursor
(`provider_progress_for_run` reads the run's own transcript). Before
secretary-1719 it did not. The episode then aged on the pid alone, and only child processes held it
healthy, within their ceiling: secretary-1703's working worker read `suspected_stall`, then
`confirmed_stall`.

Darkness freezes the stall clock for at most `dark_ceiling`, measured from the start of the current
outage (a source that answers again leaves `unavailable_since`; its next outage starts a new window):

- **inside the window** an earned `SuspectedStall`/`ConfirmedStall` stands, and a healthy episode stays
  `HealthyQuiet` with the dark channel's diagnostic as reason; `basis` says `dark:<n>s@<source>`;
- **past the window** the episode ages on the pid alone, as in the pid-only case, with a reason naming
  the dark source and its duration. An earned confirmation is preserved.

For the guard, "not dark" means "answered on the most recent reduction".

### Quiet restart after a nudge

The report nudge (`dispatch.worker_report.prompt_worker_report`) stamps `quiet_since = now`. The head is charged only with
silence after it was asked; `last_progress_at` is kept. Records without the field have no restart.

### Retention

`/proc` state `T` has two possible owners. The dispatcher parks a finished worker itself:
`host.retain_worker` sends SIGSTOP on `report:done` and `WorkerContinuation.begin_retention` records it,
so the worker stops editing while CI and the reviewer own the checkout and a red verdict can resume the
same conversation.

`dispatch.wait_vitality.reduce_and_store_vitality_episode` passes `record.worker_continuation.retained` for the worker head
(the review head always passes `False`). `Retained` earns no rung: no SIGCONT, no nudge, no operator
escalation, and the guard refuses every destructive step (`retained`). A head stopped without an
active retention follows the `Suspended` ladder.

### Thresholds

| Threshold | Default |
|---|---|
| `suspect_after` | `IDLE_STALL_DEFAULT` (5 min) |
| `confirm_after` | `2 × IDLE_STALL_DEFAULT` (10 min) |
| `dark_ceiling` | `2 × IDLE_STALL_DEFAULT` (10 min) |
| `child_activity_ceiling` | `CHILD_ACTIVITY_CEILING_DEFAULT` (45 min) |
| `idle_turn_suspect_after` | `IDLE_TURN_SUSPECT_DEFAULT` (5 min), from the idle turn's start |
| `idle_turn_confirm_after` | `IDLE_TURN_CONFIRM_DEFAULT` (10 min), from the idle turn's start |
| suspension response window | 5 min (`UMMANU_HEAD_SUSPENSION_RESPONSE_SECONDS`, `SUSPENSION_RESPONSE_WINDOW_DEFAULT`) |
| deterministic refusal limit | 3 |
| worker report outer ceiling | `WORKER_REPORT_STALL_DEFAULT` (6 h) |
| gate pending outer ceiling | `GATE_PENDING_STALL_SECONDS` (6 h) |

`dark_ceiling` must outlast a provider startup window, during which the output cursor cannot tell a
starting head from a settled one, and stay far below the six-hour ceilings. A dark and quiet head is
nudged at `max(dark_ceiling, suspect_after)`.

The idle-turn pair judges a finished, silent head of a verified adapter (see
[Idle-turn stall](#idle-turn-stall) for the premise and its evidence). The lower bound is the
dispatcher's latency in accepting an answer the head just wrote (one tick) plus a delivery about to
land; the card bounds confirmation at 10 minutes.

`child_activity_ceiling` must outlast the longest command a worker legitimately runs in the
foreground (two `timeout 580` integration shards, or one broad suite: about 20 minutes, plus slack for
a slow host) and still catch a hung child that spins within the hour. Forty-five minutes puts a
spinning child's confirmation at about an hour of head silence.

**`UMMANU_HEAD_IDLE_STALL_SECONDS` governs no production path.** It is read only by
`watchdog.idle_stall_seconds()`, whose last production caller (the idle fence and clock-only wait
ladder) was removed in cadc5c7 when the wait tick moved onto the vitality verdict; today only tests call
it. The vitality thresholds are built from the constant `IDLE_STALL_DEFAULT` and ignore the variable,
which is why an installation setting it to 1500 still confirmed a stall at 968 s. It is deliberately
**not** wired into the thresholds: doing so would move every head's suspicion from 5 to 25 minutes and
confirmation from 15 to 75 minutes as a side effect of a workaround (issue:b5195041, 2026-08-12) that
predates the vitality ladder, while the failure it was meant to cover — a working head read as stalled —
is now answered by the child-process source above. An installation may drop the line.

### Verdict persistence

The worker/review wait tick persists each role's episode (`worker_vitality_episode`,
`review_vitality_episode` on the dispatcher record) and writes one durable comment per verdict change,
keyed on the transition. A tick whose status carries none of the observed sources runs no reduction and
writes nothing. A reduction failure degrades to "no episode" with a comment and never breaks the tick.

The episode format stays at `EPISODE_VERSION = 1`: fields added later (`quiet_since`, the Turn pair,
the child-process fields `child_progress_at`, `child_activity_since`, `last_child_*`, and
`idle_turn_since`) are optional with empty defaults, so an older record loads, and an older reader,
which reads named keys only, ignores them. Snapshots gained optional `child_key`, `command`,
`output_path` and `turn_since` on the same terms.

## Decision path

The wait tick decides from the persisted episode's verdict. The reduction runs on every wait tick,
including not-live shapes: a heartbeat naming a gone process reduces to `Dead`; an unreadable status over
a live process is an observation failure that waits.

One verdict outranks the table below: **provider failure** (secretary-1799, ummanu-108,
`dispatch/provider_failure.py`). Before the episode is reduced, `wait_watchdog` asks the host whether the
head's last completed turn ended on a provider error (a spent usage limit, 401/403, 429, 5xx, a
reconnect loop that gave up), read from the run's own Codex rollout or Claude transcript, or the bottom
of its PTY screen when no source is bound. If it did, the head is not stalled, it was refused, and no
stall reading applies: its resource is recorded red until the reset the provider named (else a bounded
backoff), the head is stopped, and the role is relaunched on the next launchable head of its fallback
chain on that same tick; when every provider of the chain is down the card is Blocked once for the
operator with every resource and its reset. No nudge, respawn or round. A head idle at its prompt after
such an error would otherwise read `SuspectedStall` at +5 min and `ConfirmedStall` at +10 min under the
idle-turn rule and be respawned into the same provider (the 2026-09-25 and 2026-10-06 cases). The
details are in [PROTOCOLS.md](PROTOCOLS.md#provider-failure-of-a-heads-turn), with the probe statuses
(`timed_out` included) in [Resource probe statuses](PROTOCOLS.md#resource-probe-statuses).

| Verdict | Wait-tick action |
|---|---|
| `HealthyActive`, `HealthyQuiet`, `Unverifiable` | `wait`. Fresh evidence renews the outer `worker_waiting_since` window. A recovered suspension lands here with the ladder cleared. |
| `Retained` | `wait` only; the role's wait clock is renewed. |
| `Suspended` | Recovery policy: one identity-fenced SIGCONT per suspension span, a response window, then operator escalation. Never a stop. |
| `SuspectedStall` | At most one idempotent report nudge per round generation, then visible degradation (`{kind}-stall-suspected`). Never destructive. |
| `ConfirmedStall` | One report prompt if the round has not spent it, else `dispatch.wait_vitality._trigger_wait_watchdog` → respawn once → escalate to Blocked. |
| `Dead` | Reclaim via `dispatch.wait_vitality._trigger_wait_watchdog`. |
| No episode / `Unverifiable`, ceiling elapsed | Operator escalation, head untouched (`_escalate_unobservable_wait`): one idempotent durable comment naming the evidence gap and a degraded `{kind}-unobserved-wait-escalated` outcome. Before that, an authoritative deterministic refusal seen `deterministic_refusal_limit` times escalates without waiting for the ceiling. |

### The guard

Every watchdog-driven destructive step passes
`ummanu.dispatch.head_vitality_guard.assert_destructive_allowed` before anything is stopped, killed,
respawned or replaced. It allows only `ConfirmedStall` and `Dead`. Refusal classes: `missing-episode`,
`foreign-run` (episode names another HeadRun), `healthy-active`, `healthy-quiet`, `unverifiable`,
`suspended`, `retained`, `suspected-stall`, `pid-only-ceiling-unelapsed`.

A confirmation earned with no progress source answering (pid-only, or a witnessed source now dark)
also requires the role's outer ceiling (`WORKER_REPORT_STALL_DEFAULT` class) to have elapsed since the
episode began; otherwise `pid-only-ceiling-unelapsed`. Only a confirmation on quiet that a progress
source is still answering for is acted on without that hold. A raising reducer fails safe to `wait`
plus one comment.

A refusal produces a degraded `{kind}-guard-refused` outcome and one idempotent durable comment keyed
on the wait cycle and refusal class. The body says only what that key names; live measurements (quiet,
dark sources, next deadline) stay on the episode and are read with `ummanu head-status`.

Guarded entry point: `dispatch.wait_vitality._trigger_wait_watchdog`, which fences both arms
(`dispatch.wait_vitality._respawn_wait`, `dispatch.wait_vitality._escalate_wait`) through `dispatch.wait_vitality._guard_or_wait`. The no-episode fallback's evidence
branches (`no output since launch`, `no terminal progress`) keep their triggers but act only under
`dispatch.wait_vitality._trigger_wait_watchdog`; its pure clock branch escalates without destroying.
`DispatcherRuntime._stop_worker_confirmed` and `_end_review_pane_confirmed` carry no guard of their
own: a vitality stop reaches them through the guarded arms above, and their other callers are the
stops below that do not act on vitality.

Not guarded, because they do not act on vitality:

- operator-initiated stops (`CommandHostRuntime.stop_head` from an explicit operator command);
- card-lifecycle stops: Done/Blocked transitions, drain, the review bring-up's confirmed worker freeze
  (`_adopt_launch_intent`);
- launch-recovery stops in `ummanu.dispatch.launch.resolve_launch_intent`, which act on durable launch
  intents and heartbeat identity;
- the provider-failure stop (`dispatch.provider_failure`), which acts on the run's own provider error
  record, not on vitality, and runs ahead of it.

Tests: refusal classes in `tests/test_head_vitality_guard.py`; call-site coverage in
`tests/test_head_vitality_guard_sites.py` (guarded paths with the guard forced to refuse and through the
real guard; unguarded stops never consult it).

## Recovery policy

`head_vitality_policy.py` consumes only a persisted `VitalityEpisode` and returns a `RecoveryDecision`:
an intent, the rung it leaves the head on, and telemetry detail. It executes nothing; the dispatcher
executes intents under the guard. No input produces anything beyond `escalate_operator`.

### Rungs

Rung state persists on the episode (`recovery_rung`, `recovery_span_started_at`,
`deterministic_refusals`), so a dispatcher restart resumes the same rung.

| Rung | Verdict | Intent | Meaning |
|---|---|---|---|
| — | `Retained` | `observe` | Checked first, before the deterministic-refusal path. Resets the ladder to 0. |
| 0 | Healthy\*, `Unverifiable`, `Dead` | `observe` | Nothing earned. A recovered suspension clears the ladder here. |
| 1 | `SuspectedStall` | `nudge` | The wait tick spends the single nudge (`dispatch.worker_report.prompt_worker_report`). |
| 2→3 | `Suspended`, new span | `sigcont` | One identity-fenced SIGCONT per span; the response window opens. |
| 3 | `Suspended`, window running | `observe` | The reduction flips the verdict as soon as the head resumes. |
| 4 | `Suspended`, window expired | `escalate_operator` | One durable comment; holds for the rest of the span. Never kill. |
| — | deterministic refusal ×N | `escalate_operator` | After `deterministic_refusal_limit` (3) identical authoritative sightings. |

A suspension span is keyed on the reducer's freeze stamp (`stall_frozen_since`): it starts when the
process is first seen parked and clears when it runs. Within a span nothing re-fires; a new span
restarts the ladder; recovery resets it to 0. Repeated identical observations are therefore free.

The policy does not yet route rungs through `request_drain`, safe replacement at a quiescent boundary,
same-profile respawn, runtime failover or evidence-backed `block`. `ConfirmedStall` recovery is respawn
once, then escalate.

### SIGCONT execution

`dispatch.wait_vitality._sigcont_head` is the only signal this path sends. At send time it re-verifies
identity through `guard_head_run_identity` (pid, boot id, proc start time, expected HeadRun); a
mismatched, unreadable or vanished heartbeat sends nothing. Delivery follows `_signal_head`: the head's
own process group when it has one, else the pid. SIGTERM/SIGKILL stay behind their guarded entries.
Each send writes one durable comment (`{kind}-vitality-sigcont`) naming the span and response window,
idempotent via the span stamp in the request id.

The response window must outlast several ticks so a resumed head can show life. Expiry escalates to the
operator and touches nothing.

### Gate phase

While CI is non-terminal, each `_gate_pending` tick runs the same reduction and policy for the worker
head: a stopped process is seen and SIGCONT'd within one tick, and an expired window reaches the operator.
A probe failure falls back to ordinary gate behaviour. `GATE_PENDING_STALL_SECONDS` is the outer
ceiling for the CI rollup (Blocked for a human, non-destructive); `WORKER_REPORT_STALL_DEFAULT` plays the
same role on the report-wait path.

### Deterministic refusal reasons

Only authoritative deterministic reasons may skip rungs. `DETERMINISTIC_TERMINAL_REASONS` is the
allowlist of refusals naming a property of this launch that retrying cannot change: invalid
configuration, missing executable, authentication rejected, resource exhausted, and
`terminal_split_source_not_found`. Matching is token-in-bounded-string against the diagnostic on the
snapshot (a dark source's reason is carried onto the episode). Timing, availability and transport
refusals do not qualify: counting them would let one dark channel fast-track a live head to
escalation. A tick with no deterministic reason resets the count.

The `terminal_split_*` tokens come from pane launches before A20, when heads ran as Orca panes and a
reviewer was split from the worker's pane. A `local-pty` reviewer is a second supervised process in the
worker's worktree, so its bring-up splits nothing and never produces them.

Tests: rungs and idempotency in `tests/test_head_vitality_policy.py`; real-process SIGCONT and
foreign-identity refusal in `tests/test_head_vitality_policy_execution.py`; wait-tick and gate-phase
decisions in `tests/test_head_vitality_wait_decisions.py` and `tests/test_head_vitality_legacy_path.py`.

## Regression invariants

Each invariant is replayed tick by tick through the snapshot builders, fed the producer payload shapes
(`head_process_status`, `provider_progress_for_run`, and the pane-era pane readiness) and folded by the
reducer under `DEFAULT_VITALITY_THRESHOLDS`. A false "working" costs idle time; a false kill loses a
live round, so a verdict that can stop a head needs strong admitted evidence.

| Invariant | Tests |
|---|---|
| Provider `Advancing` ⇒ `HealthyActive`; advisory pane-idle (pane-era) alone never leaves `Unverifiable`; no destructive verdict while the transcript moves; such a head is never prompted or respawned. | `IssueB5195041CodexTranscriptBlindnessTests`, `IssueB5195041LegacyIdlePathTests` |
| Busy pane (pane-era advisory) + `Running` + `Advancing` ⇒ `HealthyActive`; readiness unavailable is Turn-only and never stall evidence; unknown provider ⇒ `HealthyQuiet`, never `Dead`/`ConfirmedStall`. | `Issue3e7abdf9BusyReadAsUnavailableTests` |
| `Running` + admitted `Quiet` ⇒ `SuspectedStall` at +300 s and `ConfirmedStall` at +900 s from last progress; a busy pane (pane-era advisory) only corroborates. | `Issue8f86ed63BusyMasksStallTests` |
| `/proc` state `T` ⇒ `Suspended` within one tick; stall clocks frozen; never `ConfirmedStall` or `Dead`; the gate-pending tick SIGCONTs a non-retained suspended worker within one tick. | `IssueFe04011bStoppedWorkerSixHourCeilingTests`, `IssueFe04011bLegacyGatePendingTests` |
| A repeated deterministic reason with a live terminal keeps `Unverifiable` and escalates after 3 identical sightings; a repeated heuristic reason earns only observation. | `CodegenOrchestrator1194DeterministicSplitFailureTests` |
| A confirmed retention ⇒ `Retained`: no SIGCONT or other rung, so a red gate reuses the suspended session; `Dead` still outranks it. | `Issue02fe04d7RetainedWorkerTests` |
| A dark progress source freezes only for `dark_ceiling`, then `SuspectedStall` (spending the nudge) and `ConfirmedStall`; the reason names the dark source; nothing is stopped before the outer ceiling. | `Ummanu1517Tests`, `Ummanu1517WaitTickTests` |
| A status with no provider channel (`reason: "pid"`, `"disconnected"`) after the provider answered once is stamped `absent@provider_cursor`, takes the `dark_ceiling` window, and a confirmation is held behind the outer ceiling. | `ProviderLessStatusShapesTests` |
| Pid-only `Running` with no progress evidence ages to `SuspectedStall` then `ConfirmedStall`. | `Issue06dcf6cbUmbrellaLivenessContractTests` |
| A turn that ended on a provider error (a spent quota included, mid-run included) falls back on the tick that sees it: no `SuspectedStall` action, no respawn; the resource is recorded red until its reset and the role moves down its chain (one Blocked for the operator when every provider is down). | `ProviderFailureFallbackTests` (`tests/test_dispatcher_provider_failure.py`), `tests/test_provider_failure.py`, `tests/test_cross_family_fallback.py` |
| Journal `Turn=Idle` on a claude/codex run with an owed answer ⇒ `SuspectedStall` at +5 min and `ConfirmedStall` at +10 min from the idle start, child activity notwithstanding; the same silent-child journal on another adapter stays healthy under the child hold; an open turn keeps the ladder and the child hold; a continued worker that works is never suspected; every hostile journal value makes the reading unavailable. | `Ummanu1727ReplayTests`, `IdleTurnStallRuleTests`, `IdleTurnAdapterPremiseTests`, `ResumedWorkerThatWorksTests`, `HostileJournalValueTests` |

Reducer timelines live in `tests/test_head_vitality_regression.py` and
`tests/test_head_vitality_episode.py`; wait-tick and gate behaviour in
`tests/test_head_vitality_legacy_path.py` and `tests/test_head_vitality_wait_decisions.py`. The
vitality suites carry no `expectedFailure` markers.
