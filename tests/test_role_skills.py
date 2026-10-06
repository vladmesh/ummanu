"""The role-skill registry: the product canon, the optional instance overlay layered over it, the
command entry points either of them can declare, and the delivery check that reads both."""

from __future__ import annotations

import contextlib
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ummanu.role_skills import (
    BIN_DIR_ENV,
    INSTANCE_ORIGIN,
    MANIFEST,
    MANIFEST_ENV,
    OWNERSHIP_MARKER,
    PRODUCT_ORIGIN,
    RegistryError,
    audit,
    find_overlapping_target_roots,
    instance_manifest_path,
    iter_expected,
    iter_expected_commands,
    load_manifest,
    load_registry,
    main,
    manifest_sources,
    product_manifest_path,
    skill_delivery,
    sync,
)

ROLES_ROOT = MANIFEST.parent / "roles"

OBSERVER_SKILL = "observe-sprint"
OPEN_SPRINT_SKILL = "open-sprint"
CURATOR_SKILL = "curate"


class CanonicalRegistryTests(unittest.TestCase):
    """Read the product manifest itself, not a fixture: it is what `role-skills sync` delivers.

    The instance is an empty directory on purpose. The default instance path is the host's real
    one, and on the machine that develops this product that instance owns an overlay; a test about
    the product canon has to say so or it silently asserts against the developer's installation.

    The manifest is named for the same reason on the product side. Unnamed, the registry resolves
    to the checkout this host is configured with, which is the right default for delivery and the
    wrong one here: these tests are about what *this* checkout ships.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.instance = Path(tmp.name)
        self.manifest = load_manifest(MANIFEST)
        self.registry = load_registry(self.instance, product_manifest=MANIFEST)

    def test_the_observer_role_owns_its_own_skill(self) -> None:
        self.assertEqual(self.manifest["roles"]["observer"]["skills"], [OBSERVER_SKILL])

    def test_a_codex_standing_head_finds_its_skill_in_its_own_workspace(self) -> None:
        # A Codex head runs on CODEX_HOME=<data_dir>/codex-home and reads project skills from
        # `.agents/skills` in its cwd, `~/orca/workspaces/ummanu/<role>` for a standing role. A copy
        # only in the legacy Orca home left a Codex curator with "no skill `curate`".
        home = Path("/fixture-home")
        expected = iter_expected(self.registry, home)
        for role in ("curator", "retro", "steward"):
            workspace_root = home / "orca" / "workspaces" / "ummanu" / role / ".agents" / "skills"
            delivered = {
                item.skill for item in expected
                if item.shell == "codex" and item.role == role and item.dest.parent == workspace_root
            }
            self.assertEqual(delivered, set(self.manifest["roles"][role]["skills"]), role)

    def test_the_canonical_observer_skill_is_in_this_repository(self) -> None:
        source = ROLES_ROOT / "observer" / OBSERVER_SKILL / "SKILL.md"

        self.assertTrue(source.is_file(), f"{source} is missing")

    def test_curator_skill_routes_ambiguous_multi_project_facts_to_po_review(self) -> None:
        source = ROLES_ROOT / "curator" / CURATOR_SKILL / "SKILL.md"
        text = source.read_text(encoding="utf-8")

        self.assertIn("<canonical-id|unknown|review:po>", text)
        self.assertIn("Multiple distinct registered\nreservations route to `review:po`", text)
        self.assertIn("tags `pending-review,multi-project`", text)
        self.assertIn("name its candidate project scopes", text)
        self.assertIn("Do not create a review fact merely to record that a session was examined", text)
        self.assertIn("tags `pending-review,unknown-source`", text)
        self.assertIn("Ownership ambiguity is not a reason to discard a durable conclusion", text)
        self.assertIn("--scope <global|project:<dir>|review:po>", text)

    def test_observer_skill_contains_the_minimal_supported_change_guardrails(self) -> None:
        source = ROLES_ROOT / "observer" / OBSERVER_SKILL / "SKILL.md"
        text = source.read_text(encoding="utf-8")

        self.assertIn("Before creating a `code` card, work through this decision sequence", text)
        self.assertIn("standard library or a native platform capability", text)
        self.assertIn("not supported legacy by default", text)
        self.assertIn("fail-closed upgrade precondition", text)
        self.assertIn("removal may lose real user data or break a supported installation path", text)
        self.assertIn("When you see RED or Blocked evidence", text)
        self.assertIn("a local defect inside the card may return for supported rework or retry", text)
        self.assertIn("the planned architecture or card cut is wrong", text)
        self.assertIn("hardening or compatibility outside the sprint is deferred or taken to the user", text)
        # A red review no longer reworks on its own: the card parks and waits for the decision.
        # The guardrail this replaces told the observer the opposite, which is how a head ends up
        # waiting for a worker that nothing will resume.
        self.assertIn("A red reviewer verdict on your sprint's card starts no rework on its own", text)
        self.assertIn("A red mechanical gate is the exception and does rework", text)
        self.assertNotIn("may already have started the first automatic rework before you wake", text)
        self.assertNotIn("Before an ordinary RED goes back for another worker round", text)
        self.assertIn("Apply the same classification to the Blocked evidence", text)

    def test_observer_skill_requires_live_owner_comments_before_semantic_writes(self) -> None:
        source = ROLES_ROOT / "observer" / OBSERVER_SKILL / "SKILL.md"
        text = source.read_text(encoding="utf-8")

        self.assertIn("Before `task create`, before `task decide`", text)
        self.assertIn("post-Done next-cut or close", text)
        self.assertIn("complete comments list in board order", text)
        self.assertIn("Do not use a\ntimestamp cutoff", text)
        self.assertIn("Apply every applicable owner decision before the saved resume", text)
        self.assertIn("reflect it in the resume written for this turn", text)

    def test_observer_skill_stops_a_round_that_only_moves_the_defect(self) -> None:
        source = ROLES_ROOT / "observer" / OBSERVER_SKILL / "SKILL.md"
        text = source.read_text(encoding="utf-8")

        self.assertIn("blockers that the previous round's own repairs introduced", text)
        self.assertIn("each repair is locally right and shifts the problem", text)
        self.assertIn("Name the single place that must enforce the invariant", text)
        self.assertIn("what takes precedence during recovery", text)
        self.assertIn("exhibit in its", text)
        self.assertIn("refining it after", text)

    def test_the_observer_skill_reaches_the_shell_its_head_runs_in(self) -> None:
        """`role_defaults.observer` is a codex profile, so the codex shell must carry the skill."""
        shells = {
            item.shell
            for item in iter_expected(self.registry)
            if item.role == "observer" and item.skill == OBSERVER_SKILL
        }

        self.assertIn("codex", shells)

    def test_no_target_root_is_nested_in_another_of_the_same_shell(self) -> None:
        self.assertEqual(find_overlapping_target_roots(self.registry), [])

    def test_the_document_sprint_loop_is_gone(self) -> None:
        """A sprint is an entity with an observer; the document loop is not shipped."""
        skills = self.manifest["roles"]["ummanu"]["skills"]

        for skill in ("start-sprint", "run-sprint"):
            self.assertNotIn(skill, skills)
            self.assertFalse((ROLES_ROOT / "ummanu" / skill).exists())

    def test_the_ummanu_role_owns_the_sprint_entity_skill(self) -> None:
        self.assertIn(OPEN_SPRINT_SKILL, self.manifest["roles"]["ummanu"]["skills"])

    def test_the_canonical_open_sprint_skill_is_in_this_repository(self) -> None:
        source = ROLES_ROOT / "ummanu" / OPEN_SPRINT_SKILL / "SKILL.md"

        self.assertTrue(source.is_file(), f"{source} is missing")

    def test_open_sprint_reaches_both_ummanu_shells(self) -> None:
        """Sprint birth must not depend on which ummanu the human opened."""
        shells = {
            item.shell
            for item in iter_expected(self.registry)
            if item.role == "ummanu" and item.skill == OPEN_SPRINT_SKILL
        }

        self.assertLessEqual({"claude", "codex"}, shells)

    def test_every_skill_the_product_declares_has_a_source_in_this_repository(self) -> None:
        missing = [
            str(item.source)
            for item in iter_expected(self.registry)
            if not (item.source / "SKILL.md").is_file()
        ]

        self.assertEqual(missing, [])

    def test_every_shipped_target_root_belongs_to_whoever_installs_it(self) -> None:
        """The product does not know which user runs it, so no root may name one.

        A root is relative to the installing user's home or to the installation's PO workspace in
        its data directory; neither names a host path.
        """
        roots = [target["root"] for target, _ in self.registry.targets.values()]

        self.assertTrue(roots)
        for root in roots:
            with self.subTest(root):
                self.assertTrue(root.startswith(("~/", "@po/")), root)

    def test_a_shipped_target_root_expands_into_the_installing_users_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"HOME": tmp}, clear=False):
            registry = load_registry(self.instance, product_manifest=MANIFEST)
            destinations = {item.dest for item in iter_expected(registry)}
            commands = {command.dest for command in iter_expected_commands(registry)}

        outside = [str(path) for path in destinations | commands if not str(path).startswith(tmp)]
        self.assertEqual(outside, [])

    def test_a_shipped_skill_source_stays_beside_the_manifest_that_declared_it(self) -> None:
        """Only the target moves with the home: a source is a file in a checkout."""
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"HOME": tmp}, clear=False):
            sources = {
                item.source for item in iter_expected(load_registry(self.instance, product_manifest=MANIFEST))
            }

        self.assertTrue(sources)
        for source in sources:
            with self.subTest(str(source)):
                self.assertEqual(source.parent.parent, ROLES_ROOT)

    def test_the_product_canon_reads_the_same_without_an_instance_directory(self) -> None:
        """A checkout on a machine with no installation at all is still a readable registry."""
        registry = load_registry(self.instance / "not-installed", product_manifest=MANIFEST)

        self.assertEqual([source.origin for source in registry.sources], [PRODUCT_ORIGIN])
        self.assertEqual(registry.roles.keys(), self.registry.roles.keys())


class OverlayFixture(unittest.TestCase):
    """A product manifest and an instance directory this test owns end to end."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.shell_root = self.root / "codex-shell"
        self.product = self.root / "product" / "manifest.toml"
        self.instance = self.root / "instance"
        self.instance.mkdir()
        self.write_product()
        env = mock.patch.dict(os.environ, {MANIFEST_ENV: str(self.product)})
        env.start()
        self.addCleanup(env.stop)

    def write_product(self, body: str | None = None) -> None:
        self.product.parent.mkdir(parents=True, exist_ok=True)
        self.product.write_text(
            body
            if body is not None
            else (
                '[roles.ummanu]\nskills = ["shipped"]\n\n'
                f'[targets.t]\nshell = "codex"\nroot = "{self.shell_root}"\n'
                'roles = ["ummanu"]\n'
            ),
            encoding="utf-8",
        )
        self.write_skill(self.product.parent / "roles" / "ummanu" / "shipped", "# shipped\n")

    def write_overlay(self, body: str) -> Path:
        manifest = self.instance / "skills" / "manifest.toml"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(body, encoding="utf-8")
        return manifest

    def write_skill(self, directory: Path, text: str) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        skill = directory / "SKILL.md"
        skill.write_text(text, encoding="utf-8")
        return skill

    def personal_skill_dir(self) -> Path:
        return self.instance / "skills" / "roles" / "ummanu" / "personal"


