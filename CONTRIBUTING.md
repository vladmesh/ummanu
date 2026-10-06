# Contributing

`ummanu` is developed in the open around one deployment profile: a single trusted owner running
one appliance on one host.

## Feedback

Issues, pull requests, security reports and rough notes are welcome, in any format. Keep real
credentials and other private data out of public reports. For a change to task lifecycle, recovery,
host ownership or the security boundary, state the intended contract in the issue or pull request.

## Development setup

On Ubuntu 24.04, install `python3-venv` first (`sudo apt-get install --yes python3-venv`).
Run the following from your checkout:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[memory,dev]'
.venv/bin/python -m tests.broad
```

Keep the package installed in editable mode: tests and runtime commands resolve deployment assets
from the checkout. Use `.venv/bin/python` for the commands below, or activate `.venv` first.

The `dev` extra is the pinned `ruff`; `required-version` in `pyproject.toml` makes any other version
refuse to run. Lint only changed and untracked Python paths with the command in
[Testing](docs/TESTING.md#changed-python-lint), never the whole repository.

The unit suite is hermetic: it needs no Docker, board store, network or configured instance,
because a board client is built only from an explicit instance's `board-store.env`. See [tests/README.md](tests/README.md). A live canary
belongs in an operator runbook or an explicitly opted-in integration test against a disposable
endpoint, never in a `test_*` module the default run discovers.

## Preparing a change

- Scope a diff to one problem.
- Cover changed behaviour with tests.
- When product behaviour changes, update the affected document under `docs/` so it describes the
  current behaviour.
- Prefer the standard library; justify a new runtime dependency.
- Write commit messages and code comments in English.

## Before a pull request

```bash
python3 -m tests.broad
python3 -m ummanu role-skills audit --check
```

`python3 -m tests.broad` is the local `unit` and `component` profile, not the gate: a pull request is
judged by the exact-SHA GitHub CI run described in [Testing](docs/TESTING.md). When a change touches
another suite, run it by module name (`python3 -m unittest tests.test_bootstrap`) rather than through
repository-wide discovery.

To keep the result after the terminal scrolls, run the suite through the receipt wrapper. It streams
the same output, exits with the same status and leaves a summary in the ignored `state/checks/` that
`check show` reads back:

```bash
python3 -m ummanu check broad --module tests.broad
python3 -m ummanu check show --module tests.broad
```

The receipt shapes (`--module`, `--command`, a project adapter's `broad_check`) and what each attests
are described in [Operations](docs/OPERATIONS.md).

In the pull request, say what changed and what you verified, including any manual check the suite
does not cover.

## License

Contributions are accepted under the [Apache License 2.0](LICENSE).
