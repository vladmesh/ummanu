import contextlib
import copy
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from tests.retired_board import LEGACY_ENV, LEGACY_SECRET_IDS, LEGACY_VALUES
from ummanu import _fsutil, installation, runtime_env, secret_commands, secret_store, state_repo
from ummanu.cli import main
from ummanu.config import validate
from ummanu.infra import export_allowlist
from ummanu.infra.export_allowlist import is_exported
from ummanu.memory.canon import CanonTransaction
from ummanu.runtime import role_env
from ummanu.secret_store import (
    CATALOG_NAME,
    KEY_NAME,
    KEY_PARAMS_NAME,
    KEY_RELATIVE,
    RecoveryPhraseError,
    SecretStoreError,
    SecretStoreStateError,
    SecretStoreValidationError,
    generate_recovery_phrase,
    import_env_file,
    initialize_store,
    list_secrets,
    load_installation_key,
    materialize_secrets,
    read_secret,
    remove_secret,
    restore_installation_key,
    set_secret,
    store_divergence,
    store_revision,
)
from ummanu.secret_words import RECOVERY_WORDS

# Scrypt at the production work factor costs about a tenth of a second per call;
# a test that initializes a store in every setUp would spend most of its time
# there. The parameters are read back out of the file, so a cheaper factor
# exercises the same code path.
FAST_KDF = {
    "format": secret_store.KEY_PARAMS_FORMAT,
    "version": secret_store.KEY_PARAMS_VERSION,
    "kdf": {"id": "scrypt", "salt": "", "length": 32, "n": 2**8, "r": 8, "p": 1},
}


def fast_key_params():
    params = json.loads(json.dumps(FAST_KDF))
    params["kdf"]["salt"] = secret_store._b64(b"0123456789abcdef")
    return params


class SecretStoreCase(unittest.TestCase):
    """A live root (a plain directory, not a Git work tree) for a store to be initialized in."""

    phrase = " ".join(RECOVERY_WORDS[:16])

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.instance_dir = Path(self.tmpdir.name) / "secretary-instance"
        self.instance_dir.mkdir(parents=True)
        (self.instance_dir / "instance.yaml").write_text("version: 1\n", encoding="utf-8")
        self.kdf_patch = mock.patch.object(secret_store, "_new_key_params", side_effect=fast_key_params)
        self.kdf_patch.start()
        self.addCleanup(self.kdf_patch.stop)
        self.addCleanup(self.tmpdir.cleanup)

    def initialize(self) -> None:
        initialize_store(self.instance_dir, phrase=self.phrase, actor="tester")

    def exported(self) -> list[str]:
        """Every live-root file the snapshot export would copy."""
        return sorted(
            path.relative_to(self.instance_dir).as_posix()
            for path in self.instance_dir.rglob("*")
            if path.is_file() and is_exported(path.relative_to(self.instance_dir).as_posix())
        )

    def store_state(self) -> dict[str, bytes]:
        """Every file under `secrets/`, by live-root path, with its bytes."""
        root = self.instance_dir / "secrets"
        if not root.exists():
            return {}
        return {
            path.relative_to(self.instance_dir).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def changed_since(self, before: dict[str, bytes]) -> list[str]:
        """The `secrets/` files written, added or removed since `before`."""
        after = self.store_state()
        return sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))

    def catalog(self) -> dict:
        return yaml.safe_load((self.instance_dir / "secrets" / CATALOG_NAME).read_text(encoding="utf-8"))


class InitCase(SecretStoreCase):
    def test_init_creates_key_catalog_and_answers_the_store_revision(self) -> None:
        result = initialize_store(self.instance_dir, phrase=self.phrase, actor="tester")
        self.assertTrue(result.commit.startswith("sha256:"))
        self.assertEqual(result.commit, store_revision(self.instance_dir))
        self.assertEqual(result.catalog_path, self.instance_dir / "secrets" / CATALOG_NAME)
        self.assertEqual(self.catalog(), {"version": secret_store.CATALOG_VERSION, "secrets": []})
        self.assertEqual(
            self.exported(), ["instance.yaml", "secrets/catalog.yaml", "secrets/installation-key.json"]
        )
        self.assertFalse((self.instance_dir / ".gitignore").exists())
        self.assertFalse((self.instance_dir / ".git").exists())

    def test_installation_key_is_0600_and_never_exported(self) -> None:
        self.initialize()
        key = self.instance_dir / "secrets" / KEY_NAME
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        self.assertEqual(KEY_RELATIVE, f"secrets/{KEY_NAME}")
        self.assertFalse(is_exported(KEY_RELATIVE))
        self.assertNotIn(KEY_RELATIVE, self.exported())

    def test_key_params_are_open_and_hold_no_key_material(self) -> None:
        self.initialize()
        params = json.loads((self.instance_dir / "secrets" / "installation-key.json").read_text("utf-8"))
        self.assertEqual(params["format"], secret_store.KEY_PARAMS_FORMAT)
        self.assertEqual(params["version"], secret_store.KEY_PARAMS_VERSION)
        self.assertEqual(params["kdf"]["id"], "scrypt")
        self.assertEqual(params["verifier"]["id"], "chacha20poly1305")
        key = load_installation_key(self.instance_dir)
        self.assertNotIn(secret_store._b64(key), json.dumps(params))

    def test_second_init_refuses_and_changes_nothing(self) -> None:
        self.initialize()
        head = self.store_state()
        key_before = (self.instance_dir / "secrets" / KEY_NAME).read_bytes()
        with self.assertRaises(SecretStoreStateError) as caught:
            initialize_store(self.instance_dir, phrase=self.phrase, actor="tester")
        self.assertIn("already initialized", str(caught.exception))
        self.assertEqual(self.store_state(), head)
        self.assertEqual((self.instance_dir / "secrets" / KEY_NAME).read_bytes(), key_before)


