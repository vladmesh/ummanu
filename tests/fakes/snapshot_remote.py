"""A local bare remote whose tip is a real exporter snapshot, for recovery tests.

The source installation is a live root without Git and a data directory holding a board and run
export. `SnapshotExporter` cuts it (with the export producers and the audit gate stood in for, as the
exporter's own tests do) into a bare snapshot repository, which is pushed to the remote. Its
`instance.yaml` already names the data directory and the remote the recovered installation uses.
"""

from __future__ import annotations

import getpass
import json
import subprocess
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.fakes.installation import CARD, PRODUCT_ROOT, SPRINT, write_memory_metadata
from ummanu import installation, upgrade
from ummanu.board.migrate import head_revision
from ummanu.checkpoint import SNAPSHOT_REF, CheckpointWriter, SnapshotExporter
from ummanu.data import DataExport

REVISION = "0123456789abcdef0123456789abcdef01234567"
ROLES = ("new_card", "reviewer", "observer", "curator", "retro", "steward")
HEAD = "recovered-head"
FACT = "---\nsource: test\ncreated: 2026-07-17\n---\nThe recovered installation keeps this fact.\n"


def canon(head: str = HEAD) -> str:
    """An installation-owned heads canon that routes every role to ``head``."""
    roles = "".join(f'{role} = "{head}"\n' for role in ROLES)
    return (
        '[resources.acct]\naccount = "acct"\n\n'
        f'[profiles.{head}]\nresource = "acct"\nadapter = "codex"\nfallback = []\n\n'
        f"[role_defaults]\n{roles}"
    )


def git(repo: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], text=True, capture_output=True, check=check
    ).stdout.strip()


class _SettledAudit:
    def status(self) -> dict:
        return {"ok": True, "pending": 0}


@dataclass
class ExporterRemote:
    """The remote, the source installation it was cut from, and the recovery target's paths."""

    root: Path
    remote: Path
    source: Path
    source_data: Path
    source_snapshot: Path
    target: Path
    data_dir: Path
    cards: list[dict]
    sprints: list[dict]

    @property
    def tip(self) -> str:
        return git(self.remote, "rev-parse", "--verify", SNAPSHOT_REF)

    def cut(self, *, stand_in: bool = True, state_dir: Path | None = None) -> str:
        """Cut the source installation once more and push the cut; returns the remote tip.

        `stand_in` replaces the board and run producers and the audit gate with the seeded export
        files; without it the cut exports the source's own board store and `state_dir`.
        """
        options = {"state_dir": state_dir} if state_dir is not None else {}
        with exporter_producers() if stand_in else ExitStack():
            result = SnapshotExporter(
                self.source_data,
                self.source,
                snapshot_repo=self.source_snapshot,
                product_revision=REVISION,
                board_schema_head=head_revision(),
                **options,
            ).write()
        if result.status not in ("committed", "unchanged"):
            raise AssertionError(f"exporter fixture cut failed: {result.reason}")
        git(self.source_snapshot, "push", "--quiet", str(self.remote), f"{SNAPSHOT_REF}:{SNAPSHOT_REF}")
        return self.tip


def exporter_producers() -> ExitStack:
    """The board and run producers and the audit gate, stood in for over the seeded export files."""

    def export(len_of: str):
        def produce(data_dir, **_kwargs):
            lines = (Path(data_dir) / len_of).read_text(encoding="utf-8")
            return DataExport(path=Path(data_dir), count=len(lines.splitlines()), source="test")

        return produce

    stack = ExitStack()
    for patcher in (
        mock.patch.object(CheckpointWriter, "_audit_owner", return_value=(None, _SettledAudit())),
        mock.patch("ummanu.checkpoint.export_board", side_effect=export("board/cards.ndjson")),
        mock.patch("ummanu.checkpoint.export_runs", side_effect=export("runs/runs.ndjson")),
    ):
        stack.enter_context(patcher)
    return stack


