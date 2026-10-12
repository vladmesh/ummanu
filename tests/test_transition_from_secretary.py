"""The one-shot transition over fixture installations (docs/RENAME.md §T3).

Every fixture lives under a temporary root: a home with the checkout, data dir, tools, Claude and
Codex files; an `/opt`, a units directory and a `bin` for the `orca` link; a bare origin whose
`main` carries the rename; and an instance repository. `git` runs for real; `sudo`, `systemctl`,
`docker` and the product CLIs go through a stub runner that records them (and performs the file
operations `sudo` would). The board store is a stand-in here: its steps run against PostgreSQL only
in the CI integration shard (`tests/test_transition_board.py`).

Class T (§T5): this file keeps both names, and it is not rewritten by the rename card, so it finds
the transition package by its path instead of importing it by the old name.
"""

from __future__ import annotations

import ast
import base64
import contextlib
import importlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from typing import Any, ClassVar

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = next(path.parent.parent.name for path in sorted((ROOT / "src").glob("*/transition/names.py")))
TRANSITION = ROOT / "src" / PACKAGE / "transition"

names = importlib.import_module(f"{PACKAGE}.transition.names")
context = importlib.import_module(f"{PACKAGE}.transition.context")
engine = importlib.import_module(f"{PACKAGE}.transition.engine")
rewrite = importlib.import_module(f"{PACKAGE}.transition.rewrite")
secret_rewrap = importlib.import_module(f"{PACKAGE}.transition.secret_rewrap")
steps = importlib.import_module(f"{PACKAGE}.transition.steps")
OLD, NEW = names.OLD, names.NEW

GIT_ENV = {
    "GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
}


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True,
        env={**os.environ, **GIT_ENV},
    )
    return result.stdout.strip()


