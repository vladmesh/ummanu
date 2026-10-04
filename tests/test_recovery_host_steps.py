"""Recovery host steps the infra drill (ummanu-48) found broken on a fresh host.

One test class per defect: a role worktree whose registration outlived its directory (P1b), the
fastembed cache layout (P2), Caddy for the web front (P4), a drained tick with an open sprint (P5),
disabled project bindings (P6) and root-created directories handed to the runtime user (P11). The
memory export after recover (P3) runs through the snapshot recovery in `tests/test_snapshot_recover.py`.

The re-drill after them (ummanu-52) left three more, under the same numbering: the web-front
Caddyfile nothing rendered on a recovered host (P12), doctor's `missing_on_host` for disabled
bindings (P13) and the embedding model recover kept resident beside the memory service (P14).
"""

from __future__ import annotations

import ast
import contextlib
import getpass
import io
import os
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

from ummanu import bootstrap, installation, restore, upgrade
from ummanu.config import validate_instance
from ummanu.dispatch import commands, production
from ummanu.dispatch.observer import DRAIN_DEFERRED_REASON, ObserverRecord, put_observers
from ummanu.dispatch.observer_fence import REASON_DEFERRED, REASON_NO_RECORD, observer_fence
from ummanu.host import HostInventory, build_doctor_expectations, build_expectations, inventory
from ummanu.installation import InstallError, ProjectProvisionResult
from ummanu.secret_store import generate_recovery_phrase, initialize_store, set_secret
from ummanu.sprint_observer import head_choice
from ummanu.web.app import ROUTES
from ummanu.webfront.caddyfile import HASH_SECRET_ID, SESSION_SECRET_ID
from ummanu.webfront.guard import unguarded_routes, upstreams

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "ummanu"


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _product_with_a_role_worktree(root: Path) -> Path:
    """A product checkout whose agent specs declare one role worktree, `curator`."""
    product = root / "product"
    agent = product / "src" / "ummanu" / "automations" / "agents" / "curator"
    agent.mkdir(parents=True)
    (agent / "automation.toml").write_text("name = 'curator'\n", encoding="utf-8")
    (product / "pyproject.toml").write_text(
        '[tool.ummanu]\nagent-specs = "src/ummanu/automations/agents"\n', encoding="utf-8"
    )
    _git("init", "--quiet", "-b", "main", str(product))
    _git("-C", str(product), "add", ".")
    _git("-C", str(product), "commit", "--quiet", "-m", "product")
    _git("-C", str(product), "remote", "add", "origin", str(product))
    return product


def _context(product: Path, **overrides) -> upgrade.UpgradeContext:
    values = {
        "instance_path": product.parent / "instance",
        "product_root": product,
        "base_branch": "main",
        "dry_run": False,
        "units": mock.Mock(),
    }
    values.update(overrides)
    return upgrade.UpgradeContext(**values)


