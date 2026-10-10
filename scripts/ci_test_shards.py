#!/usr/bin/env python3
"""Validate and run the explicit top-level unittest suites used by GitHub CI."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import traceback
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TextIO

# Import-by-path consumers must use the helper beside their candidate runner.
_selection_path = Path(__file__).resolve().with_name("ci_selection.py")
_selection_spec = importlib.util.spec_from_file_location(f"{__name__}._selection", _selection_path)
if _selection_spec is None or _selection_spec.loader is None:
    raise ImportError(f"selection helper unavailable: {_selection_path}")
ci_selection = importlib.util.module_from_spec(_selection_spec)
_selection_spec.loader.exec_module(ci_selection)

# The installed wrapper imports this file by path to validate the candidate manifest.
# Resolve its stdlib-only collector from that same candidate, even before an upgrade.
_timing_path = Path(__file__).resolve().parents[1] / "src/ummanu/projects/test_timing.py"
_timing_spec = importlib.util.spec_from_file_location(f"{__name__}._candidate_test_timing", _timing_path)
if _timing_spec is None or _timing_spec.loader is None:
    raise ImportError(f"timing helper unavailable: {_timing_path}")
_timing = importlib.util.module_from_spec(_timing_spec)
# Dataclasses need registration only while the helper's definitions execute.
# Keep the candidate module by reference afterwards; never register/reuse the
# installed canonical ummanu.projects.test_timing identity.
_timing_registered = _timing_spec.name in sys.modules
_prior_timing = sys.modules.get(_timing_spec.name)
sys.modules[_timing_spec.name] = _timing
try:
    _timing_spec.loader.exec_module(_timing)
finally:
    if _timing_registered:
        sys.modules[_timing_spec.name] = _prior_timing
    else:
        del sys.modules[_timing_spec.name]
TestRecord, TimingResult, TimingSuite = _timing.TestRecord, _timing.TimingResult, _timing.TimingSuite
diagnostic, violations = _timing.diagnostic, _timing.violations
native_diagnostic = _timing.native_diagnostic
junit_tag = _timing.junit_tag

SUITES = (
    "unit",
    "component",
    "runtime-component",
    "integration-recovery",
    "integration-memory",
    "integration-board",
    "integration-dispatcher",
    "integration-heads",
    "packaging",
)

FAST_MODULES = (
    "tests.test_hermetic_board",
    "tests.test_hermetic_pipeline_state",
)
FAST_TIMEOUT_SECONDS = 120
_FAST_TERMINATE_GRACE_SECONDS = 5
EVIDENCE_SCHEMA_VERSION = 1
MAX_LOG_BYTES = 1_000_000
SLOWEST_TEST_LIMIT = 10
MAX_STATUS_CHANGE_SAMPLES = 10
MAX_STATUS_CHANGE_CHARS = 200
EVIDENCE_FILES = ("report.json", "junit.xml", "test-output.log")
COVERAGE_SOURCE_ROOTS = ("src/ummanu",)
COVERAGE_RAW_PREFIX = "coverage."
COVERAGE_JSON_NAME = "combined-coverage.json"
CHANGED_LINES_JSON_NAME = "changed-lines.json"
MAX_COVERAGE_JSON_BYTES = 5_000_000
# `coverage json` also writes per-function and per-class regions, which restate the file's line and
# branch lists and dominate its size (about 85% for the full product). They are read under this looser
# intake bound and not published; the published artifact keeps exactly these per-file keys.
MAX_NATIVE_COVERAGE_JSON_BYTES = 50_000_000
COVERAGE_LIST_KEYS = (
    "executed_lines",
    "missing_lines",
    "excluded_lines",
    "executed_branches",
    "missing_branches",
)
COVERAGE_FILE_KEYS = (*COVERAGE_LIST_KEYS, "summary")
MAX_CHANGED_LINES = 10_000
OUTCOMES = (
    "success",
    "product_failure",
    "infrastructure_failure",
    "cancelled",
    "not_applicable",
)

# Loaded automatically by the fast profile's child interpreter. The selected
# tests still exercise their normal seams, while this makes an accidental new
# network or host-tool dependency fail at its boundary rather than consulting
# a control host's operational state.
_FAST_GUARD = """\
import os
import socket
import subprocess
import sys


def _deny(kind):
    raise RuntimeError(f"fast test profile forbids {kind}")


class _NoNetworkSocket(socket.socket):
    def connect(self, address):
        _deny("network access")

    def connect_ex(self, address):
        _deny("network access")


socket.socket = _NoNetworkSocket
socket.create_connection = lambda *args, **kwargs: _deny("network access")
_popen = subprocess.Popen


def _guarded_popen(args, *positional, **keyword):
    command = args[0] if isinstance(args, (list, tuple)) else args
    if os.path.realpath(os.fspath(command)) != os.path.realpath(sys.executable):
        if not (
            os.path.basename(os.fspath(command)) == "git"
            and isinstance(args, (list, tuple))
            and (
                any(action in args for action in ("log", "ls-files", "check-ignore"))
                or (
                    "config" in args
                    and list(args[args.index("config") + 1:]) == ["--get", "remote.origin.url"]
                )
            )
        ):
            _deny("external command execution")
    return _popen(args, *positional, **keyword)