def write(path: Path, text: str, mode: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mode is not None:
        path.chmod(mode)
    return path


class StubRunner(context.Runner):
    """`git` for real; privileged and product commands recorded, file operations under sudo performed."""

    FILE_COMMANDS = frozenset({"mv", "ln", "rm", "cp", "cat", "mkdir"})

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.fail: dict[str, int] = {}
        #: stdout by a marker in the command line; the freeze answers like the real command.
        self.answers: dict[str, str] = {"pause freeze": json.dumps({"status": "ok", "warnings": []}) + "\n"}

    def run(self, argv, *, cwd=None, check=True, env=None, timeout=900):  # type: ignore[override]
        argv = [str(part) for part in argv]
        if argv[0] == "git":
            return super().run(argv, cwd=cwd, check=check, env={**os.environ, **GIT_ENV}, timeout=timeout)
        self.calls.append(argv)
        command = argv[2:] if argv[:2] == ["sudo", "-n"] else argv
        for marker, code in list(self.fail.items()):
            if marker in " ".join(argv):
                del self.fail[marker]
                if check:
                    raise context.TransitionError(f"stub failure of {marker}")
                return subprocess.CompletedProcess(argv, code, "", "stub failure")
        if command[0] in self.FILE_COMMANDS:
            return super().run(command, cwd=cwd, check=check, timeout=timeout)
        if command[:2] == ["systemctl", "is-enabled"]:
            return subprocess.CompletedProcess(argv, 0, "enabled\n", "")
        if command[:2] == ["systemctl", "is-active"]:
            return subprocess.CompletedProcess(argv, 0, "active\n", "")
        for marker, stdout in self.answers.items():
            if marker in " ".join(argv):
                return subprocess.CompletedProcess(argv, 0, stdout, "")
        return subprocess.CompletedProcess(argv, 0, "ok\n", "")

    def privileged(self) -> list[list[str]]:
        return [call for call in self.calls if call[0] in ("sudo", "systemctl", "docker")]


class FakeBoard:
    """The board half of steps 1, 3 and 8 without PostgreSQL."""

    COUNTS: ClassVar[dict[str, int]] = {"tasks": 5, "issues": 3, "sprints": 2, "board_events": 9, "products": 1, "projects": 2,
              "repositories": 2, "product_projects": 1, "sprint_comments": 2}

    def __init__(self, markers: dict[str, int] | None = None) -> None:
        self.markers_found = {names.BASELINE_MARKER: 1, names.COUNTS_MARKER: 2} if markers is None else markers
        self.calls: list[str] = []
        self.fail_dump = 0

    def reads(self, ctx):
        board = self

        class Reads:
            def prepared_sprints(self) -> list[str]:
                board.calls.append("prepared_sprints")
                return ["sprint:1475"] if board.markers_found.get(names.BASELINE_MARKER) else []

            def markers(self, sprint: str) -> dict[str, int]:
                board.calls.append(f"markers {sprint}")
                return dict(board.markers_found)

        return Reads()

    def dump(self, ctx) -> dict[str, Any]:
        self.calls.append("dump")
        if self.fail_dump:
            self.fail_dump -= 1
            raise context.TransitionError("the dump was interrupted")
        directory = ctx.layout.state_dir / "board"
        directory.mkdir(parents=True, exist_ok=True)
        metadata = {"table_counts": dict(self.COUNTS), "alembic_head": "0001", "bytes": 4}
        (directory / "postgres.dump").write_bytes(b"dump")
        (directory / "dump.json").write_text(json.dumps(metadata), encoding="utf-8")
        return metadata

    def stop_old(self, ctx) -> None:
        self.calls.append("stop_old")

    def build_new(self, ctx, metadata) -> dict[str, Any]:
        self.calls.append("build_new")
        after = {name: count + names.ADDED_ROWS.get(name, 0) for name, count in metadata["table_counts"].items()}
        return {"provision": [], "restore": "restored", "translation": {"translated": True}, "counts_after": after}


class Installation:
    """A fixture installation for the retained transition (docs/RENAME.md §T3)."""

    def __init__(self, root: Path, *, extra_revision: bool = False, extra_merge: bool = False,
                 rename: bool = True) -> None:
        self.root = root
        self.home = root / "home"
        self.layout = context.Layout(
            home=self.home, instance=self.home / f"{names.INSTANCE_PROJECT}",
            opt_root=root / "opt", units_dir=root / "units", bin_dir=root / "bin",
        )
        self.origin = root / "origin.git"
        self._product(extra_revision=extra_revision, extra_merge=extra_merge, rename=rename)
        self._data()
        self._host()
        self._instance()
        self._clients()

    # -- product checkout and its origin ---------------------------------------------------------

    def _tree(self, root: Path, package: str, revisions: tuple[str, ...]) -> None:
        write(root / "src" / package / "dispatch" / "runtime_preflight.py", "PACKAGE = 'x'\n")
        for revision in revisions:
            write(root / "src" / package / "board" / "migrations" / "versions" / revision, "revision = 1\n")
        write(root / "pyproject.toml",
              f'[project]\nname = "{package}"\n[project.scripts]\n{package} = "{package}.cli:main"\n'
              '[project.optional-dependencies]\ndev = ["ruff"]\nmemory = ["fastembed"]\n')

    def _product(self, *, extra_revision: bool, extra_merge: bool, rename: bool) -> None:
        checkout = self.layout.product_root(OLD)
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(checkout)], check=True)
        self._tree(checkout, OLD.package, ("0001_initial.py",))
        write(checkout / ".gitignore", ".venv/\n*.egg-info/\n")
        git(checkout, "add", "-A")
        git(checkout, "commit", "-q", "-m", "pre-rename")
        git(checkout, "remote", "add", "origin", str(self.origin))
        git(checkout, "push", "-q", "origin", "main")
        self.pre_sha = git(checkout, "rev-parse", "HEAD")
        work = self.root / "work"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(work)], check=True)
        if extra_merge:
            git(work, "checkout", "-q", "-b", "other")
            write(work / "OTHER.md", "another card\n")
            git(work, "add", "-A")
            git(work, "commit", "-q", "-m", "another card")
            git(work, "checkout", "-q", "main")
            git(work, "merge", "-q", "--no-ff", "-m", "Merge another card", "other")
        if rename:
            git(work, "checkout", "-q", "-b", "rename")
            git(work, "mv", f"src/{OLD.package}", f"src/{NEW.package}")
            self._tree(work, NEW.package, ("0001_initial.py", "0002_added.py") if extra_revision else ("0001_initial.py",))
            git(work, "add", "-A")
            git(work, "commit", "-q", "-m", "rename the package")
            git(work, "checkout", "-q", "main")
            git(work, "merge", "-q", "--no-ff", "-m", "Merge the rename", "rename")
        else:
            write(work / "NOTES.md", "not a rename\n")
            git(work, "add", "-A")
            git(work, "commit", "-q", "-m", "not a rename")
        git(work, "push", "-q", "origin", "main")
        git(checkout, "fetch", "-q", "origin")
        self.target = git(checkout, "rev-parse", "origin/main")
        venv = checkout / ".venv"
        write(venv / "bin" / "python3", "#!/bin/sh\n", 0o755)
        write(venv / "bin" / OLD.package, "#!/bin/sh\n", 0o755)
        write(venv / "pyvenv.cfg", "home = /usr/bin\nexecutable = /usr/bin/python3\n")
        for distribution in ("ruff-0.1.dist-info", "fastembed-1.0.dist-info"):
            (venv / "lib" / "python3.12" / "site-packages" / distribution).mkdir(parents=True)
        (checkout / "src" / f"{OLD.package}.egg-info").mkdir()
        # A live card worktree in the data dir, moved with it and repaired afterwards.
        self.card_worktree = self.layout.data_dir(OLD) / "workspaces" / OLD.project_id / "card-1"
        self.card_worktree.parent.mkdir(parents=True)
        git(checkout, "worktree", "add", "-q", "--detach", str(self.card_worktree), "HEAD")

    # -- data dir, host roots, instance, clients -------------------------------------------------

    def _data(self) -> None:
        data = self.layout.data_dir(OLD)
        write(data / "dispatcher" / "pause.json", json.dumps({"mode": "drain"}))
        write(data / "dispatcher" / "production-state.json", json.dumps({
            "owner": OLD.dispatcher_owner, "records": {}, "post_merge_watches": {}, "e2e_after_merge": {},
            "resume_workspaces": {str(self.card_worktree): {"path": str(self.card_worktree)}},
        }))
        write(data / "data-manifest.json", json.dumps({"version": 1, "data_dir": str(data)}))
        write(data / "codex-home" / "config.toml",
              f'[projects."{self.layout.product_root(OLD)}"]\ntrust_level = "trusted"\n\n'
              f'[projects."{self.card_worktree}"]\ntrust_level = "trusted"\n')
        write(data / "webfront" / "Caddyfile", "{\n\tadmin off\n}\nhttps://front.example, https://192.0.2.1 {\n}\n")

    def _host(self) -> None:
        units = self.layout.units_dir
        prefix = OLD.unit_prefix
        write(units / f"{prefix}dispatcher-production.service",
              f"[Service]\nEnvironment={OLD.env_prefix}INSTANCE={self.layout.instance}\n")
        write(units / f"{prefix}dispatcher-production.timer", "[Timer]\n")
        write(units / f"{prefix}web.service", "[Service]\n")
        write(units / f"{prefix}supervisor.service", "[Service]\n")  # foreign: another product
        opt = self.layout.opt_dir(OLD)
        write(opt / "postgres-compose.yml", f"ports: ${{{OLD.env_prefix}DB_PORT}}\n", 0o600)
        write(opt / "orca" / "bin" / "orca-ide", "", 0o755)
        self.layout.bin_dir.mkdir(parents=True)
        self.layout.orca_link.symlink_to(opt / "orca" / "bin" / "orca-ide")
        tools = self.layout.tools_dir(OLD)
        write(tools / "uv" / "bin" / "uv", "", 0o755)
        self.layout.uv_link.parent.mkdir(parents=True)
        self.layout.uv_link.symlink_to(tools / "uv" / "bin" / "uv")

    def _instance(self) -> None:
        instance = self.layout.instance
        home = self.home
        subprocess.run(["git", "init", "-q", "-b", "main", str(instance)], check=True)
        write(instance / ".gitignore", "runtime.env\n/board-store.env\nsecrets/installation.key\n")
        write(instance / "instance.yaml",
              f"version: 1\nname: {OLD.instance_name}\n"
              f"description: Inventory for the {OLD.package} installation; {names.INSTANCE_PROJECT} keeps its name.\n"
              f"data_dir: {home / OLD.data_dir}\n# a comment that stays\nhost:\n  unit_prefix: {OLD.unit_prefix}\n"
              f"  foreign_units:\n    - {OLD.unit_prefix}supervisor.service\n  memory_dim: 1024\n")
        write(instance / "projects" / f"{OLD.project_id}.yaml",
              f"id: {OLD.project_id}\nrepo: {home / OLD.product_dir}\nremote: {OLD.remote}\n"
              f"orca_binding: {OLD.project_id}\ncurator_roots:\n  - /gone/one\n  - /gone/two\nenabled: true\n"
              f"adapter: {OLD.project_id}\n")
        write(instance / "adapters" / f"{OLD.project_id}.yaml", f"broad_check:\n  import_package: {OLD.package}\n")
        write(instance / "adapters" / "other.yaml", f'setup: mkdir -p "$HOME/{OLD.tools_dir}"\n')
        write(instance / "heads" / "heads.toml", f'probe = "python3 -P -m {OLD.package}.runtime.resource_probe"\n')
        write(instance / "secrets" / "catalog.yaml",
              f"secrets:\n- environment: {OLD.env_prefix}WEB_FRONT_PASSWORD\n  id: web-front-password\n"
              f"  materialize:\n    path: {home / OLD.data_dir}/webfront/owner-password.env\n", 0o600)
        self.key = os.urandom(32)
        write(instance / "secrets" / "installation.key", base64.b64encode(self.key).decode() + "\n", 0o600)
        params = secret_rewrap.rewrap_params(self.key, self._old_params(), NEW, OLD)
        write(instance / "secrets" / "installation-key.json", json.dumps(params, indent=2, sort_keys=True) + "\n")
        envelope = secret_rewrap.seal_value(self.key, "web-front-password", b"hunter2", OLD)
        write(instance / "secrets" / "values" / "web-front-password.enc.json", json.dumps(envelope), 0o600)
        facts = instance / "state" / "memory"
        write(facts / "facts" / OLD.memory_scope_dir / "fact.md", "a project fact\n")
        write(facts / "facts" / OLD.product_memory / "fact.md", "a product fact\n")
        write(facts / "packs" / f"{OLD.product_memory}.json", "{}\n")
        write(instance / "runtime.env", f"# comment\n{OLD.env_prefix}HEAD_IDLE_STALL_SECONDS=900\n"
              f"{names.DROPPED_RUNTIME_KEYS[0]}=sql\nOTHER=1\n", 0o600)
        values = ("127.0.0.1", "5432", OLD.db_name, OLD.db_owner, "pw-owner", OLD.db_app, "pw-app", OLD.db_read, "pw-read")
        write(instance / "board-store.env",
              "".join(f"{key}={value}\n" for key, value in zip(OLD.board_store_env, values, strict=True)), 0o600)
        git(instance, "add", "-A")
        git(instance, "commit", "-q", "-m", "instance")

    def _old_params(self) -> dict[str, Any]:
        """Key-file parameters sealed under the old names, built by re-wrapping a new-names file."""
        nonce = os.urandom(12)
        sealed = ChaCha20Poly1305(self.key).encrypt(nonce, NEW.verifier_plaintext, NEW.verifier_aad)
        return {"format": NEW.key_params_format, "version": 1,
                "kdf": {"id": "scrypt", "salt": "c2FsdA==", "length": 32, "n": 2, "r": 8, "p": 1},
                "verifier": {"id": "chacha20poly1305", "nonce": base64.b64encode(nonce).decode(),
                             "ciphertext": base64.b64encode(sealed).decode()}}

    def _clients(self) -> None:
        projects = self.layout.claude_projects
        self.claude = {old: new for old, new in rewrite.claude_moves(self.home, "sprint:1475")}
        for old in self.claude:
            write(projects / old / "memory" / "MEMORY.md", f"memory of {old}\n")
        write(projects / rewrite.claude_key(self.home / OLD.data_dir / "workspaces" / OLD.project_id / "x"), "")
        write(self.layout.claude_json, json.dumps({"projects": {
            str(self.layout.product_root(OLD)): {"hasTrustDialogAccepted": True},
            str(self.home / OLD.data_dir / "dispatcher" / "observer-root" / "observers"): {"hasTrustDialogAccepted": True},
            str(self.card_worktree): {"hasTrustDialogAccepted": True},
        }}, indent=2) + "\n")
        write(self.layout.codex_config,
              f'[projects."{self.layout.product_root(OLD)}"]\ntrust_level = "trusted"\n\n'
              f'[mcp_servers.po_memory]\ncommand = "{self.layout.product_root(OLD)}/.venv/bin/{OLD.package}-memory-po-bridge"\n'
              "args = []\n\n[tui]\ntheme = \"dark\"\n")

    # -- driving ----------------------------------------------------------------------------------

    def context(self, *, board: FakeBoard | None = None, runner: StubRunner | None = None, **options: Any):
        return context.Context(
            layout=self.layout, runner=runner or StubRunner(), journal=context.Journal.load(self.layout.journal_path),
            board=board or FakeBoard(), require_renamed=False, **options,
        )

    def bootstrap_checkout(self, *, target: str = "", keep_old_package: bool = False) -> None:
        """What the shell bootstrap does for step 5: fast-forward to the commit step 1 journalled."""
        root = self.layout.product_root(NEW)
        journal = context.Journal.load(self.layout.journal_path)
        git(root, "fetch", "-q", "origin")
        git(root, "merge", "-q", "--ff-only", target or journal.fact("target_sha"))
        git(root, "remote", "set-url", "origin", NEW.remote)
        shutil.rmtree(root / "src" / f"{OLD.package}.egg-info")
        if keep_old_package:
            write(root / "src" / OLD.package / "__pycache__" / "cli.cpython-312.pyc", "")
        else:
            shutil.rmtree(root / "src" / OLD.package, ignore_errors=True)
        state = self.layout.state_dir
        state.mkdir(exist_ok=True)
        os.rename(root / ".venv", state / "old-venv")
        write(root / ".venv" / "bin" / NEW.package, "#!/bin/sh\n", 0o755)

    def snapshot(self) -> dict[str, tuple[str, bytes | str]]:
        files: dict[str, tuple[str, bytes | str]] = {}
        for path in sorted(self.root.rglob("*")):
            relative = str(path.relative_to(self.root))
            if path.is_symlink():
                files[relative] = ("link", os.readlink(path))
            elif path.is_file():
                files[relative] = ("file", path.read_bytes())
            elif path.is_dir():
                files[relative] = ("dir", "")
        return files


