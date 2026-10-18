"""Real fastembed/ORT loading and released-vector comparisons owned by integration-memory."""

import base64
import gc
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ummanu.memory import DEFAULT_MODEL, model

FIXTURES = Path(__file__).parent / "fixtures"


def exercise_external_model(root: Path, layout: str, early_hub: bool) -> None:
    if early_hub:
        importlib.import_module("huggingface_hub")
    memory_service = importlib.import_module("ummanu.memory_service")

    import numpy as np
    from fastembed import TextEmbedding
    from tokenizers import Tokenizer, models, pre_tokenizers

    from ummanu import memory_reindex, restore

    case = unittest.TestCase()
    cache = root / "data" / "memory" / "fastembed-cache"
    snapshot = cache / "models--qdrant--multilingual-e5-large-onnx" / "snapshots" / model.MODEL_REVISION
    snapshot.mkdir(parents=True)
    fixture = json.loads((FIXTURES / "external_embedding.json").read_text())
    tokenizer = Tokenizer(models.WordLevel({"[PAD]": 0, "[UNK]": 1, "alpha": 2, "beta": 3},
                                          unk_token="[UNK]"))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    contents = {
        "model.onnx": bytes.fromhex(fixture["model_hex"]),
        "model.onnx_data": bytes.fromhex(fixture["weights_hex"]),
        "config.json": b'{"pad_token_id":0}',
        "tokenizer_config.json": b'{"model_max_length":32,"pad_token":"[PAD]"}',
        "special_tokens_map.json": b'{"unk_token":"[UNK]","pad_token":"[PAD]"}',
        "tokenizer.json": tokenizer.to_str().encode(),
    }
    for name, data in contents.items():
        if layout == "shared":
            blob = root / "shared-blobs" / name / "blob"
            blob.parent.mkdir(parents=True)
            blob.write_bytes(data)
            (snapshot / name).symlink_to(blob)
        else:
            (snapshot / name).write_bytes(data)
    if layout == "shared":
        with case.assertRaisesRegex(Exception, "External data path escapes model directory"):
            TextEmbedding(DEFAULT_MODEL, specific_model_path=str(snapshot), threads=1)

    instance = root / "instance"
    canon = instance / "state" / "memory" / "facts"
    (canon / "global").mkdir(parents=True)
    for name in ("alpha", "beta"):
        (canon / "global" / f"{name}.md").write_text(f"---\nsource: fixture\n---\n{name}\n")
    before = {path: path.read_bytes() for path in canon.rglob("*.md")}
    memory = cache.parent
    index = memory / "index.sqlite"
    manifest = {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}
    with (
        mock.patch.object(model, "MODEL_FILES", manifest),
        mock.patch.multiple(memory_service, CANON=canon, CANON_EXPORT=memory / "export.ndjson",
                            DB_PATH=str(index), MODEL_CACHE_DIR=cache, DIM=4, THREADS=1,
                            MODEL=DEFAULT_MODEL, _embedder=None),
    ):
        case.assertEqual(memory_service.bootstrap_index(), 2)
        expected = np.asarray([1, 2, 3, 4], dtype=np.float32) / np.sqrt(30)
        np.testing.assert_allclose(memory_service.embed_doc("alpha"), expected, atol=1e-7)
        np.testing.assert_allclose(memory_service.embed_query("память"), expected, atol=1e-7)
        case.assertTrue(memory_service.search_ready())
        for _ in range(2):
            result = memory_reindex.rebuild(canon, memory / "export.ndjson", index, DEFAULT_MODEL, 4,
                                           cache_dir=cache, threads=1)
            case.assertEqual(result["parity"], {"expected": 2, "indexed": 2, "ok": True})
            memory_service._embedder = None
            case.assertEqual(memory_service.bootstrap_index(), 2)
        for _ in range(2):
            case.assertEqual(restore.rebuild_memory_index(root / "data", instance, dim=4,
                                                         threads=1, isolated=True), 2)
        case.assertEqual(memory_service.index_compatibility(index, DEFAULT_MODEL, 4), ("compatible", None))
        connection = memory_service.db(index)
        try:
            case.assertEqual(connection.execute("SELECT text FROM memories ORDER BY text").fetchall(),
                             [("alpha",), ("beta",)])
            vectors = connection.execute("SELECT embedding FROM vec_memories ORDER BY rowid").fetchall()
            for (vector,) in vectors:
                np.testing.assert_allclose(np.frombuffer(vector, dtype=np.float32), expected, atol=1e-7)
        finally:
            connection.close()
        prepared = model.prepare_model(cache)
        for damage in (None, b"corrupt weights"):
            original_index = index.read_bytes()
            shutil.rmtree(prepared)
            weights = (snapshot / "model.onnx_data").resolve()
            if damage is None:
                weights.unlink()
            else:
                weights.write_bytes(damage)
            with case.assertRaisesRegex(model.ModelPreparationError, "model.onnx_data"):
                memory_reindex.rebuild(canon, memory / "export.ndjson", index, DEFAULT_MODEL, 4,
                                       cache_dir=cache, threads=1)
            case.assertEqual(index.read_bytes(), original_index)
            with case.assertRaisesRegex(restore.RestoreError, "model.onnx_data"):
                restore.rebuild_memory_index(root / "data", instance, dim=4, threads=1, isolated=True)
            case.assertEqual(index.read_bytes(), original_index)
            memory_service._embedder = None
            with case.assertRaisesRegex(model.ModelPreparationError, "model.onnx_data"):
                memory_service.bootstrap_index()
            case.assertEqual(index.read_bytes(), original_index)
            case.assertFalse(memory_service.search_ready())
            weights.write_bytes(contents["model.onnx_data"])
            case.assertEqual(memory_service.bootstrap_index(), 2)
            prepared = model.prepare_model(cache)
        for name, data in contents.items():
            case.assertEqual((snapshot / name).read_bytes(), data)
            case.assertFalse((prepared / name).is_symlink())
    case.assertEqual({path: path.read_bytes() for path in before}, before)