class RecoveryPhraseCase(SecretStoreCase):
    def test_generated_phrase_is_from_the_wordlist_and_long_enough(self) -> None:
        phrase = generate_recovery_phrase()
        words = phrase.split()
        self.assertEqual(len(words), 16)
        self.assertTrue(set(words) <= set(RECOVERY_WORDS))
        self.assertNotEqual(phrase, generate_recovery_phrase())

    def test_wordlist_is_exactly_256_distinct_words(self) -> None:
        self.assertEqual(len(RECOVERY_WORDS), 256)
        self.assertEqual(len(set(RECOVERY_WORDS)), 256)

    def test_phrase_rebuilds_the_same_key_after_the_key_file_is_lost(self) -> None:
        self.initialize()
        key_file = self.instance_dir / "secrets" / KEY_NAME
        original = load_installation_key(self.instance_dir)
        key_file.unlink()
        with self.assertRaises(SecretStoreStateError):
            load_installation_key(self.instance_dir)
        restore_installation_key(self.instance_dir, self.phrase.upper() + "  ")
        self.assertEqual(load_installation_key(self.instance_dir), original)
        self.assertEqual(key_file.stat().st_mode & 0o777, 0o600)

    def test_restored_key_opens_a_value_written_before_the_loss(self) -> None:
        self.initialize()
        set_secret(
            self.instance_dir,
            secret_id="service.api-token",
            value=b"token-value",
            scope="installation",
            purpose="board api",
            actor="tester",
        )
        (self.instance_dir / "secrets" / KEY_NAME).unlink()
        restore_installation_key(self.instance_dir, self.phrase)
        self.assertEqual(read_secret(self.instance_dir, "service.api-token"), b"token-value")

    def test_wrong_phrase_is_an_explicit_error_and_writes_nothing(self) -> None:
        self.initialize()
        key_file = self.instance_dir / "secrets" / KEY_NAME
        key_file.unlink()
        wrong = " ".join(RECOVERY_WORDS[16:32])
        with self.assertRaises(RecoveryPhraseError) as caught:
            restore_installation_key(self.instance_dir, wrong)
        self.assertIn("does not match", str(caught.exception))
        self.assertFalse(key_file.exists())

    def test_wrong_phrase_never_yields_a_usable_key(self) -> None:
        self.initialize()
        good = load_installation_key(self.instance_dir)
        params = json.loads((self.instance_dir / "secrets" / "installation-key.json").read_text("utf-8"))
        wrong = secret_store._derive_key(" ".join(RECOVERY_WORDS[32:48]), params)
        self.assertNotEqual(wrong, good)

    def test_invalid_or_expensive_scrypt_parameters_are_refused_before_derivation(self) -> None:
        self.initialize()
        params_path = secret_store.key_params_path(self.instance_dir)
        original = json.loads(params_path.read_text(encoding="utf-8"))
        mutations = (
            {"length": 16},
            {"length": "32"},
            {"n": True},
            {"n": 3},
            {"r": 0},
            {"r": 64},
            {"p": -1},
            {"p": 32},
            {"n": 2**19, "r": 8},
            {"n": 2**16, "r": 8, "p": 5},
            {"salt": ""},
            {"id": "sentinel-secret-do-not-leak"},
        )
        for mutation in mutations:
            with self.subTest(parameters=mutation):
                params = copy.deepcopy(original)
                params["kdf"].update(mutation)
                params_path.write_text(json.dumps(params), encoding="utf-8")
                before = self.store_state()
                with mock.patch.object(secret_store, "Scrypt") as scrypt:
                    with self.assertRaises(SecretStoreStateError) as caught:
                        restore_installation_key(self.instance_dir, self.phrase)
                    scrypt.assert_not_called()
                self.assertNotIn("sentinel-secret-do-not-leak", str(caught.exception))
                self.assertEqual(self.store_state(), before)

    def test_recovery_uses_supported_recorded_scrypt_parameters(self) -> None:
        self.initialize()
        params = fast_key_params()
        params["kdf"].update(n=512, r=4, p=2)
        expected = secret_store.Scrypt(salt=b"0123456789abcdef", length=32, n=512, r=4, p=2).derive(
            self.phrase.encode("utf-8")
        )
        params["verifier"] = secret_store._seal_verifier(expected)
        secret_store.key_params_path(self.instance_dir).write_text(json.dumps(params), encoding="utf-8")
        secret_store.key_path(self.instance_dir).unlink()

        restore_installation_key(self.instance_dir, self.phrase)

        self.assertEqual(load_installation_key(self.instance_dir), expected)

    def test_a_kdf_runtime_failure_is_a_content_free_state_error(self) -> None:
        params = fast_key_params()
        with mock.patch.object(secret_store, "Scrypt") as scrypt:
            scrypt.return_value.derive.side_effect = ValueError("sentinel-secret-do-not-leak")
            with self.assertRaises(SecretStoreStateError) as caught:
                secret_store._derive_key(self.phrase, params)
        self.assertNotIn("sentinel-secret-do-not-leak", str(caught.exception))

    def test_malformed_verifier_is_a_state_error_and_keeps_the_key(self) -> None:
        self.initialize()
        params_path = secret_store.key_params_path(self.instance_dir)
        original = json.loads(params_path.read_text(encoding="utf-8"))
        for mutation in (
            {"nonce": secret_store._b64(b"x")},
            {"nonce": None},
            {"ciphertext": {"secret": "sentinel-secret-do-not-leak"}},
        ):
            with self.subTest(verifier=mutation):
                params = copy.deepcopy(original)
                params["verifier"].update(mutation)
                params_path.write_text(json.dumps(params), encoding="utf-8")
                before = self.store_state()
                with self.assertRaises(SecretStoreStateError) as caught:
                    restore_installation_key(self.instance_dir, self.phrase)
                self.assertNotIn("sentinel-secret-do-not-leak", str(caught.exception))
                self.assertEqual(self.store_state(), before)

    def test_restoring_a_key_ignores_a_preexisting_temporary_symlink(self) -> None:
        self.initialize()
        key_file = secret_store.key_path(self.instance_dir)
        original = key_file.read_bytes()
        victim = Path(self.tmpdir.name) / "leave-alone"
        victim.write_bytes(b"unchanged")
        victim.chmod(0o644)
        old_temporary = key_file.with_name(f".{key_file.name}.tmp")
        old_temporary.symlink_to(victim)

        restore_installation_key(self.instance_dir, self.phrase)

        self.assertEqual(victim.read_bytes(), b"unchanged")
        self.assertEqual(victim.stat().st_mode & 0o777, 0o644)
        self.assertFalse(key_file.is_symlink())
        self.assertEqual(key_file.read_bytes(), original)
        self.assertEqual(key_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(key_file.parent.glob(".installation.key.*.tmp")), [])

    def test_interrupted_key_publication_keeps_the_old_key_and_removes_its_temporary(self) -> None:
        self.initialize()
        key_file = secret_store.key_path(self.instance_dir)
        before = self.store_state()
        real_replace = os.replace

        def fail_key_replace(source, destination):
            if Path(destination) == key_file:
                temporary = Path(source)
                self.assertFalse(temporary.is_symlink())
                self.assertEqual(temporary.stat().st_mode & 0o777, 0o600)
                self.assertEqual(temporary.parent, key_file.parent)
                raise OSError(5, "injected")
            return real_replace(source, destination)

        for name, failure in (("replace", fail_key_replace), ("fsync", OSError(5, "injected"))):
            with self.subTest(stage=name):
                with (
                    mock.patch.object(secret_store.os, name, side_effect=failure),
                    self.assertRaises(SecretStoreError),
                ):
                    restore_installation_key(self.instance_dir, self.phrase)
                self.assertEqual(self.store_state(), before)
                self.assertEqual(list(key_file.parent.glob(".installation.key.*.tmp")), [])


class RoundTripCase(SecretStoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.initialize()

    def test_set_list_read_round_trip(self) -> None:
        result = set_secret(
            self.instance_dir,
            secret_id="openrouter.api-token",
            value=b"sk-live-value",
            scope="installation",
            purpose="model routing",
            environment="OPENROUTER_API_KEY",
            actor="tester",
        )
        self.assertTrue(result.created)
        entries = list_secrets(self.instance_dir)
        self.assertEqual(
            [dict(entry) for entry in entries],
            [
                {
                    "id": "openrouter.api-token",
                    "scope": "installation",
                    "purpose": "model routing",
                    "environment": "OPENROUTER_API_KEY",
                    "created_at": entries[0]["created_at"],
                }
            ],
        )
        self.assertEqual(read_secret(self.instance_dir, "openrouter.api-token"), b"sk-live-value")

    def test_multiline_and_binary_values_survive_unchanged(self) -> None:
        multiline = b"-----BEGIN CERTIFICATE-----\nline one\r\nline two\n\n  trailing spaces   \n"
        binary = bytes(range(256)) * 4
        set_secret(
            self.instance_dir,
            secret_id="github.app-key",
            value=multiline,
            scope="project:ummanu",
            purpose="github app private key",
            actor="tester",
        )
        set_secret(
            self.instance_dir,
            secret_id="binary.blob",
            value=binary,
            scope="installation",
            purpose="raw bytes",
            actor="tester",
        )
        self.assertEqual(read_secret(self.instance_dir, "github.app-key"), multiline)
        self.assertEqual(read_secret(self.instance_dir, "binary.blob"), binary)

    def test_catalog_holds_metadata_only_and_the_value_file_hides_the_value(self) -> None:
        set_secret(
            self.instance_dir,
            secret_id="service.api-token",
            value=b"plaintext-needle",
            scope="installation",
            purpose="board api",
            actor="tester",
        )
        catalog_text = (self.instance_dir / "secrets" / CATALOG_NAME).read_text("utf-8")
        self.assertNotIn("plaintext-needle", catalog_text)
        envelope_text = (self.instance_dir / "secrets" / "values" / "service.api-token.enc.json").read_text(
            "utf-8"
        )
        self.assertNotIn("plaintext-needle", envelope_text)
        for name in self.exported():
            self.assertNotIn(b"plaintext-needle", (self.instance_dir / name).read_bytes(), name)

    def test_envelope_declares_its_format_kdf_and_aead_in_the_open(self) -> None:
        set_secret(
            self.instance_dir,
            secret_id="service.api-token",
            value=b"token",
            scope="installation",
            purpose="board api",
            actor="tester",
        )
        envelope = json.loads(
            (self.instance_dir / "secrets" / "values" / "service.api-token.enc.json").read_text("utf-8")
        )
        self.assertEqual(envelope["format"], secret_store.ENVELOPE_FORMAT)
        self.assertEqual(envelope["version"], secret_store.ENVELOPE_VERSION)
        self.assertEqual(envelope["kdf"]["id"], "hkdf-sha256")
        self.assertEqual(envelope["aead"]["id"], "chacha20poly1305")
        self.assertIn("salt", envelope["kdf"])
        self.assertIn("nonce", envelope["aead"])

    def test_a_newer_envelope_version_is_refused_not_guessed_at(self) -> None:
        key = load_installation_key(self.instance_dir)
        envelope = secret_store.seal_value(key, "x", b"value")
        envelope["version"] = secret_store.ENVELOPE_VERSION + 1
        with self.assertRaises(SecretStoreStateError) as caught:
            secret_store.open_value(key, envelope)
        self.assertIn("upgrade ummanu", str(caught.exception))

    def test_tampering_with_the_open_header_breaks_the_seal(self) -> None:
        key = load_installation_key(self.instance_dir)
        envelope = secret_store.seal_value(key, "x", b"value")
        envelope["id"] = "y"
        with self.assertRaises(SecretStoreStateError):
            secret_store.open_value(key, envelope)

    def test_corrupt_envelopes_are_content_free_state_errors(self) -> None:
        key = load_installation_key(self.instance_dir)
        sentinel = "sentinel-secret-do-not-leak"
        original = secret_store.seal_value(key, "x", sentinel.encode())
        mutations = (
            (("version",), sentinel),
            (("id",), sentinel),
            (("aead",), {"id": sentinel}),
            (("kdf",), {"id": sentinel}),
            (("aead", "nonce"), secret_store._b64(b"x")),
            (("aead", "nonce"), None),
            (("ciphertext",), {"secret": sentinel}),
            (("ciphertext",), ""),
            (("kdf", "length"), 16),
            (("kdf", "length"), "32"),
            (("kdf", "length"), True),
            (("kdf", "salt"), ""),
            (("kdf", "info"), {"secret": sentinel}),
        )
        for fields, value in mutations:
            with self.subTest(fields=fields, value=value):
                envelope = copy.deepcopy(original)
                target = envelope if len(fields) == 1 else envelope[fields[0]]
                target[fields[-1]] = value
                with self.assertRaises(SecretStoreStateError) as caught:
                    secret_store.open_value(key, envelope)
                self.assertNotIn(sentinel, str(caught.exception))

    def test_whole_envelope_substitution_is_refused_on_read_and_materialization(self) -> None:
        target = Path(self.tmpdir.name) / "runtime.env"
        for secret_id, value in (("a", b"alpha"), ("b", b"beta")):
            set_secret(
                self.instance_dir,
                secret_id=secret_id,
                value=value,
                scope="installation",
                purpose="service credential",
                actor="tester",
                environment=secret_id.upper(),
                materialize={"target": "file", "path": str(target)},
            )
        a_path = secret_store.value_path(self.instance_dir, "a")
        b_path = secret_store.value_path(self.instance_dir, "b")
        a_bytes, b_bytes = a_path.read_bytes(), b_path.read_bytes()
        a_path.write_bytes(b_bytes)
        b_path.write_bytes(a_bytes)
        target.write_bytes(b"UNCHANGED=value\n")

        # Both envelopes are intact; it is their relationship to the requested id that is wrong.
        key = load_installation_key(self.instance_dir)
        self.assertEqual(secret_store.open_value(key, json.loads(a_path.read_text())), b"beta")
        for secret_id in ("a", "b"):
            with self.subTest(secret_id=secret_id), self.assertRaises(SecretStoreStateError):
                read_secret(self.instance_dir, secret_id)
        with self.assertRaises(SecretStoreStateError):
            materialize_secrets(self.instance_dir)
        self.assertEqual(target.read_bytes(), b"UNCHANGED=value\n")

    def test_setting_a_secret_can_replace_a_damaged_envelope(self) -> None:
        arguments = {
            "secret_id": "x",
            "scope": "installation",
            "purpose": "service credential",
            "actor": "tester",
        }
        set_secret(self.instance_dir, value=b"old-value", **arguments)
        path = secret_store.value_path(self.instance_dir, "x")
        envelope = json.loads(path.read_text(encoding="utf-8"))
        envelope["aead"]["nonce"] = secret_store._b64(b"x")
        path.write_text(json.dumps(envelope), encoding="utf-8")

        set_secret(self.instance_dir, value=b"new-value", **arguments)

        self.assertEqual(read_secret(self.instance_dir, "x"), b"new-value")
        self.assertEqual(len(list_secrets(self.instance_dir)), 1)

    def test_updating_a_secret_keeps_created_at_and_one_catalog_entry(self) -> None:
        first = set_secret(
            self.instance_dir,
            secret_id="service.api-token",
            value=b"one",
            scope="installation",
            purpose="board api",
            actor="tester",
        )
        created_at = list_secrets(self.instance_dir)[0]["created_at"]
        second = set_secret(
            self.instance_dir,
            secret_id="service.api-token",
            value=b"two",
            scope="installation",
            purpose="board api, rotated",
            actor="tester",
        )
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        entries = list_secrets(self.instance_dir)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["created_at"], created_at)
        self.assertEqual(entries[0]["purpose"], "board api, rotated")
        self.assertEqual(read_secret(self.instance_dir, "service.api-token"), b"two")

    def test_catalog_and_value_land_in_the_same_write(self) -> None:
        before = self.store_state()
        set_secret(
            self.instance_dir,
            secret_id="service.api-token",
            value=b"token",
            scope="installation",
            purpose="board api",
            actor="tester",
        )
        self.assertEqual(
            self.changed_since(before),
            ["secrets/catalog.yaml", "secrets/values/service.api-token.enc.json"],
        )

    def test_set_refuses_once_the_key_would_be_exported(self) -> None:
        exported = mock.patch.object(
            export_allowlist, "SNAPSHOT_ALLOWLIST", (*export_allowlist.SNAPSHOT_ALLOWLIST, KEY_RELATIVE)
        )
        with exported, self.assertRaises(SecretStoreError) as caught:
            set_secret(
                self.instance_dir,
                secret_id="service.api-token",
                value=b"token",
                scope="installation",
                purpose="board api",
                actor="tester",
            )
        self.assertIn("export allowlist", str(caught.exception))
        self.assertEqual(list_secrets(self.instance_dir), ())

    def test_a_pasted_secret_in_an_open_field_stops_the_write(self) -> None:
        head = self.store_state()
        with self.assertRaises(SecretStoreValidationError) as caught:
            set_secret(
                self.instance_dir,
                secret_id="service.api-token",
                value=b"token",
                scope="installation",
                purpose="use AKIAIOSFODNN7EXAMPLE for the bucket",
                actor="tester",
            )
        self.assertIn("secret detected", str(caught.exception))
        self.assertEqual(self.store_state(), head)
        self.assertEqual(list_secrets(self.instance_dir), ())

    def test_bad_input_is_rejected_before_anything_is_written(self) -> None:
        cases = [
            {"secret_id": "Not-Lower"},
            {"secret_id": "../escape"},
            {"scope": "team:everyone"},
            {"purpose": "   "},
            {"value": b""},
        ]
        for override in cases:
            request = {
                "secret_id": "service.api-token",
                "value": b"token",
                "scope": "installation",
                "purpose": "board api",
                "actor": "tester",
                **override,
            }
            with self.subTest(override=override), self.assertRaises(SecretStoreValidationError):
                set_secret(self.instance_dir, **request)
        self.assertEqual(list_secrets(self.instance_dir), ())


