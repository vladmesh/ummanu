"""File-only characterization of the two real stores, with deterministic lock/effect seams."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from ummanu.webproto import runs, sprint_requests, store_io

BASE_SHA = "2347277b18f281505dfb82d39b6134035cdd003a"
BASE_HASHES = {
    "runs": "4e6c162bbabcc1d6186dfd69e7313bbc40c2fea367702acbfedda060dcaa66a4",
    "sprint_requests": "5d91fffe5011d70fd980f84619c20a9bf298ae53a5f3176453b70600a25bb1d7",
}


def base_modules(directory):
    """Load exact base store bytes from an explicit external developer/worker capture."""
    loaded = []
    for name, digest in BASE_HASHES.items():
        path = Path(directory) / f"{name}.py"
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError(f"incorrect {BASE_SHA} source for {name}")
        spec = importlib.util.spec_from_file_location(f"tests._request_index_base_{name}", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        # The historical sprint store imports the historical run store's exception, too.
        with mock.patch.dict(sys.modules, {"ummanu.webproto.runs": loaded[0]} if loaded else {}):
            spec.loader.exec_module(module)
        loaded.append(module)
    return loaded


def packets(modules=(runs, sprint_requests)):
    """Capture paths/bytes, values/types, exceptions and actual read/build/lock/write order."""
    result = {}

    @contextmanager
    def fixture(kind, name, request_id="request/雪"):
        module = modules[kind]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            store = module.RunStore(root) if kind == 0 else module.SprintRequestStore(root)
            events = []
            locked = False

            def normalize(value):
                return str(value).replace(str(root), "<data>")

            @contextmanager
            def lock(path):
                nonlocal locked
                assert not locked
                assert store.root.is_dir()
                if kind == 0:
                    assert store.requests.is_dir()
                locked = True
                events.append(["lock", normalize(path)])
                try:
                    yield
                finally:
                    events.append(["unlock", normalize(path)])
                    locked = False

            real_write = store_io.write_text_atomic

            def write(path, payload):
                assert locked
                events.append(["write", normalize(path), payload])
                real_write(path, payload)

            def build(run_id):
                assert locked
                events.append(["build", run_id])
                return module.ProductRun(run_id, request_id, "ummanu-1", "ummanu", "worker", "profile")

            def call(action):
                try:
                    value = action()
                    claimed = None
                    if isinstance(value, tuple):
                        value, claimed = value
                    packet = {
                        "type": type(value).__name__,
                        "value": value.to_json() if value else None,
                        "claimed": claimed,
                    }
                except (
                    store_io.RunStoreError,
                    OSError,
                    ValueError,
                    OverflowError,
                    TypeError,
                    AttributeError,
                ) as exc:
                    packet = {
                        "error": type(exc).__name__,
                        "message": normalize(exc),
                        "cause": type(exc.__cause__).__name__ if exc.__cause__ else None,
                        "context": type(exc.__context__).__name__ if exc.__context__ else None,
                        "suppress_context": exc.__suppress_context__,
                    }
                packet["files"] = {
                    normalize(p): p.read_bytes().hex() for p in sorted(root.rglob("*")) if p.is_file()
                }
                packet["directories"] = [normalize(p) for p in sorted(root.rglob("*")) if p.is_dir()]
                packet["root_exists"] = root.exists()
                packet["events"] = list(events)
                result[f"{'run' if kind == 0 else 'sprint'}/{name}/{len(steps)}"] = packet
                steps.append(packet)
                return packet

            def claim(operation="op", fingerprint="fp"):
                return (
                    store.claim(request_id, build, operation=operation, fingerprint=fingerprint)
                    if kind == 0
                    else store.claim(request_id, operation=operation, fingerprint=fingerprint, now=12.5)
                )

            original = store.by_request

            def lookup(*args, **kwargs):
                events.append(["lookup", locked, args[0], kwargs])
                return original(*args, **kwargs)

            steps = []
            with (
                mock.patch.object(module, "file_lock", lock),
                mock.patch.object(store_io, "write_text_atomic", write),
                mock.patch.object(store, "by_request", lookup),
                mock.patch.object(modules[0], "new_run_id", side_effect=["pr-one", "pr-two", "pr-three"]),
            ):
                if kind == 0:
                    original_get = store.get

                    def get(run_id):
                        events.append(["get", locked, run_id])
                        return original_get(run_id)

                    with mock.patch.object(store, "get", get):
                        yield store, root, call, claim
                else:
                    yield store, root, call, claim

    for kind in (0, 1):
        for request_id in ("request/雪", "", "../a/b", "x" * 10000, "\ud800"):
            with fixture(kind, f"id-{request_id!r}", request_id) as (store, root, call, claim):
                call(lambda request_id=request_id: store.by_request(request_id))
                call(claim)
                call(claim)
                call(
                    lambda request_id=request_id: store.by_request(
                        request_id, operation="other", fingerprint="other"
                    )
                )
                call(
                    lambda request_id=request_id: store.by_request(
                        request_id, operation="op", fingerprint="other"
                    )
                )
                call(lambda request_id=request_id: store.by_request(request_id, operation="", fingerprint=""))
                other = modules[kind].RunStore(root) if kind == 0 else modules[kind].SprintRequestStore(root)
                call(lambda other=other, request_id=request_id: other.by_request(request_id))
                if kind == 1:
                    call(lambda request_id=request_id: store.record_reference(request_id, ""))
                    call(lambda request_id=request_id: store.record_reference(request_id, "sprint:7"))
                    call(lambda request_id=request_id: store.record_reference(request_id, "sprint:999"))

        documents = [
            None,
            [],
            "scalar",
            3,
            False,
            {},
            {"run_id": "pr-one"},
            {"operation": "", "fingerprint": "", "run_id": "pr-one"},
            {"operation": "old", "fingerprint": "old", "run_id": "pr-one"},
            {"fingerprint": "old", "run_id": "pr-one"},
            {"operation": "old", "run_id": "pr-one"},
            {
                "request_id": 7,
                "operation": [],
                "fingerprint": False,
                "run_id": 7,
                "reference": [],
                "claimed_at": True,
            },
            {"claimed_at": "12"},
            {"claimed_at": 12.5},
            {"claimed_at": 10**400, "operation": "old", "fingerprint": "old"},
            {"run_id": "../invalid"},
        ]
        documents += [
            {
                "operation": value,
                "fingerprint": value,
                "reference": value,
                "claimed_at": value,
                "run_id": value,
            }
            for value in (None, False, 0, 2.5, [], {}, "")
        ]
        for number, payload in enumerate(documents):
            for target in ("missing", "valid", "corrupt") if kind == 0 else ("none",):
                with fixture(kind, f"json-{number}-{target}") as (store, root, call, claim):
                    index_root = store.requests if kind == 0 else store.root
                    index_root.mkdir(parents=True)
                    digest = hashlib.sha256("request/雪".encode()).hexdigest()
                    (index_root / f"{digest}.json").write_text(json.dumps(payload), encoding="utf-8")
                    if target in ("valid", "corrupt"):
                        run_payload = modules[0].ProductRun("pr-one", "request/雪", "ref", "p", "worker", "x")
                        (store.root / "pr-one.json").write_text(
                            json.dumps(run_payload.to_json()) if target == "valid" else "{", encoding="utf-8"
                        )
                    for operation, fingerprint in (("", ""), ("op", "fp"), ("", "fp"), ("old", "fp")):
                        call(
                            lambda operation=operation, fingerprint=fingerprint: store.by_request(
                                "request/雪", operation=operation, fingerprint=fingerprint
                            )
                        )

        for name, content in (("malformed", b"{"), ("utf8", b"\xff"), ("empty", b"")):
            with fixture(kind, name) as (store, root, call, claim):
                index_root = store.requests if kind == 0 else store.root
                index_root.mkdir(parents=True)
                digest = hashlib.sha256("request/雪".encode()).hexdigest()
                (index_root / f"{digest}.json").write_bytes(content)
                call(lambda: store.by_request("request/雪"))
        for error in (
            OSError("read refused"),
            PermissionError("permission refused"),
            FileNotFoundError("gone"),
        ):
            with (
                fixture(kind, type(error).__name__) as (store, root, call, claim),
                mock.patch.object(Path, "read_text", side_effect=error),
            ):
                call(lambda: store.by_request("request/雪"))
        with fixture(kind, "empty-directory") as (store, root, call, claim):
            store.root.mkdir(parents=True)
            call(lambda: store.by_request("request/雪"))
        if kind == 0:
            with fixture(kind, "builder-failure") as (store, root, call, claim):
                call(lambda: store.claim("request/雪", lambda rid: None))
                call(claim)
        with (
            fixture(kind, "prepare-failure") as (store, root, call, claim),
            mock.patch.object(Path, "mkdir", side_effect=PermissionError("mkdir refused")),
        ):
            call(claim)
        for failure in (1, 2) if kind == 0 else (1,):
            for error in (
                RuntimeError("disk refused"),
                OSError("disk refused"),
                store_io.RunStoreError("typed"),
            ):
                with fixture(kind, f"write-{failure}-{type(error).__name__}") as (store, root, call, claim):
                    real_write = store_io.write_text_atomic
                    count = 0

                    def fail(
                        path,
                        payload,
                        failure=failure,
                        error=error,
                        real_write=real_write,
                        kind=kind,
                        root=root,
                    ):
                        nonlocal count
                        # Failure 1/2 continues to address canonical/request writes. Derived card
                        # lookup interruption has its own file-only protocol fixtures.
                        if kind == 0 and Path(path).is_relative_to(root / "webproto/runs/refs"):
                            return real_write(path, payload)
                        count += 1
                        if count == failure:
                            # Record the attempted boundary even when no file reaches disk.
                            result[
                                f"{'run' if kind == 0 else 'sprint'}/write-attempt-{failure}-{type(error).__name__}"
                            ] = {
                                "path": str(path).replace(str(root), "<data>"),
                                "payload": payload,
                                "prior_files": sorted(
                                    str(p).replace(str(root), "<data>") for p in root.rglob("*.json")
                                ),
                            }
                            raise error
                        real_write(path, payload)

                    with mock.patch.object(store_io, "write_text_atomic", fail):
                        call(claim)
                    call(claim)
                    call(claim)
        if kind == 1:
            with fixture(kind, "reference-failure") as (store, root, call, claim):
                call(lambda: store.record_reference("request/雪", "sprint:7"))
                call(claim)
                with mock.patch.object(
                    store_io, "write_text_atomic", side_effect=RuntimeError("reference refused")
                ):
                    call(lambda: store.record_reference("request/雪", "sprint:7"))
                call(claim)
                call(lambda: store.record_reference("request/雪", "sprint:7"))
                call(lambda: store.record_reference("request/雪", "sprint:8"))
    return result
