"""The actual deployed argv producer, loaded without running its old launcher."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

from tests import source_trees
from ummanu.dispatch.runtime_preflight import PACKAGE
from ummanu.runtime.head.memory import scope_unit

DEPLOYED_PO_SHA = "64c42d735bd99740bba94c65788d34f7f354bee0"


def deployed_scope_argv():
    # Both local Git and exact-SHA CI retain this delivered main ancestor. Execute
    # only the old emitter, with native identity and the same public scope hash.
    root = Path(__file__).resolve().parents[1]
    # That commit shipped the package under its earlier name: find the module by its place in the
    # package, and run the emitter as this package's, since only this package's bootstrap
    # module is accepted (there is no alias for the old one).
    listing = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", DEPLOYED_PO_SHA, "src"],
        cwd=root, check=True, capture_output=True, text=True,
    ).stdout.splitlines()
    path = next(name for name in listing
                if name.endswith("/runtime/head/memory.py") and name.count("/") == 4)
    released = path.split("/")[1]
    source = subprocess.run(
        ["git", "show", f"{DEPLOYED_PO_SHA}:{path}"],
        cwd=root, check=True, capture_output=True, text=True,
    ).stdout.replace(f'"{released}.runtime.', f'"{PACKAGE}.runtime.')
    function = next(node for node in source_trees.parse(source).body
                    if isinstance(node, ast.FunctionDef) and node.name == "scope_argv")
    namespace = {"os": os, "sys": sys, "scope_unit": scope_unit}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "deployed-scope-argv", "exec"), namespace)
    return namespace["scope_argv"]