class InterruptedWriteCase(SecretStoreCase):
    """A write cut in the middle leaves the catalog and the values agreeing."""

    def setUp(self) -> None:
        super().setUp()
        self.initialize()
        set_secret(
            self.instance_dir,
            secret_id="first.secret",
            value=b"first",
            scope="installation",
            purpose="already stored",
            actor="tester",
        )

    def assert_consistent(self) -> None:
        self.assertEqual(store_divergence(self.instance_dir), ())
        for entry in list_secrets(self.instance_dir):
            self.assertTrue(read_secret(self.instance_dir, entry["id"]))
        self.assertFalse((self.instance_dir / "secrets" / ".undo").exists())

    def test_interrupt_between_the_value_and_the_catalog_rolls_both_back(self) -> None:
        head = self.store_state()
        real_replace = os.replace
        calls = {"count": 0}

        def failing_replace(source, destination):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("interrupted between the value and the catalog")
            return real_replace(source, destination)

        with (
            mock.patch.object(_fsutil.os, "replace", side_effect=failing_replace),
            self.assertRaises(SecretStoreError),
        ):
            set_secret(
                self.instance_dir,
                secret_id="second.secret",
                value=b"second",
                scope="installation",
                purpose="interrupted",
                actor="tester",
            )
        self.assertEqual(self.store_state(), head)
        self.assert_consistent()
        self.assertEqual([entry["id"] for entry in list_secrets(self.instance_dir)], ["first.secret"])

    def test_interrupt_after_the_value_restores_both_files_byte_for_byte(self) -> None:
        """No commit follows the write any more: a failure after the envelope is in restores it too."""
        before = self.store_state()
        real_write = CanonTransaction.write
        calls: list[Path] = []

        def write_then_fail(transaction, path, text):
            calls.append(path)
            real_write(transaction, path, text)
            if len(calls) == 2:
                raise RuntimeError("interrupted after the catalog")

        with (
            mock.patch.object(CanonTransaction, "write", write_then_fail),
            self.assertRaises(SecretStoreError),
        ):
            set_secret(
                self.instance_dir,
                secret_id="second.secret",
                value=b"second",
                scope="installation",
                purpose="interrupted",
                actor="tester",
            )
        self.assertEqual(self.store_state(), before)
        self.assert_consistent()

        set_secret(
            self.instance_dir,
            secret_id="second.secret",
            value=b"second",
            scope="installation",
            purpose="interrupted",
            actor="tester",
        )
        self.assert_consistent()
        self.assertEqual(read_secret(self.instance_dir, "second.secret"), b"second")

    def test_divergence_is_reported_when_a_value_file_disappears(self) -> None:
        (self.instance_dir / "secrets" / "values" / "first.secret.enc.json").unlink()
        self.assertEqual(store_divergence(self.instance_dir), ("first.secret: catalogued with no value",))


class LegacyBoardSecretTests(SecretStoreCase):
    """An installed store still holding the retired transport's three ids keeps working.

    The catalog is open, so nothing names those ids: they are ordinary entries, listed, read,
    redacted by the ordinary rule and removable with the supported command.  No store operation
    refuses or special-cases them.
    """

    def setUp(self) -> None:
        super().setUp()
        self.initialize()
        # The on-disk shape an older build left: the three legacy ids, each catalogued with its
        # runtime variable, next to a current secret.
        for secret_id, environment, value in zip(LEGACY_SECRET_IDS, LEGACY_ENV, LEGACY_VALUES):
            set_secret(
                self.instance_dir,
                secret_id=secret_id,
                value=value.encode(),
                scope="installation",
                purpose="historic board configuration",
                environment=environment,
                actor="tester",
            )
        set_secret(
            self.instance_dir,
            secret_id="current.provider",
            value=b"ghp_" + b"a" * 36,
            scope="installation",
            purpose="current credential",
            environment="GITHUB_TOKEN",
            actor="tester",
        )

    def test_the_store_opens_lists_and_reads_with_the_legacy_ids_present(self) -> None:
        ids = [entry["id"] for entry in list_secrets(self.instance_dir)]
        self.assertEqual(sorted(ids), sorted([*LEGACY_SECRET_IDS, "current.provider"]))
        self.assertEqual(read_secret(self.instance_dir, "current.provider"), b"ghp_" + b"a" * 36)
        self.assertEqual(read_secret(self.instance_dir, LEGACY_SECRET_IDS[2]), LEGACY_VALUES[2].encode())
        self.assertEqual(store_divergence(self.instance_dir), ())
        self.assertEqual(secret_store.store_findings(self.instance_dir), ())
        health = secret_store.store_health(self.instance_dir)
        self.assertEqual(health["secret_count"], 4)
        self.assertEqual(health["installation_key"], {"present": True, "usable": True})

    def test_redaction_keeps_working_and_applies_the_ordinary_rule(self) -> None:
        values = secret_store.redaction_values(self.instance_dir)
        # The token's variable name is sensitive; the URL and user are plain configuration.
        self.assertIn(LEGACY_VALUES[2], values)
        self.assertNotIn(LEGACY_VALUES[0], values)
        self.assertNotIn(LEGACY_VALUES[1], values)
        self.assertIn("ghp_" + "a" * 36, values)

    def test_the_legacy_ids_are_removable_with_the_supported_command(self) -> None:
        for secret_id in LEGACY_SECRET_IDS:
            remove_secret(self.instance_dir, secret_id=secret_id, actor="tester")
        self.assertEqual([entry["id"] for entry in list_secrets(self.instance_dir)], ["current.provider"])
        self.assertEqual(store_divergence(self.instance_dir), ())


