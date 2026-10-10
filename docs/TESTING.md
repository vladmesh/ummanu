# Testing

Dispatcher-owned exact-SHA GitHub CI is the complete test contract. Every pull request,
push to `main` and manual dispatch starts the workflow and publishes the terminal required
aggregate `test`. `tests/ci-shards.txt` defines nine owners; a validated execution plan
determines which owners and modules apply to a PR. Pushes and manual dispatches run all nine:

| Suite | CI job | Scope |
| --- | --- | --- |
| unit | test / unit | Isolated product and protocol behaviour. |
| component | test / component | Individual Ummanu components and their direct adapters. |
| runtime-component | test / runtime-component | Runtime and local-PTY component boundaries. |
| integration-recovery | test / integration-recovery | Backup, checkpoint, restore and recovery flows. |
| integration-memory | test / integration-memory | Memory and curator integration flows. |
| integration-board | test / integration-board | Board, sprint, task and web protocol integration flows over a card store. |
| integration-dispatcher | test / integration-dispatcher | Dispatcher host: claims, ticks, gates and project git access. |
| integration-heads | test / integration-heads | Head launch, vitality, observer, review lifecycle and attempt accounting. |
| packaging | test / packaging | Bootstrap, installation, provisioning and upgrade flows. |

## Suite manifest

`tests/ci-shards.txt` owns the taxonomy: every top-level `tests/test_*.py` occurs exactly once, under
one suite name. Unknown names, missing files, stale or duplicate entries and empty suites make the
manifest invalid before any suite starts. On the control host, check a runner or manifest change
through the declared wrapper:

    ummanu check tests/test_ci_shards.py
    ummanu check tests/test_broad_suite.py

`unit` contains isolated product and protocol checks; `component` covers direct adapters with temporary
state. Real virtualenv preparation, service sockets, process-group lifecycles, wall-clock waits and
multi-component recovery or dispatcher flows belong in the existing CI-only suites according to their
domain. Fake provider executables and an in-memory board do not make a real service/process integration
a unit test. CI still enforces 5 seconds per test and 90 seconds per module in `unit` and `component`;
the local warning above 5 seconds does not change the verdict. A
repository-wide AST check parses through `tests/source_trees.py`, so each file is parsed once per module.

## PR selection

`CI selection` checks out the exact PR head, fetches history and validates the complete manifest
before classifying the diff. `ci-selection-<sha>/plan.json` is a bounded execution plan, not a
test receipt. It contains candidate SHA/tree, PR base SHA, merge-base, event/ref, manifest hash,
mode, reasons, original suite owners, selected module paths and explanations. The diff is from
the merge-base to the candidate; coverage changed lines retain the explicit PR base comparison.
Git paths use NUL delimiters. Both rename paths participate in classification.

- `docs-only`: every old/new path is `*.md`, or a documentation file under `docs/` with extension
  `.rst`, `.txt`, `.png`, `.jpg` or `.svg`. The test matrix, typecheck and lint are skipped. There
  are zero test-suite jobs and no hermetic fast run. The aggregate recomputes this classification;
  absent test and coverage artifacts are allowed only for that validated plan. LICENSE, YAML,
  executable files under docs and other arbitrary paths have no documentation exemption.
- `affected`: only selected manifest modules execute, within their original owners. The selector
  parses the base and candidate Python under `src/ummanu/` and `tests/`, resolves direct/relative
  imports, package initialization, literal dynamic imports and literal module references (including
  patch targets), and computes transitive consumers. Changed tests and helper consumers participate
  in the same graph. No analyzed module is imported or executed during selection.
- `full`: empty diffs, unavailable base/merge-base, unknown paths, configuration/YAML, dependencies,
  workflows, scripts, manifest and shared infrastructure changes, deleted/renamed/type-changed Python,
  ambiguous module identities, unresolved internal imports, invalid syntax/encoding or exceeded
  analysis bounds require the full manifest. Invalid test membership or plan authority refuses as
  infrastructure instead of choosing an empty set. Every push and manual dispatch is full,
  including documentation commits on main.

The supported impact boundary is deliberately conservative. Source readers,
subprocess execution, wildcard imports and nonliteral dynamic loading are opaque consumers.
Opaque product modules depend on all product Python; opaque tests depend on all analyzed Python.
Their transitive test consumers are therefore included even when no specific import edge is known.
Changing opaque code itself requires full fallback. This prevents uncertain consumers from being
silently omitted; it can select many more modules than the direct import closure. It does not
promise to discover arbitrary execution through an unrecognized third-party loader, native code,
an externally constructed callback or a new runtime mechanism. Extend the conservative boundary
before relying on selection for such code; changes to shared selection infrastructure already
fall back to full. Aliased recognized loader calls are included. Docstrings are documentation,
not executable import/patch targets; field `getattr` and module `__getattr__` do not add implicit
import edges, while imports and recognized dynamic loaders inside their bodies are analyzed normally.
Analysis never executes imports
to try to infer their runtime behaviour.

