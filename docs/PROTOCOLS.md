# Protocols

`--instance` accepts either an instance directory or a direct path to `instance.yaml`. The instance
holds installation configuration and the portable checkpoint in `state/`; the data directory holds
local mutable and derived runtime state.

## Checks and host ownership

```bash
python3 -P -m ummanu doctor --instance INSTANCE
python3 -P -m ummanu doctor --offline --instance INSTANCE
python3 -P -m ummanu doctor --instance INSTANCE --host-fixture DIR
```

`doctor` is read-only. A normal run checks config, data and live inventory; `--offline` keeps only
config and data; `--host-fixture` replaces live inventory with a deterministic fixture and cannot be
combined with `--offline`. Exit `0`: no findings; `1`: findings (or warnings under `--strict`); `2`:
invalid input or unreachable inventory. Without `--strict`, warnings alone stay green.

### Board schema gate

Every operational board-store connection reads Alembic's version table when it opens
(`board/schema_gate.py`, [Board store §7.4](BOARD_STORE.md#74-schema-versioning-and-migrations)).
A store at an earlier revision of this build's migrations, or with no schema at all, is refused
before any schema-dependent statement with code `schema_owed`, the actual and expected revisions,
and every owed migration in application order; the upgrade's `board-store` step applies them. A
store at a revision this build does not know was migrated by a later build and is read as additive,
not refused. Bootstrap, provisioning, migration and restore are exempt: they are how an owed schema
is inspected and applied.

A normal `doctor` run reads the same assessment on the `read` role and never writes. Text prints a
`board schema:` line (`current at …`, `owed: …` followed by `board schema pending: …`, `ahead: …`,
`unavailable: …` or `not configured: …`); JSON carries the same dict as `board_schema`. An owed
schema adds the finding `schema_owed` with `actual`, `expected` and `pending`, and a store that is
configured but cannot be read adds `board_schema_unavailable`; both exit `1`. A current, ahead or
unconfigured store adds no finding. `--offline` and `--host-fixture` read no live board store and
report it `not inspected`; in JSON their status snapshot skips the sprints too
(`installation.sprints.skipped`), since sprints are read from that store.

Live parity uses the same desired state as `reconcile`: each project checkout is checked against the
normalised absolute path from its binding, including a path outside the projects root; the projects
root is only used to find unmanaged checkouts. An unreachable or unnormalisable expected checkout
makes project inventory unavailable (exit `2`), not a missing-on-host finding. Unit files and the
enabled/active state of long-running services and timers are
checked; a missing resource or unhealthy required state is a finding (exit `1`); a oneshot service
may be inactive. Units in `foreign_units` are excluded from managed parity.

```bash
python3 -P -m ummanu reconcile plan --instance INSTANCE [--host-fixture DIR]
python3 -P -m ummanu reconcile adopt --instance INSTANCE --logical-id ID [--yes]
```

`reconcile plan` reads desired state and inventory, applies nothing and writes no manifest;
`--offline` is rejected. Exit `0`: no conflicts; `1`: conflicts; `2`: invalid input or unreachable
inventory.

`reconcile adopt` touches one existing desired systemd unit. It adopts it only when the installed file
matches the shipped file's digest byte for byte, shows a fingerprint, and is a preview without `--yes`.
A confirmed run atomically adds a managed record without changing systemd or worktrees; any other
resource kind has no verifiable adoption identity and is refused.

## Tasks

The public path to the board is `ummanu task`. A card carries a `ref`, project, type, state,
dependency, claim, routing, workspace, retry and audit metadata:

```text
Issues → ready → in_progress → validate → assessment → done
                         └───────────────────────────→ blocked
```

Board columns, in order: `Issues, Ready, In progress, Validate, Assessment, Blocked, Done`.

`assessment` is a durable wait for a decision. A substantive reviewer verdict, green or red, parks
the card there: the reviewer is stopped, the round's worker stays suspended with its workspace, and
nothing merges or reworks until a decision. Mechanical gate outcomes (CI, stand run, pre-merge
re-check) resolve in Validate and never pass through Assessment. The steward may move a card from
Assessment to Blocked with a reason; workers and reviewers move nothing. A card left in Assessment
past the steward's stale threshold is reported like any other stuck card.

### Card kinds, live impact and the review choice

`task create --type` takes one of five kinds:

- `code`: a change to a repository, delivered as a candidate branch.
- `research`: an investigation or experiment; hypotheses and budget live in the description.
- `infra`: work on hosts or services rather than a repository.
- `decision` and `operation`: a question or a short action the PO answers inside a turn of its
  sprint's PO session. The PO service executes them, not a head; see
  [Decision and operation cards](#decision-and-operation-cards).

Kind, live-impact flag and review choice are set at create and shown by `task show` and `task list`
as `type`, `live_impact` and `review`. None of them is editable afterwards.

**Live impact** (`--live-impact`, research only; refused for `code` and `infra`) marks a research
card that touches live systems. Such a card is refused at create unless its description declares
bounds, and `task edit` cannot replace the description with one that lacks them:

```markdown
## Impact bounds

### Allowed
What may be touched.

### Forbidden
What must not be touched.

### Cleanup
How to undo what the card changed.
```

All three subsections must be present and non-empty inside the `## Impact bounds` section; the
refusal names what is missing or empty.

**Review choice** is stored on the card as `review: required|skipped`. The default is `required`
for `code` and `skipped` for every other kind; `--review required|skipped` overrides it in
either direction (a `decision` or `operation` card refuses `required`), and a reviewer head never changes it. The review choice decides whether review
runs; the reviewer head decides who reviews, and a sprint's reviewer pin sets that head exactly as
for any card, whatever the review choice. A caller-supplied reviewer head with `skipped` is refused,
at create and at `task edit --review-head`, unless it is the head the sprint pins. A card written
before the choice was stored reads as `required`. The store keeps the value in `tasks.review` and
`tasks.live_impact`, and export/restore carries both.

**Lifecycle by kind.** A `code` card delivers a candidate: after `report:done` the dispatcher runs
the mechanical gate (branch, pull request, CI), then review if required, and Done means the candidate
was merged. A `research` or `infra` card has no candidate. The worker's checkout need not be committed
or clean, no branch is published, no pull request is opened, no CI gate or workflow runs, and an
unchanged HEAD is never rejected as a stale result: every done report of a new round (for example
after an observer `rework`) is a fresh one. Its path is:

```text
report:done → Validate → reviewer, only if review: required
  → Assessment and an observer release/rework/reslice decision, if the sprint parks for decisions;
    otherwise release at once
  → completion evidence check → teardown of the workspace and heads → Done
```

`report:blocked` behaves as for any card.

**Review choice.** `review: skipped` launches no reviewer for any kind, and `review: required` launches
one for any kind. A `code` card with `skipped` still runs the full mechanical gate and merges on
release; it only has no reviewer step. A release without a reviewer records the verdict as `missing`.

**Completion evidence.** Every way a card reaches Done (an automatic release outside a parking
sprint, an observer release from Assessment, and a release replayed after a lost tick) passes one
dispatcher check. For `code` the evidence is the merge the release performs. For `research` and
`infra` it is a marked comment on the card written by the dispatcher; a comment from any other role
that carries the marker is not evidence. Without it the card goes to Blocked, not Done, with the
reason `completion evidence missing` naming the absent marker, and its workspace is kept.

- `infra`: the worker's done report body must carry two non-empty sections, `## What was done` and
  `## How to verify` (a command or an observation). `task report --kind done` refuses a body that
  lacks either. When it accepts the report, the dispatcher writes the completion record, one comment
  per report round, visible in `task show` and carried by export/restore:

  ```markdown
  [completion:infra]

  ## What was done

  ...

  ## How to verify

  ...
  ```

- `research`: a completion link naming the card's report directory:

  ```markdown
  [completion:research]

  state/knowledge/reports/<card ref>/
  ```

  The worker puts its report and every artifact (markdown, scripts, data, subdirectories) in one
  declared directory of its workspace, `.ummanu-report/`, with the report itself in a non-empty
  `.ummanu-report/report.md`. The directory is never committed to the project repository; bring-up
  adds `/.ummanu-report/` to the checkout's Git exclude. `task report --kind done` on a research
  card refuses, with a `validation` error, a workspace without that file.

  After the report is accepted and the review, if required, is done, and before the card parks in
  Assessment or is released outside a parking sprint, the dispatcher copies `.ummanu-report/` to
  `state/knowledge/reports/<card ref>/` through the knowledge directory writer (`knowledge write
  --dir`, actor `dispatcher`; files only, no Git), then writes one `[completion:research]` comment
  whose request id is keyed on the report generation. The observer therefore decides with the report
  already in knowledge, and a replayed tick writes and comments nothing new. An observer release
  repeats the transfer, a no-op for a card parked green and the transfer itself for a card parked by
  a red verdict. A rework round's next report replaces the directory's whole contents and writes a
  fresh link; the checkpoint history keeps the earlier rounds.

  If the transfer is refused or fails, the card goes to Blocked with the reason `research report
  transfer refused (<cause>)`, where the cause is `report_missing`, `path`, `source_missing`,
  `source_empty`, `special_file` (a symlink, a special file or an entry whose name starts with
  `.git`), `secret`, `size_cap` or `write_failed` (a filesystem or git error). No link is written, nothing is committed under
  `reports/<card ref>/`, and the workspace is kept. The completion evidence check is unchanged and
  still the only check before Done.

### Decision and operation cards

A `decision` card asks the PO a question; an `operation` card asks it to do something short. Both are
executed by the PO service in a turn of the sprint's PO session, never by a head, and both are
no-candidate kinds: no workspace, branch, pull request, CI or reviewer.

**Create.** The observer or the PO creates one (`task create --type decision|operation`, no other
role). The one exception is the dispatcher's own `decision` card for a spent e2e budget
([The e2e run budget](#the-e2e-run-budget)); it cuts no operation card. It must carry `--sprint`, except
when the PO cuts it inside a PO turn: such a card carries its
[origin](#po-delegation) and may name no sprint, and the session of that turn executes it (below). The
dispatcher's budget decision for a card outside every sprint carries that card's origin the same way.
With no sprint and no origin it is refused (`validation`) as before. It refuses, each with its reason, `--head`, `--review-head`,
`--review required`, `--live-impact`, `--seed-ref` and `--base-branch`. Its review is `skipped`, and
a sprint's executor pins do not apply to it. The PO needs no `--sprint-override` to create one in its
sprint.

An `operation` card names the production it touches: `--touches-production <project>|none` is
required on it, where `<project>` is a project of the instance registry (the check `sprint create
--allow-production` makes) and `none` means it touches no production. The flag is refused on every other
kind, and a missing flag or an unknown project is refused (`validation`) with nothing written. The value
is one typed field of the card's extension bag (`extensions.extra.touches_production`; no column, no
migration), written only at create and part of the create's request identity. `task show` and `task
list` carry it as a top-level `touches_production`, and the card page shows it. See
[Production rights](#production-rights).

**Submit.** When the dispatcher claims a Ready one it launches no head and cuts no workspace. The
claim moves the card In progress; such a card neither takes nor counts against the claim capacity,
which counts heads. The dispatcher then:

1. resolves the sprint's PO session, `sprint_session(sprint_ref, request_id)` (see
   [The sprint's PO session and productions](#the-sprints-po-session-and-productions)). A card with no
   sprint goes instead to its origin session, or to the session that succeeded it in the card's origin
   line (`session_outcome: origin`; no sprint is resolved and no session opened). When that session is
   closed or missing at the submit, the dispatcher opens its successor the way a result return does
   ([PO delegation](#po-delegation)), records it on the card and submits there under the same submit id;
   a successor that cannot be opened yet postpones the submit to the next tick (`po-service-unanswered`).
   Such a card's input says it belongs to no sprint and carries no sprint comments;
2. submits one input to that session, `source: dispatcher`, carrying the card ref, kind and title, the
   card body, the sprint's comments in board order, and the exact completion command, and beside the
   text the card's structured facts (`card`, below):

   ```text
   python3 -P -m ummanu task complete --ref <card> --role po --kind <kind> --body-file <file> --request-id <id>
   ```

The input also quotes the handover command, for a card only the owner can answer (see
[Handover to the owner](#handover-to-the-owner)):

```text
python3 -P -m ummanu task handover --ref <card> --role po --to owner --reason-file <file> --request-id <id>
```

The facts are `{card_ref, kind, touches_production, sprint_ref, input}`: `touches_production` is the
operation card's value (`null` on a decision card), `sprint_ref` is empty for a card with no sprint,
and `input` is `card` for this submit and `owner_answer` for a follow-up carrying the owner's answer
(below). A card with an origin records the session it is submitted to on itself (`po_return.executor`),
before the submit, for its reader; it is no proof of who completed the card. They are part of the submit's
fingerprint (`send_fingerprint(session, text, card)`, in `po_requests` and on the queued input), so a
replay is the same session, text and facts, and anything else under the same id is `request_conflict`
as before. A web input carries none, and a send without facts binds exactly what it bound before them.

The four request ids (resolve, submit, completion, handover) are `dispatcher-<claim attempt>-po-session-<card>`,
`...-po-submit-<card>`, `...-po-complete-<card>` and `...-po-handover-<card>`, derived at claim and kept on
the card's dispatcher record with the resolve's answer (the session, `created` or `recorded`), the frozen
input text and facts, and the submit's answer. A resolve or submit the service did not answer (`outcome_unknown`, the service not
running, or a refusal coded `unavailable`) is repeated on the next tick under the same id, never a
fresh one, since a fresh id could open a second PO session. The card stays In progress and the tick
reports `po-service-unanswered` (degraded); a service that stays down is not a failure of the card. A
refusal that is an answer (the sprint closed or missing, the session closed, the id bound to something
else) Blocks the card with `the PO service refused the <resolve|submit> of this card: <reason>`. A
dispatcher record lost with its state file is rebuilt from the dispatcher's own claim event, whose
request id carries the claim attempt, so the same requests are repeated. The frozen input text is lost
with the record, so the rebuilt record composes the input again from the card and the sprint as they
are now, and the service may answer its submit with `request_conflict` when the text changed. That
conflict is taken as submitted, not as a refusal, when the submit id is already the service's: a turn in
`po_requests`, or an input still pending in `po-queue/` for the same session. Any other conflict Blocks
the card.

**Complete.** The PO answers the card in that turn and completes it:

```text
task complete --ref <card> --role po --kind decision|operation --body-file <file> [--request-id <id>]
```

The body must carry two non-empty level-2 sections: `## Decision` and `## How to verify` for a
decision, `## What was done` and `## How to verify` for an operation. Otherwise it is refused and
nothing is written. It is allowed only on an In progress card of that kind and only for role `po`.
It is one Card transition In progress → Done whose reason is the rendered completion record, written
as the PO's comment in the same transaction:

```markdown
[completion:decision]

## Decision

...

## How to verify

...
```

(`[completion:operation]` with `## What was done` for an operation.) The record is read back only from
a comment the PO wrote, the way the infra record is read only from the dispatcher's. The same request
id repeated with the same body is a replay that writes nothing; with another body it is
`request_conflict`. `task complete` does not pass the sprint guard: it is the PO executing its
sprint's card, not an override move. Run inside a PO turn it records that turn's session
(`UMMANU_PO_SESSION`) in the transition's data as `po_session`; a repeat keeps the recorded one. It
permits nothing; a delegated card's result return reads it ([PO delegation](#po-delegation)).

**Waiting.** After the submit the dispatcher checks the card once per tick, from the PO store and the
service's queue directory, not the service: `po_requests` names the turn the input became once the
service claimed it. The outcomes:

- the card left In progress (Done with its completion record): the dispatcher record is closed;
- the card carries the handover mark: it waits for the owner (below), whatever its turn did;
- the turn that took the input settled `completed`, `failed` or `interrupted` and the card is still In
  progress without the mark: the card goes to Blocked with `PO turn <session>/<seq> ended <state> without
  completing the card`;
- the input is in no turn and the service set it aside in `po-queue/refused/` (its session closed or
  gone, its id bound to something else): it will never run, so the card goes to Blocked with `the PO
  service set the card's input aside and will not run it: <the service's reason>` (`find_refused` of
  the submit id);
- otherwise it waits: the input may still be queued behind a seed or another turn (`po-card-queued`),
  or its turn is running (`po-card-turn-running`).

The PO's Done and the dispatcher's Blocked are card transitions of a card linked to the sprint, so each
wakes the observer ([Resume and observer wakes](#resume-and-observer-wakes)); the claim and the submit
do not.

#### Production rights

By default an operation touches no production; a sprint allows its operations the productions it names
at create ([`allowed_productions`](#the-sprints-po-session-and-productions)), and the ones its PO allows
later with `sprint allow-production` (below). The rule is evaluated in one place: the PO service's
handling of a dispatcher submit (`PoService.submit`), after the request id is reserved and the session
found open. Neither the dispatcher nor the web evaluates it. It refuses nothing and hands nothing over
(secretary-1769): every operation card becomes a normal PO turn, and the rule only decides what that
turn's input says. On a submit whose facts say `kind: operation`, `input: card`, the service queues the
input with a **rights section** (`## Production rights (the PO service)`) that the turn's prompt, and
the session's feed, carry after the dispatcher's text:

- `touches_production` is `none`, or is in the sprint's `allowed_productions` (read through the
  service's sprint port): the section gives the rights line `touches production <p>; sprint <ref> allows
  [<list>]` and says the sprint allows it. The PO runs the operation with no confirmation;
- otherwise the section gives the same rights line (the list comma-separated, `[]` when empty) and the
  instruction to decide under the owner's standing rule: production of ummanu is allowed by default,
  because it is the development server; any other production only as agreed at sprint planning. If the
  PO may allow it, it records the allowance with `python3 -P -m ummanu sprint allow-production --ref
  <sprint> --role po --project <p> --reason <text> --request-id <submit id>:allow-production` and runs
  the operation in the same turn. If it may not, it hands the card to the owner with `task handover
  --to owner` ([below](#handover-to-the-owner)), as for any card. The service journal says `<card>
  queued for the PO to decide: <rights line>`;
- facts that are missing or malformed (an operation with no `touches_production`, a dispatcher input
  with no facts at all) and a sprint that cannot be read are refused as `unavailable`, with nothing
  queued. The input is never executed; the dispatcher repeats it each tick (`po-service-unanswered`)
  and the card stays In progress.

The rights section is the service's, not the submitter's: the request id binds the dispatcher's text
and card facts only, so a repeat of the same submit id answers the input already queued (or its turn)
whatever the sprint allows by then. A sprint opened before `allowed_productions` existed reads `[]`, so
each of its operations goes to the PO turn above, never straight to the owner.

An operation cut outside every sprint (`sprint_ref` empty, [PO delegation](#po-delegation)) has no
sprint allowance to read or to record. Its rights section says so, `touches production <p>; no sprint`,
and tells the PO to decide it under the same standing rule: production of ummanu is allowed by
default; any other production only when the owner agreed to it, in the session or on the card; if not,
the PO hands the card to the owner. There is nothing for `sprint allow-production` to record, and the
service journal says `<card> queued for the PO to decide: touches production <p>; no sprint`. Its
`none` reads `touches production none: nothing to allow`.

A decision card gets no rights section. Neither does the owner's answer on a handed-over card (`input:
owner_answer`): the owner decided, so the follow-up is queued as today, its production line says the
owner decided, and the PO executes the card within the owner's answer. Inside a turn the PO touches
only the production the card names.

**`sprint allow-production`.**

```text
sprint allow-production --ref <sprint> --role po --project <p> --reason <text> [--request-id <id>]
```

Role `po` only; the actor is `--actor`, default `$BOARD_ACTOR` (`po` in a PO turn). Another role is
refused with `role_forbidden`, and `--role po` with actor `observer` with `role_masquerade` (the one
check every sprint write makes). `--project` must be a project of the instance registry and `--reason`
non-empty (`validation`); the sprint must be `open` (`closed`, exit status `3`, otherwise). It appends the
project to `allowed_productions` and writes one audit event `production_allowed` (payload `project` and
`reason`; the actor in the event), so `sprint show` and `status` carry it at once. It only adds: a
project already allowed is a no-op that writes nothing and answers `already_allowed`. The request id
makes a repeat the same write, and binds the sprint and project; the same id for another project is
`validation`. No other field of the sprint's contract is editable this way, and nothing removes a
production.

#### Handover to the owner

The PO hands a card to the owner when a person is needed: money, a key or access only the owner holds,
or a product decision that is the owner's. An architecture fork is the PO's own to decide.

```text
task handover --ref <card> --to owner (--reason <text> | --reason-file <file>) [--request-id <id>]
```

Role `po` only, on an In progress `decision` or `operation` card that carries no mark yet; any other
card, column or role is refused, as is an empty reason, and a card already handed over is refused with
`already_handed_over` and its mark. One write, in one transaction:

- **the mark**, three fields of the card's extension bag (`extensions.extra`; no column, no
  migration): `waiting_owner` (the moment, RFC 3339 UTC), `waiting_owner_reason` and `waiting_owner_by`
  (the PO actor). Only `task handover` writes them; a mark missing one of them, or with a moment that
  does not parse, reads as no mark;
- **a PO comment** `[handover:owner]` with the reason;
- **the audit record** of kind `handed_to_owner` (payload: `to`, `reason_sha256`, the card's `kind` and
  `sprint`, and the mark's moment).

After that transaction commits, the same call writes the owner event `card_handed_to_owner` (class
`needs_owner`, subject the card; [Owner events](#owner-events-and-the-bell)) under the dedup key
`card_handed_to_owner:<card>:<event id of the handover>`, so a repeat writes nothing new.

The card stays In progress. The request id makes it idempotent: a repeat answers the recorded handover
and writes nothing, and the same id with another card or reason is refused. `task show` and `task list`
carry the mark as a top-level `waiting_owner: {since, reason, by}`, and the card page shows a
`waiting for the owner` chip and the reason.

**The owner's answer.** The owner answers in one of two ways:

- a card comment, `task comment --ref <card> --role owner --body-file <file>`. `owner` is a comment role
  only: allowed on any card, always written with actor `owner`, and refused for every other verb;
- a message in the sprint's PO session on the `/po` page. The PO completes the card from there with
  `task complete`; there is no other machinery.

On a marked card the dispatcher reads the card's audit each tick. When an owner comment follows the
latest handover, it submits one follow-up input to the same PO session, `source: dispatcher`, carrying
the card ref, the handover reason, the owner's comments since the handover in board order, the event
id of the latest one (which a raise of an e2e budget names as `--authorized-by`) and the completion
command. Its request id is `dispatcher-po-owner-answer-<card>-<event id of that owner
comment>`, kept on the dispatcher record with the frozen text, so a repeat, an unanswered submit or a
rebuilt record never makes a second input for the same comment. It carries the card's facts with
`input: owner_answer`, which the service does not check against the sprint's productions. A later
owner comment makes one more follow-up, carrying every comment since the handover. A follow-up the service does not answer is
repeated next tick under the same id; one it refuses outright (its session closed, say) Blocks the card
with `the PO service refused the owner answer of this card: <reason>`, and one it set aside in
`po-queue/refused/` Blocks it with `the PO service set the owner's answer aside and will not run it:
<reason>`. The owner's comments are the only thing that re-submits: a settled turn on a marked card,
the first one or a follow-up, means wait (`po-card-waiting-owner`, then `po-card-owner-answered`), never
Blocked. A follow-up whose turn ended `failed` or `interrupted` with the card still marked waits as
`po-card-owner-answer-turn-ended`, with the reason `the owner's answer reached the PO, but its turn
failed` (or `was interrupted`); the `po_turn_failed` owner event tells the owner. The PO completes the
card with `task complete`, which takes the mark off in the same transaction as the Done; any other move
out of In progress takes it off too, and with it marks the card's `needs_owner` events read.

The dashboard's card comment form answers too: on a card carrying the mark it posts with role and actor
`owner` (the web front is behind the owner's password), so the dispatcher forwards it like a CLI owner
comment; on any other card it posts as `po`.

**The sprint reads `waiting`.** While the sprint's current card is an In progress `decision` or
`operation` card, `sprint status` (`work.waiting`) and the dashboard's sprint row read `state:
waiting`, with `card` pointing at it and the reason `<card> (<kind>) is handed to the owner: <reason>`
when it carries the mark, or `<card> (<kind>) is with the PO` otherwise. The Pipeline listing decides
it, before the dispatcher's record, since no head runs such a card; every other answer of the section
carries `card` as well (the sprint's current card, or null).
Every card of the sprint that waits on the PO, the owner or a run, current or not, is listed in
`work.waiting_on` ([What a sprint is doing](#what-a-sprint-is-doing)).

### Wait cards

A `wait` card (secretary-1790) waits for one external or board fact with no head and delivers its
outcome to the return addresses its creator named, exactly once. The dispatcher advances it once per
tick; there is no watcher process, background job or queue, and everything the wait knows is on the
card, so a restarted dispatcher (or PO service) continues where the last one stopped. It is a
no-candidate kind: no workspace, branch, pull request, CI, reviewer or broad check.

**Create.** The PO, or the observer for its own sprint (`--sprint` required for the observer), runs
`task create --type wait` with exactly one target, a deadline and at least one return address (none,
for the PO inside a PO turn). The dispatcher is the one other creator, of one wait only: the wait of a
code card's [e2e run](#the-e2e-stage), returning to `card:<that card>`. It creates no other kind
(`role_forbidden`), and no other role may name a `card:<ref>` address (`validation`); every other role
still cannot create a wait card:

| Flag | Meaning |
| --- | --- |
| `--wait-run <owner/repo> --wait-run-id <id>` or `--wait-run <run URL>` | a GitHub Actions run concluding (`https://github.com/<owner>/<repo>/actions/runs/<id>`) |
| `--wait-card <ref> --wait-states <state>[,<state>]` | another card reaching one of the named states |
| `--wait-until <UTC>` | a point in time, ISO-8601 with its zone; not after the deadline |
| `--wait-deadline <UTC>\|<duration>` | required: an absolute time with its zone, or a duration from creation (`90m`, `2h`, `1d12h`); it must lie in the future |
| `--wait-return <address>` | required (inside a PO turn, optional: see below), repeatable: `observer` (a card with `--sprint` only), `po-session:<id>` (a session the PO store holds and that is open), `dependents` |
| `--wait-transient-window <duration>` | how long consecutive transient source errors may last before `source_unreachable`; default `30m` |

The form, as the PO instructions quote it:

```text
python3 -P -m ummanu task create --role po --project <project> --type wait --title <title> --wait-run <run URL>|--wait-card <ref> --wait-states <state>[,<state>]|--wait-until <UTC> --wait-deadline <UTC>|<duration> [--wait-return observer|po-session:<id>|dependents]... [--wait-transient-window <duration>] [--sprint <sprint>]
```

`--wait-return` is required everywhere except inside a PO turn with `--role po`: there, a wait with no
`--wait-return` returns to the session of that turn (`po-session:<its origin session>`, which is not
looked up in the PO store: it is the session creating the card). The create's request identity keeps
the flags as given, so no address is recorded there. A wait card delivers only through its return
addresses: its origin never gets a second delivery ([PO delegation](#po-delegation)).

A missing or malformed target, two targets, a missing deadline or one already past, an unknown or
closed PO session and `observer` without a sprint are refused as `validation` (a PO store that cannot
answer the session question as `po_store_unavailable`) with nothing written. As for decision and
operation cards, a wait card refuses `--head`, `--review-head`, `--review required`, `--live-impact`,
`--seed-ref` and `--base-branch`; its review is `skipped`, a sprint's executor pins do not apply, and
every other kind refuses the `--wait-*` flags. It touches no repository, so no project reservation
admits or refuses it, at create or at claim, and it takes no claim capacity. The create's request id
binds the flags as given (`wait_request`), so a retry is a replay whatever the clock says.

The spec is one typed field of the card's extension bag, `extensions.extra.wait` (JSON text: target,
deadline, return addresses, `created_at`, `transient_window_seconds`), written once at create. Two more
fields sit beside it: `wait_state`, written only by the dispatcher, and `wait_cancel`, written only by
`task cancel`. No column; migration `0020_wait_card_kind` admits the kind in `task_type`'s CHECK.

**Run.** Claiming a Ready wait card creates no workspace, launches no head and runs no broad check or
Git preflight; the card moves to In progress, and every tick after that observes the target once. The
dispatcher keeps no record for it. A GitHub run is only read, `GET repos/<repo>/actions/runs/<id>`
through the gate's `gh api` (the CI gate's one read per tick); the wait never starts, reruns or
dispatches a workflow, recovery included. `wait_state` holds when the wait started (`since`), the last
observation and when it was first seen, the last error with the start of its run of consecutive
failures (`error_since`), the frozen result, and one delivery record per address. It is rewritten only
when one of those changes.

**Result.** The first terminal fact observed is frozen into `wait_state.result`, with its key (a digest
of the outcome and the fact), before anything is delivered, and it is never overwritten:

- `target_reached`: the run completed, whatever its conclusion; a run concluding `failure` is still
  `target_reached`, and the result carries the conclusion, `html_url` and timestamps. For a card, the
  state it reached; for a time, the time;
- `cancelled`: the card carries a cancel (below);
- `source_unreachable`: GitHub answered 404, 410, or 403 that is not a rate limit (no access); or the
  named card does not exist. Transient errors (no answer, 5xx, 429, a 403 rate limit, a card read that
  failed) do not end the wait: each is recorded as the last error and retried next tick, and once
  consecutive errors have lasted the transient window, the wait ends `source_unreachable`;
- `deadline_passed`: the deadline passed with no result.

The deadline is the cutoff, enforced in one place, the function that freezes a result
(`_settle` in `dispatch/wait_cards.py`); every observation (the cancel, the run, the card, the time,
a definitive answer and an expired transient window) reaches it as a candidate. Before the deadline the
first candidate is frozen as it is. At or past the deadline the only outcome frozen is
`deadline_passed`, with one exception: a `target_reached` whose own source time puts it at or before
the deadline. That time is the completed run's `updated_at` (a completed run is not updated again
unless it is rerun, which makes it not completed), the `occurred_at` of the card's latest audited
transition into the state it reached, or the target time itself. A cancel, a 404/410/403 or a transient
window that ran out, observed after the deadline, is `deadline_passed`, never its own outcome; so is a
run that completed after the deadline however long the dispatcher was away. The result then records
what was seen after the deadline (`seen_after_deadline`).

**Delivery.** Every delivery is keyed by (card, address, frozen result key) and recorded in
`wait_state.deliveries` only after the receiving side accepted it. A crash in between repeats it under
the same key, and the receiving side makes the repeat a no-op:

- `po-session:<id>`: one input to that session through `PoService.submit`, `source: dispatcher`, request
  id `dispatcher-wait-po-<card>-po-session-<id>-<key>`, carrying the card ref, the target, the outcome,
  the result and the evidence link, with the facts `{card_ref, kind: wait, touches_production: null,
  sprint_ref, input: wait_outcome}` (a wait may belong to no sprint, and it gets no rights section). A
  service that does not answer, or refuses, postpones the delivery to the next tick
  (`wait-delivery-postponed`, degraded); it is never lost or duplicated, and the only delivery status is
  `accepted`. A session that is **closed or missing** never swallows the result: the dispatcher opens one
  successor and delivers there, under the same delivery request id (a submit to a closed session
  reserves nothing). The sprint's own session (the wait card is in a sprint whose recorded, open
  `po_session` is the one addressed) is succeeded through `sprint_session`, which opens the successor,
  seeds it and records it on the sprint. Any other session is succeeded through `create_session` with
  the closed session's CLI, model and effort; an effort no longer offered gives way to the first one
  offered for that CLI, and a missing session takes the new-session form's defaults, the rule
  `sprint_session` applies (`po.models.successor_choice`). The route is written to
  `wait_state.successors` before the call and the successor's id right after it, and the call's request
  id is `dispatcher-wait-po-successor-<card>-<closed session>-<key>`, so a repeat after a crash takes the
  same route and opens no second successor. A successor that is itself closed before it takes the
  input gets its own successor the same way. The delivery record names the session that took the
  result (`session`), and the completion comment says `taken by PO session <id>`;
- `dependents`: every card whose `blocked_by` names the wait card gets one dispatcher comment
  `[wait:<outcome>] <wait card>` with the result (request id `dispatcher-wait-dependent-comment-<wait
  card>-<card>-<key>`). On `target_reached` that is all: the card becomes claimable. On any other
  outcome a Ready dependent is also moved to Blocked with the outcome as its reason. Until the wait
  delivers, the claim pass leaves a Ready card whose `blocked_by` names a Ready or In progress wait card
  in Ready (`blocked by pending wait <ref>`). Blockers of any other kind behave as before: the claim's
  own predecessor rule;
- `card:<ref>` (the dispatcher's e2e wait only): one dispatcher comment `[wait:<outcome>] <wait card>`
  on that card (request id `dispatcher-wait-card-comment-<wait card>-card-<ref>-<key>`). The card's own
  e2e stage reads the frozen result off the wait card and acts on it; the comment is what the card's
  history shows. A card that no longer exists takes nothing and the delivery is given up; any other
  board error postpones it. `dependents` would not reach that card: it is in Validate or Assessment,
  not Ready, and names no `blocked_by`;
- `observer`: the wait card's terminal move, last, after every other address: Done for
  `target_reached`, Blocked for every other outcome, with the result as the move's comment
  (`[wait:<outcome>]`, the target, result, evidence and each address's delivery). The observer's wake on
  a sprint card's Done or Blocked is the delivery. No outcome leaves the card In progress with nothing
  happening, and only `target_reached` reaches Done.

The dispatcher takes two edges for these cards only, and `task move` refuses them anywhere else: In
progress → Done for a wait card whose frozen result is `target_reached`, and Ready → Blocked for a card
whose `blocked_by` names a wait card. Both moves, like a wait card's Blocked, carry `wait_outcome` in
their transition data: the outcome of a wait, not a pipeline restart, so the sprint budget does not
charge them, and they do not close the sprint or the project to claims for the tick.

**Cancel.**

```text
task cancel --ref <wait card> --role po|observer --reason <text>|--reason-file <file> [--request-id <id>]
```

The PO, or the observer of the card's own sprint, cancels a Ready or In progress wait card whose result
is not frozen yet; the reason must be non-empty. One write: the `wait_cancel` field (when, who, the
role, the reason) and a `[wait:cancel]` comment, in the transaction of one `wait_cancelled` audit record;
idempotent under the request id. The card does not move there: the dispatcher's next tick freezes
`cancelled` and delivers it like any other outcome. A second cancel is `already_cancelled`, a cancel of
a wait with a frozen result `already_settled` (that result stands).

**`task show`.** A wait card carries one block, `wait`, in `task show` and `task list`: `state`, the
`target` (with `link` for a run), `waiting_since`, `deadline`, `return_to`, `last_observation` (`at`,
`text`), `last_error` (`at`, `since`, `text`), the `result`, `delivery` (`pending`/`complete`), per-address
`deliveries`, `po_sessions` (per PO address: the `addressed` session and the one that took the result,
`received_by`, which is a successor when the addressed one was closed or missing, or `null` until
taken) and `cancel`. `state` is `waiting` (nothing frozen), `result_ready` (`target_reached`
frozen, delivery not complete), `delivered` (every address accepted), or the outcome itself:
`cancelled`, `deadline_passed`, `source_unreachable`.

A wait card moved back to Ready by hand is claimed again, keeps its frozen result and its delivery
records, and ends the same way at once: a wait is not restarted; cut a new one.

### PO delegation

A PO turn is a conversation with the owner. Work that would take longer than about a minute, or read
more than about ten files, becomes a card the PO cuts (the rule its instructions state,
`packaging/po-workspace/AGENTS.md`), and the card's result comes back to the session that cut it as a
new input (secretary-1792). Nothing waits in the turn, and a background job is not a way to wait: a
long wait is a [wait card](#wait-cards).

**Origin.** `task create --role po` inside a PO turn records the card's origin: the turn's session
(`UMMANU_PO_SESSION`) and the request id of the input the turn answers (`UMMANU_PO_REQUEST`), both
set by the PO service in every turn. There is no flag for it: the CLI reads the two variables for `--role
po` only, any other role's create ignores them, and `TaskWriter.create(origin=...)` refuses an origin
for any role but `po` (`validation`), with two dispatcher exceptions that carry an existing card's origin
onto a card cut for it: the decision a spent e2e cap needs, and the hotfix of a red after-merge e2e run
([After merge](#after-merge)). Outside a PO turn no origin is recorded. The origin is one typed
field of the extension bag, `extensions.extra.po_origin` (JSON `{session, request}`), written once by
create and part of its request identity (`po_origin` in the create payload: the same turn's retry
replays, another turn's is a conflict); nothing writes it afterwards. `task show` and `task list` carry
an `origin` block: `po_session`, `request_id`, `current_session` (the session a result goes to now: the
origin, or the successor that replaced it), `executor` (below) and `returns`, the card's outbox rows
(below), oldest first (`event_id`, `state`, `created_at`, `delivered_at`, `status`, `notice`, `session`,
`request_id`). The dispatcher's side is a second bag field, `po_return` (`executor`, `successors`),
written only by the dispatcher (`TaskWriter.record_po_return`).

**The outbox.** What a delegated card owes is written down when it becomes owed. The board store keeps
one table, `origin_returns` (revision `0022_origin_returns`, `board/origin_outbox.py`): one row per
transition into Done or Blocked of a card that carries an origin and is not a wait card, with the card
ref, the transition's event id (unique) and audit request id, the column it entered, when, and its
delivery (`delivered_at`, `status` `delivered` or `skipped`, the notice result, the session that took
it, the input's request id). The row is written by `SqlTaskAudit.append`, the one place a committed
audit record is written, in the same transaction as the record, and a card transition's move and its
event commit in one transaction (`TaskWriter._transition_card`): the move, its event and its row stand
or fall together, and no writer can commit one without the other. Every writer of a transition into
Done or Blocked reaches it: `task move` of any role (the dispatcher's edges, a release's Done, the wait
edges, which owe nothing since a wait card writes no row), `task complete`, and a pending transition
finished by repair. Archive, reopen and any later move write no row and touch none. The
normalized-board restore replays history through `append(..., restoring=True)` and owes nothing for it:
those returns were owed, and made, when the transitions first committed (a PostgreSQL-level recovery
restores the table itself).

**Return of the result.** Each tick, after the claim pass, the dispatcher reads the undelivered
outbox rows in id order, one indexed query, and scans no card (`dispatch/origin_returns.py`).
`pending_returns` is that query, and it is the only thing that decides what is returned: nothing reads
a card's present column or picks "the latest" event. A card that was Done or Blocked and was since
reopened, moved on or archived before any pass still has its row. Rows are made oldest first; the
first row of a card that does not complete stops that card for the tick, and its later rows wait for
the next; no row is skipped because a later one exists. For each row, the dispatcher reads the card
(`task show` finds an archived card) and its audit, finds the row's event by its event id, and submits
one input to the origin session through `PoService.submit`, `source: dispatcher`, request id
`dispatcher-origin-return-<card>-<event id>`, with the facts `{card_ref, kind: <the card's kind>,
touches_production: null, sprint_ref, input: delegated_result}` (any kind, in a sprint or not; no rights
section). The input carries the card ref, kind and title, the terminal state and when, the sprint, and
the result: for Done the completion record when the Done move carries one (`[completion:...]`, with the
decision the move names), else the worker's done report of that round, else the Done move's reason; for
Blocked the Blocked move's reason and a classification (the round's `report:blocked` classification,
else `board: <blocked_reason>` of the move's terminal taxonomy, else `unclassified`), with the worker's
blocked report when it differs. The links are every GitHub pull request and Actions run URL in the
card's audit up to that move, and the merge a release recorded. Everything about the state is the
event's: the column it entered and when, and its result; the card's present column is never read, so
the same row always renders the same text.

The delivery is complete only when both its halves are, in this order: (1) the PO submit, idempotent by
its request id; (2) the `delegated_card_settled` notice (below), written through the bell's strict
writer `owner_events.record_strict`, which answers `written`, `already_present` (under the dedup key)
or `failed` (the store raised or did not answer) instead of swallowing the failure as `record` does for
every other producer; (3) the row marked delivered, only after (1) and (2) were accepted. The row is the
only delivery record. A notice that failed marks nothing (`origin-return-notice-failed`, degraded), and
the next tick repeats both steps under the same keys: the submit is the earlier input to the service (a
`request_conflict` on an id the service holds for the session is taken as it), the notice a no-op on its
dedup key. A crash between any two steps is repaired the same way: no second input, no second notice.
An installation with no board store configured at all has no bell: the strict writer answers
`not_applicable` (logged), which does not hold the delivery, and the row says so (`notice`). A marked
row is never selected again.

The PO delivery is the one the wait cards use (`dispatch/po_delivery.py`). A service that does not
answer, or refuses, postpones it to the next tick (`origin-return-postponed`, degraded). A closed or
missing origin session never loses the result: the dispatcher opens one successor, through
`sprint_session` when it is the recorded session of the card's open sprint, through `create_session`
with its CLI, model and effort otherwise (`po.models.successor_choice`), under request id
`dispatcher-origin-successor-<card>-<closed session>`, recorded in `po_return.successors` (route before
the call, session after it). The successor is the card's origin line from then on: later results, and
an out-of-sprint card's execution, go to it, and a successor closed in turn gets its own.

**Exceptions.**

- A `wait` card delivers only through its own return addresses (above); its transitions write no
  outbox row, so its origin session never gets the outcome twice. Inside a PO turn `--wait-return` may be omitted and
  then names the origin session.
- A Done is skipped only on proof, carried by that Done's own transition, that the origin's line already
  has the result; it is decided per event.
  `task complete` records the PO session whose turn ran it (`UMMANU_PO_SESSION`, present in every PO
  turn; no flag) in the completion transition's data as `po_session`; `task handover` records it in the
  handover record's payload the same way. Neither restricts who may run the command. When the Done's
  recorded `po_session` is the origin session or one of its recorded successors (`po_return.successors`),
  the Done's row is marked `skipped`, with no input and no notice, so it is never pending again. Anything else is delivered normally,
  with input and notice: a completion that ran outside a PO turn (no session recorded), one by another
  session, any Done not made by `task complete`, and every Blocked. The session the dispatcher handed the
  card to (`po_return.executor`) proves nothing and is not asked.

**Out-of-sprint decision and operation cards.** Created by the PO inside a PO turn with no `--sprint`,
such a card is accepted; the dispatcher claims it and submits it to its origin line (see
[Decision and operation cards](#decision-and-operation-cards)), under the same request ids, the same
waiting and settle rules, the handover and the owner's answer, and a turn that settles without
completing it Blocks it. Production rights have no sprint allowance to read; the PO service's rights
section says what applies ([Production rights](#production-rights)). With no sprint and no origin it is
refused at create; a legacy one is Blocked at claim.

### Owner events and the bell

What needs the owner, and what the owner should know, is one board entity: the table `owner_events`
(revision `0018_owner_events`; `0021_delegated_card_settled`, `0023_sprint_e2e_budget` and `0024_e2e_after_merge_kind` add a kind), written and read only through
`ummanu.board.owner_events`. A row is
`id`, `kind`, `class`, `subject_ref` (a card, sprint or issue ref, `po-session:<id>`, or null), `text`,
`created_at`, `read_at` (null while unread) and `dedup_key` (unique).

**Kinds and classes.** The class is derived from the kind (`owner_events.KIND_CLASS`); the database
holds the kind vocabulary, the class vocabulary and that rule as CHECKs.

| kind | class | written by, at | subject | dedup key |
| --- | --- | --- | --- | --- |
| `card_handed_to_owner` | `needs_owner` | `TaskWriter.handover` (`task handover`, inside a PO turn), after the handover commits | the card | `card_handed_to_owner:<card>:<handover event id>` |
| `steward_needs_human` | `needs_owner` | `TaskWriter.move` of a steward report card to Blocked by role `steward` whose reason carries a non-empty "Needs a human" section | the report card | `steward_needs_human:<card>:<move event id>` |
| `e2e_budget_spent` | `needs_owner` | the dispatcher's e2e stage ([The e2e stage](#the-e2e-stage)), before it Blocks a code card outside every sprint, with no PO origin, whose e2e cap is spent; its text is the Blocked reason. Also the after-merge stage ([After merge](#after-merge)), when a covered card outside every open sprint, with no PO origin, has its cap spent and is declined | the card | `e2e_budget_spent:<card>:<cap>` |
| `e2e_after_merge` | `needs_owner` | the dispatcher's after-merge e2e ([After merge](#after-merge)): a run that ended with no verdict (another conclusion, a wait outcome other than `target_reached`, refused, unidentified, ambiguous, run on another SHA); or a red run whose hotfix card no open sprint and no PO origin owns, after that card is Blocked | the run's carrier (the newest covered card), or the hotfix card | `e2e_after_merge:<dispatch id>`, `e2e_after_merge:hotfix:<dispatch id>` |
| `sprint_closed` | `notice` | `SprintWriter.close`, after the close commits | the sprint | `sprint_closed:<sprint>:<close event id>` |
| `sprint_stopped` | `notice` | `SprintWriter` in the budget charge that reached the hard limit (the dispatcher's budget pass), in its transaction | the sprint | `sprint_stopped:<sprint>:<charge request id>` |
| `budget_signal` | `notice` | `SprintWriter.record_budget` once the sprint's budget reaches its signal threshold | the sprint | `budget_signal:<sprint>` |
| `observer_dead` | `notice` | the dispatcher's observer reconcile: a head positively dead at the start of the tick that the tick did not relaunch (backoff, drain, a failed bring-up) | the sprint | `observer_dead:<sprint>:<launch count>` |
| `head_dead` | `notice` | the dispatcher's wait watchdog: a worker or reviewer head dead or stalled again after its one respawn, the card Blocked for the operator; or a worker respawn that failed | the card | `head_dead:<blocking request id>` |
| `po_turn_failed` | `notice` | the PO service, when its runner settles a turn `failed` (`PoRunner._finish`); a stop by the owner is `interrupted` and writes nothing | the card when a dispatcher input started the turn, else `po-session:<id>` | `po_turn_failed:<session>:<seq>` |
| `provider_red` | `notice` | `ummanu doctor` (not `--dry-run`): a resource probe `unauthenticated` (expired key or missing login), `exhausted`, `unavailable`, `timed_out` or `probe_broken` | none | `provider_red:<resource>:<state>:<UTC day>` |
| `delegated_card_settled` | `notice` | the dispatcher's result return ([PO delegation](#po-delegation)), after the origin session (or its successor) took a delegated card's result, through `record_strict`: a failed write is repeated next tick and the return is not recorded until it lands; its text names the card, its terminal state and the origin session, and the successor that took it | the card | `delegated_card_settled:<card>:<transition event id>` |

**The writer never fails its caller.** Every producer calls `owner_events.record(kind, subject_ref, text,
dedup_key, to=...)` once, at the place its fact is decided. It is idempotent on the dedup key (`ON
CONFLICT DO NOTHING`) and swallows and logs every failure: a store that does not answer, a board that
owes migrations (merged code runs before the upgrade applies them; refused as `schema_owed`, see
[Board schema gate](#board-schema-gate)), no board store at all. A producer's
own write never depends on it.

**Stay-unread.** A `needs_owner` event whose subject card carries the `waiting_owner` mark stays unread:
a click on it is refused and "mark all read" takes notices only. Its `read_at` is set when the mark
clears, by `TaskWriter._reset_transition_metadata` in the transition's own transaction (a savepoint on
PostgreSQL): `task complete`, or any other move out of In progress. A `needs_owner` event whose card
carries no mark (the steward's report) is read by a click.

**The web.** The header of every page shows the bell, the unread count read from the board for that
render (`?` with the reason when the board cannot count, for instance while it owes migrations); it links to the
unread view. `GET /owner-events` lists the unread events, open `needs_owner` events pinned first, then
newest first, each with its class badge and a link to its subject; `?all=1` lists every event, unread
rows highlighted. `?unread=1` from an older link, or any other value, is the unread default. A
notice's "Mark read" posts `/owner-events/{id}/read`; "Mark all notices read" posts
`/owner-events/read-all`; both return to the view they were pressed from. A board without the table lists no events, with the source `unavailable`, and
refuses the two writes (503). From a terminal: `ummanu owner-events list`
([Operations](OPERATIONS.md#owner-events)).

### Cards outside a sprint

The PO may create a card of kind `code`, `research` or `infra` with no `--sprint`, in Ready, on
any project, including one an open sprint reserves, and may move and edit it without
`--sprint-override` (see [The sprint guard](#the-sprint-guard)). Other roles keep their rules: the
observer creates only cards of its own sprint, the steward creates its report card In progress, and
worker, reviewer, retro and steward otherwise create only proposals.

Whether such a card runs is decided once, at admission, before the claim. The dispatcher asks the same
reserved-project index the write guard reads (seeded from the sprints board when it was never written,
each sprint it names re-read live):

- a card linked to a sprint is that sprint's work and is not asked about;
- `research` and `infra` are admitted on any project, reserved or not;
- `code` on a project no open sprint reserves is admitted;
- `code` on a project an open sprint reserves is refused. It is blocked through the pre-claim refusal
  of [Bring-up outcomes](#bring-up-outcomes), like `contract-preflight-blocked`: no workspace, head or
  round exists, the transition carries action token `sprint-reservation-blocked` (class
  `infrastructure`, so no budget is charged), and the tick outcome has step `sprint-reservation-refused`
  and a `sprint_reservation` object (`refusal`, `project`, `sprints`, `detail`). The Blocked reason
  shown by `task show` names `refusal=sprint_reserved`, the project and the reserving sprint, and says
  the card may run inside that sprint or after it closes. The refusal is not retried; a card the PO
  moves back to Ready is asked again under a fresh attempt, and after the sprint closes it is admitted;
- `code` whose project's reservations cannot be verified (the index cannot be seeded or a sprint it
  names cannot be read) is refused as the claim-skip `sprint-reservation-unverifiable`, refusal
  `sprint_reservation_unverifiable`. Nothing is written: the Blocked move would meet the same unverifiable
  index at the write guard. The card stays in Ready and is asked again on the next tick. An unverifiable
  index never refuses `research` or `infra`.

A card already in progress when a sprint opens is not moved or blocked by this rule.

## Codex provider-internal fan-out policy

Codex fan-out is a best-effort operational preference, not a lifecycle or security boundary. Every
worker, reviewer and observer launch uses the strongest validated low-fan-out CLI configuration and
an explicit instruction to do its turn in the current head without spawning or delegating to
children. A rare provider-internal child is acceptable. The launch policy is practical suppression,
not capability isolation.

`ummanu.runtime.codex_preflight` is the one pre-launch preparation boundary. Its v1 record
keeps `schema_absent`, `schema_unknown`, `allowed`, `unknown` and `violation` as diagnostics; none of
these fan-out states permits or refuses a launch. Workspace trust is the hard pre-launch requirement.
Launches proceed with `schema_absent`, an unbound structured journal source where available, and the
low-fan-out launch configuration.

Provider-edge collection is bound to the same run. It appends one of `collaboration_call`,
`child_thread_edge`, `unknown_thread_edge` or `unparseable_provider_event`, with parent and child
thread identities when present, tool name when known, SHA-256 raw-event digest, source
sequence/location and capture time. A collaboration call or non-empty child edge is a violation. An
unknown tool or relation, missing expected parent, malformed event or failed event write is unknown.
These states are telemetry only: they never stop or replace the HeadRun, block a card or sprint,
refuse prompt delivery, or affect continuation liveness. Telemetry loss is non-fatal. An observed
edge is recorded and the run continues.

The Codex source is its structured session-event JSONL, not a terminal read and not the workspace-level
session liveness lookup. The pre-launch attestation stores the v1 source root and the set of journal
paths that existed before the head started. The collector reads the journal's `session_meta` and
`event_msg` envelopes, never terminal text. The TUI collaboration shape is
`event_msg.payload.item.type = CollabAgentToolCall`, with `tool`, `sender_thread_id` and
`receiver_thread_ids`; the `collab_tool_call` shape is also normalized. An unfamiliar
collaboration-shaped item is `unknown`, never ordinary output.

Before the first prompt, exactly one newly created journal for that workspace must supply one
session identity. An explicit parent `thread.started` is preferred; a journal that omits it uses the
selected `session_meta` id as parent/root identity. That path and identity, durable first/root/last
record anchors and a digest of the initially observed range are written onto the same `HeadRun` with
a zero cursor anchored to the first raw record. The scanner classifies the complete selected range
from that first record through the root and every already-present tail line before delivery;
pre-root records are not exempt. Ordinary records may durably advance the cursor. A malformed,
collaboration, child-edge, unknown-relation or cursor-write failure becomes typed diagnostic evidence
where writable and never gates a prompt. Recovery reopens the same path where available and verifies
its complete initial range, session id, workspace, parent identity and prior cursor before reading a
later line. Missing, unreadable, changed or ambiguous source evidence is non-fatal `unknown`.

### Post-delivery HeadRun handoff

There is one authoritative `HeadRun` after a launch delivery. Writer order: construct and validate the
run; persist its handleless preflight identity in the role launch intent; start the supervised head;
persist its handle (the supervisor socket) and leaf; bind and persist the Codex source when applicable;
capture that post-delivery run; then write routing, role state and clear the intent. `head_ops.spawn`
returns the captured run, and worker, reviewer and observer launchers, intent confirmation and adoption
consume that value, not a pre-delivery copy.

The provider callback owns source facts. A later launcher or lifecycle writer may add only the head
address it proved and its own forward lifecycle evidence. It cannot remove a bound source, move a
cursor backwards, replace a bound session/range, or replace run id, spec, workspace, task, role or
pid identity. A conflicting, stale, malformed or foreign candidate is an identity fence: it is not
adopted, resumed, signalled, stopped, replaced or attributed. Worker, reviewer and observer recovery
use the same merge. Source binding never gives fan-out telemetry lifecycle authority.

Observer event delivery: when a retained Codex observer HeadRun carries its v1 source descriptor, the
dispatcher persists a versioned `wake_liveness` episode before it interprets the head's readiness
(busy while its supervisor has a turn open). The episode
names the exact run id and HeadRun fingerprint, source fingerprint and opaque cursor, first
observation, last admitted progress, no-progress rung and terminal outcome. A new admitted cursor
keeps the same head and event batch and resets only that batch's no-progress ladder. Missing,
malformed, incomplete and foreign source evidence is typed unavailable or identity-mismatch and
cannot refresh, reset or rebind an episode. A bound unavailable episode keeps its binding and
observation across dispatcher reload; without an admitted baseline a later cursor cannot create one
for the same batch. This is provider-progress liveness only; fan-out events have no stop, delivery,
replacement, cleanup or blocking authority. At an identity-fenced observer replacement the old
episode is terminalized and retained as audit evidence; the replacement launch intent opens a fresh
episode bound only to the new HeadRun, carrying the unchanged delivery id and event high-water mark.

## Receipt names

The protocol names exactly two receipts:

- A **worker-local broad receipt**, owned by the worker. It attests one local broad suite's result for
  the current content, stays in that worker's workspace and never travels downstream.
- A **dispatcher-owned exact-SHA gate receipt**, owned by the dispatcher. It attests completed
  terminal gate checks for one exact SHA and lifecycle stage, and travels with the active card to
  review, Assessment and the release audit.

### Dispatcher-owned exact-SHA gate receipt

Every real mechanical gate result produces a receipt bound to one checkout with `validated_sha`,
`base_sha`, `gate_mode`, terminal `required_checks` (name, conclusion, URL), `completed_at` and
`command_or_check_set_digest` (local gate: the configured command's digest; GitHub: a stable
required-check-set identity, not a workflow or run digest). Both object IDs are full 40-character
SHA-1 or 64-character SHA-256; abbreviations are not evidence. A local gate captures HEAD before and
after its command and fails closed if the command moves it. Only `local` and `github` carry
receipts; `none` and noop are valid only without one; an unknown mode is never accepted.

The dispatcher persists the receipt with the active card, renders it into the reviewer's task
document, replaces it with the fresh post-review receipt in the Assessment delivery, and writes the
fresh final receipt into the release audit after the mandatory exact-SHA pre-merge re-check.

The review remains valid across a gate refresh-merge only when the reviewed commit is an ancestor of
HEAD, every intervening commit outside `origin/<base>` is a merge commit, and every path changed
from `merge-base(reviewed commit, origin/<base>)` to the reviewed commit is byte-for-byte unchanged
at HEAD. The release audit records the reviewed SHA, HEAD, base SHA, reviewed path count and that
the reviewed paths are unchanged. An unreadable git answer or any failed condition keeps the drift
refusal. The existing instance publication recovery remains a separate exception.

The reviewer document is written outside the checkout, under the installation-private run artifacts; the
head receives only a bounded pointer. `TASK.md` is a generated, git-ignored workspace handoff packet.
Neither is repository documentation or a candidate change. A receipt does not permit skipping the
pre-merge check or independent review.

## Seed and integration base

A card names at most two git refs:

- **seed** — `workspace.seed_ref`, a ref name or exact object id: where its checkout starts;
- **integration base** — `workspace.base_branch`: the PR base, the range candidate history is read
  over, the receipt's `base_sha`, and the branch the merge writes to.

A card with no seed starts from its integration base; a card with neither starts from and lands on
the project's default branch.

A successor inherits its predecessor's content, never its branch as a target:

    ummanu task create --project <p> --type code --title '<t>' \
      --seed-ref <predecessor candidate sha> --supersedes <predecessor card ref>

A seed without `--supersedes` is refused, and `--supersedes` without a seed is refused. A seed may be
a branch or an exact object id; an object id is fetched with the whole remote and must then be
present.

An integration base override must be a branch the project declares it integrates into: its binding's
`default_branch` or one of the optional `integration_bases`. It is refused, never normalised, in two
places:

- **at admission**, by the board: a per-card `pipeline/*` branch (refused by name), an object id
  (refused as "that is a seed"), or a value that is not a well-formed git ref;
- **at run time**, by the dispatcher: a well-formed branch outside the declared set, refused with the
  set listed (the board does not read bindings).

Cards already carrying an invalid base (admitted earlier or reproduced by restore) are refused the
first time a tick reads them, as a task-class bring-up outcome with cause `base_branch_contract` —
once, and never as a retried infrastructure failure.

## The pull request a GitHub gate opens

A `github` gate opens the pull request the `pull_request` workflow needs. Title and body are built
deterministically from the board; no model is involved. The title is `<ref>: <card title>` (it lands
in the merge commit). The body names the card and the target branch, quotes the card's statement and
carries the worker's `report:done` account; each source is bounded, redacted like other board
excerpts, and omitted when not yet present. Every later gate run, and the one before merge, brings
an already-open PR up to the best description it can build.

On every tick an open PR is checked against the card's integration base and retargeted when it
points elsewhere. The candidate, card state and worker are untouched. Because `pull_request`
workflows subscribe to `opened`, `synchronize` and `reopened` and a base change is none of those, the
PR is closed and reopened on the same head commit. A backend that refuses either half is a
determinate gate failure naming that half. Retargeting is not bounded by the authorship record below;
its scope is: only the base, only for a head in the dispatcher's `pipeline/<ref>` namespace, only
when the base is not the card's integration base, and the PR is left open. Title and body are never
rewritten by this path.

Rewriting a description is bounded by the dispatcher's own record, never by PR text. When the gate
opens or edits a PR and the backend accepts, it records on the card's dispatcher record which PR it
wrote and a digest of the exact title and body sent. A later tick may rewrite that PR only while that
record exists, names that PR, and still matches what GitHub returns. Everything else is left alone:
a PR a person opened (including one with an empty body), one edited after the gate wrote it, one
opened before the record existed, and any PR whose card lost its record to a restore, reinstallation
or re-adoption. Text that already matches the record is not re-sent. Anyone who can edit the
dispatcher's production state can claim any PR; repository write access does not confer that.

The description is not a condition on the code: a refused update leaves the PR and the gate verdict
unchanged. A PR that cannot be opened at all is a gate failure, because the project's CI never runs.

## Publishing the candidate branch

The gate publishes the worker's branch itself. A worker held between rounds rebases, so its branch is
routinely not a fast-forward of the remote.

The push carries a lease on the object id this dispatcher last published to that branch, recorded on
the card's dispatcher record when a publication succeeds (not a read taken just before the push). A
branch never published seeds the lease from a read of the remote, and the push still fences that
read: a remote that moves in between is refused.

A lease refusal is its own red gate class, `publication`, on the gate result and the record. Nothing
was published, no check ran, and the card carries both object ids (expected and observed). It is not
`infrastructure` and is not rerun by the dispatcher. A lease refusal is git's push report from the
server, so it is never mistaken for a silent backend, and a transport failure is never mistaken for a
refusal.

A remote already contained in the candidate's history is re-leased once instead of refused (a lost
record write, or a human force-push of this head). The re-lease is fenced on the value just observed
and happens at most once per gate run.

Repeated refusals are ended by the ordinary stale-done bound: the refusal returns the card to its
worker, an unchanged done report on the same rejected SHA is bounced once, and the next one moves the
card to Blocked. The gate never force-pushes past a refusal.

## A rollup nothing can fill

When the check rollup is empty, the gate reads the candidate's `.github/workflows` and asks whether
any declares a pull-request trigger admitting this base, honouring `branches`, `branches-ignore` and
GitHub's glob (`*` stops at `/`). Only a definite negative changes the verdict: workflow files exist,
every one parses, and none admits the base. Then the verdict is red, class `topology`, reason
`ci-trigger-impossible`, naming the base and the files read. A missing directory, an unparsable file,
or any admitting workflow leaves the verdict ordinary pending.

A `topology` red does not go back to the worker: the card goes to Blocked with the cause, for a
person to repair the integration base or the project's triggers. It is distinct from `publication`
and from `infrastructure` (the only class the dispatcher reruns itself).

## Candidate history

Before a gate publishes or validates anything, the dispatcher reads the candidate's commit messages
over `base..HEAD` and rejects forbidden AI attribution: a `Co-Authored-By:` trailer belonging to a
coding agent. The check is deterministic, covers the common agents whatever runtime wrote the commit,
and leaves human co-authorship alone. It runs in every validation mode, `none` included. A violation
is a red gate naming the commits, with the local repair (`git commit --amend` or `git rebase -i` in
the worker's checkout, then report done again). Nothing is rewritten or force-pushed for the worker.
Every worker launch packet carries the same instruction, and the reviewer re-reads commit messages.

Commit messages are untrusted input. Object ids are listed first from `%H`; a listing that is not
object ids is refused; each message is then read on its own. An addressed co-author is rejected only
when its complete normalized name/address pair is in the registered-agent list; a vendor domain or
ambiguous local part alone is not evidence. A trailer with no address is compared against the
agents' exact full names. Anything unreadable — missing workspace, unresolvable base, unreadable
message — fails closed and stops the card.

### Gate transport failures

A gate that could not reach its backend gave no verdict. A timeout, TLS or DNS failure, dropped
connection or backend-served 5xx leaves the card where it is — no board move, no head stopped, no
verdict or decision spent — and the question is asked again next tick. Each retry is one
`gate-transport-retry` action carrying the attempt number and error. Retries are bounded by
`UMMANU_GATE_TRANSPORT_MAX_ATTEMPTS` (default 5) and count consecutive silence only; any answer
(green, red, pending) resets the budget. When spent, the card moves to Blocked with a reason naming
the transport and last error. This applies to the pre-review gate, the pre-merge re-check and the
release re-check of a parked decision. An answer that did arrive decides as usual.

"No answer" is decided where the question is asked. Every remote gate call — base fetch, remote
branch read, branch publish, open-PR probe, PR create and retarget, repository name, check rollup,
failed-job log — goes through one helper, and only that helper raises the transport failure. A local
validation command that hangs past its ceiling is a determinate answer and blocks the card
immediately. A probe never turns silence into a fact (an unanswered open-PR probe is not "no PR").
Inside the helper, an answer is recognised positively: an HTTP status the tool quotes (except a 5xx),
a GraphQL error or parsed response body, or git's push report from the remote. Anything else is
silence and the card waits.

### Review infrastructure retries

A reviewer that cannot be started (its head will not come up, reviewer resource unavailable, launch
intent unwritable) is a review-stage failure, not a verdict. The card keeps its green gate receipt,
candidate SHA, report round, request ids and suspended worker session, and the next tick relaunches
the reviewer against the same evidence: no move through Ready, no worker launch, no gate or broad
re-run, no regenerated candidate, no budget event. Each attempt is one `review-infrastructure-retry`
action naming the held candidate. Retries are bounded by `UMMANU_REVIEW_INFRA_RETRY_ATTEMPTS`
(default 10), consecutive failures only. At the ceiling the card moves to Blocked with a reason naming
the infrastructure, the untouched receipt and the candidate SHA. An inventory that will not answer
cannot prove whether a reviewer is live, so it keeps launch ambiguity and retries the inventory
without launching another head or consuming the headless-failure ceiling. Those retries and the
resulting block use the [bring-up](#bring-up-outcomes) vocabulary: the ceiling is spent before the
outcome is written and yields an infrastructure outcome over the held candidate.

When a reviewer head and heartbeat exist but the document nudge gets typed `busy` evidence before any
send, delivery is pending, not started. The launch intent keeps the exact reviewer HeadRun,
handle/leaf binding and workspace with a capped durable retry schedule. Until a later nudge confirms
delivery, recovery does not freeze or signal the worker, write reviewer routing or lifecycle
attribution, clear the intent, or replace the head. Confirmation crosses the ordinary launch adoption
boundary once; `unavailable`, malformed and stale-handle evidence keep their own conservative paths.

### Provider failure on a head's first turn

A worker or reviewer head whose first turn ends on a provider error, with no report or verdict, has
failed on its provider. That is a provider verdict, not a stall (secretary-1799,
`src/ummanu/dispatch/provider_failure.py`). The provider errors in scope are an HTTP 401/403, a 429,
any 5xx (529 included), and a connection the client gave up on after its own retries ("Reconnecting...
5/5", "exceeded retry limit", "stream disconnected before completion"). A turn that ends on anything else
(a context window, a tool failure, a refusal) keeps its old path.

Sources, read by `CommandHostRuntime.provider_failure` from the role's exact HeadRun, never from the
workspace at large:

- Codex: the run's bound rollout journal. A turn runs from `task_started` to `task_complete`; the
  failure is a `task_complete` whose `error.message` is a provider error (or an in-turn `error` event
  closed by a `task_complete` with no agent message).
- Claude: the run's bound session transcript. The failure is an `isApiErrorMessage` record
  (`apiErrorStatus`, typed `error`) that is the transcript's last user/assistant record. When no
  transcript can be bound, the bottom of the head's PTY screen is read instead (the supervisor's output
  buffer rendered to a screen), and only when the supervisor journal says the head is idle after turn 1.

First turn means the head never completed a clean turn: every completed turn so far ended on an error
and the last one on a provider error. A head that has completed a clean turn, reported or given a verdict
is out of scope and behaves as before.

**Precedence.** `wait_vitality.wait_watchdog` asks this before the vitality verdict, on every wait tick:
the tick that first observes the turn's end acts on it, without waiting for any stall timer, and no
`{kind}-stall-suspected`, `{kind}-respawned`, report nudge or stall escalation is produced for that head.
From the error to the relaunch takes one tick.

**On detection, in this order:**

1. The head's resource is recorded `unavailable` in `<data>/dispatcher/resource_health.json`
   (`HeadHealth.record`), with the reason, replacing its cached probe verdict. It holds for the probe
   TTL; the probe runs again after that.
2. The head is stopped (reviewer: initiator `provider-failure`; worker: the confirmed replacement stop).
3. The same role is relaunched on the next launchable head of the card's chain: `resolve_head_chain`
   from the card's head override (`head_override` / `review_head_override`), else the role default. The
   report generation, TASK.md and the green candidate stay as they are.
4. One card comment and the tick outcome (`worker-provider-fallback` / `review-provider-fallback`) name
   the head, the resource, the error summary (secrets and request identifiers removed) and the head
   switched to (`switched_to`).

None of this charges a round, a respawn, the red-review counter or the sprint budget, and none of it
moves a card to Blocked.

**Empty chain.** Worker phase: the card moves to Ready with the reason `provider unavailable: <resource>`
(action token `provider-unavailable-ready`, which the sprint budget does not count as a preempt); the
claim-time walk claims it again once a head of its chain is launchable. Reviewer phase: the card stays in
Validate with no reviewer (`review-provider-unavailable`, the record's `review_provider_hold` carries the
reason); `start_review` walks the chain again on every tick, spends no infrastructure retry, and launches
the reviewer on the first head that can run, with one comment. The worker's candidate, gate receipt and
report are not discarded.

### Resource probe statuses

`ummanu.head_health` owns the verdict on a head resource. A claim, a reviewer launch and a fallback
walk launch only on `ready` or `unknown`; every other status walks the fallback chain.

| Status | Meaning | Launch |
|---|---|---|
| `ready` | the probe succeeded | yes |
| `unknown` | the probe answered with something nobody could classify, or has no probe command | yes |
| `timed_out` | the provider gave the probe no answer in time (outer command killed, or the inner probe's own `status=timeout`) | no |
| `unavailable` | the provider failed: 5xx, reconnect exhaustion, a 429 rate limit, or a head's first-turn provider error recorded by the dispatcher | no |
| `unauthenticated` | the account was refused: a missing login, an expired key, a 401/403 for the account | no |
| `exhausted` | the quota is spent | no |
| `probe_broken` | the probe command could not be launched | no |
| `missing` | a chain entry the registry does not describe | no |

A 401 "Incorrect API key provided" from the ChatGPT/Codex backend while `CODEX_HOME/auth.json` is a
ChatGPT-mode login is the provider's fault, not the account's: the inner `openai-sub` probe reports it as
`status=provider-unavailable` and it reads `unavailable`. A 401 with an API-key login, or no login at all,
stays `unauthenticated`. Reasons carry no secret.

Timeouts are per resource. The inner probe (`ummanu.runtime.resource_probe.probe_timeout_s`) waits 75 s
for `openai-sub`, whose client reconnects about ten times before it prints a refusal, and 20 s for the
others. `TA_PROBE_TIMEOUT_S` moves the default and `TA_PROBE_TIMEOUT_S_<RESOURCE>` (id upper-cased, `-`
as `_`, e.g. `TA_PROBE_TIMEOUT_S_OPENAI_SUB`) sets one resource. The outer timeout around the probe
command (`head_health.probe_timeout_seconds`) is the inner one plus 10 s, so the inner classifier always
answers first. `ummanu doctor` shows `timed_out` like the other non-ready statuses: a
`resource_readiness` finding and a `provider_red` owner event.

### A settled head is not a delivered prompt

The `local-pty` supervisor waits for the head to settle, types the line, waits for the turn over that
line to close, then sends Enter alone and watches the head answer. Only output that shows a turn
started sets `turn_confirmed`; a line typed and not taken is `payload_left_in_composer`, never `ok`.
Codex's `Update available!` modal is prevented before the head starts, best effort: preflight sets
`dismissed_version` in the runtime `CODEX_HOME`'s `version.json` to the version found, the same thing
"Skip until next version" writes. No delivery ever upgrades.

The pre-delivery states (`update-modal`, `starting`, `unknown-dialog`) and `sendability`
(`unestablished`, `dialog-refused`) are delivery-record vocabulary from before A20, when heads ran as
Orca panes: a pane's readiness answer held for a TUI quiescent in a dialog, so that delivery
classified the live screen before writing. Records written then carry those fields and read back
unchanged (`runtime/tui_delivery.py`).

The evidence keeps apart **modal resolution** (`modal_resolution`, `modal_answers`,
`pre_delivery_*`, empty on a `local-pty` delivery), **delivery receipt** (`delivery_receipt`, from
`payload_left_in_composer` and `turn_confirmed`) and **provider binding** (`provider_bound`, the
caller's criterion), with `sendability` beside them.

### A live head is not a delivered pointer

`delivery_receipt` is the one predicate launch, recovery and adoption all ask. It reads only the
delivery boundary's evidence; a live pid and a transport's write acceptance (`send_accepted` /
`bytes_written`, which pane-era records carry as Orca's answer) are never consulted. Positive
`payload_left_in_composer` evidence is a determinate `refused` and outranks a provider turn.

A bring-up that aborted with its head still running carries that receipt onto its launch intent. Adoption
refuses an undelivered launch: no claim, no routing event, no `review_starting`, no `reviewing`, no
`waiting-review-verdict`, no worker freeze, and the intent is **not spent**. The refusal is bounded by
`UMMANU_LAUNCH_DELIVERY_MAX_ATTEMPTS` (default 5). Inside it, a reviewer re-delivers the *same*
immutable pointer at the same path over the exact recorded run (the document body never enters the
terminal), so an interrupted tick resumes one delivery transaction. Past the ceiling the head is
stopped through its own intent and the ordinary path launches again; a stop the host will not confirm
keeps the intent.

This covers delivery and adoption only; it does not change watchdog rungs or the `healthy_quiet`
ceiling. Ordering with the headless recovery below: the delivery-evidence refusal runs first inside
`_adopt_launch_intent` and returns before any identity question. The missing-identity path is
`_adopt`, which has no launch intent or delivery evidence to consult.

### A card in an active state with no worker

A card can be in an active column with nothing running: a raw board move out of Blocked into In
progress, or a bring-up whose tick died before binding anything. `_adopt` rebuilds a record from the
board; unless the worker's own pid heartbeat can be bound, that record names no head.

The tick settles it before any wait, establishing exactly one of:

- **a verified live worker identity** — `_adopt` binds the worker heartbeat only after its run, role
  and card binding are promoted into a HeadRun and re-checked;
- **a replacement launch** — the launch intent is written first, bound to the retained checkout and
  its exact candidate (path, branch, clean/dirty tree, exact SHA), and the head is brought up under
  it. Nothing is recreated from base, re-seeded or reset, and no other workspace is substituted;
- **a refusal** — the card goes back to Blocked with a named recovery error: `workspace_missing`,
  `workspace_unbindable`, `workspace_unreadable`, `candidate_unknown` or `round_already_answered`.

`round_already_answered`: the retained checkout holds the document of a round the board has already
consumed a worker report for (evidence: the card's consumed report markers and the checkout's TASK.md
round record). No worker round is invented and no receipt is invented.

A live heartbeat at the card's worker pid path that cannot be bound is reported
`orphan-worker-heartbeat-unbound`; nothing is launched beside it and nothing is signalled.

A card with no dispatcher record is ticked under the constant `production_adopt_attempt_id(ref)`, so
the refusal and relaunch comment are keyed on the episode too: the stamp written when the episode was
first observed plus the card's comment count at that moment. Within one episode a retried tick
replays onto the same id; a second episode on the same card gets a second refusal.

While unresolved, `ummanu status` shows the card as degraded. The attempt row carries `headless`
(record state, that no handle and heartbeat are known, since when, retained workspace, branch, dirty
flag, candidate SHA), and the sprint summary lists it under `degraded_cards`.

### Broad-check handling

Workers use focused checks while developing and run at most one local broad suite per report
generation/unchanged SHA unless they state why it was rerun. The broad run goes through
`ummanu check broad`, which streams output, returns the check's exit status and writes a
worker-local broad receipt under the ignored `state/checks/` path (in a dispatcher workspace, under the
dispatcher-owned `.ummanu-task-env/checks/`): command and check-set digest, cwd
and imported project provenance, start/end/duration, exit code, parsed verdict and counts (scanned off
the stream), and a bounded diagnostic tail. The receipt records content as one git tree object id —
the tree this worktree, with tracked edits and untracked files, would commit to — so
`ummanu check show` answers whether it still describes the code, and committing that content
unchanged keeps it usable. While a usable receipt exists, rerunning the broad suite only because
output scrolled away is prohibited; an edited worktree or a concrete red result being fixed justifies
a new run, named in the report.

A receipt only claims an import it observed from the process that ran the check. The `--module` shape
runs the suite itself and records what that process imported. An arbitrary `--command` shell may
change directory or import environment before any interpreter starts, so it attests no import and is
never reused in place of a run. Reuse also requires the import to have
resolved inside the candidate workspace; an import resolving to another checkout is recorded and
refused. One predicate answers
this for every route, and a check is keyed by its structured check set (shape, module, exact argument
vector). A truncated or edited artifact, a result no run could have written, a killed or timed-out
run, or a checkout with no resolvable identity attests nothing.

The worker-local receipt never leaves the workspace and is never committed. Only an executed
local/GitHub gate with a valid dispatcher-owned exact-SHA gate receipt is reusable evidence
downstream. A none/noop gate or missing receipt attests no broad suite, so the role runs or requests
appropriate validation. Reviewers inspect changed code and invariants but do not repeat an attested
broad command on the same SHA without a recorded `rerun_reason`; targeted reproduction stays
appropriate for a new blocker, uncovered external behaviour, or security/data-loss risk. Re-review
packets carry the previous reviewed SHA, previous blocker text/IDs, current SHA and changed-path
delta.

Worker and reviewer shells run with `workspace/.ummanu-task-env/venv/bin` first. When an adapter
declares `broad_check` without `broad_check.interpreter`, its candidate `.[dev]` install supplies the
project runtime there. Adapter setup runs outside both virtualenvs, and an explicit relative
broad-check interpreter may select the adapter's own `.venv`. The role environment removes the
launcher's production `PYTHONPATH`. The module receipt records the actual interpreter, environment
prefix and import origin; an origin outside the candidate is a refusal. One control-plane renderer
makes every head-visible Ummanu protocol, report, verdict, `check broad` and `check show` command
name the absolute production interpreter, `-P`, and registered production `src`. A fit broad contract
may select an explicit candidate interpreter for its inner suite; missing, refused and module-less
contracts keep the production wrapper reachable but infer no inner runtime from `PATH`.

Observer, steward, retro and curator shells run the product's own CLI, so they run with the product's
managed `<product root>/.venv/bin` first, where the product root is the checkout the role imports
from (`TA_RUNTIME_PYTHONPATH`, else `UMMANU_REPO`). `role_env exec` refuses to start one of
those heads when that `python3` is missing or not executable, naming the path and
`ummanu upgrade --no-pull --product-root <root>` as the repair, as the agent gate does.

One production-runtime provenance probe fences workspace prepare, worker/reviewer launch, both sides
of a gate query, both sides of release, and worktree removal. It runs the fixed production
interpreter in isolated mode and classifies `interpreter_unavailable`, `missing_import`, `wrong_root`
and `workspace_targeted_editable` (plain editable paths, executable editable finder modules named by
`.pth`, and `direct_url.json`, including vanished paths under either workspaces root: the Orca
workspaces root of A20 steps 8 and 11, `~/orca/workspaces`, and `<data_dir>/workspaces`). Any
refusal becomes durable blocked evidence and keeps the checkout; installation metadata is never
repaired implicitly.

Before a card is given to a worker, the dispatcher asks whether the project's broad-check contract
can attest the project, through the same implementation `ummanu check broad --module` uses. It
reads only the project binding and adapter. The answer is one of three named states, and no caller
treats an unrecognised answer as permission:

- `fit` — the card is issued;
- `refused(shape)` — one of `adapter_unavailable`, `adapter_invalid`, `broad_check_incomplete`,
  `broad_check_not_declared`, `interpreter_unavailable`, `cannot_attest_project`. Always before the
  card is put in work (no workspace, head or round). The card is blocked through the
  [bring-up](#bring-up-outcomes) vocabulary as infrastructure, with action token
  `contract-preflight-blocked` and the refusal shape as evidence;
- `undecidable(question)` — one of `relative_interpreter`, `no_registered_project`,
  `project_unavailable`. The card goes to work.

The preflight answers for the declared contract only. A relative interpreter resolves against the
candidate workspace, which does not exist at preflight, so it is `relative_interpreter`, not answered
against the registered checkout. A declared contract is executed as declared: the preflight checks
only that it is complete and that the named interpreter starts; what a run actually imported is
caught by the receipt's provenance.

Declaring `broad_check` is mandatory for every project that gets cards. An adapter without one is
refused as `broad_check_not_declared` and its cards block at preflight. `cannot_attest_project` means
a declared contract that cannot attest its own checkout.

Observers consume the worker report, reviewer verdict and gate receipt before code/CI exploration. A
valid executed gate receipt suppresses a routine broad rerun; none/noop or missing evidence does not.
Contradictory evidence, RED/Blocked classification, a real Definition-of-Done gap, or a
security/data-loss concern requires research. The role that owns a further broad rerun records its
reason in the report; a worker-local receipt never transfers ownership of an unexplained rerun or
suppresses a targeted reproduction of a new concrete risk.

### Decisions on a parked card

A card parks only where a decision can come from: its sprint is open and declares a concrete observer
head. A card with no linked sprint, or whose sprint declares `--observer none` or has closed, acts on
its verdict on its own tick.

The observer records the decision; the dispatcher performs it: merge and `assessment -> done` for a
release, a new worker round and `assessment -> in_progress` for a rework, `assessment -> blocked` for
a reslice.

```bash
python3 -P -m ummanu task decide --role observer --ref PROJECT-N \
  --kind release --reason-file REASON.md --request-id REQUEST_ID
```

`--kind` is `release`, `rework` or `reslice`; the reason is required. Only the observer decides, and
only about a card whose project an open sprint reserves; otherwise it is refused.

A move out of Assessment carries the decision it performs, with one destination each: `release` →
Done, `rework` → In progress, `reslice` → Blocked. A `--decision` the card's audit does not hold since
it entered the column, or paired with the wrong destination, is refused whoever passes it. The
dispatcher must carry a decision: its move to Done or In progress without `--decision` is refused, as
are `assessment -> ready`, `-> validate` and `-> issues`. The PO is not bound by this (its move is the
escape hatch; on a card of a sprint that holds its project it carries `--sprint-override` and a reason).
`assessment -> blocked` takes no decision from anyone (steward escalation and dispatcher failures).
The observer takes no exit out of Assessment at all, even with a matching decision; its authority is
`task decide`.

A release the dispatcher cannot carry out (merge rejected, or pre-merge re-check red while parked)
takes the card to Blocked with the failure. Deciding again on a partly failed release is a separate
card.

`ummanu task move` is the one transition writer. Steward report cards, steward signals and retro
Done retention go through Ummanu's TaskReader/TaskWriter adapters with the same audit and sprint
guards. `--target` is an alias of `--to`.

```bash
python3 -P -m ummanu task list --project PROJECT
python3 -P -m ummanu task show --ref PROJECT-N
python3 -P -m ummanu task list --sprint sprint:ID
python3 -P -m ummanu task create --role po --project PROJECT --type code \
  --title TITLE --state ready --head codex-sol-high --sprint sprint:ID
python3 -P -m ummanu task archive --role po --ref PROJECT-N \
  --reason-file REASON.md --request-id REQUEST_ID
python3 -P -m ummanu task edit --role po --ref PROJECT-N \
  --body-file SPEC.md --head codex-terra-high --review-head claude-opus-high
python3 -P -m ummanu task create --role po --project PROJECT --type code --title HOTFIX \
  --sprint sprint:ID --sprint-override --sprint-override-reason-file REASON.md
```

`create` accepts `--description` or `--body-file`, plus dependency, workspace and routing fields.
With `--sprint`, the sprint must be open and the project one of its reservations (a closed sprint and
an unreserved project are separate errors, both before any backend write). Without it, only the PO
creates an execution task (see [Cards outside a sprint](#cards-outside-a-sprint)); for other roles it
requires `--sprint`. `--priority` is rejected. Execution tasks are created in Ready; worker, reviewer, retro
and steward roles create only proposals in Issues, which a PO triages to Ready. The steward's one other
create is its report card In progress (research, a slug, no sprint); a steward proposal is refused in
any other column (`role_forbidden`) or with a sprint on a reserved project (`sprint_write_forbidden`),
as a retro proposal is, and its audit names the steward as the creator.

Without `--ref`, `task create` allocates `PROJECT-N` from the project's board-wide high-water mark
over open and closed cards. Any reference, allocated or given, is checked as unclaimed before it is
written; an archived card holds its reference for good. Allocation, staging and the backend create
are one locally serialized operation. The pending audit records the chosen reference and, once
returned, the backend task id; a recovered pending create verifies and repairs only that recorded
backend id. A pending create without a recorded id is not adopted or repaired automatically.

`--codex-mode` is valid only for a worker profile on a `codex` adapter, and its only value is `tui`:
every Codex head is one interactive session. `exec` is rejected before the board is touched, a head
profile naming it fails registry validation, and a card read or restored with it carries no launch
mode.

`archive` closes an execution task and removes it from active listings without deleting board
history. PO-only, non-empty reason, append-only audit, idempotent through `--request-id`. Cards in
In progress or Validate, or with an active claim, are rejected. A card archived from Done stays a
satisfied dependency; from any other column it is not Done and unblocks nothing. It cannot close a
Product or Issue (use `ummanu issue close`).

`edit` replaces a card's spec in place: `--title`, `--description`/`--body-file` (full new text),
`--head`, `--review-head`. PO, dispatcher and observer may edit; an ordinary card is editable only in
Ready or Blocked (an active card goes through preempt and requeue). The `edited` audit event records
old and new digests; past text is recoverable from the checkpoint's board export history. Comments
are the dialogue of an attempt; the spec lives only in the description.

## The e2e stage

A project may declare one end-to-end check: a GitHub Actions workflow the dispatcher dispatches on a
`code` card's candidate and waits for through a [wait card](#wait-cards) (secretary-1795). The stage
is `dispatch/e2e_stage.py`; the declaration and the GitHub calls are `dispatch/e2e.py`.

**Declaration.** In the adapter, beside the gate:

```yaml
validation:
  ci: github              # required: only the github gate publishes the candidate branch
  e2e:
    workflow: e2e.yml     # the workflow file name, or its numeric id
    inputs: {suite: mega} # optional static workflow_dispatch inputs (string, number or boolean)
    deadline: 6h          # optional duration, default 6h: the wait card's deadline
    candidate_input: sha  # optional: the input that receives the candidate SHA
    dispatch_id_input: sid # optional: the input that receives the dispatch id (see below)
    placement: before_merge # optional: before_merge (default) or after_merge (see [After merge](#after-merge))
```

It is read with the adapter (`InstanceCatalog.adapter`), and a malformed one fails that read with
`AdapterE2eDeclarationError`, a typed adapter error naming the adapter and the problem: an unknown key,
no or a bad `workflow`, `inputs` that is not a mapping of input names to scalars, a bad `deadline`, a bad
`candidate_input` or `dispatch_id_input`, a static input or `candidate_input` sharing the
`dispatch_id_input` name, a `placement` other than `before_merge` or `after_merge`, or `ci` other than
`github`. The card's gate
then fails with that reason, and nothing is skipped silently. The adapter schema says the same. A
project with no `e2e` key behaves exactly as before.

The workflow needs no input of the dispatcher's: it is dispatched with exactly its declared `inputs`,
plus `candidate_input` when declared, plus `dispatch_id_input` when declared (GitHub refuses an input the
workflow does not declare, HTTP 422).

**Placement.** For a `code` card of a project declaring the default `placement: before_merge`, the stage
runs in `park_green_verdict`: after a
green review verdict, or right after green CI when the card's review is `skipped`, and before the park in
Assessment or the no-observer release. A red review never reaches the stage, so rework rounds spend
no runs. The release audit (`release_parked`) runs the same stage: a card parked without a green run
(a red review released by the observer) dispatches it there, waits, and merges only after it is green.

**One SHA, from the gate.** The stage never reads HEAD itself. Each tick it first deals with a run
still underway or a result not yet acted on, without reading the gate. Then it reads the merge gate,
without accepting it (`read_merge_gate`, `read_release_gate`); that read may refresh-merge a newer base
(`_recover_base`). The SHA the green receipt validated (`validated_sha`) is the one the stage
dispatches on, records, identifies against and judges. The caller accepts that same gate result once,
after the stage is green, and attests it on the card. So a review-required card parks in Assessment
with the e2e result on it, and a red e2e never parks.

The stage proceeds only on a SHA with a green run of its own, or one reconciled to a green run:
- the validated SHA has a green run: proceed;
- otherwise, the newest green run's SHA reconciles to it through `reconcile_reviewed_base_move`: proceed.
  This is the rule that carries a review across a base-only move. Nothing between the two SHAs but base
  history, and the card's own changed paths byte-identical. The reconciliation is recorded on the green
  entry (`reconciled`, shown in `task show` as `reconciled_to`) and in the attestation of the park or the
  release audit (`E2E/base reconciliation: ...`);
- otherwise, the green run does not authorize this SHA: the stage runs again for it, which counts
  against the e2e run budget ([below](#the-e2e-run-budget)).

A base-only move does not invalidate an e2e result for the card's own change, as it does not invalidate
a review; CI still gates the new SHA. This avoids re-paying a long run for every base move on an active
main (the observer's decision, secretary-1795).

**Dispatch identity.** The card's `e2e` field (`extensions.extra.e2e`, JSON, written only by the
dispatcher through `TaskWriter.record_e2e_state`; no column, no migration) is the stage's record: one
entry per dispatch. The entry is written as an intent (card, SHA, dispatch id
`<card>-e2e-<n>-<random>`, repository, branch, workflow, deadline) before `POST
repos/<repo>/actions/workflows/<workflow>/dispatches` on `pipeline/<card>` with `return_run_details:
true`. The run is identified by GitHub's own answer: the 200 response carries `workflow_run_id` (GitHub
changelog 2026-02-19, "Workflow dispatch API now returns run IDs"). The parameter is sent explicitly
through `gh api`, so it does not depend on the host's `gh` version. The run id is recorded right after
the call, and the run's `head_sha` (`GET repos/<repo>/actions/runs/<id>`) is checked against the
candidate.

An entry is continued and never dispatched again.

**Recovery.** Recovery runs only when GitHub's answer naming the run was lost: the dispatcher died after
the POST and before recording it, the POST got no answer or was rate limited, or the answer carried no
details. The run is looked up among the workflow's runs by all of:
- `event == workflow_dispatch`;
- branch `pipeline/<card>`;
- `head_sha ==` the candidate;
- `created_at` no earlier than the intent less a 120-second clock margin (GitHub's clock against the
  dispatcher's).

The lookup waits until that window has settled: until the intent + the margin + a settle period of 3
minutes (`UMMANU_E2E_RECOVERY_SETTLE_SECONDS`). Before then even a single visible match is not
attached. After it:
- exactly one match is attached as `recovered`: the entry records `identified_by: recovery` and the
  rule it matched, and a dispatcher comment says the run was identified by recovery, not by GitHub's
  answer (so does the green comment);
- two or more matches are ambiguous and never guessed: the card is Blocked with every candidate listed;
- no match within 15 minutes of the intent (`UMMANU_E2E_IDENTIFY_SECONDS`) Blocks the card.

With `dispatch_id_input` declared, the dispatch id is sent in that input, and recovery requires it in
the run's title as well. That is exact for a workflow that puts the input in its `run-name`, and a run
without it is never attached.

The residual case is a POST that GitHub never executed, together with a foreign dispatch of the same
workflow on `pipeline/<card>` at the same SHA inside the window. Recovery would then attach the foreign
run. The branch is the dispatcher's own and a foreign dispatch on it is unsupported; an adapter that
needs exactness declares `dispatch_id_input`.

A run whose SHA could not be read within the same 15 minutes, a run on another SHA, and a dispatch
GitHub refused (no workflow, no `workflow_dispatch` trigger, an input the workflow does not take, no
access) Block the card with the reason, not rework.

**Wait.** Once the run is identified, the dispatcher creates a `wait` card for it: target the run,
the adapter's deadline, the code card's sprint (none if it has none), return address
`card:<code card>`, request id `dispatcher-e2e-wait-<dispatch id>`, so a repeat after a crash is the
same card. The entry records its ref. The wait card is on the board and in the code card's `task show`.
No other poller exists: the wait card reads the run, and the stage reads only the wait's frozen result,
copying it into the entry.

**Outcomes.**

- conclusion `success`: a `## E2E — green` dispatcher comment (run, SHA, dispatch id, wait card, runs
  dispatched and the budget they were charged to), and the card proceeds: Assessment for a card that parks, the release otherwise;
- conclusion `failure`: rework, as a red gate (`gate_red_to_worker`, phase `e2e-gate`, so the red CI
  is charged as one): the worker's TASK.md carries, under "Mechanical gate failure to address", the
  run URL, the conclusion, each failed job with its failed steps, and the gate's bounded
  `gh run view --log-failed` fragment of the first failed job. In the release audit, where no rework
  round is open, the same evidence Blocks the card instead (`blocked_reason: gate`);
- any other conclusion (`cancelled`, `timed_out`, `action_required`, ...) and every other wait outcome
  (`deadline_passed`, `source_unreachable`, `cancelled`): Blocked with the outcome, the run link and
  the wait card, `blocked_reason: infrastructure`. No worker round is opened or charged.

Every Blocked the stage causes records its request id (`dispatcher-e2e-blocked-<dispatch id>`) and
reason on the entry before the move, so a repeat moves once. Every result is marked `acted` on the
entry when the stage acts on it, so a later round never acts on it again. Once a Blocked move is
committed, the run's pass is over: a card brought back on the same SHA (unblocked, re-claimed and
reported again) may dispatch a new run. A `success` stands for its SHA (and for a SHA reconciled to it),
and so does a `failure` that sent the card to rework.

A declaration the stage cannot read Blocks the card as `gate`.

### The e2e run budget

Every e2e run pays for stands (BitLaunch machines), `mega-noop` included, so runs are budgeted
(secretary-1796; `board/e2e_budget.py`). The budget replaces the interim per-card cap of 3 for every
card of a sprint.

**The budget.** A sprint carries `e2e_budget`, the runs it may dispatch (`sprint create --e2e-budget N`,
an integer 0 or more, default 3), and `e2e_used`, the runs it dispatched: two columns of `sprints`
(revision `0023_sprint_e2e_budget`, which gave every existing sprint, open ones included, 3 and 0). Each
charged run is one row of `sprint_e2e_charges` (dispatch id, the key; card; sprint; when). `sprint
show` and `sprint status` carry `e2e`: `budget`, `used`, `summary` (`e2e: <used> of <budget>`), `cards`
(the cards that spent runs, first charge first) and `charges` (`card`, `dispatch_id`, `at`).

**Counting.** Every run the stage dispatches counts, at any level. A run is charged when its dispatch
intent is written, before the POST: a dispatch whose outcome is unknown has been paid for, and so has
one GitHub refused. The check and the increment are one statement on the sprint row, in the transaction
that writes the intent on the card (`TaskWriter.record_e2e_intent`): `UPDATE sprints SET e2e_used =
e2e_used + 1 WHERE ref = <sprint> AND e2e_used < e2e_budget ...`, then the charge row. Two cards of one
sprint can never together exceed the budget: PostgreSQL serializes the two UPDATEs, and the second finds
nothing left. An intent that is not charged is not written, and nothing is dispatched. A recovered run
(after a crash between the intent and the POST, or a lost answer) continues its entry and writes no
second intent, so it is never charged twice; a dispatch id is charged at most once.

A card outside every sprint keeps its own cap of 3 runs across its SHAs (`run_cap`), plus every raise
recorded on it. Its runs are counted the same way: every entry whose intent was persisted counts,
whatever GitHub answered, a refused dispatch included (`E2eState.dispatched`, which `task show` shows as
`runs_dispatched`).

**Budget spent, a card of a sprint.** The stage does not start and nothing is dispatched. The dispatcher
cuts one `decision` card on the sprint, in the waiting card's project, under request id
`dispatcher-e2e-budget-<sprint>-<budget>`: one card per (sprint, budget generation), the generation
being the budget the runs were spent against. Its body carries the card waiting for e2e and its SHA,
every run spent (card, SHA, run link, state and result summary, when), the question ("raise the e2e
budget of <sprint> by N runs, or no?") with the two exact answer lines below, the instruction to hand
it to the owner quoting them (a money decision; the PO does not raise the budget on its own authority),
and the exact raise command. It is executed like
every decision card ([Decision and operation cards](#decision-and-operation-cards)): the sprint's PO
session takes it, and the PO hands it to the owner with `task handover`.

The waiting card is not Blocked. It stays in its column (Validate, or Assessment in the release audit)
and its entry records `budget_wait` (`decision`, `generation`, `scope`, `since`); `task show` carries
`e2e.mark`: `e2e: budget spent, waiting on <decision>`, and the tick outcome is `e2e-budget-waiting`
with `decision` and `mark`. A card that reaches the stage while the budget is still spent joins the
same decision with one dispatcher comment on it (`dispatcher-e2e-budget-join-<decision>-<card>`); no
second decision is cut. Each tick the stage re-checks a waiting card first, without reading the gate:

- a run is available again (the budget was raised): the mark is cleared, and the stage reads the gate
  and charges the next run as usual. When a raise was spent by other cards first, the card's next
  attempt finds the budget spent again and waits on the decision of the new generation;
- the decision is Done and the budget was not raised (the owner said no): the mark is cleared and the
  card is Blocked with the decision's completion text (`blocked_reason: other`, request id
  `dispatcher-e2e-budget-declined-<card>-<decision>`): the owner's money decision, not a defect of the
  card's code, so no worker round is charged;
- otherwise it keeps waiting.

**The owner's answer.** The owner answers on the decision card with a comment (`task comment --role
owner`) holding exactly one answer line, parsed in one place (`e2e_budget.owner_answer`):

```text
e2e budget: raise <N>
e2e budget: no
```

`<N>` is a positive integer. Matching is case-insensitive and ignores the whitespace around the line;
the rest of the comment is free prose. A comment with neither line, with both, or with two raise lines
answers nothing. An `e2e budget: no` comment moves nothing by itself: the PO completes the decision card.

**Raising the budget.** Only on the owner's recorded word, and by the owner's recorded number:

```bash
python3 -P -m ummanu sprint e2e-budget --ref <sprint> --role po --authorized-by <event id> [--add <N>]
```

`--role po` only (every other role is refused, `role_forbidden`), and `--authorized-by` the event id of
an owner-role comment on one of this sprint's e2e budget decision cards, made after that card was
handed to the owner, whose answer line is `e2e budget: raise <N>`. The raise is that N: `--add` is
optional, and when given it has to equal N. The owner's answer input the dispatcher sends the PO names
that event id. A missing, unknown or non-comment event, a comment that is not the owner's, a comment on
any other card (another sprint's decision included), one made before the handover, an `e2e budget: no`
comment, a comment with no single answer line, and an `--add` other than the owner's N are refused
(`authorization_refused`, exit status 3), and nothing is written. One owner comment raises once: the default request id is `e2e-budget-raise-<event id>`, a
repeat of it is the same raise, and another request id naming the same event is refused. The raise adds
`N` to `e2e_budget` in place, in the transaction of one `e2e_budget_raised` sprint audit record whose
payload names `add` (the owner's N), `authorized_by` (the comment's event id) and `decision`; a closed
or stopped sprint refuses it. The waiting
cards dispatch on the next tick, and the PO completes the decision card as usual.

**Budget spent, a card outside every sprint.** At its cap the stage dispatches nothing:

- a card with a PO origin ([PO delegation](#po-delegation)) gets the same decision card, cut with that
  origin (the dispatcher carries the card's `po_origin` onto it; no other role but the PO records an
  origin) and no sprint, under `dispatcher-e2e-cap-<card>-<cap>`, so it goes to the card's origin line.
  The raise is `task e2e-budget --ref <card> --role po --authorized-by <event id> [--add <N>]`, authorized
  the same way, by the same answer line and to the same N, by an owner comment on that card's decision (or
  on an after-merge batch decision naming the card spent, [After merge](#after-merge)) after its handover; it is recorded on the
  card (bag field `e2e_cap`, `{raises: [{add, authorized_by, decision, at}]}`, one `e2e_cap_raised`
  audit record each) and raises that card's cap. The wait, the re-check and the decline are as above;
- a card with no origin has nobody to hand the decision to: it is Blocked with `e2e run cap reached
  (<cap>)` (`blocked_reason: other`) and one `e2e_budget_spent` bell event
  ([Owner events and the bell](#owner-events-and-the-bell)).

**`task show`.** A card that reached the stage carries `e2e`: `runs_dispatched`; `run_cap` (a card
outside every sprint: 3 plus its raises; null for a card of a sprint) and `budget` (the sprint whose
budget the card spends, or null); `mark` and `waiting_on` while it waits on a budget decision; and per
run `sha`, `dispatch_id`, `workflow`, `state` (`dispatching`, `identifying`, `wait_card_pending`,
`waiting`, the conclusion, the wait outcome, or `dispatch_refused`), `run`, `identified_by` (`answer` or
`recovery`, with `recovery_rule`), `reconciled_to`, `wait_card`, `dispatched_at`, `result`.

### After merge

Some e2e workflows can only run on a commit whose releases the post-merge CI of `main` published: the
Codegen mega's `stand-e2e.yml` waits for the worker and service releases of its exact SHA, and codegen
publishes them only on a push to `main`. Its candidate branch never has them. A project declares that with
`placement: after_merge` (secretary-1807, the PO's decision secretary-1805; `dispatch/e2e_after_merge.py`).

**No stage before the merge.** A card of such a project never runs the stage above: it goes through
review, Assessment and release as a card of a project with no e2e.

**Queueing.** When the [post-merge watch](#post-merge-ci) records a card's merge commit `green`, the
card joins its project's pending set with its merge SHA (`e2e_after_merge.enqueue`); `red`, `absent` and
`timeout` queue nothing (red is handled as before). A green watch is dropped only once the card is
queued, or its project is established not to be `after_merge`: the queued card is saved first, then the
drop. While the adapter or its `validation.e2e` cannot be read (unavailable, or malformed) the watch is
kept, marked `published` so its fact is not published again, the tick reports a degraded
`e2e-after-merge-not-queued` outcome, and each pass retries the enqueue alone; no new timer bounds it. The
enqueue is idempotent per card and merge SHA (a card already pending, covered by the run in flight, or
marked on the board for that merge SHA is not queued again), so a replay after a save lost between the
two queues the card once. The
pending sets live in the dispatcher's production state (`e2e_after_merge`, one queue per project:
`pending`, the `run` in flight, `budget_waits`, `cleanup`), beside the post-merge watches; `production
observe` lists them. Each queued card is marked on the board too (below).

**One run in flight per project, coalesced.** Each tick, after the post-merge watches, every project's
queue advances on its own (`reconcile_after_merge`); one project never waits on another. While a project
has a run in flight its pending cards wait. When it has none and the set is not empty:

- the target is the newest merge SHA in the set (by merge time); every card in the set has green
  post-merge CI, or it would not be there;
- the run covers every pending card whose merge SHA is the target or an ancestor of it (GitHub's
  compare, `ahead` or `identical`); a card off that line stays pending, and so does every card merged
  later;
- the covered cards and the SHA are recorded before the dispatch, in the intent: the run record (dispatch
  id `<carrier>-e2e-am-<n>-<random>`) lives on the newest covered card, the *carrier*, in its `e2e` field
  under `after_merge_runs`, with `covered` (`{ref, merge_sha}` each), the branch, and what paid for it;
  every covered card's mark says `covered` with that dispatch id. All of it is one write,
  `TaskWriter.record_after_merge_intent`, in the transaction that charges the run. The production state
  names the run in flight before that write, so a dispatcher that dies in between finds no intent on the
  carrier and puts the cards back, with nothing charged and nothing dispatched.

**Exact SHA, on a branch the dispatcher owns.** `workflow_dispatch` takes a branch or a tag, not a SHA,
and `main` may have moved past the target. The dispatcher creates the branch `pipeline-e2e/<dispatch
id>` at the target (`POST repos/{repo}/git/refs`; a branch that already exists is accepted only at that
SHA) and dispatches on it. The name is unique per run, and codegen's `ci.yml` runs on a push to `main`
only, so the branch triggers nothing. `candidate_input`, when declared, receives the target SHA. The run
is identified as in the stage above (GitHub's dispatch answer, else the recovery rule on that branch, with
the settle and the ambiguity rules), and its `head_sha` must equal the target: a run on anything else is
never attached to the covered cards. The branch is recorded in the intent. Once the run's result was
acted on the dispatcher deletes it (`DELETE .../git/refs/heads/<branch>`). The branch counts as gone
only when GitHub answers the delete with success, or answers it 422 or 404 and a follow-up `GET
git/ref/heads/<branch>` answers 404: GitHub also answers 422 for a delete it refused (validation, a
protected branch, spam limiting). Any other answer, a 422 whose read still finds the branch or whose read
fails included, keeps it in the queue's `cleanup` list, asked again each pass until it is gone. The
dispatch is sent only in the tick that wrote the intent: a dispatcher that died after the intent, the
branch or the POST looks the run up and never dispatches it again; none found within the identification
window is an outcome like any other non-verdict (below).

**Budget.** The run is charged at the intent, atomically, exactly as a run of the stage above:

- the newest covered card's sprint is open: to that sprint (`sprint_e2e_charges`, the carrier as `card`);
- otherwise: to every covered card's own cap, all charged together or none. Each covered card records the
  dispatch id in its mark's `charged`, and those count in its `runs_dispatched` against its cap
  (3 plus its raises).

`sprint show` and `sprint status` count after-merge runs with the rest (`used`), and name them:
`after_merge`, the charged runs whose dispatch id is an after-merge one, and the summary `e2e: <used> of
<budget> (<n> after merge)`. When nothing is left nothing is dispatched: no intent, no branch. The batch
of covered cards is the unit, and one decision card owns all of it:

- in an open sprint, the sprint's budget decision of the stage above is cut (or joined) with every covered
  card named under "Waiting for e2e";
- outside one, the dispatcher cuts one decision for the batch, under `dispatcher-e2e-caps-<generation>-<the
  spent cards, joined by .>` (the generation is the sum of their caps), with no sprint and the PO origin of
  the newest spent card that has one, else of the newest covered card that has one. It names every covered
  card with its cap (`cap spent, 3 of 3 runs: needs a raise` or `cap 1 of 3 runs, not spent`) and gives the
  `task e2e-budget` command for each spent card. That decision authorizes a raise of each card it names
  spent (`e2e_budget.authorized_raise`), exactly as a card's own decision does; `task e2e-budget` also
  takes a card whose sprint is closed, since such a card spends its own cap here. With no origin anywhere
  in the batch nobody can be handed the decision: every covered card is declined at once, and each spent
  one rings the `e2e_budget_spent` bell.

Every covered card's mark says `e2e: budget spent, waiting on <decision>`, and cards queued while it is open
join it with one comment each and are marked the same. Each tick the budget is re-checked first: a raise
(the sprint's budget, or every spent card's cap) lets the next pass attempt one run over everything
pending; the decision completed without it declines every card waiting on it (a comment, mark `declined`
with the decision, out of the pending set). None is released silently.

**Waiting.** A wait card on the run ([Wait cards](#wait-cards)), with the adapter's deadline, in the
charged sprint while it is open (else in none), returning to `card:<carrier>`, created once under
`dispatcher-e2e-wait-<dispatch id>`. Its frozen result is read each tick; there is no other poller.

**Outcomes.**

- Conclusion `success`: one comment `## E2E after merge — green` on every covered card, with the run
  link, the SHA and the list of covered cards with their merge SHAs; each mark says `green`.
- Conclusion `failure`: exactly one `code` hotfix card, cut by the dispatcher under
  `dispatcher-e2e-am-hotfix-<dispatch id>` (so a replay after a crash creates nothing new; this request id
  is the only way the dispatcher creates a code card). Its description carries the run URL, the
  conclusion, the failed jobs and steps, the bounded `--log-failed` fragment, the SHA, and every covered
  card with its merge SHA, frozen on the run before the create. It goes to, in this order:
  - the newest covered card's sprint while it is open, with a `hotfix` budget event; the sprint's
    observer wakes on its create as on a Blocked card of the sprint (`tasks.is_dispatcher_hotfix`);
  - otherwise outside every sprint with the newest covered card's PO origin, so its Done or Blocked
    returns to that session ([PO delegation](#po-delegation));
  - otherwise it is created and Blocked at once, reason `after-merge e2e red, no sprint or origin owns
    it` (`blocked_reason: other`), with one `e2e_after_merge` bell event.

  Every covered card gets one `## E2E after merge — red` comment naming the hotfix, and its mark says
  `red -> <hotfix card>`. The cards do not go back to the pending set: the hotfix, once merged, is covered
  by the next run.
- Anything else: another conclusion (`cancelled`, `timed_out`, ...), a wait outcome other than
  `target_reached` (`deadline_passed`, `source_unreachable`, `cancelled`), a refused dispatch or branch,
  a run never identified, several candidates, or a run on another SHA (the run's state is `blocked`: it
  is never attached). No hotfix. One `## E2E after merge — <requeued|blocked>` comment on every covered
  card with the outcome, one `e2e_after_merge` bell event on the carrier, and the covered cards go back
  to the pending set; the next run covers them and is charged as usual.

The resolution (`green`, `red`, `requeued`, `blocked`) is written on the run before any of its effects,
each effect is idempotent (comments and the hotfix under request ids derived from the dispatch id, the
bell under its dedup key), and the run is marked acted on only after all of them.

**`task show`.** A covered card's `e2e` block carries `placement: after_merge`, `state` (`pending`,
`covered by <run>`, `green`, `red -> <hotfix card>`, `e2e: budget spent, waiting on <decision>`,
`declined`), `merge_sha`, `run` (the link), `covered_by` (the dispatch id), `carrier`, `hotfix`,
`decision` and `note` (the last outcome that sent it back, or why it was declined), and `mark` with
`waiting_on` while it waits on a budget decision. The carrier also lists `after_merge_runs`: per run the
dispatch id, SHA, workflow, `ref` (the branch) and `ref_state` (`created`, `deleted`), `covered`,
`charged_to` (the sprint, or `cards`), `state` (the resolution once acted on), `run`, `wait_card`,
`hotfix`, `reason` and `result`.

## Products and issues

`ummanu product` and `ummanu issue` use typed records in the existing Pipeline backend, so
board export, checkpoint and restore carry their metadata and comments.

```bash
python3 -P -m ummanu product create --role po --id ummanu --project ummanu --title Ummanu
python3 -P -m ummanu issue create --role po --product ummanu --kind feature --priority P2 --title TITLE
python3 -P -m ummanu issue create --role observer --kind improvement --priority P3 --title TITLE
python3 -P -m ummanu issue list --product ummanu
python3 -P -m ummanu issue show --ref issue:123
python3 -P -m ummanu issue update-priority --role po --ref issue:123 --priority P1 --reason REASON
python3 -P -m ummanu issue append --role po --ref issue:123 --reason REASON --body-file BLOCK.md
python3 -P -m ummanu issue close --role po --ref issue:123 --reason resolved
```

Who writes what:

| Verb | `po` | `observer` |
| --- | --- | --- |
| `product create` | yes | not offered |
| `issue create` | yes, any product (`--product` required) | yes, under the [identity guard](#the-sprint-guard), for its sprint's product |
| `issue update-priority`, `issue append` | yes | `role_forbidden` |
| `issue close` | yes | `role_forbidden` (an observer's `sprint close` closes its sprint's declared issues on its verdicts, in its name) |
| `task move`/`task edit` of a Product or Issue | `transition_forbidden` | `role_forbidden` |

An observer's `issue create` needs a head bound to a sprint: an unbound head is refused as
`observer_identity_unbound`, audited as `sprint_guard_denied`, and nothing is written. The issue takes
the product of that sprint (`--product` may be left out; another product is `validation`), and its
`entity.created` event carries `actor: {role: observer, id: <actor>}` and the sprint ref in
`related_refs` beside the product. Every write records the role and actor it was made with.

`--actor` defaults to `$BOARD_ACTOR` and never to the role. Every role head's environment names it:
`role_env` exports `BOARD_ACTOR` for each board role (the observer, steward and retro heads are their
role, a worker or reviewer head the profile the dispatcher launched), and a PO service turn writes as
`po`. A Product or Issue write with neither `--actor` nor `BOARD_ACTOR` is refused as `actor_required`,
exit 2, before anything is read.

A Product id is stable and unique; its non-empty project set may contain only ids registered under
the instance `projects/` directory. Every new issue requires its Product, one kind (`bug`, `feature`,
`question`, `improvement`) and one priority (`P0`–`P3`). A priority change requires a non-empty
reason, adds an `[issue:priority]` board comment and a durable audit event. Only the PO closes an
issue with `issue close`, with exactly one of `resolved`, `invalid`, `duplicate` or `wont_do`; closure
archives the backend record and keeps comments and audit available through `issue show --ref` and
checkpoint recovery. `sprint close` closes an issue through this same lifecycle when its decisions file gives
one of those verdicts; it never closes an issue merely because a sprint that declared it ended, and
never records somebody else's close as its own verdict. `issue list --closed` includes open and
closed issues; without it only open issues are listed.

`issue append` is the only change to an issue description after create, and it only adds: the
`--body-file` block goes after the unchanged current text, under a `---` rule and an
`[issue:appended <UTC time> by <actor>]` line. PO-only, non-empty reason and block; a closed issue
refuses it. It writes one `entity.updated` event carrying `data.append` with `body_sha256` (the block
as given), `description_sha256_was` and `description_sha256`. A repeat of the same `--request-id` with
the same ref, reason and block (matched by `body_sha256`) is answered without a second block or event;
the same id with anything else is `validation`.

Products and Issues never enter the execution columns: `move` and `claim` reject one before any write.
Work on an issue is a separate card the PO creates in Ready.

Every Product and Issue row is created in its product's lane: the active swimlane named exactly after
the product id, created on demand. An Issue uses `issue_product`, a Product its own id. Board lane
order, first lane, `Default swimlane` and the product's project bindings take no part. Execution cards
stay in their project's lane.

Rows placed otherwise (pre-existing, or restored into their checkpoint lane) are repaired by one
idempotent command that plans by default:

```bash
python3 -P -m ummanu product reconcile-lanes
python3 -P -m ummanu product reconcile-lanes --apply
```

The plan writes nothing and reports each misplaced row with its current and target lane, the lanes to
create and a per-product summary. `--apply` performs exactly those moves, using the same
`product_swimlane_id` every writer uses. A move is one `moveTaskPosition` into another lane of the same
column, keeping reference, metadata, comments, column and open/closed state; moved rows land after the
destination lane's rows, in plan order. A board already in order moves nothing; an interrupted run is
continued by the next one. Closed records are counted, not moved. A record whose product is unstated or
not a registered Product is listed for a human, never guessed; a closed unresolved row is both counted
as closed and listed as unresolved.

Every Product and Issue write is staged before it touches the backend, and an unfinished staged write
blocks checkpoint and board export. A backend-declined create is reported as `backend_rejected`, and
once the board shows no row for that request the staged document is dropped. `validation` and `closed`
refusals before the first backend write also drop it.

A staged write that reached the backend stays under its request id. Repair:

```bash
python3 -P -m ummanu product transaction list
python3 -P -m ummanu product transaction retry --request-id REQUEST_ID
python3 -P -m ummanu product transaction discard --request-id REQUEST_ID
python3 -P -m ummanu product transaction adopt --path FILE
```

`list` includes typed Product/Issue pending events (read-only: request id, event kind, subject ref) and
released transaction documents. `retry` finishes the staged operation where it stopped and commits its
one audit event; an already committed request is answered with its record. `discard` drops a released
transaction only after reading the board: a create whose row exists, or a priority/close change whose
board comment exists, is refused as `live_write` and must be retried. A typed pending event is never
discarded (`live_write`; repaired by retry). `adopt` files a released transaction document from outside
the journal back under its request id. One journal covers Product and Issue writes. Runbook:
[Operations](OPERATIONS.md#a-checkpoint-blocked-by-a-productissue-transaction).

## Sprints

A sprint is a data entity on a separate `Ummanu sprints` board: one board task per sprint, board
created lazily and idempotently. References have the form `sprint:ID`, separate from `PROJECT-N`.

```bash
python3 -P -m ummanu sprint create --role po --goal GOAL --dod-file DOD.md \
  --product PRODUCT_ID --issue issue:ID --project PROJECT_ID \
  --observer HEAD_PROFILE --repository REPO --request-id REQUEST_ID \
  [--worker HEAD_PROFILE] [--reviewer HEAD_PROFILE] \
  [--po-session SESSION_ID] [--allow-production PROJECT_ID ...] [--e2e-budget N] \
  [--local-run-exceptions-file EXCEPTIONS.json]
python3 -P -m ummanu sprint list --status open
python3 -P -m ummanu sprint show --ref sprint:ID
python3 -P -m ummanu sprint status --ref sprint:ID
python3 -P -m ummanu sprint comment --role worker --ref sprint:ID --body-file NOTE.md
python3 -P -m ummanu sprint current-task --role dispatcher --ref sprint:ID --task PROJECT-N
python3 -P -m ummanu sprint budget --role dispatcher --ref sprint:ID --type red_ci
python3 -P -m ummanu sprint resume --role observer --ref sprint:ID --body-file RESUME.json
python3 -P -m ummanu sprint reopen --role po --ref sprint:ID --observer HEAD_PROFILE
python3 -P -m ummanu sprint allow-production --role po --ref sprint:ID --project PROJECT_ID --reason WHY
python3 -P -m ummanu sprint e2e-budget --role po --ref sprint:ID --authorized-by EVENT_ID [--add N]
python3 -P -m ummanu sprint close --role po --ref sprint:ID --reason WHY \
  --decisions-file DECISIONS.yaml --closeout-file CLOSEOUT.md
python3 -P -m ummanu sprint close-result --ref sprint:ID --event-id evt_ID
```

### Control-host local runs

Worker and reviewer packets permit only the project's adapter-declared broad check and subsets
locally. Integration shards, Docker/container runs, stands, provisioning and network-heavy checks
run in CI only, except for exact vectors in the sprint's creation-only `local_run_exceptions` field.
Development convenience, an acceptance criterion and missing/none/noop gate receipts grant no
additional authority. An excessive local heavy run is a non-blocking observation, never grounds
for RED. Exclude its results from validation evidence, even if tests passed; CI or an allowed
local check supplies that evidence. Review verdicts depend on code and valid evidence, and
reviewers follow these bounds for their own verification. The observer does not order rework or
charge the budget for such a run alone. Historical verdicts remain in the audit and are not
reopened. Missing required valid evidence or a code defect can still block release. Receipt reuse
and mandatory dispatcher-owned exact-SHA CI gates still apply.

The shared worker/reviewer [Docker guard](HEAD_RUNTIME.md#worker-and-reviewer-docker-guard)
refuses `run`, `create`, `build` and `compose up|run|build` unless the current card's project-scoped
sprint snapshot grants `["docker", *original_arguments]` by exact string-vector equality before
any parsing or translation. Inherited policy and `runtime.env` grant nothing. Cleanup ownership
checks remain mandatory even with an exact exception. Tests/broad must not require local Docker;
report a declared broad-suite dependency on Docker on the card instead of weakening the guard.

Create with `--local-run-exceptions-file EXCEPTIONS.json`, or pass `local_run_exceptions` to
`SprintWriter.create` or `SprintOperationLayer.sprint_create`. The web creation form accepts the
same JSON list. The default is `[]`, including released sprints and staged requests missing the
field. An entry has exactly three keys:

```json
[{"project":"ummanu","argv":["python3","-m","tests.integration","--case","two words"],"rationale":"Owner requires this exact local probe for this sprint"}]
```

`project` must be registered and reserved by the sprint. `argv` is a nonempty list of strings,
starting with a nonempty executable; every argument boundary, including an empty argument, is
preserved. `rationale` is nonempty text. Control characters are refused in all strings. Each
entry grants only its exact vector, with no shell parsing, wildcard or inferred suite. Nonempty
declarations are stored, audited and included in request identity: changing them under the same
request id is refused. Empty defaults are omitted from request identity to preserve old retries.
There is no post-creation mutation verb. Snapshots and restores preserve declarations.

Both packet types render only entries for that card's sprint and project, and literal `none`
otherwise. Malformed state or failed sprint reads authorize no exception. Card text, DoD prose,
sprint comments and a head's judgement never grant one. A missing adapter module is a configuration
gap: report the gap and evidence obtainable through CI or declared exceptions; workers cannot
choose a replacement module, invent a command or run repository-wide discovery.

The roles each sprint write admits:

| Verb | Roles |
| --- | --- |
| `sprint create` | `po`, `steward` |
| `sprint comment` | `po`, `dispatcher`, `worker`, `reviewer`, `observer`, `steward`, `retro` |
| `sprint current-task`, `sprint resume` | `po`, `dispatcher`, `observer`, `steward` |
| `sprint budget` | `po`, `dispatcher`, `steward` |
| `sprint reopen` | `po` |
| `sprint e2e-budget` | `po`, with the owner's comment as `--authorized-by` ([The e2e run budget](#the-e2e-run-budget)) |
| `sprint close` | `po` (any sprint), `observer` (its own sprint) |

A write of role `observer` passes the [identity guard](#the-sprint-guard) first: it must name the sprint
the head was launched for. A comment is stored under its role's marker, `[observer]` for the observer's;
it does not wake the observer ([Resume and observer wakes](#resume-and-observer-wakes)). An observer's
close takes the same `--reason`, `--decisions-file` and `--closeout-file` as the PO's, and every step it
takes (archive, issue close, card disposition) is written with role `observer` and the observer's actor;
the observer's disposition moves need no sprint override. Before anything is staged, every disposition
move the plan would make is checked against the Card transition table for the closing role; a close with
a move its role may not make (for the observer, any card still in Assessment) is refused whole as
`close_plan_forbidden` (exit 3, `owner_conflict` in the operation layer), naming each card and its
column, and writes nothing. The observer decides such a card with `task decide` before it closes.

A write whose role is `po` and whose actor is `observer` is refused as `role_masquerade` (exit 3), with a
message naming `--role observer`, before anything is read or written. It is one check,
`ummanu.tasks.admit_role`, the role admission every role-taking sprint, task and issue write makes
first, so it covers every verb: `sprint` comment, close and the other sprint writes, every `task` write,
`product create` and every `issue` write. The operation layer carries the code through unchanged, as
it does `observer_identity_unbound` and `observer_sprint_mismatch` (exit 3 from the CLI, 403 over
HTTP). No other role or actor is affected.

Stored fields: goal, Definition of Done text, repositories, owning product, its issues, reserved
projects, `open`/`closed`/`stopped` status, declared observer, optional worker and reviewer pins, the PO
session and allowed productions
([The sprint's PO session and productions](#the-sprints-po-session-and-productions)), a budget counter by
event type, current card and a structured resume entry.

The six charged budget event types are `red_review`, `blocked`, `red_ci`, `preempt`, `recreated_task`
and `hotfix`. Production derives them from durable card audit events: a red review, a move to Blocked,
a red mechanical gate, a preempt of an active card back to Ready, or a tagged recreation or hotfix
creation. The card-event id is the budget request id, so a repeated tick cannot charge twice. Green
cards and observer activity do not move the counter. One further type, `infrastructure_blocked`, is
recorded in its own field and never charged; see [Bring-up outcomes](#bring-up-outcomes).

A new sprint belongs to a Product, serves at least one of its open Issues and reserves at least one
registered project: `--product` names an existing Product, every `--issue` is an open Issue of that
Product, every `--project` is an id from the instance project registry. `--repository` is the
write-guard scope; a repository root is canonicalized when declared (absolute path persisted; an
unresolvable root is refused with the value). An Issue of another Product and a closed Issue are
refused with their own messages. Conflict and limit rules are in
[The open-sprint limit](#the-open-sprint-limit). Every check is a read, so a refused sprint leaves no
board row, metadata or audit event. A repeated `--request-id` returns the first event.

`create`, `reopen` and `close` hold one exclusive lock on the data directory
(`sprints/admission.lock`) across check and write. The lock holds no sprint state.

Admission order for `create` and `reopen` (both on the staged-intent journal):

1. take the admission lock;
2. settle the request id: a committed or staged intent with the same id is returned as is, before any
   live-state check; the same id with a different payload is `validation`;
3. for a fresh request only, check product, issues, registry and both conflict rules (a staged intent
   resumes on the state it was admitted on);
4. apply backend steps through the staged intent, each recognising what an earlier attempt did; a
   metadata answer other than `True` is a backend refusal and the call reports `audit_pending` (the
   staged intent stays; retry with the same request id);
5. commit one audit event.

A caller-supplied sprint reference is used as given; otherwise `sprint:N` is allocated from the
sprint board's high-water mark over open and archived rows, with the same claim check as a card, and
remembered so a repeat writes the same reference.

The sprint reference is written last and publishes the sprint: a sprint-board row counts as a sprint
only once it carries one, so an interrupted create is never observed as an open sprint.

A step refused after the row was created takes the row back; only if that also fails does the row stay
for repair. Before publishing, a resumed create or reopen re-checks both conflict rules; if another
sprint took the slot or project meanwhile, it is refused as `sprint_conflict` or `resource_conflict`
naming that sprint and publishes nothing — unless the row cannot be taken back or a refused `reopen`
cannot restore its observer preimage, in which case the answer is `audit_pending` and the caller
retries under the same request id until cleanup succeeds, then files the request again as a fresh one.
An unfinished sprint create blocks the checkpoint until retried or dropped.

Sprints without ownership fields stay readable, exportable and restorable; `show`, `status`, the board
export and the checkpoint omit the three fields rather than answering `""`/`[]`. `reopen` re-checks
every rule, so such a sprint is refused naming what it lacks (open a new sprint instead). `reopen` is
also refused when the sprint's issues have closed or its projects are held elsewhere.

`sprint close` freezes the active cards linked to the sprint, archives its terminal Done tasks with
the normal archive audit, and returns that list. The Done transition clears the completed worker claim
and resolved routing fields so terminal archival is not blocked; `archive` still refuses a live claim.
Cards without that `sprint_ref` are not considered. Product and Issue records are never closure
targets, even if malformed metadata links one to the sprint. The close request is staged: retrying the
same request id after a lost archive, issue close, disposition or status reply resumes the same task
set, repeats none of those writes and records one close event; a retry stating other decisions is
refused. Sprints without reservations are closed without archiving cards and declare no issues, so they
need no decisions.

### The decisions a close carries

A close states what became of every declared issue and every card still in a working state:

```yaml
issues:
  - ref: issue:e6e8c24e9de7a7cad54b
    verdict: resolved          # resolved | invalid | duplicate | wont_do close the issue; open keeps it
    reason: the nudge fix landed in this sprint
  - ref: issue:32a78b7822bb013ef99a
    verdict: already_closed    # somebody else closed it; name what they closed it as
    actual: duplicate
    reason: another PO closed it as a duplicate while this sprint ran
cards:
  - ref: secretary-1400
    verdict: drop              # done | drop
    reason: superseded by the next sprint's cut
  - ref: secretary-1401
    verdict: already_moved     # somebody else took it there; name the state it is in
    actual: ready
    reason: its own head put it back in Ready before the close got to it
```

Both sections are optional in the file; neither is optional in the close. Every declared issue needs a
verdict and every card not in Done needs a disposition; a close short of one is refused with
`validation` before the transaction opens, naming the undecided issues and the cards with their states,
and writes nothing. Also refused that way: an unknown ref, a ref decided twice, an unknown verdict, an
empty reason, an unknown field or section, a non-string key (`1: x`), an unparsable file. `actual` is
required by the two confirmations and refused on every other decision.

A closing verdict closes the issue through the `issue close` lifecycle with that reason. `open` writes
nothing to the issue; the close event carries the basis.

A disposition ends with the card archived. `done` moves it to Done; `drop` moves it through Ready (the
edge that releases a retained worker). Both moves carry the disposition's reason as the card comment,
and the archive carries it again. A card whose dispatcher work is still live is refused with
`live_work`, naming it; settle the head first.

`closed` is published as the last step. The terminal phase order is the verdicts on the declared issues, the archival of the Done cards, the dispositions, the knowledge closeout, then the
status, then the reserved-project index and completion of the transaction. An interrupted close leaves
the sprint open and still reserving its projects, so no successor can be created on them until the
retry finishes. Because dispositions move cards of a still-open sprint, the close carries the guard's
own `sprint_override` with reason `disposed by the close of <sprint-ref>: <the disposition's reason>`.

A terminal refusal discards the staged close only while nothing has been written. From the first issue
write onward (the marker is durable before that write) a refusal is `audit_pending`, the staged plan
stays, and a retry stating other decisions is still refused.

Every step — issue verdict, archival, disposition move and its archival — runs under a request id
derived from the close's, and that id's committed event is the only proof the step happened. A pending
event under it means the backend effect landed and the journal write did not; the close drives the
same id again and does not finish while it is pending. Observed object state is never proof. Whether a
disposition needs its move is read from the state frozen into the close's plan.

An object changed under a close with no committed step of this close to account for it is settled by
explicit decision:

- **Before any write.** Preflight reads every declared issue. A closing verdict, or `open`, for an
  issue somebody else already closed is refused with `validation`, naming each ref and its reason. The
  closer confirms with `already_closed` and that reason in `actual`. Confirming an open issue, or naming
  a reason the issue does not carry, is refused the same way.
- **In flight** (preflight passed, writes begun). The close stops with `close_conflict`, naming the
  ref, the stated verdict and the actual fact, and records the conflict on its transaction. A retry
  under the same request id may amend exactly those refs to the matching confirmation
  (`already_closed` with the issue's reason, `already_moved` with the card's state) and nothing else.

A confirmation writes nothing to its object. `already_closed` leaves the issue's reason;
`already_moved` skips the move and archives the card where it stands, accepted only for `done` or
`ready`. Both are recorded in the close event with their prose.

#### The closeout a close writes

A close writes one knowledge document as a step of the close, into `state/knowledge` through
`write_knowledge_document`. Its path, `closeouts/<day>-<sprint-ref>.md`, uses the day the close was
staged and is frozen into the staged plan, so a retry on a later day writes the same document.

The step runs under a derived request id like every other terminal step; a retry skips it when
committed and re-drives the identical write when pending. Exactly one document results. It runs before
the status, so a sprint that reads `closed` has its closeout.

The account of what was achieved, what is unfinished and the owner's decision about the remainder is
supplied by the caller and carried verbatim. The close adds what only it knows: the sprint, who closed
it and why, the verdict on every declared issue and the disposition of every card not done.

A close is not a completed Definition of Done. The closeout carries that sentence; a close answer
carries `definition_of_done` with `satisfied: false` only. Closing a sprint states what became of its
work; it is not a statement that the sprint's Definition of Done was reached.

A close made with no closeout writes none. The protocol operation requires one
([Closing one](#closing-one)); the writer takes it as an option for recovery and internal callers.

#### A comment on a sprint that has ended

A closed or stopped sprint accepts no current task and no observer resume. A comment is accepted. It
does not change status, reopen the sprint, restore a reservation, or wake or launch a head (the tick
stops the observer of a non-open sprint and drops its record). It is idempotent on `request_id` like
every sprint write. Its delivery reads `not_deliverable`.

What `SprintWriter._write` answers for every sprint write it handles when the sprint is `closed` or
`stopped` (the complete set; pinned by a test that derives the kinds from `SprintWriter`):

| sprint write | a `closed` or `stopped` sprint |
| --- | --- |
| `budget_recorded` | `accepted` |
| `commented` | `accepted` |
| `current_task_set` | `refused` — `closed`, exit status `3` |
| `e2e_budget_raised` | `refused` — `closed`, exit status `3` |
| `po_session_set` | `accepted` |
| `production_allowed` | `refused` — `closed`, exit status `3` |
| `restored` | `accepted` |
| `resume_recorded` | `refused` — `closed`, exit status `3` |

A late budget charge updates the totals `show` reports but cannot stop the sprint again or change its
status (the hard-limit edge is taken only from `open`). A restore rebuilds fields from a backup and is
accepted on any status. `po_session_set` is written only by the PO service's resolver, which refuses to
start a re-seed on a sprint that is not open; a repeat that finishes one begun while the sprint was open
still records its session.

### Budget

Installation config may set `sprint_budget.signal` and `sprint_budget.hard`; defaults 3 and 6. Omitted
values resolve to defaults before a hard limit below the signal limit is rejected. Each charge is a
`budget_recorded` audit event; the charge that stops a sprint is paired with a `budget_hard_stopped`
event carrying the hard limit and the triggering card-event identity. `show` returns thresholds and
`signal_reached`/`hard_reached` with totals. The signal appears in a newly launched observer prompt
and does not stop work. At the hard limit the dispatcher marks the sprint `stopped`, stops its
observer and skips new linked claims; active cards continue their cycle. Only
`sprint reopen --role po` clears the stop.

This budget counts restarts. The e2e run budget, which counts paid e2e runs, is separate:
[The e2e run budget](#the-e2e-run-budget).

### Resume and observer wakes

`sprint resume` accepts JSON with required string fields `selected_step`, `selected_why`,
`rejected_alternatives`, `current_task`, `dod_state` and `next_safe_step`. It is stored apart from
normal comments with a `[sprint:resume]` marker, as a concise semantic delta rather than a copy of
machine telemetry. For an open sprint, `show` and `status` compute freshness only from semantic
observer work: a card entering Assessment, Blocked or Done (except a Done whose release merged, see
[Post-merge CI](#post-merge-ci)); a post-merge CI result; a budget event; or a PO sprint comment.
Claims, reports, Validate moves, reviewer launches, routing and observer-authored events do not age a
resume. Missing data is `resume_missing`; a semantic transition may trail its resume for up to five
minutes, then `resume_stale`.

A closed or stopped sprint records no resume and takes no current task, so its freshness is read from
its row's last record alone. A post-close comment is accepted
([A comment on a sprint that has ended](#a-comment-on-a-sprint-that-has-ended)), and a terminal sprint's freshness never reads the audit the comment is recorded in. `reopen`
restores the ordinary comparison. A sprint summary reads the committed audit at most once per
operation, and not at all when every sprint has ended. Neither command reads an observer transcript.

The dispatcher records a durable delivery batch before it wakes or replaces an observer, coalesces
pending semantic events to one high-water mark, and owns all waiting for workers, reviewers and CI. An
observer acknowledges by passing the `--delivery-id` and `--through-event` from `status` to
`sprint resume` (audit payload, not resume fields). `ummanu status --json` exposes the same state
for every sprint in `installation.sprints.items` (stopped status and reason, budget, resume freshness,
observer state); an unreadable live board is reported in `installation.sprints.error`.

Only these open observer work: semantic card edges (Assessment, Blocked, Done), a post-merge CI
result, an eligible human control-plane return to Issues, sprint budget events and PO sprint comments.
Claims, routing, reports, validation telemetry and observer-authored writes do not. Delivery records only that the head took the
prompt; the next ordinary reconciliation reads one durable audit snapshot and closes the batch only on
the matching resume. Delivery never polls the board or calls an observer-facing `Monitor` command.

#### Post-merge CI

After a release, the observer is woken on the post-merge CI result, not on the merge. When a release
merges a card (either merge path: the GitHub pull request or the fast-forward push of a local-CI
project), the dispatcher records a post-merge watch in its own
production state (`post_merge_watches`: card ref, project, integration base, the commit that actually
landed on the base, start time) before the Done move, and the Done carries a `release_merge` marker.
The watch survives a restart and holds no claim: the card is Done and is not active for claims or for
the sprint's one-card rule.

Each tick reads the base's CI for that exact commit (every check-runs page and the combined status) and
judges it in one place, `post_merge.read_merge_ci`, with the gate's rollup narrowed to
`validation.required_checks` when declared. The result is terminal only for a reading that is complete
and exact: every answer a well-formed JSON object of the expected shape, every check run and the status
naming the full merge commit, the check runs read across all pages adding up to `total_count`, and
every required check present. The watch resolves to exactly one of:

- `green` — every selected check finished successfully;
- `red` — at least one failed; the result carries the run ids, the failed check names and the gate's
  classification (`infrastructure` only when every failed run read is infrastructure, else `product`);
- `absent` — the project validates without GitHub CI, or no workflow triggers on a push to the base
  (the gate's trigger analysis, read off the merged checkout); decided at once, without waiting;
- `timeout` — no terminal result within `UMMANU_POST_MERGE_CI_CEILING_SECONDS`, default 3600 (one
  hour).

If any one condition fails, the whole reading is pending, whatever the other answer says: a transport
error, an unreadable or malformed `gh` answer, a partial page set, a check for another commit, a
completed check without a conclusion, a run that has not started. Pending lasts until the ceiling and
then resolves `timeout`; none of it is ever `green`. A required check that runs only on pull requests therefore holds the watch to the
ceiling.

The result is written to the watch first and then published once, under fixed request ids: one
dispatcher card event (a comment carrying the `post_merge_ci` payload) and, for a sprint card, one
dispatcher comment on the sprint naming the card, the full 40-hex merge commit, result and runs. A replay after a crash
republishes the same fact and writes nothing new. The one enforcement place is the observer
significance predicate (`tasks.is_significant_card_event`): the Done of a release that merged is not a
wake, the post-merge result is. A Done with no merge (research and infra cards, a decision or
operation card the PO completed, a release that merged nothing, automerge off, a manual PO or steward
Done) wakes as before. The wake text states the result
with its runs, and for `red` the failed checks and classification; a red result is never worded as a
plain Done. A `green` result of a project whose e2e stage is declared `placement: after_merge` also
queues the card for that project's next after-merge e2e run ([After merge](#after-merge)); such a watch
is dropped only once the card is queued, or its project is established not to be `after_merge`.

An Assessment entry is one decision visit. The first observer `task decide` is canonical for that
visit; a redelivered turn repeating the same kind returns that decision without another comment, and
a different kind is refused until the card enters Assessment again. The decision transaction (fresh
state read, visit resolution, staged board/audit write) is serialized per card.

### The open-sprint limit

The instance setting `open_sprint_limit` is `1` or `2`; absent means `1`. A value the schema refuses
fails closed to `1` (never widening the limit or stopping admission), and `validate_instance` reports
an `open_sprint_limit` finding. The limit is read from installation config at admission time, so
changes need no restart; an unreadable config answers `1`.

This section is the single statement of what admission checks. Each check reads live state before the
first board write, in this order:

1. **disjoint project reservations**, at either limit: a project another open sprint reserves is
   refused, naming the project and holder;
2. **a different product**, at limit 2 only: two open sprints may not share the owning Product. A
   sprint with no product cannot be proven disjoint and is refused, whichever side it is on (the
   candidate is judged on its own value first);
3. **non-overlapping canonical repository roots**, at limit 2 only, compared as absolute resolved
   paths; nesting counts as overlap. A stored root that is not absolute is refused on either side, not
   resolved at check time. The candidate's own roots are judged before any pairwise comparison, whether
   or not another sprint is open;
4. **the count**, at either limit: the installation already holds as many open sprints as it may.

Resource collisions are reported before the count, including at the limit, because a resource refusal
names the sprint holding it. At limit 1 only checks 1 and 4 run.

`create`, `reopen` and restore hold the same invariants under `sprints/admission.lock`. Restore judges
the exported open sprints as a set: once, before the first backend write of either set, admitting rows
one at a time in reference order against the rows already accepted, at the *target* installation's
limit, with the whole restore inside the admission lock.

### The declared observer

A sprint carries exactly one observer value in `sprint_observer`. There is no default, no inheritance
from `role_defaults.observer` and no missing-field fallback. Four tagged forms exist; only the first two
are executable:

| form | meaning |
| --- | --- |
| `{"kind": "head", "profile": "claude-opus-high"}` | the sprint is observed by that one head profile |
| `{"kind": "none"}` | the sprint runs without an observer |
| `{"kind": "historical", "profile": HEAD, "source": "observer_lifecycle_audit", "event_id": EVT}` | a closed row whose head was recovered from durable lifecycle events |
| `{"kind": "historical", "profile": null, "source": "migration_unknown"}` | a closed row that never launched an observer |

A `historical` value is provenance, not a declaration; an open sprint carrying one is corrupt.

`create` and `reopen` require `--observer`: `none` or one head profile from `heads.yaml` (absent, null,
empty, `default` and `inherited` are refused). A named profile is resolved against this installation's
head registry at create, reopen and the restore preflight; a sprint is never opened, reopened or
republished on a head that does not exist. Registry drift after declaration is handled by the fence.
`reopen` writes the new choice while the sprint is still closed and then changes status; `create`
writes it before the reference publishes the row.

The reader is strict: an open sprint whose observer metadata is missing, unreadable, historical, or
names a profile the registry lacks is corrupt. It is not launched from `role_defaults.observer`, its
cards do not move, and it does not become observer-free.

Restore validates the whole exported set, cards included, before the first backend write, and refuses
rather than publishing part of it. Every exported row, closed rows included, must carry a value; a row
without one is named and refused. Repair: declare the value on that row in the export's
`state/board/sprints.json` and restore again. The same repair applies to an open row whose declared head
left the registry.

### The optional executor pins

A sprint may fix the worker profile (`sprint_worker`) and the reviewer profile (`sprint_reviewer`)
its cards run on. Each is independent:

| state | meaning |
| --- | --- |
| the field is absent | the owner pinned no profile for that role; the sprint's observer chooses one per card under the current rules |
| the field holds a profile name | every card of this sprint runs that role on exactly that profile |
| the field holds anything else | corruption, reported as such; it is never read as "pinned nothing" |

`sprint show` always answers both roles as `{"state": "unset"}` or
`{"state": "pinned", "profile": HEAD}`; `sprint status` carries the same beside the observer state. No
read substitutes `role_defaults`, an empty string or `null`, and no pin is inferred from cards.

`none` and the empty string are refused at `sprint create`; a role is left unpinned by omitting its
option. A named profile is resolved against the head registry at `sprint create`, before any row,
field or audit event; an unknown profile or unreadable registry is refused. `sprint reopen` does not
restate pins; they survive a close.

The observer launch document prints both roles in an `## Executors` section: the pinned profile, or
that the owner fixed none and the observer chooses per card.

Every write of a card's `head` or `review_head` — `task create` (first, later or recreated card) and
`task edit` — goes through one check: it writes the pinned profile when the caller names none (including
an edit that would clear the field), and refuses a different profile with `sprint_executor_pinned`,
naming the sprint and pinned profile. An unreadable pin refuses both with `sprint_executor_unreadable`.
The dispatcher's `resolved_head` at claim is not a separate write path: it launches the profile the
card declares. A sprint that pins nothing adds no check.

Pins travel the recovery path: the normalized export carries a key per role only where pinned, absence
survives the round trip, both fields are compared by sprint parity, and an exported key that is not a
profile name stops restore in preflight before the first backend write.

### The sprint's PO session and productions

Two fields a sprint records at `sprint create` (board-store revision `0016`, columns
`sprints.po_session` and `sprints.allowed_productions`):

| field | set by | meaning |
| --- | --- | --- |
| `po_session` | `--po-session SESSION_ID`, default `$UMMANU_PO_SESSION` | the PO session that opened the sprint; `null` when neither is given (a `--role steward` create outside a PO turn) |
| `allowed_productions` | `--allow-production PROJECT_ID`, repeatable | registered projects whose production the sprint's operations may touch; empty by default |

The PO service gives every PO turn `UMMANU_PO_SESSION=<session_id>` (new turns, re-runs and
relaunches alike), so a `sprint create` inside a PO turn records its session with no flag. Beside it,
`UMMANU_PO_REQUEST=<request id>` names the request id of the input the turn answers (read from the
PO store, so a re-run names the same one; unset when the input carried none), which a card created in
the turn records ([PO delegation](#po-delegation)). A session id
that is not an existing, open PO session is refused (`validation`), and so is an `--allow-production`
that is not a project of the instance registry. Nothing is inferred: no production is allowed that was
not named, the sprint's own reserved projects included. Both checks are reads made with the ownership
checks, for a fresh request only, so a refused create writes nothing. Both fields are inputs of the
request: the staged intent carries each only when set, so a repeat with a different value is
`validation` and an intent staged before them replays unchanged.

`sprint show`, `sprint status`, `sprint list` and the sprint protocol documents carry `po_session` (a
session id or `null`) and `allowed_productions` (a list); a sprint opened before `0016` reads `null` and
`[]`. The observer launch document prints both, as `## PO session` and `## Allowed productions`. The
checkpoint export carries each only where set, restore writes it back, and sprint parity compares both.
Neither is changed after create except `po_session` by the PO service's resolver below (the audit kind
of that write is `po_session_set`, role `po`, actor `po-service`), and `allowed_productions`, which the
PO only extends with `sprint allow-production` ([Production rights](#production-rights), audit kind
`production_allowed`).

**The resolver.** `sprint_session(sprint_ref, request_id)` is an operation of the PO service
(`ummanu.po.client.PoServiceClient.sprint_session`, [Operations](OPERATIONS.md#the-po-service)). It
answers `{session_id, created, repeated}`:

- the recorded `po_session` exists and is open: that session, `created: false`, nothing written;
- it is `null`, missing from the store or closed: a fresh session, `created: true`. It takes the recorded
  session's CLI, model and effort when that row exists, else the new-session form's preselection (the
  first CLI offering a model in `po.models`, its first model). Its effort is never `default`: a recorded
  session's effort is reused unless it is `default` or no longer offered for that CLI in `po.efforts`,
  and then, as with no recorded session, it opens at the first effort offered for the chosen CLI. A CLI
  that offers no effort is refused (`validation`) and nothing is opened. Its first input is a
  seeding message naming the sprint and why the session was opened, quoting the sprint's why-document —
  the one `state/knowledge/decisions/*.md` of the live root that names the sprint ref as a whole
  word; with none or several it says so and lists the paths — and telling the head to read `NOTES.md`
  in its workspace. The sprint gets a comment (role `po`, actor `po-service`): `the PO session <old or
  none> no longer exists; opened <new> seeded with <why-doc path | no why-document found> and NOTES.md`,
  and then records the new session as its `po_session`.

The request id is bound to the sprint (`po_sprint_session` in `po_requests`, an operation the CHECK
`po_request_operation_in_vocabulary` admits since `0016`; reserved by `PoService._reserve` like every
other id). A repeat of a resolve that created a session answers the same
session (`repeated: true`) and creates nothing; one that failed after the session was claimed answered
`outcome_unknown`, and its repeat finishes the seed, comment and record, each under its own id derived
from the request id. Resolves are serialized under the service lock and read the sprint's session inside
it, so two resolves of one sprint with different ids open one session. An unknown sprint and a sprint
that is not open are refused (`validation`, nothing written).

### The observer fence

The production tick checks every open sprint's observer before it reconciles records, advances active
cards or claims Ready. The check reads the sprint board, head registry and observer records and
launches nothing.

A sprint is fenced when its declared head has not been launched, is dead, does not match the running
record, is parked behind a failed bring-up, or its declaration is corrupt. Fencing is project-local: it
excludes that sprint's reserved projects and linked cards from reconciliation, advancement and Ready
claims; other projects run. `{"kind": "none"}` passes with no launch and no probe.

An unreadable sprint board fences. Each successful pass records every open sprint's reservations in
production state; a pass that cannot read the board fences from that snapshot plus every card whose
metadata names a sprint. Cards in no sprint keep running. Known gap: a sprint admitted after the last
successful pass is in neither source, so a card in a project it reserves that does not name the sprint
can advance or be claimed until the next pass that reads the sprint board.

The fence writes one durable `observer_fence_raised` event with `outcome: critical` per reason
(`deferred` under a drain or freeze while the launch only waits for the resume: no record yet, or the
drain's own deferral; the cards stay fenced and the tick is not degraded), and
`observer_fence_cleared` once adoption is confirmed: a record for that sprint naming exactly the
declared profile, with a live pid on disk. An unwritten pid does not clear it, so clearing normally
happens on a later tick than the launch.

If the fence check raises (e.g. its critical outcome cannot be staged on a full or unwritable volume),
the tick returns `observer-fence-unavailable` and runs no reconciliation, advancement, budget
accounting, observer reconciliation or Ready claim.

### The sprint guard

`task create --sprint` records the sprint reference in card metadata; `task show` and `task list`
expose it as `sprint`, and `task list --sprint` filters by it. `sprint show` derives `cards` from live
card metadata. New links are refused after a sprint is closed. `current-task` requires that the card
already carries this sprint reference.

An open sprint holds every project in its `reservations`: only its observer may create a card of the
sprint there, and only with `--sprint` naming that sprint. Observer and dispatcher may move and edit
linked cards. The PO may create a card linked to the holding sprint, and move or edit a card linked to
it, only with `--sprint-override` plus a non-empty `--sprint-override-reason-file` (the reason is stored
as its own audit field). Without it the PO gets `sprint_write_forbidden`, as do retro, steward and
every other role; the refusal names the holding sprint.

A PO create, move or edit of a card linked to no sprint (and a create that links none) is not refused
because its project is reserved, for every kind and without an override; running it is decided at
[admission](#cards-outside-a-sprint). This narrowing is inside the one guard every `create`, `move`
(including a replayed generic move) and `edit` passes, after the index is verified, so an index that
cannot be verified still refuses the write as `sprint_guard_unavailable`. An override passed anyway is
granted and audited as before.

Both guard answers are audited once per request id: a refusal as `sprint_guard_denied`; a granted
override as `sprint_guard_override` carrying project, holding sprint, override reason and the request id
of the authorized operation. Each has its own derived request id, and the grant is written before the
operation stages or effects anything; an override that could not be recorded does not happen. The
grant record is separate from the operation's own record, with the same shape across `move`, `create`
and `edit`.

A write with role `observer` is authenticated against the sprint it names first. The dispatcher binds
`UMMANU_OBSERVER_SPRINT` and `UMMANU_OBSERVER_GENERATION` into the head's command line;
`runtime.env` cannot supply or replace them. A card linked to another sprint, or `sprint resume` /
`sprint current-task` naming another sprint, is refused as `observer_sprint_mismatch`; a head with no
binding is refused as `observer_identity_unbound`. Both are audited as `sprint_guard_denied` with that
code.

The check keys off the declared role. `sprint comment` and `sprint close` of role `observer` are guarded
the same way, and an observer's `issue create` must be bound to a sprint. `sprint reopen` and the budget
write take role `po` with no binding. A head that declares `--role po` in the observer's name is refused
as `role_masquerade` (see [Sprints](#sprints)); the observer closes its own sprint as `--role observer`,
audited with that role.

The index of projects held by open sprints is kept next to the audit log, keyed by project id; an index
in an older key space is rebuilt from the sprints board before it answers. A project outside any open
sprint triggers no sprints-board read. For a write into a held project the sprint is re-read live; an
unreachable board returns `sprint_guard_unavailable`. Closing or stopping a sprint releases the hold.

### Task writes and the audit

Sprint mutations share the board event log and pending-audit recovery with card mutations, carry the
sprint reference as `ref`, and a repeated `--request-id` returns the committed event.

Every write passes role guards and transition checks. A mutation first receives an append-only pending
audit event, is checked against the live board, and only then counts as committed. An unresolved
pending write blocks a consistent export and the recovery checkpoint until `reconcile-audit`.

Card state changes (`move`, `claim`) run through the board host and are recorded as typed protocol
events in `events.ndjson`: `record_type` is `board.protocol_event`, the edge is
`transition: {source, target}`, and the reason is text. Older generic `moved` and `claimed` rows stay
readable as written; every other operation (`report`, `verdict`, `decide`, `routing`, comments,
create/edit/archive) keeps its generic record. Readers of card transitions must handle both shapes,
distinguished by `record_type`. A request id recorded as a generic `move` or `claim` keeps that
operation: a retry replays that record and finishes its cleanup.

One staged occurrence has one outcome owner, `MutationEventTransaction`, and one window in which its
record may be discarded: the call that issues the single column operation. Everything before that call
(reading the card, re-authorizing the edge) is inside the window. Everything after is fail-closed,
including the confirming read: a read that times out or finds another writer's column keeps the exact
pending typed record.

Follow-up board work runs inside the same transaction, after the column effect is confirmed and before
the event commits: claim metadata for `claim`, the routing and retry reset on a move into Ready or
Done, clearing the review head on a move out of Validate, and the move's reason comment. A refused or
failed column move leaves nothing; a follow-up that does not land keeps the pending typed record.

Recovery re-reads the card and commits a pending typed transition's exact event only when the requested
target is live. It never repeats a column move and refuses an unproven, contradicted or gone effect,
leaving the pending record. Recomputable outstanding work (the Ready and Done reset) is finished first,
and the record is not closed while incomplete. A claim's metadata (worker id, resolved heads) is not
recomputable: recovery publishes the proven occurrence and leaves the missing claim to the dispatcher's
live claim check, which reports a controlled divergence instead of launching. Retrying the same request
id while the record is still pending also restores the metadata (no admission check or column move is
repeated); once `reconcile-audit` has published the occurrence, the retry answers from the committed
event and writes nothing.

A `--request-id` is an ownership claim over the operation it recorded. A retry under an id the audit
holds, committed or staged, is answered from that record only for the same event kind, card and
request — for `create`, `comment`, `report`, `verdict`, `decide`, `claim`, `move`, `edit`, `archive`,
`routing` and restore writes alike. For a report the comparison covers the marker, body digest and
classification, so a second `report --kind done` under the previous round's id with a new body is
refused with `validation` (exit 2): no second event, comment or false success. Three fields are excluded
because a retry cannot recompute them: the column a `move` left, the digests of text an `edit`
overwrote, and a restored comment's body until confirmed on the card.

Every task write result carries `replayed`: `false` when this call performed the write, `true` when it
answered from an event an earlier call under the same id had committed or staged.

`report --kind done` checks `git status --porcelain` of the worker's workspace first and refuses with
`uncommitted` when there are uncommitted changes (an untracked runtime tail does not count). `--kind
blocked` is not gated. The dispatcher's later check stays as defence in depth.

`report --kind blocked` requires `--classification`: `external_fact` (a fact outside the card must
change first) or `wrong_task_definition` (the card itself is wrong). It goes into the `reported` audit
payload as `classification` and into the report comment as a `classification:` line under the marker,
in the one backend write the report makes; it is not card metadata. `--kind done` refuses a
classification. An observer moving a card out of Blocked must give a non-empty reason (as the steward
must moving one in); it is a card comment carried by the transition event. The `reported` events are
the authoritative copy of block classifications. The report vocabulary is defined once, in
`ummanu.tasks`.

### Rejected SHAs and gate infrastructure reruns

The dispatcher remembers the SHA a mechanical gate or red review rejected in the current attempt. A
`done` report on the same SHA does not move to Validate: the first sends the worker back to rework in
the same workspace (a new commit is required); the second moves the card to Blocked. If the code
deliberately does not change for a substantive rejection (e.g. the defect is in a test or the gate),
the worker reports `--kind blocked` with the analysis.

Exception: a red GitHub gate classified from its failed job and step as an enumerated infrastructure
failure — action-download HTTP 5xx, unavailable image registry or Buildx registry setup, or an
unavailable runner during `Set up job`. Classification reads only the `gh run view --log-failed`
fragment: action download needs its runner notice adjacent to a failed-download 5xx entry; registry
failures need a container/Buildx step and either a named registry outage or a Docker daemon HTTP 5xx;
runner failures need a runner-service diagnostic. A setup-step name, card comment or manual flag never
classifies; a pytest assertion mentioning a registry or 503, or a broken workflow setup, stays
substantive. The SHA stays in Validate for an automatic retry with no worker round, no `gate-red`
transition and no `red_ci` budget event: the gate asks GitHub to rerun the failed run and treats the
rollup as pending until that run reaches a new terminal state. Reruns are SHA-scoped and bounded by
`UMMANU_GATE_INFRASTRUCTURE_RERUN_MAX_ATTEMPTS` (default 2); the rerun request uses the
`UMMANU_GATE_TRANSPORT_MAX_ATTEMPTS` ceiling. An exhausted ceiling, or a run GitHub cannot rerun,
moves the card to Blocked with the infrastructure class and count or unavailable-rerun cause. This
applies to the pre-review and pre-merge gates and the release re-check; the pending-stall ceiling
covers a rerun that never completes.

A recovered stale worker result bearing that infrastructure class is accepted once into the same
bounded gate path; a further report of the unchanged SHA is Blocked, naming the class and prior retry.

The gate classes (`substantive` vs infrastructure) and the bring-up classes (`task` vs
`infrastructure`, [below](#bring-up-outcomes)) are separate axes: a host failure is not a verdict about
the card in either.

The audit trail is always written to the installation data directory: `--data-dir`, else
`UMMANU_DATA_DIR`, else `data_dir` from instance config. A relative `data_dir` resolves against the
instance file. An unresolvable data directory is a usage error.

### The no-observer ceiling

A card with no observer (no sprint, a sprint declaring `none`, a closed sprint, or an unreadable sprint
board) is bounded by a ceiling on substantive red reviews. The count is the card's own `review:red`
comments (a retried verdict write dedupes on its request id). Red mechanical gates and red CI rollups
leave no verdict marker and are not counted. The third red review moves the card to Blocked with a
reason naming the ceiling instead of opening another round; only the terminals stop, and workspace and
branch stay as the round left them. The ceiling does not apply to a card that parks for a decision. A
dead head is handled by the wait watchdog's per-kind respawn ceiling, observed or not.

### Routing telemetry per attempt

A card keeps no routing history (the resolved review head is cleared on leaving Validate; the routing
block is reset on a return to Ready). Who worked and reviewed attempt N lives only in the append-only
journal as `kind: "routing"` events, written by the dispatcher through the normal pending/commit path
with no backend mutation, idempotent by request id.

An attempt (round) is one worker launch plus the review it earned. A claim opens attempt 1; each bounce
back to rework (red verdict, red gate) opens the next. Respawn, resume after a pause and restart after a
rejected SHA stay inside their attempt. A return to Ready followed by a new claim adds an attempt; the
number comes from the journal, so it survives a lost record and a restore. Both an operator retry of a
blocked card and a preempt or requeue of a live card count as a return to Ready: the dispatcher issues a
new attempt id then (so the next claim is not answered by the old committed claim) and stops the previous
attempt's heads.

```json
{"kind": "routing", "ref": "PROJECT-N", "payload": {
  "attempt": 2, "attempt_id": "...", "phase": "verdict", "outcome": "red",
  "heads": [{"role": "worker", "head": "codex-terra-high", "head_source": "card",
             "adapter": "codex", "model": "gpt-5.6-terra", "model_source": "profile",
             "effort": "high", "codex_mode": "tui",
             "resource": "openai-sub", "account": "openai-subscription",
             "session_id": "0198b0b0-...", "session_id_reason": "",
             "prompt_path": "/workspaces/PROJECT-N/TASK.md",
             "prompt_version": "sha256:..."}]}}
```

`phase` is `worker` (worker launch), `review` (reviewer launch) or `verdict` (the attempt's outcome,
carrying both heads). A verdict `outcome` is `green` or `red` from the reviewer; a mechanical-gate
bounce closes the attempt with its own value (`gate_red`, `merge-gate_red`, `review-freeze_red`). If
the reviewer returned green and the merge gate then bounced, both events stay.

Head choice is made at claim time, with no substitution at launch, except when a head's first turn
ends on a provider error ([provider failure](#provider-failure-on-a-heads-first-turn)): the role is then
relaunched on its chain and the new head is journalled as that attempt's active head. It reads the card
override or `role_defaults`, then resource health. A preferred head whose resource is red or spent is replaced
by the first launchable head along the registry's fallback chain for it (breadth-first, cycles read
once); a chain entry the registry no longer describes is dropped. `head_source` records where the id
came from: `card`, `role_default`, `fallback`, or `record` (pinned in the dispatcher record at an
earlier claim).

Two answers end the walk without a claim, leaving the card in Ready with the reason (dead resource and
probe verdict) on the tick: nothing launchable in the chain, or a transfer that would give worker and
reviewer the same head (a registry that itself points both roles at one head is claimed normally). Both
are claim-skips about one card: the Ready pass records it and considers the next card. Every claim-skip
kind is in one set the pass reads; a skip missing from that set ends the pass.

A failover head writes `resolved_worker_head` / `resolved_review_head` onto the card, adds one comment
naming the head, the replaced preference and the resource verdict, reports both in the tick, and the
reviewer's document names the head that wrote the branch when it differs from the card's.

A dispatcher that lost its record takes the head pair from the card's resolved fields when adopting.
If the claimed head has left the registry, nothing is launched: the card moves to Blocked with that
reason, the dispatcher record is dropped, and nothing is appended to the journal.

Each head carries its full launch configuration, snapshotted at bring-up and written to the journal as
is; the registry is re-read only for an adopted card launched in a previous dispatcher life.
`model_source` says where the model came from; `model` is empty only under `cli_default` (the CLI
picks), and the launch record rejects an empty model under any other source. The snapshot reads the
role launch environment (after the role-environment wrapper drops `runtime.env` variables outside the
role allowlist), not the dispatcher's own.

Each launch record carries the provider's durable `session_id` (Codex rollout or Claude jsonl
session); if unavailable at bring-up it is `null` with `session_id_reason`. `prompt_path` names the task
document delivered, and `prompt_version` is its `sha256:` content address at that bring-up.

Every launch inside an attempt writes its own event (respawn, restart after a pause, rework). The
request id includes a configuration digest, so relaunching the same head commits once; a different
adapter, model, effort or resource adds an event and replaces the attempt's active head. The verdict
carries the head that earned it.

Reader: `ummanu.routing_journal.attempts(events, ref)` returns a finished card's attempts with heads
and outcome. These events travel in the recovery checkpoint with the rest of the event log.

### Attempt outcome ledger v1

`attempt.outcome` version 1 is append-only observational telemetry, written by the dispatcher
lifecycle owner only after its terminal effect is confirmed. No card move, merge, retry, admission,
gate, observer decision or recovery decision reads it.

Dispatcher terminal effects cross one forward-only typed taxonomy before observational fields are
written. Disposition axis: `release|rework|reslice|blocked|drop|operator_stop`; blocked-reason axis:
`implementation|review|task_contract|gate|provider|infrastructure|operator|other`. The transition keeps
the taxonomy's `source_evidence` beside its normalized reason (a worker `wrong_task_definition` report
maps to `task_contract` with that source token; `external_fact` stays source evidence under `other`).
Invalid forward values are rejected at that boundary. Events with no taxonomy read as explicit
`legacy`/`other` evidence, with no rewrite, backfill or inference from request ids, prose, timestamps or
live state.

The normalizer accepts the terminal cause once and persists a forward record with disposition,
normalized reason, source evidence and budget class. That immutable record travels with the lifecycle
effect, supplies its post-effect `attempt.outcome` obligation, and is the only forward input to budget
reconciliation. Recovery reads the committed record's own disposition (an assessment `reslice` to
Blocked stays `reslice` while its `blocked` budget class still charges). Only a classified head
bring-up infrastructure outcome is uncharged `infrastructure_blocked`; worker-result, gate, merge,
adoption and durable-state mismatch causes keep their charged class, and every other normalized block
charges `blocked`. An event with no taxonomy keeps its durable action-token budget class. These
consumers cannot gate, reorder, retry or undo the lifecycle effect; a malformed record yields the
non-gating `terminal-taxonomy-invalid` budget diagnostic and later budget events continue.

Natural key: exactly `(card_ref, attempt_id, report_generation)`; `attempt` is the observed ordinal and
not part of the key. The Card subject supplies `card_ref`; `data` carries `version: 1`, non-empty
`attempt_id`, positive `attempt` and `report_generation`, nullable `sprint_ref` and
`specification_revision`, `terminal_state` (`done`, `blocked`, or `in_progress` for a rework), `verdict`
(`green|red|blocked|missing|legacy`), `disposition` and nullable `blocked_reason` (present exactly when
disposition is `blocked`).

`source_event_ids` always contains nullable `report`, `verdict`, `decision`, `effect`, `worker_usage`
and `review_usage`. `usage_completeness` always contains `worker` and `review`, each
`collected|degraded|missing|legacy`; collected or degraded usage has its usage-event ref, missing and
legacy have none. An unknown report/verdict/decision reference stays null and is never inferred.
`null`, `missing` (no forward occurrence), `degraded` (occurrence without usable measurement) and
`legacy` (predates the contract) are distinct; a null field is not zero. Historical events are not
rewritten or backfilled.

An exact replay returns the staged or committed occurrence. A different payload for the same natural
key raises `AnalyticsOutcomeConflict`. A crash after stage reuses the pending event; a crash before stage
remains owed on the confirmed terminal lifecycle event, whose immutable obligation carries round
identity and disposition, and the lifecycle owner reconstructs the occurrence from that event and other
typed journal occurrences only. Unreadable, conflicting, staging or append failures are degraded
analytics diagnostics and leave the lifecycle effect and teardown unchanged. Unknown versions, fields
and enums are rejected by the typed board-event reader.

Only a dispatcher-owned transition that closes a started round with durable round context carries an
obligation; waiting, reviewer-launch and pre-claim refusal/retry paths do not. A lost claimed record
that cannot be adopted still commits its Blocked effect but produces no outcome. Operator stop/drop
produces an outcome only where the stopping lifecycle record carries a durable attempt id, round and
generation.

### Offline analytics projection v1

`ummanu.board.analytics.project_analytics_checkpoint(directory)` is the offline reader for one
copied `state/board` checkpoint. It first calls `verify_analytics_checkpoint(directory)`, and only then
parses the logical `cards.ndjson`, `sprints.ndjson` and `events.ndjson`, read through the checkpoint
reader in either layout ([Recovery](RECOVERY.md#board-checkpoint-layout)). `export.json` is verified only as a count
summary. The reader has no live board, dispatcher, provider session, comment, transcript or lifecycle
dependency and never mutates a checkpoint. `cards.ndjson` and `sprints.ndjson` are membership sources
for typed event subjects; repeated references and rows without a usable reference create no analytics
identity and do not invalidate unrelated evidence.

The versioned result has `checkpoint_id`, `rows`, `incomplete` and `incomplete_reasons`; `ndjson()`
emits one deterministic object per row. Each row is keyed by `card_ref`, `attempt_id` and
`report_generation` and carries the outcome's round fields, terminal taxonomy, `source_event_ids`,
`usage_completeness`, and nullable `worker_usage` and `review_usage` objects (usage event id, typed
phase identity, collection outcome and all three nullable token accounts; never synthesized or zeroed).

The only usage join is a non-null `source_event_ids.worker_usage` or `.review_usage`. The referred
event must be a typed `attempt.usage` on the same Card with the same attempt id, attempt and report
generation; worker requires role/phase `worker`/`worker`, review requires `reviewer`/`review`.
`collected` requires a collected occurrence; `degraded` a typed non-collected one. No order, timestamp,
request id, prose, marker, provider state or live Card field participates in a join.

The projection fails closed with `AnalyticsProjectionError` codes `analytics_malformed_row`,
`analytics_read_failed`, `analytics_invalid_typed_event`, `analytics_conflicting_event_identity`,
`analytics_conflicting_request_ownership`, `analytics_conflicting_outcome_natural_key`,
`analytics_dangling_card_ref`, `analytics_dangling_sprint_ref`, `analytics_dangling_source_event_ref`,
`analytics_incompatible_source_event_ref` and `analytics_incompatible_usage_join`, each naming its file
and record number. An exact replay of the same typed event and request is one occurrence. A cut with no
v1 outcome reports no rows and `no_attempt_outcome_v1`; legacy outcome or usage evidence sets explicit
incompleteness. The checkpoint seal is in [Recovery](RECOVERY.md#analytics-checkpoint-seal-v2).

### What a finished phase cost

Every completed worker phase and review phase leaves one `attempt.usage` event on the card: a typed
board protocol event (`kind: "attempt.usage"`, Card subject) with no backend mutation, written through
the append-only audit and carried by board/audit export. There is no backfill; a card without a usage
record is not a zero.

**When.** On the acceptance path, only for an accepted terminal outcome: a `report:done` or
`report:blocked` the dispatcher accepts, and a `review:green` or `review:red` it acts on. A done report
bounced at an already-rejected checkout writes nothing. The write happens while the completed run and
its provider session are still on the record (a retained worker before its freeze; a reviewer after its
head is confirmed stopped).

**Fields.** Card ref and subject, numeric attempt and attempt id, the report generation closed, role
(`worker`/`reviewer`) and phase (`worker`/`review`), head id, adapter, resolved model and
`model_source`, launch id, provider `session_id` or its typed absence with reason, collection outcome,
and three token accounts. Identity fields come from the routing journal's launch snapshot.

**What actually ran.** `model` is the configured name (an alias such as `opus`, or empty under
`cli_default`). Beside it the occurrence carries what the provider journal says the CLI resolved,
read from the same session records as the tokens and session-wide through the phase's end:
`resolved_models` (every distinct model id in order of last use), `resolved_model` (the last of them,
e.g. `claude-opus-5-5`; empty when the journal named none) and `resolved_effort` (the last reasoning
effort the journal recorded; empty when none). Claude: `message.model` of assistant records (a CLI
`<synthetic>` message names none) and the record's `effort`. Codex: `model` and `effort` (older
rollouts `reasoning_effort`) of each `turn_context` record. They are filled whatever the token
outcome whenever the journal was read, and never copied from `model`. An occurrence written before
these fields has none of them and reads as unknown; when present, all three are present,
`resolved_models` holds non-empty distinct ids and `resolved_model` is its last entry.

**Token dimensions.** `input` (uncached input), `cache_input` (cache creation/write input),
`cache_read_input` (input served from cache), `output` (total generated output, including reasoning)
and `reasoning` (subset of `output`), each a non-negative integer or `null`. The additive total is
`input + cache_input + cache_read_input + output`; never add `reasoning` again. Where both are known,
`reasoning <= output`. `null` means the provider did not report the dimension. Accounts: `tokens` (the
interval this phase owns), `session_totals` (running provider-session total at phase end) and
`phase_baseline` (the boundary it started from).

Accounts are nullable per dimension and independently. For every dimension the phase owns,
`tokens = session_totals - phase_baseline`. An unattributable dimension is `null` in both `tokens` and
`phase_baseline`, while `session_totals` keeps what the provider reported. A `collected` outcome reports
at least one `session_totals` dimension (`tokens` and `phase_baseline` may be entirely null). Every
degraded outcome reports no dimension in any account. There is no price table or monetary conversion.

**Phase-attribution lattice.** One rule for every provider and lifecycle path (first or retained phase,
live acceptance, staged or replayed obligation):

- **No predecessor occurrence.** Each known dimension starts from a zero boundary; the phase owns its
  whole value. An unreported dimension is `null` in `tokens` and `phase_baseline`.
- **Predecessor, both values known and nondecreasing.** The phase owns the difference.
- **Predecessor, either value unavailable.** `null` in `tokens` and `phase_baseline`; no zero invented.
  The phase's `session_totals` value is kept, so the next phase subtracts from it normally.
- **Predecessor, current value below it.** The whole occurrence is `arithmetic_contradiction` and
  publishes no account.

Containment is validated when accounts are produced and when written: `reasoning` escaping `output` in
any account degrades the occurrence the same way. (A Claude journal can stream a detail-less record
before the completed duplicate, so a phase may end with no reasoning detail that the next phase on the
same session sees in full.)

**Canonical projection.** One repository projection reads the card's committed and staged TaskAudit
records under the audit lock, validates every `attempt.usage` record through the typed schema, and
returns each immutable occurrence with a separate `pending` flag. An exact committed-plus-pending copy
is one occurrence. A request id with another payload, an event id with another request owner, an
unreadable record, or conflicting phase ownership makes the projection unavailable; readers fail closed.
The flag affects only export visibility: a successful stage is immediately an accounted occurrence.

One session can serve several phases (a retained worker resumed into the next round). A phase owns the
usage recorded after the previous authoritative terminal boundary for the same card, role, adapter and
provider `session_id`, through its own boundary. The predecessor is selected by explicit causal identity
(attempt, attempt id ownership, report generation, phase), never by iteration or append order. A session
with no matching prior occurrence starts at zero. A matching predecessor whose degraded occurrence
carries no `session_totals` at all is an audit failure (no zero baseline is invented); a predecessor that
recorded some dimensions is handled per dimension by the lattice. An unreadable or conflicting
projection is an audit failure.

**Order and failure precedence.** First projection integrity and causal identity (a phase slot owned by
a conflicting attempt id fails closed regardless of the read); second the provider read (a whole-session
total only); third attribution and cross-account validation, in one place; fourth immutable staging; last
publication recovery. A degraded provider read needs no interval arithmetic, does not consult the
predecessor, writes its named outcome and proceeds.

**Codex aggregation.** Codex writes cumulative `token_count` snapshots, and a new user turn can reset
`total_token_usage` within the same session and rollout file. A decrease in any raw counter starts a new
segment; the session total is the sum of each segment's final snapshot, and repeated snapshots within a
segment add nothing. `cached_input_tokens` and `cache_write_input_tokens` are contained in
`input_tokens`; `reasoning_output_tokens` is contained in `output_tokens`. At each segment endpoint:
`input = input_tokens - cached_input_tokens - cache_write_input_tokens`, `output = output_tokens`,
`reasoning = reasoning_output_tokens`. All five raw fields and valid containment are required, otherwise
the snapshot is malformed.

**Claude aggregation.** One `usage` object per assistant message; `input_tokens` already excludes
`cache_creation_input_tokens` and `cache_read_input_tokens`, which map directly to `input`,
`cache_input` and `cache_read_input`. The session total is the sum over distinct message ids, each
contributing its last usage object once. `output_tokens` maps to `output`. `reasoning` is the sum of
`output_tokens_details.thinking_tokens` only when every contributing usage object supplies a valid
detail (explicit zero included); otherwise aggregate `reasoning` is `null` while `output` stays. A
missing, malformed or out-of-range detail (not an object, or a thinking count that is not a non-negative
integer within that message's output) costs only reasoning, is counted in `skipped_records`, and keeps
the message's other counts. A `usage` that is not an object or has no usable count contributes nothing.

**Outcomes.** `collected`; `arithmetic_contradiction` (current total below an immutable earlier
boundary, or reasoning escaping output); `adapter_unsupported` (adapter has no structured usage
records); `session_unavailable` (no provider session bound to the run); `source_unavailable` (record
source never bound, or names no journal); `source_unreadable`; `records_malformed` (a record declaring
itself usage — a Codex `token_count`, an assistant message with `usage` — with an unpublished schema, or
a journal where nothing parsed); `usage_absent` (the journal parsed and holds no usage record). A
truncated tail line is skipped, not a failed read. `skipped_records` counts unparseable lines and
schema-invalid declared usage records; `records` counts what the aggregation used.

**Idempotency.** One occurrence per completed phase, keyed by attempt id, phase, attempt number and
report generation. A replayed request or re-entered acceptance commits the event that already owns the
id, so recovery against a grown session file can neither add nor overwrite an occurrence. A repeated
done report inside one round (the infrastructure-classified gate retry) returns that round's occurrence.

**Durability order.** On every worker-report and reviewer-verdict path the occurrence is staged under
its request id and appended *before* the control event and transition. A card may advance past a staged
obligation (a later tick publishes it), never past nothing.

**Settling staged obligations.** Every production tick, once singleton, pause and mutation guards permit
work and before observer fencing, `ACTIVE_STATES` selection, active-card reconciliation, phase-boundary
reads and new claims, publishes every occurrence the projection marks pending. It uses no dispatcher
record, board lookup or card state. A publication failure keeps the exact staged occurrence pending and
reports a degraded `attempt-usage-recovery` action naming the cards owed; it is retried on every later
permitted tick. Publication always finishes the exact staged occurrence and is idempotent; a tick that
owes nothing reports nothing.

**Non-blocking.** Provider reading never decides anything: degraded outcomes are recorded in the
occurrence and the report or verdict is accepted normally. Failing to make the occurrence durable is an
audit failure and is raised: the card's tick ends with the phase unadvanced and the report or verdict
still on the board, and the next tick retries the same acceptance.

### The report generation

A worker round is identified by a report generation, a counter in the dispatcher record: 1 at claim,
advanced by one whenever a new report round opens (red mechanical gate, red review, or a done report
bounced at an already-rejected checkout). It never repeats within an attempt and never goes backwards. A
respawn inside a round does not advance it. A red transition reserves the round's generation in the
intent it writes before moving the board, and the completing tick assigns that reservation, so a
re-entered completion does not spend a second generation.

The generation is the suffix of the `done` and `blocked` request ids in `TASK.md` and of the report body
paths those commands name; each block classification gets its own id. It is persisted before any
document names it, `TASK.md` is written from it, and the prompt waking a retained worker names it and
the suffix the round's commands carry.

A stale command with a new body is refused with `validation` (exit 2); an identical retry is answered
from its committed event with `replayed: true` and adds no marker. When the next round opens, all round
body files are deleted, including the new round's, so a replayed command reading one fails on its first
step. Guarantee: a command from an ended round never records a report of the current round; it can
still answer its caller with a success belonging to its own round.

The dispatcher ends a round on two facts of its own. First, a report belongs to a round only by the
request id its command carried (the audit keeps it beside the marker), and that id encodes attempt and
generation: ids from an ended round, an earlier attempt or an invented id end nothing and leave the card
where it was. Second, a head that stopped with its round unreported is pointed at the current command
once and then the card is blocked. Wait mechanics: [Operations](OPERATIONS.md).

Which ids a round issued is read from the checkout first and dispatcher state only as fallback. Only ids
naming the open generation are taken from the document. When the checkout cannot be read, the ids are
those the dispatcher would issue from the record's attempt and open generation, so a live worker holding
older ids is bounced once and re-materialised on the same round.

The document is read through a hidden record, never by scanning for commands: every worker `TASK.md` ends
with `<!-- report-round generation=N ids=... -->` carrying the round's request ids base64-encoded,
written after every section a description or decision is rendered into, matched as a whole line, last
such line taken. The checkout's generation is read from the same record.

Only a committed audit event ends a round; a staged `reported` event does not. A report whose comment
landed but whose audit append failed leaves the round open until the audit is repaired (retrying the
same command does that). The report protocol itself is unchanged.

A dispatcher that lost its record recovers the generation from the checkout's `TASK.md` and from the
count of consumed reports; both are lower bounds and the larger wins, so a recovered generation may
skip a number but never reuses one. Unconsumed reports are not counted.

The comment index a new report marker is scanned against is a separate comment count.
`review_baseline` is only the comment index the next verdict is read from and the round key of the
reviewer's verdict identity.

### The observer decision a rework round is opened on

A round opened by a `rework` decision carries that decision to its worker. The decision text is frozen
with the generation: written into the red transition's intent before the board moves, assigned to the
record when the transition completes, and rendered into every `TASK.md` of that round (replacement or
retained head). It is never looked up again at document-build time, so a later decision does not
replace the instruction the round runs under.

In the document the decision comes first, under a heading naming it the instruction to follow, with the
reviewer's red body below it as context; where they disagree the decision wins. The prompt waking a
retained worker names the decision as the authoritative instruction.

Every round carries only the decision that opened it. The red transition assigns it unconditionally;
the stale-done bounce clears it in the same mutation that advances the generation. Rounds opened by a
red gate or bounced done report carry no decision.

A lost record recovers the decision from the checkout: every worker `TASK.md` ends with
`<!-- observer-decision generation=N body=... -->` carrying it base64-encoded (empty body when none),
and recovery reads the last such line.

### Revision-bound worker feedback

The description in a worker `TASK.md` is authoritative. A create or description edit has an immutable
audit event and description digest; its event id is the card's current specification revision.
Reviewer verdicts and observer decisions record that revision and digest. The task-document feedback
selector renders a red review, or a rework decision with any supporting red review, only when each
rendered item is bound to the current revision. A correctly bound rework decision stays the instruction
that opened the round even when no red review exists or the red review predates the revision. A reslice
followed by a description edit therefore starts a fresh worker without the prior reviewer's
instructions. Missing, malformed or ambiguous binding omits historical feedback.

### Card comments the worker is handed

Comments from the PO, the owner and the observer (markers `po`, `owner`, `observer`) refine the spec,
and when one contradicts the description the comment wins. Every worker `TASK.md`, first launch and
every rework or continuation alike, renders all of them in one section, *Comments from the PO, the
owner and the observer*, right after the description and before the observer decision and the red
review and gate bodies. They are listed in board order (creation time), each under its role and
timestamp with its full text. A card with none has no such section. Comments of every other role
(dispatcher, worker, reviewer, steward, retro) are left out: the review and gate bodies have their own
sections, and the observer's Assessment decision reaches the worker as the dispatcher's own prose. Unlike
the revision-bound feedback above, these are not filtered by specification revision: a comment written
before the description's last edit is still shown.

Each comment is keyed by the `commented` audit event that wrote it, paired by marker and body digest.
A comment no such event accounts for (a move reason, a restored comment) is keyed by marker, digest and
occurrence. The keys a document renders are recorded on its last lines as
`<!-- worker-comments keys=... -->`, base64-encoded.

**Mid-round continuation.** While the worker is running a round (record `claimed`, not paused, no
continuation retained or pending, heartbeat a live match and not suspended, the conversation
addressable), a tick that finds a key neither in the checkout's `TASK.md` record line nor in the
record's `worker_comment_deliveries` points the worker at it once: the keys are added to the record and
saved, `TASK.md` is re-rendered for the same generation and decision, and only then the pointer, "re-read
its comments section", goes out through the same worker delivery as the report prompt. A tick repeat or
a restarted dispatcher sends nothing again; a failed send is recorded as delivery evidence and not
retried, and the next round's `TASK.md` carries the comment either way. A worker that is parked in Assessment,
held for validation, paused or between rounds is sent nothing and nothing is recorded; the next round's
`TASK.md` carries the comment. `decision` and `operation` cards have no worker and are unaffected.

### Head heartbeat identity

Every dispatcher-launched worker, reviewer and observer writes one atomically replaced version-1 JSON
heartbeat before its shell `exec`s the provider: pid, Linux boot id, process start ticks, durable
`HeadRun` id, role, card or sprint binding, and the head's leaf once known. Head creation and the
writer are unordered, so the launcher first atomically writes a matching leaf handoff beside the
heartbeat: a later writer incorporates it in its first record, and an already-written matching record
gets a guarded second atomic replace. The writer re-checks the handoff after its base replace, and a late
binder never annotates another process that reused the pid-file path.

Readers classify a matching live record, a dead record, a live identity mismatch, a not-yet-written
record and an unreadable record separately. Boot, start ticks, run id, role, task and a known leaf must
all agree. Lifecycle and recovery consumers use one HeadRun classification boundary, which builds the
expected identity before a stop can persist `finishing` or `stopped_by`, a review launch can become
`reviewing`, or a head or workspace can be stopped or relocated. A mismatch is an operator-facing degraded
state: retention, launch recovery, watchdogs, stop paths and observer reconciliation leave the prior run
unattributed and never open a replacement beside it. The guard is rechecked immediately before every
destructive head stop, workspace stop and heartbeat signal. Raw command overrides write no heartbeat
and get no synthetic identity; they keep only the launch grace and output fallbacks.

A head is alive only by its own observation: a live matching heartbeat whose process is running or
suspended, or an advancing provider cursor bound to the same `HeadRun`. Only the heartbeat may say a
head is gone. Anything else is `unproven`, including a role with a head identity but no durable
`HeadRun`. Terminal readings never enter the answer. `ummanu head-status` shows the answer
and which source proved it; see [Head status in a live workspace](OPERATIONS.md#head-status-in-a-live-workspace).
Stall ageing, rungs and the destructive guard are in [Head vitality](HEAD_VITALITY.md).

### Worker retention through validation and review

After a worker reports `done`, the dispatcher suspends its live, addressable session before moving the
card to Validate. A head that is not addressable is stopped with a confirmed stop instead. The retained
state stays on the record through the mechanical gate and the following review, so the worker cannot
change the checkout while it is judged. Before the reviewer starts, the suspension is confirmed from the
heartbeat; an unconfirmable session gets a confirmed stop and the round loses its continuation. The
reviewer runs as a second supervised process in the same worktree.

While retention is on the record, no vitality path wakes the session: a confirmed retention reduces to
`Retained`, which earns no recovery rung and is refused by the destructive guard. A head stopped without
an active retention keeps the ordinary suspension ladder. A retained session that is provably gone still
reduces to `Dead`, and one running again is caught by the heartbeat confirmation before the delivery
boundary, which stops and replaces it once. See
[Head vitality](HEAD_VITALITY.md#retention).

Substantive red verdicts return the card to In progress through one transition that hands the round
back to the session that wrote the code. (An enumerated infrastructure gate red instead stays in
Validate; see [Rejected SHAs and gate infrastructure reruns](#rejected-shas-and-gate-infrastructure-reruns).)
A substantive red mechanical gate opens the rework directly. A red review on a card whose sprint
declares a concrete observer parks it in Assessment once the reviewer's stop is confirmed, and the
transition runs on the tick that performs a recorded `rework` decision. A red review on a card with no
observer opens the rework directly after the confirmed stop, up to the no-observer ceiling. Nothing else
moves a card to In progress for rework. The transition order:

1. The red intent — phase, baseline of the report it closes, reserved generation, opening observer
   decision if any, and the reason — goes to disk.
2. The card moves.
3. The reserved generation and decision become the record's, and are persisted.
4. Delivery: a confirmed-suspended session takes the continuation; anything else gets a confirmed stop
   and exactly one replacement. Either way the head gets a `TASK.md` for the new generation before it is
   woken or launched.

A round with nothing to reuse opens the same durable intent. A tick dying after step 1 is recovered by
re-entering this transition against the current board, never by replaying the Validate handoff. An open
intent outranks everything: every tick finishes it before reading the gate, a report marker or a verdict,
and before starting a reviewer. The intent is immutable and carries its reason, so a rollup turning green
cannot retract an owed red round, and the card moves once.

The suspension of the session to reuse is re-confirmed from the heartbeat immediately before the
delivery boundary, on recovery as on the first attempt; an unconfirmed session is stopped and replaced
once. The dispatcher updates `TASK.md` with the failure and the round's report identity, persists a
pending-delivery boundary before SIGCONT, and checkpoints confirmation only after the provider durably
records the continuation user turn. Terminal activity is only a recovery hint for records without that
boundary. A delivery the runtime refuses as busy (the supervisor has a turn open) is busy evidence,
not a transport failure: the check runs before SIGCONT, so HeadRun, handle binding, workspace and pending
continuation stay as recorded, and its bounded retry delay authorizes no stop, replacement or new
attribution. Unavailable transport, malformed evidence and `terminal_handle_stale` remain separately
typed conservative failures; absent fields on older evidence are unknown, never busy. Recovery cannot
mistake the previous `done` for a new completion, replay an incomplete delivery as confirmed, or
overwrite a confirmed continuation. A checkpointed delivery finishes on the next tick, and reuse is
recorded on the card once. A pending delivery whose head is awake by the next tick fails confirmation
and takes the confirmed stop and single replacement.

All supported Codex and Claude workers are interactive and accept a continuation. The routing record
and card comment name the outcome as a reused continuation or a replacement, with worker profile, model,
effort, reason and timestamp. A dead session, unavailable continuation transport or lost handle falls
back explicitly: confirm the old worker stopped, write a durable launch intent, start exactly one
replacement. The transition changes hands only once that intent write succeeds; otherwise the red
transition stays on the record and the next tick finishes it. Retention and stop signal the head's
private process group. An unconfirmed stop never permits a second writer in the workspace.

Retention is scoped to one round: the report that opened it, the gate and review that judge it, the park
the verdict opens, and the decision that hands it back. A preempt or requeue to Ready, a
`report:blocked`, a move to Blocked and a reconciliation onto another card all stop the worker head and
clear retained state. A green review ends the round too: the merge tears the worktree down, waking the
suspended head before it is killed.

#### Retained-continuation provider liveness

`worker_continuation_liveness` is version 1 state bound to the exact retained `HeadRun`: run id and
digest of immutable launch facts, first busy observation, last provider observation, last fresh provider
progress, opaque provider cursor/source fingerprint, persisted source baseline, busy count, recovery
rung and terminal outcome. It holds no prompt, composer or provider text. A record is created only when
the retained delivery boundary is written. Missing, malformed, unsupported or HeadRun-mismatched values
are durably `unknown`; the only unbound serialised shape is explicit `unknown`. Older busy counts are
audit data and cannot bind a later run, reset the ladder or spend a rung.

Before every retained-continuation retry, one admission step validates the durable episode and exact
`HeadRun`, resolves its launch-bound provider source, and persists/uses the v1 baseline for that source.
Codex reads only the bound session journal selected from its pre-launch baseline; Claude reads only the
exactly-one transcript selected from its pre-launch baseline; neither uses a workspace-wide newest-file
mtime. A changed opaque cursor is fresh progress: it keeps run, workspace, claim, continuation intent
and retry owner, resets only the no-progress ladder, and makes a busy delivery refusal non-destructive.
Source absence, ambiguity, a foreign or malformed source, or an episode without a baseline is typed
unavailable or unknown and cannot become progress, reset or advance the ladder, or authorise recovery
or replacement. Fan-out telemetry and recorder failures never enter this decision. The worker/reviewer
watchdog carries the same typed source result: provider-unavailable, stale handle, identity mismatch,
confirmed dead and busy are distinct, and only admitted progress renews liveness.

Once an episode rejects a foreign or changing source it is sealed as `unknown`: its HeadRun binding,
baseline, cursor and ladder become audit-only and cannot be re-admitted. The shared worker/reviewer
status seam checks every apparently accepted provider observation against the persisted HeadRun before
it renews the watchdog clock.

Codex preflight writes the immutable run descriptor: run id, HeadRun fingerprint, resolved workspace,
role and task reference. Binding selects exactly one journal and keeps that descriptor verbatim, adding
only verified journal identity, range, cursor and bind time. Ingress and the shared provider reader both
validate the descriptor before admitting a cursor; a missing, overwritten or foreign field is
unavailable or identity mismatch.

An unchanged cursor records `completed_turn_residual_composer` (equal non-empty composer and output
fingerprints prove the composer is residual) or `active_or_unknown_turn`; screen text never decides.
After three unchanged admitted busy observations the dispatcher persists a single
`safe_recovery_pending` rung before asking for an explicit provider/terminal-safe capability. There is no
raw interrupt, generic key chord or screen-derived action. The current host has no such capability: it
records the typed absence and takes the confirmed-stop/HeadRun fence to one replacement. A capability
must return a safe receipt bound to the same run; its response window is rechecked for admitted progress
and may return to normal delivery once; otherwise the recorded replacement path follows. A source
identity failure is a typed blocked outcome and never touches a potentially foreign head. A stop that
cannot yet be confirmed stays identity-fenced and never opens a second worker.

## Production dispatcher

```bash
python3 -P -m ummanu dispatcher production-tick --instance INSTANCE
python3 -P -m ummanu dispatcher production-observe --instance INSTANCE
python3 -P -m ummanu dispatcher production-run --instance INSTANCE
```

The systemd timer runs the one-shot `production-tick`. The runtime handles only supported task
transitions, persists claim and review state, and checks the live board before recovery. The
production owner is recorded in dispatcher state; an owner mismatch, a dirty workspace, a missing
report or an unresolved audit state stops a transition rather than falling back.

Before the unit invokes the `ummanu` entry point, the configured production interpreter runs
`src/ummanu/dispatch/runtime_preflight.py` with `-I` (standard-library code executed by pathname).
It binds the configured product root and production interpreter and inspects that interpreter's plain
editable `.pth` files, executable editable finders and `direct_url.json` records for `ummanu`. The
result names the classification (`valid`, `wrong_root`, `workspace_targeted_editable`,
`missing_import` or `interpreter_unavailable`), interpreter, product root, observable import origin,
metadata source and offending target. Containment is decided on resolved paths, so a sibling such as
`ummanu-old` or a symlink escape does not pass.

Only `valid` execs `ummanu dispatcher production-tick`. A refusal exits `78` before candidate
package code, reconciliation or metadata change, and atomically writes the minimal `runtime_provenance`
and unhealthy-tick diagnostic into the production-state file. The pipeline health line and steward
incident reducer consume that telemetry, so repeated refusals are one incident and the first later
healthy tick records one recovery. A valid preflight clears only a prior refusal; it does not claim the
following tick completed.

`ummanu doctor` reports a failed check as the read-only `production_runtime_provenance` finding with
the exact target and metadata source. Repair is manual and never automated; see
[Operations](OPERATIONS.md#production-interpreter-provenance).

### Bring-up outcomes

A bring-up is everything between giving a card to a head and that head existing. When one produces no
head, the outcome is classified in one place for the worker path (claim, respawn, rework) and the
reviewer path (`start_review`). A closed set of causes decides the class:

- `infrastructure` — `launch_aborted` (a launch that may have left a head running, never turned into
  a second one); `host_unavailable` (anything else the host could not do: a head that would not start,
  a supervisor that would not answer, a registry that cannot supply a usable broad-check contract);
- `task` — `workspace_contract` (the checkout the card was requeued onto is gone, or is not the
  worktree on the branch its claim recorded); `base_branch_contract` (an integration base the project
  cannot integrate into, or a seed the project remote does not carry).

The cause decides the class; no caller pairs them freely, and an unrecognised cause is ignored. A host
failure carries no verdict about the card; only the card's own contract is a `task` failure.

For a new transition the immutable taxonomy record is the durable budget classification: a classified
head bring-up writes `infrastructure_blocked`; every other normalized blocked terminal (worker-result
failure, gate exhaustion, merge failure, unavailable adopted head, durable-record mismatch) writes
`blocked`. A transition without taxonomy keeps its action-token accounting and reads as `legacy`/`other`.
The classification is not card metadata.

The card's Blocked reason and the tick's outcome are built from one object. The reason ends in a clause
naming class, cause, stage (`claim`, `respawn`, `rework`, `review`), head and attempt id, followed by
the class sentence (for infrastructure: the head never came up, so this is not a verdict about the
card). The tick outcome carries `failure_class`, `failure_cause`, the same `failure_reason` string, and
a `bring_up` object with the same fields plus the host's detail.

The dispatcher classifies and presents the evidence and stops there: after an infrastructure outcome it
opens no attempt, schedules no return and moves the card nowhere else. Whether to retry or block the
sprint is the observer's decision, carried out by moving the card out of Blocked; a card back in Ready
is claimed under a fresh attempt id.

The reviewer's bounded relaunch over a green candidate
([Review infrastructure retries](#review-infrastructure-retries)) ends in an infrastructure outcome.

An infrastructure outcome charges nothing. It is recorded as the uncharged event type
`infrastructure_blocked`, shown as `budget.uncharged` in `sprint show` and `sprint status`, and enters
neither `total` nor the `signal` and `hard` thresholds. Every other block charges `blocked`. Both go
through the same budget write, distinguished by the taxonomy record. A sprint row without the field
reads as zero.

## Pause

The pause is pipeline-wide and sits on top of the product dispatcher:

```bash
python3 -P -m ummanu pause-scope  --instance INSTANCE          # what a pause would reach
python3 -P -m ummanu pause drain|freeze --instance INSTANCE --reason "why"
python3 -P -m ummanu resume --instance INSTANCE
python3 -P -m ummanu pause-status --instance INSTANCE
```

`drain` stops claiming new cards and dispatching background roles; running cards finish their cycle.
`freeze` also stops live worker and reviewer heads (a stop, never a teardown) and freezes the tick:
nothing advances and no watchdog fires on a head stopped on purpose. `resume` brings stopped heads back
up in the same workspaces, hands a card whose report already arrived to the next tick, and restarts
watchdog windows.

The flag is `<data_dir>/dispatcher/pause.json`, read by every `production-tick`. Background roles read
a mirror flag written and cleared by the same command. During a freeze `--exclude-workspace` excludes
the operator's own workspace (used by the manual archive command to freeze from inside a worker).

A freeze set by an automation on the configured allowlist expires after a TTL
(`TA_HARD_PAUSE_AUTO_RESUME_TTL_S`, default 45 minutes): the tick checks this before skipping on freeze
and lifts the pause through the ordinary `resume` under the same tick lock. A freeze set by a person
holds until an explicit `resume`. A frozen tick moves no cards but keeps the checkpoint cadence and
due-push coordination. Runbooks: [Operations](OPERATIONS.md#pause-or-breakage).

### The pause as protocol operations

The same pause through the transport-independent layer (`ummanu.webproto.pause_ops`,
`ummanu.webproto.pause_reads`): two operations and two reads. Every rule stays in
`ummanu.dispatch.pause_ops`; the layer adds no rule, flag, lock or store.

| operation | inputs | answers with | errors |
| --- | --- | --- | --- |
| `pause_drain` | `actor`, `reason` | a `pause_command` document | `validation` (empty actor or reason, or an installation whose config does not validate), `owner_conflict` (already paused in the other mode), `backend_unavailable` (a durable source or the host refused) |
| `pause_resume` | `actor` | a `pause_command` document, with `restored` | `validation` (an installation whose config does not validate), `backend_unavailable` (a durable source or the host refused) |
| `pause_state` | — | a `pause_state` document | `validation` (an installation whose config does not validate, with no data directory to fall back on) |
| `pause_scope` | — | a `pause_scope` document | `validation` (the same) |

The table is checked against `ummanu.webproto.pause_ops.PAUSE_ERRORS`. Every operation of the
package can also answer `backend_unavailable` for an implementation failure caught by
`ummanu.webproto.boundary` (layer-wide, not listed per row).

Properties every document states as fields:

1. **Pipeline-wide.** One flag, every `production-tick`; no per-sprint pause. Every document carries
   `extent` (`{"scope": "pipeline", "per_sprint": false, …}`), read from no source, so it is present
   even when every source refused. The scope read lists every open sprint and every Pipeline card, each
   with the sprint holding it or `null`.
2. **A drain stops no running head.** It stops Ready claims, background role dispatch and raising an
   observer for a sprint opened during the pause. `modes.drain.does_not_stop` says so, and the heads a
   drain leaves alone are reported under `heads` as `running`. `stopped_worker`, `stopped_reviewer` and
   `stopped_observer` are filled only by a freeze.
3. **A freeze is never an implicit upgrade.** The layer has no freeze operation
   (`modes.freeze.operation` is `null`). `pause_drain` takes no mode and passes the literal `drain`. A
   drain while frozen is `owner_conflict` and writes nothing.
4. **A resume says what it put back.** `restored` carries `resumed_mode`, the `relaunched`, `parked`
   and `skipped` lists, and `observers_resumed`. After a drain the lists are empty with a statement that
   a drain stopped nothing. Where a freeze's resume completed but its report could not be read back, the
   lists are `null`, not `[]`.

**`pause_scope`** is a read: before any command, it says the pause is pipeline-wide, which dispatcher
and files a command would write (`target.pause_file`, `target.state_file`,
`target.legacy_mirror_file`), which sprints are open, which cards are on the Pipeline and which sprint
holds each, which heads are running, and separately what a drain does not stop and what a freeze would.
It sets no flag, takes no tick lock, stops or starts no head, and writes nothing. It lists every card
from the one Pipeline listing, except Product and Issue records (`ummanu.tasks._TYPED_RECORD_TYPES`),
which a pause never reaches.

**Repeat and conflict.** The pause is idempotent in its own mode, so these operations carry no request
index: a repeated `pause_drain` answers `action: "noop"`, `changed: false`, writes nothing, and the flag
keeps the original actor and reason. A `pause_resume` of an unpaused pipeline answers the same way. A
`pause_drain` while frozen is `owner_conflict`; after a `resume` it is admitted. `action` is `paused`,
`noop` or `resumed`, `changed` its boolean, and a refusal never reaches a document.

**A command that did something says so even when the state cannot be rendered afterwards.**
`dispatch.pause_ops.pause` and `resume` write the flag and then render status through `pause_status`,
which converts every dispatcher record; a semantically corrupt `production-state.json` can make that
render refuse after the pause took effect. The caller then gets the ordinary `pause_command` document:
its `action` and `changed`, the render refusal under `warnings`, and the embedded `state` with the
failing source marked unavailable.

That action is the one `dispatcher_pause_ops` decided inside the production tick lock, in the code that
performed the command, and it travels out with the render failure on
`dispatch.pause_ops.PauseCommandCompleted`. It is never inferred from observing the flag: a flag
observed before and after an unlocked command is not evidence of which command set it (a concurrent
`resume` and re-drain look identical to a noop). Rendering stays outside the lock.

Nothing is repaired or re-decided: a command that did not complete raises unchanged; a `validation` or
`pause_conflict` refusal is never turned into an action; a freeze's resume whose own answer never
arrived has `null` `restored` lists. `PauseCommandCompleted` carries the render failure's code, message
and exit status, so `ummanu pause freeze` and the tick's auto-resume answer as before. The tick's
auto-resume names a failed recovery by exception class, so it unwraps the completed command and reports
the render's own class; any wrapper added there must do the same.

**Sources and precedence.** Every section goes through `SourceSet.decide` or `mark`; a refused source
reaches no claim:

| source | what it is | what it alone can settle |
| --- | --- | --- |
| `installation` | `instance.yaml`, validated | where this installation keeps its data, and therefore which files a pause acts on |
| `pause` | `<data_dir>/dispatcher/pause.json` | whether the pipeline is paused, in what mode, by whom, since when, and what a resume would put back |
| `liveness` | `<data_dir>/dispatcher/production-state.json` | which dispatcher this is and which heads are behind its cards and sprints |
| `sprints` | the sprint board, one pass | which sprints are open |
| `cards` | the Pipeline, one listing | which cards exist and which sprint holds each of them |

| section | may be answered by | what it says with its input missing |
| --- | --- | --- |
| `target` | `installation` | every field `null` — it is the answer to "which flag", so it survives a flag nobody can read |
| `dispatcher` | `liveness` | `kind: null`, never "production" under a state nobody read |
| `state` | `pause` | every field `null`, `paused` included, sourced `pause` |
| `heads` | `liveness` | `cards: null` and `observers: null`, never `[]` |
| `sprints` | `sprints` | `items: null`, never `[]` — an empty list would claim no sprint is inside a pipeline-wide pause |
| `cards` | `cards` | `items: null`, never `[]`. It does not need `sprints`: which cards exist is the listing's own answer, so an unreadable sprint board does not take the card list with it |

An unreadable pause flag is the `pause` source refusing, and its `reason` states the product's rule:
every production tick reads an unreadable flag as a freeze (`ProductionPause.load`) until repaired.
`paused` stays `null`. `ummanu backup create` treats an unestablished pause state as paused.

A durable file that parses but cannot be converted (a pause flag whose `stopped_worker` is `1`, a
production record whose `attempt_round` is `"not-an-integer"`) is also a refused source: conversions
happen inside the source read. A source read catches everything raised while reading and converting its
one document and returns a refused `Reading` with the cause's type and message. Assembling sections and
documents (`PauseSections`) is outside every such span, so a layer defect travels as itself. A
semantically corrupt flag gets its own reason (the tick still parses and obeys it), not the
unreadable-flag sentence.

**The commands are clients.** `ummanu pause drain`, `ummanu resume`, `ummanu pause-status` and
`ummanu pause-scope` call these operations, print the document, and map codes to exit status as
`ummanu web-run` does: `validation`/`not_found` → 2, `owner_conflict` → 3 (a `pause_conflict`),
`backend_unavailable` → 1. An installation whose config does not validate is `validation` (exit 2).
With an explicit `--data-dir` and an invalid config, the two reads answer from the flag and dispatcher
state and report `installation` unavailable. `ummanu pause freeze` does not go through the layer and
keeps its own path.

## Connecting a project

Low-level onboarding stages:

```bash
python3 -P -m ummanu project add ...
python3 -P -m ummanu project provision-start ...
python3 -P -m ummanu project provision-apply ...
python3 -P -m ummanu project gate ...
```

A project's identity is set once by the top-level binding: `id`, `repo`, `adapter`, `default_branch`.
The mutable `plane`, `policy` and `remote` fields, the optional legacy `orca_binding` (never written
for a new project) and curator-only `curator_roots` are not identity and are carried over by a repeat
`project add`. Scanner and provisioning prepare changes
but do not enable a binding; enabling happens only through a passing gate tied to verified revisions, a
provision run and a write set.

An enabled binding is never rewritten by an ordinary `project add`. `project add --re-onboard` disables
the binding and drops its canonical adapter in the same transition that publishes the new draft, so the
project has no executable adapter until a new gate passes. The binding is written last, so an
interrupted re-onboarding stays visible as the enabled binding it started from and a retry completes it.
It enables nothing and grants the scanner and provision agent nothing.

A disabled binding on another adapter (e.g. `inventory-only`) is moved onto the project's own adapter by
a plain `project add` and stays disabled; provision and gate state reset to pending, and provision run
ids derive from the adapter. An enabled one still refuses.

A takedown opens a new onboarding cycle, recorded in the draft as `onboarding_cycle`; the provision run
id derives from it, so provision results and gate receipts from an earlier cycle cannot be reused on an
unchanged scanner head.

Registration needs no commit. `project add`, `provision-apply` and `gate` write `projects/<id>.yaml`
and `adapters/<id>.yaml` straight into the live root, and the next snapshot exporter window carries
them to the snapshot repository ([Recovery](RECOVERY.md#writers)); drafts, provision runs and gate
receipts stay in `<data>/onboarding/` and are not exported. The live root itself is never a project:
a card whose project repository resolves to it is refused at admission
([Operations](OPERATIONS.md#changing-installation-config)).

Diagnosis, stale draft recovery, re-onboarding and verification:
[Operations](OPERATIONS.md#connecting-a-project-gate-and-stale-input-recovery).

## Memory

Facts are stored flat as `memory/facts/global/<slug>.md` or `memory/facts/<project-dir>/<slug>.md`, one
distilled markdown record per fact. The curator is the writer role; other agents read through
`memory_search`, `memory_get` and `memory_list`.

```bash
python3 -P -m ummanu memory verify --instance INSTANCE
python3 -P -m ummanu memory propose --instance INSTANCE --actor ACTOR \
  --scope SCOPE --slug SLUG --file FACT.md
python3 -P -m ummanu memory commit --instance INSTANCE --actor ACTOR --propose-id ID
python3 -P -m ummanu memory supersede --instance INSTANCE --actor ACTOR \
  --scope SCOPE --slug SLUG --file FACT.md --supersedes OLD-ID
python3 -P -m ummanu memory reindex --instance INSTANCE
```

Write authority is split. `propose` stages a fact in the curator inbox
(`<data_dir>/memory/.staging/<propose-id>`) and touches no canon; `commit` and `supersede` write
`state/memory` of the live root as files, without Git, all or nothing
([Recovery](RECOVERY.md#writers)); their `commit` field is the canon's content revision. **Proposer** roles: `curator`, `ummanu`, `operator`,
`butler`. **Canonical writer** roles: `curator`, `ummanu`, `operator`. A butler's `commit` or
`supersede` is refused with a permission error saying butler proposals await curator review. Every actor
may use only a `source` of its own role, so a butler proposal stays butler-sourced through commit. An
actor commits its own proposal; a `ummanu` or `operator` actor may commit anyone's.

Writer operations require an actor and go through the journal protocol; direct edits bypass the audit
trail. `reindex` changes only the derived index and must not overlap another index writer. Model and
dimension come from instance configuration.

Facts whose owner needs product-owner judgment use the `review:po` scope
(`state/memory/facts/po-review`): a triage basket, not operational truth. Entries carry the
`pending-review` tag, their source sprint/session and candidate project scopes. Interactive PO, curator
and retro identities may read it; worker, reviewer, observer and steward grants never do. The PO reads it
with `scope=review:po` and publishes the resolved fact into `global` or `project:<dir>` with
`supersede`, atomically removing the review entry.

Curator input is a bounded, two-phase batch protocol. Each source is routed from the selected
instance's project registry before selection: a normalized descendant of one registered `repo` routes to
its canonical `id`; an optional safe `orca_binding` adds that binding's Orca workspace tree
(`<workspaces root>/<orca_binding>/...`), which still routes a recorded cwd after the per-card worktree
is removed; optional absolute `curator_roots` name ad-hoc historical checkout trees (curator input
only, no execution authority). Exact directory boundaries and an unambiguous route are required; empty,
relative, unreadable, malformed, unregistered or ambiguous paths are `unknown`; installation-wide
sources are `global`. An observer workspace `workspaces/observers/sprint-<token>` restores the
`sprint:` prefix before consulting the board: one registered reservation routes to that project;
multiple distinct registered reservations route to `review:po`; malformed, duplicate or unregistered
sets are `unknown`.

Harvest, precheck and advance share one cursor-settlement transaction: a curator-local advisory flock
serializes `watermark.json` and `pending.json` (released by the OS if its holder exits).
`harvest --project <canonical-id|review:po>` filters routes before taking the deterministic bounded
prefix; omitting `--project` means all backlog. A pending batch records and signs its selector, so a
retry or advance with a different selector fails closed.
`curator backlog [--project <canonical-id|review:po>] [--json]` reports only aggregate route/head
metadata (session, signal-turn and memory-file counts, timestamp bounds); for a selected project with
no pending batch and baseline-valid cursors, JSON also carries `cutoff.id` and `cutoff.cursor_count`.
It creates no pending record and moves no cursor.

A selected batch with turns or memory is written as a versioned pending record bound to the current
curator workspace/run/session identity and selector; a retry replays it exactly. A scan that classified
only complete non-emitting records advances those cursors atomically without writing `pending.json`.
Advance accepts only the fact-bearing pending form, verifies its identity and each source's starting
cursor, then moves only the listed cursors. Line-based watermarks stay readable until a source advances
to a byte cursor; unversioned, stale, foreign, corrupt or cursor-only pending data is never guessed,
rewritten or advanced. A row-limited scan records the complete non-emitting or oversized records it
classified, so noise prefixes cannot stall a source. An incomplete trailing JSONL record is
source-local: that cursor stays at its last complete record while other safe records settle, and the
partial source is reported and retried after its writer completes the row. Precheck takes the
transaction nonblocking; contention returns the dedicated `102` defer result, which the gate answers
successfully without dispatch or cleanup.

### Project baseline settlement

`python3 -P -m ummanu automations curator baseline` settles existing curator input without running the
curator, changing its schedule or writing facts. It takes one registered canonical project id or
`review:po`, an explicit actor, a non-empty one-line reason, and exactly one evidence identity:

```bash
python3 -P -m ummanu automations curator backlog --project PROJECT --json
python3 -P -m ummanu automations curator baseline \
  --project PROJECT --actor OPERATOR --reason 'reviewed historical backlog' --cutoff-id CUTOFF_ID

# The same audited flow settles manually reviewed multi-project observer input.
python3 -P -m ummanu automations curator backlog --project review:po --json
python3 -P -m ummanu automations curator baseline \
  --project review:po --actor OPERATOR --reason 'reviewed multi-project observer backlog' --cutoff-id CUTOFF_ID

# Or settle the exact fact-bearing pending batch already returned by `harvest --json`.
python3 -P -m ummanu automations curator baseline \
  --project PROJECT --actor OPERATOR --reason 'approved pending batch' --batch-id BATCH_ID
```

The cutoff id (from `backlog --json`) binds the project, each selected source's starting watermark and
its current complete terminal cursor; it is metadata only, never a path, cursor value, transcript or
memory body. A changed source, incomplete JSONL tail, malformed watermark, stale proof or empty cutoff is
refused. A fact-bearing pending batch is bound by its versioned identity, selector and starting cursors;
its `batch_id` is the alternative evidence. A baseline never accepts the all-backlog selector, cannot
bypass a pending record, and rejects a foreign, ambiguous, mismatched, malformed or stale source before
state changes.

The callable API is `ummanu.automations.agents.curator.cli.baseline_settlement` with the same required
`project`, `actor`, `reason` and exactly one of `cutoff_id` or `batch_id`, under the same
cursor-settlement lock. The transition writes the watermark, removes the selected pending record when
settling that batch, and appends to `baseline-audit.ndjson` in the curator state directory, rolling back
on a local write failure. Each audit event records version, time, project, actor, redacted reason,
evidence kind/id, outcome, and hashed affected cursor identities/count; no transcript, memory or fact
text, raw source payload or credential. Output names only the selected project and cursor count.

### Memory read identity

The Memory MCP endpoint requires FastMCP Bearer authentication. Before a head is opened, its launcher
writes a digest-only access grant bound to that exact `HeadRun` and puts the opaque bearer in the
launched process, not in `runtime.env`. The service resolves the grant and rechecks the HeadRun
heartbeat on every read. Missing, expired, malformed, foreign or stopped bindings return a typed
data-free denial. `caller`, `scope` and other tool arguments are never authority.

Resolved read policy: an interactive PO has installation-wide read; a worker or reviewer has exactly its
card's `project:<id>` plus `product:ummanu` (also when the project is `ummanu`); an observer has
its sprint reservations plus `product:ummanu`; the curator and retro standing duties have
installation-wide read; steward has `project:ummanu` and `product:ummanu`. Other runtime roles
have no grant. A requested scope only narrows the set; search never retries wider, and `memory_get` and
`memory_list` use the same guard.

An ordinary Claude or Codex session reaches the interactive identity through the installation-owned
`ummanu-memory-po-bridge` stdio MCP server in its user configuration. The bridge creates a PO
HeadRun, keeps the bearer inside the bridge process, and deletes its heartbeat and grant on exit.
Dispatcher-launched Claude heads use `--strict-mcp-config`; Codex heads disable `po_memory` with a
command-line override. Both get only the direct HTTP `memory` server and their launch-bound bearer.
Client setup: [Operations](OPERATIONS.md#memory-access-from-claude-and-codex).

The memory search log is an authorization audit: every record names its action and the resolved role,
subject, scopes and outcome with result ids/scores, never fact text, queries or bearer material.
Consumers wanting search evidence filter `action == "memory_search"`.

Bearer delivery assumes processes of the same host user are mutually trusted: that user can read
another same-user process's environment or command line. The protocol limits cross-user and stale-head
access and keeps the capability out of durable output; it is not same-user credential isolation.

### Restart health contract

A Memory restart requested by an upgrade, unit change, product-code/dependency change or shipped-pack
reconciliation is not healthy merely because systemd reports it active. Upgrade creates a short-lived
steward `HeadRun` and its launch-bound grant, hands the temporary bindings tree to the configured runtime
account, and a dropped-privilege child of that account publishes the versioned heartbeat and performs
`initialize` then `tools/call(memory_list)` over the MCP endpoint, so the service verifies bearer,
heartbeat and read guard as for heads. The probe supplies neither `caller` nor `scope`, expects a
Ummanu/product-scoped row, and removes its temporary grant and heartbeat afterwards. An unavailable
service, stale or denied identity, malformed MCP reply or missing expected row fails the `memory`
upgrade step visibly; a denial is final, not retried into a timeout. The list tool may return one JSON
text block per row or structured content; the probe normalizes every supported form to rows.

## Reading the pipeline

`ummanu.webproto` is one read layer for "what is running", "what is this card doing" and "what
happened next", used by every transport. It knows nothing about HTTP, sockets, rendering or frameworks,
and writes nothing (no board mutation, dispatcher state, repair or cache). Design:
[Architecture](ARCHITECTURE.md#the-read-layer).

```bash
python3 -P -m ummanu web-read system --instance INSTANCE [--offline] [--json]
python3 -P -m ummanu web-read task --instance INSTANCE --ref REF [--events N] [--json]
python3 -P -m ummanu web-read events --instance INSTANCE --ref REF [--cursor C] [--limit N] [--json]
```

Every document validates against the packaged `web-read` schema and carries `schema_version`, a `kind`
of `system`, `task` or `task_events`, and `observed_at`. Identities are the pipeline's own: a card is its
reference, a project its registered id.

**`system_snapshot()`** — installation health (`collect_status`, as `ummanu status --json`), the
registered projects from validated bindings, the cards in `ready`, `in_progress`, `validate`,
`assessment` and `blocked`, and every head the dispatcher holds with its card and project.

**`task_snapshot(ref)`** — the card as `ummanu task show` reads it, its project and whether that
project is registered, what the dispatcher durably holds for it (attempt, round, gate state, workspace,
heads, pause), the heads working it, what each role ran (`heads`, below), the tail of its history with a
cursor, and its result: the worker's
`report:done` / `report:blocked` (with classification), the reviewer's `review:green` / `review:red`, the
observer's `decision:*`, and the latest of them, marked `terminal` when the card is Done. `card.value`
also carries the card's `origin` ([PO delegation](#po-delegation), with its `returns`), `wait`
([Wait cards](#wait-cards)) and `e2e` ([The e2e stage](#the-e2e-stage)) blocks as `task show` gives them,
each null for a card without one.

**`task_events(ref, cursor, limit)`** — the history, one page at a time, with `next_cursor`.

**`po_delegated(session)`** — the cards whose `origin.po_session` is this PO session (`relation:
delegated`) or whose `origin.current_session` is (`inherited`: the session succeeded the origin), each
`{ref, title, type, state, relation, last_return}`, `last_return` being the card's newest outbox row or
null. One Pipeline listing (`TaskReader.list`: one `getAllTasks`, the metadata in one batch, the outbox
rows in one read) and nothing per card; `items` is null, never `[]`, with an unavailable `source` when
the board could not be read.

### Sources fail apart

Each section of each document always carries a source record: `state` (`available` or `unavailable`),
`reason`, `observed_at` and `data_age_seconds`. An answering source is stamped with the read time and age
0; a refusing one carries why and dates the newest evidence still on disk. An empty list therefore always
means an answer, and an unreachable board store blanks the card list, not the page.

### The four states of an agent

Liveness is process state only. A head's shell publishes a launch-identity heartbeat (pid, boot id,
process start ticks, run id, role, task) before it `exec`s, and the layer classifies it against the
durable `HeadRun`. A terminal or window is never consulted.

| state | what it means |
| --- | --- |
| `running` | a live process whose recorded identity matches this head's run |
| `finished` | the run's stop was confirmed, or its process ended after a stop was asked for |
| `process_failed` | the run still expects a process and the heartbeat names none that is alive |
| `source_unavailable` | the heartbeat itself could not be read; nothing is proven either way |
| `unknown` | no evidence yet: no durable run, no heartbeat published, or a foreign pid |

Every agent row carries the `evidence` it was decided from (heartbeat state, pid, pid file) and states
the invariant in words. It says nothing about the model: that is the head run's row (`heads`, below),
joined to the agent by `run_id`.

### What each run was, and what it ran

`task_snapshot(ref).heads` is one row per head run the card recorded, oldest first, read from the card's
whole committed history and the dispatcher's record in the one traversal the history tail already makes
(:mod:`ummanu.webproto.head_view`), so it outlives the dispatcher's record and a finished card still
answers. A run is a `launch_id` a `routing` or `attempt.usage` event names, or a run the dispatcher record
holds now (`current: true`). Each row joins three sources by that id:

- identity and liveness — `run_id`, `role`, `head`, `current`, `runtime`, `local_pty`, `state`
  (`running`, `finished` or `unknown` from the local-pty supervisor lock) and `reason`; a local-pty
  run has a view at `/tasks/{ref}/heads/{run_id}`;
- the launch configuration from the routing snapshot that recorded the run — `attempt`, `adapter`,
  `model` as configured (an alias such as `opus`, null when the CLI picks), `model_source` and `effort`;
- what the provider journal says the CLI actually ran, from that same run's latest `attempt.usage`
  occurrence by report generation — `resolved_model`, `resolved_models`, `resolved_effort` and
  `resolved_report_generation` (e.g. `claude-opus-5-5` for `opus`; see
  [What a finished phase cost](#what-a-finished-phase-cost)).

The resolved fields are null (and `resolved_models` empty) until a phase of that run finished, and for an
occurrence written before they existed. Because the join is by run, a model one launch resolved is never
shown beside another launch of the same role, and a retained worker resumed for a second round stays one
row. A routing head recorded before launches carried an id is not a run and has no row.

### Continuing a read

The cursor is a position in the committed board audit: the reader resolves the installation's card
client and asks `ummanu.tasks.task_audit_for` for its audit owner, the committed `requests`
traversal ([Board store](BOARD_STORE.md), §7.3). The file projection under `<data>/board` is never
consulted.

The cursor is opaque: a base64 document carrying the card, a position and its kind, `ordinal` (how
many of this card's committed records precede the next). A cursor of any other kind, or with no kind,
is refused (`validation`), never reinterpreted. After a refusal, a client reads a fresh task snapshot, whose `next_cursor` is valid for
the installation's store.

A committed record's position never changes, so:

* reading with a page's `next_cursor` returns what was appended after that page, exactly once;
* reading the same cursor twice returns the same page;
* a cursor issued before new events, read after them, returns exactly those new events.

A page ends with `next_cursor` (always present, even on an empty page) and `has_more` (true only when
`limit` cut the page short). Both record shapes are returned — typed board protocol events and generic
audit records — told apart by `typed`. Uncommitted staged records are not events and no cursor lands
inside them. Omitting `--cursor` starts at the beginning; the `next_cursor` a task snapshot returns is
the end, so a polling client is never handed an event it was just shown.

### Errors

Failures other than a source outage are typed exceptions with the task protocol's codes. The CLI prints
`{"error": {"code", "message"}}` on stderr and exits 2 on `not_found` and `validation`, 1 on
`backend_unavailable`.

| exception | code | when |
| --- | --- | --- |
| `TaskNotFound` | `not_found` | the board answered and holds no card under that reference |
| `InvalidCursor` | `validation` | a cursor this layer did not issue, one belonging to another card, or one past the end of the journal — never silently reset to the beginning |
| `InstallationUnavailable` | `backend_unavailable` | the instance config does not validate, so there is no data plane to read |

`ummanu.webproto.boundary.ProtocolBoundary` wraps every public method of the read and run layers at
class creation and turns implementation exceptions (`RunStoreError`, `OSError`, an unparsable document)
into `backend_unavailable`; a layer defect such as `TypeError` travels as itself. Every file the package
writes goes through `ummanu.webproto.store_io.write_document`, which turns the atomic writer's
`RuntimeError` into `RunStoreError` and therefore `backend_unavailable`. Both are enforced by
`tests/test_web_run_protocol.py` (`ErrorContractTests`, `FileWriteSeamTests`).

## Running the pipeline

Three operations of `ummanu.webproto` raise a real Codex worker for one card, raise a real Claude
reviewer on that worker's result, and read a run. No HTTP, sockets or framework; failures are typed
codes. Design: [Architecture](ARCHITECTURE.md#the-product-runtime).

```bash
python3 -P -m ummanu web-run start  --instance I --ref REF --request-id ID --profile P [--instruction TEXT]
python3 -P -m ummanu web-run review --instance I --worker-run RUN --request-id ID --profile P
python3 -P -m ummanu web-run state  --instance I --run-id RUN
python3 -P -m ummanu web-run list   --instance I --ref REF
```

Every document validates against the packaged `web-run` schema and carries `schema_version`, a `kind`
of `product_run` or `product_review`, and `observed_at`. A run id is `pr-` prefixed.

`--profile` is required, with no default. The profile comes from the head registry and must declare the
`local-pty` runtime; a profile naming any other runtime is refused. `--heads-registry` (or
`TA_HEADS_REGISTRY`) points one run at another registry.

**`run_start(ref, request_id, profile)`** cuts a workspace, raises a worker head into it and points it at
a task document. **`run_review(request_id, profile, worker_run)`** settles the worker run first and
refuses while it is open, then raises a reviewer head in the same workspace with the worker's result.
**`run_state(run_id)`** reads one run and is where a run's ending becomes durable.
**`run_list(ref)`** lists every product run of one card, each item the whole `product_run` document
`run_state` returns.

### The lifecycle of a run, and the order it holds

A run passes through `claimed → raising → raised → settled`, moved only by
`ummanu.webproto.lifecycle.RunLifecycle.advance`. Within `ummanu.webproto`, the backend's `start`
and `stop` and the run store's `settle` are called only from that module (test-enforced).

1. **Write-ahead.** Before a spawn, the record carries what is needed to find and stop the head: run
   directory, pid path and a head description addressed by run id (`raising`). A failure to bind the
   handle after a successful spawn orphans nothing.
2. **Ownership from disk.** The supervised backend addresses a head from the run id and the run's pid
   file and confirms an ending from the launch identity on that path; the write-ahead record alone
   suffices to stop the head.
3. **A possibly-live process outranks closing the record.** Closing a run ends its head first and
   settles only on a confirmed ending. An unconfirmed ending puts the run in `unresolved`: state
   `unknown`, not over, unsettled (so it fences the card). Every later `web-run state` retries the same
   stop, and the run settles when the ending is confirmed, with the state read off the process at that
   moment (a head that published its result and ended settles `finished`).
4. **"Over" and "how it ended" are stored apart.** `ended` (on the record and on `state`) says the
   process is provably gone — a confirmed stop, a launch identity showing nothing there — or none was
   spawned. `state.value` is one of the five words, derived from evidence. Only `ended` frees a card.

`phase`, `ended` and `state` are on every run document.

### What the product owns

The workspace is a detached `git worktree` of the project repository at its declared default branch,
in `<data>/webproto/workspaces/<run-id>`; it has no branch, and nothing here commits, pushes or opens a
PR. The head's process is held by the product's supervisor under `<data>/webproto/heads/<run-id>`, with
`head.pid`, `journal.jsonl`, `supervisor.log`, the task document and `result.json`. The run record is
`<data>/webproto/runs/<run-id>.json`. All paths are on the run document.

A head receives `UMMANU_RUN_RESULT` (the only place a result may appear), `UMMANU_RUN_ID`,
`UMMANU_RUN_ROLE`, `UMMANU_RUN_REF` and `UMMANU_RUN_WORKSPACE`. A `run_state` that finds the
result file ends the head holding it. A run that publishes nothing is ended at its deadline
(`--deadline-seconds`, default one hour).

### One owner of a card

`ummanu.webproto.admission.admit` is the single gate for both start paths, before anything is built
or spawned. Order:

1. the board holds the card — otherwise `not_found`;
2. the card's project is registered and enabled — otherwise `validation`;
3. no open sprint reserves that project (read from `sprints/active-repositories.json`, the index the
   board's write guard uses);
4. the card is not in the production dispatcher's lane (`ready`, `in_progress`, `validate`,
   `assessment`, `blocked`), so a product run takes a card only from `issues`;
5. the dispatcher's durable production state holds no record for the card; an unreadable state file
   refuses too;
6. this layer holds no run for the card that is not over (decided by `ended` only). A run whose cleanup
   could not be confirmed is not over.

Refusals in 3–6 are `owner_conflict`. The dispatcher state is read, never written.

### Idempotency

`start` and `review` are idempotent on `--request-id`. The id is claimed under the run store's lock with
the run id and every decided path before anything is provisioned or spawned, so a repeat returns the
same run and never raises a second head or cuts a second workspace. There is no distributed lock.

The record owned by the id carries the operation (`run_start` or `run_review`) and a digest of its
inputs — card reference, profile and instruction for a start; card reference, worker run and profile for
a review. A repeat disagreeing with either is refused with `validation`. Only the digest is stored.

### What a run ended as

`state.value` uses the agent vocabulary; the process exit status is `state.exit`; `state.ended` says
whether the run is over and is never derived from the value.

| state | exit | ended | what it means |
| --- | --- | --- | --- |
| `running` | — | no | a live process matches this run's launch identity |
| `finished` | any | yes | the run published its result and its process has ended |
| `finished` | `code: 0` | yes | the process ended normally, having published nothing |
| `process_failed` | `code: N` | yes | the process exited with a non-zero status |
| `process_failed` | `signal: N` | yes | the process was ended by a signal |
| `process_failed` | none | yes | the process is gone, published nothing, and nothing recorded how it ended |
| `source_unavailable` | — | yes | the head is gone and its journal could not be read: it ended, and how was not established |
| `source_unavailable` | — | no | the launch identity itself could not be read; nothing is proven about the process either way |
| `unknown` | — | no | no evidence yet: no head raised, or no heartbeat published, or a foreign pid; or a run whose cleanup could not be confirmed (`phase: unresolved`), where nothing establishes what the process is doing |

A settled run is over whatever value it carries.

Evidence is the launch-identity heartbeat, the supervisor journal and the result file; a window or
terminal screen is never evidence. The first observation of an ended run settles it, recording state,
reason, exit status and result together, and a settled run answers the same forever, even after its run
directory is swept. A failed bring-up settles `process_failed` with its named cause.

### Where a run is read back

A run publishes exactly two events, `product_run.started` and `product_run.finished`, into the board's
append-only journal, so `ummanu web-read events --ref REF` and `web-read task` show them. There is no
separate run history store. Both are idempotent through the audit's request-id ownership. They are
generic audit records, not typed Card events: a product run moves no card and wakes no observer.

Every path restores publication: a `start` or `review` returning an existing run, and every `run_state`
of a settled run, republish what that run owes before answering, and a publication failure is reported,
not swallowed. Both events are pure functions of the run record (including `occurred_at`), so a replay
matches the journal's record. The run record keeps the exit status and result so the terminal event can
be rebuilt after the run directory is swept.

### Errors

The read layer's exceptions plus two only a mutation can raise. The CLI prints
`{"error": {"code", "message"}}` on stderr and exits 2 on `not_found` and `validation`, 3 on
`owner_conflict`, 1 on `backend_unavailable`.

| exception | code | when |
| --- | --- | --- |
| `TaskNotFound` / `RunNotFound` | `not_found` | no such card, or no such run on this installation |
| `ValidationRefused` | `validation` | no request id, a request id already owning another operation or another request's inputs, no profile, an unlaunchable profile, one on another backend, or an unregistered project |
| `OwnerConflict` | `owner_conflict` | somebody else owns this card — an open sprint, the dispatcher's lane, its durable record, an unsettled run of this layer's, or a worker run that has not ended yet |
| `RuntimeUnavailable` | `backend_unavailable` | the workspace, the head or the run's own record could not be made |

## Opening and watching a sprint

Three operations open, comment on and close a sprint; five reads answer what a sprint can be built from,
what one sprint is doing, what every sprint is doing, what happened to one comment, and what one close
decided. Same properties as the layers above: no transport knowledge, typed codes, per-section
availability.

Every document validates against the packaged `web-sprint` schema and carries `schema_version`, a
`kind` of `sprint_options`, `sprint`, `sprint_list`, `sprint_created`, `sprint_comment`,
`sprint_comment_delivery`, `sprint_closed` or `sprint_close_result`, and `observed_at`. Identities: a
sprint is `sprint:N`, a product its id, an issue `issue:*`, a project its registered id, a head profile
its registry id.

Every rule stays with its writer. `SprintWriter.create` decides what a sprint may be (existing product,
at least one open issue of it, registered projects, no project another open sprint reserves, an observer
that is a registry profile or `none`, executor pins from the same registry); the operation calls it. No
second admission gate, audit, reservation index or transaction; see [Sprints](#sprints).

There is no "start" operation: the production tick raises one observer per open sprint that has none, so
opening a sprint with an observer starts it. The operation returns where the sprint is (launch states
below).

### What a sprint can be built from

**`sprint_options()`** answers four sections:

| section | source | what is in it |
| --- | --- | --- |
| `products` | `ProductIssueStore.list_products` | id, label, ref, and the projects the product names |
| `issues` | `ProductIssueStore.list_issues` | the **open** issues only, each with the product that owns it |
| `projects` | `registered_projects` plus `sprints/active-repositories.json` | id, label, and `reserved_by`: the open sprints holding it |
| `heads` | the installation's generated `<data>/heads/heads.yaml` | every profile with its model, effort, adapter and resource |

Only open (admissible) issues are offered. `reserved_by` is `null`, never `[]`, when the reservation
index could not be read.

Head profiles are read from the installed registry. Each entry carries `id`, a `label` built from the
registry (`codex · gpt-5.6-sol · medium effort`), `model`, `effort`, `adapter`, `resource`, the roles it
is the registry default for, and `observer`: whether a sprint may declare it as observer, decided by
calling `check_observer_profile` (the same call create makes). `heads.observer` carries `none` and the
registry's observer default.

### Opening one

**`sprint_create(request_id, actor, product, goal, issues, projects, observer, …)`** opens a sprint and
answers `kind: sprint_created`: the request id, whether this call claimed it, and the full `sprint`
document.

`observer` is required: a profile id or `none`. `worker` and `reviewer` are optional; `None` travels
into `SprintWriter._executor_intent` and the row gets no field for that role. `""` and `none` are
refused. See [The optional executor pins](#the-optional-executor-pins).

### Idempotency

`sprint_create` is idempotent on `request_id`. The id is claimed in the layer's request index
(`<data>/webproto/sprint-requests/<digest>.json`, id digested) **before** the writer is called, passed
down to `SprintWriter.create`, and the produced sprint reference is recorded under the id afterwards. A
repeat finding a recorded reference answers from it without calling the writer. The record carries the
operation and an input digest; a repeat with different product, goal or pin is `validation`. No
distributed lock.

**A partial failure is repeated, not restarted.** Between claim and recorded reference (and, one level
down, between the writer's board row and its publishing reference) a sprint may exist unnamed. The
repeat with the same request id resumes the writer's staged transaction. Such a create is refused with
`backend_unavailable` and an `OperationPending` whose `data` says so:

```json
{"code": "backend_unavailable",
 "message": "...",
 "data": {"reason": "sprint_create_pending_repair",
          "action": {"operation": "sprint_create", "repeat_request": true,
                     "request_id": "…", "reference": null}}}
```

`repeat_request` is the only safe action (a new request id would open a second sprint). `reference` is
filled in when the layer knows which sprint the request holds.

Once `SprintWriter.create` has returned, every remaining step (recording the reference, reading the
sprint back) runs in one region that answers this way whatever raises, with the cause chained. The
message states the durable fact (which sprint exists, that repeating is safe) before the cause.

### Watching one

**`sprint_state(ref)`**: the goal and Definition of Done, product, issues, reserved projects,
repositories, status, current card, each executor pin's state, the last observer resume entry, whether
its observer is up, and what the sprint is doing (`work`, below).

The sprint's fields and the observer's liveness fail apart: an unreadable dispatcher state leaves the
sprint's fields and marks liveness unavailable. The sprint is read through `SprintReader.list`, never
`show` (which would create the sprint board).

Liveness comes from the dispatcher's production state, classified by the same `observer_snapshot` rows
`ummanu sprint status` shows; no terminal screen is evidence. `observer.launch.state` is one of:

| state | what it means |
| --- | --- |
| `not_started` | the entity is saved and the production tick holds no observer record for it yet |
| `running` | the dispatcher holds a record and its head is alive |
| `unavailable` | the production state could not be read, or the sprint board could not be: nothing is established either way |
| `stopped` | a record exists and its head is not alive — which is neither of the two above |
| `not_declared` | the sprint declared `--observer none`, so the tick raises none for it |
| `ended` | the sprint is closed or stopped: the tick stopped its observer and holds no record |

`observer.declared` carries what the row declares: `declared`, `absent` or `malformed`. With the sprint
board unavailable, `launch` is `unavailable` sourced from the sprint board and `declared` is `unknown`.

### Commenting on a running sprint

**`sprint_comment(request_id, actor, reference, body, role="po")`** puts one comment on a sprint entity
and answers `kind: sprint_comment`. It is the PO's intervention in a running sprint; no operation edits
the sprint's executor cards.

| field | what it carries |
| --- | --- |
| `request_id` | required; the idempotency key of this one comment (below) |
| `ref` | the sprint the comment is on |
| `comment_id` | the **durable identifier of the comment**: the committed audit event id `SprintWriter._write` minted for it. It is what a later read takes back, and it is deliberately not a board row number a caller would have to know how to interpret |
| `saved` | whether *this* call saved the comment. `false` is a repeat that found it already saved and wrote nothing |
| `delivery` | the whole `sprint_comment_delivery` document below, embedded exactly as a create embeds the sprint |

`SprintWriter.comment` owns the rules: allowed roles (`po`, `dispatcher`, `worker`, `reviewer`,
`steward`, `retro`) and a non-empty body. A closed or stopped sprint accepts a comment; see
[A comment on a sprint that has ended](#a-comment-on-a-sprint-that-has-ended).

**Idempotency** is the audit's own claim: `SprintWriter._write` claims `request_id` in the committed
audit before the board is touched, and a repeat is answered from the committed event without calling
the mutation — no second comment, audit event, observer wake or head launch. There is no second request
index. `sprint_comment` compares the event the id already owns (kind, sprint, actor, body digest) with
the request and refuses a mismatch with `validation`.

**A half-written comment is repeated.** `audit_pending` from the writer reaches the caller as an
`OperationPending` (`backend_unavailable`) with a `data` action naming `sprint_comment` and this request
id.

### What happened to a comment

**`sprint_comment_delivery(ref, comment_id)`** answers three separate things:

* **`comment`** — whether the committed audit holds this comment on this sprint (`saved`), holds no such
  event (`absent`), or could not be read (`unknown`). Sourced `journal`;
* **`delivery`** — where the dispatcher's observer delivery has got it. Sourced `liveness`, needing
  `journal`;
* **`acceptance`** — read from no source, always `established: false`: neither of the above means the
  observer read or accepted the comment. The answer names `issue:cf5c9f03ee0f92d3d347` as the mechanism
  that would establish it.

Delivery is a batch fact: the dispatcher cursor is `through_event` over the installation's committed
event stream, so the answer is the relation between this comment's event and the cursors:

| what the record says | answer | why |
| --- | --- | --- |
| no observer record for this sprint | `unknown` | nothing here establishes where the comment is — never "not delivered" |
| the production state could not be read | `unknown` | the source that would say refused; the section is sourced `liveness` and unavailable |
| the committed audit could not be read | `unknown` | the comment and the cursors cannot be placed in one order; sourced `journal` and unavailable |
| a cursor names an event the audit does not hold | `unknown` | the same: the comment cannot be placed against it |
| `acknowledged_through` is this comment's event or a later one | `handed_over` | the batch that carried it was acknowledged by the head that was woken for it |
| stage `waiting_for_idle` | `waiting` | a batch is owed and is held until the head is idle; it carries everything after the acknowledged cursor |
| stage `delivery_intent` or `awaiting_ack`, `through_event` at or after this comment | `waiting` | the batch was fixed and sent and is not acknowledged |
| stage `retry_deferred` over the same range | `error`, with `last_failure_reason` | the batch failed and the dispatcher is retrying it |
| stage `idle`, or an active batch fixed *before* this comment arrived | `saved` | no batch carries it yet; an event appended after a delivery intent is deliberately left for the next batch |
| the sprint is closed or stopped | `not_deliverable` | no batch will *ever* carry it: the tick stops the observer of a sprint that is no longer open and drops its record. Answered before the dispatcher is consulted at all, because a dropped record would otherwise read as `unknown` for something that is exactly known |

`unknown` is never folded into another answer.

`batch` carries the delivery record the answer stands on — stage, delivery and cursor ids, wake and
launch failure counts, last failure reason, and the `delivery_evidence_summary` line — or `null` when
there is no record.

Limits:

* `handed_over` means a batch carrying the comment was acknowledged, not that the observer read or acted
  on it, or that the comment caused the batch;
* a comment by a role other than `po` is not a semantic wake (`is_significant_observer_event`); it is
  carried when a later significant event moves the cursor past it;
* the answer is as fresh as the durable state; it consults no terminal or head.

The read performs no delivery: no wake, nudge, retry, head launch or dispatcher-state write.

### Closing one

**`sprint_close(request_id, actor, reference, reason, closeout, decisions, role="po")`** closes a sprint
and answers `kind: sprint_closed`. It takes the owner's reason, the decisions file
([The decisions a close carries](#the-decisions-a-close-carries)) and the closeout body, and calls
`SprintWriter.close`.

| field | what it carries |
| --- | --- |
| `request_id` | required; the idempotency key of this one close, and the id whose repeat resumes a half-finished one |
| `ref` | the sprint that was closed |
| `event_id` | the **durable identifier of the close**: the committed audit event id, and what `sprint_close_result` takes back |
| `definition_of_done` | `satisfied: false`, always, with the sentence saying why the question is not what a close answers |
| `result` | the whole `sprint_close_result` document below, embedded exactly as a comment embeds its delivery |

Every close rule (decisions, pre-write refusal, terminal phase order, admission lock, per-step request
ids, `live_work`, `close_conflict`, confirmations, `audit_pending` with the staged plan retained, the
knowledge closeout) belongs to `SprintWriter.close` and `ummanu.sprint_close`; the operation adds no
store, lock or scheduler.

The closeout is required by this operation; its content is the caller's, verbatim.

**Idempotency** is the staged close's own: a repeat resumes it, repeats no step whose derived id carries
a committed event, and is refused with `validation` when it states other decisions, reason or closeout.

**A half-finished close is repeated.** `audit_pending` reaches the caller as an `OperationPending`
(`backend_unavailable`) with a `data` action naming `sprint_close` and this request id. `live_work` and
`close_conflict` are `owner_conflict`: settle the running head or the moved object, then repeat.

**The close ends no head.** It releases reservations; the production tick stops the observer of a sprint
that is no longer open.

### What a close decided

**`sprint_close_result(ref, event_id)`** is the read half:

* **`close`** — what the committed audit records: verdict per declared issue and which were closed,
  disposition per card and which were archived, who closed and why, the closeout's path and commit.
  `absent`: the journal holds no such close; `unknown`: unreadable. Sourced `journal`;
* **`reservations`** — which declared projects the installation still holds for the sprint, from
  `sprints/active-repositories.json`. Sourced `reservations`, needing `sprints`. An unreadable index
  answers `null`, never "released";
* **`sprint`** — the sprint's record with its new status. Sourced `sprints`;
* **`definition_of_done`** — from no source, always `satisfied: false`.

### One place says which source answered

> A source that refused, or that was never read, may not delete, shadow or fabricate an answer
> another source already gave. Every section says which source answered it, and an answer is
> attributed to the source that actually produced it.

`ummanu.webproto.section` enforces this; every section of every sprint document is assembled there:

* a **source is read once** per document, as a `Reading`, whose payload is unreadable when it did not
  answer;
* a **section is decided by rules**, each naming the source it answers from and every source it needs; a
  rule does not run unless all of them answered. The section carries and names the `Source` of the rule
  that answered;
* **what a refusal may say is declared once per section** as the field values that claim nothing. When no
  rule can answer, the section is that blank, attributed to the first refused source in the precedence
  order below, with its reason. Answering under a refused source fails;
* a section that **cannot answer when every source answered** is raised as a layer defect;
* **a section carries its provenance**: `render` refuses a plain mapping or a section this module did not
  decide, and `SectionSet` wraps every public method of `SprintSections` so each must return a section
  produced by `decide` or `mark`. A directly constructed section cannot reach a document.

Pinned by `tests/test_web_sprint_protocol.py` (`SectionSeamTests`, `SourceIsolationMatrixTests`).

**Sources, in precedence order** (the order a refusal is attributed in):

| source | what it is | what it alone can settle |
| --- | --- | --- |
| `installation` | `instance.yaml`, validated | where this installation keeps its data, and its own budget thresholds |
| `sprints` | the sprint board, one pass with batched metadata | which sprints exist, and everything on their rows |
| `cards` | the Pipeline, one listing with batched metadata | which column each of a sprint's cards stands in |
| `journal` | `board/events.ndjson`, the committed audit | when the last significant event on an open sprint's cards happened, and when the current card last moved |
| `liveness` | `dispatcher/production-state.json` | whether a head is really behind a card, and behind a sprint |
| `heads` | the installed head registry (`<data>/heads/heads.yaml`) | which adapter, model and effort each profile a sprint names configures |

The journal is its own source, read once and handed to `SprintReader.status_views`. Both documents carry
`cards`, `journal`, `liveness` and `installation` beside their items.

### What a sprint is doing

A listing item and the watched sprint's `work` are built by the same call over the same sources.

**Per-section source contract** (exhaustive: the builders of `SprintSections`):

| section | may be answered by | what it says with an input missing |
| --- | --- | --- |
| `sprint` (watched) / `sprints` (listing) | `sprints` | `value: null` / `items: null` — never an empty listing, which would claim this installation holds no sprints |
| `current_task` | `sprints` | `ref: null`, `live: false`, sourced `sprints` |
| `current_card_state` | `sprints` for `not_applicable`; `journal` otherwise, which also needs `cards` | `unknown` with `state`, `since` and `age_seconds` null, sourced by whichever of `sprints`, `cards`, `journal` was missing first; `card` still names the current card wherever the row answered |
| `decision` | `sprints` | `entry: null`, sourced `sprints` |
| `decision.freshness` | `sprints` for a closed or stopped sprint (its record is frozen); `journal` for an open one, which also needs `cards` | `value: null`, sourced by whichever of `sprints`, `cards`, `journal` was missing first |
| `cards` | `cards` | `states: null`, never `{}` |
| `degraded_cards` | `liveness` | `items: null`, never `{}` |
| `checks` | `sprints` for `not_applicable`; `liveness` otherwise | `unknown`, sourced `sprints` or `liveness`; `card` still names the current card wherever the row answered |
| `waiting` | `sprints`, then `cards`, then `liveness`, then `cards`, then `liveness` (below) | `unknown`, sourced by the first missing input, and its reason names the column the board *did* establish where it did |
| `head_profiles` | `heads` (the installed head registry), which also needs `sprints`, and `liveness` when it answered | every role `null`, sourced by the first of `sprints`, `liveness`, `heads` that was missing; a `liveness` alone missing costs only `via: launched` |
| `observer.declared` | `sprints` | `unknown` — never `absent`, which would be a claim about a row nobody has seen |
| `observer.launch` | `liveness`, which also needs `sprints` | `unavailable`, sourced `sprints` or `liveness` — never `not_started` |
| `comment` (delivery document) | `journal` | `unknown`, sourced `journal`; `id` still names which comment the answer would have been about |
| `delivery` (delivery document) | `journal` when it holds no such comment; `liveness` otherwise, which also needs `journal` | `unknown` with `batch: null`, sourced by whichever of `journal`, `liveness` was missing first |

An unreadable `journal` marks only `decision.freshness`. An unavailable `sprints` yields no affirmative
observer claim.

**`waiting_on`** is what the sprint waits for, beside `waiting` (which is about the current card only): a
list of `{kind, card, detail}`, one entry per waiting live card of the sprint, derived at read time from
the sprint's cards in the Pipeline listing and nothing else -- no dispatcher record, no journal, nothing
stored (secretary-1811). It is a list rather than a section; the document's `cards` mark is its source.
`kind` is exactly one of:

| kind | a card of the sprint that is | `detail` |
| --- | --- | --- |
| `run` | a `wait` card not Done or Blocked whose `wait.state` is `waiting` or `result_ready` | `waits for <run URL, card reaching states, or time> since <t>, deadline <d>` (and `result ready, delivery pending`) |
| `run` | a card not Done with an e2e run still `dispatching`, `identifying`, `wait_card_pending` or `waiting`; or any card carrying such an after-merge run | `e2e run <run URL> on <sha>: <state>` |
| `run` | a merged card whose after-merge mark is `covered` -- every card a coalesced run covers, not only its carrier -- unless the carrier's own record of that run has answered | `after-merge e2e run <run URL> carried by <carrier>, covering merge <sha>` (the dispatch id while the run is not identified) |
| `run` | a merged card whose after-merge mark is `pending` (queued, no run covers it yet) | `queued for the next after-merge run` |
| `owner` | a card carrying the `waiting_owner` mark ([Handover to the owner](#handover-to-the-owner)) | `<kind> handed to the owner: <reason>` |
| `owner` | a card waiting on an e2e budget decision (`e2e.mark`) | the mark, `e2e: budget spent, waiting on <decision>` |
| `po` | a `decision` or `operation` card In progress without the mark | `<kind> card with the PO` |

One card may carry more than one entry (a run and a budget mark), but one run is said once per card:
the carrier's run record and its own `covered` mark are one entry, deduplicated by (card, run URL or
dispatch id). A `green`, `red` or `declined` after-merge mark waits for nothing. The list is empty when nothing is
waited for and for a closed sprint; it is null, never `[]`, when the sprint board or the listing did not
answer. `ummanu sprint status` prints it under `work.waiting_on`, every `sprint_list` item carries it,
and the sprint page draws it as "Waiting on", each entry linking its card and any URL in its detail.

**`waiting`**, in decision order:

| answer | source | when |
| --- | --- | --- |
| `ended` | the sprint row | the sprint is closed |
| `blocked` | the sprint row | the sprint is stopped, with its stop reason |
| `waiting` | the sprint row | the sprint has no current card |
| `blocked` | the Pipeline listing | the current card stands in Blocked, with its `blocked_by` — answered before the production state is consulted at all, readable or not |
| `waiting` | the Pipeline listing | the current card is in Ready, Issues or Done, and the dispatcher has nothing to add: its state could not be read, or it holds no record for the card |
| `blocked` | the production state | the current card stands in an active column with no worker the dispatcher can name |
| `waiting` | the production state | the dispatcher holds no record for a card in an active column |
| `working` | the production state | the dispatcher's record for the current card, and the state it is in |
| `unknown` | the production state | the card is in an active column and the production state could not be read — the reason still names the column, because that much *was* established |
| `unknown` | the Pipeline listing | neither the listing nor the production state could be read: the listing is the first input the chain was missing, so it is what the section names |
| `unknown` | the sprint row | the sprint board could not be read, so there is no sprint here to be waiting |

A column is not evidence that a head is behind it, so the board alone settles only Blocked, Ready,
Issues and Done. `checks` is `not_applicable` from the sprint row for an ended sprint or one with no
current card, never under an unavailable production state.

A closed sprint has no current card: `current_task.ref` (and `sprint.value.current_task`) is null
whatever its row stores, and the reason names no card (`ummanu.sprints.public_current_task`, the
same rule `ummanu sprint show` prints through). A stopped sprint may be resumed, so it keeps its
card, and `current_task.live` qualifies it as the record of an ended sprint rather than work in
progress. The watched sprint document carries the sprint's `status` as its first key, so
`ummanu sprint status` names it first. `cards.states` and `degraded_cards.items` are `null`, never `{}`, when their
source is unreadable.

**`current_card_state`** is where the current card stands and since when, and it is a section of its own
because `current_task` is the sprint row's alone: a journal or a Pipeline listing that could not be read
marks this section and never blanks the card's reference or the sprint's row. `state` is the column the
Pipeline listing holds the card in, and `title` the card's title from that same listing entry (null when
the listing holds no such card, or the section is `not_applicable` or `unknown`). `since` is the card's **last state transition** on the committed
audit -- read in both shapes history holds, a typed event's `transition.source`/`transition.target` and a
legacy `moved` event's `payload.from`/`payload.to` (`ummanu.tasks.recorded_card_transition`) -- and never
`updated_at`, which moves for a comment, a report or any other edit, and never the newest event of any
kind. `age_seconds` is how old that moment was when the document was read. `transition` is `recorded`
when the journal holds one, `absent` when it answered and holds none for this card (which is never
spelled as a zero age), `not_applicable` for a sprint with no current card or one whose card is where it
ended -- decided from `current_task.live` rather than by re-deriving the rule -- and `unknown` for a
source nobody could read. The journal is walked once for the whole document, as every other source is.

**`head_profiles`** is `{observer, worker, reviewer}`, each `{profile, via, registered, label, adapter,
model, effort}` joined against the installed head registry, so a page never joins a sprint against the
registry itself. The observer's profile is the head its dispatcher record names (`via: launched`), else
the row's declaration (`declared`; `none` for a sprint without one, `undeclared` when the row names no
profile). A worker or reviewer is the row's executor pin (`pinned`); an unpinned role has no profile and
`via: unset` (or `malformed`), because the dispatcher then chooses per card and that choice belongs to the
card (`web-read task` `heads`). A profile the registry no longer describes keeps its id with `registered:
false` and null details. `model` and `effort` are what the profile configures (an alias such as `opus`);
the model a CLI actually resolved is known per card only.

`checks` is the mechanical gate as the dispatcher record holds it; nothing is re-run and no CI backend is
called. `gate` carries only the recorded `state`, attested SHA, whether a run is pending, last transport
error and the record's state. `unknown` means no dispatcher record names the card (or the state is
unreadable) and is never folded into `not_green`. `not_applicable` is a sprint with no current card, or
one that has ended.

### Listing them all

**`sprint_list(statuses=…)`**: `sprints.items` has one entry per sprint with `ref`, `goal`, `status`,
product, issues, reservations, repositories, executor pins, budget, its `observer` section as
`sprint_state` gives it, and the `work` sections above.

`statuses` filters on `open`, `closed` and `stopped` after the single board pass; an unknown status is
`validation`.

Cost per document regardless of sprint count: one sprint-board pass with batched metadata, one Pipeline
listing with batched metadata, one read of production state, at most one committed-audit traversal. No
sprint comments, card opens, CI calls or terminals. `cards.source`, `journal.source`,
`liveness.source` and `installation.source` carry document-level availability.

`ummanu sprint list` and `ummanu sprint status` are clients of these two reads and map codes as
`web-read` does (`not_found`/`validation` → 2, `backend_unavailable` → 1). `ummanu sprint comment`
and `ummanu sprint comment-delivery` are clients of the comment operations the same way, with
`sprint comment` using `web-run`'s table so `owner_conflict` exits `3`. `sprint comment` on a `closed` or
`stopped` sprint succeeds with exit status `0` and the saved comment on stdout
([A comment on a sprint that has ended](#a-comment-on-a-sprint-that-has-ended)); its delivery reads
`not_deliverable`. `owner_conflict` (exit `3`) remains for `sprint resume` and `sprint current-task` on
an ended sprint and for create and close conflicts. The comment command mints a `--request-id` when none
is given; retry with the same id to get the same comment back.

The installation config is one more source. An invalid config removes only the data-plane location and
the installation's budget thresholds (which fall back to defaults). With an explicit `--data-dir` and a
usable board transport, `sprint list` and `sprint status` still answer and report `installation`
unavailable; without a data directory the caller is refused with `backend_unavailable`.

The reads create nothing: sprints via `SprintReader.list(create=False)`, cards via `TaskReader`.

### Errors

No new codes. The writer's `TaskError` codes are mapped once, in `ummanu.webproto.sprint_ops`:

| writer code | exception | code | when |
| --- | --- | --- | --- |
| `validation`, `role_forbidden` | `ValidationRefused` | `validation` | a closed or foreign issue, an unregistered project, an unknown observer or executor profile, a missing request id, a repeat over different inputs |
| `not_found` | `TaskNotFound` | `not_found` | the board holds no such product, issue or sprint |
| `sprint_conflict`, `resource_conflict`, `closed` | `OwnerConflict` | `owner_conflict` | an open sprint already reserves one of these projects, this installation is at its open-sprint limit, or the sprint a resume or a current task names has ended (a comment on it is accepted, not refused) |
| `audit_pending` | `OperationPending` | `backend_unavailable` | the create, the comment or the close is part-done and repairable with the same request id (above); a repeated close continues the same terminal phase and writes no second closeout; the `data` action names which operation |
| `backend_error`, anything else | `RuntimeUnavailable` | `backend_unavailable` | a durable source of this installation refused |

## What has been commanded, and what became of a request

`ummanu.webproto.command_reads` adds two reads over the committed audit:

```bash
python3 -P -m ummanu web-read commands --instance INSTANCE [--cursor C] [--limit N] [--json]
python3 -P -m ummanu web-read request  --instance INSTANCE --request-id ID [--json]
```

Both documents validate against the packaged `web-command` schema and carry `schema_version`, a `kind`
of `command_history` or `command_request`, and `observed_at`.

**`command_history(cursor, limit)`** — a page of the last commands across every entity, newest first.
Each row has `actor`, `action`, `entity` (reference, plus entity kind where the record carries one) and
`result` (the `reason` of a typed protocol event, or the `outcome` of a generic audit record; never
renamed into each other). No second store, index, cache or scheduler.

**`command_request(request_id)`** — what became of one request id:

| state | what it means |
| --- | --- |
| `committed` | the operation finished. Its action, entity, actor, result and event id are on the answer, and `staged` says whether an uncleared staged record still stands beside it (an owed audit repair) |
| `pending` | a staged record exists and no committed one does. What it did is durably recorded and may be part-done; `continuation` names the safe move -- repeat this same request id |
| `not_found` | the audit answered and holds neither a committed nor a staged record under this id |
| `unknown` | the audit could not be read, so nothing is established. Never folded into `not_found`: "this installation never saw that request" and "nobody could say" are opposite answers |

It reads the audit's `committed_event` and `pending_event` (the pair `SprintWriter._write` consults) and
never performs, retries, resumes or repairs the operation. Both reads write nothing.

### Paging and honesty

The cursor (`ummanu.webproto.cursor`) is a position in the traversal's append-ordered sequence, bound
to no entity; a card cursor and a history cursor are not interchangeable. `next_cursor` continues into
older commands; `has_more` is true only when the limit cut the page. `DEFAULT_LIMIT` is 50 and
`MAX_LIMIT` 500. Newest first is reversed append order, not a sort by `occurred_at`.

Both reads go through `ummanu.tasks.task_audit_for`, whose canon is the `requests` table
([Board store](BOARD_STORE.md), §7.3); the file journal is never consulted.

An unreadable audit is an unavailable source, never an empty history: `items` is `null` with a reason,
including a store that will not answer. An empty
`requests` table is an honestly empty history. A record's entity kind is `null` when not carried, never
inferred.

### Operation identity, in one place

Every mutation of the layer either takes a `request_id` or deliberately takes none. The table is
published as `ummanu.webproto.command_reads.OPERATION_IDENTITY`, travels on every `command_request`
answer, and is derived from the operation signatures by `tests/test_web_command_protocol.py`, which holds
this table to it.

| operation | identity | what a repeat means, or why there is no key |
| --- | --- | --- |
| `run_start` | `request_id` | returns the same run; it raises no second head and cuts no second workspace |
| `run_review` | `request_id` | returns the same reviewer run over the same worker result, and raises no second reviewer head |
| `sprint_create` | `request_id` | resumes the sprint this request already opened, finishing whatever step was owed; a new id would open a second sprint beside the half-written one |
| `sprint_comment` | `request_id` | returns the comment this request already saved, and never writes a second one |
| `sprint_close` | `request_id` | resumes the staged close, keeps its plan and repeats no committed step; a new id would open a second close beside a half-finished one |
| `pause_drain` | none | the pause is idempotent in its own mode: a drain over a draining pipeline changes nothing and says so, and a drain over a freeze is refused as a conflict |
| `pause_resume` | none | a resume over a pipeline that is not paused is a no-op that reports itself as one; its idempotence is the state of the flag, not a recorded request |

Part-done and related failures:

* **`OperationPending`** (code `backend_unavailable`) — durably part-done and repairable; repeat *this*
  request id, which `data.action` carries with `repeat_request`, the operation name and the reference
  where known.
* **`audit_pending`** (exit status `4`) — the writer's spelling of the same fact; the staged record is
  kept and a repeat with the same request id resumes it.
* **`close_conflict`** (exit status `3`) — refused on the state of the world; nothing was written; not
  part-done.
* **`PauseCommandCompleted`** — the pause or resume completed and only its report could not be rendered;
  the command answers with what it did.

### Errors

Neither read has a code of its own; any other durable-source failure is `backend_unavailable` through
`ummanu.webproto.boundary`.

| read | code | when |
| --- | --- | --- |
| `command_history` | `validation` | the instance config does not validate, or the cursor is one this reader did not issue, belongs to a card, or is past the end of a journal that only grows |
| `command_request` | `validation` | the instance config does not validate, or no request id was given |

## Serving the pipeline locally

The web transport is a caller of `ummanu.webproto` beside `web-read` and `web-run`: one route is one
operation, with no snapshot, state derivation, liveness rule or mutation of its own. Design:
[Architecture](ARCHITECTURE.md#the-web-transport); running it:
[Operations](OPERATIONS.md#the-local-web-transport).

```bash
python3 -P -m ummanu web-serve --instance INSTANCE [--data-dir DIR] \
  [--host 127.0.0.1] [--port 8787] [--heads-registry REGISTRY] [--offline]
```

**Loopback only.** The service has no password, TLS or authorisation, and two routes start real heads.
`--host` is resolved before a socket exists and refused unless every resolved address is loopback;
the literal resolved address is bound. External access goes only through the guarded front
([below](#publishing-the-pipeline-the-guarded-front)). Pages call operations in-process, so there is no
second internal HTTP surface.

### Routes

The table is the whole externally reachable surface. No route takes a command, script, path or module
to run, and there is no catch-all (`tests/test_web_transport.py`). An unrouted path is 404 and an
unrouted method on a routed path is 405; neither reaches a handler.

| method | route | operation | answers |
| --- | --- | --- | --- |
| GET | `/` | `reads.system_snapshot` (+ `po.po_running_count`) | the compact dashboard: pipeline controls and running PO turns, the current health problem, open sprints with their card, heads and budget |
| GET | `/tasks/{ref}` | `reads.task_snapshot` (+ `ops.run_list`, `po.po_session_titles`) | one card: its full description, state, attempt, heads with model and effort, product runs, worker and reviewer output, result, event tail; and, when the card carries them, the **Delegation** block ("Delegated by" the PO session's title linked to `/po/sessions/<id>`, its short id when untitled or gone, the successor the result goes to now, and each return: state, delivery, when, which session received it), the **Wait** block (the target as a run link, a card link with the awaited states, or a time; waiting since, deadline, state, the frozen result with its evidence link, and each return address with its delivery) and the **E2E** block (each run's SHA, link, state or result and wait card; the budget or cap, the budget mark with its decision card, and the after-merge state and runs). A PO store that does not answer marks the titles unavailable and leaves the rest of the page |
| GET | `/tasks/{ref}/heads/{run_id}` | `reads.head_view` | one of the card's local-pty heads, read-only: its terminal's tail as redacted plain text and its journal's tail; a run id the card did not record is 404 |
| GET | `/sprints` | `sprint_reads.sprint_list` | active sprints or the searchable `?view=archive`, optionally filtered by `q` and `project` |
| GET | `/projects` | `reads.system_snapshot` | registered projects |
| GET | `/projects/{project}` | `reads.system_snapshot` (+ `sprint_reads.sprint_list`) | one project's registration details and collapsible sprint list |
| GET | `/sprints/new` | `sprint_reads.sprint_options` | the "new sprint" form, on this installation's own products, open issues, projects and head profiles |
| POST | `/sprints` | `sprint_ops.sprint_create` | open one sprint from that form; 303 to its page, or the form again with what was refused |
| GET | `/sprints/{ref}` | `sprint_reads.sprint_state` | one sprint: its current card, gate, heads and budget, **what it waits on** (`work.waiting_on`: a run, the owner or the PO, each linking its card; nothing when the list is empty), the observer's last decision, its cards, Definition of Done, resume and issues, its pins, and whether its observer is up |
| GET | `/api/system` | `reads.system_snapshot` | the dashboard's document |
| GET | `/api/tasks/{ref}` | `reads.task_snapshot` | the card page's document; `?events=N` sets the tail length |
| GET | `/api/tasks/{ref}/events` | `reads.task_events` | one page of history; `?cursor=C&limit=N` |
| GET | `/api/tasks/{ref}/runs` | `ops.run_list` | every product run of one card |
| GET | `/api/tasks/{ref}/heads/{run_id}` | `reads.head_view` | the head view's document |
| GET | `/api/runs/{run_id}` | `ops.run_state` | one run, and where its ending settles |
| POST | `/api/runs/start` | `ops.run_start` | raise a worker; body `{ref, request_id, profile, instruction?}` |
| POST | `/api/runs/review` | `ops.run_review` | raise a reviewer; body `{request_id, profile, worker_run_id?, ref?}` |
| GET | `/history` | `command_reads.command_history` | the whole command history as a page, newest first; `?cursor=C&limit=N` |
| GET | `/doctor` | `doctor.doctor_snapshot` | the problems behind the bottom bar's doctor lamp, each with its code, grouped by the severity that decides the colour; recorded state only, and health that could not be read is said as itself rather than as an empty list |
| GET | `/api/pause` | `pause_reads.pause_state` | whether the pipeline is paused, in what mode, since when, and the heads behind its cards |
| GET | `/api/pause/scope` | `pause_reads.pause_scope` | what a pause would reach: the open sprints, their cards, the running heads |
| POST | `/api/pause/drain` | `pause_ops.pause_drain` | drain the pipeline (no new claims; running heads finish); body `{reason}`, actor `web` |
| POST | `/api/pause/resume` | `pause_ops.pause_resume` | clear the pause and put back what a freeze stopped; body `{}` |
| GET | `/api/sprints` | `sprint_reads.sprint_list` | every sprint and what it is doing; `?status=open` (repeatable) filters |
| POST | `/api/sprints/{ref}/comment` | `sprint_ops.sprint_comment` | one comment on a sprint, under role `po` and actor `web`; body `{request_id, body}` |
| POST | `/api/sprints/{ref}/close` | `sprint_ops.sprint_close` | close a sprint; body `{request_id, reason, closeout, decisions?}` — `decisions` is the CLI's decisions file, as text or as its parsed object |
| GET | `/api/history` | `command_reads.command_history` | a page of the last commands across every entity; `?cursor=C&limit=N` |
| GET | `/api/history/{request_id}` | `command_reads.command_request` | what became of one request id |
| POST | `/api/tasks/{ref}/comment` | `card_ops.task_comment` | one comment on a card, under role `po` and actor `web`, or role and actor `owner` on a card carrying `waiting_owner` (the owner's answer, which the dispatcher forwards to the PO; a repeated request id keeps its first role); body `{request_id, body}` |
| POST | `/api/tasks/{ref}/move` | `card_ops.task_move` | move a card, the owner's intervention; body `{request_id, target, reason, sprint_override?, sprint_override_reason?}` |
| POST | `/api/providers/codex/reset-limit` | `provider_ops.codex_reset_limit` | spend one Codex rate-limit reset credit, under role `po` and actor `web`; body `{request_id}`, sent to the provider as `redeem_request_id`. A committed record for the id is returned as it stands (`replayed: true`) and nothing is sent; otherwise the Codex usage is re-read past the cache, and no available credit is `refused` without a consume (whether a reset applies is the provider's call: `applicable_available_count` follows an undocumented rule and the provider's own client offers the reset on any available credit, so the consume is sent and the answer recorded); otherwise consume is POSTed (30 s) and its `outcome` recorded: `reset`, `nothing_to_reset`, `no_credit`, `already_redeemed`, or `error` with its `reason`. Every answer is a `codex_reset_limit` record of the board audit, so `/history` lists it and `/api/history/{request_id}` answers for it; a `reset` clears the provider cache |
| POST | `/po/login` | `po_auth.po_login` | the PO token form; body `token`; 303 to `/po` with cookie `ummanu_po`, or 401. The one `/po` route without the token |
| GET | `/po` | `po.po_overview` | open PO sessions (with `?closed=1` the closed ones, with `closed_at` and a link back; the open list links to them with `closed_count`), newest `last_activity_at` first (latest of creation, turn start/finish, feed entry), each row linked by its `title` when set (then the start of its first message on a line of its own), else by the start of its `first_message` (earliest owner entry, 80 characters, `no message yet` without one), with last activity, CLI, model, state, running turn, short id and a `close` form; and the new-session form (CLI and model from `po.models`) |
| POST | `/po/sessions` | `po.po_create_session` | open a PO session; form `request_id, cli, model`; 303 to it, or the page with the refusal |
| GET | `/po/sessions/{session}` | `po.po_session` (+ `reads.po_delegated`) | one PO session: its title heading the page when set and a rename form beside the header, feed, turn states, message box, stop while a turn runs, close while none does; a closed one shows `closed_at`/`closed_by` and no message box or close; and the **Delegated cards** it delegated or inherited as a successor, each with its ref linked, title, kind, column and last return state, from one board listing (a board that refused is said as itself) |
| POST | `/po/sessions/{session}/messages` | `po.po_send` | queue one message with the PO service; form `request_id, text`; 303 to the session once it is on disk, where it runs at once or waits for the running turn; a closed session is refused (409 `session_closed`) and a stopped service (503 `backend_unavailable`, `the PO service is not running`), nothing written |
| POST | `/po/sessions/{session}/stop` | `po.po_stop` | stop turn `seq` if it is the running one; form `seq` |
| POST | `/po/sessions/{session}/close` | `po.po_close` | close the session as actor `owner`; empty form, no request id; 303 to `/po`, also when already closed (first `closed_at`/`closed_by` kept); a running turn or a queued message renders the session refused (409 `owner_conflict`), nothing written; unknown session 404 |
| POST | `/po/sessions/{session}/title` | `po.po_rename` | set the session's title, open or closed; form `title`, no request id (a repeat sets the same value); trimmed, one line, at most 120 characters, empty clears it; 303 to the session, or the session rendered with the refusal and the text kept (400 `validation`); unknown session 404 |
| GET | `/po/api/sessions/{session}` | `po.po_session` (+ `reads.po_delegated`) | the session page's document with `delegated`, the `po_delegated` read; polled while a turn runs or a message is queued, the poll asking `?cards=0`, which reads no board and answers `delegated: null` |
| GET | `/owner-events` | `owner_events.owner_event_list` | the owner's bell: the unread owner events by default, `?all=1` every event (unread highlighted); open `needs_owner` events first, then newest first, with its class badge and subject link; `?unread=1` or any other value is the unread default; the board without `owner_events` reads as no events with the source `unavailable` ([Owner events](#owner-events-and-the-bell)) |
| POST | `/owner-events/read-all` | `owner_events.mark_all_read` | mark every unread notice read; a `needs_owner` event is never touched; form `view?` (`all` returns to `?all=1`; missing or any other value to the unread default); 303 to the list |
| POST | `/owner-events/{event_id}/read` | `owner_events.mark_read` | mark one event read; a `needs_owner` event whose card carries `waiting_owner` is refused (409 `owner_conflict`); form `view?` (`all` returns to `?all=1`; missing or any other value to the unread default); 303 to the list |

The dashboard (`GET /`) reads four documents — system snapshot, pause state, open sprints, last commands
— and a refusing one marks only its own section; only the snapshot's refusal fails the page. Card and
sprint pages carry the owner's actions as forms posting to the routes above: a comment, a move with its
reason, and on an open sprint a close. There is no `decide` route: `TaskWriter.decide` is observer-only,
so the owner's intervention on a parked card is a move.

A POST body is a JSON object with only the listed fields, except `/sprints`, which takes the submitted
form (`application/x-www-form-urlencoded`) with `request_id`, `product`, `goal`, `definition_of_done`,
`issues`, `projects`, `observer`, `worker` and `reviewer`, and the `/po` POSTs, which take forms too. An
unknown field is refused.

**`/po` is behind the PO token.** Every route whose path is `/po` or starts with `/po/` requires cookie
`ummanu_po` whose value matches the HMAC of `DATA_DIR/po-web-token`, checked once in
`WebApp.handle` after the cross-origin check and before any handler; the only exception is `POST
/po/login`. Without it a page route answers 401 with the login form and a JSON route 401
`po_token_required`; neither reaches the PO service or the board store. Token and cookie:
[Operations](OPERATIONS.md#the-po-head-in-the-dashboard). A `/po` `request_id` belongs to one operation
and its inputs installation-wide (`po_requests`): repeated with the same inputs it answers the recorded
session, the turn, or the message still queued, and does nothing else; reused otherwise it is 409
`request_conflict`. Reads come from the board store and the queue directory; every write goes to the PO
service over its socket (`ummanu.po.client`), and with the service stopped it is refused as 503
`backend_unavailable` whose message starts `the PO service is not running`; the web never runs a turn. A
write that reached the service but lost its answer is 503 `backend_unavailable` with `data: {reason:
"outcome_unknown", action: "repeat_same_request"}` (`PoOutcomeUnknown`): it may have been done; a stop or
close may simply be repeated. A create or send that the service accepted (queue file written, session
committed) is answered as accepted even when a later lookup fails. A refused create or send form keeps its
request id, so a resend is a replay; it gets a fresh one only after a refusal whose `data` carries
`nothing_written: true` — service not reached, validation before the id was reserved, `request_conflict`,
unknown or closed session.

**PO documents.** `po.po_create_session(request_id, cli, model, effort)` answers
`{kind: "po_session_created", request_id, session_id, effort, repeated}`. `effort` is required and must be
one of the installation's offered efforts for that CLI (`po.efforts`, `default` never among them); an
empty effort, `default`, `none`, or one outside that list is refused (400 `validation`) and nothing is
written. The effort is one of the inputs the request id is bound to. `default` survives only as the stored
value of sessions opened before this rule: they resume with no effort flag, `effort` in their documents
stays `default`, and the pages say their effort is `not set`. `po_models()` (and `po_overview`) carry
`models` and `efforts`, each `{cli: [values]}`. Every session object (`po_overview.sessions[]`,
`po_session.session`) carries `effort` and `resolved_model` — the model the latest turn that reported one
ran, `null` before any did — and every turn object (`po_session.turns[]`, `last_turn`) its own
`resolved_model`. `po_send(request_id, session_id, text)` answers `{kind, request_id, session_id, queued,
seq, state, repeated}`: `kind` is `po_turn_started` with the turn's `seq` and `state` when the message
became a turn at once, or `po_turn_queued` with `seq`/`state` null while it waits behind the running
turn. `po_session` carries `queued`: the session's messages not yet taken, oldest first, each
`{request_id, text, source, queued_at}`. [Operations](OPERATIONS.md#po-head-sessions-and-turns) says where each comes from.

### Opening a sprint from a browser

The sprint pages are a client of the contract above. Every form choice is an entry of `sprint_options`;
the observer and the two optional pins are selects over the registry's profiles showing label, model and
effort.

| field | what it carries |
| --- | --- |
| `request_id` | the id this form was served with, hidden in the form and submitted back unchanged |
| `product`, `goal`, `definition_of_done` | required; an empty one is refused by name before the layer is called |
| `issues`, `projects` | one value per checked box; at least one of each is required |
| `observer` | required, and always a profile the layer marked eligible: the `none` spelling `heads.observer.none` publishes is **not** offered on this route and a crafted one is refused before the layer |
| `worker`, `reviewer` | two independent optional selects whose default option is empty; an empty one reaches the layer as `None`, and never as the empty string |

A field the form requires is refused by the transport, named field by field. What a sprint may be is
judged by `SprintWriter.create` through the layer, and the transport shows the refusal. Either way the
form returns with every submitted value, including a value the catalogue no longer offers (kept as a
marked choice).

The request id is minted when the form is served, so a double click, retried POST or reconnect reaches
the same sprint. Success is a **303** to `/sprints/{ref}`.

`sprint_create` claims the id with an input digest before the writer judges the inputs
([Idempotency](#idempotency-1)), so a refusal spends the id:

| what the layer answered | what the form comes back with | why |
| --- | --- | --- |
| `OperationPending` | the same id and the same values, and the words "this sprint exists, submitting this form again is safe" | a sprint exists and only that id resumes it; a new form would open a second beside it |
| any other refusal | a **new** id, every submitted value, and the words "nothing was created" | the id was claimed and refused while nothing durable was created, so the corrected submission is a new request |

The browser form never offers `observer` `none`; use `ummanu sprint create --observer none`, and
such sprints render normally (`not_declared`). The sprint page renders `observer.launch.state` in words,
not colour alone.

### Who may make a mutation

Every POST is checked once, in `WebApp.handle`, before a handler is chosen, covering every POST route.
A POST whose `Origin` names an authority other than the `Host` it was addressed to is refused **403**
before any operation runs.

* No `Origin` header is not a browser and is allowed (`ummanu web-run`, `curl`, the diagnostics in
  [Operations](OPERATIONS.md#the-local-web-transport)). `Origin: null` is refused.
* The comparison is authority, never scheme (the front terminates TLS and proxies plain HTTP to
  `127.0.0.1`).

GETs are not checked; the front's password decides read access. `Content-Security-Policy` carries
`form-action 'self'`.

### Protocol code to HTTP status

One table, in `ummanu.web.statuses`; no status number elsewhere in the transport.

| code | status | when |
| --- | --- | --- |
| `not_found` | 404 | the board or the run store answered and holds nothing under that name |
| `validation` | 400 | the request is wrong: a missing field, an unknown field, a bad page size, a cursor this layer did not issue |
| `owner_conflict` | 409 | the request is well formed and refused on the state of the world |
| `request_conflict` | 409 | a `/po` request id reused for another operation or other inputs |
| `session_closed` | 409 | a message into a PO session the owner closed; no turn, feed entry, request row or process. It never clears by waiting |
| `backend_unavailable` | 503 | a source this request needs could not be reached at all |
| anything else | 500 | a code this transport has never heard of — a defect of the transport, not of the client |

The transport's own refusals are 404 (unrouted path) and 405 (unrouted method). A refusal on a page
route is rendered as a page with the same status.

### Watching a card over HTTP

The server keeps no session state. A card page renders the snapshot's event tail and gives the browser
that page's `next_cursor`; the browser polls `/api/tasks/{ref}/events?cursor=...` from its own position,
so a reload, second tab or reconnect resumes without re-running anything or skipping events. A cursor
the installation will not honour is 400 (`validation`): watching stops with the reason, never reset.

The request id in a POST is the client's and kept across a reload; `ops.run_start` / `ops.run_review`
answer a repeat with the existing run, so a repeated POST raises no second head.

### What a card page says about a product run

One row per run:

| column | what it is |
| --- | --- |
| `state` | what the run's *process* did: one of `running`, `finished`, `process_failed`, `source_unavailable`, `unknown`, with the reason the evidence gave and `(open)` or `(over)` beside it |
| `outcome` | what the run *produced*: the verdict when the result carries one (`state.result.verdict`), the head's own result summary, and the exit status or signal the supervisor recorded |

Both come straight from the `product_run` document (as `/api/runs/{run_id}` returns). A finished run
shows its result (a review its `green` or `red` verdict); a failed run shows `process_failed`, its exit
status and that no result was published; an open run says it has produced nothing yet.

## Publishing the pipeline: the guarded front

External access goes through Caddy (from the Ubuntu archive), which terminates TLS, checks a password and
proxies to `127.0.0.1`. The product implements no authentication: `basicauth` checks the owner's password
against a bcrypt hash taken from the installation's secret store at render time.

```bash
python3 -P -m ummanu web-front set-password --instance INSTANCE (--stdin | --generate)
python3 -P -m ummanu web-front render --instance INSTANCE --site https://HOST [--site ...] [--bind ADDR]
python3 -P -m ummanu web-front check --instance INSTANCE [--config FILE]
```

| verb | what it does |
| --- | --- |
| `set-password` | reads a password from stdin, or generates one from `secrets`; stores the value and its `caddy hash-password` bcrypt hash as two catalog entries. A plaintext never travels through argv and nothing here prints one. |
| `render` | reads the *hash* from the store and writes the Caddyfile, mode 0600, under `<data-dir>/webfront/`. Refuses a non-https site address, a hash that is not bcrypt, an upstream that is not loopback, and any configuration it would then have to report as leaving a route unguarded. A file that already holds the same text at mode 0600 is not rewritten; the answer's `changed` says which. Install, recover and upgrade run this verb with the sites of `host.web_front.sites` ([Operations](OPERATIONS.md#web-front-sites)). |
| `check` | parses a rendered file and reports every published route it would answer without a password check, and every address it proxies to. Exit 3 when there is a finding. |

The front is the only listener on a public interface; the application refuses non-loopback addresses
before a socket exists. The rendered upstream is checked with the same loopback predicate as `--host`.
`basicauth *` covers every path. `ummanu.webfront.guard` parses the rendered file and asks, for every
entry of `ummanu.web.app.ROUTES`, whether anything answers that path before a password check;
`tests/test_web_front.py` runs it over the shipped renderer and over counter-examples that must be
reported.

TLS uses `tls internal` (Caddy's own CA), since the installation has no domain name; trusting the root:
[Operations](OPERATIONS.md#the-published-web-front). Plain HTTP is only the redirect Caddy derives from
the `https://` sites; an `http://` block serving anything but a redirect is a `check` finding.

## Knowledge

Long recoverable documents live in `state/knowledge/<section>/<document>.md` for the installation and
`state/knowledge/projects/<project id>/<section>/<document>.md` for a connected project. Relation to
memory and the board: [Architecture](ARCHITECTURE.md#knowledge-planes).

```bash
python3 -P -m ummanu knowledge write --instance INSTANCE --actor ACTOR \
  --path decisions/2026-07-25-sprint-1.md --file DOC.md
python3 -P -m ummanu knowledge write --instance INSTANCE --actor ACTOR \
  --path projects/codegen-orchestrator/brainstorms/qa-node.md --file DOC.md
python3 -P -m ummanu knowledge list --instance INSTANCE
```

Path segments are ASCII letters, digits, `.`, `_` and `-`; an imported non-ASCII filename is renamed.
`write` replaces a document wholesale under the shared writer lock and starts no Git child; the next
checkpoint (the legacy tick's commit or the exporter's cut) carries it out (no manual `git commit`).
A document containing a secret is rejected with code 2 and nothing reaches disk. Rewriting identical
content reports `changed: false` and writes nothing. The answer's `commit` is the content revision
of what was written (`sha256:...`), not a Git commit ([Recovery](RECOVERY.md#writers)).

`write` takes exactly one of `--file` and `--dir`. With `--dir`, `--path` names a directory below
`state/knowledge` (same segment rules, no `..`, not absolute), and under one writer lock its whole
contents are replaced by the source directory's, so a file the source no longer has disappears; a
directory that already holds exactly the source's files is left as it is. It is how the dispatcher
moves a research report into `state/knowledge/reports/<card ref>/`.

```bash
python3 -P -m ummanu knowledge write --instance INSTANCE --actor ACTOR \
  --path reports/secretary-1640 --dir REPORT_DIR
```

Refused with code 2 before anything is written: a missing source or one with no files; a symlink,
special file or an entry whose name starts with `.git` (`.git`, `.gitignore`, `.gitattributes`,
`.gitmodules`) anywhere in it; a text file (UTF-8 without NUL bytes) that contains a secret; a total
size over 20 MiB. Binary files are copied unchanged and are **not** secret-scanned. Empty
subdirectories are not kept. If the write fails at any step the previous directory is put back and
`state/knowledge` is byte-identical to before.

The swap is staged outside `state/knowledge`, in `state/.knowledge-swap/` on the same filesystem: the
new contents are written there, the previous directory is moved beside them, and a file names the
target. A crash mid-swap therefore leaves nothing under `state/knowledge` for a checkpoint to pick
up. Every knowledge write (`--file` or `--dir`) first recovers interrupted swaps under the state
repository lock: a previous directory whose target is gone is moved back, and the rest is removed.

## Secrets

```bash
python3 -P -m ummanu secret init --instance INSTANCE
python3 -P -m ummanu secret set --instance INSTANCE --id ID --scope SCOPE --purpose PURPOSE \
  --stdin [--environment VAR] [--materialize runtime-env|file [--materialize-path PATH]]
python3 -P -m ummanu secret list --instance INSTANCE
python3 -P -m ummanu secret import --instance INSTANCE --file ENV_FILE --scope SCOPE \
  --purpose PURPOSE [--materialize runtime-env|file [--materialize-path PATH]]
python3 -P -m ummanu secret remove --instance INSTANCE --id ID
python3 -P -m ummanu secret materialize --instance INSTANCE [--target runtime-env|file]
```

A secret value never travels through argv: `set` reads stdin or `--file`; `import` takes a `KEY=VALUE`
env file (LF-separated, no comments or blank lines, one secret per variable). No command prints a value:
`list` returns catalog metadata only; `import` and `materialize` print ids and variable names. Reading a
value is internal API only.

`secret init` is interactive: it refuses when stdin or stderr is not a terminal, checked before the
recovery phrase is generated. The phrase is printed once to stderr, the operator confirms, screen and
scrollback are cleared, and `init` asks for a few words back before creating the store.

`init`, `set`, `import`, `remove` and `checkpoint-github set` take the live-root writer lock and write
`secrets/` all or nothing, starting no Git child; the next checkpoint carries the exported store files
out, and the answer's `commit` is the store's content revision ([Recovery](RECOVERY.md#writers)). An
unchanged value is never re-encrypted. `list` takes no lock and writes nothing. `materialize` takes
the lock, writes only the materialisation targets outside `secrets/`, and refuses a target inside the
live root that the export allowlist would copy. Catalog metadata passes the same
redaction gate as `state/`; a secret pasted into `purpose` stops the write.

The installation key belongs to the installation user. The store does not isolate workers: no broker or
grants, and the key opens every secret. Store layout and recovery (`locked`/`missing` report):
[Recovery](RECOVERY.md#secrets). Runbooks: [Operations](OPERATIONS.md#runtime-secrets).

## Card protocol address

A Card row's integer protocol address is its database-backed `board_key`, not the numeric
suffix of its public reference. Public refs and `(project_id, task_number)` stay stable and
project-local. Card keys occupy `[1,2000000000)`; Sprint, Product and Issue dispatch keep the disjoint
ranges above it. Reference updates preserve the Card key.
