"""Append-only registered identities and references to resident provisions."""

from alembic import op

revision = "0012_identity_versions"
down_revision = "0011_deletion_fencing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(r"""
        CREATE TABLE core_identities (
            character_id text NOT NULL CHECK (character_id ~ '^[A-Za-z0-9_-]+$'),
            version integer NOT NULL CHECK (version > 0),
            "values" text[] NOT NULL,
            prohibitions text[] NOT NULL,
            registered_at timestamptz NOT NULL,
            PRIMARY KEY (character_id, version)
        );
        CREATE TABLE temperaments (
            character_id text NOT NULL CHECK (character_id ~ '^[A-Za-z0-9_-]+$'),
            version integer NOT NULL CHECK (version > 0),
            description text NOT NULL CHECK (description ~ '\S'),
            registered_at timestamptz NOT NULL,
            PRIMARY KEY (character_id, version)
        );
        CREATE TABLE personality_profiles (
            character_id text NOT NULL CHECK (character_id ~ '^[A-Za-z0-9_-]+$'),
            version integer NOT NULL CHECK (version > 0),
            aspects jsonb NOT NULL,
            description text NOT NULL CHECK (description ~ '\S'),
            renderer_version text NOT NULL CHECK (renderer_version ~ '\S'),
            temperament_version integer NOT NULL,
            registered_at timestamptz NOT NULL,
            PRIMARY KEY (character_id, version),
            UNIQUE (character_id),
            FOREIGN KEY (character_id, temperament_version)
                REFERENCES temperaments(character_id, version)
        );
        CREATE FUNCTION reject_identity_change() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Identity is append-only' USING ERRCODE = '55000';
        END;
        $$;
        ALTER TABLE provision_records ADD COLUMN resident_provision_id text
            REFERENCES provision_records(provision_id) ON DELETE SET NULL;
        CREATE INDEX provision_resident_reference ON provision_records(resident_provision_id)
            WHERE resident_provision_id IS NOT NULL;
        DO $$ DECLARE constraint_name text; BEGIN
            FOR constraint_name IN SELECT conname FROM pg_constraint
                WHERE conrelid = 'provision_records'::regclass AND contype = 'c'
                AND (pg_get_constraintdef(oid) LIKE '%kind%'
                    OR (pg_get_constraintdef(oid) LIKE '%query%'
                        AND pg_get_constraintdef(oid) LIKE '%turn_id%'))
            LOOP EXECUTE format('ALTER TABLE provision_records DROP CONSTRAINT %I', constraint_name);
            END LOOP;
        END $$;
        ALTER TABLE provision_records ADD CONSTRAINT provision_kind
            CHECK (kind IN ('turn','resident'));
        ALTER TABLE provision_records ADD CONSTRAINT provision_turn_source
            CHECK (kind <> 'turn' OR ((query IS NULL) <> (turn_id IS NULL)));
    """)
    for table in ("core_identities", "temperaments", "personality_profiles"):
        op.execute(f"""
            CREATE TRIGGER identity_append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON {table}
                FOR EACH STATEMENT EXECUTE FUNCTION reject_identity_change();
        """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE provision_records DROP COLUMN resident_provision_id;
        DELETE FROM provision_records WHERE kind='resident';
        ALTER TABLE provision_records DROP CONSTRAINT provision_kind;
        ALTER TABLE provision_records DROP CONSTRAINT provision_turn_source;
        ALTER TABLE provision_records ADD CHECK (kind='turn');
        ALTER TABLE provision_records ADD CHECK ((query IS NULL) <> (turn_id IS NULL));
        DROP TABLE personality_profiles;
        DROP TABLE temperaments;
        DROP TABLE core_identities;
        DROP FUNCTION reject_identity_change();
    """)
