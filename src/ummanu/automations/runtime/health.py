"""Health check for every registered triggered-agent.

One line per agent: is the systemd timer active, and how fresh is the last *healthy* tick in
runs.jsonl (a tick that answered counts — precheck-nothing-to-do still proves the timer fires and
the runtime runs). A result of "error" or "board-unreachable" does NOT count on its own: precheck
logs one of those every tick a broken board/env keeps failing, so if freshness went by the raw
last event a permanently down board would look perpetually alive (fresh error every 3 minutes)
instead of red. "board-unreachable" is the deferred-run record (secretary-964): it is not the
agent's own failure, but it is not an answered tick either, and a board that never comes back must
still go red here. Last `advance` is informational: it can legitimately be days old when nothing changed
upstream. Exit non-zero if any agent is red.

Both sources here are the live data plane, not a checkout (secretary-833):

  * curator/steward/retro write runs.jsonl through `AgentState`, whose root is UMMANU_STATE or
    `~/ummanu-data/automation-state` — the packaged units set neither UMMANU_STATE nor a different
    HOME, so that default is where their real records land. Reading a `state/` dir inside the
    agent's worktree instead reported `no runs.jsonl yet` for agents that were ticking fine.
  * the pipeline line is the production dispatcher's own tick telemetry
    (runtime/production_telemetry.py), the record written by the timer that actually moves cards.
    A recorded tick that ended unhealthy is red on its own: it must never be answered with the
    last healthy one that came before it.
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

from ummanu.infra.env import env_value
from ummanu.runtime.paths import component_enabled, configured_product_root
from ummanu.runtime.state import AgentState

from . import production_telemetry

_ENV_MAX_AGE_NAME = "UMMANU_HEALTH_MAX_AGE_S" if os.environ.get("UMMANU_HEALTH_MAX_AGE_S") else "TA_HEALTH_MAX_AGE_S"
_ENV_MAX_AGE = env_value("UMMANU_HEALTH_MAX_AGE_S", nonempty=True)  # global override, wins for every agent
# Units are the packaged ones under host.unit_prefix, not the decommissioned
# ta-* names. Most agents map to <prefix><agent>.timer; the pipeline's clock is
# the production dispatcher's timer, which is named after the component rather
# than the agent.
_UNIT_PREFIX = os.environ.get("UMMANU_UNIT_PREFIX", "ummanu-")
_UNIT_COMPONENT = {"pipeline": "dispatcher-production"}
_ROLE_COMPONENT = {"pipeline": "dispatcher-production"}
# Freshness budget per systemd cadence: timer period + slack. Read from the agent's spec so a
# daily agent (retro) isn't flagged red just for ticking less often than an hourly one.
_CADENCE_MAX_AGE_S = {"hourly": 3 * 3600, "daily": 26 * 3600}


@dataclass(frozen=True)
class RoleExpectation:
    """The scheduled role this installation declares, before looking at systemd or telemetry."""

    component: str
    enabled: bool
    unit_prefix: str
    max_age_s: int


def _max_age_s(agent: str, product_root: Path | None = None) -> int:
    """Freshness budget from the configured product's automation spec.

    The product root is supplied by the installed role environment, never derived from the checkout
    that imported this module.  A caller which needs a fail-closed answer uses
    ``_role_expectations``; this compatibility helper retains the historical fallback for direct
    status readers.
    """
    if _ENV_MAX_AGE:
        return int(_ENV_MAX_AGE)
    try:
        spec = tomllib.loads(
            (
                (product_root or configured_product_root())
                / "src"
                / "ummanu"
                / "automations"
                / "agents"
                / agent
                / "automation.toml"
            ).read_text(encoding="utf-8")
        )
    except (OSError, tomllib.TOMLDecodeError):
        return 3 * 3600
    return _max_age_from_spec(spec)


def _max_age_from_spec(spec: dict) -> int:
    # An explicit [health] max_age_s wins — needed for a calendar like pipeline's raw 3-min
    # OnCalendar expression, which isn't one of the named cadences below.
    health = spec.get("health", {})
    systemd = spec.get("systemd", {})
    if not isinstance(health, dict) or not isinstance(systemd, dict):
        raise ValueError("health or systemd table is not an object")  # noqa: TRY004 - public validation contract
    explicit = health.get("max_age_s")
    if explicit is not None:
        if isinstance(explicit, bool):
            raise ValueError("health.max_age_s is not an integer")
        try:
            max_age_s = int(explicit)
        except (TypeError, ValueError):
            raise ValueError("health.max_age_s is not an integer") from None
        if max_age_s <= 0:
            raise ValueError("health.max_age_s is not positive")
        return max_age_s
    cadence = systemd.get("calendar", "hourly")
    if not isinstance(cadence, str):
        raise ValueError("systemd.calendar is not a string")  # noqa: TRY004 - public validation contract
    return _CADENCE_MAX_AGE_S.get(cadence, 3 * 3600)


def _role_expectations(agents: tuple[str, ...]) -> tuple[dict[str, RoleExpectation] | None, str | None]:
    """Read scheduled-role intent from the instance this process is bound to.

    ``host.components`` is the source of truth for whether a packaged component is wanted: an
    omitted component is enabled, and an explicit ``enabled: false`` means its timer and telemetry
    are intentionally irrelevant.  We validate the same instance shape host reconciliation uses,
    so an unreadable or ambiguous configuration cannot be mistaken for the importing checkout's
    defaults.
    """
    host, error = production_telemetry.instance_host_configuration()
    if error:
        return None, error
    assert host is not None
    configured_prefix = host.get("unit_prefix")
    unit_prefix = configured_prefix if isinstance(configured_prefix, str) else _UNIT_PREFIX
    product_root = configured_product_root()
    expectations: dict[str, RoleExpectation] = {}
    for agent in agents:
        component = _ROLE_COMPONENT.get(agent, agent)
        enabled = component_enabled(host, component)
        if not enabled:
            expectations[agent] = RoleExpectation(component, False, unit_prefix, 0)
            continue
        spec_path = product_root / "src" / "ummanu" / "automations" / "agents" / agent / "automation.toml"
        try:
            spec = tomllib.loads(spec_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            return None, f"cannot read scheduled-role spec {spec_path}: {exc}"
        if not isinstance(spec, dict):
            return None, f"invalid scheduled-role spec {spec_path}"
        if _ENV_MAX_AGE:
            try:
                max_age_s = int(_ENV_MAX_AGE)
            except ValueError:
                return None, f"{_ENV_MAX_AGE_NAME} is not an integer"
            if max_age_s <= 0:
                return None, f"{_ENV_MAX_AGE_NAME} is not positive"
        else:
            try:
                max_age_s = _max_age_from_spec(spec)
            except ValueError as exc:
                return None, f"invalid scheduled-role spec {spec_path}: {exc}"
        expectations[agent] = RoleExpectation(component, True, unit_prefix, max_age_s)
    return expectations, None


def timer_unit(agent: str, unit_prefix: str | None = None) -> str:
    return f"{unit_prefix if unit_prefix is not None else _UNIT_PREFIX}{_UNIT_COMPONENT.get(agent, agent)}.timer"


def _timer_active(agent: str, unit_prefix: str | None = None) -> bool:
    p = subprocess.run(
        ["systemctl", "is-active", timer_unit(agent, unit_prefix)], capture_output=True, text=True, check=False
    )
    return p.stdout.strip() == "active"


def _runs(agent: str) -> list[dict]:
    path = AgentState(agent).dir / "runs.jsonl"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _age_s(ts: str) -> float:
    then = datetime.datetime.fromisoformat(ts)
    if then.tzinfo is None:
        then = then.replace(tzinfo=datetime.UTC)
    return (datetime.datetime.now(datetime.UTC) - then).total_seconds()


def _stale(ts: str, agent: str, max_age_s: int | None = None) -> str | None:
    """Problem text when `ts` is older than the agent's freshness budget, else None. An
    unparseable timestamp is itself a problem: it is a record nobody can date, not a fresh one."""
    try:
        age = _age_s(ts)
    except ValueError:
        return f"last healthy tick has an unreadable timestamp ({ts!r})"
    max_age = max_age_s if max_age_s is not None else _max_age_s(agent)
    if age > max_age:
        return f"last healthy tick {int(age / 60)}min ago (> {max_age // 60}min)"
    return None


#: Results that record a tick which never got an answer: the agent broke ("error"), or the board
#: refused the connection for every attempt the gate made ("board-unreachable"). Neither proves the
#: agent is alive, so neither may set the freshness clock.
_UNANSWERED_RESULTS = {"error", "board-unreachable"}


def _runs_status(agent: str, max_age_s: int | None = None) -> tuple[list[str], str]:
    """(problems, informational detail) from the agent's own runs.jsonl."""
    runs = _runs(agent)
    if not runs:
        return ["no runs.jsonl yet"], ""
    problems = []
    healthy = [r for r in runs if r.get("result") not in _UNANSWERED_RESULTS]
    if not healthy:
        problems.append("no answered tick yet — board/env never came up")
        last_tick = runs[-1]
    else:
        last_tick = healthy[-1]
        stale = _stale(str(last_tick.get("ts") or ""), agent, max_age_s)
        if stale:
            problems.append(stale)
    last_advance = next((r for r in reversed(runs) if r.get("event") == "advance"), None)
    tick = last_tick.get("ts", "-")
    adv = last_advance["ts"] if last_advance else "-"
    return problems, f"last tick {tick}, last advance {adv}"