Call identity preserves import namespaces in lexical module, function, class and
comprehension scopes. A single direct scope-body import with no competing binding can
prove an alias; parameters, assignments/deletions, conditional imports and ambiguous
receivers refuse that proof. Methods, nested classes and comprehension bodies resolve enclosing
names outside the class namespace; the first comprehension iterable uses its enclosing scope.
Wildcard imports, global/nonlocal writes and explicit namespace mutation invalidate proof
conservatively, including mutation through another alias of the imported namespace or a
computed receiver whose value may contain that namespace. Conditional/container aliases
cannot preserve a safe identity after such a write.
Attribute stores/deletes, `setattr`/`delattr` and subscript stores/deletes invalidate
receiver authority. Namespace exposure also revokes authority without classifying later
dictionary operations: `globals()`, `locals()` and argument-free `vars()` refuse import
identity module-wide, even for read-only observations. Escaped namespace accessor functions
also refuse proof, including imported/value aliases and functions stored in containers.
`vars(namespace)` and `namespace.__dict__` invalidate that receiver's import authority
as soon as the dictionary is exposed, including value/container aliases, computed receivers
and alternate imports of the same namespace. Dictionary method writes, deletes and escaped
handles therefore cannot retain a safe identity merely because no store was enumerated.
The collector gathers bindings, aliases, mutations and exposures, propagates uncertainty
to a fixed point, then applies one authority veto before resolving immutable imports.
Both the `unittest.mock.call` exception and literal-loader proof consume that same result;
neither alternative spelling nor a later import can restore vetoed authority.
Only names actually bound by assignment targets (names and recursive tuple/list/starred
targets) carry value-alias spellings; attribute/subscript receivers and indices do not.
Value aliases retain hazardous spellings but do not establish safe identity. Their
uncertainty remains module-wide across lexical scopes, allowing conservative over-selection.
Proven `unittest.mock.call` constructs an expectation record without invoking its arguments,
so it no longer creates universal dependencies solely because its leaf is `call`.
Calls inside its arguments are still analyzed. Other imported APIs with the same name,
unknown receivers, source/file reads (including temporary files), subprocesses and entry
points remain conservative. Literal dynamic imports require a proven `importlib.import_module`
or builtin `__import__` identity; an ambiguous loader stays opaque even with a literal argument.
Relative dynamic names, nonzero/unknown import levels, nonempty/unknown `__import__` fromlists
and argument expansion stay opaque. A proven literal loader alias uses its API identity even
when the alias is spelled `call` or `open`.
`asyncio.run` remains opaque: its identity alone proves neither the origin nor the dependencies
of an arbitrary awaitable/callback. This refinement does not infer callback targets, receiver
types, arbitrary third-party/native execution or runtime monkeypatching through external code,
including `mock.patch.object`, indirect `sys.modules` monkeypatching and other external
monkeypatch APIs. There is no general discovery of calls such as `operator.setitem`;
an explicit `mock.__dict__` argument still revokes authority through exposure itself.
Such APIs do not prove static purity; the supported local mutation and exposure forms
above still invalidate import authority.

