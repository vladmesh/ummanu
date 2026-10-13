"""The knowledge writer, the secret store and local-file exclusion on a live root without Git.

Contract: docs/RECOVERY.md, "Writers" and "Security boundary". Every live root here is a plain
directory, not a Git work tree, and every assertion reads files and their bytes. The legacy tick and
the exporter cut that carry these writes are in `tests/test_snapshot_exporter.py`; the no-Git AST
rule is `tests/test_memory_canon.py::MemoryPathHasNoGitTests`.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from ummanu import knowledge_write, runtime_env, secret_store
from ummanu._fsutil import content_revision
from ummanu.board import store as board_store
from ummanu.board.sprint_close import SprintCloseoutPlan
from ummanu.infra import export_allowlist
from ummanu.infra.export_allowlist import SNAPSHOT_ALLOWLIST, is_exported
from ummanu.infra.github_credential import CHECKPOINT_CREDENTIAL_ID, CHECKPOINT_CREDENTIAL_PURPOSE
from ummanu.knowledge_write import (
    KnowledgeError,
    write_knowledge_directory,
    write_knowledge_document,
)
from ummanu.secret_store import (
    SecretStoreError,
    import_env_file,
    initialize_store,
    list_secrets,
    read_secret,
    remove_secret,
    restore_installation_key,
    set_secret,
    store_divergence,
    store_revision,
)
from ummanu.secret_words import RECOVERY_WORDS
from ummanu.sprints import SprintWriter

PHRASE = " ".join(RECOVERY_WORDS[:16])
DOCUMENT = "decisions/2026-10-02-no-git.md"
BODY = "# No Git\n\nThe writer writes files only.\n"
REPORT = "reports/ummanu-24"


def tree_bytes(root: Path) -> dict[str, bytes | None]:
    """Every entry under `root` by relative path: a file's bytes, a directory as None."""
    if not root.exists():
        return {}
    found: dict[str, bytes | None] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        found[relative] = None if path.is_dir() else path.read_bytes()
    return found


def fast_key_params() -> dict[str, Any]:
    return {
        "format": secret_store.KEY_PARAMS_FORMAT,
        "version": secret_store.KEY_PARAMS_VERSION,
        "kdf": {
            "id": secret_store.PHRASE_KDF_ID,
            "salt": secret_store._b64(b"0123456789abcdef"),
            "length": secret_store.KEY_LENGTH,
            "n": 2**8,
            "r": 8,
            "p": 1,
        },
    }


def no_child_process():
    return mock.patch.object(subprocess, "Popen", side_effect=AssertionError("a child process was started"))


