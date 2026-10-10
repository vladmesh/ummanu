---
id: worker-reporting
title: Worker completion and blocked reports
---

A worker performs only the assigned card in its assigned workspace and works against the card specification and acceptance criteria. Do not manually move the card between workflow columns: report the outcome and let the workflow advance it.

After completing the work, commit and push the intended result, verify that the workspace has no uncommitted changes, then send a non-empty `ummanu task report --role worker --ref PROJECT-N --kind done` report using the request identity supplied for the round. A done report describes the delivered result and evidence; it is not a claim that uncommitted local changes are complete.

A fresh code done report for any registered project declaring `broad_check` requires its complete green full-profile receipt on the committed HEAD tree, with the exact declared interpreter and candidate imports. Use the packet's worker wrapper and reuse an unchanged validated tree. Subsets never satisfy completion. Admission reads evidence without running tests and records the receipt in the immutable report event; missing or mismatched evidence refuses before an accepted marker. Same-request replay preserves the original accepted report without reevaluating later checkout changes. Research and infra completion contracts are unchanged.

If the work cannot proceed, send `ummanu task report --role worker --ref PROJECT-N --kind blocked` with a non-empty explanation and exactly one classification:

- `external_fact`: an external prerequisite or fact must change first.
- `wrong_task_definition`: the card's specification is incorrect or insufficient.

Blocked is a normal, useful outcome. Do not invent missing requirements, hide uncertainty, or retry an unchanged rejected result indefinitely. Reuse a request identity only to retry the same operation and same report, never to replace its content.
