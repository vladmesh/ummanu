"""Card state Post-merge CI is persisted without changing existing rows or rounds."""

from alembic import op

revision = "0038_card_post_merge_ci"
down_revision = "0037_stage_budget_candidates"
branch_labels = None
depends_on = None
release_safety = "additive"

NAMED_CHECK = "task_state_in_vocabulary"
_STATES = "'issues','ready','in_progress','validate','ci','review','assessment','blocked','done'"


def upgrade() -> None:
    op.drop_constraint(NAMED_CHECK, "tasks", type_="check")
    op.create_check_constraint(NAMED_CHECK, "tasks", f"state IN ({_STATES},'post_merge_ci')")


def downgrade() -> None:
    if op.get_bind().exec_driver_sql("SELECT 1 FROM tasks WHERE state = 'post_merge_ci' LIMIT 1").first():
        raise RuntimeError("Post-merge CI cards exist; downgrade cannot drop their state")
    op.drop_constraint(NAMED_CHECK, "tasks", type_="check")
    op.create_check_constraint(NAMED_CHECK, "tasks", f"state IN ({_STATES})")
