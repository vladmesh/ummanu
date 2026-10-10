---
id: roles-and-authority
title: Roles and decision authority
---

The PO owns product direction: it creates and triages work, maintains the specification, and makes explicit exceptions when a sprint contract must be overridden. The dispatcher owns routine lifecycle progression and the effects of accepted workflow decisions.

The worker changes the assigned deliverable and reports `done` or `blocked`. The reviewer independently evaluates the result against the card; it does not merge the work or move the card. The observer follows an open sprint and records `release`, `rework`, or `reslice` decisions for cards in Assessment; the dispatcher performs the corresponding effect. The steward can escalate stale or unrecoverable work to Blocked with a reason.

The reviewer reads diffs, commits, admitted worker receipts and exact-SHA CI evidence without executing tests or invoking `ummanu check`, including readback and reuse. Missing evidence goes in the verdict; the worker or CI supplies additional validation. Reviewer test refusal uses the common dispatcher's bound role launch identity and precedes wrapper ancestry authorization.

Authority is deliberately narrow. A role should use its own reporting, verdict, comment, or decision command rather than impersonating another role or forcing a state transition. If the required action is outside the assigned role or card scope, record the blocker or ask the responsible role to decide.
