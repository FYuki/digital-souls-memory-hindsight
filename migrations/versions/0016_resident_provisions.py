"""Record resident selections separately from turn recall material."""

from alembic import op

revision = "0016_resident_provisions"
down_revision = "0015_character_states"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE provision_records
            ALTER COLUMN temperament_version TYPE integer USING temperament_version::integer,
            ALTER COLUMN personality_version TYPE integer USING personality_version::integer,
            ADD COLUMN core_identity_version integer,
            ADD COLUMN exemplar_ids text[],
            ADD COLUMN profile_revision integer,
            ADD COLUMN profile_entries jsonb,
            ADD COLUMN processing_time_ms integer,
            ADD CONSTRAINT provision_resident_shape CHECK (kind <> 'resident' OR (
                turn_id IS NULL AND exemplar_ids IS NOT NULL
                AND profile_entries IS NOT NULL AND jsonb_typeof(profile_entries) = 'array'
                AND processing_time_ms IS NOT NULL AND processing_time_ms >= 0
                AND (profile_revision IS NOT NULL OR profile_entries = '[]'::jsonb)
                AND cardinality(memory_ids) = 0 AND source_spans = '[]'::jsonb
                AND cardinality(verbatim_anchor_ids) = 0
            )),
            ADD CONSTRAINT provision_turn_without_resident_fields CHECK (kind <> 'turn' OR (
                core_identity_version IS NULL AND temperament_version IS NULL
                AND personality_version IS NULL AND exemplar_ids IS NULL
                AND profile_revision IS NULL AND profile_entries IS NULL
                AND processing_time_ms IS NULL
            ));
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE provision_records
            DROP CONSTRAINT provision_resident_shape,
            DROP CONSTRAINT provision_turn_without_resident_fields,
            DROP COLUMN core_identity_version,
            DROP COLUMN exemplar_ids,
            DROP COLUMN profile_revision,
            DROP COLUMN profile_entries,
            DROP COLUMN processing_time_ms,
            ALTER COLUMN temperament_version TYPE text USING temperament_version::text,
            ALTER COLUMN personality_version TYPE text USING personality_version::text;
    """)
