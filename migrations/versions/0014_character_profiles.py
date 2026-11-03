"""Persist profile limits, revisions and original turn intervals."""

from alembic import op

revision = "0014_character_profiles"
down_revision = "0013_voice_exemplars"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(r"""
        CREATE TABLE character_profiles (
            character_id text PRIMARY KEY CHECK (character_id ~ '^[A-Za-z0-9_-]+$'),
            char_limit bigint NOT NULL CHECK (char_limit >= 0),
            revision integer NOT NULL CHECK (revision >= 1),
            updated_at timestamptz NOT NULL
        );
        CREATE TABLE character_profile_entries (
            character_id text NOT NULL REFERENCES character_profiles(character_id),
            position integer NOT NULL CHECK (position >= 0),
            turn_id text NOT NULL REFERENCES turns(turn_id),
            span_start integer NOT NULL,
            span_end integer NOT NULL,
            CHECK (span_start >= 0 AND span_start < span_end),
            PRIMARY KEY (character_id, position)
        );
        CREATE INDEX character_profile_entries_turn ON character_profile_entries(turn_id)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE character_profile_entries")
    op.execute("DROP TABLE character_profiles")
