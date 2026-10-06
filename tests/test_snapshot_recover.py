"""`ummanu recover` from a remote whose tip is an exporter snapshot (docs/RECOVERY.md, "Fresh install
and recovery").

The remote is a local bare repository holding a real `SnapshotExporter` commit. Recovery runs through
`install()` with only the board store, the memory model, project checkouts and the host steps other
than the head registry stood in for; the clone, the manifest check, the snapshot repository, the
takeover marker, the live root, the checkpoint and the recovery identity are all real. The board
import against PostgreSQL is `tests/test_snapshot_recovery_postgres.py`.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.fakes import snapshot_remote
from tests.fakes.installation import CARD, SPRINT, _checkpoint, _git, write_memory_metadata
from tests.fakes.snapshot_remote import (
    HEAD,
    REVISION,
    canon,
    exporter_producers,
    exporter_remote,
    git,
    recover_snapshot,
    recovery_args as _args,
)
from ummanu import bootstrap as bootstrap_module, installation, restore, secret_store
from ummanu.board import provision as provision_module, store
from ummanu.board.migrate import head_revision
from ummanu.checkpoint import (
    SNAPSHOT_BASE_REF,
    SNAPSHOT_MANIFEST,
    SNAPSHOT_REF,
    SnapshotExporter,
    snapshot_foreign_commits,
    tick_checkpoint_pusher,
    tick_checkpoint_writer,
)
from ummanu.config import validate_instance
from ummanu.head_registry import installed_heads, installed_pair
from ummanu.infra.export_allowlist import is_exported
from ummanu.installation import InstallError, _recovery_identity
from ummanu.memory.canon import fact_content_hash, fact_files, parse_fact_text
from ummanu.memory_journal import verify_memory_journal
from ummanu.secret_words import RECOVERY_WORDS
from ummanu.sprint_observer import head_choice

PHRASE = " ".join(RECOVERY_WORDS[:16])
SERVICE_ENV = "EXAMPLE_URL=http://127.0.0.1/rpc\nEXAMPLE_API_TOKEN=live-token\n"


def _fast_key_params() -> dict:
    """The store's key parameters at a cheap work factor; the derivation reads them back from the file."""
    return {
        "format": secret_store.KEY_PARAMS_FORMAT,
        "version": secret_store.KEY_PARAMS_VERSION,
        "kdf": {
            "id": "scrypt",
            "salt": secret_store._b64(b"0123456789abcdef"),
            "length": 32,
            "n": 2**8,
            "r": 8,
            "p": 1,
        },
    }


def _stood_in_bootstrap(
    args: SimpleNamespace, opt: Path, store_steps: mock.Mock, chowned: list[tuple[Path, int, int]]
) -> int:
    """The real `ummanu bootstrap` with root's view, the host check, the platform install and Docker
    stood in for. `provision` runs and writes `board-store.env`; the migration and the role check are
    recorded on `store_steps`; every `chown` runs and is recorded on `chowned`."""
    kwdefaults = dict(provision_module.provision.__kwdefaults__ or {})
    kwdefaults["compose_path"] = opt / "postgres-compose.yml"
    real_chown = os.chown

    def chown(path, uid: int, gid: int, *, follow_symlinks: bool = True) -> None:
        chowned.append((Path(os.fsdecode(path)), uid, gid))
        real_chown(path, uid, gid, follow_symlinks=follow_symlinks)

    with (
        mock.patch("ummanu.bootstrap.os.geteuid", return_value=0),
        mock.patch("ummanu.bootstrap.os.chown", side_effect=chown),
        mock.patch("ummanu.bootstrap._host_supported"),
        mock.patch("ummanu.bootstrap._ensure_installation_user"),
        mock.patch("ummanu.bootstrap._install_platform", store_steps.install_platform),
        mock.patch.object(provision_module.provision, "__kwdefaults__", kwdefaults),
        mock.patch("ummanu.board.provision._exists", return_value=False),
        mock.patch("ummanu.board.provision._run", return_value="container-id"),
        mock.patch("ummanu.board.provision._inspect_container"),
        mock.patch("ummanu.board.provision._wait_ready"),
        mock.patch("ummanu.bootstrap.migrate_instance", store_steps.migrate),
        mock.patch("ummanu.bootstrap.verify_board_store_roles", store_steps.verify),
        mock.patch("builtins.print"),
    ):
        return bootstrap_module.bootstrap(args)


def _service_index(data_dir: Path, instance_dir: Path, *, model: str, dim: int, **_kwargs) -> int:
    """The reindex without the embedding model, in the service's schema, so `memory verify` can read it."""
    index = data_dir / "memory" / "index.sqlite"
    index.unlink(missing_ok=True)
    write_memory_metadata(index, model=model, dim=dim)
    facts = fact_files(instance_dir / "state" / "memory" / "facts")
    with sqlite3.connect(index) as conn:
        conn.execute(
            "CREATE TABLE memories(id INTEGER PRIMARY KEY AUTOINCREMENT, fact_id TEXT UNIQUE, "
            "content_hash TEXT, text TEXT, scope TEXT, tags TEXT, source TEXT, created_at TEXT)"
        )
        for fact_id, path in facts:
            fact = parse_fact_text(path.read_text(encoding="utf-8"), f"{fact_id}.md", fact_id=fact_id)
            conn.execute(
                "INSERT INTO memories(fact_id, content_hash, text, scope, tags, source, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    fact["id"],
                    fact_content_hash(fact),
                    fact["text"],
                    fact["scope"],
                    fact["tags"],
                    fact["source"],
                    fact["created_at"],
                ),
            )
        conn.commit()
    return len(facts)


class SnapshotRecoverCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="snapshot-recover-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fixture = exporter_remote(self.root)
        self.board = mock.Mock(return_value=len(self.fixture.cards))

    def recover(self, *, failures: dict[str, BaseException] | None = None, **overrides):
        """One `ummanu recover`; `failures` makes the named installation callable raise instead."""
        return recover_snapshot(self.fixture, board=self.board, failures=failures, **overrides)

    def steps(self, result) -> dict[str, tuple[str, str]]:
        return {step.name: (step.status, step.detail) for step in result.steps}

    @property
    def repository(self) -> Path:
        return self.fixture.data_dir / "backup" / "instance.git"

    def live_files(self) -> dict[str, bytes]:
        target = self.fixture.target
        return {p.relative_to(target).as_posix(): p.read_bytes() for p in target.rglob("*") if p.is_file()}

    def leftovers(self) -> list[str]:
        parent = self.fixture.target.parent
        return (
            sorted(p.name for p in parent.iterdir() if p.name.startswith(".instance."))
            if parent.exists()
            else []
        )

    def relocate_data_dir(self, value: str) -> str:
        """Cut the source again with `data_dir: <value>`, rooted at the live root it is recovered into."""
        path = self.fixture.source / "instance.yaml"
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace(f"data_dir: {self.fixture.data_dir}\n", f"data_dir: {value}\n"), "utf-8")
        return self.fixture.cut()

    def push_tree(self, edit, *, message: str = "edited by hand") -> str:
        """Commit `edit(work tree)` on top of the remote tip with ordinary Git and push it."""
        work = self.root / "edit"
        shutil.rmtree(work, ignore_errors=True)
        subprocess.run(["git", "clone", "--quiet", str(self.fixture.remote), str(work)], check=True)
        edit(work)
        git(work, "add", "-A")
        git(
            work,
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "commit",
            "--quiet",
            "-m",
            message,
        )
        git(work, "push", "--quiet", "origin", "HEAD:main")
        return self.fixture.tip