subprocess.Popen = _guarded_popen
os.system = lambda *args, **kwargs: _deny("external command execution")
"""


class ManifestError(ValueError):
    pass


class EvidenceError(ValueError):
    pass


class CheckoutStatusError(RuntimeError):
    pass


class CoverageError(ValueError):
    pass


def _required_integration_setup_reason(error: object) -> str | None:
    """Recognize only the explicit test-fixture setup signal, never arbitrary errors."""
    try:
        from tests.integration_setup import RequiredIntegrationSetup
    except ImportError:
        return None
    if not isinstance(error, tuple) or len(error) < 2:
        return None
    exception = error[1]
    if isinstance(exception, RequiredIntegrationSetup):
        return str(exception) or "required integration setup is unavailable"
    return None


@dataclasses.dataclass
class CheckoutStatus:
    before_entries: int
    before_sha256: str
    after_entries: int
    after_sha256: str
    changed: bool
    changed_entry_count: int
    changed_entries: list[str]
    omitted_changed_entries: int

    def as_json(self) -> dict[str, object]:
        return {
            "before": {"entries": self.before_entries, "sha256": self.before_sha256},
            "after": {"entries": self.after_entries, "sha256": self.after_sha256},
            "changed": self.changed,
            "changed_entry_count": self.changed_entry_count,
            "changed_entries": self.changed_entries,
            "omitted_changed_entries": self.omitted_changed_entries,
        }


@dataclasses.dataclass
class SuiteEvidence:
    suite: str
    candidate_sha: str
    outcome: str
    counts: dict[str, int]
    duration_seconds: float
    slowest_tests: list[TestRecord]
    failure_locations: list[str]
    log_truncated: bool = False
    detail: str | None = None
    test_records: list[TestRecord] = dataclasses.field(default_factory=list, repr=False)
    checkout_status: CheckoutStatus | None = None
    timing: dict = dataclasses.field(default_factory=dict)
    timing_violations: list[dict] = dataclasses.field(default_factory=list)
    selection_digest: str | None = None
    selected_modules: list[str] = dataclasses.field(default_factory=list)
    executed_modules: list[str] = dataclasses.field(default_factory=list)

    def as_json(self) -> dict[str, object]:
        return {
            "schema_version": 2 if self.selection_digest is not None else EVIDENCE_SCHEMA_VERSION,
            "selection_digest": self.selection_digest,
            "selected_modules": self.selected_modules,
            "executed_modules": self.executed_modules,
            "suite": self.suite,
            "candidate_sha": self.candidate_sha,
            "outcome": self.outcome,
            "counts": self.counts,
            "duration_seconds": round(self.duration_seconds, 3),
            "slowest_tests": [dataclasses.asdict(record) for record in self.slowest_tests],
            "failure_locations": self.failure_locations,
            "timing": self.timing or {"status": "unavailable"},
            "timing_violations": self.timing_violations,
            "log_truncated": self.log_truncated,
            "detail": self.detail,
            "artifacts": {"junit": "junit.xml", "log": "test-output.log"},
            "checkout_status": self.checkout_status.as_json() if self.checkout_status else None,
        }


class BoundedTee:
    """Copy test output to Actions while retaining one bounded artifact log."""

    def __init__(self, console: TextIO, path: Path, maximum_bytes: int = MAX_LOG_BYTES) -> None:
        self.console = console
        self.path = path
        self.maximum_bytes = maximum_bytes
        self.written_bytes = 0
        self.truncated = False
        self._log = path.open("w", encoding="utf-8")

    def write(self, value: str) -> int:
        self.console.write(value)
        encoded = value.encode("utf-8")
        remaining = self.maximum_bytes - self.written_bytes
        if remaining > 0:
            clipped = encoded[:remaining]
            self._log.write(clipped.decode("utf-8", errors="ignore"))
            self.written_bytes += len(clipped)
        if len(encoded) > max(remaining, 0):
            self.truncated = True
        return len(value)

    def flush(self) -> None:
        self.console.flush()
        self._log.flush()

    def close(self) -> None:
        if self.truncated:
            self._log.write("\n[CI evidence log truncated at 1000000 bytes]\n")
        self._log.close()


class EvidenceResult(TimingResult):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.required_integration_setup_reasons: list[str] = []

    def addError(self, test: unittest.case.TestCase, err: object) -> None:
        if reason := _required_integration_setup_reason(err):
            self.required_integration_setup_reasons.append(reason)
        super().addError(test, err)


def _failure_location(identifier: str, detail: str | None) -> str:
    if detail:
        locations = re.findall(r'File "([^\"]+)", line (\d+)', detail)
        if locations:
            path, line = locations[-1]
            return f"{identifier}: {path}:{line}"
    return identifier


def _counts(result: EvidenceResult) -> dict[str, int]:
    failed = len(result.failures) + len(result.expectedFailures) + len(result.unexpectedSuccesses)
    errors = len(result.errors)
    skipped = len(result.skipped)
    return {
        "collected": result.testsRun,
        "passed": result.successful_tests,
        "failed": failed,
        "error": errors,
        "skipped": skipped,
    }


def _empty_counts() -> dict[str, int]:
    return {"collected": 0, "passed": 0, "failed": 0, "error": 0, "skipped": 0}


def _checkout_snapshot(root: Path) -> str:
    """Return Git's complete tracked and untracked checkout-state snapshot."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=root,
            capture_output=True,
            check=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CheckoutStatusError("git status command failed") from exc
    return result.stdout


def _bounded_status_entry(entry: str) -> str:
    if len(entry) <= MAX_STATUS_CHANGE_CHARS:
        return entry
    return f"{entry[:MAX_STATUS_CHANGE_CHARS]}..."


def _checkout_status(before: str, after: str) -> CheckoutStatus:
    before_entries = before.splitlines()
    after_entries = after.splitlines()
    changed_entries = [f"- {entry}" for entry in before_entries if entry not in after_entries]
    changed_entries.extend(f"+ {entry}" for entry in after_entries if entry not in before_entries)
    samples = [_bounded_status_entry(entry) for entry in changed_entries[:MAX_STATUS_CHANGE_SAMPLES]]
    return CheckoutStatus(
        len(before_entries),
        hashlib.sha256(before.encode()).hexdigest(),
        len(after_entries),
        hashlib.sha256(after.encode()).hexdigest(),
        before != after,
        len(changed_entries),
        samples,
        max(len(changed_entries) - len(samples), 0),
    )


def _checkout_failure_detail(test_outcome: str, reason: str) -> str:
    if test_outcome == "product_failure":
        return f"test outcome before checkout verification: product_failure; {reason}"
    return reason


