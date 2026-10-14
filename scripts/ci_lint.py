#!/usr/bin/env python3
"""Run the pinned Ruff check on non-deleted Python paths in the candidate diff."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


def git(root: Path, *arguments: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(root), *arguments], stderr=subprocess.PIPE)


def require_candidate(root: Path, base: str, head: str) -> None:
    for sha in (base, head):
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("lint revisions must be full commit SHAs")
    if git(root, "rev-parse", "HEAD").decode().strip() != head:
        raise ValueError("lint checkout does not match the candidate SHA")


def candidate_base(root: Path, base: str, candidate: str) -> str:
    """The given base; for a manual run the candidate's parent, for an initial push the empty tree."""
    if base and base != "0" * 40:
        return base
    parents = git(root, "rev-list", "--parents", "-n", "1", candidate).decode().split()
    return (
        parents[1] if len(parents) > 1 else git(root, "hash-object", "-t", "tree", "--stdin").decode().strip()
    )


def changed_python(root: Path, base: str, head: str) -> list[str]:
    require_candidate(root, base, head)
    paths = git(root, "diff", "--name-only", "-z", "--diff-filter=ACMRT", base, head, "--", "*.py")
    return sorted(path.decode() for path in paths.split(b"\0") if path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-sha", default="")
    parser.add_argument("--candidate-sha", required=True)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    try:
        paths = changed_python(
            root, candidate_base(root, args.base_sha, args.candidate_sha), args.candidate_sha
        )
        if not paths:
            print("No changed Python files; Ruff has no applicable paths.")
            return 0
        print(f"Ruff checks {len(paths)} changed Python files.", flush=True)
        return subprocess.call([sys.executable, "-m", "ruff", "check", "--", *paths], cwd=root)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Cannot run candidate lint: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