class LiveRootCase(unittest.TestCase):
    """A live root that is a plain directory holding one config file."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.live = self.root / "instance"
        self.live.mkdir()
        (self.live / "instance.yaml").write_text("version: 1\n", encoding="utf-8")
        self.knowledge = self.live / "state" / "knowledge"
        self.secrets = self.live / "secrets"

    def tearDown(self) -> None:
        self.assertFalse((self.live / ".git").exists(), "no writer may make the live root a Git work tree")
        self.assertFalse((self.live / ".gitignore").exists(), "no writer may write .gitignore")

    def source(self, files: dict[str, str | bytes], name: str = "source") -> Path:
        root = self.root / name
        root.mkdir()
        for relative, data in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(data, bytes):
                path.write_bytes(data)
            else:
                path.write_text(data, encoding="utf-8")
        return root


class KnowledgeWithoutGitTests(LiveRootCase):
    def test_a_document_lands_on_a_plain_live_root_with_a_content_revision(self):
        with no_child_process():
            result = write_knowledge_document(self.live, document=DOCUMENT, actor="po", text=BODY)

        self.assertTrue(result.changed)
        self.assertEqual((self.knowledge / DOCUMENT).read_bytes(), BODY.encode("utf-8"))
        self.assertEqual(
            result.commit, content_revision({DOCUMENT: hashlib.sha256(BODY.encode("utf-8")).hexdigest()})
        )
        self.assertTrue(result.commit.startswith("sha256:"))

    def test_the_same_content_gives_the_same_revision_and_writes_nothing(self):
        first = write_knowledge_document(self.live, document=DOCUMENT, actor="po", text=BODY)
        before = (self.knowledge / DOCUMENT).stat()

        again = write_knowledge_document(self.live, document=DOCUMENT, actor="po", text=BODY)

        self.assertFalse(again.changed)
        self.assertEqual(again.commit, first.commit)
        after = (self.knowledge / DOCUMENT).stat()
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))
        elsewhere = self.root / "other"
        elsewhere.mkdir()
        self.assertEqual(write_knowledge_document(elsewhere, document=DOCUMENT, actor="po", text=BODY).commit, first.commit)
        edited = write_knowledge_document(self.live, document=DOCUMENT, actor="po", text=BODY + "More.\n")
        self.assertTrue(edited.changed)
        self.assertNotEqual(edited.commit, first.commit)

    def test_a_missing_live_root_is_refused(self):
        with self.assertRaises(KnowledgeError):
            write_knowledge_document(self.root / "absent", document=DOCUMENT, actor="po", text=BODY)

    def test_a_path_through_a_file_is_refused_by_the_preflight_before_anything_is_written(self):
        (self.live / "state").mkdir()
        (self.live / "state" / "knowledge").write_text("not a directory\n", encoding="utf-8")

        for check in (
            lambda: knowledge_write.check_knowledge_document(self.live, document=DOCUMENT, actor="po", text=BODY),
            lambda: write_knowledge_directory(
                self.live, directory=REPORT, actor="po", source_dir=self.source({"report.md": "x\n"})
            ),
        ):
            with self.assertRaises(knowledge_write.KnowledgeValidationError) as caught:
                check()
            self.assertEqual(caught.exception.reason, "path")
        self.assertEqual((self.live / "state" / "knowledge").read_text(encoding="utf-8"), "not a directory\n")

    def test_a_failed_document_write_leaves_the_knowledge_tree_as_it_was(self):
        with (
            mock.patch.object(knowledge_write, "_write_text_atomic", side_effect=RuntimeError("disk full")),
            self.assertRaises(KnowledgeError),
        ):
            write_knowledge_document(self.live, document="deep/er/one.md", actor="po", text=BODY)

        self.assertEqual(tree_bytes(self.knowledge), {})

    def test_a_directory_write_carries_the_revision_of_its_files_and_repeats_as_unchanged(self):
        files = {"report.md": "# Findings\n", "data/blob.bin": bytes(range(256))}
        with no_child_process():
            result = write_knowledge_directory(
                self.live, directory=REPORT, actor="dispatcher", source_dir=self.source(files)
            )

        expected = content_revision(
            {
                f"{REPORT}/{name}": hashlib.sha256(
                    data if isinstance(data, bytes) else data.encode("utf-8")
                ).hexdigest()
                for name, data in files.items()
            }
        )
        self.assertTrue(result.changed)
        self.assertEqual(result.commit, expected)
        self.assertEqual((self.knowledge / REPORT / "data" / "blob.bin").read_bytes(), bytes(range(256)))
        inode = (self.knowledge / REPORT).stat().st_ino

        again = write_knowledge_directory(
            self.live, directory=REPORT, actor="dispatcher", source_dir=self.source(files, "source-again")
        )

        self.assertFalse(again.changed)
        self.assertEqual(again.commit, expected)
        self.assertEqual((self.knowledge / REPORT).stat().st_ino, inode, "an unchanged directory is not rewritten")


    def test_equal_files_in_two_directories_give_two_revisions_and_a_rewrite_gives_the_same_one(self):
        files = {"report.md": "# Findings\n", "data/a.csv": "1\n"}

        first = write_knowledge_directory(
            self.live, directory="reports/ummanu-24", actor="dispatcher", source_dir=self.source(files, "one")
        )
        other = write_knowledge_directory(
            self.live, directory="reports/ummanu-25", actor="dispatcher", source_dir=self.source(files, "two")
        )
        again = write_knowledge_directory(
            self.live, directory="reports/ummanu-24", actor="dispatcher", source_dir=self.source(files, "three")
        )

        self.assertNotEqual(first.commit, other.commit)
        self.assertFalse(again.changed)
        self.assertEqual(again.commit, first.commit)


class KnowledgeDirectoryAtomicityTests(LiveRootCase):
    """Acceptance 3: a directory write that fails midway leaves the knowledge tree byte-identical."""

    def setUp(self) -> None:
        super().setUp()
        write_knowledge_document(self.live, document="decisions/keep.md", actor="po", text="keep\n")
        write_knowledge_directory(
            self.live,
            directory=REPORT,
            actor="dispatcher",
            source_dir=self.source({"report.md": "one\n", "data/a.csv": "1\n"}, "first"),
        )
        self.before = tree_bytes(self.knowledge)
        self.replacement = self.source({"report.md": "two\n", "data/b.csv": "2\n", "more/c.txt": "3\n"}, "second")

    def write(self, directory: str = REPORT):
        return write_knowledge_directory(self.live, directory=directory, actor="dispatcher", source_dir=self.replacement)

    def assert_unchanged(self) -> None:
        self.assertEqual(tree_bytes(self.knowledge), self.before)
        swap = self.live / knowledge_write.KNOWLEDGE_SWAP_RELATIVE
        self.assertEqual(list(swap.iterdir()) if swap.exists() else [], [])

    def test_a_failure_while_staging_the_new_files(self):
        real = Path.write_bytes
        calls = []

        def fail_on_second(path, data):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("disk full")
            return real(path, data)

        with mock.patch.object(Path, "write_bytes", fail_on_second), self.assertRaises(KnowledgeError):
            self.write()

        self.assert_unchanged()

    def test_a_failure_moving_the_new_directory_in(self):
        real = os.replace
        calls = []

        def fail_on_move_in(source, destination):
            calls.append(destination)
            if len(calls) == 2:
                raise OSError("rename failed")
            return real(source, destination)

        with (
            mock.patch.object(knowledge_write.os, "replace", side_effect=fail_on_move_in),
            self.assertRaises(KnowledgeError),
        ):
            self.write()

        self.assert_unchanged()

    def test_a_failure_writing_a_new_directory_removes_the_parents_it_created(self):
        real = Path.write_bytes

        def fail(path, data):
            raise OSError("disk full")

        with mock.patch.object(Path, "write_bytes", fail), self.assertRaises(KnowledgeError):
            self.write(directory="reports/new/nested")

        self.assert_unchanged()
        self.assertIs(Path.write_bytes, real)


class SecretStoreCase(LiveRootCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(secret_store, "_new_key_params", side_effect=fast_key_params)
        patcher.start()
        self.addCleanup(patcher.stop)

    def init(self):
        return initialize_store(self.live, phrase=PHRASE, actor="tester")

    def set(self, secret_id: str = "service.api-token", value: bytes = b"token-one", **overrides: Any):
        request: dict[str, Any] = {
            "secret_id": secret_id,
            "value": value,
            "scope": "installation",
            "purpose": "board api",
            "actor": "tester",
        }
        request.update(overrides)
        return set_secret(self.live, **request)

    def env_file(self, text: str) -> Path:
        path = self.root / "import.env"
        path.write_text(text, encoding="utf-8")
        return path

    def exported_store(self) -> dict[str, bytes | None]:
        return {name: data for name, data in tree_bytes(self.secrets).items() if is_exported(f"secrets/{name}")}


class SecretStoreWithoutGitTests(SecretStoreCase):
    def test_every_store_write_runs_on_a_plain_live_root_and_starts_no_child(self):
        source = self.env_file("ALPHA_TOKEN=alpha-value\nBETA_TOKEN=beta-value\n")
        with no_child_process():
            init = self.init()
            setting = self.set()
            github = set_secret(
                self.live,
                secret_id=CHECKPOINT_CREDENTIAL_ID,
                value=b"ghp_" + b"a" * 36,
                scope="installation",
                purpose=CHECKPOINT_CREDENTIAL_PURPOSE,
                actor="tester",
            )
            imported = import_env_file(
                self.live, source=source, scope="installation", purpose="imported", actor="tester"
            )
            removed = remove_secret(self.live, secret_id="alpha_token", actor="tester")
            restored = restore_installation_key(self.live, PHRASE)

        results = [init, setting, github, imported, removed]
        for result in results:
            self.assertTrue(result.commit.startswith("sha256:"), result)
        self.assertEqual(removed.commit, store_revision(self.live))
        self.assertEqual(len({result.commit for result in results}), len(results))
        self.assertEqual(restored, self.secrets / secret_store.KEY_NAME)
        self.assertEqual(read_secret(self.live, "beta_token"), b"beta-value")
        self.assertEqual(store_divergence(self.live), ())

    def test_the_revision_is_the_exported_files_and_nothing_else(self):
        self.init()
        self.set()
        revision = store_revision(self.live)
        (self.secrets / secret_store.KEY_NAME).write_text("not part of it\n", encoding="utf-8")
        (self.secrets / "notes.txt").write_text("not exported\n", encoding="utf-8")

        self.assertEqual(store_revision(self.live), revision)
        names = sorted(f"secrets/{name}" for name, data in self.exported_store().items() if data is not None)
        expected = content_revision(
            {name: hashlib.sha256((self.live / name).read_bytes()).hexdigest() for name in names}
        )
        self.assertEqual(revision, expected)

    def test_an_unchanged_set_writes_nothing_and_answers_the_same_revision(self):
        self.init()
        first = self.set()
        before = tree_bytes(self.secrets)

        with mock.patch.object(secret_store, "seal_value", side_effect=AssertionError("re-encrypted")):
            again = self.set()

        self.assertFalse(again.created)
        self.assertEqual(again.commit, first.commit)
        self.assertEqual(tree_bytes(self.secrets), before)

    def test_new_metadata_over_the_same_value_never_rewrites_the_envelope(self):
        self.init()
        self.set()
        envelope = secret_store.value_path(self.live, "service.api-token")
        before = envelope.read_bytes(), envelope.stat().st_ino

        with mock.patch.object(secret_store, "seal_value", side_effect=AssertionError("re-encrypted")):
            self.set(purpose="board api, described better")

        self.assertEqual((envelope.read_bytes(), envelope.stat().st_ino), before)
        self.assertEqual(list_secrets(self.live)[0]["purpose"], "board api, described better")

    def test_a_reimport_of_the_same_file_writes_nothing(self):
        self.init()
        source = self.env_file("ALPHA_TOKEN=alpha-value\n")
        first = import_env_file(self.live, source=source, scope="installation", purpose="imported", actor="tester")
        before = tree_bytes(self.secrets)

        again = import_env_file(self.live, source=source, scope="installation", purpose="imported", actor="tester")

        self.assertEqual(again.unchanged, ("alpha_token",))
        self.assertEqual(again.commit, first.commit)
        self.assertEqual(tree_bytes(self.secrets), before)


class SecretStoreAtomicityTests(SecretStoreCase):
    """Acceptance 3: a store write that fails midway leaves `secrets/` byte-identical."""

    def setUp(self) -> None:
        super().setUp()
        self.init()
        self.set("first.secret", b"first-value")
        self.before = tree_bytes(self.secrets)

    def fail_on(self, count: int, *, after: bool = False):
        """Make the `count`-th canon write fail, before or after it replaced its file."""
        from ummanu.memory.canon import CanonTransaction

        real = CanonTransaction.write
        calls = []

        def write(transaction, path, text):
            calls.append(path)
            if len(calls) == count and not after:
                raise RuntimeError("disk full")
            real(transaction, path, text)
            if len(calls) == count:
                raise RuntimeError("interrupted after the write")

        return mock.patch.object(CanonTransaction, "write", write)

    def assert_unchanged(self) -> None:
        self.assertEqual(tree_bytes(self.secrets), self.before)
        self.assertEqual(store_divergence(self.live), ())
        self.assertEqual(read_secret(self.live, "first.secret"), b"first-value")

    def test_set_failing_between_the_envelope_and_the_catalog(self):
        with self.fail_on(2), self.assertRaises(SecretStoreError):
            self.set("second.secret", b"second-value")

        self.assert_unchanged()
        self.assertFalse((self.secrets / "values" / "second.secret.enc.json").exists())

    def test_set_failing_after_the_catalog_was_replaced(self):
        with self.fail_on(2, after=True), self.assertRaises(SecretStoreError):
            self.set("second.secret", b"second-value")

        self.assert_unchanged()

    def test_import_failing_on_its_second_envelope(self):
        source = self.env_file("ALPHA_TOKEN=alpha-value\nBETA_TOKEN=beta-value\n")
        with self.fail_on(2), self.assertRaises(SecretStoreError):
            import_env_file(self.live, source=source, scope="installation", purpose="imported", actor="tester")

        self.assert_unchanged()

    def test_remove_failing_after_the_catalog_was_replaced(self):
        from ummanu.memory.canon import CanonTransaction

        with (
            mock.patch.object(CanonTransaction, "remove", side_effect=RuntimeError("unlink failed")),
            self.assertRaises(SecretStoreError),
        ):
            remove_secret(self.live, secret_id="first.secret", actor="tester")

        self.assert_unchanged()

    def test_a_crashed_write_is_restored_before_the_next_one_reads_the_store(self):
        from ummanu.memory.canon import CanonTransaction

        with (
            mock.patch.object(CanonTransaction, "rollback", lambda transaction: None),
            self.fail_on(2, after=True),
            self.assertRaises(SecretStoreError),
        ):
            self.set("second.secret", b"second-value")
        self.assertTrue((self.secrets / ".undo").is_dir(), "the crash left its undo area")
        self.assertNotEqual(tree_bytes(self.secrets), self.before)

        self.set("first.secret", b"first-value")

        self.assert_unchanged()

    def test_init_failing_midway_leaves_no_store_behind(self):
        fresh = self.root / "fresh"
        fresh.mkdir()
        with self.fail_on(2), self.assertRaises(SecretStoreError):
            initialize_store(fresh, phrase=PHRASE, actor="tester")

        self.assertFalse((fresh / "secrets").exists())


class ExclusionTests(LiveRootCase):
    """Acceptance 5: excluded means "not matched by the export allowlist"."""

    def write_runtime_env(self, relative: str) -> Path:
        path = self.live / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("EXAMPLE_API_TOKEN=opaque\n", encoding="utf-8")
        path.chmod(0o600)
        return path

    def test_the_local_credential_files_are_never_exported(self):
        for relative in ("secrets/installation.key", "runtime.env", "board-store.env", "/board-store.env"):
            with self.subTest(path=relative):
                self.assertFalse(is_exported(relative))
        for relative in ("secrets/catalog.yaml", "secrets/values/a.enc.json", "state/knowledge/x/y.md"):
            with self.subTest(path=relative):
                self.assertTrue(is_exported(relative))
        for relative in ("secrets/values/sub/a.enc.json", "secrets/.undo/journal.json", "state/.knowledge-swap/x", "../instance.yaml", ""):
            with self.subTest(path=relative):
                self.assertFalse(is_exported(relative))

    def test_runtime_env_outside_the_allowlist_is_accepted_without_git(self):
        self.write_runtime_env("runtime.env")

        with no_child_process():
            values = runtime_env.read_runtime_env(self.live)

        self.assertEqual(values, {"EXAMPLE_API_TOKEN": "opaque"})

    def test_runtime_env_at_an_exported_path_is_refused(self):
        path = self.write_runtime_env("persona/runtime.env")

        with self.assertRaisesRegex(runtime_env.RuntimeEnvError, "snapshot export copies"):
            runtime_env.read_runtime_env(self.live, str(path))
        self.assertEqual(runtime_env.read_runtime_env(self.live, str(path), require_ignored=False), {"EXAMPLE_API_TOKEN": "opaque"})

    def test_runtime_env_outside_the_live_root_is_accepted(self):
        outside = self.root / "elsewhere" / "runtime.env"
        outside.parent.mkdir()
        outside.write_text("A=b\n", encoding="utf-8")
        outside.chmod(0o600)

        self.assertEqual(runtime_env.read_runtime_env(self.live, str(outside)), {"A": "b"})

    def test_runtime_env_at_its_default_path_is_refused_once_the_allowlist_would_export_it(self):
        self.write_runtime_env("runtime.env")

        with (
            mock.patch.object(export_allowlist, "SNAPSHOT_ALLOWLIST", (*SNAPSHOT_ALLOWLIST, "runtime.env")),
            self.assertRaisesRegex(runtime_env.RuntimeEnvError, "snapshot export copies"),
        ):
            runtime_env.read_runtime_env(self.live)

    def test_the_board_store_materialises_on_a_plain_live_root_and_refuses_an_exported_path(self):
        with no_child_process():
            config = board_store.materialize_fresh(self.live)
            self.assertEqual(board_store.resolve(self.live), config)
            self.assertEqual(board_store.findings(self.live), [])

        other = self.root / "other"
        other.mkdir()
        exported = mock.patch.object(export_allowlist, "SNAPSHOT_ALLOWLIST", (*SNAPSHOT_ALLOWLIST, "board-store.env"))
        with exported:
            with self.assertRaisesRegex(board_store.BoardStoreError, "snapshot export copies"):
                board_store.materialize_fresh(other)
            self.assertEqual(sorted(os.listdir(other)), [])
            with self.assertRaisesRegex(board_store.BoardStoreError, "snapshot export copies"):
                board_store.resolve(self.live)
            self.assertIn("snapshot export copies", board_store.findings(self.live)[0])

    def test_the_installation_key_is_refused_once_the_allowlist_would_export_it(self):
        with (
            mock.patch.object(secret_store, "_new_key_params", side_effect=fast_key_params),
            mock.patch.object(export_allowlist, "SNAPSHOT_ALLOWLIST", (*SNAPSHOT_ALLOWLIST, "secrets/*.key")),
            self.assertRaisesRegex(SecretStoreError, "export allowlist"),
        ):
            initialize_store(self.live, phrase=PHRASE, actor="tester")
        self.assertFalse(self.secrets.exists())

    def test_materialize_refuses_a_target_the_export_would_copy(self):
        with mock.patch.object(secret_store, "_new_key_params", side_effect=fast_key_params):
            initialize_store(self.live, phrase=PHRASE, actor="tester")
        set_secret(
            self.live,
            secret_id="persona.token",
            value=b"opaque",
            scope="installation",
            purpose="written where it would leave the host",
            actor="tester",
            environment="PERSONA_TOKEN",
            materialize={"target": "file", "path": "persona/leak.env"},
        )

        with self.assertRaisesRegex(SecretStoreError, "snapshot export copies"):
            secret_store.materialize_secrets(self.live, target="file")
        self.assertFalse((self.live / "persona" / "leak.env").exists())


class RecoveryCheckoutChangesTests(LiveRootCase):
    """Recovery's "local changes" check: host-local files the export never copies are not changes.

    No product code writes a `.gitignore` entry for `installation.key`, `runtime.env` or
    `board-store.env` any more, so a recovered checkout holds them untracked; a second `recover`
    must still find the checkout clean, while every other change keeps refusing it.
    """

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.live), *args], text=True, capture_output=True, check=True
        ).stdout

    def setUp(self) -> None:
        super().setUp()
        (self.secrets).mkdir()
        (self.secrets / "catalog.yaml").write_text("version: 1\nsecrets: []\n", encoding="utf-8")
        (self.live / "persona").mkdir()
        (self.live / "persona" / "rules.md").write_text("Be brief.\n", encoding="utf-8")
        self.git("init", "--quiet", "--initial-branch", "main")
        self.git("add", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "--quiet", "-m", "x")
        for local in ("secrets/installation.key", "runtime.env", "board-store.env"):
            (self.live / local).write_text("local\n", encoding="utf-8")

    def changes(self) -> str:
        from ummanu.installation.clone import _checkout_changes

        return _checkout_changes(self.live, label="test")

    def test_untracked_host_local_files_are_not_local_changes(self):
        self.assertEqual(self.changes(), "")

    def test_every_other_change_still_counts(self):
        (self.live / "persona" / "new").mkdir()
        (self.live / "persona" / "new" / "voice.md").write_text("new\n", encoding="utf-8")
        (self.secrets / "values").mkdir()
        (self.secrets / "values" / "x.enc.json").write_text("{}\n", encoding="utf-8")
        (self.live / "instance.yaml").write_text("version: 2\n", encoding="utf-8")

        self.assertEqual(
            sorted(self.changes().splitlines()),
            [" M instance.yaml", "?? persona/new/voice.md", "?? secrets/values/x.enc.json"],
        )

    def tearDown(self) -> None:
        # This case is a Git work tree by construction; the base check is for the writers alone.
        pass


class CloseoutPlanRevisionTests(unittest.TestCase):
    """Acceptance 4: the sprint-close closeout step stores the content revision, and a close whose
    plan was staged with a Git commit id still completes."""

    def setUp(self) -> None:
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        self.live = Path(tmpdir.name) / "instance"
        self.live.mkdir()
        self.plan = SprintCloseoutPlan("closeouts/sprint-1.md", "# Closeout\n\nDeferred.\n", "sha")

    def writer(self, step_status: str) -> tuple[Any, list[tuple[str, str, dict[str, Any]]]]:
        audit: list[tuple[str, str, dict[str, Any]]] = []
        fake = SimpleNamespace(
            instance=str(self.live),
            transactions=SimpleNamespace(save=lambda document: None),
            reader=SimpleNamespace(show=lambda reference, include_cards=False: {"ref": reference}),
            audit=SimpleNamespace(
                stage=lambda request_id, step: audit.append(("stage", request_id, json.loads(json.dumps(step)))),
                append=lambda request_id, step: audit.append(("append", request_id, json.loads(json.dumps(step)))),
            ),
            _close_step_status=lambda request_id: step_status,
            _require_close_step_settled=lambda request_id: None,
            _event=lambda kind, role, actor, reference, request_id, payload, document: {
                "kind": kind,
                "ref": reference,
                "payload": payload,
            },
        )
        fake._instance_dir = lambda: SprintWriter._instance_dir(fake)
        fake._check_closeout_is_writable = lambda document, text, *, actor: SprintWriter._check_closeout_is_writable(
            fake, document, text, actor=actor
        )
        return fake, audit

    def close(self, fake: Any, plan: SprintCloseoutPlan) -> dict[str, Any]:
        document = {"request_id": "close-1", "intent": {"actor": "operator", "role": "po"}}
        payload = {"closeout": plan.to_document()}
        SprintWriter._write_closeout(fake, document, {"ref": "sprint:1"}, payload)
        return payload

    def test_the_written_closeout_step_and_plan_carry_the_content_revision(self):
        fake, audit = self.writer("missing")

        payload = self.close(fake, self.plan)

        expected = content_revision(
            {self.plan.document: hashlib.sha256(self.plan.text.encode("utf-8")).hexdigest()}
        )
        written = SprintCloseoutPlan.from_document(payload["closeout"])
        assert written is not None
        self.assertTrue(written.written)
        self.assertEqual(written.commit, expected)
        self.assertEqual(written.to_result()["commit"], expected)
        appended = [step for action, _request, step in audit if action == "append"]
        self.assertEqual([step["payload"]["commit"] for step in appended], [expected])
        self.assertEqual((self.live / "state" / "knowledge" / self.plan.document).read_text("utf-8"), self.plan.text)
        self.assertFalse((self.live / ".git").exists())

    def test_a_staged_plan_with_an_old_commit_id_still_completes(self):
        old_commit = "0123456789abcdef0123456789abcdef01234567"
        written_by_old_code = self.plan.mark_written(old_commit)

        fake, audit = self.writer("done")
        payload = self.close(fake, written_by_old_code)

        resumed = SprintCloseoutPlan.from_document(payload["closeout"])
        self.assertEqual(resumed, written_by_old_code)
        self.assertEqual(resumed.to_result(), {"document": self.plan.document, "commit": old_commit, "written": True})
        self.assertEqual(audit, [])

    def test_a_step_staged_by_old_code_but_not_settled_is_written_again_with_the_revision(self):
        staged_by_old_code = SprintCloseoutPlan.from_document({**self.plan.to_document(), "commit": "f" * 40})
        assert staged_by_old_code is not None
        fake, audit = self.writer("staged")

        payload = self.close(fake, staged_by_old_code)
        again = self.close(self.writer("staged")[0], staged_by_old_code)

        written = SprintCloseoutPlan.from_document(payload["closeout"])
        assert written is not None
        self.assertTrue(written.written)
        self.assertTrue(written.commit.startswith("sha256:"))
        self.assertEqual(SprintCloseoutPlan.from_document(again["closeout"]), written, "same content, same revision")
        self.assertEqual(audit[-1][2]["payload"]["changed"], True)


if __name__ == "__main__":
    unittest.main()