def run_reported_suite(
    suite_name: str, paths: list[str], candidate_sha: str, log: BoundedTee
) -> SuiteEvidence:
    """Run one manifest suite and retain the unittest facts needed by CI evidence."""
    started = time.monotonic()
    loader = unittest.defaultTestLoader
    result = None
    loaded = []

    def collect_result(*args, **kwargs):
        nonlocal result
        result = EvidenceResult(*args, **kwargs)
        return result

    def interrupted_evidence(outcome, location, detail=None):
        records = list(result.records.values()) if result is not None else []
        return SuiteEvidence(
            suite_name, candidate_sha, outcome,
            _counts(result) if result is not None else _empty_counts(),
            time.monotonic() - started, [], [location], detail=detail,
            test_records=records,
            timing=result.observation() if result is not None else {"status": "unavailable"},
            selected_modules=modules(paths),
            executed_modules=list(result.observation().get("modules", {})) if result is not None else [],
        )

    try:
        sources = modules(paths)
        def collect():
            for module in sources:
                suite = loader.loadTestsFromName(module)
                loaded.append(module)
                yield suite
        selected = TimingSuite(collect(), source_modules=sources)
        runner = unittest.TextTestRunner(stream=log, verbosity=2, resultclass=collect_result)
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            result = runner.run(selected)
    except KeyboardInterrupt:
        return interrupted_evidence("cancelled", "test execution cancelled")
    except Exception as exc:  # noqa: BLE001 - this boundary must report unexpected runner failures.
        traceback.print_exc(file=log)
        return interrupted_evidence("infrastructure_failure", f"test runner error: {type(exc).__name__}", str(exc))

    timing = result.observation()
    for event in timing["native_outcomes"]:
        print(native_diagnostic(event), file=log)
    budget_violations = violations(timing, modules=True) if suite_name in {"unit", "component"} else []
    for violation in budget_violations:
        print(diagnostic(violation), file=log)
    records = list(result.records.values())
    failures = [record for record in [*records, *result.native_outcomes]
                if junit_tag(record.outcome) in {"failure", "error"}]
    counts = _counts(result)
    setup_reasons = result.required_integration_setup_reasons
    if setup_reasons:
        outcome = "infrastructure_failure"
        detail = f"required integration setup unavailable: {setup_reasons[0]}"
    else:
        outcome = "success" if result.wasSuccessful() and not budget_violations else "product_failure"
        detail = None
        if timing["status"] != "complete" and outcome == "success":
            outcome, detail = "infrastructure_failure", "incomplete test timing"
    return SuiteEvidence(
        suite_name,
        candidate_sha,
        outcome,
        counts,
        time.monotonic() - started,
        sorted(records, key=lambda record: record.duration_seconds if record.duration_seconds is not None else -1, reverse=True)[:SLOWEST_TEST_LIMIT],
        [_failure_location(record.identifier, record.detail) for record in failures]
        + [diagnostic(item) for item in budget_violations],
        detail=detail,
        test_records=records,
        timing=timing,
        timing_violations=budget_violations,
        selected_modules=sources,
        executed_modules=[module for module in loaded if module in timing["modules"]],
    )


def _write_junit_outcome(case, outcome, detail):
    tag = junit_tag(outcome)
    if tag == "skipped":
        ET.SubElement(case, tag, {"message": detail or "skipped"})
    elif tag is not None:
        ET.SubElement(case, tag).text = detail or outcome


def _write_junit(path: Path, evidence: SuiteEvidence) -> None:
    testsuite = ET.Element(
        "testsuite",
        {
            "name": evidence.suite,
            "tests": str(len(evidence.test_records) + len(evidence.timing.get("native_outcomes", []))
                         + len(evidence.timing_violations)),
            "failures": str(evidence.counts["failed"] + len(evidence.timing_violations)),
            "errors": str(evidence.counts["error"]),
            "skipped": str(evidence.counts["skipped"]),
            "time": f"{evidence.duration_seconds:.3f}",
            "hostname": evidence.candidate_sha,
        },
    )
    for record in evidence.test_records:
        case = ET.SubElement(
            testsuite,
            "testcase",
            {"classname": record.classname, "name": record.name,
             **({"time": f"{record.duration_seconds:.6f}"} if record.duration_seconds is not None else {})},
        )
        _write_junit_outcome(case, record.outcome, record.detail)
    for event in evidence.timing.get("native_outcomes", []):
        case = ET.SubElement(testsuite, "testcase", {
            "classname": event["module"], "name": event["identifier"], "phase": event["phase"],
        })
        _write_junit_outcome(case, event["outcome"], event.get("detail"))
    for item in evidence.timing_violations:
        case = ET.SubElement(testsuite, "testcase", {
            "classname": "timing_budget", "name": item["identifier"],
            "time": f"{item['duration_seconds']:.6f}",
        })
        ET.SubElement(case, "failure", {"message": diagnostic(item)}).text = diagnostic(item)
    ET.ElementTree(testsuite).write(path, encoding="utf-8", xml_declaration=True)


