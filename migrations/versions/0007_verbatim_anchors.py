"""Store approved anchor references and enable ledger-side full-text matching."""

from alembic import op

revision = "0007_verbatim_anchors"
down_revision = "0006_provision_records"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgroonga")
    op.execute(r"""
        CREATE TABLE verbatim_anchors (
            character_id text NOT NULL CHECK (character_id ~ '^[A-Za-z0-9_-]+$'),
            anchor_id text NOT NULL CHECK (anchor_id ~ '^[A-Za-z0-9_-]+$'),
            turn_id text NOT NULL REFERENCES turns(turn_id),
            kind text NOT NULL CHECK (kind IN (
                'identity_statement', 'commitment', 'nickname', 'anniversary'
            )),
            reason text NOT NULL CHECK (reason ~ '\S'),
            approved_by text NOT NULL CHECK (approved_by ~ '\S'),
            registered_at timestamptz NOT NULL,
            PRIMARY KEY (character_id, anchor_id)
        );
    """)


def downgrade() -> None:
    op.execute("DROP TABLE verbatim_anchors")
    op.execute("DROP EXTENSION pgroonga")
