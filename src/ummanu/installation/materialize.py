"""Checkpoint, run state, host, registry and memory materialization; recovery progress."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ummanu._fsutil import (
    ndjson_line,
    ndjson_lines,
    publish_component_entries,
    publish_state_atomic,
    write_json,
    write_text_atomic,
)
from ummanu.board.backend import CARD, board_client
from ummanu.board.checkpoint_layout import CheckpointBoard, CheckpointLayoutError, open_checkpoint_board
from ummanu.board.close_checkpoint import CloseCheckpointError, read_checkpoint_close_facts
from ummanu.board.owner_checkpoint import OWNER_FILE, OwnerCheckpointError, read_checkpoint_owner_events
from ummanu.board.po_checkpoint import PO_FILE, PoCheckpointError, read_checkpoint_po
from ummanu.board.po_notes_checkpoint import NOTES_CHECKPOINT_FILE, PoNotesError, read_checkpoint_notes
from ummanu.board.po_queue_checkpoint import (
    QUEUE_CHECKPOINT_FILE,
    PoQueueCheckpointError,
    read_checkpoint_queue,
)
from ummanu.config import ConfigError, load_config, validate_instance
from ummanu.data import init_layout
from ummanu.dispatch.runtime_provenance import ProductionRuntime
from ummanu.host import foreign_units
from ummanu.host_apply import SystemdUnitInstaller, resolve_runtime_owner
from ummanu.installation.credentials import _runtime_environment
from ummanu.installation.models import InstallError, PipelineStateMaterialization
from ummanu.installation.support import _checked_data_target, _data_target_entries, _set_installation_owner
from ummanu.installation.upgrade.dependencies import _snapshot_install
from ummanu.installation.upgrade.host import step_host, step_pipeline_state
from ummanu.installation.upgrade.types import StepResult, UpgradeContext, UpgradeResult
from ummanu.installation.upgrade.workspaces import step_head_registry, workspaces_root
from ummanu.kernel.errors import TaskError
from ummanu.memory_journal import export_memory_snapshot
from ummanu.projects.availability import ProjectAvailability
from ummanu.runtime.paths import component_enabled
from ummanu.runtime.shared_state import resolve_pipeline_state_dir
from ummanu.tasks import TaskReader
from ummanu.upgrade import STEPS, run_steps
from ummanu.webfront.commands import configured_sites, missing_sites_message

CHECKPOINT_BOARD = (
    "cards.ndjson",
    "sprints.ndjson",
    "events.ndjson",
    "audit.ndjson",
    "export.json",
    "origin_returns.ndjson",
    PO_FILE,
    OWNER_FILE,
    NOTES_CHECKPOINT_FILE,
    QUEUE_CHECKPOINT_FILE,
)



CHECKPOINT_RUNS = ("runs.ndjson", "claims.json", "watermarks.json", "export.json")



WEB_FRONT_COMPONENT = "web-front"



CADDY_BINARY = Path("/usr/bin/caddy")



def _instance_host(instance_dir: Path) -> dict[str, Any] | None:
    """The `host` block of `instance.yaml`; None when the file cannot be read or has none."""
    try:
        instance = load_config(Path(instance_dir) / "instance.yaml")
    except ConfigError:
        return None
    host = instance.get("host") if isinstance(instance, dict) else None
    return host if isinstance(host, dict) else None



def web_front_wanted(instance_dir: Path) -> bool:
    """Whether this installation's desired units include `<prefix>web-front.service`.

    The same answer the host plan gives: units exist only under a unit prefix, for an enabled
    component, and not when the installation declares the name foreign. An instance that cannot be
    read wants nothing here; the install refuses it on its own terms.
    """
    host = _instance_host(instance_dir)
    if host is None:
        return False
    prefix = host.get("unit_prefix")
    if not isinstance(prefix, str) or not prefix:
        return False
    return component_enabled(host, WEB_FRONT_COMPONENT) and (
        f"{prefix}{WEB_FRONT_COMPONENT}.service" not in foreign_units(host)
    )



def caddy_installed() -> bool:
    """Whether the binary `ummanu-web-front.service` executes is on this host."""
    return os.access(CADDY_BINARY, os.X_OK)



def check_prerequisites(instance_dir: Path) -> None:
    # The prerequisite is the board this installation serves cards from: the PostgreSQL store.
    try:
        TaskReader(board_client(instance_dir, serves=(CARD,))).list()
    except TaskError as exc:
        raise InstallError(f"PostgreSQL prerequisite failed: {exc.message}") from None
    # Up front, not at materializer verify: without it the front crash-loops (203/EXEC).
    if web_front_wanted(instance_dir) and not caddy_installed():
        raise InstallError(
            f"caddy prerequisite failed: the web-front component is enabled and {CADDY_BINARY} is "
            "absent; install the distribution's caddy (`ummanu bootstrap` does) or disable the component"
        )
    # The rendered Caddyfile is data-dir state no checkpoint carries; the materializer renders it from
    # instance config (`upgrade.step_web_front_config`), so without sites there the front cannot start.
    if web_front_wanted(instance_dir) and not configured_sites(_instance_host(instance_dir)):
        raise InstallError(f"web-front prerequisite failed: {missing_sites_message(instance_dir)}")



def materialize_checkpoint(
    instance_dir: Path,
    data_dir: Path,
    *,
    dry_run: bool = False,
    own: tuple[Path, ...] = (),
) -> tuple[int, int]:
    """Validate the checkpoint and optionally publish it into the local layout.

    `instance_dir` is where `state/board` and `state/runs` are read: the checkout of a legacy
    checkpoint, or the extracted tree of an exporter snapshot. `own` is what this recovery's secret
    store step wrote into the data target before it (`_checked_data_target`).
    """
    bootstrap_evidence = _checked_data_target(data_dir, own=own)
    from ummanu.board.origin_checkpoint import OriginCheckpointError, read_checkpoint_outbox

    board_source = instance_dir / "state" / "board"
    runs_source = instance_dir / "state" / "runs"
    try:
        board = open_checkpoint_board(board_source)
    except CheckpointLayoutError as exc:
        raise InstallError(f"private checkpoint has an unreadable board layout: {exc}") from None
    for name in ("cards.ndjson", "export.json"):
        if not board.has(name):
            raise InstallError(f"private checkpoint is missing state/board/{name}")
    try:
        read_checkpoint_outbox(board_source)
        read_checkpoint_owner_events(board_source)
        read_checkpoint_close_facts(board_source)
        read_checkpoint_po(board_source)
        read_checkpoint_notes(board_source)
        read_checkpoint_queue(board_source)
    except (
        OriginCheckpointError, OwnerCheckpointError, PoCheckpointError, CloseCheckpointError, PoNotesError,
        PoQueueCheckpointError,
    ) as exc:
        raise InstallError(str(exc)) from None
    for name in CHECKPOINT_RUNS:
        if not (runs_source / name).is_file():
            raise InstallError(f"private checkpoint is missing state/runs/{name}")

    try:
        card_lines = [line for line in ndjson_lines(board.read_text("cards.ndjson")) if line.strip()]
        run_lines = [
            line
            for line in ndjson_lines((runs_source / "runs.ndjson").read_text(encoding="utf-8"))
            if line.strip()
        ]
        # A checkpoint written before sprints joined the board export carries no
        # sprints.ndjson; its export.json declares no sprint count either, and the
        # next tick writes both.
        sprint_lines = [
            line
            for line in ndjson_lines(board.read_text("sprints.ndjson") if board.has("sprints.ndjson") else "")
            if line.strip()
        ]
        cards = [json.loads(line) for line in card_lines]
        sprints = [json.loads(line) for line in sprint_lines]
        for line in run_lines:
            json.loads(line)
        board_export = json.loads(board.read_text("export.json"))
        run_export = json.loads((runs_source / "export.json").read_text(encoding="utf-8"))
        claims = json.loads((runs_source / "claims.json").read_text(encoding="utf-8"))
        watermarks = json.loads((runs_source / "watermarks.json").read_text(encoding="utf-8"))
    except CheckpointLayoutError as exc:
        raise InstallError(f"private checkpoint has an unreadable board layout: {exc}") from None
    except (OSError, UnicodeError, ValueError):
        raise InstallError("private checkpoint contains invalid normalized state") from None
    if not isinstance(board_export, dict) or not isinstance(run_export, dict):
        raise InstallError("private checkpoint contains invalid export metadata")
    if any(not isinstance(card, dict) for card in cards) or board_export.get("card_count") != len(cards):
        raise InstallError("private checkpoint board count does not match cards.ndjson")
    declared_sprints = board_export.get("sprint_count")
    if any(not isinstance(sprint, dict) for sprint in sprints) or (
        declared_sprints is not None and declared_sprints != len(sprints)
    ):
        raise InstallError("private checkpoint sprint count does not match sprints.ndjson")
    run_count = len(run_lines)
    declared_runs = run_export.get("run_record_count")
    if not isinstance(declared_runs, int) or declared_runs != run_count:
        raise InstallError("private checkpoint run count does not match runs.ndjson")
    claim_entries = claims.get("claims") if isinstance(claims, dict) else None
    watermark_entries = watermarks.get("files") if isinstance(watermarks, dict) else None
    if not isinstance(claim_entries, dict) or run_export.get("claim_count") != len(claim_entries):
        raise InstallError("private checkpoint claim count does not match claims.json")
    if not isinstance(watermark_entries, list) or run_export.get("watermark_count") != len(watermark_entries):
        raise InstallError("private checkpoint watermark count does not match watermarks.json")

    if dry_run:
        return len(cards), run_count

    if not _data_target_entries(data_dir, own=own) or bootstrap_evidence:
        init_layout(data_dir)

    board_target = data_dir / "board"
    runs_target = data_dir / "runs"
    board_target.mkdir(parents=True, exist_ok=True)
    runs_target.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix=".checkpoint-board-", dir=board_target) as staging_raw:
            staging = Path(staging_raw)
            write_json(staging / "cards.json", {"version": 1, "cards": cards})
            write_json(staging / "sprints.json", {"version": 1, "sprints": sprints})
            for name in CHECKPOINT_BOARD:
                if board.has(name):
                    write_text_atomic(staging / name, board.read_text(name))
            publish_component_entries(
                staging,
                board_target,
                [
                    "cards.json",
                    "sprints.json",
                    *[n for n in CHECKPOINT_BOARD if (staging / n).is_file()],
                ],
                "checkpoint board materialization",
            )
        with tempfile.TemporaryDirectory(prefix=".checkpoint-runs-", dir=runs_target) as staging_raw:
            staging = Path(staging_raw)
            for name in CHECKPOINT_RUNS:
                write_text_atomic(staging / name, (runs_source / name).read_text(encoding="utf-8"))
            publish_component_entries(
                staging,
                runs_target,
                list(CHECKPOINT_RUNS),
                "checkpoint runs materialization",
            )
    except (OSError, RuntimeError, CheckpointLayoutError) as exc:
        raise InstallError(f"could not materialize checkpoint: {exc}") from None
    return len(cards), run_count



def _restored_run_journals(runs_source: Path) -> dict[Path, list[tuple[int, str]]]:
    """Rebuild the JSONL files whose records the checkpoint normalizes."""
    grouped: dict[Path, list[tuple[int, object]]] = {}
    try:
        lines = ndjson_lines((runs_source / "runs.ndjson").read_text(encoding="utf-8"))
        for raw in lines:
            if not raw.strip():
                continue
            entry = json.loads(raw)
            source = entry.get("source") if isinstance(entry, dict) else None
            line = entry.get("line") if isinstance(entry, dict) else None
            if not isinstance(source, str) or not source or not isinstance(line, int) or line < 1:
                raise ValueError
            relative = Path(source)
            if relative.is_absolute() or ".." in relative.parts or relative.suffix != ".jsonl":
                raise ValueError
            grouped.setdefault(relative, []).append((line, entry.get("record")))
    except (OSError, UnicodeError, ValueError, TypeError):
        raise InstallError("private checkpoint contains invalid run journal records") from None

    journals: dict[Path, list[tuple[int, str]]] = {}
    for relative, records in grouped.items():
        ordered = sorted(records, key=lambda item: item[0])
        numbers = [number for number, _ in ordered]
        if len(numbers) != len(set(numbers)):
            raise InstallError(
                f"private checkpoint has duplicate run journal lines for {relative.as_posix()}"
            )
        try:
            journals[relative] = [(number, ndjson_line(record)) for number, record in ordered]
        except (TypeError, ValueError):
            raise InstallError(
                f"private checkpoint contains an unserializable run journal record for {relative.as_posix()}"
            ) from None
    return journals



def _render_restored_journal(records: list[tuple[int, str]]) -> str:
    """Keep original physical line numbers; blank source lines are meaningful offsets."""
    rendered: list[str] = []
    previous = 0
    for number, record in records:
        rendered.append("\n" * (number - previous - 1))
        rendered.append(record)
        previous = number
    return "".join(rendered)



def _live_run_journals(state_dir: Path) -> dict[Path, list[str]]:
    """Parse the current journal into the same canonical record spelling as a checkpoint."""
    journals: dict[Path, list[str]] = {}
    try:
        if not state_dir.is_dir():
            return journals
        for path in state_dir.rglob("*.jsonl"):
            if not path.is_file():
                continue
            relative = path.relative_to(state_dir)
            journals[relative] = [
                ndjson_line(json.loads(line))
                for line in ndjson_lines(path.read_text(encoding="utf-8"))
                if line.strip()
            ]
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise InstallError(f"could not read live pipeline state at {state_dir}: {exc}") from None
    return journals



def materialize_pipeline_state(
    instance_dir: Path,
    state_dir: Path,
    *,
    dry_run: bool = False,
) -> PipelineStateMaterialization:
    """Put canonical run journals back where the dispatcher checkpoint reads them.

    Recovery restores ``state/runs`` into the data plane, but the dispatcher exports from its
    pipeline worktree. This bridge is intentionally narrow: it restores only the JSONL content the
    canonical checkpoint carries, and refuses to overwrite a different non-empty live journal.
    """
    try:
        runs_source = Path(instance_dir).expanduser().resolve() / "state" / "runs"
        state_dir = Path(state_dir).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise InstallError(f"could not resolve pipeline state paths: {exc}") from None
    journals = _restored_run_journals(runs_source)
    existing = _live_run_journals(state_dir)
    for relative, canonical in journals.items():
        live = existing.get(relative, [])
        canonical_records = [record for _, record in canonical]
        if live and live[: len(canonical_records)] != canonical_records:
            raise InstallError(
                f"live pipeline state at {state_dir} does not extend the checkpoint; refusing to overwrite it"
            )
    records = sum(len(journal) for journal in journals.values())
    if dry_run:
        # What a real run would change: a missing directory or an absent/empty journal.
        pending = not state_dir.exists() or any(not existing.get(relative) for relative in journals)
        return PipelineStateMaterialization(records=records, changed=pending)
    try:
        created = not state_dir.exists()
        state_dir.mkdir(parents=True, exist_ok=True)
        # A valid live extension is newer than the checkpoint and must survive a
        # retry. Only absent/empty journals receive the reconstructed prefix.
        writes = [
            (state_dir / relative, _render_restored_journal(records))
            for relative, records in journals.items()
            if not existing.get(relative)
        ]
        if writes:
            publish_state_atomic(writes)
    except (OSError, RuntimeError) as exc:
        raise InstallError(f"could not materialize live pipeline state: {exc}") from None
    return PipelineStateMaterialization(records=records, changed=created or bool(writes))



def pipeline_state_path(runtime_home: Path) -> Path:
    """The dispatcher-owned state source below the installation user's worktree."""
    return resolve_pipeline_state_dir(workspaces_root(runtime_home))



