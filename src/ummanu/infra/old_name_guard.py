"""The old product name guard: one matcher, two allowlists.

docs/RENAME.md, "Guard". A path or a line naming `secretary` in any case passes only when a row of the
caller's allowlist covers the match. The product's tree is guarded by `tests/test_old_name_guard.py`
with its own table; the live root is guarded by `ummanu config check` with `LIVE_ROOT_ALLOWLIST`
below, ported from the instance repository's retired `tests/test_old_name_guard.py`: every row
verbatim except the receipt rows the comment at its end names.

This module names the old name on purpose: it is the guard's own source, the one self-exemption of
the product guard's table.
"""

from __future__ import annotations

import fnmatch
import re

#: The retained instance repository and historical live root; never an operational default.
INSTANCE_PROJECT = "secretary-instance"

OLD_NAME = re.compile(r"(?i)secretary")

#: (class, what the old name may appear in, the paths where it may). A pattern of `None` allows the
#: whole file, content and path alike. `fnmatch` patterns, so `*` crosses `/`.
Row = tuple[str, re.Pattern[str] | None, tuple[str, ...]]

#: Any path.
ANYWHERE = ("*",)

#: Undated runbooks and the current plans: live instructions agents follow today, so the whole-file
#: history row below does not cover them even though they sit under `state/knowledge/`.
LIVE_KNOWLEDGE = ("state/knowledge/runbooks/[!0-9]*", "state/knowledge/plans/current-*")

#: The live root's allowlist.
LIVE_ROOT_ALLOWLIST: tuple[Row, ...] = (
    # The Hermes Telegram agent `secretary` keeps its name. Today it is named only inside the R paths
    # below (board cards, audit, knowledge reports); the row admits its own spellings and no others.
    ("H", re.compile(r"\bhermes-secretary(?:-roles)?\b|~/\.hermes/skills/secretary(?:-roles)?\b"), ANYWHERE),
    # The repository name, its project id and its cards (`old_name_guard.INSTANCE_PROJECT`, the one
    # spelling). The same name with `-maintenance` was a product unit, not this repository.
    ("I", re.compile(rf"\b{re.escape(INSTANCE_PROJECT)}\b(?!-maintenance)"), ANYWHERE),
    # A card ref of the old product, e.g. `secretary-1566` or `pipeline/secretary-1932`.
    ("R", re.compile(r"\bsecretary-\d+\b(?!\.\d)"), ANYWHERE),
    # A knowledge directory named after the old product (`projects/secretary/`, kept by D9): a path
    # reference to a record, not a live name.
    ("R", re.compile(r"\bprojects/secretary/"), ("state/knowledge/*",)),
    (
        "R",
        None,
        (
            # Card and sprint checkpoints, the board audit and event journal: written by the
            # dispatcher's checkpoint, append-only history.
            "state/board/*",
            # Brainstorms, decisions, closeouts, reports and dated runbooks: records of what was
            # decided and done at the time, under the names of that time. `LIVE_KNOWLEDGE` is not.
            "state/knowledge/*",
            # Run records and claims, appended by the checkpoint.
            "state/runs/*",
            # Curated memory facts: the curator writes them outside any gate, and a fact quotes the
            # cards, logs and paths it was learned from under the names they had.
            "state/memory/facts/*",
        ),
    ),
    # The instance guard's rows for gate and provision receipts at the root are not ported: the
    # receipts live in the data directory (`onboarding.OnboardingStorage`, their one home), the
    # export allowlist never carries them, and this guard reads only what it carries.
)


def applies(globs: tuple[str, ...], path: str) -> bool:
    return any(fnmatch.fnmatchcase(path, glob) for glob in globs)


def rows(
    allowlist: tuple[Row, ...], path: str, *, live: tuple[str, ...] = ()
) -> list[tuple[str, re.Pattern[str] | None]]:
    """The allowlist rows that apply to `path`; a path matching `live` gets no whole-file row."""
    is_live = applies(live, path)
    return [
        (cls, pattern)
        for cls, pattern, globs in allowlist
        if applies(globs, path) and not (is_live and pattern is None)
    ]


def violations(
    path: str, text: str | None, allowlist: tuple[Row, ...], *, live: tuple[str, ...] = ()
) -> list[str]:
    """Every match of the old name in `path` and in its `text` (None for a binary file) that no
    allowlist row covers, as `path:line: match in context`."""
    applying = rows(allowlist, path, live=live)
    if any(pattern is None for _, pattern in applying):
        return []
    patterns = [pattern for _, pattern in applying if pattern is not None]
    found = [f"{path}: path carries '{match}'" for match in _uncovered(path, patterns)]
    if text is None:
        return found
    for number, line in enumerate(text.splitlines(), start=1):
        found += [
            f"{path}:{number}: '{match}' in {line.strip()[:160]!r}" for match in _uncovered(line, patterns)
        ]
    return found


def live_root_violations(path: str, text: str | None) -> list[str]:
    """`violations` under the live root's allowlist, with live knowledge refused its history row."""
    return violations(path, text, LIVE_ROOT_ALLOWLIST, live=LIVE_KNOWLEDGE)


def _uncovered(text: str, patterns: list[re.Pattern[str]]) -> list[str]:
    hits = list(OLD_NAME.finditer(text))
    if not hits:
        return []
    allowed = [match.span() for pattern in patterns for match in pattern.finditer(text)]
    return [
        hit.group(0)
        for hit in hits
        if not any(start <= hit.start() and hit.end() <= end for start, end in allowed)
    ]


def text_of(data: bytes) -> str | None:
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None
