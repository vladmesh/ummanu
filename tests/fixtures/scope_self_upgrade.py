"""A harmless CI head performing host reconcile inside its own PO scope."""

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.fakes.upgrade import FakeUnitInstaller
from tests.runtime_scope_fixtures import host_fixture
from ummanu import cli, status
from ummanu.host_apply import SystemdUnitInstaller
from ummanu.installation.upgrade import host as upgrade_host, types as upgrade_types
from ummanu.runtime.head.memory import scope_unit

root, data, run_id = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
unit = scope_unit(run_id)
instance, packaged, desired, fixture = host_fixture(root, data, unit)
instance_dir = root / "instance"
instance_dir.mkdir()
report = SimpleNamespace(instance=instance, bindings=[], host=instance["host"], data_dir=data,
                         instance_path=instance_dir / "instance.yaml", name="disposable", projects=[])
owner = data / "po-heads" / run_id / "scope-owner.json"
# The supervisor may be a few microseconds behind the head it just forked.
# Wait for its launch journal rather than letting the fixture race that receipt.
deadline = time.monotonic() + 5
while not (owner.parent / "journal.jsonl").exists():
    if time.monotonic() > deadline:
        raise RuntimeError("CI supervisor did not publish its launch journal")
    time.sleep(0.01)
before = owner.read_bytes()
results = []
effects = []
for dry in (True, False):
    installer = FakeUnitInstaller()
    context = upgrade_types.UpgradeContext(instance_path=instance_dir, product_root=root, base_branch="main",
                                      dry_run=dry, units=installer, report=report, host_fixture=fixture)
    result = upgrade_host.step_host(context)
    if result.status == "failed":
        raise RuntimeError(result.detail)
    results.append(result.status)
    effects.extend(installer.calls)
    if any(name == unit for _, name in installer.calls):
        raise RuntimeError("host reconcile attempted an effect on its own runtime scope")
    if owner.read_bytes() != before:
        raise RuntimeError("host reconcile changed the runtime owner")
    if unit in (data / "host-managed.json").read_text():
        raise RuntimeError("host reconcile persisted a runtime scope in the packaged manifest")
with mock.patch("ummanu.host_apply.resolve_installed_packaged", return_value=packaged):
    _, collected, diffs = cli.collect_host_inventory(report, SimpleNamespace(host_fixture=str(fixture)))
if collected.errors or unit in diffs["units"].unmanaged_on_host or unit in diffs["units"].missing_on_host:
    raise RuntimeError("doctor did not preserve its own owned runtime scope")
with mock.patch.object(status, "resolve_installed_packaged", return_value=packaged):
    snapshot = status.collect_status(report, host_fixture=str(fixture), sprints=False, recovery={"fixture": True})
if run_id not in [row["run_id"] for row in snapshot["host"]["runtime_scopes"]]:
    raise RuntimeError("status did not report its active runtime scope")

# Exercise native host enumeration and the real installer on a fully reconciled
# disposable contour. This catalogue has no persistent units; the only live
# resource in its namespace is this scope. Any attempted scope operation fails
# before it can terminate the fixture.
native_product = root / "native-product"
(native_product / "packaging/systemd").mkdir(parents=True)
native_instance = {**instance, "host": {"unit_prefix": "ummanu-head-",
                                       "components": {"dispatcher-production": {"enabled": False}}}}
native_report = SimpleNamespace(**{**vars(report), "instance": native_instance, "host": native_instance["host"]})
(data / "host-managed.json").write_text('{"version":1,"resources":[]}\n')
class NativeInstaller(SystemdUnitInstaller):
    def _run(self, command, label):
        effects.append((command[1], command[-1]))
        if unit in command:
            raise RuntimeError("native installer attempted an effect on its own scope")
        return super()._run(command, label)

for dry in (True, False):
    installer = NativeInstaller(unit_dir=root / "native-units", sudo=False)
    context = upgrade_types.UpgradeContext(instance_path=instance_dir, product_root=native_product,
                                      base_branch="main", dry_run=dry, units=installer, report=native_report)
    result = upgrade_host.step_host(context)
    if result.status == "failed":
        raise RuntimeError(result.detail)
    results.append(result.status)
    if owner.read_bytes() != before:
        raise RuntimeError("native host reconciliation changed its runtime owner")
with mock.patch("ummanu.host_apply.resolve_installed_packaged", return_value=[]):
    _, native_collected, native_diffs = cli.collect_host_inventory(native_report, SimpleNamespace(host_fixture=None))
if (native_collected.errors or unit in native_diffs["units"].unmanaged_on_host
        or unit in native_diffs["units"].missing_on_host):
    raise RuntimeError("native doctor inventory did not preserve its active runtime scope")
with mock.patch.object(status, "resolve_installed_packaged", return_value=[]):
    native_snapshot = status.collect_status(native_report, sprints=False, recovery={"fixture": True})
if (native_snapshot["host"]["inventory_errors"]
        or run_id not in [row["run_id"] for row in native_snapshot["host"]["runtime_scopes"]]):
    raise RuntimeError("native status did not preserve its active runtime scope")

(root / "proof.json").write_text(json.dumps({"unit": unit, "results": results, "effects": effects,
                                             "runtime_scopes": snapshot["host"]["runtime_scopes"],
                                             "pid": os.getpid()}))
# Stay live while the outer test re-reads the proof. Its registered lifecycle
# cleanup settles the scope after this head returns.
deadline = time.monotonic() + 30
while not (root / "finish").exists():
    if time.monotonic() > deadline:
        raise RuntimeError("CI owner did not settle its fixture")
    time.sleep(0.05)
