"""Serve the real web UI on loopback over synthetic data, read-only, for previews and screenshots.

    python scripts/web_preview.py --port 8797 [--host 127.0.0.1]

No instance, board store, dispatcher or head is touched: `WebApp` runs over the layers of
`web_preview_fixtures`. Every request other than GET or HEAD is refused before any route runs, and a
page whose rendering raises is answered with the error page and the failure, so the server stays up.
The bind goes through `ummanu.web.server`, which refuses any address that is not loopback.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import web_preview_fixtures as fixtures
from ummanu.web import pages
from ummanu.web.app import HTML_TYPE, Response, WebApp
from ummanu.web.server import DEFAULT_HOST, LoopbackOnly, serve

DEFAULT_PORT = 8797

#: The methods the preview answers; the rest would be writes.
READ_METHODS = frozenset({"GET", "HEAD"})


class PreviewApp(WebApp):
    """`WebApp` over synthetic layers, with writes refused and render failures shown as a page."""

    def __init__(self) -> None:
        super().__init__(
            fixtures.PreviewReads(),
            fixtures.PreviewOps(),
            fixtures.PreviewSprintReads(),
            fixtures.PreviewSprintReads(),
            fixtures.PreviewPauseReads(),
            fixtures.PreviewPauseReads(),
            fixtures.PreviewCommandReads(),
            fixtures.PreviewOps(),
            fixtures.PreviewProviderUsage(),
            fixtures.PreviewDoctor(),
            po=fixtures.PreviewPo(),
            owner_events=fixtures.PreviewOwnerEvents(),
            provider_ops=fixtures.PreviewOps(),
        )

    def handle(self, method: str, path: str, *, query: str = "", body: bytes = b"", headers: Any = None) -> Response:
        """A GET answered from the fixtures at their fixed clock; any other method refused unread."""
        with pages.render_clock(lambda: fixtures.NOW_DT):
            if method.upper() not in READ_METHODS:
                with pages.from_post(True):
                    markup = pages.error(405, "preview_read_only", fixtures.READ_ONLY)
                return Response(405, markup.encode("utf-8"), HTML_TYPE, {"Allow": "GET, HEAD"})
            try:
                return super().handle(method, path, query=query, body=body, headers=headers)
            except Exception as exc:  # noqa: BLE001 - a preview shows a broken page instead of dying
                traceback.print_exc(file=sys.stderr)
                said = f"rendering {path} raised {type(exc).__name__}: {exc}"
                return Response(500, pages.error(500, "preview_render_failed", said).encode("utf-8"), HTML_TYPE)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default=DEFAULT_HOST, help="a loopback address (default %(default)s)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="default %(default)s")
    args = parser.parse_args(argv)
    try:
        return serve(PreviewApp(), host=args.host, port=args.port)
    except LoopbackOnly as exc:
        print(f"web preview: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
