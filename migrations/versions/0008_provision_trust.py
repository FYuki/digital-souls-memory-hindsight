"""Keep trust provenance outside Hindsight, including previously recorded provisions."""

from alembic import op

revision = "0008_provision_trust"
down_revision = "0007_verbatim_anchors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE provision_records
            ADD COLUMN memory_trust text[],
            ADD COLUMN source_span_trust text[],
            ADD COLUMN verbatim_anchor_trust text[];

        UPDATE provision_records SET
            memory_trust = array_fill('inferred'::text, ARRAY[cardinality(memory_ids)]),
            source_span_trust = array_fill('evidence'::text, ARRAY[jsonb_array_length(source_spans)]),
            verbatim_anchor_trust = array_fill('evidence'::text, ARRAY[cardinality(verbatim_anchor_ids)]);

        ALTER TABLE provision_records
            ALTER COLUMN memory_trust SET NOT NULL,
            ALTER COLUMN source_span_trust SET NOT NULL,
            ALTER COLUMN verbatim_anchor_trust SET NOT NULL,
            ADD CHECK (cardinality(memory_trust) = cardinality(memory_ids)),
            ADD CHECK (cardinality(source_span_trust) = jsonb_array_length(source_spans)),
            ADD CHECK (cardinality(verbatim_anchor_trust) = cardinality(verbatim_anchor_ids)),
            ADD CHECK (memory_trust <@ ARRAY['evidence', 'inferred']::text[]),
            ADD CHECK (source_span_trust <@ ARRAY['evidence', 'inferred']::text[]),
            ADD CHECK (verbatim_anchor_trust <@ ARRAY['evidence', 'inferred']::text[]);
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE provision_records
            DROP COLUMN memory_trust,
            DROP COLUMN source_span_trust,
            DROP COLUMN verbatim_anchor_trust;
    """)
