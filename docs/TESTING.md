# Testing

Dispatcher-owned exact-SHA GitHub CI is the complete test contract. It validates
`tests/ci-shards.txt`, then runs nine named jobs in parallel:

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
manifest invalid before any suite starts. When changing the runner or manifest, run:

    python3 -m unittest -v tests.test_ci_shards
    python3 scripts/ci_test_shards.py --check

`unit` is the in-process suite and should finish in about three minutes in CI. A module that builds a
virtualenv or runs pip, serves real HTTP on loopback, waits out a real wall-clock cadence or replays
minutes of recorded PTY output belongs in `component`, `runtime-component` or `packaging`. A
repository-wide AST check parses through `tests/source_trees.py`, so each file is parsed once per module.

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
locations.

The runner records `git status --porcelain=v1 --untracked-files=all` for the candidate checkout before
and after each suite; a green suite requires identical snapshots. Evidence keeps entry counts, digests
and at most ten changed-status entries.

Each suite also uploads raw coverage `coverage.<suite>` as `ci-coverage-<suite>-<sha>` (line and branch
coverage of `src/ummanu`, the background agents' `ummanu.automations` included; coverage is a CI-only dependency). The
aggregate step rejects missing, malformed or uncombinable data as an infrastructure failure and
publishes `ci-coverage-combined-<sha>` with `combined-coverage.json` (per-file executed/missing/excluded
lines and branches and the branch summary; coverage.py's per-function and per-class regions, which restate
those lists, are left out, and the published file is bounded at 5 MB) and `changed-lines.json`. For pull requests, `changed-lines.json` classifies each
changed source line against the exact base and head SHAs as `covered`, `missed`, `excluded` or
`not_executable`; other events mark it not applicable. A successful push to `main` also keeps the
aggregate as `ci-coverage-baseline-<sha>` for 90 days. There is no coverage threshold and no local
coverage collection.

The `test` job is the required aggregate result and succeeds only when every applicable suite
succeeds, coverage evidence combines, and both `typecheck` and changed-file `lint` succeed.
A failed, skipped or cancelled typecheck/lint cannot produce a green aggregate or publish a main
coverage baseline. Its summary lists each suite as `success`, `product_failure`, `infrastructure_failure`,
`cancelled` or `not_applicable`:

- a failing test is a product failure;
- missing, malformed or unwritable JSON/JUnit/log evidence, an unavailable Git status command, or any
  test-generated tracked or untracked artifact is an infrastructure failure. A contaminated suite is an
  infrastructure failure even if a product test also failed; the failure location stays in evidence;
- cancelled work is never success; routing that skips a suite records `not_applicable`.

## Control-host fast profile

    python3 scripts/ci_test_shards.py --fast

The one fast profile for worker feedback. It validates a fixed module list (`FAST_MODULES`) and runs
only hermetic board-refusal and pipeline-state proofs. The explicit SQL board injection
proof is `tests.test_hermetic_board_integration` in `integration-board`, outside the fast profile.
CI executes the real fast profile in the unit job. It is not a CI suite and does not
read `tests/ci-shards.txt` or use discovery.

The child process group has a 120-second ceiling; on timeout the runner reports failure, terminates the
group and waits for it. The child gets only a fixture-owned temporary home, XDG directories, Codex home,
pipeline-state directory, temp directory, a restricted tool path and the candidate source path, with no
board, API, cloud or other credentials. A startup guard rejects network connections and subprocesses
other than Python and the read-only temporary-instance Git queries of the board seam, so live board/API
use and Docker, VM, Ansible or provisioning commands fail loudly.

Start with focused checks and `--fast`. When a task or repository contract requires the local broad
suite, run the broad profile once through the receipt wrapper.

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

Runner-owned profiles delegate membership and node validation to the declared broad runner. They
need no module map. A selector appends to `broad_check.args`; `selector_args` supplies either an empty
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
remain before `--`. The `shared` runner owns the host profile, environment whitelist, empty PYTHONPATH,
ci_only marker family (docker, ansible, privileged, slow), deselection, budgets and refusal status.
The wrapper does not replace that launcher with pytest or impose an Ummanu module list.
Live granular codegen validation depends on the external delivery of shared's agreed
`python -m shared -- <pytest selector>...` interface. Install this declaration only after that delivery;
a declaration promises runner support, and cannot detect a runner that silently ignores all arguments.
These fixtures prove the wrapper interface, not the current live codegen runner. No other repository
or live adapter is changed here.

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

The wrapper appends the intact selector to these arguments, never substitutes it for them. Thus
pytest still collects the declared paths; a node selection may also collect other tests under those
paths. Pytest owns marker deselection and the no-tests-selected status. To avoid widening collection,
an appended selector must stay within the declared paths (or the candidate root when pytest has no
explicit paths). The installed adapters' separate-value options `-c`, `--rootdir`, `-m`, `-k`, `-p`,
`-o` and `--override-ini` are excluded when finding those paths; equals-form options are also retained.
[Prepared fragments](../examples/check-adapters/instance-local.yaml) retain the exact existing argv for
codegen-product-kit, codegen-platform-services, personal-site and dnd-simulator. They are additions to
existing adapters, not replacements for setup, smoke or validation configuration.

Manifest declarations use exactly unit + component and `module: tests.broad`. Declared reporting and
control arguments such as `-v` remain supported; test names and filters cannot narrow the declared
complete manifest profile. Malformed declarations or unreadable manifests fail without discovery.
The unshipped intermediate `modules` map is not a supported contract.

Legacy full-profile `ummanu check broad --reuse --module <adapter.module>` and `ummanu check show`
remain supported with the declared module arguments. With a local declaration, a legacy single
`--module-arg` selector is validated or delegated and runs without a receipt. A full declared argv
followed by the separator and one selector also runs as a subset; `show` refuses both subset forms.
Other shape overrides fail. Without `local`, only the old full-profile argv remains supported; the
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
