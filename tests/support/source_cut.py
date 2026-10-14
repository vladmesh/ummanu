"""File-only service materializer fixture; Git/process/host/probes are explicit doubles."""

from __future__ import annotations

import io
import os
from collections import Counter
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.fakes.upgrade import FakeUnitInstaller
from ummanu import upgrade
from ummanu.host_apply import UnitProcessIdentity
from ummanu.installation import materialize
from ummanu.installation.upgrade import checkout, fingerprints, services, types

MEMORY = "ummanu-memory.service"
WEB = "ummanu-web.service"
PO = "ummanu-po.service"
UNIT = b"[Service]\nEnvironment=MEMORY_MODEL=model/v1\nExecStart=/x --host 127.0.0.1 --port 8787\n"
IDENTITY = UnitProcessIdentity(123, 456, "invocation")


class PayloadCounts:
    """Observe actual opened streams and bytes, including descriptor acquisition and legacy reads."""

    def __init__(self, fixture):
        self.fixture = fixture
        self.opens = Counter()
        self.reads = Counter()
        self.bytes = Counter()
        self.feeds = Counter()
        self.metadata = Counter()
        self.git = Counter()
        self.descriptors = {}

    def install(self, stack: ExitStack):
        real_open, real_io, real_stat, real_feed = os.open, io.open, Path.lstat, fingerprints._feed

        def name(path):
            if isinstance(path, int):
                return self.descriptors.get(path)
            path = Path(path)
            if path.is_relative_to(self.fixture.product):
                return path.relative_to(self.fixture.product).as_posix()
            if path == self.fixture.snapshot:
                return "@data/heads.yaml"
            return None

        def opened(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            self.descriptors[fd] = name(path)
            return fd

        owner = self

        class Stream:
            def __init__(self, stream, relative):
                self.stream, self.relative = stream, relative

            def __enter__(self):
                self.stream.__enter__()
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def __getattr__(self, attr):
                return getattr(self.stream, attr)

            def read(self, *args):
                contents = self.stream.read(*args)
                owner.reads[self.relative] += 1
                owner.bytes[self.relative] += len(contents)
                return contents

        def io_open(path, mode="r", *args, **kwargs):
            stream = real_io(path, mode, *args, **kwargs)
            relative = name(path)
            if relative is not None and mode == "rb":
                self.opens[relative] += 1
                return Stream(stream, relative)
            return stream

        def lstat(path, *args, **kwargs):
            self.metadata[name(path) or "outside-product"] += 1
            return real_stat(path, *args, **kwargs)

        def feed(digest, relative, contents):
            self.feeds[relative] += 1
            return real_feed(digest, relative, contents)

        stack.enter_context(mock.patch.object(os, "open", side_effect=opened))
        stack.enter_context(mock.patch.object(io, "open", side_effect=io_open))
        stack.enter_context(mock.patch.object(Path, "lstat", lstat))
        stack.enter_context(mock.patch.object(fingerprints, "_feed", side_effect=feed))
        stack.enter_context(mock.patch.object(checkout, "_git", side_effect=lambda root, args: self.count_git(root, args)))
        stack.enter_context(mock.patch.object(fingerprints, "_git", side_effect=lambda root, args: self.count_git(root, args)))

    def count_git(self, root, args):
        self.git[" ".join(args)] += 1
        return self.fixture.git(root, args)

    def packet(self):
        return {key: dict(getattr(self, key)) for key in ("opens", "reads", "bytes", "feeds", "metadata", "git")}


class SourceFixture:
    def __init__(self, root: Path, n: int = 8, *, service_owner=services, entry=upgrade):
        self.root = root
        self.product = root / "product"
        self.data = root / "data"
        self.snapshot = self.data / "heads.yaml"
        self.owner, self.entry = service_owner, entry
        self.revision = "a" * 40
        self.sources = {
            **{f"src/app{i:03}.py": f"VALUE={i:03}\n".encode() for i in range(n - n // 4)},
            **{f"src/ummanu/schemas/{i:03}.json": f'{{"const":{i}}}\n'.encode() for i in range(n // 4)},
        }
        self.dependencies = {"pyproject.toml": b"[project]\n", "uv.lock": b"lock\n", "requirements.txt": b"req\n"}
        self.payloads = {
            **self.sources, **self.dependencies,
            "packaging/systemd/ummanu-web.service": UNIT,
            "packaging/systemd/ummanu-web-front.service": b"[Unit]\nPartOf=ummanu-web.service\n",
        }
        self.tracked = sorted(self.payloads)
        for relative, contents in self.payloads.items():
            path = self.product / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)
        # Dirty bytes, not a Git blob, must feed both wire domains for this schema.
        self.schema = self.product / "src/ummanu/schemas/000.json"
        self.schema.write_bytes(b'{"const":9}\n')
        self.sources["src/ummanu/schemas/000.json"] = b'{"const":9}\n'
        (self.product / "src/untracked.py").write_bytes(b"ignored\n")
        bytecode = self.product / "src/__pycache__/app.pyc"
        bytecode.parent.mkdir()
        bytecode.write_bytes(b"ignored\n")
        self.data.mkdir(parents=True)
        self.snapshot.write_bytes(b"heads snapshot\n")
        self.units = FakeUnitInstaller({name: UNIT for name in (MEMORY, WEB, PO)}, {MEMORY, WEB, PO})
        self.units.identities = {name: IDENTITY for name in (MEMORY, WEB, PO)}
        self.report = SimpleNamespace(ok=True, errors=[], instance_path=root / "instance/instance.yaml",
                                      data_dir=self.data, host={"unit_prefix": "ummanu-"})
        self.context = types.UpgradeContext(root / "instance", self.product, "main", False, self.units,
                                           pull=False, report=self.report)

    def git(self, root, args, **kwargs):
        if Path(root).resolve() != self.product.resolve():
            raise types.GitError("unexpected selected root")
        if args == ["rev-parse", "HEAD"]:
            return self.revision
        if args[0] == "ls-files":
            paths = args[2:]
            return "\n".join(name for name in self.tracked if any(name == p or name.startswith(p) for p in paths))
        raise AssertionError(args)

    def install(self, stack: ExitStack):
        owner = self.owner
        stack.enter_context(mock.patch.object(checkout, "_git", side_effect=self.git))
        stack.enter_context(mock.patch.object(fingerprints, "_git", side_effect=self.git))
        stack.enter_context(mock.patch.object(owner, "installed_pair", return_value=SimpleNamespace(snapshot=self.snapshot)))
        stack.enter_context(mock.patch.object(owner, "step_host", return_value=types.StepResult("host", "unchanged")))
        stack.enter_context(mock.patch.object(owner.role_skills, "audit", return_value={"ok": True}))
        stack.enter_context(mock.patch.object(owner, "assert_snapshot_current"))
        stack.enter_context(mock.patch.object(owner, "installed_heads"))
        stack.enter_context(mock.patch.object(owner, "probe_memory"))
        stack.enter_context(mock.patch.object(owner, "probe_web", return_value=200))
        stack.enter_context(mock.patch.object(owner.os, "getpid", return_value=IDENTITY.pid))
        stack.enter_context(mock.patch.object(owner, "_process_start_ticks", return_value=IDENTITY.start_ticks))
        # Keep receipt identity byte-comparable across base/candidate fixture runs.
        stack.enter_context(mock.patch.object(self.units, "_new_identity", return_value=IDENTITY))

    def bind_po(self):
        return self.owner.write_po_process_receipt(self.data, self.product, environ={"INVOCATION_ID": IDENTITY.invocation_id})

    def steps(self):
        return tuple(getattr(self.owner, "step_" + name) for name in ("memory", "po", "web", "verify"))

    def run(self, steps=None):
        return self.entry.run_steps(self.context, self.steps() if steps is None else steps)

    def materialize(self, stack: ExitStack, *, recovery=False):
        stack.enter_context(mock.patch.object(materialize, "validate_instance", return_value=self.report))
        stack.enter_context(mock.patch.object(materialize, "check_product_runtime"))
        stack.enter_context(mock.patch.object(materialize, "resolve_runtime_owner", return_value=(None, self.root / "home")))
        stack.enter_context(mock.patch.object(materialize, "SystemdUnitInstaller", return_value=self.units))
        steps = (self.owner.step_host, *self.steps())
        stack.enter_context(mock.patch.object(materialize, "step_host", self.owner.step_host))
        stack.enter_context(mock.patch.object(materialize, "STEPS", steps))
        stack.enter_context(mock.patch.object(materialize, "run_steps", side_effect=lambda ctx, steps=steps: self.entry.run_steps(ctx, steps)))
        return materialize.materialize_host(self.root / "instance", self.product,
                                            before_host=(lambda ctx: None) if recovery else None)

    def receipts(self):
        return {name: path.read_bytes().decode() for name, path in {
            "memory": self.data / self.owner.MEMORY_PROCESS_RECEIPT_RELATIVE,
            "web": self.owner.web_process_receipt_path(self.context),
            "po": self.owner.po_client.process_receipt_path(self.data),
        }.items() if path.exists()}


def capture(root, n, *, service_owner=services, entry=upgrade, recovery=False):
    fixture = SourceFixture(root, n, service_owner=service_owner, entry=entry)
    counts = PayloadCounts(fixture)
    with ExitStack() as stack:
        fixture.install(stack)
        fixture.bind_po()  # Separate service process acquisition, outside the upgrade counter.
        counts.install(stack)
        result = fixture.materialize(stack, recovery=recovery)
    return {"steps": [asdict(step) for step in result.steps], "counts": counts.packet(),
            "receipts": fixture.receipts(), "source_bytes": sum(map(len, fixture.sources.values()))}
