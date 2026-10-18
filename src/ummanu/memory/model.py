"""Load the released embedding contract independently of hub cache topology."""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

from ummanu._fsutil import directory_lock
from ummanu.memory import DEFAULT_MODEL

MODEL_REPOSITORY = "qdrant/multilingual-e5-large-onnx"
MODEL_REVISION = "66076b8dc6e367337e3e90e6fb309fb0f3addaf6"
MODEL_FILES = {
    "config.json": "1de8c3be1f344c0eefa4962480a006f8639f416dfafaa95a770e3cf4bceae6a4",
    "model.onnx": "1c09780c907c8a91a77a6ab1fd231f79e090d2907ca431223703dfebeed3d36c",
    "model.onnx_data": "0cf1883fee81c63819a44e2ba0efa51d4043d9759685a4ebebbde97e0623d15c",
    "special_tokens_map.json": "8c785abebea9ae3257b61681b4e6fd8365ceafde980c21970d001e834cf10835",
    "tokenizer.json": "f59925fcb90c92b894cb93e51bb9b4a6105c5c249fe54ce1c704420ac39b81af",
    "tokenizer_config.json": "f90024142df07163e5e6c5b9a6ad7c8c68b22a9112af11e3db4559a9ff90f737",
}


class ModelPreparationError(RuntimeError):
    """The pinned model cannot be prepared without changing its interpretation."""


def _verify_file(path: Path, expected: str) -> None:
    try:
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError as error:
        raise ModelPreparationError(f"model file unavailable: {path}: {error}") from error
    if actual != expected:
        raise ModelPreparationError(f"model file checksum mismatch: {path}: expected {expected}, got {actual}")


def prepare_model(cache_dir: str | Path) -> Path:
    """Publish verified regular files together; never modify hub snapshots or blobs."""
    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import LocalEntryNotFoundError

    cache = Path(cache_dir).expanduser().absolute()
    prepared = cache / "ummanu-models" / "multilingual-e5-large"
    target = prepared / MODEL_REVISION
    with directory_lock(prepared):
        if target.exists():
            for name, digest in MODEL_FILES.items():
                path = target / name
                if path.is_symlink():
                    raise ModelPreparationError(f"prepared model file must be regular: {path}")
                _verify_file(path, digest)
            return target

        arguments = {
            "repo_id": MODEL_REPOSITORY,
            "revision": MODEL_REVISION,
            "cache_dir": str(cache),
            "allow_patterns": list(MODEL_FILES),
        }
        try:
            snapshot = snapshot_download(**arguments, local_files_only=True)
        except LocalEntryNotFoundError:
            snapshot = snapshot_download(**arguments)

        staging = prepared / f".{MODEL_REVISION}.preparing"
        # A killed preparer leaves only this private directory; the directory lock excludes users.
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir()
        try:
            for name, digest in MODEL_FILES.items():
                path = staging / name
                try:
                    shutil.copyfile(Path(snapshot) / name, path)
                except OSError as error:
                    raise ModelPreparationError(f"could not prepare model file {name}: {error}") from error
                _verify_file(path, digest)
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
            descriptor = os.open(staging, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(staging, target)
            descriptor = os.open(prepared, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    return target


def load_text_embedding(model: str, cache_dir: str | Path, threads: int):
    """Use fastembed's released pooling and tokenizer with verified default-model files."""
    from fastembed import TextEmbedding

    options = {}
    if model.lower() == DEFAULT_MODEL.lower():
        options = {"specific_model_path": str(prepare_model(cache_dir)), "local_files_only": True}
    return TextEmbedding(model_name=model, cache_dir=str(cache_dir), threads=threads, **options)
