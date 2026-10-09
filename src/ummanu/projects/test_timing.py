"""Timing observations shared by the declared project unittest profile and CI.

Test time spans startTest/stopTest (setUp, body, tearDown and test cleanups).
Module time spans its complete suite, including class/module fixtures and cleanups.
The local runner only warns; CI applies budgets to unit/component observations.
"""

from __future__ import annotations

import dataclasses
import math
import time
import unittest
from collections import OrderedDict

TEST_LIMIT = 5.0
MODULE_LIMIT = 90.0


@dataclasses.dataclass
class TestRecord:
    identifier: str
    classname: str
    name: str
    duration_seconds: float | None
    outcome: str = "passed"
    detail: str | None = None
    module: str = ""


class TimingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records: dict[str, TestRecord] = {}
        self._started: dict[str, float] = {}
        self.module_durations: dict[str, float] = {}
        self.timing_complete = False

    def _record(self, test):
        identifier = test.id()
        if identifier not in self.records:
            classname, _, name = identifier.rpartition(".")
            self.records[identifier] = TestRecord(
                identifier, classname, name, None,
                module=(getattr(self, "active_module", "")
                        if isinstance(test, unittest.suite._ErrorHolder) else test.__class__.__module__)
            )
        return self.records[identifier]

    def startTest(self, test):
        self._record(test)
        self._started[test.id()] = time.monotonic()
        super().startTest(test)

    def stopTest(self, test):
        started = self._started.pop(test.id(), None)
        self._record(test).duration_seconds = (
            time.monotonic() - started if started is not None else None
        )
        super().stopTest(test)

    def _mark(self, test, outcome, detail=None):
        record = self._record(test)
        record.outcome, record.detail = outcome, detail

    def addFailure(self, test, err):
        self._mark(test, "failed", self._exc_info_to_string(err, test))
        super().addFailure(test, err)

    def addError(self, test, err):
        self._mark(test, "error", self._exc_info_to_string(err, test))
        super().addError(test, err)

    def addSkip(self, test, reason):
        self._mark(test, "skipped", reason)
        super().addSkip(test, reason)

    def addExpectedFailure(self, test, err):
        self._mark(test, "expected_failure", self._exc_info_to_string(err, test))
        super().addExpectedFailure(test, err)

    def addUnexpectedSuccess(self, test):
        self._mark(test, "unexpected_success")
        super().addUnexpectedSuccess(test)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self._mark(test, "failed" if issubclass(err[0], test.failureException) else "error",
                       self._exc_info_to_string(err, test))
        super().addSubTest(test, subtest, err)

    def observation(self):
        return {
            "status": "complete" if self.timing_complete and not self._started
                      and all(r.duration_seconds is not None for r in self.records.values()) else "incomplete",
            "tests": [dataclasses.asdict(record) for record in self.records.values()],
            "modules": self.module_durations,
        }


class TimingSuite(unittest.TestSuite):
    """Keep all classes of each actual module inside one fixture-inclusive clock."""

    def run(self, result, debug=False):
        grouped = OrderedDict()

        def collect(suite):
            for test in suite:
                if isinstance(test, unittest.TestSuite):
                    collect(test)
                else:
                    grouped.setdefault(test.__class__.__module__, []).append(test)

        collect(self)
        for module, tests in grouped.items():
            if result.shouldStop:
                return result
            started = time.monotonic()
            result.active_module = module
            # Each module is a top-level stdlib suite, so its final class/module teardown
            # and cleanups finish before the module clock stops.
            result._testRunEntered = False
            result._previousTestClass = None
            result._moduleSetUpFailed = False
            unittest.TestSuite(tests).run(result, debug)
            result.module_durations[module] = time.monotonic() - started
        result.timing_complete = not result.shouldStop
        return result


class TimingRunner(unittest.TextTestRunner):
    resultclass = TimingResult

    def run(self, test):
        return super().run(TimingSuite([test]))


def valid_observation(value):
    def duration(number):
        return (isinstance(number, (float, int)) and not isinstance(number, bool)
                and math.isfinite(number) and number >= 0)

    if (not isinstance(value, dict) or value.get("status") not in {"complete", "incomplete"}
            or not isinstance(value.get("tests"), list) or not isinstance(value.get("modules"), dict)):
        return False
    for record in value["tests"]:
        if (not isinstance(record, dict)
                or not all(isinstance(record.get(key), str) and record[key] for key in ("identifier", "module"))
                or record.get("outcome") not in {"passed", "failed", "error", "skipped", "expected_failure", "unexpected_success"}
                or (record.get("duration_seconds") is None and value["status"] == "complete")
                or (record.get("duration_seconds") is not None and not duration(record["duration_seconds"]))):
            return False
    return all(isinstance(module, str) and module and duration(seconds)
               for module, seconds in value["modules"].items())


def violations(observation, *, modules=False):
    found = []
    for record in observation.get("tests", []):
        duration = record.get("duration_seconds")
        if duration is not None and duration > TEST_LIMIT:
            found.append({"kind": "test", "identifier": record["identifier"],
                          "duration_seconds": duration, "limit_seconds": TEST_LIMIT})
    if modules:
        for module, duration in observation.get("modules", {}).items():
            if duration > MODULE_LIMIT:
                found.append({"kind": "module", "identifier": module,
                              "duration_seconds": duration, "limit_seconds": MODULE_LIMIT})
    return found


def diagnostic(violation):
    return (f"timing budget {violation['kind']} {violation['identifier']}: "
            f"{violation['duration_seconds']:.6f}s > {violation['limit_seconds']:g}s")


def summary(observation):
    lines = [f"timing: {observation.get('status', 'unavailable')}"]
    records = [record for record in observation.get("tests", [])
               if record.get("duration_seconds") is not None]
    for record in sorted(records, key=lambda r: r['duration_seconds'], reverse=True)[:10]:
        lines.append(f"slow test {record['identifier']}: {record['duration_seconds']:.6f}s ({record['outcome']})")
    lines.extend(f"WARNING: {diagnostic(item)}" for item in violations(observation))
    return "\n".join(lines)
