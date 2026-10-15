"""Owner event kinds for a PO session moved to the other CLI family after a provider refusal.

`po_session_moved_account` (needs_owner) is an auth, plan or quota refusal the owner has to act on;
`po_session_moved` (notice) is any other provider refusal. Both CHECKs are restated one for one.
"""
from alembic import op

revision = "0033_po_session_moved"
down_revision = "0032_accept_decision"
branch_labels = None
depends_on = None
release_safety = "additive"

_KINDS = (
    "'card_handed_to_owner','steward_needs_human','sprint_closed','sprint_stopped',"
    "'budget_signal','observer_dead','head_dead','po_turn_failed','provider_red',"
    "'delegated_card_settled','e2e_budget_spent','e2e_after_merge','card_waits_for_person','po_card_escalated'"
)
_NEEDS_OWNER = "'card_handed_to_owner','steward_needs_human','po_card_escalated'"


def upgrade() -> None:
    op.drop_constraint("owner_event_class_follows_kind", "owner_events", type_="check")
    op.create_check_constraint("owner_event_class_follows_kind", "owner_events",
        "(class = 'needs_owner') = (kind IN ('card_handed_to_owner','steward_needs_human','po_card_escalated',"
        "'po_session_moved_account'))")
    op.drop_constraint("owner_event_kind_in_vocabulary", "owner_events", type_="check")
    op.create_check_constraint("owner_event_kind_in_vocabulary", "owner_events",
        "kind IN ('card_handed_to_owner','steward_needs_human','sprint_closed','sprint_stopped',"
        "'budget_signal','observer_dead','head_dead','po_turn_failed','provider_red',"
        "'delegated_card_settled','e2e_budget_spent','e2e_after_merge','card_waits_for_person','po_card_escalated',"
        "'po_session_moved','po_session_moved_account')")


def downgrade() -> None:
    if op.get_bind().exec_driver_sql(
        "SELECT 1 FROM owner_events WHERE kind IN ('po_session_moved','po_session_moved_account') LIMIT 1"
    ).first():
        raise RuntimeError("PO session moved events exist; downgrade cannot drop their kinds")
    op.drop_constraint("owner_event_class_follows_kind", "owner_events", type_="check")
    op.create_check_constraint("owner_event_class_follows_kind", "owner_events",
        f"(class = 'needs_owner') = (kind IN ({_NEEDS_OWNER}))")
    op.drop_constraint("owner_event_kind_in_vocabulary", "owner_events", type_="check")
    op.create_check_constraint("owner_event_kind_in_vocabulary", "owner_events", f"kind IN ({_KINDS})")
