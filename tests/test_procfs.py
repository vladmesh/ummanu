"""Pure tokenization and real reader adapters, using only deterministic file observations."""

from __future__ import annotations

import ast
import builtins
import hashlib
import json
import re
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from ummanu import host_apply
from ummanu.po import runner
from ummanu.runtime import procfs, state, test_guard
from ummanu.runtime.head import children, identity
from ummanu.runtime.head.local_pty import scoped_lifecycle as scoped, supervisor

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / ".ummanu-task-env/procstat"
OWNERS = {
    identity: ("_proc_starttime_ticks", "head_process_status"),
    children: ("_stat_fields", "read_head_children"),
    scoped: ("launch_identity", "launch_group_present"),
    supervisor: ("_live_head",),
    runner: ("process_identity", "still_running"),
    state: ("_process_start",),
    host_apply: ("_process_start_ticks",),
    test_guard: ("reviewer_head", "authorized"),
}


def stat(*, state="S", parent="20", group="30", start="42", count=20, separator=" "):
    fields = [state, parent, group, *[str(n) for n in range(3, 19)], start][:count]
    return "30 (name with spaces (and) multiple ) closes)" + separator + " \t".join(fields)


def close_split_sites(source):
    """Catch new hand-maintained comm splitting without pinning reader module names."""
    if not re.search(r'\.(?:rfind|rindex|rsplit|split|partition|rpartition)\(\s*["\'][^"\']*\)', source) and not (
        "/stat" in source and re.search(r'\.split\(\s*["\'] ["\']\s*\)', source)
    ):
        return []
    tree = ast.parse(source)
    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or not node.args:
            continue
        arg = node.args[0]
        if (isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                and node.func.attr in {"rfind", "rindex", "rsplit", "split", "partition", "rpartition"}
                and ")" in arg.value):
            sites.append(node.lineno)
    # A fixed-space stat reader also bypasses the comm boundary. Limit this check
    # to functions reading that format, not unrelated space-separated protocols.
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        nodes = list(ast.walk(function))
        reads_stat = any(isinstance(n, ast.Constant) and isinstance(n.value, str)
                         and (n.value.endswith("/stat") or n.value == "stat") for n in nodes)
        if reads_stat:
            sites.extend(n.lineno for n in nodes if isinstance(n, ast.Call)
                         and isinstance(n.func, ast.Attribute) and n.func.attr == "split"
                         and n.args and isinstance(n.args[0], ast.Constant) and n.args[0].value == " ")
    return sorted(set(sites))


def base_adapters(module, names):
    """Execute original function bytes with the same deterministic adapter dependencies.

    Optional worker evidence is outside git. CI always runs the explicit contract
    assertions below; this comparison does not claim pristine-base suite execution.
    """
    path = EVIDENCE / "base" / (module.__name__.replace(".", "/") + ".py")
    tree = ast.parse(path.read_text())
    selected = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert len(selected) == len(names)
    namespace = dict(vars(module))
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), namespace)
    return types.SimpleNamespace(**{name: namespace[name] for name in names})


