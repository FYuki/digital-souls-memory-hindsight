"""Persist the first voice review independently of exemplar deletion."""

from alembic import op

revision = "0017_voice_consistency_reviews"
down_revision = "0016_resident_provisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(r"""
        CREATE TABLE voice_consistency_reviews (
            character_id text PRIMARY KEY CHECK (character_id ~ '^[A-Za-z0-9_-]+$'),
            exemplar_id text NOT NULL,
            consistent boolean NOT NULL,
            reason text NOT NULL CHECK (reason ~ '\S'),
            reviewed_at timestamptz NOT NULL
        );
    """)


def downgrade() -> None:
    op.execute("DROP TABLE voice_consistency_reviews")
