"""The old product name stays only where docs/RENAME.md, Guard allows it.

Every tracked path and every tracked text file is checked for `secretary` in any case. A match
passes only when a row of `ALLOWLIST` covers it: the Hermes agent (H), the instance repository (I),
historical records (R) or guard/policy source (G). There is one table and no per-file
exemption outside those four classes, so a new mention of the old name fails here with its file,
line and text.

Class G: this file and the matcher it shares with `ummanu config check`
(`src/ummanu/infra/old_name_guard.py`, which holds the live root's allowlist) name the old name on
purpose.
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

from ummanu.infra import old_name_guard
from ummanu.infra.old_name_guard import INSTANCE_PROJECT, applies as _applies, text_of

ROOT = Path(__file__).resolve().parent.parent

#: Any tracked path.
ANYWHERE = ("*",)

#: The allowlist: (class, what the old name may appear in, the paths where it may). A pattern of
#: `None` allows the whole file, content and path alike. `fnmatch` patterns, so `*` crosses `/`.
ALLOWLIST: tuple[tuple[str, re.Pattern[str] | None, tuple[str, ...]], ...] = (
    ("H", re.compile(r"~/\.hermes/skills/secretary(?:-roles)?\b"), ("skills/manifest.toml",)),
    ("H", re.compile(r"\bhermes-secretary(?:-roles)?\b"), ("skills/manifest.toml",)),
    # `secretary-instance-maintenance` is a product unit, not the instance repository.
    ("I", re.compile(r"\bsecretary-instance\b(?!-maintenance)"), ANYWHERE),
    # A card ref; `secretary-0.1.0.dist-info` is a version, not a ref.
    ("R", re.compile(r"\bsecretary-\d+\b(?!\.\d)"), ANYWHERE),
    ("R", re.compile(r"\bsecretary_1727_[0-9a-f]+\.jsonl\.gz\b"), ANYWHERE),
    # A quoted historical record, replayed against its immutable audit digest byte for byte.
    ("R", None, ("tests/fixtures/gate_attestation_1883.json",)),
    # Historical cwd data has one released producer (sprint:1475) and curator rebind consumer.
    ("R", re.compile(r'^_OLD_(?:PRODUCT = "secretary"|DATA = "secretary-data")$'),
     ("src/ummanu/automations/agents/curator/move_paths.py",)),
    ("R", None, ("tests/fixtures/rebind_move_sprint1475.json",)),
    # Exact negative env/package fixtures. No runtime alias is admitted.
    ("G", re.compile(r'"(?:SECRETARY_(?:DATA_DIR|INSTANCE|RUNTIME_ENV_FILE)|TA_SECRETARY_REPO|SECRETARY_|TA_SECRETARY_|import secretary|ModuleNotFoundError: No module named \'secretary\')"'),
     ("tests/test_env_prefix.py",)),
    # Removal regression's former verb and exact removed script/test inventory, only here.
    ("G", re.compile(r'"(?:from-secretary|scripts/(?:rename_to_ummanu\.py|transition-from-secretary\.sh)|tests/test_transition_from_secretary\.py)"'),
     ("tests/test_installation_retirement.py",)),
    ("G", None, ("docs/RENAME.md", "tests/test_old_name_guard.py", "src/ummanu/infra/old_name_guard.py")),
)


def violations(path: str, text: str | None) -> list[str]:
    """Every match of the old name in `path` and in its `text` (None for a binary file) that no
    allowlist row covers, as `path:line: match in context`. The matcher is the one
    `ummanu config check` runs over the live root."""
    return old_name_guard.violations(path, text, ALLOWLIST)


#: Only the policy owner may define the historical instance name; operational source imports it.
PRODUCT_SOURCE = "src/*"
INSTANCE_OWNER = "src/ummanu/infra/old_name_guard.py"
INSTANCE_DEFINITION = 'INSTANCE_PROJECT = "secretary-instance"'


def instance_literals(path: str, text: str | None) -> list[str]:
    """Instance-name literals outside the owner's single exact definition are forbidden."""
    if text is None or not _applies((PRODUCT_SOURCE,), path):
        return []
    return [
        f"{path}:{number}: {line.strip()}"
        for number, line in enumerate(text.splitlines(), 1)
        if INSTANCE_PROJECT in line and not (path == INSTANCE_OWNER and line == INSTANCE_DEFINITION)
    ]


def tracked_files() -> list[str]:
    completed = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=False)
    if completed.returncode != 0:
        raise AssertionError(f"git ls-files failed: {completed.stderr.decode(errors='replace').strip()}")
    return [name for name in completed.stdout.decode().split("\0") if name]


