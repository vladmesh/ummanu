# ummanu

Portable personal appliance for running multiple AI agent heads across many projects from a remote
VPS. The repository contains the CLI, the task and memory protocols, the dispatcher runtime, restore
logic, schemas and generic skills.

The product repository holds no installation data. Private installation configuration and portable
state live in the installation's live root, a plain directory; the snapshot exporter commits it into a
local snapshot repository and pushes that to a private instance remote, the recovery checkpoint. Local
mutable and derived runtime state lives in a local data directory. Install and recovery are documented in
[Recovery](docs/RECOVERY.md).

## Documentation

- [Vision](docs/VISION.md) — what the appliance is for and who it is for
- [Roadmap](docs/ROADMAP.md) — product states, milestones and open questions
- [Architecture](docs/ARCHITECTURE.md) — storage boundary, runtime flow, security model
- [Head runtime](docs/HEAD_RUNTIME.md) — the one head runtime, `local-pty`, and the A20 record of how it replaced Orca
- [Head scopes](docs/HEAD_SCOPES.md) — head scope ownership, memory limits and memory exits
- [Head vitality](docs/HEAD_VITALITY.md) — observation axes, snapshots and their invariants
- [Outcome lineage](docs/OUTCOME_LINEAGE.md) — durable round handoffs between worker and reviewer launches
- [Board store](docs/BOARD_STORE.md) — board read/write inventory and the PostgreSQL schema
- [Requests growth](docs/REQUESTS_GROWTH.md) — the decision to keep every `requests` row
- [Owned cleanup](docs/OWNED_CLEANUP.md) — who settles dispatcher-owned Git residue
- [Protocols](docs/PROTOCOLS.md) — command contracts for tasks, sprints, memory and secrets
- [Operations](docs/OPERATIONS.md) — runbooks for a running installation
- [Recovery](docs/RECOVERY.md) — the checkpoint contract, fresh install and restore
- [Testing](docs/TESTING.md) — CI suite taxonomy and local test boundaries
- [Rename](docs/RENAME.md) — the rename to `ummanu`: inventory of the old name and the transition design

## Install

The host bootstrap supports Ubuntu 24.04. Install the CLI and memory runtime from a checkout:

```bash
python3 -m pip install -e '.[memory]'
```

The editable install is required because the runtime also uses deployment assets from the checkout.
The shipped systemd units run `PRODUCT_ROOT/.venv/bin/…`, and `upgrade` installs dependencies only
into that `.venv`, so the checkout an installation runs from needs its virtual environment at `.venv`.

Bootstrap the host first:

```bash
sudo ummanu bootstrap --instance-remote REMOTE --instance-dir INSTANCE \
  --installation-user INSTALL_USER
```

For a new installation, continue with:

```bash
sudo ummanu install --instance-remote REMOTE --instance-dir INSTANCE \
  --installation-user INSTALL_USER
```

To rebuild an existing installation from its private checkpoint, use `recover` instead of `install`:

```bash
sudo ummanu recover --instance-remote REMOTE --instance-dir INSTANCE \
  --installation-user INSTALL_USER
```

Bootstrap installs Docker and Compose from the distribution and provisions the loopback-only `postgres:16`
board-store container and persistent volume, with its local mode-0600 ignored `board-store.env`,
through the current Alembic head; see [Board store](docs/BOARD_STORE.md).

## Status

The project is pre-1.0. It is developed against one opinionated deployment
profile: a single trusted owner running one appliance on one host. See
[SECURITY.md](SECURITY.md) for the boundaries of that model.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[Apache License 2.0](LICENSE).
