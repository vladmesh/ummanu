"""Lifecycle of the ordinary-session PO Memory identity."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ummanu.memory import access
from ummanu.memory.po_bridge import bind_operator
from ummanu.runtime.head_runtime_backends import is_legacy_record
from ummanu.runtime.head_runtimes import LOCAL_PTY_RUNTIME


class MemoryPoBridgeTests(unittest.TestCase):
    def test_bridge_owns_an_installation_wide_grant_and_cleans_it_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            binding = bind_operator(data_dir)
            self.addCleanup(binding.close)

            identity = access.resolve_token(binding.grant.token, data_dir=data_dir)
            self.assertIsInstance(identity, access.MemoryReadIdentity)
            assert isinstance(identity, access.MemoryReadIdentity)
            self.assertEqual(identity.role, "po")
            self.assertIsNone(identity.scopes)
            self.assertTrue(binding.grant_path.is_file())
            self.assertTrue(binding.pid_path.is_file())
            # Named, not defaulted: a hand-built spec would carry the legacy Orca record rule.
            self.assertEqual(binding.run.spec.runtime, LOCAL_PTY_RUNTIME)
            self.assertFalse(is_legacy_record(binding.run))

            binding.close()
            self.assertFalse(binding.grant_path.exists())
            self.assertFalse(binding.pid_path.exists())
            denial = access.resolve_token(binding.grant.token, data_dir=data_dir)
            self.assertIsInstance(denial, access.MemoryAccessDenial)
            assert isinstance(denial, access.MemoryAccessDenial)
            self.assertEqual(denial.code, "runtime_identity_unknown")


class MemoryPoBridgeProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_server_advertises_only_memory_read_tools_and_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindings = Path(tmp) / "access-grants"
            env = {
                **os.environ,
                access.MEMORY_ACCESS_BINDINGS_ENV: str(bindings),
            }
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "ummanu.memory.po_bridge"],
                env=env,
            )
            async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                self.assertEqual(
                    {tool.name for tool in tools.tools},
                    {"memory_get", "memory_list", "memory_search"},
                )
                self.assertEqual(len(list(bindings.glob("*.json"))), 1)
                self.assertEqual(len(list((bindings / "po-bridges").glob("*.pid"))), 1)

            self.assertEqual(list(bindings.glob("*.json")), [])
            self.assertEqual(list((bindings / "po-bridges").glob("*.pid")), [])


if __name__ == "__main__":
    unittest.main()
