"""Memory model settings and index identity, without importing the embedding runtime."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ummanu.memory import DEFAULT_MODEL

DEFAULT_DIM = 1024
DEFAULT_THREADS = 1


@dataclass(frozen=True)
class MemoryConfig:
    model: str = DEFAULT_MODEL
    dim: int = DEFAULT_DIM
    threads: int = DEFAULT_THREADS


def memory_config(host: Mapping[str, Any]) -> MemoryConfig:
    """Resolve the schema-validated host settings shared by rebuilds and the service."""
    return MemoryConfig(
        model=host.get("memory_model", DEFAULT_MODEL),
        dim=host.get("memory_dim", DEFAULT_DIM),
        threads=host.get("memory_threads", DEFAULT_THREADS),
    )


def index_matches(path: Path, config: MemoryConfig) -> bool:
    """A recovery retry may reuse only an index bearing this model and dimension.

    Missing, legacy and unreadable indexes are derived state that recovery can rebuild. Read only
    the SQLite metadata table: importing ``memory_service`` here would load the embedding stack in
    the parent that must release its memory before starting the service.
    """
    try:
        if not path.is_file():
            return False
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=1)) as conn:
            rows = conn.execute(
                "SELECT key, value FROM index_metadata WHERE key IN ('model', 'dimension')"
            ).fetchall()
        return len(rows) == 2 and dict(rows) == {"model": config.model, "dimension": str(config.dim)}
    except (OSError, sqlite3.Error):
        return False
