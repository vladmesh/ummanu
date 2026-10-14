"""`dependencies` and `memory` decide by installed state, not by this run's pull delta (secretary-1743)."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.fakes.upgrade import FakeUnitInstaller
from ummanu import upgrade
from ummanu.backup_policy import FULL_POLICY, should_skip_data_entry
from ummanu.installation.upgrade import (
    checkout as upgrade_checkout,
    dependencies as upgrade_dependencies,
    services as upgrade_services,
    types as upgrade_types,
)
from ummanu.memory import DEFAULT_MODEL

MEMORY_UNIT = "ummanu-memory.service"
PYPROJECT = """[project]
name = "ummanu"

[project.optional-dependencies]
dev = ["ruff==0.16.4"]
typecheck = ["mypy==1.18.2"]
memory = ["mcp==1.28.1", "fastembed==0.8.0", "sqlite-vec==0.1.9", "numpy==NUMPY"]
"""
MEMORY_DISTRIBUTIONS = ("mcp-1.28.1", "fastembed-0.8.0", "sqlite_vec-0.1.9", "numpy-2.4.6")


def memory_unit(model: str | None = "model-a") -> bytes:
    environment = f"Environment=MEMORY_MODEL={model}\n" if model is not None else ""
    return (
        "[Service]\nEnvironment=MEMORY_PORT=8077\n"
        + environment
        + "ExecStart=/x/.venv/bin/ummanu-memory-mcp\n"
    ).encode()


class StateReceiptFixture(unittest.TestCase):
    """A Git product checkout with an editable venv, a data dir and a fake memory unit."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.product = self.root / "product"
        self.data = self.root / "data"
        self.data.mkdir()
        self._write_product()
        self.units = FakeUnitInstaller(present={MEMORY_UNIT: memory_unit()}, active={MEMORY_UNIT})
        self.report = SimpleNamespace(
            host={"unit_prefix": "ummanu-"},
            instance={"host": {"unit_prefix": "ummanu-"}, "data_dir": str(self.data)},
            data_dir=self.data,
            bindings=[],
        )
        self.installs: list[list[str]] = []
        real_run = upgrade_checkout._proc.run

        def run(argv, *args, **kwargs):
            if list(argv[1:4]) == ["-m", "pip", "install"]:
                self.installs.append(list(argv))
                return subprocess.CompletedProcess(argv, 0, "", "")
            return real_run(argv, *args, **kwargs)

        self.enterContext(mock.patch.object(upgrade_checkout._proc, "run", side_effect=run))
        self.probe = self.enterContext(mock.patch.object(upgrade_services, "probe_memory"))

    def _write_product(self) -> None:
        files = {
            "pyproject.toml": PYPROJECT.replace("NUMPY", "2.4.6"),
            "src/ummanu/__init__.py": "",
            "src/ummanu/app.py": "VERSION = 'A'\n",
            "docs/README.md": "a\n",
            ".gitignore": ".venv/\n",
        }
        for relative, text in files.items():
            path = self.product / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        bin_dir = self.product / ".venv" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "python").write_text("", encoding="utf-8")
        ruff = bin_dir / "ruff"
        ruff.write_text("#!/bin/sh\necho 'ruff 0.16.4'\n", encoding="utf-8")
        ruff.chmod(0o755)
        dist_info = self.site_packages() / "ummanu-0.1.0.dist-info"
        dist_info.mkdir(parents=True)
        (dist_info / "direct_url.json").write_text(
            json.dumps({"url": "file:///product", "dir_info": {"editable": True}}), encoding="utf-8"
        )
        self._git("init", "--quiet", "--initial-branch=main")
        self.commit("A")

    def site_packages(self) -> Path:
        return self.product / ".venv" / "lib" / "python3.12" / "site-packages"

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.product), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    def commit(self, message: str) -> None:
        self._git("add", "-A")
        self._git(
            "-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "--quiet", "-m", message
        )

    def move_checkout(self, relative: str, text: str) -> None:
        """A commit this upgrade did not pull: a manual reset, a pull by hand, a dispatcher release."""
        (self.product / relative).write_text(text, encoding="utf-8")
        self.commit(f"move {relative}")

    def context(self, **overrides) -> upgrade_types.UpgradeContext:
        base = upgrade_types.UpgradeContext(
            instance_path=self.root / "instance",
            product_root=self.product,
            base_branch="main",
            dry_run=False,
            units=self.units,
            pull=False,
            report=self.report,
            memory_pack_digest="a" * 64,
        )
        return replace(base, **overrides)

    def dependency_receipt(self) -> Path:
        return self.data / upgrade_dependencies.DEPENDENCY_RECEIPT_RELATIVE

    def memory_receipt(self) -> Path:
        return self.data / upgrade_services.MEMORY_PROCESS_RECEIPT_RELATIVE

    def extras_installed(self) -> str:
        """The extras list of the last `pip install -e <root>[...]`."""
        target = self.installs[-1][-1]
        self.assertTrue(target.startswith(f"{self.product}["), target)
        return target[len(f"{self.product}[") : -1]


