"""Volatile state and its generation-fenced work queue."""

from alembic import op

revision = "0015_character_states"
down_revision = "0014_character_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE character_states (
            character_id text NOT NULL,
            session_id text NOT NULL REFERENCES sessions(session_id),
            surface text NOT NULL,
            generation bigint NOT NULL CHECK (generation >= 1),
            mood text,
            relationship_temperature text,
            current_topic text,
            state_generation bigint CHECK (state_generation >= 1 AND state_generation <= generation),
            generated_at timestamptz,
            PRIMARY KEY (character_id, session_id, surface),
            CHECK (
                (mood IS NULL AND relationship_temperature IS NULL AND current_topic IS NULL
                    AND state_generation IS NULL AND generated_at IS NULL)
                OR (mood IS NOT NULL AND relationship_temperature IS NOT NULL
                    AND current_topic IS NOT NULL AND state_generation IS NOT NULL
                    AND generated_at IS NOT NULL)
            )
        );
        CREATE TABLE character_state_outbox (
            character_id text NOT NULL,
            session_id text NOT NULL,
            surface text NOT NULL,
            generation bigint NOT NULL CHECK (generation >= 1),
            status text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','processing','completed','failed')),
            attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            available_at timestamptz NOT NULL,
            lease_token text,
            lease_until timestamptz,
            last_error text,
            completed_at timestamptz,
            PRIMARY KEY (character_id, session_id, surface),
            FOREIGN KEY (character_id, session_id, surface)
                REFERENCES character_states(character_id, session_id, surface),
            CHECK ((status = 'processing') = (lease_token IS NOT NULL)),
            CHECK ((status = 'processing') = (lease_until IS NOT NULL)),
            CHECK ((status = 'completed') = (completed_at IS NOT NULL))
        );
        CREATE INDEX character_state_outbox_due ON character_state_outbox(available_at)
            WHERE status = 'pending';
        CREATE INDEX character_state_outbox_leases ON character_state_outbox(lease_until)
            WHERE status = 'processing';
    """)


def downgrade() -> None:
    op.execute("DROP TABLE character_state_outbox; DROP TABLE character_states;")