class RoleWorktreeRegistrationTests(unittest.TestCase):
    """P1b: `git worktree add` refuses a path that is registered but whose directory is gone."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.product = _product_with_a_role_worktree(self.root)
        self.worktree = self.root / "workspaces" / "ummanu" / "curator"
        patcher = mock.patch.dict(os.environ, {"TA_WORKSPACES_ROOT": str(self.root / "workspaces")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_registered_worktree_whose_directory_was_lost_is_added_again(self) -> None:
        self.assertEqual(upgrade.step_worktrees(_context(self.product)).status, "changed")
        shutil.rmtree(self.worktree)

        result = upgrade.step_worktrees(_context(self.product))

        self.assertEqual(result.status, "changed", result.detail)
        self.assertEqual(result.detail, "created curator")
        self.assertTrue((self.worktree / ".git").is_file())
        self.assertEqual(
            _git("-C", str(self.worktree), "rev-parse", "HEAD"),
            _git("-C", str(self.product), "rev-parse", "HEAD"),
        )
        self.assertEqual(upgrade.step_worktrees(_context(self.product)).status, "unchanged")

    def test_a_genuine_refusal_reports_gits_fatal_line_not_its_progress_line(self) -> None:
        upgrade.step_worktrees(_context(self.product))
        # A lock is the operator's word that the registration stays: one `--force` does not override it.
        _git("-C", str(self.product), "worktree", "lock", str(self.worktree))
        shutil.rmtree(self.worktree)

        result = upgrade.step_worktrees(_context(self.product))

        self.assertEqual(result.status, "failed")
        self.assertIn("curator: git worktree: fatal:", result.detail)
        self.assertIn("locked", result.detail)
        self.assertNotIn("Preparing worktree", result.detail)

    def test_a_failure_without_a_fatal_line_reports_the_last_line(self) -> None:
        self.assertEqual(
            upgrade._git_failure_reason("Preparing worktree\nerror: one\nlast words\n"), "last words"
        )
        self.assertEqual(
            upgrade._git_failure_reason("hint: try\nfatal: the cause\nhint: more\n"), "fatal: the cause"
        )
        self.assertEqual(upgrade._git_failure_reason(""), "failed")


class EmbedderEnvironmentTests(unittest.TestCase):
    """P2: shared hub blobs split `model.onnx` from `model.onnx_data`, which onnxruntime refuses."""

    VARIABLE = "HF_HUB_DISABLE_SHARED_BLOBS"
    HUB_MODULES = ("fastembed", "huggingface_hub")

    def test_memory_service_sets_the_variable_before_its_first_third_party_import(self) -> None:
        tree = ast.parse((SOURCE / "memory_service.py").read_text(encoding="utf-8"))
        setting = next(
            index
            for index, node in enumerate(tree.body)
            if isinstance(node, ast.Assign)
            and ast.unparse(node.targets[0]) == f"os.environ['{self.VARIABLE}']"
            and ast.literal_eval(node.value) == "1"
        )
        imported_before = [
            name.split(".")[0]
            for node in tree.body[:setting]
            if isinstance(node, ast.Import | ast.ImportFrom)
            for name in (
                [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            )
        ]
        self.assertTrue(imported_before)
        self.assertTrue(
            all(name == "__future__" or name in sys.stdlib_module_names for name in imported_before),
            imported_before,
        )
        fastembed = next(
            index
            for index, node in enumerate(tree.body)
            if isinstance(node, ast.ImportFrom) and node.module == "fastembed"
        )
        self.assertGreater(fastembed, setting)

    def test_no_other_module_imports_the_embedding_stack(self) -> None:
        """One place sets it: every embedder (service, reindex, recover) is built through `memory_service`."""
        importers = set()
        for path in SOURCE.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = (
                    [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                    if isinstance(node, ast.ImportFrom)
                    else []
                )
                if any(name.split(".")[0] in self.HUB_MODULES for name in names):
                    importers.add(path.relative_to(SOURCE).as_posix())
        self.assertEqual(importers, {"memory_service.py"})
        reindex = ast.parse((SOURCE / "memory_reindex.py").read_text(encoding="utf-8"))
        self.assertIn(
            "memory_service",
            [alias.name for node in reindex.body if isinstance(node, ast.ImportFrom) for alias in node.names],
        )

    def test_the_hub_sees_the_variable_when_the_service_module_imports_it(self) -> None:
        """The import order itself, with the third-party modules stood in so no model is needed."""
        script = textwrap.dedent(
            """
            import importlib.abc, importlib.machinery, json, os, sys, types
            from unittest import mock

            STUBBED = {"numpy", "sqlite_vec", "fastembed", "huggingface_hub", "mcp"}
            seen = {}

            class Stub(types.ModuleType):
                def __getattr__(self, attr):
                    if attr.startswith("__"):
                        raise AttributeError(attr)
                    value = mock.MagicMock(name=f"{self.__name__}.{attr}")
                    setattr(self, attr, value)
                    return value

            class Finder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
                def find_spec(self, name, path=None, target=None):
                    if name.split(".")[0] in STUBBED:
                        return importlib.machinery.ModuleSpec(name, self, is_package=True)
                    return None

                def create_module(self, spec):
                    return Stub(spec.name)

                def exec_module(self, module):
                    seen[module.__name__] = os.environ.get("HF_HUB_DISABLE_SHARED_BLOBS")

            sys.meta_path.insert(0, Finder())
            import ummanu.memory_service
            print(json.dumps(seen))
            """
        )
        environment = {key: value for key, value in os.environ.items() if key != self.VARIABLE}
        environment["PYTHONPATH"] = str(ROOT / "src")
        completed = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, env=environment, check=False
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        import json

        seen = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertIn("fastembed", seen)
        self.assertEqual(seen["fastembed"], "1")


#: `host.web_front.sites`, as a line of a `host:` block.
SITES = "  web_front:\n    sites: [https://front.example, https://198.51.100.7]\n"
#: A bcrypt-shaped hash and a session secret long enough for the renderer; neither is a credential.
SAMPLE_HASH = "$2a$14$" + "x" * 53
SAMPLE_SESSION_SECRET = "fixture-session-secret-" + "x" * 32


def _write_instance(directory: Path, host: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "instance.yaml").write_text(f"version: 1\nname: drill\n{host}", encoding="utf-8")
    return directory


class WebFrontCaddyTests(unittest.TestCase):
    """P4: `ummanu-web-front.service` runs `/usr/bin/caddy`, which nothing installed."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def instance(self, host: str) -> Path:
        return _write_instance(self.root / f"instance-{len(list(self.root.iterdir()))}", host)

    def test_the_web_front_is_wanted_exactly_when_the_host_plan_would_install_its_unit(self) -> None:
        prefixed = "host:\n  unit_prefix: ummanu-\n"
        self.assertTrue(installation.web_front_wanted(self.instance(prefixed)))
        self.assertFalse(installation.web_front_wanted(self.instance("host:\n  memory_threads: 1\n")))
        self.assertFalse(
            installation.web_front_wanted(
                self.instance(prefixed + "  components:\n    web-front:\n      enabled: false\n")
            )
        )
        self.assertFalse(
            installation.web_front_wanted(
                self.instance(prefixed + "  foreign_units: [ummanu-web-front.service]\n")
            )
        )
        self.assertFalse(installation.web_front_wanted(self.root / "absent"))

    def test_bootstrap_masks_the_distribution_unit_and_installs_caddy_for_the_web_front(self) -> None:
        with (
            mock.patch("ummanu.bootstrap.os.geteuid", return_value=0),
            mock.patch("ummanu.bootstrap.shutil.which", return_value="/usr/bin/docker"),
            mock.patch("ummanu.bootstrap._docker_compose_available", return_value=True),
            mock.patch("ummanu.bootstrap.caddy_installed", return_value=False),
            mock.patch("ummanu.bootstrap._ensure_docker_ready"),
            mock.patch("ummanu.bootstrap._run") as run,
        ):
            bootstrap._install_platform(dry_run=False, runtime_user="dev", web_front=True)

        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [
                ["apt-get", "update"],
                ["systemctl", "mask", "caddy.service"],
                ["apt-get", "install", "--yes", "caddy"],
            ],
        )

    def test_bootstrap_installs_no_caddy_when_it_is_present_or_not_wanted(self) -> None:
        for web_front, present in ((True, True), (False, False)):
            with (
                mock.patch("ummanu.bootstrap.os.geteuid", return_value=0),
                mock.patch("ummanu.bootstrap.shutil.which", return_value="/usr/bin/docker"),
                mock.patch("ummanu.bootstrap._docker_compose_available", return_value=True),
                mock.patch("ummanu.bootstrap.caddy_installed", return_value=present),
                mock.patch("ummanu.bootstrap._ensure_docker_ready"),
                mock.patch("ummanu.bootstrap._run") as run,
            ):
                bootstrap._install_platform(dry_run=False, web_front=web_front)
            run.assert_not_called()

    def test_bootstrap_asks_the_installed_instance_whether_the_web_front_is_wanted(self) -> None:
        target = self.root / "instance"

        def lay_out(_remote: str, directory: Path, **_kwargs: object) -> str:
            _write_instance(directory, "host:\n  unit_prefix: ummanu-\n")
            return "cloned"

        args = SimpleNamespace(
            instance_dir=str(target), instance_remote="remote", installation_user="dev", dry_run=False
        )
        steps = mock.Mock()
        with (
            mock.patch("ummanu.bootstrap.os.geteuid", return_value=0),
            mock.patch("ummanu.bootstrap._host_supported"),
            mock.patch("ummanu.bootstrap._ensure_installation_user"),
            mock.patch("ummanu.bootstrap._reads_remote_shape", return_value=False),
            mock.patch("ummanu.bootstrap._clone_or_reuse", side_effect=lay_out),
            mock.patch("ummanu.bootstrap._mark_bootstrap_checkout"),
            mock.patch("ummanu.bootstrap._install_platform", steps.install_platform),
            mock.patch("ummanu.bootstrap._set_installation_owner"),
            mock.patch("ummanu.bootstrap.provision_board_store"),
            mock.patch("ummanu.bootstrap.migrate_instance"),
            mock.patch("ummanu.bootstrap.verify_board_store_roles"),
            mock.patch("builtins.print"),
        ):
            self.assertEqual(bootstrap.bootstrap(args), 0)

        steps.install_platform.assert_called_once_with(dry_run=False, runtime_user="dev", web_front=True)

    def test_prerequisites_refuse_up_front_and_name_caddy(self) -> None:
        instance = self.instance("host:\n  unit_prefix: ummanu-\n")
        with (
            mock.patch("ummanu.installation.caddy_installed", return_value=False),
            mock.patch("ummanu.installation.board_client"),
            mock.patch("ummanu.installation.TaskReader"),
            self.assertRaisesRegex(InstallError, r"caddy prerequisite failed: .*web-front.*/usr/bin/caddy"),
        ):
            installation.check_prerequisites(instance)

    def test_prerequisites_pass_caddy_when_it_is_installed_or_not_wanted(self) -> None:
        cases = (
            # An enabled front also needs its sites since ummanu-53 (`WebFrontSitesTests`).
            (self.instance("host:\n  unit_prefix: ummanu-\n" + SITES), True),
            (
                self.instance(
                    "host:\n  unit_prefix: ummanu-\n  components:\n    web-front:\n      enabled: false\n"
                ),
                False,
            ),
        )
        for instance, present in cases:
            with (
                mock.patch("ummanu.installation.caddy_installed", return_value=present),
                mock.patch("ummanu.installation.board_client"),
                mock.patch("ummanu.installation.TaskReader") as reader,
            ):
                installation.check_prerequisites(instance)
            reader.return_value.list.assert_called_once_with()


