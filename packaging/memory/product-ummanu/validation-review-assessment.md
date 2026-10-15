---
id: validation-review-assessment
title: Validation, review, and Assessment
---

A done report sends a card to `Validate`. Ummanu verifies the candidate revision through the project's configured mechanical gates, then obtains independent review against the card's acceptance criteria. A passing local check alone does not release work; the relevant validation and review evidence must concern the current candidate revision.

Mechanical failures, merge conflicts, unavailable required evidence, or unrecoverable delivery failures are handled by the workflow as rework or `Blocked`, with a visible reason. Do not bypass a failed or pending gate by manually declaring the card done.

`Assessment` is a durable wait for a human-direction decision, not a queue for another machine check. For sprint work with an active observer, a substantive reviewer verdict parks the card in Assessment after the mechanical gates have passed. The retained worker workspace is preserved while the decision is pending.

The three Assessment decisions are `accept`, `rework`, and `reslice`: accept permits the release effect, rework starts another work round, and reslice sends the card to Blocked for replanning. A decision is recorded before its effect, so a reviewer verdict alone never authorizes a merge or a new round.
