"""HTTP framing and source-read failures preserve their respective boundaries."""

from __future__ import annotations

import io
import json
import socket
import tempfile
import unittest
from http.client import HTTPConnection, HTTPResponse
from pathlib import Path
from threading import Thread
from unittest import mock

from ummanu.web import pages
from ummanu.web.app import MAX_BODY_BYTES, Response
from ummanu.web.server import build_server
from ummanu.webproto import command_reads, pause_reads
from ummanu.webproto.reads import ReadLayer
from ummanu.webproto.section import read_source
from ummanu.webproto.sprint_reads import SprintReadLayer


class BodyFramingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = mock.Mock()
        self.app.handle.return_value = Response(200, b"handled", "text/plain")
        self.enterContext(mock.patch("sys.stderr", io.StringIO()))
        self.server = build_server(self.app, port=0)
        self.addCleanup(self.server.server_close)
        thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(self.server.shutdown)

    def refused(self, headers: bytes, *, status: int) -> None:
        sock = socket.create_connection(self.server.server_address, timeout=3)
        self.addCleanup(sock.close)
        # The unread suffix would be a second request if the refused connection were reused.
        sock.sendall(
            b"POST /api/pause/drain HTTP/1.1\r\nHost: localhost\r\n"
            + headers
            + b"\r\nGET /smuggled HTTP/1.1\r\nHost: localhost\r\n\r\n"
        )
        response = HTTPResponse(sock)
        self.addCleanup(response.close)
        response.begin()
        self.assertEqual(response.status, status)
        self.assertEqual(response.getheader("Connection"), "close")
        self.assertEqual(len(response.read()), int(response.getheader("Content-Length")))
        try:
            remaining = sock.recv(1024)
        except ConnectionResetError:
            remaining = b""
        self.assertEqual(remaining, b"", "the unread suffix must not receive a second response")
        self.app.handle.assert_not_called()

    def test_an_oversized_body_closes_without_dispatching_its_suffix(self) -> None:
        self.refused(f"Content-Length: {MAX_BODY_BYTES + 1}\r\n".encode(), status=413)

    def test_invalid_or_ambiguous_framing_closes_without_dispatching(self) -> None:
        for headers in (
            b"Content-Length: -1\r\n",
            b"Content-Length: +1\r\n",
            b"Content-Length: rubbish\r\n",
            b"Content-Length: \r\n",
            b"Content-Length: 1, 1\r\n",
            b"Content-Length: 1\r\nContent-Length: 1\r\n",
            b"Content-Length: 1\r\nContent-Length: 2\r\n",
            b"Transfer-Encoding: chunked\r\n",
            b"Transfer-Encoding: chunked\r\nContent-Length: 1\r\n",
            b"Content-Length: " + b"9" * 5000 + b"\r\n",
        ):
            with self.subTest(headers=headers[:100]):
                self.refused(headers, status=400)

    def test_ordinary_requests_reuse_one_connection(self) -> None:
        connection = HTTPConnection(*self.server.server_address, timeout=3)
        self.addCleanup(connection.close)
        for body in (b"", b"x" * MAX_BODY_BYTES):
            connection.request("POST", "/api/example", body=body)
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"handled")
            sock = connection.sock
            connection.request("GET", "/")
            response = connection.getresponse()
            self.assertEqual(response.read(), b"handled")
            self.assertIs(connection.sock, sock)
        self.assertEqual(self.app.handle.call_count, 4)

    def test_a_head_refusal_has_headers_and_no_body(self) -> None:
        sock = socket.create_connection(self.server.server_address, timeout=3)
        self.addCleanup(sock.close)
        sock.sendall(b"HEAD / HTTP/1.1\r\nHost: localhost\r\nContent-Length: -1\r\n\r\n")
        response = HTTPResponse(sock, method="HEAD")
        self.addCleanup(response.close)
        response.begin()
        self.assertEqual(response.status, 400)
        self.assertEqual(response.getheader("Connection"), "close")
        self.assertEqual(response.read(), b"")
        self.assertEqual(sock.recv(1), b"")
        self.app.handle.assert_not_called()


class SourceBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state = self.data / "dispatcher" / "production-state.json"
        self.state.parent.mkdir()
        self.clock = 1_800_000_000.0

    def test_a_novel_source_failure_is_unavailable_in_both_existing_layers(self) -> None:
        class NewSourceFailure(Exception):
            pass

        for module in (pause_reads, command_reads):
            with self.subTest(layer=module.__name__):
                produce = mock.Mock(side_effect=NewSourceFailure("cannot decode source"))
                reading = module._source(
                    "state",
                    produce,
                    refusal=lambda exc: str(exc),
                    now=self.clock,
                    evidence=self.state,
                )
                self.assertFalse(reading.answered)
                self.assertIsNone(reading.value)
                self.assertEqual(reading.source.reason, "cannot decode source")
                produce.assert_called_once_with()

    def test_a_failure_while_formatting_the_refusal_is_not_hidden(self) -> None:
        with self.assertRaisesRegex(ValueError, "bad formatter"):
            read_source(
                "state",
                mock.Mock(side_effect=OSError("unreadable")),
                refusal=mock.Mock(side_effect=ValueError("bad formatter")),
                now=self.clock,
                evidence=None,
            )

    def test_an_answered_empty_source_is_available(self) -> None:
        reading = read_source("state", lambda: None, refusal=str, now=self.clock, evidence=None)
        self.assertTrue(reading.answered)
        self.assertIsNone(reading.value)

    def test_cancellation_is_not_a_source_refusal(self) -> None:
        with self.assertRaises(KeyboardInterrupt):
            read_source(
                "state",
                mock.Mock(side_effect=KeyboardInterrupt),
                refusal=str,
                now=self.clock,
                evidence=None,
            )

    def test_a_corrupt_dispatcher_record_marks_agents_and_attempt_unavailable(self) -> None:
        self.state.write_text(json.dumps({"records": {"ummanu-9": {"worker_retained_at": 1}}}))
        layer = ReadLayer(self.data)
        agents = layer._agents(self.data, now=self.clock, projects_by_ref={})
        attempt, record, source = layer._attempt("ummanu-9", self.data, now=self.clock)
        self.assertEqual(agents["source"]["state"], "unavailable")
        self.assertEqual(agents["items"], [])
        self.assertIn("production state could not be read", agents["source"]["reason"])
        self.assertIsNone(attempt)
        self.assertIsNone(record)
        self.assertEqual(source.state, "unavailable")

    def test_document_assembly_errors_propagate_after_a_successful_read(self) -> None:
        layer = ReadLayer(self.data)
        with (
            mock.patch.object(layer, "_records", return_value={"ummanu-9": object()}),
            mock.patch(
                "ummanu.webproto.reads.agent_reads.agent_rows", side_effect=RuntimeError("bad assembly")
            ),
            self.assertRaisesRegex(RuntimeError, "bad assembly"),
        ):
            layer._agents(self.data, now=self.clock, projects_by_ref={})

    def test_sprint_source_refusals_preserve_the_other_sources(self) -> None:
        layer = SprintReadLayer(self.data)
        with mock.patch(
            "ummanu.webproto.sprint_reads.require_active_sprint_projects",
            side_effect=RuntimeError("bad index"),
        ):
            reading = layer._reservations(self.data, now=self.clock)
        self.assertFalse(reading.answered)
        self.assertIn("bad index", reading.source.reason)
        self.state.write_text('{"records": {}, "observers": {}}')
        self.assertTrue(layer._production(self.data, now=self.clock).answered)
        self.state.write_text("[]")
        self.assertFalse(layer._production(self.data, now=self.clock).answered)

    def test_the_page_shell_loads_no_external_stylesheet(self) -> None:
        markup = pages._page("Test", "<p>Content</p>")
        self.assertNotIn('rel="stylesheet"', markup)
        self.assertNotIn('rel="preconnect"', markup)
        self.assertIn("system-ui", markup)
