"""Explicit service display and append-only sprint comment positions.

Positions belong to an actual session and sprint, never to a timestamp. Pending
accepted queue inputs and claimed feed entries own the boundary; rendering does
not. Released inputs without metadata retain their original expanded readback.
"""

from __future__ import annotations

from typing import Any

EXCERPT_LIMIT = 1500


def excerpt(body: str) -> str:
    body = body.strip() or "(empty)"
    if len(body) <= EXCERPT_LIMIT:
        return body
    return body[:EXCERPT_LIMIT] + "\n[Truncated excerpt; read the full body with the native show command below.]"


def comment_position(metadata: dict[str, Any] | None, sprint_ref: str) -> int:
    if not isinstance(metadata, dict) or metadata.get("sprint_ref") != sprint_ref:
        return 0
    position = metadata.get("comment_position")
    return position if type(position) is int and position >= 0 else 0


def comments_note(sprint_ref: str, comments: list[dict[str, str]], start: int) -> str:
    lines = [f"## New comments of {sprint_ref}, in board order", ""]
    for index, comment in enumerate(comments[start:], start + 1):
        lines += [f"### Comment {index}: {comment.get('created_at') or 'undated'}", "",
                  excerpt(comment.get("body") or ""), ""]
    if start >= len(comments):
        lines.append("(none)")
    lines += ["", f"Full comments: `python3 -P -m ummanu sprint show --ref {sprint_ref}`."]
    return "\n".join(lines)
