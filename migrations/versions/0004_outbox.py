"""Durable retain operations with fenced execution leases."""

from alembic import op

revision = "0004_outbox"
down_revision = "0003_delivery_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE outbox (
            operation_id text PRIMARY KEY,
            turn_id text NOT NULL REFERENCES turns(turn_id),
            bank_id text NOT NULL,
            document_id text NOT NULL,
            content text NOT NULL,
            occurred_at timestamptz NOT NULL,
            status text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
            attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            available_at timestamptz NOT NULL DEFAULT now(),
            lease_token text,
            lease_until timestamptz,
            last_error text,
            completed_at timestamptz,
            UNIQUE (turn_id, bank_id, document_id),
            CHECK ((status = 'processing') = (lease_token IS NOT NULL)),
            CHECK ((status = 'processing') = (lease_until IS NOT NULL)),
            CHECK ((status = 'completed') = (completed_at IS NOT NULL))
        );
        CREATE INDEX outbox_due ON outbox (available_at) WHERE status = 'pending';
        CREATE INDEX outbox_leases ON outbox (lease_until) WHERE status = 'processing';
    """)


def downgrade() -> None:
    op.execute("DROP TABLE outbox")
