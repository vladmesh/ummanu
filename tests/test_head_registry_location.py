"""Where the head-registry pair lives: `<data>/heads/`, and nowhere else.

`ummanu upgrade` and `recover` write `heads.yaml` and `source.yaml` into the data directory and
commit neither (ummanu-26). Until ummanu-39 every reader fell back to the pair an older upgrade
committed into the live root while `<data>/heads/` was empty (the deploy skew); that fallback is
gone. A live root's own pair is never read, and a missing data pair is an error naming
`ummanu upgrade`.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from tests.head_registry import live_root_pair, write_installed_pair
from ummanu import cli, installation, status, task_commands, upgrade
from ummanu.config import validate_instance
from ummanu.dispatch.host import InstanceCatalog
from ummanu.head_registry import (
    HeadRegistryConfigError,
    canonical_heads,
    generated_pair,
    installed_heads,
    installed_pair,
)
from ummanu.onboarding import OnboardingStorage
from ummanu.runtime import heads

ROLES = ("new_card", "reviewer", "observer", "curator", "retro", "steward")


def canon(head: str) -> str:
    """An installation-owned canon that routes every role to ``head``."""
    roles = "".join(f'{role} = "{head}"\n' for role in ROLES)
    return (
        '[resources.acct]\naccount = "acct"\n\n'
        f'[profiles.{head}]\nresource = "acct"\nadapter = "codex"\nfallback = []\n\n'
        f"[role_defaults]\n{roles}"
    )


def instance_yaml(data_dir: Path) -> str:
    return (
        f"version: 1\nname: skew\ndata_dir: {data_dir}\n"
        "offsite:\n  instance_remote: git@example.invalid:x/y.git\n"
        "host:\n  unit_prefix: ummanu-\n"
    )


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout


class LiveRootPairIgnoredTests(unittest.TestCase):
    """A live root still holding the pair an older upgrade committed, and an empty `<data>/heads/`.

    Rewritten from ummanu-26's `DeploySkewTests`, which pinned the fallback this card removes: the
    readers used to follow the live root's pair and status/doctor named it as `legacy`.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.instance = self.root / "instance"
        self.data_dir = self.root / "data"
        (self.instance / "heads").mkdir(parents=True)
        (self.data_dir / "heads").mkdir(parents=True)
        (self.instance / "instance.yaml").write_text(instance_yaml(self.data_dir), encoding="utf-8")
        self.canon = self.instance / "heads" / "heads.toml"
        self.canon.write_text(canon("live-root-head"), encoding="utf-8")
        snapshot = yaml.safe_dump(
            canonical_heads(upgrade.running_product_root(), self.instance), sort_keys=False
        )
        self.live_root_copy = write_installed_pair(self.instance, snapshot, live_root=True)
        heads._load_registry.cache_clear()
        self.addCleanup(heads._load_registry.cache_clear)

    def upgrade_step(self) -> upgrade.StepResult:
        report = validate_instance(self.instance)
        self.assertTrue(report.ok, report.errors)
        context = upgrade.UpgradeContext(
            instance_path=self.instance,
            product_root=upgrade.running_product_root(),
            base_branch="main",
            dry_run=False,
            units=mock.Mock(),
            report=report,
        )
        return upgrade.step_head_registry(context)

    def readers(self) -> dict[str, object]:
        """What every reader of the pair sees right now: its answer, or its error's text."""

        def read(reader):
            try:
                return reader()
            except Exception as exc:  # noqa: BLE001 - the error text is what is compared
                return f"error: {exc}"

        def runtime():
            with mock.patch.dict(os.environ, {"UMMANU_INSTANCE": str(self.instance)}):
                heads._load_registry.cache_clear()
                return (heads.registry_path(), heads.load_registry().role_default("new_card"))

        report = validate_instance(self.instance)
        record = status._head_registry(self.instance, report.data_dir)
        return {
            "catalog": read(lambda: InstanceCatalog(self.instance).worker_head({})),
            "task": read(lambda: task_commands._load_heads(self.instance)["role_defaults"]["new_card"]),
            "runtime": read(runtime),
            "status": (record["snapshot"], record["error"]),
            "installed": read(lambda: installed_heads(self.instance)["role_defaults"]["new_card"]),
        }

    def test_the_live_root_pair_is_never_read_and_one_upgrade_step_writes_the_data_pair(self):
        data_pair = generated_pair(self.instance)
        self.assertEqual(data_pair.snapshot, self.data_dir / "heads" / "heads.yaml")
        self.assertEqual(installed_pair(self.instance), data_pair)
        self.assertFalse(hasattr(data_pair, "legacy"))

        before = self.readers()

        # No reader answers from the live root's copy; the ones that report an error name the data
        # directory's pair and what generates it.
        for name in ("catalog", "task", "runtime", "installed"):
            with self.subTest(reader=name):
                self.assertIsInstance(before[name], str)
                self.assertNotIn("live-root-head", before[name])
                self.assertIn(str(data_pair.snapshot), before[name])
                self.assertIn("ummanu upgrade", before[name])
        self.assertEqual(before["status"][0], str(data_pair.snapshot))
        self.assertIn("ummanu upgrade", before["status"][1])

        self.canon.write_text(canon("upgraded-head"), encoding="utf-8")
        result = self.upgrade_step()
        after = self.readers()

        self.assertEqual(result.status, "changed", result.detail)
        self.assertTrue(data_pair.snapshot.is_file())
        self.assertTrue(data_pair.source.is_file())
        self.assertEqual(after["catalog"], "upgraded-head")
        self.assertEqual(after["task"], "upgraded-head")
        self.assertEqual(after["runtime"], (data_pair.snapshot, "upgraded-head"))
        self.assertEqual(after["status"], (str(data_pair.snapshot), None))
        self.assertEqual(after["installed"], "upgraded-head")
        # The upgrade neither rewrote nor removed the live root's copy: it is simply not read.
        self.assertIn("live-root-head", self.live_root_copy.read_text(encoding="utf-8"))

    def test_status_and_doctor_name_no_legacy_source(self):
        report = validate_instance(self.instance)
        record = status._head_registry(self.instance, report.data_dir)

        self.assertNotIn("legacy_source", record)
        self.assertFalse(hasattr(cli, "_legacy_head_registry_source"))

    def test_a_data_pair_that_is_present_is_read_even_when_it_is_broken(self):
        broken = generated_pair(self.instance).snapshot
        broken.write_text("profiles: {}\n", encoding="utf-8")

        pair = installed_pair(self.instance)

        self.assertEqual(pair.snapshot, broken)
        with self.assertRaisesRegex(HeadRegistryConfigError, str(broken)):
            installed_heads(self.instance)

    def test_with_no_data_pair_the_error_names_ummanu_upgrade(self):
        live = live_root_pair(self.instance)
        live.snapshot.unlink()
        live.source.unlink()

        pair = installed_pair(self.instance)

        self.assertEqual(pair, generated_pair(self.instance))
        with self.assertRaisesRegex(HeadRegistryConfigError, "ummanu upgrade") as raised:
            installed_heads(self.instance)
        self.assertIn(str(pair.snapshot), str(raised.exception))
        self.assertIn("is missing", str(raised.exception))


