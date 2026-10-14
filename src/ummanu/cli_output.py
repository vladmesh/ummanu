"""Shared JSON rendering for CLI handlers with deliberately distinct layouts.

A refusal is one line, ``{"error": {...}}``. Its stream and its exit status stay each command
group's own: a bare group answers its usage refusal on stdout with status 2, and a group's typed
errors keep the stream they have always used. Only the rendering lives here.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping
from typing import Any, TextIO

EXIT_USAGE = 2


def print_json(
    payload: Any, *, indent: int | None = None, compact: bool = False, sort_keys: bool = True
) -> None:
    separators = (",", ":") if compact else None
    print(json.dumps(payload, ensure_ascii=False, indent=indent, separators=separators, sort_keys=sort_keys))


def error_line(error: Mapping[str, Any], *, compact: bool = False, sort_keys: bool = False) -> str:
    """The refusal envelope as one line, escaped to ASCII as every CLI refusal has been."""
    separators = (",", ":") if compact else None
    return json.dumps({"error": error}, separators=separators, sort_keys=sort_keys)


def print_error(
    error: Mapping[str, Any],
    *,
    stderr: bool = False,
    compact: bool = False,
    sort_keys: bool = False,
) -> None:
    """Print one refusal envelope; the stream is looked up at call time so redirection holds."""
    stream: TextIO = sys.stderr if stderr else sys.stdout
    print(error_line(error, compact=compact, sort_keys=sort_keys), file=stream)


def usage_handler(message: str) -> Callable[[argparse.Namespace], int]:
    """The handler of a bare command group: a usage refusal on stdout and status 2."""

    def handler(_args: argparse.Namespace) -> int:
        print_error({"code": "usage", "message": message})
        return EXIT_USAGE

    return handler
