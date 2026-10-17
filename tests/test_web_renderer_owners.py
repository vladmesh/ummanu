"""Presentation ownership, packaged assets and request context isolation."""

from __future__ import annotations

import ast
import runpy
import unittest
from collections import UserDict
from contextlib import ExitStack
from contextvars import ContextVar, Token, copy_context
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from unittest import mock

from tests.support.web_render_packets import ASSETS
from ummanu.web import page_assets, page_shell, page_values, pages

WEB = Path(__file__).resolve().parents[1] / "src/ummanu/web"
LAYERS = {
    "page_values": 0, "page_assets": 0, "page_parts": 1, "page_shell": 2,
    "page_overview": 3, "page_board": 3, "page_card": 3, "page_sprint": 3, "page_po": 3, "pages": 4,
}


def dict_test(node):
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "isinstance" and len(node.args) == 2
            and isinstance(node.args[1], ast.Name) and node.args[1].id == "dict")


def normalizers(tree):
    """Find dict-or-empty implementations by behavior, including a renamed planted copy."""
    found = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.IfExp) and dict_test(node.test)
            and isinstance(node.orelse, ast.Dict) and not node.orelse.keys
            and ast.dump(node.body) == ast.dump(node.test.args[0])):
            found.append(node)
        if (isinstance(node, ast.If) and dict_test(node.test)
            and len(node.body) == 1 and isinstance(node.body[0], ast.Return)
            and ast.dump(node.body[0].value) == ast.dump(node.test.args[0])):
            found.append(node)
    return found


class RendererOwnershipTests(unittest.TestCase):
    def test_dependency_direction_and_single_normalizer_and_binding(self):
        mappings, bindings = [], []
        for module, layer in LAYERS.items():
            path = WEB / (module + ".py")
            source = path.read_text(encoding="utf-8")
            self.assertLessEqual(len(source.splitlines()), 3000, module)
            if layer == 3:
                self.assertLessEqual(len(source.splitlines()), 1500, module)
            tree = ast.parse(source)
            mappings += [(module, n.lineno) for n in normalizers(tree)]
            for n in ast.walk(tree):
                if isinstance(n, ast.ImportFrom):
                    self.assertTrue(all(a.name != "*" for a in n.names), module)
                    dependency = (n.module or "").removeprefix("ummanu.web.")
                    if dependency in LAYERS:
                        self.assertLess(LAYERS[dependency], layer, f"{module} -> {dependency}")
                    if n.module == "ummanu.web":
                        for alias in n.names:
                            if alias.name in LAYERS:
                                self.assertLess(LAYERS[alias.name], layer, f"{module} -> {alias.name}")
                if isinstance(n, ast.FunctionDef):
                    self.assertNotEqual(n.name, "__getattr__", module)
                    resets = [call for call in ast.walk(n) if isinstance(call, ast.Call)
                              and isinstance(call.func, ast.Attribute) and call.func.attr == "reset"]
                    if resets:
                        bindings.append((module, n.name, len(resets)))
            self.assertNotIn("sys.modules", source, module)
        self.assertEqual([m for m, _ in mappings], ["page_values"])
        self.assertEqual(bindings, [("page_values", "_bind", 1)])

    def test_renaming_or_expanding_a_duplicate_does_not_hide_it(self):
        for source in (
            "def extra(v): return v if isinstance(v, dict) else {}",
            "def extra(v):\n if isinstance(v, dict):\n  return v\n return {}",
        ):
            self.assertEqual(len(normalizers(ast.parse(source))), 1)

    def test_public_exports_are_the_actual_callable_owners(self):
        families = {"dashboard": "page_overview", "commands": "page_overview", "doctor": "page_overview",
                    "task": "page_card", "head_view": "page_card", "sprint": "page_sprint",
                    "sprint_form": "page_sprint", "po_page": "page_po", "po_session": "page_po",
                    "error": "page_shell", "render_clock": "page_values"}
        for name, owner in families.items():
            self.assertEqual(getattr(pages, name).__module__, "ummanu.web." + owner)
        self.assertNotIn("_head_header", vars(pages))
        self.assertNotIn("STYLE", vars(pages))

    def test_only_dicts_are_accepted_and_dict_identity_is_preserved(self):
        class DictSubclass(dict):
            pass
        for value in ({}, {"x": 1}, DictSubclass(x=1)):
            self.assertIs(page_values._mapping(value), value)
        for value in (None, [], "x", 1, False, UserDict(x=1)):
            self.assertEqual(page_values._mapping(value), {})
        self.assertIsNot(page_values._mapping(None), page_values._mapping(None))


