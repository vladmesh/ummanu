"""Recovering an installation whose only input is the private remote.

The host is gone: the clone carries the catalog and the sealed values, never the
installation key. These tests cover both branches the operator can be in, with
the phrase and without it, and the two ways a secret can fail to come back.
"""

from __future__ import annotations

import contextlib
import getpass
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.fakes.installation import PRODUCT_ROOT, _checkpoint, _git
from tests.retired_board import LEGACY_ENV, LEGACY_SECRET_IDS, LEGACY_VALUES
from ummanu import installation, secret_recover, secret_store
from ummanu.cli import main
from ummanu.secret_store import (
    RecoveryPhraseError,
    import_env_file,
    initialize_store,
)
from ummanu.secret_words import RECOVERY_WORDS

PHRASE = " ".join(RECOVERY_WORDS[:16])
RUNTIME_ENV = "EXAMPLE_URL=http://127.0.0.1/rpc\nEXAMPLE_API_USER=rpc-user\nEXAMPLE_API_TOKEN=live-token\n"


def fast_key_params() -> dict:
    """The production work factor costs a tenth of a second per derivation.

    Every parameter is read back out of the committed file, so a cheaper factor
    exercises the same derivation the real store uses.
    """
    return {
        "format": secret_store.KEY_PARAMS_FORMAT,
        "version": secret_store.KEY_PARAMS_VERSION,
        "kdf": {
            "id": "scrypt",
            "salt": secret_store._b64(b"0123456789abcdef"),
            "length": 32,
            "n": 2**8,
            "r": 8,
            "p": 1,
        },
    }


