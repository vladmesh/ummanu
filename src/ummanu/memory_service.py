"""Memory MCP server, shared semantic memory for any MCP-speaking agent.

SQLite + sqlite-vec for storage/ANN, fastembed for multilingual embeddings,
exposed over streamable-HTTP so Claude / Codex / Hermes all share ONE warm instance.
"""

import datetime
import json
import os
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import sqlite_vec
from fastembed import TextEmbedding
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP

from ummanu.memory import DEFAULT_MODEL
from ummanu.memory import access as memory_access
from ummanu.memory.canon import (  # noqa: F401  # Service-level names of the canon parsing.
    fact_content_hash,
    fact_files,
    parse_fact_text,
    parse_frontmatter,
    scope_for_relative,
)
from ummanu.memory.model import load_text_embedding
from ummanu.runtime.paths import default_instance_path

DEFAULT_MEMORY_DIR = Path.home() / "ummanu-data" / "memory"
# Canon lives in the private instance repo (docs/RECOVERY.md, "Layout"); the
# export and the vector index stay derived under the data dir.
DEFAULT_CANON = default_instance_path() / "state" / "memory" / "facts"

CANON = Path(os.environ.get("MEMORY_CANON_ROOT", DEFAULT_CANON))

DB_PATH = os.environ.get("MEMORY_DB", str(DEFAULT_MEMORY_DIR / "index.sqlite"))
MODEL = os.environ.get("MEMORY_MODEL", DEFAULT_MODEL)
PORT = int(os.environ.get("MEMORY_PORT", "8077"))
DIM = int(os.environ.get("MEMORY_DIM", "1024"))
MODEL_CACHE_DIR = Path(os.environ.get("MEMORY_CACHE_DIR", str(DEFAULT_MEMORY_DIR / "fastembed-cache")))
THREADS = int(os.environ.get("MEMORY_THREADS", "1"))
SEARCH_LOG = os.environ.get("MEMORY_SEARCH_LOG", str(Path(DB_PATH).parent / "search-log.jsonl"))
CANON_EXPORT = (
    Path(os.environ["MEMORY_CANON_EXPORT"])
    if "MEMORY_CANON_EXPORT" in os.environ
    else CANON.parent / "export.ndjson"
)
WATCH_INTERVAL = float(os.environ.get("MEMORY_WATCH_INTERVAL", "10"))

_embedder = None
_lock = threading.Lock()
_reindex_lock = threading.Lock()
_search_log_lock = threading.Lock()  # own lock: don't couple log I/O to embedder/reindex locks
_ready_event = threading.Event()
_ready_error = None


def embedder() -> TextEmbedding:
    global _embedder
    if _embedder is None:
        with _lock:
            if _embedder is None:
                _embedder = load_text_embedding(MODEL, MODEL_CACHE_DIR, THREADS)
    return _embedder


