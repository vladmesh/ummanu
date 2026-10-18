from __future__ import annotations

import json
import os
import pwd
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ummanu.memory import access, health

try:
    from ummanu import memory_service
except ImportError:
    memory_service = None


class _Response:
    def __init__(self, status: int, body: str, *, session: str | None = None) -> None:
        self.status = status
        self._body = body.encode()
        self._session = session

    def read(self) -> bytes:
        return self._body

    def getheader(self, name: str, default=None):
        if name.lower() == "mcp-session-id":
            return self._session
        if name.lower() == "content-type":
            return "application/json"
        return default


class _Connection:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = iter(responses)
        self.calls: list[tuple[str, str, str, dict]] = []
        self.closed = False

    def request(self, method: str, path: str, *, body: str, headers: dict) -> None:
        self.calls.append((method, path, body, headers))

    def getresponse(self) -> _Response:
        return next(self.responses)

    def close(self) -> None:
        self.closed = True


class MemoryHealthWireTests(unittest.TestCase):
    def test_list_result_normalizes_each_text_block_and_structured_forms(self) -> None:
        text_rows = health._tool_value(
            {
                "result": {
                    "content": [
                        {"type": "text", "text": '{"id": 7, "scope": "product:ummanu"}'},
                        {"type": "text", "text": '[{"id": 8, "scope": "project:ummanu"}]'},
                    ]
                }
            }
        )
        self.assertEqual(
            text_rows,
            [{"id": 7, "scope": "product:ummanu"}, {"id": 8, "scope": "project:ummanu"}],
        )
        for structured in (
            [{"id": 9, "scope": "product:ummanu"}],
            {"result": [{"id": 10, "scope": "project:ummanu"}]},
        ):
            with self.subTest(structured=structured):
                expected = structured["result"] if isinstance(structured, dict) else structured
                self.assertEqual(health._tool_value({"result": {"structuredContent": structured}}), expected)

    def test_mcp_probe_authenticates_then_calls_memory_list_without_caller_or_scope(self) -> None:
        connection = _Connection(
            [
                _Response(200, '{"jsonrpc":"2.0","id":1,"result":{}}', session="session-1"),
                _Response(202, ""),
                _Response(
                    200,
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "result": {
                                "content": [
                                    {"type": "text", "text": '[{"id": 7, "scope": "product:ummanu"}]'}
                                ]
                            },
                        }
                    ),
                ),
            ]
        )
        with mock.patch("ummanu.memory.health.http.client.HTTPConnection", return_value=connection):
            rows = health._authenticated_list("launch-bound-token", port=8077, timeout_seconds=5)

        self.assertEqual(rows, [{"id": 7, "scope": "product:ummanu"}])
        self.assertTrue(connection.closed)
        self.assertEqual([json.loads(call[2])["method"] for call in connection.calls], [
            "initialize",
            "notifications/initialized",
            "tools/call",
        ])
        for sent in connection.calls:
            self.assertEqual(json.loads(sent[2])["jsonrpc"], "2.0")
        call = json.loads(connection.calls[-1][2])
        self.assertEqual(call["params"], {"name": "memory_list", "arguments": {"limit": 1}})
        self.assertEqual(connection.calls[-1][3]["Authorization"], "Bearer launch-bound-token")

    def test_returned_memory_denial_is_immediate_and_typed(self) -> None:
        connection = _Connection(
            [
                _Response(200, '{"jsonrpc":"2.0","id":1,"result":{}}', session="session-1"),
                _Response(202, ""),
                _Response(
                    200,
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "result": {
                                "structuredContent": [{"status": "denied", "error": "runtime_identity_stale"}]
                            },
                        }
                    ),
                ),
            ]
        )
        with (
            mock.patch("ummanu.memory.health.http.client.HTTPConnection", return_value=connection),
            self.assertRaisesRegex(health.MemoryProbeError, "denied.*runtime_identity_stale"),
        ):
            health._authenticated_list("launch-bound-token", port=8077, timeout_seconds=5)