def observe(modules, text, *, stat_error=None, boot_error=None):
    """Call the real adapters; retain values, errors, encoding, and observation order."""
    ident, child, scope, sup, po, agent, host, guard = modules
    trace = []
    heartbeat = json.dumps({"version": 1, "pid": 30, "boot_id": "boot", "proc_starttime_ticks": "42",
                            "run_id": "run", "role": "worker", "task": "task"})
    digest = hashlib.sha256(b"bootstrap").hexdigest()
    argv = ["python", "-c", "bootstrap", "a", "b", "c", "/candidate:/candidate/src"]

    def read(path, *args, **kwargs):
        trace.append(["text", str(path), list(args), kwargs])
        if str(path) == "/fixture/head.pid":
            return heartbeat
        if path.name == "status":
            return "State:\tS (sleeping)\n"
        if path.name == "uptime":
            return "10 0"
        if path.name == "io":
            return "rchar: 7\nwchar: 9\n"
        error = boot_error if path.name == "boot_id" else stat_error
        if error is not None:
            raise error
        return "boot" if path.name == "boot_id" else text

    def read_bytes(path):
        trace.append(["bytes", str(path)])
        if path.name == "environ":
            return b"BOARD_ROLE=reviewer\0" if path.parent.name == "20" else b"BOARD_ROLE=worker\0"
        return "\0".join(argv).encode() if path.parent.name == "20" else b"child\0"

    calls = {
        "identity": lambda: ident._proc_starttime_ticks(30),
        "head_status": lambda: ident.head_process_status("/fixture/head.pid"),
        "children": lambda: child._stat_fields(30, Path("/proc")),
        "child_activity": lambda: child.read_head_children(30),
        "scoped_identity": lambda: scope.launch_identity(30),
        "scoped_group": lambda: scope.launch_group_present(30, "boot:42"),
        "supervisor": lambda: sup._live_head(Path("/fixture/head.pid"), "run"),
        "po_identity": lambda: po.process_identity(30),
        "po_running": lambda: po.still_running(30, "boot:42"),
        "state": lambda: agent._process_start(30),
        "host": lambda: host._process_start_ticks(30),
        "reviewer": guard.reviewer_head,
        "authorized": lambda: guard.authorized("/candidate", digest),
    }
    results = {}
    with (mock.patch.object(Path, "read_text", read),
          mock.patch.object(Path, "read_bytes", read_bytes),
          mock.patch.object(Path, "iterdir", lambda path: iter([Path("/proc/40")])),
          mock.patch.object(Path, "exists", return_value=True),
          mock.patch.object(children.os, "listdir", return_value=["30", "40"]),
          mock.patch.object(children.os, "sysconf", return_value=100),
          mock.patch.object(children.os, "readlink", return_value="/dev/null"),
          mock.patch.object(test_guard.os, "getpid", return_value=30),
          mock.patch.object(supervisor.os, "kill", side_effect=lambda *args: trace.append(["kill", *args]))):
        for name, call in calls.items():
            trace.clear()
            try:
                result = {"value": call()}
            except (OSError, UnicodeError, ValueError, IndexError, scoped.MemoryScopeError) as exc:
                result = {"error": type(exc).__name__, "message": str(exc)}
            results[name] = {**result, "trace": list(trace)}
    return results


