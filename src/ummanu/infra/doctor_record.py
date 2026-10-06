"""Latest real doctor result. The native timer writes; web reads never evaluate doctor.

One document retains the last completed attempt beside current collection metadata.
Replacement in the same directory is atomic, so interruption never redates the result.
No history, raw process output, environment or credentials are published.
"""

from __future__ import annotations

import fcntl
import json
import os
import resource
import signal
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ummanu.config import validate_instance
from ummanu.infra.doctor_findings import accepted, active_findings
from ummanu.runtime.paths import add_instance_argument

RESULT_PATH = Path("doctor/latest.json")
FRESH_SECONDS = 180
TIMEOUT_SECONDS = 40
# Allow the child deadline and publication some margin before declaring a collector stuck.
STUCK_SECONDS = 60
MAX_BYTES = 2 * 1024 * 1024


def utc(now: float) -> str:
    # Preserve fractional ordering between a completion and a same-second restart.
    return datetime.fromtimestamp(now, UTC).isoformat(timespec="auto").replace("+00:00", "Z")


def identity(instance: Path, data_dir: Path) -> dict[str, str]:
    config = instance / "instance.yaml" if instance.is_dir() else instance
    return {"instance": str(config.resolve()), "data_dir": str(data_dir.resolve())}


def publish(path: Path, document: dict[str, Any]) -> None:
    """Flush a private sibling, then replace the sole latest document, then flush its directory."""
    payload = (json.dumps(document, sort_keys=True, allow_nan=False) + "\n").encode()
    if len(payload) > MAX_BYTES:
        raise ValueError("doctor document exceeds its size bound")
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".latest-", delete=False) as stream:
            temporary = stream.name
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def _output_limit() -> None:
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_BYTES, MAX_BYTES))


def collect(command: list[str], *, timeout: float = TIMEOUT_SECONDS) -> tuple[int, dict[str, Any]]:
    """A bounded child of this installed product; kill its entire process group on timeout."""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    with tempfile.TemporaryFile() as output:
        with subprocess.Popen(
            command, stdout=output, stderr=subprocess.DEVNULL, env=environment,
            start_new_session=True, preexec_fn=_output_limit,
        ) as process:
            try:
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise TimeoutError("doctor exceeded its collection deadline") from None
        output.seek(0)
        raw = output.read(MAX_BYTES + 1)
    if code not in (0, 1, 2):
        raise ValueError(f"doctor process exited with {code}")
    if len(raw) >= MAX_BYTES:
        raise ValueError("doctor output exceeds its size bound")
    payload = json.loads(raw)
    validate_result(payload, code)
    # The findings are the CLI evaluator's unchanged findings. Status has its own web source.
    return code, {key: payload[key] for key in ("schema_version", "ok", "findings")}


def validate_result(payload: Any, code: Any) -> None:
    if (
        not isinstance(payload, dict) or payload.get("schema_version") != 1
        or type(code) is not int or code not in (0, 1, 2)
        or type(payload.get("ok")) is not bool or not isinstance(payload.get("findings"), list)
    ):
        raise ValueError("invalid doctor result envelope")
    findings = payload["findings"]
    if any(not isinstance(item, dict) or not isinstance(item.get("code"), str) or not item["code"] for item in findings):
        raise ValueError("invalid doctor finding")
    for finding in findings:
        if ("accepted" in finding or "acceptance_reason" in finding) and not accepted(finding):
            raise ValueError("invalid doctor acceptance annotation")
    active = active_findings(findings)
    if payload["ok"] != (not active) or (code == 0 and active) or (code == 1 and not active):
        raise ValueError("doctor exit and findings disagree")


