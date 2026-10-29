"""Migrate only the ledger database selected by the shared settings boundary."""

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import make_url

from digital_souls_memory_hindsight.settings import load_settings

database_url = make_url(load_settings().database_url).set(drivername="postgresql+psycopg")

if context.is_offline_mode():
    context.configure(url=database_url, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    with create_engine(database_url, poolclass=pool.NullPool).connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
