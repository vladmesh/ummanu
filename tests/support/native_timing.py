"""In-memory stdlib producers for timing outcome and receipt regressions."""

import unittest
from types import ModuleType


def native_outcome_modules(now, kind):
    def module_with_case(name, duration):
        module = ModuleType(name)

        class Case(unittest.TestCase):
            def test_body(self):
                now[0] += duration

        Case.__module__, Case.__qualname__ = name, "Case"
        module.Case = Case
        return module

    before = module_with_case("tests.native_timing_before", 1)
    middle = module_with_case("tests.native_timing_middle", 3)
    after = module_with_case("tests.native_timing_after", 2)
    if kind.startswith("load_"):
        def load_tests(loader, tests, pattern):
            if kind == "load_failure":
                raise AssertionError("native load_tests assertion")
            raise ValueError("native load_tests error")
        middle.load_tests = load_tests
    elif kind.startswith("fixture_"):
        def setup(cls):
            now[0] += 3
            if kind == "fixture_skip":
                raise unittest.SkipTest("native fixture skip")
            raise AssertionError("native fixture error")
        middle.Case.setUpClass = classmethod(setup)
    else:
        def test_body(self):
            now[0] += 3
            with self.subTest(part="skip"):
                self.skipTest("native subtest skip")
        middle.Case.test_body = test_body
    return before, middle, after
