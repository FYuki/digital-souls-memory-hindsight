"""Record the contents of each successful turn recall."""

from alembic import op

revision = "0006_provision_records"
down_revision = "0005_bank_projection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE provision_records (
            provision_id text PRIMARY KEY,
            kind text NOT NULL CHECK (kind = 'turn'),
            character_id text NOT NULL,
            session_id text NOT NULL,
            surface text NOT NULL,
            query text,
            turn_id text,
            sensitivity text,
            classifier_version text,
            memory_ids text[] NOT NULL,
            source_spans jsonb NOT NULL,
            verbatim_anchor_ids text[] NOT NULL,
            temperament_version text,
            personality_version text,
            created_at timestamptz NOT NULL,
            CHECK ((query IS NULL) <> (turn_id IS NULL)),
            CHECK ((query IS NOT NULL) = (sensitivity IS NOT NULL)),
            CHECK ((query IS NOT NULL) = (classifier_version IS NOT NULL))
        );
    """)


def downgrade() -> None:
    op.execute("DROP TABLE provision_records")