class MaterializeError(InstallError):
    """The materializer stopped at a failed step; `steps` runs up to and including that step."""

    def __init__(self, steps: list[StepResult]) -> None:
        failed = steps[-1]
        super().__init__(f"materializer {failed.name} failed: {failed.detail}")
        self.steps = tuple(steps)


def materialize_host(
    instance: Path,
    product_root: Path,
    host_fixture: Path | None = None,
    installation_user: str | None = None,
    before_host: Callable[[UpgradeContext], None] | None = None,
    project_availability: ProjectAvailability | None = None,
):
    report = validate_instance(instance)
    if not report.ok:
        raise InstallError("invalid instance config: " + "; ".join(map(str, report.errors)))
    check_product_runtime(product_root)
    try:
        installation_user, runtime_home = resolve_runtime_owner(instance, installation_user)
    except ValueError as exc:
        raise InstallError(str(exc)) from None
    context = UpgradeContext(
        instance_path=instance,
        product_root=product_root,
        base_branch="main",
        dry_run=False,
        units=SystemdUnitInstaller(runtime_user=installation_user),
        host_fixture=host_fixture,
        pull=False,
        report=report,
        runtime_user=installation_user,
        runtime_home=runtime_home,
        project_availability=project_availability or ProjectAvailability(),
    )
    # The steps resolve their own paths against `runtime_home`; HOME is exported for the
    # subprocesses they start, which read the environment and not this context.
    with _runtime_environment({"HOME": str(runtime_home)}):
        if before_host is None:
            result = run_steps(context)
        else:
            # Recovery restores the pipeline state itself, inside its ownership barrier.
            steps = tuple(step for step in STEPS if step is not step_pipeline_state)
            host_index = steps.index(step_host)
            prepared = run_steps(context, steps=steps[:host_index])
            if not prepared.ok:
                raise MaterializeError(prepared.steps)
            before_host(context)
            finished = run_steps(context, steps=steps[host_index:])
            result = UpgradeResult(steps=[*prepared.steps, *finished.steps])
    if not result.ok:
        raise MaterializeError(result.steps)
    return result



