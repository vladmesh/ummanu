"""Memory, PO and web activation, process evidence and final verification."""

from __future__ import annotations

import hashlib
import os
import shlex
import stat
import time
from dataclasses import replace
from pathlib import Path

from ummanu import role_skills
from ummanu.head_registry import (
    HeadRegistryConfigError,
    assert_snapshot_current,
    generated_pair,
    installed_heads,
    installed_pair,
)
from ummanu.host_apply import (
    HostCommandError,
    UnitProcessIdentity,
    _process_start_ticks,
)
from ummanu.infra import process_receipts
from ummanu.infra.process_receipts import ReceiptError as ReceiptError
from ummanu.installation.upgrade.checkout import (
    _git_tracked_digest,
    _product_revision,
    planned_unit_names,
)
from ummanu.installation.upgrade.host import (
    step_host,
)
from ummanu.installation.upgrade.types import (
    DEPENDENCY_PATHS,
    MEMORY_COMPONENT,
    PACKAGED_UNIT_ROOT,
    PO_COMPONENT,
    PO_RESTART_POLL_SECONDS,
    PO_RESTART_WAIT_SECONDS,
    PRODUCT_SOURCE_PATHS,
    SCHEMA_PATHS,
    UPGRADE_RECEIPT_ROOT,
    WEB_COMPONENT,
    WEB_PROBE_TIMEOUT_SECONDS,
    GitError,
    StepResult,
    UpgradeContext,
    WebProcessReceiptError,
    _component_unit_prefix,
    _data_dir,
    _memory_unit_prefix,
    _po_unit,
    _process_unit_enabled,
    _upgrade_receipt_path,
)
from ummanu.installation.upgrade.workspaces import (
    _role_skills_manifest,
    _set_runtime_owner,
)
from ummanu.memory import DEFAULT_MODEL as DEFAULT_MEMORY_MODEL
from ummanu.memory.health import MemoryProbeError, probe_memory
from ummanu.po import client as po_client
from ummanu.web.health import WebProbeError, probe_web, target_from_unit
from ummanu.web.server import LoopbackOnly

MEMORY_PROCESS_RECEIPT_RELATIVE = UPGRADE_RECEIPT_ROOT / "memory-process-receipt.json"


def _source_digest(context: UpgradeContext, paths: tuple[str, ...], revision: str) -> str:
    if context.source_cut is None:
        return _git_tracked_digest(context.product_root, paths)
    return context.source_cut.digest(context.product_root, paths, revision=revision)


def _source_fence(context: UpgradeContext) -> None:
    if context.source_cut is not None:
        context.source_cut.check(context.product_root)


def _unit_environment(unit_text: bytes | None, key: str) -> str | None:
    """The last ``Environment=`` assignment of ``key`` in a unit file, if it makes one."""
    if unit_text is None:
        return None
    try:
        lines = unit_text.decode("utf-8").splitlines()
    except UnicodeError:
        return None
    value = None
    for line in lines:
        line = line.strip()
        if not line.startswith("Environment="):
            continue
        try:
            words = shlex.split(line[len("Environment=") :])
        except ValueError:
            continue
        for word in words:
            name, separator, item = word.partition("=")
            if separator and name == key:
                value = item
    return value


def memory_process_inputs(context: UpgradeContext, unit: str) -> dict[str, str | None]:
    """The checkout and installed state an active memory process has to be bound to."""
    _source_fence(context)
    model = _unit_environment(context.units.installed(unit), "MEMORY_MODEL") or DEFAULT_MEMORY_MODEL
    revision = _product_revision(context.product_root, ReceiptError)
    return {
        "product_revision": revision,
        "product_sha256": _source_digest(context, PRODUCT_SOURCE_PATHS, revision),
        "dependency_sha256": _git_tracked_digest(context.product_root, DEPENDENCY_PATHS),
        "memory_model": model,
        "memory_pack_sha256": context.memory_pack_digest,
    }