class ExternalWeightsLoaderTests(unittest.TestCase):
    def test_real_loader_flat_and_shared_with_normal_and_early_hub_import(self):
        for layout in ("flat", "shared"):
            for early in (False, True):
                with self.subTest(layout=layout, early_hub=early), tempfile.TemporaryDirectory() as directory:
                    program = (
                        "from pathlib import Path; "
                        "from tests.test_memory_model_loader import exercise_external_model; "
                        "import sys; exercise_external_model(Path(sys.argv[1]),sys.argv[2],sys.argv[3]=='1')"
                    )
                    completed = subprocess.run(
                        [sys.executable, "-c", program, directory, layout, str(int(early))],
                        env={**os.environ, "HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_SHARED_BLOBS": "0"},
                        text=True, capture_output=True, timeout=120,
                    )
                    self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)


class ReleasedModelLoaderTests(unittest.TestCase):
    def test_cold_model_matches_production_vectors_and_flat_loader(self):
        import numpy as np
        from fastembed import TextEmbedding

        from ummanu import memory_service

        reference = json.loads((FIXTURES / "memory_production_vectors.json").read_text())
        corpus = [sample["text"] for sample in reference["samples"]]
        probes = ["memory scope and recovery", "Общая память и восстановление", "ذاكرة مشتركة", "共有メモリ"]
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "empty-cache"
            with mock.patch.multiple(memory_service, MODEL=DEFAULT_MODEL, MODEL_CACHE_DIR=cache,
                                     THREADS=1, _embedder=None):
                service_vectors = [memory_service.embed_doc(text) for text in corpus]
                query_vectors = [memory_service.embed_query(text) for text in probes]
                for sample, actual in zip(reference["samples"], service_vectors):
                    self.assertEqual(hashlib.sha256(sample["text"].encode()).hexdigest(), sample["text_sha256"])
                    raw = base64.b64decode(sample["float32_le_base64"])
                    self.assertEqual(hashlib.sha256(raw).hexdigest(), sample["vector_sha256"])
                    expected = np.frombuffer(raw, dtype="<f4")
                    np.testing.assert_allclose(actual, expected, atol=2e-5, rtol=2e-4)
                    print(json.dumps({"production_sample": sample["path"],
                                      "max_abs_error": float(np.max(np.abs(actual - expected)))}))
                memory_service._embedder = None
                gc.collect()
            prepared = model.prepare_model(cache)
            baseline = TextEmbedding(DEFAULT_MODEL, specific_model_path=str(prepared), threads=1)
            for text, actual in zip(corpus, service_vectors):
                output = baseline.model.onnx_embed([text])
                mask = output.attention_mask[..., None]
                mean = (output.model_output * mask).sum(axis=1) / mask.sum(axis=1)
                np.testing.assert_allclose(actual, memory_service._unit(mean[0]), atol=2e-5, rtol=2e-4)
                cls = memory_service._unit(output.model_output[0, 0])
                self.assertGreater(float(np.linalg.norm(actual - cls)), 0.01)
            for text, actual in zip(probes, query_vectors):
                expected = memory_service._unit(next(baseline.query_embed(text)))
                np.testing.assert_allclose(actual, expected, atol=2e-5, rtol=2e-4)
            del baseline
            gc.collect()
            documents = memory_service.build_document_embedder(DEFAULT_MODEL, cache, 1)
            for text, actual in zip(corpus, service_vectors):
                np.testing.assert_allclose(documents(text), actual, atol=2e-5, rtol=2e-4)
            print(json.dumps({"revision": model.MODEL_REVISION,
                              "files": model.MODEL_FILES,
                              "packages": {name: importlib.metadata.version(name) for name in
                                           ("fastembed", "onnxruntime", "huggingface-hub", "tokenizers", "numpy")}}))