def exporter_remote(
    root: Path, *, cards: list[dict] | None = None, sprints: list[dict] | None = None, cut: bool = True
) -> ExporterRemote:
    """Build the source installation and a fresh bare remote; with `cut`, cut it once and push."""
    cards = [CARD] if cards is None else cards
    sprints = [SPRINT] if sprints is None else sprints
    remote = root / "remote.git"
    source = root / "source"
    source_data = root / "source-data"
    data_dir = root / "data"
    repository = root / "repository"
    subprocess.run(
        ["git", "init", "--quiet", "--bare", "--initial-branch", "main", str(remote)],
        check=True,
        capture_output=True,
    )
    repository.mkdir()
    files = {
        "instance.yaml": (
            "version: 1\n"
            "name: recovered\n"
            f"data_dir: {data_dir}\n"
            f"offsite:\n  instance_remote: {remote}\n"
            # The front is enabled (no component opts out), so recovery needs its sites (ummanu-53 P12).
            "host:\n  unit_prefix: ummanu-\n  web_front:\n    sites: [https://recovered.example]\n"
        ),
        "projects/ummanu.yaml": (
            f"id: ummanu\nrepo: {repository}\nenabled: false\nadapter: ummanu\ndefault_branch: main\n"
        ),
        "adapters/ummanu.yaml": (
            "setup:\n  commands: ['true']\nsmoke:\n  command: 'true'\n"
            "validation:\n  ci: local\n  command: 'true'\n"
            "artifact_policy:\n  write_project_files: false\n"
        ),
        "heads/heads.toml": canon(),
        "persona/rules.md": "Be brief.\n",
        "persona/hooks/check.sh": "#!/bin/sh\nexit 0\n",
        "skills/manifest.toml": "[skills]\n",
        "state/knowledge/decisions/one.md": "# One\n",
        "state/memory/facts/global/recovered.md": FACT,
        # Never exported, so never recovered: host-local, generated or outside the allowlist.
        "README.md": "instance\n",
        "runtime.env": "UMMANU_FIXTURE=1\n",
        "heads/heads.yaml": "generated: true\n",
        "state/board/stale.ndjson": "live-root board files never enter a cut\n",
    }
    for relative, text in files.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    (source / "persona" / "hooks" / "check.sh").chmod(0o755)
    (source / "runtime.env").chmod(0o600)
    board = source_data / "board"
    board.mkdir(parents=True)
    (board / "cards.ndjson").write_text("".join(json.dumps(c, sort_keys=True) + "\n" for c in cards), "utf-8")
    (board / "sprints.ndjson").write_text(
        "".join(json.dumps(s, sort_keys=True) + "\n" for s in sprints), "utf-8"
    )
    (board / "events.ndjson").write_text("", encoding="utf-8")
    (board / "audit.ndjson").write_text("", encoding="utf-8")
    (board / "export.json").write_text(
        json.dumps({"version": 1, "card_count": len(cards), "sprint_count": len(sprints)}), encoding="utf-8"
    )
    runs = source_data / "runs"
    runs.mkdir(parents=True)
    (runs / "runs.ndjson").write_text("", encoding="utf-8")
    (runs / "watermarks.json").write_text(json.dumps({"version": 1, "files": []}), encoding="utf-8")
    (runs / "claims.json").write_text(json.dumps({"version": 1, "claims": {}}), encoding="utf-8")
    (runs / "export.json").write_text(
        json.dumps({"version": 1, "run_record_count": 0, "watermark_count": 0, "claim_count": 0}),
        encoding="utf-8",
    )
    fixture = ExporterRemote(
        root=root,
        remote=remote,
        source=source,
        source_data=source_data,
        source_snapshot=root / "source-snapshot.git",
        target=root / "instance",
        data_dir=data_dir,
        cards=cards,
        sprints=sprints,
    )
    if cut:
        fixture.cut()
    return fixture


def recovery_args(fixture, **overrides) -> SimpleNamespace:
    """`ummanu recover` arguments for `fixture`'s remote into its target."""
    values = {
        "instance_dir": str(fixture.target),
        "instance_remote": str(fixture.remote),
        "installation_user": getpass.getuser(),
        "recover": True,
        "adopt": False,
        "dry_run": False,
        "runtime_env": None,
        "product_root": str(PRODUCT_ROOT),
        "bootstrap_credential_file": None,
        "bootstrap_credential_stdin": False,
        "recovery_phrase_file": None,
        "recovery_phrase_stdin": False,
        "host_fixture": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _head_registry_only(context, steps=installation.STEPS):
    """The recover materializer with only its head-registry step running (the host is not this test's)."""
    return upgrade.run_steps(
        context, steps=tuple(step for step in steps if step is upgrade.step_head_registry)
    )


def _rebuilt_index(data_dir: Path, instance_dir: Path, *, model: str, dim: int, **_kwargs) -> int:
    """The reindex without the embedding model: one index file for the facts the live root holds."""
    facts = sorted((instance_dir / "state" / "memory" / "facts").rglob("*.md"))
    index = data_dir / "memory" / "index.sqlite"
    index.unlink(missing_ok=True)
    write_memory_metadata(index, model=model, dim=dim)
    return len(facts)


def recover_snapshot(
    fixture: ExporterRemote,
    *,
    board: mock.Mock,
    failures: dict[str, BaseException] | None = None,
    **overrides,
):
    """One `ummanu recover` from `fixture`'s remote, with only the board store, the memory model,
    project checkouts and the host steps other than the head registry stood in for; `failures`
    makes the named installation callable raise instead."""
    patches = {
        "check_prerequisites": mock.Mock(),
        "check_product_runtime": mock.Mock(),
        "import_normalized_board": board,
        "rebuild_memory_index": mock.Mock(side_effect=_rebuilt_index),
        "provision_project_checkouts": mock.Mock(return_value=[]),
        "provision_codex_home": mock.Mock(return_value=0),
        "run_steps": mock.Mock(side_effect=_head_registry_only),
        "mark_reconcile_applied": mock.Mock(),
        "restore_findings": mock.Mock(return_value=[]),
    }
    for name, failure in (failures or {}).items():
        patches[name] = mock.Mock(side_effect=failure)
    with ExitStack() as stack:
        for name, replacement in patches.items():
            stack.enter_context(mock.patch.object(installation, name, replacement))
        return installation.install(recovery_args(fixture, **overrides))
