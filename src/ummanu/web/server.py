"""The socket half: standard-library `ThreadingHTTPServer`, bound to loopback and nothing else.

No web framework is a dependency. Security: the service has no password, TLS or authorisation and
its POST routes start real heads, so a non-loopback bind is refused in code. The guarded front
(:mod:`ummanu.webfront`) terminates TLS, checks a password and proxies to loopback; this refusal is
what makes it the only way in from off-host. Never relax it. See docs/PROTOCOLS.md, "Serving the
pipeline locally" and "Publishing the pipeline: the guarded front".
"""

from __future__ import annotations

import ipaddress
import socket
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from uuid import uuid4

from ummanu.web.app import MAX_BODY_BYTES, WebApp

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787

#: Why a non-loopback bind is refused, quoted verbatim into the refusal and into OPERATIONS.md.
LOOPBACK_ONLY = (
    "this service has no password, no TLS and no authorisation, and its routes start real heads on "
    "this installation, so it binds a loopback address only. External access is published by the "
    "guarded front instead (`ummanu web-front`, DoD 5), which terminates TLS, checks a password "
    "and proxies here; this refusal is what makes that front the only way in"
)


#: The one line every answered request writes to stderr (the service journal): client, method,
#: target, status and duration. The duration covers the body read, `WebApp.handle` and the write.
REQUEST_LINE = "{client} {method} {target} {status} {duration:.1f}ms"


#: How many innermost frames an unhandled failure is logged with: enough to locate it, bounded.
LOGGED_FRAMES = 5


def _frames(exc: BaseException) -> str:
    """The innermost call sites of a failure and the classes it was wrapped in, without messages.

    Messages are left out: an unexpected failure's text is unaudited and may quote config, a request
    or a credential. Frames (file, line, function) are product-side facts.
    """
    parts = [
        f"{frame.filename}:{frame.lineno} in {frame.name}"
        for frame in traceback.extract_tb(exc.__traceback__)[-LOGGED_FRAMES:]
    ]
    chain: list[str] = []
    cause = exc.__cause__ or exc.__context__
    while cause is not None and len(chain) < LOGGED_FRAMES:
        chain.append(type(cause).__name__)
        cause = cause.__cause__ or cause.__context__
    if chain:
        parts.append("caused by " + " <- ".join(chain))
    return "; ".join(parts) or "no frames"


class LoopbackOnly(Exception):
    """A bind this transport refuses. Raised before a socket exists, never after."""


