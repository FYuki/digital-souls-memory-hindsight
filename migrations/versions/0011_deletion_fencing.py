"""Persist operation identifiers used to invalidate delayed projection sends."""

from alembic import op

revision = "0011_deletion_fencing"
down_revision = "0010_deletion_execution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE outbox ADD COLUMN fenced_operation_id text;
        ALTER TABLE outbox ADD CONSTRAINT outbox_fenced_operation CHECK (
            fenced_operation_id IS NULL OR (
                deletion_request_id IS NOT NULL AND update_mode = 'replace'
                AND turn_id IS NULL AND source_scope_id IS NULL
                AND operation_id = fenced_operation_id
            )
        );
    """)


def downgrade() -> None:
    op.execute("""
        DELETE FROM outbox WHERE fenced_operation_id IS NOT NULL;
        ALTER TABLE outbox DROP CONSTRAINT outbox_fenced_operation;
        ALTER TABLE outbox DROP COLUMN fenced_operation_id;
    """)