def _read_memory_process_receipt(
    context: UpgradeContext, unit: str
) -> tuple[tuple[UnitProcessIdentity, dict[str, str | None]] | None, str]:
    try:
        path = _upgrade_receipt_path(context, MEMORY_PROCESS_RECEIPT_RELATIVE)
    except ReceiptError as exc:
        return None, f"the memory process receipt cannot be located: {exc}"
    receipt, reason = process_receipts.read_process_receipt(path, process_receipts.MEMORY, unit)
    return ((receipt.identity, receipt.inputs), "") if receipt is not None else (None, reason)


def _memory_receipt_evidence(
    context: UpgradeContext,
    unit: str,
    identity: UnitProcessIdentity | None,
    inputs: dict[str, str | None],
) -> tuple[bool, str]:
    """Whether the active memory process is the one the receipt bound to these inputs, and why."""
    if identity is None:
        return False, "the active memory process identity is unavailable"
    receipt, reason = _read_memory_process_receipt(context, unit)
    if receipt is None:
        return False, reason
    recorded_identity, recorded = receipt
    mismatch = process_receipts.mismatch(
        process_receipts.MEMORY, recorded_identity, recorded, identity, inputs
    )
    if mismatch:
        return False, mismatch
    return True, f"memory process receipt verified: pid {identity.pid}; {_memory_inputs_summary(inputs)}"


def _memory_inputs_summary(inputs: dict[str, str | None]) -> str:
    return (
        f"product revision {process_receipts.short(inputs['product_revision'])}, "
        f"product sha256 {process_receipts.short(inputs['product_sha256'])}, "
        f"deps sha256 {process_receipts.short(inputs['dependency_sha256'])}, "
        f"model {inputs['memory_model']}, pack sha256 {process_receipts.short(inputs['memory_pack_sha256'])}"
    )


def step_memory(context: UpgradeContext) -> StepResult:
    """Keep the memory service running the checkout's code, dependencies, model and pack.

    Like `step_web`, the service is bound to what it was started on by a process receipt, so a
    service started before the checkout moved outside this upgrade is restarted, not reported as
    current. Unlike the web, a stopped memory service is started: the pipeline cannot run without it.
    """
    report = context.report
    unit = f"{_memory_unit_prefix(report)}service"
    if not _process_unit_enabled(context, MEMORY_COMPONENT, unit):
        return StepResult("memory", "skipped", f"{unit} is outside this installation's desired units")
    try:
        inputs = memory_process_inputs(context, unit)
    except ReceiptError as exc:
        return StepResult("memory", "failed", f"cannot compare the memory service with the checkout: {exc}")
    reasons = []
    try:
        active = context.units.is_active(unit)
    except HostCommandError as exc:
        return StepResult("memory", "failed", str(exc))
    if not active:
        reasons.append("service is not active")
    if context.unit_changed:
        reasons.append("unit file changed")
    if context.code_changed:
        reasons.append("product code or dependencies changed")
    if context.memory_pack_changed:
        reasons.append("memory pack export changed")
    if active:
        try:
            identity = context.units.process_identity(unit)
        except HostCommandError as exc:
            reasons.append(f"the active memory process identity could not be observed: {exc}")
        else:
            verified, evidence = _memory_receipt_evidence(context, unit, identity, inputs)
            if verified and not reasons:
                _source_fence(context)
                return StepResult("memory", "unchanged", evidence)
            if not verified:
                reasons.append(evidence)
    reason = "; ".join(reasons)
    if context.dry_run:
        return StepResult("memory", "changed", f"would restart {unit}: {reason}")
    try:
        context.units.restart(unit)
    except HostCommandError as exc:
        return StepResult("memory", "failed", str(exc))
    try:
        before_probe = context.units.process_identity(unit)
    except HostCommandError as exc:
        return StepResult(
            "memory", "failed", f"{unit} restarted but its process identity is unavailable: {exc}"
        )
    if before_probe is None:
        return StepResult("memory", "failed", f"{unit} restarted but its process identity is unavailable")
    try:
        data_dir = report.data_dir
        if data_dir is None:
            return StepResult(
                "memory", "failed", "authenticated probe: instance has no resolved data directory"
            )
        probe_memory(
            data_dir,
            runtime_handoff=lambda path: _set_runtime_owner(path, context.runtime_user),
            runtime_user=context.runtime_user,
        )
    except (MemoryProbeError, GitError) as exc:
        return StepResult("memory", "failed", f"authenticated probe failed: {exc}")
    try:
        after_probe = context.units.process_identity(unit)
        final_inputs = memory_process_inputs(context, unit)
    except (HostCommandError, ReceiptError) as exc:
        return StepResult("memory", "failed", f"{unit} probe passed but its receipt cannot be bound: {exc}")
    if after_probe != before_probe:
        return StepResult(
            "memory", "failed", f"{unit} changed process generation during the probe; no receipt was written"
        )
    if final_inputs != inputs:
        return StepResult(
            "memory", "failed", f"{unit} inputs changed during the restart; no receipt was written"
        )
    try:
        _source_fence(context)
        process_receipts.write_process_receipt(
            _upgrade_receipt_path(context, MEMORY_PROCESS_RECEIPT_RELATIVE),
            process_receipts.MEMORY,
            after_probe,
            final_inputs,
            context.runtime_user,
            ReceiptError,
            unit,
        )
    except ReceiptError as exc:
        return StepResult("memory", "failed", f"restarted and authenticated {unit} but {exc}")
    return StepResult(
        "memory",
        "changed",
        f"restarted and authenticated {unit} ({_memory_inputs_summary(final_inputs)}) and wrote the "
        f"memory process receipt: {reason}",
    )


