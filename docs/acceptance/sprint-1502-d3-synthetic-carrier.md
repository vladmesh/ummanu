# SYNTHETIC d3 carrier: manual head_exit check

Status: SYNTHETIC. This file is the harmless work product of a one-off acceptance carrier card.
It is never merged. It describes how an operator checks that a real production watchdog writes a
real `head_changed` record with cause `head_exit` after one worker head of the carrier card ends.
It records no evidence and makes no claim that such a record exists.

## Scope

The scenario touches exactly one head: the worker head of the carrier card itself, identified by
the card ref and the `run_id` of its current worker `HeadRun`. Nothing else is in scope:

- not the sprint observer or any control or standing head;
- not the reviewer head of the carrier card;
- not any head of another card or another sprint;
- no pipeline freeze, since a freeze produces `operator_stop`, not `head_exit`.

The operator sends one signal, to one verified PID, once. Everything after that is the production
dispatcher's own behaviour.

## Synthetic input and actual production effect

Two things are artificial and two things must be real. Keeping them apart is the whole point of the
check.

Synthetic input:

1. The carrier card. Its task (write this draft) exists only so that a worker head is running.
2. The operator kill. A head normally ends on its own; here the operator ends it on purpose.

Actual production effect:

1. The wait watchdog reading the head's vitality as `Dead` on its normal wait tick and reclaiming
   it through its normal path ([Head vitality](../HEAD_VITALITY.md)).
2. The single journal record `kind: "head_changed"` on the carrier card, written through the
   normal pending/commit path after the stop is confirmed
   ([Head changes](../PROTOCOLS.md#head-changes)).

No audit event, fixture, runtime projection or hand-written journal record may stand in for the
second list. If the effect has to be produced by anything other than the running dispatcher, the
check has failed, whatever the CLI shows.

## Preconditions

- Production runs the main revision that contains the head change producer, and the dispatcher
  is not frozen or paused.
- The carrier card is In progress with a dispatcher-held worker head, and its `head_changes` list
  in `task show` is empty or its last entry is known, so a new entry can be told apart.
- The operator has read the card's authority section and holds the right to exactly one signal
  against this card's worker head.
- The worker head itself takes no part: it does not signal, stop, freeze or run dispatcher
  commands, and it does not write events. If it is relaunched, it continues the same draft.

## Verify the target before acting

The signal goes to a PID that was proved to be this card's worker head a moment earlier. Verify
all three values together, in one sitting, and act only if they agree:

1. Card ref. Read the card with `ummanu task show --ref <ref>` and confirm the title marks it as
   the SYNTHETIC carrier, the state is In progress and the role is `worker`.
2. Head run id. Read `ummanu head-status --instance <dir> --workspace <path>` for the card's
   workspace. Pick the row whose `kind` is worker, whose `head` is `alive`, and note its `run_id`.
   A reviewer row, an `absent` or `unproven` head, or more than one worker row stops the check.
3. Worker PID. Take the agent PID from that same row's launch identity (`process`), not from a
   process listing by name. Confirm in `/proc/<pid>` that the process is live and its working
   directory is the card's workspace.

If any value disagrees, or the row changes between reads, do nothing and re-read. A head that ends
by itself before the signal is not attributed to the operator and does not count as this check.

## Operator action

Send one SIGKILL to the verified agent PID. Do not stop the supervisor, do not remove the heartbeat,
do not touch the workspace, and do not run a dispatcher tick by hand. Write down the wall-clock time
of the signal; it is the lower bound for the record's `occurred_at`.

## What the watchdog is expected to do

On a following wait tick the vitality reducer reads the heartbeat naming a gone process and the
verdict becomes `Dead`, which outranks every other verdict. The watchdog then either respawns the
worker or, when the per-kind respawn ceiling is reached, escalates the round to Blocked. Once the
stop is confirmed it owes one change on the dispatcher record. The owed change is completed:

- as `replaced` when the next worker run is launched and its routing is recorded, or
- as `stopped` when a terminal effect of the round leaves the worker role without a successor.

Until one of these happens nothing is written, so an empty `head_changes` list right after the
signal is expected, not a failure.

## Reading the result

After the watchdog has acted, read the record from two independent surfaces of the same card.

CLI: `ummanu task show --ref <ref>` carries `head_changes`, oldest first. The new entry has `at`,
`request_id` and the payload.

Timeline: the card page draws each change between the transitions in time order with role, stopped
or replaced, cause, profile before and after, run ids and reason.

Both surfaces must describe the same single change.

## Success criteria

The check passes only if all of the following hold for one new entry:

1. `role` is `worker` and `cause` is `head_exit`.
2. `before.run_id` equals the `run_id` verified before the signal, and `before.profile` names the
   worker profile of that run.
3. Either `change` is `replaced` and `after.run_id` is a new run id (a same-profile respawn still
   differs in `run_id`), or `change` is `stopped` and `after` is `null` with the card in Blocked.
4. `occurred_at` is not earlier than the recorded signal time, and `reason` is present.
5. `request_id` has the shape `dispatcher-<attempt>-head-change-<ref>-worker-<run_id>` with the
   stopped run's id.
6. Exactly one entry was added. No entry names the observer, the reviewer or another card.
7. The Timeline shows the same role, cause, profiles, run ids and reason as the CLI entry.

Any other cause, a missing entry after the head was replaced, a second entry, or an entry that could
only have come from a hand-written source fails the check. The observer records the outcome in its
own sprint evidence, not in this file and not in the worker report.

## Cleanup without merge

The carrier never reaches main, whatever the outcome:

1. The observer cancels the card through the normal task workflow with the reason
   `synthetic d3 carrier, acceptance done`.
2. A pull request opened for the carrier branch is closed without merge.
3. The card ends archived as cancelled, never Done.
4. The workspace and branch are left to the dispatcher's normal cleanup; nothing from the carrier,
   including this file, is deployed or copied into another repository.