class LayeredRegistryTests(OverlayFixture):
    """Two manifests, each owning the `roles/` tree beside it."""

    def test_a_missing_overlay_is_a_supported_installation(self) -> None:
        sources = manifest_sources(self.instance)
        result = sync(instance_path=self.instance)

        self.assertEqual([source.origin for source in sources], [PRODUCT_ORIGIN])
        self.assertTrue(result["after"]["ok"], result["after"])
        self.assertTrue((self.shell_root / "shipped" / "SKILL.md").is_file())

    def test_the_overlay_adds_to_a_product_role_instead_of_replacing_it(self) -> None:
        self.write_overlay('[roles.ummanu]\nskills = ["personal"]\n')
        self.write_skill(self.personal_skill_dir(), "# personal\n")

        registry = load_registry(self.instance)

        self.assertEqual([skill for skill, _ in registry.roles["ummanu"]], ["shipped", "personal"])

    def test_each_skill_resolves_beside_the_manifest_that_declared_it(self) -> None:
        self.write_overlay('[roles.ummanu]\nskills = ["personal"]\n')
        self.write_skill(self.personal_skill_dir(), "# personal\n")

        by_skill = {item.skill: item for item in iter_expected(load_registry(self.instance))}

        self.assertEqual(by_skill["shipped"].source, self.product.parent / "roles" / "ummanu" / "shipped")
        self.assertEqual(by_skill["shipped"].origin, PRODUCT_ORIGIN)
        self.assertEqual(by_skill["personal"].source, self.personal_skill_dir())
        self.assertEqual(by_skill["personal"].origin, INSTANCE_ORIGIN)

    def test_sync_delivers_both_layers_into_one_shell_root(self) -> None:
        self.write_overlay('[roles.ummanu]\nskills = ["personal"]\n')
        self.write_skill(self.personal_skill_dir(), "# personal\n")

        result = sync(instance_path=self.instance)

        self.assertTrue(result["after"]["ok"], result["after"])
        self.assertTrue((self.shell_root / "shipped" / "SKILL.md").is_file())
        self.assertTrue((self.shell_root / "personal" / "SKILL.md").is_file())
        self.assertEqual(
            {item["skill"]: item["origin"] for item in result["copied"]},
            {"shipped": PRODUCT_ORIGIN, "personal": INSTANCE_ORIGIN},
        )

    def test_an_overlay_skill_with_no_source_is_reported_against_its_own_manifest(self) -> None:
        overlay = self.write_overlay('[roles.ummanu]\nskills = ["personal"]\n')

        result = audit(instance_path=self.instance)

        self.assertFalse(result["ok"])
        self.assertEqual(
            [(item["skill"], item["origin"], item["manifest"]) for item in result["source_missing"]],
            [("personal", INSTANCE_ORIGIN, str(overlay))],
        )

    def test_the_audit_names_every_manifest_it_read(self) -> None:
        overlay = self.write_overlay('[roles.ummanu]\nskills = ["personal"]\n')
        self.write_skill(self.personal_skill_dir(), "# personal\n")
        sync(instance_path=self.instance)

        result = audit(instance_path=self.instance)

        self.assertTrue(result["ok"], result)
        self.assertEqual(
            result["manifests"],
            [
                {"origin": PRODUCT_ORIGIN, "path": str(self.product)},
                {"origin": INSTANCE_ORIGIN, "path": str(overlay)},
            ],
        )

    def test_the_text_audit_attributes_every_finding_to_the_manifest_that_owns_it(self) -> None:
        """`audit` without `--json` is the operator path, and it has to say which file to edit."""
        overlay = self.write_overlay('[roles.ummanu]\nskills = ["personal"]\n')

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(["audit", "--instance", str(self.instance)])
        lines = out.getvalue().splitlines()

        shipped = next(line for line in lines if "ummanu/shipped" in line)
        personal = next(line for line in lines if "ummanu/personal" in line)
        self.assertIn(str(self.product), shipped)
        self.assertIn(str(overlay), personal)

    def test_an_overlay_target_replaces_the_product_target_of_the_same_name(self) -> None:
        """A target is one shell root: merging two of them means nothing, so the last one wins."""
        other = self.root / "other-shell"
        self.write_overlay(f'[targets.t]\nshell = "codex"\nroot = "{other}"\nroles = ["ummanu"]\n')

        registry = load_registry(self.instance)

        self.assertEqual(registry.targets["t"][0]["root"], str(other))
        self.assertEqual(registry.targets["t"][1].origin, INSTANCE_ORIGIN)

    def write_clashing_overlay(self) -> Path:
        """An installation skill of the product skill's name, in a role the same shell carries."""
        overlay = self.write_overlay(
            '[roles.observer]\nskills = ["shipped"]\n\n'
            f'[targets.t]\nshell = "codex"\nroot = "{self.shell_root}"\n'
            'roles = ["ummanu", "observer"]\n'
        )
        self.write_skill(
            self.instance / "skills" / "roles" / "observer" / "shipped", "# not the product one\n"
        )
        return overlay

    def test_two_skills_of_one_name_cannot_claim_one_skill_directory(self) -> None:
        """Skill directories are flat, so copying both would bury one without saying so."""
        overlay = self.write_clashing_overlay()

        with self.assertRaises(RegistryError) as caught:
            sync(instance_path=self.instance)

        message = str(caught.exception)
        self.assertIn(str(self.shell_root / "shipped"), message)
        self.assertIn(str(self.product), message)
        self.assertIn(str(overlay), message)
        self.assertFalse(self.shell_root.exists(), "sync delivered half of a refused registry")

    def test_the_audit_reports_a_claimed_skill_directory_rather_than_a_drift(self) -> None:
        overlay = self.write_clashing_overlay()

        result = audit(instance_path=self.instance)

        self.assertFalse(result["ok"])
        self.assertEqual(
            [
                (item["dest"], item["left_manifest"], item["right_manifest"])
                for item in result["destination_conflicts"]
            ],
            [(str(self.shell_root / "shipped"), str(self.product), str(overlay))],
        )

    def test_the_overlay_is_found_from_instance_yaml_just_as_from_the_directory(self) -> None:
        """`--instance` takes either the private repo or the config file inside it."""
        self.assertEqual(
            instance_manifest_path(self.instance / "instance.yaml"),
            instance_manifest_path(self.instance),
        )

    def test_the_configured_instance_is_used_when_no_path_is_passed(self) -> None:
        overlay = self.write_overlay('[roles.ummanu]\nskills = ["personal"]\n')

        with mock.patch.dict(os.environ, {"UMMANU_INSTANCE": str(self.instance)}):
            sources = manifest_sources()

        self.assertEqual([str(source.path) for source in sources], [str(self.product), str(overlay)])

    def test_the_command_default_instance_comes_from_the_environment(self) -> None:
        self.write_overlay('[roles.ummanu]\nskills = ["personal"]\n')
        self.write_skill(self.personal_skill_dir(), "# personal\n")

        with mock.patch.dict(os.environ, {"UMMANU_INSTANCE": str(self.instance)}):
            code = main(["sync"])

        self.assertEqual(code, 0)
        self.assertTrue((self.shell_root / "personal" / "SKILL.md").is_file())