WEB_PROCESS_RECEIPT_RELATIVE = Path("web") / "process-receipt.json"


def web_process_receipt_path(context: UpgradeContext) -> Path:
    data_dir = getattr(context.report, "data_dir", None)
    if not isinstance(data_dir, Path):
        raise WebProcessReceiptError("web process receipt: instance has no resolved data directory")
    return data_dir / WEB_PROCESS_RECEIPT_RELATIVE


def _digest_web_units(context: UpgradeContext, name_prefix: str) -> str:
    """Hash both shipped and installed web unit bytes without retaining their host-specific text."""
    digest = hashlib.sha256()
    digest.update(b"ummanu-web-units-v1\0")
    for name in (f"{name_prefix}.service", f"{name_prefix}-front.service"):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0installed\0")
        installed = context.units.installed(name)
        digest.update(installed if installed is not None else b"<absent>")
        shipped = context.product_root / PACKAGED_UNIT_ROOT / name
        digest.update(b"\0shipped\0")
        try:
            info = shipped.lstat()
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise OSError("not a regular file")
            digest.update(shipped.read_bytes())
        except FileNotFoundError:
            digest.update(b"<absent>")
        except OSError as exc:
            raise WebProcessReceiptError(f"cannot read shipped web unit {shipped}: {exc}") from None
    return digest.hexdigest()


def web_process_inputs(context: UpgradeContext, name_prefix: str) -> dict[str, str]:
    """The product and materialized state an active web process has to be bound to."""
    _source_fence(context)
    revision = _product_revision(context.product_root, WebProcessReceiptError)
    try:
        snapshot = installed_pair(context.instance_path, _data_dir(context)).snapshot
    except HeadRegistryConfigError as exc:
        raise WebProcessReceiptError(str(exc)) from None
    try:
        snapshot_bytes = snapshot.read_bytes()
    except FileNotFoundError:
        snapshot_bytes = b"<missing>"
    except OSError as exc:
        raise WebProcessReceiptError(f"cannot read head registry snapshot {snapshot}: {exc}") from None
    return {
        "product_revision": revision,
        "product_sha256": _source_digest(context, PRODUCT_SOURCE_PATHS, revision),
        "dependency_sha256": _git_tracked_digest(context.product_root, DEPENDENCY_PATHS),
        "schemas_sha256": _source_digest(context, SCHEMA_PATHS, revision),
        "web_units_sha256": _digest_web_units(context, name_prefix),
        "head_registry_sha256": hashlib.sha256(snapshot_bytes).hexdigest(),
    }


def _read_web_process_receipt(
    context: UpgradeContext, unit: str
) -> tuple[process_receipts.ProcessReceipt | None, str]:
    receipt, reason = process_receipts.read_process_receipt(
        web_process_receipt_path(context), process_receipts.WEB, unit
    )
    return receipt, reason