class SnapshotRecoveryTests(SnapshotRecoverCase):
    def test_a_snapshot_remote_recovers_into_a_plain_live_root_a_bare_repository_and_the_data_plane(self):
        tip = self.fixture.tip

        result = self.recover()

        steps = self.steps(result)
        self.assertEqual(result.status, "ok", result.render())
        self.assertEqual(steps["instance-checkout"][0], "changed")
        self.assertIn(f"recovered exporter snapshot {tip[:12]}", steps["instance-checkout"][1])
        # The snapshot repository: a depth-1 bare clone of the remote tip, origin the configured remote.
        repo = self.repository
        self.assertEqual(git(repo, "rev-parse", "--is-bare-repository"), "true")
        self.assertEqual(git(repo, "rev-parse", "--is-shallow-repository"), "true")
        self.assertEqual(git(repo, "rev-parse", SNAPSHOT_REF), tip)
        self.assertEqual(git(repo, "rev-list", "--count", SNAPSHOT_REF), "1")
        self.assertEqual(git(repo, "config", "--get", "remote.origin.url"), str(self.fixture.remote))
        self.assertEqual(git(repo, "cat-file", "blob", SNAPSHOT_BASE_REF), tip)
        # The live root: exactly the tree's allowlisted files, byte for byte, with their modes.
        tree = git(repo, "ls-tree", "-r", "--name-only", "--full-tree", tip).splitlines()
        exported = sorted(path for path in tree if is_exported(path))
        live = self.live_files()
        self.assertEqual(sorted(live), exported)
        for path in exported:
            blob = subprocess.run(
                ["git", "-C", str(repo), "cat-file", "blob", f"{tip}:{path}"], capture_output=True, check=True
            ).stdout
            self.assertEqual(live[path], blob, path)
        self.assertTrue((self.fixture.target / "persona/hooks/check.sh").stat().st_mode & 0o100)
        self.assertFalse((self.fixture.target / "persona/rules.md").stat().st_mode & 0o111)
        for absent in (".git", "state/board", "state/runs", SNAPSHOT_MANIFEST, "runtime.env", "README.md"):
            self.assertFalse((self.fixture.target / absent).exists(), absent)
        self.assertIn("state/board/layout.json", tree)
        self.assertEqual(self.leftovers(), [])
        # The data plane came from the tree's board and runs.
        board = self.fixture.data_dir / "board"
        self.assertEqual(json.loads((board / "cards.json").read_text(encoding="utf-8"))["cards"], [CARD])
        self.assertEqual(
            json.loads((board / "sprints.json").read_text(encoding="utf-8"))["sprints"], [SPRINT]
        )
        self.assertEqual(steps["checkpoint"], ("changed", "1 board card(s), 0 run record(s)"))
        self.board.assert_called_once_with(self.fixture.data_dir, instance=self.fixture.target)
        # The heads were regenerated into the data directory from the recovered canon.
        pair = installed_pair(self.fixture.target)
        self.assertEqual(pair.snapshot.parent, self.fixture.data_dir / "heads")
        self.assertEqual(installed_heads(self.fixture.target)["role_defaults"]["new_card"], HEAD)
        self.assertFalse((self.fixture.target / "heads" / "heads.yaml").exists())
        # The first tick runs in exporter mode against the recovered repository.
        writer = tick_checkpoint_writer(self.fixture.data_dir, self.fixture.target)
        self.assertIsInstance(writer, SnapshotExporter)
        self.assertEqual(writer.snapshot_repo, repo.resolve())

    def test_memory_verifies_right_after_recover_with_the_export_handed_to_the_user(self):
        """ummanu-48 P3: the rebuild writes only the index, so recover publishes the export too."""
        with mock.patch.object(snapshot_remote, "_rebuilt_index", _service_index):
            result = self.recover()

        self.assertEqual(result.status, "ok", result.render())
        self.assertEqual(self.steps(result)["memory"], ("changed", "rebuilt index for 1 fact(s)"))
        verified = verify_memory_journal(self.fixture.data_dir, self.fixture.target)
        self.assertTrue(verified.ok, verified.findings)
        self.assertEqual((verified.fact_count, verified.export_count, verified.index_count), (1, 1, 1))

    def test_a_retry_past_the_rebuild_still_publishes_a_missing_export(self):
        with mock.patch.object(snapshot_remote, "_rebuilt_index", _service_index):
            self.recover()
            (self.fixture.data_dir / "memory" / "export.ndjson").unlink()
            again = self.recover()

        self.assertEqual(again.status, "ok", again.render())
        self.assertEqual(
            self.steps(again)["memory"],
            ("changed", "checkpoint index already rebuilt; published the memory export"),
        )
        self.assertTrue(verify_memory_journal(self.fixture.data_dir, self.fixture.target).ok)

    def test_the_recovery_identity_is_the_live_roots_facts_and_the_trees_board_and_runs(self):
        self.recover()
        progress = json.loads((self.fixture.data_dir / "recovery-progress.json").read_text(encoding="utf-8"))
        bindings = validate_instance(self.fixture.target).bindings
        legacy_shaped = self.root / "legacy-shaped"
        shutil.copytree(self.fixture.target, legacy_shaped)
        work = self.root / "tree"
        subprocess.run(["git", "clone", "--quiet", str(self.fixture.remote), str(work)], check=True)
        shutil.copytree(work / "state" / "board", legacy_shaped / "state" / "board")
        shutil.copytree(work / "state" / "runs", legacy_shaped / "state" / "runs")

        # The same inputs a legacy checkout of this tree would give: one identity for one state.
        self.assertEqual(progress["identity"], _recovery_identity(legacy_shaped, bindings))
        self.assertEqual(progress["checkpoint"], "complete")
        self.assertEqual(progress["board"], "complete")

    def test_after_recovery_one_exporter_window_commits_and_pushes_on_the_recovered_tip(self):
        recovered = self.fixture.tip
        self.recover()
        (self.fixture.target / "state" / "knowledge" / "decisions" / "two.md").write_text("# Two\n", "utf-8")

        with exporter_producers():
            writer = tick_checkpoint_writer(self.fixture.data_dir, self.fixture.target)
            writer._product_revision, writer._board_schema_head = REVISION, head_revision()
            cut = writer.write()
        pushed = tick_checkpoint_pusher(writer).push({}, now=1_800_000_000.0)

        self.assertEqual(cut.status, "committed", cut.reason)
        self.assertEqual(git(self.repository, "rev-parse", f"{cut.commit}^"), recovered)
        self.assertEqual(pushed["status"], "pushed", pushed.get("reason"))
        self.assertEqual(self.fixture.tip, cut.commit)
        self.assertEqual(snapshot_foreign_commits(self.fixture.target, self.fixture.data_dir), "")
        # A plumbing commit after the recovery is still named.
        tree = git(self.repository, "rev-parse", f"{cut.commit}^{{tree}}")
        foreign = git(
            self.repository,
            *("-c", "user.name=a head", "-c", "user.email=head@example.invalid"),
            *("commit-tree", tree, "-p", cut.commit, "-m", "fix by hand"),
        )
        git(self.repository, "update-ref", SNAPSHOT_REF, foreign)
        finding = snapshot_foreign_commits(self.fixture.target, self.fixture.data_dir)
        self.assertIn(foreign[:12], finding)
        self.assertNotIn(cut.commit[:12], finding)
        self.assertNotIn(recovered[:12], finding)

    def test_host_local_files_never_come_from_the_tree(self):
        planted = {
            "secrets/installation.key": b"not from the tree\n",
            "runtime.env": b"UMMANU_FROM_TREE=1\n",
            "board-store.env": b"PGPASSWORD=from-tree\n",
        }

        def plant(work: Path) -> None:
            manifest = json.loads((work / SNAPSHOT_MANIFEST).read_text(encoding="utf-8"))
            for relative, payload in planted.items():
                (work / relative).parent.mkdir(parents=True, exist_ok=True)
                (work / relative).write_bytes(payload)
                manifest["files"][relative] = hashlib.sha256(payload).hexdigest()
            (work / SNAPSHOT_MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")

        self.push_tree(plant)

        result = self.recover()

        self.assertEqual(result.status, "ok", result.render())
        for relative in planted:
            self.assertFalse((self.fixture.target / relative).exists(), relative)

    def test_a_relative_snapshot_repo_is_resolved_as_the_exporter_resolves_it(self):
        self.fixture.source.joinpath("instance.yaml").write_text(
            self.fixture.source.joinpath("instance.yaml")
            .read_text(encoding="utf-8")
            .replace("host:", "  snapshot_repo: offsite/snap.git\nhost:"),
            encoding="utf-8",
        )
        tip = self.fixture.cut()

        result = self.recover()

        self.assertEqual(result.status, "ok", result.render())
        relocated = self.fixture.data_dir / "offsite" / "snap.git"
        self.assertEqual(git(relocated, "rev-parse", SNAPSHOT_REF), tip)
        self.assertFalse(self.repository.exists())
        self.assertEqual(
            tick_checkpoint_writer(self.fixture.data_dir, self.fixture.target).snapshot_repo, relocated
        )

    def test_a_live_root_inside_the_data_directory_recovers(self):
        """The layout this sprint deploys: live root `<data>/instance`, `data_dir: ..` and the snapshot
        repository at its default, `<data>/backup/instance.git`, beside the live root."""
        data_dir = self.root / "ummanu-data"
        self.fixture.target = data_dir / "instance"
        tip = self.relocate_data_dir("..")
        self.fixture.data_dir = data_dir

        result = self.recover()

        self.assertEqual(result.status, "ok", result.render())
        repository = data_dir / "backup" / "instance.git"
        self.assertEqual(git(repository, "rev-parse", SNAPSHOT_REF), tip)
        self.assertEqual(git(repository, "cat-file", "blob", SNAPSHOT_BASE_REF), tip)
        self.assertFalse(repository.is_relative_to(self.fixture.target))
        tree = git(repository, "ls-tree", "-r", "--name-only", "--full-tree", tip).splitlines()
        self.assertEqual(sorted(self.live_files()), sorted(path for path in tree if is_exported(path)))
        self.assertTrue((data_dir / "data-manifest.json").is_file())
        self.assertEqual(self.leftovers(), [])
        writer = tick_checkpoint_writer(data_dir, self.fixture.target)
        self.assertIsInstance(writer, SnapshotExporter)
        self.assertEqual(writer.snapshot_repo, repository.resolve())
        # A rerun finds the same live root and the same repository beside it.
        again = self.recover()
        self.assertEqual(again.status, "ok", again.render())
        self.assertEqual(self.steps(again)["instance-checkout"][0], "unchanged")


class SnapshotRefusalTests(SnapshotRecoverCase):
    def assert_refused_before_writing(self, result, reason: str) -> None:
        steps = self.steps(result)
        self.assertEqual(result.status, "failed", result.render())
        self.assertIn(reason, steps["install"][1])
        self.assertFalse(self.fixture.target.exists())
        self.assertFalse(self.fixture.data_dir.exists())
        self.assertEqual(self.leftovers(), [])

    def edit_manifest(self, change) -> None:
        def edit(work: Path) -> None:
            path = work / SNAPSHOT_MANIFEST
            manifest = json.loads(path.read_text(encoding="utf-8"))
            change(manifest)
            path.write_text(json.dumps(manifest), encoding="utf-8")

        self.push_tree(edit)

    def test_a_malformed_manifest_is_refused_by_name_before_anything_is_written(self):
        self.push_tree(lambda work: (work / SNAPSHOT_MANIFEST).write_text("{not json", encoding="utf-8"))

        self.assert_refused_before_writing(self.recover(), "snapshot manifest is malformed: not JSON")

    def test_an_unknown_manifest_version_is_refused_by_name(self):
        self.edit_manifest(lambda manifest: manifest.update(version=2))

        self.assert_refused_before_writing(
            self.recover(), "snapshot manifest version 2 is unknown to this product, which reads version 1"
        )

    def test_a_file_the_manifest_does_not_list_is_refused(self):
        self.push_tree(
            lambda work: (work / "persona" / "extra.md").write_text("unlisted\n", encoding="utf-8")
        )

        self.assert_refused_before_writing(
            self.recover(), "in the tree but not the manifest: persona/extra.md"
        )

    def test_a_listed_file_the_tree_lacks_is_refused(self):
        self.push_tree(lambda work: (work / "persona" / "rules.md").unlink())

        self.assert_refused_before_writing(
            self.recover(), "in the manifest but not the tree: persona/rules.md"
        )

    def test_a_digest_mismatch_is_refused(self):
        self.push_tree(
            lambda work: (work / "persona" / "rules.md").write_text("Be loud.\n", encoding="utf-8")
        )

        self.assert_refused_before_writing(
            self.recover(), "with a digest the manifest does not hold: persona/rules.md"
        )

    def test_a_board_schema_newer_than_the_product_is_refused_naming_both_heads(self):
        self.edit_manifest(lambda manifest: manifest.update(board_schema_head="9999_from_the_future"))

        result = self.recover()

        self.assert_refused_before_writing(result, "9999_from_the_future")
        self.assertIn(f"this product's schema head {head_revision()}", self.steps(result)["install"][1])

    def assert_nothing_written_beside(self, data_dir: Path, operator_file: Path) -> None:
        self.assertFalse(self.fixture.target.exists())
        self.assertEqual(operator_file.read_text(encoding="utf-8"), "the operator's\n")
        self.assertFalse((data_dir / "data-manifest.json").exists())
        self.assertFalse((data_dir / "backup").exists())
        self.assertEqual(self.leftovers(), [])

    def test_a_live_root_deeper_inside_the_data_directory_is_refused(self):
        """The reviewer's reproduction: `data_dir: ../..` into `<data>/foreign-parent/instance`."""
        data_dir = self.root / "ummanu-data"
        self.fixture.target = data_dir / "foreign-parent" / "instance"
        self.relocate_data_dir("../..")
        self.fixture.data_dir = data_dir
        operator_file = data_dir / "foreign-parent" / "operator-file.txt"
        operator_file.parent.mkdir(parents=True)
        operator_file.write_text("the operator's\n", encoding="utf-8")

        result = self.recover()

        self.assertEqual(result.status, "failed", result.render())
        refusal = self.steps(result)["install"][1]
        self.assertIn(
            f"the live root {self.fixture.target} lies inside the data directory {data_dir}", refusal
        )
        self.assertIn("not a direct child of it; this layout is not supported, nothing was written", refusal)
        self.assert_nothing_written_beside(data_dir, operator_file)

    def test_a_foreign_sibling_of_a_direct_child_live_root_is_refused(self):
        """Shape (b) leaves out the live root and its staging exactly; any other entry is data ummanu
        did not lay out."""
        data_dir = self.root / "ummanu-data"
        self.fixture.target = data_dir / "instance"
        self.relocate_data_dir("..")
        self.fixture.data_dir = data_dir
        operator_file = data_dir / "operator-file.txt"
        data_dir.mkdir()
        operator_file.write_text("the operator's\n", encoding="utf-8")

        result = self.recover()

        self.assertEqual(result.status, "failed", result.render())
        self.assertIn(
            f"data target {data_dir} is not an installation created by ummanu",
            self.steps(result)["install"][1],
        )
        self.assert_nothing_written_beside(data_dir, operator_file)

    def test_a_data_directory_inside_the_live_root_is_refused_before_anything_is_written(self):
        self.relocate_data_dir("data")
        target = self.fixture.target

        for prepared in ("absent", "empty"):
            with self.subTest(target=prepared):
                if prepared == "empty":
                    target.mkdir()
                result = self.recover()

                self.assertEqual(result.status, "failed", result.render())
                refusal = self.steps(result)["install"][1]
                self.assertIn(f"data directory {target / 'data'} inside the live root {target}", refusal)
                self.assertIn("not supported, nothing was written", refusal)
                if prepared == "absent":
                    self.assertFalse(target.exists())
                else:
                    self.assertEqual(list(target.iterdir()), [])
                self.assertFalse(self.fixture.data_dir.exists())
                self.assertEqual(self.leftovers(), [])

    def test_a_divergent_non_empty_live_root_is_refused_and_left_as_it_was(self):
        target = self.fixture.target
        (target / "persona").mkdir(parents=True)
        (target / "persona" / "rules.md").write_text("mine\n", encoding="utf-8")
        (target / "notes.txt").write_text("keep\n", encoding="utf-8")
        planted = {"persona/rules.md": b"mine\n", "notes.txt": b"keep\n"}

        # Without an `instance.yaml` it is no live root of any snapshot: today's refusal, unread remote.
        with mock.patch.object(installation, "_snapshot_checkout") as probe:
            result = self.recover()
        probe.assert_not_called()
        self.assertEqual(result.status, "failed")
        self.assertIn("is not a valid instance checkout", self.steps(result)["install"][1])
        self.assertEqual(self.live_files(), planted)

        # With one it is compared with the tip, and a divergence is refused.
        instance_yaml = (self.fixture.source / "instance.yaml").read_bytes()
        (target / "instance.yaml").write_bytes(instance_yaml)
        planted["instance.yaml"] = instance_yaml
        result = self.recover()

        self.assertEqual(result.status, "failed")
        refusal = self.steps(result)["install"][1]
        self.assertIn("is not empty and is not snapshot", refusal)
        self.assertIn("adapters/ummanu.yaml missing", refusal)
        self.assertEqual(self.live_files(), planted)
        self.assertFalse(self.repository.exists())
        self.assertEqual(self.leftovers(), [])

    def test_an_existing_snapshot_repository_the_remote_does_not_extend_is_refused(self):
        self.recover(failures={"_materialize_live_root": InstallError("interrupted after the bare clone")})
        kept = git(self.repository, "rev-parse", SNAPSHOT_REF)

        # Another history: an orphan commit of a valid snapshot tree, force-pushed over the tip.
        work = self.root / "rewrite"
        subprocess.run(["git", "clone", "--quiet", str(self.fixture.remote), str(work)], check=True)
        git(work, "checkout", "--quiet", "--orphan", "rewritten")
        (work / "persona" / "rules.md").write_text("Be loud.\n", encoding="utf-8")
        manifest = json.loads((work / SNAPSHOT_MANIFEST).read_text(encoding="utf-8"))
        manifest["files"]["persona/rules.md"] = hashlib.sha256(b"Be loud.\n").hexdigest()
        (work / SNAPSHOT_MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
        git(work, "add", "-A")
        git(
            work,
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "rewrite",
        )
        git(work, "push", "--quiet", "--force", "origin", "rewritten:main")

        result = self.recover()

        self.assertEqual(result.status, "failed")
        self.assertIn("which the remote tip", self.steps(result)["install"][1])
        self.assertEqual(git(self.repository, "rev-parse", SNAPSHOT_REF), kept)
        self.assertFalse(self.fixture.target.exists())

    def test_a_snapshot_on_another_branch_than_main_is_refused(self):
        git(self.fixture.remote, "branch", "-m", "main", "trunk")

        self.assert_refused_before_writing(self.recover(), "the exporter publishes main")


class SnapshotRetryTests(SnapshotRecoverCase):
    def progress(self) -> dict:
        return json.loads((self.fixture.data_dir / "recovery-progress.json").read_text(encoding="utf-8"))

    def assert_recovered(self) -> None:
        tip = self.fixture.tip
        self.assertEqual(git(self.repository, "rev-parse", SNAPSHOT_REF), tip)
        self.assertEqual(git(self.repository, "cat-file", "blob", SNAPSHOT_BASE_REF), tip)
        tree = git(self.repository, "ls-tree", "-r", "--name-only", "--full-tree", tip).splitlines()
        live = {path for path in self.live_files() if is_exported(path)}
        self.assertEqual(live, {path for path in tree if is_exported(path)})
        self.assertEqual(self.leftovers(), [])
        again = self.recover()
        self.assertEqual(again.status, "ok", again.render())
        steps = self.steps(again)
        for name in ("instance-checkout", "checkpoint", "board", "memory"):
            self.assertEqual(steps[name][0], "unchanged", (name, steps[name]))
        self.assertEqual(self.board.call_count, 1)

    def test_an_interruption_after_the_bare_clone_resumes_to_the_same_end_state(self):
        interrupted = self.recover(
            failures={"_materialize_live_root": InstallError("simulated interruption")}
        )
        self.assertEqual(interrupted.status, "failed")
        self.assertEqual(git(self.repository, "rev-parse", SNAPSHOT_REF), self.fixture.tip)
        self.assertFalse(self.fixture.target.exists())

        resumed = self.recover()

        self.assertEqual(resumed.status, "ok", resumed.render())
        self.assertEqual(self.steps(resumed)["instance-checkout"][0], "changed")
        self.assert_recovered()

    def test_an_interruption_after_materialisation_resumes_with_the_same_identity_and_no_second_import(self):
        interrupted = self.recover(failures={"check_prerequisites": InstallError("simulated interruption")})
        self.assertEqual(interrupted.status, "failed")
        live_before = self.live_files()
        # What bootstrap and the secret store leave in a live root is the host's own, never divergence.
        (self.fixture.target / "board-store.env").write_text("FIXTURE=1\n", encoding="utf-8")
        (self.fixture.target / "board-store.env").chmod(0o600)
        failed_import = self.recover(
            failures={"import_normalized_board": InstallError("simulated interruption")}
        )
        self.assertEqual(failed_import.status, "failed")
        identity = self.progress()["identity"]

        resumed = self.recover()

        self.assertEqual(resumed.status, "ok", resumed.render())
        self.assertEqual(self.steps(resumed)["instance-checkout"][0], "unchanged")
        self.assertIn("reused exporter snapshot", self.steps(resumed)["instance-checkout"][1])
        self.assertEqual(self.steps(resumed)["checkpoint"][0], "unchanged")
        self.assertEqual(self.progress()["identity"], identity)
        self.assertEqual({k: v for k, v in self.live_files().items() if k != "board-store.env"}, live_before)
        self.assert_recovered()
        self.assertEqual(self.progress()["identity"], identity)

    def test_a_fast_forward_of_an_interrupted_snapshot_repository_resumes_on_the_new_tip(self):
        self.recover(failures={"_materialize_live_root": InstallError("simulated interruption")})
        first = git(self.repository, "rev-parse", SNAPSHOT_REF)
        (self.fixture.source / "state" / "knowledge" / "decisions" / "two.md").write_text("# Two\n", "utf-8")
        tip = self.fixture.cut()
        self.assertNotEqual(tip, first)

        result = self.recover()

        self.assertEqual(result.status, "ok", result.render())
        self.assertEqual(git(self.repository, "rev-parse", SNAPSHOT_REF), tip)
        self.assertEqual(git(self.repository, "cat-file", "blob", SNAPSHOT_BASE_REF), tip)
        self.assertEqual(
            (self.fixture.target / "state" / "knowledge" / "decisions" / "two.md").read_text(
                encoding="utf-8"
            ),
            "# Two\n",
        )

    def test_a_dry_run_on_a_materialised_live_root_writes_nothing(self):
        self.recover(failures={"check_prerequisites": InstallError("simulated interruption")})
        before = (self.live_files(), git(self.repository, "rev-parse", SNAPSHOT_REF))

        result = self.recover(dry_run=True)

        self.assertEqual(result.status, "ok", result.render())
        self.assertEqual(self.steps(result)["instance-checkout"][0], "would-change")
        self.assertEqual(self.steps(result)["checkpoint"][0], "would-change")
        self.assertEqual((self.live_files(), git(self.repository, "rev-parse", SNAPSHOT_REF)), before)
        self.assertEqual(self.leftovers(), [])


class SnapshotBootstrapTests(SnapshotRecoverCase):
    """`ummanu bootstrap` against a snapshot remote, then `ummanu recover` (docs/RECOVERY.md, "Fresh
    install and recovery"): the clean-host sequence the stand drill runs.

    Bootstrap runs for real with root's view, the host check, the platform install and Docker stood
    in for: the shape decision, the snapshot repository, the live root and `provision`'s
    `board-store.env` are real, and so is every ownership handoff, recorded on its way to `chown`.
    The source carries a secret store, so recovery opens it from the phrase.
    """

    def setUp(self) -> None:
        super().setUp()
        service_env = self.root / "service.env"
        service_env.write_text(SERVICE_ENV, encoding="utf-8")
        service_env.chmod(0o600)
        with mock.patch.object(secret_store, "_new_key_params", side_effect=_fast_key_params):
            secret_store.initialize_store(self.fixture.source, phrase=PHRASE, actor="tester")
        secret_store.import_env_file(
            self.fixture.source,
            source=service_env,
            scope="installation",
            purpose="service credentials",
            actor="tester",
            materialize={"target": secret_store.MATERIALIZE_RUNTIME_ENV},
        )
        self.fixture.cut()
        self.phrase_file = self.root / "phrase.txt"
        self.phrase_file.write_text(PHRASE + "\n", encoding="utf-8")
        self.chowned: list[tuple[Path, int, int]] = []
        self.store_steps = mock.Mock()
        self.account = (os.getuid(), os.getgid())

    def bootstrap(self, **overrides) -> int:
        args = SimpleNamespace(
            instance_dir=str(self.fixture.target),
            instance_remote=str(self.fixture.remote),
            installation_user=getpass.getuser(),
            dry_run=False,
        )
        for name, value in overrides.items():
            setattr(args, name, value)
        return _stood_in_bootstrap(args, self.root / "opt", self.store_steps, self.chowned)

    def assert_bootstrapped(self, tip: str) -> None:
        target = self.fixture.target
        repo = self.repository
        # The snapshot repository at its resolved location, at the tip, with the takeover marker.
        self.assertEqual(git(repo, "rev-parse", "--is-bare-repository"), "true")
        self.assertEqual(git(repo, "rev-parse", SNAPSHOT_REF), tip)
        self.assertEqual(git(repo, "cat-file", "blob", SNAPSHOT_BASE_REF), tip)
        # A plain live root: exactly the allowlisted tree, plus the two host-local files bootstrap adds.
        tree = git(repo, "ls-tree", "-r", "--name-only", "--full-tree", tip).splitlines()
        live = self.live_files()
        self.assertEqual(
            sorted(live),
            sorted([*(path for path in tree if is_exported(path)), ".ummanu-bootstrap", "board-store.env"]),
        )
        self.assertFalse((target / ".git").exists())
        self.assertEqual(live[".ummanu-bootstrap"], b"created by ummanu bootstrap\n")
        # `board-store.env`: private, handed to the installation user after the store steps used it.
        store_file = store.store_path(target)
        self.assertEqual(store_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(store.resolve(target).dbname, store.parse(store_file).dbname)
        self.assertIn((store_file, *self.account), self.chowned)
        self.assertIn((target / ".ummanu-bootstrap", *self.account), self.chowned)
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(
            self.store_steps.mock_calls[-3:],
            [
                mock.call.install_platform(dry_run=False, runtime_user=getpass.getuser(), web_front=True),
                mock.call.migrate(target),
                mock.call.verify(target),
            ],
        )

    def test_bootstrap_lays_a_snapshot_out_as_recover_does_and_recover_then_finishes(self):
        tip = self.fixture.tip
        target = self.fixture.target

        self.assertEqual(self.bootstrap(), 0)

        self.assert_bootstrapped(tip)
        # The data directory the clone step laid out is the installation user's too.
        self.assertTrue((self.fixture.data_dir / "data-manifest.json").is_file())
        self.assertIn((self.fixture.data_dir, *self.account), self.chowned)

        result = self.recover(recovery_phrase_file=str(self.phrase_file))

        steps = self.steps(result)
        self.assertEqual(result.status, "ok", result.render())
        # The live root bootstrap laid out is this tip's (`live_root_state` same): nothing is cloned again.
        self.assertEqual(steps["instance-checkout"][0], "unchanged")
        self.assertIn(f"reused exporter snapshot {tip[:12]}", steps["instance-checkout"][1])
        self.assertEqual(git(self.repository, "rev-parse", SNAPSHOT_REF), tip)
        # The secret store opened from the phrase and materialised runtime.env.
        self.assertEqual(steps["secret-store"][0], "changed")
        self.assertTrue(secret_store.key_path(target).is_file())
        self.assertEqual((target / "runtime.env").read_text(encoding="utf-8"), SERVICE_ENV)
        # Board, memory, heads, and the first tick's writer.
        self.assertEqual(steps["board"], ("changed", "1 card(s) at parity"))
        self.board.assert_called_once_with(self.fixture.data_dir, instance=target)
        self.assertEqual(steps["memory"], ("changed", "rebuilt index for 1 fact(s)"))
        self.assertEqual(installed_pair(target).snapshot.parent, self.fixture.data_dir / "heads")
        self.assertEqual(installed_heads(target)["role_defaults"]["new_card"], HEAD)
        writer = tick_checkpoint_writer(self.fixture.data_dir, target)
        self.assertIsInstance(writer, SnapshotExporter)
        self.assertEqual(writer.snapshot_repo, self.repository.resolve())

    def test_a_second_bootstrap_and_a_second_recover_change_nothing(self):
        tip = self.fixture.tip
        self.assertEqual(self.bootstrap(), 0)
        credentials = store.store_path(self.fixture.target).read_bytes()
        self.assertEqual(self.recover(recovery_phrase_file=str(self.phrase_file)).status, "ok")

        self.assertEqual(self.bootstrap(), 0)

        # Same tip, same credentials, and the live root still the tip's plus host-local files.
        self.assertEqual(git(self.repository, "rev-parse", SNAPSHOT_REF), tip)
        self.assertEqual(store.store_path(self.fixture.target).read_bytes(), credentials)
        self.assertEqual(self.leftovers(), [])
        again = self.recover(recovery_phrase_file=str(self.phrase_file))
        self.assertEqual(again.status, "ok", again.render())
        steps = self.steps(again)
        for name in ("instance-checkout", "checkpoint", "board", "memory"):
            self.assertEqual(steps[name][0], "unchanged", (name, steps[name]))
        self.assertEqual(self.board.call_count, 1)
        self.assertEqual(store.store_path(self.fixture.target).read_bytes(), credentials)

    def test_a_live_root_inside_the_data_directory_bootstraps_and_recovers(self):
        data_dir = self.root / "ummanu-data"
        self.fixture.target = data_dir / "instance"
        tip = self.relocate_data_dir("..")
        self.fixture.data_dir = data_dir

        self.assertEqual(self.bootstrap(), 0)

        repository = data_dir / "backup" / "instance.git"
        self.assertEqual(git(repository, "rev-parse", SNAPSHOT_REF), tip)
        self.assertTrue((data_dir / "data-manifest.json").is_file())
        self.assertIn((data_dir, *self.account), self.chowned)
        result = self.recover(recovery_phrase_file=str(self.phrase_file))
        self.assertEqual(result.status, "ok", result.render())
        self.assertEqual(self.steps(result)["instance-checkout"][0], "unchanged")
        self.assertEqual(self.steps(result)["checkpoint"][0], "changed")
        self.assertEqual(
            tick_checkpoint_writer(data_dir, self.fixture.target).snapshot_repo, repository.resolve()
        )

    def test_a_plain_install_after_a_snapshot_bootstrap_is_the_first_install_as_after_a_legacy_one(self):
        self.assertEqual(self.bootstrap(), 0)

        result = self.recover(recover=False, recovery_phrase_file=str(self.phrase_file))

        self.assertEqual(result.status, "ok", result.render())
        self.assertIn("reused exporter snapshot", self.steps(result)["instance-checkout"][1])
        self.assertEqual(self.steps(result)["board"][0], "changed")
        # The first install consumes the stamp, and a second one is refused as an existing installation.
        self.assertFalse((self.fixture.target / ".ummanu-bootstrap").exists())
        again = self.recover(recover=False)
        self.assertEqual(again.status, "failed")
        self.assertIn("choose --recover", self.steps(again)["install"][1])

    def test_a_preview_against_an_absent_target_stays_offline(self):
        with mock.patch.object(bootstrap_module, "_snapshot_checkout") as probe:
            self.assertEqual(self.bootstrap(dry_run=True), 0)

        probe.assert_not_called()
        self.assertFalse(self.fixture.target.exists())


class LegacyShapeTests(unittest.TestCase):
    def test_a_remote_tip_without_a_manifest_takes_the_legacy_clone_step(self):
        # A bare remote, and a work tree named as the remote (as tests/test_secret_recover.py does).
        for bare in (True, False):
            with self.subTest(bare=bare), tempfile.TemporaryDirectory(prefix="legacy-shape-") as temporary:
                root = Path(temporary)
                source, target, data = root / "source", root / "instance", root / "data"
                source.mkdir()
                _checkpoint(source, data)
                _git(source, "init", "-b", "main")
                _git(source, "add", ".")
                _git(source, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-m", "x")
                remote = source
                if bare:
                    remote = root / "instance.git"
                    subprocess.run(
                        ["git", "clone", "--quiet", "--bare", str(source), str(remote)], check=True
                    )
                legacy_clone = mock.Mock(wraps=installation._clone_or_reuse)

                with (
                    mock.patch.object(installation, "_clone_or_reuse", legacy_clone),
                    mock.patch.object(installation, "check_prerequisites"),
                    mock.patch.object(installation, "import_normalized_board", return_value=1),
                    mock.patch.object(installation, "rebuild_memory_index", return_value=1),
                    mock.patch.object(
                        installation, "materialize_host", return_value=SimpleNamespace(steps=[])
                    ),
                    mock.patch.object(
                        installation,
                        "materialize_pipeline_state",
                        return_value=SimpleNamespace(records=0, changed=False),
                    ),
                    mock.patch.object(installation, "provision_project_checkouts", return_value=[]),
                    mock.patch.object(installation, "provision_codex_home", return_value=0),
                    mock.patch.object(installation, "mark_reconcile_applied"),
                    mock.patch.object(installation, "restore_findings", return_value=[]),
                ):
                    result = installation.install(_args(SimpleNamespace(target=target, remote=remote)))

                self.assertEqual(result.status, "ok", result.render())
                legacy_clone.assert_called_once()
                self.assertEqual(
                    {step.name: step.detail for step in result.steps}["instance-checkout"],
                    "cloned private instance remote",
                )
                self.assertTrue((target / ".git").is_dir())
                self.assertTrue((target / "state" / "board" / "cards.ndjson").is_file())
                self.assertFalse((data / "backup").exists())
                self.assertEqual(
                    sorted(p.name for p in root.iterdir() if p.name.startswith(".instance.")), []
                )

    def test_bootstrap_clones_a_legacy_remote_as_before_after_reading_its_shape(self):
        with tempfile.TemporaryDirectory(prefix="legacy-shape-") as temporary:
            root = Path(temporary)
            source, remote, target = root / "source", root / "instance.git", root / "instance"
            source.mkdir()
            _checkpoint(source, root / "data")
            _git(source, "init", "-b", "main")
            _git(source, "add", ".")
            _git(source, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-m", "x")
            subprocess.run(["git", "clone", "--quiet", "--bare", str(source), str(remote)], check=True)
            args = SimpleNamespace(
                instance_dir=str(target),
                instance_remote=str(remote),
                installation_user=getpass.getuser(),
                dry_run=False,
            )
            probe = mock.Mock(wraps=installation._snapshot_checkout)
            chowned: list[tuple[Path, int, int]] = []

            for _run in range(2):
                with mock.patch.object(bootstrap_module, "_snapshot_checkout", probe):
                    self.assertEqual(_stood_in_bootstrap(args, root / "opt", mock.Mock(), chowned), 0)

            # The first run read the shape and found no manifest; the rerun of a checkout did not.
            probe.assert_called_once()
            self.assertEqual(git(target, "rev-parse", "HEAD"), git(remote, "rev-parse", "main"))
            self.assertEqual(git(target, "status", "--porcelain", "--untracked-files=no"), "")
            exclude = (target / ".git" / "info" / "exclude").read_text(encoding="utf-8")
            self.assertIn("/.ummanu-bootstrap\n", exclude)
            self.assertIn("/runtime.env\n", exclude)
            self.assertEqual(store.store_path(target).stat().st_mode & 0o777, 0o600)
            # No data directory and no snapshot repository: nothing of the snapshot path ran.
            self.assertFalse((root / "data").exists())
            self.assertEqual(sorted(p.name for p in root.iterdir() if p.name.startswith(".instance.")), [])

    def test_a_work_tree_target_never_reads_the_remote_shape(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "instance"
            (target / ".git").mkdir(parents=True)
            with (
                mock.patch.object(installation, "_snapshot_checkout") as probe,
                mock.patch.object(installation, "_clone_or_reuse", side_effect=InstallError("legacy step")),
            ):
                result = installation.install(_args(SimpleNamespace(target=target, remote="remote")))

            probe.assert_not_called()
            self.assertIn("legacy step", {step.name: step.detail for step in result.steps}["install"])

    def test_a_fresh_install_and_an_offline_dry_run_never_read_the_remote_shape(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "instance"
            for overrides in ({"recover": False}, {"dry_run": True}):
                with (
                    self.subTest(**overrides),
                    mock.patch.object(installation, "_snapshot_checkout") as probe,
                    mock.patch.object(installation, "_ensure_installation_user"),
                    mock.patch.object(
                        installation, "_clone_or_reuse", side_effect=InstallError("legacy step")
                    ),
                ):
                    installation.install(_args(SimpleNamespace(target=target, remote="remote"), **overrides))
                probe.assert_not_called()


#: An open sprint whose observer is a head of the recovered canon: the import refuses it unless the
#: installed pair names that profile.
OPEN_SPRINT = {**SPRINT, "status": "open", "current_task": "", "observer": head_choice(HEAD)}
OWNER_PASSWORD = "OWNER_PASSWORD=from-the-store\n"


def _observer_preflight(data_dir: Path, *, instance: Path) -> int:
    """The board stand-in runs the import's own observer preflight over the exported sprints, against
    the pair installed at that moment (`restore._check_restored_observers`), and writes nothing."""
    sprints = restore._normalized_sprints(data_dir)
    restore._check_restored_observers(sprints, instance)
    return 1


class CleanHostRecoverTests(unittest.TestCase):
    """The clean-host recover of the ummanu-41 stand drill, on both remote shapes.

    The data directory starts without `heads/`; the export holds an open sprint whose observer is a
    head, so the board import needs the pair, and recover has to have written it first. The legacy
    remote carries a secret store whose file target is in the data directory (`webfront/
    owner-password.env` on the stand), written by recover's own secret step before the data-target
    check.
    """

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="clean-host-recover-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.board = mock.Mock(side_effect=_observer_preflight)

    def steps(self, result) -> dict[str, tuple[str, str]]:
        return {step.name: (step.status, step.detail) for step in result.steps}

    def assert_pair_written_before_the_board(self, result, target: Path, data_dir: Path) -> None:
        names = [step.name for step in result.steps]
        self.assertLess(names.index("head-registry"), names.index("board"), names)
        steps = self.steps(result)
        self.assertEqual(steps["head-registry"][0], "changed")
        self.board.assert_called_once_with(data_dir, instance=target)
        self.assertEqual(installed_pair(target).snapshot.parent, data_dir / "heads")
        self.assertIn(HEAD, installed_heads(target)["profiles"])

    def test_a_snapshot_recover_writes_the_pair_before_the_board_import_needs_it(self):
        fixture = exporter_remote(self.root, sprints=[OPEN_SPRINT])
        self.assertFalse((fixture.data_dir / "heads").exists())

        result = recover_snapshot(fixture, board=self.board)

        self.assertEqual(result.status, "ok", result.render())
        self.assert_pair_written_before_the_board(result, fixture.target, fixture.data_dir)
        # The host materializer's own head-registry step then finds the pair current.
        self.assertEqual(
            self.steps(result)["host"], ("unchanged", "materializer complete (0 changed step(s))")
        )

    def test_without_the_pair_the_import_refuses_as_on_the_stand(self):
        """The control: with recover's head-registry step taken out, the drill's failure comes back."""
        fixture = exporter_remote(self.root, sprints=[OPEN_SPRINT])

        with mock.patch.object(installation, "materialize_head_registry", return_value=("unchanged", "-")):
            result = recover_snapshot(fixture, board=self.board)

        self.assertEqual(result.status, "failed", result.render())
        self.assertIn(
            "sprint observer metadata cannot be validated: the head registry could not be read: "
            f"installation head snapshot {fixture.data_dir / 'heads' / 'heads.yaml'} is missing",
            self.steps(result)["install"][1],
        )

    def legacy_remote(self) -> tuple[Path, Path, Path, Path]:
        """A legacy checkpoint remote with the open sprint, the heads canon and a secret store whose
        one secret materializes into `<data>/webfront/owner-password.env`."""
        source, target, data = self.root / "source", self.root / "instance", self.root / "data"
        source.mkdir()
        _checkpoint(source, data, sprints=[OPEN_SPRINT])
        (source / "heads").mkdir()
        (source / "heads" / "heads.toml").write_text(canon(), encoding="utf-8")
        _git(source, "init", "-b", "main")
        _git(source, "add", ".")
        _git(source, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-m", "x")
        password = self.root / "owner-password.env"
        password.write_text(OWNER_PASSWORD, encoding="utf-8")
        password.chmod(0o600)
        with mock.patch.object(secret_store, "_new_key_params", side_effect=_fast_key_params):
            secret_store.initialize_store(source, phrase=PHRASE, actor="tester")
        materialized = data / "webfront" / "owner-password.env"
        secret_store.import_env_file(
            source,
            source=password,
            scope="installation",
            purpose="web owner password",
            actor="tester",
            materialize={"target": secret_store.MATERIALIZE_FILE, "path": str(materialized)},
        )
        # The legacy tick commits the store's files, never the installation key.
        _git(source, "add", "--", "secrets/catalog.yaml", "secrets/installation-key.json", "secrets/values")
        _git(source, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-m", "store")
        phrase = self.root / "phrase.txt"
        phrase.write_text(PHRASE + "\n", encoding="utf-8")
        return source, target, data, phrase

    def recover_legacy(self, source: Path, target: Path, phrase: Path):
        with (
            mock.patch.object(installation, "check_prerequisites"),
            mock.patch.object(installation, "import_normalized_board", self.board),
            mock.patch.object(installation, "rebuild_memory_index", return_value=1),
            mock.patch.object(installation, "materialize_host", return_value=SimpleNamespace(steps=[])),
            mock.patch.object(
                installation,
                "materialize_pipeline_state",
                return_value=SimpleNamespace(records=0, changed=False),
            ),
            mock.patch.object(installation, "provision_project_checkouts", return_value=[]),
            mock.patch.object(installation, "provision_codex_home", return_value=0),
            mock.patch.object(installation, "mark_reconcile_applied"),
            mock.patch.object(installation, "restore_findings", return_value=[]),
        ):
            return installation.install(
                _args(SimpleNamespace(target=target, remote=source), recovery_phrase_file=str(phrase))
            )

    def test_a_legacy_recover_onto_a_clean_host_completes_past_its_own_secret_file(self):
        source, target, data, phrase = self.legacy_remote()
        self.assertFalse(data.exists())

        result = self.recover_legacy(source, target, phrase)

        steps = self.steps(result)
        self.assertEqual(result.status, "ok", result.render())
        self.assertEqual(steps["instance-checkout"][1], "cloned private instance remote")
        # The store wrote the data directory's first file, and the data-target check took it as this
        # run's own: the layout was laid out around it.
        self.assertEqual(steps["secret-store"], ("changed", "1 env file(s) written"))
        password = data / "webfront" / "owner-password.env"
        self.assertEqual(password.read_text(encoding="utf-8"), OWNER_PASSWORD)
        self.assertEqual(password.stat().st_mode & 0o777, 0o600)
        self.assertEqual(steps["checkpoint"], ("changed", "1 board card(s), 0 run record(s)"))
        self.assertTrue((data / "data-manifest.json").is_file())
        self.assert_pair_written_before_the_board(result, target, data)
        # A rerun passes the same steps and changes nothing it already did.
        again = self.recover_legacy(source, target, phrase)
        self.assertEqual(again.status, "ok", again.render())
        for name in ("secret-store", "checkpoint", "head-registry", "board"):
            self.assertEqual(self.steps(again)[name][0], "unchanged", (name, self.steps(again)[name]))

    def test_a_foreign_file_at_the_secret_target_is_refused_before_anything_is_written(self):
        """The reviewer's reproduction: the store would replace it, and then nothing could tell it apart."""
        source, target, data, phrase = self.legacy_remote()
        foreign = data / "webfront" / "owner-password.env"
        foreign.parent.mkdir(parents=True)
        foreign.write_bytes(b"OPERATOR_PASSWORD=foreign\n")

        result = self.recover_legacy(source, target, phrase)

        self.assertEqual(result.status, "failed", result.render())
        self.assertIn(
            f"data target {data.resolve()} is not an installation created by ummanu, and it already holds "
            f"{foreign.resolve()}, where the secret store would write; nothing was written",
            self.steps(result)["install"][1],
        )
        self.assertEqual(foreign.read_bytes(), b"OPERATOR_PASSWORD=foreign\n")
        # The store never opened: no step, no installation key, no layout, no import.
        self.assertNotIn("secret-store", self.steps(result))
        self.assertFalse(secret_store.key_path(target).exists())
        self.assertFalse((data / "data-manifest.json").exists())
        self.board.assert_not_called()

    def test_a_laid_out_data_directory_has_its_secret_target_refreshed(self):
        source, target, data, phrase = self.legacy_remote()
        self.assertEqual(self.recover_legacy(source, target, phrase).status, "ok")
        password = data / "webfront" / "owner-password.env"
        password.write_text("OWNER_PASSWORD=stale\n", encoding="utf-8")

        again = self.recover_legacy(source, target, phrase)

        self.assertEqual(again.status, "ok", again.render())
        self.assertEqual(self.steps(again)["secret-store"], ("changed", "1 env file(s) written"))
        self.assertEqual(password.read_text(encoding="utf-8"), OWNER_PASSWORD)

    def test_a_foreign_file_in_the_data_directory_is_still_refused_by_name(self):
        source, target, data, phrase = self.legacy_remote()
        planted = {
            # Beside the store's file, and inside its directory next to it.
            "beside": (data / "operator-file.txt", "operator-file.txt"),
            "inside": (data / "webfront" / "notes.txt", "webfront"),
        }
        for case, (path, named) in planted.items():
            with self.subTest(case):
                shutil.rmtree(target, ignore_errors=True)
                shutil.rmtree(data, ignore_errors=True)
                path.parent.mkdir(parents=True)
                path.write_text("the operator's\n", encoding="utf-8")

                result = self.recover_legacy(source, target, phrase)

                self.assertEqual(result.status, "failed", result.render())
                self.assertIn(
                    f"data target {data} is not an installation created by ummanu (it holds {named}); "
                    "choose adopt or a clean recovery target",
                    self.steps(result)["install"][1],
                )
                self.assertEqual(path.read_text(encoding="utf-8"), "the operator's\n")
                self.assertFalse((data / "data-manifest.json").exists())
                self.board.assert_not_called()


if __name__ == "__main__":
    unittest.main()