def record(instance: Path, *, data_dir: Path | None = None, offline: bool = False,
           host_fixture: str | None = None, timeout: float = TIMEOUT_SECONDS) -> int:
    report = validate_instance(instance)
    root = data_dir or report.data_dir
    if root is None:
        print("doctor record: configured data root is unavailable", file=sys.stderr)
        return 2
    path = root / RESULT_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with (path.parent / "record.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("doctor record: collection already in progress", file=sys.stderr)
                return 2
            installation = identity(instance, root)
            now = time.time()
            previous = None
            try:
                prior = _load(path)
                if prior.get("installation") == installation:
                    previous, _ = _parts(prior, now=now)
            except (OSError, ValueError, KeyError, TypeError, OverflowError):
                pass  # No valid completed result to retain; the initial state is explicit.
            attempt = {
                "run_at": utc(now), "completed_at": None, "exit_code": None,
                "mode": "offline" if offline else "host_fixture" if host_fixture else "live",
                "outcome": "collecting", "reason": "doctor attempt has not completed", "result": None,
            }
            document = {
                "schema_version": 2, "installation": installation, "completed": previous,
                "collecting": {key: attempt[key] for key in ("run_at", "mode")},
            }
            publish(path, document)
            command = [sys.executable, "-P", "-m", "ummanu", "doctor", "--instance", str(instance), "--json"]
            if offline:
                command.append("--offline")
            if host_fixture:
                command.extend(("--host-fixture", host_fixture))
            try:
                code, result = collect(command, timeout=timeout)
                attempt.update(exit_code=code, result=result, outcome="unavailable" if code == 2 else "result",
                                reason="doctor reported diagnostic unavailability" if code == 2 else None)
            except (OSError, ValueError, TimeoutError) as exc:
                # Never publish raw output/exception strings, which could contain secrets.
                attempt.update(outcome="failed", reason=f"doctor collection failed ({type(exc).__name__})")
            attempt["completed_at"] = utc(time.time())
            document.update(completed=attempt, collecting=None)
            publish(path, document)
            return 0 if attempt["outcome"] == "result" else 2
    except (OSError, ValueError):
        print("doctor record: result publication failed; previous timestamp is not refreshed", file=sys.stderr)
        return 2


def _load(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("record exceeds size bound")
    document = json.loads(raw)
    if not isinstance(document, dict) or type(document.get("schema_version")) is not int or document["schema_version"] not in (1, 2):
        raise ValueError("unsupported recorded doctor schema")
    return document


def _start(attempt: Any, *, now: float) -> float:
    if not isinstance(attempt, dict):
        raise ValueError("invalid doctor attempt")
    started = _timestamp(attempt["run_at"])
    if started > now + 5:
        raise ValueError("doctor time is in the future")
    if attempt.get("mode") not in ("live", "offline", "host_fixture"):
        raise ValueError("invalid doctor mode")
    return started


def _parts(document: dict[str, Any], *, now: float) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Validate both parts; only progress begun after the completed attempt can be current.

    Version 1 is the released latest-attempt format. An erased predecessor is unrecoverable.
    """
    if document["schema_version"] == 1:
        completed = None if document.get("outcome") == "collecting" else document
        collecting = document if completed is None else None
        if collecting is not None and any(document.get(key) is not None for key in ("completed_at", "result", "exit_code")):
            raise ValueError("collecting attempt contains a completed result")
    else:
        completed, collecting = document["completed"], document["collecting"]
    if completed is not None:
        started = _start(completed, now=now)
        finished = _timestamp(completed["completed_at"])
        if finished < started or finished > now + 5:
            raise ValueError("invalid doctor completion time")
        outcome = completed["outcome"]
        result = completed.get("result")
        if outcome in ("result", "unavailable"):
            validate_result(result, completed.get("exit_code"))
            if (outcome == "unavailable") != (completed["exit_code"] == 2):
                raise ValueError("outcome and exit disagree")
        elif outcome != "failed" or result is not None or completed.get("exit_code") is not None:
            raise ValueError("invalid failed doctor attempt")
        if completed.get("reason") is not None and not isinstance(completed["reason"], str):
            raise ValueError("invalid doctor reason")
        completed = {key: completed.get(key) for key in
                     ("run_at", "completed_at", "mode", "outcome", "exit_code", "reason", "result")}
    if collecting is not None:
        started = _start(collecting, now=now)
        collecting = {key: collecting[key] for key in ("run_at", "mode")}
        if completed is not None and started <= _timestamp(completed["completed_at"]):
            collecting = None
    return completed, collecting


def read_latest(instance: Path, data_dir: Path, *, now: float, offline: bool = False) -> dict[str, Any]:
    """Read and validate only local bytes. A failure keeps its identity and never means no findings."""
    path = data_dir / RESULT_PATH
    reading: dict[str, Any] = {"state": "unknown", "reason": "not yet collected", "run_at": None,
                               "completed_at": None, "exit_code": None, "findings": [], "path": str(path),
                               "collecting": None}
    try:
        document = _load(path)
        if document.get("installation") != identity(instance, data_dir):
            reading.update(state="wrong_installation", reason="doctor result belongs to another installation or data root")
            return reading
        completed, collecting = _parts(document, now=now)
        if collecting is not None:
            elapsed = max(0, now - _timestamp(collecting["run_at"]))
            reading["collecting"] = {**collecting, "elapsed_seconds": elapsed,
                                     "threshold_seconds": STUCK_SECONDS, "stuck": elapsed > STUCK_SECONDS}
        if completed is not None:
            result = completed["result"]
            age = max(0, now - _timestamp(completed["run_at"]))
            reading.update(state="available" if completed["outcome"] == "result" else completed["outcome"],
                           reason=completed["reason"], run_at=completed["run_at"],
                           completed_at=completed["completed_at"], exit_code=completed["exit_code"],
                           findings=result["findings"] if result is not None else [],
                           mode=completed["mode"], age_seconds=age)
            if age > FRESH_SECONDS:
                reading.update(state="stale", reason=f"latest completed doctor attempt is older than {FRESH_SECONDS} seconds ({completed['outcome']})")
        if not offline and any(part["mode"] != "live" for part in (completed, collecting) if part is not None):
            reading.update(state="wrong_mode", reason="recorded doctor did not inspect the live installation")
        return reading
    except FileNotFoundError:
        return reading
    except OSError:
        reading.update(state="unavailable", reason="recorded doctor document cannot be read")
    except (ValueError, KeyError, TypeError, OverflowError):
        reading.update(state="malformed", reason="recorded doctor document is malformed or unsupported")
    return reading


def _timestamp(value: Any) -> float:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("doctor time must be UTC")
    return datetime.fromisoformat(value).timestamp()


def add_subcommand(subparsers: Any) -> None:
    command = subparsers.add_parser("doctor-record", help="atomically record one bounded real doctor attempt")
    add_instance_argument(command, type=Path)
    command.add_argument("--data-dir", type=Path, help="override the result data root")
    command.add_argument("--offline", action="store_true")
    command.add_argument("--host-fixture")
    command.set_defaults(handler=lambda args: record(args.instance, data_dir=args.data_dir,
                                                   offline=args.offline, host_fixture=args.host_fixture))
