"""Character-serialized identity transactions, with commit before publication."""

from collections.abc import Iterator
from contextlib import contextmanager
from types import MappingProxyType

import psycopg
from psycopg import sql
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb

from .identity.ports import (
    CoreIdentity,
    IdentityConflict,
    IdentitySnapshot,
    IdentityTransaction,
    IdentityUnavailable,
    PersonalityProfile,
    Temperament,
)


class _IdentityRows:
    def __init__(self, connection: psycopg.Connection[DictRow], character_id: str) -> None:
        self._connection = connection
        self._character_id = character_id

    def _get(self, table: str, version: int | None) -> DictRow | None:
        statement = sql.SQL("SELECT * FROM {} WHERE character_id=%s").format(sql.Identifier(table))
        parameters: tuple[object, ...] = (self._character_id,)
        if version is not None:
            statement += sql.SQL(" AND version=%s")
            parameters += (version,)
        statement += sql.SQL(" ORDER BY version DESC LIMIT 1")
        return self._connection.execute(statement, parameters).fetchone()

    def get_core(self, version: int | None) -> CoreIdentity | None:
        row = self._get("core_identities", version)
        return (
            CoreIdentity(
                row["character_id"],
                row["version"],
                tuple(row["values"]),
                tuple(row["prohibitions"]),
                row["registered_at"],
            )
            if row is not None
            else None
        )

    def get_temperament(self, version: int | None) -> Temperament | None:
        row = self._get("temperaments", version)
        return Temperament(**row) if row is not None else None

    def get_profile(self, version: int | None) -> PersonalityProfile | None:
        row = self._get("personality_profiles", version)
        return (
            PersonalityProfile(**{**row, "aspects": MappingProxyType(dict(row["aspects"]))})
            if row is not None
            else None
        )

    def add_core(self, value: CoreIdentity) -> None:
        self._connection.execute(
            'INSERT INTO core_identities (character_id,version,"values",prohibitions,'
            "registered_at) "
            "VALUES (%s,%s,%s,%s,%s)",
            (
                value.character_id,
                value.version,
                list(value.values),
                list(value.prohibitions),
                value.registered_at,
            ),
        )

    def add_temperament(self, value: Temperament) -> None:
        self._connection.execute(
            "INSERT INTO temperaments (character_id,version,description,registered_at) "
            "VALUES (%s,%s,%s,%s)",
            (value.character_id, value.version, value.description, value.registered_at),
        )

    def add_profile(self, value: PersonalityProfile) -> None:
        self._connection.execute(
            "INSERT INTO personality_profiles (character_id,version,aspects,description,"
            "renderer_version,temperament_version,registered_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (
                value.character_id,
                value.version,
                Jsonb(dict(value.aspects)),
                value.description,
                value.renderer_version,
                value.temperament_version,
                value.registered_at,
            ),
        )


class PostgresIdentityStore:
    def __init__(self, database_url: str) -> None:
        self._url = database_url

    @contextmanager
    def _connection(self) -> Iterator[psycopg.Connection[DictRow]]:
        try:
            with psycopg.connect(self._url, row_factory=dict_row, connect_timeout=5) as connection:
                connection.isolation_level = psycopg.IsolationLevel.READ_COMMITTED
                yield connection
        except psycopg.errors.UniqueViolation as error:
            raise IdentityConflict() from error
        except psycopg.Error as error:
            raise IdentityUnavailable() from error

    @contextmanager
    def transaction(self, character_id: str) -> Iterator[IdentityTransaction]:
        with self._connection() as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ("identity:" + character_id,),
            )
            yield _IdentityRows(connection, character_id)

    def current(self, character_id: str) -> IdentitySnapshot:
        with self._connection() as connection:
            rows = _IdentityRows(connection, character_id)
            return IdentitySnapshot(
                rows.get_core(None), rows.get_temperament(None), rows.get_profile(None)
            )

    def get_core(self, character_id: str, version: int) -> CoreIdentity | None:
        with self._connection() as connection:
            return _IdentityRows(connection, character_id).get_core(version)

    def get_temperament(self, character_id: str, version: int) -> Temperament | None:
        with self._connection() as connection:
            return _IdentityRows(connection, character_id).get_temperament(version)

    def get_profile(self, character_id: str, version: int) -> PersonalityProfile | None:
        with self._connection() as connection:
            return _IdentityRows(connection, character_id).get_profile(version)
