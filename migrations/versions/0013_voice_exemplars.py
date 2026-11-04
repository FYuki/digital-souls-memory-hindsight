"""Persist approved, verbatim voice exemplars and their original intervals."""

from alembic import op

revision = "0013_voice_exemplars"
down_revision = "0012_identity_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(r"""
        CREATE TABLE voice_exemplars (
            character_id text NOT NULL CHECK (character_id ~ '^[A-Za-z0-9_-]+$'),
            exemplar_id text NOT NULL CHECK (exemplar_id ~ '^[A-Za-z0-9_-]+$'),
            source text NOT NULL CHECK (source IN ('canonical_seed', 'turn')),
            answer text NOT NULL CHECK (answer <> ''),
            question text CHECK (question <> ''),
            answer_turn_id text REFERENCES turns(turn_id),
            answer_start integer,
            answer_end integer,
            question_turn_id text REFERENCES turns(turn_id),
            question_start integer,
            question_end integer,
            approved_by text NOT NULL CHECK (approved_by ~ '\S'),
            registered_at timestamptz NOT NULL,
            PRIMARY KEY (character_id, exemplar_id),
            CHECK (
                (source = 'canonical_seed'
                    AND answer_turn_id IS NULL AND answer_start IS NULL AND answer_end IS NULL
                    AND question_turn_id IS NULL AND question_start IS NULL AND question_end IS NULL)
                OR (source = 'turn'
                    AND answer_turn_id IS NOT NULL
                    AND answer_start IS NOT NULL AND answer_end IS NOT NULL
                    AND answer_start >= 0 AND answer_start < answer_end
                    AND (
                        (question IS NULL AND question_turn_id IS NULL
                            AND question_start IS NULL AND question_end IS NULL)
                        OR (question IS NOT NULL AND question_turn_id IS NOT NULL
                            AND question_start IS NOT NULL AND question_end IS NOT NULL
                            AND question_start >= 0 AND question_start < question_end)
                    ))
            )
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE voice_exemplars")
