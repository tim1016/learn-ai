"""Creation and upgrade of one source-bar evidence store, done once at open.

``source_bar_ledger.SourceBarLedger`` is this module's only consumer. The two
halves separate on the line the ledger's ``read_only`` handle discovered:
creating and upgrading the store is the *owner's* lifecycle, while retaining
and querying evidence is what both handles do. Keeping the owner-only half
here leaves one ``if not read_only:`` guard at the call site instead of three
scattered through a constructor.

Nothing here decides anything about a bar. It creates tables, adds the
columns a pre-#1921 file lacks, back-fills that file's evidence journal, and
imports the one legacy JSONL WAL — then the ledger owns the store.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.services.source_bar_ledger import RetainedSourceBar

LEGACY_SOURCE_BAR_LEDGER_FILENAME = "source_bars.jsonl"
"""The pre-SQLite evidence WAL, imported once and never deleted."""

_UNJOURNALED_BARS = """
    SELECT b.seq, b.fetched_at_ms
    FROM source_bars b
    LEFT JOIN source_evidence_journal j ON j.kind = 'bar' AND j.bar_seq = b.seq
    WHERE j.evidence_seq IS NULL
    ORDER BY b.seq ASC
"""

_EVIDENCE_BAR_COLUMNS = {
    "provenance": "provenance TEXT NOT NULL DEFAULT 'realtime'",
    "authorization_id": "authorization_id TEXT",
    "continuity_event_ref": "continuity_event_ref TEXT",
}
"""Provenance columns added to ``source_bars`` by #1921, as ``ALTER TABLE`` fragments."""


def initialize_store(conn: sqlite3.Connection, *, account_id: str, legacy_path: Path) -> None:
    """Create the schema, migrate an older one, and import a legacy WAL — once, at open.

    Called only by an owning (writable) ledger handle, and only after that
    handle has refused a file retaining another account's evidence: a file
    that is not ours is never rewritten.

    No lock is taken. The caller runs this inside its own constructor, before
    any other thread can hold a reference to the ledger; the cross-*process*
    serialization that matters is SQLite's own ``BEGIN IMMEDIATE`` below,
    which is what makes a concurrent second handle's migration safe.
    """
    _create_schema(conn)
    _migrate_evidence_schema_if_needed(conn)
    _migrate_legacy_jsonl_if_needed(conn, account_id=account_id, legacy_path=legacy_path)


