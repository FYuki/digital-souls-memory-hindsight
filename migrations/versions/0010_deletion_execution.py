"""Execute author deletions without weakening ordinary evidence writes."""

from alembic import op

revision = "0010_deletion_execution"
down_revision = "0009_deletion_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE turn_tombstones (
            turn_id text PRIMARY KEY,
            request_id text NOT NULL REFERENCES deletion_requests(request_id),
            deleted_at timestamptz NOT NULL
        );
        CREATE TABLE deletion_outbox (
            request_id text PRIMARY KEY REFERENCES deletion_requests(request_id),
            status text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','processing','completed','failed')),
            attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            available_at timestamptz NOT NULL DEFAULT now(),
            lease_token text,
            lease_until timestamptz,
            last_error text,
            completed_at timestamptz,
            progress jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(progress) = 'object'),
            CHECK ((status = 'processing') = (lease_token IS NOT NULL)),
            CHECK ((status = 'processing') = (lease_until IS NOT NULL)),
            CHECK ((status = 'completed') = (completed_at IS NOT NULL))
        );
        CREATE INDEX deletion_outbox_due ON deletion_outbox(available_at)
            WHERE status = 'pending';
        CREATE INDEX deletion_outbox_leases ON deletion_outbox(lease_until)
            WHERE status = 'processing';
        ALTER TABLE outbox ALTER COLUMN turn_id DROP NOT NULL;
        ALTER TABLE outbox ADD COLUMN deletion_request_id text
            REFERENCES deletion_requests(request_id);
        DO $$ DECLARE constraint_name text; BEGIN
            FOR constraint_name IN SELECT conname FROM pg_constraint
                WHERE conrelid = 'outbox'::regclass AND contype = 'c'
                AND pg_get_constraintdef(oid) LIKE '%source_scope_id%'
            LOOP EXECUTE format('ALTER TABLE outbox DROP CONSTRAINT %I', constraint_name);
            END LOOP;
        END $$;
        ALTER TABLE outbox ADD CONSTRAINT outbox_projection_source CHECK (
            (update_mode = 'append' AND turn_id IS NOT NULL
                AND source_scope_id IS NULL AND deletion_request_id IS NULL)
            OR (update_mode IN ('replace','delete') AND (
                (source_scope_id IS NOT NULL AND deletion_request_id IS NULL AND turn_id IS NOT NULL)
                OR (source_scope_id IS NULL AND deletion_request_id IS NOT NULL AND turn_id IS NULL)
            ))
        );
        ALTER TABLE turns DROP CONSTRAINT turns_reply_to_turn_id_fkey;
        CREATE FUNCTION permit_evidence_deletion() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF current_setting('dsm.evidence_deletion', true) IS DISTINCT FROM 'on' THEN
                RAISE EXCEPTION 'Evidence is append-only' USING ERRCODE = '55000';
            END IF;
            RETURN NULL;
        END;
        $$;
    """)
    for table in ("turns", "delivery_receipts", "delivery_scopes"):
        op.execute(f"""
            DROP TRIGGER evidence_append_only ON {table};
            CREATE TRIGGER evidence_append_only BEFORE UPDATE OR TRUNCATE ON {table}
                FOR EACH STATEMENT EXECUTE FUNCTION reject_evidence_change();
            CREATE TRIGGER evidence_author_deletion BEFORE DELETE ON {table}
                FOR EACH STATEMENT EXECUTE FUNCTION permit_evidence_deletion();
        """)


def downgrade() -> None:
    for table in ("turns", "delivery_receipts", "delivery_scopes"):
        op.execute(f"""
            DROP TRIGGER evidence_append_only ON {table};
            DROP TRIGGER evidence_author_deletion ON {table};
            CREATE TRIGGER evidence_append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON {table}
                FOR EACH STATEMENT EXECUTE FUNCTION reject_evidence_change();
        """)
    op.execute("""
        DROP FUNCTION permit_evidence_deletion();
        DELETE FROM outbox WHERE deletion_request_id IS NOT NULL;
        ALTER TABLE outbox DROP CONSTRAINT outbox_projection_source;
        ALTER TABLE outbox DROP COLUMN deletion_request_id;
        ALTER TABLE outbox ALTER COLUMN turn_id SET NOT NULL;
        ALTER TABLE outbox ADD CHECK (
            (update_mode IN ('replace','delete')) = (source_scope_id IS NOT NULL));
        ALTER TABLE turns ADD CONSTRAINT turns_reply_to_turn_id_fkey
            FOREIGN KEY (reply_to_turn_id) REFERENCES turns(turn_id) NOT VALID;
        DROP TABLE turn_tombstones;
        DROP TABLE deletion_outbox;
    """)
