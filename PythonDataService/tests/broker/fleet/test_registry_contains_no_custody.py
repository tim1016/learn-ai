"""The registry is custody-free: the shipped DDL never grows lane data (FR-023).

Every table and every column of the fleet schema is asserted against a closed
allowlist. Custody, orders, fills, positions, activation, arming and envelope
facts live on clerk volumes under provider authority; a column with such a
name appearing here would mean the coordinator had started storing execution
data, which ADR 0062 Decision 1 forbids.
"""

from __future__ import annotations

import re
import sqlite3

from app.broker.fleet import schema

_ALLOWED_TABLES = {
    "fleet_meta",
    "clerks",
    "clerk_sessions",
    "clerk_session_history",
    "account_assignments",
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


def _identifier_tokens(name: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", name.lower()))


def _materialize_schema() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    schema.configure_connection(conn)
    schema.apply_schema(conn)
    return conn


def test_every_table_is_expected() -> None:
    conn = _materialize_schema()
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    finally:
        conn.close()
    assert tables == _ALLOWED_TABLES


def test_no_column_name_carries_a_custody_or_secret_fragment() -> None:
    conn = _materialize_schema()
    try:
        for table in sorted(_ALLOWED_TABLES):
            columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
            assert columns, f"{table} vanished from the schema"
            for column in columns:
                tokens = _identifier_tokens(column)
                for fragment in _FORBIDDEN_NAME_FRAGMENTS:
                    assert fragment not in tokens, (
                        f"{table}.{column} names {fragment!r}; the fleet registry "
                        "stores no such fact (ADR 0062 Decision 1)"
                    )
    finally:
        conn.close()


def test_no_timestamp_column_is_textual() -> None:
    conn = _materialize_schema()
    try:
        for table in sorted(_ALLOWED_TABLES):
            info = conn.execute(f"PRAGMA table_info({table})").fetchall()
            for row in info:
                name, declared_type = row[1], row[2]
                if name.endswith("_ms"):
                    assert declared_type.upper() == "INTEGER", (
                        f"{table}.{name} is {declared_type}; canonical timestamps "
                        "are INTEGER ms UTC (ADR 0022)"
                    )
    finally:
        conn.close()