class ProcStatTests(unittest.TestCase):
    def test_offsets_whitespace_comm_and_separator_edges(self):
        fields = procfs.proc_stat_fields(stat())
        self.assertEqual([fields[n] for n in (procfs.STATE, procfs.PPID, procfs.PGRP)], ["S", "20", "30"])
        self.assertEqual([fields[n] for n in (procfs.UTIME, procfs.STIME, procfs.CUTIME, procfs.CSTIME)],
                         ["11", "12", "13", "14"])
        self.assertEqual(fields[procfs.STARTTIME], "42")
        for separator in (" ", "\t", "\n", " \n\t  "):
            self.assertEqual(procfs.proc_stat_fields(stat(separator=separator) + " \n"), fields)
        # Both historical lexical forms remain explicit, even on malformed input.
        self.assertEqual(procfs.proc_stat_fields(stat(separator="")), fields[1:])
        self.assertEqual(procfs.proc_stat_fields(stat(separator=""), skip_separator=False), fields)
        for text in ("", "30 (unclosed S 1", "no close"):
            with self.assertRaises(ValueError):
                procfs.proc_stat_fields(text)
        for count in (0, 1, 2, 3, 19, 20):
            self.assertEqual(len(procfs.proc_stat_fields(stat(count=count))), count)
        self.assertEqual(procfs.proc_stat_fields("30 ()"), [])
        self.assertEqual(procfs.proc_stat_fields(stat(start="not-a-number"))[-1], "not-a-number")

    def test_adapters_preserve_distinct_contracts_and_basebyte_observations(self):
        vectors = {"valid": stat(), "empty": "", "missing_close": "30 unclosed S 20",
                   "empty_tail": "30 ()", "no_separator": stat(separator=""),
                   "space_prefix": "  \t" + stat() + "\n", "short": stat(count=19),
                   "ppid_only": stat(count=2), "bad_ppid": stat(parent="bad"),
                   "bad_group": stat(group="bad"), "bad_start": stat(start="bad"),
                   "negative": stat(start="-42"), "zero": stat(start="0"),
                   **{s: stat(state=s) for s in ("Z", "X", "x")}}
        modules = list(OWNERS)
        outcomes = {name: observe(modules, text) for name, text in vectors.items()}
        errors = {"enoent": FileNotFoundError(2, "vanished"), "esrch": ProcessLookupError(3, "vanished"),
                  "permission": PermissionError(13, "denied"), "io": OSError(5, "unreadable"),
                  "unicode": UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")}
        for name, error in errors.items():
            outcomes[name] = observe(modules, stat(), stat_error=error)
            outcomes["boot_" + name] = observe(modules, stat(), boot_error=error)
        valid = outcomes["valid"]
        expected = {"identity": "42", "children": procfs.proc_stat_fields(stat()),
                    "scoped_identity": "boot:42", "scoped_group": True, "supervisor": 30,
                    "po_identity": "boot:42", "po_running": True, "state": 42, "host": 42,
                    "reviewer": True, "authorized": True}
        self.assertEqual({k: v["value"] for k, v in valid.items() if k in expected}, expected)
        self.assertEqual(valid["head_status"]["value"]["state"], "live-match")
        self.assertEqual(outcomes["missing_close"]["head_status"]["value"]["known"], False)
        self.assertEqual(outcomes["bad_start"]["head_status"]["value"]["state"], "identity-mismatch")
        self.assertEqual(valid["child_activity"]["value"]["state"], "observed")
        for name in ("empty", "missing_close", "empty_tail", "short"):
            result = outcomes[name]
            self.assertEqual(result["identity"]["error"], "ValueError")
            self.assertIsNone(result["children"]["value"])
            self.assertEqual(result["supervisor"]["value"], 0)
            self.assertIsNone(result["state"]["value"])
            self.assertIsNone(result["host"]["value"])
        for label, value in (("bad_start", "bad"), ("negative", "-42"), ("zero", "0")):
            self.assertEqual(outcomes[label]["identity"]["value"], value)
            self.assertEqual(outcomes[label]["po_identity"]["value"], "boot:" + value)
            self.assertIsNone(outcomes[label]["host"]["value"])
        self.assertEqual(outcomes["negative"]["state"]["value"], -42)
        self.assertEqual(outcomes["zero"]["state"]["value"], 0)
        self.assertEqual(outcomes["bad_start"]["reviewer"]["value"], True)
        self.assertEqual(outcomes["ppid_only"]["authorized"]["value"], True)
        for name in ("missing_close", "bad_ppid", "enoent", "esrch", "permission", "io", "unicode"):
            self.assertFalse(outcomes[name]["reviewer"]["value"])
            self.assertFalse(outcomes[name]["authorized"]["value"])
        self.assertEqual(outcomes["missing_close"]["scoped_identity"]["error"], "MemoryScopeError")
        self.assertEqual(outcomes["missing_close"]["scoped_group"]["error"], "IndexError")
        self.assertEqual(outcomes["missing_close"]["po_identity"]["error"], "ValueError")
        for zombie in ("Z", "X"):
            self.assertIsNone(outcomes[zombie]["scoped_identity"]["value"])
            self.assertFalse(outcomes[zombie]["po_running"]["value"])
        self.assertEqual(outcomes["Z"]["supervisor"]["value"], 0)
        self.assertEqual(outcomes["X"]["supervisor"]["value"], 30)
        self.assertTrue(outcomes["x"]["po_running"]["value"])
        self.assertEqual(valid["scoped_identity"]["trace"][0][1], "/proc/30/stat")
        self.assertEqual(valid["scoped_group"]["trace"][0][1], "/proc/sys/kernel/random/boot_id")
        self.assertEqual(valid["supervisor"]["trace"][1], ["kill", 30, 0])
        self.assertEqual(valid["identity"]["trace"][0][-1], {"encoding": "utf-8"})
        self.assertEqual(valid["po_identity"]["trace"][0][-1], {"encoding": "utf-8", "errors": "replace"})
        self.assertEqual(valid["scoped_identity"]["trace"][0][-1], {})
        # Worker comparison is conditional only on the presence of archived original
        # bytes, never on a failing assertion. No baseline code is installed/imported.
        if (EVIDENCE / "base").is_dir():
            original = [base_adapters(module, names) for module, names in OWNERS.items()]
            baseline = {name: observe(original, text) for name, text in vectors.items()}
            for name, error in errors.items():
                baseline[name] = observe(original, stat(), stat_error=error)
                baseline["boot_" + name] = observe(original, stat(), boot_error=error)
            self.assertEqual(outcomes, baseline)
            (EVIDENCE / "characterization.json").write_text(json.dumps(
                {"kind": "fixture-on-basebytes", "vectors": len(outcomes),
                 "adapter_observations": len(outcomes) * len(valid), "base": baseline, "candidate": outcomes},
                indent=2) + "\n")

    def test_actual_parse_bindings_delegate_and_remain_patchable(self):
        for module in OWNERS:
            self.assertIs(module.proc_stat_fields, procfs.proc_stat_fields, module.__name__)
            with mock.patch.object(module, "proc_stat_fields", wraps=procfs.proc_stat_fields) as parse:
                observe(list(OWNERS), stat())
                self.assertGreater(parse.call_count, 0, module.__name__)
        with (tempfile.TemporaryDirectory() as scratch,
              mock.patch.object(identity, "current_boot_id", return_value="boot"),
              mock.patch.object(identity, "_proc_starttime_ticks", return_value="patched")):
            path = Path(scratch) / "head.pid"
            identity.publish_heartbeat(str(path), {"run_id": "r", "role": "worker", "task": "t"}, pid=30)
            self.assertEqual(json.loads(path.read_text())["proc_starttime_ticks"], "patched")

    def test_real_decoding_and_child_activity_numeric_policies(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            raw = root / "invalid-comm"
            raw.write_bytes(stat().encode().replace(b"name", b"\xff"))
            original_read = Path.read_text

            def read(path, *args, **kwargs):
                if path.name == "boot_id":
                    return "boot"
                return original_read(raw, *args, **kwargs)

            with mock.patch.object(Path, "read_text", read):
                for call in (lambda: identity._proc_starttime_ticks(30), lambda: host_apply._process_start_ticks(30)):
                    with self.assertRaises(UnicodeDecodeError):
                        call()
                self.assertEqual(runner.process_identity(30), "boot:42")
                self.assertEqual(state._process_start(30), 42)
                self.assertEqual(children._stat_fields(30, root)[procfs.STARTTIME], "42")
            proc = root / "proc"
            proc.mkdir()
            (proc / "uptime").write_text("10 0")
            records = {30: stat(parent="1"), 40: stat(parent="30"),
                       41: stat(parent="30", state="Z"), 42: stat(parent="30", state="X"),
                       43: stat(parent="30", state="x"), 44: stat(parent="30", start="bad"),
                       45: stat(parent="bad"), 46: stat(parent="30").replace(" \t11 ", " \tbad ")}
            for pid, text in records.items():
                directory = proc / str(pid)
                directory.mkdir()
                (directory / "stat").write_text(text)
                (directory / "io").write_text("rchar: 7\nwchar: 9\n")
                (directory / "cmdline").write_bytes(b"child\0argument\0")
            with (mock.patch.object(children.os, "sysconf", return_value=100),
                  mock.patch.object(children.os, "readlink", return_value="/dev/null")):
                result = children.read_head_children(30, proc_root=str(proc))
                self.assertEqual(result["descendant_count"], 1)
                self.assertEqual(result["total_cpu_ms"], 770)
                self.assertEqual(result["total_io"], 16)
                self.assertEqual(result["descendants"][0]["pid"], 40)
                if (EVIDENCE / "base").is_dir():
                    old = base_adapters(children, OWNERS[children])
                    self.assertEqual(result, old.read_head_children(30, proc_root=str(proc)))

    def test_one_maintained_tokenizer_and_stdlib_leaf(self):
        product = ROOT / "src/ummanu"
        sites = {str(path.relative_to(product)): close_split_sites(path.read_text())
                 for path in product.rglob("*.py")}
        self.assertEqual([name for name, lines in sites.items() if lines], ["runtime/procfs.py"])
        tree = ast.parse(Path(procfs.__file__).read_text())
        self.assertFalse(any(isinstance(n, (ast.Import, ast.ImportFrom)) for n in ast.walk(tree)))
        self.assertTrue(all(isinstance(n, (ast.Expr, ast.Assign, ast.FunctionDef)) for n in tree.body))
        for source in ('def f(s): return s.rfind(")")', 'def f(s): return s.rsplit(")", 1)[1].split()',
                       'def f(s): return s.split(") ")[1]',
                       'def f(s):\n p = "/proc/1/stat"\n return s.split(" ")[21]'):
            self.assertTrue(close_split_sites(source))
        self.assertEqual(close_split_sites('def f(s): return s.split(" ")'), [])
        # The protected writer is intentionally embedded source, not a reader.
        writer = (product / "runtime/head/command.py").read_text()
        self.assertIn("close = stat.rfind(')')", writer)


class StandaloneParserTests(unittest.TestCase):
    def test_rendered_definition_execution_and_guard_decisions_without_product_import(self):
        source = test_guard.standalone_source()
        tree = ast.parse(source)
        leaf = ast.parse(Path(procfs.__file__).read_text())
        rendered = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "proc_stat_fields")
        canonical = next(n for n in leaf.body if isinstance(n, ast.FunctionDef))
        self.assertEqual(ast.dump(rendered), ast.dump(canonical))
        imported = []
        original_import = builtins.__import__

        def import_stdlib(name, *args, **kwargs):
            imported.append(name)
            self.assertFalse(name.startswith("ummanu"), name)
            return original_import(name, *args, **kwargs)

        namespace = {"__name__": "_ummanu_test_guard", "__builtins__": {**vars(builtins), "__import__": import_stdlib}}
        exec(compile(source, "rendered_guard.py", "exec"), namespace)
        deployed = types.SimpleNamespace(**namespace)
        for text in (stat(), stat(separator=""), stat(count=2), "", "30 ()", "unclosed"):
            candidate = observe(list(OWNERS), text)
            modules = [*list(OWNERS)[:-1], deployed]
            for name in ("reviewer", "authorized"):
                self.assertEqual(observe(modules, text)[name], candidate[name])
        self.assertEqual(deployed.proc_stat_fields.__code__.co_code, procfs.proc_stat_fields.__code__.co_code)
        self.assertEqual(deployed.proc_stat_fields.__code__.co_consts, procfs.proc_stat_fields.__code__.co_consts)
        self.assertFalse(any(isinstance(n, ast.ImportFrom) and (n.module or "").startswith("ummanu") for n in tree.body))
        self.assertIn("hashlib", imported)
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            test_guard.install_workspace(root)
            installed = root / test_guard.WORKSPACE_STARTUP_DIR / test_guard.MODULE_FILE
            self.assertEqual(installed.read_text(), source)
            prefix = root / "fixture-venv"
            site = prefix / "lib/python3.12/site-packages"
            site.mkdir(parents=True)
            (prefix / "pyvenv.cfg").touch()
            test_guard.install_environment(root, prefix)
            self.assertEqual((site / test_guard.MODULE_FILE).read_text(), source)
        if EVIDENCE.is_dir():
            (EVIDENCE / "rendered_guard.py").write_text(source)
            provenance = {"source_guard_sha256": hashlib.sha256(Path(test_guard.__file__).read_bytes()).hexdigest(),
                          "leaf_sha256": hashlib.sha256(Path(procfs.__file__).read_bytes()).hexdigest(),
                          "rendered_guard_sha256": hashlib.sha256(source.encode()).hexdigest(),
                          "parser_ast_equal": True, "parser_code_equal": True,
                          "startup_product_imports": []}
            (EVIDENCE / "guard-provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