class _BodyRefused(ValueError):
    """A body whose framing or size prevents safe reuse of the connection."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def resolve_bind(host: str) -> tuple[int, str]:
    """The socket family and the literal address to bind, or a refusal, before any socket exists.

    Security: the name is resolved here (a host may map `localhost` to a routable address) and every
    resolved address must be loopback. The literal address is what gets bound, so nothing re-resolves
    the name between the check and the socket.
    """
    candidate = (host or "").strip() or DEFAULT_HOST
    literal = candidate.strip("[]")
    try:
        infos = socket.getaddrinfo(literal, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise LoopbackOnly(f"{host!r} does not resolve to an address ({exc}): {LOOPBACK_ONLY}") from None
    if not infos:
        raise LoopbackOnly(f"{host!r} does not resolve to an address: {LOOPBACK_ONLY}")
    resolved: list[tuple[int, str]] = []
    for family, _socktype, _proto, _canonname, sockaddr in infos:
        if family not in (socket.AF_INET, socket.AF_INET6):
            raise LoopbackOnly(f"{host!r} is not an IP address: {LOOPBACK_ONLY}")
        found = str(sockaddr[0]).partition("%")[0]
        try:
            address = ipaddress.ip_address(found)
        except ValueError:
            raise LoopbackOnly(f"{host!r} resolves to {found!r}, which is not an address: {LOOPBACK_ONLY}") from None
        if not address.is_loopback:
            raise LoopbackOnly(
                f"{host!r} resolves to {found}, which is not a loopback address: {LOOPBACK_ONLY}"
            )
        resolved.append((family, found))
    return resolved[0]


def check_bind(host: str) -> str:
    """The literal loopback address to bind, or a refusal. See :func:`resolve_bind`."""
    return resolve_bind(host)[1]


class _Handler(BaseHTTPRequestHandler):
    """The thinnest adapter: request line in, `WebApp.handle` out.

    Statuses, bodies and the cross-origin check (headers are handed over unread) all belong to the
    application. The one thing decided here is an unexpected exception, answered by :meth:`_contain`
    as a bounded 500 instead of a closed socket.
    """

    protocol_version = "HTTP/1.1"
    server_version = "ummanu-web"
    sys_version = ""

    #: The status `_write` answered with; 0 means the answer never reached the socket.
    _answered_status = 0
    #: When this request started, and whether its one line has been written yet.
    _request_started: float | None = None
    _request_logged = False

    def do_GET(self) -> None:  # the base class names the verbs
        self._answer("GET")

    def do_POST(self) -> None:
        self._answer("POST")

    def do_HEAD(self) -> None:
        self._answer("GET", head=True)

    def parse_request(self) -> bool:
        """Start this request's clock where the request itself starts.

        Not in `handle_one_request`: idle keep-alive time would be counted. Not in :meth:`_answer`:
        requests `http.server` refuses by itself never reach it and are logged from :meth:`send_error`.
        """
        self._request_started = time.perf_counter()
        self._request_logged = False
        self._answered_status = 0
        return super().parse_request()

    def _answer(self, method: str, *, head: bool = False) -> None:
        # Every exit (early returns, containment) is an answered request: one request, one line.
        try:
            self._answer_body(method, head=head)
        finally:
            self._log_answer(self._answered_status)

    def _answer_body(self, method: str, *, head: bool = False) -> None:
        path, _, query = self.path.partition("?")
        try:
            body = self._read_body()
        except _BodyRefused as exc:
            # Unread bytes must never be parsed as a second request on this connection.
            self.close_connection = True
            self._write(
                exc.status,
                str(exc).encode("utf-8"),
                "text/plain; charset=utf-8",
                head=head,
                extra={"Connection": "close"},
            )
            return
        try:
            response = self.server.app.handle(method, path, query=query, body=body, headers=self.headers)
        except Exception as exc:  # noqa: BLE001 - the containment boundary; see _contain
            self._contain(exc, head=head)
            return
        self._write(response.status, response.body, response.content_type, head=head, extra=response.headers)

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """Log a status `http.server` decided by itself (501, 400, 431), which never reaches `_answer`."""
        super().send_error(code, message, explain)
        self._log_answer(int(code))

    def _log_answer(self, status: int) -> None:
        """The per-request line: the verb as sent, the target, the status and the duration.

        `self.command`, not the verb given to the application, because HEAD is answered via GET. The
        flag keeps it to one line when `send_error` is reached from inside the application path.
        """
        if self._request_logged:
            return
        self._request_logged = True
        started = self._request_started
        print(
            REQUEST_LINE.format(
                client=self.address_string(),
                method=self.command or "-",
                target=getattr(self, "path", "") or "-",
                status=status,
                duration=0.0 if started is None else (time.perf_counter() - started) * 1000.0,
            ),
            file=sys.stderr,
        )

    def _contain(self, exc: BaseException, *, head: bool) -> None:
        """Answer an escaped application exception as a complete 500 instead of a closed socket.

        Not a second code-to-status table: deliberate refusals return a `Response`. Without this,
        `http.server` closes the connection with no response. Written through `_write`, so security
        headers and `Content-Length` apply and the connection stays usable. Security: the body carries
        only a fixed sentence, the exception class and a reference that joins it to the logged frames;
        never the message, request, config or installation data.
        """
        reference = uuid4().hex[:12]
        print(
            f"{self.address_string()} unhandled {type(exc).__name__} ref={reference} at {_frames(exc)}",
            file=sys.stderr,
        )
        body = (
            f"ummanu web: this request could not be answered.\n"
            f"An unexpected {type(exc).__name__} escaped the application.\n"
            f"reference: {reference}\n"
            f"The service journal holds the failing call site under this reference "
            f"(journalctl -u ummanu-web.service).\n"
        ).encode()
        self._write(500, body, "text/plain; charset=utf-8", head=head)

    def _read_body(self) -> bytes:
        if self.headers.get_all("Transfer-Encoding"):
            raise _BodyRefused("this service does not accept Transfer-Encoding")
        lengths = self.headers.get_all("Content-Length") or []
        if len(lengths) > 1:
            raise _BodyRefused("this request declares more than one Content-Length")
        declared = lengths[0].strip() if lengths else "0"
        if not declared.isascii() or not declared.isdecimal():
            raise _BodyRefused("this request declares an invalid Content-Length")
        try:
            length = int(declared)
        except ValueError:
            raise _BodyRefused("this request declares an invalid Content-Length") from None
        if length > MAX_BODY_BYTES:
            raise _BodyRefused(
                f"this request body is larger than the {MAX_BODY_BYTES} bytes accepted here",
                413,
            )
        return self.rfile.read(length) if length > 0 else b""

    def _write(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        head: bool,
        extra: dict[str, str] | None = None,
    ) -> None:
        self._answered_status = status
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        # Pages are one self-contained file each and load nothing external. `form-action 'self'` lets
        # the sprint form submit to this service and nowhere else.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'",
        )
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def log_request(self, code: Any = "-", size: Any = "-") -> None:  # the base class names these
        """Nothing: :meth:`_log_answer` writes the one line, with a duration this hook cannot know."""

    def log_message(self, format: str, *args: Any) -> None:  # the base class names this argument
        """Stderr, for what the base class reports outside an answered request (bad line, timeout)."""
        print(f"{self.address_string()} {format % args}", file=sys.stderr)


class WebServer(ThreadingHTTPServer):
    """A threading server that carries the application it answers from."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self, address: tuple[str, int], app: WebApp, *, family: int = socket.AF_INET
    ) -> None:
        self.app = app
        self.address_family = family
        super().__init__(address, _Handler)


def build_server(app: WebApp, *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> WebServer:
    """A bound server, or a refusal — and the refusal comes before the socket."""
    family, address = resolve_bind(host)
    return WebServer((address, int(port)), app, family=family)


def serve(app: WebApp, *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> int:
    """Bind, announce where to point a browser, and serve until interrupted."""
    server = build_server(app, host=host, port=port)
    bound_host, bound_port = server.server_address[0], server.server_address[1]
    shown = f"[{bound_host}]" if ":" in str(bound_host) else bound_host
    print(f"ummanu web on http://{shown}:{bound_port} — {LOOPBACK_ONLY}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopping", file=sys.stderr)
    finally:
        server.shutdown()
        server.server_close()
    return 0
