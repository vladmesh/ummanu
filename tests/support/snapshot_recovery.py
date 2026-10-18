"""`ummanu recover` from an exporter snapshot with only host provisioning stood in for.

`recover_patches` is the one list of stand-ins the PostgreSQL snapshot recovery tests use; run as a
module, this file performs one such recovery as real root and prints its result and the ownership
facts of the data plane as JSON (`tests/test_snapshot_recovery_postgres.py`).
"""

from __future__ import annotations

import hashlib
import json
import stat
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from ummanu import upgrade
from ummanu.installation import materialize as installation_materialize
from ummanu.installation.upgrade import workspaces as upgrade_workspaces
from ummanu.restore import DEFAULT_MEMORY_DIM

PO_ARTIFACTS = (
    ".po-recovery-admission.json", ".po-restore", ".po-restore/plan.json", ".po-restore/contexts.json",
    "po", "po/NOTES.md",
)


class Embedder:
    """A deterministic stand-in for the fastembed model: the index is real, the vectors are not."""

    def embed_many(self, texts: list[str]):
        import numpy as np

        vectors = []
        for text in texts:
            raw = np.arange(1, DEFAULT_MEMORY_DIM + 1, dtype=np.float32) * (1 + len(text) % 7)
            vectors.append(raw / np.linalg.norm(raw))
        return vectors

    def __call__(self, text: str):
        return self.embed_many([text])[0]


def head_registry_only(context, steps=installation_materialize.STEPS):
    return upgrade.run_steps(
        context, steps=tuple(step for step in steps if step is upgrade_workspaces.step_head_registry)
    )


def recover_patches(*, owners: bool) -> list[Any]:
    """Project checkouts, CODEX_HOME, host steps but the head registry, Caddy and the embedding model.

    `owners` also stands in for every ownership change, for a recovery that runs unprivileged.
    """
    patches = [
        mock.patch("ummanu.installation.commands.provision_project_checkouts", return_value=[]),
        mock.patch("ummanu.installation.commands.provision_codex_home", return_value=0),
        mock.patch("ummanu.installation.materialize.run_steps", side_effect=head_registry_only),
        mock.patch("ummanu.installation.materialize.check_product_runtime"),
        mock.patch("ummanu.installation.materialize.caddy_installed", return_value=True),
        mock.patch("ummanu.memory_service.build_document_embedder", return_value=Embedder()),
    ]
    if owners:
        patches += [
            mock.patch("ummanu.installation.commands._ensure_installation_user"),
            mock.patch("ummanu.installation.clone._set_installation_owner"),
            mock.patch("ummanu.installation.commands._set_installation_owner"),
            mock.patch("ummanu.installation.materialize._set_installation_owner"),
            mock.patch("ummanu.installation.support._set_installation_owner"),
        ]
    return patches


def ownership_facts(data_dir: Path, instance_dir: Path, uid: int) -> dict[str, Any]:
    """Owner and mode of the PO recovery artifacts, and every path the runtime user does not own."""
    def entry(path: Path) -> dict[str, int] | None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        return {"uid": info.st_uid, "mode": stat.S_IMODE(info.st_mode)}

    foreign = []
    for root in (data_dir, instance_dir):
        for path in (root, *root.rglob("*")):
            info = path.lstat()
            # The ownership barrier leaves links and multiply linked files alone.
            if stat.S_ISLNK(info.st_mode) or (stat.S_ISREG(info.st_mode) and info.st_nlink > 1):
                continue
            if info.st_uid != uid:
                foreign.append(str(path))
    plan = data_dir / ".po-restore" / "plan.json"
    admission = data_dir / ".po-recovery-admission.json"
    notes = data_dir / "po" / "NOTES.md"
    return {
        "data_dir": entry(data_dir),
        "artifacts": {name: entry(data_dir / name) for name in PO_ARTIFACTS},
        "plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest() if plan.exists() else None,
        "notes_sha256": hashlib.sha256(notes.read_bytes()).hexdigest() if notes.exists() else None,
        "admission_phase": json.loads(admission.read_text())["phase"] if admission.exists() else None,
        "foreign": sorted(foreign)[:20],
    }


def main() -> int:
    """Read the recovery spec on stdin, run `install` as this (root) process, print JSON facts."""
    # Load what the runtime-user child imports lazily, so it never reads the product tree itself.
    import psycopg  # noqa: F401
    from psycopg.types.json import Jsonb  # noqa: F401

    from ummanu.board.po_checkpoint import PoCheckpointError
    from ummanu.board.po_restore import PoRestoreStore
    from ummanu.board.sql_cards import SqlCardClient  # noqa: F401
    from ummanu.installation import commands
    from ummanu.po.recovery_admission import PoRecoveryAdmission  # noqa: F401
    from ummanu.po.store import PoStore  # noqa: F401
    from ummanu.restore import restore_state

    spec = json.load(sys.stdin)
    data_dir, instance_dir, uid = Path(spec["data_dir"]), Path(spec["args"]["instance_dir"]), spec["uid"]
    barrier = commands._establish_recovery_ownership_barrier
    before_barrier: list[dict[str, Any]] = []

    def observed_barrier(*args: Any, **kwargs: Any) -> None:
        before_barrier.append(ownership_facts(data_dir, instance_dir, uid))
        barrier(*args, **kwargs)

    patches = [*recover_patches(owners=False),
               mock.patch.object(commands, "_establish_recovery_ownership_barrier", observed_barrier)]
    if spec["fail_import"]:
        restore = PoRestoreStore.restore

        def failing_import(store: Any, *args: Any) -> None:
            restore(store, *args)
            raise PoCheckpointError("injected root recovery import failure")

        patches.append(mock.patch.object(PoRestoreStore, "restore", failing_import))
    with ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        result = commands.install(SimpleNamespace(**spec["args"]))
    print(json.dumps({
        "status": result.status,
        "steps": [[step.name, step.status, step.detail] for step in result.steps],
        "before_final_barrier": before_barrier[-1],
        "after": ownership_facts(data_dir, instance_dir, uid),
        "po_sql_restore": restore_state(data_dir).get("po_sql_restore"),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