def _pipeline_status(max_age_s: int | None = None) -> tuple[list[str], str]:
    """(problems, informational detail) from the production dispatcher's durable tick telemetry.

    The unhealthy branch is the point of this line: the dispatcher keeps ticking through a broken
    board or a failing host, so the last tick's own outcome decides the status and a fresher
    healthy predecessor never speaks for it. Freshness is checked on top of that, for the case
    where the timer or the whole tick stopped producing records at all.
    """
    telemetry = production_telemetry.read()
    if not telemetry.available:
        return [f"{telemetry.unavailable} ({telemetry.path})"], ""
    problems = []
    last = telemetry.last
    if not last:
        problems.append("no production tick recorded yet")
    elif not last.get("healthy"):
        problems.append(f"last tick unhealthy: {production_telemetry.describe(last)}")
    if not telemetry.last_healthy_at:
        problems.append("no healthy production tick recorded yet")
    else:
        stale = _stale(telemetry.last_healthy_at, "pipeline", max_age_s)
        if stale:
            problems.append(stale)
    detail = f"last tick {last.get('at', '-')}, last healthy {telemetry.last_healthy_at or '-'}"
    return problems, detail


def check(agents: tuple[str, ...]) -> int:
    expectations, error = _role_expectations(agents)
    if error:
        for agent in agents:
            print(f"ERROR {agent}: effective installation configuration unavailable: {error}")
        return 1
    assert expectations is not None
    rc = 0
    for agent in agents:
        expectation = expectations[agent]
        if not expectation.enabled:
            print(f"DISABLED {agent}: {expectation.component} disabled by installation configuration")
            continue
        problems = []
        if not _timer_active(agent, expectation.unit_prefix):
            problems.append(f"{timer_unit(agent, expectation.unit_prefix)} not active")
        source_problems, detail = (
            _pipeline_status(expectation.max_age_s)
            if agent == "pipeline"
            else _runs_status(agent, expectation.max_age_s)
        )
        problems += source_problems
        status = "RED " if problems else "OK  "
        print(f"{status}{agent}: {'; '.join(problems) if problems else detail}")
        if problems:
            rc = 1
    return rc
