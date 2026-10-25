"""Card states CI and Review are persisted without changing existing rows or rounds."""

from alembic import op

revision = "0036_card_ci_review"
down_revision = "0035_issue_reopened"
branch_labels = None
depends_on = None
release_safety = "additive"

GENERATED_NAME = "tasks_state_check"
NAMED_CHECK = "task_state_in_vocabulary"
_STATES = "'issues','ready','in_progress','validate','assessment','blocked','done'"


def upgrade() -> None:
    op.drop_constraint(GENERATED_NAME, "tasks", type_="check")
    op.create_check_constraint(NAMED_CHECK, "tasks", f"state IN ({_STATES},'ci','review')")


def downgrade() -> None:
    if op.get_bind().exec_driver_sql(
        "SELECT 1 FROM tasks WHERE state IN ('ci','review') LIMIT 1"
    ).first():
        raise RuntimeError("CI or Review cards exist; downgrade cannot drop their states")
    op.drop_constraint(NAMED_CHECK, "tasks", type_="check")
    op.create_check_constraint(GENERATED_NAME, "tasks", f"state IN ({_STATES})")
