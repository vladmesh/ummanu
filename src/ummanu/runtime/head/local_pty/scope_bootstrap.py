"""Set the kernel's group OOM contract before dropping scope privileges.

The supervisor is the sole protected process. The head clears the inherited protection before
exec, so a cgroup OOM kills the whole head process tree while the supervisor can journal its exit.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Executed by absolute path under -I. Only this product's source is importable,
# independently of caller PYTHONPATH/PYTHONHOME, cwd, user site or loader settings.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    __package__ = "ummanu.runtime.head.local_pty"

from ..memory import HEAD_SCOPE_PREFIX, OOM_STREAM_ENV, open_oom_stream, own_cgroup
from .scope_environment import EnvironmentTransferError, read_environment


def install_oom_contract(cgroup: Path, protection: Path = Path("/proc/self/oom_score_adj")) -> None:
    (cgroup / "memory.oom.group").write_text("1\n", encoding="ascii")
    protection.write_text("-1000\n", encoding="ascii")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["--runtime"]:
        oom_stream = int(args[1])
        environment = read_environment()
        null = os.open("/dev/null", os.O_RDONLY)
        try:
            os.dup2(null, 0)
        finally:
            os.close(null)
        # This descriptor belongs to privileged bootstrap, never to the caller.
        environment[OOM_STREAM_ENV] = str(oom_stream)
        os.execvpe(args[2], args[2:], environment)
        return 127
    divider = args.index("--")
    privileges, command = args[:divider], args[divider + 1 :]
    cgroup = own_cgroup()
    if cgroup is None or not cgroup.name.startswith(HEAD_SCOPE_PREFIX):
        raise RuntimeError("scope bootstrap is outside a ummanu head scope")
    install_oom_contract(cgroup)
    oom_stream = open_oom_stream()
    os.execve("/usr/bin/setpriv", [
        "/usr/bin/setpriv", *privileges, sys.executable, "-I", str(Path(__file__).resolve()),
        "--runtime", str(oom_stream), *command,
    ], {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"})
    return 127


if __name__ == "__main__":  # pragma: no cover - execs the scoped supervisor
    try:
        raise SystemExit(main())
    except (EnvironmentTransferError, OSError, ValueError, RuntimeError):
        print("scoped bootstrap refused", file=sys.stderr)
        raise SystemExit(1) from None