def _receipt_evidence(
    context: UpgradeContext,
    unit: str,
    identity: UnitProcessIdentity | None,
    inputs: dict[str, str],
) -> tuple[bool, str]:
    if identity is None:
        return False, "the active web process identity is unavailable"
    receipt, reason = _read_web_process_receipt(context, unit)
    if receipt is None:
        return False, reason
    mismatch = process_receipts.mismatch(
        process_receipts.WEB, receipt.identity, receipt.inputs, identity, inputs
    )
    if mismatch:
        return False, mismatch
    evidence = (
        f"web process receipt verified: pid {identity.pid}, start ticks {identity.start_ticks}, "
        f"invocation {identity.invocation_id}; product revision {inputs['product_revision']}; "
        f"product {inputs['product_sha256']}; dependencies {inputs['dependency_sha256']}; "
        f"schemas {inputs['schemas_sha256']}; web units {inputs['web_units_sha256']}; "
        f"head registry {inputs['head_registry_sha256']}"
    )
    return True, evidence


def _write_web_process_receipt(
    context: UpgradeContext,
    unit: str,
    identity: UnitProcessIdentity,
    inputs: dict[str, str],
) -> None:
    """Publish the receipt only after a proved generation, as one private atomic replacement."""
    _source_fence(context)
    process_receipts.write_process_receipt(
        web_process_receipt_path(context),
        process_receipts.WEB,
        identity,
        inputs,
        context.runtime_user,
        WebProcessReceiptError,
        unit,
    )


# The PO service writes its own process receipt when it starts (`ummanu po-serve`), unlike the web
# and memory receipts, which the upgrade writes after its own restart: an upgrade only asks the PO
# service to restart, and a busy service exits long after the upgrade that asked has ended
# (secretary-1759). It lives in the service's own directory, which the runtime user owns, and no
# backup carries it (`po-service/` is outside the backed-up roots).
def po_process_inputs(product_root: Path, *, context: UpgradeContext | None = None) -> dict[str, str]:
    """The checkout a PO process has to be bound to: its code, dependencies and bundled schemas."""
    if context is not None:
        if context.product_root != product_root:
            raise ReceiptError("PO inputs selected a different product root from their upgrade context")
        _source_fence(context)
    revision = _product_revision(product_root, ReceiptError)
    return {
        "product_revision": revision,
        "product_sha256": (
            _source_digest(context, PRODUCT_SOURCE_PATHS, revision) if context is not None
            else _git_tracked_digest(product_root, PRODUCT_SOURCE_PATHS)
        ),
        "dependency_sha256": _git_tracked_digest(product_root, DEPENDENCY_PATHS),
        "schemas_sha256": (
            _source_digest(context, SCHEMA_PATHS, revision) if context is not None
            else _git_tracked_digest(product_root, SCHEMA_PATHS)
        ),
    }


def write_po_process_receipt(
    data_dir: Path, product_root: Path, *, environ: dict[str, str] | None = None
) -> str:
    """Bind the calling PO service process to the checkout it was started on; the journal line.

    The identity is the one `SystemdUnitInstaller.process_identity` observes from outside: this pid,
    its kernel start ticks and systemd's ``INVOCATION_ID``. A process systemd did not start has no
    invocation id and gets no receipt, so an upgrade asks it to restart. The receipt is one private
    atomic replacement, so a new process generation replaces the previous one's.
    """
    env = os.environ if environ is None else environ
    pid = os.getpid()
    invocation_id = env.get("INVOCATION_ID", "")
    if not invocation_id:
        raise ReceiptError("no INVOCATION_ID: the process was not started by systemd")
    start_ticks = _process_start_ticks(pid)
    if start_ticks is None:
        raise ReceiptError(f"cannot read the start ticks of pid {pid}")
    identity = UnitProcessIdentity(pid, start_ticks, invocation_id)
    inputs = po_process_inputs(product_root)
    path = po_client.process_receipt_path(data_dir)
    process_receipts.write_process_receipt(path, process_receipts.PO, identity, inputs, None, ReceiptError)
    return f"wrote the PO process receipt {path}: pid {pid}; {_po_inputs_summary(inputs)}"