# The three keys the live installation's runtime.env holds, in the order the live
# file holds them, which is not alphabetical: URL, user, token. The token ends in
# '=' padding so a value that looks like another KEY=VALUE split has to survive
# the round trip too.
LIVE_RUNTIME_ENV = (
    "EXAMPLE_URL=https://board.example.invalid/rpc\n"
    "EXAMPLE_API_USER=ummanu\n"
    "EXAMPLE_API_TOKEN=1f2e3d4c5b6a==\n"
)


class EnvStoreCase(SecretStoreCase):
    """An initialized store plus a runtime.env shaped like the live one."""

    def setUp(self) -> None:
        super().setUp()
        self.initialize()
        self.source = Path(self.tmpdir.name) / "runtime.env"
        self.source.write_text(LIVE_RUNTIME_ENV, encoding="utf-8")
        os.chmod(self.source, 0o600)
        self.target = Path(self.tmpdir.name) / "materialized" / "runtime.env"
        override = mock.patch.dict(os.environ, {"UMMANU_RUNTIME_ENV_FILE": str(self.target)})
        override.start()
        self.addCleanup(override.stop)

    def do_import(self, source: Path | None = None, **overrides):
        request = {
            "source": source or self.source,
            "scope": "installation",
            "purpose": "board api",
            "actor": "tester",
            "materialize": {"target": "runtime-env"},
            **overrides,
        }
        return import_env_file(self.instance_dir, **request)