class AlternateProductManifestTests(OverlayFixture):
    """A caller installing one checkout while running from another.

    `UMMANU_ROLE_SKILLS_MANIFEST` still points at the fixture the base class wrote, which stands
    in for the checkout that runs the process. Everything below names a second one instead.
    """

    def setUp(self) -> None:
        super().setUp()
        self.other_root = self.root / "other-checkout"
        self.other = product_manifest_path(self.other_root)
        self.other.parent.mkdir(parents=True)
        self.other.write_text(
            '[roles.ummanu]\nskills = ["newer"]\n\n'
            f'[targets.t]\nshell = "codex"\nroot = "{self.shell_root}"\nroles = ["ummanu"]\n',
            encoding="utf-8",
        )
        self.write_skill(self.other.parent / "roles" / "ummanu" / "newer", "# newer\n")

    def test_the_named_checkouts_manifest_replaces_the_running_ones(self) -> None:
        result = audit(instance_path=self.instance, product_manifest=self.other)

        self.assertEqual(result["manifest"], str(self.other))
        self.assertEqual([item["skill"] for item in result["missing"]], ["newer"])

    def test_sync_delivers_the_named_checkouts_skill_and_not_the_running_ones(self) -> None:
        result = sync(instance_path=self.instance, product_manifest=self.other)

        self.assertTrue(result["after"]["ok"], result["after"])
        self.assertTrue((self.shell_root / "newer" / "SKILL.md").is_file())
        self.assertFalse((self.shell_root / "shipped").exists())

    def test_a_checkout_root_names_the_manifest_inside_it(self) -> None:
        self.assertEqual(product_manifest_path(self.other_root), self.other)

    def test_an_overlay_still_layers_over_the_named_checkout(self) -> None:
        self.write_overlay('[roles.ummanu]\nskills = ["personal"]\n')
        self.write_skill(self.personal_skill_dir(), "# personal\n")

        result = sync(instance_path=self.instance, product_manifest=self.other)

        self.assertEqual(
            [(item["origin"], item["skill"]) for item in result["copied"]],
            [(PRODUCT_ORIGIN, "newer"), (INSTANCE_ORIGIN, "personal")],
        )

    def test_a_malformed_manifest_in_the_named_checkout_names_that_file(self) -> None:
        self.other.write_text("[roles.ummanu\n", encoding="utf-8")

        with self.assertRaises(RegistryError) as caught:
            audit(instance_path=self.instance, product_manifest=self.other)

        self.assertIn(str(self.other), str(caught.exception))


