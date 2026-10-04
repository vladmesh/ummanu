from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.retired_board import STALE_FILE
from ummanu import bootstrap as bootstrap_module
from ummanu.bootstrap import (
    BOOTSTRAP_STAMP,
    BootstrapError,
    _host_supported,
    _install_platform,
    bootstrap,
)
from ummanu.installation import SnapshotCheckout


class BootstrapTests(unittest.TestCase):
    # secretary-756: the four scenarios formerly here (idempotent ownership, refusing an
    # unowned matching unit, starting a foreign/legacy-CLI Orca ahead of ownership removal,
    # and a missing-executable error preceding any unit write) all called `_start_orca_service`,
    # which bootstrap no longer defines. Orca is host-owned and external (secretary-739/755):
    # bootstrap never installs, starts, or owns a `ummanu-orca.service` unit, so none of
    # these scenarios has a current-contract equivalent. Deleted rather than rewritten.

    def test_platform_installs_docker_and_distribution_compose_and_nothing_of_orca(self) -> None:
        with (
            mock.patch("ummanu.bootstrap.os.geteuid", return_value=0),
            mock.patch("ummanu.bootstrap.shutil.which", side_effect=lambda name: None),
            mock.patch("ummanu.bootstrap._docker_compose_available", return_value=False),
            mock.patch("ummanu.bootstrap._compose_package", return_value="docker-compose-v2"),
            mock.patch("ummanu.bootstrap._ensure_docker_ready"),
            mock.patch("ummanu.bootstrap._run") as run,
        ):
            _install_platform(dry_run=False)

        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(
            commands,
            [
                ["apt-get", "update"],
                ["apt-get", "install", "--yes", "docker.io", "docker-compose-v2"],
            ],
        )
        # A20 step 9 (secretary-1726): no Orca AppImage, no xvfb, no Electron runtime packages.
        self.assertFalse(hasattr(bootstrap_module, "_install_orca"))
        self.assertFalse(hasattr(bootstrap_module, "ORCA_APPIMAGE_URL"))

    def test_platform_with_docker_present_installs_nothing(self) -> None:
        with (
            mock.patch("ummanu.bootstrap.os.geteuid", return_value=0),
            mock.patch("ummanu.bootstrap.shutil.which", return_value="/usr/bin/docker"),
            mock.patch("ummanu.bootstrap._docker_compose_available", return_value=True),
            mock.patch("ummanu.bootstrap._ensure_docker_ready") as ready,
            mock.patch("ummanu.bootstrap._run") as run,
        ):
            _install_platform(dry_run=False, runtime_user="existing-dedicated-user")

        run.assert_not_called()
        ready.assert_called_once_with()

    def test_host_contract_accepts_only_ubuntu_2404(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary) / "os-release"
            release.write_text('ID=ubuntu\nVERSION_ID="24.04"\n', encoding="utf-8")
            _host_supported(release)
            release.write_text('ID=ubuntu\nVERSION_ID="22.04"\n', encoding="utf-8")
            with self.assertRaisesRegex(BootstrapError, "Ubuntu 24.04 only"):
                _host_supported(release)
            release.write_text('ID=debian\nVERSION_ID="12"\n', encoding="utf-8")
            with self.assertRaisesRegex(BootstrapError, "Ubuntu 24.04 only"):
                _host_supported(release)

    def _clone(self, _remote: str, directory: Path, **_kwargs: object) -> str:
        directory.mkdir()
        subprocess.run(["git", "init", "--quiet", str(directory)], check=True)
        subprocess.run(["git", "-C", str(directory), "config", "user.name", "Test"], check=True)
        subprocess.run(
            ["git", "-C", str(directory), "config", "user.email", "test@example.invalid"], check=True
        )
        (directory / "instance.yaml").write_text(
            "version: 1\nname: bootstrap\ndata_dir: "
            + str(directory.parent / "data")
            + "\noffsite:\n  instance_remote: git@example.invalid:bootstrap/instance\n"
            + "host:\n  unit_prefix: ummanu-\n",
            encoding="utf-8",
        )
        return "cloned private instance remote"

    def _bootstrap(self, target: Path, *, dry_run: bool = False) -> tuple[int, mock.Mock]:
        """Run bootstrap with the host edges stubbed, recording the board-store steps in order."""
        args = SimpleNamespace(
            instance_dir=str(target),
            instance_remote="remote",
            installation_user="dev",
            dry_run=dry_run,
        )
        steps = mock.Mock()
        clone = mock.Mock(
            side_effect=lambda remote, directory, **kwargs: (
                "reused checkpoint checkout" if directory.exists() else self._clone(remote, directory)
            )
        )
        refuse_board_runtime = AssertionError("bootstrap ran a board runtime command")
        with (
            mock.patch("ummanu.bootstrap.os.geteuid", return_value=0),
            mock.patch("ummanu.bootstrap._host_supported"),
            mock.patch("ummanu.bootstrap._ensure_installation_user"),
            # A legacy remote: the shape decision finds no snapshot manifest at its tip.
            mock.patch("ummanu.bootstrap._snapshot_checkout", return_value=None),
            mock.patch("ummanu.bootstrap._clone_or_reuse", clone),
            mock.patch("ummanu.bootstrap._install_platform", steps.install_platform),
            mock.patch("ummanu.bootstrap._set_installation_owner", steps.set_owner),
            mock.patch("ummanu.bootstrap.provision_board_store", steps.provision),
            mock.patch("ummanu.bootstrap.migrate_instance", steps.migrate),
            mock.patch("ummanu.bootstrap.verify_board_store_roles", steps.verify),
            mock.patch("ummanu.bootstrap._run", side_effect=refuse_board_runtime),
            mock.patch("builtins.print"),
        ):
            code = bootstrap(args)
        return code, steps

    def test_bootstrap_provisions_migrates_and_verifies_the_store_with_no_retired_board_step(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "instance"

            code, steps = self._bootstrap(target)

            self.assertEqual(code, 0)
            # The whole board-side sequence, in order: the store is provisioned, migrated and
            # verified, and no other board is started, waited for or shaped.
            self.assertEqual(
                steps.mock_calls,
                [
                    # The cloned instance has a unit prefix, so its web front wants Caddy.
                    mock.call.install_platform(dry_run=False, runtime_user="dev", web_front=True),
                    mock.call.provision(target, allow_create=True),
                    mock.call.migrate(target),
                    mock.call.verify(target),
                    # The handoff comes after provisioning, so it covers `board-store.env`.
                    mock.call.set_owner(target, "dev"),
                ],
            )
            self.assertFalse((target / STALE_FILE).exists())
            gitignore = target / ".gitignore"
            self.assertNotIn(
                STALE_FILE, gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
            )
            self.assertTrue((target / BOOTSTRAP_STAMP).is_file())
            exclude = (target / ".git" / "info" / "exclude").read_text(encoding="utf-8")
            self.assertIn(f"/{BOOTSTRAP_STAMP}", exclude)
            self.assertIn("/runtime.env", exclude)
            for removed in ("ensure_pipeline_board", "migrate_assessment_column", "_compose_file"):
                self.assertFalse(hasattr(bootstrap_module, removed), removed)

    def test_a_snapshot_remote_is_laid_out_by_the_recovery_clone_step_and_gets_no_git_exclude(self) -> None:
        """The snapshot branch: `_snapshot_checkout` lays the live root out, bootstrap adds its stamp
        and the store, and hands over the live root and the data directory; no `.git` is written."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target, data, scratch = root / "instance", root / "data", root / ".instance.snapshot-x"

            def lay_out(remote: str, directory: Path, **kwargs: object) -> SnapshotCheckout:
                self.assertEqual(
                    (remote, kwargs),
                    ("remote", {"dry_run": False, "bootstrap_credential": None, "installation_user": "dev"}),
                )
                directory.mkdir()
                (directory / "instance.yaml").write_text("version: 1\n", encoding="utf-8")
                scratch.mkdir()
                return SnapshotCheckout(
                    "tip", data / "backup" / "instance.git", scratch, scratch, True, "", data
                )

            args = SimpleNamespace(
                instance_dir=str(target), instance_remote="remote", installation_user="dev", dry_run=False
            )
            steps = mock.Mock()
            with (
                mock.patch("ummanu.bootstrap.os.geteuid", return_value=0),
                mock.patch("ummanu.bootstrap._host_supported"),
                mock.patch("ummanu.bootstrap._ensure_installation_user"),
                mock.patch("ummanu.bootstrap._snapshot_checkout", side_effect=lay_out),
                mock.patch("ummanu.bootstrap._clone_or_reuse", steps.clone),
                mock.patch("ummanu.bootstrap._install_platform", steps.install_platform),
                mock.patch("ummanu.bootstrap._set_installation_owner", steps.set_owner),
                mock.patch("ummanu.bootstrap.provision_board_store", steps.provision),
                mock.patch("ummanu.bootstrap.migrate_instance", steps.migrate),
                mock.patch("ummanu.bootstrap.verify_board_store_roles", steps.verify),
                mock.patch("builtins.print"),
            ):
                self.assertEqual(bootstrap(args), 0)

            self.assertEqual(
                steps.mock_calls,
                [
                    # `version: 1` alone declares no unit prefix, so no web-front unit.
                    mock.call.install_platform(dry_run=False, runtime_user="dev", web_front=False),
                    mock.call.provision(target, allow_create=True),
                    mock.call.migrate(target),
                    mock.call.verify(target),
                    mock.call.set_owner(target, "dev"),
                    mock.call.set_owner(data, "dev"),
                ],
            )
            self.assertTrue((target / BOOTSTRAP_STAMP).is_file())
            self.assertFalse((target / ".git").exists())
            self.assertFalse(scratch.exists())

    def test_a_fresh_bootstrap_writes_no_runtime_file(self) -> None:
        """There is one board backend, so bootstrap has nothing to record in `runtime.env`."""
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "instance"

            code, _steps = self._bootstrap(target)

            self.assertEqual(code, 0)
            self.assertFalse((target / "runtime.env").exists())

    def test_a_rerun_leaves_an_existing_runtime_file_exactly_as_it_is(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "instance"
            self.assertEqual(self._bootstrap(target)[0], 0)
            runtime = target / "runtime.env"
            body = "# operator note\nEXAMPLE_TOKEN=kept\n"
            runtime.write_text(body, encoding="utf-8")

            self.assertEqual(self._bootstrap(target)[0], 0)

            self.assertEqual(runtime.read_text(encoding="utf-8"), body)

    def test_a_preview_writes_no_runtime_file_and_touches_no_store(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "instance"

            code, steps = self._bootstrap(target, dry_run=True)

            self.assertEqual(code, 0)
            self.assertEqual(steps.mock_calls, [])
            self.assertFalse((target / "runtime.env").exists())

    def test_the_real_provision_leaves_board_store_env_to_the_installation_user_at_0600(self) -> None:
        """Real `provision` materializes `board-store.env` as root; bootstrap then hands it over.

        Only the Docker edges of `provision` are stood in for. The test cannot switch users, so
        it runs `_set_installation_owner` for real with root's view of the host and records the
        uid and gid each `chown` receives, and when, relative to the store steps.
        """
        from ummanu.board import provision as provision_module
        from ummanu.board import store

        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "instance"
            compose = Path(temporary) / "opt" / "postgres-compose.yml"
            events: list[tuple[object, ...]] = []
            account = SimpleNamespace(pw_uid=4242, pw_gid=4343)

            def chown(path: object, uid: int, gid: int, *, follow_symlinks: bool = True) -> None:
                events.append(("chown", Path(str(path)), uid, gid))

            def step(name: str):
                def record(instance: Path, *args: object, **kwargs: object) -> None:
                    events.append((name, Path(instance)))
                    # The store file has to exist, private, before anything reads it.
                    store_file = store.store_path(instance)
                    self.assertEqual(store_file.stat().st_mode & 0o777, 0o600)

                return record

            args = SimpleNamespace(
                instance_dir=str(target), instance_remote="remote", installation_user="dev", dry_run=False
            )
            kwdefaults = dict(provision_module.provision.__kwdefaults__ or {})
            kwdefaults["compose_path"] = compose
            with (
                mock.patch("ummanu.bootstrap.os.geteuid", return_value=0),
                mock.patch("ummanu.bootstrap._host_supported"),
                mock.patch("ummanu.bootstrap._ensure_installation_user"),
                mock.patch("ummanu.bootstrap._snapshot_checkout", return_value=None),
                mock.patch("ummanu.bootstrap._clone_or_reuse", side_effect=self._clone),
                mock.patch("ummanu.bootstrap._install_platform"),
                # `provision` itself runs; only Docker is answered for it.
                mock.patch.object(provision_module.provision, "__kwdefaults__", kwdefaults),
                mock.patch("ummanu.board.provision._exists", return_value=False),
                mock.patch("ummanu.board.provision._run", return_value="container-id"),
                mock.patch("ummanu.board.provision._inspect_container"),
                mock.patch("ummanu.board.provision._wait_ready"),
                mock.patch("ummanu.bootstrap.migrate_instance", side_effect=step("migrate")),
                mock.patch("ummanu.bootstrap.verify_board_store_roles", side_effect=step("verify")),
                # `_set_installation_owner` runs for real, as root would, against a stand-in account.
                mock.patch("ummanu.upgrade.os.geteuid", return_value=0),
                mock.patch("ummanu.upgrade.pwd.getpwnam", return_value=account),
                mock.patch("ummanu.upgrade.os.chown", side_effect=chown),
                mock.patch("builtins.print"),
            ):
                self.assertEqual(bootstrap(args), 0)

            store_file = store.store_path(target)
            self.assertEqual(store_file.stat().st_mode & 0o777, 0o600)
            self.assertIn("UMMANU_DB_APP_PASSWORD=", store_file.read_text(encoding="utf-8"))
            names = [event[0] for event in events]
            handed = [event for event in events if event[0] == "chown" and event[1] == store_file]
            self.assertEqual(handed, [("chown", store_file, 4242, 4343)])
            # In order: the store steps first, then the handoff of the file they needed.
            self.assertLess(names.index("verify"), events.index(handed[0]))
            self.assertIn(("chown", target / BOOTSTRAP_STAMP, 4242, 4343), events)
            # The store's exclusion is the export allowlist: provisioning writes no `.gitignore`.
            self.assertFalse((target / ".gitignore").exists())
            # The Compose definition is root's, outside the instance, and is never handed over.
            self.assertTrue(compose.is_file())
            self.assertEqual(compose.stat().st_mode & 0o777, 0o600)
            self.assertFalse(any(event[0] == "chown" and event[1] == compose for event in events))

    def test_rejects_unsupported_host_before_creating_user_or_checkout(self) -> None:
        args = SimpleNamespace(
            instance_dir="/tmp/instance",
            instance_remote="remote",
            installation_user="dev",
            dry_run=False,
        )
        with (
            mock.patch("ummanu.bootstrap.os.geteuid", return_value=0),
            mock.patch("ummanu.bootstrap._host_supported", side_effect=BootstrapError("unsupported")),
            mock.patch("ummanu.bootstrap._ensure_installation_user") as ensure_user,
            mock.patch("ummanu.bootstrap._clone_or_reuse") as clone,
            mock.patch("builtins.print"),
        ):
            self.assertEqual(bootstrap(args), 1)
        ensure_user.assert_not_called()
        clone.assert_not_called()


if __name__ == "__main__":
    unittest.main()
