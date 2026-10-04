---
name: open-sprint
description: "Open a sprint as a board entity: gather live context, finish grilling the unresolved product forks, fix the goal, Definition of Done and repositories, create the entity with `ummanu sprint create`, and record only the 'why' in knowledge. Use on requests like 'open a sprint', 'start a sprint', 'a new sprint as an entity', `$open-sprint`."
---

# Open Sprint

A sprint is born as an entity on the sprints board, not as a document. Your work ends at the created
entity: after that the dispatcher launches the observer and the sprint runs without you.

You do not execute the sprint. You create no cards, start no work and launch no observer by hand.

## What is not delegated

The choice of goal. A wrong goal costs the whole sprint, and neither a reviewer, nor CI, nor the budget
will catch it: it only shows up when the results are read. A person formulates the goal in dialogue with
you. Do not supply the goal for them, do not derive it from the backlog "by majority", and do not hand
the choice to a subagent.

Everything else — gathering context, surfacing deferred items, reading the roadmap and the Issues
backlog, wording —
is your preparation work.

## Where things are stored

- The sprint entity: goal, Definition of Done text, repositories, status, budget, current card, resume
  entry, comments. This is what the code reads and what a person sees in the summary.
- A document in the instance repository's knowledge directory: only what is hard to formalise — the
  context of the moment, why this goal was chosen, which alternatives were rejected and why.
- The document does not repeat the entity's fields. Two sources of truth about one thing diverge, and
  they diverge silently.

The test: if a decision can be made automatically from a fact, it is a field of the entity; if the fact
is needed to understand "why", it goes in the document; if it passes neither test, do not write it at
all.

## 1. Gather live context

Read live sources, not your memory of them.

```bash
python3 -P -m ummanu sprint list --status open
python3 -P -m ummanu sprint list --status closed
python3 -P -m ummanu task list --state issues --project <project>
python3 -P -m ummanu task list --state ready --project <project>
python3 -P -m ummanu task list --sprint sprint:<ID>
```

What you need as input:

- open and recently closed sprints: their goals, repositories and what was left unclosed;
- deferred items from past sprints, which live in the resume entries and comments of their entities
  (`python3 -P -m ummanu sprint show --ref sprint:<ID>`), not in a separate list;
- the product roadmap and vision (`docs/ROADMAP.md`, `docs/VISION.md` of the affected repositories);
- the Issues backlog of the affected repositories, as input material for future cards rather than as
  a plan;
- the recorded facts of the installation and the relevant knowledge documents.

Do not promote Issues to Ready and do not touch cards. Fresh cards are cut by the observer from current
understanding.

## 2. Check that the projects are free

An open sprint holds the projects it reserves as their only writer. A second sprint on the same project is
not opened.

```bash
python3 -P -m ummanu sprint list --status open
```

Compare the `repositories` field of each open sprint against the repositories the new one needs. On an
overlap:

1. Do not create the entity. A refusal here is cheaper than two writers on one repository.
2. Name the conflicting sprint (ref, goal) to the person and offer a choice: wait for it to close, narrow
   the new sprint to the free repositories, or close the current one by an explicit decision
   (`python3 -P -m ummanu sprint close --role po --actor <you> --ref sprint:<ID> --decisions-file
   <decisions>.yaml`, which states the verdict on every issue that sprint declared).
3. You may not close someone else's open sprint on your own "so it stops getting in the way".

Anything urgent that lands in a running sprint's repository does not become a separate sprint: it is added
as an entry on that sprint's entity.

```bash
python3 -P -m ummanu sprint comment --ref sprint:<ID> --role po --actor <you> --body-file /tmp/note.md
```

## 3. Finish grilling the forks

First derive what is provable from the code, the board and the documents. Grilling is spent only on what
does not follow from them: unresolved product forks.

The rules are the same as in `grilling`: one question at a time, each with your recommended answer, and
you wait for the answer before the next question. A question that reading the code would answer must not
be asked — go and read it.

Grilling is finished when these are fixed:

- one sentence of product goal: an end state of the product, not a list of fixes;
- a Definition of Done as checkable items;
- the list of repositories;
- the boundaries: what is definitely outside the sprint;
- the observer: `none`, or one head profile from `heads.yaml`;
- the allowed productions: which registered projects' production the sprint's operations may touch,
  possibly none.

The allowed productions are chosen explicitly here, at planning, and have no default: a sprint may touch
no production unless a project is named, and naming the sprint's own project is a decision like any
other, not a given. Ask it plainly when the Definition of Done needs anything done on a live system.

The observer is the owner's decision and has no default. Ask it plainly: does this sprint need a
head watching it, and if so which one, matched to how hard the work is. A simple sprint does not
need one and should not pay for one. Do not pick a profile yourself and do not carry over what the
last sprint used.

If the goal conflicts with the product vision, stop and put the contradiction to the person rather than
smoothing it over with wording.

## 4. Formulate the Definition of Done

Each item is a checkable fact about the product that the observer can confirm against the default branch
of the affected repositories and the live system. Not "improved", not "covered", not "considered".

An item is good if it is clear what closes it: a command that can be run, a state that is visible in data,
a behaviour that reproduces through a product interface. Rephrase or drop any item whose evidence cannot be
described in one phrase.

There is no decomposition of the Definition of Done into phases, a task pool or a one-item-per-card
mapping.

Prepare the Definition of Done text as a file so it goes into the entity whole:

```bash
# /tmp/dod.md — a bulleted list of checkable items
```

## 5. Create the entity

The only way is the product command. A sprint is opened by the PO.

```bash
python3 -P -m ummanu sprint create --role po --actor <you> \
  --goal "<one sentence about the end product state>" \
  --dod-file /tmp/dod.md \
  --product <product-id> --issue issue:<ID> --project <project-id> \
  --observer <head-profile|none> \
  --repository <repo> --repository <repo> \
  [--allow-production <project-id> ...]
```

- `--role` accepts `po` and `steward`. Opening a sprint is `po`.
- `--product` is the Product the sprint belongs to; `--issue` is repeated once per open Issue of that
  Product the sprint serves, and at least one is required. Read them with `ummanu issue list --product
  <product-id>`.
- `--project` is repeated once per registered project the sprint reserves. While the sprint is open, no
  other sprint may reserve them. By default the installation holds only this one open sprint; a second
  one is a pilot that opens only when the `open_sprint_limit` instance setting is deliberately raised to
  2, and what admission then checks is in "The open-sprint limit" in `docs/PROTOCOLS.md`.
- `--repository` is repeated once per repository.
- The sprint records the PO session that creates it. Inside a PO turn this is automatic: every turn
  carries `UMMANU_PO_SESSION`, which is the default of `--po-session`, so pass nothing. Outside a PO
  turn (a `--role steward` create) the sprint records none unless `--po-session <id>` names an open one.
  When that session later no longer exists, the PO service opens a fresh one for the sprint and seeds it
  with the why-document of step 6 and `NOTES.md`.
- `--allow-production` is repeated once per registered project whose production the sprint's operations
  may touch, exactly as agreed in grilling; omit it for none. Nothing is inferred from `--project`.
- `--observer` is required and has no fallback: pass the profile the owner chose, or `none`. The
  sprint stores exactly that value; the dispatcher launches from it and never from a role default.
  A profile the head registry does not have is refused here rather than at the first tick. Changing
  it later is not an edit: it is `sprint close` and a fresh `sprint reopen --observer ...`.
- Do not set `--ref`: the entity gets its own `sprint:<ID>`. If you do set it, the value must start with
  `sprint:` and must clash with neither an existing sprint nor a card.
- Instead of `--dod-file` there is a `--definition-of-done` string; use the file for multi-line text.

Read back what you created immediately and check the fields are the ones agreed:

```bash
python3 -P -m ummanu sprint show --ref sprint:<ID>
python3 -P -m ummanu sprint status --ref sprint:<ID>
```