class MalformedManifestTests(OverlayFixture):
    """An operator edits these files by hand, so a bad edit has to read like a bad edit."""

    def assert_bounded(self, code: int, output: str, manifest: Path) -> None:
        self.assertEqual(code, 2, output)
        self.assertIn(str(manifest), output)
        self.assertNotIn("Traceback", output)
        self.assertLessEqual(len(output.splitlines()), 3, output)

    def run_command(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main([*argv, "--instance", str(self.instance)])
        return code, out.getvalue()

    def test_an_overlay_that_is_not_toml_names_itself_in_the_audit(self) -> None:
        overlay = self.write_overlay("this is not toml = [")

        code, output = self.run_command("audit")

        self.assert_bounded(code, output, overlay)

    def test_an_overlay_with_the_wrong_shape_names_itself_and_the_key(self) -> None:
        overlay = self.write_overlay('[roles.ummanu]\nskills = "personal"\n')

        code, output = self.run_command("audit")

        self.assert_bounded(code, output, overlay)
        self.assertIn("roles.ummanu.skills", output)

    def test_a_target_naming_an_unknown_role_is_rejected(self) -> None:
        overlay = self.write_overlay(
            f'[targets.u]\nshell = "codex"\nroot = "{self.root / "u"}"\nroles = ["ghost"]\n'
        )

        code, output = self.run_command("audit")

        self.assert_bounded(code, output, overlay)
        self.assertIn("ghost", output)

    def test_a_malformed_overlay_makes_sync_write_nothing(self) -> None:
        overlay = self.write_overlay("[roles.ummanu]\nskills = [1]\n")

        code, output = self.run_command("sync")

        self.assert_bounded(code, output, overlay)
        self.assertFalse(self.shell_root.exists(), "sync delivered the product half of a bad registry")

    def test_a_directory_where_the_overlay_belongs_is_not_a_portable_installation(self) -> None:
        """An installation that meant to own an overlay and has a broken one is not portable."""
        overlay = self.instance / "skills" / "manifest.toml"
        overlay.mkdir(parents=True)

        code, output = self.run_command("audit")

        self.assert_bounded(code, output, overlay)

    def test_an_overlay_that_is_a_dangling_link_is_reported_rather_than_skipped(self) -> None:
        overlay = self.instance / "skills" / "manifest.toml"
        overlay.parent.mkdir(parents=True)
        overlay.symlink_to(self.root / "never-checked-out.toml")

        code, output = self.run_command("audit")

        self.assert_bounded(code, output, overlay)

    def test_a_skill_name_that_is_a_path_cannot_leave_the_shell_root(self) -> None:
        """A name is joined onto a shell root, so a name with a separator moves the write."""
        overlay = self.write_overlay('[roles.ummanu]\nskills = ["../escaped"]\n')
        self.write_skill(self.instance / "skills" / "roles" / "ummanu" / "escaped", "# escaped\n")
        self.shell_root.mkdir(parents=True)

        code, output = self.run_command("sync")

        self.assert_bounded(code, output, overlay)
        self.assertIn("../escaped", output)
        self.assertFalse((self.root / "escaped").exists(), "sync wrote outside the shell root")
        self.assertEqual(list(self.shell_root.iterdir()), [])

    def test_a_role_name_that_is_a_path_cannot_leave_the_roles_tree(self) -> None:
        """A quoted role key is still a directory name, and reading is joined the same way."""
        overlay = self.write_overlay('[roles."../outside"]\nskills = ["personal"]\n')

        code, output = self.run_command("audit")

        self.assert_bounded(code, output, overlay)
        self.assertIn("../outside", output)

    def test_a_malformed_product_manifest_names_the_product_manifest(self) -> None:
        self.write_product("[roles.ummanu\n")

        code, output = self.run_command("audit")

        self.assert_bounded(code, output, self.product)


class RetiredSkillTests(OverlayFixture):
    """A skill the product manifest stops declaring, in a shell root under the installation's home.

    This is how a shipped skill leaves a host: the manifest drops it, and the next sync removes the
    copy it delivered there, while a copy that carries no delivery marker stays the operator's.
    """

    def setUp(self) -> None:
        super().setUp()
        self.home = self.root / "home"
        self.shell = self.home / ".claude" / "skills"
        for skill in ("kept", "dropped"):
            self.write_skill(self.product.parent / "roles" / "ummanu" / skill, f"# {skill}\n")

    def declare(self, *skills: str) -> None:
        listed = ", ".join(f'"{skill}"' for skill in skills)
        self.product.write_text(
            f"[roles.ummanu]\nskills = [{listed}]\n\n"
            '[targets.t]\nshell = "claude"\nroot = "~/.claude/skills"\nroles = ["ummanu"]\n',
            encoding="utf-8",
        )

    def run_sync(self) -> dict:
        return sync(instance_path=self.instance, product_manifest=self.product, home=self.home)

    def test_the_next_sync_removes_a_delivered_copy_and_leaves_an_unmarked_one(self) -> None:
        self.declare("kept", "dropped")
        self.run_sync()
        self.assertTrue((self.shell / "dropped" / OWNERSHIP_MARKER).is_file())
        unmarked = self.shell / "operators-copy" / "SKILL.md"
        self.write_skill(unmarked.parent, "# dropped\n")

        self.declare("kept")
        before = audit(instance_path=self.instance, product_manifest=self.product, home=self.home)
        result = self.run_sync()

        self.assertEqual([(item["skill"], item["root"]) for item in before["retired"]], [("dropped", str(self.shell))])
        self.assertEqual([item["skill"] for item in result["removed"]], ["dropped"])
        self.assertFalse((self.shell / "dropped").exists())
        self.assertTrue((self.shell / "kept" / "SKILL.md").is_file())
        self.assertTrue(unmarked.is_file())
        self.assertTrue(result["after"]["ok"], result["after"])


class CommandEntryPointTests(OverlayFixture):
    """A skill that ships a command is only usable once that command is on PATH."""

    def setUp(self) -> None:
        super().setUp()
        self.bin_dir = self.root / "bin"
        self.link = self.bin_dir / "personal"
        self.helper = self.personal_skill_dir() / "personal.sh"
        env = mock.patch.dict(os.environ, {BIN_DIR_ENV: str(self.bin_dir)})
        env.start()
        self.addCleanup(env.stop)

    def install_personal_skill(self, *, with_command: bool = True) -> Path:
        self.write_skill(self.personal_skill_dir(), "# personal\n")
        if with_command:
            self.helper.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        return self.write_overlay('[roles.ummanu]\nskills = ["personal"]\n')

    def test_sync_materializes_an_executable_entry_point(self) -> None:
        overlay = self.install_personal_skill()

        result = sync(instance_path=self.instance)

        self.assertEqual(self.link.resolve(), self.helper)
        self.assertTrue(os.access(self.link, os.X_OK))
        self.assertTrue(bool(self.helper.stat().st_mode & stat.S_IXUSR))
        self.assertEqual(
            [(item["command"], item["was"], item["origin"], item["manifest"]) for item in result["linked"]],
            [("personal", "missing", INSTANCE_ORIGIN, str(overlay))],
        )

    def test_sync_repairs_a_link_left_behind_by_the_move_out_of_the_product(self) -> None:
        """The skill moved from the product tree into the installation; the old link dangles.

        The link still names the product `roles/` tree, which is where this registry reads product
        skills from, so the tree it points into is the proof that the link is ours.
        """
        self.install_personal_skill()
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        moved_from = self.product.parent / "roles" / "ummanu" / "personal"
        self.link.symlink_to(moved_from / "personal.sh")
        self.assertFalse(self.link.exists(), "the fixture link is supposed to dangle")

        before = audit(instance_path=self.instance)
        result = sync(instance_path=self.instance)

        self.assertEqual(
            [(item["command"], item["status"]) for item in before["entry_points"]],
            [("personal", "stale")],
        )
        self.assertEqual(self.link.resolve(), self.helper)
        self.assertEqual([item["was"] for item in result["linked"]], ["stale"])

    def test_a_link_into_an_older_layout_of_the_same_tree_is_repointed(self) -> None:
        """A skill that changed role keeps its command: the link still resolves into our tree."""
        self.install_personal_skill()
        older = self.instance / "skills" / "roles" / "assistant" / "personal"
        older.mkdir(parents=True)
        (older / "personal.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.link.symlink_to(older / "personal.sh")

        result = sync(instance_path=self.instance)

        self.assertEqual([item["was"] for item in result["linked"]], ["stale"])
        self.assertEqual(self.link.resolve(), self.helper)

    def test_a_second_sync_leaves_the_link_untouched(self) -> None:
        self.install_personal_skill()
        sync(instance_path=self.instance)
        before = self.link.lstat()

        result = sync(instance_path=self.instance)

        self.assertEqual([item["was"] for item in result["linked"]], ["ok"])
        self.assertEqual(self.link.lstat().st_ino, before.st_ino)
        self.assertEqual(self.link.lstat().st_ctime_ns, before.st_ctime_ns)
        self.assertTrue(result["after"]["ok"], result["after"])

    def test_a_relative_instance_path_still_materializes_a_working_command(self) -> None:
        """A symlink resolves its own text against `bin`, not against the working directory."""
        self.install_personal_skill()

        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.root)
        sync(instance_path="instance")

        target = Path(os.readlink(self.link))
        self.assertTrue(target.is_absolute(), target)
        self.assertTrue(self.link.exists(), f"{self.link} -> {target} dangles")
        self.assertEqual(self.link.resolve(), self.helper.resolve())

    def test_sync_refuses_to_overwrite_a_real_file(self) -> None:
        self.install_personal_skill()
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.link.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")

        with self.assertRaises(RegistryError) as caught:
            sync(instance_path=self.instance)

        self.assertIn(str(self.link), str(caught.exception))
        self.assertEqual(self.link.read_text(encoding="utf-8"), "#!/bin/sh\necho mine\n")

    def test_sync_refuses_a_foreign_link_that_merely_looks_like_ours(self) -> None:
        """Somebody else's file under a path with the shape of a skill source is still theirs."""
        self.install_personal_skill()
        theirs = self.root / "elsewhere" / "roles" / "ummanu" / "personal"
        theirs.mkdir(parents=True)
        script = theirs / "personal.sh"
        script.write_text("#!/bin/sh\necho theirs\n", encoding="utf-8")
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.link.symlink_to(script)

        result = audit(instance_path=self.instance)
        with self.assertRaises(RegistryError) as caught:
            sync(instance_path=self.instance)

        self.assertEqual([item["status"] for item in result["entry_points"]], ["conflict"])
        self.assertIn(str(script), str(caught.exception))
        self.assertEqual(self.link.resolve(), script)

    def test_sync_refuses_a_foreign_dangling_link_shaped_like_a_skill_source(self) -> None:
        """A path outside both trees is not ours to take over because nothing is there to break.

        The dangling case is where a name-shaped guess is least checkable: no file contradicts it,
        and the command being claimed can be the one that opens the operator's browser session.
        """
        self.install_personal_skill()
        theirs = self.root / "elsewhere" / "roles" / "ummanu" / "personal" / "personal.sh"
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.link.symlink_to(theirs)
        self.assertFalse(self.link.exists(), "the fixture link is supposed to dangle")

        result = audit(instance_path=self.instance)
        with self.assertRaises(RegistryError) as caught:
            sync(instance_path=self.instance)

        self.assertEqual([item["status"] for item in result["entry_points"]], ["conflict"])
        self.assertIn(str(theirs), str(caught.exception))
        self.assertEqual(Path(os.readlink(self.link)), theirs)

    def test_a_conflicting_entry_point_stops_sync_before_any_skill_is_copied(self) -> None:
        self.install_personal_skill()
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.link.write_text("mine\n", encoding="utf-8")

        with self.assertRaises(RegistryError):
            sync(instance_path=self.instance)

        self.assertFalse(self.shell_root.exists())

    def test_two_skills_of_one_name_cannot_both_own_the_command(self) -> None:
        """A registry that wants one link for two scripts is refused before anything is written."""
        overlay = self.install_personal_skill()
        # Separate shell roots: the two skills fit side by side on disk, and the one thing they
        # cannot both have is the single link named after them.
        self.write_product(
            '[roles.ummanu]\nskills = ["shipped"]\n\n'
            '[roles.helper]\nskills = ["personal"]\n\n'
            f'[targets.t]\nshell = "codex"\nroot = "{self.shell_root}"\n'
            'roles = ["ummanu"]\n\n'
            f'[targets.u]\nshell = "codex"\nroot = "{self.root / "helper-shell"}"\n'
            'roles = ["helper"]\n'
        )
        clashing = self.product.parent / "roles" / "helper" / "personal"
        self.write_skill(clashing, "# also personal\n")
        (clashing / "personal.sh").write_text("#!/bin/sh\n", encoding="utf-8")

        with self.assertRaises(RegistryError) as caught:
            sync(instance_path=self.instance)

        message = str(caught.exception)
        self.assertIn(str(self.product), message)
        self.assertIn(str(overlay), message)
        self.assertIn(str(self.link), message)
        self.assertFalse(self.shell_root.exists())
        self.assertFalse(self.bin_dir.exists())

    def test_the_audit_reports_an_entry_point_that_was_never_materialized(self) -> None:
        overlay = self.install_personal_skill()

        result = audit(instance_path=self.instance)

        self.assertFalse(result["ok"])
        self.assertEqual(
            [
                (item["command"], item["status"], item["origin"], item["manifest"])
                for item in result["entry_points"]
            ],
            [("personal", "missing", INSTANCE_ORIGIN, str(overlay))],
        )

    def test_the_text_audit_names_the_manifest_behind_a_broken_entry_point(self) -> None:
        overlay = self.install_personal_skill()

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(["audit", "--instance", str(self.instance)])

        line = next(line for line in out.getvalue().splitlines() if line.startswith("- personal "))
        self.assertIn(str(overlay), line)
        self.assertIn(INSTANCE_ORIGIN, line)

    def test_a_skill_that_ships_no_command_has_no_entry_point_to_answer_for(self) -> None:
        self.install_personal_skill(with_command=False)

        result = sync(instance_path=self.instance)

        self.assertEqual(result["linked"], [])
        self.assertTrue(result["after"]["ok"], result["after"])


class SkillDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.shell_root = self.root / "codex-shell"
        self.instance = self.root / "instance"
        self.instance.mkdir()
        self.manifest = self.root / "manifest.toml"
        self.write_manifest(shell="codex")
        env = mock.patch.dict(
            os.environ,
            {MANIFEST_ENV: str(self.manifest), "UMMANU_INSTANCE": str(self.instance)},
        )
        env.start()
        self.addCleanup(env.stop)

    def write_manifest(self, *, shell: str) -> None:
        self.manifest.write_text(
            f'[roles.observer]\nskills = ["{OBSERVER_SKILL}"]\n\n'
            f'[targets.t]\nshell = "{shell}"\n'
            f'root = "{self.shell_root}"\nroles = ["observer"]\n',
            encoding="utf-8",
        )

    def write_overlay(self, body: str) -> Path:
        overlay = self.instance / "skills" / "manifest.toml"
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text(body, encoding="utf-8")
        return overlay

    def deliver(self) -> Path:
        skill = self.shell_root / OBSERVER_SKILL / "SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text("# skill\n", encoding="utf-8")
        return skill

    def test_a_delivered_skill_reads_as_delivered_with_its_path(self) -> None:
        skill = self.deliver()

        result = skill_delivery("observer", OBSERVER_SKILL, "codex")

        self.assertTrue(result["delivered"])
        self.assertEqual(result["paths"], [str(skill)])
        self.assertEqual(result["reason"], "")

    def test_a_missing_skill_names_the_file_and_the_repair(self) -> None:
        result = skill_delivery("observer", OBSERVER_SKILL, "codex")

        self.assertFalse(result["delivered"])
        self.assertIn(str(self.shell_root / OBSERVER_SKILL / "SKILL.md"), result["reason"])
        self.assertIn("role-skills sync", result["reason"])

    def test_a_shell_without_a_target_is_not_delivery(self) -> None:
        self.deliver()

        result = skill_delivery("observer", OBSERVER_SKILL, "claude")

        self.assertFalse(result["delivered"])
        self.assertEqual(result["paths"], [])
        self.assertIn("no claude target", result["reason"])

    def test_an_unreadable_registry_is_not_delivery(self) -> None:
        self.deliver()
        self.manifest.write_text("this is not toml = [", encoding="utf-8")

        result = skill_delivery("observer", OBSERVER_SKILL, "codex")

        self.assertFalse(result["delivered"])
        self.assertIn("could not be read", result["reason"])

    def test_an_unreadable_overlay_is_not_delivery_either(self) -> None:
        """An unreadable registry is not evidence that the skill is there, whoever owns it."""
        self.deliver()
        self.write_overlay("this is not toml = [")

        result = skill_delivery("observer", OBSERVER_SKILL, "codex")

        self.assertFalse(result["delivered"])
        self.assertIn("could not be read", result["reason"])

    def test_an_overlay_skill_is_checked_in_the_shell_the_head_opens(self) -> None:
        self.write_overlay('[roles.observer]\nskills = ["personal"]\n')

        result = skill_delivery("observer", "personal", "codex")

        self.assertFalse(result["delivered"])
        self.assertEqual(result["paths"], [str(self.shell_root / "personal" / "SKILL.md")])


if __name__ == "__main__":
    unittest.main()
