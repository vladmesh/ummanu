"""Discovery — where each head writes its raw session files and personal-memory facts.

Paths mirror Orca's ai-vault session-scanner (src/main/ai-vault/session-scanner-*),
which we reuse as reference rather than runtime (it only keeps 5-message previews and is
reachable only in-process). We read the raw files ourselves. Claude, Hermes and Codex
sessions on this host are wired; add a parser + path as new heads produce sessions.

Self-exclusion: the curator excludes only its own exact workspace and, where the
launcher provides one, its exact session id. It deliberately does not exclude the
Ummanu base checkout, sibling worker worktrees, or observer workspaces.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path

from ummanu.config import ConfigError, load_config
from ummanu.infra.env import env_value
from ummanu.runtime.codex_home import session_roots
from ummanu.runtime.paths import default_instance_path, instance_dir
from ummanu.sprints import SPRINT_REFERENCE_PREFIX, SprintReader, sprint_client

# Claude project-dir naming: every non-alphanumeric cwd character becomes "-".
# Overridable via UMMANU_CLAUDE_PROJECTS_DIR so a run (e.g. an e2e on fixtures) can point the
# scan at a synthetic tree instead of the live ~/.claude/projects.
CLAUDE_PROJECTS = Path(env_value("UMMANU_CLAUDE_PROJECTS_DIR", str(Path.home() / ".claude" / "projects")))

# Hermes home. Overridable via UMMANU_HERMES_HOME_DIR for the same reason as
# UMMANU_CLAUDE_PROJECTS_DIR above. On this host Hermes 0.17.0 stores sessions in a shared
# SQLite DB (hermes_state.py: "replacing the per-session JSONL file approach") rather than
# the per-session `session_*.json` files under a `sessions/` dir that the Orca ai-vault
# scanner (our format reference) still expects -- that dir exists but is always empty here.
# We read the live schema instead of the stale file-based reference.
HERMES_HOME = Path(env_value("UMMANU_HERMES_HOME_DIR", str(Path.home() / ".hermes")))
HERMES_STATE_DB = HERMES_HOME / "state.db"
HERMES_MEMORY_DIR = HERMES_HOME / "memories"

# Sessions of the CODEX_HOMEs pipeline Codex heads run with. Overridable in tests for the same
# reason as CLAUDE_PROJECTS/HERMES_HOME; unset, every home a head may be writing into is scanned
# (`codex_home.session_roots`), so the move to `<data_dir>/codex-home` loses no live head's sessions.
_CODEX_SESSIONS_OVERRIDE = env_value("UMMANU_CODEX_SESSIONS_DIR", nonempty=True)
CODEX_SESSIONS: Path | None = Path(_CODEX_SESSIONS_OVERRIDE) if _CODEX_SESSIONS_OVERRIDE else None

ROUTE_UNKNOWN = "unknown"
ROUTE_GLOBAL = "global"
ROUTE_PO_REVIEW = "review:po"
_PROJECT_ID = re.compile(r"[a-z0-9][a-z0-9-]*\Z")


def selected_instance() -> Path:
    """The installation whose registry is authoritative for curator routing."""
    return Path(os.environ.get("UMMANU_INSTANCE", str(default_instance_path()))).expanduser()


def _normalized_directory(value: str | Path, *, strict: bool) -> Path | None:
    """Return a resolved directory, never treating a missing or unreadable path as a route."""
    try:
        path = Path(value).expanduser().resolve(strict=strict)
        return path if path.is_dir() else None
    except (OSError, RuntimeError, ValueError):
        return None


def _normalized_absolute_path(value: str | Path) -> Path | None:
    """Normalize an absolute recorded cwd without requiring its leaf to survive.

    Curator input is historical.  Dispatcher worktrees are deliberately removed after a
    card completes, so requiring the recorded cwd to remain a directory would erase the
    otherwise unambiguous project boundary from old sessions.  The path is data used for
    routing, not filesystem authority: relative and malformed values remain unknown.
    """
    try:
        path = Path(value).expanduser()
        if not path.is_absolute():
            return None
        return path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None


def project_bindings(instance: Path | None = None) -> list[dict]:
    """Read usable canonical bindings from the selected instance registry.

    A malformed or unreadable entry is deliberately not a partial route.  A canonical binding
    needs only its `id` and absolute `repo`; an optional safe `orca_binding` adds its Orca
    workspace tree as another route boundary.
    """
    root = instance_dir(instance or selected_instance())
    directory = root / "projects"
    if not directory.is_dir():
        return []
    result = []
    for path in sorted(directory.glob("*.yaml")):
        try:
            binding = load_config(path)
        except ConfigError:
            continue
        if not isinstance(binding, dict):
            continue
        project_id, repo = binding.get("id"), binding.get("repo")
        if (
            not all(isinstance(value, str) and value for value in (project_id, repo))
            or not _PROJECT_ID.fullmatch(project_id)
            or not Path(repo).is_absolute()
        ):
            continue
        route = {"id": project_id, "repo": repo}
        orca_binding = binding.get("orca_binding")
        if (
            isinstance(orca_binding, str)
            and orca_binding
            and Path(orca_binding).name == orca_binding
            and orca_binding not in {".", ".."}
        ):
            route["orca_binding"] = orca_binding
        curator_roots = binding.get("curator_roots")
        if isinstance(curator_roots, list):
            roots = []
            for value in curator_roots:
                if not isinstance(value, str):
                    continue
                normalized = _normalized_absolute_path(value)
                if normalized is not None:
                    roots.append(str(normalized))
            if roots:
                route["curator_roots"] = tuple(dict.fromkeys(roots))
        result.append(route)
    return result


def registered_project_ids(instance: Path | None = None) -> set[str]:
    """Canonical ids which a curator selector may name, without deriving ids from paths."""
    return {binding["id"] for binding in project_bindings(instance)}


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _workspace_root() -> Path:
    return Path(env_value("UMMANU_WORKSPACES_ROOT", nonempty=True) or Path.home() / "orca" / "workspaces")


def _observer_reference(cwd: Path) -> str | None:
    """Extract an observer's sprint reference only from its canonical workspace shape."""
    root = _normalized_absolute_path(_workspace_root() / "observers")
    if root is None or not _within(cwd, root):
        return None
    relative = cwd.relative_to(root)
    if not relative.parts:
        return None
    name = relative.parts[0]
    if not name.startswith("sprint-") or len(name) == len("sprint-"):
        return None
    return f"{SPRINT_REFERENCE_PREFIX}{name[len('sprint-') :]}"