`show` returns the goal, Definition of Done, repositories, `po_session`, `allowed_productions`, status,
budget, current task, resume entry and comments. `status` returns the summary: card states, budget,
resume freshness and observer state. A newly created sprint normally reports a missing resume entry — the
first one is written by the observer.

## 6. Write the "why" document

The document is written after the entity and references it.

```bash
python3 -P -m ummanu knowledge write --instance <instance dir> --actor ummanu \
  --path decisions/YYYY-MM-DD-<slug>.md --file /tmp/<slug>.md
```

The document holds:

- a pointer to the entity (`sprint:<ID>`), without retelling its fields. Name it exactly: a fresh PO
  session for the sprint is seeded with the one `decisions/*.md` that names `sprint:<ID>`, so write one
  such document per sprint;
- why the sprint is being opened now: what in the product demanded it;
- which goals were considered and why this one was chosen;
- the rejected alternatives and the reason for rejecting them;
- the premises that may not hold, and what changes if they do not.

The document does not hold: the goal verbatim, the Definition of Done text, the repository list, status,
budget or current card. Those are fields of the entity; a copy in the document would go stale silently.

No status pointer file is written: sprint state lives where the cards are and cannot diverge from them.

## 7. From here the sprint is not run by hand

Once the entity exists:

- the dispatcher launches the observer itself on the next production tick; do not launch a head by hand;
- communication with a running sprint goes through entries on the entity (`sprint comment`), not messages
  to the head;
- status is read from data (`sprint status`, `task list --sprint`), not asked of the observer;
- after creation the goal, Definition of Done and boundaries are a contract. They change only by an
  explicit human decision, not in passing.

If the observer does not appear for a long time, find the reason in data:
`python3 -P -m ummanu status --instance <instance dir> --json` (`installation.sprints`,
`dispatcher.observers`) and `python3 -P -m ummanu sprint status --ref sprint:<ID>` (the `observer` field).
The instance path is mandatory for `status`. A common cause is an undelivered role skill, fixed with
`python3 -P -m ummanu role-skills sync`.

## Report back

Tell the person: the entity's ref, the goal, the Definition of Done items, the repositories, the path to
the document, and that the observer runs the sprint from here. Do not create a card "just in case".

## One loop

A sprint is an entity on the sprints board with an observer head running it. There is no second loop:
a sprint is never a knowledge document with a status pointer, and the document beside it holds only the
"why".


## Quoted standing owner decisions

Read `python3 -P -m ummanu sprint show --ref sprint:<ID>` before applying sprint authority.
The PO can supply `--owner-decisions-file <JSON>` at `sprint create`, or record a later
owner answer from its conversation directly, without an owner-role comment:

    python3 -P -m ummanu sprint record-owner-decisions --ref sprint:<ID> --role po --decisions-file <JSON> --request-id <request>

The file is a list of `{id, scope, kind, value, quotation}`. Preserve the owner's nonempty
quotation verbatim and reuse the entry ID on retries. Kinds: `production` with registered
project scope and boolean value; `e2e_grant` with scope `sprint` and positive runs;
`e2e_refusal` with scope `sprint` and value `no_more_e2e`; `advance_consent` with sprint/project
scope and value `{action, max_uses}`. Later scoped answers supersede earlier ones; grants add
once. Apply covered answers without asking again. Advance consents grant only the explicit
action and finite uses, accounted against recorded operations; they never imply money or
production permission. `sprint show` and the native sprint page carry IDs, quotations and
attribution. See docs/PROTOCOLS.md, Standing owner decisions on a sprint.

A sprint e2e answer in the owner conversation is recorded through this list. A refusal stops
new dispatches even with budget room and applies to pending and later cards. Complete any
existing budget decision card after recording the answer. The genuine owner-comment grant
command `sprint e2e-budget --authorized-by <event>` remains supported and records the same
grant entry; outside a sprint, use the existing `task e2e-budget` path. Never manufacture an
owner quotation or a grant from the sprint specification.
