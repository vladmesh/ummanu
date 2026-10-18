"""Verified model preparation with small files and a disposable hub cache."""

import hashlib
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType
from unittest import mock

from ummanu.memory import model


class ModelPreparationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cache = self.root / "cache"
        self.snapshot = self.root / "snapshot"
        self.snapshot.mkdir()
        self.files = {"model.onnx": b"graph", "model.onnx_data": b"external weights"}
        self.digests = {name: hashlib.sha256(data).hexdigest() for name, data in self.files.items()}
        for name, data in self.files.items():
            shard = self.root / "blobs" / name
            shard.mkdir(parents=True)
            blob = shard / "blob"
            blob.write_bytes(data)
            (self.snapshot / name).symlink_to(blob)
        hub = ModuleType("huggingface_hub")
        errors = ModuleType("huggingface_hub.errors")
        errors.LocalEntryNotFoundError = type("LocalEntryNotFoundError", (Exception,), {})
        self.download = mock.Mock(return_value=str(self.snapshot))
        hub.snapshot_download = self.download
        for patch in (
            mock.patch.dict(sys.modules, {"huggingface_hub": hub, "huggingface_hub.errors": errors}),
            mock.patch.object(model, "MODEL_FILES", self.digests),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.missing = errors.LocalEntryNotFoundError

    def test_shared_blobs_become_independent_regular_files_and_repeat_offline(self):
        target = model.prepare_model(self.cache)
        for name, data in self.files.items():
            path = target / name
            self.assertFalse(path.is_symlink())
            self.assertEqual(path.read_bytes(), data)
            self.assertNotEqual(path.stat().st_ino, (self.snapshot / name).stat().st_ino)
        self.download.side_effect = AssertionError("a prepared model needs no hub access")
        self.assertEqual(model.prepare_model(self.cache), target)
        (target / "model.onnx_data").write_bytes(b"independent")
        self.assertEqual((self.snapshot / "model.onnx_data").read_bytes(), self.files["model.onnx_data"])
        with self.assertRaisesRegex(model.ModelPreparationError, "checksum mismatch.*model.onnx_data"):
            model.prepare_model(self.cache)

    def test_empty_cache_downloads_the_exact_revision_and_file_set(self):
        self.download.side_effect = [self.missing(), str(self.snapshot)]
        target = model.prepare_model(self.cache)
        self.assertEqual((target / "model.onnx_data").read_bytes(), self.files["model.onnx_data"])
        arguments = {"repo_id": model.MODEL_REPOSITORY, "revision": model.MODEL_REVISION,
                     "cache_dir": str(self.cache), "allow_patterns": list(self.digests)}
        self.assertEqual(self.download.call_args_list,
                         [mock.call(**arguments, local_files_only=True), mock.call(**arguments)])

    def test_missing_and_corrupt_source_files_refuse_publication_and_allow_retry(self):
        weights = (self.snapshot / "model.onnx_data").resolve()
        for data, message in ((None, "could not prepare model file model.onnx_data"),
                              (b"broken", "checksum mismatch.*model.onnx_data")):
            with self.subTest(data=data):
                if data is None:
                    weights.unlink()
                else:
                    weights.write_bytes(data)
                with self.assertRaisesRegex(model.ModelPreparationError, message):
                    model.prepare_model(self.cache)
                weights.write_bytes(self.files["model.onnx_data"])
        target = model.prepare_model(self.cache)
        self.assertEqual((target / "model.onnx_data").read_bytes(), self.files["model.onnx_data"])

    def test_interrupted_copy_is_retried_without_publishing_partial_files(self):
        copy = model.shutil.copyfile

        def interrupted(source, target):
            Path(target).write_bytes(b"partial")
            raise OSError("interrupted")

        with (
            mock.patch.object(model.shutil, "copyfile", side_effect=interrupted),
            self.assertRaisesRegex(model.ModelPreparationError, "interrupted"),
        ):
            model.prepare_model(self.cache)
        stale = self.cache / "ummanu-models" / "multilingual-e5-large" / f".{model.MODEL_REVISION}.preparing"
        stale.mkdir()
        (stale / "model.onnx").write_bytes(b"killed copy")
        with mock.patch.object(model.shutil, "copyfile", wraps=copy):
            target = model.prepare_model(self.cache)
        self.assertEqual((target / "model.onnx").read_bytes(), self.files["model.onnx"])

    def test_concurrent_preparers_publish_one_complete_model(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            targets = list(executor.map(model.prepare_model, [self.cache, self.cache]))
        self.assertEqual(targets[0], targets[1])
        self.download.assert_called_once()
        self.assertEqual((targets[0] / "model.onnx_data").read_bytes(), self.files["model.onnx_data"])
