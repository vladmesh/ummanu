"""Which CODEX_HOME a Codex head runs with, resolved at launch (secretary-1710, secretary-1723).

The resolver has three rungs: the profile's `codex_home`, `TA_CODEX_HOME`, and
`<data_dir>/codex-home` when it holds a login (a non-empty `auth.json`). With none of them it fails
closed with `CodexHomeLoginMissing`, whose message names the fix; the legacy Orca home it used to
fall back to is gone (A20 step 7). Nothing here reads or writes the live homes: every data dir and
every legacy home is a temp dir.
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from ummanu.automations.agents.curator import discover
from ummanu.automations.agents.pipeline import codex_sessions as pipeline_codex_sessions
from ummanu.dispatch import commands as dispatch_commands, tui as dispatcher_tui
from ummanu.runtime import codex_home as codex_home_module, codex_preflight
from ummanu.runtime.codex_preflight import (
    CODEX_HOME_DATA_DIR,
    CODEX_HOME_ENV,
    CODEX_HOME_PROFILE,
    CodexHomeLoginMissing,
    CodexPreflightError,
    codex_home,
    resolve_codex_home,
)
from ummanu.runtime.head import HeadCommandError, render_head_command

SRC = Path(__file__).resolve().parents[1] / "src" / "ummanu"
RESOLVERS = {"codex_home", "resolve_codex_home"}


def _without(*names: str) -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key not in names}


class ResolverOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name) / "data"
        self.data_home = self.data_dir / "codex-home"
        self.data_home.mkdir(parents=True)
        # The suite pins TA_CODEX_HOME for every test; the lower rungs are only reachable without it.
        env = mock.patch.dict(
            os.environ, _without("TA_CODEX_HOME", "UMMANU_DATA_DIR", "UMMANU_INSTANCE"), clear=True
        )
        env.start()
        self.addCleanup(env.stop)

    def log_in(self) -> None:
        (self.data_home / "auth.json").write_text('{"tokens": "fixture"}\n', encoding="utf-8")

    def test_the_profile_home_wins_over_every_other_rung(self) -> None:
        self.log_in()
        os.environ["TA_CODEX_HOME"] = "/tmp/env-home"
        home = resolve_codex_home({"codex_home": "/tmp/profile-home"}, data_dir=self.data_dir)
        self.assertEqual((home.path, home.kind), ("/tmp/profile-home", CODEX_HOME_PROFILE))

    def test_ta_codex_home_wins_over_a_logged_in_data_dir_home(self) -> None:
        self.log_in()
        os.environ["TA_CODEX_HOME"] = "/tmp/env-home"
        home = resolve_codex_home({}, data_dir=self.data_dir)
        self.assertEqual((home.path, home.kind), ("/tmp/env-home", CODEX_HOME_ENV))

    def test_without_a_login_in_the_data_dir_home_the_resolver_fails_closed_naming_the_fix(self) -> None:
        """A20 step 7: no fallback to the legacy Orca home, and the refusal says what to do."""
        with self.assertRaises(CodexHomeLoginMissing) as caught:
            resolve_codex_home({}, data_dir=self.data_dir)
        self.assertIsInstance(caught.exception, CodexPreflightError)
        self.assertEqual(caught.exception.home, self.data_home)
        message = str(caught.exception)
        self.assertIn(f"log in under {self.data_home}", message)
        self.assertIn(f"CODEX_HOME={self.data_home} codex login", message)
        self.assertIn("copy an auth.json there", message)
        self.assertNotIn("orca", message)

    def test_an_empty_auth_file_is_not_a_login(self) -> None:
        (self.data_home / "auth.json").write_text("", encoding="utf-8")
        with self.assertRaises(CodexHomeLoginMissing):
            codex_home({}, data_dir=self.data_dir)

    def test_a_login_in_the_data_dir_home_selects_it(self) -> None:
        self.log_in()
        home = resolve_codex_home({}, data_dir=self.data_dir)
        self.assertEqual((home.path, home.kind), (str(self.data_home), CODEX_HOME_DATA_DIR))

    def test_the_data_dir_comes_from_the_environment_the_launch_carries(self) -> None:
        self.log_in()
        os.environ["UMMANU_DATA_DIR"] = str(self.data_dir)
        self.assertEqual(codex_home({}), str(self.data_home))

    def test_the_data_dir_comes_from_an_explicitly_selected_instance(self) -> None:
        self.log_in()
        instance = self.data_dir.parent / "instance"
        instance.mkdir()
        (instance / "instance.yaml").write_text(
            "version: 1\nname: codex-home\n"
            f"data_dir: {self.data_dir}\n"
            "offsite:\n  instance_remote: https://example.invalid/instance.git\n",
            encoding="utf-8",
        )
        os.environ["UMMANU_INSTANCE"] = str(instance)
        # The leaf resolver reads no instance file; the installation helper does, and so does a
        # launching process once it has bound the data dir.
        with self.assertRaises(CodexHomeLoginMissing):
            codex_home({})
        self.assertEqual(codex_home_module.selected_data_dir(), self.data_dir.resolve())
        self.assertEqual(
            codex_home_module.installation_codex_home().path, str(self.data_dir.resolve() / "codex-home")
        )
        with codex_home_module.bound_data_dir():
            self.assertEqual(codex_home({}), str(self.data_dir.resolve() / "codex-home"))
        self.assertNotIn("UMMANU_DATA_DIR", os.environ)

    def test_no_selected_installation_is_refused_with_the_fix(self) -> None:
        self.log_in()
        self.assertIsNone(codex_home_module.selected_data_dir())
        for resolve in (lambda: codex_home({}), lambda: codex_home_module.installation_codex_home()):
            with self.assertRaises(CodexHomeLoginMissing) as caught:
                resolve()
            self.assertIsNone(caught.exception.home)
            self.assertIn("UMMANU_DATA_DIR", str(caught.exception))
            self.assertIn("<data_dir>/codex-home", str(caught.exception))

    def test_a_bound_data_dir_is_scoped_and_never_replaces_the_operators(self) -> None:
        self.log_in()
        with codex_home_module.bound_data_dir(self.data_dir):
            self.assertEqual(os.environ["UMMANU_DATA_DIR"], str(self.data_dir))
            self.assertEqual(codex_home({}), str(self.data_home))
        self.assertNotIn("UMMANU_DATA_DIR", os.environ)

        os.environ["UMMANU_DATA_DIR"] = "/tmp/operator-data"
        with codex_home_module.bound_data_dir(self.data_dir):
            self.assertEqual(os.environ["UMMANU_DATA_DIR"], "/tmp/operator-data")
        self.assertEqual(os.environ["UMMANU_DATA_DIR"], "/tmp/operator-data")

    def test_a_dispatcher_operation_launches_against_its_own_data_dir(self) -> None:
        """The production entry binds the runtime's data dir around the operation that launches."""
        self.log_in()
        seen: list[str] = []

        def operation(_runtime: object) -> dict[str, str]:
            seen.append(codex_home({}))
            return {"status": "ok"}

        runtime = mock.Mock(data_dir=self.data_dir)
        args = mock.Mock(instance="unused", data_dir=None, host_mode="noop", owner="ummanu-production")
        with (
            mock.patch("ummanu.dispatch.commands.runtime_from_args", return_value=runtime),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(dispatch_commands._run_production(args, operation), 0)
        self.assertEqual(seen, [str(self.data_home)])
        self.assertNotIn("UMMANU_DATA_DIR", os.environ)

    def test_a_head_launched_after_the_login_renders_the_data_dir_home(self) -> None:
        os.environ["UMMANU_DATA_DIR"] = str(self.data_dir)
        workspace = str(self.data_dir.parent.resolve())
        # Before the login there is no command to render: the renderer refuses with the fix.
        with self.assertRaises(HeadCommandError) as caught:
            render_head_command({"adapter": "codex"}, workspace=workspace)
        self.assertIn(f"CODEX_HOME={self.data_home} codex login", str(caught.exception))

        self.log_in()

        after = render_head_command({"adapter": "codex"}, workspace=workspace).command
        self.assertTrue(after.startswith(f"CODEX_HOME={self.data_home} codex "), after)

    def test_the_trust_preflight_writes_into_the_home_the_launch_names(self) -> None:
        os.environ["UMMANU_DATA_DIR"] = str(self.data_dir)
        self.log_in()
        workspace = self.data_dir.parent / "workspace"
        workspace.mkdir()
        codex_preflight.ensure_codex_workspace_trusted({}, str(workspace))
        trusted = (self.data_home / codex_preflight.CODEX_CONFIG_FILE).read_text(encoding="utf-8")
        self.assertIn(str(workspace.resolve()), trusted)

    def test_the_session_readers_put_the_current_home_first(self) -> None:
        os.environ["UMMANU_DATA_DIR"] = str(self.data_dir)
        # With no login there is no current home, and a reader still scans rather than raising.
        self.assertEqual(pipeline_codex_sessions.sessions_roots()[0], self.data_home / "sessions")
        os.environ["TA_CODEX_HOME"] = str(self.data_dir.parent / "env-home")
        self.assertEqual(
            pipeline_codex_sessions.sessions_roots()[0], self.data_dir.parent / "env-home" / "sessions"
        )
        os.environ.pop("TA_CODEX_HOME")
        self.log_in()
        self.assertEqual(pipeline_codex_sessions.sessions_roots()[0], self.data_home / "sessions")


class LiveSessionCutoverTests(unittest.TestCase):
    """Session readers never depend on which home is current (secretary-1710, round 3).

    Since secretary-1723 no head launches on the legacy Orca home, but its `sessions/` is still
    read, read-only, for the rollouts the curator has not ingested (`codex_home._LEGACY_SESSIONS`).
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "home"
        self.legacy = self.home / ".config" / "orca" / "codex-runtime-home" / "home"
        self.data_dir = self.root / "data"
        self.data_home = self.data_dir / "codex-home"
        self.workspace = self.root / "workspace"
        for path in (self.legacy, self.data_home, self.workspace):
            path.mkdir(parents=True)
        env = mock.patch.dict(
            os.environ,
            {
                **_without(
                    "TA_CODEX_HOME",
                    "UMMANU_INSTANCE",
                    "TA_CODEX_SESSIONS",
                    "UMMANU_CODEX_SESSIONS",
                    "TA_CODEX_SESSIONS_DIR",
                ),
                "HOME": str(self.home),
                "UMMANU_DATA_DIR": str(self.data_dir),
            },
            clear=True,
        )
        env.start()
        self.addCleanup(env.stop)
        # The curator reads its override once at import; unset, it scans every home.
        curator = mock.patch.object(discover, "CODEX_SESSIONS", None)
        curator.start()
        self.addCleanup(curator.stop)

    def log_in(self) -> None:
        (self.data_home / "auth.json").write_text('{"tokens": "fixture"}\n', encoding="utf-8")

    def rollout(self, home: Path, name: str, *, turn_at: datetime) -> Path:
        """One rollout for this workspace, dated today, holding one interactive user turn."""
        day = home / "sessions" / f"{turn_at:%Y}" / f"{turn_at:%m}" / f"{turn_at:%d}"
        day.mkdir(parents=True, exist_ok=True)
        path = day / f"rollout-{name}.jsonl"
        meta = {"type": "session_meta", "payload": {"id": name, "cwd": str(self.workspace)}}
        turn = {
            "timestamp": turn_at.isoformat().replace("+00:00", "Z"),
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": []},
        }
        path.write_text(json.dumps(meta) + "\n" + json.dumps(turn) + "\n", encoding="utf-8")
        return path

    def assert_every_reader_finds_the_turn(self, sent_at: float) -> None:
        workspace = str(self.workspace)
        latest = pipeline_codex_sessions.latest_activity_for(workspace)
        self.assertIsNotNone(latest)
        self.assertGreater(latest, sent_at)
        self.assertTrue(dispatcher_tui.provider_turn_started(workspace, sent_at, adapter="codex"))
        self.assertIsNotNone(dispatcher_tui.latest_user_turn_for(workspace, sent_at))
        self.assertEqual([entry["cwd"] for entry in discover.codex_sessions()], [workspace])

    def test_a_rollout_left_on_the_legacy_home_is_still_read_but_never_launched_into(self) -> None:
        now = datetime.now(UTC)
        sent_at = (now - timedelta(seconds=30)).timestamp()
        # No login: the launch has no home at all, and never the legacy one.
        with self.assertRaises(CodexHomeLoginMissing):
            codex_home({})

        self.log_in()
        self.assertEqual(codex_home({}), str(self.data_home))
        # A rollout an earlier head wrote on the legacy home is read where it lies.
        later = self.rollout(self.legacy, "legacy-head", turn_at=now)
        os.utime(later, (now.timestamp(), now.timestamp()))

        self.assert_every_reader_finds_the_turn(sent_at)
        self.assertIn(self.legacy / "sessions", codex_home_module.session_roots())

    def test_a_head_on_the_data_dir_home_is_found(self) -> None:
        self.log_in()
        now = datetime.now(UTC)
        self.rollout(self.data_home, "data-head", turn_at=now)
        self.assert_every_reader_finds_the_turn((now - timedelta(seconds=30)).timestamp())

    def test_with_both_homes_present_a_session_is_counted_once(self) -> None:
        self.log_in()
        now = datetime.now(UTC)
        self.rollout(self.data_home, "data-head", turn_at=now)
        # The data-dir home is both the current home and a candidate of its own, and a legacy
        # `sessions/` that is a link to it names the same files again.
        (self.legacy / "sessions").symlink_to(self.data_home / "sessions", target_is_directory=True)

        self.assertEqual(len(codex_home_module.session_roots()), 1)
        self.assertEqual(len(list(pipeline_codex_sessions._session_paths_for(str(self.workspace)))), 1)
        self.assertEqual(len(list(dispatcher_tui._session_paths_for(str(self.workspace)))), 1)
        self.assertEqual(len(discover.codex_sessions()), 1)

    def test_a_sessions_override_is_the_only_root(self) -> None:
        self.log_in()
        override = self.root / "override"
        override.mkdir()
        os.environ["TA_CODEX_SESSIONS"] = str(override)
        self.assertEqual(pipeline_codex_sessions.sessions_roots(), [override])
        self.assertEqual(dispatcher_tui._sessions_roots(), [override])
        os.environ.pop("TA_CODEX_SESSIONS")
        os.environ["UMMANU_CODEX_SESSIONS"] = str(override)
        self.assertEqual(dispatcher_tui._sessions_roots(), [override])
        with mock.patch.object(discover, "CODEX_SESSIONS", override):
            self.rollout(self.data_home, "data-head", turn_at=datetime.now(UTC))
            self.assertEqual(discover.codex_sessions(), [])


class NoImportTimeHomeTests(unittest.TestCase):
    def test_no_module_resolves_codex_home_at_import(self) -> None:
        """A module-level call would freeze the home before the PO's login could move it."""
        offenders: list[str] = []
        for path in sorted(SRC.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            offenders.extend(f"{path.relative_to(SRC)}:{node.lineno}" for node in _import_time_calls(tree))
        self.assertEqual(offenders, [])

    def test_the_scan_sees_the_constant_this_card_removed(self) -> None:
        tree = ast.parse("CODEX_HOME = codex_home({})\ndef later():\n    return codex_home({})\n")
        self.assertEqual([call.lineno for call in _import_time_calls(tree)], [1])


def _import_time_calls(tree: ast.Module) -> list[ast.Call]:
    """Resolver calls evaluated when the module is imported: anywhere outside a function body."""
    found: list[ast.Call] = []

    def visit(node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            # Decorators and defaults are evaluated at import; bodies are not.
            for expression in [*getattr(node, "decorator_list", []), *node.args.defaults]:
                visit(expression)
            for expression in node.args.kw_defaults:
                if expression is not None:
                    visit(expression)
            return
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if name in RESOLVERS:
                found.append(node)
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return found


if __name__ == "__main__":
    unittest.main()