def quietly(function, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()) as out:
        result = function(*args, **kwargs)
    return result, out.getvalue()


class FixtureTestCase(unittest.TestCase):
    def install(self, **options: Any) -> Installation:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Installation(Path(temporary.name), **options)


class PlanTests(FixtureTestCase):
    def test_plan_writes_nothing_and_names_every_step(self) -> None:
        install = self.install()
        before = install.snapshot()
        runner, board = StubRunner(), FakeBoard()
        code, output = quietly(engine.plan, install.context(runner=runner, board=board))
        self.assertEqual(code, 0, output)
        self.assertEqual(install.snapshot(), before)
        self.assertFalse(install.layout.journal_path.exists())
        self.assertFalse(install.layout.lock_path.exists())
        self.assertFalse(install.layout.state_dir.exists())
        self.assertEqual(runner.privileged(), [], "the plan ran a privileged command")
        self.assertEqual([call for call in board.calls if call in ("dump", "stop_old", "build_new")], [])
        for step in steps.STEPS:
            self.assertIn(f"Step {step.number} [{step.name}", output)
        self.assertIn("[ok] rename commit", output)
        self.assertIn(f"mv -T {install.layout.product_root(OLD)} {install.layout.product_root(NEW)}", output)
        self.assertIn("https://front.example", output)

    def test_plan_reports_unmet_preconditions_and_still_writes_nothing(self) -> None:
        install = self.install(rename=False)
        before = install.snapshot()
        code, output = quietly(engine.plan, install.context(board=FakeBoard(markers={})))
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] rename commit", output)
        self.assertIn("[FAIL] baseline and counts comments", output)
        self.assertEqual(install.snapshot(), before)