class DependencyReceiptTests(StateReceiptFixture):
    def test_first_run_installs_once_writes_the_receipt_and_the_repeat_is_unchanged(self) -> None:
        first = upgrade_dependencies.step_dependencies(self.context())
        second = upgrade_dependencies.step_dependencies(self.context())

        self.assertEqual(first.status, "changed", first.detail)
        self.assertIn("the dependency receipt is missing", first.detail)
        self.assertEqual(len(self.installs), 1)
        self.assertEqual(self.extras_installed(), "dev,memory")
        self.assertEqual(os.stat(self.dependency_receipt()).st_mode & 0o777, 0o600)
        self.assertEqual(second.status, "unchanged", second.detail)
        self.assertRegex(
            second.detail, r"^venv matches checkout \(deps sha256 [0-9a-f]{12}, extras dev,memory\)$"
        )
        self.assertNotIn("no dependency manifest moved", first.detail + second.detail)

    def test_a_memory_pin_bump_outside_upgrade_reinstalls_with_an_empty_pull_delta(self) -> None:
        upgrade_dependencies.step_dependencies(self.context())
        self.move_checkout("pyproject.toml", PYPROJECT.replace("NUMPY", "2.4.7"))
        context = self.context()

        pulled = upgrade_checkout.step_pull(context)
        result = upgrade_dependencies.step_dependencies(context)
        again = upgrade_dependencies.step_dependencies(self.context())

        self.assertEqual((pulled.status, context.changed_paths), ("skipped", ()))
        self.assertEqual(result.status, "changed", result.detail)
        self.assertRegex(result.detail, r"deps sha256 [0-9a-f]{12} -> [0-9a-f]{12}")
        self.assertEqual(len(self.installs), 2)
        self.assertEqual(self.extras_installed(), "dev,memory")
        self.assertTrue(context.code_changed)
        self.assertEqual(again.status, "unchanged", again.detail)

    def test_a_source_only_move_leaves_the_venv_alone(self) -> None:
        upgrade_dependencies.step_dependencies(self.context())
        self.move_checkout("src/ummanu/app.py", "VERSION = 'B'\n")

        result = upgrade_dependencies.step_dependencies(self.context())

        self.assertEqual(result.status, "unchanged", result.detail)
        self.assertEqual(len(self.installs), 1)

    def test_required_extras_come_from_the_installation(self) -> None:
        cases = {
            "memory unit installed": ({MEMORY_UNIT: memory_unit()}, set(), (), ("dev", "memory")),
            "memory unit only active": ({}, {MEMORY_UNIT}, (), ("dev", "memory")),
            "no memory unit": ({}, set(), (), ("dev",)),
            "venv carries memory": ({}, set(), MEMORY_DISTRIBUTIONS, ("dev", "memory")),
            "venv carries part of memory": ({}, set(), MEMORY_DISTRIBUTIONS[:2], ("dev",)),
            "venv carries typecheck": ({}, set(), ("mypy-1.18.2",), ("dev", "typecheck")),
        }
        for name, (present, active, distributions, expected) in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                venv = Path(tmp) / ".venv"
                for distribution in distributions:
                    (venv / "lib" / "python3.12" / "site-packages" / f"{distribution}.dist-info").mkdir(
                        parents=True
                    )
                units = FakeUnitInstaller(present=present, active=active)

                self.assertEqual(upgrade_dependencies.required_extras(self.context(units=units), venv), expected)

    def test_an_extra_the_installation_started_using_reinstalls(self) -> None:
        self.units = FakeUnitInstaller()
        first = upgrade_dependencies.step_dependencies(self.context())
        self.assertEqual(self.extras_installed(), "dev")
        self.units = FakeUnitInstaller(present={MEMORY_UNIT: memory_unit()})

        result = upgrade_dependencies.step_dependencies(self.context())

        self.assertEqual(first.status, "changed")
        self.assertEqual(result.status, "changed", result.detail)
        self.assertIn("extras dev -> dev,memory", result.detail)
        self.assertEqual(self.extras_installed(), "dev,memory")

    def test_dry_run_names_the_reason_and_installs_nothing(self) -> None:
        result = upgrade_dependencies.step_dependencies(self.context(dry_run=True))

        self.assertEqual(result.status, "changed")
        self.assertIn("would reinstall", result.detail)
        self.assertEqual(self.installs, [])
        self.assertFalse(self.dependency_receipt().exists())

    def test_a_failed_install_writes_no_receipt(self) -> None:
        real_run = upgrade_checkout._proc.run.side_effect

        def run(argv, *args, **kwargs):
            if list(argv[1:4]) == ["-m", "pip", "install"]:
                raise subprocess.CalledProcessError(1, argv)
            return real_run(argv, *args, **kwargs)

        with mock.patch.object(upgrade_checkout._proc, "run", side_effect=run):
            result = upgrade_dependencies.step_dependencies(self.context())

        self.assertEqual(result.status, "failed")
        self.assertFalse(self.dependency_receipt().exists())

    def test_hostile_receipts_mean_install_never_an_exception(self) -> None:
        venv = str((self.product / ".venv").resolve())
        digest = upgrade_checkout._git_tracked_digest(self.product, upgrade_types.DEPENDENCY_PATHS)

        def body(**fields: object) -> str:
            payload = {"version": 1, "venv": venv, "dependency_sha256": digest, "extras": ["dev", "memory"]}
            payload.update(fields)
            return json.dumps(payload)

        for name, text in _hostile(body, extras_key="extras").items():
            with self.subTest(name):
                self.installs.clear()
                self._plant(self.dependency_receipt(), text)

                result = upgrade_dependencies.step_dependencies(self.context())

                self.assertEqual(result.status, _EXPECTED.get(name, "changed"), result.detail)
                self.assertEqual(len(self.installs), 1)
        with self.subTest("another venv path"):
            self.installs.clear()
            self._plant(self.dependency_receipt(), body(venv="/elsewhere/.venv"))
            result = upgrade_dependencies.step_dependencies(self.context())
            self.assertIn("names another venv", result.detail)
            self.assertEqual(len(self.installs), 1)
        with self.subTest("the valid control is unchanged"):
            self._plant(self.dependency_receipt(), body())
            self.assertEqual(upgrade_dependencies.step_dependencies(self.context()).status, "unchanged")

    def _plant(self, path: Path, text: str | None) -> None:
        _plant(path, text)