class PresentationResourcesTests(unittest.TestCase):
    def test_canonical_scripts_are_the_consumed_values_and_css_is_generated(self):
        static = resources.files("ummanu.web").joinpath("static")
        for name in ASSETS[1:]:
            filename = name.strip("_").lower().removesuffix("_script").replace("_", "-") + ".js"
            self.assertEqual(getattr(page_assets, name), static.joinpath(filename).read_text(encoding="utf-8"))
        self.assertIn("TAB_RULES", static.joinpath("style.css").read_text(encoding="utf-8"))
        self.assertNotIn("TAB_RULES", page_assets.STYLE)
        tree = ast.parse((WEB / "page_assets.py").read_text(encoding="utf-8"))
        for n in tree.body:
            if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "MAX_TABS" for t in n.targets):
                n.value = ast.Constant(value=page_assets.MAX_TABS + 1)
        namespace = {}
        exec(compile(ast.fix_missing_locations(tree), "altered_tab_limit", "exec"), namespace)
        self.assertIn(f".tab-panel:nth-of-type({page_assets.MAX_TABS + 1}) {{ display: block; }}", namespace["STYLE"])
        self.assertNotEqual(namespace["STYLE"], page_assets.STYLE)

    def test_missing_packaged_resource_fails_at_the_loading_boundary(self):
        with (mock.patch("importlib.resources.files", side_effect=FileNotFoundError("missing asset")),
              self.assertRaisesRegex(FileNotFoundError, "missing asset")):
            runpy.run_path(str(WEB / "page_assets.py"))

    def test_source_rules_cover_python_and_canonical_css_and_scripts(self):
        source = [*WEB.glob("*.py"), *WEB.joinpath("static").glob("*.css"), *WEB.joinpath("static").glob("*.js")]
        self.assertTrue(any(p.suffix == ".css" for p in source))
        self.assertEqual(sum(p.suffix == ".js" for p in source), 7)
        for path in source:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("del limits", text, path.name)
            if path.suffix in {".css", ".js"}:
                self.assertNotIn("</script", text.lower(), path.name)
                self.assertNotIn("</style", text.lower(), path.name)
                self.assertNotIn("@import", text, path.name)


class RequestContextTests(unittest.TestCase):
    def bindings(self):
        return ((pages.limits_source, page_values._LIMITS_SOURCE),
                (pages.doctor_source, page_values._DOCTOR_SOURCE),
                (pages.bell_source, page_values._BELL_SOURCE),
                (pages.render_clock, page_values._RENDER_CLOCK),
                (pages.from_post, page_values._FROM_POST))

    def test_independent_nested_and_exceptional_contexts_restore_each_value(self):
        pairs = self.bindings()
        defaults = [variable.get() for _, variable in pairs]
        values = [lambda: {}, lambda: {}, lambda: {}, lambda: datetime(2026, 9, 20, tzinfo=UTC), True]
        with ExitStack() as stack:
            for (bind, _), value in zip(pairs, values, strict=True):
                stack.enter_context(bind(value))
            self.assertEqual([variable.get() for _, variable in pairs], values)
            for bind, variable in pairs:
                with self.assertRaisesRegex(RuntimeError, "render failed"), bind(None):
                    self.assertIsNone(variable.get())
                    raise RuntimeError("render failed")
                self.assertEqual([variable.get() for _, variable in pairs], values)
        self.assertEqual([variable.get() for _, variable in pairs], defaults)

    def test_interleaved_request_contexts_do_not_share_values(self):
        pairs = self.bindings()
        contexts = [copy_context(), copy_context()]
        managers = []
        expected = []
        for index, context in enumerate(contexts):
            values = [lambda index=index: {"request": index} for _ in range(3)]
            values += [lambda: datetime(2026, 9, 20, tzinfo=UTC), bool(index)]
            stack = ExitStack()
            for (bind, _), value in zip(pairs, values, strict=True):
                context.run(stack.enter_context, bind(value))
            managers.append(stack)
            expected.append(values)
        try:
            for index in (0, 1, 0, 1):
                self.assertEqual(contexts[index].run(lambda: [v.get() for _, v in pairs]), expected[index])
        finally:
            for context, stack in zip(contexts, managers, strict=True):
                context.run(stack.close)
        for context in contexts:
            self.assertEqual(context.run(lambda: [v.get() for _, v in pairs]), [v.get() for _, v in pairs])

    def test_token_reset_is_once_and_in_reverse_order_after_failure(self):
        variable = ContextVar("binding-test", default="default")
        observed = mock.Mock(wraps=variable)
        with (self.assertRaisesRegex(RuntimeError, "failed"),
              page_values._bind(observed, "outer"), page_values._bind(observed, "inner")):
            self.assertEqual(variable.get(), "inner")
            raise RuntimeError("failed")
        self.assertEqual(variable.get(), "default")
        tokens = [call.args[0] for call in observed.reset.call_args_list]
        self.assertEqual(len(tokens), 2)
        self.assertEqual([token.old_value for token in tokens], ["outer", Token.MISSING])

    def test_wrappers_control_renderer_reads_and_reset_after_source_failure(self):
        for bind, variable in self.bindings()[:2]:
            default = variable.get()
            read = mock.Mock(side_effect=OSError("source failed"))
            with self.assertRaisesRegex(OSError, "source failed"), bind(read):
                pages.error(404, "code", "text")
            read.assert_called_once()
            self.assertIs(variable.get(), default)
        read = mock.Mock(side_effect=OSError("source failed"))
        with pages.bell_source(read):
            self.assertIn("bell-unknown", pages.error(404, "code", "text"))
        read.assert_called_once()
        self.assertIsNone(page_values._BELL_SOURCE.get())
        with pages.from_post(True):
            self.assertNotIn(page_assets._REFRESH_SCRIPT, pages.error(404, "code", "text"))
        self.assertIn(page_assets._REFRESH_SCRIPT, pages.error(404, "code", "text"))
        self.assertEqual(page_shell._bell(), page_shell._bell())