class LocatedByDataDirOnlyTests(unittest.TestCase):
    """Generated state is found from `data_dir` alone, never from the rest of `instance.yaml`.

    An unreadable `open_sprint_limit` is reported by `validate_instance` and fails closed to one
    open sprint where it is read; it must not also hide the head registry from a sprint create.
    """

    def test_an_invalid_unrelated_setting_leaves_the_pair_and_onboarding_storage_reachable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance"
            instance.mkdir()
            (instance / "instance.yaml").write_text(
                instance_yaml(root / "data") + "open_sprint_limit: two\n", encoding="utf-8"
            )
            self.assertFalse(validate_instance(instance).ok)
            write_installed_pair(
                instance,
                yaml.safe_dump(
                    {
                        "resources": {"acct": {"account": "acct"}},
                        "profiles": {"only-head": {"resource": "acct", "adapter": "codex", "fallback": []}},
                        "role_defaults": {"new_card": "only-head"},
                    }
                ),
            )

            self.assertEqual(installed_pair(instance).snapshot, root / "data" / "heads" / "heads.yaml")
            self.assertEqual(installed_heads(instance)["role_defaults"]["new_card"], "only-head")
            self.assertEqual(OnboardingStorage.for_instance(instance).root, root / "data" / "onboarding")

    def test_an_instance_naming_no_data_directory_still_fails_by_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / "instance.yaml").write_text("open_sprint_limit: 2\n", encoding="utf-8")

            with self.assertRaisesRegex(HeadRegistryConfigError, "data_dir must be a non-empty string"):
                installed_pair(instance)


