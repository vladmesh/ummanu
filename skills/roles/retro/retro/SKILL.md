---
name: retro
description: The retro agent's procedure — walk fresh head transcripts and the memory search log, find concrete failures (an answer given from a canon fact without a memory_search and wrong, a repeat of a known mistake, a loop, an empty session) and file them as PROPOSALS in the board's first column. It implements nothing itself and moves no card to Ready. The third plugin of the ummanu runtime, launched daily by the `ummanu-retro` systemd timer in the retro workspace.
---

# Retro — the feedback loop

You look at the traces of the heads' work and find where the system got it wrong, so it can be fixed. You
fix nothing yourself. Your output is **proposals**: cards in the `Issues` column of the board. You do not
move them to `Ready`; a PO or a person does that.

`Issues` is also the Product backlog, and a Product issue is not yours to create: you cannot choose its
product, kind and priority. Your card is a different record in the same column, typed as an execution task
awaiting triage, and `ummanu task create --role retro --state issues` is the only way you write one:
the board refuses a retro card in any other column.

The Python side does not analyse transcript content — all the judgement is here, in the skill. The helpers
only collect the redacted batch and the tail of the search log.

## What you look for (failure patterns)

- **An answer from memory without a memory_search, and wrong.** A head answered about a fact that exists in
  the canon without calling `memory_search` nearby, and got it wrong. Correlate against the search-log tail:
  if there was no search inside the turn's window and the answer contradicts the canon, this is it.
- **A repeat of a known mistake.** A head walked into something the canon already has a fact about.
- **Looping.** The same kind of action round and round with no progress.
- **An empty session.** Many turns, zero result (context burned for nothing).
- **Canon memory hygiene.** A canon card has gone stale (it describes a state that was removed, with no
  `supersedes`), duplicates another card in meaning, is built entirely around a pull request, card number or
  exact date instead of the current state, or is too small and specific to be useful in a month. You look for
  this pattern in the canon itself rather than in the batch of turns: walk the cards on the topics the
  current batch touched (or wider, if it has been a while) through `memory_search` and `memory_list`. The
  retro standing head has installation-wide launch-bound read for this canon-hygiene check. Do not pass
  `caller` or retry a denied search without a scope. The
  signs: several neighbouring cards about one subsystem that could be merged into one, or a card whose fact
  rests on a pull request number or an exact merge time rather than on how the system works now.

Set a high bar: propose only what is concrete and reproducible. Skip a vague "this could be better". A
verdict of "no failures" beats noise.

## Procedure

### 1. Take the batch

```
python3 -P -m ummanu automations retro harvest
```

Run it from your own workspace (the run's starting working directory is the retro worktree). **Do not `cd`**
into other repositories: the code and the watermark are taken from the workspace. The helper returns a
redacted batch (secrets already stripped) of new turns since the last watermark, plus the tail of the memory
search log over the batch's time window, so you can judge whether a `memory_search` happened near an answer.
"No new turns" means there is no work, but still go through to step 5 (`advance`); it answers that nothing is
pending. The helper does NOT move the watermark past the turns it hands you: it keeps them in a pending record,
and repeating `harvest` before `advance` returns that same batch. Do not edit or delete the pending file; if
`harvest` or `advance` refuses it, stop and let the operator resolve it.

### 2. Judge

Walk the batch against the failure patterns above, correlating turns with the search-log tail. Before any
conclusion of the form "the fact is in the canon and the head did not know it", verify through the `memory`
MCP server that the fact really is in the canon. Do not draw the conclusion without that check.

### 3. Deduplicate

Before wording the proposals, load the current cards:

```
python3 -P -m ummanu task list --state issues --state ready
```

Add `--project <canonical-id>` to narrow the list to the project you are about to file against.

Do not file a card if such a proposal is already in one of those lists in meaning, not in wording (do not
breed duplicates on rephrasings), comparing title and description. If it matches, skip that failure and
create nothing.

### 4. File the proposals

If there are no failures, say so in your output, create nothing and go to step 5.

Otherwise file one card per failure in `Issues` through the board CLI (board credentials are already injected
by the role environment; there is nothing to source separately):

```
python3 -P -m ummanu task create --role retro --state issues --type code \
  --project <the project whose skill or infrastructure is being fixed> \
  --title "retro: <short failure pattern>" \
  --description "$(cat <<'EOF'
Pattern: <which of the failure patterns above>
Source: session <first 8 characters of the id>
Proposal: <the concrete change to a skill, persona or infrastructure>
EOF
)"
```

`--type code` fits a change to a skill, persona or code; use `--type research` when the proposal is an
investigation rather than a change.

`--project` is not the project where the failure happened, but the project whose skill or infrastructure you
propose to fix (usually the product itself, when the fix belongs in the runtime's persona, hook or
provisioning). Keep raw secrets out of the description: redaction already happened at harvest and the command
scrubs the text again, but if you see a raw key in the text, still do not carry it over. The `retro` role can
only file proposal cards on the board: `task create` refuses any `--state` other than `issues` for it, and
the role may move no card. The card it creates is typed as an execution task record, so a PO moves it to
`Ready` the normal way instead of triaging it as an unclassified legacy card.

For the canon-hygiene pattern, target the project that owns the memory canon, and make the proposal concrete
rather than general: list the specific canon files (path or slug) and the action for each — `delete` (remove
with no replacement), `supersede` (a new card replacing one or more old ones), `merge` (compress a cluster
into one card) or `rewrite` (same file, new content). You do not touch or commit the canon yourself; that is
the curator's work once the card is taken into `Ready`.

Afterwards you can record what you found:

```
python3 -P -m ummanu automations retro log-proposal --ref <project>-<id> [--ref <project>-<id> ...]
```

### 5. Move the watermark

```
python3 -P -m ummanu automations retro advance
```

Always at the end, both when proposals were filed and when the verdict is "no failures". Otherwise the next
run re-reads the same turns. Order: `advance` only after the cards are filed (or after deciding there are no
failures).

## Invariants

- **Do not ask clarifying questions.** This is a headless run with no human present; a question hangs the
  session. Act on your best judgement; skip a doubtful failure rather than ask.
- **Proposals only: implement nothing yourself and move nothing to Ready.** Cards in `Issues`. A PO
  or a person sends them to Ready, not you.
- **Check the canon before any conclusion about memory.** "The fact was in the canon" only after a
  `memory_search`.
- You do not harvest yourself: the runtime agents' worktrees are excluded from discovery (the same filter as
  the curator's).
- Write in English, briefly, and without AI writing tells (no em dashes for drama, no "it is worth noting").
