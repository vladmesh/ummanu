"""Interpreter startup guard for candidate worker/reviewer environments.

Installed as a standalone module: it must work without Ummanu, PYTHONPATH or any
launcher environment. This prevents accidental commands, not deliberate bypasses
by the owner of the interpreter (for example Python's -S).
"""

from __future__ import annotations

import hashlib
import os
import shlex
import sys
from pathlib import Path

MODULE_FILE = "_ummanu_test_guard.py"
STARTUP_FILE = "01-ummanu-test-guard.pth"
WORKSPACE_STARTUP_DIR = ".ummanu-task-env/test-guard"


def reviewer_head() -> bool:
    """Read the role_env launch identity from live process ancestry.

    BOARD_ROLE is set by the common role launcher, never by candidate configuration.
    /proc's exec environment also survives a child's env -i and changes to its own
    environment. Like the existing bootstrap guard, this is an accidental-command
    policy, not a security sandbox against the process owner.
    """
    pid = os.getpid()
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        proc = Path("/proc") / str(pid)
        try:
            if b"BOARD_ROLE=reviewer" in proc.joinpath("environ").read_bytes().split(b"\0"):
                return True
            fields = proc.joinpath("stat").read_text().rsplit(")", 1)[1].split()
            pid = int(fields[1])
        except (OSError, UnicodeError, ValueError, IndexError):
            break
    return False


def check_refusal() -> int | None:
    if reviewer_head():
        sys.stderr.write("test-guard: reviewer ummanu check refused; read worker artifacts and CI evidence; request validation from worker or CI\n")
        return 125
    return None


def bootstrap_authorizes(argv: list[str], workspace: str, digest: str) -> bool:
    """Recognize the wrapper's exact module bootstrap and candidate import roots."""
    # CheckSpec.argv has a fixed, versioned-in-code shape. Hash the whole bootstrap,
    # rather than treating a fragment, environment marker or process name as authority.
    return (
        len(argv) >= 7
        and argv[1] == "-c"
        and hashlib.sha256(argv[2].encode()).hexdigest() == digest
        and argv[6].split(os.pathsep) == [workspace, str(Path(workspace) / "src")]
    )


def authorized(workspace: str, digest: str) -> bool:
    """Allow only a live wrapper runner and its descendants in this workspace.

    /proc ancestry survives env -i, PYTHONPATH='' and subprocess close_fds. There
    is no cached PID, lease file or inherited permission: reparented children and
    subsequent/sibling head commands have no runner ancestor and lose permission.
    """
    pid = os.getpid()
    seen: set[int] = set()
    try:
        while pid > 1 and pid not in seen:
            seen.add(pid)
            proc = Path("/proc") / str(pid)
            argv = proc.joinpath("cmdline").read_bytes().rstrip(b"\0").decode().split("\0")
            if bootstrap_authorizes(argv, workspace, digest):
                return True
            # comm can contain spaces and parentheses; fields after its final ')' are stable.
            fields = proc.joinpath("stat").read_text().rsplit(")", 1)[1].split()
            pid = int(fields[1])
    except (OSError, UnicodeError, ValueError, IndexError):
        pass
    return False


def guidance(runner: str, args: list[str]) -> str:
    selectors: list[str] = []
    skip = False
    values = ({"-k", "-m", "-p", "-c", "-o", "--rootdir"} if runner == "pytest"
              else {"--start-directory", "-s", "-t", "--pattern", "-p"})
    for arg in args:
        if skip:
            skip = False
        elif arg in values:
            skip = True
        elif not arg.startswith("-") and arg != "discover":
            selectors.append(arg.replace("\n", " ").replace("\r", " "))
    command = shlex.join(["ummanu", "check", *selectors])
    return f"test-guard: direct {runner} refused; use {command}\n"


def install(workspace: str, digest: str) -> None:
    def audit(event: str, args: tuple[object, ...]) -> None:
        runner = ""
        if event == "cpython.run_module" and args[0] in {"pytest", "unittest"}:
            runner = str(args[0])
        elif event == "cpython.run_file" and Path(str(args[0])).name in {"pytest", "py.test"}:
            runner = "pytest"
        if runner and (reviewer_head() or not authorized(workspace, digest)):
            # Exiting from an audit hook avoids site/runpy tracebacks and imports no test runner.
            message = ("test-guard: reviewer test execution refused, including ummanu check; read worker artifacts and CI evidence\n"
                       if reviewer_head() else guidance(runner, sys.argv[1:]))
            os.write(2, message.encode())
            os._exit(125)

    sys.addaudithook(audit)


def install_environment(workspace: Path, environment: Path) -> None:
    """Materialize only a real candidate-local venv, never a live/shared interpreter."""
    from ummanu._fsutil import write_text_atomic
    from ummanu.broad_check import _PROVENANCE_BOOTSTRAP

    root = workspace.resolve(strict=True)
    prefix = environment.resolve(strict=True)
    sites = [path for path in prefix.glob("lib/python3*/site-packages") if path.is_dir()]
    if (
        not prefix.is_relative_to(root)
        or not (prefix / "pyvenv.cfg").is_file()
        or len(sites) != 1
        or not sites[0].resolve(strict=True).is_relative_to(prefix)
    ):
        raise ValueError(f"test guard requires a candidate-local venv at {environment}")
    digest = hashlib.sha256(_PROVENANCE_BOOTSTRAP.encode()).hexdigest()
    files = {
        MODULE_FILE: Path(__file__).read_text(encoding="utf-8"),
        STARTUP_FILE: f"import _ummanu_test_guard; _ummanu_test_guard.install({str(root)!r}, {digest!r})\n",
    }
    for name, body in files.items():
        target = sites[0] / name
        if target.is_file() and not target.is_symlink() and target.read_text(encoding="utf-8") == body:
            continue
        write_text_atomic(target, body)


def install_workspace(workspace: Path) -> None:
    """Provide startup hooks to external interpreters through the role's import path.

    The directory belongs to the claimed workspace namespace. No external or
    shared prefix is modified, even when an adapter declares a system Python.
    """
    from ummanu._fsutil import write_text_atomic
    from ummanu.broad_check import _PROVENANCE_BOOTSTRAP

    root = workspace.resolve(strict=True)
    namespace = root / ".ummanu-task-env"
    if not namespace.resolve().is_relative_to(root):
        raise ValueError("workspace test guard namespace escapes the candidate")
    directory = root / WORKSPACE_STARTUP_DIR
    directory.mkdir(parents=True, exist_ok=True)
    if not directory.resolve(strict=True).is_relative_to(namespace.resolve(strict=True)):
        raise ValueError("workspace test guard directory escapes its namespace")
    digest = hashlib.sha256(_PROVENANCE_BOOTSTRAP.encode()).hexdigest()
    files = {
        MODULE_FILE: Path(__file__).read_text(encoding="utf-8"),
        "sitecustomize.py": f"import _ummanu_test_guard; _ummanu_test_guard.install({str(root)!r}, {digest!r})\n",
    }
    for name, body in files.items():
        target = directory / name
        if target.is_file() and not target.is_symlink() and target.read_text(encoding="utf-8") == body:
            continue
        write_text_atomic(target, body)
