"""Routine released notifications become notices without changing audit or read history.

0018/0023/0024/0025 producers used the three reclassified kinds for direct attention.
Explicit handovers and steward escalations retain their IDs, text and unanswered state.
The released runtime can still read every row. Its obsolete attention inserts during
activation are normalized to notices before the current constraint is checked.
"""
from alembic import op

revision = "0028_owner_turns"
down_revision = "0027_sprint_owner_decisions"
branch_labels = None
depends_on = None
release_safety = "additive"


def upgrade() -> None:
    op.drop_constraint("owner_event_class_follows_kind", "owner_events", type_="check")
    op.get_bind().exec_driver_sql(
        "UPDATE owner_events SET class = 'notice' "
        "WHERE kind IN ('card_waits_for_person','e2e_budget_spent','e2e_after_merge')"
    )
    # Actual released 0023/0024/0025 producers still pass needs_owner during activation.
    # Accept their event and dedup identity without preserving their obsolete authority.
    op.get_bind().exec_driver_sql("""
        CREATE FUNCTION owner_event_routine_notice() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.kind IN ('card_waits_for_person','e2e_budget_spent','e2e_after_merge')
               AND NEW.class = 'needs_owner' THEN
                NEW.class := 'notice';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.get_bind().exec_driver_sql(
        "CREATE TRIGGER owner_event_routine_notice BEFORE INSERT OR UPDATE OF kind, class ON owner_events "
        "FOR EACH ROW EXECUTE FUNCTION owner_event_routine_notice()"
    )
    op.create_check_constraint("owner_event_class_follows_kind", "owner_events",
        "(class = 'needs_owner') = (kind IN ('card_handed_to_owner','steward_needs_human','po_card_escalated'))")
    op.drop_constraint("owner_event_kind_in_vocabulary", "owner_events", type_="check")
    op.create_check_constraint("owner_event_kind_in_vocabulary", "owner_events",
        "kind IN ('card_handed_to_owner','steward_needs_human','sprint_closed','sprint_stopped',"
        "'budget_signal','observer_dead','head_dead','po_turn_failed','provider_red',"
        "'delegated_card_settled','e2e_budget_spent','e2e_after_merge','card_waits_for_person','po_card_escalated')")


def downgrade() -> None:
    if op.get_bind().exec_driver_sql(
        "SELECT 1 FROM owner_events WHERE kind IN "
        "('card_waits_for_person','e2e_budget_spent','e2e_after_merge','po_card_escalated') LIMIT 1"
    ).first():
        raise RuntimeError("owner-turn events exist; downgrade cannot invent implicit owner authority")
    op.get_bind().exec_driver_sql("DROP TRIGGER owner_event_routine_notice ON owner_events")
    op.get_bind().exec_driver_sql("DROP FUNCTION owner_event_routine_notice()")
    op.drop_constraint("owner_event_class_follows_kind", "owner_events", type_="check")
    op.create_check_constraint("owner_event_class_follows_kind", "owner_events",
        "(class = 'needs_owner') = (kind IN ('card_handed_to_owner','steward_needs_human',"
        "'e2e_budget_spent','e2e_after_merge','card_waits_for_person'))")
    op.drop_constraint("owner_event_kind_in_vocabulary", "owner_events", type_="check")
    op.create_check_constraint("owner_event_kind_in_vocabulary", "owner_events",
        "kind IN ('card_handed_to_owner','steward_needs_human','sprint_closed','sprint_stopped',"
        "'budget_signal','observer_dead','head_dead','po_turn_failed','provider_red',"
        "'delegated_card_settled','e2e_budget_spent','e2e_after_merge','card_waits_for_person')")
