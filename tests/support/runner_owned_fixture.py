"""A host-profile fixture, not the live codegen runner or its implementation."""

from __future__ import annotations

import os
import sys
from pathlib import Path

PARAMETER_NODE = "checks/test_local.py::test_value[one space;$(touch injected)]"
NODES = {
    PARAMETER_NODE: "",
    "checks/test_local.py::test_two": "",
    "checks/test_ci.py::test_docker": "docker",
    "checks/test_ci.py::test_ansible": "ansible",
    "checks/test_ci.py::test_privileged": "privileged",
    "checks/test_ci.py::test_slow": "slow",
}


def host_profile(argv, environment, emit, execute):
    if argv[:2] != ["--fixture-env", "host"]:
        emit("fixture runner: missing declared host arguments")
        return 2
    remaining = argv[2:]
    if remaining and remaining[0] != "--":
        emit("fixture runner: selectors require --")
        return 2
    selectors = remaining[1:]
    if selectors and environment.get("FIXTURE_SELECTOR_UNAVAILABLE"):
        emit("fixture runner: selector support unavailable")
        return 2
    selected = list(NODES)
    if selectors:
        selected = []
        for selector in selectors:
            matches = [node for node in NODES if node == selector or node.startswith(selector + "::")]
            if not matches:
                emit(f"{selector}: fixture runner unknown node")
                return 4
            selected.extend(matches)
    local = [node for node in selected if not NODES[node]]
    if not local:
        markers = ", ".join(sorted({NODES[node] for node in selected}))
        emit(f"{selectors[0]}: ci_only ({markers}); execution only in CI")
        return 23
    # The runner owns its environment whitelist even when the wrapper supplies PYTHONPATH.
    fixture_env = {"FIXTURE_ENV": "host", "PYTHONPATH": ""}
    emit(f"fixture-env=host PYTHONPATH='' ci_only deselected={len(selected) - len(local)}")
    for node in local:
        execute(node, fixture_env)
    emit(f"Ran {len(local)} tests in 0.000s\nOK")
    return 0


if __name__ == "__main__":
    execution_log = Path(os.environ["FIXTURE_EXECUTION_LOG"])

    def record(node, environment):
        os.environ.clear()
        os.environ.update(environment)
        assert environment == {"FIXTURE_ENV": "host", "PYTHONPATH": ""}
        assert dict(os.environ) == environment
        with execution_log.open("a") as handle:
            handle.write(node + "\n")

    raise SystemExit(host_profile(sys.argv[1:], os.environ, print, record))