def _write_evidence(report_dir: Path, evidence: SuiteEvidence, log: BoundedTee) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    log.close()
    evidence.log_truncated = log.truncated
    _write_junit(report_dir / "junit.xml", evidence)
    (report_dir / "report.json").write_text(
        json.dumps(evidence.as_json(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _read_checkout_status(data: object) -> CheckoutStatus | None:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise EvidenceError("invalid checkout status")
    try:
        before = data["before"]
        after = data["after"]
        changed = data["changed"]
        changed_entry_count = data["changed_entry_count"]
        changed_entries = data["changed_entries"]
        omitted_changed_entries = data["omitted_changed_entries"]
        if not isinstance(before, dict) or not isinstance(after, dict):
            raise TypeError("snapshots must be objects")
        before_entries = before["entries"]
        before_sha256 = before["sha256"]
        after_entries = after["entries"]
        after_sha256 = after["sha256"]
        if (
            not all(
                isinstance(value, int) and value >= 0
                for value in (
                    before_entries,
                    after_entries,
                    changed_entry_count,
                    omitted_changed_entries,
                )
            )
            or type(changed) is not bool
            or not isinstance(changed_entries, list)
            or len(changed_entries) > MAX_STATUS_CHANGE_SAMPLES
            or not all(
                isinstance(entry, str) and len(entry) <= MAX_STATUS_CHANGE_CHARS + 3
                for entry in changed_entries
            )
            or not all(re.fullmatch(r"[0-9a-f]{64}", value) for value in (before_sha256, after_sha256))
            or changed_entry_count != len(changed_entries) + omitted_changed_entries
            or changed != (before_sha256 != after_sha256)
            or (
                not changed
                and (
                    before_entries != after_entries
                    or changed_entry_count != 0
                    or omitted_changed_entries != 0
                )
            )
        ):
            raise ValueError("invalid checkout status values")
    except (KeyError, TypeError, ValueError) as exc:
        raise EvidenceError(f"invalid checkout status: {exc}") from exc
    return CheckoutStatus(
        before_entries,
        before_sha256,
        after_entries,
        after_sha256,
        changed,
        changed_entry_count,
        changed_entries,
        omitted_changed_entries,
    )


def _read_evidence(report_dir: Path) -> SuiteEvidence:
    required = {name: report_dir / name for name in EVIDENCE_FILES}
    missing = [name for name, path in required.items() if not path.is_file() or path.stat().st_size == 0]
    if missing:
        raise EvidenceError(f"missing required evidence: {', '.join(missing)}")
    if any(path.stat().st_size > 5_000_000 for path in required.values()):
        raise EvidenceError("evidence exceeds intake bound")
    try:
        data = json.loads(required["report.json"].read_text(encoding="utf-8"),
                          object_pairs_hook=ci_selection.json_object)
        if data.get("schema_version") not in {EVIDENCE_SCHEMA_VERSION, 2}:
            raise EvidenceError("unsupported report schema")
        if data.get("outcome") not in OUTCOMES:
            raise EvidenceError("invalid report outcome")
        counts = data["counts"]
        if set(counts) != {"collected", "passed", "failed", "error", "skipped"}:
            raise EvidenceError("invalid report counts")
        if any(type(n) is not int or n < 0 for n in counts.values()):
            raise EvidenceError("invalid count values")
        if data.get("schema_version") == 2:
            if not re.fullmatch(r"[0-9a-f]{64}", data.get("selection_digest", "")):
                raise EvidenceError("invalid selection digest")
            for key in ("selected_modules", "executed_modules"):
                value = data.get(key)
                if (not isinstance(value, list) or not all(isinstance(n, str) for n in value)
                        or len(value) != len(set(value))):
                    raise EvidenceError("invalid module membership")
            if data["outcome"] == "success" and not _timing.valid_observation(data.get("timing")):
                raise EvidenceError("invalid successful timing observation")
        elif data.get("selection_digest") is not None:
            raise EvidenceError("schema 1 cannot attest a selection plan")
        if not data.get("suite") or not re.fullmatch(r"[0-9a-f]{40,64}", data.get("candidate_sha", "")):
            raise EvidenceError("report lacks an exact candidate SHA")
        junit = ET.parse(required["junit.xml"]).getroot()
        if data.get("schema_version") == 2:
            cases = junit.findall("testcase")
            timing = data.get("timing", {})
            expected_cases = [(r["classname"], r["name"]) for r in timing.get("tests", [])]
            expected_cases.extend((r["module"], r["identifier"]) for r in timing.get("native_outcomes", []))
            expected_cases.extend(("timing_budget", r["identifier"]) for r in data.get("timing_violations", []))
            if (junit.tag != "testsuite" or junit.get("name") != data["suite"]
                    or junit.get("hostname") != data["candidate_sha"]
                    or junit.get("tests") != str(len(cases))
                    or [(case.get("classname"), case.get("name")) for case in cases] != expected_cases
                    or junit.get("failures") != str(counts["failed"] + len(data.get("timing_violations", [])))
                    or junit.get("errors") != str(counts["error"]) or junit.get("skipped") != str(counts["skipped"])):
                raise EvidenceError("JUnit does not match suite execution evidence")
        checkout_status = _read_checkout_status(data.get("checkout_status"))
    except (KeyError, TypeError, ValueError, ET.ParseError) as exc:
        raise EvidenceError(f"malformed evidence: {exc}") from exc
    return SuiteEvidence(
        data["suite"],
        data["candidate_sha"],
        data["outcome"],
        counts,
        float(data["duration_seconds"]),
        [TestRecord(**record) for record in data.get("slowest_tests", [])],
        list(data.get("failure_locations", [])),
        bool(data.get("log_truncated")),
        data.get("detail"),
        checkout_status=checkout_status,
        timing=data.get("timing", {}),
        timing_violations=data.get("timing_violations", []),
        selection_digest=data.get("selection_digest"),
        selected_modules=data.get("selected_modules", []),
        executed_modules=data.get("executed_modules", []),
    )


def _summary(evidence: SuiteEvidence) -> str:
    counts = evidence.counts
    lines = [
        f"## CI suite: {evidence.suite}",
        "",
        f"- Candidate SHA: `{evidence.candidate_sha}`",
        f"- Outcome: `{evidence.outcome}`",
        f"- Selection digest: `{evidence.selection_digest}`",
        f"- Selected modules: {', '.join(evidence.selected_modules)}",
        f"- Executed modules: {', '.join(evidence.executed_modules)}",
        f"- Counts: collected {counts['collected']}, passed {counts['passed']}, failed {counts['failed']}, errors {counts['error']}, skipped {counts['skipped']}",
        f"- Duration: {evidence.duration_seconds:.3f}s",
        f"- Artifacts: `junit.xml`, `test-output.log`{' (truncated at 1,000,000 bytes)' if evidence.log_truncated else ''}",
    ]
    if evidence.failure_locations:
        lines.extend(
            ["- Failure locations:", *[f"  - `{location}`" for location in evidence.failure_locations]]
        )
    if evidence.slowest_tests:
        lines.extend(
            [
                "- Slowest tests:",
                *[
                    f"  - `{record.identifier}` ({record.duration_seconds}s)"
                    for record in evidence.slowest_tests
                ],
            ]
        )
    lines.extend(f"- {diagnostic(item)}" for item in evidence.timing_violations)
    lines.extend(f"- {native_diagnostic(event)}" for event in evidence.timing.get("native_outcomes", []))
    if evidence.detail:
        lines.append(f"- Detail: {evidence.detail}")
    if evidence.checkout_status:
        status = evidence.checkout_status
        if status.changed:
            lines.append(
                "- Checkout status: changed "
                f"(before {status.before_entries} entries, after {status.after_entries}; "
                f"{status.changed_entry_count} changed entries)"
            )
            if status.changed_entries:
                lines.extend(
                    [
                        "- Changed checkout status entries:",
                        *[f"  - {json.dumps(entry)}" for entry in status.changed_entries],
                    ]
                )
            if status.omitted_changed_entries:
                lines.append(f"  - {status.omitted_changed_entries} additional entries omitted")
        else:
            lines.append(
                "- Checkout status: unchanged "
                f"({status.before_entries} entries; SHA-256 `{status.before_sha256}`)"
            )
    return "\n".join(lines) + "\n"


def _outcome_code(outcome: str) -> int:
    return {
        "success": 0,
        "not_applicable": 0,
        "product_failure": 1,
        "infrastructure_failure": 3,
        "cancelled": 130,
    }[outcome]


def report_summary(report_dir: Path) -> int:
    try:
        evidence = _read_evidence(report_dir)
    except EvidenceError as exc:
        print(f"## CI suite evidence infrastructure failure\n\n- {exc}")
        return 3
    print(_summary(evidence), end="")
    return _outcome_code(evidence.outcome)


def aggregate_evidence(evidence_dir: Path, needs_result: str, plan: dict | None = None) -> int:
    reports: dict[str, SuiteEvidence] = {}
    required = plan["selected"] if plan else {suite: [] for suite in SUITES}
    invalid = []
    if plan and evidence_dir.is_dir():
        for path in evidence_dir.rglob("*"):
            if path.is_file() and path.name in {"junit.xml", "test-output.log"} and not (path.parent / "report.json").is_file():
                invalid.append(f"orphan evidence: {path}")
    for path in evidence_dir.rglob("report.json") if evidence_dir.is_dir() else ():
        try:
            evidence = _read_evidence(path.parent)
            if evidence.suite not in required or evidence.suite in reports:
                raise EvidenceError("duplicate or foreign suite evidence")
            if evidence.outcome == "not_applicable":
                raise EvidenceError("required suite cannot attest not_applicable")
            if reports and evidence.candidate_sha != next(iter(reports.values())).candidate_sha:
                raise EvidenceError("suite evidence mixes candidate SHAs")
            if plan:
                expected = modules(required[evidence.suite])
                if (evidence.candidate_sha != plan["candidate_sha"]
                        or evidence.selection_digest != ci_selection.plan_digest(plan)
                        or evidence.selected_modules != expected or evidence.executed_modules != expected):
                    raise EvidenceError("wrong SHA, plan or selected/executed module membership")
                if evidence.outcome == "success":
                    timing = evidence.timing
                    observed = {r["module"] for r in timing.get("tests", [])}
                    observed.update(r["module"] for r in timing.get("native_outcomes", []))
                    observed.update(timing.get("modules", {}))
                    if (not evidence.checkout_status or evidence.checkout_status.changed
                            or timing.get("status") != "complete" or not observed <= set(expected)
                            or evidence.counts["collected"] <= 0 or evidence.counts["failed"]
                            or evidence.counts["error"]):
                        raise EvidenceError("successful suite lacks intact execution evidence")
        except (EvidenceError, KeyError, TypeError, ValueError) as exc:
            invalid.append(f"{path}: {exc}")
            continue
        reports[evidence.suite] = evidence
    outcomes: dict[str, str] = {}
    for suite in SUITES:
        evidence = reports.get(suite)
        if suite not in required:
            outcomes[suite] = "not_applicable"
        elif evidence:
            outcomes[suite] = evidence.outcome
        elif needs_result == "cancelled":
            outcomes[suite] = "cancelled"
        else:
            outcomes[suite] = "infrastructure_failure"
    lines = ["## Required CI test aggregate", "", *[f"- `{suite}`: `{outcomes[suite]}`" for suite in SUITES]]
    if plan:
        lines.insert(0, ci_selection.summary(plan))
    lines.extend(f"- Invalid evidence: {detail}" for detail in invalid)
    print("\n".join(lines))
    if invalid:
        return 3
    if "infrastructure_failure" in outcomes.values():
        return 3
    if "product_failure" in outcomes.values():
        return 1
    if "cancelled" in outcomes.values():
        return 130
    if needs_result != ("success" if required else "skipped"):
        return 3
    return 0


def _exact_sha(value: str, label: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40,64}", value):
        raise CoverageError(f"{label} is not an exact SHA")
    return value


def _suite_coverage_data(coverage_dir: Path, candidate_sha: str, suites=SUITES) -> list[Path]:
    paths: list[Path] = []
    for suite in suites:
        path = coverage_dir / f"ci-coverage-{suite}-{candidate_sha}" / f"{COVERAGE_RAW_PREFIX}{suite}"
        if not path.is_file() or path.stat().st_size == 0:
            raise CoverageError(f"missing or empty raw coverage datum for {suite} at {path}")
        paths.append(path)
    actual = set(coverage_dir.rglob("coverage.*")) if coverage_dir.is_dir() else set()
    if actual != set(paths):
        raise CoverageError("foreign or duplicate raw coverage data")
    return paths


def _validate_coverage_datum(path: Path) -> None:
    """Reject corrupt or incompatible coverage databases before combining."""
    try:
        from coverage import CoverageData
        from coverage.exceptions import CoverageException
    except ImportError as exc:
        raise CoverageError("coverage.py is unavailable for aggregate validation") from exc
    try:
        data = CoverageData(basename=str(path))
        data.read()
        data.measured_files()
    except (CoverageException, OSError, ValueError) as exc:
        raise CoverageError(f"unreadable or incompatible raw coverage datum at {path}: {exc}") from exc


def _run_coverage(command: list[str], root: Path) -> None:
    try:
        subprocess.run(command, cwd=root, capture_output=True, check=True, text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = (
            exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else str(exc)
        )
        raise CoverageError(f"coverage command failed: {detail}") from exc


def _candidate_checkout(root: Path, candidate_sha: str) -> None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, check=True, text=True
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CoverageError(f"cannot verify candidate checkout: {exc}") from exc
    if result.stdout.strip() != candidate_sha:
        raise CoverageError("coverage aggregate checkout does not match the candidate SHA")


def _source_file(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return any(normalized.startswith(f"{root}/") for root in COVERAGE_SOURCE_ROOTS)


def _coverage_payload(path: Path) -> dict[str, object]:
    """The native `coverage json` output, validated, with each file reduced to `COVERAGE_FILE_KEYS`."""
    if not path.is_file() or path.stat().st_size > MAX_NATIVE_COVERAGE_JSON_BYTES:
        raise CoverageError("combined coverage JSON is missing, empty, or exceeds its bound")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        files = data["files"]
        if not isinstance(files, dict) or not files:
            raise TypeError("files is empty or not an object")
        reduced: dict[str, dict[str, object]] = {}
        for filename, payload in files.items():
            if not isinstance(filename, str) or not _source_file(filename) or not isinstance(payload, dict):
                raise TypeError("coverage includes a file outside the configured source roots")
            for key in COVERAGE_LIST_KEYS:
                if not isinstance(payload.get(key), list):
                    raise TypeError(f"coverage file lacks {key}")
            summary = payload.get("summary")
            if not isinstance(summary, dict) or not isinstance(summary.get("num_branches"), int):
                raise TypeError("coverage file lacks branch summary")
            reduced[filename] = {key: payload[key] for key in COVERAGE_FILE_KEYS}
        data["files"] = reduced
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise CoverageError(f"malformed combined coverage JSON: {exc}") from exc
    return data


def _changed_candidate_lines(root: Path, base_sha: str, candidate_sha: str) -> dict[str, list[int]]:
    _exact_sha(base_sha, "base SHA")
    _exact_sha(candidate_sha, "candidate SHA")
    # Diffed over the roots' parent directories with rename detection, so a file moved into a
    # root from beside it (a package rename) reports the lines it changed, not every line as new.
    # Only files under a root are kept below.
    pathspecs = sorted(
        {root_path.rsplit("/", 1)[0] if "/" in root_path else "." for root_path in COVERAGE_SOURCE_ROOTS}
    )
    try:
        result = subprocess.run(
            [
                "git",
                "diff",
                "--no-ext-diff",
                "--unified=0",
                "--find-renames",
                base_sha,
                candidate_sha,
                "--",
                *pathspecs,
            ],
            cwd=root,
            capture_output=True,
            check=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CoverageError(f"cannot diff exact base and candidate SHAs: {exc}") from exc
    changed: dict[str, list[int]] = {}
    filename: str | None = None
    for line in result.stdout.splitlines():
        if line.startswith("+++ b/"):
            candidate = line.removeprefix("+++ b/")
            filename = candidate if _source_file(candidate) else None
            continue
        match = re.match(r"^@@ -[^ ]+ \+(\d+)(?:,(\d+))? @@", line)
        if match and filename is not None:
            start, count = (int(value) if value else 1 for value in match.groups())
            if count:
                changed.setdefault(filename, []).extend(range(start, start + count))
    total = sum(len(lines) for lines in changed.values())
    if total > MAX_CHANGED_LINES:
        raise CoverageError(f"changed executable-line report exceeds its {MAX_CHANGED_LINES}-line bound")
    return changed


def _changed_line_report(
    coverage: dict[str, object], changed: dict[str, list[int]], *, base_sha: str, candidate_sha: str
) -> dict[str, object]:
    files = coverage["files"]
    assert isinstance(files, dict)
    entries: list[dict[str, object]] = []
    for filename in sorted(changed):
        payload = files.get(filename, {})
        if not isinstance(payload, dict):
            payload = {}
        executed = set(payload.get("executed_lines", []))
        missing = set(payload.get("missing_lines", []))
        excluded = set(payload.get("excluded_lines", []))
        for number in sorted(set(changed[filename])):
            classification = (
                "excluded"
                if number in excluded
                else "covered"
                if number in executed
                else "missed"
                if number in missing
                else "unmeasured"
                if filename not in files
                else "not_executable"
            )
            entries.append({"path": filename, "line": number, "classification": classification})
    return {
        "schema_version": 1,
        "base_sha": base_sha,
        "candidate_sha": candidate_sha,
        "applicable": True,
        "reason": "changed candidate source lines classified from the exact GitHub pull-request base SHA",
        "line_limit": MAX_CHANGED_LINES,
        "lines": entries,
    }


def aggregate_coverage(
    root: Path, coverage_dir: Path, output_dir: Path, candidate_sha: str, base_sha: str | None,
    plan: dict | None = None,
) -> int:
    try:
        candidate_sha = _exact_sha(candidate_sha, "candidate SHA")
        _candidate_checkout(root, candidate_sha)
        raw_data = _suite_coverage_data(coverage_dir, candidate_sha, plan["selected"] if plan else SUITES)
        if not raw_data:
            if not plan or plan["mode"] != "docs-only":
                raise CoverageError("no applicable coverage without a validated docs-only plan")
            print(ci_selection.summary(plan))
            print("- Coverage: not applicable to validated docs-only selection")
            return 0
        for path in raw_data:
            _validate_coverage_datum(path)
        output_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="ummanu-coverage-") as work:
            # coverage combine recognizes inputs by the basename set through
            # --data-file. Keep that basename aligned with coverage.<suite>,
            # while keeping its intermediate SQLite data outside the published
            # aggregate artifact directory.
            combined_data = Path(work) / "coverage"
            raw_json = output_dir / ".coverage.native.json"
            _run_coverage(
                [
                    sys.executable,
                    "-m",
                    "coverage",
                    "combine",
                    "--data-file",
                    str(combined_data),
                    *map(str, raw_data),
                ],
                root,
            )
            _run_coverage(
                [
                    sys.executable,
                    "-m",
                    "coverage",
                    "json",
                    "--data-file",
                    str(combined_data),
                    "-o",
                    str(raw_json),
                ],
                root,
            )
        coverage = _coverage_payload(raw_json)
        if base_sha:
            changed_report = _changed_line_report(
                coverage,
                _changed_candidate_lines(root, base_sha, candidate_sha),
                base_sha=base_sha,
                candidate_sha=candidate_sha,
            )
        else:
            changed_report = {
                "schema_version": 1,
                "candidate_sha": candidate_sha,
                "applicable": False,
                "reason": "not applicable: this event has no pull-request base SHA",
                "line_limit": MAX_CHANGED_LINES,
                "lines": [],
            }
        combined = {
            "schema_version": 1,
            "candidate_sha": candidate_sha,
            "selection_mode": plan["mode"] if plan else "full",
            "selection_digest": ci_selection.plan_digest(plan) if plan else None,
            "selected": plan["selected"] if plan else None,
            "coverage_scope": "selected modules" if plan and plan["mode"] == "affected" else "full profile",
            "source_roots": list(COVERAGE_SOURCE_ROOTS),
            "aggregate_artifacts": [COVERAGE_JSON_NAME, CHANGED_LINES_JSON_NAME],
            "changed_line_visibility": {
                "applicable": changed_report["applicable"],
                "reason": changed_report["reason"],
            },
            "coverage": coverage,
        }
        combined_path = output_dir / COVERAGE_JSON_NAME
        changed_path = output_dir / CHANGED_LINES_JSON_NAME
        # Per-file line and branch detail is large for the full product. Keep
        # the downloaded evidence deterministic and within its published bound
        # without dropping any line or branch detail; only the per-function and
        # per-class regions, which restate those lists, are left out.
        combined_path.write_text(
            json.dumps(combined, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
        )
        changed_path.write_text(
            json.dumps(changed_report, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
        )
        if combined_path.stat().st_size > MAX_COVERAGE_JSON_BYTES:
            raise CoverageError("published combined coverage JSON exceeds its bound")
    except (CoverageError, OSError) as exc:
        print(f"## CI coverage infrastructure failure\n\n- {exc}")
        return 3
    print(
        "\n".join(
            (
                "## Combined CI coverage",
                "",
                f"- Candidate SHA: `{candidate_sha}`",
                f"- Coverage scope: {combined['coverage_scope']}",
                f"- Source roots: {', '.join(f'`{path}`' for path in COVERAGE_SOURCE_ROOTS)}",
                f"- Aggregate artifacts: `{COVERAGE_JSON_NAME}`, `{CHANGED_LINES_JSON_NAME}`",
                f"- Changed-line visibility: {'applicable' if changed_report['applicable'] else 'not applicable'} ({changed_report['reason']})",
            )
        )
    )
    return 0


def load_manifest(root: Path, manifest: Path | None = None) -> dict[str, list[str]]:
    manifest = manifest or root / "tests" / "ci-shards.txt"
    grouped = {name: [] for name in SUITES}
    owners: dict[str, str] = {}
    for number, raw in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2:
            raise ManifestError(f"{manifest}:{number}: expected '<suite> <test-file>'")
        suite, relative = parts
        if suite not in grouped:
            raise ManifestError(f"{manifest}:{number}: unknown suite {suite!r}")
        if not re.fullmatch(r"tests/test_[A-Za-z0-9_]+\.py", relative):
            raise ManifestError(f"{manifest}:{number}: invalid top-level test path {relative!r}")
        if relative in owners:
            raise ManifestError(f"{manifest}:{number}: {relative} already belongs to {owners[relative]!r}")
        owners[relative] = suite
        grouped[suite].append(relative)
        if (root / relative).is_symlink():
            raise ManifestError(f"declared test is a symlink: {relative}")

    discovered = {path.relative_to(root).as_posix() for path in (root / "tests").glob("test_*.py")}
    declared = set(owners)
    missing = sorted(discovered - declared)
    stale = sorted(declared - discovered)
    empty = [name for name, paths in grouped.items() if not paths]
    problems = []
    if missing:
        problems.append(f"unclaimed tests: {', '.join(missing)}")
    if stale:
        problems.append(f"missing declared tests: {', '.join(stale)}")
    if empty:
        problems.append(f"empty suites: {', '.join(empty)}")
    if problems:
        raise ManifestError("; ".join(problems))
    return grouped


def modules(paths: list[str]) -> list[str]:
    return [path.removesuffix(".py").replace("/", ".") for path in paths]


def validate_fast_profile(root: Path) -> None:
    if len(FAST_MODULES) != len(set(FAST_MODULES)):
        raise ManifestError("fast profile contains duplicate modules")
    if not FAST_MODULES:
        raise ManifestError("fast profile is empty")
    for module in FAST_MODULES:
        if not module.startswith("tests.test_"):
            raise ManifestError(f"fast profile has invalid test module {module!r}")
        path = root / (module.replace(".", "/") + ".py")
        if not path.is_file():
            raise ManifestError(f"fast profile names missing test module {module!r}")


def fast_environment(root: Path, fixture_root: Path) -> dict[str, str]:
    """Build the only environment the bounded hermetic child may inherit."""
    home = fixture_root / "home"
    guard = fixture_root / "guard"
    paths = {
        "HOME": home,
        "TA_CODEX_HOME": fixture_root / "codex-home",
        "TA_PIPELINE_STATE_DIR": fixture_root / "pipeline-state",
        "TEMP": fixture_root / "tmp",
        "TMP": fixture_root / "tmp",
        "TMPDIR": fixture_root / "tmp",
        "XDG_CACHE_HOME": fixture_root / "cache",
        "XDG_CONFIG_HOME": fixture_root / "config",
        "XDG_DATA_HOME": fixture_root / "data",
    }
    for path in (*paths.values(), guard):
        path.mkdir(parents=True, exist_ok=True)
    (guard / "sitecustomize.py").write_text(_FAST_GUARD, encoding="utf-8")
    return {
        **{name: str(path) for name, path in paths.items()},
        "PATH": os.defpath,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": os.pathsep.join((str(guard), str(root), str(root / "src"))),
        "UMMANU_REPO": str(root),
    }


def _terminate_fast_child(child: subprocess.Popen[object]) -> None:
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        child.wait(timeout=_FAST_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()


def run_bounded(
    command: list[str], *, root: Path, environment: dict[str, str], timeout_seconds: float
) -> int:
    child = subprocess.Popen(command, cwd=root, env=environment, start_new_session=True)
    try:
        return child.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _terminate_fast_child(child)
        print(
            f"Fast test profile timed out after {timeout_seconds:g} seconds; test child was stopped.",
            file=sys.stderr,
        )
        return 124


def run_fast(root: Path) -> int:
    try:
        validate_fast_profile(root)
    except ManifestError as exc:
        print(f"Fast test profile is invalid: {exc}", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="ummanu-fast-tests.") as temporary:
        environment = fast_environment(root, Path(temporary))
        return run_bounded(
            [sys.executable, "-P", "-m", "unittest", "-v", *FAST_MODULES],
            root=root,
            environment=environment,
            timeout_seconds=FAST_TIMEOUT_SECONDS,
        )


def run_suite_with_evidence(root: Path, suite_name: str, report_dir: Path, candidate_sha: str,
                            plan: dict | None = None) -> int:
    """Make all three required suite artifacts or return an infrastructure result."""
    try:
        report_dir.mkdir(parents=True, exist_ok=True)
        log = BoundedTee(sys.stdout, report_dir / "test-output.log")
    except OSError as exc:
        print(f"CI evidence infrastructure failure: cannot create {report_dir}: {exc}", file=sys.stderr)
        return 3
    started = time.monotonic()
    if not re.fullmatch(r"[0-9a-f]{40,64}", candidate_sha):
        print("CI evidence infrastructure failure: candidate SHA is not exact", file=log)
        evidence = SuiteEvidence(
            suite_name,
            candidate_sha,
            "infrastructure_failure",
            _empty_counts(),
            time.monotonic() - started,
            [],
            ["candidate SHA is not exact"],
        )
    else:
        try:
            grouped = load_manifest(root)
            if plan:
                if candidate_sha != plan["candidate_sha"] or suite_name not in plan["selected"]:
                    raise ManifestError("suite is not required by selection")
                grouped = plan["selected"]
        except (OSError, ManifestError) as exc:
            print(f"CI test manifest is invalid: {exc}", file=log)
            evidence = SuiteEvidence(
                suite_name,
                candidate_sha,
                "infrastructure_failure",
                _empty_counts(),
                time.monotonic() - started,
                [],
                ["CI test manifest is invalid"],
                detail=str(exc),
            )
        else:
            sys.path.insert(0, str(root))
            try:
                with contextlib.chdir(root):
                    try:
                        before = _checkout_snapshot(root)
                        if plan and before:
                            raise CheckoutStatusError("planned suite checkout is not clean")
                    except CheckoutStatusError:
                        print("CI checkout status is unavailable before suite execution", file=log)
                        evidence = SuiteEvidence(
                            suite_name,
                            candidate_sha,
                            "infrastructure_failure",
                            _empty_counts(),
                            time.monotonic() - started,
                            [],
                            ["checkout status unavailable before suite execution"],
                            detail="git status command failed",
                        )
                    else:
                        evidence = run_reported_suite(suite_name, grouped[suite_name], candidate_sha, log)
                        try:
                            after = _checkout_snapshot(root)
                        except CheckoutStatusError:
                            print("CI checkout status is unavailable after suite execution", file=log)
                            original_outcome = evidence.outcome
                            evidence.outcome = "infrastructure_failure"
                            evidence.detail = _checkout_failure_detail(
                                original_outcome, "git status command failed after suite execution"
                            )
                            evidence.failure_locations.append(
                                "checkout status unavailable after suite execution"
                            )
                        else:
                            evidence.checkout_status = _checkout_status(before, after)
                            if evidence.checkout_status.changed:
                                original_outcome = evidence.outcome
                                evidence.outcome = "infrastructure_failure"
                                evidence.detail = _checkout_failure_detail(
                                    original_outcome, "checkout status changed during suite execution"
                                )
                                evidence.failure_locations.append(
                                    "checkout status changed during suite execution"
                                )
            finally:
                sys.path.pop(0)
    try:
        if plan:
            evidence.selection_digest = ci_selection.plan_digest(plan)
            evidence.selected_modules = modules(plan["selected"].get(suite_name, []))
        _write_evidence(report_dir, evidence, log)
    except OSError as exc:
        print(f"CI evidence infrastructure failure: cannot write required report: {exc}", file=sys.stderr)
        return 3
    return _outcome_code(evidence.outcome)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true", help="validate and print the manifest")
    action.add_argument("--select", action="store_true", help="write exact-event CI selection plan")
    action.add_argument("--validation-aggregate", action="store_true", help="require applicable jobs")
    action.add_argument("--suite", choices=SUITES, help="validate, then run one CI suite")
    action.add_argument("--fast", action="store_true", help="run the bounded hermetic control-host profile")
    action.add_argument("--summary", action="store_true", help="validate evidence and print a step summary")
    action.add_argument("--aggregate", action="store_true", help="summarize all downloaded suite evidence")
    action.add_argument(
        "--coverage-aggregate", action="store_true", help="combine downloaded raw coverage evidence"
    )
    parser.add_argument("--report-dir", type=Path, help="directory containing one suite's CI evidence")
    parser.add_argument("--candidate-sha", help="exact candidate SHA recorded in suite evidence")
    parser.add_argument("--evidence-dir", type=Path, help="download directory containing suite artifacts")
    parser.add_argument("--coverage-dir", type=Path, help="download directory containing raw suite coverage")
    parser.add_argument("--coverage-output-dir", type=Path, help="directory for combined coverage artifacts")
    parser.add_argument("--base-sha", default="", help="pull-request base SHA for changed-line coverage")
    parser.add_argument("--selection-plan", type=Path)
    parser.add_argument("--event", choices=("pull_request", "push", "workflow_dispatch"))
    parser.add_argument("--event-ref", default="")
    parser.add_argument("--job-results", help="JSON object of Actions needs results")
    parser.add_argument(
        "--needs-result",
        choices=("success", "failure", "cancelled", "skipped"),
        help="GitHub matrix result used to classify unavailable suite evidence",
    )
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[1]
    plan = None
    if args.select or args.selection_plan is not None:
        if args.selection_plan is None or args.candidate_sha is None or args.event is None:
            parser.error("selection requires --selection-plan, --candidate-sha and --event")
        try:
            grouped = load_manifest(root)
            if args.select:
                plan = ci_selection.build_plan(root, grouped, args.candidate_sha, args.base_sha,
                                               args.event, args.event_ref)
                encoded = json.dumps(plan, sort_keys=True, indent=2) + "\n"
                if len(encoded.encode()) > ci_selection.MAX_PLAN_BYTES:
                    raise ci_selection.SelectionError("selection plan exceeds bound")
                args.selection_plan.parent.mkdir(parents=True, exist_ok=True)
                args.selection_plan.write_text(encoded, encoding="utf-8")
                print(ci_selection.summary(plan))
                if os.environ.get("GITHUB_OUTPUT"):
                    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
                        output.write(f"suites={json.dumps(list(plan['selected']))}\nmode={plan['mode']}\n")
                return 0
            plan = ci_selection.read_plan(root, args.selection_plan, grouped,
                                          candidate_sha=args.candidate_sha, base_sha=args.base_sha,
                                          event=args.event, ref=args.event_ref)
        except (OSError, ValueError) as exc:
            print(f"CI selection infrastructure failure: {exc}", file=sys.stderr)
            return 3
    if args.validation_aggregate:
        if plan is None or args.job_results is None:
            parser.error("validation aggregate requires selection and --job-results")
        try:
            results = json.loads(args.job_results)
            print(f"Applicable validation results: {results}")
            return 0 if ci_selection.validation_result(plan, results) else 3
        except (ValueError, TypeError):
            return 3
    if args.summary:
        if args.report_dir is None:
            parser.error("--summary requires --report-dir")
        return report_summary(args.report_dir)
    if args.aggregate:
        if args.evidence_dir is None or args.needs_result is None:
            parser.error("--aggregate requires --evidence-dir and --needs-result")
        return aggregate_evidence(args.evidence_dir, args.needs_result, plan)
    if args.coverage_aggregate:
        if args.coverage_dir is None or args.coverage_output_dir is None or args.candidate_sha is None:
            parser.error(
                "--coverage-aggregate requires --coverage-dir, --coverage-output-dir and --candidate-sha"
            )
        return aggregate_coverage(
            root, args.coverage_dir, args.coverage_output_dir, args.candidate_sha, args.base_sha or None, plan
        )
    if args.fast:
        return run_fast(root)

    if args.suite is not None and (args.report_dir is not None or args.candidate_sha is not None):
        if args.report_dir is None or args.candidate_sha is None:
            parser.error("reported suite execution requires --report-dir and --candidate-sha")
        return run_suite_with_evidence(root, args.suite, args.report_dir, args.candidate_sha, plan)

    try:
        grouped = load_manifest(root)
    except (OSError, ManifestError) as exc:
        print(f"CI test manifest is invalid: {exc}", file=sys.stderr)
        return 2

    if args.check:
        print(json.dumps({name: len(paths) for name, paths in grouped.items()}, sort_keys=True))
        return 0
    assert args.suite is not None
    if plan is not None:
        parser.error("selective execution requires --report-dir and --candidate-sha")
    return subprocess.call([sys.executable, "-m", "unittest", "-v", *modules(grouped[args.suite])], cwd=root)


if __name__ == "__main__":
    raise SystemExit(main())