def _observer_route(reference: str, instance: Path, known_ids: set[str]) -> str:
    """Resolve an observer from its sprint's complete structured reservation set."""
    try:
        sprint = SprintReader(sprint_client(instance)).show(
            reference, include_cards=False, include_resume_freshness=False
        )
    # Board reachability is not curator work.  A failure to read the structured record is
    # deliberately an unknown route, never an exception that suppresses other discovery.
    except Exception:  # noqa: BLE001 - every board transport/schema failure is an unknown route
        return ROUTE_UNKNOWN
    reservations = sprint.get("reservations") if isinstance(sprint, dict) else None
    if (
        not isinstance(reservations, list)
        or not reservations
        or not all(isinstance(project, str) and project in known_ids for project in reservations)
        or len(set(reservations)) != len(reservations)
    ):
        return ROUTE_UNKNOWN
    return reservations[0] if len(reservations) == 1 else ROUTE_PO_REVIEW


class RouteResolver:
    """One immutable routing snapshot shared by a complete source scan.

    Discovery used to reload and normalize every project binding for every session file.  A host
    with a few thousand sessions therefore performed tens of thousands of YAML reads and path
    resolutions before the project selector could discard unrelated sources.  The registry and
    path boundaries cannot legitimately change halfway through one scan, so compile them once and
    cache observer sprint lookups for the lifetime of that scan.
    """

    def __init__(self, instance: Path | None = None):
        self.instance = instance_dir(instance or selected_instance())
        bindings = project_bindings(self.instance)
        self.known_ids = {binding["id"] for binding in bindings}
        workspace_root = _normalized_directory(_workspace_root(), strict=False)
        self.boundaries: list[tuple[str, tuple[Path, ...]]] = []
        for binding in bindings:
            roots = []
            if repo := _normalized_directory(binding["repo"], strict=True):
                roots.append(repo)
            orca_binding = binding.get("orca_binding")
            if (
                workspace_root
                and orca_binding
                and (workspace := _normalized_absolute_path(workspace_root / orca_binding))
            ):
                roots.append(workspace)
            roots.extend(
                root
                for value in binding.get("curator_roots", ())
                if (root := _normalized_absolute_path(value)) is not None
            )
            self.boundaries.append((binding["id"], tuple(dict.fromkeys(roots))))
        self._observer_routes: dict[str, str] = {}

    def resolve(self, cwd: str, *, global_source: bool = False) -> str:
        if global_source:
            return ROUTE_GLOBAL
        candidate = _normalized_absolute_path(cwd) if cwd else None
        if candidate is None:
            return ROUTE_UNKNOWN
        reference = _observer_reference(candidate)
        if reference is not None:
            if reference not in self._observer_routes:
                self._observer_routes[reference] = _observer_route(reference, self.instance, self.known_ids)
            return self._observer_routes[reference]
        matches = [
            project_id
            for project_id, roots in self.boundaries
            if any(_within(candidate, root) for root in roots)
        ]
        return matches[0] if len(matches) == 1 else ROUTE_UNKNOWN