class RuntimeEnvRoundTripTests(EnvStoreCase):
    def test_import_materialize_and_process_keep_bytes_and_runtime_meaning(self) -> None:
        payload = (
            'UMMANU_DATA_DIR="data root"\n'
            "EXAMPLE_TOKEN=opaque\\-value\\-sentinel\n"
            "EXAMPLE_HASH=a#b\n"
            "EXAMPLE_SPACE=a b\n"
        )
        self.source.write_text(payload, encoding="utf-8")
        self.do_import()
        before = self.store_state()
        secret_store.materialize_secrets(self.instance_dir)
        self.assertEqual(self.target.read_bytes(), payload.encode())
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store_state(), before)
        self.assertEqual(secret_store.read_secret(self.instance_dir, "ummanu_data_dir"), b'"data root"')
        expected = {
            "UMMANU_DATA_DIR": "data root",
            "EXAMPLE_TOKEN": "opaque-value-sentinel",
            "EXAMPLE_HASH": "a#b",
            "EXAMPLE_SPACE": "a b",
        }
        self.assertEqual(runtime_env.read_runtime_env(self.instance_dir, str(self.target)), expected)
        self.assertEqual(role_env.load_env_file(self.target), expected)
        env = role_env.runtime_env(
            "pipeline",
            base_env={"PATH": os.defpath, "EXAMPLE_TOKEN": "ambient", "UMMANU_DATA_DIR": "ambient"},
            env_file=self.target,
        )
        self.assertEqual(env["UMMANU_DATA_DIR"], "data root")
        self.assertNotIn("EXAMPLE_TOKEN", env)
        child = subprocess.run(
            [sys.executable, "-c", "import json,os; print(json.dumps(os.environ['UMMANU_DATA_DIR']))"],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(json.loads(child.stdout), "data root")

    def test_unsupported_import_does_not_change_store_or_previous_materialization(self) -> None:
        self.do_import()
        secret_store.materialize_secrets(self.instance_dir)
        before_store, before_env = self.store_state(), self.target.read_bytes()
        for value in ('"unclosed-sentinel', "unclosed-sentinel\\", "'x'concatenated-sentinel"):
            with self.subTest(value=value):
                self.source.write_text(f"EXAMPLE_URL={value}\n", encoding="utf-8")
                with self.assertRaises(secret_store.SecretStoreValidationError) as caught:
                    self.do_import()
                self.assertNotIn("sentinel", str(caught.exception))
                self.assertEqual(self.store_state(), before_store)
                self.assertEqual(self.target.read_bytes(), before_env)

    def test_unrenderable_stored_value_preserves_previous_env_file(self) -> None:
        self.do_import()
        secret_store.materialize_secrets(self.instance_dir)
        before = self.target.read_bytes()
        secret_store.set_secret(
            self.instance_dir,
            secret_id="example_url",
            value=b'"unclosed-sentinel',
            scope="installation",
            purpose="synthetic configuration",
            environment="EXAMPLE_URL",
            materialize={"target": "runtime-env", "order": 0},
            actor="tester",
        )
        with self.assertRaises(secret_store.SecretStoreValidationError) as caught:
            secret_store.materialize_secrets(self.instance_dir)
        self.assertNotIn("unclosed-sentinel", str(caught.exception))
        self.assertEqual(self.target.read_bytes(), before)
        self.assertEqual(sorted(self.target.parent.iterdir()), [self.target])


class ImportCase(EnvStoreCase):
    def test_imported_escaped_secret_redacts_the_runtime_value_without_an_env_file(self) -> None:
        serialized = r"opaque\-credential\-sentinel"
        self.source.write_text(f"API_TOKEN={serialized}\n", encoding="utf-8")
        self.do_import()
        materialize_secrets(self.instance_dir)
        self.assertEqual(self.target.read_bytes(), self.source.read_bytes())
        decoded = secret_store.role_env.load_env_file(self.target)["API_TOKEN"]
        self.assertEqual(decoded, "opaque-credential-sentinel")
        values = secret_store.redaction_values(self.instance_dir)
        self.assertIn(serialized, values)
        self.assertIn(decoded, values)
        self.source.unlink()
        self.target.unlink()

        with mock.patch("ummanu.runtime.redact.DEFAULT_ENV_FILES", []):
            output = secret_store.redact(f"runtime: {decoded}\nfile: {serialized}", secret_values=values)

        self.assertNotIn(decoded, output)
        self.assertNotIn(serialized, output)

    def test_import_makes_one_secret_per_variable(self) -> None:
        result = self.do_import()
        self.assertEqual(result.created, ("example_url", "example_api_user", "example_api_token"))
        entries = list_secrets(self.instance_dir)
        self.assertEqual(
            [(entry["id"], entry["environment"]) for entry in entries],
            [
                ("example_api_token", "EXAMPLE_API_TOKEN"),
                ("example_api_user", "EXAMPLE_API_USER"),
                ("example_url", "EXAMPLE_URL"),
            ],
        )
        # The catalog is sorted by id, the file is not: each entry carries the
        # line it came from, so the file's own order survives the store.
        self.assertEqual(
            [(entry["id"], entry["materialize"]) for entry in entries],
            [
                ("example_api_token", {"target": "runtime-env", "order": 2}),
                ("example_api_user", {"target": "runtime-env", "order": 1}),
                ("example_url", {"target": "runtime-env", "order": 0}),
            ],
        )
        self.assertEqual(read_secret(self.instance_dir, "example_api_user"), b"ummanu")
        self.assertEqual(store_divergence(self.instance_dir), ())

    def test_import_lands_as_one_write(self) -> None:
        before = self.store_state()
        revision = store_revision(self.instance_dir)
        result = self.do_import()
        self.assertNotEqual(result.commit, revision)
        self.assertEqual(result.commit, store_revision(self.instance_dir))
        self.assertEqual(
            self.changed_since(before),
            [
                "secrets/catalog.yaml",
                "secrets/values/example_api_token.enc.json",
                "secrets/values/example_api_user.enc.json",
                "secrets/values/example_url.enc.json",
            ],
        )

    def test_reimporting_the_same_file_duplicates_nothing_and_writes_nothing(self) -> None:
        self.do_import()
        head = self.store_state()
        envelope = self.instance_dir / "secrets" / "values" / "example_url.enc.json"
        sealed = envelope.read_bytes()

        result = self.do_import()
        self.assertEqual(result.created, ())
        self.assertEqual(result.updated, ())
        self.assertEqual(result.unchanged, ("example_url", "example_api_user", "example_api_token"))
        self.assertEqual(self.store_state(), head)
        self.assertEqual(envelope.read_bytes(), sealed)
        self.assertEqual(len(list_secrets(self.instance_dir)), 3)

    def test_reimport_names_the_variable_that_moved(self) -> None:
        self.do_import()
        before = self.store_state()
        self.source.write_text(LIVE_RUNTIME_ENV.replace("=ummanu\n", "=ummanu-two\n"), encoding="utf-8")
        result = self.do_import()
        self.assertEqual(result.updated, ("example_api_user",))
        self.assertEqual(result.created, ())
        self.assertEqual(result.unchanged, ("example_url", "example_api_token"))
        self.assertEqual(read_secret(self.instance_dir, "example_api_user"), b"ummanu-two")
        # Only the rotated envelope moves: the catalog says the same thing it did
        # before, so the write does not restate it.
        self.assertEqual(self.changed_since(before), ["secrets/values/example_api_user.enc.json"])

    def test_import_keeps_created_at_across_a_rotation(self) -> None:
        self.do_import()
        created_at = list_secrets(self.instance_dir)[0]["created_at"]
        self.source.write_text(LIVE_RUNTIME_ENV.replace("=1f2e3d4c5b6a\n", "=rotated\n"), encoding="utf-8")
        self.do_import()
        self.assertEqual(list_secrets(self.instance_dir)[0]["created_at"], created_at)

    def test_a_file_import_cannot_read_is_refused_before_anything_is_written(self) -> None:
        head = self.store_state()
        cases = [
            "export EXAMPLE_URL=https://board\n",
            "EXAMPLE URL\n",
            "1BAD=value\n",
            "EXAMPLE_URL=a\nEXAMPLE_URL=b\n",
            "EXAMPLE_URL=\n",
            "# only a comment\n",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.source.write_text(text, encoding="utf-8")
                with self.assertRaises(SecretStoreValidationError):
                    self.do_import()
        self.assertEqual(list_secrets(self.instance_dir), ())
        self.assertEqual(self.store_state(), head)

    def test_a_file_the_store_could_not_reproduce_is_refused(self) -> None:
        """Anything the catalog cannot record is refused rather than dropped.

        The store keeps names, values and line order. A comment, a blank line, a
        padded line, a CR or a missing final newline would come back out as
        different bytes, so the import says so instead.
        """
        head = self.store_state()
        cases = {
            "no trailing newline": "EXAMPLE_URL=https://board\nEXAMPLE_API_USER=x",
            "blank line between": "EXAMPLE_URL=https://board\n\nEXAMPLE_API_USER=x\n",
            "blank line at the end": "EXAMPLE_URL=https://board\n\n",
            "comment above": "# board\nEXAMPLE_URL=https://board\n",
            "padded name": "  EXAMPLE_URL=https://board\n",
            "space around the equals": "EXAMPLE_URL = https://board\n",
            "trailing space in the value": "EXAMPLE_URL=https://board \n",
            "crlf": "EXAMPLE_URL=https://board\r\n",
        }
        for name, text in cases.items():
            with self.subTest(case=name):
                self.source.write_text(text, encoding="utf-8", newline="")
                with self.assertRaises(SecretStoreValidationError):
                    self.do_import()
        self.assertEqual(list_secrets(self.instance_dir), ())
        self.assertEqual(self.store_state(), head)

    def test_import_moves_an_earlier_variable_below_the_imported_block(self) -> None:
        set_secret(
            self.instance_dir,
            secret_id="extra.flag",
            value=b"on",
            scope="installation",
            purpose="added by hand",
            environment="EXTRA_FLAG",
            materialize={"target": "runtime-env"},
            actor="tester",
        )
        self.do_import()
        orders = {entry["id"]: entry["materialize"]["order"] for entry in list_secrets(self.instance_dir)}
        self.assertEqual(
            orders,
            {
                "example_url": 0,
                "example_api_user": 1,
                "example_api_token": 2,
                "extra.flag": 3,
            },
        )
        materialize_secrets(self.instance_dir)
        self.assertEqual(self.target.read_text(encoding="utf-8"), LIVE_RUNTIME_ENV + "EXTRA_FLAG=on\n")

    def test_import_refuses_names_that_differ_only_in_case(self) -> None:
        # One id per variable, so two names sharing an id are refused whole:
        # taking the file in would drop a line on the way back out.
        cased = Path(self.tmpdir.name) / "cased.env"
        cased.write_text("FOO=upper\nfoo=lower\n", encoding="utf-8")
        head = self.store_state()
        with self.assertRaises(SecretStoreValidationError) as caught:
            self.do_import(source=cased)
        self.assertIn("differ only in case", str(caught.exception))
        self.assertNotIn("upper", str(caught.exception))
        self.assertNotIn("lower", str(caught.exception))
        # Refused before the first write: no entry, no envelope, no commit.
        self.assertEqual(list(list_secrets(self.instance_dir)), [])
        self.assertEqual(self.store_state(), head)
        self.assertEqual(list((self.instance_dir / "secrets" / "values").glob("*")), [])
        self.assertEqual(materialize_secrets(self.instance_dir), ())
        self.assertFalse(self.target.exists())

    def test_import_does_not_take_over_a_variable_already_stored_under_that_id(self) -> None:
        set_secret(
            self.instance_dir,
            secret_id="foo",
            value=b"upper",
            scope="installation",
            purpose="board api",
            environment="FOO",
            materialize={"target": "runtime-env", "order": 0},
            actor="tester",
        )
        lower = Path(self.tmpdir.name) / "lower.env"
        lower.write_text("foo=lower\n", encoding="utf-8")
        with self.assertRaises(SecretStoreValidationError) as caught:
            self.do_import(source=lower)
        self.assertIn("would take over", str(caught.exception))
        entry = next(item for item in list_secrets(self.instance_dir) if item["id"] == "foo")
        self.assertEqual(entry["environment"], "FOO")
        self.assertEqual(read_secret(self.instance_dir, "foo"), b"upper")

    def test_import_does_not_read_a_file_that_is_not_there(self) -> None:
        with self.assertRaises(SecretStoreValidationError) as caught:
            self.do_import(source=Path(self.tmpdir.name) / "absent.env")
        self.assertIn("not found", str(caught.exception))


class RemoveCase(EnvStoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.do_import()

    def test_remove_drops_the_entry_and_the_envelope_in_one_write(self) -> None:
        envelope = self.instance_dir / "secrets" / "values" / "example_url.enc.json"
        before = self.store_state()
        result = remove_secret(self.instance_dir, secret_id="example_url", actor="tester")
        self.assertEqual(result.commit, store_revision(self.instance_dir))
        self.assertFalse(envelope.exists())
        self.assertEqual(
            [entry["id"] for entry in list_secrets(self.instance_dir)],
            ["example_api_token", "example_api_user"],
        )
        self.assertEqual(store_divergence(self.instance_dir), ())
        self.assertEqual(
            self.changed_since(before),
            ["secrets/catalog.yaml", "secrets/values/example_url.enc.json"],
        )
        self.assertNotIn("secrets/values/example_url.enc.json", self.exported())

    def test_removing_a_secret_that_is_not_there_is_an_error(self) -> None:
        head = self.store_state()
        with self.assertRaises(SecretStoreStateError) as caught:
            remove_secret(self.instance_dir, secret_id="never.stored", actor="tester")
        self.assertIn("no secret named", str(caught.exception))
        self.assertEqual(self.store_state(), head)
        self.assertEqual(len(list_secrets(self.instance_dir)), 3)


class MaterializeCase(EnvStoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.do_import()

    def test_materialize_writes_the_env_file_0600(self) -> None:
        results = materialize_secrets(self.instance_dir)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].target, "runtime-env")
        self.assertEqual(results[0].path, self.target)
        self.assertTrue(results[0].changed)
        self.assertEqual(self.target.read_text(encoding="utf-8"), LIVE_RUNTIME_ENV)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)

    def test_the_target_path_comes_from_role_env_not_from_a_constant(self) -> None:
        moved = Path(self.tmpdir.name) / "elsewhere" / "runtime.env"
        with mock.patch.dict(os.environ, {"UMMANU_RUNTIME_ENV_FILE": str(moved)}):
            results = materialize_secrets(self.instance_dir)
        self.assertEqual(results[0].path, moved)
        self.assertEqual(moved.read_text(encoding="utf-8"), LIVE_RUNTIME_ENV)
        self.assertFalse(self.target.exists())

    def test_ummanu_runtime_env_pin_beats_an_ambient_ta_override(self) -> None:
        pinned = Path(self.tmpdir.name) / "recovery" / "runtime.env"
        ambient = Path(self.tmpdir.name) / "live" / "runtime.env"
        entry = {"materialize": {"target": "runtime-env"}}
        with (
            mock.patch.dict(os.environ, {"TA_RUNTIME_ENV_FILE": str(ambient)}, clear=True),
            installation._runtime_environment({"UMMANU_RUNTIME_ENV_FILE": str(pinned)}),
        ):
            self.assertEqual(secret_store.materialize_path(self.instance_dir, entry), pinned)

    def test_a_second_run_leaves_the_file_byte_for_byte_the_same(self) -> None:
        materialize_secrets(self.instance_dir)
        first = self.target.read_bytes()
        before = self.target.stat().st_ino
        results = materialize_secrets(self.instance_dir)
        self.assertEqual(self.target.read_bytes(), first)
        self.assertFalse(results[0].changed)
        # Unchanged means untouched: systemd never sees a rename it did not need.
        self.assertEqual(self.target.stat().st_ino, before)

    def test_an_interrupted_swap_leaves_the_previous_file_in_place(self) -> None:
        materialize_secrets(self.instance_dir)
        before = self.target.read_bytes()
        set_secret(
            self.instance_dir,
            secret_id="example_api_user",
            value=b"rotated",
            scope="installation",
            purpose="board api",
            environment="EXAMPLE_API_USER",
            materialize={"target": "runtime-env"},
            actor="tester",
        )

        def fail_replace(source, destination):
            raise OSError("interrupted between the temporary file and the target")

        with (
            mock.patch.object(secret_store.os, "replace", side_effect=fail_replace),
            self.assertRaises(SecretStoreError),
        ):
            materialize_secrets(self.instance_dir)

        self.assertEqual(self.target.read_bytes(), before)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)
        leftovers = [path.name for path in self.target.parent.iterdir()]
        self.assertEqual(leftovers, [self.target.name])

    def test_the_generated_file_passes_the_installation_validator(self) -> None:
        materialize_secrets(self.instance_dir)
        values = installation.read_runtime_env(self.instance_dir, str(self.target))
        self.assertEqual(
            values,
            {
                "EXAMPLE_API_TOKEN": "1f2e3d4c5b6a==",
                "EXAMPLE_API_USER": "ummanu",
                "EXAMPLE_URL": "https://board.example.invalid/rpc",
            },
        )

    def test_import_then_materialize_reproduces_the_original_bytes(self) -> None:
        materialize_secrets(self.instance_dir)
        self.assertEqual(self.target.read_bytes(), self.source.read_bytes())
        # Not by accident of sorting: the file's order is not alphabetical, and
        # the last line carries '=' padding that a re-split would mangle.
        written = self.target.read_text(encoding="utf-8").splitlines()
        self.assertEqual(written[0].split("=", 1)[0], "EXAMPLE_URL")
        self.assertNotEqual(written, sorted(written))
        self.assertTrue(written[-1].endswith("1f2e3d4c5b6a=="))

    def test_a_reordered_source_moves_the_lines_and_nothing_else(self) -> None:
        materialize_secrets(self.instance_dir)
        before = self.store_state()
        reordered = "".join(reversed(LIVE_RUNTIME_ENV.splitlines(keepends=True)))
        self.source.write_text(reordered, encoding="utf-8")
        result = self.do_import()
        # Same values, new layout: only the catalog moves, no envelope is resealed.
        self.assertEqual(result.created, ())
        # The middle line did not move, so only the two that swapped are updated.
        self.assertEqual(result.updated, ("example_api_token", "example_url"))
        self.assertEqual(result.unchanged, ("example_api_user",))
        self.assertEqual(self.changed_since(before), ["secrets/catalog.yaml"])
        materialize_secrets(self.instance_dir)
        self.assertEqual(self.target.read_text(encoding="utf-8"), reordered)

    def test_a_file_target_is_written_where_the_catalog_says(self) -> None:
        elsewhere = Path(self.tmpdir.name) / "other" / "app.env"
        set_secret(
            self.instance_dir,
            secret_id="app.token",
            value=b"app-value",
            scope="project:ummanu",
            purpose="app credentials",
            environment="APP_TOKEN",
            materialize={"target": "file", "path": str(elsewhere)},
            actor="tester",
        )
        results = materialize_secrets(self.instance_dir)
        self.assertEqual({result.path for result in results}, {self.target, elsewhere})
        self.assertEqual(elsewhere.read_text(encoding="utf-8"), "APP_TOKEN=app-value\n")

        only_runtime = materialize_secrets(self.instance_dir, target="runtime-env")
        self.assertEqual([result.path for result in only_runtime], [self.target])

    def test_materialize_refuses_a_target_the_export_would_copy(self) -> None:
        inside = self.instance_dir / "persona" / "tracked.env"
        set_secret(
            self.instance_dir,
            secret_id="app.token",
            value=b"app-value",
            scope="installation",
            purpose="app credentials",
            environment="APP_TOKEN",
            materialize={"target": "file", "path": "persona/tracked.env"},
            actor="tester",
        )
        with self.assertRaises(SecretStoreError) as caught:
            materialize_secrets(self.instance_dir, target="file")
        self.assertIn("snapshot export copies", str(caught.exception))
        self.assertFalse(inside.exists())

        # The same target outside the export allowlist is written: exclusion is the allowlist, not Git.
        set_secret(
            self.instance_dir,
            secret_id="app.token",
            value=b"app-value",
            scope="installation",
            purpose="app credentials",
            environment="APP_TOKEN",
            materialize={"target": "file", "path": "local/tracked.env"},
            actor="tester",
        )
        materialize_secrets(self.instance_dir, target="file")
        self.assertEqual(
            (self.instance_dir / "local" / "tracked.env").read_text(encoding="utf-8"), "APP_TOKEN=app-value\n"
        )

    def test_a_value_with_a_newline_never_becomes_an_env_line(self) -> None:
        set_secret(
            self.instance_dir,
            secret_id="app.key",
            value=b"-----BEGIN KEY-----\nbody\n",
            scope="installation",
            purpose="pem body",
            environment="APP_KEY",
            materialize={"target": "runtime-env"},
            actor="tester",
        )
        with self.assertRaises(SecretStoreValidationError) as caught:
            materialize_secrets(self.instance_dir)
        self.assertIn("newline", str(caught.exception))
        self.assertFalse(self.target.exists())

    def test_two_secrets_claiming_one_variable_stop_the_write(self) -> None:
        materialize_secrets(self.instance_dir)
        before = self.target.read_bytes()
        set_secret(
            self.instance_dir,
            secret_id="service.url.copy",
            value=b"https://other.example.invalid/rpc",
            scope="installation",
            purpose="a second claim on the same variable",
            environment="EXAMPLE_URL",
            materialize={"target": "runtime-env"},
            actor="tester",
        )
        with self.assertRaises(SecretStoreStateError) as caught:
            materialize_secrets(self.instance_dir)
        self.assertIn("EXAMPLE_URL", str(caught.exception))
        self.assertEqual(self.target.read_bytes(), before)

    def test_a_secret_with_no_materialize_record_stays_in_the_store(self) -> None:
        set_secret(
            self.instance_dir,
            secret_id="offline.note",
            value=b"not an env var",
            scope="installation",
            purpose="kept for recovery only",
            actor="tester",
        )
        materialize_secrets(self.instance_dir)
        self.assertNotIn("offline", self.target.read_text(encoding="utf-8"))


