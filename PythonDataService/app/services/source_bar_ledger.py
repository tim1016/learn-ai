"""Durable, indexed source observations for one sealed account authority.

The source-bar ledger is decision evidence, not a chart cache. It stores the
unfiltered feed observation before a program can apply its own session policy.
SQLite owns identity uniqueness, monotonic live delivery, and WAL bounding so
the durable path stays indexed as an authority runs for months.

A bar is not the only fact a run's evidence needs. A broker-socket reconnect
(#1921) produces continuity facts -- an interruption, a recovery, a gap, a
substitution, a refusal -- that only mean something *relative to the bars
around them*. Both land in one append-only ``source_evidence_journal`` whose
``evidence_seq`` is the single causal order a later replay reads, so "this bar
arrived after that interruption" is a stored fact rather than a reconstruction
from wall-clock timestamps.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.broker.alpaca.paths import resolve_contained_path, safe_path_component
from app.marketdata.feed import ContinuityEventRef, FeedContinuityEvent, MarketDataBar

SOURCE_BAR_LEDGER_FILENAME = "source_bars.sqlite3"
"""Indexed durable authority store for retained source observations."""

_LEGACY_SOURCE_BAR_LEDGER_FILENAME = "source_bars.jsonl"
SOURCE_BAR_STREAM_CAPACITY = 200_000
"""Retained one-minute bars per provider/symbol stream before ``append`` fails closed.