def _unit(vec) -> np.ndarray:
    v = np.asarray(vec, dtype=np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


def embed_doc(text: str) -> np.ndarray:
    return _unit(list(embedder().embed([text]))[0])


def embed_query(text: str) -> np.ndarray:
    return _unit(list(embedder().query_embed(text))[0])


def db(path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def create_schema(conn: sqlite3.Connection, dim: int | None = None) -> None:
    dim = DIM if dim is None else dim
    conn.execute(
        "CREATE TABLE IF NOT EXISTS memories("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, fact_id TEXT UNIQUE, content_hash TEXT, "
        "text TEXT NOT NULL, scope TEXT, tags TEXT, source TEXT, created_at TEXT)"
    )
    conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_memories USING vec0(embedding float[{dim}])")
    conn.execute("CREATE TABLE IF NOT EXISTS index_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)")


class NotReadyError(RuntimeError):
    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class IndexReadError(RuntimeError):
    pass


def not_ready_response(reason: str, detail: str) -> dict:
    return {
        "status": "not_ready",
        "error": reason,
        "retryable": True,
        "detail": detail,
    }


def index_exists() -> bool:
    return Path(DB_PATH).is_file()


def search_ready() -> bool:
    return _ready_event.is_set() and index_exists()


def mark_search_ready(error: Exception | None = None) -> None:
    global _ready_error
    _ready_error = error
    _ready_event.set()


def mark_search_not_ready(error: Exception | None = None) -> None:
    global _ready_error
    _ready_error = error
    _ready_event.clear()


def normalize_scope(scope: str | None) -> str | None:
    """Accept the canonical memory scope spellings without granting one."""
    return memory_access.normalize_scope(scope)


def search_memory(
    query: str,
    k: int = 5,
    scope: str | None = None,
    *,
    allowed_scopes: frozenset[str] | None = None,
) -> list:
    if not index_exists():
        raise NotReadyError("index_missing", f"index does not exist: {DB_PATH}")
    scope = normalize_scope(scope)
    if scope is not None:
        allowed_scopes = frozenset({scope})
    # sqlite-vec KNN can't push a join filter into MATCH, so with a scope we over-fetch
    # and trim post-hoc. Fine at canon size (hundreds of facts).
    fetch = max(k * 5, 25) if allowed_scopes is not None else k
    qvec = sqlite_vec.serialize_float32(embed_query(query).tolist())
    with _reindex_lock:  # don't read while a rebuild is swapping tables
        conn = db()
        rows = conn.execute(
            "SELECT v.rowid, v.distance, m.text, m.scope, m.tags, m.source, m.created_at "
            "FROM vec_memories v JOIN memories m ON m.id = v.rowid "
            "WHERE v.embedding MATCH ? AND k = ? ORDER BY v.distance",
            (qvec, fetch),
        ).fetchall()
        conn.close()
    if allowed_scopes is not None:
        rows = [r for r in rows if r[3] in allowed_scopes][:k]
    # unit vectors → L2 distance d relates to cosine: cos = 1 - d^2/2
    return [
        {
            "id": r[0],
            "score": round(1 - (r[1] ** 2) / 2, 4),
            "text": r[2],
            "scope": r[3],
            "tags": r[4],
            "source": r[5],
            "created_at": r[6],
        }
        for r in rows
    ]


def log_read(
    action: str,
    identity: memory_access.MemoryReadIdentity | None,
    outcome: str,
    *,
    results: list | None = None,
    requested_k: int | None = None,
) -> None:
    """Append data-free authorization telemetry. It never logs facts or bearer material."""
    try:
        entry = {
            "ts": datetime.datetime.now(datetime.UTC).isoformat(),
            "action": action,
            "outcome": outcome,
            # Keep the schema useful for every denial.  An unresolved identity has no
            # attributable role or subject, but it is visibly distinct from an omitted field.
            "role": None,
            "subject": None,
            "scopes": [],
        }
        if identity is not None:
            entry.update(identity.audit_json())
        if results is not None:
            entry["hits"] = [{"id": r["id"], "score": r["score"]} for r in results]
        if requested_k is not None:
            entry["k"] = requested_k
        line = json.dumps(entry, ensure_ascii=False)
        with _search_log_lock, open(SEARCH_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
    except Exception:
        pass


class MemoryTokenVerifier:
    """FastMCP's standard Bearer-token verifier for launch-bound memory grants."""

    async def verify_token(self, token: str) -> AccessToken | None:
        resolved = memory_access.resolve_token(token)
        if isinstance(resolved, memory_access.MemoryAccessDenial):
            log_read("authenticate", None, resolved.code)
            return None
        log_read("authenticate", resolved, "allowed")
        scopes = sorted(resolved.scopes) if resolved.scopes is not None else ["installation-wide"]
        return AccessToken(
            token=token,
            client_id=f"memory:{resolved.grant_id}",
            subject=resolved.role,
            scopes=scopes,
            claims={"memory_grant_id": resolved.grant_id},
        )


def read_guard(requested_scope: str | None = None) -> memory_access.MemoryReadIdentity | memory_access.MemoryAccessDenial:
    """The one server-side authorization guard for every memory read endpoint."""
    access_token = get_access_token()
    claims = access_token.claims if access_token is not None and isinstance(access_token.claims, dict) else {}
    grant_id = claims.get("memory_grant_id")
    resolved = memory_access.resolve_grant_id(grant_id)
    if isinstance(resolved, memory_access.MemoryAccessDenial):
        return resolved
    return memory_access.narrow(resolved, requested_scope)


def list_memory_entries(limit: int = 50, *, allowed_scopes: frozenset[str] | None = None) -> list[dict[str, Any]]:
    """Read index entries without MCP authorization, for local index maintenance checks only."""
    conn = db()
    try:
        if allowed_scopes is None:
            rows = conn.execute(
                "SELECT id, text, scope, tags, source, created_at FROM memories ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            placeholders = ",".join("?" for _ in allowed_scopes)
            rows = conn.execute(
                f"SELECT id, text, scope, tags, source, created_at FROM memories WHERE scope IN ({placeholders}) "
                "ORDER BY id DESC LIMIT ?",
                (*sorted(allowed_scopes), limit),
            ).fetchall()
    finally:
        conn.close()
    return [
        {"id": r[0], "text": r[1], "scope": r[2], "tags": r[3], "source": r[4], "created_at": r[5]}
        for r in rows
    ]


def get_memory_entry(id: int, *, allowed_scopes: frozenset[str] | None = None) -> dict[str, Any] | None:
    """Read one index entry without MCP authorization, for local index maintenance checks only."""
    conn = db()
    try:
        if allowed_scopes is None:
            row = conn.execute(
                "SELECT id, text, scope, tags, source, created_at FROM memories WHERE id = ?", (id,)
            ).fetchone()
        else:
            placeholders = ",".join("?" for _ in allowed_scopes)
            row = conn.execute(
                f"SELECT id, text, scope, tags, source, created_at FROM memories "
                f"WHERE id = ? AND scope IN ({placeholders})",
                (id, *sorted(allowed_scopes)),
            ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {"id": row[0], "text": row[1], "scope": row[2], "tags": row[3], "source": row[4], "created_at": row[5]}


# ── Canon → index (daemon-owned reindex) ──────────────────────────────────────
# The production canon is the live root's state/memory/facts, named by MEMORY_CANON_ROOT.
# Prefer the atomically published export.ndjson snapshot; without one we read the canon
# files themselves. The live root need not be a Git work tree, and nothing here calls Git.


def load_export_snapshot(path: Path) -> list[dict]:
    facts = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            obj = json.loads(line)
            raw = obj.get("text", "")
            rel = obj.get("path") or f"{obj['id']}.md"
            facts.append(parse_fact_text(raw, rel, fact_id=obj.get("id")))
    return facts


def load_canon_files(path: Path) -> list[dict]:
    """Every fact file under the canon root as it is on disk; symlinks are not facts."""
    facts = []
    for fact_id, fact_path in fact_files(path):
        raw = fact_path.read_bytes().decode("utf-8")
        facts.append(parse_fact_text(raw, Path(f"{fact_id}.md"), fact_id=fact_id))
    return facts


def load_canon_entries(canon: Path | None = None, export: Path | None = None) -> list[dict]:
    canon = canon or CANON
    export = export or CANON_EXPORT
    if export.is_file():
        return load_export_snapshot(export)
    if not canon.is_dir():
        return []
    return load_canon_files(canon)


def canon_signature() -> tuple:
    """Cheap change token: the export's stat, else (file count, max mtime, total size) of the canon."""
    if CANON_EXPORT.is_file():
        st = CANON_EXPORT.stat()
        return ("export", st.st_mtime, st.st_size)
    if not CANON.is_dir():
        return (0, 0.0, 0)
    stats = [path.stat() for _fact_id, path in fact_files(CANON)]
    return (
        "files",
        len(stats),
        max((st.st_mtime_ns for st in stats), default=0),
        sum(st.st_size for st in stats),
    )


def build_document_embedder(model: str, cache_dir: str | Path, threads: int):
    """Return a normalized document embedder for an explicit model."""
    embedding_model = load_text_embedding(model, cache_dir, threads)

    class DocumentEmbedder:
        def __call__(self, text: str) -> np.ndarray:
            return self.embed_many([text])[0]

        def embed_many(self, texts: list[str]) -> list[np.ndarray]:
            return [_unit(vector) for vector in embedding_model.embed(texts, batch_size=2)]

    return DocumentEmbedder()


def indexed_fact_count(path: str | Path | None = None) -> int:
    path = Path(path or DB_PATH)
    if not path.is_file():
        return 0
    conn = None
    try:
        conn = db(path)
        return conn.execute("SELECT count(*) FROM memories").fetchone()[0]
    except sqlite3.DatabaseError as error:
        raise IndexReadError(f"index unreadable: {path}: {error}") from error
    finally:
        if conn is not None:
            conn.close()


def write_index(facts: list[dict], target: Path, model: str, dim: int, document_embed) -> int:
    """Write a complete index to a temporary file, then atomically publish it."""
    embed_many = getattr(document_embed, "embed_many", None)
    vectors = (
        embed_many([fact["text"] for fact in facts])
        if callable(embed_many)
        else [document_embed(fact["text"]) for fact in facts]
    )
    if len(vectors) != len(facts):
        raise RuntimeError(f"embedder returned {len(vectors)} vectors for {len(facts)} facts")
    rows = list(zip(facts, vectors))
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    tmp.unlink(missing_ok=True)
    try:
        conn = db(tmp)
        create_schema(conn, dim)
        conn.executemany(
            "INSERT INTO index_metadata(key, value) VALUES (?, ?)",
            [("model", model), ("dimension", str(dim)), ("schema", "2")],
        )
        for f, vec in rows:
            cur = conn.execute(
                "INSERT INTO memories(fact_id, content_hash, text, scope, tags, source, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    f["id"],
                    fact_content_hash(f),
                    f["text"],
                    f["scope"],
                    f["tags"],
                    f["source"],
                    f["created_at"],
                ),
            )
            conn.execute(
                "INSERT INTO vec_memories(rowid, embedding) VALUES (?, ?)",
                (cur.lastrowid, sqlite_vec.serialize_float32(vec.tolist())),
            )
        conn.commit()
        conn.close()
        indexed = indexed_fact_count(tmp)
        if indexed != len(facts):
            raise RuntimeError(f"index parity failed before publish: expected {len(facts)}, got {indexed}")
        with _reindex_lock:
            os.replace(tmp, target)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return indexed_fact_count(target)


def index_metadata(path: str | Path) -> dict[str, str]:
    path = Path(path)
    if not path.is_file():
        return {}
    conn = None
    try:
        conn = db(path)
        rows = conn.execute("SELECT key, value FROM index_metadata").fetchall()
    except sqlite3.OperationalError as error:
        if "no such table: index_metadata" in str(error):
            return {}
        raise IndexReadError(f"index unreadable: {path}: {error}") from error
    except sqlite3.DatabaseError as error:
        raise IndexReadError(f"index unreadable: {path}: {error}") from error
    finally:
        if conn is not None:
            conn.close()
    return {key: value for key, value in rows}


def index_compatibility(path: str | Path, model: str, dim: int) -> tuple[str, str | None]:
    try:
        metadata = index_metadata(path)
    except IndexReadError as error:
        return "unusable", str(error)
    if not metadata:
        return "legacy", None
    if metadata.get("model") != model or metadata.get("dimension") != str(dim):
        return "mismatch", (
            f"index metadata mismatch: expected model={model!r} dimension={dim}, "
            f"got model={metadata.get('model')!r} dimension={metadata.get('dimension')!r}"
        )
    return "compatible", None


def offline_rebuild(
    canon: str | Path,
    export: str | Path,
    target_db: str | Path,
    model: str,
    dim: int,
    document_embed=None,
    allow_empty: bool = False,
    cache_dir: str | Path = MODEL_CACHE_DIR,
    threads: int = THREADS,
) -> dict:
    """Build and atomically publish an index from an explicit canon snapshot.

    ``document_embed`` is an injection point for tests. Production callers omit it,
    which loads the requested fastembed model.
    """
    canon_path = Path(canon)
    export_path = Path(export)
    target = Path(target_db)
    if int(dim) <= 0:
        raise ValueError(f"dimension must be positive: {dim}")
    if not export_path.is_file() and not canon_path.is_dir():
        raise RuntimeError(
            f"canon snapshot unavailable: no export at {export_path} and no canon at {canon_path}"
        )
    facts = load_canon_entries(canon_path, export_path)
    if not facts and not allow_empty:
        raise RuntimeError(
            "canon snapshot has no current facts; pass allow_empty=True to publish an empty index"
        )
    document_embed = document_embed or build_document_embedder(model, cache_dir, threads)
    indexed = write_index(facts, target, model, int(dim), document_embed)
    parity = {"expected": len(facts), "indexed": indexed, "ok": indexed == len(facts)}
    if not parity["ok"]:
        raise RuntimeError(f"index parity failed after publish: expected {len(facts)}, got {indexed}")
    return {
        "ok": parity["ok"],
        "canon": str(canon_path),
        "export": str(export_path),
        "target_db": str(target),
        "model": model,
        "dimension": int(dim),
        "parity": parity,
    }


def incremental_update(
    canon: str | Path = CANON,
    export: str | Path = CANON_EXPORT,
    target_db: str | Path = DB_PATH,
    model: str = MODEL,
    dim: int = DIM,
    document_embed=None,
    allow_empty: bool = True,
    cache_dir: str | Path = MODEL_CACHE_DIR,
    threads: int = THREADS,
) -> dict:
    """Update added, changed and deleted facts in a compatible index.

    Legacy and mismatched indexes are rebuilt atomically. Changed embeddings are
    computed before the transaction, so an embedding failure preserves the old index.
    """
    canon_path = Path(canon)
    export_path = Path(export)
    target = Path(target_db)
    facts = load_canon_entries(canon_path, export_path)
    if not facts and not allow_empty:
        raise RuntimeError("canon snapshot has no current facts")
    compatibility, _ = index_compatibility(target, model, int(dim))
    if compatibility != "compatible":
        rebuilt = offline_rebuild(
            canon_path,
            export_path,
            target,
            model,
            int(dim),
            document_embed,
            allow_empty,
            cache_dir,
            threads,
        )
        return {
            **rebuilt,
            "mode": "rebuild",
            "added": len(facts),
            "updated": 0,
            "deleted": 0,
            "reused": 0,
        }

    conn = db(target)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
        if not {"fact_id", "content_hash"}.issubset(columns):
            conn.close()
            rebuilt = offline_rebuild(
                canon_path,
                export_path,
                target,
                model,
                int(dim),
                document_embed,
                allow_empty,
                cache_dir,
                threads,
            )
            return {
                **rebuilt,
                "mode": "rebuild",
                "added": len(facts),
                "updated": 0,
                "deleted": 0,
                "reused": 0,
            }
        existing = {
            fact_id: (rowid, content_hash)
            for rowid, fact_id, content_hash in conn.execute("SELECT id, fact_id, content_hash FROM memories")
        }
        desired = {fact["id"]: (fact, fact_content_hash(fact)) for fact in facts}
        added = [item for key, item in desired.items() if key not in existing]
        changed = [item for key, item in desired.items() if key in existing and existing[key][1] != item[1]]
        deleted = [key for key in existing if key not in desired]
        document_embed = document_embed or build_document_embedder(model, cache_dir, threads)
        vectors = {fact["id"]: document_embed(fact["text"]) for fact, _ in (*added, *changed)}
        with _reindex_lock:
            conn.execute("BEGIN IMMEDIATE")
            for fact_id in deleted:
                rowid = existing[fact_id][0]
                conn.execute("DELETE FROM vec_memories WHERE rowid = ?", (rowid,))
                conn.execute("DELETE FROM memories WHERE id = ?", (rowid,))
            for fact, digest in changed:
                rowid = existing[fact["id"]][0]
                conn.execute("DELETE FROM vec_memories WHERE rowid = ?", (rowid,))
                conn.execute(
                    "UPDATE memories SET content_hash=?, text=?, scope=?, tags=?, source=?, "
                    "created_at=? WHERE id=?",
                    (
                        digest,
                        fact["text"],
                        fact["scope"],
                        fact["tags"],
                        fact["source"],
                        fact["created_at"],
                        rowid,
                    ),
                )
                conn.execute(
                    "INSERT INTO vec_memories(rowid, embedding) VALUES (?, ?)",
                    (rowid, sqlite_vec.serialize_float32(vectors[fact["id"]].tolist())),
                )
            for fact, digest in added:
                cur = conn.execute(
                    "INSERT INTO memories(fact_id, content_hash, text, scope, tags, source, "
                    "created_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        fact["id"],
                        digest,
                        fact["text"],
                        fact["scope"],
                        fact["tags"],
                        fact["source"],
                        fact["created_at"],
                    ),
                )
                conn.execute(
                    "INSERT INTO vec_memories(rowid, embedding) VALUES (?, ?)",
                    (cur.lastrowid, sqlite_vec.serialize_float32(vectors[fact["id"]].tolist())),
                )
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    indexed = indexed_fact_count(target)
    if indexed != len(facts):
        raise RuntimeError(f"incremental index parity failed: expected {len(facts)}, got {indexed}")
    return {
        "ok": True,
        "mode": "incremental",
        "canon": str(canon_path),
        "export": str(export_path),
        "target_db": str(target),
        "model": model,
        "dimension": int(dim),
        "added": len(added),
        "updated": len(changed),
        "deleted": len(deleted),
        "reused": len(facts) - len(added) - len(changed),
        "parity": {"expected": len(facts), "indexed": indexed, "ok": True},
    }


def update_index() -> dict:
    """Daemon-owned incremental update using the warm document embedder."""
    return incremental_update(
        CANON, CANON_EXPORT, DB_PATH, MODEL, DIM, document_embed=embed_doc, allow_empty=True
    )


def bootstrap_index() -> int | None:
    """Warm the embedder and reconcile the index incrementally."""
    mark_search_not_ready()
    embedder()
    try:
        result = update_index()
        n = result["parity"]["indexed"]
    except Exception as e:
        has_index = index_exists()
        status, detail = index_compatibility(DB_PATH, MODEL, DIM) if has_index else ("missing", None)
        if has_index and status in {"compatible", "legacy"}:
            mark_search_ready(e)
            print(
                f"memory-mcp: incremental reconciliation failed, keeping previous index: {e}",
                flush=True,
            )
            return None
        error = RuntimeError(detail) if detail else e
        mark_search_not_ready(error)
        print(
            f"memory-mcp: incremental reconciliation failed, no compatible index available: {error}",
            flush=True,
        )
        return None
    status, detail = index_compatibility(DB_PATH, MODEL, DIM)
    if status != "compatible":
        error = detail or "incrementally reconciled index has no metadata"
        mark_search_not_ready(RuntimeError(error))
        print(f"memory-mcp: incrementally reconciled index is incompatible: {error}", flush=True)
        return None
    mark_search_ready()
    return n


def start_background_bootstrap() -> None:
    def loop():
        try:
            n = bootstrap_index()
            if n is not None:
                print(
                    f"memory-mcp ready: model={MODEL} dim={DIM} db={DB_PATH} port={PORT} "
                    f"facts={n} canon={CANON} export={CANON_EXPORT} watch={WATCH_INTERVAL}s",
                    flush=True,
                )
        finally:
            start_canon_watcher()

    threading.Thread(target=loop, name="bootstrap-index", daemon=True).start()


def start_canon_watcher() -> None:
    """Background thread: rebuild the index whenever the canon changes on disk."""

    def loop():
        last = canon_signature()
        while True:
            time.sleep(WATCH_INTERVAL)
            try:
                sig = canon_signature()
                if sig != last:
                    result = update_index()
                    n = result["parity"]["indexed"]
                    mark_search_ready()
                    last = sig
                    print(
                        "memory-mcp: canon changed, "
                        f"indexed={n} added={result['added']} updated={result['updated']} "
                        f"deleted={result['deleted']} reused={result['reused']}",
                        flush=True,
                    )
            except Exception as e:  # never let the watcher kill the daemon
                print(f"memory-mcp: watcher error: {e}", flush=True)

    threading.Thread(target=loop, name="canon-watcher", daemon=True).start()


mcp = FastMCP(
    "memory",
    host="127.0.0.1",
    port=PORT,
    token_verifier=MemoryTokenVerifier(),
    auth=AuthSettings(
        issuer_url=f"http://127.0.0.1:{PORT}",
        resource_server_url=f"http://127.0.0.1:{PORT}/mcp",
        required_scopes=[],
    ),
)

# Memory is read-only for agents: only the curator writes (the markdown canon, then a reindex).
# Nothing here writes the index directly; the index is derived from the canon by a reindex.


@mcp.tool()
def memory_search(query: str, k: int = 5, scope: str = "", caller: str = "") -> Any:
    """Search facts allowed by the launch-bound Bearer identity.

    ``scope`` can only narrow the server-resolved scope set. ``caller`` is
    retained for old clients and ignored as authorization input."""
    authorization = read_guard(scope or None)
    if isinstance(authorization, memory_access.MemoryAccessDenial):
        log_read("memory_search", authorization.identity, authorization.code)
        return authorization.response()
    # Result cap: the ranker's scores sit in a narrow band (~0.80-0.84 in telemetry), so a long
    # tail is indistinguishable from the top and just clutters the context.
    # The original k is written to the log: telemetry must see what was actually asked for.
    requested_k = k
    k = max(1, min(k, 10))
    if not search_ready():
        reason = "embedder_loading" if _ready_error is None else "index_unavailable"
        detail = "embedding model/index are still loading"
        if _ready_error is not None:
            detail = str(_ready_error)
        log_read("memory_search", authorization, reason)
        return not_ready_response(reason, detail)
    try:
        results = search_memory(query, k, allowed_scopes=authorization.scopes)
    except NotReadyError as e:
        log_read("memory_search", authorization, e.reason)
        return not_ready_response(e.reason, e.detail)
    log_read("memory_search", authorization, "allowed", results=results, requested_k=requested_k)
    return results


@mcp.tool()
def memory_get(id: int) -> dict:
    """Fetch one authorized memory entry by id."""
    authorization = read_guard()
    if isinstance(authorization, memory_access.MemoryAccessDenial):
        log_read("memory_get", authorization.identity, authorization.code)
        return authorization.response()
    if not index_exists():
        log_read("memory_get", authorization, "index_missing")
        return not_ready_response("index_missing", f"index does not exist: {DB_PATH}")
    entry = get_memory_entry(id, allowed_scopes=authorization.scopes)
    if entry is None:
        log_read("memory_get", authorization, "not_found_or_not_permitted")
        return {"error": "not found", "id": id}
    log_read("memory_get", authorization, "allowed", results=[{"id": entry["id"], "score": 0.0}])
    return entry


@mcp.tool()
def memory_list(limit: int = 50) -> list:
    """List recent memory entries in the launch-bound allowed scopes."""
    authorization = read_guard()
    if isinstance(authorization, memory_access.MemoryAccessDenial):
        log_read("memory_list", authorization.identity, authorization.code)
        return [authorization.response()]
    if not index_exists():
        log_read("memory_list", authorization, "index_missing")
        return [not_ready_response("index_missing", f"index does not exist: {DB_PATH}")]
    rows = list_memory_entries(limit, allowed_scopes=authorization.scopes)
    log_read("memory_list", authorization, "allowed", results=[{"id": row["id"], "score": 0.0} for row in rows])
    return rows


def main() -> None:
    print(
        f"ummanu memory MCP listening: port={PORT} db={DB_PATH} canon={CANON}; "
        "index warming in background",
        flush=True,
    )
    start_background_bootstrap()
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
