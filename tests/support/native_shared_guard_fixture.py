"""Bounded compatibility model of codegen ec96fa79's CLEAN_ENV and ancestry.

These are the dummy values from scripts/test-unit-local.sh, inspected read-only.
It models both an absolute empty-PYTHONPATH child and a bash/subshell/PATH child
with a per-suite PYTHONPATH override; it is not the real native host argv or suite
scheduler. No service, network, live repository or production environment is used.
"""

from __future__ import annotations

import os
import resource
import subprocess
import sys
from pathlib import Path

# Preserve only these native inputs. In particular no BOARD_ROLE, PYTHONPATH from
# the launcher or hypothetical wrapper permission survives this boundary.
CLEAN_ENV = {
    "HOME": os.environ.get("HOME", ""),
    "PATH": os.environ.get("PATH", ""),
    "VIRTUAL_ENV": os.environ.get("VIRTUAL_ENV", ""),
    "PYTHONPATH": "",
    "REDIS_URL": "redis://localhost:6379/0",
    "API_BASE_URL": "http://127.0.0.1:9",
    "OPENAI_API_KEY": "sk-test-not-real",
    "LANGSMITH_API_KEY": "ls-test-not-real",
    "GITHUB_APP_ID": "12345",
    "GITHUB_APP_PRIVATE_KEY_PATH": "/dev/null",
    "TELEGRAM_BOT_TOKEN": "0000000000:test-token",
    "SECRETS_ENCRYPTION_KEY": "wHhIQWmPfLt60oHdxzbQhY1ZKnUon12e5_SuZ33xDxc=",
    "ORCHESTRATOR_HOSTNAME": "localhost",
    "REGISTRY_USER": "test",
    "REGISTRY_PASSWORD": "test",
    "WORKER_MANAGER_URL": "http://localhost:8001",
    "WORKER_REDIS_URL": "redis://localhost:6379/0",
    "WORKER_API_URL": "http://localhost:8000",
    "WORKER_BROKER_INTERNAL_TOKEN": "test-worker-broker-internal-token",
    "WORKER_BROKER_URL": "http://localhost:8001",
    "LK_DOMAIN": "https://lk.test.example.com",
    "TELEGRAM_MAX_CONCURRENT_UPDATES": "8",
    "INTERNAL_API_KEY": "test-internal-key",
    "LK_JWT_SECRET": "test-lk-jwt-secret",
    "DEFAULT_AGENT_TYPE": "claude",
    "DATABASE_URL": "postgresql+asyncpg://test:test@localhost:5432/test",
    "FIXTURE_ENV": "host",
}


def main() -> int:
    assert sys.argv[1:2] in ([], ["--"])
    selected = sys.argv[2:] or ["checks/test_local.py"]
    for selector in selected:
        if selector.startswith("checks/test_ci.py"):
            print(selector + ": ci_only (" + selector.rsplit("_", 1)[-1] + "); execution only in CI")
            return 23
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    arguments = ["-m", "pytest", *selected, "-m", "not ci_only", "-q", "-p", "no:cacheprovider",
                 "--timeout=90", "--timeout-method=thread", "--unit-test-budget=0.5"]
    clean = [f"{key}={value}" for key, value in CLEAN_ENV.items()]
    if os.environ.get("FIXTURE_SHELL_RUNNER") == "1":
        root = Path.cwd()
        # Keep the wrapper runner alive while bash waits for its subshell. Python
        # is resolved from the declared venv's bin directory through the whitelist.
        shell = '( /usr/bin/env -i "$@" ) &\npid=$!\nwait "$pid"\nrc=$?\nexit "$rc"\n'
        argv = ["/bin/bash", "--noprofile", "--norc", "-c", shell, "fixture", *clean,
                f"PATH={Path(sys.executable).parent}{os.pathsep}{CLEAN_ENV['PATH']}",
                f"PYTHONPATH={root / 'checks'}{os.pathsep}{root}", "python", *arguments]
    else:
        argv = ["/usr/bin/env", "-i", *clean, sys.executable, *arguments]
    result = subprocess.run(argv, check=False)
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    total = after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime
    print(f"CPU total: {total:.1f}s (budget 240s)")
    return 1 if result.returncode == 0 and total > 240 else result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
