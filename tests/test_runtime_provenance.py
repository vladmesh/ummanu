"""Regression coverage for task-environment isolation and production import fences."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ummanu.automations.agents.steward import signals as steward_signals
from ummanu.automations.runtime import health, production_telemetry
from ummanu.cli import main as ummanu_main
from ummanu.dispatch.production import record_tick_telemetry
from ummanu.dispatch.runtime_provenance import ProductionRuntime
from ummanu.head_registry import generated_pair
from ummanu.runtime.role_env import runtime_env
from ummanu.runtime.state import AgentState

PREFLIGHT = Path(__file__).parents[1] / "src" / "ummanu" / "dispatch" / "runtime_preflight.py"


def _fixture(root: Path, marker: str) -> None:
    package = root / "ummanu"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(f"MARKER = {marker!r}\n", encoding="utf-8")
    (package / "dispatcher_tick.py").write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "from ummanu import MARKER\n"
        "Path(sys.argv[1]).write_text(json.dumps({'action': 'normal', 'marker': MARKER}) + '\\n')\n",
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        "[build-system]\nrequires = []\nbuild-backend = 'fixture_backend'\nbackend-path = ['.']\n",
        encoding="utf-8",
    )
    (root / "fixture_backend.py").write_text(
        "from pathlib import Path\n"
        "from zipfile import ZIP_DEFLATED, ZipFile\n\n"
        "def build_editable(wheel_directory, config_settings=None, metadata_directory=None):\n"
        "    name = 'ummanu-0.1-py3-none-any.whl'\n"
        "    dist = 'ummanu-0.1.dist-info/'\n"
        "    with ZipFile(Path(wheel_directory) / name, 'w', ZIP_DEFLATED) as wheel:\n"
        "        wheel.writestr('__editable__.ummanu-0.1.pth', str(Path.cwd()) + '\\n')\n"
        "        wheel.writestr(dist + 'METADATA', "
        "'Metadata-Version: 2.1\\nName: ummanu\\nVersion: 0.1\\nProvides-Extra: dev\\n')\n"
        "        wheel.writestr(dist + 'WHEEL', "
        "'Wheel-Version: 1.0\\nGenerator: fixture\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n')\n"
        "        wheel.writestr(dist + 'RECORD', '')\n"
        "    return name\n",
        encoding="utf-8",
    )


def _venv(root: Path) -> Path:
    subprocess.run(
        [os.sys.executable, "-m", "venv", str(root)],
        check=True,
        capture_output=True,
        text=True,
    )
    return root / "bin" / "python3"


def _install(python: Path, checkout: Path) -> None:
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "-e",
            ".[dev]",
        ],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    )


def _instance(root: Path, product: Path, data_dir: Path) -> Path:
    instance = root / "instance"
    instance.mkdir(parents=True)
    (instance / "instance.yaml").write_text(
        "version: 1\n"
        "name: runtime-provenance-fixture\n"
        f"data_dir: {data_dir}\n"
        "offsite:\n"
        "  instance_remote: https://example.invalid/instance.git\n",
        encoding="utf-8",
    )
    # Doctor uses only this installed pin. That makes the fixture prove it never falls back to a
    # developer's live checkout when it diagnoses an editable installation.
    pin = generated_pair(instance).source
    pin.parent.mkdir(parents=True, exist_ok=True)
    pin.write_text(f"product_root: {product}\n", encoding="utf-8")
    return instance


def _preflight(
    python: Path,
    product: Path,
    data_dir: Path,
    *command: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            str(python),
            "-I",
            str(PREFLIGHT),
            "--product-root",
            str(product),
            "--interpreter",
            str(python),
            "--workspaces-root",
            str(product.parent / "workspaces"),
            "--state-path",
            str(data_dir / "dispatcher" / "production-state.json"),
            "--",
            *command,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


class ProductionRuntimeTests(unittest.TestCase):
    def test_offline_doctor_does_not_probe_an_unstarted_production_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "ummanu"
            data_dir = root / "data"
            _fixture(product, "production")
            instance = _instance(root, product, data_dir)
            output = io.StringIO()
            with (
                mock.patch.object(ProductionRuntime, "probe", side_effect=AssertionError("offline probe")),
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(ummanu_main(["doctor", "--offline", "--instance", str(instance)]), 0)

    def test_live_doctor_reports_an_unstarted_production_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "ummanu"
            data_dir = root / "data"
            _fixture(product, "production")
            instance = _instance(root, product, data_dir)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    ummanu_main(["doctor", "--dry-run", "--json", "--instance", str(instance)]), 1
                )
            findings = json.loads(output.getvalue())["findings"]
            finding = next(item for item in findings if item["code"] == "production_runtime_provenance")
            self.assertEqual(finding["classification"], "interpreter_unavailable")
            self.assertIn("pip install --no-deps -e", finding["repair"])

    def test_missing_interpreter_has_its_own_classification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            observed = ProductionRuntime(
                str(root / "missing-python"),
                str(root / "ummanu"),
                workspaces_root=str(root / "workspaces"),
            ).probe()
        self.assertEqual(observed.classification, "interpreter_unavailable")

    def test_missing_import_has_its_own_classification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            python = _venv(root / ".venv")
            observed = ProductionRuntime(
                str(python), str(root / "ummanu"), workspaces_root=str(root / "workspaces")
            ).probe()
        self.assertEqual(observed.classification, "missing_import")

    def test_representative_worker_install_cannot_retarget_production(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            production = root / "ummanu"
            task = root / "workspaces" / "ummanu" / "task-1"
            _fixture(production, "production")
            _fixture(task, "candidate")
            production_python = _venv(production / ".venv")
            _install(production_python, production)
            _venv(task / ".ummanu-task-env" / "venv")

            env = runtime_env(
                "worker",
                base_env={"PATH": os.environ["PATH"], "PYTHONPATH": str(production)},
                env_file=root / "absent.env",
                workspace=task,
            )
            resolved = subprocess.run(
                ["/bin/sh", "-c", "command -v python3; command -v python; command -v pip"],
                env=env,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
            task_bin = task / ".ummanu-task-env" / "venv" / "bin"
            self.assertTrue(all(str(task_bin) in entry for entry in resolved))
            self.assertNotIn("PYTHONPATH", env)

            # This is the ordinary incident command shape. The launch environment, not a rewritten
            # pip command, is what makes its target the disposable task environment.
            subprocess.run(
                ["python3", "-m", "pip", "install", "-e", ".[dev]"],
                cwd=task,
                env={**env, "PIP_NO_INDEX": "1"},
                check=True,
                capture_output=True,
                text=True,
            )
            shutil.rmtree(task)

            runtime = ProductionRuntime(
                str(production_python),
                str(production),
                workspaces_root=str(root / "workspaces"),
            )
            observed = runtime.probe()
            self.assertEqual(observed.classification, "valid", observed.as_dict())
            tick_record = root / "dispatcher-tick.json"
            subprocess.run(
                [
                    str(production_python),
                    "-I",
                    "-m",
                    "ummanu.dispatcher_tick",
                    str(tick_record),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                json.loads(tick_record.read_text(encoding="utf-8")),
                {"action": "normal", "marker": "production"},
            )

    def test_workspace_metadata_is_refused_before_and_after_checkout_vanishes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            production = root / "ummanu"
            task = root / "workspaces" / "ummanu" / "task-2"
            _fixture(production, "production")
            _fixture(task, "candidate")
            python = _venv(production / ".venv")
            _install(python, task)
            site_packages = next((production / ".venv" / "lib").glob("python*/site-packages"))
            dist_info = site_packages / "ummanu-0.1.dist-info"
            dist_info.mkdir(exist_ok=True)
            (dist_info / "direct_url.json").write_text(
                json.dumps({"url": task.as_uri(), "dir_info": {"editable": True}}),
                encoding="utf-8",
            )
            runtime = ProductionRuntime(
                str(python), str(production), workspaces_root=str(root / "workspaces")
            )

            present = runtime.probe()
            self.assertEqual(present.classification, "workspace_targeted_editable")
            self.assertEqual({entry[0] for entry in present.metadata_targets}, {"pth", "direct_url"})
            shutil.rmtree(task)
            self.assertEqual(runtime.probe().classification, "workspace_targeted_editable")

    def test_executable_editable_finder_is_refused_without_direct_url_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            production = root / "ummanu"
            task = root / "workspaces" / "ummanu" / "task-finder"
            _fixture(production, "production")
            _fixture(task, "candidate")
            python = _venv(production / ".venv")
            _install(python, production)
            site_packages = next((production / ".venv" / "lib").glob("python*/site-packages"))
            for direct in site_packages.glob("ummanu-*.dist-info/direct_url.json"):
                direct.unlink()
            finder_name = "__editable___ummanu_finder"
            (site_packages / f"{finder_name}.py").write_text(
                f"MAPPING = {{'ummanu': {str(task / 'ummanu')!r}}}\n", encoding="utf-8"
            )
            (site_packages / "__editable__.ummanu.pth").write_text(
                f"import {finder_name}; {finder_name}.install()\n", encoding="utf-8"
            )

            observed = ProductionRuntime(
                str(python), str(production), workspaces_root=str(root / "workspaces")
            ).probe()

            self.assertEqual(observed.classification, "workspace_targeted_editable")
            self.assertIn("pth_finder", {entry[0] for entry in observed.metadata_targets})

    def test_an_editable_install_in_the_git_workspaces_root_is_workspace_targeted(self) -> None:
        """secretary-1710: `<data_dir>/workspaces` holds card checkouts too, not only the Orca root."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            production = root / "ummanu"
            data_dir = root / "ummanu-data"
            task = data_dir / "workspaces" / "ummanu" / "ummanu-1710"
            _fixture(production, "production")
            _fixture(task, "candidate")
            python = _venv(production / ".venv")
            _install(python, production)
            site_packages = next((production / ".venv" / "lib").glob("python*/site-packages"))
            for direct in site_packages.glob("ummanu-*.dist-info/direct_url.json"):
                direct.unlink()
            finder_name = "__editable___ummanu_finder"
            (site_packages / f"{finder_name}.py").write_text(
                f"MAPPING = {{'ummanu': {str(task / 'ummanu')!r}}}\n", encoding="utf-8"
            )
            (site_packages / "__editable__.ummanu.pth").write_text(
                f"import {finder_name}; {finder_name}.install()\n", encoding="utf-8"
            )
            orca_root = root / "orca" / "workspaces"

            observed = ProductionRuntime(
                str(python),
                str(production),
                workspaces_root=str(orca_root),
                git_workspaces_root=str(data_dir / "workspaces"),
            ).probe()
            self.assertEqual(observed.classification, "workspace_targeted_editable", observed.as_dict())

            # The packaged unit names only `--data-dir`; the fence derives the git root from it.
            with mock.patch.dict(os.environ):
                os.environ.pop("UMMANU_DATA_DIR", None)
                fenced = subprocess.run(
                    [
                        str(python),
                        "-I",
                        str(PREFLIGHT),
                        "--product-root",
                        str(production),
                        "--interpreter",
                        str(python),
                        "--workspaces-root",
                        str(orca_root),
                        "--data-dir",
                        str(data_dir),
                        "--json",
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )
            self.assertEqual(json.loads(fenced.stdout)["classification"], "workspace_targeted_editable")
            self.assertNotEqual(fenced.returncode, 0)

    def test_symlinks_are_normalized_without_lexical_prefix_confusion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            production = root / "ummanu"
            linked_production = root / "registered-ummanu"
            similarly_named = root / "ummanu-old"
            workspaces = root / "real-workspaces"
            linked_workspaces = root / "workspaces"
            _fixture(production, "production")
            linked_production.symlink_to(production, target_is_directory=True)
            _fixture(similarly_named, "old")
            workspaces.mkdir()
            linked_workspaces.symlink_to(workspaces, target_is_directory=True)
            python = _venv(production / ".venv")
            _install(python, production)
            valid = ProductionRuntime(
                str(python), str(linked_production), workspaces_root=str(linked_workspaces)
            ).probe()
            self.assertEqual(valid.classification, "valid", valid.as_dict())

            wrong = ProductionRuntime(
                str(python), str(similarly_named), workspaces_root=str(linked_workspaces)
            ).probe()
            self.assertEqual(wrong.classification, "wrong_root")

    def test_preflight_refusal_diagnosis_deduplication_and_explicit_recovery_are_all_fixture_bound(self) -> None:
        """The 2026-09 failure sequence, before any candidate package code can execute."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            production = root / "ummanu"
            workspace = root / "workspaces" / "ummanu" / "task-77"
            data_dir = root / "data"
            _fixture(production, "production")
            _fixture(workspace, "candidate")
            # If the fence tried to verify provenance by importing the candidate, the test would
            # fail before it could leave the diagnostic a broken service needs.
            (workspace / "ummanu" / "__init__.py").write_text(
                "raise RuntimeError('candidate package was imported')\n", encoding="utf-8"
            )
            python = _venv(production / ".venv")
            _install(python, workspace)
            instance = _instance(root, production, data_dir)

            first = _preflight(python, production, data_dir, "/bin/false")
            self.assertEqual(first.returncode, 78, first.stderr)
            state_path = data_dir / "dispatcher" / "production-state.json"
            first_state = json.loads(state_path.read_text(encoding="utf-8"))
            refusal = first_state["runtime_provenance"]
            observed = refusal["observation"]
            self.assertEqual(refusal["status"], "refused")
            self.assertEqual(observed["classification"], "workspace_targeted_editable")
            self.assertEqual(observed["offending_target"], str(workspace.resolve()))
            self.assertTrue(observed["metadata_source"].endswith(".pth"))
            self.assertEqual(first_state["tick_telemetry"]["incident_total"], 1)

            doctor_text = io.StringIO()
            with (
                mock.patch.object(ProductionRuntime, "probe", side_effect=AssertionError("offline probe")),
                contextlib.redirect_stdout(doctor_text),
            ):
                self.assertEqual(ummanu_main(["doctor", "--offline", "--instance", str(instance)]), 1)
            self.assertIn("production runtime provenance refused", doctor_text.getvalue())
            self.assertIn(str(workspace.resolve()), doctor_text.getvalue())
            self.assertIn("pip install --no-deps -e", doctor_text.getvalue())
            doctor_json = io.StringIO()
            with contextlib.redirect_stdout(doctor_json):
                self.assertEqual(ummanu_main(["doctor", "--offline", "--json", "--instance", str(instance)]), 1)
            findings = json.loads(doctor_json.getvalue())["findings"]
            finding = next(item for item in findings if item["code"] == "production_runtime_provenance")
            self.assertEqual(finding["offending_target"], str(workspace.resolve()))
            self.assertTrue(finding["metadata_source"].endswith(".pth"))

            shutil.rmtree(workspace)
            vanished = _preflight(python, production, data_dir, "/bin/false")
            self.assertEqual(vanished.returncode, 78, vanished.stderr)
            vanished_state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                vanished_state["runtime_provenance"]["observation"]["offending_target"], str(workspace.resolve())
            )
            self.assertEqual(vanished_state["tick_telemetry"]["incident_total"], 1)
            self.assertEqual(vanished_state["tick_telemetry"]["incident"]["unhealthy_ticks"], 2)

            with mock.patch.dict(os.environ, {"UMMANU_PRODUCTION_STATE": str(state_path)}):
                problems, _detail = health._pipeline_status()
                self.assertTrue(any("workspace_targeted_editable" in problem for problem in problems))
                telemetry = production_telemetry.read()
                mark = {
                    **steward_signals._empty_watermark(),
                    "pipeline_incident_total": 0,
                    "pipeline_recovery_total": 0,
                    "pipeline_telemetry_generation": telemetry.generation,
                }
                steward_state = AgentState("steward", state_dir=root / "steward")
                with mock.patch.object(steward_signals, "STATE", steward_state):
                    hits, pending = steward_signals._pipeline_tick_signals(mark)
                    self.assertEqual([hit["event"] for hit in hits], ["pipeline-tick-unhealthy"])
                    self.assertIn("workspace_targeted_editable", hits[0]["cause"])

            # This is the one supported repair. It changes only the disposable fixture venv, never
            # a real installation or metadata file by hand.
            subprocess.run(
                [str(python), "-m", "pip", "install", "--no-index", "--no-deps", "-e", str(production)],
                check=True,
                capture_output=True,
                text=True,
            )
            tick_record = root / "representative-tick.json"
            recovered = _preflight(
                python,
                production,
                data_dir,
                str(python),
                "-I",
                "-m",
                "ummanu.dispatcher_tick",
                str(tick_record),
            )
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertEqual(
                json.loads(tick_record.read_text(encoding="utf-8")),
                {"action": "normal", "marker": "production"},
            )
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["runtime_provenance"]["status"], "valid")
            record_tick_telemetry(payload, {"status": "ok", "step": "production-tick"})
            state_path.write_text(json.dumps(payload), encoding="utf-8")

            with mock.patch.dict(os.environ, {"UMMANU_PRODUCTION_STATE": str(state_path)}):
                self.assertEqual(health._pipeline_status()[0], [])
                with mock.patch.object(steward_signals, "STATE", steward_state):
                    hits, _pending = steward_signals._pipeline_tick_signals(dict(mark, **pending))
            self.assertEqual([hit["event"] for hit in hits], ["pipeline-tick-recovered"])


if __name__ == "__main__":
    unittest.main()
