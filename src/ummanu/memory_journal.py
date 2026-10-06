from __future__ import annotations

import json
import os
import socket
import sqlite3
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ummanu import state_repo
from ummanu._fsutil import (
    cleanup_staging_dir as _cleanup_staging_dir,
    copy_tree as _copy_tree,
    ensure_dir as _ensure_dir,
    publish_component_entries as _publish_component_entries,
    regular_files_under as _regular_files_under,
    write_json as _write_json,
    write_ndjson as _write_ndjson,
)
from ummanu.memory.canon import (
    content_revision,
    fact_content_hash,
    fact_files,
    parse_fact_text,
    parse_frontmatter,
    pending_undo,
    recover_canon_undo,
    text_digest,
)
from ummanu.memory_errors import MemoryLockError, MemoryProtocolError, MemoryValidationError

MEMORY_LOCK_NAME = ".write.lock"


@dataclass(frozen=True)
class MemoryExportSnapshot:
    path: Path
    count: int
    source: str


@dataclass(frozen=True)
class MemoryVerify:
    """What `memory verify` found.

    `journal_commit` holds the content revision of the canon fact set (`memory.canon`), not a Git
    commit; `dirty` says an unfinished write left its undo area behind.
    """

    facts_dir: Path
    ok: bool
    findings: tuple[str, ...]
    journal_commit: str | None
    fact_count: int
    export_count: int | None
    index_count: int | None
    dirty: bool


def init_memory_journal(instance_dir: Path) -> tuple[Path, bool]:
    """Resolve `state/memory/facts` in the live root.

    Contract: docs/RECOVERY.md, "Layout" and "Writers". Facts live flat in the live root, which may
    or may not be a Git work tree; there is no nested journal to initialize and no repository to
    require.
    """
    instance_dir = Path(instance_dir).expanduser().resolve()
    if not instance_dir.is_dir():
        raise RuntimeError(f"instance directory not found: {instance_dir}")
    facts_dir = state_repo.memory_facts_dir(instance_dir)
    created = not facts_dir.is_dir()
    try:
        facts_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"cannot prepare memory facts dir: {exc}") from None
    return facts_dir, created


def reject_legacy_memory_journal(memory_dir: Path) -> None:
    """Refuse to write or publish while a pre-flatten journal sits in the data dir.

    Facts live in `state/memory/facts` of the instance repo and nowhere else, so
    a `<data-dir>/memory/facts` left by an older release holds facts this
    release cannot read. Carrying them over on the fly is the compatibility
    promise this product dropped, so the boundary refuses instead: the operator
    moves them, and until then nothing writes a canon that silently excludes
    them.
    """
    legacy = memory_dir / "facts"
    if not _legacy_journal_facts(legacy):
        return
    raise MemoryProtocolError(
        f"legacy memory journal is still present: {legacy}. "
        "This release reads memory only from state/memory/facts in the instance repo; "
        "move these facts there and remove the directory before writing memory again."
    )


def _legacy_journal_facts(legacy: Path) -> bool:
    if (legacy / ".git").exists():
        return True
    try:
        return any(legacy.rglob("*.md"))
    except OSError:
        return False


def export_memory_snapshot(data_dir: Path, instance_dir: Path) -> MemoryExportSnapshot:
    """Refresh the derived export from the live facts of the live root.

    The live root is the only source, so an export cannot carry facts that are not this
    installation's canon. An unfinished write is restored first, under the same locks a writer
    holds, so an export never publishes half of one.
    """
    data_dir = data_dir.expanduser().resolve()
    instance_dir = Path(instance_dir).expanduser().resolve()
    memory_dir = data_dir / "memory"
    _ensure_dir(memory_dir, "memory data dir")
    facts_dir = state_repo.memory_facts_dir(instance_dir)
    with _memory_journal_lock(memory_dir):
        reject_legacy_memory_journal(memory_dir)
        if pending_undo(memory_dir) is not None:
            with state_repo.state_repo_lock(instance_dir):
                recover_canon_undo(memory_dir)
        try:
            staging = Path(tempfile.mkdtemp(prefix=".memory-export-", suffix=".tmp", dir=memory_dir))
        except OSError as exc:
            raise RuntimeError(f"could not create memory export staging: {exc}") from None
        try:
            _copy_tree(facts_dir, staging)
            facts = _read_memory_facts(staging)
            _publish_memory_export(
                memory_dir,
                facts=facts,
                source_memory=facts_dir,
                source_root=facts_dir,
                changed=False,
                record_import=False,
            )
        finally:
            _cleanup_staging_dir(staging)

    return MemoryExportSnapshot(
        path=memory_dir / "export.ndjson",
        count=len(facts),
        source=str(facts_dir),
    )


