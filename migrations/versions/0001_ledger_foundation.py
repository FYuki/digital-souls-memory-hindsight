"""Initialize the migration history; business tables belong to later issues."""

revision = "0001_ledger_foundation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """The foundation revision intentionally creates no business tables."""


def downgrade() -> None:
    """The foundation revision has no business schema to remove."""
