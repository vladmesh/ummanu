"""Who may propose a memory fact and who may publish one.

Contract: docs/PROTOCOLS.md, "Memory". Proposal authority and canonical-write
authority are two different gates: the butler may stage a proposal for the
curator inbox, and only a canonical writer may commit or supersede.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from ummanu.memory_write import (
    MemoryPermissionError,
    commit_memory_proposal,
    propose_memory_fact,
    supersede_memory_fact,
)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=True)
    return result.stdout


def init_instance_repo(instance_dir: Path) -> Path:
    instance_dir.mkdir(parents=True, exist_ok=True)
    git(instance_dir, "init", "--quiet", "--initial-branch", "main")
    git(instance_dir, "config", "user.name", "operator")
    git(instance_dir, "config", "user.email", "operator@example.invalid")
    git(instance_dir, "config", "commit.gpgsign", "false")
    (instance_dir / "instance.yaml").write_text("version: 1\n", encoding="utf-8")
    git(instance_dir, "add", "instance.yaml")
    git(instance_dir, "commit", "--quiet", "-m", "config")
    return instance_dir


class MemoryWriteAuthorityTests(unittest.TestCase):
    def setUp(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        self.root = Path(tmpdir.name)
        self.data_dir = self.root / "ummanu-data"
        self.instance_dir = init_instance_repo(self.root / "instance")
        (self.instance_dir / "projects").mkdir(exist_ok=True)
        (self.instance_dir / "projects" / "ummanu.yaml").write_text("id: ummanu\n", encoding="utf-8")
        self.fact = self.root / "fact.md"
        self.fact.write_text("a fact the butler noticed\n", encoding="utf-8")

    def _propose(self, *, actor: str, source: str, slug: str):
        return propose_memory_fact(
            self.data_dir,
            actor=actor,
            scope="project:ummanu",
            slug=slug,
            fact_file=self.fact,
            source=source,
        )

    def test_butler_proposal_is_staged_for_the_curator_inbox(self):
        proposal = self._propose(
            actor="butler:telegram/session", source="butler:telegram/session", slug="butler-fact"
        )

        staged = self.data_dir / "memory" / ".staging" / proposal.propose_id
        payload = json.loads((staged / "proposal.json").read_text(encoding="utf-8"))

        self.assertEqual(proposal.actor, "butler:telegram/session")
        self.assertEqual(proposal.source, "butler:telegram/session")
        self.assertEqual(payload["actor"], "butler:telegram/session")
        self.assertEqual(payload["scope_dir"], "ummanu")
        self.assertEqual(payload["slug"], "butler-fact")
        self.assertIn("a fact the butler noticed", (staged / "fact.md").read_text(encoding="utf-8"))
        # Nothing canonical was written: the proposal only asks the curator.
        self.assertFalse((self.instance_dir / "state" / "memory" / "facts").exists())

    def test_butler_cannot_commit_its_own_proposal(self):
        proposal = self._propose(
            actor="butler:telegram/session", source="butler:telegram/session", slug="butler-fact"
        )

        with self.assertRaises(MemoryPermissionError) as caught:
            commit_memory_proposal(
                self.data_dir,
                self.instance_dir,
                actor="butler:telegram/session",
                propose_id=proposal.propose_id,
            )

        message = str(caught.exception)
        self.assertIn("may propose memory facts but cannot commit canonical memory", message)
        self.assertIn("butler proposals await curator review", message)
        self.assertNotIn("is not allowed to write memory", message)
        # The proposal survives the refusal and stays in the inbox.
        self.assertTrue(
            (self.data_dir / "memory" / ".staging" / proposal.propose_id / "proposal.json").is_file()
        )

    def test_a_privileged_reviewer_publishes_a_butler_proposal(self):
        """Publication stays with the reviewer, and the fact keeps butler provenance.

        Cross-actor commit authority is unchanged by this card: it is the
        `ummanu`/`operator` rule in `_ensure_commit_actor`, not a new one.
        """
        proposal = self._propose(
            actor="butler:telegram/session", source="butler:telegram/session", slug="butler-fact"
        )

        result = commit_memory_proposal(
            self.data_dir,
            self.instance_dir,
            actor="operator:vlad",
            propose_id=proposal.propose_id,
        )

        self.assertEqual(result.fact, "ummanu/butler-fact")
        self.assertEqual(result.actor, "butler:telegram/session")
        self.assertEqual(result.source, "butler:telegram/session")
        self.assertTrue(
            (self.instance_dir / "state" / "memory" / "facts" / "ummanu" / "butler-fact.md").is_file()
        )

    def test_butler_cannot_supersede_a_canonical_fact(self):
        proposal = self._propose(
            actor="curator:claude/session", source="curator:claude/session", slug="old-fact"
        )
        commit_memory_proposal(
            self.data_dir,
            self.instance_dir,
            actor="curator:claude/session",
            propose_id=proposal.propose_id,
        )
        replacement = self.root / "replacement.md"
        replacement.write_text("the butler's newer reading\n", encoding="utf-8")

        with self.assertRaises(MemoryPermissionError) as caught:
            supersede_memory_fact(
                self.data_dir,
                self.instance_dir,
                actor="butler:telegram/session",
                scope="project:ummanu",
                slug="new-fact",
                fact_file=replacement,
                supersedes=["old-fact"],
                source="butler:telegram/session",
            )

        self.assertIn("cannot supersede canonical memory", str(caught.exception))
        self.assertTrue(
            (self.instance_dir / "state" / "memory" / "facts" / "ummanu" / "old-fact.md").is_file()
        )
        self.assertFalse(
            (self.instance_dir / "state" / "memory" / "facts" / "ummanu" / "new-fact.md").exists()
        )

    def test_butler_cannot_propose_a_fact_sourced_as_another_role(self):
        with self.assertRaises(MemoryPermissionError) as caught:
            self._propose(
                actor="butler:telegram/session",
                source="curator:claude/session",
                slug="borrowed-source",
            )

        self.assertIn("source curator:claude/session is not allowed", str(caught.exception))

    def test_unknown_role_still_gets_the_generic_write_refusal(self):
        with self.assertRaises(MemoryPermissionError) as caught:
            self._propose(actor="worker:codex/1", source="worker:codex/1", slug="worker-fact")

        self.assertIn("actor is not allowed to write memory: worker:codex/1", str(caught.exception))

        with self.assertRaises(MemoryPermissionError) as commit_caught:
            commit_memory_proposal(
                self.data_dir,
                self.instance_dir,
                actor="worker:codex/1",
                propose_id="0" * 32,
            )

        self.assertIn("actor is not allowed to write memory: worker:codex/1", str(commit_caught.exception))

    def test_privileged_commit_ownership_rules_are_unchanged(self):
        proposal = self._propose(actor="curator:claude/one", source="curator:claude/one", slug="owned-fact")

        with self.assertRaises(MemoryPermissionError) as caught:
            commit_memory_proposal(
                self.data_dir,
                self.instance_dir,
                actor="curator:claude/two",
                propose_id=proposal.propose_id,
            )

        self.assertIn(
            "actor curator:claude/two cannot commit proposal owned by curator:claude/one",
            str(caught.exception),
        )

        for actor in ("ummanu:tick", "operator:vlad"):
            with self.subTest(actor=actor):
                other = self._propose(
                    actor="curator:claude/one",
                    source="curator:claude/one",
                    slug=f"owned-{actor.split(':')[0]}",
                )
                result = commit_memory_proposal(
                    self.data_dir,
                    self.instance_dir,
                    actor=actor,
                    propose_id=other.propose_id,
                )
                self.assertEqual(result.actor, "curator:claude/one")


if __name__ == "__main__":
    unittest.main()