def check_product_runtime(product_root: Path) -> None:
    """Refuse an unusable checkout runtime before the materializer changes the host."""
    product_root = product_root.expanduser().resolve()
    runtime = ProductionRuntime.installed(product_root)
    executables = ("python3", "ummanu", "ummanu-memory-mcp", "ummanu-memory-po-bridge")
    missing = [
        name
        for name in executables
        if not (product_root / ".venv" / "bin" / name).is_file()
        or not os.access(product_root / ".venv" / "bin" / name, os.X_OK)
    ]
    remedy = (
        f"prepare {product_root}/.venv with python3 -m venv and install this checkout editable "
        "with its .venv/bin/python -m pip install -e '.[memory,dev]'; "
        "see docs/RECOVERY.md, Fresh install and recovery"
    )
    if missing:
        raise InstallError(f"product runtime unavailable: missing .venv/bin/{missing[0]}; {remedy}")
    provenance = runtime.probe()
    if not provenance.valid:
        raise InstallError(f"{provenance.refusal('install/recover')}; {remedy}")
    try:
        snapshot = _snapshot_install(Path(runtime.interpreter))
    except (AttributeError, TypeError):
        # A partial pip write or malformed direct_url metadata cannot establish an editable install.
        snapshot = True
    if snapshot:
        raise InstallError(f"product runtime is not an editable install; {remedy}")