There is no pruning: reaching this capacity raises ``SourceBarRetentionLimitError``
and requires a reviewed rollover (#1740). The registry-parametrized floor test
(``tests/services/test_signal_program_retention_floor.py``) pins that this exceeds
every sealed program's declared warmup plus one open decision cycle.
"""


class RetainedSourceBar(BaseModel):
    """One exact, closed feed observation with stable authority-scoped identity.

    ``provenance`` and its two companions say how the observation was produced
    (#1921); ``run_id`` and ``evidence_seq`` say where it sits in the evidence
    journal's causal order. Every one of them defaults, so a bar retained
    before the continuity channel existed -- or one built by a caller that has
    no run -- is still a complete, valid observation.

    ``provenance`` is typed ``str`` rather than ``BarProvenanceTag`` on
    purpose: this is a decode of a durable row, and a store must be able to
    read back a value a newer writer wrote instead of failing to open the
    file. The write path takes its value from a tagged ``MarketDataBar``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(ge=1)
    account_id: str
    provider: str
    symbol: str
    bar_identity: str
    bar_ref: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int = Field(ge=0)
    fetched_at_ms: int = Field(ge=0)
    session_phase: str
    provenance: str = "realtime"
    authorization_id: str | None = None
    continuity_event_ref: str | None = None
    run_id: str | None = None
    evidence_seq: int | None = Field(default=None, ge=1)

    @classmethod
    def from_market_bar(
        cls,
        *,
        seq: int,
        account_id: str,
        bar: MarketDataBar,
    ) -> RetainedSourceBar:
        provider = bar.feed_id
        identity = f"{provider}:{bar.symbol}:{bar.start_ms}:{bar.end_ms}"
        return cls(
            seq=seq,
            account_id=account_id,
            provider=provider,
            symbol=bar.symbol,
            bar_identity=identity,
            bar_ref=f"source-bar:{account_id}:{identity}",
            start_ms=bar.start_ms,
            end_ms=bar.end_ms,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            fetched_at_ms=bar.fetched_at_ms,
            session_phase=bar.session_phase,
            provenance=bar.provenance,
            authorization_id=bar.authorization_id,
            continuity_event_ref=bar.continuity_event_ref,
        )


class RetainedContinuityEvent(FeedContinuityEvent):
    """One durable continuity fact, with its row identity and journal position.

    Subclassing is the point: every field the feed boundary records is stored
    and read back unchanged, and a field added to ``FeedContinuityEvent``
    cannot silently stop being persisted -- ``extra="forbid"`` plus the
    column-per-field decode in ``events`` turns that into a loud failure.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(ge=1)
    run_id: str
    evidence_seq: int = Field(ge=1)


class SourceBarConflictError(RuntimeError):
    """A provider reused evidence identity or violated delivery monotonicity."""


class SourceBarRetentionLimitError(RuntimeError):
    """A stream reached its bounded evidence capacity and must roll over safely."""


class SourceBarLedgerCorruptError(RuntimeError):
    """A ledger file failed SQLite integrity, is not a source-bar ledger, or holds another account's evidence."""


class SourceBarCheckpointBusyError(RuntimeError):
    """A truncating WAL checkpoint could not complete because a reader still held the log."""


_BUSY_TIMEOUT_MS = 5_000
"""How long a write or truncating checkpoint waits on another connection before failing."""

_BARS_WITH_JOURNAL = """
    SELECT b.*, j.run_id AS run_id, j.evidence_seq AS evidence_seq
    FROM source_bars b
    LEFT JOIN source_evidence_journal j ON j.kind = 'bar' AND j.bar_seq = b.seq
"""
"""Every bar read carries its journal position; the join is LEFT because a bar
retained before the journal existed and not yet back-filled still has none."""

_EVENTS_WITH_JOURNAL = """
    SELECT e.*, j.evidence_seq AS evidence_seq
    FROM source_stream_events e
    JOIN source_evidence_journal j ON j.kind = 'event' AND j.event_seq = e.seq
"""

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


def verify_ledger_file(path: Path, *, account_id: str) -> int:
    """Integrity-check one ledger file read-only and return its retained-bar count.

    Every retained row must belong to ``account_id``: a structurally valid
    ledger cut for another account would otherwise restore cleanly and then
    mix that account's ``bar_ref`` evidence with this one's new appends. An
    empty ledger carries no evidence and so cannot be foreign.

    The schema knowledge (which table holds the bars) stays here, next to the
    ``CREATE TABLE`` that defines it; the Clerk recovery tooling calls this
    when it cuts and verifies a backup bundle (#1740) rather than querying the
    ledger's tables itself.
    """
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise SourceBarLedgerCorruptError(f"{path.name} failed integrity_check")
        row = conn.execute("SELECT COUNT(*) FROM source_bars").fetchone()
        foreign = _foreign_account_row(conn, account_id)
    except sqlite3.DatabaseError as exc:
        raise SourceBarLedgerCorruptError(f"{path.name} is not a readable source-bar ledger: {exc}") from exc
    finally:
        conn.close()
    if foreign is not None:
        raise SourceBarLedgerCorruptError(
            f"{path.name} retains evidence for account {foreign!r}, not {account_id!r}"
        )
    return int(row[0])


def _foreign_account_row(conn: sqlite3.Connection, account_id: str) -> str | None:
    """The first retained ``account_id`` that is not the expected one, if any."""
    row = conn.execute(
        "SELECT account_id FROM source_bars WHERE account_id != ? LIMIT 1", (account_id,)
    ).fetchone()
    return None if row is None else str(row[0])


class SourceBarLedger:
    """SQLite-backed, authority-scoped source evidence with durable uniqueness.

    ``append`` is for real-time delivery and rejects out-of-order identities.
    ``append_history`` is an explicit cold-start import path; it accepts only
    ascending historical observations and is never used after retained replay
    exists. Neither path silently deletes evidence: reaching the fixed per
    stream capacity fails closed so retention requires a reviewed rollover.
    """

    def __init__(self, *, artifacts_root: Path, account_id: str) -> None:
        safe_account_id = safe_path_component(account_id, "account id")
        account_dir = resolve_contained_path(
            Path(artifacts_root), "accounts", "alpaca", safe_account_id
        )
        account_dir.mkdir(parents=True, exist_ok=True)
        self.account_id = account_id
        self._path = account_dir / SOURCE_BAR_LEDGER_FILENAME
        self._legacy_path = account_dir / _LEGACY_SOURCE_BAR_LEDGER_FILENAME
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._configure()
        self._create_schema()
        self._refuse_foreign_account_rows()
        self._migrate_evidence_schema_if_needed()
        self._migrate_legacy_jsonl_if_needed()

    @property
    def path(self) -> Path:
        return self._path

    def close(self, *, checkpoint: bool = True) -> None:
        """Close this handle after its caller has stopped using the authority.

        Checkpoints and truncates the WAL first, explicitly: SQLite only does
        so on its own when this is the last open connection, and a verifier or
        backup reader may still be attached at shutdown (#1740). If that
        reader still holds the log after the busy timeout the checkpoint
        raises ``SourceBarCheckpointBusyError`` and this handle stays open, so
        the controlled close fails loudly instead of leaving a WAL behind; the
        caller retries once the reader is gone.

        Read-only evidence consumers (run replay proof) pass checkpoint=False:
        they never wrote, so folding the WAL is the writer's job, and a busy
        checkpoint must not fail a read.
        """
        with self._lock:
            if checkpoint:
                self.checkpoint_wal()
            self._conn.close()

    def append(self, bar: MarketDataBar, *, run_id: str | None = None) -> RetainedSourceBar:
        """Persist one live observation, refusing a non-monotonic new identity.

        ``run_id`` names the run this observation is evidence for, so the
        journal can order it against that run's continuity events. It is
        optional because a bar is evidence whether or not a run claimed it.
        """
        return self._append(bar, delivery="live", run_id=run_id)

    def append_history(self, bar: MarketDataBar, *, run_id: str | None = None) -> RetainedSourceBar:
        """Persist one ordered warmup observation before live delivery begins."""
        return self._append(bar, delivery="history", run_id=run_id)

    def append_event(self, event: FeedContinuityEvent, *, run_id: str) -> ContinuityEventRef:
        """Persist one continuity fact and its journal position in one transaction.

        A continuity fact that outlived its journal row (or the reverse) would
        be evidence with no place in the run's causal order, so both rows
        commit together or neither does.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._conn.execute(
                    """
                    INSERT INTO source_stream_events (
                        run_id, kind, feed_id, symbol, observed_at_ms, cause, generation_from, generation_to,
                        window_start_ms, window_end_ms, bar_identity, authorization_id, reason,
                        last_delivered_end_ms, deadline_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        event.kind,
                        event.feed_id,
                        event.symbol,
                        event.observed_at_ms,
                        event.cause,
                        event.generation_from,
                        event.generation_to,
                        event.window_start_ms,
                        event.window_end_ms,
                        event.bar_identity,
                        event.authorization_id,
                        event.reason,
                        event.last_delivered_end_ms,
                        event.deadline_ms,
                    ),
                )
                evidence_seq = self._journal(
                    run_id=run_id,
                    kind="event",
                    row_seq=int(cursor.lastrowid),
                    observed_at_ms=event.observed_at_ms,
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return ContinuityEventRef(run_id=run_id, evidence_seq=evidence_seq)

    def events(
        self,
        *,
        run_id: str,
        evidence_end_seq: int | None = None,
    ) -> list[RetainedContinuityEvent]:
        """Return one run's continuity facts in journal order, up to a bound.

        ``evidence_end_seq`` bounds the read at a journal position so a
        receipt reports exactly the facts that preceded the evidence it
        covers, rather than everything the ledger has learned since.
        """
        sql = f"{_EVENTS_WITH_JOURNAL} WHERE e.run_id = ?"
        params: list[object] = [run_id]
        if evidence_end_seq is not None:
            sql += " AND j.evidence_seq <= ?"
            params.append(evidence_end_seq)
        sql += " ORDER BY j.evidence_seq ASC"
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        # The selected columns are one-for-one with the model's fields;
        # ``extra="forbid"`` makes a schema drift a loud decode failure.
        return [RetainedContinuityEvent.model_validate(dict(row)) for row in rows]

    def evidence_end_seq(self) -> int | None:
        """Return the newest journal position in this ledger, or None when it holds none."""
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(evidence_seq) AS end_seq FROM source_evidence_journal"
            ).fetchone()
        return None if row is None or row["end_seq"] is None else int(row["end_seq"])

    def by_identity(self, bar_identity: str) -> RetainedSourceBar | None:
        """Return the exact durable observation for one stable source-bar identity."""
        with self._lock:
            row = self._conn.execute(
                f"{_BARS_WITH_JOURNAL} WHERE b.bar_identity = ?", (bar_identity,)
            ).fetchone()
        return None if row is None else _retained_row(row)

    def find_by_market_bar(self, bar: MarketDataBar) -> RetainedSourceBar | None:
        """Find the exact retained observation corresponding to one evaluated bar."""
        return self.by_identity(f"{bar.feed_id}:{bar.symbol}:{bar.start_ms}:{bar.end_ms}")

    def find_by_closed_end(
        self,
        *,
        provider: str,
        symbol: str,
        end_ms: int,
    ) -> RetainedSourceBar | None:
        """Return exactly one retained source observation for a decision close."""
        with self._lock:
            rows = self._conn.execute(
                f"""
                {_BARS_WITH_JOURNAL}
                WHERE b.provider = ? AND b.symbol = ? AND b.end_ms = ?
                ORDER BY b.seq ASC LIMIT 2
                """,
                (provider, symbol, end_ms),
            ).fetchall()
        if len(rows) > 1:
            raise SourceBarConflictError(
                "SOURCE_BAR_DECISION_AMBIGUITY: "
                f"{provider}:{symbol}:{end_ms} has {len(rows)} retained observations"
            )
        return None if not rows else _retained_row(rows[0])

    def bars(self, *, provider: str, symbol: str) -> list[RetainedSourceBar]:
        """Return one provider/symbol's retained observations in durable order."""
        with self._lock:
            rows = self._conn.execute(
                f"{_BARS_WITH_JOURNAL} WHERE b.provider = ? AND b.symbol = ? ORDER BY b.seq ASC",
                (provider, symbol),
            ).fetchall()
        return [_retained_row(row) for row in rows]

    def latest(self, *, provider: str, symbol: str) -> RetainedSourceBar | None:
        """Return the latest retained observation for one evidence stream."""
        with self._lock:
            row = self._conn.execute(
                f"""
                {_BARS_WITH_JOURNAL}
                WHERE b.provider = ? AND b.symbol = ?
                ORDER BY b.end_ms DESC, b.seq DESC LIMIT 1
                """,
                (provider, symbol),
            ).fetchone()
        return None if row is None else _retained_row(row)

    def latest_for_symbol(self, symbol: str) -> RetainedSourceBar | None:
        """Return the newest durable bar for a symbol across sealed providers."""
        with self._lock:
            row = self._conn.execute(
                f"{_BARS_WITH_JOURNAL} WHERE b.symbol = ? ORDER BY b.seq DESC LIMIT 1", (symbol,)
            ).fetchone()
        return None if row is None else _retained_row(row)

    def providers_for(self, symbol: str) -> list[str]:
        """Return the distinct providers with retained evidence for one symbol."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT provider FROM source_bars WHERE symbol = ? ORDER BY provider",
                (symbol,),
            ).fetchall()
        return [str(row["provider"]) for row in rows]

    def checkpoint_wal(self) -> None:
        """Checkpoint and truncate durable evidence after controlled shutdown or backup.

        ``wal_checkpoint(TRUNCATE)`` reports a blocked checkpoint as a
        ``busy`` column, never as an exception; a caller that ignored it
        would believe the WAL was folded when a concurrent reader kept it.
        """
        with self._lock:
            row = self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        assert row is not None
        if int(row["busy"]) != 0:
            raise SourceBarCheckpointBusyError(
                "SOURCE_BAR_CHECKPOINT_BUSY: "
                f"{self._path.name} still has a reader on its WAL after {_BUSY_TIMEOUT_MS}ms"
            )

    def _append(
        self,
        bar: MarketDataBar,
        *,
        delivery: Literal["history", "live"],
        run_id: str | None,
    ) -> RetainedSourceBar:
        candidate = RetainedSourceBar.from_market_bar(seq=1, account_id=self.account_id, bar=bar)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                retained = self.by_identity(candidate.bar_identity)
                if retained is not None:
                    if _same_market_payload(retained, candidate):
                        self._conn.execute("COMMIT")
                        return retained
                    raise SourceBarConflictError(
                        "SOURCE_BAR_IDENTITY_CONFLICT: "
                        f"{candidate.bar_identity!r} was observed with a different payload"
                    )

                latest = self._conn.execute(
                    """
                    SELECT end_ms FROM source_bars
                    WHERE provider = ? AND symbol = ?
                    ORDER BY end_ms DESC LIMIT 1
                    """,
                    (candidate.provider, candidate.symbol),
                ).fetchone()
                stream_state = self._conn.execute(
                    """
                    SELECT live_started FROM source_bar_stream_state
                    WHERE provider = ? AND symbol = ?
                    """,
                    (candidate.provider, candidate.symbol),
                ).fetchone()
                if delivery == "history" and stream_state is not None and bool(stream_state["live_started"]):
                    raise SourceBarConflictError(
                        "SOURCE_BAR_HISTORY_AFTER_LIVE: retained replay must be used after live delivery begins"
                    )
                if latest is not None and candidate.end_ms <= int(latest["end_ms"]):
                    raise SourceBarConflictError(
                        f"SOURCE_BAR_NON_MONOTONIC_{delivery.upper()}: "
                        f"{candidate.bar_identity!r} is not after the retained decision clock"
                    )

                count = self._conn.execute(
                    "SELECT COUNT(*) AS count FROM source_bars WHERE provider = ? AND symbol = ?",
                    (candidate.provider, candidate.symbol),
                ).fetchone()
                assert count is not None
                if int(count["count"]) >= SOURCE_BAR_STREAM_CAPACITY:
                    raise SourceBarRetentionLimitError(
                        "SOURCE_BAR_RETENTION_LIMIT: "
                        f"{candidate.provider}:{candidate.symbol} reached {SOURCE_BAR_STREAM_CAPACITY} retained bars"
                    )

                cursor = self._conn.execute(
                    """
                    INSERT INTO source_bars (
                        account_id, provider, symbol, bar_identity, bar_ref,
                        start_ms, end_ms, open, high, low, close, volume,
                        fetched_at_ms, session_phase,
                        provenance, authorization_id, continuity_event_ref
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate.account_id,
                        candidate.provider,
                        candidate.symbol,
                        candidate.bar_identity,
                        candidate.bar_ref,
                        candidate.start_ms,
                        candidate.end_ms,
                        str(candidate.open),
                        str(candidate.high),
                        str(candidate.low),
                        str(candidate.close),
                        candidate.volume,
                        candidate.fetched_at_ms,
                        candidate.session_phase,
                        candidate.provenance,
                        candidate.authorization_id,
                        candidate.continuity_event_ref,
                    ),
                )
                seq = int(cursor.lastrowid)
                evidence_seq = self._journal(
                    run_id=run_id,
                    kind="bar",
                    row_seq=seq,
                    observed_at_ms=candidate.fetched_at_ms,
                )
                if delivery == "live":
                    self._conn.execute(
                        """
                        INSERT INTO source_bar_stream_state (provider, symbol, live_started)
                        VALUES (?, ?, 1)
                        ON CONFLICT(provider, symbol) DO UPDATE SET live_started = 1
                        """,
                        (candidate.provider, candidate.symbol),
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return candidate.model_copy(
            update={"seq": seq, "run_id": run_id, "evidence_seq": evidence_seq}
        )

    def _journal(
        self,
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
        cursor = self._conn.execute(
            f"INSERT INTO source_evidence_journal (run_id, kind, {column}, observed_at_ms) VALUES (?, ?, ?, ?)",
            (run_id, kind, row_seq, observed_at_ms),
        )
        return int(cursor.lastrowid)

    def _configure(self) -> None:
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        # SQLite checkpoints automatically at this bound; explicit shutdown
        # checkpoints use ``checkpoint_wal`` for a compact backup artifact.
        self._conn.execute("PRAGMA wal_autocheckpoint=1000")

    def _create_schema(self) -> None:
        self._conn.executescript(
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
                deadline_ms INTEGER
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

    def _refuse_foreign_account_rows(self) -> None:
        """Fail closed when an existing file retains another account's evidence.

        The file path is account-scoped, but a restore or an operator copy can
        put any ledger there; the same rule ``verify_ledger_file`` applies to
        a backup snapshot applies to the live file on open.
        """
        foreign = _foreign_account_row(self._conn, self.account_id)
        if foreign is not None:
            self._conn.close()
            raise SourceBarLedgerCorruptError(
                "SOURCE_BAR_ACCOUNT_MISMATCH: "
                f"{self._path.name} retains evidence for account {foreign!r}, not {self.account_id!r}"
            )

    def _migrate_evidence_schema_if_needed(self) -> None:
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
        with self._lock:
            migrated = self._bar_columns().issuperset(_EVIDENCE_BAR_COLUMNS)
            if migrated and not self._has_unjournaled_bars():
                return
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                # Both halves are re-read under the write lock: another handle
                # on the same file may have migrated it while this one waited,
                # and a repeated ADD COLUMN is an error, not a no-op.
                columns = self._bar_columns()
                for name, ddl in _EVIDENCE_BAR_COLUMNS.items():
                    if name not in columns:
                        self._conn.execute(f"ALTER TABLE source_bars ADD COLUMN {ddl}")
                for row in self._conn.execute(_UNJOURNALED_BARS).fetchall():
                    self._journal(
                        run_id=None,
                        kind="bar",
                        row_seq=int(row["seq"]),
                        observed_at_ms=int(row["fetched_at_ms"]),
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def _bar_columns(self) -> set[str]:
        """The column names ``source_bars`` currently has on disk."""
        return {str(row["name"]) for row in self._conn.execute("PRAGMA table_info(source_bars)")}

    def _has_unjournaled_bars(self) -> bool:
        """Whether any retained bar still lacks its journal position.

        Counted, not searched. ``bar_seq``'s foreign key and the uniqueness of
        ``source_evidence_journal_bar`` make the journal's bar rows inject into
        ``source_bars``, so equal counts *is* "every bar has a journal row" --
        and both counts are index-only walks. The equivalent anti-join probe
        would scan every retained bar on a fully migrated file, at every open,
        forever; this open-time check must stay cheap on a ledger holding
        ``SOURCE_BAR_STREAM_CAPACITY`` bars.
        """
        bars = self._conn.execute("SELECT COUNT(*) AS count FROM source_bars").fetchone()
        journaled = self._conn.execute(
            "SELECT COUNT(*) AS count FROM source_evidence_journal WHERE kind = 'bar'"
        ).fetchone()
        return int(bars["count"]) != int(journaled["count"])

    def _migrate_legacy_jsonl_if_needed(self) -> None:
        """Import an old evidence WAL once without deleting the recoverable source."""
        with self._lock:
            existing = self._conn.execute("SELECT 1 FROM source_bars LIMIT 1").fetchone()
            if existing is not None or not self._legacy_path.exists():
                return
            rows = list(_read_legacy_rows(self._legacy_path))
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for row in rows:
                    if row.account_id != self.account_id:
                        raise SourceBarConflictError("SOURCE_BAR_LEGACY_ACCOUNT_MISMATCH")
                    self._conn.execute(
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
                    self._journal(
                        run_id=None,
                        kind="bar",
                        row_seq=row.seq,
                        observed_at_ms=row.fetched_at_ms,
                    )
                    self._conn.execute(
                        """
                        INSERT INTO source_bar_stream_state (provider, symbol, live_started)
                        VALUES (?, ?, 1)
                        ON CONFLICT(provider, symbol) DO UPDATE SET live_started = 1
                        """,
                        (row.provider, row.symbol),
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise


def _retained_row(row: sqlite3.Row) -> RetainedSourceBar:
    """Decode one journal-joined ``source_bars`` row (see ``_BARS_WITH_JOURNAL``)."""
    return RetainedSourceBar(
        seq=int(row["seq"]),
        account_id=str(row["account_id"]),
        provider=str(row["provider"]),
        symbol=str(row["symbol"]),
        bar_identity=str(row["bar_identity"]),
        bar_ref=str(row["bar_ref"]),
        start_ms=int(row["start_ms"]),
        end_ms=int(row["end_ms"]),
        open=Decimal(str(row["open"])),
        high=Decimal(str(row["high"])),
        low=Decimal(str(row["low"])),
        close=Decimal(str(row["close"])),
        volume=int(row["volume"]),
        fetched_at_ms=int(row["fetched_at_ms"]),
        session_phase=str(row["session_phase"]),
        provenance=str(row["provenance"]),
        authorization_id=_optional_str(row["authorization_id"]),
        continuity_event_ref=_optional_str(row["continuity_event_ref"]),
        run_id=_optional_str(row["run_id"]),
        evidence_seq=None if row["evidence_seq"] is None else int(row["evidence_seq"]),
    )


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


_STORAGE_ONLY_FIELDS = {"seq", "fetched_at_ms", "run_id", "evidence_seq"}
"""Where a row landed, not what the feed observed: excluded from redelivery equality.

``provenance`` and its two companions are deliberately *not* here. The same
identity re-observed with a different provenance is a different fact about how
that bar came to exist, and overwriting it silently would falsify the run's
evidence -- so it is refused as an identity conflict like any other revision.
"""


def _same_market_payload(existing: RetainedSourceBar, candidate: RetainedSourceBar) -> bool:
    """Ignore only fetch time and storage position for exact feed redelivery."""
    return existing.model_dump(exclude=_STORAGE_ONLY_FIELDS) == candidate.model_dump(
        exclude=_STORAGE_ONLY_FIELDS
    )


def _read_legacy_rows(path: Path) -> Iterable[RetainedSourceBar]:
    """Decode the former JSONL ledger solely for one-time non-destructive import."""
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
    "SOURCE_BAR_LEDGER_FILENAME",
    "SOURCE_BAR_STREAM_CAPACITY",
    "RetainedContinuityEvent",
    "RetainedSourceBar",
    "SourceBarConflictError",
    "SourceBarLedger",
    "SourceBarLedgerCorruptError",
    "SourceBarRetentionLimitError",
    "verify_ledger_file",
]