class RecoverRegenerationTests(unittest.TestCase):
    """Recover step 7 regenerates the pair into `<data>/heads/`, whatever the checkpoint holds."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.instance = self.root / "instance"
        self.data_dir = self.root / "data"
        (self.instance / "heads").mkdir(parents=True)
        self.data_dir.mkdir()
        (self.instance / "instance.yaml").write_text(instance_yaml(self.data_dir), encoding="utf-8")
        (self.instance / "heads" / "heads.toml").write_text(canon("recovered-head"), encoding="utf-8")

    def materialize(self) -> None:
        """The recover materializer, with only its head-registry step running."""

        def head_registry_only(context, steps=installation.STEPS):
            self.assertIn(upgrade.step_head_registry, steps)
            return upgrade.run_steps(context, steps=(upgrade.step_head_registry,))

        with (
            mock.patch.object(installation, "run_steps", side_effect=head_registry_only),
            mock.patch.object(installation, "check_product_runtime"),
        ):
            result = installation.materialize_host(self.instance, upgrade.running_product_root())
        self.assertTrue(result.ok, result.render())

    def assert_regenerated(self) -> None:
        pair = installed_pair(self.instance)
        self.assertEqual(pair, generated_pair(self.instance))
        self.assertEqual(pair.snapshot.parent, self.data_dir / "heads")
        self.assertEqual(installed_heads(self.instance)["role_defaults"]["new_card"], "recovered-head")

    def test_from_a_legacy_checkpoint_that_tracks_the_pair(self):
        stale = "# a pair an older upgrade committed\nresources: {}\n"
        (self.instance / "heads" / "heads.yaml").write_text(stale, encoding="utf-8")
        (self.instance / "heads" / "source.yaml").write_text("revision: old\n", encoding="utf-8")
        git(self.instance, "init", "--quiet", "--initial-branch", "main")
        git(self.instance, "add", ".")
        git(
            self.instance,
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "legacy checkpoint",
        )
        head = git(self.instance, "rev-parse", "HEAD")

        self.materialize()

        self.assert_regenerated()
        # The tracked legacy copy is left exactly as checked out, and nothing was committed.
        self.assertEqual((self.instance / "heads" / "heads.yaml").read_text(encoding="utf-8"), stale)
        self.assertEqual(git(self.instance, "status", "--porcelain", "--untracked-files=all"), "")
        self.assertEqual(git(self.instance, "rev-parse", "HEAD"), head)

    def test_from_an_exporter_shaped_tree_that_lacks_it(self):
        (self.instance / "snapshot-manifest.json").write_text("{}\n", encoding="utf-8")
        self.assertFalse((self.instance / "heads" / "heads.yaml").exists())

        self.materialize()

        self.assert_regenerated()
        self.assertFalse((self.instance / "heads" / "heads.yaml").exists())
        self.assertFalse((self.instance / ".git").exists())


if __name__ == "__main__":
    unittest.main()