def materialize_head_registry(
    instance: Path, product_root: Path, report: Any, installation_user: str | None
) -> tuple[str, str]:
    """Generate `<data>/heads/` before the board import, with upgrade's own head-registry step.

    The import validates an open sprint's observer head against the installed pair, and on a clean
    host nothing has written one yet: the pair is generated state, never recovery canon, and the
    data directory it lives in (ummanu-26) is recovered empty. So recovery writes it here, from the
    canon the recovered live root carries, as `ummanu upgrade` would; the host materializer later
    runs the same step and finds it current. It is idempotent, so a retry passes through it again.
    """
    context = UpgradeContext(
        instance_path=instance,
        product_root=product_root,
        base_branch="main",
        dry_run=False,
        units=SystemdUnitInstaller(runtime_user=installation_user),
        pull=False,
        report=report,
        runtime_user=installation_user,
    )
    step = step_head_registry(context)
    if step.status == "failed":
        raise InstallError(f"head registry: {step.detail}")
    return step.status, step.detail



def _publish_recovered_memory_export(data_dir: Path, instance: Path, installation_user: str | None) -> None:
    """Publish `<data>/memory/export.ndjson` from the recovered facts and hand it to the user.

    The index rebuild writes only the index, and the pack step skips a ledger the restore already
    matches, so without this `memory verify` finds no export on a recovered host.
    """
    try:
        export_memory_snapshot(data_dir, instance)
    except (OSError, RuntimeError) as exc:
        raise InstallError(f"could not publish the memory export: {exc}") from None
    _set_installation_owner(data_dir / "memory", installation_user)



