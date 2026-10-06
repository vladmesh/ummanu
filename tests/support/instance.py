"""A disposable instance configuration for offline status proofs."""

from pathlib import Path

from ummanu.config import validate_instance


def status_instance(root: Path):
    instance = root / "instance.yaml"
    instance.write_text(
        "version: 1\nname: test\n"
        f"data_dir: {root / 'data'}\n"
        "offsite:\n  instance_remote: git@example.invalid:x/y.git\n",
        encoding="utf-8",
    )
    return validate_instance(instance)