def verify_memory_journal(data_dir: Path, instance_dir: Path) -> MemoryVerify:
    """Compare the canon, `export.ndjson` and `index.sqlite` by fact id and content, not by count.

    Every divergence is a named finding: ids missing from or extra in the export or the index, a
    fact whose export text or index row differs from the canon, an index that cannot be checked by
    id, and an undo area an unfinished write left behind.
    """
    data_dir = data_dir.expanduser().resolve()
    instance_dir = Path(instance_dir).expanduser().resolve()
    memory_dir = data_dir / "memory"
    findings: list[str] = []
    revision: str | None = None
    fact_count = 0
    export_count: int | None = None
    index_count: int | None = None
    dirty = False
    facts_dir = state_repo.memory_facts_dir(instance_dir)
    canon_texts: dict[str, str] | None = None

    with _memory_journal_lock(memory_dir):
        legacy = memory_dir / "facts"
        if (legacy / ".git").is_dir():
            findings.append(f"nested memory journal is still present: {legacy}")
        try:
            pending = pending_undo(memory_dir)
        except RuntimeError as exc:
            pending = ()
            findings.append(str(exc))
        if pending is not None:
            dirty = True
            named = ", ".join(pending) if pending else "no path recorded"
            findings.append(
                f"memory undo state is left behind: {memory_dir / '.undo'} ({named}); "
                "the next memory write restores it"
            )

        if not instance_dir.is_dir():
            findings.append(f"instance directory not found: {instance_dir}")
        elif not facts_dir.is_dir():
            findings.append(f"memory canon missing: {facts_dir}")
        else:
            canon_texts = _read_canon_texts(facts_dir)
            digests = {fact_id: text_digest(text) for fact_id, text in canon_texts.items()}
            revision = content_revision(digests)
            fact_count = len(canon_texts)

        export_path = memory_dir / "export.ndjson"
        if not export_path.is_file():
            findings.append(f"memory export missing: {export_path}")
        else:
            export_rows = _read_export_fact_texts(export_path)
            export_count = len(export_rows)
            findings.extend(_duplicate_findings("export", [fact_id for fact_id, _text in export_rows]))
            if canon_texts is not None:
                findings.extend(
                    _set_findings("export", canon_texts, {fact_id for fact_id, _text in export_rows})
                )
                # Every row is compared, so a stale duplicate beside a current row stays red.
                changed = sorted(
                    {
                        fact_id
                        for fact_id, text in export_rows
                        if fact_id in canon_texts and text_digest(text) != text_digest(canon_texts[fact_id])
                    }
                )
                if changed:
                    findings.append(f"memory export content differs from the canon: {', '.join(changed)}")

        index_path = memory_dir / "index.sqlite"
        if not index_path.is_file():
            findings.append(f"memory index missing: {index_path}")
        else:
            index_rows, index_count, index_finding = _read_index_rows(index_path)
            if index_finding:
                findings.append(index_finding)
            if index_rows is not None:
                findings.extend(_duplicate_findings("index", [fact_id for fact_id, _row in index_rows]))
            if index_rows is not None and canon_texts is not None:
                findings.extend(
                    _set_findings("index", canon_texts, {fact_id for fact_id, _row in index_rows})
                )
                findings.extend(_index_content_findings(canon_texts, index_rows))

    return MemoryVerify(
        facts_dir=facts_dir,
        ok=not findings,
        findings=tuple(findings),
        journal_commit=revision,
        fact_count=fact_count,
        export_count=export_count,
        index_count=index_count,
        dirty=dirty,
    )


def _read_canon_texts(facts_dir: Path) -> dict[str, str]:
    texts = {}
    for fact_id, path in fact_files(facts_dir):
        try:
            texts[fact_id] = path.read_bytes().decode("utf-8")
        except OSError as exc:
            raise RuntimeError(f"could not read memory fact {fact_id}: {exc}") from None
        except UnicodeError as exc:
            raise RuntimeError(f"could not decode memory fact {fact_id}: {exc}") from None
    return texts