This safety boundary limits effectiveness. The historical independent ummanu-205 review of
[PR #714](https://github.com/vladmesh/ummanu/pull/714), candidate `c3b51bf1113bf66dd13d4903041556d35615c903`,
found 219 of 294 manifest test modules and 149 of 386 product modules opaque. Its offline graph
analysis selected 280–293 of 294 test modules and all nine owners for single-module nonopaque
product-source changes; opaque source changes required full fallback. This is safe over-selection, with practically no
code-PR wall-clock gain at that snapshot, not evidence of useful acceleration. The docs-only path
avoids suite execution independently of this limitation.

For example, `tests.test_ci_selection.SelectionTests` changes a pure `ummanu.leaf` consumed
directly by `tests.test_direct` and through `ummanu.middle` and `tests.helper` by
`tests.test_transitive`. It selects those two modules under unit/component and omits
`tests.test_other`, whose only dependency is the unrelated leaf. The CI-only Git fixture exercises
the same rule with real commits, one selected module, actual execution and native coverage.
The CI-only repository projection for `src/ummanu/board/terminal_taxonomy.py` prints every
selected module and its reason in `test / integration-dispatcher`'s bounded log. That projection
uses the committed source graph and checks that membership is smaller than the full manifest.
At candidate205 that projection selected 291 of 294 modules.
Fixtures and projections are not natural PR acceptance proofs.

The CI-only identity comparison in the same existing `integration-dispatcher` owner runs
the original analyzer pinned at `8d722448497dcbd4ab32ee6e347fb1bc53024d9d` and the refined
analyzer over one committed candidate source snapshot. Its bounded log records SHA/tree,
opaque counts, removed identities and graph edges, and before/after membership, owners and
every selected reason for `terminal_taxonomy.py`, `webfront/caddyfile.py`, `dispatch/observer.py` and the real
`test_web_front_cookie_order.py` expectation consumer. Those projections describe graph
effectiveness, not an executed affected-source PR or a wall-clock speedup. This infrastructure
PR itself requires the full manifest, and leaves the genuine affected-source acceptance proof
and final-main controlled-load measurement to their later authorized work.

The [repaired CI comparison at candidate `74977cd0`](https://github.com/vladmesh/ummanu/actions/runs/38091522011/job/114328788730)
used source snapshot `74977cd08e300d86609087fae26eccfbbb8d7fbf`, tree
`7a6c295565934a6a30744aaaeddbd7632ab4df81`, for both analyzers. It removed 740 obsolete
graph edges from the proven `unittest.mock.call` at line 93 of `test_web_front_cookie_order.py`.
Conservative handling of actual value aliases added 2,206 edges and three opaque modules:
`tests.test_dispatcher_sprint_admission`, `tests.test_sprint_guard_outside_sprint`,
and `tests.test_sprint_listing_budget`. Opaque manifest modules were 219 before and 221 after;
opaque product modules were 149 before and 149 after (387 product graph identities including
package ancestors). Total test opacity and total edges did not decrease. The comparison prints
the added aliases' potential call identities as well as the removed identity.

The prior candidate `fb8a8d68` made `ummanu.dispatch.observer` opaque through an
assignment-target defect: `record.run = ...`
incorrectly treated `record` as a bound value alias and tainted the unrelated proven
`owner_events.record` call. That defect forced an observer-only source change to full 294/294;
it was not unresolved API identity. The repair separates bound names from mutated receivers:
observer is nonopaque, and its projected change remains affected 291/294, matching the original
analyzer. Its membership and reasons are included in the same-snapshot comparison.

| Projected single-module change | Original mode/modules | Refined mode/modules |
| --- | --- | --- |
| `src/ummanu/board/terminal_taxonomy.py` | affected, 291/294 | affected, 291/294 |
| `src/ummanu/webfront/caddyfile.py` | affected, 291/294 | affected, 291/294 |
| `src/ummanu/dispatch/observer.py` | affected, 291/294 | affected, 291/294 |
| `tests/test_web_front_cookie_order.py` | full, 294/294 | affected, 291/294 |

All nine owners remain included. None of the three product projections removes or adds membership:
the cookie-order test still consumes `ummanu.webfront.commands`, whose source/subprocess
dependencies remain opaque. For the changed-test projection, the removed modules are
`tests/test_provider_models.py` and `tests/test_hermetic_source_tree.py` under unit, and
`tests/test_agent_prompt_transport.py` under component; no owner disappears and no module
is added. The full fallback reason for that test becomes the ordinary base/candidate
closure reason, with `changed test` for the consumer itself. These data show a bounded
false-positive repair and a small changed-test membership improvement, not product-source
acceleration. The unresolved consumers remain blockers to substantial selection gains.
The observer projection likewise retains the base/candidate closure reason; its existing
`tests/test_dispatcher_observer.py` consumer remains under integration-heads with
`conservative opaque consumer` as its selected reason.

Runner and aggregators independently regenerate the plan from the exact checkout/base/event and
require byte-equivalent JSON data. A manually edited subset, wrong event/base/SHA, duplicate or
foreign membership cannot authorize green. Actions uses the plan's suite list for its dynamic
matrix; final validation checks job applicability from the regenerated plan. Full CLI/default
suite execution remains full. The local `tests.broad` declaration remains complete unit+component;
selective PR execution cannot supply a local broad receipt.

## Required setup

A missing required dependency is an infrastructure failure, never a green skip. The one exception:
the daemon suites of `tests.test_memory_service` and `tests.test_memory_health` skip when
`ummanu[memory]` is not installed. CI installs memory extras for the suites that can reach those
proofs. `unit` installs only `.[ci,dev]`: coverage and the pinned Ruff used by lint-runner tests.
The shared Python setup lives in `.github/actions/python-setup`.

- `integration-memory` needs `ummanu[memory]`;
- PostgreSQL tests (for example `tests.test_board_store_schema`, `tests.test_postgres_recovery`,
  and the `integration-board`, `integration-dispatcher` and `integration-heads` card-store fixtures) need Docker, Compose, `postgres:16`, psycopg,
  SQLAlchemy and Alembic. They use disposable Compose projects and volumes on dynamically selected
  loopback ports.

Fixtures use only temporary state. No test contacts a live board or reads or writes the live
installation, its Compose project or volume. Real host, systemd, credential, live GitHub
authentication and recovery-drill contours are outside PR CI and are operator checks.

## CI evidence

Each suite run writes a GitHub step summary and uploads `ci-evidence-<suite>-<sha>` with
`report.json`, `junit.xml` and `test-output.log`. The log keeps up to 1,000,000 bytes and marks
truncation. Artifacts are retained 14 days. `<sha>` is the pull-request head SHA, otherwise
`github.sha`. The summary names the SHA, outcome, counts, duration, slowest tests and concise failure
locations, selected/executed module membership and the selection digest. Executed membership
comes from the runner's actual module clocks. Plan-bound runs produce schema 2 reports; the
previously shipped schema 1 remains readable for unplanned full CLI reports only. Schema 1
cannot satisfy a planned CI aggregate. No unpublished intermediate schema is accepted.

The runner records `git status --porcelain=v1 --untracked-files=all` for the candidate checkout before
and after each suite; a green suite requires identical snapshots. Evidence keeps entry counts, digests
and at most ten changed-status entries.

Each suite also uploads raw coverage `coverage.<suite>` as `ci-coverage-<suite>-<sha>` (line and branch
coverage of `src/ummanu`, the background agents' `ummanu.automations` included; coverage is a CI-only dependency). The
aggregate step rejects missing, malformed or uncombinable data as an infrastructure failure and
publishes `ci-coverage-combined-<sha>` with `combined-coverage.json` (per-file executed/missing/excluded
lines and branches and the branch summary; coverage.py's per-function and per-class regions, which restate
those lists, are left out, and the published file is bounded at 5 MB) and `changed-lines.json`. For pull requests, `changed-lines.json` classifies each
changed source line against the exact base and head SHAs as `covered`, `missed`, `excluded`,
`not_executable` or `unmeasured` (the file is absent from measured coverage). Other events mark
it not applicable. Affected coverage explicitly records `selected modules`, mode, plan digest
and module membership; it is never presented as full-profile coverage. Only selected raw data
is required, and duplicate/foreign raw data refuses combination. Docs-only requires no raw or
combined coverage artifacts. A successful push to `main` also keeps the full
aggregate as `ci-coverage-baseline-<sha>` for 90 days. There is no coverage threshold and no local
coverage collection.

The `test` job is the required aggregate result and succeeds only when every applicable suite
succeeds, coverage evidence combines, and both `typecheck` and changed-file `lint` succeed.
A failed, skipped or cancelled applicable typecheck/lint cannot produce a green aggregate or publish a main
coverage baseline. Docs-only explicitly requires skipped test_suites/typecheck/lint and a successful
selection job. A failed/cancelled selection or unexpected job result refuses green. Missing, corrupt,
wrong-SHA/plan, duplicate, foreign or mismatched selected/executed module evidence also refuses green.
Its summary lists each suite as `success`, `product_failure`, `infrastructure_failure`,
`cancelled` or `not_applicable`:

- a failing test is a product failure;
- missing, malformed or unwritable JSON/JUnit/log evidence, an unavailable Git status command, or any
  test-generated tracked or untracked artifact is an infrastructure failure. A contaminated suite is an
  infrastructure failure even if a product test also failed; the failure location stays in evidence;
- cancelled required work is never success; unselected owners are `not_applicable` only by a
  validated plan. Arbitrary skipped matrices without a plan refuse. Required suites cannot
  self-report `not_applicable`.

After merging a selector/workflow change and observing full main green, the observer arranges two
separate natural cards: a docs-only PR and a small supported Python-source PR. Do not create proof
branches, PRs or manual duplicate workflow runs as part of implementing selection. For docs-only,
inspect `CI selection`'s exact SHA/base/event plan, absence of matrix execution, skipped validation
jobs, terminal successful `test`, and the existing dispatcher's exact-SHA gate receipt. For the
code PR, compare the plan's selected modules and explanations with actual schema 2 membership/counts,
omitted owners marked not applicable, selected coverage and changed-line visibility, terminal
`test` and gate receipt. Keep those real check contexts and artifact links with the acceptance card.
This implementation PR changes the workflow/selector/manifest and must itself use full fallback;
its green CI proves delivery, not docs-only or narrow live routing. Main must still publish all-nine
evidence and a full coverage baseline. No adapter, branch protection or dispatcher gate change is needed.

Before reporting completion, inspect the check runs on the exact candidate SHA, including other
workflow runs on that SHA. The dispatcher reads all instances of its selected check names; a green
PR run does not supersede a failed or cancelled manual run with the same names. Cancelling a
redundant current-candidate run can leave a failed `test` aggregate even after the PR run succeeds.
Use the existing PR run when it supplies the required evidence. Let already-started runs finish;
if a selected run was interrupted, rerun its failed/cancelled jobs and wait for their terminal
results. Do not report completion while a selected check remains failed, cancelled or pending.
The failed aggregate's preceding suite/coverage steps identify its cause; the final shell assertion
alone does not establish a candidate test defect.

Read-only inspection for this repository (replace `CANDIDATE_SHA` with the committed full SHA):

```bash
gh api --paginate repos/vladmesh/ummanu/commits/CANDIDATE_SHA/check-runs \
  --jq '.check_runs[] | {name, status, conclusion, head_sha, details_url}'
```

This check does not mint a mechanical gate attestation or change the gate's selection policy.

## CI hermetic fast profile

    python3 scripts/ci_test_shards.py --fast

The CI full-profile supplement validates a fixed module list (`FAST_MODULES`) and runs
only hermetic board-refusal and pipeline-state proofs. The explicit SQL board injection
proof is `tests.test_hermetic_board_integration` in `integration-board`, outside the fast profile.
CI executes the real fast profile in the unit job only when the plan is full. Selective and docs-only
PRs never run it. It is not a CI suite and does not
read `tests/ci-shards.txt` or use discovery.

The child process group has a 120-second ceiling; on timeout the runner reports failure, terminates the
group and waits for it. The child gets only a fixture-owned temporary home, XDG directories, Codex home,
pipeline-state directory, temp directory, a restricted tool path and the candidate source path, with no
board, API, cloud or other credentials. A startup guard rejects network connections and subprocesses
other than Python and the read-only temporary-instance Git queries of the board seam, so live board/API
use and Docker, VM, Ansible or provisioning commands fail loudly.

Workers start with focused `ummanu check <selector>` checks. Their permitted local
checks are the adapter-declared broad profile and its subsets; the fast runner above is not another
worker validation command. When the task requires the local broad suite, run it once through the
receipt wrapper and reuse its intact receipt for unchanged content.

Before a fresh code `report:done`, every registered project declaring `broad_check` must supply an
intact complete green receipt for its exact full declaration, candidate interpreter, import provenance
and committed HEAD tree. The report writer reads evidence and never runs tests. A refusal writes no
accepted report marker and leaves the card In progress; `report:blocked`, non-candidate completion
and same-request report replay keep their existing contracts. Subsets write no full receipt.

Reviewers inspect code, commits and existing worker/CI evidence without executing tests. Every
`ummanu check` form, including `show`, `--reuse`, selectors and legacy overrides, refuses with 125
before resolution or receipt lookup. Reviewer packets embed the admitted worker receipt, its summary,
digest, artifact path, candidate SHA and content tree separately from exact-SHA mechanical gate
attestation. Missing, none and noop gates attest no suite: name the gap in the verdict and request
validation from the worker or CI. A worker receipt never becomes a CI gate attestation.

## Control-host local profile

Use the registered project's explicit local declaration:

```bash
ummanu check
ummanu check tests/test_local_check.py
ummanu check tests/test_local_check.py::LocalSelectorTests::test_known_ci_only_and_unknown_selectors_refuse_before_runner_or_import
```

The first command runs the entire declared local profile, or reuses its intact content-bound
worker-local broad receipt. The second runs one permitted module. The third runs one permitted test.
A module or node-id always executes and returns the runner's status, streams test output, and leaves
any full-round receipt untouched. Its JSON output identifies the selector and observed import; it
contains no full-round receipt or claim of full-profile validation. `check show` reads the full receipt.

Unittest accepts both the path/`::` form above and its native dotted node-id, for example
`tests.test_local_check.LocalSelectorTests.test_known_ci_only_and_unknown_selectors_refuse_before_runner_or_import`.
Pytest node-ids retain their parameter text as one argv argument, including spaces and punctuation:

```bash
ummanu check 'checks/test_model.py::test_value[param with spaces]'
```

In manifest profiles, selectors are checked for module membership before the runner starts or imports a test. Ummanu's
`tests/test_board.py` and its node-ids fail with `tests/test_board.py: shard integration-board;
execution only in CI`. An unknown module fails without an invented shard or test discovery.

Ummanu's installable example is [adapters/ummanu.yaml](../examples/check-adapters/ummanu.yaml):

```yaml
broad_check:
  module: tests.broad
  import_package: ummanu
  local:
    runner: unittest
    ci_manifest: tests/ci-shards.txt
    shards: [unit, component]
```

The module set is exactly `unit` + `component` from `scripts/ci_test_shards.py::load_manifest`.
The existing validator checks all CI ownership for duplicate, stale, missing or unclaimed modules and
empty shards. This validation is not a discovery fallback. The other seven shards run only in exact-SHA
CI. `tests/__init__.py` supplies the hermetic defaults before any selected test module imports.

The historical ummanu-202 routing baseline, measured on the control host on 2026-10-10, contained
141 modules and 2,644 tests:
92.254 seconds through the sequential module receipt wrapper (89.354 seconds in the native runner),
complete/passed with two existing skips, at observed host load 0.90–1.35. The saved pre-routing run
was 282.311 seconds for 160 modules and 3,337 tests; the card also records earlier 681/693-second runs. The old approximately
77-second estimate is not the current promise.

The later published observation is ummanu-205's final candidate
`c3b51bf1113bf66dd13d4903041556d35615c903` ([PR #714](https://github.com/vladmesh/ummanu/pull/714)),
tree `e9997e9efe9e3a3d538921ea1086483b29370ab9`: 142 modules (125 unit + 17 component),
2,677 tests and two existing skips, complete/passed, 98.670 seconds through the sequential wrapper
and 96.706 seconds in the native runner. The saved worker receipt's module timings and that
candidate's manifest confirm the module count. No host load was reported for this run; the
historical load sample does not apply to it. The target remains 100 seconds at load at most 2,
not an unconditional 98.670-second latency promise. This candidate observation is neither a
final-main measurement nor final-main controlled-load acceptance, and its worker-local receipt
is not a dispatcher gate attestation. Each candidate's own full receipt supplies its exact
duration, content tree, count and import provenance.

All moved checks remain in the same nine-suite manifest and execute when selected, or on every
full run: interpreter preparation and
cleanup journals run in `integration-dispatcher`; real PO sockets, turns and role launches in
`integration-heads`; process locks, child cleanup and native doctor execution in `runtime-component`;
snapshot export/recovery and bulk restore in `integration-recovery`; native CLI flows in
`integration-board`; installation, rename and web-process upgrade flows in `packaging`. The real
process/checkout receipt classes are in `tests/test_broad_check_process.py` (`integration-dispatcher`),
sharing the temporary checkout fixture with `tests/test_broad_check.py`. Local receipt integrity,
result invariants, timing, composition, manifest refusal and subset-without-receipt checks remain.
No test assertions, CI budgets, skips, coverage evidence or manifest/receipt formats change.

The example adapter is an operator-installable fragment. Existing live declarations of
`tests.broad` with `unit` + `component` continue to resolve membership from the candidate manifest;
no runner or receipt migration is needed. Updating the live adapter's timing comment is a separate
operator operation after merge, not part of worker validation.

Runner-owned profiles delegate membership and node validation to the declared broad runner. They
need no module map. Except for pytest below, a selector appends to `broad_check.args`; `selector_args` supplies either an empty
list for native positional arguments or `["--"]` for a launcher with a selector separator. The complete
profile always uses exactly the declared broad argv, without the selector separator.

For codegen-orchestrator, the prepared [declaration](../examples/check-adapters/codegen-orchestrator.yaml)
is an adapter fragment for a later operator installation:

```yaml
broad_check:
  module: shared
  interpreter: .venv/bin/python
  import_package: shared
  local:
    membership: runner
    selector_args: ["--"]
```

`ummanu check 'tests/test_x.py::test_name[param with spaces]'` then invokes the candidate interpreter
with `-m shared -- <selector>`, through the common import-provenance bootstrap. Declared broad args
remain before `--`. The `shared` runner owns the host profile, environment whitelist, per-suite import paths,
ci_only marker family (docker, ansible, privileged, slow), deselection, budgets and refusal status.
The wrapper does not replace that launcher with pytest or impose an Ummanu module list.

Worker/reviewer bring-up installs a startup audit hook in the dispatcher-owned workspace venv
and the declared candidate-local venv after setup. Resuming a materialized workspace repairs these
files without rebuilding dependencies. Direct `pytest`, `python -m pytest` and `python -m unittest`
refuse before runner/test import, with status 125 and one line naming `ummanu check <selector>`.
This includes `python`, `python3` and absolute candidate-local declared interpreter paths. Ordinary Python,
library imports of pytest/unittest and other Ummanu CLI commands continue to work. Native Ansible commands and
sudo in the head PATH refuse with one line directing execution to CI. The Docker guard's order,
backend bindings and exact policy are unchanged. Standing roles receive no head guard PATH.

Reviewer refusal takes precedence over wrapper ancestry. The guard reads the common role launcher's
`BOARD_ROLE=reviewer` identity from the live process exec environment in `/proc` ancestry, including
when a child clears its environment. A check argument, candidate adapter, inherited runtime.env value
or prompt does not set that identity: `role_env` overwrites it from the bound launch role. Both new
and supported retained review bring-up repair candidate-local startup hooks. Shared and external
interpreter prefixes are never written. The role's `PYTHONPATH` carries only the workspace-owned
`.ummanu-task-env/test-guard` startup directory for absolute external interpreters; it does not carry
the product source tree. Candidate-local venv hooks continue to work when a child clears its environment.

Worker authorization belongs to the live module runner process launched by the receipt wrapper, and its
descendants, in that workspace. The hook checks Linux `/proc` ancestry for the exact provenance
bootstrap digest and candidate import roots. Wrapper169's existing bootstrap is compatible; there
is no wrapper permission environment marker, permission file or cached PID. Native `shared` children retain
authorization through a fresh environment whitelist and `PYTHONPATH=""`. Sibling head commands,
later commands and reparented children have no such ancestor. The hook does not replace the native
runner's membership checks, fixture environment, marker families or budgets. Refusal is command
feedback; it introduces no review/CI classification, retry or restart-budget handling.

The acceptance boundary is the fresh report marker transaction: the writer validates the intact,
complete green full receipt against the declared check set, interpreter, candidate imports and
committed HEAD tree, then records a bounded admission snapshot with its SHA/tree binding.
The full receipt stays in the existing workspace artifact; audit and packets contain no per-test or
module timing, output or tail. `receipt_digest` identifies that original full artifact and is never
recomputed from the compact snapshot. `snapshot_digest` is SHA-256 over the snapshot's canonical
JSON (sorted keys, compact separators, UTF-8 with surrogateescape, excluding `snapshot_digest`).

`admitted_check` enforces a 16,384-byte maximum for the canonical snapshot including its digest,
exactly 15 top-level fields, the six-field module check set, and seven provenance fields with exactly
two import roots. Paths, check-set strings/arguments and provenance strings each have a 2,048-byte
limit; the exact argument vector has at most 64 entries. The generated summary is at most 512 bytes.
Counts allow only `tests`, `skipped`, `failures`, `errors`, `expected_failures`, and
`unexpected_successes`, each an integer from 0 through 2^63-1. Unknown fields are refused, rather
than copied or truncated. No per-test list enters this bound. An oversized declared contract or
provenance refuses before the report marker with the allowed worker wrapper command.

Handoff, recovery and packets use the same canonical snapshot verifier: digest, complete/exit-zero/
passed, exact check-set digest and import/environment provenance remain mandatory. Handoff also
compares the freshly authorized snapshot digest with the immutable admission, covering the original
SHA/tree, check set and artifact digest. Same-request replay retains that data without reading the card
or checkout again. Before consuming a report, replaying a pending Validate move, gating a recovered
Validate record or bringing up review, the dispatcher verifies durable candidate evidence against
that admitted binding. Report handoff requires the exact admitted checkout; later gates/reviews read
the immutable snapshot and verify its commit/tree and ancestry after machinery-owned base refreshes.
The exact-SHA gate owns the refreshed tree. Recovery verifies first, freezes or confirms the worker once, saves retention,
then performs the idempotent Validate move. A lost or changed receipt is an explicit refusal; reviewer
packets also label an unavailable artifact even when the immutable admitted snapshot survives.

Production activation requires a separate operation. Drain all pre-policy code report occurrences
with outstanding effects, including staged/pending report writes and `validation_move_pending`
continuations, and code Validate/review records for adapters declaring `broad_check`. Their released
producers never recorded `worker_check`; old accepted events must remain unchanged and cannot be
backfilled from report prose. Settle their current effects under the installed policy before upgrading,
or park them for an explicit new worker round under the new policy. A new round needs a new request
identity and full green receipt. Existing same-request historical replay remains available and does
not create new admission authority. Drain existing reviewer heads before activation so future commands
import the upgraded control-plane guard and receive regenerated packets. Native adapters that still
omit `broad_check.module` must declare their actual full suite first; no discovery can supply it.

Two live registered consumers have a separate unresolved precondition: `public-profile` and
`ai-risk-decision-lab` both declare `interpreter: /usr/bin/python3`, `module: doctest`, and
`import_package: doctest`. The public-profile args are `README.md`, `PLAN.md`, `content_plan.md`,
`series/README.md`. The ai-risk-decision-lab args are `README.md`, `open-questions.md`,
`research/funding-landscape.md`, `projects/risk-decision-lab/research/mit-data-recon.md`,
`projects/risk-decision-lab/applications/manifund/draft.md`, and
`projects/risk-decision-lab/applications/bluedot-rapid/draft.md`.
These Markdown doctest declarations import the stdlib package outside the candidate. They produce
no reusable candidate receipt under the existing provenance boundary. After activation, fresh
code `report:done` for either project necessarily refuses; `report:blocked` remains available.
Declaring a module alone does not resolve this precondition.

The separate production operation must explicitly resolve both consumers through a supported
candidate-owned check contract, or record an operational classification acknowledging that code
completion remains impossible for them. Until then activation readiness is unresolved. Do not
backfill accepted reports, exempt these projects, weaken provenance, or silently treat a docs-only
declaration as code completion evidence. This code card changes neither live adapters nor card
classification. Read-only precondition inspection, without executing any check:

```bash
activation_instance="${UMMANU_INSTANCE:-$HOME/ummanu-data/instance}"
for activation_project in public-profile ai-risk-decision-lab; do
  sed -n '/^id:/p; /^repo:/p; /^enabled:/p; /^adapter:/p' "$activation_instance/projects/$activation_project.yaml"
  sed -n '/^broad_check:/,$p' "$activation_instance/adapters/$activation_project.yaml"
done
/usr/bin/python3 -P -c 'import importlib.util; print(importlib.util.find_spec("doctest").origin)'
```

The inspected bindings point to these same-named adapters; the result is the shapes and args above
and an external stdlib `doctest.py` origin. Recheck the bindings and their named adapters before the
operation; changing only the installed policy leaves both prerequisites unresolved.
No deployment, service restart or executor registry write is part of this change. The natural live
DoD5 card follows production activation; fixtures and candidate CI are not a delivered live packet proof.

This is an accidental-command guard, like the Docker guard, not an isolation boundary against the
interpreter's owner deliberately using `-S`, forging the bootstrap argv, or replacing startup files.
Every preparation path uses the same host ownership decision: the dispatcher-owned workspace venv
is required; an additional declared venv is instrumented only if its prefix and site directory
resolve inside the candidate. A normal venv executable symlink to system Python does not change
ownership of its local prefix. External system/shared/live declarations are preserved and skipped
as write destinations. Their absolute commands receive the workspace startup hook through the
role's import path. Deliberately suppressing startup or replacing that path can bypass this
accidental-command guard; head checks must still use `ummanu check`. The installer refuses writes to external
prefixes or escaped site directories, and failures installing the required workspace guard still
fail preparation. Upgrade/live
delivery is a separate observer step; these changes do not update production. Fast policy and fake
backend regressions are local unit/component tests. Real interpreter, role launch and native
whitelist/absolute-child-pytest and bash/subshell/PATH-child proof belongs to `integration-dispatcher` CI.
The bounded runner model covers an empty PYTHONPATH and a per-suite PYTHONPATH override; it simplifies
codegen's suite scheduling and is not an exact copy of its native host argv. That fixture
is not live codegen evidence: live main `ec96fa79` still needs its next acting head's declared
`uv sync --locked` setup with pytest_timeout before granular node/count/marker validation.
The PO confirmed delivery of shared's agreed `python -m shared -- <pytest selector>...` interface
in codegen-orchestrator-1586, main `ec96fa79`, PR #761. Live granular validation still requires a
subsequent operator installation of this declaration and the wrapper. A declaration promises runner
support and cannot detect a runner that silently ignores all arguments. These fixtures prove the
wrapper interface; they do not attest the live installation. No other repository or live adapter is
changed here.

For a declared pytest runner, keep its paths, configuration, markers and plugin options:

```yaml
broad_check:
  module: pytest
  interpreter: .venv/bin/python
  import_package: framework
  args: [tests/unit, tests/tooling, tests/copier, -m, "not slow"]
  local:
    membership: runner
    selector_args: []
```

Declared collection roots define the allowed set. Full `check` / `check broad` passes the exact
original argv. A granular module, node-id or parameter replaces only positional collection roots
with the selected tokens, keeping every option, marker, configuration argument and the same
interpreter, environment and import provenance. Multiple pytest selectors are allowed; each is one
argv token and all must pass membership before execution. For the example above, selecting
`tests/unit/test_x.py::test_name[param with spaces]` produces
`["tests/unit/test_x.py::test_name[param with spaces]", "-m", "not slow"]`.

Membership compares the selector path before `::`, normalized relative to the checkout, with the
roots and their descendants. Absolute paths, any `..` component, leading `-` and paths outside roots
refuse before execution, naming the roots and CI. No file-existence discovery determines membership;
a missing file under a directory root reaches pytest. Pytest missing-file/node status 4 and native
errors propagate. Exit 5 becomes a one-line refusal naming deselection, the declared marker expression
if present, and CI. It never becomes a successful check or a full-round receipt.

The parser excludes values of known pytest options such as `-m`, `-k`, `-p`, `-c`, `-o`, `-W`,
`--rootdir`, `--confcutdir`, `--basetemp`, `--junitxml`, `--durations`, `--ignore`, `--ignore-glob`,
`--deselect`, `--cache-show`, `--debug`, `-r` and logging options. Equals-form options occupy one token. Unknown separate-value plugin
options and missing option values make granular resolution fail closed, with a hint to declare
`broad_check.collection_roots`; full argv remains unchanged. No implicit checkout-wide root is used.

Optional explicit `broad_check.collection_roots: [tests/unit, tests/tooling, tests/copier]` identifies
the complete set of exact positional argv tokens without inferring plugin option arity. The schema requires a nonempty
unique list of strings. Granular validation requires each root to occur exactly once in `args`, be a
relative path without `..`, a leading `-` or `::`, and not occupy a known option's value position.
All other tokens remain in their original order. A root repeated as an option value must be spelled
differently there (for example `--ignore=tests/unit`) to make its positional role explicit. Invalid
root roles refuse granular execution; a schema-valid full argv still runs unchanged.

This implements decision ummanu-168 variant 1, clarifying the
prior ummanu-161 append wording: an addressed node must execute only the selection.
[Prepared fragments](../examples/check-adapters/instance-local.yaml) retain the exact existing argv for
codegen-product-kit, codegen-platform-services, personal-site and dnd-simulator. They are additions to
existing adapters, not replacements for setup, smoke or validation configuration.

Manifest declarations use exactly unit + component and `module: tests.broad`. Declared reporting and
control arguments such as `-v` remain supported; test names and filters cannot narrow the declared
complete manifest profile. Malformed declarations or unreadable manifests fail without discovery.
The unshipped intermediate `modules` map is not a supported contract.

Legacy full-profile `ummanu check broad --reuse --module <adapter.module>` and `ummanu check show`
remain supported with the declared module arguments. With a local declaration, legacy
`--module-arg` selectors are validated or delegated and run without a receipt. A full declared argv
followed by the declared separator and selectors also runs as a subset; `show` refuses both subset forms.
Pytest legacy append inputs reach the same membership/replacement resolver. Other shape overrides fail. Without `local`, only the old full-profile argv remains supported; the
new bare command and selectors fail as `local_check_not_declared`. The wrapper never guesses membership.

Candidate interpreter and import provenance use the same adapter resolution and bootstrap as broad
checks. `--default-interpreter` supplies the dispatcher-owned candidate interpreter when the adapter
omits its own interpreter. A local receipt is worker evidence, never a dispatcher-owned exact-SHA CI
receipt. The unit shard covers manifest and injected runner-owned behavior on temporary repositories;
`integration-dispatcher` adds actual temporary shared-runner execution and pytest with declared paths
and markers, parameterized selectors, failure statuses and receipt preservation.

## Runtime deadline boundary

`runtime-component` owns real local-PTY, process-group, socket and lifecycle tests. None of them belongs
in `--fast`; the fast-profile regression rejects that. Expiry, retry, termination and recovery tests
inject short bounds instead of waiting for shipped deadlines.

`tests.test_runtime_deadline_contract.ShippedRuntimeDeadlineContractTests` is the exception: it starts
the production local-PTY substrate and runtime without overrides and checks the shipped delivery
deadline, grace and stop-confirmation wiring. It belongs only to `runtime-component`.

## Changed Python lint

CI runs pinned Ruff 0.16.4 through `scripts/ci_lint.py`, using exact PR base/head SHAs or the push's
before/head pair. Manual runs compare the candidate with its parent. Missing revisions or a checkout
that differs from the candidate fail; non-deleted changed Python paths are passed explicitly, and an
empty set succeeds without invoking Ruff. Renames and names containing spaces are supported. CI
checks lint only; local format verification remains scoped to edited code below.


The dispatcher-owned `.ummanu-task-env/venv` installs the candidate's `.[dev]` extra when its adapter
declares `broad_check` without `broad_check.interpreter`, and puts its tools on worker and reviewer
`PATH`. Without `broad_check` it stays bare. The receipt wrapper and protocol/report/verdict commands
use the absolute production interpreter; only the inner broad suite uses the candidate venv. An
adapter-owned `.venv` is used only when the adapter or its broad-check contract names it. The production
virtualenv is not a lint or test tool.

Never lint the whole repository. Against the task base, collect non-deleted changed and untracked
Python files and pass only that set to both checks:

```bash
base=$(git merge-base main HEAD)
{
  git diff --name-only -z --diff-filter=d "$base" -- '*.py'
  git ls-files --others --exclude-standard -z -- '*.py'
} | sort -zu | xargs -0r ruff check

base=$(git merge-base main HEAD)
{
  git diff --name-only -z --diff-filter=d "$base" -- '*.py'
  git ls-files --others --exclude-standard -z -- '*.py'
} | sort -zu | xargs -0r ruff format --check
```

`xargs -r` makes an empty set a no-op instead of letting Ruff default to the whole repository.

## Feature test boundaries

Where a change's tests live. Behaviour contracts are in [Recovery](RECOVERY.md),
[Protocols](PROTOCOLS.md) and [Board store](BOARD_STORE.md).

| Area | Modules | Suite |
| --- | --- | --- |
| Secret recovery, ownership barrier, materializer order, checkpoint publication | `tests.test_secret_recover`, `tests.test_checkpoint` | integration-recovery |
| | `tests.test_installation`, `tests.test_upgrade` | packaging |
| | `tests.test_github_credential` | component |
| Bulk card restore | `tests.test_bulk_card_restore` | integration-recovery |
| Bulk comment restore | `tests.test_bulk_comment_restore` | integration-recovery |
| Post-close order reconciliation, restore | `tests.test_restore` | integration-recovery |
| Cold archive and PostgreSQL restore | `tests.test_backup`, `tests.test_postgres_recovery` | integration-recovery |
| PostgreSQL schema, roles, privileges | `tests.test_board_store_schema` | integration-board |
| Published web path | `tests.test_web_front` | unit |
| | `tests.test_web_transport`, `tests.test_web_read_protocol`, `tests.test_web_run_protocol` | integration-board |

Notes:

- The root ownership fixture in the recovery tests is platform-gated: it needs `runuser` and a usable
  non-root account, and copies product packages under its own temporary root.
- Head-registry and checkout-reuse recovery tests use real local repositories, a real depth-1 checkout
  and an installation-user Git child.
- <a id="normalized-board-bulk-recovery"></a>`tests.test_bulk_card_restore` and `tests.test_bulk_comment_restore` drive the real
  `SqlCardClient.call_batch` against a disposable card store, on a sanitized production-shape fixture
  that holds only shape fields. Their timings are labelled
  `durability=excluded` and are structural, not an SLO. The full real-audit comment benchmark is opt-in:

  ```console
  UMMANU_FULL_BULK_BENCHMARK=1 PYTHONPATH=src python3 -m unittest -v \
    tests.test_bulk_comment_restore.DurableAuditBenchmark.test_full_production_shape_real_audit
  ```

- `tests.test_web_run_protocol` (in `integration-board`, not `runtime-component`) has two suites that
  start real processes on real terminals under `LocalPtyHeadRuntime`: `RealHeadOwnershipTests` (a head stopped from a `HeadRun` rebuilt from the
  write-ahead record) and `RealBackendContractTests` (`run_start`/`run_review` against the real backend;
  only the head binary is substituted).
- Whether the installed web service works is a host check, not a test:
  [Operations](OPERATIONS.md#running-a-card-through-the-installed-service) and
  [Auditing what is exposed](OPERATIONS.md#auditing-what-is-exposed).
- Never point a test or rehearsal at `/home/dev/ummanu-data`, `/home/dev/secretary-instance`,
  production systemd or a live observer.

## Test timing budgets

The declared Ummanu local unit/component profile records every test's stable identifier,
actual Python module, outcome and elapsed time in the full receipt's `parsed.timing` field.
`check show` and receipt reuse retain these observations. Local output lists the ten slowest
tests and warns for every test strictly over 5 seconds; warnings preserve the suite exit status.
Subsets print observations without writing or changing the full receipt. Legacy receipts remain
valid under their original schema and digest; their timing is unavailable. Missing, interrupted
or invalid native timing is explicitly unavailable/incomplete, without manufactured durations.

CI applies strict >5 seconds/test and >90 seconds/module budgets only to unit and component.
A violation is a product failure reported by exact identifier, observed duration and limit in
JSON, JUnit, logs and the job summary. Existing test failures and infrastructure/cancellation
classification remain intact. Integration, runtime-component and packaging retain their own
budgets, and workflow timeouts are unchanged. Codegen retains its native 0.5s/test and 240s CPU
budgets; these Ummanu thresholds do not apply to its runner.

Test clocks span unittest `startTest` through `stopTest`, including instance setup, teardown
and cleanups. Module clocks span all classes of the actual test module, including module/class
setup, teardown and cleanups; each module completes its stdlib suite before the next begins.
Imports and collection are outside module clocks. Fixture, loader and skipped subtest outcomes are separate
`native_outcomes` with source module, phase and native identifier, without per-test durations.
Completed fixture skips leave timing complete; fixture/loader errors retain native failures.
The shared callback outcome domain drives collection, validation and JUnit rendering for
both timed tests and native events. A loader `load_tests` AssertionError is a native failure;
a fixture AssertionError is a native error, as reported by unittest. Complete observations
are validated before serialization, retaining other tests' measurements for either outcome.
Missing test stops and interrupted execution leave timing incomplete. Empty selected modules
run an empty stdlib suite and have no test measurements or fixture execution.
The manifest importer loads its collector from candidate source under a private identity,
registered only while definitions execute. Installed receipt validation and rendering import
the installed canonical helper independently of the candidate manifest import order.
Permanent regressions use deterministic clocks. The intentional slow specimen belongs only to
a separate CI proof PR and must never enter the candidate or main.

Timing rollout exposed four modules containing real integration work. Their assertions and
fixtures are retained in CI: `test_docker_guard` executes installed role wrappers and native
tool subprocesses in runtime-component; `test_owned_cleanup` and `test_bounded_cleanup_replay`
exercise real Git, process termination, journals and runtime cleanup in integration-dispatcher;
`test_measurement_script` runs loopback HTTP and real cadence waits in integration-board.
The original component CI report observed a 94.627437s cleanup replay module, a 10.425188s
guard test and an 18.513881s measurement test. The journal-layout component tests remain local.