class ObservabilityCase(SecretStoreCase):
    """`store_health` and `store_findings` are the surface `status`/`doctor` use."""

    def test_uninitialized_store_is_absent_and_finding_free(self) -> None:
        health = secret_store.store_health(self.instance_dir)
        self.assertEqual(
            health,
            {
                "initialized": False,
                "secret_count": 0,
                "last_modified_at": None,
                "installation_key": {"present": False, "usable": None},
                "materialize": [],
            },
        )
        self.assertEqual(secret_store.store_findings(self.instance_dir), ())

    def test_healthy_store_reports_counts_and_no_findings(self) -> None:
        self.initialize()
        set_secret(
            self.instance_dir,
            secret_id="service.api-token",
            value=b"token-value",
            scope="installation",
            purpose="board api",
            environment="EXAMPLE_API_TOKEN",
            materialize={"target": "runtime-env"},
            actor="tester",
        )
        health = secret_store.store_health(self.instance_dir)
        self.assertTrue(health["initialized"])
        self.assertEqual(health["secret_count"], 1)
        self.assertIsNotNone(health["last_modified_at"])
        self.assertEqual(health["installation_key"], {"present": True, "usable": True})
        self.assertEqual(health["materialize"], [{"target": "runtime-env", "path": None, "count": 1}])
        self.assertEqual(secret_store.store_findings(self.instance_dir), ())

    def test_health_never_carries_a_value_or_the_recovery_phrase(self) -> None:
        self.initialize()
        set_secret(
            self.instance_dir,
            secret_id="service.api-token",
            value=b"super-secret-value",
            scope="installation",
            purpose="board api",
            actor="tester",
        )
        dump = json.dumps(secret_store.store_health(self.instance_dir))
        self.assertNotIn("super-secret-value", dump)
        self.assertNotIn(self.phrase, dump)

    def test_initialized_empty_store_gives_no_finding_for_a_missing_key(self) -> None:
        self.initialize()
        (self.instance_dir / "secrets" / KEY_NAME).unlink()
        self.assertEqual(secret_store.store_findings(self.instance_dir), ())
        self.assertEqual(
            secret_store.store_health(self.instance_dir)["installation_key"],
            {"present": False, "usable": None},
        )

    def test_missing_key_with_a_non_empty_catalog_is_a_finding(self) -> None:
        self.initialize()
        set_secret(
            self.instance_dir,
            secret_id="service.api-token",
            value=b"token-value",
            scope="installation",
            purpose="board api",
            actor="tester",
        )
        (self.instance_dir / "secrets" / KEY_NAME).unlink()
        findings = secret_store.store_findings(self.instance_dir)
        self.assertEqual(len(findings), 1)
        self.assertIn("installation key is missing or unusable", findings[0])
        self.assertEqual(
            secret_store.store_health(self.instance_dir)["installation_key"],
            {"present": False, "usable": None},
        )

    def test_wide_key_permissions_are_a_finding_even_with_an_empty_catalog(self) -> None:
        self.initialize()
        key_path = self.instance_dir / "secrets" / KEY_NAME
        os.chmod(key_path, 0o644)
        findings = secret_store.store_findings(self.instance_dir)
        self.assertEqual(len(findings), 1)
        self.assertIn("permissions are too broad", findings[0])
        self.assertEqual(
            secret_store.store_health(self.instance_dir)["installation_key"],
            {"present": True, "usable": False},
        )

    def test_wide_key_permissions_do_not_duplicate_the_unusable_finding(self) -> None:
        self.initialize()
        set_secret(
            self.instance_dir,
            secret_id="service.api-token",
            value=b"token-value",
            scope="installation",
            purpose="board api",
            actor="tester",
        )
        os.chmod(self.instance_dir / "secrets" / KEY_NAME, 0o644)
        findings = secret_store.store_findings(self.instance_dir)
        self.assertEqual(len(findings), 1)
        self.assertIn("permissions are too broad", findings[0])

    def test_catalog_value_divergence_is_a_finding(self) -> None:
        self.initialize()
        set_secret(
            self.instance_dir,
            secret_id="service.api-token",
            value=b"token-value",
            scope="installation",
            purpose="board api",
            actor="tester",
        )
        (self.instance_dir / "secrets" / "values" / "service.api-token.enc.json").unlink()
        findings = secret_store.store_findings(self.instance_dir)
        self.assertIn("secret store: service.api-token: catalogued with no value", findings)

    def test_missing_key_params_with_a_non_empty_catalog_is_a_finding(self) -> None:
        """Reproduces a store where `init` ran and a secret was set, then only
        installation-key.json was lost. Catalog and envelope survive, but the
        raw key can no longer be checked against a verifier, so it must read
        as unusable, not as a store that was never initialized."""
        self.initialize()
        set_secret(
            self.instance_dir,
            secret_id="service.api-token",
            value=b"token-value",
            scope="installation",
            purpose="board api",
            actor="tester",
        )
        (self.instance_dir / "secrets" / KEY_PARAMS_NAME).unlink()
        health = secret_store.store_health(self.instance_dir)
        self.assertFalse(health["initialized"])
        self.assertEqual(health["secret_count"], 1)
        self.assertEqual(health["installation_key"], {"present": True, "usable": False})
        findings = secret_store.store_findings(self.instance_dir)
        self.assertEqual(len(findings), 1)
        self.assertIn("installation key is missing or unusable", findings[0])

    def test_corrupted_key_params_version_does_not_leak_the_installation_key(self) -> None:
        """A key-params file with the raw installation key stuffed into its
        `version` field (as `restore_installation_key` or manual tampering
        could produce) must not have that value echoed back by a finding."""
        self.initialize()
        set_secret(
            self.instance_dir,
            secret_id="service.api-token",
            value=b"token-value",
            scope="installation",
            purpose="board api",
            actor="tester",
        )
        key_path = self.instance_dir / "secrets" / KEY_NAME
        raw_key = key_path.read_text(encoding="utf-8").strip()
        params_path = self.instance_dir / "secrets" / KEY_PARAMS_NAME
        params = json.loads(params_path.read_text(encoding="utf-8"))
        params["version"] = raw_key
        params_path.write_text(json.dumps(params), encoding="utf-8")
        findings = secret_store.store_findings(self.instance_dir)
        self.assertEqual(len(findings), 1)
        self.assertNotIn(raw_key, findings[0])
        self.assertIn("installation key is missing or unusable", findings[0])

    def test_missing_catalog_with_key_params_present_is_a_finding_not_absence(self) -> None:
        self.initialize()
        (self.instance_dir / "secrets" / CATALOG_NAME).unlink()
        health = secret_store.store_health(self.instance_dir)
        self.assertFalse(health["initialized"])
        self.assertEqual(health["secret_count"], 0)
        findings = secret_store.store_findings(self.instance_dir)
        self.assertEqual(len(findings), 1)
        self.assertIn("secret store:", findings[0])

    def test_malformed_catalog_is_a_finding_not_a_crash(self) -> None:
        self.initialize()
        (self.instance_dir / "secrets" / CATALOG_NAME).write_text("bad: catalog\n", encoding="utf-8")
        findings = secret_store.store_findings(self.instance_dir)
        self.assertEqual(len(findings), 1)
        self.assertIn("secret store:", findings[0])
        health = secret_store.store_health(self.instance_dir)
        self.assertTrue(health["initialized"])

    def test_a_malformed_scalar_does_not_leak_its_content_into_a_finding(self) -> None:
        self.initialize()
        sentinel = "sentinel-secret-do-not-leak"
        (self.instance_dir / "secrets" / CATALOG_NAME).write_text(
            f'version: 1\nsecrets: "{sentinel}\n', encoding="utf-8"
        )
        findings = secret_store.store_findings(self.instance_dir)
        self.assertEqual(len(findings), 1)
        self.assertIn("secret store:", findings[0])
        for finding in findings:
            self.assertNotIn(sentinel, finding)


