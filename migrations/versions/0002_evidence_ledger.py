"""Install the independent, append-only evidence ledger."""

from alembic import op

revision = "0002_evidence_ledger"
down_revision = "0001_ledger_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE sessions (
            session_id text PRIMARY KEY,
            started_at timestamptz NOT NULL,
            ended_at timestamptz,
            surface text NOT NULL,
            reports_delivery boolean NOT NULL,
            CHECK (ended_at IS NULL OR ended_at >= started_at)
        );
        CREATE TABLE turns (
            turn_id text PRIMARY KEY,
            session_id text NOT NULL REFERENCES sessions(session_id),
            surface text NOT NULL,
            actor text NOT NULL,
            kind text NOT NULL CHECK (kind IN ('utterance', 'observation', 'thought')),
            text text NOT NULL,
            perceived_by text[] NOT NULL,
            addressees text[] NOT NULL,
            address_mode text NOT NULL CHECK (address_mode IN ('direct', 'broadcast', 'self')),
            reply_to_turn_id text REFERENCES turns(turn_id),
            language text NOT NULL,
            register jsonb NOT NULL CHECK (jsonb_typeof(register) = 'object'),
            sensitivity text NOT NULL,
            classifier_version text NOT NULL,
            occurred_at timestamptz NOT NULL,
            recorded_at timestamptz NOT NULL,
            CHECK ((address_mode = 'direct') = (cardinality(addressees) > 0)),
            CHECK (kind <> 'thought' OR perceived_by = ARRAY[actor])
        );
        CREATE INDEX turns_perception ON turns USING gin (perceived_by);
        CREATE TABLE delivery_receipts (
            receipt_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            turn_id text NOT NULL REFERENCES turns(turn_id),
            status text NOT NULL CHECK (status IN ('full', 'partial', 'none')),
            spoken_until integer NOT NULL CHECK (spoken_until >= 0),
            reported_by text NOT NULL,
            reported_at timestamptz NOT NULL
        );
        CREATE TABLE session_endings (
            session_id text PRIMARY KEY REFERENCES sessions(session_id),
            ended_at timestamptz NOT NULL,
            reported_by text NOT NULL,
            reported_at timestamptz NOT NULL
        );
        CREATE FUNCTION reject_evidence_change() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Evidence is append-only' USING ERRCODE = '55000';
        END;
        $$;
    """)
    for table in ("sessions", "turns", "delivery_receipts", "session_endings"):
        op.execute(
            f"CREATE TRIGGER evidence_append_only BEFORE UPDATE OR DELETE OR TRUNCATE "
            f"ON {table} FOR EACH STATEMENT EXECUTE FUNCTION reject_evidence_change()"
        )


def downgrade() -> None:
    for table in ("session_endings", "delivery_receipts", "turns", "sessions"):
        op.execute(f"DROP TABLE {table}")
    op.execute("DROP FUNCTION reject_evidence_change()")