class MemoryReceiptTests(StateReceiptFixture):
    def test_first_run_restarts_once_writes_the_receipt_and_the_repeat_is_unchanged(self) -> None:
        first = upgrade_services.step_memory(self.context())
        second = upgrade_services.step_memory(self.context())

        self.assertEqual(first.status, "changed", first.detail)
        self.assertIn("the memory process receipt is missing", first.detail)
        self.assertEqual(self.units.calls, [("restart", MEMORY_UNIT)])
        self.probe.assert_called_once()
        self.assertEqual(os.stat(self.memory_receipt()).st_mode & 0o777, 0o600)
        self.assertEqual(second.status, "unchanged", second.detail)
        self.assertIn("memory process receipt verified", second.detail)
        self.assertIn("model model-a", second.detail)
        self.assertNotIn("serving the current code", second.detail)

    def test_a_source_commit_outside_upgrade_restarts_the_service_on_no_pull(self) -> None:
        upgrade_services.step_memory(self.context())
        self.move_checkout("src/ummanu/app.py", "VERSION = 'B'\n")
        context = self.context()

        steps = upgrade.run_steps(
            context, steps=(upgrade_checkout.step_pull, upgrade_dependencies.step_dependencies, upgrade_services.step_memory)
        )
        again = upgrade.run_steps(self.context(), steps=(upgrade_dependencies.step_dependencies, upgrade_services.step_memory))

        pulled, dependencies, memory = steps.steps
        self.assertEqual(pulled.status, "skipped")
        # No receipt yet for the venv: the first run of this card installs once as well.
        self.assertEqual(dependencies.status, "changed", dependencies.detail)
        self.assertEqual(memory.status, "changed", memory.detail)
        self.assertRegex(memory.detail, r"product revision [0-9a-f]{12} -> [0-9a-f]{12}")
        self.assertRegex(memory.detail, r"product sha256 [0-9a-f]{12} -> [0-9a-f]{12}")
        self.assertEqual([step.status for step in again.steps], ["unchanged", "unchanged"], again.render())
        self.assertIn("venv matches checkout", again.steps[0].detail)
        self.assertIn("memory process receipt verified", again.steps[1].detail)

    def test_a_restart_outside_upgrade_is_a_different_process_generation(self) -> None:
        upgrade_services.step_memory(self.context())
        self.units.restart(MEMORY_UNIT)
        self.units.calls.clear()

        result = upgrade_services.step_memory(self.context())

        self.assertEqual(result.status, "changed", result.detail)
        self.assertIn("different process generation", result.detail)
        self.assertEqual(self.units.calls, [("restart", MEMORY_UNIT)])

    def test_a_changed_memory_model_restarts_the_service(self) -> None:
        upgrade_services.step_memory(self.context())
        self.units.files[MEMORY_UNIT] = memory_unit("model-b")
        self.units.calls.clear()

        result = upgrade_services.step_memory(self.context())

        self.assertEqual(result.status, "changed", result.detail)
        self.assertIn("memory model model-a -> model-b", result.detail)
        self.assertEqual(self.units.calls, [("restart", MEMORY_UNIT)])
        self.assertEqual(upgrade_services.step_memory(self.context()).status, "unchanged")

    def test_a_unit_without_a_model_binds_the_service_default(self) -> None:
        self.units.files[MEMORY_UNIT] = memory_unit(None)
        upgrade_services.step_memory(self.context())
        self.units.files[MEMORY_UNIT] = memory_unit(DEFAULT_MODEL)

        result = upgrade_services.step_memory(self.context())

        self.assertEqual(result.status, "unchanged", result.detail)
        self.assertIn(f"model {DEFAULT_MODEL}", result.detail)

    def test_a_changed_memory_pack_digest_restarts_the_service(self) -> None:
        upgrade_services.step_memory(self.context())

        result = upgrade_services.step_memory(self.context(memory_pack_digest="b" * 64))

        self.assertEqual(result.status, "changed", result.detail)
        self.assertIn(f"memory pack sha256 {'a' * 12} -> {'b' * 12}", result.detail)

    def test_the_existing_change_flags_are_still_reasons(self) -> None:
        upgrade_services.step_memory(self.context())
        for flag, reason in (
            ("unit_changed", "unit file changed"),
            ("code_changed", "product code or dependencies changed"),
            ("memory_pack_changed", "memory pack export changed"),
        ):
            with self.subTest(flag):
                result = upgrade_services.step_memory(self.context(**{flag: True}))
                self.assertEqual(result.status, "changed")
                self.assertIn(reason, result.detail)

    def test_dry_run_names_the_moved_checkout_and_restarts_nothing(self) -> None:
        upgrade_services.step_memory(self.context())
        self.move_checkout("src/ummanu/app.py", "VERSION = 'B'\n")
        self.units.calls.clear()

        result = upgrade_services.step_memory(self.context(dry_run=True))

        self.assertEqual(result.status, "changed")
        self.assertIn(f"would restart {MEMORY_UNIT}", result.detail)
        self.assertIn("product sha256", result.detail)
        self.assertEqual(self.units.calls, [])

    def test_a_failed_probe_writes_no_receipt(self) -> None:
        self.probe.side_effect = upgrade_services.MemoryProbeError("no allowed read")

        result = upgrade_services.step_memory(self.context())

        self.assertEqual(result.status, "failed")
        self.assertFalse(self.memory_receipt().exists())

    def test_hostile_receipts_mean_restart_never_an_exception(self) -> None:
        upgrade_services.step_memory(self.context())
        valid = json.loads(self.memory_receipt().read_text(encoding="utf-8"))

        def body(**fields: object) -> str:
            payload = json.loads(json.dumps(valid))
            payload.update(fields)
            return json.dumps(payload)

        cases = _hostile(body, extras_key="inputs")
        cases["another unit"] = body(unit="ummanu-other.service")
        cases["huge pid"] = body(process={**valid["process"], "pid": 10**30})
        cases["negative start ticks"] = body(process={**valid["process"], "start_ticks": -1})
        cases["model with a newline"] = body(inputs={**valid["inputs"], "memory_model": "a\nb"})
        cases["missing input"] = body(
            inputs={k: v for k, v in valid["inputs"].items() if k != "memory_model"}
        )
        for name, text in cases.items():
            with self.subTest(name):
                self.units.calls.clear()
                self._plant(text)

                result = upgrade_services.step_memory(self.context())

                self.assertEqual(result.status, _EXPECTED.get(name, "changed"), result.detail)
                self.assertEqual(self.units.calls, [("restart", MEMORY_UNIT)])
        with self.subTest("the valid control is unchanged"):
            self.assertEqual(upgrade_services.step_memory(self.context()).status, "unchanged")

    def _plant(self, text: str | None) -> None:
        _plant(self.memory_receipt(), text)