class CatalogSchemaCase(unittest.TestCase):
    """The materialization record is only as good as the schema that guards it."""

    def catalog(self, entry: dict) -> dict:
        return {
            "version": secret_store.CATALOG_VERSION,
            "secrets": [
                {
                    "id": "example_url",
                    "scope": "installation",
                    "purpose": "board api",
                    "created_at": "2026-07-26T10:00:00Z",
                    **entry,
                }
            ],
        }

    def test_a_usable_record_validates(self) -> None:
        for instruction in (
            {"target": "runtime-env", "order": 0},
            {"target": "file", "path": "/etc/ummanu/app.env", "order": 7},
        ):
            with self.subTest(instruction=instruction):
                catalog = self.catalog({"environment": "EXAMPLE_URL", "materialize": instruction})
                self.assertEqual(validate(catalog, "secret-catalog", "catalog.yaml"), [])

    def test_a_record_nothing_could_act_on_is_rejected(self) -> None:
        cases = [
            {"environment": "EXAMPLE_URL", "materialize": {"target": "elsewhere", "order": 0}},
            {"environment": "EXAMPLE_URL", "materialize": {"target": "file", "order": 0}},
            {
                "environment": "EXAMPLE_URL",
                "materialize": {"target": "runtime-env", "path": "/etc/runtime.env", "order": 0},
            },
            # Without a variable name there is nothing to write on the left of '='.
            {"materialize": {"target": "runtime-env", "order": 0}},
            {
                "environment": "not-an-env-name",
                "materialize": {"target": "runtime-env", "order": 0},
            },
            # Without a line number the file layout is not recorded at all.
            {"environment": "EXAMPLE_URL", "materialize": {"target": "runtime-env"}},
            {"environment": "EXAMPLE_URL", "materialize": {"target": "runtime-env", "order": -1}},
            {
                "environment": "EXAMPLE_URL",
                "materialize": {"target": "runtime-env", "order": "first"},
            },
        ]
        for entry in cases:
            with self.subTest(entry=entry):
                self.assertNotEqual(validate(self.catalog(entry), "secret-catalog", "catalog.yaml"), [])


