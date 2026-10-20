---
name: open-issue
description: "File a product issue on the board as the PO: check the product and the open issues for a duplicate, choose kind, priority, title and description yourself, and create it with `ummanu issue create` without asking the owner first. Use on requests like 'file an issue', 'add this to the backlog', 'open an issue', `$open-issue`."
---

# Open Issue

An issue is a durable record on the product's board: a bug, a feature, a question or an improvement
the product owes. It is input for future sprints, not a plan. Your work ends at the created issue; you
do not cut cards, promote it to a sprint or start work on it.

## 1. Find the product and check for a duplicate

```bash
python3 -P -m ummanu product list
python3 -P -m ummanu issue list --product <product>
python3 -P -m ummanu issue show --ref <issue-ref>
```

If an open issue already covers the problem, do not file a second one. Add what is new to it instead:

```bash
python3 -P -m ummanu issue append --role po --ref <issue-ref> \
  --reason "<why this block is added>" --body-file <file>
```

## 2. Choose the fields

You choose every field yourself; there is no owner confirmation before create.

- `--kind`: `bug`, `feature`, `question` or `improvement`.
- `--priority`: `P0` to `P3`. Pick the one that fits; the owner may change it later
  (`issue update-priority`).
- `--title`: one line that names the problem, not the fix.
- `--description`: what is observed or wanted, the evidence (commands, outputs, refs) and what is
  deliberately out of it. Enough for an observer who was not in this conversation.

## 3. Create it

```bash
python3 -P -m ummanu issue create --role po --product <product> \
  --kind <kind> --priority <P0-P3> --title "<title>" --description "<description>" \
  --request-id <stable-id>
```

`--request-id` makes a retry safe: repeating the command with the same id does not create a second
issue. Report the issue reference the command prints back to the owner.

## Later changes

- Priority: `issue update-priority --role po --ref <ref> --priority <P0-P3> --reason "<why>"`.
- Title or kind: `issue edit --role po --ref <ref> --title "<title>" --kind <kind> --reason "<why>"` (either or both).
- Close: `issue close --role po --ref <ref> --reason resolved|invalid|duplicate|wont_do`.
- Reopen a closed one: `issue reopen --role po --ref <ref> --reason "<why>"`.
