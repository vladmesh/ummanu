# The PostgreSQL board store

Technical reference for the board store: schema, client construction, transactions, audit mapping,
migrations, identifiers and store configuration.

The installation serves Products, Issues, Sprints and Cards from PostgreSQL; it is the only board
backend. The history of the board that preceded it is archived in the live root's knowledge at
`state/knowledge/reports/secretary-1674/report.md`.

Related documents:

- module layout and component boundaries: [ARCHITECTURE.md](ARCHITECTURE.md);
- provisioning runbook: [PostgreSQL board store](OPERATIONS.md#postgresql-board-store);
- backup and restore of the store: [Backend-aware cold archives](RECOVERY.md#backend-aware-cold-archives).

---

## 1. Scope

The schema holds board entities only: Products, Issues, Sprints, Cards, their links, comments,
budget events, close decisions, request claims and typed board events. Everything else stays
outside it (§3.11).

---

## 2. Store modules and client construction

### 2.1 Modules

| Module | Role |
|---|---|
| `board/schema.py` | SQLAlchemy models; the source of truth for §3 |
| `board/migrations/` | Alembic environment and revisions `0001`–`0030` (§7.4) |
| `board/migrate.py` | migration runner: advisory lock, owner connection, role passwords (§7.4) |
| `board/release_migrations.py` | the release's target bundle, its eligibility and bounded apply (§7.4) |
| `board/schema_gate.py` | the schema gate every operational connection and doctor read (§7.4) |
| `board/store.py` | `board-store.env` parsing, resolution and git exclusion (§5.4) |
| `board/provision.py` | Compose definition, container/volume reconciliation, role verification (§5.1–§5.5) |
| `board/backend.py` | `board_client`, entity identities and record keys (§2.2) |
| `board/sql_cards.py` | `SqlCardClient`: the board vocabulary over cards (§7.1) |
| `board/sql_product_issues.py`, `board/sql_sprints.py` | Product/Issue and Sprint rows over the same client |
| `board/sql_audit.py` | `SqlTaskAudit`: the audit owner over `requests`/`board_events` (§7.3) |
| `board/postgres_recovery.py` | PostgreSQL archive restore helpers (see RECOVERY.md) |

### 2.2 Client construction

`TaskReader`/`TaskWriter`, `SprintReader`/`SprintWriter` and `ProductIssueStore` have one
implementation, over PostgreSQL. Nothing selects it: `board-store.env` provides the connection
material and nothing else.

`board/backend.py:board_client(instance, serves=..., role="app")` is the only constructor of a
board client. `serves` names what the call site needs (`card`, `sprint`, `product/issue`); an
unknown capability refuses by name. Store failures leave as `TaskError` (`backend_error`,
`backend_unavailable`), not tracebacks.

**Entity identity in normalized rows.** `<kind>_postgres_<n>` (`task_postgres_37`,
`sprint_postgres_9`) is minted only by `entity_id` and read only by `entity_number`. `n` is the
store's `board_key`. The reader accepts any lowercase store word and a bare number, because
recorded history carries identities minted before this store (§9). An identity is not the row's
identity; the reference is (§9).

**Integer record keys.** The inherited board vocabulary addresses rows by integer. Each row stores
its key as a unique indexed `board_key`:

| Kind | Range | Allocation |
|---|---|---|
| Card | `[1, 2 000 000 000)` | `card_board_key_seq`, immutable |
| Sprint, numbered `sprint:N` | `[2 000 000 000, 2 500 000 000)` | base + N |
| Sprint, other references | `[2 500 000 000, 3 000 000 000)` | hash of the reference |
| Product | `[3 000 000 000, 4 000 000 000)` | hash of `product:<id>` |
| Issue | `[4 000 000 000, 5 000 000 000)` | hash of `issue:<id>` |

Lookups use the unique index. A malformed or colliding key is refused, never resolved by scanning;
keys cannot cross kinds.

---

## 3. ER schema

Conventions: `text` identifiers, `timestamptz` times, surrogate `bigint` keys only where a row has
no natural key. Every reference between entities is a foreign key; a reference meaningful only
within one sprint is a composite foreign key carrying the sprint (§3.3, §3.4, §3.8). Closed
vocabularies are `CHECK` constraints (§3.12). `jsonb` appears in eleven columns (§3.10).

The DDL is grouped by entity. Forward and mutual references are added with `ALTER TABLE` after both
tables exist (§3.13). `board/schema.py` is authoritative; the DDL below mirrors it.

### 3.1 Products, projects, repositories

```sql
CREATE TABLE products (
    product_id   text PRIMARY KEY,                    -- "ummanu"; matches ^[a-z0-9][a-z0-9-]{0,62}$
    board_key    bigint NOT NULL UNIQUE,              -- §2.2
    ref          text GENERATED ALWAYS AS ('product:' || product_id) STORED UNIQUE,
    title        text NOT NULL CHECK (title <> ''),
    description  text NOT NULL DEFAULT '',
    state        text NOT NULL DEFAULT 'active' CHECK (state IN ('active','archived')),
    extensions   jsonb NOT NULL DEFAULT '{}'::jsonb, -- (J7)
    created_at   timestamptz NOT NULL,
    updated_at   timestamptz NOT NULL
);

CREATE TABLE projects (
    project_id       text PRIMARY KEY,                -- registry id, e.g. "ummanu"
    enabled          boolean NOT NULL DEFAULT true,
    plane            text NOT NULL DEFAULT 'project',
    adapter          text,
    orca_binding     text,
    registry_present boolean NOT NULL DEFAULT true    -- false: referenced by history, absent from the registry
);

CREATE TABLE repositories (
    repository_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id     text REFERENCES projects(project_id),
    path           text NOT NULL,                     -- absolute working-tree path
    remote         text,
    default_branch text NOT NULL DEFAULT 'main',
    role           text NOT NULL DEFAULT 'primary'
                     CHECK (role IN ('primary','curator_root')),
    UNIQUE (path)
);
CREATE UNIQUE INDEX repositories_one_primary
    ON repositories (project_id) WHERE role = 'primary';

CREATE TABLE product_projects (
    product_id text NOT NULL REFERENCES products(product_id) ON DELETE CASCADE,
    project_id text NOT NULL REFERENCES projects(project_id),
    PRIMARY KEY (product_id, project_id)
);
```

`products.extensions` keeps metadata keys outside the known Product columns and the
`product_projects` set under one fixed top-level key (§8); known keys are filtered before merging,
so provenance cannot override identity or relationships.

`projects` and `repositories` exist so `tasks.project_id`, `sprint_projects.project_id` and
`sprint_repositories.repository_id` can be foreign keys. They are not canonical: the registry files
`<instance>/projects/*.yaml` are (§6.2), and `registered_projects()` reads the files. Writers:

- a Product's project-set write inserts a missing `projects` row with the id only
  (`ON CONFLICT DO NOTHING`);
- a Sprint's repository-list write inserts a missing `repositories` row with the path only.

Rows are never deleted; `registry_present = false` marks an id absent from the registry.

### 3.2 Issues

```sql
CREATE TABLE issues (
    issue_id     text PRIMARY KEY,                    -- the 20-hex suffix of issue:<id>
    board_key    bigint NOT NULL UNIQUE,              -- §2.2
    ref          text GENERATED ALWAYS AS ('issue:' || issue_id) STORED UNIQUE,
    product_id   text NOT NULL REFERENCES products(product_id),
    title        text NOT NULL CHECK (title <> ''),
    description  text NOT NULL DEFAULT '',
    issue_kind   text NOT NULL
                   CHECK (issue_kind IN ('bug','feature','question','improvement')),
    priority     text NOT NULL CHECK (priority IN ('P0','P1','P2','P3')),
    state        text NOT NULL DEFAULT 'open' CHECK (state IN ('open','closed')),
    close_reason text CHECK (close_reason IN ('resolved','invalid','duplicate','wont_do')),
    extensions   jsonb NOT NULL DEFAULT '{}'::jsonb, -- (J6); see §8.2
    created_at   timestamptz NOT NULL,
    updated_at   timestamptz NOT NULL,
    CONSTRAINT issue_close_reason_matches_state
        CHECK ((state = 'closed') = (close_reason IS NOT NULL))
);
```

### 3.3 Sprints

```sql
CREATE TABLE sprints (
    ref                text PRIMARY KEY,              -- "sprint:1037", "sprint:canary-terra-20260813"
    board_key          bigint NOT NULL UNIQUE,        -- §2.2
    sprint_number      integer UNIQUE,                -- N in sprint:N, NULL when the ref has none
    goal               text NOT NULL,
    definition_of_done text NOT NULL,
    product_id         text REFERENCES products(product_id),
    status             text NOT NULL DEFAULT 'open'
                         CHECK (status IN ('open','closed','stopped')),
    observer           jsonb,                         -- (J1)
    worker_pin         text,
    reviewer_pin       text,
    po_session         text,                          -- the PO session it answers to (0016)
    allowed_productions text[] NOT NULL DEFAULT '{}', -- projects its operations may touch (0016)
    owner_decisions jsonb NOT NULL DEFAULT '[]', -- quoted sprint authority (0027, J9)
    local_run_exceptions jsonb NOT NULL DEFAULT '[]', -- exact command authority at create (0026, J8)
    e2e_budget         integer NOT NULL DEFAULT 3,    -- e2e runs it may dispatch (0023)
    e2e_used           integer NOT NULL DEFAULT 0,    -- e2e runs it dispatched (0023)
    current_task_ref   text,                          -- scoped cursor, below
    resume_id          bigint,                        -- scoped cursor, below
    close_reason       text,
    closeout_document  text,                          -- state/knowledge path, not the prose
    source_audit       jsonb,                         -- (J2)
    created_at         timestamptz NOT NULL,
    updated_at         timestamptz NOT NULL,
    closed_at          timestamptz,
    CONSTRAINT sprint_closed_has_time CHECK ((status = 'open') = (closed_at IS NULL)),
    CONSTRAINT sprint_e2e_counts_are_not_negative CHECK (e2e_budget >= 0 AND e2e_used >= 0),
    CONSTRAINT sprint_owner_decisions_are_array CHECK (jsonb_typeof(owner_decisions) = 'array'),
    CONSTRAINT sprint_local_runs_are_array CHECK (jsonb_typeof(local_run_exceptions) = 'array'),
    CONSTRAINT sprint_ref_is_a_sprint_reference CHECK (ref ~ '^sprint:'),
    CONSTRAINT sprint_number_agrees_with_ref CHECK (
        (ref ~ '^sprint:[0-9]+$') = (sprint_number IS NOT NULL) AND
        (sprint_number IS NULL OR ref = 'sprint:' || sprint_number))
);
CREATE SEQUENCE sprint_number_seq;                    -- see §9

CREATE TABLE sprint_repositories (
    sprint_ref    text   NOT NULL REFERENCES sprints(ref) ON DELETE CASCADE,
    repository_id bigint NOT NULL REFERENCES repositories(repository_id),
    ordinal       integer NOT NULL DEFAULT 0,
    PRIMARY KEY (sprint_ref, repository_id)
);

CREATE TABLE sprint_issues (
    sprint_ref text NOT NULL REFERENCES sprints(ref) ON DELETE CASCADE,
    issue_id   text NOT NULL REFERENCES issues(issue_id),
    ordinal    integer NOT NULL DEFAULT 0,
    PRIMARY KEY (sprint_ref, issue_id)
);

CREATE TABLE sprint_resumes (                         -- append-only; sprints.resume_id names the live one
    resume_id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sprint_ref           text NOT NULL REFERENCES sprints(ref) ON DELETE CASCADE,
    selected_step        text NOT NULL,
    selected_why         text NOT NULL,
    rejected_alternatives text NOT NULL,
    current_task         text NOT NULL,
    dod_state            text NOT NULL,
    next_safe_step       text NOT NULL,
    recorded_at          timestamptz NOT NULL,
    recorded_at_source   text, -- malformed restored legacy spelling, retained as stale evidence
    po_request           jsonb, -- future PO wait: {card, action}; NULL for released resumes (0029, J10)
    CHECK (po_request IS NULL OR jsonb_typeof(po_request) = 'object'),
    UNIQUE (resume_id, sprint_ref)                    -- target of the scoped cursor
);
```

The reference is the sprint's identity, so non-numbered references are representable.
`sprint_number` is set exactly for `sprint:N` references. Resume fields are the six-field
`RESUME_FIELDS` tuple; resume freshness is derived, not stored.

**Scoped cursors.** A sprint's current task and live resume must belong to that sprint:

Deferred constraints (step 2 of §3.13):

```sql
ALTER TABLE sprints
  ADD CONSTRAINT sprint_current_task_is_in_this_sprint
      FOREIGN KEY (current_task_ref, ref)
      REFERENCES tasks (task_ref, sprint_ref) MATCH SIMPLE
      DEFERRABLE INITIALLY DEFERRED,
  ADD CONSTRAINT sprint_resume_is_of_this_sprint
      FOREIGN KEY (resume_id, ref)
      REFERENCES sprint_resumes (resume_id, sprint_ref) MATCH SIMPLE
      DEFERRABLE INITIALLY DEFERRED;
```

- `tasks UNIQUE (task_ref, sprint_ref)` and `sprint_resumes UNIQUE (resume_id, sprint_ref)` exist
  only as targets of these keys.
- `MATCH SIMPLE`: a composite key with a NULL column is not checked, so a sprint with no current
  task or no resume is legal.
- `DEFERRABLE INITIALLY DEFERRED`: normalized restore inserts Cards naming a Sprint before the
  Sprint row, and the Sprint then names one of them; validation happens at commit.
- Moving a card to another sprint fails while its old sprint still names it as current task; clear
  the cursor first.

### 3.4 Budget

```sql
CREATE TABLE sprint_budget_events (
    budget_event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sprint_ref      text NOT NULL REFERENCES sprints(ref) ON DELETE CASCADE,
    event_type      text NOT NULL CHECK (event_type IN
        ('red_review','blocked','red_ci','preempt','recreated_task','hotfix',
         'infrastructure_blocked')),
    charged         boolean NOT NULL,
    task_ref        text,
    reason          text NOT NULL,
    request_id      text NOT NULL,                   -- references `requests`; see §3.9
    occurred_at     timestamptz NOT NULL,
    CONSTRAINT budget_charge_matches_type
        CHECK (charged = (event_type <> 'infrastructure_blocked'))
);
```

Deferred constraint (step 2 of §3.13):

```sql
ALTER TABLE sprint_budget_events
  ADD CONSTRAINT budget_card_is_in_this_sprint
      FOREIGN KEY (task_ref, sprint_ref)
      REFERENCES tasks (task_ref, sprint_ref) MATCH SIMPLE;
```

Budget totals (`by_type`, `total`, `signal_reached`, `hard_reached`) are aggregates over these rows
against the installation thresholds. Retry idempotency comes from the request claim (§3.9).
`request_id` is not unique here: restore may replay several exported occurrences under one restore
request. `task_ref` is nullable; a charge naming a card must name a card of that sprint.

The e2e run budget (0023, `board/e2e_budget.py`, [Protocols](PROTOCOLS.md#the-e2e-run-budget)) is
`sprints.e2e_budget` and `sprints.e2e_used`, and one row per charged run:

```sql
CREATE TABLE sprint_e2e_charges (
    dispatch_id text PRIMARY KEY,                     -- a run is charged once
    sprint_ref  text NOT NULL REFERENCES sprints(ref) ON DELETE CASCADE,
    task_ref    text NOT NULL,
    charged_at  timestamptz NOT NULL
);
CREATE INDEX sprint_e2e_charges_by_sprint ON sprint_e2e_charges (sprint_ref);
```

A charge is `UPDATE sprints SET e2e_used = e2e_used + 1 WHERE ref = ... AND e2e_used < e2e_budget AND
NOT EXISTS (a charge of that dispatch id) RETURNING ...` and, when it returns a row, the insert, in the
transaction that writes the run's intent on the card. A raise adds to `e2e_budget` in place under one
`e2e_budget_raised` sprint audit record.

### 3.5 Cards (tasks)

```sql
CREATE SEQUENCE card_board_key_seq;

CREATE TABLE tasks (
    task_ref       text PRIMARY KEY,                  -- "secretary-1580"
    board_key      bigint NOT NULL UNIQUE DEFAULT nextval('card_board_key_seq'),
    project_id     text REFERENCES projects(project_id),  -- NULL when the card names no project
    task_number    integer NOT NULL,
    title          text NOT NULL CHECK (title <> ''),
    description    text NOT NULL DEFAULT '',
    task_type      text CONSTRAINT task_type_is_a_known_type_or_nothing
                     CHECK (task_type IS NULL OR task_type IN ('code','research','infra',
                                                               'decision','operation','wait')),
    review         text CONSTRAINT task_review_is_a_known_choice_or_nothing
                     CHECK (review IS NULL OR review IN ('required','skipped')),  -- NULL reads as required
    live_impact    boolean NOT NULL DEFAULT false,    -- research only (task_live_impact_is_research_only)
    state          text NOT NULL CHECK (state IN
                     ('issues','ready','in_progress','validate','assessment','blocked','done')),
    archived       boolean NOT NULL DEFAULT false,
    position       integer NOT NULL DEFAULT 0,
    sprint_ref     text REFERENCES sprints(ref) DEFERRABLE INITIALLY DEFERRED,
    claim_worker   text,
    claimed_at     timestamptz,
    -- workspace
    slug           text,
    base_branch    text,
    seed_ref       text,
    -- routing
    complexity        text NOT NULL DEFAULT 'standard'
                        CHECK (complexity IN ('cheap','standard','hard','frontier')),
    family_preference text NOT NULL DEFAULT 'auto'
                        CHECK (family_preference IN ('auto','claude','codex')),
    head_override        text,
    review_head_override text,
    resolved_worker_head text,
    resolved_worker_family text,
    resolved_review_head text,
    resolved_review_family text,
    routing_reason       text,
    quota_snapshot_at    timestamptz,
    codex_launch_mode    text CHECK (codex_launch_mode IN ('tui')),
    retry_same     integer NOT NULL DEFAULT 0 CHECK (retry_same >= 0),
    retry_switch   integer NOT NULL DEFAULT 0 CHECK (retry_switch >= 0),
    extensions     jsonb NOT NULL DEFAULT '{}'::jsonb,   -- (J3)
    created_at     timestamptz NOT NULL,
    updated_at     timestamptz NOT NULL,
    date_moved     timestamptz,                       -- NULL when no move time was observed
    CONSTRAINT task_board_key_is_in_card_range CHECK (board_key > 0 AND board_key < 2000000000),
    CONSTRAINT task_number_is_in_card_key_range CHECK (task_number < 2000000000),
    UNIQUE (project_id, task_number),
    UNIQUE (task_ref, sprint_ref)                     -- target of scoped keys (§3.3, §3.4, §3.8)
);

CREATE TABLE task_retry_heads (                       -- order of retry_heads
    task_ref text NOT NULL REFERENCES tasks(task_ref) ON DELETE CASCADE,
    ordinal  integer NOT NULL,
    head     text NOT NULL,
    PRIMARY KEY (task_ref, ordinal)
);

CREATE TABLE task_issues (
    task_ref text NOT NULL REFERENCES tasks(task_ref) ON DELETE CASCADE,
    issue_id text NOT NULL REFERENCES issues(issue_id),
    PRIMARY KEY (task_ref, issue_id)
);

CREATE TABLE task_dependencies (                      -- blocked_by
    task_ref        text NOT NULL REFERENCES tasks(task_ref) ON DELETE CASCADE,
    depends_on      text NOT NULL,                    -- the reference as written
    depends_on_task text REFERENCES tasks(task_ref),  -- set when the store holds that card
    PRIMARY KEY (task_ref, depends_on),
    CONSTRAINT no_self_dependency CHECK (task_ref <> depends_on),
    CONSTRAINT dependency_resolution_is_the_same_reference
        CHECK (depends_on_task IS NULL OR depends_on_task = depends_on)
);

CREATE TABLE task_supersessions (                     -- supersedes
    task_ref     text PRIMARY KEY REFERENCES tasks(task_ref) ON DELETE CASCADE,
    supersedes   text NOT NULL REFERENCES tasks(task_ref),
    recorded_at  timestamptz NOT NULL,
    CONSTRAINT no_self_supersession CHECK (task_ref <> supersedes)
);
```

- `board_key` is the card's immutable protocol address; `task_number` is the public per-project
  number parsed from the reference. Neither replaces `task_ref` as identity.
- `task_type` and `project_id` are NULL when the card carries no value; the vocabulary stays
  closed.
- `depends_on_task IS NULL` means the dependency names a card the store does not hold.
- `task_supersessions` keeps one supersession per card.
- `date_moved` is set on SQL create and on every column move; Done retention reads it. Rows
  without an observed move keep NULL and are skipped by retention until they move.

### 3.6 Reservations

```sql
CREATE TABLE sprint_projects (
    sprint_ref  text    NOT NULL REFERENCES sprints(ref) ON DELETE CASCADE,
    project_id  text    NOT NULL REFERENCES projects(project_id),
    reserved    boolean NOT NULL DEFAULT true,
    reserved_at timestamptz NOT NULL,
    released_at timestamptz,
    ordinal     integer NOT NULL DEFAULT 0,
    PRIMARY KEY (sprint_ref, project_id),
    CONSTRAINT reserved_matches_release CHECK (reserved = (released_at IS NULL))
);

CREATE UNIQUE INDEX sprint_projects_one_live_reservation
    ON sprint_projects (project_id) WHERE reserved;
```

See §4.

### 3.7 Comments

```sql
CREATE TABLE sprint_comments (
    comment_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sprint_ref    text NOT NULL REFERENCES sprints(ref) ON DELETE CASCADE,
    marker        text,                               -- "po", "sprint:resume", NULL for unmarked
    body          text NOT NULL,
    actor_role    text,
    actor_id      text,
    request_id    text UNIQUE,                        -- claim key added in §3.13 step 2
    created_at    timestamptz NOT NULL
);

CREATE TABLE task_comments (
    comment_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_ref    text NOT NULL REFERENCES tasks(task_ref) ON DELETE CASCADE,
    marker      text,                                 -- role, or report:/review:/decision:*
    body        text NOT NULL,
    actor_role  text,
    actor_id    text,
    request_id  text UNIQUE,                          -- claim key added in §3.13 step 2
    created_at  timestamptz NOT NULL
);
CREATE INDEX task_comments_by_task ON task_comments (task_ref, created_at);

CREATE TABLE issue_comments (
    comment_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    issue_id    text NOT NULL REFERENCES issues(issue_id) ON DELETE CASCADE,
    marker      text,                                 -- a role, or issue:* (§8.1)
    body        text NOT NULL,
    actor_role  text,
    actor_id    text,
    request_id  text UNIQUE,                          -- claim key added in §3.13 step 2
    created_at  timestamptz NOT NULL
);
CREATE INDEX issue_comments_by_issue ON issue_comments (issue_id, created_at);

CREATE TABLE product_comments (
    comment_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    product_id  text NOT NULL REFERENCES products(product_id) ON DELETE CASCADE,
    marker      text,
    body        text NOT NULL,
    actor_role  text,
    actor_id    text,
    request_id  text UNIQUE,                          -- claim key added in §3.13 step 2
    created_at  timestamptz NOT NULL,
    product_ref text GENERATED ALWAYS AS ('product:' || product_id) STORED
);
CREATE INDEX product_comments_by_product ON product_comments (product_id, created_at);
```

`marker` is a column holding the parsed marker token; `body` keeps the whole comment text, the
`[marker]` line included (§8.1).

### 3.8 Sprint decisions

```sql
CREATE TABLE sprint_decisions (
    decision_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sprint_ref    text NOT NULL REFERENCES sprints(ref) ON DELETE CASCADE,
    subject_kind  text NOT NULL CHECK (subject_kind IN ('issue','card')),
    issue_id      text,                               -- scoped below
    task_ref      text,                               -- scoped below
    verdict       text NOT NULL,
    actual        text,
    reason        text NOT NULL CHECK (reason <> ''),
    request_id    text NOT NULL,                      -- not unique: one close, many decisions
    decided_at    timestamptz NOT NULL,
    CONSTRAINT decision_subject_is_exactly_one CHECK (
        (subject_kind = 'issue' AND issue_id IS NOT NULL AND task_ref IS NULL) OR
        (subject_kind = 'card'  AND task_ref IS NOT NULL AND issue_id IS NULL)),
    CONSTRAINT decision_verdict_in_vocabulary CHECK (
        (subject_kind = 'issue' AND verdict IN
            ('resolved','invalid','duplicate','wont_do','open','already_closed')) OR
        (subject_kind = 'card'  AND verdict IN ('done','drop','already_moved'))),
    CONSTRAINT decision_actual_only_on_confirmation CHECK (
        (verdict IN ('already_closed','already_moved')) = (actual IS NOT NULL))
);
CREATE UNIQUE INDEX sprint_decisions_one_per_issue
    ON sprint_decisions (sprint_ref, issue_id) WHERE issue_id IS NOT NULL;
CREATE UNIQUE INDEX sprint_decisions_one_per_card
    ON sprint_decisions (sprint_ref, task_ref) WHERE task_ref IS NOT NULL;
```

Deferred constraints (step 2 of §3.13):

```sql
ALTER TABLE sprint_decisions
  ADD CONSTRAINT decided_issue_is_declared_by_this_sprint
      FOREIGN KEY (sprint_ref, issue_id)
      REFERENCES sprint_issues (sprint_ref, issue_id) MATCH SIMPLE,
  ADD CONSTRAINT decided_card_is_in_this_sprint
      FOREIGN KEY (task_ref, sprint_ref)
      REFERENCES tasks (task_ref, sprint_ref) MATCH SIMPLE;
```

An issue decision is representable only for an issue the sprint declared, a card decision only for
a card the sprint holds. With `MATCH SIMPLE` each row is checked by the one key whose columns are
non-NULL.

### 3.9 Request ownership, events and idempotency

One request-id namespace for the whole installation:

```sql
CREATE TABLE requests (
    request_id  text PRIMARY KEY,
    operation   text NOT NULL,                        -- the record's kind
    intent      jsonb NOT NULL,                       -- (J5) the full audit record
    status      text NOT NULL CHECK (status IN ('staged','committed','discarded')),
    protocol    boolean NOT NULL DEFAULT false,       -- true for board.protocol_event records
    entity_kind text CHECK (entity_kind IN ('product','issue','sprint','card')),
    ref         text,
    created_at  timestamptz NOT NULL,
    settled_at  timestamptz,
    CONSTRAINT request_settled_matches_status
        CHECK ((status = 'staged') = (settled_at IS NULL)),
    CONSTRAINT requests_ref_identity UNIQUE (request_id, ref)   -- target of claim keys
);

CREATE TABLE board_events (
    event_id     text PRIMARY KEY,                    -- globally unique
    request_id   text NOT NULL UNIQUE REFERENCES requests(request_id),
    kind         text NOT NULL CHECK (kind IN (       -- EventKind, §3.12
        'entity.created','entity.updated','product.archived','issue.closed',
        'sprint.closed','sprint.stopped','sprint.reopened',
        'card.readied','card.started','card.submitted','card.assessed','card.reworked',
        'card.released','card.blocked','card.unblocked','card.returned','card.moved',
        'card.reported','card.verdict','card.decided','card.decision_refused',
        'attempt.usage','attempt.outcome')),
    entity_kind  text NOT NULL CHECK (entity_kind IN ('product','issue','sprint','card')),
    ref          text NOT NULL,
    actor_role   text NOT NULL,
    actor_id     text NOT NULL,
    head_run_ref text,
    reason       text NOT NULL,
    source_state text,
    target_state text,
    related_refs text[] NOT NULL DEFAULT '{}',
    data         jsonb NOT NULL DEFAULT '{}'::jsonb,  -- (J4)
    occurred_at  timestamptz NOT NULL,
    committed    boolean NOT NULL DEFAULT false,
    committed_at timestamptz
);
CREATE INDEX board_events_by_ref ON board_events (ref, occurred_at);
```

Claim keys (step 2 of §3.13). Each child that carries a `request_id` references the claim together
with its own entity reference, so a child cannot hang off another entity's request:

```sql
ALTER TABLE issue_comments
  ADD COLUMN issue_ref text GENERATED ALWAYS AS ('issue:' || issue_id) STORED;
```

```sql
ALTER TABLE board_events
  ADD CONSTRAINT board_event_claims_its_request
      FOREIGN KEY (request_id, ref) REFERENCES requests (request_id, ref) MATCH SIMPLE;
ALTER TABLE task_comments
  ADD CONSTRAINT task_comment_claims_its_request
      FOREIGN KEY (request_id, task_ref) REFERENCES requests (request_id, ref) MATCH SIMPLE;
ALTER TABLE issue_comments
  ADD CONSTRAINT issue_comment_claims_its_request
      FOREIGN KEY (request_id, issue_ref) REFERENCES requests (request_id, ref) MATCH SIMPLE;
ALTER TABLE product_comments
  ADD CONSTRAINT product_comment_claims_its_request
      FOREIGN KEY (request_id, product_ref) REFERENCES requests (request_id, ref) MATCH SIMPLE;
ALTER TABLE sprint_comments
  ADD CONSTRAINT sprint_comment_claims_its_request
      FOREIGN KEY (request_id, sprint_ref) REFERENCES requests (request_id, ref) MATCH SIMPLE;
ALTER TABLE sprint_budget_events
  ADD CONSTRAINT budget_event_claims_its_request
      FOREIGN KEY (request_id, sprint_ref) REFERENCES requests (request_id, ref) MATCH SIMPLE;
ALTER TABLE sprint_decisions
  ADD CONSTRAINT decision_belongs_to_its_close_request
      FOREIGN KEY (request_id, sprint_ref) REFERENCES requests (request_id, ref) MATCH SIMPLE;
```

The `UNIQUE (request_id)` on comment tables means at most one comment per claimed request.

**Request lifecycle in `SqlTaskAudit`.**

- The whole record is frozen in `requests.intent`; `operation` is its `kind`. Only records whose
  kind is an `EventKind` also get a `board_events` row. Generic records (`moved`, `edited`,
  `commented`, …) live in `requests` alone.
- `claim`/`stage` insert a `staged` row (`settled_at` NULL). `append` writes the row as
  `committed` with `settled_at` set, replacing its own staged row, and inserts the typed event.
- A mutation claims, applies and commits inside one database transaction (§7.1), so a failure
  leaves no row at all.
- A conflicting claim compares the stored record with the new one: different → `"request id
  belongs to another operation or payload"`; same and committed → replay, nothing written; same
  and staged → the existing claim is returned.
- A generic stage may replace a generic staged record, never a protocol one (`requests.protocol`).
  An accepted replacement by a different record resets `created_at`, so a staged row's age is the
  age of its current record. A commit over a row's own staged claim keeps its `created_at`.
- `discard` deletes a staged non-protocol row.
- A row staged outside a transaction whose writer died stays `staged`. The checkpoint writer calls
  `settle_stale_staged` before its gate counts staged rows, and settles each row staged longer than
  `STALE_STAGED_GRACE_SECONDS` (15 min). It commits the row when its effect is proven present: the
  record is its whole effect (`attempt.usage`, `attempt.outcome`, guard decisions, `routing`,
  `outcome_round_context`, anything marked `backend.revision = "not_written"`), or an effect table
  (`sprint_budget_events`, `sprint_comments`, `sprint_decisions`, `task_comments`,
  `issue_comments`, `product_comments`) holds a row claiming its request id. Every other row is
  refused, whether its effect is absent or cannot be proven. Refusal moves that same row to
  `discarded` in one statement guarded by `status = 'staged'`, which is the only writer of that
  status, and records the reason on the row as `intent.stale_refusal`. The row is a staged claim the
  settlement owns, not a frozen committed record. No request id is allocated and no other row is
  written. The refusal is found by the row's own id and status (`refusal`, `refusals`). A
  `discarded` request id is terminal: `stage`, `claim` and `append` refuse it. Settlement never applies an effect.
- A pending-request count is `SELECT count(*) FROM requests WHERE status = 'staged'`; it is the
  export gate (§6.3) and `ummanu task verify-audit`'s answer.
- One advisory lock (`ummanu.board.requests`) serializes separate claims outside a transaction;
  the per-card marker lock is also an advisory lock.

### 3.10 The eleven `jsonb` columns

| Column | Content |
|---|---|
| (J1) `sprints.observer` | tagged observer union (`{"kind":"head","profile":…}`); variants belong to the head registry |
| (J2) `sprints.source_audit` | provenance of a restored row (`created_at`, `updated_at`, `board`, original spelling) |
| (J3) `tasks.extensions` | unknown card metadata under one fixed top-level key (§8.2), plus markers such as `board_never_named` carried by older rows |
| (J4) `board_events.data` | per-`EventKind` payload, validated by `board/models.py` |
| (J5) `requests.intent` | the frozen audit record compared on retry |
| (J6) `issues.extensions` | (J3) for Issues |
| (J7) `products.extensions` | (J3) for Products |
| (J9) `sprints.owner_decisions` | append-only quoted entries with stable IDs and PO audit attribution; latest scoped answer wins; empty for released sprints |
| (J11) `po_feed.metadata` | optional native input source, visible summary, sprint ref and delivered comment position; NULL for historical expanded text |
| (J10) `sprint_resumes.po_request` | optional `{card, action}` identifying a board-owned PO wait; admission locks and validates the actual card, not references extracted from prose; NULL for released resumes |
| (J8) `sprints.local_run_exceptions` | creation-only list of `{project, argv, rationale}`; exact vectors for registered, reserved projects; default `[]` |

No extension bag may hold a field the schema names. Link sets, budget counters, resume fields and
close decisions are relational.

### 3.11 Not in this schema

Agent runtime and head processes; the head registry (`<data>/heads/heads.yaml`, `source.yaml`);
memory facts and the vector index; personas and adapters; secrets; provider sessions,
quotas and credentials; run journals (`state/pipeline/`, exported as `state/runs/**`); transcripts
and artifacts; knowledge documents.

### 3.12 Closed vocabularies

A closed vocabulary is a `CHECK` constraint on the column, never a reference table. Widening one is
an Alembic revision shipped with the code that emits the new value.

| Column | Values | Source |
|---|---|---|
| `products.state` | `active`, `archived` | `ProductState` |
| `issues.state` | `open`, `closed` | `IssueState` |
| `issues.issue_kind` | `bug`, `feature`, `question`, `improvement` | `product_issues.ISSUE_KINDS` |
| `issues.priority` | `P0`–`P3` | `product_issues.ISSUE_PRIORITIES` |
| `issues.close_reason` | `resolved`, `invalid`, `duplicate`, `wont_do` | `product_issues.ISSUE_CLOSE_REASONS` |
| `sprints.status` | `open`, `closed`, `stopped` | `SprintState` |
| `tasks.state` | the seven card states | `CardState` |
| `tasks.task_type` | `code`, `research`, `infra`, `decision`, `operation`, `wait`, or NULL | `board.task_routing.TaskType` |
| `tasks.review` | `required`, `skipped`, or NULL (legacy, read as `required`) | `board.task_routing.TaskReview` |
| `tasks.complexity` | `cheap`, `standard`, `hard`, `frontier` | `board.task_routing.TaskComplexity` |
| `tasks.family_preference` | `auto`, `claude`, `codex` | `board.task_routing.FamilyPreference` |
| `tasks.codex_launch_mode` | `tui` | `tasks._CODEX_LAUNCH_MODES` ← `head/command.py:CODEX_LAUNCH_MODES` |
| `sprint_budget_events.event_type` | six charged types + `infrastructure_blocked` | `sprints.BUDGET_RECORDED_EVENT_TYPES` |
| `sprint_decisions.verdict` | per subject kind | `sprint_close.ISSUE_VERDICTS`, `CARD_DISPOSITIONS` |
| `requests.status` | `staged`, `committed`, `discarded` | this schema |
| `board_events.kind` | the 23 `EventKind` values (§3.9) | `board/models.py:EventKind` |
| `board_events.entity_kind`, `requests.entity_kind` | `product`, `issue`, `sprint`, `card` | `EntityKind` |
| `repositories.role` | `primary`, `curator_root` | this schema (§3.1) |
| `owner_events.kind` | the 14 owner event kinds | `board.owner_events.KINDS` |
| `owner_events.class` | `needs_owner`, `notice`, derived from the kind (`owner_event_class_follows_kind`) | `board.owner_events.KIND_CLASS` |

A retired `codex_launch_mode` reads as NULL (`tasks.py`); an audit record whose kind is not an
`EventKind` stays a generic `requests` row (§3.9).

Open vocabularies (`claim_worker`, `slug`, `base_branch`, pins, comment markers, head/profile
columns) are not constrained; their values come from the head registry or the operator.

### 3.13 Executable order and revisions

Alembic builds the schema in two steps:

1. **Create tables** in §3 order: §3.1, §3.2, §3.3 (with `sprint_number_seq`), §3.4, §3.5 (with
   `card_board_key_seq`), §3.6, §3.7, §3.8, §3.9, plus Alembic's `alembic_version`.
2. **Add deferred constraints** in the same order: §3.3's scoped cursors, §3.4's
   `budget_card_is_in_this_sprint`, §3.8's scoped decision subjects, §3.9's generated `issue_ref`
   and the claim keys.

In `board/schema.py` every step-2 constraint carries `use_alter=True`.

Revisions (`src/ummanu/board/migrations/versions/`):

| Revision | Change |
|---|---|
| `0001_initial` | tables, constraints, roles, grants and default privileges (§5.5) |
| `0002_board_gaps` | `issue_comments`; `issues.extensions`; `sprints.ref` as primary key; nullable `tasks.project_id`; `depends_on`/`depends_on_task` split |
| `0003_task_type_optional` | nullable `tasks.task_type` with `task_type_is_a_known_type_or_nothing` |
| `0004_product_issue_sql` | `product_comments`; Product/Issue `board_key`; `products.extensions`; `tasks.date_moved` |
| `0005_sprint_sql` | Sprint runtime: relation ordinals, `sprint_resumes.recorded_at_source`, deferrable card→sprint reference |
| `0006_sprint_transport_key` | `sprints.board_key` |
| `0007_card_transport_key` | `tasks.board_key` from `card_board_key_seq` |
| `0008_po_sessions` | PO head `po_sessions`, `po_turns` (one running turn per session), `po_feed` |
| `0009_po_requests` | `po_requests`: each /po form request id, its operation and input fingerprint, and the session or turn it made |
| `0010_po_session_close` | `po_sessions.closed_at`, `closed_by`, set exactly when `state = 'closed'` (`po_session_closed_iff_audited`) |
| `0011_card_kinds` | `infra` in `task_type_is_a_known_type_or_nothing`; nullable `tasks.review` (`task_review_is_a_known_choice_or_nothing`); `tasks.live_impact` defaulting to false, research only (`task_live_impact_is_research_only`) |
| `0012_request_read_indexes` | indexes on `requests` only: committed by `ref` and in claim order, staged in claim order, by `intent->>'kind'`, by `intent->>'event_id'`, and the records owing an attempt outcome; the audit's narrowed reads (`docs/REQUESTS_GROWTH.md`) |
| `0013_budget_candidates` | one partial index on `requests` only, `requests_budget_candidates`: committed rows meeting the budget pass's candidate predicate (`board/budget_candidates.py`), in claim order |
| `0014_neutral_extension_bag` | data only: the extension bag of current `tasks`, `products` and `issues` rows moves onto the key `extra` (§8.2); refuses a store whose premise does not hold or that holds a non-committed `done-retention-` request; history is not rewritten; no downgrade |
| `0015_po_effort_resolved_model` | `po_sessions.effort` (text, not null, default `'default'`: every existing session ran at the CLI's own effort) and `po_turns.resolved_model` (nullable text: the model the CLI reported for that turn); no downgrade |
| `0016_sprint_po_session` | `sprints.po_session` (nullable text: the PO session that opened the sprint, or the one the PO service's resolver opened for it) and `sprints.allowed_productions` (text[], not null, default `'{}'`: registered projects whose production the sprint's operations may touch); every existing sprint loads with neither; `po_request_operation_in_vocabulary` re-created to admit `po_sprint_session` beside `po_session_create` and `po_send` (`po_request_seq_only_for_a_send` unchanged); no downgrade |
| `0017_po_card_kinds` | `decision` and `operation` in `task_type_is_a_known_type_or_nothing`, restated one for one; every existing row of `code`, `research`, `infra` or no type loads unchanged; no column; no downgrade |
| `0018_owner_events` | `owner_events` (`id` identity, `kind`, `class`, `subject_ref`, `text`, `created_at`, `read_at`, `dedup_key` unique as `owner_event_dedup_key_is_unique`; index `owner_events_by_subject`), with `owner_event_kind_in_vocabulary`, `owner_event_class_in_vocabulary` and `owner_event_class_follows_kind`; one new table, every existing row loads unchanged; no downgrade |
| `0019_po_session_title` | `po_sessions.title` (nullable text: a readable name the owner or the PO sets); every existing session loads untitled, then each session a `sprints.po_session` names and whose title is null takes that sprint's ref (the first created, if two sprints name it); the downgrade drops the column |
| `0020_wait_card_kind` | `wait` in `task_type_is_a_known_type_or_nothing`, restated one for one; every existing row loads unchanged; no column (the spec and state are extension-bag keys, §8.2); the downgrade restores `0017`'s vocabulary and fails while a `wait` card exists |
| `0021_delegated_card_settled` | `delegated_card_settled` in `owner_event_kind_in_vocabulary`, restated one for one (a notice; `owner_event_class_follows_kind` unchanged); every existing event loads unchanged; no column (a card's origin and return state are extension-bag keys, §8.2); the downgrade restores `0018`'s vocabulary and fails while such an event exists |
| `0022_origin_returns` | `origin_returns`, the origin-return outbox (`board/origin_outbox.py`): `id` identity, `task_ref`, `event_id` unique as `origin_return_event_is_unique`, `request_id`, `target_state` (`origin_return_target_is_terminal`: done or blocked), `created_at`, and the delivery `delivered_at`, `status` (`origin_return_status_in_vocabulary`: delivered or skipped; `origin_return_status_with_its_delivery`: set exactly with `delivered_at`), `notice`, `session`, `po_request_id`; the partial index `origin_returns_undelivered` (`id` where `delivered_at IS NULL`) and `origin_returns_by_card`; one new table, every existing row loads unchanged; the downgrade drops it |
| `0023_sprint_e2e_budget` | `sprints.e2e_budget` (integer, not null, default 3) and `sprints.e2e_used` (integer, not null, default 0) with `sprint_e2e_counts_are_not_negative`: every existing sprint, open ones included, loads with a budget of 3 and nothing used; `sprint_e2e_charges` (`dispatch_id` primary key, `sprint_ref` references `sprints` on delete cascade, `task_ref`, `charged_at`; index `sprint_e2e_charges_by_sprint`); `e2e_budget_spent` in `owner_event_kind_in_vocabulary` and, as a `needs_owner` kind, in `owner_event_class_follows_kind`, both restated; the downgrade restores `0021`'s two constraints and drops the table and the columns, and fails while an `e2e_budget_spent` event exists |
| `0024_e2e_after_merge_kind` | `e2e_after_merge` in `owner_event_kind_in_vocabulary` and, as a `needs_owner` kind, in `owner_event_class_follows_kind`, both restated: an after-merge e2e run that needs the owner (secretary-1807); the after-merge records themselves are typed fields of a card's `e2e` bag field, no column; the downgrade restores `0023`'s two constraints and fails while an `e2e_after_merge` event exists |
| `0025_card_waits_for_person` | additive widening of the owner-event vocabulary/class constraints for real sprint Blocked decisions and PO waits; backfills unresolved open sprint waits lacking an open needs_owner event, skips superseded/archived/wait cards, preserves prior rows; no column or new ledger; downgrade refuses while the added kind exists |
| `0026_sprint_local_runs` | additive `sprints.local_run_exceptions` jsonb, not null, default `[]`, array CHECK; existing sprints gain no exceptions; released empty create intents retain request identity; ships through the automatic release migration boundary; downgrade refuses while a nonempty declaration exists |
| `0027_sprint_owner_decisions` | additive quoted owner decision array, default `[]`; existing permission/counter/charge records unchanged; downgrade refuses nonempty authority |
| `0028_owner_turns` | reclassifies released `card_waits_for_person`, `e2e_budget_spent`, `e2e_after_merge` rows as notices, preserving IDs, quotations, dedup keys and read history; adds `po_card_escalated` with matching current kind/class constraints. The `owner_event_routine_notice` trigger normalizes actual 0023/0024/0025 producers' obsolete class during release activation, so their real occurrences remain notices. Downgrade refuses while any changed kind exists rather than fabricate old attention. No new table or column. |
| `0029_po_channel` | nullable typed PO wait on `sprint_resumes`, object CHECK; released six-field resumes, delivery cursors and audit are unchanged. Task PO execution assignment and per-run disposition use existing extension bags. Downgrade refuses while a typed PO wait exists. |
| `0030_po_input_context` | nullable object metadata on `po_feed`; accepted queue context is copied with turn claim; historical bytes are unchanged; downgrade refuses to lose recorded delivery positions |

`0007` upgrades an occupied `0006` store in place: it assigns keys in stable reference order,
advances the sequence past the backfill, runs `SET CONSTRAINTS ALL IMMEDIATE`, then makes the column
non-null, unique and range-checked. Refs, numbers, relations, comments and audit rows are untouched.

`0008` and `0009` only add tables, and `0010` two nullable columns and one `CHECK` that every existing
(open) row satisfies; their use is in [Operations](OPERATIONS.md#po-head-sessions-and-turns).
`0011` leaves every existing card with `review` NULL and `live_impact` false, which both constraints
admit.

Catalogue at head, counted from a real `postgres:16` by `tests/test_board_store_schema.py`
(including `alembic_version`): 31 tables, 61 `CHECK`, 45 foreign keys, 31 primary keys, 19 `UNIQUE`,
5 partial unique indexes.

---

## 4. Reservations

`sprint_projects` holds reservation history; there is no separate reservations table. At most one
row per project may have `reserved = true`:

```sql
CREATE UNIQUE INDEX sprint_projects_one_live_reservation
    ON sprint_projects (project_id) WHERE reserved;
```

A second sprint reserving a held project fails with a unique violation inside its transaction.
Release is an `UPDATE`, never a `DELETE`:

```sql
UPDATE sprint_projects
   SET reserved = false, released_at = now()
 WHERE sprint_ref = 'sprint:1432' AND reserved;
```

`reserved_matches_release` forbids a released row still marked reserved.

On PostgreSQL every Sprint protocol operation runs in one transaction under
`pg_advisory_xact_lock(1600)`, shared by admission, request claims and reservations. The other
admission rules (`open_sprint_limit`, repository-tree overlap) are enforced in `sprints.py` inside
that transaction.

---

## 5. Operational boundary of the PostgreSQL installation

Operator steps are in [OPERATIONS.md](OPERATIONS.md#postgresql-board-store).

### 5.1 Container

`postgres:16` in its own Docker container, defined by `/opt/ummanu/postgres-compose.yml`, which
`board/provision.py` writes and verifies (mode 0600 or narrower). Bootstrap creates it; upgrade
reconciles it (`step_board_store_provision`) before migrations. Only the database is containerized;
CLI, dispatcher, web and heads run on the host. Client tools (`psql`, `pg_dump`, `pg_restore`) are
used from inside the container.

### 5.2 Persistent volume

```yaml
services:
  postgres:
    image: postgres:16
    restart: unless-stopped
    ports:
      - 127.0.0.1:${UMMANU_DB_PORT}:5432
    environment:
      POSTGRES_DB: ${UMMANU_DB_NAME}
      POSTGRES_USER: ${UMMANU_DB_OWNER_USER}
      POSTGRES_PASSWORD: ${UMMANU_DB_OWNER_PASSWORD}
    volumes:
      - board-db:/var/lib/postgresql/data
volumes:
  board-db:
```

- Compose project `ummanu-board-store`; volume `ummanu-board-store_board-db`, outside
  `<data>`, so file-level backups never copy live database files.
- Reconciliation verifies image, restart policy, loopback publication and mount before
  `compose up`; drift is refused, not repaired by recreating the container.
- An existing volume without `board-store.env` is refused: the image environment initializes only
  an empty volume.

### 5.3 Port publication

Loopback only: `127.0.0.1:<UMMANU_DB_PORT>` (fresh default 5432) → container 5432. Clients run
on the host, so the port must be published.

### 5.4 `board-store.env`

`<instance>/board-store.env`: `KEY=VALUE`, mode 0600, git-ignored, never in the secret store and
never in archives.

```
UMMANU_DB_HOST=127.0.0.1
UMMANU_DB_PORT=5432
UMMANU_DB_NAME=ummanu
UMMANU_DB_OWNER_USER=ummanu_owner
UMMANU_DB_OWNER_PASSWORD=<generated>
UMMANU_DB_APP_USER=ummanu_app
UMMANU_DB_APP_PASSWORD=<generated>
UMMANU_DB_READ_USER=ummanu_read
UMMANU_DB_READ_PASSWORD=<generated>
```

- All nine keys are required. An unknown, missing or empty key, a symlink, or `mode & 0o077`
  refuses the file.
- `materialize_fresh` generates three independent `secrets.token_urlsafe(32)` passwords, writes
  the git ignore first and publishes the complete file atomically. It refuses to replace an
  existing file; there is no implicit rotation.
- `store.resolve` / `resolve_role(instance, role)` is the only way to a configured store and
  enforces the git exclusion; a tracked file refuses.
- Role per connection: `board_client` defaults to `app`; `owner` is used by migration,
  provisioning and recovery database administration; `read` by read-only restore and recovery
  verification.
- Upgrade with no file: board-store steps are skipped. With a broken file: upgrade fails before
  container or migration work.

### 5.5 Roles and privileges

| Role | Privileges |
|---|---|
| `ummanu_owner` | initialization superuser from `POSTGRES_USER`; owns the schema; runs migrations |
| `ummanu_app` | `SELECT, INSERT, UPDATE, DELETE` on all tables, `USAGE` on sequences; no DDL |
| `ummanu_read` | `USAGE` on the schema, `SELECT` only |

`0001_initial`, run as owner, creates the two login roles with passwords passed as run parameters
(`config.attributes`, rendered as literals because `CREATE ROLE` takes no bound parameter) and
grants:

```sql
CREATE ROLE ummanu_app  LOGIN PASSWORD :'app_password';
CREATE ROLE ummanu_read LOGIN PASSWORD :'read_password';
GRANT USAGE ON SCHEMA public TO ummanu_app, ummanu_read;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO ummanu_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO ummanu_app;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO ummanu_read;
ALTER DEFAULT PRIVILEGES FOR ROLE ummanu_owner IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ummanu_app;
ALTER DEFAULT PRIVILEGES FOR ROLE ummanu_owner IN SCHEMA public
  GRANT USAGE ON SEQUENCES TO ummanu_app;
ALTER DEFAULT PRIVILEGES FOR ROLE ummanu_owner IN SCHEMA public
  GRANT SELECT ON TABLES TO ummanu_read;
```

Default privileges make tables added by later revisions visible to `app` and `read`.
`step_board_store_roles` verifies owner/app/read logins and the privilege boundary after
migration. The owner's `POSTGRES_PASSWORD` is read only when the volume is empty; changing it
later requires `ALTER ROLE`. Rotation procedure: [OPERATIONS.md](OPERATIONS.md#postgresql-board-store).

### 5.6 Connections

`SqlCardClient` keeps a bounded pool of psycopg connections, opened lazily, with autocommit off.
The bound is `POOL_SIZE` (4) per client. The web process serves each request on its own thread,
so four readers of one client run at once instead of queueing on one connection. The dispatcher
tick and each CLI process use one thread and so hold one connection. The web process, dispatcher
tick and each CLI process build their own clients.

A thread pins one pooled connection for a **session**:

- a read or write outside every session borrows a connection for that one statement;
- one board call (`call`, `call_batch`) is one session, so its statements and its commit share a
  connection;
- `transaction()` pins one connection for its whole extent, per thread. Nested `transaction()`
  calls, board calls and statements on the same thread join it. The connection is borrowed before
  the thread waits for its turn: transactions of different threads still take turns, so the
  thread holding the turn never waits for the pool. Staged Product/Issue and Sprint creates
  belong to the thread that staged them (`SqlCardClient._staged`): no other thread's read sees
  them, and a transaction's end drops its own. Lanes a transaction adds stay its thread's until
  it commits (`_add_lane`); a rollback drops them;
- a session-level advisory lock (`SqlTaskAudit._locked`, `marker_comment_lock`) is a session, so
  the lock and its unlock run on one server session.

When the outermost session ends, the connection's open work is ended before it returns to the
pool: committed after a clean exit, rolled back after a failure or an aborted statement (with
`pg_advisory_unlock_all()`, since an aborted session cannot have run its unlock). An idle pooled
connection never holds a transaction.

Exhaustion: a thread that finds all `POOL_SIZE` connections in use waits up to
`POOL_WAIT_SECONDS` (10 s) for one to return, then fails as `backend_unavailable`. A thread never
holds more than one connection of a client.

Reconnect rule: a long-lived client survives a board-store restart without a process restart.
Dead connections are never handed out and never returned to the pool:

- outside a transaction, a pinned connection that is `closed` or `broken` (psycopg's own verdict)
  is discarded and another is borrowed;
- an idle pooled connection is checked before it is handed out: `closed`, `broken`, or a socket
  with something to read. An idle connection has nothing to read unless the server hung up on it
  (a terminated backend or a restarted server), so that connection is closed and the next idle or
  new one is used. A read between a server restart and the next call therefore succeeds;
- a statement outside a transaction that fails and leaves the connection `closed` or `broken`
  still fails as `backend_unavailable`, and the connection is discarded, so the next call
  reconnects. The failed call is not retried;
- inside `transaction()` there is never a reconnect: the statement and the transaction fail as
  before. When the transaction ends with the connection dead, or its rollback fails, the
  connection is discarded and the next operation opens a new one;
- a statement error on a live connection (`backend_error`) and a rollback that succeeds keep
  the connection. A healthy connection is reused, so there is no reconnect per call.

### 5.7 Backup and restore

A `full` archive carries a custom-format data-only dump (`postgres_dump` component); roles and
credentials are not in it. Contract and
procedure: [RECOVERY.md](RECOVERY.md#backend-aware-cold-archives).

### 5.8 Python dependencies

Core dependencies in `pyproject.toml`: `psycopg[binary]>=3.2` (driver; wheels bundle libpq),
`SQLAlchemy>=2.0`, `alembic>=1.13`. `sqlalchemy`, `alembic` and `psycopg` are imported inside the
functions that need them, so an upgrade can start on a venv that lacks them. Upgrade order:
`step_dependencies` → `step_board_store_provision` → `step_board_store` (migrations) →
`step_board_store_roles` → later steps and service restarts.

---

## 6. Ownership boundaries

### 6.1 Canonical in PostgreSQL

| Data | Writer |
|---|---|
| products, issues, sprints, tasks and their link tables | `ummanu_app` through the board protocol: dispatcher tick, CLI commands, `webproto/ops.py`, `webproto/sprint_ops.py` |
| `projects`, `repositories` (derived, not canonical) | Product project-set and Sprint repository-list writes (§3.1) |
| comment tables | the same writers |
| `sprint_decisions`, close reason, closeout document path | `SprintWriter.close` |
| `sprint_budget_events` | `SprintWriter.record_budget`, dispatcher |
| `sprint_resumes` | `SprintWriter.resume`, observer through the CLI |
| `requests`, `board_events` | `SqlTaskAudit` (§3.9, §7.3) |

The normalized entity identity (§2.2) is a process property, not data.

### 6.2 Canonical in files and git snapshots

What PostgreSQL does not own is canonical as files in the live root, a plain directory with no Git
([Recovery](RECOVERY.md#layout)). Each path has one writer, and none of them commits
([Recovery](RECOVERY.md#writers)):

| Data | Location | Writer |
|---|---|---|
| project/repository bindings, adapters | `<instance>/projects/*.yaml`, `<instance>/adapters/*.yaml` | onboarding (`project add`, `provision-apply`, `gate`); otherwise an operation card plus `config check` |
| instance config, personas, heads canon, skill manifest | `<instance>/instance.yaml`, `persona/`, `heads/heads.toml`, `skills/manifest.toml` | an operation card plus `config check` |
| secrets | `<instance>/secrets/**` | secret store |
| memory facts and pack ledgers | `<instance>/state/memory/**` | memory writer |
| knowledge, incl. sprint closeouts | `<instance>/state/knowledge/**` | knowledge writer |
| host-local material | `<instance>/runtime.env`, `board-store.env`, `secrets/installation.key` | secret materialisation / bootstrap; never exported |

Board, sprint and run state is not a live-root file. Its canon is PostgreSQL (§6.1) and the pipeline
role worktree's run journals (`state/pipeline/`); the tick exports both into the data directory
(`<data>/board`, `<data>/runs`, §6.3). Transcripts, artifacts, backups, the vector index, the
head-registry pair and onboarding drafts are derived data-directory state.

The git snapshot is not a working tree and nobody edits it. It is a derived artifact of the snapshot
exporter: each window commits one cut into the bare repository `<data>/backup/instance.git`
(`offsite.snapshot_repo`), made of the live root's allowlisted files, `state/board` and `state/runs`
staged from the exports, and `snapshot-manifest.json`. The pusher publishes that branch to the
instance remote, and recovery reads it back ([Recovery](RECOVERY.md#snapshot-repository)).

### 6.3 Exports

- The export is generated, never edited. `export_board` reads through `board_client` and
  `task_audit_for`, so it is generated from PostgreSQL.
- `export_board` refuses while the audit owner reports staged requests (§3.9).
- `events.ndjson` in `state/board` is a generated projection of committed audit, stored as
  immutable segments ([Recovery](RECOVERY.md#board-checkpoint-layout)) and read by
  `board/analytics.py` from a sealed copy. Live readers never use it as the audit (§7.3).
- The checkpoint refuses to publish a truncated or rewritten export over a non-empty journal.

---

## 7. Transactions and migrations

### 7.1 One transaction per protocol mutation

`SqlCardClient.transaction()` wraps a whole protocol mutation; nested calls join it. Inside it:

1. claim the request id in `requests` (§3.9);
2. write the entity rows, links and comments;
3. insert the `board_events` row for a typed event and commit the claim.

Any failure rolls back all three. A Product/Issue create that was staged but not finished inside
the transaction aborts the commit. Sprint operations take `pg_advisory_xact_lock(1600)` first (§4).

Effects outside the database (knowledge closeout commit, head launch) happen after commit and are
recorded by their own committed events.

### 7.2 Isolation level

PostgreSQL's default `READ COMMITTED`. Invariants are held by constraints and unique indexes
(one live reservation per project, one claim per request id, one decision per subject per sprint,
one row per reference), which hold under any isolation level. Read-then-decide rules are serialized
by advisory locks: `1600` for Sprint operations, the request-namespace lock for claims, and a
per-card lock for marker comments.

### 7.3 Idempotency, audit and partial-command recovery

| Concern | Mechanism |
|---|---|
| request-id namespace | `requests.request_id` primary key; comments and budget charges claim it too |
| staged and committed records | `requests.status` and `board_events.committed`, under advisory locks |
| stage / commit / `committed(request_id)` | insert/upsert on `requests`, then compare the stored `intent` |
| generic-stage replacement | allowed only over staged rows with `NOT protocol` |
| `event_id` owner | lookup by `intent->>'event_id'` in `requests`, served by `requests_by_event_id` |
| same-event check | comparison against `requests.intent` |
| effect and record | one transaction (§7.1) |
| half-applied board writes | none: a rolled-back mutation leaves nothing; `reconcile` answers `(0, 0)`; a claim staged outside a transaction by a writer that died is settled by the checkpoint tick (`settle_stale_staged`, §3.9) |
| Product/Issue effects | one transaction with their claims |
| marker comments | advisory lock per card marker |

Caller contracts: same `request_id`, same replay answer, same refusal on reuse with another
payload, installation-wide scope.

**Narrowed reads.** `events(reference, kind=, references=, since=)`, `events_page(end=, limit=)` and
`_occurrence_projection_records(kinds, outcome_owed=)` take their filters. `SqlTaskAudit`
applies them in SQL, each served by an index of `0012_request_read_indexes`: a ref or ref set by
`requests.ref`, a kind by `intent->>'kind'` including the released action spellings
(`_event_action`), a window by `settled_at`, a page by the committed claim-order index, and a
projection's slice (its kind, records sharing an `event_id` with it, records owing an outcome) in
one statement; a page and its count are one statement too, so they share a snapshot. Nothing the
production tick runs reads the audit without a reference set, a window or a page bound
(`tests/test_dispatcher_observer.py::unfiltered_audit_reads_raise` guards it, with no exemption);
whole-history readers such as restore stay off the tick and the web request path.

**Budget candidates.** The budget pass (`_reconcile_sprint_budget`) reads a page of
`uncharged_budget_candidates(limit=)`: committed records meeting the classifier's necessary
conditions (`board/budget_candidates.py`, over both the `kind`/`payload` and the
`record_type`/`transition` shapes) with no committed record under their charge id
`sprint-budget-<event_id or request_id>`. `SqlTaskAudit` walks `requests_budget_candidates` (`0013`)
in claim order and probes each charge id through the primary key. There is no cursor: a record that commits late, or whose card lookup fails for
any number of ticks, stays in the set until it is charged. Every candidate leaves it exactly once, by
a charge, a `budget_unlinked` marker (card has no sprint) or a `budget_unclassified` marker (the
classifier types it nothing, or its terminal taxonomy is invalid), each under the charge id. The growth policy that rests on this is `docs/REQUESTS_GROWTH.md`.

**Card edges.** `TaskWriter._transition_card` and `TaskWriter.retire_done` run inside
`TaskWriter._mutation()`. The claim, the state/archive change, the caller's finishing writes (claim
metadata, Ready reset, reason comment) and the committed event are one transaction. A failure is an
ordinary refusal with no repair obligation.

**Audit owner.** `tasks.task_audit_for(client, data_dir)` returns `SqlTaskAudit(client)`; SQL is
the audit canon and `data_dir` is ignored. `TaskWriter`, `SprintReader`/`SprintWriter`,
`ProductIssueStore`, the dispatcher runtime and its command host take the owner from there. Live
readers:

| Reader | Reads |
|---|---|
| `CheckpointWriter` publication gate | staged `requests`; an unavailable card client blocks the checkpoint by name |
| `ummanu task verify-audit` | staged count, backend named; exit 0 clean, 1 pending |
| `CommandReadLayer.command_history` / `command_request` | committed and staged `requests`; unreadable audit is `unavailable`/`unknown`, never empty or `not_found` |
| `webproto.ops` product-run publication | the `requests` row of the generic `product_run.*` record |
| `BoardEventCanon` | the audit its caller's client named; with neither audit nor data directory it refuses |
| `SprintReader` / sprint status reads | `requests` via `task_audit_for`; `_AuditOnce` has no data-directory construction |
| `ReadLayer.task_snapshot` / `task_events` | committed `requests` for the card, paged by ordinal; no file under `<data>/board` is opened |

`tests/test_architecture.py::FileAuditOwnershipTests` keeps the list of file-audit constructions
empty.

**Card event readers.** `ReadLayer._events` returns `webproto.journal.CommittedAudit` (ordinal
pages over committed audit, no file). The cursor names its kind, `ordinal`; a cursor of another
kind is refused.

### 7.4 Schema versioning and migrations

- **Schema:** `src/ummanu/board/schema.py` declarative models. CHECKs are `CheckConstraint`,
  partial unique indexes are `Index(..., postgresql_where=...)`, generated refs are
  `Computed(..., persisted=True)`, deferred keys carry `use_alter=True`.
- **Revisions:** `src/ummanu/board/migrations/versions/`, shipped in the package (§3.13). Each
  runs in its own transaction (`transaction_per_migration`); `0001` has no downgrade.
- **Version table:** Alembic's `alembic_version`; no other bookkeeping.
  `migrate.EXPECTED_SCHEMA_REVISION` and `migrate.head_revision()` name the head
  (`0030_po_input_context`); a test holds them equal. PostgreSQL restore compares against `head_revision()`.
- **Connection:** no `alembic.ini`. `ummanu.board.migrate` builds the Alembic `Config` in code
  and passes `env.py` an owner connection from `board-store.env`; `env.py` refuses to open its own.
- **Role passwords:** read from `board-store.env`, passed in `config.attributes`, never stored in a
  revision file.
- **Lock:** `pg_advisory_lock(ADVISORY_LOCK_KEY)` on the migration session for the whole run,
  released once at the end. Owed revisions are read under it and run one at a time, each committed
  before the next; a failure is `MigrationFailed` naming the revision, what was committed before it
  and the server's cause. A failed run is rolled back *before* the unlock, and a cleanup that cannot
  run invalidates the session (the lock ends with it) instead of replacing the original failure.
- **Where it runs:** bootstrap, and `step_board_store` in `ummanu upgrade` (§5.8 order).
  No file → skipped; current → unchanged; broken or tracked file, unreachable server or failed
  revision → failed, before service restart. These apply every owed revision, whatever it declares.
- **At release (`board/release_migrations.py`, `dispatch/production_checkout.py`):** when the
  dispatcher releases a card whose merge moves the production checkout it runs from
  (`production_runtime.product_root`), both release paths (push, and GitHub's post-merge refresh)
  pin the fetched target to its full commit id, refuse a checkout that cannot fast-forward to it,
  then apply the target's owed revisions and only then `merge --ff-only <commit>`. The running
  build is the old one, so the revisions come from the target: `git archive <commit>
  src/ummanu/board/migrations` out of the checkout's own objects, extracted into a temporary
  directory and run as the script location of the same `migrate.apply`, owner connection and lock.
  The version table must then hold the target's head. The connection has a connect timeout and a
  session `lock_timeout` (`UMMANU_RELEASE_MIGRATION_LOCK_TIMEOUT_SECONDS`, default 30) bounding
  the advisory-lock wait and every DDL lock wait. Nothing owed: one lock, one read, unchanged. Any
  refusal (`release_schema_refused`: `bundle_unreadable`, `store_unavailable`, `lock_timeout`,
  `destructive`, `unclassified`, `migration_failed`, `unknown_revision`, `not_verified`) leaves the
  checkout on its old commit. The release persists its original typed facts, remote delivery and
  exact board requests in its dispatcher record before creating one `operation` for the sprint's
  PO (`dispatch/release_activation.py`). A registry, sprint or board refusal retains that obligation;
  ordinary ticks, including after restart, retry it before another activation or record removal.
  Only after the operation commits does the release write its canonical reason naming the operation,
  durably Block the source, and remove the release record. Request-id replay uses the persisted facts
  and description, even if the remote ref moves. Other projects' checkouts keep their plain
  fast-forward.
- **Release eligibility (for revision authors):** the release applies a revision unattended only if
  it declares, at module level, `release_safety = "additive"`: the previous release keeps working
  against the migrated store, because the revision only adds tables, nullable or defaulted
  columns, indexes, grants or widened CHECK vocabularies, and drops, renames or narrows nothing the
  previous release reads or writes. Declare `release_safety = "destructive"` otherwise. A
  destructive or undeclared revision (`0001`–`0024` declare nothing) is refused before any owed
  revision runs, and that release is a person's `ummanu upgrade`. A revision is loaded by the
  *previous* build at release, so it imports from the product only what that build already ships.
- **Schema gate (`board/schema_gate.py`):** every operational connection reads `alembic_version`
  once when it opens, before any schema-dependent statement: each new connection of
  `SqlCardClient`'s pool (cards, sprints, products/issues, SQL audit, `SqlBoardHost`), each
  `PoStore` operation and each `OwnerEventStore` connection of its own (one joined to a client
  transaction was admitted with that client's connection). Exactly
  `EXPECTED_SCHEMA_REVISION` is current: one read, no Alembic import. A revision of this build's
  lineage short of it, or no version table at all, is refused with code `schema_owed` naming the
  actual and expected revisions and the owed migrations in application order, in each caller's own
  error family (`CardSchemaOwed` is a `TaskError`, `PoSchemaOwed` a `PoStoreError`,
  `OwnerEventsSchemaOwed` an `OwnerEventsUnavailable`). A refused connection is closed, never
  pooled, so the next read checks again and succeeds once the upgrade applied the migrations. A
  revision the lineage does not contain is a later build's schema and is read as additive, not
  refused: the previous build's dispatcher finishes its release against it. Destructive migrations
  are not supported by this. `ummanu doctor` reports the same assessment
  ([Protocols](PROTOCOLS.md#board-schema-gate)). Exempt, because they exist to inspect or apply an
  owed schema: `migrate` (bootstrap and `step_board_store`), `provision` (container and role
  probes), `postgres_recovery` (dump preflight and restore target, which migrate first).
- **Drift check:** `tests/test_board_store_schema.py` migrates a real `postgres:16`, checks the
  §3.13 catalogue and requires an empty Alembic autogenerate diff against the models.

## 8. Metadata the model does not name

### 8.1 Marker comments

The first line of a comment is a marker only when it is a complete `[token]` line and the token
is a role in `_ROLES`, one of the prefixes `report:`, `review:`, `decision:`, `issue:`,
`validate:`, `claim:`, `watchdog:`, or one of `sprint:resume`, `archive`, `rejected`,
`steward:blocked-done`, `provision:request`. Otherwise `marker` is NULL; the body is kept whole either way.
`steward:blocked-done` is not the role `steward`.

### 8.2 The extension bag

Card, Issue and Product metadata keys outside `_KNOWN_METADATA` are stored under one fixed
top-level key of the row's `extensions` column (`tasks`/`issues`/`products`): `extra`, defined once
as `EXTENSION_BAG` in `board/extension_bag.py` and read and written by `board/sql_cards.py` and
`board/sql_product_issues.py`. A card document read through `TaskReader` carries the same bag as
`extensions.extra`. It also holds the card's observed swimlane, which never overrides the lane
derived from the product. A key on many rows indicates a missing column.

Three typed keys live here on purpose, with no column: the owner-handover mark of a `decision` or
`operation` card (`waiting_owner`, `waiting_owner_reason`, `waiting_owner_by`; `board/owner_handover.py`,
[Protocols](PROTOCOLS.md#handover-to-the-owner)). Only `task handover` writes them, a card that leaves
In progress or an atomically recorded owner answer drops them, and they are read only through `waiting_owner`, which treats a partial or
malformed set as no mark. A store without them reads exactly as before.

One more typed key, with no column either: `touches_production` of an `operation` card, a registered
project id or `none` (`board/production_rights.py`, [Protocols](PROTOCOLS.md#production-rights)). Only
`task create` writes it, and it is read only through `touches_production`, which treats a malformed value
as none at all. No other kind carries it.

A `wait` card's three keys, with no column either (`board/wait_card.py`,
[Protocols](PROTOCOLS.md#wait-cards)), each JSON text: `wait`, the spec, written once by `task create`;
`wait_state`, the dispatcher's observation, frozen result and delivery records, written only by the
dispatcher; and `wait_cancel`, written only by `task cancel`. Each is read through its own reader
(`wait_spec`, `wait_state`, `wait_cancel`), which treats a field that does not parse as absent.

A delegated card's two keys (`board/po_origin.py`, [Protocols](PROTOCOLS.md#po-delegation)), each JSON
text: `po_origin`, `{session, request}` of the PO turn that created the card, written once by `task
create --role po` inside a PO turn and never again; and `po_return`, the dispatcher's `executor`,
and `successors`, written only by the dispatcher. Each is read through its own reader (`po_origin`,
`return_state`), which treats a field that does not parse as absent. What the card owes its origin
session is not a bag key: it is the card's rows of `origin_returns` (`0022`).

A code card's e2e key, with no column either (`board/e2e_record.py`, [Protocols](PROTOCOLS.md#the-e2e-stage)),
JSON text: `e2e`, the runs the dispatcher dispatched for the card (intent, run, wait card, frozen result,
the Blocked move a run ended in), written only by the dispatcher (`TaskWriter.record_e2e_state`) and read
through `e2e_state`, which treats a field that does not parse as no runs. It also carries
`budget_wait` while the card waits on the decision a spent e2e budget needs. A code card outside every
sprint has a second key, `e2e_cap`, JSON `{raises: [{add, authorized_by, decision, at}]}`, the raises
of its own e2e cap the owner authorized, written only by `task e2e-budget` (`TaskWriter.raise_e2e_cap`)
and read through `e2e_budget.cap_raises` (secretary-1796).
After-merge runs additionally hold an optional `disposition_result` receipt of native
PO completion. Covered marks' optional `holder` is the live obligation, distinct from
their historical `decision` link; absent/null preserves released readback and empty
means no active holder. Both use the same e2e extension text and normalized metadata.
The actual hotfix's optional `e2e.hotfix_route` is the typed `{carrier, run, result}`
copy of that receipt, with native `blocked_by` holding its live operation/follow-up.
Its public schema and normalized metadata preserve this same text. Reconciliation
locks sorted operation/carrier/source/hotfix/follow-up rows in its SQL transaction
and commits receipt, marks, hotfix dependency and audit/comment together, then
reconstructs queue projections. Retention is not terminal disposition evidence;
complete supported board reads retain unresolved sources and carrier records. No new table, authority grant,
origin backfill or released-bag migration is needed; 0029 leaves those bags untouched,
including absent hotfix_route. It does not invent terminal route authority, and native
downgrade refuses existing hotfix route receipts rather than letting older consumers forget them.

The only other top-level keys are the markers in `EXTENSION_MARKERS` (`board_never_named`, §3.10).
Rows written before `0014_neutral_extension_bag` held the bag under the retired board's name; that
revision moved current rows onto `extra`. History (`board_events`, committed `requests`) and
checkpoints written before it are not rewritten: restore reads a record's `extensions` through
`fold_extension_bags`, which folds every top-level key other than `extra` and the markers into the
bag, the current bag winning a field both name.

---

## 9. Stable identifiers

| Identifier | In the schema |
|---|---|
| `sprint:N` and other `sprint:` references | `sprints.ref` primary key, stored verbatim; `sprints.sprint_number` nullable `UNIQUE`; new numbers from `sprint_number_seq` |
| `<project>-<n>` task refs | `tasks.task_ref` primary key; `UNIQUE (project_id, task_number)`; the next reference is allocated over all project cards, archived included (`next_reference`) |
| `product:<id>` | `products.product_id` primary key, `products.ref` generated `UNIQUE` |
| `issue:<hash>` | `issues.issue_id` primary key, `issues.ref` generated `UNIQUE` |
| integer protocol address | `board_key` per table (§2.2) |
| sprint↔issue | `sprint_issues` |
| sprint↔project | `sprint_projects` (§4) |
| sprint↔repository | `sprint_repositories` → `repositories` |
| card→sprint | `tasks.sprint_ref` → `sprints.ref` |
| card→card dependency | `task_dependencies.depends_on`, `.depends_on_task` |
| supersession | `task_supersessions` |
| archive | `tasks.archived`; rows are never deleted |
| issue close reason | `issues.close_reason` |
| sprint close reason and account | `sprints.close_reason`, `sprints.closeout_document` (path), `sprint_decisions` |
| card decision, worker report, review verdict | `board_events` + comment `marker` |
| budgets | `sprint_budget_events` + derived totals |
| `request_id` | `requests.request_id` primary key; referenced by `board_events`, the four comment tables, `sprint_budget_events`, `sprint_decisions` (§3.9) |
| `event_id` | `board_events.event_id` primary key; `intent->>'event_id'` for generic records |
| head run reference | `board_events.head_run_ref` (reference only) |

Nothing renumbers or re-derives an existing reference. Entity identities recorded before this store
carry another store word and still read back to their number (§2.2).

Owner-turn records in `extensions.extra`: `owner_answer` is JSON text containing the current
handover audit ID, answer audit ID, verbatim quotation, original mark, PO session and answer time;
`owner_escalation` is JSON text containing a persisted claim/answer episode ID and explicit reason.
Card mutations and their required owner-event creation/settlement share the SQL transaction.
Released three-field handovers remain valid. The dispatcher accepts an existing released owner
comment only by its owner audit marker, current handover order and exact body digest, then delivers
the durable answer through the existing deterministic PO submission ID. No answer is synthesized.


### PO input context (0030)

New decision/operation submissions carry an explicit `deliver_sprint_comments`
flag beside frozen card facts and a bounded `display_summary`. The PO service
also carries these fields into new owner-answer follow-ups from the frozen
initial facts; released follow-ups retain their original request bytes.
The service
reads the native sprint at acceptance, under its existing submit lock. It selects
comments after the maximum accepted position for that actual session and sprint
from pending queue metadata and claimed feed metadata. Board order, including
same-time occurrences, defines the position; dates and prose are never cursors.
The native sprint comment list is append-only in this delivery contract.

The selected comment excerpts and the service's original production-rights note
are frozen together in the durable queue. Claiming the request copies metadata
into `po_feed` in the same transaction as the turn and request id. Rendering and
refusals before acceptance consume nothing; a retry finds the existing queue or
request before reading new context. A fresh session has its own boundary. A shorter native history is refused rather than skipping evidence. No
separate delivery store is introduced. Sprint context rollover uses a new actual session with its own
accepted position; predecessor positions and frozen request targets remain unchanged. Canonical
sprint/predecessor rollover IDs and caller bindings reuse `po_requests.po_sprint_session`; transition
measurement, threshold and seed pointers reuse the first service input's queue/feed metadata.

`metadata.source` explicitly classifies `dispatcher` and `po-service` inputs.
`metadata.summary` is visible on `/po`; the complete accepted prompt, including
instructions, comments, rights and native full-text pointers, is in a closed HTML
`details` element. Owner text stays ordinary text. Released queue records and
historical feed entries without metadata remain readable and expanded; their
original text is never guessed into a new format or migrated. Their missing
comment position starts the first new flagged delivery at zero. Full engine
backup/export carries this board column; normalized card export is unchanged.