class PreconditionRefusalTests(FixtureTestCase):
    def refuse(self, install: Installation, expected: str, **options: Any) -> str:
        before_checkout = install.layout.product_root(OLD).exists()
        runner = StubRunner()
        with self.assertRaises(context.TransitionError) as caught:
            quietly(engine.apply, install.context(runner=runner, **options))
        self.assertIn(expected, str(caught.exception))
        journal = context.Journal.load(install.layout.journal_path)
        self.assertFalse(journal.done("preconditions"))
        self.assertEqual(install.layout.product_root(OLD).exists(), before_checkout)
        self.assertEqual(runner.privileged(), [], "a refused step 1 must not touch the host")
        return str(caught.exception)

    def test_missing_rename_commit(self) -> None:
        self.refuse(self.install(rename=False), "rename commit")

    def test_extra_alembic_revision_in_the_gap(self) -> None:
        self.refuse(self.install(extra_revision=True), "no new Alembic revision")

    def test_extra_merge_is_refused_unless_named(self) -> None:
        install = self.install(extra_merge=True)
        self.refuse(install, "only the rename merge")
        extra = git(install.layout.product_root(OLD), "rev-list", "--merges", "--first-parent",
                    f"{install.pre_sha}..origin/main").split()
        named = [merge for merge in extra if "another card" in git(install.layout.product_root(OLD), "log", "-1",
                                                                    "--format=%s", merge)]
        checks, _facts = steps.preconditions.git_checks(
            install.context(allow_extra_merges=(named[0][:12],)), install.layout.product_root(OLD)
        )
        self.assertTrue(all(check.ok for check in checks), [check.line() for check in checks])

    def test_live_worker_head(self) -> None:
        install = self.install()
        socket = write(install.layout.data_dir(OLD) / "heads" / "leaf" / "head.sock", "")
        state = install.layout.data_dir(OLD) / "dispatcher" / "production-state.json"
        payload = json.loads(state.read_text())
        payload["records"] = {f"{OLD.project_id}-7": {"handle": str(socket), "review_handle": ""}}
        state.write_text(json.dumps(payload))
        message = self.refuse(install, "no live worker or reviewer head")
        self.assertIn("no in-flight records", message)

    def test_observer_heads_are_allowed(self) -> None:
        install = self.install()
        state = install.layout.data_dir(OLD) / "dispatcher" / "production-state.json"
        payload = json.loads(state.read_text())
        payload["observers"] = {"sprint:1475": {"bound": True}}
        state.write_text(json.dumps(payload))
        checks, _facts = steps.preconditions.pipeline_checks(install.layout.data_dir(OLD))
        self.assertTrue(all(check.ok for check in checks), [check.line() for check in checks])

    def test_missing_baseline_comment(self) -> None:
        self.refuse(self.install(), "baseline and counts comments",
                    board=FakeBoard(markers={names.COUNTS_MARKER: 3}), sprint="sprint:1475")

    def test_missing_counts_comment(self) -> None:
        self.refuse(self.install(), "baseline and counts comments",
                    board=FakeBoard(markers={names.BASELINE_MARKER: 3}), sprint="sprint:1475")

    def test_a_pause_that_is_not_set_is_refused(self) -> None:
        install = self.install()
        (install.layout.data_dir(OLD) / "dispatcher" / "pause.json").unlink()
        self.refuse(install, "pause allows a freeze")


class ApplyTests(FixtureTestCase):
    def apply_through(self, install: Installation, through: str, **options: Any) -> Any:
        ctx = install.context(**options)
        quietly(engine.apply, ctx, through=through)
        return ctx

    def test_journal_resumes_at_the_interrupted_step(self) -> None:
        install = self.install()
        board, runner = FakeBoard(), StubRunner()
        board.fail_dump = 1
        with self.assertRaises(context.TransitionError):
            quietly(engine.apply, install.context(board=board, runner=runner), through="move")
        journal = context.Journal.load(install.layout.journal_path)
        self.assertTrue(journal.done("preconditions") and journal.done("freeze"))
        self.assertFalse(journal.done("dump"))
        freezes = [call for call in runner.calls if "freeze" in call]
        self.assertEqual(len(freezes), 1)
        self.assertEqual(journal.fact("pre_transition_sha"), install.pre_sha)

        rerun = StubRunner()
        quietly(engine.apply, install.context(board=board, runner=rerun), through="move")
        journal = context.Journal.load(install.layout.journal_path)
        self.assertTrue(journal.done("dump") and journal.done("move"))
        self.assertEqual([call for call in rerun.calls if "freeze" in call], [], "step 2 ran twice")
        self.assertEqual(board.calls.count("dump"), 2)
        self.assertTrue(install.layout.product_root(NEW).is_dir())
        self.assertFalse(install.layout.product_root(OLD).exists())
        self.assertEqual(journal.fact("venv_extras"), ["dev", "memory"])
        self.assertEqual(os.readlink(install.layout.orca_link),
                         str(install.layout.opt_dir(NEW) / "orca" / "bin" / "orca-ide"))

    def test_the_old_tree_stops_before_step_five(self) -> None:
        install = self.install()
        ctx = install.context()
        ctx.require_renamed = True
        # Run as the pre-rename tree whichever tree this is: the renamed one goes on into step 5.
        with unittest.mock.patch.object(engine, "RUNNING_PACKAGE", OLD.package):
            _code, output = quietly(engine.apply, ctx)
        journal = context.Journal.load(install.layout.journal_path)
        self.assertTrue(journal.done("move"))
        self.assertFalse(journal.done("checkout"))
        self.assertIn("runs from the renamed checkout", output)

    def test_step_five_refuses_an_unfinished_bootstrap(self) -> None:
        install = self.install()
        self.apply_through(install, "move")
        with self.assertRaises(context.TransitionError) as caught:
            quietly(engine.apply, install.context(), through="checkout")
        self.assertIn("step 5 is not complete", str(caught.exception))

    def run_to(self, install: Installation, through: str) -> Any:
        self.apply_through(install, "move")
        install.bootstrap_checkout()
        return self.apply_through(install, through)

    def test_full_run_rewrites_instance_secrets_dirs_and_reports(self) -> None:
        install = self.install()
        runner = StubRunner()
        self.apply_through(install, "move")
        install.bootstrap_checkout()
        quietly(engine.apply, install.context(runner=runner))
        layout, instance = install.layout, install.layout.instance
        journal = context.Journal.load(layout.journal_path)
        self.assertTrue(all(journal.done(step.name) for step in steps.STEPS))

        text = (instance / "instance.yaml").read_text()
        self.assertIn(f"name: {NEW.instance_name}\n", text)
        self.assertIn(f"data_dir: {install.home / NEW.data_dir}\n", text)
        self.assertIn(f"  unit_prefix: {NEW.unit_prefix}\n", text)
        self.assertNotIn("foreign_units", text)
        self.assertIn("# a comment that stays", text)
        self.assertIn(names.INSTANCE_PROJECT, text)
        project = (instance / "projects" / f"{NEW.project_id}.yaml").read_text()
        self.assertFalse((instance / "projects" / f"{OLD.project_id}.yaml").exists())
        self.assertIn(f"repo: {install.home / NEW.product_dir}\n", project)
        self.assertNotIn("curator_roots", project)
        self.assertIn(f"import_package: {NEW.package}", (instance / "adapters" / f"{NEW.project_id}.yaml").read_text())
        self.assertIn(NEW.tools_dir, (instance / "adapters" / "other.yaml").read_text())
        self.assertIn(f"-m {NEW.package}.runtime", (instance / "heads" / "heads.toml").read_text())
        runtime = (instance / "runtime.env").read_text()
        self.assertIn(f"{NEW.env_prefix}HEAD_IDLE_STALL_SECONDS=900", runtime)
        self.assertNotIn(names.DROPPED_RUNTIME_KEYS[0], runtime)
        self.assertIn("# comment", runtime)
        store = (instance / "board-store.env").read_text()
        self.assertIn(f"{NEW.env_prefix}DB_NAME={NEW.db_name}\n", store)
        self.assertIn(f"{NEW.env_prefix}DB_OWNER_PASSWORD=pw-owner\n", store)
        self.assertEqual((instance / "board-store.env").stat().st_mode & 0o777, 0o600)
        self.assertTrue((instance / "state" / "memory" / "facts" / NEW.memory_scope_dir / "fact.md").exists())
        self.assertFalse((instance / "state" / "memory" / "facts" / OLD.product_memory).exists())
        self.assertEqual(git(instance, "status", "--porcelain"), "")
        self.assertEqual(git(instance, "log", "-1", "--format=%s"), steps.COMMIT_SUBJECT)
        self.assertEqual(secret_rewrap.verify_store(instance / "secrets", NEW), ["web-front-password"])
        self.assertTrue((layout.state_dir / "secrets" / "installation-key.json").exists())

        data = layout.data_dir(NEW)
        state = json.loads((data / "dispatcher" / "production-state.json").read_text())
        self.assertEqual(state["owner"], NEW.dispatcher_owner)
        self.assertEqual(list(state["resume_workspaces"]), [str(data / "workspaces" / OLD.project_id / "card-1")])
        self.assertEqual(json.loads((data / "data-manifest.json").read_text())["data_dir"], str(data))
        codex_home = (data / "codex-home" / "config.toml").read_text()
        self.assertIn(f'[projects."{layout.product_root(NEW)}"]', codex_home)
        self.assertIn(f'[projects."{install.card_worktree}"]', codex_home, "a finished card's key is history")
        codex = layout.codex_config.read_text()
        self.assertNotIn("mcp_servers", codex)
        self.assertIn('[tui]', codex)
        self.assertIn(f'[projects."{layout.product_root(NEW)}"]', codex)
        moved = data / "workspaces" / OLD.project_id / "card-1"
        self.assertEqual(git(moved, "rev-parse", "--show-toplevel"), str(moved))

        for old, new in install.claude.items():
            self.assertFalse((layout.claude_projects / old).exists(), old)
            self.assertTrue((layout.claude_projects / new / "memory" / "MEMORY.md").exists(), new)
        trust = json.loads(layout.claude_json.read_text())["projects"]
        self.assertIn(str(layout.product_root(NEW)), trust)
        self.assertIn(str(layout.product_root(OLD)), trust)

        self.assertEqual(sorted(path.name for path in layout.units_dir.iterdir()), [f"{OLD.unit_prefix}supervisor.service"])
        self.assertEqual(sorted(path.name for path in (layout.state_dir / "units").iterdir()), [
            f"{OLD.unit_prefix}dispatcher-production.service", f"{OLD.unit_prefix}dispatcher-production.timer",
            f"{OLD.unit_prefix}web.service",
        ])
        joined = [" ".join(call) for call in runner.calls]
        self.assertTrue(any("upgrade" in call and "--no-pull" in call for call in joined))
        self.assertTrue(any("web-front render" in call and "--site https://front.example" in call for call in joined))
        report = (layout.state_dir / "report.md").read_text()
        self.assertTrue(report.startswith(names.DONE_MARKER))
        self.assertIn("## What was done", report)
        self.assertIn("## How to verify", report)
        self.assertIn(install.pre_sha, report)
        comment = next(call for call in runner.calls if "comment" in call)
        self.assertIn("sprint:1475", comment)
        with self.assertRaises(context.TransitionError):
            quietly(engine.run_rollback, install.context())

    def test_claude_move_refuses_a_non_empty_target(self) -> None:
        install = self.install()
        old, new = next(iter(install.claude.items()))
        write(install.layout.claude_projects / new / "other-session.jsonl", "{}\n")
        with self.assertRaises(context.TransitionError) as caught:
            self.run_to(install, "claude")
        self.assertIn("non-empty", str(caught.exception))
        self.assertTrue((install.layout.claude_projects / old / "memory" / "MEMORY.md").exists())
        empty = Path(tempfile.mkdtemp(dir=install.root))
        source = write(install.root / "source" / "a", "a").parent
        self.assertEqual(rewrite.move_dir(source, empty), "moved")
        self.assertEqual((empty / "a").read_text(), "a")


