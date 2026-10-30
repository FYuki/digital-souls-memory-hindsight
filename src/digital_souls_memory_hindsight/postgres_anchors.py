"""Anchor metadata storage and PGroonga matching on projected ledger text."""

import psycopg

from .anchors.ports import AnchorConflict, VerbatimAnchor
from .evidence.ports import EvidenceUnavailable

_COLUMNS = "character_id, anchor_id, turn_id, kind, reason, approved_by, registered_at"

# A question rarely contains the anchor text verbatim ("土曜日の約束はいつ？"), so an
# anchor also matches when it contains any content noun of the query. Pronouns,
# suffixes, numerals and one-character nouns are too common to select anchors.
_MATCH = """
    SELECT a.anchor_id
    FROM unnest(%s::text[], %s::text[]) AS a(anchor_id, text),
        (SELECT %s::text AS value) AS q,
        LATERAL (
            SELECT coalesce(array_agg(DISTINCT t->>'value'), '{}') AS words
            FROM unnest(
                pgroonga_tokenize(q.value, 'tokenizer', 'TokenMecab("include_class", true)')
            ) AS t
            WHERE t->'metadata'->>'class' = '名詞'
                AND coalesce(t->'metadata'->>'subclass0', '')
                    NOT IN ('代名詞', '非自立', '接尾', '数')
                AND char_length(t->>'value') >= 2
        ) AS k
    WHERE a.text &@~ pgroonga_query_escape(q.value) OR a.text &@| k.words
"""


class PostgresAnchorStore:
    def __init__(self, database_url: str) -> None:
        self._url = database_url

    def add(self, value: VerbatimAnchor) -> None:
        try:
            with psycopg.connect(self._url, connect_timeout=5) as connection:
                connection.isolation_level = psycopg.IsolationLevel.READ_COMMITTED
                inserted = connection.execute(
                    f"INSERT INTO verbatim_anchors ({_COLUMNS}) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (character_id, anchor_id) DO NOTHING RETURNING anchor_id",
                    (
                        value.character_id,
                        value.anchor_id,
                        value.turn_id,
                        value.kind,
                        value.reason,
                        value.approved_by,
                        value.registered_at,
                    ),
                ).fetchone()
                if inserted is None:
                    raise AnchorConflict()
        except psycopg.Error as error:
            raise EvidenceUnavailable() from error

    def get(self, character_id: str, anchor_id: str) -> VerbatimAnchor | None:
        try:
            with psycopg.connect(self._url, connect_timeout=5) as connection:
                row = connection.execute(
                    f"SELECT {_COLUMNS} FROM verbatim_anchors "
                    "WHERE character_id = %s AND anchor_id = %s",
                    (character_id, anchor_id),
                ).fetchone()
                return VerbatimAnchor(*row) if row is not None else None
        except psycopg.Error as error:
            raise EvidenceUnavailable() from error

    def list(self, character_id: str) -> tuple[VerbatimAnchor, ...]:
        try:
            with psycopg.connect(self._url, connect_timeout=5) as connection:
                rows = connection.execute(
                    f"SELECT {_COLUMNS} FROM verbatim_anchors "
                    "WHERE character_id = %s ORDER BY registered_at, anchor_id",
                    (character_id,),
                ).fetchall()
                return tuple(VerbatimAnchor(*row) for row in rows)
        except psycopg.Error as error:
            raise EvidenceUnavailable() from error

    def match(self, candidates: tuple[tuple[str, str], ...], query: str) -> frozenset[str]:
        try:
            with psycopg.connect(self._url, connect_timeout=5) as connection:
                rows = connection.execute(
                    _MATCH,
                    (
                        [identifier for identifier, _ in candidates],
                        [text for _, text in candidates],
                        query,
                    ),
                ).fetchall()
                return frozenset(row[0] for row in rows)
        except psycopg.Error as error:
            raise EvidenceUnavailable() from error
