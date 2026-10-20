"""`issue.reopened` joins the board event vocabulary: a closed Issue may return to open.

`0001`'s unnamed `kind` CHECK on `board_events` is restated with the new kind under a name, so the
next revision that widens it does not have to know PostgreSQL's generated one.
"""

from alembic import op

revision = "0035_issue_reopened"
down_revision = "0034_po_restore_sequence"
branch_labels = None
depends_on = None
release_safety = "additive"

#: PostgreSQL's own name for `0001`'s unnamed single-column `CHECK` on `board_events.kind`.
GENERATED_NAME = "board_events_kind_check"
NAMED_CHECK = "board_event_kind_in_vocabulary"
_KINDS = (
    "'entity.created','entity.updated','product.archived','issue.closed',"
    "'sprint.closed','sprint.stopped','sprint.reopened',"
    "'card.readied','card.started','card.submitted','card.assessed','card.reworked',"
    "'card.released','card.blocked','card.unblocked','card.returned','card.moved',"
    "'card.reported','card.verdict','card.decided','card.decision_refused',"
    "'attempt.usage','attempt.outcome'"
)


def upgrade() -> None:
    op.drop_constraint(GENERATED_NAME, "board_events", type_="check")
    op.create_check_constraint(NAMED_CHECK, "board_events", f"kind IN ({_KINDS},'issue.reopened')")


def downgrade() -> None:
    if op.get_bind().exec_driver_sql(
        "SELECT 1 FROM board_events WHERE kind = 'issue.reopened' LIMIT 1"
    ).first():
        raise RuntimeError("issue.reopened events exist; downgrade cannot drop their kind")
    op.drop_constraint(NAMED_CHECK, "board_events", type_="check")
    op.create_check_constraint(GENERATED_NAME, "board_events", f"kind IN ({_KINDS})")