def _read_po_process_receipt(
    data_dir: Path,
) -> tuple[tuple[UnitProcessIdentity, dict[str, str]] | None, str]:
    receipt, reason = process_receipts.read_process_receipt(
        po_client.process_receipt_path(data_dir), process_receipts.PO
    )
    return ((receipt.identity, receipt.inputs), "") if receipt is not None else (None, reason)


def _po_receipt_evidence(
    data_dir: Path | None, identity: UnitProcessIdentity | None, inputs: dict[str, str]
) -> tuple[bool, str]:
    """Whether the active PO process is the one its receipt bound to these inputs, and why."""
    if data_dir is None:
        return False, "the PO process receipt cannot be located: instance has no resolved data directory"
    if identity is None:
        return False, "the active PO process identity is unavailable"
    receipt, reason = _read_po_process_receipt(data_dir)
    if receipt is None:
        return False, reason
    recorded_identity, recorded = receipt
    mismatch = process_receipts.mismatch(process_receipts.PO, recorded_identity, recorded, identity, inputs)
    if mismatch:
        return False, mismatch
    return True, f"PO process receipt verified: pid {identity.pid}, {_po_inputs_summary(inputs)}"


def _po_inputs_summary(inputs: dict[str, str]) -> str:
    return (
        f"revision {process_receipts.short(inputs['product_revision'])}, "
        f"product sha256 {process_receipts.short(inputs['product_sha256'])}, "
        f"deps sha256 {process_receipts.short(inputs['dependency_sha256'])}, "
        f"schemas sha256 {process_receipts.short(inputs['schemas_sha256'])}"
    )


def step_po(context: UpgradeContext) -> StepResult:
    """Put the PO service on this upgrade's code without killing a running PO turn.

    Every PO turn is a child of `ummanu-po.service`, and the PO itself runs `ummanu upgrade`
    inside a turn, so restarting the unit here would kill and re-run this very upgrade's caller. The
    one rule is the service's (`PoService.request_restart`, asked through
    `ummanu.po.client.request_restart`): idle, it exits now and `Restart=always` starts the new
    code, which this step waits for; busy, it takes no new turn and exits as soon as its running turns
    settle, and this step reports the restart as deferred rather than failing.

    The reasons are the service's process inputs: the product source and dependencies, the bundled
    schemas and the unit file (an update; a unit reconcile just created was started on this code).
    This run's pull delta is not enough: on an installation whose dispatcher release fast-forwards the
    checkout before the PO runs `upgrade`, the pull is empty while the service still runs the old code
    (secretary-1759). So, like the web and memory services, the running process is bound to what it
    was started on by a process receipt the service writes at start (`write_po_process_receipt`): a
    missing one, one of another process generation, or one whose revision or digests differ from the
    checkout is a reason too. Like
    the memory service and unlike the web, a stopped PO service is started: nothing runs in a stopped
    unit, and without it no PO turn runs at all. An installation that opted the component out has no
    unit, and the step is skipped.
    """
    report = context.report
    unit = _po_unit(report)
    if not _process_unit_enabled(context, PO_COMPONENT, unit):
        return StepResult("po", "skipped", f"{unit} is outside this installation's desired units")
    if context.units.installed(unit) is None:
        return StepResult("po", "skipped", f"{unit} is not installed; this host runs no PO service")
    try:
        active = context.units.is_active(unit)
    except HostCommandError as exc:
        return StepResult("po", "failed", str(exc))
    if not active:
        if context.dry_run:
            return StepResult("po", "changed", f"would start {unit}: service is not active")
        try:
            context.units.restart(unit)
        except HostCommandError as exc:
            return StepResult("po", "failed", f"starting {unit} failed: {exc}")
        return StepResult("po", "changed", f"started {unit}: service was not active")
    try:
        inputs = po_process_inputs(context.product_root, context=context)
    except ReceiptError as exc:
        return StepResult("po", "failed", f"cannot compare the PO service with the checkout: {exc}")
    reasons = []
    if context.po_unit_changed or planned_unit_names(
        context.changed_paths, f"{_component_unit_prefix(report, PO_COMPONENT)}."
    ):
        reasons.append("the PO unit file changed")
    if context.schemas_changed:
        reasons.append("bundled schemas changed")
    if context.code_changed:
        reasons.append("product code or dependencies changed")
    try:
        identity = context.units.process_identity(unit)
    except HostCommandError as exc:
        reasons.append(f"the active PO process identity could not be observed: {exc}")
    else:
        verified, evidence = _po_receipt_evidence(report.data_dir, identity, inputs)
        if verified and not reasons:
            _source_fence(context)
            return StepResult("po", "unchanged", evidence)
        if not verified:
            reasons.append(evidence)
    reason = "; ".join(reasons)
    if context.dry_run:
        return StepResult("po", "changed", f"would ask {unit} to restart once no PO turn runs: {reason}")
    data_dir = report.data_dir
    if data_dir is None:
        return StepResult("po", "failed", "the instance has no resolved data directory for the PO service")
    try:
        before = context.units.process_identity(unit)
        directory = po_client.service_dir(data_dir)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        _set_runtime_owner(directory, context.runtime_user)
        answer = po_client.request_restart(data_dir, reason)
        _set_runtime_owner(po_client.restart_marker_path(data_dir), context.runtime_user)
    except (HostCommandError, GitError, OSError) as exc:
        return StepResult("po", "failed", f"could not ask {unit} to restart: {exc}")
    if answer.outcome == "deferred":
        return StepResult(
            "po",
            "changed",
            f"PO service restart deferred: {answer.running} turn(s) running; "
            f"{unit} restarts itself once they end: {reason}",
        )
    if answer.outcome == "unanswered":
        return StepResult(
            "po",
            "changed",
            f"PO service restart requested but not acknowledged ({answer.detail}); "
            f"{unit} restarts itself at its next idle check: {reason}",
        )
    deadline = _monotonic() + PO_RESTART_WAIT_SECONDS
    while True:
        try:
            after = context.units.process_identity(unit)
        except HostCommandError:
            after = None
        if after is not None and after != before and context.units.is_active(unit):
            return StepResult("po", "changed", f"restarted {unit} while idle (pid {after.pid}): {reason}")
        if _monotonic() >= deadline:
            return StepResult(
                "po",
                "failed",
                f"{unit} exited for the restart but no new process came up within {PO_RESTART_WAIT_SECONDS:g}s",
            )
        _sleep(PO_RESTART_POLL_SECONDS)


