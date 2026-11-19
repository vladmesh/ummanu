"""The head sandbox: which roots a confined head gets, which connects it may make, and the kernel's word.

The policy and broker tests are pure. `KernelConfinementTests` runs the launcher for real and is
skipped on a kernel without Landlock or seccomp user notification.
"""

from __future__ import annotations

import errno
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
        (self.instance / "projects").mkdir()
        (self.instance / "projects" / "app.yaml").write_text(f"id: app\nrepo: {self.repo}\n", encoding="utf-8")
        (self.home / "secrets").mkdir()
        self.product = self.root / "product"
        self.product.mkdir()

    def policy(self, **kwargs) -> sandbox.Policy:
        options = {
            "home": self.home,
            "environ": {"UMMANU_INSTANCE": str(self.instance)},
            "product_root": self.product,
            "git_store": self.common,
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

    def test_the_git_store_comes_from_the_installation_not_the_workspace(self) -> None:
        store = sandbox.trusted_git_store(self.workspace, data_dir=self.data, instance=str(self.instance))
        self.assertEqual(store, self.common)
        observer = self.data / "workspaces" / "observers" / "sprint-1"
        self.assertEqual(
            sandbox.trusted_git_store(observer, data_dir=self.data, instance=str(self.instance)),
            self.data / "dispatcher" / "observer-root" / "observers" / ".git",
        )
        elsewhere = self.home / "scratch" / "card-1"
        self.assertIsNone(sandbox.trusted_git_store(elsewhere, data_dir=self.data, instance=str(self.instance)))

    def test_a_rewritten_git_pointer_opens_nothing(self) -> None:
        for target in (self.home / "secrets", self.home / ".hermes", self.common.parent):
            with self.subTest(target=target):
                (self.workspace / ".git").write_text(f"gitdir: {target}\n", encoding="utf-8")
                policy = self.policy()
                self.assertFalse(any(root.is_relative_to(self.home / "secrets") for root in policy.read))
                self.assertNotIn(self.common, policy.read)
        (self.workspace / ".git").write_text(f"gitdir: {self.admin}\n", encoding="utf-8")
        (self.admin / "commondir").write_text(f"{self.home / 'secrets'}\n", encoding="utf-8")
        self.assertIn(self.common, self.policy().read)
        self.assertNotIn(self.home / "secrets", self.policy().read)

    def test_installation_configuration_is_read_and_its_state_is_not(self) -> None:
        policy = self.policy()
        self.assertIn(self.instance / "heads", policy.read)
        self.assertIn(self.instance / "board-store.env", policy.read)
        self.assertFalse(any(self.instance.is_relative_to(root) for root in policy.read))
        self.assertEqual(policy.loopback_ports, {5432, sandbox.MEMORY_PORT, sandbox.DNS_PORT})

    def test_the_board_locks_are_the_only_data_directory_writes(self) -> None:
        sandbox.prepare_data_locks(self.data)
        policy = self.policy(data_dir=self.data)
        expected = {self.data / name for name in (*sandbox.DATA_WRITE_FILES, *sandbox.DATA_WRITE_DIRS)}
        written = {path for path in policy.write if path.is_relative_to(self.data / "board")}
        written |= {path for path in policy.write if path.is_relative_to(self.data / "dispatcher")}
        self.assertEqual(written, expected)
        self.assertTrue(all(path.exists() for path in expected))

    def test_a_root_that_holds_the_home_is_refused(self) -> None:
        for extra in ([self.home], [self.root], [Path("/")], [self.home / "secrets" / ".."]):
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
        self.broker.local = {ipaddress.IPv4Address("10.1.2.3"), ipaddress.IPv6Address("2001:db8::7")}

    def test_unix_sockets_inside_a_write_root_only(self) -> None:
        policy = self.broker.policy
        self.assertTrue(policy.socket_allowed(self.write / "s"))
        self.assertTrue(policy.socket_allowed(Path(sandbox.ALLOWED_SOCKETS[0])))
        for path in ("/run/docker.sock", "/run/user/1000/bus", "/srv/work/other/s"):
            with self.subTest(path=path):
                self.assertFalse(policy.socket_allowed(Path(path)))
        self.assertFalse(self.broker.permits(unix_address(f"{self.write}/s"), "/"))


    def test_loopback_reaches_listed_ports_and_the_network_is_open(self) -> None:
        with mock.patch.object(sandbox, "sandbox_listening_ports", return_value=set()):
            self.assertTrue(self.broker.permits(inet_address("127.0.0.1", 5432), "/"))
            self.assertFalse(self.broker.permits(inet_address("127.0.0.1", 8787), "/"))
            self.assertFalse(self.broker.permits(inet_address("0.0.0.0", 8787), "/"))
            self.assertFalse(self.broker.permits(inet6_address("::1", 8787), "/"))
            self.assertFalse(self.broker.permits(inet6_address("::ffff:127.0.0.1", 8787), "/"))
            self.assertFalse(self.broker.permits(inet_address("10.1.2.3", 8787), "/"))
            self.assertFalse(self.broker.permits(inet6_address("2001:db8::7", 8787), "/"))
            self.assertTrue(self.broker.permits(inet_address("10.1.2.3", 5432), "/"))
            for host in ("172.17.0.2", "192.168.1.10", "100.64.0.1", "169.254.169.254"):
                with self.subTest(host=host):
                    self.assertFalse(self.broker.permits(inet_address(host, 5555), "/"))
            self.assertFalse(self.broker.permits(inet6_address("fd00::2", 5555), "/"))
            self.assertTrue(self.broker.permits(inet_address("140.82.112.3", 443), "/"))
            self.assertTrue(self.broker.permits(inet6_address("2606:4700::6810:84e5", 443), "/"))

    def test_a_listener_inside_the_sandbox_is_reachable(self) -> None:
        with mock.patch.object(sandbox, "sandbox_listening_ports", return_value={41000}):
            self.assertTrue(self.broker.permits(inet_address("127.0.0.1", 41000), "/"))


class FilterTests(unittest.TestCase):
    """The cBPF program, run by a small interpreter over hand-made seccomp_data."""

    def evaluate(self, nr: int, *, arch: int | None = None, args: tuple[int, ...] = (0,) * 6) -> int:
        abi = sandbox._arch()
        data = struct.pack("<iIQ6Q", nr, abi.audit if arch is None else arch, 0, *args)
        pc, accumulator = 0, 0
        program = sandbox._connect_filter()
        while True:
            code, jt, jf, k = program[pc]
            if code == 0x20:
                accumulator = struct.unpack_from("<I", data, k)[0]
                pc += 1
            elif code == 0x06:
                return k
            else:
                taken = {0x15: accumulator == k, 0x35: accumulator >= k, 0x45: bool(accumulator & k)}[code]
                pc += 1 + (jt if taken else jf)

    def test_connect_goes_to_the_broker_and_other_calls_pass(self) -> None:
        abi = sandbox._arch()
        self.assertEqual(self.evaluate(abi.connect), sandbox.SECCOMP_RET_USER_NOTIF)
        self.assertEqual(self.evaluate(0), sandbox.SECCOMP_RET_ALLOW)
        self.assertEqual(self.evaluate(abi.sendto), sandbox.SECCOMP_RET_ALLOW)

    def test_fast_open_sends_io_uring_and_foreign_abis_are_refused(self) -> None:
        abi = sandbox._arch()
        fastopen = sandbox.MSG_FASTOPEN
        errno_of = sandbox.SECCOMP_RET_ERRNO
        self.assertEqual(self.evaluate(abi.sendto, args=(3, 0, 0, fastopen, 0, 0)), errno_of | errno.EOPNOTSUPP)
        self.assertEqual(self.evaluate(abi.sendmmsg, args=(3, 0, 0, fastopen, 0, 0)), errno_of | errno.EOPNOTSUPP)
        self.assertEqual(self.evaluate(abi.sendmsg, args=(3, 0, fastopen, 0, 0, 0)), errno_of | errno.EOPNOTSUPP)
        self.assertEqual(self.evaluate(abi.sendmsg, args=(3, 0, 0x40, 0, 0, 0)), sandbox.SECCOMP_RET_ALLOW)
        self.assertEqual(self.evaluate(abi.io_uring_setup), errno_of | errno.ENOSYS)
        self.assertEqual(self.evaluate(0, arch=0x40000003), errno_of | errno.EPERM)
        self.assertEqual(self.evaluate(abi.connect | sandbox.X32_SYSCALL_BIT), errno_of | errno.EPERM)


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
                "GIT_CONFIG_GLOBAL": os.devnull,
                "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
            },
        )
        named = sandbox.child_environment({"GIT_CONFIG_GLOBAL": "/srv/gitconfig"}, claude_home=None, tmp=self.root)
        self.assertEqual(named["GIT_CONFIG_GLOBAL"], "/srv/gitconfig")

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

    def test_links_planted_in_the_claude_home_are_replaced_not_followed(self) -> None:
        credentials = self.home / "secrets" / "credentials.json"
        credentials.write_text('{"token": "owner"}', encoding="utf-8")
        victim = self.home / "secrets" / "victim"
        victim.write_text("kept\n", encoding="utf-8")
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / "CLAUDE.md").write_text("rules\n", encoding="utf-8")
        claude_home = self.root / "state" / "claude"
        claude_home.mkdir(parents=True)
        (claude_home / ".claude.json").symlink_to(credentials)
        (claude_home / "CLAUDE.md").symlink_to(victim)
        (claude_home / "skills").symlink_to(self.home / "secrets")
        (claude_home / "projects").symlink_to(self.home / "secrets")
        sandbox.prepare_claude_home(claude_home, self.workspace, home=self.home, skills=[])
        self.assertEqual(credentials.read_text(encoding="utf-8"), '{"token": "owner"}')
        self.assertEqual(victim.read_text(encoding="utf-8"), "kept\n")
        for name in (".claude.json", "CLAUDE.md", "skills", "projects"):
            with self.subTest(name=name):
                self.assertFalse((claude_home / name).is_symlink())
        self.assertNotIn("owner", (claude_home / ".claude.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(path.name for path in (self.home / "secrets").iterdir()), ["credentials.json", "victim"])

    def test_a_fifo_left_at_the_claude_state_does_not_stall_the_launch(self) -> None:
        claude_home = self.root / "state" / "claude"
        claude_home.mkdir(parents=True)
        os.mkfifo(claude_home / ".claude.json")
        sandbox.prepare_claude_home(claude_home, self.workspace, home=self.home, skills=[])
        state = json.loads((claude_home / ".claude.json").read_text(encoding="utf-8"))
        self.assertTrue(state["projects"][str(self.workspace)]["hasTrustDialogAccepted"])

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

    def test_loopback_reaches_only_the_sandbox_s_own_listeners(self) -> None:
        outside = socket.socket()
        self.addCleanup(outside.close)
        outside.bind(("127.0.0.1", 0))
        outside.listen()
        probe = (
            "import socket,sys\n"
            "def attempt(port):\n"
            "    try:\n"
            "        socket.create_connection(('127.0.0.1', port), 5).close(); return 'ok'\n"
            "    except OSError as exc:\n"
            "        return exc.__class__.__name__\n"
            "own = socket.socket(); own.bind(('127.0.0.1', 0)); own.listen()\n"
            "fast = socket.socket()\n"
            "try:\n"
            "    fast.sendto(b'x', 0x20000000, ('127.0.0.1', int(sys.argv[1]))); opened = 'sent'\n"
            "except OSError as exc:\n"
            "    opened = exc.__class__.__name__\n"
            "print(attempt(int(sys.argv[1])), attempt(own.getsockname()[1]), opened)\n"
        )
        result = self.launch(f"/usr/bin/python3 -I -c {shlex.quote(probe)} {outside.getsockname()[1]}")
        self.assertEqual(result.stdout.split(), ["PermissionError", "ok", "OSError"], result.stderr)

    def test_a_connect_from_a_second_thread_is_brokered(self) -> None:
        outside = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(outside.close)
        outside.bind(str(self.home / "secrets" / "s"))
        outside.listen()
        probe = (
            "import socket,sys,threading\n"
            "results = []\n"
            "def attempt(path):\n"
            "    s = socket.socket(socket.AF_UNIX)\n"
            "    try:\n"
            "        s.connect(path); results.append('ok')\n"
            "    except OSError as exc:\n"
            "        results.append(exc.__class__.__name__)\n"
            "inside = sys.argv[2] + '/t'\n"
            "listener = socket.socket(socket.AF_UNIX); listener.bind(inside); listener.listen()\n"
            "for path in (sys.argv[1], inside):\n"
            "    worker = threading.Thread(target=attempt, args=(path,)); worker.start(); worker.join()\n"
            "print(*results)\n"
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
