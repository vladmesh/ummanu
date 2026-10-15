from __future__ import annotations

import unittest

from ummanu.board.models import CardState
from ummanu.board.task_routing import (
    ACTIVE_STATES,
    BLOCK_CLASSIFICATION_VALUES,
    DECIDED_TARGETS,
    DECISION_TARGETS,
    DECISION_VALUES,
    EDITABLE_STATES,
    FAMILY_PREFERENCE_VALUES,
    ROUTING_PHASE_VALUES,
    TASK_COMPLEXITY_VALUES,
    TASK_TYPE_VALUES,
    UNDECIDED_EXITS,
    BlockClassification,
    FamilyPreference,
    RoutingPhase,
    TaskComplexity,
    TaskDecision,
    TaskMetadata,
    TaskReview,
    TaskRouting,
    TaskType,
    default_review,
    impact_bounds_refusal,
)


class TaskRoutingVocabularyTests(unittest.TestCase):
    def test_closed_vocabularies_keep_the_released_spellings(self) -> None:
        self.assertEqual(TASK_TYPE_VALUES, {"code", "research", "infra", "decision", "operation", "wait"})
        self.assertEqual(TASK_COMPLEXITY_VALUES, {"cheap", "standard", "hard", "frontier"})
        self.assertEqual(FAMILY_PREFERENCE_VALUES, {"auto", "claude", "codex"})
        self.assertEqual(ROUTING_PHASE_VALUES, {"worker", "review", "verdict"})
        self.assertEqual(BLOCK_CLASSIFICATION_VALUES, ("external_fact", "wrong_task_definition"))
        self.assertEqual(DECISION_VALUES, {"accept", "rework", "reslice"})
        self.assertEqual(TaskType.CODE, "code")
        self.assertEqual(TaskComplexity.FRONTIER, "frontier")
        self.assertEqual(FamilyPreference.CODEX, "codex")
        self.assertEqual(RoutingPhase.REVIEW, "review")
        self.assertEqual(BlockClassification.EXTERNAL_FACT, "external_fact")
        self.assertEqual(TaskDecision.RESLICE, "reslice")

    def test_decision_and_state_sets_are_typed(self) -> None:
        self.assertEqual(
            DECISION_TARGETS,
            {
                TaskDecision.ACCEPT: CardState.DONE,
                TaskDecision.REWORK: CardState.IN_PROGRESS,
                TaskDecision.RESLICE: CardState.BLOCKED,
            },
        )
        self.assertEqual(EDITABLE_STATES, {CardState.READY, CardState.BLOCKED})
        self.assertEqual(ACTIVE_STATES, {CardState.IN_PROGRESS, CardState.VALIDATE, CardState.ASSESSMENT})
        self.assertEqual(DECIDED_TARGETS, {CardState.DONE, CardState.IN_PROGRESS})
        self.assertEqual(UNDECIDED_EXITS, {CardState.READY, CardState.VALIDATE, CardState.ISSUES})

    def test_legacy_routing_normalizes_once_and_renders_the_same_document(self) -> None:
        routing = TaskRouting.from_legacy(
            {
                "complexity": "hard",
                "family_preference": "codex",
                "head": "codex-sol",
                "review_head": "claude-opus",
                "resolved_head": "codex-sol-1",
                "resolved_review_head": "claude-opus-1",
                "routing_reason": "quota",
                "quota_snapshot_at": "2026-09-16T10:00:00Z",
                "codex_launch_mode": "tui",
            },
            codex_modes={"tui"},
        )
        self.assertIs(routing.complexity, TaskComplexity.HARD)
        self.assertIs(routing.family_preference, FamilyPreference.CODEX)
        self.assertEqual(
            routing.to_document(),
            {
                "complexity": "hard",
                "family_preference": "codex",
                "head_override": "codex-sol",
                "review_head_override": "claude-opus",
                "resolved_worker_family": None,
                "resolved_worker_head": "codex-sol-1",
                "resolved_review_family": None,
                "resolved_review_head": "claude-opus-1",
                "routing_reason": "quota",
                "quota_snapshot_at": "2026-09-16T10:00:00Z",
                "codex_launch_mode": "tui",
            },
        )

    def test_legacy_routing_keeps_existing_fallback_semantics(self) -> None:
        routing = TaskRouting.from_legacy(
            {"complexity": "retired", "family_preference": "other", "codex_launch_mode": "exec"},
            codex_modes={"tui"},
        )
        self.assertIs(routing.complexity, TaskComplexity.STANDARD)
        self.assertIs(routing.family_preference, FamilyPreference.AUTO)
        self.assertIsNone(routing.codex_launch_mode)

    def test_metadata_is_typed_internally_but_keeps_the_public_shape(self) -> None:
        metadata = TaskMetadata.from_legacy(
            {
                "project": "ummanu",
                "task_type": "code",
                "blocked_by": "ummanu-1",
                "claim": "worker-1",
                "complexity": "frontier",
                "family_preference": "claude",
                "retry_same": "2",
                "retry_switch": "1",
                "retry_heads": "claude-opus,codex-sol",
                "slug": "work",
                "base_branch": "main",
                "seed_ref": "abc123",
                "supersedes": "ummanu-2",
                "sprint_ref": "sprint:7",
                "record_type": "task",
            },
            codex_modes={"tui"},
        )
        self.assertIs(metadata.task_type, TaskType.CODE)
        self.assertIs(metadata.routing.complexity, TaskComplexity.FRONTIER)
        self.assertIs(metadata.routing.family_preference, FamilyPreference.CLAUDE)
        self.assertEqual(metadata.retry_heads, ("claude-opus", "codex-sol"))
        document = metadata.to_document_fields()
        self.assertEqual(document["type"], "code")
        self.assertEqual(document["routing"]["complexity"], "frontier")
        self.assertEqual(document["retry"]["heads"], ["claude-opus", "codex-sol"])

    def test_unknown_legacy_task_type_is_preserved_only_as_compatibility_text(self) -> None:
        metadata = TaskMetadata.from_legacy({"task_type": "old-kind"}, codex_modes={"tui"})
        self.assertIsNone(metadata.task_type)
        self.assertEqual(metadata.task_type_raw, "old-kind")
        self.assertEqual(metadata.to_document_fields()["type"], "old-kind")

    def test_review_and_live_impact_are_typed_and_legacy_silence_reads_as_required(self) -> None:
        legacy = TaskMetadata.from_legacy({"task_type": "research"}, codex_modes={"tui"})
        self.assertIs(legacy.review, TaskReview.REQUIRED)
        self.assertFalse(legacy.live_impact)
        stored = TaskMetadata.from_legacy(
            {"task_type": "research", "review": "skipped", "live_impact": "1"}, codex_modes={"tui"}
        )
        document = stored.to_document_fields()
        self.assertEqual((document["type"], document["review"], document["live_impact"]), ("research", "skipped", True))
        self.assertIs(TaskMetadata.from_legacy({"task_type": "infra"}, codex_modes={"tui"}).task_type, TaskType.INFRA)
        self.assertEqual(
            [default_review(kind) for kind in (TaskType.CODE, TaskType.RESEARCH, TaskType.INFRA)],
            [TaskReview.REQUIRED, TaskReview.SKIPPED, TaskReview.SKIPPED],
        )

    def test_impact_bounds_need_three_non_empty_subsections_under_the_section(self) -> None:
        complete = "## Impact bounds\n### Allowed\na\n### Forbidden\nb\n### Cleanup\nc\n"
        self.assertEqual(impact_bounds_refusal("# Title\n\n" + complete + "## Later\nx"), "")
        self.assertIn("needs a '## Impact bounds' section", impact_bounds_refusal("### Allowed\na"))
        self.assertIn(
            "missing '### Forbidden', '### Cleanup' and has empty '### Allowed'",
            impact_bounds_refusal("## Impact bounds\n### Allowed\n   \n"),
        )
        # A subsection after the section has ended does not count.
        self.assertIn(
            "missing '### Cleanup'",
            impact_bounds_refusal("## Impact bounds\n### Allowed\na\n### Forbidden\nb\n## Else\n### Cleanup\nc"),
        )

if __name__ == "__main__":
    unittest.main()