def _monotonic() -> float:
    return time.monotonic()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def step_web(context: UpgradeContext) -> StepResult:
    """Make the long-lived web process coherent with what this upgrade just materialized.

    The web transport was the one supported long-lived process an upgrade did not own. That is not
    a gap you can see from a step list: `ummanu upgrade` reported `ok`, the checkout was current,
    the unit was current, and `ummanu-web.service` went on running the callables it had imported
    a day earlier. On 2026-09-11 that process answered every route with an empty reply for nineteen
    hours, because `importlib.resources` reads the bundled schemas from the checkout *now* while the
    validator that reads them was loaded *then*: a new relative `$ref` against an old registry-less
    validator (secretary-1624). Timestamps could not fix that and were never the defect; the process
    was. So the reconciliation is a restart, and it is here, after everything the new process needs.

    Three properties are load-bearing:

    **It is last of the materializing steps.** A restart is the moment the old code stops being the
    code that answers, so everything the new process reads has to be in place first: the checkout
    (`step_pull`), the installed dependencies and the bundled schemas that came with them
    (`step_dependencies`), and the unit itself (`step_host`). `run_steps` stops at the first failed
    step, so a failed prerequisite means this step does not run at all and the run is failed — it
    can never be the case that something reported the web as serving current code over a failed
    prerequisite.

    **A restart without a read is not evidence.** systemd accepting `restart` says nothing about
    whether the new process bound its socket or whether it can answer. So a restart is followed by
    one bounded loopback GET against the address the *installed unit* names, and a probe that does
    not come back 200 fails the step: an upgrade that cannot read the transport it just restarted
    does not get to say the transport is current.

    **The unit is optional and this step never starts it.** The web is not on every installation,
    and a stopped transport is a state an operator chose — an upgrade that quietly published a
    dashboard because it found a unit file would be making that choice for them. So an uninstalled
    or inactive unit is reported as skipped, with which of the two it was, and nothing is started.
    That is the one place this step deliberately differs from `step_memory`, which starts a stopped
    memory service because the pipeline cannot run without it.

    Only `ummanu-web.service` is restarted. `ummanu-web-front.service` is `PartOf=` it and
    comes along, which is also why a change to either unit file is a reason to restart this one.

    The restart reasons are every process-local input this upgrade materializes: the product source
    and its dependencies, the bundled schemas, the shipped web unit files, and the head registry
    snapshot — `load_registry` caches per process, so a profile added to the canon by a `--no-pull`
    run is invisible to the running transport until it is replaced, and that made the documented
    `heads.toml` procedure need a manual restart nobody was reminded of. A unit change is read from
    both sides: `web_unit_changed` is what reconcile actually did, and `planned_unit_names` is what
    the target revision will do, which is the only one of the two that can answer under `--dry-run`.
    """
    report = context.report
    name_prefix = _component_unit_prefix(report, WEB_COMPONENT)
    unit = f"{name_prefix}.service"
    if not _process_unit_enabled(context, WEB_COMPONENT, unit):
        return StepResult("web", "skipped", f"{unit} is outside this installation's desired units")
    installed = context.units.installed(unit)
    if installed is None:
        return StepResult("web", "skipped", f"{unit} is not installed; this host serves no web transport")
    try:
        active = context.units.is_active(unit)
    except HostCommandError as exc:
        return StepResult("web", "failed", str(exc))
    if not active:
        return StepResult(
            "web", "skipped", f"{unit} is installed but not active; an upgrade does not start it"
        )
    try:
        inputs = web_process_inputs(context, name_prefix)
    except WebProcessReceiptError as exc:
        return StepResult("web", "failed", str(exc))
    reasons = []
    if context.web_unit_changed or planned_unit_names(context.changed_paths, name_prefix):
        reasons.append("a web unit file changed")
    if context.web_front_config_changed:
        reasons.append("the web front configuration was rendered")
    if context.schemas_changed:
        reasons.append("bundled schemas changed")
    if context.code_changed:
        reasons.append("product code or dependencies changed")
    if context.head_registry_changed:
        reasons.append("the head registry snapshot changed")
    try:
        identity = context.units.process_identity(unit)
    except HostCommandError as exc:
        identity = None
        receipt_reason = f"the active web process identity could not be observed: {exc}"
    else:
        verified, receipt_reason = _receipt_evidence(context, unit, identity, inputs)
        if verified and not reasons:
            _source_fence(context)
            return StepResult("web", "unchanged", receipt_reason)
    reasons.append(receipt_reason)
    try:
        target = target_from_unit(installed)
    except LoopbackOnly as refused:
        return StepResult("web", "failed", f"{unit} does not serve a loopback address: {refused}")
    reason = "; ".join(reasons)
    if context.dry_run:
        return StepResult("web", "changed", f"would restart {unit} and probe {target.url}: {reason}")
    try:
        context.units.restart(unit)
    except HostCommandError as exc:
        return StepResult("web", "failed", f"restarting {unit} failed: {exc}")
    try:
        before_probe = context.units.process_identity(unit)
    except HostCommandError as exc:
        return StepResult("web", "failed", f"{unit} restarted but its process identity is unavailable: {exc}")
    if before_probe is None:
        return StepResult("web", "failed", f"{unit} restarted but its process identity is unavailable")
    try:
        status = probe_web(target, timeout_seconds=WEB_PROBE_TIMEOUT_SECONDS)
    except WebProbeError as exc:
        return StepResult("web", "failed", f"{unit} restarted but the loopback probe failed: {exc}")
    try:
        after_probe = context.units.process_identity(unit)
    except HostCommandError as exc:
        return StepResult(
            "web", "failed", f"{unit} probe passed but its process identity is unavailable: {exc}"
        )
    if after_probe != before_probe:
        return StepResult(
            "web",
            "failed",
            f"{unit} changed process generation during the loopback probe; no receipt was written",
        )
    try:
        final_inputs = web_process_inputs(context, name_prefix)
    except WebProcessReceiptError as exc:
        return StepResult("web", "failed", f"{unit} probe passed but receipt inputs could not be read: {exc}")
    if final_inputs != inputs:
        return StepResult(
            "web", "failed", f"{unit} materialized inputs changed during restart; no receipt was written"
        )
    try:
        _write_web_process_receipt(context, unit, after_probe, final_inputs)
    except WebProcessReceiptError as exc:
        return StepResult("web", "failed", str(exc))
    return StepResult(
        "web",
        "changed",
        f"restarted {unit} and probed {target.url} -> {status}; wrote web process receipt: {reason}",
    )


