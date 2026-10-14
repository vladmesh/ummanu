"""Characterize deterministic packets; optional capture compares artifact base bytes."""

from __future__ import annotations

import ast
import dis
import inspect
import json
import os
import unittest
from contextlib import ExitStack
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from types import CodeType, ModuleType
from unittest import mock

from tests.support.web_render_packets import ASSETS, NOW, helper_packets, packets
from ummanu.web import (
    page_assets,
    page_card,
    page_overview,
    page_parts,
    page_po,
    page_shell,
    page_sprint,
    page_values,
    pages,
)


def lookup(module, name):
    if module is not pages:
        return getattr(module, name)
    for owner in (pages, page_parts, page_card, page_po, page_sprint, page_shell, page_overview, page_values):
        if hasattr(owner, name):
            return getattr(owner, name)
    raise AttributeError(name)


class ConsolidateDicts(ast.NodeTransformer):
    def visit_Name(self, node):
        if node.id == "_block":
            node.id = "_mapping"
        return node

    def visit_IfExp(self, node):
        self.generic_visit(node)
        condition = node.test
        if (isinstance(condition, ast.Call) and isinstance(condition.func, ast.Name)
            and condition.func.id == "isinstance" and len(condition.args) == 2
            and isinstance(condition.args[1], ast.Name) and condition.args[1].id == "dict"
            and isinstance(node.orelse, ast.Dict) and not node.orelse.keys
            and ast.dump(node.body) == ast.dump(condition.args[0])):
            return ast.Call(func=ast.Name(id="_mapping", ctx=ast.Load()), args=[node.body], keywords=[])
        return node


def moves(source, baseline, testcase):
    """Artifact-only full move audit; future tests do not pin the old module's definitions."""
    result = {}
    contexts = {"limits_source", "doctor_source", "bell_source", "render_clock", "from_post"}
    for node in ast.parse(source).body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name == "_block":
            result[node.name] = {"consolidated_into": "ummanu.web.page_values._mapping"}
            continue
        original = lookup(baseline, node.name)
        candidate = lookup(pages, node.name)
        testcase.assertEqual(str(inspect.signature(original)), str(inspect.signature(candidate)), node.name)
        after = ast.parse(inspect.getsource(inspect.unwrap(candidate))).body[0]
        transformed = deepcopy(node)
        if node.name not in contexts | {"_mapping"}:
            transformed = ConsolidateDicts().visit(transformed)
            testcase.assertEqual(ast.dump(transformed), ast.dump(after), node.name)
        elif node.name == "_mapping":
            testcase.assertEqual(ast.dump(node), ast.dump(after), node.name)
        # Resolve each old module global at its actual new lookup, rather than at an export alias.
        globals_ = {}
        old_globals = inspect.unwrap(original).__globals__
        new_globals = inspect.unwrap(candidate).__globals__
        def loaded_globals(code):
            names = {op.argval for op in dis.get_instructions(code) if op.opname == "LOAD_GLOBAL"}
            for constant in code.co_consts:
                if isinstance(constant, CodeType):
                    names.update(loaded_globals(constant))
            return names
        for name in sorted(loaded_globals(inspect.unwrap(original).__code__) & old_globals.keys()):
            value = old_globals[name]
            new_name = "_mapping" if name == "_block" else name
            if node.name in contexts:
                continue  # their sole binding body is checked by the ownership/context regressions
            carried = new_globals[new_name]
            if inspect.isfunction(value) and value.__module__ == baseline.__name__:
                testcase.assertIs(carried, lookup(pages, new_name), node.name + "." + name)
                globals_[name] = carried.__module__ + "." + carried.__name__
            elif name.startswith("_") and isinstance(value, type(page_values._RENDER_CLOCK)):
                testcase.assertEqual(value.name, carried.name)
                globals_[name] = "ummanu.web.page_values." + new_name
            else:
                testcase.assertEqual(value, carried, node.name + "." + name)
                globals_[name] = "unchanged value/import"
        result[node.name] = {"owner": candidate.__module__, "globals": globals_,
            "base_ast_sha256": sha256(ast.dump(node).encode()).hexdigest(),
            "candidate_ast_sha256": sha256(ast.dump(after).encode()).hexdigest()}
    return result