class ReviewFixTests(FixtureTestCase):
    """The four points the observer asked for before the one live run (generation 3)."""

    def to_move(self, install: Installation, runner: StubRunner | None = None) -> None:
        quietly(engine.apply, install.context(runner=runner), through="move")

    def test_origin_main_moving_after_step_one_is_refused(self) -> None:
        install = self.install()
        self.to_move(install)
        journal = context.Journal.load(install.layout.journal_path)
        self.assertEqual(journal.fact("target_sha"), install.target)
        work = install.root / "work"
        write(work / "LATER.md", "merged after the preconditions\n")
        git(work, "add", "-A")
        git(work, "commit", "-q", "-m", "a later card")
        git(work, "push", "-q", "origin", "main")
        moved = git(work, "rev-parse", "HEAD")
        install.bootstrap_checkout(target=moved)
        with self.assertRaises(context.TransitionError) as caught:
            quietly(engine.apply, install.context(), through="checkout")
        self.assertIn(f"not at {install.target}", str(caught.exception))
        self.assertFalse(context.Journal.load(install.layout.journal_path).done("checkout"))
        script = BootstrapScriptTests.SCRIPT.read_text(encoding="utf-8")
        self.assertIn('git -C "$root" merge --ff-only --quiet "$target"', script)
        self.assertIn("origin/main moved since step 1", script)

    def test_a_leftover_old_package_and_an_importable_one_are_refused(self) -> None:
        install = self.install()
        self.to_move(install)
        install.bootstrap_checkout(keep_old_package=True)
        with self.assertRaises(context.TransitionError) as caught:
            quietly(engine.apply, install.context(), through="checkout")
        self.assertIn(str(install.layout.product_root(NEW) / "src" / OLD.package), str(caught.exception))

        shutil.rmtree(install.layout.product_root(NEW) / "src" / OLD.package)
        runner = StubRunner()
        runner.fail["-P -c"] = 1
        with self.assertRaises(context.TransitionError) as caught:
            quietly(engine.apply, install.context(runner=runner), through="checkout")
        self.assertIn(f"the new venv imports {OLD.package}", str(caught.exception))
        check = next(call for call in runner.calls if "-c" in call)
        self.assertEqual((check[0], check[-1]), (str(install.layout.python(NEW)), OLD.package))

        runner = StubRunner()
        quietly(engine.apply, install.context(runner=runner))
        self.assertTrue(context.Journal.load(install.layout.journal_path).done("report"))
        report = (install.layout.state_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn(f"'import {OLD.package}'` in the new venv: ModuleNotFoundError", report)
        script = BootstrapScriptTests.SCRIPT.read_text(encoding="utf-8")
        self.assertIn('rm -rf "$root/src/$OLD_PACKAGE" "$root/src/$OLD_PACKAGE.egg-info"', script)
        self.assertIn("except ModuleNotFoundError", script)

    def test_the_import_check_itself_tells_a_missing_package_from_a_present_one(self) -> None:
        import sys

        missing = subprocess.run([sys.executable, "-P", "-c", steps.IMPORT_CHECK, "no_such_package_here"],
                                 capture_output=True, text=True, check=False)
        present = subprocess.run([sys.executable, "-P", "-c", steps.IMPORT_CHECK, "json"],
                                 capture_output=True, text=True, check=False)
        self.assertEqual((missing.returncode, missing.stdout.strip()), (0, "ModuleNotFoundError"))
        self.assertEqual((present.returncode, present.stdout.strip()), (1, "importable"))

    def run_to_checkout(self, install: Installation) -> None:
        self.to_move(install)
        install.bootstrap_checkout()
        quietly(engine.apply, install.context(), through="data-plane")

    def test_a_dirty_instance_path_is_refused_before_the_rewrite(self) -> None:
        install = self.install()
        self.run_to_checkout(install)
        instance = install.layout.instance
        heads = instance / "heads" / "heads.toml"
        heads.write_text(heads.read_text() + "# an operator's unfinished edit\n")
        before = git(instance, "rev-parse", "HEAD")
        with self.assertRaises(context.TransitionError) as caught:
            quietly(engine.apply, install.context(), through="instance")
        self.assertIn("heads/heads.toml", str(caught.exception))
        self.assertEqual(git(instance, "rev-parse", "HEAD"), before)
        self.assertIn(f"name: {OLD.instance_name}", (instance / "instance.yaml").read_text())

    def test_an_unrelated_change_is_not_committed(self) -> None:
        install = self.install()
        self.run_to_checkout(install)
        instance = install.layout.instance
        write(instance / "NOTES.md", "somebody's draft\n")
        write(instance / "persona" / "AGENTS.md", "tracked prose\n")
        quietly(engine.apply, install.context(), through="instance")
        committed = git(instance, "show", "--name-only", "--format=", "HEAD").splitlines()
        self.assertNotIn("NOTES.md", committed)
        self.assertNotIn("persona/AGENTS.md", committed)
        self.assertIn("instance.yaml", committed)
        self.assertIn(f"projects/{NEW.project_id}.yaml", committed)
        self.assertIn("secrets/installation-key.json", committed)
        status = git(instance, "status", "--porcelain")
        self.assertIn("NOTES.md", status)

    def test_a_freeze_warning_refuses_step_two(self) -> None:
        install = self.install()
        runner = StubRunner()
        runner.answers["pause freeze"] = json.dumps({
            "status": "ok",
            "warnings": ["observer heads could not be stopped and are retried by the next tick: sprint:1475"],
        }) + "\n"
        with self.assertRaises(context.TransitionError) as caught:
            quietly(engine.apply, install.context(runner=runner), through="move")
        self.assertIn("sprint:1475", str(caught.exception))
        journal = context.Journal.load(install.layout.journal_path)
        self.assertFalse(journal.done("freeze"))
        self.assertFalse(any("disable" in call for call in runner.calls), "units were disabled anyway")

        # The rerun finds the pipeline frozen, but the pending stop is still on the books.
        write(install.layout.data_dir(OLD) / "dispatcher" / "pause.json", json.dumps({"mode": "freeze"}))
        state = install.layout.data_dir(OLD) / "dispatcher" / "production-state.json"
        payload = json.loads(state.read_text())
        payload["observers"] = {"sprint:1475": {"state": steps.OBSERVER_STOP_PENDING}}
        state.write_text(json.dumps(payload))
        with self.assertRaises(context.TransitionError) as caught:
            quietly(engine.apply, install.context(), through="move")
        self.assertIn("still on the books: sprint:1475", str(caught.exception))
        self.assertFalse(context.Journal.load(install.layout.journal_path).done("freeze"))


class RollbackTests(FixtureTestCase):
    def test_rollback_restores_paths_units_secrets_dirs_and_the_checkout(self) -> None:
        install = self.install()
        layout, instance = install.layout, install.layout.instance
        secrets_before = {path.name: path.read_bytes() for path in (instance / "secrets").rglob("*") if path.is_file()}
        tracked_before = {path: (instance / path).read_bytes() for path in
                          ("instance.yaml", f"projects/{OLD.project_id}.yaml", "runtime.env", "board-store.env")}
        units_before = {path.name: path.read_bytes() for path in layout.units_dir.iterdir()}
        claude_before = layout.claude_json.read_bytes()
        codex_before = layout.codex_config.read_bytes()
        state_before = (layout.data_dir(OLD) / "dispatcher" / "production-state.json").read_bytes()
        orca_before = os.readlink(layout.orca_link)

        ctx = install.context()
        quietly(engine.apply, ctx, through="move")
        install.bootstrap_checkout()
        quietly(engine.apply, install.context(), through="old-units")
        self.assertFalse(layout.product_root(OLD).exists())

        runner = StubRunner()
        quietly(engine.run_rollback, install.context(runner=runner))

        for old, new in rewrite.data_plane_prefixes(install.home)[:2]:
            self.assertTrue(Path(old).is_dir(), old)
            self.assertFalse(Path(new).exists(), new)
        self.assertTrue(layout.opt_dir(OLD).is_dir())
        self.assertFalse(layout.opt_dir(NEW).exists())
        self.assertTrue(layout.tools_dir(OLD).is_dir())
        self.assertEqual(os.readlink(layout.orca_link), orca_before)
        self.assertEqual(os.readlink(layout.uv_link), str(layout.tools_dir(OLD) / "uv" / "bin" / "uv"))
        self.assertIn(f"${{{OLD.env_prefix}DB_PORT}}", layout.compose_path(OLD).read_text())

        self.assertEqual({path.name: path.read_bytes() for path in layout.units_dir.iterdir()}, units_before)
        joined = [" ".join(call) for call in runner.calls]
        self.assertTrue(any(call.startswith("sudo -n systemctl enable") for call in joined))
        self.assertTrue(any(OLD.compose_project in call and call.endswith("start") for call in joined))

        self.assertEqual({path.name: path.read_bytes() for path in (instance / "secrets").rglob("*") if path.is_file()},
                         secrets_before)
        for path, content in tracked_before.items():
            self.assertEqual((instance / path).read_bytes(), content, path)
        self.assertTrue((instance / "state" / "memory" / "facts" / OLD.memory_scope_dir / "fact.md").exists())

        for old, new in install.claude.items():
            self.assertTrue((layout.claude_projects / old / "memory" / "MEMORY.md").exists(), old)
            self.assertFalse((layout.claude_projects / new).exists(), new)
        self.assertEqual(layout.claude_json.read_bytes(), claude_before)
        self.assertEqual(layout.codex_config.read_bytes(), codex_before)
        self.assertEqual((layout.data_dir(OLD) / "dispatcher" / "production-state.json").read_bytes(), state_before)

        checkout = layout.product_root(OLD)
        self.assertEqual(git(checkout, "symbolic-ref", "--short", "HEAD"), "main")
        self.assertEqual(git(checkout, "rev-parse", "HEAD"), install.pre_sha)
        self.assertEqual(git(checkout, "remote", "get-url", "origin"), OLD.remote)
        self.assertTrue((checkout / ".venv" / "bin" / OLD.package).exists())
        self.assertTrue((checkout / "src" / OLD.package / "dispatch" / "runtime_preflight.py").exists())
        worktree = layout.data_dir(OLD) / "workspaces" / OLD.project_id / "card-1"
        self.assertEqual(git(worktree, "rev-parse", "--show-toplevel"), str(worktree))

        self.assertFalse(layout.journal_path.exists())
        self.assertFalse(layout.state_dir.exists())
        archived = list(install.home.glob(f"{names.STATE_DIR_NAME}-rolled-back-*"))
        self.assertEqual(len(archived), 1)
        self.assertTrue((archived[0] / names.JOURNAL_NAME).exists())

    def test_rollback_after_a_refused_start_only_restarts_what_was_stopped(self) -> None:
        install = self.install()
        board = FakeBoard()
        board.fail_dump = 1
        with self.assertRaises(context.TransitionError):
            quietly(engine.apply, install.context(board=board))
        runner = StubRunner()
        quietly(engine.run_rollback, install.context(runner=runner))
        checkout = install.layout.product_root(OLD)
        self.assertEqual(git(checkout, "rev-parse", "HEAD"), install.pre_sha)
        self.assertTrue(any(call[:4] == ["sudo", "-n", "systemctl", "start"] for call in runner.calls))


class SecretRewrapTests(unittest.TestCase):
    def test_old_ciphertext_opens_under_the_new_names_after_the_rewrap(self) -> None:
        key = os.urandom(32)
        old = secret_rewrap.seal_value(key, "token", b"value", OLD)
        self.assertEqual(secret_rewrap.open_value(key, old, OLD), b"value")
        with self.assertRaises(context.TransitionError):
            secret_rewrap.open_value(key, old, NEW)
        with tempfile.TemporaryDirectory() as temporary:
            secrets_dir = Path(temporary) / "secrets"
            write(secrets_dir / "installation.key", base64.b64encode(key).decode() + "\n", 0o600)
            params = Installation.__new__(Installation)
            params.key = key
            old_params = secret_rewrap.rewrap_params(key, params._old_params(), NEW, OLD)
            write(secrets_dir / "installation-key.json", json.dumps(old_params))
            write(secrets_dir / "values" / "token.enc.json", json.dumps(old), 0o600)
            result = secret_rewrap.rewrap_store(secrets_dir, Path(temporary) / "backup", OLD, NEW)
            self.assertFalse(result.already)
            resealed = json.loads((secrets_dir / "values" / "token.enc.json").read_text())
            self.assertEqual(secret_rewrap.open_value(key, resealed, NEW), b"value")
            self.assertEqual(resealed["format"], NEW.envelope_format)
            self.assertEqual(resealed["kdf"]["info"], NEW.value_kdf_info)
            self.assertEqual((secrets_dir / "values" / "token.enc.json").stat().st_mode & 0o777, 0o600)
            with self.assertRaises(context.TransitionError):
                secret_rewrap.open_value(key, resealed, OLD)
            rewrapped = json.loads((secrets_dir / "installation-key.json").read_text())
            self.assertEqual(rewrapped["kdf"], old_params["kdf"], "the recovery phrase must keep opening the store")
            secret_rewrap.check_verifier(key, rewrapped, NEW)
            with self.assertRaises(context.TransitionError):
                secret_rewrap.check_verifier(key, rewrapped, OLD)
            with self.assertRaises(context.TransitionError):
                secret_rewrap.check_verifier(os.urandom(32), rewrapped, NEW)
            self.assertEqual(json.loads((Path(temporary) / "backup" / "values" / "token.enc.json").read_text()), old)
            self.assertTrue(secret_rewrap.rewrap_store(secrets_dir, Path(temporary) / "backup", OLD, NEW).already)

    def test_a_tampered_header_fails_under_any_names(self) -> None:
        key = os.urandom(32)
        envelope = secret_rewrap.seal_value(key, "token", b"value", NEW)
        envelope["id"] = "other"
        with self.assertRaises(context.TransitionError):
            secret_rewrap.open_value(key, envelope, NEW)


class NameTableTests(unittest.TestCase):
    """The transition takes every name from its own table, so the rename cannot change its meaning."""

    def modules(self) -> list[Path]:
        return sorted(TRANSITION.glob("*.py"))

    def test_product_imports_are_relative_and_name_modules_only(self) -> None:
        offenders = []
        for path in self.modules():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    offenders += [f"{path.name}:{node.lineno} import {alias.name}" for alias in node.names
                                  if alias.name.split(".")[0] in (OLD.package, NEW.package)]
                if not isinstance(node, ast.ImportFrom):
                    continue
                if node.level == 0:
                    if (node.module or "").split(".")[0] in (OLD.package, NEW.package):
                        offenders.append(f"{path.name}:{node.lineno} absolute import of {node.module}")
                    continue
                if node.level == 1:
                    continue  # the transition's own modules
                for alias in node.names:
                    module = f"{PACKAGE}.{node.module}.{alias.name}" if node.module else f"{PACKAGE}.{alias.name}"
                    if importlib.util.find_spec(module) is None:
                        offenders.append(f"{path.name}:{node.lineno} imports the name {alias.name} from {node.module}")
        self.assertEqual(offenders, [])

    def test_no_literal_names_outside_the_table(self) -> None:
        words = {OLD.package, NEW.package}
        offenders = []
        for path in self.modules():
            if path.name == "names.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            docstrings = {
                id(node.body[0].value) for node in ast.walk(tree)
                if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef))
                and node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)
            }
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
                        and any(word in node.value.lower() for word in words)):
                    offenders.append(f"{path.name}:{node.lineno} {node.value[:60]!r}")
        self.assertEqual(offenders, [])

    def test_the_table_has_both_columns_for_every_name(self) -> None:
        for field in names.Names.__dataclass_fields__:
            old, new = getattr(OLD, field), getattr(NEW, field)
            self.assertNotEqual(old, new, field)
        self.assertEqual(NEW.verifier_aad, b"ummanu/installation-key/v1")
        self.assertEqual(NEW.value_kdf_info, "ummanu/secret/v1")
        self.assertEqual(OLD.verifier_aad, b"secretary/installation-key/v1")
        self.assertEqual(OLD.value_kdf_info, "secretary/secret/v1")


