"""Derived card membership, changed only by RunStore while holding .runs.lock.

A small completeness token fences reads across writes. Each card has a membership document
and a separate presence/checksum witness, both addressed by SHA-256 of its ref. A missing half
is unavailable; a complete lookup with neither half proves absence under the writer protocol.
Canonical records remain the authority. These helpers neither observe nor settle a run.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from ummanu._fsutil import fsync_directory
from ummanu.webproto.store_io import RunStoreError, write_document


def digest(ref: str) -> str:
    return hashlib.sha256(ref.encode("utf-8")).hexdigest()


class RefLookup:
    """RunStore's file helpers, with no I/O in construction and no autonomous recovery."""

    def __init__(self, root: Path) -> None:
        self.root = root / "refs"
        self.cards = self.root / "cards"
        self.present = self.root / "present"
        self.marker = self.root / "complete.json"

    def _read(self, path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RunStoreError(f"the product run ref lookup at {path} is unavailable: {exc}") from None
        if not isinstance(value, dict):
            raise RunStoreError(f"the product run ref lookup at {path} is not an object")
        return value

    def token(self) -> str:
        value = self._read(self.marker)
        token = value.get("token")
        if (
            value.get("schema_version") != 1
            or value.get("complete") is not True
            or not (
                isinstance(token, str) and len(token) == 32 and all(c in "0123456789abcdef" for c in token)
            )
        ):
            raise RunStoreError("the product run ref lookup requires explicit preparation/recovery")
        if not self.cards.is_dir() or not self.present.is_dir():
            raise RunStoreError("the product run ref lookup directories are unavailable")
        return token

    def mark(self, *, complete: bool) -> None:
        self._write(self.marker, {"schema_version": 1, "complete": complete, "token": uuid.uuid4().hex})
        # The refs directory's own entry must survive before a canonical write follows dirty.
        fsync_directory(self.root.parent)

    def _path(self, key: str) -> Path:
        return self.cards / f"{key}.json"

    def _witness(self, key: str) -> Path:
        return self.present / f"{key}.json"

    @staticmethod
    def _exists(path: Path) -> bool:
        try:
            path.lstat()
        except FileNotFoundError:
            return False
        return True

    def ids(self, ref: str) -> list[str]:
        key = digest(ref)
        path, witness = self._path(key), self._witness(key)
        has_card, has_witness = self._exists(path), self._exists(witness)
        if not has_card and not has_witness:
            return []
        if has_card != has_witness:
            raise RunStoreError("the product run ref lookup has missing card membership/presence")
        node, presence = self._read(path), self._read(witness)
        ids = node.get("run_ids")
        if (
            node.get("ref") != ref
            or presence.get("ref") != ref
            or presence.get("sha256") != self._checksum(node)
        ):
            raise RunStoreError("the product run ref lookup has corrupt card membership/presence")
        if (
            not isinstance(ids, list)
            or any(
                not isinstance(item, str) or not item or "/" in item or item in (".", "..") for item in ids
            )
            or ids != sorted(set(ids))
        ):
            raise RunStoreError("the product run ref lookup has invalid card/run membership")
        return ids

    def rebuild(self, groups: dict[str, list[str]]) -> None:
        """Replace membership from canonical groups while the owner holds an incomplete marker."""
        self.cards.mkdir(parents=True, exist_ok=True)
        self.present.mkdir(parents=True, exist_ok=True)
        expected = {digest(ref) + ".json" for ref in groups}
        for directory in (self.cards, self.present):
            for path in directory.iterdir():
                if path.name not in expected:
                    path.unlink()
        for ref, ids in groups.items():
            self.put(ref, ids)
        for directory in (self.cards, self.present):
            fsync_directory(directory)

    def put(self, ref: str, ids: list[str]) -> None:
        """Update just this card's paired membership while the marker is incomplete."""
        node = {"ref": ref, "run_ids": sorted(ids)}
        key = digest(ref)
        self._write(self._path(key), node)
        self._write(self._witness(key), {"ref": ref, "sha256": self._checksum(node)})

    @staticmethod
    def _checksum(value: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()

    @staticmethod
    def _write(path: Path, value: dict[str, Any]) -> None:
        write_document(path, json.dumps(value, sort_keys=True), durable=True)
