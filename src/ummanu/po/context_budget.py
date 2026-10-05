"""A deterministic budget proxy shared by both PO CLIs, not provider window usage."""

from collections.abc import Mapping, Iterable
from typing import Any
import hashlib

DEFAULT_CONTEXT_BUDGET_BYTES = 262144
CONTEXT_METRIC = "UTF-8 bytes of stored conversation"


def context_budget_bytes(config: Mapping[str, Any] | None) -> int:
    if config is not None and not isinstance(config, Mapping):
        raise ValueError("instance configuration must be an object")
    po = (config or {}).get("po", {})
    if not isinstance(po, Mapping):
        raise ValueError("po configuration must be an object")
    value = po.get("context_budget_bytes", DEFAULT_CONTEXT_BUDGET_BYTES)
    if type(value) is not int or value <= 0:
        raise ValueError("po.context_budget_bytes must be a positive integer")
    return value


def conversation_bytes(feed: Iterable[Any]) -> int:
    # Native committed rows, including historical rows without metadata. Never count
    # billing, pending requests, or a replay of the same entry a second time.
    return sum(len(entry.text.encode("utf-8")) for entry in feed)


def rollover_request_id(sprint_ref: str, predecessor: str) -> str:
    identity = f"{sprint_ref}\n{predecessor}".encode("utf-8")
    return "po-context-rollover:" + hashlib.sha256(identity).hexdigest()


def byte_excerpt(text: str, limit: int) -> str:
    if len(text.encode("utf-8")) <= limit:
        return text
    marker = "\n[Excerpt; read the full native source above.]"
    return text.encode("utf-8")[:max(0, limit - len(marker))].decode("utf-8", errors="ignore") + marker