def _unchanged_memory_step(data_dir: Path, instance: Path, installation_user: str | None) -> tuple[str, str]:
    """A retry past the rebuild: the index stays, and a missing export is still published."""
    if (data_dir / "memory" / "export.ndjson").is_file():
        return "unchanged", "checkpoint index already rebuilt"
    _publish_recovered_memory_export(data_dir, instance, installation_user)
    return "changed", "checkpoint index already rebuilt; published the memory export"



def _recovery_identity_entry(digest: Any, *, path: bytes, entry_type: bytes, content: bytes) -> None:
    """Hash one typed recovery input without allowing adjacent components to alias."""
    for component in (path, entry_type, content):
        digest.update(len(component).to_bytes(8, "big"))
        digest.update(component)



def _recovery_identity(
    instance_dir: Path, bindings: list[dict[str, object]], *, checkpoint_root: Path | None = None
) -> str:
    """The identity of one recovery's inputs: board, runs, memory facts and project bindings.

    Board and runs are read below `checkpoint_root` (the extracted tree of an exporter snapshot),
    by default the instance checkout; the facts always come from the live root.
    """
    digest = hashlib.sha256()
    checkpoint_root = instance_dir if checkpoint_root is None else checkpoint_root
    # The board is hashed by its logical files, read through the checkpoint reader, so the same
    # board has the same identity whether a flat or a split checkpoint carries it.
    try:
        board: CheckpointBoard | None = open_checkpoint_board(checkpoint_root / "state" / "board")
        board_error = b""
    except CheckpointLayoutError as exc:
        board, board_error = None, str(exc).encode()
    checkpoint_inputs = [
        *(f"state/board/{name}" for name in CHECKPOINT_BOARD),
        *(f"state/runs/{name}" for name in CHECKPOINT_RUNS),
    ]
    for relative in checkpoint_inputs:
        path = checkpoint_root / relative
        name = relative.removeprefix("state/board/")
        if name in CHECKPOINT_BOARD:
            if board is None:
                entry_type, content = b"invalid", board_error
            elif not board.has(name):
                entry_type, content = b"absent", b""
            else:
                try:
                    entry_type, content = b"file", board.read_bytes(name)
                except (OSError, CheckpointLayoutError) as exc:
                    entry_type, content = b"invalid", str(exc).encode()
        elif path.is_file():
            entry_type, content = b"file", path.read_bytes()
        else:
            entry_type, content = b"absent", b""
        _recovery_identity_entry(
            digest,
            path=relative.encode(),
            entry_type=entry_type,
            content=content,
        )
    facts = instance_dir / "state" / "memory" / "facts"
    _recovery_identity_entry(
        digest,
        path=b"state/memory/facts",
        entry_type=b"dir" if facts.is_dir() else b"absent",
        content=b"",
    )
    try:
        entries = sorted(facts.rglob("*"), key=lambda path: path.relative_to(facts).as_posix())
        for path in entries:
            relative = path.relative_to(facts).as_posix().encode()
            mode = path.lstat().st_mode
            if stat.S_ISREG(mode):
                entry_type, content = b"file", path.read_bytes()
            elif stat.S_ISDIR(mode):
                entry_type, content = b"dir", b""
            elif stat.S_ISLNK(mode):
                entry_type, content = b"symlink", os.fsencode(os.readlink(path))
            else:
                entry_type, content = b"other", b""
            _recovery_identity_entry(
                digest,
                path=relative,
                entry_type=entry_type,
                content=content,
            )
    except (OSError, RuntimeError) as exc:
        raise InstallError("could not identify memory recovery canon") from exc
    safe_bindings = [
        {key: binding.get(key) for key in ("id", "repo", "remote", "default_branch")} for binding in bindings
    ]
    _recovery_identity_entry(
        digest,
        path=b"project-bindings",
        entry_type=b"json",
        content=json.dumps(safe_bindings, sort_keys=True, separators=(",", ":")).encode(),
    )
    return digest.hexdigest()



def _read_recovery_progress(path: Path, identity: str) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 1, "identity": identity}
    if not isinstance(payload, dict) or payload.get("identity") != identity:
        return {"version": 1, "identity": identity}
    return payload



def _write_recovery_progress(path: Path, identity: str, **changes: object) -> None:
    payload = _read_recovery_progress(path, identity)
    payload.update(changes)
    payload.update(version=1, identity=identity)
    try:
        write_text_atomic(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    except RuntimeError as exc:
        raise InstallError(f"could not record recovery progress: {exc}") from None
