"""Record deletion requests and keep their character/session blocking scope."""

from alembic import op

revision = "0009_deletion_requests"
down_revision = "0008_provision_trust"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(r"""
        CREATE TABLE deletion_requests (
            request_id text PRIMARY KEY CHECK (request_id ~ '\S'),
            requested_by text NOT NULL,
            user_id text,
            target_turn_ids text[] NOT NULL,
            status text NOT NULL CHECK (status IN ('accepted','processing','completed','failed')),
            cascade_result jsonb,
            error text,
            requested_at timestamptz NOT NULL,
            completed_at timestamptz
        );
        CREATE TABLE deletion_blocks (
            request_id text NOT NULL REFERENCES deletion_requests(request_id),
            character_id text NOT NULL,
            session_id text NOT NULL,
            PRIMARY KEY (request_id, character_id, session_id)
        );
        CREATE INDEX deletion_blocks_character ON deletion_blocks(character_id);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE deletion_blocks")
    op.execute("DROP TABLE deletion_requests")