class ReceiptBackupTests(unittest.TestCase):
    def test_both_receipts_are_excluded_from_backup(self) -> None:
        for relative in (upgrade_dependencies.DEPENDENCY_RECEIPT_RELATIVE, upgrade_services.MEMORY_PROCESS_RECEIPT_RELATIVE):
            with self.subTest(str(relative)):
                self.assertTrue(should_skip_data_entry(relative, policy=FULL_POLICY))


# The work is done for a directory in the receipt's place too, and then replacing it fails the step,
# naming the receipt: an upgrade does not delete a directory it did not create.
_EXPECTED = {"directory": "failed"}


def _hostile(body, *, extras_key: str) -> dict[str, str | None]:
    """Receipts that must each read as "no receipt": missing, broken, mistyped or oversized."""
    return {
        "missing": None,
        "empty": "",
        "not JSON": "{",
        "not UTF-8": "\udcff",
        "a list": "[]",
        "deep nesting": "[" * 100_000,
        "huge int": '{"version": ' + "9" * 5000 + "}",
        "huge int version": body(version=10**30),
        "boolean version": body(version=True),
        "wrong version": body(version=2),
        "string version": body(version="1"),
        "extra key": body(unexpected=1),
        "wrong type": body(**{extras_key: "dev"}),
        "null field": body(**{extras_key: None}),
        "oversized": body(padding="x" * (upgrade_services.process_receipts.RECEIPT_MAX_BYTES + 1)),
        "directory": "<directory>",
        "symlink": "<symlink>",
    }


def _plant(path: Path, text: str | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        path.rmdir()
    if text is None:
        return
    if text == "<directory>":
        path.mkdir()
    elif text == "<symlink>":
        target = path.parent / "target.json"
        target.write_text("{}", encoding="utf-8")
        path.symlink_to(target)
    else:
        path.write_bytes(text.encode("utf-8", "surrogateescape"))


if __name__ == "__main__":
    unittest.main()
