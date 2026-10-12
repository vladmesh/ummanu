"""The one top-level key of a row's `extensions` bag (docs/BOARD_STORE.md §8.2).

Every metadata key the model does not name lives under `extensions[EXTENSION_BAG]` on `tasks`,
`products` and `issues`, and a card document read through `TaskReader` carries the same bag under
the same key.  Revision `0014_neutral_extension_bag` moved current rows onto this key; a record
written before it (a checkpoint, an archive) may still name another one, and `fold_extension_bags`
is the one rule that reads such a record.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

EXTENSION_BAG = "extra"

#: Top-level markers that sit beside the bag rather than in it (docs/BOARD_STORE.md §3.10): an
#: imported row's note of the fields the retired board never gave it.  They are not metadata.
EXTENSION_MARKERS = ("board_never_named",)


def card_bag(task: Mapping[str, Any]) -> Mapping[str, Any]:
    """A card document's bag, or an empty one when the card carries none or a malformed one."""
    extensions = task.get("extensions")
    bag = extensions.get(EXTENSION_BAG) if isinstance(extensions, Mapping) else None
    return bag if isinstance(bag, Mapping) else {}


def bag_json(task: Mapping[str, Any], key: str) -> Any:
    """A bag field holding JSON, decoded; an object is taken as is and unreadable text is None."""
    raw = card_bag(task).get(key)
    if isinstance(raw, Mapping):
        return raw
    try:
        return json.loads(str(raw or ""))
    except ValueError:
        return None


def fold_extension_bags(extensions: Any) -> dict[str, Any]:
    """`extensions` with every top-level key other than `EXTENSION_BAG` folded into that bag.

    A key whose value is an object contributes its fields; any other value is kept as one field
    named by its key.  The current bag wins a field both name, so a record already on the current
    key reads exactly as it did.  `EXTENSION_MARKERS` stay where they are.  Anything that is not an
    object reads as no bag.
    """
    if not isinstance(extensions, dict):
        return {}
    result = {key: extensions[key] for key in EXTENSION_MARKERS if key in extensions}
    folded: dict[str, Any] = {}
    for key, value in extensions.items():
        if key == EXTENSION_BAG or key in EXTENSION_MARKERS:
            continue
        if isinstance(value, dict):
            folded.update(value)
        else:
            folded[str(key)] = value
    current = extensions.get(EXTENSION_BAG)
    if isinstance(current, dict):
        folded.update(current)
    if folded:
        result[EXTENSION_BAG] = folded
    return result