class MemoryHealthIdentityTests(unittest.TestCase):
    def test_probe_uses_a_live_steward_grant_and_removes_its_temporary_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)

            def authenticated(token: str, **_kwargs):
                resolved = access.resolve_token(token, data_dir=data_dir)
                self.assertIsInstance(resolved, access.MemoryReadIdentity)
                assert isinstance(resolved, access.MemoryReadIdentity)
                self.assertEqual(resolved.role, "steward")
                self.assertEqual(resolved.scopes, frozenset({"project:ummanu", "product:ummanu"}))
                return [{"id": 1, "scope": "product:ummanu"}]

            with mock.patch("ummanu.memory.health._authenticated_list", side_effect=authenticated):
                health.probe_memory(data_dir, timeout_seconds=1, retry_seconds=0)

            self.assertFalse(list(access.bindings_dir(data_dir).glob("*.json")))
            self.assertFalse(list((access.bindings_dir(data_dir) / "health-probes").glob("*.pid")))

    def test_probe_hands_the_complete_bindings_tree_to_the_runtime_user_before_reading(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            handed_off: list[Path] = []
            with (
                mock.patch("ummanu.memory.health._authenticated_list", return_value=[{"scope": "product:ummanu"}]),
                mock.patch("ummanu._proc.os.geteuid", return_value=1000),
            ):
                health.probe_memory(
                    data_dir,
                    timeout_seconds=1,
                    retry_seconds=0,
                    runtime_handoff=handed_off.append,
                    runtime_user="memory-runtime",
                )
            self.assertEqual(handed_off, [access.bindings_dir(data_dir)])

    def test_missing_expected_steward_entry_is_a_visible_probe_failure(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch(
                "ummanu.memory.health._authenticated_list", return_value=[{"id": 1, "scope": "project:foreign"}]
            ),
            self.assertRaisesRegex(health.MemoryProbeError, "expected steward-scoped"),
        ):
            health.probe_memory(Path(tmp), timeout_seconds=0.01, retry_seconds=0)

    @unittest.skipUnless(os.geteuid() == 0, "requires a root upgrade parent")
    def test_root_probe_uses_a_runtime_user_heartbeat_resolved_by_a_separate_daemon_process(self) -> None:
        """Exercise the production cross-user shape without an installed instance.

        The root parent creates the grant, the helper drops to ``nobody`` before
        publishing its heartbeat and making the request, and a second unprivileged
        process performs the server-side grant/heartbeat resolution.  A root-owned
        heartbeat or a non-traversable access-grants tree would make this resolve
        to ``runtime_identity_stale`` instead of the allowed steward identity.
        """
        runtime = pwd.getpwnam("nobody")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root.chmod(0o755)
            data_dir = root / "data"
            data_dir.mkdir()

            def handoff(path: Path) -> None:
                for candidate in (path, *path.rglob("*")):
                    os.chown(candidate, runtime.pw_uid, runtime.pw_gid)

            def resolve_from_daemon(token: str, **_kwargs) -> list[dict]:
                pid_files = list((access.bindings_dir(data_dir) / "health-probes").glob("*.pid"))
                self.assertEqual(len(pid_files), 1)
                self.assertEqual(pid_files[0].stat().st_uid, runtime.pw_uid)
                read_fd, write_fd = os.pipe()
                child = os.fork()
                if child == 0:
                    os.close(read_fd)
                    resolved = access.resolve_token(token, data_dir=data_dir)
                    os.write(
                        write_fd,
                        json.dumps(
                            {
                                "allowed": isinstance(resolved, access.MemoryReadIdentity),
                                "code": resolved.code if isinstance(resolved, access.MemoryAccessDenial) else None,
                            }
                        ).encode(),
                    )
                    os.close(write_fd)
                    os._exit(0)
                os.close(write_fd)
                try:
                    outcome = json.loads(os.read(read_fd, 4096))
                finally:
                    os.close(read_fd)
                    os.waitpid(child, 0)
                self.assertTrue(outcome["allowed"], outcome["code"])
                return [{"scope": "product:ummanu"}]

            with mock.patch("ummanu.memory.health._authenticated_list", side_effect=resolve_from_daemon):
                health.probe_memory(
                    data_dir,
                    timeout_seconds=3,
                    retry_seconds=0,
                    runtime_handoff=handoff,
                    runtime_user=runtime.pw_name,
                )


@unittest.skipIf(memory_service is None, "ummanu[memory] is not installed")
class MemoryHealthDaemonIntegrationTests(unittest.TestCase):
    """The pinned streamable-HTTP server, not a hand-written HTTP substitute."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data_dir = self.root / "data"
        self.data_dir.mkdir()
        self.db = self.root / "index.sqlite"
        self.audit = self.root / "search-log.jsonl"
        assert memory_service is not None
        connection = memory_service.db(self.db)
        try:
            memory_service.create_schema(connection, 4)
            connection.execute(
                "INSERT INTO memories(text, scope, tags, source, created_at) VALUES (?,?,?,?,?)",
                ("portable health sentinel", "product:ummanu", None, "fixture", None),
            )
            connection.commit()
        finally:
            connection.close()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = int(listener.getsockname()[1])
        source_root = Path(health.__file__).resolve().parents[3]
        environment = {
            **os.environ,
            "PYTHONPATH": str(source_root / "src"),
            "MEMORY_PORT": str(self.port),
            "MEMORY_DB": str(self.db),
            "MEMORY_SEARCH_LOG": str(self.audit),
            access.MEMORY_ACCESS_BINDINGS_ENV: str(access.bindings_dir(self.data_dir)),
        }
        self.daemon = subprocess.Popen(
            [sys.executable, "-c", "from ummanu.memory_service import mcp; mcp.run(transport='streamable-http')"],
            cwd=source_root,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self._stop_daemon)

    def _stop_daemon(self) -> None:
        if self.daemon.poll() is None:
            self.daemon.terminate()
            try:
                self.daemon.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.daemon.kill()
                self.daemon.wait(timeout=5)

    def test_unprivileged_probe_uses_the_real_streamable_http_session_and_read_guard(self) -> None:
        # This is the ordinary runner path exercised in CI.  The root-only test
        # above covers the distinct privilege-drop handoff.
        with mock.patch("ummanu._proc.os.geteuid", return_value=1000):
            health.probe_memory(self.data_dir, port=self.port, timeout_seconds=10, retry_seconds=0.05)
        deadline = time.monotonic() + 2
        while not self.audit.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        entries = [json.loads(line) for line in self.audit.read_text(encoding="utf-8").splitlines()]
        allowed = [entry for entry in entries if entry["action"] == "memory_list" and entry["outcome"] == "allowed"]
        self.assertEqual(len(allowed), 1)
        self.assertEqual(allowed[0]["role"], "steward")
        self.assertEqual(allowed[0]["scopes"], ["product:ummanu", "project:ummanu"])
        self.assertFalse({"text", "query", "token", "capability", "grant", "token_digest"} & set(allowed[0]))

    def test_real_daemon_serializes_a_denied_list_row(self) -> None:
        """Exercise the actual list tool's denied response on the MCP wire.

        The fixture verifier only establishes an MCP session with a deliberately
        unknown grant id. The daemon's unchanged ``read_guard`` then produces
        the ordinary typed denial that the health client must parse.
        """
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            denied_port = int(listener.getsockname()[1])
        source_root = Path(health.__file__).resolve().parents[3]
        daemon = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "\n".join(
                    (
                        "import types",
                        "from mcp.server.auth.provider import AccessToken",
                        "from ummanu import memory_service",
                        "async def verify_token(token):",
                        "    return AccessToken(token=token, client_id='fixture', subject='fixture', scopes=[], claims={'memory_grant_id': '0' * 32})",
                        "memory_service.mcp._token_verifier = types.SimpleNamespace(verify_token=verify_token)",
                        "memory_service.mcp.run(transport='streamable-http')",
                    )
                ),
            ],
            cwd=source_root,
            env={**os.environ, "MEMORY_PORT": str(denied_port), "MEMORY_DB": str(self.db)},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        def stop_denied_daemon() -> None:
            if daemon.poll() is None:
                daemon.terminate()
                try:
                    daemon.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    daemon.kill()
                    daemon.wait(timeout=5)

        self.addCleanup(stop_denied_daemon)
        deadline = time.monotonic() + 5
        while True:
            try:
                health._authenticated_list("fixture", port=denied_port, timeout_seconds=1)
            except health.MemoryProbeError as exc:
                if str(exc) == "Memory MCP is unavailable" and time.monotonic() < deadline:
                    time.sleep(0.05)
                    continue
                self.assertRegex(str(exc), "denied.*runtime_identity_unknown")
                break
            else:
                self.fail("Memory MCP accepted a list read with an unknown grant")

if __name__ == "__main__":
    unittest.main()
