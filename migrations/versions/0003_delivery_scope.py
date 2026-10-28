"""Record delivery decisions separately from immutable turns and receipts."""

from alembic import op

revision = "0003_delivery_scope"
down_revision = "0002_evidence_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE delivery_scopes (
            scope_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            turn_id text NOT NULL REFERENCES turns(turn_id),
            status text NOT NULL CHECK (
                status IN ('immediate', 'pending', 'receipt_confirmed', 'assumed_confirmed')
            ),
            spoken_until integer,
            decided_at timestamptz NOT NULL,
            CHECK (
                (status = 'pending' AND spoken_until IS NULL) OR
                (status <> 'pending' AND spoken_until IS NOT NULL AND spoken_until >= 0)
            )
        );
        CREATE UNIQUE INDEX delivery_scopes_initial ON delivery_scopes (turn_id)
            WHERE status IN ('immediate', 'pending');
        CREATE INDEX delivery_scopes_latest ON delivery_scopes (turn_id, scope_id DESC);
        CREATE TRIGGER evidence_append_only BEFORE UPDATE OR DELETE OR TRUNCATE
            ON delivery_scopes FOR EACH STATEMENT EXECUTE FUNCTION reject_evidence_change();
    """)


def downgrade() -> None:
    op.execute("DROP TABLE delivery_scopes")