def _duplicate_findings(label: str, fact_ids: list[str]) -> list[str]:
    """One finding per fact id that appears in more than one derived row."""
    counts: dict[str, int] = {}
    for fact_id in fact_ids:
        counts[fact_id] = counts.get(fact_id, 0) + 1
    return [
        f"memory {label} has {count} rows for one fact: {fact_id}"
        for fact_id, count in sorted(counts.items())
        if count > 1
    ]


def _set_findings(label: str, canon: dict[str, Any], other: set[str]) -> list[str]:
    findings = []
    missing = sorted(set(canon) - set(other))
    extra = sorted(set(other) - set(canon))
    if missing:
        findings.append(f"memory {label} is missing canon facts: {', '.join(missing)}")
    if extra:
        findings.append(f"memory {label} has facts the canon does not: {', '.join(extra)}")
    return findings


def _index_content_findings(
    canon_texts: dict[str, str], index_rows: list[tuple[str, dict[str, Any]]]
) -> list[str]:
    """An index row matches its fact when both its stored hash and its stored fields hash to the canon."""
    changed: set[str] = set()
    unparsed: set[str] = set()
    for fact_id, row in index_rows:
        text = canon_texts.get(fact_id)
        if text is None:
            continue
        try:
            expected = fact_content_hash(parse_fact_text(text, f"{fact_id}.md", fact_id=fact_id))
        except (ValueError, MemoryValidationError):
            unparsed.add(fact_id)
            continue
        if row["content_hash"] != expected or fact_content_hash(row) != expected:
            changed.add(fact_id)
    findings = []
    if changed:
        findings.append(f"memory index content differs from the canon: {', '.join(sorted(changed))}")
    if unparsed:
        findings.append(f"memory canon facts the index cannot parse: {', '.join(sorted(unparsed))}")
    return findings


def _read_memory_facts(facts_dir: Path) -> list[dict[str, Any]]:
    facts = []
    for path, file_stat in _regular_files_under(facts_dir, context="memory snapshot"):
        if path.suffix != ".md":
            continue
        relative = path.relative_to(facts_dir).as_posix()
        try:
            # Bytes decoded as they are, so a fact's export text hashes to its file's bytes.
            text = path.read_bytes().decode("utf-8")
        except OSError as exc:
            raise RuntimeError(f"could not read memory fact {relative}: {exc}") from None
        except UnicodeError as exc:
            raise RuntimeError(f"could not decode memory fact {relative}: {exc}") from None
        facts.append(
            {
                "id": relative.removesuffix(".md"),
                "path": relative,
                "bytes": file_stat.st_size,
                "mtime": int(file_stat.st_mtime),
                "metadata": _memory_fact_metadata(text),
                "text": text,
            }
        )
    return facts