def resolve_route(cwd: str, *, instance: Path | None = None, global_source: bool = False) -> str:
    """Return a canonical project id, or an explicit review/global/unknown route."""
    return RouteResolver(instance).resolve(cwd, global_source=global_source)


def _with_route(source: dict, *, resolver: RouteResolver, global_source: bool = False) -> dict:
    return {**source, "route": resolver.resolve(source.get("cwd", ""), global_source=global_source)}


def curator_workspace() -> Path:
    """The one workspace this curator run must not feed back into itself."""
    return Path(env_value("UMMANU_CURATOR_WORKSPACE", nonempty=True) or Path.cwd()).resolve(strict=False)


def curator_session_id() -> str:
    return env_value("UMMANU_CURATOR_SESSION_ID", "")


def _cwd_from_claude_dir(dirname: str) -> str:
    # "-home-dev-ummanu" -> "/home/dev/ummanu". Lossy (dirs with real
    # dashes collide); only a fallback when the file carries no cwd field.
    return "/" + dirname.lstrip("-").replace("-", "/")


def _cwd_from_file(path: Path, fallback: str) -> str:
    # Claude JSONL lines carry the real `cwd`; read the first that has it (dashes intact).
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for _ in range(10):
                line = fh.readline()
                if not line:
                    break
                try:
                    cwd = json.loads(line).get("cwd")
                except json.JSONDecodeError:
                    continue
                if cwd:
                    return cwd
    except OSError:
        pass
    return fallback


def _excluded(cwd: str, session_id: str = "") -> bool:
    if session_id and session_id == curator_session_id():
        return True
    if not cwd:
        return False
    try:
        return Path(cwd).resolve(strict=False) == curator_workspace()
    except (OSError, ValueError):
        return False


