# Hermetic test contract

Every test run (a CI suite, `python3 -m tests.broad`, a named module) must produce the
same result whether or not the host running it has a live installation. `tests/__init__.py`
installs the defaults below before any `test_*` module is imported: `python -m unittest`'s
discovery imports `tests/__init__.py` first, so they are live before any test can reach a
production call path:

- `HOME` is disposable. `BASH_ENV` and `ENV` are removed; a test-owned `bash` on `PATH` runs the real
  interpreter with `--noprofile --norc`, so command stdout and exit status do not depend on a host's
  login/rc files. This is a test-tool fixture; production command rendering is unchanged. Tests of
  shell startup itself must opt in to an explicit interpreter and their own files;
- `UMMANU_DISPATCHER_BODY_DIR` is a run-owned default installed before test imports; focused
  fixtures may patch it and restore it at cleanup;
- `TMPDIR` (and `tempfile.tempdir`) point at one throwaway root the run owns and removes at exit; an
  `ummanu-*` or `orca-*` entry still in it then fails the run, naming the leak
  (`tests/test_suite_tmp_guard.py`);
- `TA_CODEX_HOME` is a throwaway Codex home, so a Codex bring-up never writes a directory-trust grant
  into the installation's home (`tests/test_hermetic_codex.py`);
- `TA_PIPELINE_STATE_DIR` is a throwaway pipeline state dir (below);
- `GIT_CONFIG_*` turns off `gc.auto` and `maintenance.auto` for every git the suite starts, so no
  detached gc writes into a temporary repository after its test returns.

## The pipeline pause flag is read from a state dir this run owns

The result-must-not-depend-on-the-host rule covers the live pipeline's pause flag.
`src/ummanu/automations/agents/pipeline/state.py` resolves `STATE` at import time and
`agents/pipeline/pause.py` binds `PAUSE_FILE` off it, so the file every
triggered-dispatch test runs against is fixed before any test body executes. At its
default that file is the live `<workspaces>/ummanu/pipeline/state/pipeline/pause.json`
of the machine running the suite: an operator holding `ummanu pause --mode freeze`
while the suite runs makes `runtime/dispatch._pipeline_paused()` true, and every dispatch
test takes the "pipeline paused — no dispatch" branch instead of the lifecycle branch it
asserts about, and the suite appends its own `runs.jsonl` records into that live directory.

`tests/__init__.py` therefore claims one throwaway `TA_PIPELINE_STATE_DIR` for the whole
run and removes it at exit, set before any `test_*` module is imported and set
unconditionally — an ambient `TA_PIPELINE_STATE_DIR` inherited from a worker, reviewer or
operator shell names exactly the live directory that must not be touched.
`tests/test_hermetic_pipeline_state.py` is the proof, in both directions: a hard freeze in
a production-like `<workspaces>/ummanu/pipeline/state/pipeline` (built under a
temporary root, never the live one) leaves a warm-reuse dispatch reusing rather than
skipping, while a freeze written into the suite's *own* state dir still pauses — so the
"not paused" half cannot pass by the reader having gone dead.

A focused test that needs a different value overrides the variable locally, the usual way
(`mock.patch.dict(os.environ, {"TA_PIPELINE_STATE_DIR": str(tmp)})`), or pops it to
exercise the default-path computation; both still work, since the suite default is just an
ordinary process environment value the `with` block shadows. Note that a test which pops
it and then *reads* a pause flag is back to reading the host — pop it only to assert about
a resolved path, as `test_pipeline_paths.py:LegacyMirrorPathTests` does.

## The suite must have imported the checkout it lives in

Every seam above keeps a *host* fact out of the run. This one checks which sources the run
imported. A head's shell carries `PYTHONPATH=$UMMANU_REPO/src`
(`src/ummanu/runtime/launch_prefix.py`), and every worktree on the pipeline host runs on
one shared venv whose editable install points at the production checkout's `src`. Both outrank a
worktree's own sources for a src-layout project, which has nothing importable at its root -- so a
suite run inside a candidate worktree can pass while exercising production's `ummanu` with
the candidate's test files.

`tests/test_hermetic_source_tree.py` asserts it: `ummanu.__file__` and
`ummanu.automations.__file__` must resolve inside the checkout that contains `tests/`. It installs no
seam and shadows nothing -- there is no default to patch here, only a fact about the process.

It is also the one guard in this family that is legitimately red on a perfectly good checkout: run
a worktree's suite with the production interpreter and no `PYTHONPATH` of your own, and it fails
and names both paths. That is the signal, not a defect. Point the interpreter at your own sources:

```
PYTHONPATH=$PWD/src python3 -m unittest ...
```

## Board reads are hermetic by construction, not by a patch

There is no default board fake to install, because there is nothing to shadow. A board client cannot be built from ambient environment variables at all: every
client comes from `ummanu.board.backend.board_client(<instance dir>)`, which resolves that
instance's local `board-store.env` and raises `backend_unavailable` when it is absent. Nothing
selects a backend; there is one.
A worker, reviewer or operator shell that inherits live-looking database variables therefore
cannot turn the unit suite into a client of a live board — the variables are simply not a
source of board configuration.

`tests/test_hermetic_board.py` is the proof, and it asserts both halves: with live-looking
`DATABASE_URL`/`PG*` in the environment and a temporary instance that has no `board-store.env`, the
status read fails closed with `backend_unavailable` while a patched `urlopen` turns any
accidental dial-out into a loud failure.

A CLI test that needs a board injects it where the client is built — the
`board_client`/`card_client` name the command or its layer binds (for example
`ummanu.task_commands.card_client`, or `board_injected()` on the sprint fixtures).

A test with sprint content of its own injects it explicitly rather than patching a global:
`collect_status(report, offline=True, sprint_client=sprint_store(self, status_seed()))` is the seam, and
`tests/test_hermetic_board_integration.py:test_a_test_can_still_opt_in_to_a_real_sprint_boards_shape`
is the worked example. Do not build a client against a real endpoint in a `test_*` module the
default `python -m unittest` run discovers; a live canary belongs in an operator runbook or an
explicit, separately opted-in integration test against a disposable endpoint instead.

Cards have one implementation, PostgreSQL, and there is no in-memory card board. A test that
needs cards gets a real store of its own: `card_store(self, seed)` (or `CardStoreCase`) from
`tests/sql_backend_fixtures.py`, seeded from a `CardSeed` of `tests/fakes/tasks.py` and dropped
when the test ends; `tests/fakes/dispatcher.py:dispatcher_seed()` is the dispatcher's board. The
server is one throwaway `postgres:16` per test process, so such a module belongs to a Docker shard
(`integration-board`), never to `unit`.
