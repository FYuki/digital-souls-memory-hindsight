"""Preserve projection order and distinguish delivery-driven replacements."""

from alembic import op

revision = "0005_bank_projection"
down_revision = "0004_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE outbox DROP CONSTRAINT outbox_turn_id_bank_id_document_id_key;
        ALTER TABLE outbox ADD COLUMN sequence_id bigint GENERATED ALWAYS AS IDENTITY;
        ALTER TABLE outbox ADD COLUMN update_mode text NOT NULL DEFAULT 'append'
            CHECK (update_mode IN ('append', 'replace'));
        ALTER TABLE outbox ADD COLUMN source_scope_id bigint REFERENCES delivery_scopes(scope_id);
        ALTER TABLE outbox ADD CHECK ((update_mode = 'replace') = (source_scope_id IS NOT NULL));
        CREATE UNIQUE INDEX outbox_initial_append ON outbox(turn_id, bank_id, document_id)
            WHERE update_mode = 'append';
        CREATE INDEX outbox_bank_order ON outbox(bank_id, sequence_id);
    """)


def downgrade() -> None:
    op.execute("""
        DELETE FROM outbox WHERE update_mode = 'replace';
        DROP INDEX outbox_bank_order;
        DROP INDEX outbox_initial_append;
        ALTER TABLE outbox DROP COLUMN source_scope_id, DROP COLUMN update_mode,
            DROP COLUMN sequence_id;
        ALTER TABLE outbox ADD UNIQUE (turn_id, bank_id, document_id);
    """)
