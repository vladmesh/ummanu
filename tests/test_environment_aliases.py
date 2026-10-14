"""Fake environment vectors through consumers; no services, credentials or probes."""

from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ummanu.automations.agents.pipeline import codex_sessions
from ummanu.automations.runtime import production_telemetry
from ummanu.dispatch import pause, tui
from ummanu.runtime import codex_preflight, heads, resource_probe, role_env, shared_state
from ummanu.runtime.paths import env_value, legacy_name


def vectors(name, canonical="/fake/canonical", legacy="/fake/legacy"):
    old = legacy_name(name)
    return (
        ({name: canonical}, canonical),
        ({old: legacy}, legacy),
        ({name: canonical, old: legacy}, canonical),
        ({}, None),
        ({name: "", old: legacy}, legacy),
        ({old: ""}, None),
        ({name: "malformed", old: legacy}, "malformed"),
    )


class EnvironmentAliasTests(unittest.TestCase):
    def test_raw_and_nonempty_policies_do_not_mutate_environment(self):
        for env, nonempty in vectors("UMMANU_STATE"):
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True):
                before = dict(os.environ)
                self.assertEqual(env_value("UMMANU_STATE", nonempty=True), nonempty)
                raw = env.get("UMMANU_STATE", env.get("TA_STATE"))
                self.assertEqual(env_value("UMMANU_STATE"), raw)
                self.assertEqual(dict(os.environ), before)

    def test_canonical_runtimefile_routes_telemetry_to_the_selected_instance(self):
        with mock.patch.dict(os.environ, {"UMMANU_RUNTIME_ENV_FILE": "/fake/selected/runtime.env"}, clear=True):
            self.assertEqual(production_telemetry._instance_from_runtime_env(), Path("/fake/selected"))

    def test_runtimefile_role_and_telemetry_share_nonempty_precedence(self):
        for env, selected in vectors("UMMANU_RUNTIME_ENV_FILE"):
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(role_env.runtime_env_path(), Path(selected or role_env.RUNTIME_ENV_DEFAULT))
                self.assertEqual(
                    production_telemetry._instance_from_runtime_env(), Path(selected).parent if selected else None
                )

    def test_telemetry_overrides_and_data_dir_keep_their_order(self):
        for name, reader, default in (
            ("UMMANU_PRODUCTION_STATE", production_telemetry.state_path, "/fake/data/dispatcher/production-state.json"),
            ("UMMANU_PRODUCTION_RESOURCE_HEALTH", production_telemetry.resource_health_path, "/fake/data/dispatcher/resource_health.json"),
        ):
            for env, selected in vectors(name):
                with self.subTest(name=name, env=env), mock.patch.dict(
                    os.environ, {"UMMANU_DATA_DIR": "/fake/data", **env}, clear=True
                ):
                    self.assertEqual(reader(), Path(selected or default))
        with mock.patch.dict(os.environ, {
            "UMMANU_INSTANCE": "/fake/explicit.yaml", "UMMANU_RUNTIME_ENV_FILE": "/fake/implied/runtime.env"
        }, clear=True):
            self.assertEqual(production_telemetry.instance_file(), Path("/fake/explicit.yaml"))

    def test_product_root_uses_alias_before_repo_without_global_mutation(self):
        for env, selected in vectors("UMMANU_RUNTIME_PYTHONPATH"):
            with self.subTest(env=env), mock.patch.dict(os.environ, {"UMMANU_REPO": "/fake/repo", **env}, clear=True):
                self.assertEqual(role_env.runtime_product_root(), Path(selected or "/fake/repo"))
                binding = " ".join(role_env.launch_binding())
                self.assertNotIn("TA_RUNTIME_", binding)
                if selected:
                    self.assertIn(f"UMMANU_RUNTIME_PYTHONPATH={selected}", binding)

    def test_registry_and_web_parser_defaults_preserve_their_empty_policies(self):
        from ummanu.cli import build_parser

        for env, selected in vectors("UMMANU_HEADS_REGISTRY"):
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(heads.registry_path(), Path(selected) if selected else heads.HEADS_TOML)
                raw = env.get("UMMANU_HEADS_REGISTRY", env.get("TA_HEADS_REGISTRY"))
                parser = build_parser()
                self.assertEqual(parser.parse_args(["web-serve"]).heads_registry, raw)
                self.assertEqual(parser.parse_args(["web-run", "list", "--ref", "fake-1"]).heads_registry, raw)
        registry = SimpleNamespace(role_defaults={"reviewer": "default"})
        for env, selected in vectors("UMMANU_REVIEWER_HEAD"):
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(heads.reviewer_head(registry), selected or "default")

    def test_codex_profile_env_and_data_dir_source_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex-home"
            home.mkdir()
            (home / "auth.json").write_text('{"fake": true}')
            for env, selected in vectors("UMMANU_CODEX_HOME"):
                with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True):
                    resolved = codex_preflight.resolve_codex_home({}, data_dir=tmp)
                    self.assertEqual(resolved.path, selected or str(home))
                    self.assertEqual(resolved.kind, "env" if selected else "data-dir")
                    resolved = codex_preflight.resolve_codex_home({"codex_home": "/fake/profile"}, data_dir=tmp)
                    self.assertEqual((resolved.path, resolved.kind), ("/fake/profile", "profile"))

    def test_codex_session_readers_share_precedence_and_do_not_collapse_dir(self):
        for env, selected in vectors("UMMANU_CODEX_SESSIONS"):
            with self.subTest(env=env), mock.patch.dict(os.environ, {
                **env, "UMMANU_CODEX_SESSIONS_DIR": "/fake/curator"
            }, clear=True), mock.patch.object(codex_sessions, "session_roots", return_value=[Path("/fake/default")]), mock.patch.object(tui, "session_roots", return_value=[Path("/fake/default")]):
                expected = [Path(selected or "/fake/default")]
                self.assertEqual(codex_sessions.sessions_roots(), expected)
                self.assertEqual(tui._sessions_roots(), expected)

    def test_pipeline_state_and_pause_keep_specific_override_order(self):
        for env, selected in vectors("UMMANU_PIPELINE_STATE_DIR"):
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(shared_state.resolve_pipeline_state_dir(), Path(selected) if selected else shared_state.WORKSPACES_ROOT / shared_state.AGENTS_PROJECT / "pipeline/state/pipeline")
                self.assertEqual(pause.legacy_mirror_path(), (Path(selected) if selected else Path.home() / "orca/workspaces/ummanu/pipeline/state/pipeline") / "pause.json")
                os.environ["UMMANU_LEGACY_PIPELINE_STATE_DIR"] = "/fake/mirror"
                self.assertEqual(pause.legacy_mirror_path(), Path("/fake/mirror/pause.json"))

    def test_workspace_and_recovery_bound_environment(self):
        from ummanu.infra import recovery_inventory
        from ummanu.installation.upgrade.workspaces import workspaces_root

        for env, selected in vectors("UMMANU_WORKSPACES_ROOT"):
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True):
                expected = Path(selected) if selected else Path.home() / "orca/workspaces"
                self.assertEqual(workspaces_root(), expected)
                with mock.patch.dict(shared_state.__dict__):
                    importlib.reload(shared_state)
                    self.assertEqual(shared_state.WORKSPACES_ROOT, expected.resolve())
                report = SimpleNamespace(instance_path=Path("/fake/instance/instance.yaml"), data_dir=Path("/fake/data"))
                with mock.patch.object(recovery_inventory.head_registry, "read_source", return_value=None):
                    rows = recovery_inventory._path_rows(report)
                    unbound = next(row for row in rows if row["capability"] == "pipeline-run-state")
                    os.environ["UMMANU_INSTANCE"] = "/fake/instance"
                    rows = recovery_inventory._path_rows(report)
                bound = next(row for row in rows if row["capability"] == "pipeline-run-state")
                self.assertIn(str(expected.resolve()), str(bound))
                self.assertIn(str((Path.home() / "orca/workspaces").resolve()), str(unbound))

    def test_dynamic_probe_timeout_validates_selected_spelling_before_fallback(self):
        name = resource_probe.probe_timeout_env_name("weird.resource-id")
        self.assertEqual(name, "UMMANU_PROBE_TIMEOUT_S_WEIRD_RESOURCE_ID")
        self.assertEqual(resource_probe.probe_timeout_env_name("münchen/1"), "UMMANU_PROBE_TIMEOUT_S_MÜNCHEN_1")
        for env, _selected in vectors("UMMANU_PROBE_TIMEOUT_S", "7.9", "9"):
            with self.subTest(global_env=env), mock.patch.dict(os.environ, env, clear=True):
                raw = env.get("UMMANU_PROBE_TIMEOUT_S", env.get("TA_PROBE_TIMEOUT_S"))
                expected = 7 if raw == "7.9" else 9 if raw == "9" else 20
                self.assertEqual(resource_probe.probe_timeout_s("custom"), expected)
                self.assertEqual(resource_probe.probe_timeout_s("openai-sub"), 75)
        for env, selected in vectors(name, "7.9", "9"):
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True):
                raw = env.get(name, env.get(legacy_name(name)))
                expected = 7 if raw == "7.9" else 9 if raw == "9" else 20
                self.assertEqual(resource_probe.probe_timeout_s("weird.resource-id"), expected)
        for value in ("", "oops", "0", "-2"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {
                name: value, legacy_name(name): "3", "UMMANU_PROBE_TIMEOUT_S": "41"
            }, clear=True):
                self.assertEqual(resource_probe.probe_timeout_s("weird.resource-id"), 41)
                self.assertEqual(resource_probe.probe_timeout_s("openai-sub"), 75)

    def test_import_time_probe_raw_integers_keep_empty_and_malformed_errors(self):
        for name, attr, default in (
            ("UMMANU_PROBE_TIMEOUT_S", "PROBE_TIMEOUT_S", 20),
            ("UMMANU_PROBE_REASON_TEXT_LIMIT", "PROBE_REASON_TEXT_LIMIT", 400),
        ):
            for env, selected in vectors(name, "7", "9"):
                with self.subTest(name=name, env=env), mock.patch.dict(os.environ, env, clear=True), mock.patch.dict(resource_probe.__dict__):
                    raw = env.get(name, env.get(legacy_name(name), str(default)))
                    if raw in ("", "malformed"):
                        with self.assertRaises(ValueError):
                            importlib.reload(resource_probe)
                    else:
                        importlib.reload(resource_probe)
                        self.assertEqual(getattr(resource_probe, attr), int(raw))

    def test_provider_overrides_never_read_a_key_from_the_other_spelling_after_selection(self):
        for env, selected in vectors("UMMANU_OPENROUTER_KEY", "fake-canonical-key", "fake-legacy-key"):
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True), mock.patch.object(resource_probe, "_OPENROUTER_ENV_FILE") as file:
                file.read_text.return_value = "OPENROUTER_API_KEY=fake-file-key\n"
                self.assertEqual(resource_probe._read_openrouter_key(), selected or "fake-file-key")
        for env, selected in vectors("UMMANU_OPENROUTER_ENV_KEY", "CANONICAL_KEY", "LEGACY_KEY"):
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True), mock.patch.dict(resource_probe.__dict__):
                importlib.reload(resource_probe)
                self.assertEqual(resource_probe._OPENROUTER_ENV_KEYS, (selected,) if selected else ("OPENROUTER_API_KEY", "open_router_key"))

    def test_role_filtering_and_launcher_binding_do_not_admit_alias_authority(self):
        forbidden = (
            "UMMANU_OPENROUTER_KEY", "UMMANU_MEMORY_ACCESS_TOKEN", "UMMANU_OBSERVER_SPRINT",
            "UMMANU_OBSERVER_GENERATION", "UMMANU_DOCKER_LOCAL_RUN_POLICY", "UMMANU_RUNTIME_PYTHONPATH",
            "UMMANU_RUNTIME_ENV_FILE", "UMMANU_INSTANCE", "UMMANU_REPO", "BOARD_ACTOR",
        )
        injected = {name: "fake-injected" for name in forbidden}
        injected.update({legacy_name(name): "fake-alias-injected" for name in forbidden if name.startswith("UMMANU_")})
        for role in ("worker", "reviewer", "observer", "curator", "steward", "retro"):
            with self.subTest(role=role), mock.patch.object(role_env, "load_env_file", return_value=injected), mock.patch.object(role_env, "_docker_bindings", return_value={}):
                # Selected instance/product are existing unit-bound allowlist members.
                base = {"UMMANU_INSTANCE": "/fake/instance", "UMMANU_REPO": "/fake/repo", "TA_RUNTIME_ENV_FILE": "/fake/unit/runtime.env"}
                output = role_env.runtime_env(role, base_env=base)
                self.assertEqual(output["UMMANU_RUNTIME_ENV_FILE"], "/fake/unit/runtime.env")
                self.assertNotIn("TA_RUNTIME_ENV_FILE", output)
                self.assertEqual(output["UMMANU_INSTANCE"], "/fake/instance")
                self.assertEqual(output["UMMANU_REPO"], "/fake/repo")
                self.assertNotIn("UMMANU_RUNTIME_PYTHONPATH", output)
                self.assertFalse(any(value.startswith("fake-") for value in output.values()))
                for name in ("UMMANU_OPENROUTER_KEY", "TA_OPENROUTER_KEY", "UMMANU_OPENROUTER_ENV_KEY", "TA_OPENROUTER_ENV_KEY"):
                    self.assertTrue(role_env.is_sensitive_env_name(name))
        with mock.patch.object(role_env, "load_env_file", return_value=injected), mock.patch.object(role_env, "_docker_bindings", return_value={}):
            output = role_env.runtime_env("worker", base_env={
                "UMMANU_OBSERVER_SPRINT": "fake-parent-binding",
                "UMMANU_RUNTIME_ENV_FILE": "/fake/canonical/runtime.env",
                "TA_RUNTIME_ENV_FILE": "/fake/legacy/runtime.env",
            })
            self.assertNotIn("UMMANU_OBSERVER_SPRINT", output)
            self.assertEqual(output["UMMANU_RUNTIME_ENV_FILE"], "/fake/canonical/runtime.env")

    def test_agent_limits_and_identity_use_aliases_at_call_time(self):
        from ummanu.automations.agents.curator import discover, harvest
        from ummanu.automations.runtime import dispatch

        defaults = (200, 262144, 32, 512, 65536, 65536)
        fields = ("turns", "input_bytes", "sources", "rows_per_source", "record_bytes", "memory_bytes")
        for index, field in enumerate(fields):
            name = "UMMANU_CURATOR_MAX_" + field.upper()
            for env, selected in vectors(name, "7", "9"):
                with self.subTest(name=name, env=env), mock.patch.dict(os.environ, env, clear=True):
                    raw = env.get(name, env.get(legacy_name(name), str(defaults[index])))
                    if raw in ("", "malformed"):
                        label = name if name in env else legacy_name(name)
                        with self.assertRaisesRegex(ValueError, label + " must be a positive integer"):
                            harvest.Limits.from_env()
                    else:
                        self.assertEqual(getattr(harvest.Limits.from_env(), "max_" + field), int(raw))
            with mock.patch.dict(os.environ, {name: "0", legacy_name(name): "8"}, clear=True), self.assertRaises(ValueError):
                harvest.Limits.from_env()
        for env, selected in vectors("UMMANU_CURATOR_WORKSPACE"):
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True):
                expected = Path(selected or Path.cwd()).resolve()
                self.assertEqual(discover.curator_workspace(), expected)
                self.assertEqual(harvest.current_identity()["workspace"], str(expected))
        for name, key in (("UMMANU_CURATOR_SESSION_ID", "session_id"), ("UMMANU_CURATOR_RUN_ID", "run_id")):
            for env, selected in vectors(name):
                with self.subTest(name=name, env=env), mock.patch.dict(os.environ, env, clear=True):
                    self.assertEqual(harvest.current_identity().get(key), selected)
                    if key == "session_id":
                        raw = env.get(name, env.get(legacy_name(name), ""))
                        self.assertEqual(discover.curator_session_id(), raw)
        for env, selected in vectors("UMMANU_WORKSPACE"):
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(dispatch._workspace("retro"), selected or str(Path.home() / "orca/workspaces/ummanu/retro"))

    def test_import_time_path_settings_keep_raw_empty_and_timing(self):
        from ummanu.automations.agents.curator import discover
        from ummanu.automations.agents.retro import search_log
        from ummanu.automations.runtime import dispatch
        from ummanu.runtime import state
        from ummanu.webproto import lifecycle

        cases = (
            (state, "UMMANU_STATE", "STATE_ROOT", Path.home() / "ummanu-data/automation-state"),
            (search_log, "UMMANU_SEARCH_LOG", "SEARCH_LOG", search_log.DEFAULT_SEARCH_LOG),
            (dispatch, "UMMANU_CLAUDE_JSON", "CLAUDE_JSON", Path.home() / ".claude.json"),
            (lifecycle, "UMMANU_CLAUDE_JSON", "CLAUDE_JSON", Path.home() / ".claude.json"),
            (discover, "UMMANU_CLAUDE_PROJECTS_DIR", "CLAUDE_PROJECTS", Path.home() / ".claude/projects"),
            (discover, "UMMANU_HERMES_HOME_DIR", "HERMES_HOME", Path.home() / ".hermes"),
            (resource_probe, "UMMANU_OPENROUTER_ENV_FILE", "_OPENROUTER_ENV_FILE", Path.home() / ".hermes/.env"),
        )
        for module, name, attr, default in cases:
            for env, _selected in vectors(name):
                with self.subTest(name=name, env=env), mock.patch.dict(os.environ, {"HOME": str(Path.home()), **env}, clear=True), mock.patch.dict(module.__dict__):
                    importlib.reload(module)
                    expected = Path(env.get(name, env.get(legacy_name(name), str(default))))
                    self.assertEqual(getattr(module, attr), expected)
                    os.environ[name] = "/fake/after-import"
                    self.assertEqual(getattr(module, attr), expected)
        for env, selected in vectors("UMMANU_CODEX_SESSIONS_DIR"):
            with self.subTest(env=env), mock.patch.dict(os.environ, {**env, "UMMANU_CODEX_SESSIONS": "/fake/pipeline"}, clear=True), mock.patch.dict(discover.__dict__):
                importlib.reload(discover)
                self.assertEqual(discover.CODEX_SESSIONS, Path(selected) if selected else None)

    def test_steward_and_health_import_time_overrides_keep_validation(self):
        from ummanu.automations.agents.steward import signals
        from ummanu.automations.runtime import health

        for env, _selected in vectors("UMMANU_STEWARD_STALE_HOURS", "7.5", "9"):
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True), mock.patch.dict(signals.__dict__):
                raw = env.get("UMMANU_STEWARD_STALE_HOURS", env.get("TA_STEWARD_STALE_HOURS", "24"))
                if raw in ("", "malformed"):
                    with self.assertRaises(ValueError):
                        importlib.reload(signals)
                else:
                    importlib.reload(signals)
                    self.assertEqual(signals.STALE_HOURS, float(raw))
        for env, selected in vectors("UMMANU_HEALTH_MAX_AGE_S", "7", "9"):
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True), mock.patch.dict(health.__dict__):
                importlib.reload(health)
                self.assertEqual(health._ENV_MAX_AGE, selected)
                with mock.patch.object(health.production_telemetry, "instance_host_configuration", return_value=({}, None)), mock.patch.object(health.Path, "read_text", return_value="[health]\nmax_age_s=11\n"):
                    expectations, error = health._role_expectations(("retro",))
                if selected == "malformed":
                    self.assertEqual(error, "UMMANU_HEALTH_MAX_AGE_S is not an integer")
                else:
                    self.assertIsNone(error)
                    self.assertEqual(expectations["retro"].max_age_s, int(selected or 11))

    def test_pause_raw_overrides_and_claude_launch_precedence(self):
        from ummanu.dispatch import launcher

        for env, _selected in vectors("UMMANU_HARD_PAUSE_AUTO_RESUME_TTL_S", "7", "9"):
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True):
                raw = env.get("UMMANU_HARD_PAUSE_AUTO_RESUME_TTL_S", env.get("TA_HARD_PAUSE_AUTO_RESUME_TTL_S"))
                expected = int(raw) if raw in ("7", "9") else pause.AUTO_RESUME_TTL_DEFAULT
                self.assertEqual(pause.auto_resume_ttl_seconds(), expected)
        for env, _selected in vectors("UMMANU_HARD_PAUSE_AUTO_RESUME_ACTORS", "first,second", "legacy"):
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True):
                raw = env.get("UMMANU_HARD_PAUSE_AUTO_RESUME_ACTORS", env.get("TA_HARD_PAUSE_AUTO_RESUME_ACTORS", pause.AUTO_RESUME_ACTORS_DEFAULT))
                self.assertEqual(pause.auto_resume_actors(), tuple(item.strip() for item in raw.split(",") if item.strip()))
        for env, _selected in vectors("UMMANU_CLAUDE_JSON"):
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True), mock.patch.object(launcher, "_load_claude_config", return_value={}) as load, mock.patch.object(launcher, "_save_claude_config"):
                raw = env.get("UMMANU_CLAUDE_JSON", env.get("TA_CLAUDE_JSON", launcher.CLAUDE_JSON_DEFAULT))
                launcher.ensure_claude_workspace_ready("/fake/workspace")
                self.assertEqual(load.call_args.args[0], Path(raw))
                launcher.ensure_claude_workspace_ready("/fake/workspace", config=Path("/fake/flag"))
                self.assertEqual(load.call_args.args[0], Path("/fake/flag"))
        for env, selected in vectors("UMMANU_CLAUDE_PROJECTS"):
            with self.subTest(env=env), mock.patch.dict(os.environ, {**env, "UMMANU_CLAUDE_PROJECTS_DIR": "/fake/curator"}, clear=True):
                self.assertEqual(tui._claude_projects_root(), Path(selected) if selected else Path.home() / ".claude/projects")

    def test_both_provider_spellings_are_filtered_and_redacted(self):
        from ummanu.runtime import redact

        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "runtime.env"
            file.write_text("UMMANU_OPENROUTER_KEY=fake-canonical-secret-material\nTA_OPENROUTER_KEY=fake-legacy-secret-material\n")
            with mock.patch.object(redact, "DEFAULT_ENV_FILES", []):
                result = redact.redact("fake-canonical-secret-material fake-legacy-secret-material", env_files=[file])
            self.assertNotIn("fake-canonical-secret-material", result)
            self.assertNotIn("fake-legacy-secret-material", result)
            for role in ("worker", "reviewer", "observer"):
                with self.subTest(role=role), mock.patch.object(role_env, "load_env_file", return_value={}), mock.patch.object(role_env, "_docker_bindings", return_value={}):
                    output = role_env.runtime_env(role, base_env={"UMMANU_OPENROUTER_KEY": "fake-canonical-secret-material", "TA_OPENROUTER_KEY": "fake-legacy-secret-material"})
                    self.assertNotIn("UMMANU_OPENROUTER_KEY", output)
                    self.assertNotIn("TA_OPENROUTER_KEY", output)


if __name__ == "__main__":
    unittest.main()