def capture(module, cases):
    outputs = {}
    for label, name, args, kwargs in cases:
        with module.render_clock(lambda: NOW):
            try:
                value = lookup(module, name)(*deepcopy(args), **deepcopy(kwargs))
                outputs[label] = {"value": value}
            except Exception as exc:  # noqa: BLE001 - record malformed-input error behavior
                outputs[label] = {"exception": type(exc).__name__, "message": str(exc)}
    return outputs


class RenderPacketsTests(unittest.TestCase):
    def test_deterministic_page_and_helper_packets(self):
        cases = [*packets(), *helper_packets()]
        candidate = capture(pages, cases)
        self.assertEqual(candidate, capture(pages, cases))
        # The artifact path is opt-in and never part of the released product. Its exact source
        # SHA is recorded with the packet; no old renderer is shipped or permanently pinned.
        if base_path := os.environ.get("UMMANU_RENDER_BASE"):
            baseline = ModuleType("base_presentation")
            source = Path(base_path).read_bytes()
            exec(compile(source, base_path, "exec"), baseline.__dict__)
            original = capture(baseline, cases)
            self.assertEqual(candidate, original)
            movement = moves(source, baseline, self)
            for _, name, _, _ in cases:
                self.assertEqual(str(inspect.signature(lookup(pages, name))),
                                 str(inspect.signature(lookup(baseline, name))), name)
            assets = {}
            for name in ASSETS:
                before, after = getattr(baseline, name), getattr(page_assets, name)
                self.assertEqual(before.encode(), after.encode(), name)
                assets[name] = {"bytes": len(after.encode()), "sha256": sha256(after.encode()).hexdigest()}
            if output := os.environ.get("UMMANU_RENDER_CAPTURE"):
                Path(output).write_text(json.dumps({"base_source_sha256": sha256(source).hexdigest(),
                    "assets": assets, "movement": movement, "base": original, "candidate": candidate,
                    "hashes": {key: sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
                               for key, value in candidate.items()}}, indent=2) + "\n", encoding="utf-8")

    def test_source_and_renderer_exceptions_preserve_packets(self):
        modules = [pages]
        if base_path := os.environ.get("UMMANU_RENDER_BASE"):
            baseline = ModuleType("base_presentation")
            exec(compile(Path(base_path).read_bytes(), base_path, "exec"), baseline.__dict__)
            modules.append(baseline)
        recorded = []
        for module in modules:
            answers = {}
            for wrapper in ("limits_source", "doctor_source", "bell_source"):
                for exception in (OSError("denied <read>"), RuntimeError("defect")):
                    def fail(exception=exception):
                        raise exception
                    with getattr(module, wrapper)(fail):
                        answers[wrapper + type(exception).__name__] = capture(module, [("error", "error", [404, "code", "text"], {})])
            for name in ("_head_header", "_head_journal"):
                owner = page_card if module is pages else module
                with mock.patch.object(owner, name, side_effect=RuntimeError("boom")) as failed:
                    answers[name] = capture(module, [("head", "head_view", [{"ref": "r"}], {})])
                    failed.assert_called_once()
                    self.assertIn("this section could not be shown (RuntimeError)", answers[name]["head"]["value"])
            recorded.append(answers)
            with ExitStack() as stack:
                stack.enter_context(module.render_clock(lambda: NOW))
                stack.enter_context(module.from_post(True))
                recorded[-1]["post"] = module.error(400, "code", "text")
        for result in recorded[1:]:
            self.assertEqual(recorded[0], result)
        if output := os.environ.get("UMMANU_RENDER_CAPTURE"):
            path = Path(output).with_name("exception-packets.json")
            path.write_text(json.dumps(recorded, indent=2) + "\n", encoding="utf-8")