class RecoveryCase(unittest.TestCase):
    """A lost host: a private remote with a store, and an empty target."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        root = Path(self.tmpdir.name)
        self.source = root / "source"
        self.target = root / "target"
        self.data = root / "data"
        self.source.mkdir()
        _checkpoint(self.source, self.data)
        knowledge = self.source / "state" / "knowledge"
        knowledge.mkdir(parents=True)
        (knowledge / "sprint.md").write_text("# sprint\n", encoding="utf-8")
        _git(self.source, "init")
        _git(self.source, "config", "user.name", "Test")
        _git(self.source, "config", "user.email", "test@example.invalid")
        _git(self.source, "add", ".")
        _git(self.source, "commit", "-m", "checkpoint")

        self.original = self.source / "runtime.env"
        self.original.write_text(RUNTIME_ENV, encoding="utf-8")
        self.original.chmod(0o600)
        with mock.patch.object(secret_store, "_new_key_params", side_effect=fast_key_params):
            initialize_store(self.source, phrase=PHRASE, actor="tester")
        import_env_file(
            self.source,
            source=self.original,
            scope="installation",
            purpose="service credentials",
            actor="tester",
            materialize={"target": secret_store.MATERIALIZE_RUNTIME_ENV},
        )
        # The store writes files and makes no commit; the legacy tick commits its exported files
        # (`checkpoint.LEGACY_LIVE_PATHS`), which is what a clone of the remote then carries.
        _git(
            self.source,
            "add",
            "--",
            "secrets/catalog.yaml",
            "secrets/installation-key.json",
            "secrets/values",
        )
        _git(self.source, "commit", "-m", "checkpoint(state): the secret store")
        self.phrase_file = root / "phrase.txt"
        self.phrase_file.write_text(PHRASE + "\n", encoding="utf-8")
        # No terminal: nothing in a test run may block on a phrase prompt.
        stdin = mock.patch("sys.stdin", io.StringIO())
        stdin.start()
        self.addCleanup(stdin.stop)

    @property
    def restored(self) -> Path:
        return self.target / "runtime.env"

    def recover(self, *extra: str) -> tuple[int, str]:
        argv = [
            "recover",
            "--instance-remote",
            str(self.source),
            "--instance-dir",
            str(self.target),
            "--installation-user",
            getpass.getuser(),
            # The recovery materializes this checkout; nothing points at one for it.
            "--product-root",
            str(PRODUCT_ROOT),
            *extra,
        ]
        output = io.StringIO()
        patches = (
            mock.patch("ummanu.installation._ensure_installation_user"),
            mock.patch("ummanu.installation.check_prerequisites"),
            mock.patch("ummanu.installation.import_normalized_board", return_value=1),
            mock.patch("ummanu.installation.rebuild_memory_index", return_value=1),
            mock.patch(
                "ummanu.installation.materialize_host",
                return_value=SimpleNamespace(steps=[SimpleNamespace(status="changed")]),
            ),
            mock.patch("ummanu.installation.materialize_pipeline_state", return_value=0),
            mock.patch("ummanu.installation.restore_findings", return_value=[]),
        )
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            stack.enter_context(contextlib.redirect_stdout(output))
            code = main(argv)
        return code, output.getvalue()

    def drop_value(self, secret_id: str) -> None:
        """Lose one envelope in the remote, keeping its catalog entry."""
        _git(self.source, "rm", "--quiet", f"secrets/values/{secret_id}.enc.json")
        _git(self.source, "commit", "-m", "lose one value")


class LegacyBoardOnlyRecoveryTests(RecoveryCase):
    def test_legacy_ids_recover_as_ordinary_entries_locked_or_unlocked(self) -> None:
        """Nothing names the retired transport's ids, so recovery treats them like any secret.

        A store holding only them reports them locked without the phrase and puts them back with
        it; the file they land in stays a valid runtime.env whose extra keys nothing reads.
        """
        root = Path(self.tmpdir.name) / "legacy-only"
        root.mkdir()
        _git(root, "init")
        _git(root, "config", "user.name", "Test")
        _git(root, "config", "user.email", "test@example.invalid")
        (root / ".gitignore").write_text("runtime.env\n", encoding="utf-8")
        with mock.patch.object(secret_store, "_new_key_params", side_effect=fast_key_params):
            initialize_store(root, phrase=PHRASE, actor="tester")
        for secret_id, environment, value in zip(LEGACY_SECRET_IDS, LEGACY_ENV, LEGACY_VALUES):
            secret_store.set_secret(
                root,
                secret_id=secret_id,
                value=value.encode(),
                scope="installation",
                purpose="historic board configuration",
                environment=environment,
                materialize={"target": "runtime-env"},
                actor="tester",
            )
        secret_store.key_path(root).unlink()
        runtime_env = root / "runtime.env"

        with mock.patch.dict(os.environ, {"UMMANU_RUNTIME_ENV_FILE": str(runtime_env)}):
            locked = secret_recover.recover_secrets(root)
            opened = secret_recover.recover_secrets(root, phrase=PHRASE)

        self.assertFalse(locked.unlocked)
        self.assertEqual(sorted(entry["id"] for entry in locked.locked), sorted(LEGACY_SECRET_IDS))
        self.assertEqual(locked.missing, ())
        self.assertTrue(opened.unlocked)
        self.assertEqual([result.path for result in opened.materialized], [runtime_env])
        self.assertEqual(installation.read_runtime_env(root, None), dict(zip(LEGACY_ENV, LEGACY_VALUES)))


class PhraseBranchCase(RecoveryCase):
    def test_a_damaged_verifier_refuses_before_writing_the_key_or_env_file(self) -> None:
        path = secret_store.key_params_path(self.source)
        params = json.loads(path.read_text(encoding="utf-8"))
        params["verifier"]["nonce"] = secret_store._b64(b"x")
        path.write_text(json.dumps(params), encoding="utf-8")
        _git(self.source, "add", "secrets/installation-key.json")
        _git(self.source, "commit", "-m", "damaged verifier")

        code, output = self.recover("--recovery-phrase-file", str(self.phrase_file))

        self.assertEqual(code, 1, output)
        self.assertIn("verifier nonce", output)
        self.assertNotIn(PHRASE, output)
        self.assertFalse(secret_store.key_path(self.target).exists())
        self.assertFalse(self.restored.exists())

    def test_the_phrase_rebuilds_the_key_and_puts_runtime_env_back(self) -> None:
        code, output = self.recover("--recovery-phrase-file", str(self.phrase_file))

        self.assertEqual(code, 0, output)
        self.assertIn("status: ok", output)
        self.assertEqual(self.restored.read_bytes(), self.original.read_bytes())
        self.assertEqual(self.restored.stat().st_mode & 0o777, 0o600)
        self.assertTrue(secret_store.key_path(self.target).is_file())

    def test_the_recovered_file_passes_the_installation_validator_unchanged(self) -> None:
        self.recover("--recovery-phrase-file", str(self.phrase_file))

        values = installation.read_runtime_env(self.target, None)
        self.assertEqual(values["EXAMPLE_API_TOKEN"], "live-token")
        self.assertEqual(values["EXAMPLE_URL"], "http://127.0.0.1/rpc")

    def test_the_phrase_arrives_through_stdin_without_touching_argv(self) -> None:
        with mock.patch("sys.stdin", io.StringIO(PHRASE + "\n")):
            code, output = self.recover("--recovery-phrase-stdin")

        self.assertEqual(code, 0, output)
        self.assertEqual(self.restored.read_bytes(), self.original.read_bytes())

    def test_a_second_recovery_is_idempotent(self) -> None:
        first_code, first_output = self.recover("--recovery-phrase-file", str(self.phrase_file))
        stamp = self.restored.stat().st_mtime_ns
        second_code, second_output = self.recover("--recovery-phrase-file", str(self.phrase_file))

        self.assertEqual((first_code, second_code), (0, 0), first_output + second_output)
        self.assertEqual(self.restored.read_bytes(), self.original.read_bytes())
        self.assertEqual(self.restored.stat().st_mtime_ns, stamp)
        self.assertIn("unchanged secret-store: 1 env file(s) written", second_output)

    def test_a_second_recovery_leaves_the_installation_key_alone(self) -> None:
        self.recover("--recovery-phrase-file", str(self.phrase_file))
        key = secret_store.key_path(self.target)
        material, stamp = key.read_bytes(), key.stat().st_mtime_ns

        code, output = self.recover("--recovery-phrase-file", str(self.phrase_file))

        self.assertEqual(code, 0, output)
        self.assertEqual(key.read_bytes(), material)
        self.assertEqual(key.stat().st_mtime_ns, stamp)

    def test_an_unusable_key_file_is_rebuilt_from_the_phrase(self) -> None:
        self.recover("--recovery-phrase-file", str(self.phrase_file))
        key = secret_store.key_path(self.target)
        material = key.read_bytes()
        key.unlink()

        code, output = self.recover("--recovery-phrase-file", str(self.phrase_file))

        self.assertEqual(code, 0, output)
        self.assertEqual(key.read_bytes(), material)

    def test_a_wrong_phrase_refuses_and_leaves_nothing_behind(self) -> None:
        wrong = self.phrase_file.parent / "wrong.txt"
        wrong.write_text(" ".join(RECOVERY_WORDS[16:32]) + "\n", encoding="utf-8")

        code, output = self.recover("--recovery-phrase-file", str(wrong))

        self.assertEqual(code, 1)
        self.assertIn("recovery phrase does not match this installation", output)
        self.assertFalse(secret_store.key_path(self.target).exists())
        self.assertFalse(self.restored.exists())
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(self.target), "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout,
            "",
        )


class NoPhraseBranchCase(RecoveryCase):
    def test_everything_that_needs_no_credentials_comes_back(self) -> None:
        code, output = self.recover()

        self.assertEqual(code, 1, output)
        cards = json.loads((self.data / "board" / "cards.json").read_text(encoding="utf-8"))
        self.assertEqual(len(cards["cards"]), 1)
        self.assertEqual(
            (self.target / "state" / "knowledge" / "sprint.md").read_text(encoding="utf-8"),
            "# sprint\n",
        )
        self.assertTrue((self.target / "instance.yaml").is_file())
        self.assertIn("changed   checkpoint", output)
        self.assertIn("changed   memory", output)
        self.assertIn("skipped   board", output)
        self.assertIn("skipped   host", output)

    def test_the_report_separates_locked_from_missing_and_holds_no_values(self) -> None:
        self.drop_value("example_api_token")

        code, output = self.recover()

        self.assertEqual(code, 1)
        self.assertIn("locked    secret:example_url", output)
        self.assertIn("missing   secret:example_api_token", output)
        self.assertNotIn("live-token", output)
        self.assertFalse(self.restored.exists())

    def test_the_refusal_names_the_store_instead_of_asking_for_a_file(self) -> None:
        code, output = self.recover()

        self.assertEqual(code, 1)
        self.assertIn("recovery is incomplete", output)
        self.assertIn("rerun with the recovery phrase", output)
        self.assertNotIn("runtime credentials are required", output)

    def test_an_operator_written_runtime_env_still_reports_the_locked_secrets(self) -> None:
        self.target.mkdir(parents=True)
        subprocess.run(
            ["git", "clone", "--quiet", str(self.source), str(self.target)],
            check=True,
            capture_output=True,
            text=True,
        )
        self.restored.write_text(RUNTIME_ENV, encoding="utf-8")
        self.restored.chmod(0o600)

        code, output = self.recover()

        # The runtime file the operator wrote is used as it is; the store stays locked and says so.
        # The recovery no longer fails on a JSON-RPC transport tuple the board store never reads.
        self.assertEqual(code, 0, output)
        self.assertIn("locked    secret:example_api_token", output)
        self.assertIn("3 secret(s) locked", output)
        self.assertIn("status: board and memory are ready, but store is locked", output)
        self.assertNotIn("refuse to guess or rotate", output)


class MissingValueCase(RecoveryCase):
    def test_a_gap_in_the_store_leaves_the_env_file_alone(self) -> None:
        """A file written without one of its variables is a component that starts
        with a plausible configuration and fails somewhere else."""
        self.drop_value("example_api_token")

        code, output = self.recover("--recovery-phrase-file", str(self.phrase_file))

        self.assertEqual(code, 1)
        self.assertFalse(self.restored.exists())
        self.assertIn("missing   secret:example_api_token", output)
        self.assertIn("withheld", output)


class NoStoreCase(unittest.TestCase):
    def test_an_installation_without_a_store_keeps_the_hand_written_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            target = root / "target"
            source.mkdir()
            _checkpoint(source, root / "data")
            _git(source, "init")
            _git(source, "config", "user.name", "Test")
            _git(source, "config", "user.email", "test@example.invalid")
            _git(source, "add", ".")
            _git(source, "commit", "-m", "checkpoint")

            output = io.StringIO()
            real_run = installation._run

            def run_orca_version(
                argv: list[str],
                *,
                label: str,
                timeout: int = 120,
                cwd: Path | None = None,
            ) -> str:
                if argv[-2:] == ["orca", "--version"]:
                    return ""
                return real_run(argv, label=label, timeout=timeout, cwd=cwd)

            with (
                mock.patch("sys.stdin", io.StringIO()),
                mock.patch("ummanu.installation._ensure_installation_user"),
                mock.patch("ummanu.installation.shutil.which", return_value="/usr/bin/orca"),
                mock.patch("ummanu.installation._run", side_effect=run_orca_version),
                contextlib.redirect_stdout(output),
            ):
                code = main(
                    [
                        "recover",
                        "--instance-remote",
                        str(source),
                        "--instance-dir",
                        str(target),
                        "--installation-user",
                        getpass.getuser(),
                    ]
                )

            self.assertEqual(code, 1)
            self.assertTrue((target / ".git").is_dir())
            self.assertTrue((target / "instance.yaml").is_file())
            self.assertTrue((target / "state").is_dir())
            self.assertIn("PostgreSQL prerequisite failed", output.getvalue())
            self.assertIn("skipped   runtime-env", output.getvalue())
            self.assertIn("skipped   secret-store", output.getvalue())


class ReportCase(RecoveryCase):
    def test_recover_secrets_reports_ids_and_targets_only(self) -> None:
        subprocess.run(
            ["git", "clone", "--quiet", str(self.source), str(self.target)],
            check=True,
            capture_output=True,
            text=True,
        )
        with mock.patch.dict(os.environ, {"UMMANU_RUNTIME_ENV_FILE": str(self.restored)}):
            locked = secret_recover.recover_secrets(self.target)
            opened = secret_recover.recover_secrets(self.target, phrase=PHRASE)

        self.assertFalse(locked.unlocked)
        self.assertTrue(locked.store_present)
        self.assertEqual(
            sorted(entry["id"] for entry in locked.locked),
            ["example_api_token", "example_api_user", "example_url"],
        )
        self.assertEqual(locked.missing, ())
        for entry in locked.locked:
            self.assertEqual(set(entry) & {"value", "ciphertext"}, set())
            self.assertEqual(entry["path"], str(self.restored))
        self.assertTrue(opened.complete)
        self.assertEqual(self.restored.read_bytes(), self.original.read_bytes())

    def test_a_wrong_phrase_writes_no_key_and_no_env_file(self) -> None:
        subprocess.run(
            ["git", "clone", "--quiet", str(self.source), str(self.target)],
            check=True,
            capture_output=True,
            text=True,
        )
        with self.assertRaises(RecoveryPhraseError):
            secret_recover.recover_secrets(self.target, phrase=" ".join(RECOVERY_WORDS[16:32]))

        self.assertFalse(secret_store.key_path(self.target).exists())
        self.assertFalse(self.restored.exists())


if __name__ == "__main__":
    unittest.main()