class SecretCliCase(SecretStoreCase):
    def run_cli(
        self,
        argv: list[str],
        stdin: bytes = b"",
        *,
        interactive: bool = True,
        clear_ok: bool = True,
    ) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        stream = io.TextIOWrapper(io.BytesIO(stdin), encoding="utf-8")
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch("sys.stdout", out))
            stack.enter_context(mock.patch("sys.stderr", err))
            stack.enter_context(mock.patch("sys.stdin", stream))
            if interactive:
                stack.enter_context(
                    mock.patch.object(secret_commands, "_stdin_and_stderr_are_interactive", return_value=True)
                )
            if clear_ok:
                stack.enter_context(
                    mock.patch.object(secret_commands, "_clear_screen_and_scrollback", return_value=True)
                )
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_corrupt_envelope_and_verifier_return_state_json_without_touching_the_target(self) -> None:
        self.initialize()
        target = Path(self.tmpdir.name) / "runtime.env"
        set_secret(
            self.instance_dir,
            secret_id="x",
            value=b"sentinel-secret-do-not-leak",
            scope="installation",
            purpose="service credential",
            actor="tester",
            environment="TOKEN",
            materialize={"target": "file", "path": str(target)},
        )
        target.write_bytes(b"UNCHANGED=value\n")
        value_path = secret_store.value_path(self.instance_dir, "x")
        params_path = secret_store.key_params_path(self.instance_dir)
        for path, section in ((value_path, "aead"), (params_path, "verifier")):
            with self.subTest(file=path.name):
                original = path.read_bytes()
                document = json.loads(original)
                document[section]["nonce"] = secret_store._b64(b"x")
                path.write_text(json.dumps(document), encoding="utf-8")
                code, output, errors = self.run_cli(
                    ["secret", "materialize", "--instance", str(self.instance_dir)]
                )
                self.assertEqual(code, 3)
                self.assertEqual(json.loads(output)["error"], "state")
                self.assertFalse(json.loads(output)["ok"])
                self.assertEqual(errors, "")
                self.assertNotIn("sentinel-secret-do-not-leak", output)
                self.assertEqual(target.read_bytes(), b"UNCHANGED=value\n")
                path.write_bytes(original)

    def test_init_shows_the_phrase_once_and_needs_it_confirmed(self) -> None:
        answers: list[str] = []
        phrase = " ".join(RECOVERY_WORDS[64:80])

        def fake_read_line(prompt: str) -> str:
            answers.append(prompt)
            if prompt.startswith("Type 'yes'"):
                return "yes"
            position = int(prompt.split()[1].rstrip(":")) - 1
            return phrase.split()[position]

        with (
            mock.patch.object(secret_commands, "generate_recovery_phrase", return_value=phrase),
            mock.patch.object(secret_commands, "_read_line", side_effect=fake_read_line),
        ):
            code, out, err = self.run_cli(["secret", "init", "--instance", str(self.instance_dir)])
        self.assertEqual(code, 0)
        # One "written it down" acknowledgement plus one question per confirmed word.
        self.assertEqual(len(answers), secret_store.CONFIRM_WORDS + 1)
        # The phrase is shown on stderr, so a redirected stdout cannot capture it.
        self.assertIn(phrase.split()[0], err)
        self.assertNotIn(phrase.split()[0], out)
        self.assertTrue(json.loads(out)["ok"])
        self.assertTrue(secret_store.is_initialized(self.instance_dir))

    def test_list_keeps_the_public_pretty_json_contract(self) -> None:
        with mock.patch.object(
            secret_commands,
            "list_secrets",
            return_value=({"id": "board.token", "scope": "installation"},),
        ):
            code, output, errors = self.run_cli(["secret", "list", "--instance", str(self.instance_dir)])

        self.assertEqual(code, 0)
        self.assertEqual(errors, "")
        self.assertEqual(
            output,
            '{\n  "ok": true,\n  "op": "list",\n  "secrets": [\n    {\n'
            '      "id": "board.token",\n      "scope": "installation"\n    }\n'
            "  ]\n}\n",
        )

    def test_list_keeps_secret_error_kinds_and_exit_codes(self) -> None:
        cases = (
            (SecretStoreStateError("locked catalog"), "state", 3),
            (SecretStoreError("cannot read catalog"), "runtime", 1),
            (state_repo.StateRepoError("git unavailable"), "runtime", 1),
        )
        for error, kind, code in cases:
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(secret_commands, "list_secrets", side_effect=error):
                    actual_code, output, errors = self.run_cli(
                        ["secret", "list", "--instance", str(self.instance_dir)]
                    )

                self.assertEqual(actual_code, code)
                self.assertEqual(errors, "")
                self.assertEqual(
                    json.loads(output),
                    {"ok": False, "op": "list", "error": kind, "message": str(error)},
                )

    def test_init_without_a_correct_confirmation_initializes_nothing(self) -> None:
        def fake_read_line(prompt: str) -> str:
            return "yes" if prompt.startswith("Type 'yes'") else "wrong"

        with mock.patch.object(secret_commands, "_read_line", side_effect=fake_read_line):
            code, out, err = self.run_cli(["secret", "init", "--instance", str(self.instance_dir)])
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(out)["ok"])
        self.assertFalse(secret_store.is_initialized(self.instance_dir))
        self.assertFalse((self.instance_dir / "secrets" / KEY_NAME).exists())
        # A wrong answer must not get the phrase printed again, nor the right word hinted.
        self.assertEqual(err.count("Recovery phrase."), 1)
        self.assertNotIn("wrong", out)

    def test_second_init_refuses_through_the_cli(self) -> None:
        self.initialize()
        code, out, _ = self.run_cli(["secret", "init", "--instance", str(self.instance_dir)])
        self.assertEqual(code, 3)
        self.assertIn("already initialized", json.loads(out)["message"])

    def test_init_refuses_when_not_interactive_before_generating_a_phrase(self) -> None:
        with mock.patch.object(secret_commands, "generate_recovery_phrase") as generate:
            code, out, err = self.run_cli(
                ["secret", "init", "--instance", str(self.instance_dir)], interactive=False
            )
        generate.assert_not_called()
        self.assertEqual(code, 2)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertIn("interactive", payload["message"])
        self.assertFalse(secret_store.is_initialized(self.instance_dir))
        combined_words = set(re.findall(r"[a-z]+", (out + err).lower()))
        self.assertFalse(combined_words & set(RECOVERY_WORDS))

    def test_init_refuses_if_the_screen_cannot_be_cleared(self) -> None:
        def fake_read_line(prompt: str) -> str:
            return "yes"

        with (
            mock.patch.object(secret_commands, "_read_line", side_effect=fake_read_line),
            mock.patch.object(secret_commands, "_clear_screen_and_scrollback", return_value=False),
        ):
            code, out, _ = self.run_cli(
                ["secret", "init", "--instance", str(self.instance_dir)], clear_ok=False
            )
        self.assertEqual(code, 2)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertIn("clear", payload["message"])
        self.assertFalse(secret_store.is_initialized(self.instance_dir))

    def test_init_sequence_is_show_then_acknowledge_then_clear_then_questions(self) -> None:
        order: list[str] = []

        def show(_phrase: str) -> None:
            order.append("show")

        def acknowledge() -> bool:
            order.append("acknowledge")
            return True

        def clear() -> bool:
            order.append("clear")
            return True

        def confirm(_phrase: str) -> bool:
            order.append("confirm")
            return True

        with (
            mock.patch.object(secret_commands, "_show_phrase", side_effect=show),
            mock.patch.object(secret_commands, "_acknowledge_written_down", side_effect=acknowledge),
            mock.patch.object(secret_commands, "_clear_screen_and_scrollback", side_effect=clear),
            mock.patch.object(secret_commands, "_confirm_phrase", side_effect=confirm),
        ):
            code, _out, _err = self.run_cli(
                ["secret", "init", "--instance", str(self.instance_dir)], clear_ok=False
            )
        self.assertEqual(code, 0)
        self.assertEqual(order, ["show", "acknowledge", "clear", "confirm"])

    def test_clear_screen_and_scrollback_refuses_on_a_dumb_terminal(self) -> None:
        err = io.StringIO()
        err.isatty = lambda: True
        with mock.patch("sys.stderr", err), mock.patch.dict(os.environ, {"TERM": "dumb"}):
            self.assertFalse(secret_commands._clear_screen_and_scrollback())

    def test_clear_screen_and_scrollback_refuses_when_stderr_is_not_a_tty(self) -> None:
        err = io.StringIO()
        with mock.patch("sys.stderr", err), mock.patch.dict(os.environ, {"TERM": "xterm"}):
            self.assertFalse(secret_commands._clear_screen_and_scrollback())

    def test_clear_screen_and_scrollback_writes_the_full_clear_sequence(self) -> None:
        err = io.StringIO()
        err.isatty = lambda: True
        with mock.patch("sys.stderr", err), mock.patch.dict(os.environ, {"TERM": "xterm-256color"}):
            self.assertTrue(secret_commands._clear_screen_and_scrollback())
        self.assertIn("\033[3J", err.getvalue())

    def test_set_reads_stdin_and_list_prints_metadata_only(self) -> None:
        self.initialize()
        code, out, _ = self.run_cli(
            [
                "secret",
                "set",
                "--instance",
                str(self.instance_dir),
                "--id",
                "service.api-token",
                "--scope",
                "installation",
                "--purpose",
                "board api",
                "--stdin",
            ],
            stdin=b"multi\nline\nvalue\n",
        )
        self.assertEqual(code, 0)
        self.assertNotIn("multi", out)
        self.assertEqual(json.loads(out)["bytes"], len(b"multi\nline\nvalue\n"))

        code, out, _ = self.run_cli(["secret", "list", "--instance", str(self.instance_dir)])
        self.assertEqual(code, 0)
        listed = json.loads(out)["secrets"]
        self.assertEqual([entry["id"] for entry in listed], ["service.api-token"])
        self.assertNotIn("multi", out)
        self.assertNotIn("value", out.replace("service.api-token", ""))
        self.assertEqual(read_secret(self.instance_dir, "service.api-token"), b"multi\nline\nvalue\n")

    def test_set_reads_a_binary_file_without_touching_argv(self) -> None:
        self.initialize()
        blob = bytes(range(256))
        source = Path(self.tmpdir.name) / "value.bin"
        source.write_bytes(blob)
        code, _out, _ = self.run_cli(
            [
                "secret",
                "set",
                "--instance",
                str(self.instance_dir),
                "--id",
                "binary.blob",
                "--scope",
                "project:ummanu",
                "--purpose",
                "raw bytes",
                "--file",
                str(source),
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(read_secret(self.instance_dir, "binary.blob"), blob)

    def test_no_command_takes_a_value_on_the_command_line(self) -> None:
        """There is no `--value`; the public CLI returns a structured usage error."""
        self.initialize()
        code, output, errors = self.run_cli(
            [
                "secret",
                "set",
                "--instance",
                str(self.instance_dir),
                "--id",
                "service.api-token",
                "--scope",
                "installation",
                "--purpose",
                "board api",
                "--value",
                "secret",
            ]
        )
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertEqual(json.loads(errors)["error"]["code"], "usage")
        self.assertEqual(list_secrets(self.instance_dir), ())

    def test_import_materialize_and_remove_through_the_cli(self) -> None:
        self.initialize()
        source = Path(self.tmpdir.name) / "runtime.env"
        source.write_text(LIVE_RUNTIME_ENV, encoding="utf-8")
        target = Path(self.tmpdir.name) / "out" / "runtime.env"

        code, out, _ = self.run_cli(
            [
                "secret",
                "import",
                "--instance",
                str(self.instance_dir),
                "--file",
                str(source),
                "--scope",
                "installation",
                "--purpose",
                "board api",
            ]
        )
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(len(report["created"]), 3)
        # The report names ids and nothing else; no value reaches stdout.
        self.assertNotIn("1f2e3d4c5b6a", out)
        self.assertNotIn("secretary-instance/secrets/values", out)

        with mock.patch.dict(os.environ, {"UMMANU_RUNTIME_ENV_FILE": str(target)}):
            code, out, _ = self.run_cli(["secret", "materialize", "--instance", str(self.instance_dir)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["targets"][0]["path"], str(target))
        self.assertNotIn("1f2e3d4c5b6a", out)
        self.assertEqual(target.read_text(encoding="utf-8"), LIVE_RUNTIME_ENV)

        code, out, _ = self.run_cli(
            ["secret", "remove", "--instance", str(self.instance_dir), "--id", "example_url"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            [entry["id"] for entry in list_secrets(self.instance_dir)],
            ["example_api_token", "example_api_user"],
        )

        code, out, _ = self.run_cli(
            ["secret", "remove", "--instance", str(self.instance_dir), "--id", "example_url"]
        )
        self.assertEqual(code, 3)
        self.assertIn("no secret named", json.loads(out)["message"])

    def test_a_file_target_needs_its_path_on_the_command_line(self) -> None:
        self.initialize()
        source = Path(self.tmpdir.name) / "runtime.env"
        source.write_text(LIVE_RUNTIME_ENV, encoding="utf-8")
        code, out, _ = self.run_cli(
            [
                "secret",
                "import",
                "--instance",
                str(self.instance_dir),
                "--file",
                str(source),
                "--scope",
                "installation",
                "--purpose",
                "board api",
                "--materialize",
                "file",
            ]
        )
        self.assertEqual(code, 2)
        self.assertIn("--materialize-path", json.loads(out)["message"])
        self.assertEqual(list_secrets(self.instance_dir), ())

    def test_list_before_init_says_so_instead_of_failing_obscurely(self) -> None:
        code, out, _ = self.run_cli(["secret", "list", "--instance", str(self.instance_dir)])
        self.assertEqual(code, 3)
        self.assertIn("not initialized", json.loads(out)["message"])


if __name__ == "__main__":
    unittest.main()
