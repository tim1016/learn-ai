"""The registry is custody-free: the shipped DDL never grows lane data (FR-023).

Every table and every column of the fleet schema is asserted against a closed
allowlist. Custody, orders, fills, positions, activation, arming and envelope
facts live on clerk volumes under provider authority; a column with such a
name appearing here would mean the coordinator had started storing execution
data, which ADR 0062 Decision 1 forbids.
"""

from __future__ import annotations

import pathlib
import re
import sqlite3

from app.broker.fleet import schema

_ALLOWED_TABLES = {
    "fleet_meta",
    "clerks",
    "clerk_sessions",
    "clerk_session_history",
    "approved_endpoints",
    "account_assignments",
    "account_assignment_history",
    "routing_receipts",
}

_FORBIDDEN_NAME_FRAGMENTS = (
    "order",
    "fill",
    "position",
    "custody",
    "activation",
    "arming",
    "envelope",
    "balance",
    "equity",
    "pnl",
    "exposure",
    "api_key",
    "secret",
    "credential",
    "endpoint_url",
)


def _identifier_tokens(name: str) -> tuple[str, ...]:
    """Split a schema identifier into its lowercase tokens, in declared order."""
    return tuple(re.findall(r"[a-z0-9]+", name.lower()))


def _contains_fragment(tokens: tuple[str, ...], fragment: str) -> bool:
    """True if ``fragment``'s own tokens appear as a contiguous run in ``tokens``.

    A single-token set-membership check can never match a multi-word
    fragment like ``api_key`` or ``endpoint_url`` — neither is ever, itself,
    one token. The fragment must be matched against the column's token
    *sequence*, not its set.
    """
    fragment_tokens = _identifier_tokens(fragment)
    span = len(fragment_tokens)
    return any(
        tokens[index : index + span] == fragment_tokens
        for index in range(len(tokens) - span + 1)
    )


def _materialize_schema(db_path: pathlib.Path) -> sqlite3.Connection:
    """Open the schema on a real file, not ``:memory:``.

    The registry is always file-backed, and an in-memory database cannot
    retain the WAL mode the PRAGMA set pins.
    """
    conn = sqlite3.connect(db_path)
    schema.configure_connection(conn)
    schema.apply_schema(conn)
    return conn


def test_every_table_is_expected(tmp_path: pathlib.Path) -> None:
    """The shipped DDL contains exactly the allowed tables."""
    conn = _materialize_schema(tmp_path / 'registry.db')
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    finally:
        conn.close()
    assert tables == _ALLOWED_TABLES


def test_no_column_name_carries_a_custody_or_secret_fragment(tmp_path: pathlib.Path) -> None:
    """No column name carries a custody, financial or secret fragment."""
    conn = _materialize_schema(tmp_path / 'registry.db')
    try:
        for table in sorted(_ALLOWED_TABLES):
            columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
            assert columns, f"{table} vanished from the schema"
            for column in columns:
                tokens = _identifier_tokens(column)
                for fragment in _FORBIDDEN_NAME_FRAGMENTS:
                    assert not _contains_fragment(tokens, fragment), (
                        f"{table}.{column} names {fragment!r}; the fleet registry "
                        "stores no such fact (ADR 0062 Decision 1)"
                    )
    finally:
        conn.close()


def test_multi_token_fragments_are_caught_against_a_synthetic_column() -> None:
    """``api_key`` and ``endpoint_url`` are two-token fragments; neither is
    ever, itself, a single token, so the sweep above is only meaningful if
    the matcher catches a column shaped like the real forbidden fact."""
    assert _contains_fragment(_identifier_tokens("provider_api_key"), "api_key")
    assert _contains_fragment(_identifier_tokens("clerk_endpoint_url"), "endpoint_url")
    assert not _contains_fragment(_identifier_tokens("reported_state"), "api_key")


def test_no_timestamp_column_is_textual(tmp_path: pathlib.Path) -> None:
    """Every timestamp column is INTEGER ms, the sweep touches at least one
    such column per table, and no time-bearing column escapes the ``_ms``
    suffix convention under another name."""
    conn = _materialize_schema(tmp_path / 'registry.db')
    try:
        for table in sorted(_ALLOWED_TABLES):
            info = conn.execute(f"PRAGMA table_info({table})").fetchall()
            ms_columns = 0
            for row in info:
                name, declared_type = row[1], row[2]
                if name.endswith("_ms"):
                    ms_columns += 1
                    assert declared_type.upper() == "INTEGER", (
                        f"{table}.{name} is {declared_type}; canonical timestamps "
                        "are INTEGER ms UTC (ADR 0022)"
                    )
                else:
                    tokens = _identifier_tokens(name)
                    assert "at" not in tokens and "time" not in tokens, (
                        f"{table}.{name} names a time-bearing fragment without "
                        "the canonical `_ms` suffix (ADR 0022)"
                    )
            assert ms_columns >= 1, f"{table} was swept for zero timestamp columns"
    finally:
        conn.close()