def journal_row(
    conn: sqlite3.Connection,
    *,
    run_id: str | None,
    kind: Literal["bar", "event"],
    row_seq: int,
    observed_at_ms: int,
) -> int:
    """Append one evidence position and return it; the caller owns the transaction.

    Bars and events share the journal precisely so their order is one
    fact, so they share the insert too -- only which foreign key is
    populated differs, and the table's CHECK enforces that pairing.
    """
    column = "bar_seq" if kind == "bar" else "event_seq"
    cursor = conn.execute(
        f"INSERT INTO source_evidence_journal (run_id, kind, {column}, observed_at_ms) VALUES (?, ?, ?, ?)",
        (run_id, kind, row_seq, observed_at_ms),
    )
    return int(cursor.lastrowid)


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS source_bars (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            symbol TEXT NOT NULL,
            bar_identity TEXT NOT NULL UNIQUE,
            bar_ref TEXT NOT NULL UNIQUE,
            start_ms INTEGER NOT NULL,
            end_ms INTEGER NOT NULL,
            open TEXT NOT NULL,
            high TEXT NOT NULL,
            low TEXT NOT NULL,
            close TEXT NOT NULL,
            volume INTEGER NOT NULL,
            fetched_at_ms INTEGER NOT NULL,
            session_phase TEXT NOT NULL,
            provenance TEXT NOT NULL DEFAULT 'realtime',
            authorization_id TEXT,
            continuity_event_ref TEXT,
            UNIQUE(provider, symbol, start_ms, end_ms),
            CHECK(end_ms > start_ms),
            CHECK(volume >= 0),
            CHECK(fetched_at_ms >= 0)
        );
        CREATE INDEX IF NOT EXISTS source_bars_stream_clock
            ON source_bars(provider, symbol, end_ms, seq);
        CREATE INDEX IF NOT EXISTS source_bars_symbol_seq
            ON source_bars(symbol, seq DESC);
        CREATE TABLE IF NOT EXISTS source_bar_stream_state (
            provider TEXT NOT NULL,
            symbol TEXT NOT NULL,
            live_started INTEGER NOT NULL CHECK(live_started IN (0, 1)),
            PRIMARY KEY(provider, symbol)
        );
        CREATE TABLE IF NOT EXISTS source_stream_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('interruption','recovered','gap','substituted','refused')),
            feed_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            observed_at_ms INTEGER NOT NULL CHECK(observed_at_ms >= 0),
            cause TEXT,
            generation_from INTEGER,
            generation_to INTEGER,
            window_start_ms INTEGER,
            window_end_ms INTEGER,
            bar_identity TEXT,
            authorization_id TEXT,
            reason TEXT,
            last_delivered_end_ms INTEGER,
            deadline_ms INTEGER,
            contribution_count INTEGER
        );
        CREATE TABLE IF NOT EXISTS source_run_decision_session (
            run_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK(kind IN ('rth','extended')),
            window_open_minute_et INTEGER,
            window_close_minute_et INTEGER,
            recorded_at_ms INTEGER NOT NULL CHECK(recorded_at_ms >= 0),
            CHECK((kind = 'extended') = (window_open_minute_et IS NOT NULL)),
            CHECK((kind = 'extended') = (window_close_minute_et IS NOT NULL))
        );
        CREATE TABLE IF NOT EXISTS source_evidence_journal (
            evidence_seq INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT,
            kind TEXT NOT NULL CHECK(kind IN ('bar','event')),
            bar_seq INTEGER REFERENCES source_bars(seq),
            event_seq INTEGER REFERENCES source_stream_events(seq),
            observed_at_ms INTEGER NOT NULL CHECK(observed_at_ms >= 0),
            CHECK((kind = 'bar' AND bar_seq IS NOT NULL AND event_seq IS NULL)
                  OR (kind = 'event' AND event_seq IS NOT NULL AND bar_seq IS NULL))
        );
        CREATE INDEX IF NOT EXISTS source_evidence_journal_run
            ON source_evidence_journal(run_id, evidence_seq);
        CREATE UNIQUE INDEX IF NOT EXISTS source_evidence_journal_bar
            ON source_evidence_journal(kind, bar_seq);
        CREATE UNIQUE INDEX IF NOT EXISTS source_evidence_journal_event
            ON source_evidence_journal(kind, event_seq);
        """
    )


def _migrate_evidence_schema_if_needed(conn: sqlite3.Connection) -> None:
    """Give a pre-#1921 ledger the continuity channel without losing evidence.

    Ledgers were already retaining bars in production when the provenance
    columns and the evidence journal arrived, so an existing file is
    migrated in place: the columns take their documented defaults, and
    every bar that predates the journal is given a journal position in
    ``seq`` order, so a run replaying an old file still reads one causal
    order over the whole of it. Both halves commit together.

    Runs after the foreign-account refusal so a file that is not ours is
    never rewritten, and takes no write lock at all once migrated.
    """
    migrated = _bar_columns(conn).issuperset(_EVIDENCE_BAR_COLUMNS)
    if migrated and not _has_unjournaled_bars(conn):
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Re-read under the write lock: another handle on the same file
        # may have migrated it while this one waited, and a repeated
        # ADD COLUMN is an error, not a no-op.
        columns = _bar_columns(conn)
        for name, ddl in _EVIDENCE_BAR_COLUMNS.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE source_bars ADD COLUMN {ddl}")
        for row in conn.execute(_UNJOURNALED_BARS).fetchall():
            journal_row(
                conn,
                run_id=None,
                kind="bar",
                row_seq=int(row["seq"]),
                observed_at_ms=int(row["fetched_at_ms"]),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _bar_columns(conn: sqlite3.Connection) -> set[str]:
    """The column names ``source_bars`` currently has on disk."""
    return {str(row["name"]) for row in conn.execute("PRAGMA table_info(source_bars)")}


def _has_unjournaled_bars(conn: sqlite3.Connection) -> bool:
    """Whether any retained bar still lacks its journal position.

    Counted, not searched. ``bar_seq``'s foreign key and the uniqueness of
    ``source_evidence_journal_bar`` make the journal's bar rows inject into
    ``source_bars``, so equal counts *is* "every bar has a journal row" --
    and both counts are index-only walks. The equivalent anti-join probe
    would scan every retained bar on a fully migrated file, at every open,
    forever; this open-time check must stay cheap on a ledger holding
    ``SOURCE_BAR_STREAM_CAPACITY`` bars.
    """
    bars = conn.execute("SELECT COUNT(*) AS count FROM source_bars").fetchone()
    journaled = conn.execute(
        "SELECT COUNT(*) AS count FROM source_evidence_journal WHERE kind = 'bar'"
    ).fetchone()
    return int(bars["count"]) != int(journaled["count"])


def _migrate_legacy_jsonl_if_needed(
    conn: sqlite3.Connection, *, account_id: str, legacy_path: Path
) -> None:
    """Import an old evidence WAL once without deleting the recoverable source."""
    # Local import: the ledger owns the record model and the conflict error and
    # imports this module at module level, so naming it here at module level
    # would close the cycle. By the time this runs the ledger is fully imported.
    from app.services.source_bar_ledger import SourceBarConflictError

    existing = conn.execute("SELECT 1 FROM source_bars LIMIT 1").fetchone()
    if existing is not None or not legacy_path.exists():
        return
    rows = list(_read_legacy_rows(legacy_path))
    conn.execute("BEGIN IMMEDIATE")
    try:
        for row in rows:
            if row.account_id != account_id:
                raise SourceBarConflictError("SOURCE_BAR_LEGACY_ACCOUNT_MISMATCH")
            conn.execute(
                """
                INSERT INTO source_bars (
                    seq, account_id, provider, symbol, bar_identity, bar_ref,
                    start_ms, end_ms, open, high, low, close, volume,
                    fetched_at_ms, session_phase
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.seq,
                    row.account_id,
                    row.provider,
                    row.symbol,
                    row.bar_identity,
                    row.bar_ref,
                    row.start_ms,
                    row.end_ms,
                    str(row.open),
                    str(row.high),
                    str(row.low),
                    str(row.close),
                    row.volume,
                    row.fetched_at_ms,
                    row.session_phase,
                ),
            )
            journal_row(
                conn,
                run_id=None,
                kind="bar",
                row_seq=row.seq,
                observed_at_ms=row.fetched_at_ms,
            )
            conn.execute(
                """
                INSERT INTO source_bar_stream_state (provider, symbol, live_started)
                VALUES (?, ?, 1)
                ON CONFLICT(provider, symbol) DO UPDATE SET live_started = 1
                """,
                (row.provider, row.symbol),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _read_legacy_rows(path: Path) -> Iterable[RetainedSourceBar]:
    """Decode the former JSONL ledger solely for one-time non-destructive import."""
    from app.services.source_bar_ledger import RetainedSourceBar, SourceBarConflictError

    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                yield RetainedSourceBar.model_validate(json.loads(stripped))
            except (json.JSONDecodeError, ValueError) as error:
                raise SourceBarConflictError(
                    f"SOURCE_BAR_LEGACY_CORRUPT: {path} line {number}"
                ) from error


__all__ = [
    "LEGACY_SOURCE_BAR_LEDGER_FILENAME",
    "initialize_store",
    "journal_row",
]