def step_verify(context: UpgradeContext) -> StepResult:
    """Re-plan against the host we just wrote. A second pass must be a no-op."""
    if context.dry_run:
        return StepResult("verify", "skipped", "--dry-run made no changes to verify")
    probe = replace(context, dry_run=True, pull=False)
    result = step_host(probe)
    if result.failed:
        return StepResult("verify", "failed", f"host is still not reconciled: {result.detail}")
    if result.status == "changed":
        return StepResult("verify", "failed", f"reconcile is not idempotent: {result.detail}")
    name_prefix = _component_unit_prefix(context.report, WEB_COMPONENT)
    web_unit = f"{name_prefix}.service"
    web_evidence = ""
    po_unit = _po_unit(context.report)
    try:
        web_active = (
            _process_unit_enabled(context, WEB_COMPONENT, web_unit)
            and context.units.installed(web_unit) is not None
            and context.units.is_active(web_unit)
        )
        po_installed = (
            _process_unit_enabled(context, PO_COMPONENT, po_unit)
            and context.units.installed(po_unit) is not None
        )
        po_active = po_installed and context.units.is_active(po_unit)
    except HostCommandError as exc:
        return StepResult("verify", "failed", str(exc))
    if web_active:
        try:
            inputs = web_process_inputs(context, name_prefix)
            identity = context.units.process_identity(web_unit)
        except (HostCommandError, WebProcessReceiptError) as exc:
            return StepResult("verify", "failed", f"active web process receipt cannot be verified: {exc}")
        verified, web_evidence = _receipt_evidence(context, web_unit, identity, inputs)
        if not verified:
            return StepResult(
                "verify", "failed", f"active web process receipt is not current: {web_evidence}"
            )
    if not po_installed:
        po_evidence = f"PO process receipt not checked: {po_unit} is not installed"
    elif not po_active:
        po_evidence = f"PO process receipt not checked: {po_unit} is not active"
    else:
        data_dir = getattr(context.report, "data_dir", None)
        try:
            po_inputs = po_process_inputs(context.product_root, context=context)
            po_identity = context.units.process_identity(po_unit)
        except (HostCommandError, ReceiptError) as exc:
            return StepResult("verify", "failed", f"active PO process receipt cannot be verified: {exc}")
        verified, po_evidence = _po_receipt_evidence(data_dir, po_identity, po_inputs)
        if not verified:
            # A busy service restarts itself once its turns end (`step_po` deferred it): the PO runs
            # `ummanu upgrade` inside a turn, so its own upgrade always ends here.
            if data_dir is None or not po_client.restart_marker_path(data_dir).exists():
                return StepResult(
                    "verify", "failed", f"active PO process receipt is not current: {po_evidence}"
                )
            po_evidence = f"PO service restart pending: {po_evidence}"
    try:
        audit = role_skills.audit(
            instance_path=context.instance_path,
            product_manifest=_role_skills_manifest(context),
            home=context.runtime_home,
            data_dir=_data_dir(context),
        )
    except (OSError, ValueError) as exc:
        return StepResult("verify", "failed", str(exc))
    if not audit["ok"]:
        return StepResult("verify", "failed", "role skills are still out of sync")
    try:
        assert_snapshot_current(context.instance_path, context.product_root, _data_dir(context))
        installed_heads(context.instance_path, generated_pair(context.instance_path, _data_dir(context)))
    except HeadRegistryConfigError as exc:
        return StepResult("verify", "failed", str(exc))
    detail = "host reconciled and role skills in sync"
    if web_evidence:
        detail += f"; {web_evidence}"
    detail += f"; {po_evidence}"
    _source_fence(context)
    return StepResult("verify", "unchanged", detail)
