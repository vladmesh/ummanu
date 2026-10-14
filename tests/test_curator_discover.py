from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ummanu.automations.agents.curator import discover
from ummanu.dispatch.state import request_token


class ClaudeProjectDirectoryTests(unittest.TestCase):
    def test_exclude_directory_uses_claudes_non_alphanumeric_encoding(self) -> None:
        self.assertEqual(
            discover._dirname_for_cwd("/home/dev/codegen_orchestrator/v1.2"),
            "-home-dev-codegen-orchestrator-v1-2",
        )


class CuratorIdentityDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.curator = "/home/dev/orca/workspaces/ummanu/curator"
        self.env = mock.patch.dict(
            "os.environ", {"UMMANU_CURATOR_WORKSPACE": self.curator, "UMMANU_CURATOR_SESSION_ID": "self"}
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def test_only_the_exact_curator_workspace_or_session_is_excluded(self) -> None:
        included = [
            "/home/dev/ummanu",
            "/home/dev/orca/workspaces/ummanu/worker-123",
            "/home/dev/orca/workspaces/ummanu/observer-456",
        ]
        self.assertFalse(discover._excluded("/home/dev/ummanu", "po"))
        for cwd in included:
            self.assertFalse(discover._excluded(cwd, "other"))
        self.assertTrue(discover._excluded(self.curator, "other"))
        self.assertTrue(discover._excluded("/anywhere", "self"))

    def test_claude_codex_and_hermes_fixtures_share_the_same_matrix(self) -> None:
        projects, sessions = self.root / "claude", self.root / "codex"
        projects.mkdir()
        sessions.mkdir()
        included = "/home/dev/orca/workspaces/ummanu/observer-456"
        for cwd, session_id in ((self.curator, "self"), (included, "other")):
            project = projects / discover._dirname_for_cwd(cwd)
            project.mkdir()
            (project / f"{session_id}.jsonl").write_text(json.dumps({"cwd": cwd}) + "\n", encoding="utf-8")
            (sessions / f"{session_id}.jsonl").write_text(
                json.dumps({"type": "session_meta", "payload": {"session_id": session_id, "cwd": cwd}})
                + "\n",
                encoding="utf-8",
            )
        with (
            mock.patch.object(discover, "CLAUDE_PROJECTS", projects),
            mock.patch.object(discover, "CODEX_SESSIONS", sessions),
        ):
            self.assertEqual([s["cwd"] for s in discover.claude_sessions()], [included])
            self.assertEqual([s["cwd"] for s in discover.codex_sessions()], [included])
        with (
            mock.patch.object(discover, "HERMES_STATE_DB", self.root / "state.db"),
            mock.patch.object(
                discover, "_hermes_query", return_value=[("self", self.curator), ("other", included)]
            ),
        ):
            (self.root / "state.db").touch()
            self.assertEqual([s["cwd"] for s in discover.hermes_sessions()], [included])

    def test_one_scan_compiles_registry_and_observer_route_once(self) -> None:
        projects = self.root / "claude"
        observer = self.root / "workspaces" / "observers" / "sprint-1412" / "head"
        project = projects / discover._dirname_for_cwd(str(observer))
        project.mkdir(parents=True)
        for session_id in ("one", "two"):
            (project / f"{session_id}.jsonl").write_text(
                json.dumps({"cwd": str(observer)}) + "\n", encoding="utf-8"
            )
        with (
            mock.patch.object(discover, "CLAUDE_PROJECTS", projects),
            mock.patch.dict("os.environ", {"UMMANU_WORKSPACES_ROOT": str(self.root / "workspaces")}),
            mock.patch.object(discover, "project_bindings", return_value=[]) as bindings,
            mock.patch.object(discover, "_observer_route", return_value="alpha") as observer_route,
        ):
            sessions = discover.claude_sessions()
        self.assertEqual([session["route"] for session in sessions], ["alpha", "alpha"])
        bindings.assert_called_once()
        observer_route.assert_called_once()


class CuratorProjectRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.instance = self.root / "instance"
        (self.instance / "projects").mkdir(parents=True)
        self.workspaces = self.root / "workspaces"
        self.workspaces.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _binding(
        self,
        project: str,
        repo: Path,
        binding: str | None = None,
        *,
        include_binding: bool = True,
        curator_roots: list[Path] | None = None,
    ) -> None:
        content = f"id: {project}\nrepo: {repo}\n"
        if include_binding:
            content += f"orca_binding: {binding or project}\n"
        if curator_roots:
            content += "curator_roots:\n" + "".join(f"  - {root}\n" for root in curator_roots)
        (self.instance / "projects" / f"{project}.yaml").write_text(content, encoding="utf-8")

    def test_checkout_and_binding_workspace_routes_are_boundary_safe(self) -> None:
        repo_a, repo_ab = self.root / "repo-a", self.root / "repo-ab"
        repo_a.mkdir()
        repo_ab.mkdir()
        (repo_a / "child").mkdir()
        alias = self.root / "repo-alias"
        alias.symlink_to(repo_a, target_is_directory=True)
        (self.workspaces / "a" / "worker").mkdir(parents=True)
        self._binding("a", repo_a)
        self._binding("ab", repo_ab)
        with mock.patch.dict("os.environ", {"UMMANU_WORKSPACES_ROOT": str(self.workspaces)}):
            self.assertEqual(discover.resolve_route(str(repo_a / "child"), instance=self.instance), "a")
            self.assertEqual(discover.resolve_route(str(alias / "child"), instance=self.instance), "a")
            self.assertEqual(
                discover.resolve_route(str(self.workspaces / "a" / "worker"), instance=self.instance), "a"
            )
            self.assertEqual(
                discover.resolve_route(str(self.root / "repo-a-not-a-route"), instance=self.instance),
                "unknown",
            )

    def test_removed_binding_workspace_still_routes_by_its_durable_boundary(self) -> None:
        repo = self.root / "repo"
        repo.mkdir()
        self._binding("alpha", repo, "alpha-binding")
        removed = self.workspaces / "alpha-binding" / "finished-card"
        self.assertFalse(removed.exists())
        with mock.patch.dict("os.environ", {"UMMANU_WORKSPACES_ROOT": str(self.workspaces)}):
            self.assertEqual(discover.resolve_route(str(removed), instance=self.instance), "alpha")
            self.assertEqual(
                discover.resolve_route(
                    str(self.workspaces / "alpha-binding-other" / "finished-card"), instance=self.instance
                ),
                "unknown",
            )

    def test_explicit_historical_roots_route_missing_descendants_and_fail_ambiguous(self) -> None:
        repo_a, repo_b = self.root / "repo-a", self.root / "repo-b"
        repo_a.mkdir()
        repo_b.mkdir()
        historical = self.root / "removed-manual-checkout"
        self._binding("alpha", repo_a, curator_roots=[historical])
        self.assertEqual(discover.resolve_route(str(historical / "worker"), instance=self.instance), "alpha")
        self._binding("beta", repo_b, curator_roots=[historical])
        self.assertEqual(
            discover.resolve_route(str(historical / "worker"), instance=self.instance), "unknown"
        )

    def test_relative_historical_root_is_not_a_route(self) -> None:
        repo = self.root / "repo"
        repo.mkdir()
        content = f"id: alpha\nrepo: {repo}\ncurator_roots: [relative/path]\n"
        (self.instance / "projects" / "alpha.yaml").write_text(content, encoding="utf-8")
        self.assertEqual(
            discover.resolve_route(str(self.root / "relative/path"), instance=self.instance), "unknown"
        )

    def test_ambiguous_or_missing_binding_never_guesses_a_project(self) -> None:
        repo = self.root / "repo"
        repo.mkdir()
        self._binding("a", repo, "a")
        self._binding("b", repo, "b")
        self._binding("gone", self.root / "missing", "gone")
        self.assertEqual(discover.resolve_route(str(repo), instance=self.instance), "unknown")
        self.assertEqual(
            discover.resolve_route(str(self.root / "missing"), instance=self.instance), "unknown"
        )

    def test_binding_without_orca_name_routes_checkout_and_remains_selectable(self) -> None:
        repo = self.root / "repo"
        repo.mkdir()
        self._binding("ready-for-traffic", repo, include_binding=False)
        (self.workspaces / "ready-for-traffic" / "worker").mkdir(parents=True)
        with mock.patch.dict("os.environ", {"UMMANU_WORKSPACES_ROOT": str(self.workspaces)}):
            self.assertEqual(discover.registered_project_ids(self.instance), {"ready-for-traffic"})
            self.assertEqual(discover.resolve_route(str(repo), instance=self.instance), "ready-for-traffic")
            self.assertEqual(
                discover.resolve_route(
                    str(self.workspaces / "ready-for-traffic" / "worker"), instance=self.instance
                ),
                "unknown",
            )

    def test_observer_uses_dispatcher_token_and_exactly_one_structured_sprint_reservation(self) -> None:
        repo = self.root / "repo"
        repo.mkdir()
        self._binding("alpha", repo)
        reference = "sprint:1412"
        token = request_token(reference)
        self.assertEqual(token, "sprint-1412")
        observer = self.workspaces / "observers" / token / "head"
        observer.mkdir(parents=True)
        with (
            mock.patch.dict("os.environ", {"UMMANU_WORKSPACES_ROOT": str(self.workspaces)}),
            mock.patch.object(discover, "SprintReader") as reader,
            mock.patch.object(discover, "sprint_client"),
        ):
            reader.return_value.show.return_value = {"reservations": ["alpha"]}
            self.assertEqual(discover.resolve_route(str(observer), instance=self.instance), "alpha")
            reader.return_value.show.assert_called_once_with(
                reference, include_cards=False, include_resume_freshness=False
            )
            reader.return_value.show.return_value = {"reservations": ["alpha", "other"]}
            self.assertEqual(discover.resolve_route(str(observer), instance=self.instance), "unknown")
            reader.return_value.show.side_effect = OSError("board unavailable")
            self.assertEqual(discover.resolve_route(str(observer), instance=self.instance), "unknown")

    def test_multi_project_observer_routes_to_po_review_when_every_reservation_is_registered(self) -> None:
        alpha, beta = self.root / "alpha", self.root / "beta"
        alpha.mkdir()
        beta.mkdir()
        self._binding("alpha", alpha)
        self._binding("beta", beta)
        observer = self.workspaces / "observers" / request_token("sprint:1412") / "head"
        observer.mkdir(parents=True)
        with (
            mock.patch.dict("os.environ", {"UMMANU_WORKSPACES_ROOT": str(self.workspaces)}),
            mock.patch.object(discover, "SprintReader") as reader,
            mock.patch.object(discover, "sprint_client"),
        ):
            reader.return_value.show.return_value = {"reservations": ["alpha", "beta"]}
            self.assertEqual(
                discover.resolve_route(str(observer), instance=self.instance),
                discover.ROUTE_PO_REVIEW,
            )
            reader.return_value.show.return_value = {"reservations": ["alpha", "alpha"]}
            self.assertEqual(discover.resolve_route(str(observer), instance=self.instance), "unknown")

    def test_removed_observer_workspace_still_uses_its_sprint_reservation(self) -> None:
        repo = self.root / "repo"
        repo.mkdir()
        self._binding("alpha", repo)
        observer = self.workspaces / "observers" / request_token("sprint:1412") / "removed-head"
        self.assertFalse(observer.exists())
        with (
            mock.patch.dict("os.environ", {"UMMANU_WORKSPACES_ROOT": str(self.workspaces)}),
            mock.patch.object(discover, "SprintReader") as reader,
            mock.patch.object(discover, "sprint_client"),
        ):
            reader.return_value.show.return_value = {"reservations": ["alpha"]}
            self.assertEqual(discover.resolve_route(str(observer), instance=self.instance), "alpha")