class BootstrapScriptTests(unittest.TestCase):
    SCRIPT = ROOT / "scripts" / "transition-from-secretary.sh"

    def test_the_script_parses_and_hands_over_to_the_new_cli(self) -> None:
        subprocess.run(["bash", "-n", str(self.SCRIPT)], check=True)
        text = self.SCRIPT.read_text(encoding="utf-8")
        self.assertTrue(os.access(self.SCRIPT, os.X_OK))
        self.assertIn("--through move", text)
        self.assertIn('exec "$new_root/.venv/bin/$NEW_PACKAGE" transition', text)
        self.assertIn('merge --ff-only --quiet "$target"', text)
        self.assertIn(f"NEW_REMOTE={NEW.remote}", text)


class CommandTests(FixtureTestCase):
    def test_the_cli_plans_through_the_registered_command(self) -> None:
        install = self.install()
        cli = importlib.import_module(f"{PACKAGE}.cli")
        reads = FakeBoard().reads
        with unittest.mock.patch.object(steps.BoardOps, "reads", lambda self, ctx: reads(ctx)):
            code, output = quietly(cli.main, ["transition", f"from-{OLD.package}", "--plan",
                                              "--instance", str(install.layout.instance), "--home", str(install.home)])
        self.assertIn("plan only, nothing is written", output)
        self.assertFalse(install.layout.journal_path.exists())
        self.assertIn(code, (0, 1))