class OldNameGuardTests(unittest.TestCase):
    def test_the_tree_names_the_old_product_only_where_the_allowlist_does(self) -> None:
        found: list[str] = []
        for name in tracked_files():
            path = ROOT / name
            if path.is_symlink() or not path.is_file():
                found += violations(name, None)
                continue
            found += violations(name, text_of(path.read_bytes()))
        self.assertEqual(found, [], "the old product name outside docs/RENAME.md, Guard:\n" + "\n".join(found))

    def test_the_instance_name_has_one_policy_definition_and_no_operational_literals(self) -> None:
        """Operational callers depend on the guard owner, never the old live-root literal."""
        found: list[str] = []
        for name in tracked_files():
            path = ROOT / name
            if path.is_file() and not path.is_symlink():
                found += instance_literals(name, text_of(path.read_bytes()))
        self.assertEqual(found, [], "import INSTANCE_PROJECT from ummanu.infra.old_name_guard:\n" + "\n".join(found))
        self.assertEqual(INSTANCE_PROJECT, "secretary-instance")
        owner = (ROOT / INSTANCE_OWNER).read_text()
        self.assertEqual(owner.splitlines().count(INSTANCE_DEFINITION), 1)

    def test_a_reintroduced_instance_literal_fails_except_the_single_owner_definition(self) -> None:
        line = 'DEFAULT = Path.home() / "secretary-instance"\n'
        self.assertEqual(
            instance_literals("src/ummanu/runtime/paths.py", line),
            ['src/ummanu/runtime/paths.py:1: DEFAULT = Path.home() / "secretary-instance"'],
        )
        self.assertEqual(len(instance_literals("src/ummanu/schemas/adapter.schema.json", line)), 1)
        self.assertEqual(len(instance_literals(INSTANCE_OWNER, line)), 1)
        self.assertEqual(instance_literals(INSTANCE_OWNER, INSTANCE_DEFINITION), [])
        self.assertEqual(instance_literals("docs/RECOVERY.md", line), [])
        self.assertEqual(instance_literals("tests/test_x.py", line), [])

    def test_a_stray_old_name_fails_in_every_file_outside_the_whole_file_rows(self) -> None:
        names = tracked_files()
        self.assertGreater(len(names), 100)
        exempt = {name for name in names if violations(name, "secretary") == []}
        self.assertEqual(
            exempt,
            {
                name
                for name in names
                if any(pattern is None and _applies(globs, name) for _, pattern, globs in ALLOWLIST)
            },
            "only the whole-file rows (the guard/policy files and exact R records) may carry a stray old name",
        )

    def test_a_failure_names_the_file_line_and_match(self) -> None:
        found = violations("docs/OPERATIONS.md", "# Operations\n\nRun `python3 -m Secretary doctor`.\n")
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].startswith("docs/OPERATIONS.md:3: 'Secretary' in "), found[0])
        self.assertEqual(
            violations("src/ummanu/secretary_helper.py", ""),
            [
                "src/ummanu/secretary_helper.py: path carries 'secretary'",
            ],
        )
        self.assertEqual(len(violations("tests/test_x.py", "SECRETARY_DATA_DIR = 1\n")), 1)

    def test_each_class_covers_its_own_spelling_only(self) -> None:
        allowed = {
            "skills/manifest.toml": '[targets.hermes-secretary-roles]\nroot = "~/.hermes/skills/secretary"\n',
            "README.md": "The instance repo `vladmesh/secretary-instance`, its card secretary-instance-12.\n",
            "docs/OPERATIONS.md": "Since secretary-1932 (branch pipeline/secretary-1932).\n",
            "tests/test_local_pty_journal_turn.py": 'FIXTURE = "secretary_1727_9c6b884b.jsonl.gz"\n',
            "tests/fixtures/local_pty_journals/secretary_1727_9c6b884b.jsonl.gz": None,
            "docs/RENAME.md": "secretary → ummanu\n",
            "tests/fixtures/gate_attestation_1883.json": '{"url": "https://github.com/vladmesh/secretary"}\n',
            "src/ummanu/automations/agents/curator/move_paths.py": '_OLD_PRODUCT = "secretary"\n_OLD_DATA = "secretary-data"\n',
            "tests/test_env_prefix.py": 'old = "SECRETARY_DATA_DIR"\n',
            "tests/test_installation_retirement.py": '["from-secretary"]\n',
        }
        for path, text in allowed.items():
            with self.subTest(path=path):
                self.assertEqual(violations(path, text), [])
        refused = {
            # H spellings belong to the skills manifest only.
            "docs/OPERATIONS.md": "root = ~/.hermes/skills/secretary\n",
            # The instance-maintenance unit is a product unit.
            "packaging/systemd/README.md": "secretary-instance-maintenance.timer\n",
            # A version is not a card ref.
            "tests/test_upgrade.py": 'dist = "secretary-0.1.0.dist-info"\n',
            "tests/test_role_skills.py": "[roles.secretary]\n",
            "pyproject.toml": "[tool.secretary]\n",
            "docs/PROTOCOLS.md": "Run `ummanu transition from-secretary --plan`.\n",
            "tests/ci-shards.txt": "unit tests/test_transition_from_secretary.py\n",
            "src/ummanu/automations/agents/curator/move_paths.py": 'ALIAS = "secretary"\n',
            "src/ummanu/infra/live_root_findings.py": 'DEFAULT = "secretary"\n',
            "docs/RECOVERY.md": "scripts/transition-from-secretary.sh\n",
            "tests/test_env_prefix.py": 'old = "SECRETARY_DB_HOST"\n',
            "tests/test_installation_retirement.py": 'alias = "secretary"\n',
            "src/ummanu/cli.py": 'prog="secretary"\n',
        }
        for path, text in refused.items():
            with self.subTest(path=path):
                self.assertEqual(len(violations(path, text)), 1, violations(path, text))


if __name__ == "__main__":
    unittest.main()