def _read_export_fact_texts(path: Path) -> list[tuple[str, str]]:
    """Every export row as `(id, text)`, in file order and with its multiplicity."""
    rows: list[tuple[str, str]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError(f"could not read memory export {path}: {exc}") from None
    except UnicodeError as exc:
        raise RuntimeError(f"could not decode memory export {path}: {exc}") from None
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid memory export JSON at line {number}: {exc}") from None
        if not isinstance(payload, dict):
            raise RuntimeError(  # noqa: TRY004  # Invalid persisted data uses the export error boundary.
                f"invalid memory export row at line {number}: not an object"
            )
        fact_id = payload.get("id")
        if not isinstance(fact_id, str) or not fact_id:
            raise RuntimeError(f"invalid memory export row at line {number}: missing id")
        text = payload.get("text")
        rows.append((fact_id, text if isinstance(text, str) else ""))
    return rows


_INDEX_FIELDS = ("fact_id", "content_hash", "text", "scope", "tags", "source", "created_at")


def _read_index_rows(path: Path) -> tuple[list[tuple[str, dict[str, Any]]] | None, int | None, str | None]:
    """`([(fact_id, row)], raw row count, finding)`; rows keep their multiplicity and are None when the
    index cannot be checked by id."""
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            count = int(conn.execute("select count(*) from memories").fetchone()[0])
            columns = {row[1] for row in conn.execute("pragma table_info(memories)")}
            if not set(_INDEX_FIELDS) <= columns:
                return None, count, f"memory index has no fact ids or content hashes to check: {path}"
            rows = conn.execute(f"select {', '.join(_INDEX_FIELDS)} from memories").fetchall()
    except sqlite3.Error as exc:
        raise RuntimeError(f"could not read memory index {path}: {exc}") from None
    return [(row[0], dict(zip(_INDEX_FIELDS, row))) for row in rows], count, None


def _memory_fact_metadata(text: str) -> dict[str, Any]:
    try:
        loaded, _ = parse_frontmatter(text)
    except MemoryValidationError:
        # A backup must retain a malformed fact's original text even when it cannot be indexed.
        return {}
    return {str(key): _jsonable_metadata(value) for key, value in sorted(loaded.items())}


def _jsonable_metadata(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_jsonable_metadata(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable_metadata(item) for key, item in sorted(value.items())}
    return str(value)


@contextmanager
def _memory_journal_lock(memory_dir: Path):
    _ensure_dir(memory_dir, "memory data dir")
    lock_path = memory_dir / MEMORY_LOCK_NAME
    payload = {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "created_at": int(time.time()),
    }
    _create_memory_lock(lock_path, payload)
    try:
        yield
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _create_memory_lock(lock_path: Path, payload: dict[str, Any]) -> None:
    while True:
        temp_path = lock_path.with_name(f".{lock_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            fd = os.open(temp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True)
                handle.write("\n")
            os.link(temp_path, lock_path)
            return
        except FileExistsError:
            if not _remove_stale_lock(lock_path):
                raise MemoryLockError(f"memory facts journal is locked: {lock_path}") from None
        except OSError as exc:
            raise RuntimeError(f"cannot lock memory facts journal: {exc}") from None
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass


def _remove_stale_lock(lock_path: Path) -> bool:
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    if payload.get("host") != socket.gethostname():
        return False
    pid = payload.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        try:
            lock_path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False
    except PermissionError:
        return False
    return False


def _publish_memory_export(
    memory_dir: Path,
    *,
    facts: list[dict[str, Any]],
    source_memory: Path,
    source_root: Path,
    changed: bool,
    record_import: bool,
) -> None:
    """Publish `export.ndjson`, `export.json` and `manifest.json` for one read of the canon.

    The manifest's `source.head` and `journal.commit` hold the content revision of exactly the
    exported facts, computed here so the two can never disagree.
    """
    reject_legacy_memory_journal(memory_dir)
    revision = content_revision({str(fact["id"]): text_digest(str(fact["text"])) for fact in facts})
    try:
        staging = Path(tempfile.mkdtemp(prefix=".memory-export-", suffix=".tmp", dir=memory_dir))
    except OSError as exc:
        raise RuntimeError(f"could not create memory export staging: {exc}") from None
    try:
        _write_ndjson(staging / "export.ndjson", facts)
        _write_json(
            staging / "export.json",
            {
                "version": 1,
                "source": str(source_memory),
                "fact_count": len(facts),
            },
        )
        _write_json(
            staging / "manifest.json",
            _memory_manifest(
                memory_dir,
                facts=facts,
                source_memory=source_memory,
                source_root=source_root,
                source_head=revision,
                commit=revision,
                changed=changed,
                record_import=record_import,
            ),
        )
        _publish_component_entries(
            staging,
            memory_dir,
            ["export.ndjson", "export.json", "manifest.json"],
            "memory export",
        )
    except RuntimeError:
        _cleanup_staging_dir(staging)
        raise


def _memory_manifest(
    memory_dir: Path,
    *,
    facts: list[dict[str, Any]],
    source_memory: Path,
    source_root: Path,
    source_head: str,
    commit: str | None,
    changed: bool,
    record_import: bool,
) -> dict[str, Any]:
    old = _read_json_file_if_valid(memory_dir / "manifest.json")
    imports = old.get("imports", []) if isinstance(old.get("imports"), list) else []
    entry = {
        "source_head": source_head,
        "journal_commit": commit,
        "fact_count": len(facts),
        "changed": changed,
    }
    provenance_keys = ("source_head", "journal_commit", "fact_count")
    if record_import and (not imports or any(imports[-1].get(key) != entry[key] for key in provenance_keys)):
        imports = [*imports, entry]
    return {
        "version": 1,
        "layout": {
            "facts": "facts",
            "export": "export.ndjson",
            "index": "index.sqlite",
        },
        "source": {
            "path": str(source_memory),
            "repo": str(source_root),
            "head": source_head,
            "readonly_fallback": True,
        },
        "journal": {
            "path": str(memory_dir / "facts"),
            "commit": commit,
            "fact_count": len(facts),
        },
        "imports": imports,
    }


def _read_json_file_if_valid(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}