class ScopeOwnerRepairTests(unittest.TestCase):
    """`--repair-scope-owners`: settled heads recorded before the rename get their current scope unit."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.data = self.home / NEW.data_dir
        self.instance = self.home / "instance"
        write(self.instance / "instance.yaml",
              f"version: 1\nname: fixture\ndata_dir: {self.data}\n"
              "offsite:\n  instance_remote: https://github.com/example/fixture.git\n")
        self.memory = importlib.import_module(f"{PACKAGE}.runtime.head.memory")
        self.lifecycle = importlib.import_module(
            f"{PACKAGE}.runtime.head.local_pty.scoped_lifecycle").ScopedHeadLifecycle
        self.repair = importlib.import_module(f"{PACKAGE}.transition.scope_owners")

    def owner(self, run_id: str, *, unit: str = "", settled: bool = True) -> Path:
        record = {
            "run_id": run_id, "unit": unit or self.repair.old_scope_unit(run_id), "generation": "g1",
            "launch_allowed": not settled, "cleanup_complete": settled,
            "role": "worker", "task": "card", "workspace": "/w",
        }
        path = write(self.data / "heads" / run_id / "scope-owner.json", json.dumps(record))
        (path.parent / "scope-owner.lock").touch()
        return path

    def run_cli(self, *extra: str) -> tuple[int, str]:
        cli = importlib.import_module(f"{PACKAGE}.cli")
        return quietly(cli.main, ["transition", f"from-{OLD.package}", "--repair-scope-owners", *extra,
                                  "--instance", str(self.instance), "--home", str(self.home)])

    def backup(self, run_id: str) -> Path:
        return self.home / names.STATE_DIR_NAME / "heads" / run_id / "scope-owner.json"

    def test_a_settled_old_record_is_renamed_and_its_original_kept(self) -> None:
        path = self.owner("run-a")
        original = path.read_bytes()

        code, output = self.run_cli("--apply")

        self.assertEqual(code, 0, output)
        record = json.loads(path.read_text())
        self.assertEqual(record["unit"], self.memory.scope_unit("run-a"))
        self.assertEqual({**record, "unit": None}, {**json.loads(original), "unit": None})
        self.assertIs(self.lifecycle.validate_owner(record), record)
        self.assertEqual(self.backup("run-a").read_bytes(), original)
        self.assertIn("1 repaired", output)

    def test_an_unsettled_or_inconsistent_old_record_is_left_untouched_and_fails_the_command(self) -> None:
        unsettled = self.owner("run-live", settled=False)
        foreign = self.owner("run-odd", unit=self.repair.old_scope_unit("someone-else"))
        settled = self.owner("run-done")
        before = {path: path.read_bytes() for path in (unsettled, foreign)}

        code, output = self.run_cli("--apply")

        self.assertNotEqual(code, 0)
        for path, raw in before.items():
            self.assertEqual(path.read_bytes(), raw)
            self.assertIn(f"untouched heads/{path.parent.name}", " ".join(output.split()))
        self.assertFalse(self.backup("run-live").exists())
        self.assertEqual(json.loads(settled.read_text())["unit"], self.memory.scope_unit("run-done"))

    def test_a_current_record_is_neither_listed_nor_touched(self) -> None:
        path = self.owner("run-new", unit=self.memory.scope_unit("run-new"))
        raw, stamp = path.read_bytes(), path.stat().st_mtime_ns

        code, output = self.run_cli("--apply")

        self.assertEqual(code, 0, output)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), (raw, stamp))
        self.assertNotIn("run-new", output)
        self.assertFalse((self.home / names.STATE_DIR_NAME).exists())

    def test_without_apply_it_lists_the_candidates_and_writes_nothing(self) -> None:
        path = self.owner("run-a")
        raw, stamp = path.read_bytes(), path.stat().st_mtime_ns

        code, output = self.run_cli()

        self.assertEqual(code, 0, output)
        self.assertIn("nothing is written", output)
        self.assertIn(f"{self.repair.old_scope_unit('run-a')} -> {self.memory.scope_unit('run-a')}", output)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), (raw, stamp))
        self.assertFalse((self.home / names.STATE_DIR_NAME).exists())

    def test_a_second_apply_is_a_no_op(self) -> None:
        path = self.owner("run-a")
        self.assertEqual(self.run_cli("--apply")[0], 0)
        raw, stamp = path.read_bytes(), path.stat().st_mtime_ns
        kept = self.backup("run-a").read_bytes()

        code, output = self.run_cli("--apply")

        self.assertEqual(code, 0, output)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), (raw, stamp))
        self.assertEqual(self.backup("run-a").read_bytes(), kept)
        self.assertIn("0 repaired", output)

    def test_a_record_whose_lock_is_held_is_left_for_a_later_run(self) -> None:
        path = self.owner("run-a")
        raw = path.read_bytes()

        with self.lifecycle.owner_lock(path.parent):
            code, output = self.run_cli("--apply")

        self.assertNotEqual(code, 0)
        self.assertEqual(path.read_bytes(), raw)
        self.assertIn("not repaired", output)

    def test_the_repair_does_not_combine_with_the_transition_modes(self) -> None:
        self.owner("run-a")
        with contextlib.redirect_stderr(io.StringIO()):
            code, _ = self.run_cli("--plan")
        self.assertEqual(code, 2)


class ProductRepairDecisionTests(unittest.TestCase):
    """`--repair-products` decides on the canary's locked row; the writes are in test_transition_products."""

    def setUp(self) -> None:
        self.products = importlib.import_module(f"{PACKAGE}.transition.products")

    def state(self, state: str = "active", projects: list[str] | None = None) -> dict[str, Any]:
        return {"state": state, "title": "t", "projects": projects or [names.OLD.project_id],
                "issues": {}, "sprints": []}

    def test_an_open_canary_on_the_old_project_is_archived_and_an_archived_one_is_left(self) -> None:
        self.assertTrue(self.products._check(self.state()))
        self.assertFalse(self.products._check(self.state("archived")))

    def test_a_missing_product_unexpected_projects_or_state_are_refused(self) -> None:
        for state in (
            None,
            self.state(projects=[names.OLD.project_id, names.INSTANCE_PROJECT]),
            self.state("archived", projects=[names.NEW.project_id]),
            self.state("frozen"),
        ):
            with self.subTest(state=state), self.assertRaisesRegex(context.TransitionError, "nothing written"):
                self.products._check(state)

    def test_the_repair_does_not_combine_with_the_transition_modes_or_the_other_repair(self) -> None:
        cli = importlib.import_module(f"{PACKAGE}.cli")
        with tempfile.TemporaryDirectory() as home:
            for extra in (["--plan"], ["--rollback"], ["--repair-scope-owners"]):
                with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()):
                    code, _ = quietly(cli.main, ["transition", f"from-{names.OLD.package}", "--repair-products",
                                                 *extra, "--instance", home, "--home", home])
                self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
