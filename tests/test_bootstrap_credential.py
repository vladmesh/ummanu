"""`ummanu bootstrap` takes the external clone credential `recover` takes (ummanu-65 P20).

A clean host bootstraps from a private GitHub instance remote, so bootstrap's clone step needs the
same `--bootstrap-credential-file` / `--bootstrap-credential-stdin` that `recover` accepts, read and
checked by the one `installation._bootstrap_credential` and handed to the same clone step.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ummanu import installation
from ummanu.bootstrap import bootstrap
from ummanu.cli import build_parser
from ummanu.installation import InstallError, SnapshotCheckout

REMOTE = "https://github.com/example/private-instance.git"
TOKEN = "fixture-bootstrap-token"
CREDENTIAL_OPTIONS = ("--bootstrap-credential-file", "--bootstrap-credential-stdin")


def _subparser(name: str):
    parser = build_parser()
    for action in parser._subparsers._group_actions:  # type: ignore[union-attr]
        if name in action.choices:
            return action.choices[name]
    raise AssertionError(f"no {name} subcommand")


def _option(parser, option: str):
    return next(action for action in parser._actions if option in action.option_strings)


class BootstrapCredentialTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.target = self.root / "instance"
        self.token_file = self.root / "token"
        self.token_file.write_text(TOKEN + "\n", encoding="utf-8")
        self.token_file.chmod(0o600)

    def args(self, *extra: str):
        return build_parser().parse_args(
            [
                "bootstrap",
                "--instance-remote",
                REMOTE,
                "--instance-dir",
                str(self.target),
                "--installation-user",
                "dev",
                *extra,
            ]
        )

    def run_bootstrap(
        self, args, *, snapshot=None, clone=None, stdin: bytes = b""
    ) -> tuple[int, mock.Mock, str]:
        """Run bootstrap with the host edges stubbed. Root is stood in for, so the caller's own token
        file is accepted as the `sudo` caller's (`SUDO_UID`), as on a real host."""
        steps = mock.Mock()
        output = io.StringIO()
        with (
            mock.patch("ummanu.bootstrap.os.geteuid", return_value=0),
            mock.patch.dict(os.environ, {"SUDO_UID": str(os.getuid())}),
            mock.patch("sys.stdin", SimpleNamespace(buffer=io.BytesIO(stdin))),
            mock.patch("ummanu.bootstrap._host_supported"),
            mock.patch("ummanu.bootstrap._ensure_installation_user", steps.ensure_user),
            mock.patch("ummanu.bootstrap._snapshot_checkout", snapshot or mock.Mock(return_value=None)),
            mock.patch("ummanu.bootstrap._clone_or_reuse", clone or mock.Mock(side_effect=self.clone)),
            mock.patch("ummanu.bootstrap._install_platform"),
            mock.patch("ummanu.bootstrap._set_installation_owner"),
            mock.patch("ummanu.bootstrap.provision_board_store"),
            mock.patch("ummanu.bootstrap.migrate_instance"),
            mock.patch("ummanu.bootstrap.verify_board_store_roles"),
            mock.patch("builtins.print", side_effect=lambda *values: output.write(" ".join(values))),
        ):
            code = bootstrap(args)
        return code, steps, output.getvalue()

    def clone(self, _remote: str, directory: Path, **_kwargs: object) -> str:
        (directory / ".git" / "info").mkdir(parents=True)
        (directory / "instance.yaml").write_text("version: 1\n", encoding="utf-8")
        return "cloned private instance remote"

    def disposable_copies(self) -> list[Path]:
        return sorted(self.root.glob(".ummanu-bootstrap-credential-*"))

    def test_bootstrap_help_lists_the_credential_options_exactly_as_recover_does(self) -> None:
        bootstrap_parser, recover_parser = _subparser("bootstrap"), _subparser("recover")
        help_text = bootstrap_parser.format_help()
        for option in CREDENTIAL_OPTIONS:
            ours, theirs = _option(bootstrap_parser, option), _option(recover_parser, option)
            self.assertIn(option, help_text)
            for field in ("help", "nargs", "const", "default", "type", "required", "dest"):
                self.assertEqual(getattr(ours, field), getattr(theirs, field), (option, field))
        # The two are one mutually exclusive choice, as for recover.
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            self.args("--bootstrap-credential-file", str(self.token_file), "--bootstrap-credential-stdin")

    def test_a_credential_file_reaches_the_legacy_clone_step(self) -> None:
        clone = mock.Mock(side_effect=self.clone)

        code, _steps, output = self.run_bootstrap(
            self.args("--bootstrap-credential-file", str(self.token_file)), clone=clone
        )

        self.assertEqual(code, 0, output)
        clone.assert_called_once()
        self.assertEqual(clone.call_args.kwargs["bootstrap_credential"], self.token_file)
        # The caller's own file is used in place and never removed.
        self.assertTrue(self.token_file.is_file())

    def test_a_credential_file_reaches_the_snapshot_clone_step(self) -> None:
        scratch, data = self.root / ".instance.snapshot-x", self.root / "data"

        def lay_out(_remote: str, directory: Path, **_kwargs: object) -> SnapshotCheckout:
            directory.mkdir()
            (directory / "instance.yaml").write_text("version: 1\n", encoding="utf-8")
            scratch.mkdir()
            return SnapshotCheckout("tip", data / "backup" / "instance.git", scratch, scratch, True, "", data)

        snapshot, clone = mock.Mock(side_effect=lay_out), mock.Mock()

        code, _steps, output = self.run_bootstrap(
            self.args("--bootstrap-credential-file", str(self.token_file)), snapshot=snapshot, clone=clone
        )

        self.assertEqual(code, 0, output)
        self.assertEqual(snapshot.call_args.kwargs["bootstrap_credential"], self.token_file)
        clone.assert_not_called()
        self.assertFalse(scratch.exists())

    def test_the_snapshot_clone_builds_its_remote_execution_with_the_credential(self) -> None:
        """Through the real `_snapshot_checkout`: the clone's `RemoteExecution` carries the file."""
        remote = mock.Mock()
        remote.return_value.run_clone.side_effect = InstallError("fixture clone refused")

        with mock.patch.object(installation, "RemoteExecution", remote):
            code, _steps, output = self.run_bootstrap(
                self.args("--bootstrap-credential-file", str(self.token_file)),
                snapshot=mock.Mock(wraps=installation._snapshot_checkout),
            )

        self.assertEqual(code, 1)
        self.assertIn("fixture clone refused", output)
        remote.assert_called_once_with(REMOTE, "initial-clone", bootstrap_file=self.token_file)
        self.assertFalse(self.target.exists())

    def test_a_permissive_credential_file_is_refused_before_any_clone_with_recovers_message(self) -> None:
        self.token_file.chmod(0o644)
        snapshot, clone = mock.Mock(), mock.Mock()

        code, steps, output = self.run_bootstrap(
            self.args("--bootstrap-credential-file", str(self.token_file)), snapshot=snapshot, clone=clone
        )

        self.assertEqual(code, 1)
        snapshot.assert_not_called()
        clone.assert_not_called()
        steps.ensure_user.assert_not_called()
        self.assertFalse(self.target.exists())

        recover_args = build_parser().parse_args(
            [
                "recover",
                "--instance-remote",
                REMOTE,
                "--instance-dir",
                str(self.target),
                "--installation-user",
                "dev",
                "--bootstrap-credential-file",
                str(self.token_file),
            ]
        )
        with (
            mock.patch.object(installation, "_ensure_installation_user"),
            mock.patch.object(installation, "_snapshot_checkout") as recover_snapshot,
            mock.patch.object(installation, "_clone_or_reuse") as recover_clone,
        ):
            result = installation.install(recover_args)
        recover_snapshot.assert_not_called()
        recover_clone.assert_not_called()
        message = {step.name: step.detail for step in result.steps}["install"]
        self.assertEqual(message, "bootstrap credential file must be a regular mode-0600 file")
        self.assertEqual(output, f"ummanu bootstrap\nstatus: failed: {message}")

    def test_a_stdin_credential_copy_is_removed_after_success(self) -> None:
        seen: list[tuple[Path, int, str]] = []

        def clone(remote: str, directory: Path, **kwargs: object) -> str:
            credential = kwargs["bootstrap_credential"]
            assert isinstance(credential, Path)
            seen.append(
                (credential, credential.stat().st_mode & 0o777, credential.read_text(encoding="utf-8"))
            )
            return self.clone(remote, directory)

        code, _steps, output = self.run_bootstrap(
            self.args("--bootstrap-credential-stdin"),
            clone=mock.Mock(side_effect=clone),
            stdin=f"{TOKEN}\n".encode(),
        )

        self.assertEqual(code, 0, output)
        [(credential, mode, body)] = seen
        self.assertEqual((credential.parent, mode, body), (self.root, 0o600, f"{TOKEN}\n"))
        self.assertTrue(credential.name.startswith(".ummanu-bootstrap-credential-"))
        self.assertEqual(self.disposable_copies(), [])

    def test_a_stdin_credential_copy_is_removed_after_failure(self) -> None:
        seen: list[Path] = []

        def clone(_remote: str, _directory: Path, **kwargs: object) -> str:
            credential = kwargs["bootstrap_credential"]
            assert isinstance(credential, Path)
            seen.append(credential)
            self.assertTrue(credential.is_file())
            raise InstallError("fixture clone refused")

        code, _steps, output = self.run_bootstrap(
            self.args("--bootstrap-credential-stdin"),
            clone=mock.Mock(side_effect=clone),
            stdin=f"{TOKEN}\n".encode(),
        )

        self.assertEqual(code, 1)
        self.assertIn("fixture clone refused", output)
        self.assertEqual(len(seen), 1)
        self.assertEqual(self.disposable_copies(), [])

    def test_without_a_credential_bootstrap_clones_as_before(self) -> None:
        clone = mock.Mock(side_effect=self.clone)

        with mock.patch(
            "ummanu.bootstrap._bootstrap_credential", wraps=installation._bootstrap_credential
        ) as read:
            code, _steps, output = self.run_bootstrap(self.args(), clone=clone)

        self.assertEqual(code, 0, output)
        read.assert_called_once()
        self.assertIsNone(clone.call_args.kwargs["bootstrap_credential"])
        self.assertEqual(self.disposable_copies(), [])

    def test_a_preview_consumes_no_credential(self) -> None:
        with mock.patch("ummanu.bootstrap._bootstrap_credential") as read:
            code, _steps, output = self.run_bootstrap(
                self.args("--bootstrap-credential-stdin", "--dry-run"), stdin=b"unread\n"
            )

        self.assertEqual(code, 0, output)
        read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
