"""The head sandbox: which roots a confined head gets, which connects it may make, and the kernel's word.

The policy and broker tests are pure. `KernelConfinementTests` runs the launcher for real and is
skipped on a kernel without Landlock or seccomp user notification.
"""

from __future__ import annotations

import ipaddress
import json
import os
import shlex
import socket
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ummanu.runtime.head import sandbox

REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = REPO_ROOT / "src" / "ummanu" / "runtime" / "head" / "sandbox.py"


def unix_address(path: str) -> bytes:
    return struct.pack("<H", socket.AF_UNIX) + path.encode() + b"\0"


def inet_address(host: str, port: int) -> bytes:
    return struct.pack("<H", socket.AF_INET) + struct.pack(">H", port) + ipaddress.IPv4Address(host).packed


def inet6_address(host: str, port: int) -> bytes:
    packed = ipaddress.IPv6Address(host).packed
    return struct.pack("<H", socket.AF_INET6) + struct.pack(">H", port) + b"\0" * 4 + packed + b"\0" * 4


class Layout(unittest.TestCase):
    """A home with a project repository, a linked worktree, an installation and a data directory."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.home = self.root / "home"
        self.repo = self.home / "projects" / "app"
        self.common = self.repo / ".git"
        self.admin = self.common / "worktrees" / "card-1"
        self.workspace = self.home / "data" / "workspaces" / "app" / "card-1"
        self.instance = self.home / "data" / "instance"
        self.data = self.home / "data"
        for path in (self.admin, *(self.common / name for name in ("objects", "refs", "logs"))):
            path.mkdir(parents=True)
        (self.admin / "commondir").write_text("../..\n", encoding="utf-8")
        self.workspace.mkdir(parents=True)
        (self.workspace / ".git").write_text(f"gitdir: {self.admin}\n", encoding="utf-8")
        (self.instance / "heads").mkdir(parents=True)
        (self.instance / "board-store.env").write_text(
            "UMMANU_DB_HOST=127.0.0.1\nUMMANU_DB_PORT=5432\n", encoding="utf-8"
        )
        (self.home / "secrets").mkdir()
        self.product = self.root / "product"
        self.product.mkdir()

    def policy(self, **kwargs) -> sandbox.Policy:
        options = {
            "home": self.home,
            "environ": {"UMMANU_INSTANCE": str(self.instance)},
            "product_root": self.product,
            **kwargs,
        }
        return sandbox.build_policy(self.workspace, **options)


class PolicyTests(Layout):
    def test_a_worktree_head_reads_the_git_store_but_not_the_main_checkout(self) -> None:
        policy = self.policy()
        self.assertIn(self.common, policy.read)
        self.assertNotIn(self.repo, policy.read)
        self.assertEqual(
            [path for path in policy.write if path.is_relative_to(self.common)],
            [self.admin, *(self.common / name for name in ("objects", "refs", "logs"))],
        )
        self.assertIn(self.workspace, policy.write)

    def test_installation_configuration_is_read_and_its_state_is_not(self) -> None:
        policy = self.policy()
        self.assertIn(self.instance / "heads", policy.read)
        self.assertIn(self.instance / "board-store.env", policy.read)
        self.assertFalse(any(self.instance.is_relative_to(root) for root in policy.read))
        self.assertEqual(policy.loopback_ports, {5432, sandbox.MEMORY_PORT})

    def test_the_board_locks_are_the_only_data_directory_writes(self) -> None:
        sandbox.prepare_data_locks(self.data)
        policy = self.policy(data_dir=self.data)
        expected = {self.data / name for name in (*sandbox.DATA_WRITE_FILES, *sandbox.DATA_WRITE_DIRS)}
        written = {path for path in policy.write if path.is_relative_to(self.data / "board")}
        written |= {path for path in policy.write if path.is_relative_to(self.data / "dispatcher")}
        self.assertEqual(written, expected)
        self.assertTrue(all(path.exists() for path in expected))

    def test_a_root_that_holds_the_home_is_refused(self) -> None:
        for extra in ([self.home], [self.root], [Path("/")]):
            with self.subTest(extra=extra), self.assertRaisesRegex(sandbox.SandboxError, "holds the home"):
                self.policy(extra_read=extra)

    def test_a_workspace_that_is_the_home_is_refused(self) -> None:
        with self.assertRaisesRegex(sandbox.SandboxError, "holds the home"):
            sandbox.build_policy(self.home, home=self.home, environ={}, product_root=self.product)

    def test_a_named_read_root_must_exist_to_count(self) -> None:
        policy = self.policy(extra_read=[self.root / "absent"])
        self.assertNotIn(self.root / "absent", policy.read)


class BrokerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.write = Path("/srv/work/card-1")
        self.broker = object.__new__(sandbox.Broker)
        self.broker.policy = sandbox.Policy(read=(), write=(self.write,), loopback_ports=frozenset({5432}))
        self.broker.root_pid = os.getpid()

    def test_unix_sockets_inside_a_write_root_only(self) -> None:
        self.assertTrue(self.broker.permits(unix_address(f"{self.write}/s"), "/"))
        self.assertTrue(self.broker.permits(unix_address("s"), str(self.write)))
        self.assertTrue(self.broker.permits(unix_address(sandbox.ALLOWED_SOCKETS[0]), "/"))
        for path in ("/run/docker.sock", "/run/user/1000/bus", f"{self.write}/../other/s"):
            with self.subTest(path=path):
                self.assertFalse(self.broker.permits(unix_address(path), "/"))

    def test_abstract_unix_sockets_are_refused(self) -> None:
        self.assertFalse(self.broker.permits(struct.pack("<H", socket.AF_UNIX) + b"\0name", "/"))

    def test_loopback_reaches_listed_ports_and_the_network_is_open(self) -> None:
        with mock.patch.object(sandbox, "sandbox_listening_ports", return_value=set()):
            self.assertTrue(self.broker.permits(inet_address("127.0.0.1", 5432), "/"))
            self.assertFalse(self.broker.permits(inet_address("127.0.0.1", 8787), "/"))
            self.assertFalse(self.broker.permits(inet_address("0.0.0.0", 8787), "/"))
            self.assertFalse(self.broker.permits(inet6_address("::1", 8787), "/"))
            self.assertFalse(self.broker.permits(inet6_address("::ffff:127.0.0.1", 8787), "/"))
            self.assertTrue(self.broker.permits(inet_address("140.82.112.3", 443), "/"))
            self.assertTrue(self.broker.permits(inet6_address("2606:4700::6810:84e5", 443), "/"))

    def test_a_listener_inside_the_sandbox_is_reachable(self) -> None:
        with mock.patch.object(sandbox, "sandbox_listening_ports", return_value={41000}):
            self.assertTrue(self.broker.permits(inet_address("127.0.0.1", 41000), "/"))


class LaunchStateTests(Layout):
    def test_the_child_loses_owner_credentials_and_gets_its_own_dirs(self) -> None:
        environ = {"CLAUDE_CODE_OAUTH_TOKEN": "t", "SSH_AUTH_SOCK": "/s", "UMMANU_MEMORY_ACCESS_TOKEN": "m"}
        env = sandbox.child_environment(environ, claude_home=self.root / "claude", tmp=self.root / "tmp")
        self.assertEqual(
            env,
            {
                "UMMANU_MEMORY_ACCESS_TOKEN": "m",
                "TMPDIR": str(self.root / "tmp"),
                "XDG_CACHE_HOME": str(self.root / "tmp" / "cache"),
                "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
            },
        )

    def test_the_claude_home_holds_trust_rules_skills_and_the_shared_transcripts(self) -> None:
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / "CLAUDE.md").write_text("rules\n", encoding="utf-8")
        skill = self.home / ".claude" / "skills" / "observe-sprint"
        skill.mkdir(parents=True)
        claude_home = self.root / "state" / "claude"
        sandbox.prepare_claude_home(claude_home, self.workspace, home=self.home, skills=[skill])
        state = json.loads((claude_home / ".claude.json").read_text(encoding="utf-8"))
        self.assertTrue(state["projects"][str(self.workspace)]["hasTrustDialogAccepted"])
        self.assertEqual((claude_home / "CLAUDE.md").read_text(encoding="utf-8"), "rules\n")
        self.assertEqual((claude_home / "skills" / "observe-sprint").resolve(), skill)
        name = sandbox.claude_project_dir_name(self.workspace)
        self.assertEqual(
            (claude_home / "projects" / name).resolve(), self.home / ".claude" / "projects" / name
        )

    def test_role_skills_come_from_the_product_and_installation_manifests(self) -> None:
        (self.product / "skills").mkdir()
        (self.product / "skills" / "manifest.toml").write_text(
            '[roles.observer]\nskills = ["observe-sprint"]\n[roles.po]\nskills = ["ummanu/grilling"]\n',
            encoding="utf-8",
        )
        (self.instance / "skills").mkdir()
        (self.instance / "skills" / "manifest.toml").write_text(
            '[roles.extra]\nskills = ["host-skill"]\n', encoding="utf-8"
        )
        self.assertEqual(
            sandbox.role_skills(self.product, str(self.instance)), {"observe-sprint", "grilling", "host-skill"}
        )

    def test_the_temp_dir_must_be_private(self) -> None:
        with mock.patch.object(sandbox, "TMP_ROOT", self.root):
            tmp = sandbox.private_tmp(self.workspace)
            self.assertEqual(tmp, sandbox.private_tmp(self.workspace))
            self.assertEqual(tmp.stat().st_mode & 0o777, 0o700)
            tmp.chmod(0o755)
            with self.assertRaisesRegex(sandbox.SandboxError, "not a private directory"):
                sandbox.private_tmp(self.workspace)


@unittest.skipUnless(
    sys.platform == "linux" and sandbox.landlock_abi() >= 1, "the kernel offers no Landlock"
)
class KernelConfinementTests(Layout):
    def launch(self, script: str) -> subprocess.CompletedProcess[str]:
        env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin"}
        return subprocess.run(
            [sys.executable, "-I", str(LAUNCHER), "--workspace", str(self.workspace),
             "--product-root", str(self.product), "--", "/bin/sh", "-c", script],
            env=env, capture_output=True, text=True, timeout=60, check=False,
        )

    def test_the_head_writes_its_workspace_and_cannot_read_the_rest_of_the_home(self) -> None:
        (self.home / "secrets" / "note").write_text("private\n", encoding="utf-8")
        (self.repo / "README").write_text("main checkout\n", encoding="utf-8")
        result = self.launch(
            f"echo made > {self.workspace}/out && cat {self.home}/secrets/note; "
            f"cat {self.repo}/README; ls {self.home}; echo x > {self.home}/planted; exit 7"
        )
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual((self.workspace / "out").read_text(encoding="utf-8"), "made\n")
        self.assertNotIn("private", result.stdout)
        self.assertNotIn("main checkout", result.stdout)
        self.assertEqual(result.stderr.count("Permission denied"), 4, result.stderr)
        self.assertFalse((self.home / "planted").exists())

    def test_a_unix_socket_outside_the_sandbox_is_refused(self) -> None:
        outside = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(outside.close)
        outside.bind(str(self.home / "secrets" / "s"))
        outside.listen()
        probe = (
            "import socket,sys\n"
            "def attempt(path):\n"
            "    s = socket.socket(socket.AF_UNIX)\n"
            "    try:\n"
            "        s.connect(path); return 'ok'\n"
            "    except OSError as exc:\n"
            "        return exc.__class__.__name__\n"
            "inside = sys.argv[2] + '/s'\n"
            "listener = socket.socket(socket.AF_UNIX); listener.bind(inside); listener.listen()\n"
            "print(attempt(sys.argv[1]), attempt(inside))\n"
        )
        result = self.launch(
            f"/usr/bin/python3 -I -c {shlex.quote(probe)} {self.home}/secrets/s {self.workspace}"
        )
        self.assertEqual(result.stdout.split(), ["PermissionError", "ok"], result.stderr)

    def test_a_home_wide_workspace_never_starts(self) -> None:
        result = subprocess.run(
            [sys.executable, "-I", str(LAUNCHER), "--workspace", str(self.home),
             "--product-root", str(self.product), "--", "/bin/true"],
            env={"HOME": str(self.home), "PATH": "/usr/bin:/bin"},
            capture_output=True, text=True, timeout=60, check=False,
        )
        self.assertEqual(result.returncode, 126)
        self.assertIn("holds the home", result.stderr)


if __name__ == "__main__":
    unittest.main()
