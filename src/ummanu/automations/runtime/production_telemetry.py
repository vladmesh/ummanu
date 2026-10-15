"""Read the production dispatcher's durable tick telemetry.

The dispatcher that actually moves cards on this host is `ummanu dispatcher production-tick`.
It records how each terminal tick ended under `tick_telemetry` in its own state file — see
`ummanu.dispatch.production.record_tick_telemetry` for the writer and the field meanings.

Both readers of that record live outside the dispatcher's process: `runtime/health.py` and the
steward's `signals.py`. They share this module so there is one place that knows where the file is
and what a missing or unusable one means. The dispatcher's resource-probe cache is resolved here
too (`resource_health_path`).

Read through the file, never by importing the dispatcher: an agent's unit runs in its own
worktree with its own environment, and the state file is the durable boundary between the two.

Path resolution follows the dispatcher's own, so the reader lands on the file the writer writes on
any installation: `--data-dir`/`UMMANU_DATA_DIR` first, then `data_dir` out of the instance the
dispatcher unit is started with. A reader's own unit gets that instance from `UMMANU_INSTANCE`,
falling back to the directory of its rendered `UMMANU_RUNTIME_ENV_FILE`. `UMMANU_PRODUCTION_STATE`
overrides the whole file.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from ummanu.config import (
    ConfigError,
    DataDirError,
    instance_data_dir as _configured_data_dir,
    load_config,
    validate,
)
from ummanu.head_health import resource_health_path as _resource_health_path
from ummanu.runtime.paths import default_instance_path, env_value, instance_file as _config_file

DEFAULT_INSTANCE = default_instance_path()


def _instance_from_runtime_env() -> Path | None:
    """The instance dir implied by the role unit's own env-file path, or None if it has none.

    Every packaged agent unit is rendered with `UMMANU_RUNTIME_ENV_FILE=<instance>/runtime.env`, so on an
    installation whose instance is not the default one, a role that was never handed
    UMMANU_INSTANCE still resolves the instance the dispatcher runs against.
    """
    configured = env_value("UMMANU_RUNTIME_ENV_FILE", nonempty=True)
    return Path(configured).expanduser().parent if configured else None


def instance_file() -> Path:
    configured = os.environ.get("UMMANU_INSTANCE")
    path = Path(configured).expanduser() if configured else (_instance_from_runtime_env() or DEFAULT_INSTANCE)
    return _config_file(path)


def instance_host_configuration() -> tuple[dict | None, str | None]:
    """The validated host section of this process's instance, or a safe diagnostic.

    The only permitted ``ummanu.automations`` edge into the rest of Ummanu is this runtime boundary.  The
    dispatcher-state reader and health command both need the same installation identity, so keep
    configuration parsing here rather than letting a role reach into the importing checkout.
    """
    path = instance_file()
    try:
        instance = load_config(path)
    except ConfigError as exc:
        return None, f"cannot read {path}: {exc}"
    errors = validate(instance, "instance", path.name)
    if errors:
        return None, f"invalid {path}: {'; '.join(str(error) for error in errors)}"
    if not isinstance(instance, dict):  # The schema currently guarantees this; do not fail open if it changes.
        return None, f"invalid {path}: instance root is not an object"
    host = instance.get("host", {})
    if not isinstance(host, dict):
        return None, f"invalid {path}: host is not an object"
    return host, None


def instance_data_dir() -> Path | None:
    """The installation's `data_dir` as the dispatcher resolves it, or None if it cannot be read.

    None is not an error: the caller falls back to the home default and reports the path it looked
    at. An instance that cannot be parsed must not take down the health command.
    """
    try:
        return _configured_data_dir(instance_file())
    except DataDirError:
        return None


def data_dir() -> Path:
    configured = os.environ.get("UMMANU_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    return instance_data_dir() or Path.home() / "ummanu-data"


def state_path() -> Path:
    """Recomputed per call, not frozen at import, so a test (or a unit with its own env) sees the
    override it just set — an import-time constant would freeze the wrong data plane for any
    process that resolves its environment after import."""
    configured = env_value("UMMANU_PRODUCTION_STATE", nonempty=True)
    return Path(configured) if configured else data_dir() / "dispatcher" / "production-state.json"


def resource_health_path() -> Path:
    """The resource-probe cache the production dispatcher keeps, resolved like `state_path()`.

    Writer: `ummanu.head_health.HeadHealth`, next to the dispatcher's own state under
    `<data_dir>/dispatcher/`. The steward reads it to see the verdict the dispatcher actually acted
    on instead of running its own paid probes. `UMMANU_PRODUCTION_RESOURCE_HEALTH` overrides the file.
    """
    configured = env_value("UMMANU_PRODUCTION_RESOURCE_HEALTH", nonempty=True)
    return Path(configured) if configured else _resource_health_path(data_dir())


def read_resource_health() -> dict[str, dict] | None:
    """{resource: {status, last_transition}} from the dispatcher's cache, or None when it cannot be read.

    None is "nothing was measured", distinct from an empty cache: the caller keeps its previous
    baseline rather than resetting it, so a transient unreadable file cannot erase a flip.
    `last_transition` is the entry's own (`head_health._cache_entry`), {} when it has none yet.
    """
    try:
        cache = json.loads(resource_health_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    if not isinstance(cache, dict):
        return None
    try:
        return {
            str(rid): {
                "status": str(entry["status"]),
                "last_transition": entry.get("last_transition") or {},
            }
            for rid, entry in cache.items()
        }
    except (KeyError, TypeError, AttributeError):
        return None


@dataclass(frozen=True)
class TickTelemetry:
    """What the dispatcher durably recorded about its ticks, or why nothing could be read.

    `unavailable` carries the reason as a short slug and is never silently the same as "the
    dispatcher is fine and quiet".
    """

    path: Path
    unavailable: str = ""
    last: dict = field(default_factory=dict)
    last_healthy_at: str = ""
    unhealthy: tuple[dict, ...] = ()
    unhealthy_total: int = 0
    tick_seq: int = 0
    # The open incident (one continuous run of unhealthy ticks) or {} when the pipeline is not in
    # one, and the last closed one with the healthy tick that ended it. The counters next to them
    # are monotonic within a generation, so a reader can dedupe on them the way it dedupes on
    # `unhealthy_total` — see `ummanu.dispatch.production._record_incident`.
    incident: dict = field(default_factory=dict)
    incident_total: int = 0
    recovery: dict = field(default_factory=dict)
    recovery_total: int = 0
    # Which telemetry history the counters above belong to. Empty means the writer never stamped
    # one (a host still on an older product); a reader that dedupes on the counters must treat
    # that as "cannot tell" rather than as a change.
    generation: str = ""

    @property
    def available(self) -> bool:
        return not self.unavailable


def read() -> TickTelemetry:
    path = state_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return TickTelemetry(path, unavailable="production-state-missing")
    except (OSError, ValueError, UnicodeError):
        return TickTelemetry(path, unavailable="production-state-unreadable")
    telemetry = payload.get("tick_telemetry") if isinstance(payload, dict) else None
    if not isinstance(telemetry, dict):
        # The state file exists but carries no tick telemetry: a dispatcher that has not ticked
        # since this record was introduced, or a host still running an older product.
        return TickTelemetry(path, unavailable="tick-telemetry-missing")
    last = telemetry.get("last")
    unhealthy = telemetry.get("unhealthy")
    incident = telemetry.get("incident")
    recovery = telemetry.get("recovery")
    return TickTelemetry(
        path=path,
        last=last if isinstance(last, dict) else {},
        last_healthy_at=str(telemetry.get("last_healthy_at") or ""),
        unhealthy=tuple(item for item in (unhealthy or []) if isinstance(item, dict))
        if isinstance(unhealthy, list)
        else (),
        unhealthy_total=_counter(telemetry.get("unhealthy_total")),
        tick_seq=_counter(telemetry.get("tick_seq")),
        generation=str(telemetry.get("generation") or ""),
        incident=incident if isinstance(incident, dict) else {},
        incident_total=_counter(telemetry.get("incident_total")),
        recovery=recovery if isinstance(recovery, dict) else {},
        recovery_total=_counter(telemetry.get("recovery_total")),
    )


def _counter(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def describe(entry: dict) -> str:
    """One-line diagnostic for a recorded tick: its status, and what actually went wrong.

    Both kinds of failure are named: the errors the tick caught, and the action outcomes that
    reported a failed operation without raising. A tick red only because of the latter would
    otherwise print its status and nothing an operator can act on.
    """
    parts = [str(entry.get("status") or "unknown")]
    reason = str(entry.get("reason") or "")
    if reason:
        parts.append(reason)
    errors = [item for item in (entry.get("errors") or []) if isinstance(item, dict)]
    for error in errors:
        ref = str(error.get("ref") or "")
        code = str(error.get("code") or "error")
        parts.append(f"{ref} {code}".strip())
    hidden = _counter(entry.get("error_count")) - len(errors)
    if hidden > 0:
        parts.append(f"+{hidden} more error(s)")
    degradations = [item for item in (entry.get("degradations") or []) if isinstance(item, dict)]
    for degradation in degradations:
        ref = str(degradation.get("ref") or "")
        what = str(degradation.get("action") or degradation.get("step") or "degraded action")
        detail = str(degradation.get("reason") or "")
        parts.append(" ".join(part for part in (ref, what, detail) if part))
    hidden = _counter(entry.get("degraded_count")) - len(degradations)
    if hidden > 0:
        parts.append(f"+{hidden} more degraded action(s)")
    return "; ".join(parts)


def describe_incident(incident: dict) -> str:
    """One-line diagnostic for an incident: when it started, how long it ran, what caused it.

    The cause is the tick that opened it, not the newest failing one: every tick of a board outage
    says the same thing, and what an operator needs is where it began.
    """
    if not incident:
        return ""
    opened = incident.get("opened")
    parts = [
        f"since {incident.get('opened_at') or '?'}",
        f"{_counter(incident.get('unhealthy_ticks'))} unhealthy tick(s)",
    ]
    cause = describe(opened) if isinstance(opened, dict) else ""
    if cause:
        parts.append(cause)
    return "; ".join(parts)