class DrainedTickFenceTests(unittest.TestCase):
    """P5: a drain launches no observer, so an open sprint's missing observer is deferred, not failed."""

    HEAD = "claude-observer"
    CARD: ClassVar[dict[str, str]] = {
        "ref": "ummanu-1",
        "project": "ummanu",
        "sprint": "sprint:1",
        "state": "Ready",
    }

    def runtime(self) -> SimpleNamespace:
        sprint = {
            "ref": "sprint:1",
            "status": "open",
            "observer": head_choice(self.HEAD),
            "reservations": ["ummanu"],
        }
        return SimpleNamespace(
            sprints=SimpleNamespace(list=lambda statuses=None: [dict(sprint)]),
            catalog=SimpleNamespace(observer_profile=lambda head: {"head": head}),
            reader=SimpleNamespace(list=lambda: [dict(self.CARD)]),
            audit=None,
            owner="drill",
            host=None,
            data_dir=Path("/nonexistent-data"),
            production_state=mock.Mock(),
        )

    def payload(self, record: ObserverRecord | None = None) -> dict:
        payload: dict = {}
        if record is not None:
            put_observers(payload, {"sprint:1": record})
        return payload

    def deferred(self, reason: str) -> ObserverRecord:
        return ObserverRecord(sprint="sprint:1", head=self.HEAD, state="deferred", deferred_reason=reason)

    def test_a_drained_sprint_without_an_observer_is_fenced_in_a_deferred_state(self) -> None:
        for record, reason in (
            (None, REASON_NO_RECORD),
            (self.deferred(DRAIN_DEFERRED_REASON), REASON_DEFERRED),
        ):
            fence = observer_fence(self.runtime(), self.payload(record), pause_mode="drain")

            [outcome] = fence["outcomes"]
            self.assertEqual(outcome["status"], "deferred")
            self.assertEqual(outcome["action"], "observer-fenced")
            self.assertEqual(outcome["observer_reason"], reason)
            self.assertIn("waits for the resume", outcome["reason"])
            self.assertEqual(production.degraded_actions(fence["outcomes"]), [])
            # The cards stay fenced all the same.
            self.assertEqual(fence["sprints"], {"sprint:1"})
            self.assertIn("ummanu-1", fence["refs"])
            self.assertTrue(production.fenced_task(fence, self.CARD))

    def test_a_launch_that_was_due_and_failed_stays_critical(self) -> None:
        failed = self.deferred("host refused the observer launch; retry in 30s")
        for pause_mode, record in (("drain", failed), ("", failed), ("", None)):
            fence = observer_fence(self.runtime(), self.payload(record), pause_mode=pause_mode)

            [outcome] = fence["outcomes"]
            self.assertEqual(outcome["status"], "critical", (pause_mode, record))
            self.assertEqual(production.degraded_actions(fence["outcomes"]), [outcome])

    def tick_exit(self, record: ObserverRecord | None) -> tuple[int, dict]:
        """One drained production tick through the command's exit-status rule, everything but the
        fence and the observer pass stood in for."""
        runtime = self.runtime()
        payload = self.payload(record)
        pause = {"paused": True, "mode": "drain"}
        skipped = {
            "status": "skipped",
            "step": "observer-reconcile",
            "sprint": "sprint:1",
            "action": "observer-launch-skipped",
            "reason": DRAIN_DEFERRED_REASON,
        }
        results: list[dict] = []

        def tick(runtime: SimpleNamespace) -> dict:
            results.append(production._production_tick_work(runtime, payload, {}, pause, None))
            return results[-1]

        args = SimpleNamespace(instance="instance", data_dir=None, host_mode="real", owner="drill")
        with (
            mock.patch.object(commands, "runtime_from_args", return_value=runtime),
            mock.patch.object(commands, "bound_data_dir", return_value=contextlib.nullcontext()),
            mock.patch.object(
                production.attempt_accounting, "publish_pending_attempt_usage", return_value=[]
            ),
            mock.patch.object(
                production.attempt_accounting, "publish_pending_attempt_outcomes", return_value=[]
            ),
            mock.patch.object(production, "_production_tasks", return_value=[dict(self.CARD)]),
            mock.patch.object(production, "_reconcile_production", return_value=[]),
            mock.patch.object(production, "_advance_active", return_value=([], [], {})),
            mock.patch.object(production, "reconcile_post_merge_watches", return_value=[]),
            mock.patch.object(production, "reconcile_after_merge", return_value=[]),
            mock.patch.object(production, "_reconcile_sprint_budget", return_value=[]),
            mock.patch.object(production, "reconcile_observers", return_value=[skipped]),
            mock.patch.object(production, "_production_claim_ready") as claim,
            mock.patch.object(production, "reconcile_origin_returns", return_value=[]),
            mock.patch.object(production, "_coordinate_checkpoint", return_value=(None, None)),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code = commands._run_production(args, tick)
        claim.assert_not_called()
        return code, results[-1]

    def test_a_drained_tick_with_an_open_sprint_and_no_observer_exits_zero(self) -> None:
        for record in (None, self.deferred(DRAIN_DEFERRED_REASON)):
            code, result = self.tick_exit(record)

            self.assertEqual(result["status"], "ok", result)
            self.assertEqual(code, 0)
            fenced = [action for action in result["actions"] if action["step"] == "observer-fence"]
            self.assertEqual([action["status"] for action in fenced], ["deferred"])

    def test_a_drained_tick_over_a_failed_launch_still_exits_three(self) -> None:
        code, result = self.tick_exit(self.deferred("host refused the observer launch; retry in 30s"))

        self.assertEqual(result["status"], "degraded")
        self.assertEqual(code, 3)


class DisabledBindingTests(unittest.TestCase):
    """P6: a retired binding (`enabled: false`) is not cloned on a recovered host."""

    def test_a_disabled_binding_is_skipped_as_disabled_and_is_not_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binding = {
                "id": "retired-instance",
                "repo": str(root / "retired-instance"),
                "remote": "https://github.com/example/retired-instance.git",
                "default_branch": "main",
                "enabled": False,
                "adapter": "retired",
            }
            with mock.patch("ummanu.installation.RemoteExecution") as remote:
                [result] = installation.provision_project_checkouts([binding], None, instance_dir=root)

            remote.assert_not_called()
            self.assertEqual(
                result,
                ProjectProvisionResult(
                    "retired-instance",
                    "not-inspected",
                    "not-contacted",
                    "disabled",
                    "disabled",
                    "binding is disabled",
                    False,
                ),
            )
            self.assertFalse((root / "retired-instance").exists())
            recovered = installation.InstallResult(projects=[result])
            self.assertEqual(recovered.status, "ok")


class RuntimeOwnershipTests(unittest.TestCase):
    """P11: directories a root recovery creates in the runtime user's home are handed to that user."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.account = SimpleNamespace(pw_uid=123, pw_gid=456)

    @contextlib.contextmanager
    def as_root(self):
        with (
            mock.patch("ummanu.upgrade.os.geteuid", return_value=0),
            mock.patch("ummanu.upgrade.pwd.getpwnam", return_value=self.account),
            mock.patch("ummanu.upgrade.os.chown") as chown,
        ):
            yield chown

    def test_role_skill_roots_and_the_directories_above_them_go_to_the_runtime_user(self) -> None:
        home = self.root / "home"
        data = self.root / "data"
        home.mkdir()
        data.mkdir()
        context = _context(
            ROOT,
            instance_path=self.root / "instance",
            runtime_user="dev",
            runtime_home=home,
            report=SimpleNamespace(data_dir=data),
        )
        (self.root / "instance").mkdir()

        with self.as_root() as chown:
            result = upgrade.step_role_skills(context)
            unchanged = upgrade.step_role_skills(context)

        self.assertEqual(result.status, "changed", result.detail)
        self.assertEqual(unchanged.status, "unchanged", unchanged.detail)
        owned = {Path(call.args[0]) for call in chown.call_args_list}
        self.assertTrue((home / ".claude" / "skills").is_dir())
        for created in (
            home / ".claude",
            home / ".claude" / "skills",
            home / ".hermes",
            home / ".hermes" / "skills",
            home / ".config",
            home / ".config" / "orca",
            home / ".config" / "orca" / "codex-runtime-home",
            data / "po",
        ):
            self.assertIn(created, owned)
        skill = next((home / ".claude" / "skills").iterdir())
        self.assertIn(skill, owned)
        self.assertNotIn(home, owned)
        self.assertNotIn(data, owned)
        self.assertTrue(all(call.args[1:] == (123, 456) for call in chown.call_args_list))

    def test_the_projects_directory_root_creates_for_a_checkout_goes_to_the_runtime_user(self) -> None:
        home = self.root / "home"
        home.mkdir()
        binding = {
            "id": "personal-site",
            "repo": str(home / "projects" / "personal-site"),
            "remote": "https://github.com/example/personal-site.git",
            "default_branch": "main",
            "enabled": True,
        }

        def clone(staging: Path, **_kwargs: object) -> None:
            staging.mkdir()
            (staging / ".git").mkdir()

        remote = mock.Mock(transport="https")
        remote.run_clone.side_effect = clone
        with (
            mock.patch("ummanu.installation.RemoteExecution", return_value=remote),
            mock.patch("ummanu.installation._set_installation_owner") as owner,
        ):
            [result] = installation.provision_project_checkouts([binding], "dev", instance_dir=self.root)

        self.assertEqual(result.outcome, "cloned", result)
        self.assertEqual(owner.call_args_list[0], mock.call(home / "projects", "dev"))


class WebFrontSitesTests(unittest.TestCase):
    """P12: the Caddyfile is rendered from `host.web_front.sites` before the host step starts the front."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / "data"
        self.caddyfile = self.data / "webfront" / "Caddyfile"

    def instance(self, *, sites: bool = True, password: bool = True) -> Path:
        """A live root with a real secret store holding the front's hash and session secret."""
        instance = self.root / "instance"
        instance.mkdir()
        (instance / "instance.yaml").write_text(
            f"version: 1\nname: drill\ndata_dir: {self.data}\n"
            "offsite:\n  instance_remote: git@example.invalid:x/y\n"
            "host:\n  unit_prefix: ummanu-\n" + (SITES if sites else ""),
            encoding="utf-8",
        )
        initialize_store(instance, phrase=generate_recovery_phrase(), actor="test")
        secrets = {SESSION_SECRET_ID: SAMPLE_SESSION_SECRET}
        if password:
            secrets[HASH_SECRET_ID] = SAMPLE_HASH
        for secret_id, value in secrets.items():
            set_secret(
                instance,
                secret_id=secret_id,
                value=value.encode("utf-8"),
                scope="installation",
                purpose="web front fixture",
                actor="test",
            )
        return instance

    def context(self, instance: Path) -> upgrade.UpgradeContext:
        report = validate_instance(instance)
        self.assertTrue(report.ok, report.errors)
        return upgrade.UpgradeContext(
            instance_path=instance,
            product_root=self.root / "product",
            base_branch="main",
            dry_run=False,
            units=mock.Mock(),
            report=report,
        )

    def file_state(self) -> tuple[bytes, int, int]:
        info = self.caddyfile.stat()
        return self.caddyfile.read_bytes(), info.st_mtime_ns, info.st_mode

    def assert_guarded(self, text: str) -> None:
        self.assertEqual(unguarded_routes(text, ROUTES), ())
        self.assertEqual(set(upstreams(text)), {"127.0.0.1:8787"})
        self.assertIn("https://front.example, https://198.51.100.7 {", text)

    def test_the_render_runs_before_the_host_step(self) -> None:
        self.assertLess(
            upgrade.STEPS.index(upgrade.step_web_front_config), upgrade.STEPS.index(upgrade.step_host)
        )

    def test_install_and_recover_render_a_guarded_front_before_their_host_step(self) -> None:
        """Through `materialize_host`, the materializer both install and recover call."""
        instance = self.instance()
        seen = []

        def host(context: upgrade.UpgradeContext) -> upgrade.StepResult:
            seen.append(self.caddyfile.read_text(encoding="utf-8"))
            return upgrade.StepResult("host", "unchanged", "")

        with (
            mock.patch.object(installation, "STEPS", (upgrade.step_web_front_config, host)),
            mock.patch.object(installation, "step_host", host),
        ):
            result = installation.materialize_host(
                instance,
                self.root / "product",
                installation_user=getpass.getuser(),
                before_host=lambda _: None,
            )

        self.assertEqual(
            [(step.name, step.status) for step in result.steps],
            [("web-front-config", "changed"), ("host", "unchanged")],
        )
        [text] = seen
        self.assert_guarded(text)
        self.assertEqual(self.caddyfile.stat().st_mode & 0o777, 0o600)

    def test_a_current_front_is_not_rewritten(self) -> None:
        context = self.context(self.instance())
        self.assertEqual(upgrade.step_web_front_config(context).status, "changed")
        self.assertTrue(context.web_front_config_changed)
        before = (self.caddyfile.read_bytes(), self.caddyfile.stat().st_mtime_ns)

        again = self.context(context.instance_path)
        result = upgrade.step_web_front_config(again)

        self.assertEqual(result.status, "unchanged", result.detail)
        self.assertFalse(again.web_front_config_changed)
        self.assertEqual((self.caddyfile.read_bytes(), self.caddyfile.stat().st_mtime_ns), before)

    def test_an_upgrade_without_sites_leaves_the_existing_caddyfile_byte_unchanged(self) -> None:
        """Production before its sites are in instance config: its rendered file keeps working."""
        context = self.context(self.instance(sites=False))
        self.caddyfile.parent.mkdir(parents=True)
        self.caddyfile.write_text("# rendered by hand\nhttps://front.example {\n}\n", encoding="utf-8")
        self.caddyfile.chmod(0o600)
        before = self.file_state()

        with mock.patch("ummanu.upgrade.state_repo.run_as_git_child") as render:
            result = upgrade.step_web_front_config(context)

        render.assert_not_called()
        self.assertEqual(result.status, "unchanged", result.detail)
        self.assertIn("host.web_front.sites is not set", result.detail)
        self.assertEqual(self.file_state(), before)
        self.assertEqual(sorted(path.name for path in self.caddyfile.parent.iterdir()), ["Caddyfile"])

    def test_an_upgrade_with_neither_sites_nor_a_file_names_the_setting_and_writes_nothing(self) -> None:
        result = upgrade.step_web_front_config(self.context(self.instance(sites=False)))

        self.assertEqual(result.status, "skipped")
        self.assertIn("host.web_front.sites", result.detail)
        self.assertFalse(self.caddyfile.parent.exists())

    def test_a_front_without_a_password_fails_and_names_set_password(self) -> None:
        result = upgrade.step_web_front_config(self.context(self.instance(password=False)))

        self.assertEqual(result.status, "failed")
        self.assertIn("ummanu web-front set-password", result.detail)
        self.assertFalse(self.caddyfile.exists())

    def test_a_disabled_front_is_not_rendered(self) -> None:
        instance = self.instance()
        config = instance / "instance.yaml"
        config.write_text(
            config.read_text(encoding="utf-8") + "  components:\n    web-front:\n      enabled: false\n",
            encoding="utf-8",
        )
        result = upgrade.step_web_front_config(self.context(instance))

        self.assertEqual(result.status, "skipped")
        self.assertFalse(self.caddyfile.exists())

    def test_prerequisites_refuse_an_enabled_front_without_sites_and_name_the_setting(self) -> None:
        instance = _write_instance(self.root / "bare", "host:\n  unit_prefix: ummanu-\n")
        with (
            mock.patch("ummanu.installation.caddy_installed", return_value=True),
            mock.patch("ummanu.installation.board_client"),
            mock.patch("ummanu.installation.TaskReader"),
            self.assertRaisesRegex(
                InstallError,
                r"web-front prerequisite failed: .*host\.web_front\.sites.*`ummanu web-front render",
            ),
        ):
            installation.check_prerequisites(instance)

    def test_the_schema_takes_https_site_addresses_only(self) -> None:
        cases = (("[https://front.example]", True), ("[http://front.example]", False), ("[]", False))
        for sites, valid in cases:
            with self.subTest(sites):
                directory = self.root / f"schema-{len(list(self.root.iterdir()))}"
                directory.mkdir()
                (directory / "instance.yaml").write_text(
                    f"version: 1\nname: drill\ndata_dir: {self.data}\n"
                    "offsite:\n  instance_remote: git@example.invalid:x/y\n"
                    f"host:\n  unit_prefix: ummanu-\n  web_front:\n    sites: {sites}\n",
                    encoding="utf-8",
                )
                self.assertEqual(validate_instance(directory).ok, valid)


class DisabledBindingExpectationTests(unittest.TestCase):
    """P13: doctor neither requires a disabled binding's checkout nor calls one that is there unmanaged."""

    def test_a_disabled_binding_yields_no_missing_on_host(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            projects = Path(temporary).resolve() / "projects"
            live, absent, present = (projects / name for name in ("live", "retired", "parked"))
            live.mkdir(parents=True)
            present.mkdir()
            bindings = [
                {"id": "live", "repo": str(live), "enabled": True},
                {"id": "retired", "repo": str(absent), "enabled": False},
                {"id": "parked", "repo": str(present), "enabled": False},
            ]
            instance = {"host": {"projects_root": str(projects)}}
            expected = build_doctor_expectations(instance, bindings, packaged=[])
            # What the live collector reports: every directory under host.projects_root.
            diff = inventory(expected, HostInventory(projects={str(live), str(present)}))["projects"]

        self.assertEqual(diff.missing_on_host, [])
        self.assertEqual(diff.unmanaged_on_host, [])
        self.assertEqual(diff.matched, [str(live)])

    def test_upgrade_s_expectations_skip_a_disabled_binding_as_recovery_does(self) -> None:
        bindings = [
            {"id": "live", "repo": "/srv/projects/live", "enabled": True},
            {"id": "retired", "repo": "/srv/projects/retired", "enabled": False},
        ]
        expected = build_expectations(bindings, {})

        self.assertEqual(expected.projects, {"live"})
        self.assertEqual(expected.dormant_projects, {"retired"})


def _fake_embedding_modules(record: Path, ballast: int, *, die: bool = False) -> dict[str, object]:
    """`ummanu.memory_service` and `ummanu.memory_reindex` without fastembed.

    The embedder writes the pid it was built in to `record` and holds `ballast` bytes it has touched,
    standing in for the resident model; the rebuild writes the index file and reports parity.
    """
    built: list[int] = []

    def build_document_embedder(model: str, cache_dir: Path, threads: int) -> object:
        built.append(os.getpid())
        record.write_text(str(os.getpid()), encoding="utf-8")
        return SimpleNamespace(model=bytearray(b"\x01") * ballast)

    def rebuild(canon, export, target_db, model, dim, document_embed=None, **_kwargs) -> dict:
        assert document_embed is not None
        if die:
            os.kill(os.getpid(), signal.SIGKILL)
        Path(target_db).parent.mkdir(parents=True, exist_ok=True)
        Path(target_db).write_text("index\n", encoding="utf-8")
        return {"parity": {"indexed": 2}}

    service = SimpleNamespace(build_document_embedder=build_document_embedder, built=built)
    return {"ummanu.memory_service": service, "ummanu.memory_reindex": SimpleNamespace(rebuild=rebuild)}


class EmbedderReleaseTests(unittest.TestCase):
    """P14: recover's rebuild holds the model in a child process, which is gone before the host step."""

    BALLAST = 64 * 1024 * 1024

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.instance = self.root / "instance"
        (self.instance / "state" / "memory" / "facts").mkdir(parents=True)
        self.record = self.root / "embedder-pid"

    def test_the_isolated_rebuild_builds_the_model_in_a_child_that_exits_with_it(self) -> None:
        modules = _fake_embedding_modules(self.record, self.BALLAST)
        with mock.patch.dict(sys.modules, modules):
            count = restore.rebuild_memory_index(self.data, self.instance, isolated=True)
        children = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * 1024

        self.assertEqual(count, 2)
        self.assertEqual(restore.restore_state(self.data)["memory_index"], "complete")
        self.assertTrue((self.data / "memory" / "index.sqlite").is_file())
        # Built in another process, and never in this one: nothing here can still reference it.
        self.assertNotEqual(int(self.record.read_text(encoding="utf-8")), os.getpid())
        self.assertEqual(modules["ummanu.memory_service"].built, [])
        # The child really held the stand-in model; it took that memory with it when it exited.
        self.assertGreaterEqual(children, self.BALLAST)

    def test_a_rebuild_child_the_kernel_kills_is_named_by_its_signal(self) -> None:
        with (
            mock.patch.dict(sys.modules, _fake_embedding_modules(self.record, 0, die=True)),
            self.assertRaisesRegex(restore.RestoreError, "memory rebuild process was killed by signal 9"),
        ):
            restore.rebuild_memory_index(self.data, self.instance, isolated=True)
        self.assertNotEqual(restore.restore_state(self.data).get("memory_index"), "complete")

    def test_recover_asks_for_the_isolated_rebuild_at_every_call(self) -> None:
        tree = ast.parse((SOURCE / "installation.py").read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and ast.unparse(node.func) == "rebuild_memory_index"
        ]
        self.assertEqual(len(calls), 2)
        for call in calls:
            isolated = {keyword.arg: keyword.value for keyword in call.keywords}.get("isolated")
            self.assertIsNotNone(isolated, ast.unparse(call))
            self.assertIs(ast.literal_eval(isolated), True)


if __name__ == "__main__":
    unittest.main()