def _dirname_for_cwd(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def _excluded_dirname(name: str) -> bool:
    return name == _dirname_for_cwd(str(curator_workspace()))


def claude_sessions() -> list[dict]:
    """List Claude session files as {head, path, session_id, cwd}, self-excluded."""
    out = []
    if not CLAUDE_PROJECTS.is_dir():
        return out
    resolver = RouteResolver()
    for proj in sorted(CLAUDE_PROJECTS.iterdir()):
        if not proj.is_dir():
            continue
        fallback = _cwd_from_claude_dir(proj.name)
        for f in sorted(proj.glob("*.jsonl")):
            cwd = _cwd_from_file(f, fallback)
            if _excluded(cwd, f.stem):
                continue
            out.append(
                _with_route(
                    {"head": "claude", "path": str(f), "session_id": f.stem, "cwd": cwd}, resolver=resolver
                )
            )
    return out


def _hermes_query(sql: str, params: tuple = ()) -> list[tuple] | None:
    """Run one read-only query against state.db. Returns None (not []) on any sqlite
    failure -- a corrupted or transiently write-locked DB must degrade Hermes discovery
    to empty, not raise and take down the whole harvest tick, including the unrelated
    Claude side."""
    try:
        # Read-only: state.db is live-written by real Hermes sessions (WAL mode per its
        # own docstring, concurrent readers are safe) -- the curator only ever reads it.
        con = sqlite3.connect(f"file:{HERMES_STATE_DB}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        return con.execute(sql, params).fetchall()
    except sqlite3.Error:
        return None
    finally:
        con.close()


def hermes_sessions() -> list[dict]:
    """List Hermes sessions from ~/.hermes/state.db as {head, path, session_id, cwd},
    self-excluded by cwd like claude_sessions(). `path` is the shared state.db for every
    row -- unlike Claude's one-file-per-session layout, harvest.py watermarks Hermes by
    session_id, not by this path."""
    out = []
    if not HERMES_STATE_DB.is_file():
        return out
    rows = _hermes_query("SELECT id, cwd FROM sessions WHERE archived = 0 ORDER BY id")
    if not rows:
        return out
    resolver = RouteResolver()
    for session_id, cwd in rows:
        cwd = cwd or ""
        if _excluded(cwd, session_id):
            continue
        out.append(
            _with_route(
                {"head": "hermes", "path": str(HERMES_STATE_DB), "session_id": session_id, "cwd": cwd},
                resolver=resolver,
            )
        )
    return out


def _codex_meta_from_file(path: Path) -> dict:
    """Read Codex's session_meta line. Returns session_id/cwd fallbacks on bad files."""
    meta = {"session_id": path.stem, "cwd": ""}
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for _ in range(20):
                line = fh.readline()
                if not line:
                    break
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") != "session_meta":
                    continue
                payload = rec.get("payload") or {}
                meta["session_id"] = payload.get("session_id") or payload.get("id") or path.stem
                meta["cwd"] = payload.get("cwd") or ""
                break
    except OSError:
        pass
    return meta


def codex_sessions() -> list[dict]:
    """List Codex session JSONL files as {head, path, session_id, cwd}, self-excluded."""
    out = []
    roots = [CODEX_SESSIONS] if CODEX_SESSIONS is not None else session_roots()
    files: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.glob("**/*.jsonl")):
            key = path.resolve(strict=False)
            if key not in seen:
                seen.add(key)
                files.append(path)
    if not files:
        return out
    resolver = RouteResolver()
    for f in files:
        meta = _codex_meta_from_file(f)
        cwd = meta["cwd"]
        if _excluded(cwd, meta["session_id"]):
            continue
        out.append(
            _with_route(
                {"head": "codex", "path": str(f), "session_id": meta["session_id"], "cwd": cwd},
                resolver=resolver,
            )
        )
    return out


def hermes_messages(
    session_id: str, since_id: int = 0, limit: int = 512, max_content_bytes: int = 65536
) -> list[dict]:
    """Return {id, role, content, timestamp} rows for one Hermes session, id > since_id.

    `active = 1` matches hermes_state.py's own default message-load filter: a /rollback
    (checkpoint restore) soft-deletes superseded messages by flipping active to 0 rather
    than removing the row, and those never became conversation the user acted on.
    """
    if not HERMES_STATE_DB.is_file():
        return []
    rows = _hermes_query(
        "SELECT id, role, CASE WHEN length(CAST(content AS BLOB)) <= ? THEN content ELSE '' END, "
        "timestamp, length(CAST(content AS BLOB)) FROM messages "
        "WHERE session_id = ? AND id > ? AND active = 1 ORDER BY id LIMIT ?",
        (max_content_bytes, session_id, since_id, limit),
    )
    if not rows:
        return []
    return [
        {"id": r[0], "role": r[1], "content": r[2], "timestamp": r[3], "content_bytes": r[4] or 0}
        for r in rows
    ]


def all_sessions() -> list[dict]:
    """All discoverable sessions across heads."""
    return claude_sessions() + hermes_sessions() + codex_sessions()


def claude_memory_files() -> list[dict]:
    """List personal-memory markdown files as {head, path, cwd}, self-excluded.

    One file per durable memory a head chose to keep, under
    `~/.claude/projects/<project>/memory/*.md`. `MEMORY.md` is the index for that
    memory, not a fact — skipped everywhere, not just for excluded projects.
    """
    out = []
    if not CLAUDE_PROJECTS.is_dir():
        return out
    resolver = RouteResolver()
    for proj in sorted(CLAUDE_PROJECTS.iterdir()):
        if not proj.is_dir():
            continue
        mem_dir = proj / "memory"
        if not mem_dir.is_dir():
            continue
        if _excluded_dirname(proj.name):
            continue
        cwd = _cwd_from_claude_dir(proj.name)
        session_files = sorted(proj.glob("*.jsonl"))
        if session_files:
            cwd = _cwd_from_file(session_files[0], cwd)
        if _excluded(cwd):
            continue
        for f in sorted(mem_dir.glob("*.md")):
            if f.name == "MEMORY.md":
                continue
            out.append(_with_route({"head": "claude", "path": str(f), "cwd": cwd}, resolver=resolver))
    return out


def hermes_memory_files() -> list[dict]:
    """List Hermes's built-in personal-memory files (MEMORY.md, USER.md) as {head, path, cwd}.

    Unlike Claude, Hermes keeps ONE global pair of files for the whole install (see
    tools/memory_tool.py in hermes-agent: MemoryStore reads/writes `<hermes home>/memories/
    {MEMORY,USER}.md`, entries delimited by a "section sign" separator line) -- not scoped
    per-project, so there is no cwd to self-exclude on here. cwd is reported as "" (global);
    the curator applies its usual durable-fact bar to judge relevance instead of a
    project-path filter.
    """
    out = []
    if not HERMES_MEMORY_DIR.is_dir():
        return out
    resolver = RouteResolver()
    for name in ("MEMORY.md", "USER.md"):
        f = HERMES_MEMORY_DIR / name
        if f.is_file():
            out.append(
                _with_route(
                    {"head": "hermes", "path": str(f), "cwd": ""}, resolver=resolver, global_source=True
                )
            )
    return out


def all_memory_files() -> list[dict]:
    """All discoverable personal-memory files across heads."""
    return claude_memory_files() + hermes_memory_files()


if __name__ == "__main__":
    for s in all_sessions():
        print(s["head"], s["session_id"], s["cwd"], s["path"])
    for m in all_memory_files():
        print(m["head"], "memory", m["cwd"], m["path"])
