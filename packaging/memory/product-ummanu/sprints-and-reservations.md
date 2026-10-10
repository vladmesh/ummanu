---
id: sprints-and-reservations
title: Sprints, products, and project reservations
---

A sprint is a separate durable entity, identified as `sprint:ID`. It records a goal, Definition of Done, product and issues it serves, reserved projects, and its open, stopped, or closed status. `ummanu sprint show` is the authoritative view of that contract.

An execution card belongs to an open sprint. An open sprint reserves its projects so that work proceeds under one visible plan rather than through competing writers. Create and modify sprint-linked work through the role and workflow assigned to that sprint; a PO override is an explicit, audited exception.

An optional observer follows sprint-level progress and decisions. It does not replace the worker or reviewer, and it does not claim cards. A stopped or closed sprint is not permission to continue changing its old card contract: start or reopen the appropriate planned work through the normal protocol.

Use sprint comments for durable communication about the sprint. Keep goals, Definition of Done, decisions, and blockers specific enough that another role can act on them without reconstructing context from a conversation.

Reviewers inspect code and worker/CI evidence without test runs; every `ummanu check` form, including
show and receipt reuse, refuses for a reviewer head. Missing evidence is named in the verdict and
requested from the worker or CI. Worker local checks on the control host are limited to the project's adapter broad
check and its subsets. Integration shards, Docker/container runs, stands, provisioning and
network-heavy checks belong in CI. Exceptions come only from the sprint's creation-only
`local_run_exceptions` list of `{project, argv, rationale}` entries, scoped to registered projects
reserved by that sprint; default `[]`. Use `sprint create --local-run-exceptions-file` to declare
them. Prose and missing gate receipts grant no exception. An excessive local heavy run is a
non-blocking observation, never grounds for RED; exclude its results from validation evidence
even if it passed. CI or an allowed local check supplies evidence; judge code and valid evidence.
The observer does not order rework or charge the budget for such a run alone. Preserve historical
verdicts in the audit without reopening them. A code defect or missing required valid evidence
can still block release.

The ordinary worker/reviewer Docker guard refuses `run`, `create`, `build` and `compose up|run|build`
unless the current card's sprint grants the exact `["docker", *original_arguments]` string vector
for that project. The dispatcher binds the creation-only authority at launch; inherited environment,
`runtime.env` and candidate files grant nothing. Exceptions never relax cleanup ownership checks.
Tests/broad must not require local Docker; report a declared broad-suite dependency on the card.
