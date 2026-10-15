"""File-only upgrade observations; every process, service and host operation is replaced."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.fakes.upgrade import FakeUnitInstaller
from ummanu.host_apply import HostCommandError, UnitProcessIdentity
from ummanu.installation import materialize


def capture(entry, checkout, dependencies, host, services, types, root: Path) -> dict:
    """Use the same fixture on concrete candidate owners or an explicitly supplied old module."""
    packet = {"order": [step.__name__ for step in entry.STEPS]}
    report = SimpleNamespace(
        ok=True,
        errors=[],
        instance_path=root / "instance" / "instance.yaml",
        data_dir=root / "data",
        host={"unit_prefix": "ummanu-"},
    )
    units = FakeUnitInstaller()

    def context(**kw):
        return types.UpgradeContext(
            instance_path=root / "instance",
            product_root=root / "product",
            base_branch="main",
            dry_run=False,
            units=units,
            report=report,
            runtime_home=root / "home",
            **kw,
        )

    def outcome(value):
        return {
            "steps": [asdict(step) for step in value.steps],
            "ok": value.ok,
            "changed": value.changed,
            "text": value.render(),
        }

    trace = []

    def first(ctx):
        trace.append(["first", ctx.base_branch, ctx.pull])
        ctx.code_changed = True
        return types.StepResult("first", "changed", "materialized")

    def failed(ctx):
        trace.append(["failed", ctx.code_changed])
        raise HostCommandError("fixture command failure")

    def last(ctx):
        trace.append(["last", ctx.code_changed])
        return types.StepResult("last", "unchanged")

    packet["steps"] = outcome(entry.run_steps(context(), (first, failed, last)))
    packet["trace"] = trace.copy()

    parser = argparse.ArgumentParser()
    entry.add_upgrade_command(parser.add_subparsers(dest="command"))
    cli = []
    for flags in (("--no-pull",), ("--no-pull", "--json"), ("--dry-run",)):
        args = parser.parse_args(
            ["upgrade", "--instance", str(root / "instance"), "--product-root", str(root / "product"), *flags]
        )
        seen = []

        def run(ctx, seen=seen):
            seen.append([ctx.pull, ctx.dry_run, str(ctx.product_root), str(ctx.runtime_home)])
            return types.UpgradeResult([types.StepResult("fixture", "unchanged", "current")])

        output = io.StringIO()
        with (
            mock.patch.dict(entry.os.environ, {}, clear=True),
            mock.patch.object(entry, "validate_instance", return_value=report),
            mock.patch.object(entry, "resolve_runtime_owner", return_value=("owner", root / "home")),
            mock.patch.object(entry, "SystemdUnitInstaller", return_value=units),
            mock.patch.object(entry, "run_steps", side_effect=run) as run_mock,
            contextlib.redirect_stdout(output),
        ):
            code = entry.run_upgrade(args)
        run_mock.assert_called_once()
        cli.append([list(flags), code, output.getvalue(), seen])
    packet["cli"] = cli

    errors = []
    for kind in ("config", "owner", "handoff-head", "handoff-dirty", "handoff-paths", "pull"):
        output = io.StringIO()
        args = parser.parse_args(
            ["upgrade", "--instance", str(root / "instance"), "--product-root", str(root / "product")]
        )
        marker = {"before": "a" * 40, "after": "b" * 40, "changed_paths": ["src/x.py"]}
        if kind == "handoff-paths":
            marker["changed_paths"] = [42]
        env = {"UMMANU_UPGRADE_HANDOFF": json.dumps(marker)} if kind.startswith("handoff") else {}
        with (
            mock.patch.dict(entry.os.environ, env, clear=True),
            mock.patch.object(
                entry,
                "validate_instance",
                return_value=(
                    SimpleNamespace(ok=False, errors=["invalid config"]) if kind == "config" else report
                ),
            ),
            mock.patch.object(
                entry,
                "resolve_runtime_owner",
                side_effect=(ValueError("unknown owner") if kind == "owner" else None),
                return_value=("owner", root / "home"),
            ),
            mock.patch.object(entry, "SystemdUnitInstaller", return_value=units),
            mock.patch.object(
                entry,
                "_git",
                side_effect=[
                    "c" * 40 if kind == "handoff-head" else "b" * 40,
                    "dirty" if kind == "handoff-dirty" else "",
                ],
            ),
            mock.patch.object(entry, "step_pull", return_value=types.StepResult("pull", "failed", "refused")),
            mock.patch.object(entry, "run_steps") as run_mock,
            contextlib.redirect_stdout(output),
        ):
            code = entry.run_upgrade(args)
        run_mock.assert_not_called()
        errors.append([kind, code, output.getvalue()])
    packet["errors"] = errors

    handoffs = []
    for inherited in (False, True):
        marker = {"before": "a" * 40, "after": "b" * 40, "changed_paths": ["src/x.py"]}

        def pull(ctx, marker=marker):
            ctx.pulled_before, ctx.pulled_after = marker["before"], marker["after"]
            ctx.changed_paths = tuple(marker["changed_paths"])
            return types.StepResult("pull", "changed", "pulled")

        output = io.StringIO()
        with (
            mock.patch.dict(
                entry.os.environ,
                {"UMMANU_UPGRADE_HANDOFF": json.dumps(marker)} if inherited else {},
                clear=True,
            ),
            mock.patch.object(entry, "validate_instance", return_value=report),
            mock.patch.object(entry, "resolve_runtime_owner", return_value=("owner", root / "home")),
            mock.patch.object(entry, "SystemdUnitInstaller", return_value=units),
            mock.patch.object(entry, "_git", side_effect=["b" * 40, ""]),
            mock.patch.object(entry, "step_pull", side_effect=pull) as pulled,
            mock.patch.object(entry, "_exec_pulled_upgrade", side_effect=OSError("fixture exec")) as handoff,
            mock.patch.object(entry, "run_steps", return_value=types.UpgradeResult()) as run_mock,
            contextlib.redirect_stdout(output),
        ):
            code = entry.run_upgrade(args)
        if inherited:
            pulled.assert_not_called()
            handoff.assert_not_called()
            run_mock.assert_called_once()
            ctx = run_mock.call_args.args[0]
            observed = [ctx.pull, ctx.handoff_before, ctx.handoff_after, list(ctx.changed_paths)]
        else:
            pulled.assert_called_once()
            handoff.assert_called_once()
            run_mock.assert_not_called()
            observed = [str(handoff.call_args.args[1]), handoff.call_args.kwargs]
        handoffs.append([inherited, code, output.getvalue(), observed])
    packet["handoffs"] = handoffs

    # Execute only the argv/env builder; the operating system never receives an exec call.
    args = parser.parse_args(
        [
            "upgrade",
            "--instance",
            str(root / "instance"),
            "--json",
            "--runtime-user",
            "owner",
            "--host-fixture",
            str(root / "host"),
        ]
    )
    with (
        mock.patch.dict(entry.os.environ, {"INHERITED": "kept"}, clear=True),
        mock.patch.object(Path, "is_file", return_value=True),
        mock.patch.object(entry.os, "execve") as execute,
    ):
        entry._exec_pulled_upgrade(
            args,
            root / "product",
            before="a" * 40,
            after="b" * 40,
            changed_paths=("src/x.py", "pyproject.toml"),
        )
    execute.assert_called_once()
    python, argv, env = execute.call_args.args
    packet["exec"] = [str(python), argv, env]

    # Dependency acquisition and installation are doubles; the actual private writer writes
    # only this fixture's data directory, making the dependency receipt bytes observable.
    python = root / "product" / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True, exist_ok=True)
    python.touch()
    with (
        mock.patch.object(dependencies, "_git_tracked_digest", return_value="c" * 64),
        mock.patch.object(dependencies, "required_extras", return_value=("dev", "memory")),
        mock.patch.object(dependencies, "_snapshot_install", return_value=False),
        mock.patch.object(dependencies, "_ruff_runtime_problem", return_value=""),
        mock.patch.object(dependencies._proc, "run") as pip,
    ):
        ctx = context()
        installed = dependencies.step_dependencies(ctx)
        repeated = dependencies.step_dependencies(context())
    pip.assert_called_once()
    packet["dependencies"] = [
        asdict(installed),
        asdict(repeated),
        ctx.code_changed,
        list(pip.call_args.args[0]),
        pip.call_args.kwargs,
    ]
    packet["dependency_bytes"] = (report.data_dir / "upgrade" / "dependency-receipt.json").read_text()

    identity = UnitProcessIdentity(123, 456, "invocation")
    inputs = {
        "product_revision": "a" * 40,
        "product_sha256": "b" * 64,
        "dependency_sha256": "c" * 64,
        "schemas_sha256": "d" * 64,
        "web_units_sha256": "e" * 64,
        "head_registry_sha256": "f" * 64,
    }
    unit = "ummanu-web.service"
    units.files[unit] = b"[Service]\nExecStart=/x --host 127.0.0.1 --port 8787\n"
    units.active.add(unit)
    with (
        mock.patch.object(services, "web_process_inputs", return_value=inputs) as acquired,
        mock.patch.object(units, "process_identity", return_value=identity),
        mock.patch.object(services, "probe_web", return_value=200) as probe,
    ):
        activated = services.step_web(context())
        repeated = services.step_web(context())
    probe.assert_called_once()
    assert acquired.call_count == 3
    packet["service"] = [asdict(activated), asdict(repeated), units.calls]
    packet["web_bytes"] = (report.data_dir / "web" / "process-receipt.json").read_text()

    memory_inputs = {key: inputs[key] for key in ("product_revision", "product_sha256", "dependency_sha256")}
    memory_inputs.update(memory_model="model/v1", memory_pack_sha256=None)
    unit = "ummanu-memory.service"
    units.files[unit] = b"[Service]\n"
    units.active.add(unit)
    with (
        mock.patch.object(services, "memory_process_inputs", return_value=memory_inputs),
        mock.patch.object(units, "process_identity", return_value=identity),
        mock.patch.object(services, "probe_memory") as probe,
    ):
        packet["memory"] = asdict(services.step_memory(context()))
    probe.assert_called_once()
    packet["memory_bytes"] = (report.data_dir / "upgrade" / "memory-process-receipt.json").read_text()
    with (
        mock.patch.object(services.os, "getpid", return_value=123),
        mock.patch.object(services, "_process_start_ticks", return_value=456),
        mock.patch.object(
            services,
            "po_process_inputs",
            return_value={
                key: inputs[key]
                for key in ("product_revision", "product_sha256", "dependency_sha256", "schemas_sha256")
            },
        ),
    ):
        packet["po"] = services.write_po_process_receipt(
            report.data_dir, root / "product", environ={"INVOCATION_ID": "invocation"}
        )
    packet["po_bytes"] = (report.data_dir / "po-service" / "process-receipt.json").read_text()

    # Both public install/recover branches use their actual materialize_host composition,
    # with three representative steps and every runtime/host operation replaced.
    flows = []
    for recovery in (False, True):
        trace.clear()
        with (
            mock.patch.object(materialize, "validate_instance", return_value=report),
            mock.patch.object(materialize, "check_product_runtime"),
            mock.patch.object(materialize, "resolve_runtime_owner", return_value=("owner", root / "home")),
            mock.patch.object(materialize, "SystemdUnitInstaller", return_value=units),
            mock.patch.object(materialize, "UpgradeContext", types.UpgradeContext),
            mock.patch.object(materialize, "UpgradeResult", types.UpgradeResult),
            mock.patch.object(
                materialize,
                "run_steps",
                side_effect=lambda ctx, steps=None: entry.run_steps(
                    ctx, (first, last) if steps is None else steps
                ),
            ),
            mock.patch.object(materialize, "STEPS", (first, host.step_pipeline_state, last)),
            mock.patch.object(materialize, "step_pipeline_state", host.step_pipeline_state),
            mock.patch.object(materialize, "step_host", last),
        ):
            result = materialize.materialize_host(
                root / "instance",
                root / "product",
                installation_user="owner",
                before_host=(lambda ctx: trace.append(["restore", ctx.code_changed])) if recovery else None,
            )
        flows.append([recovery, trace.copy(), outcome(result)])
    packet["install_recover"] = flows

    # Only temporary path spellings vary between the two executions.
    return json.loads(json.dumps(packet).replace(str(root), "<fixture>"))
