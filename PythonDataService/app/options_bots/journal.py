"""Atomic Options decision outbox and rule snapshots, never an account ledger.

The Clerk owns orders, money and custody. This local SQLite journal records the
rules' progress and guarantees a decision is durable before its first delivery.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.options_bots.session import (
    DECISION_ADAPTERS,
    CancelEntry,
    CloseLeg,
    OpenLeg,
    OptionsDecision,
    OptionsStep,
    SessionState,
)
from app.schemas.signal_program_seal import semantic_payload_hash

JOURNAL_FILENAME = "options_bot.sqlite3"


def attempt_identity(
    strategy_instance_id: str, session_open_ms: int, leg_id: str, entry_number: int, entry_at_ms: int
) -> str:
    return semantic_payload_hash(
        {
            "bot": strategy_instance_id,
            "session": session_open_ms,
            "leg": leg_id,
            "number": entry_number,
            "entry_ms": entry_at_ms,
        }
    )


@dataclass(frozen=True)
class PendingDecision:
    decision_id: str
    session_open_ms: int
    attempt_id: str
    decision: OptionsDecision
    status: Literal["pending", "submitted", "discarded"]
    dispatched: bool
    explanation: str | None


class OptionsBotJournal:
    """A bot's exact setup identity, append-only sessions and durable delivery state."""

    def __init__(
        self, instance_dir: Path, *, strategy_instance_id: str | None = None, setup_sha256: str | None = None
    ) -> None:
        instance_dir.mkdir(parents=True, exist_ok=True)
        path = instance_dir / JOURNAL_FILENAME
        if path.is_symlink():
            raise ValueError("an Options journal must be a regular file within its bot directory")
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS identity (singleton INTEGER PRIMARY KEY CHECK(singleton=1), bot TEXT NOT NULL, setup_sha256 TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions (open_ms INTEGER PRIMARY KEY, state_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, decision_id TEXT NOT NULL UNIQUE, session_open_ms INTEGER NOT NULL,
                attempt_id TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                dispatched INTEGER NOT NULL DEFAULT 0, explanation TEXT);
            CREATE TABLE IF NOT EXISTS status (singleton INTEGER PRIMARY KEY CHECK(singleton=1), payload TEXT NOT NULL);
        """)
        existing = self._conn.execute("SELECT bot,setup_sha256 FROM identity WHERE singleton=1").fetchone()
        if strategy_instance_id is not None or setup_sha256 is not None:
            if strategy_instance_id is None or setup_sha256 is None:
                self._conn.close()
                raise ValueError("an Options journal identity requires its bot and exact setup together")
            if existing is not None and tuple(existing) != (strategy_instance_id, setup_sha256):
                self._conn.close()
                raise ValueError("Options journal belongs to another bot or exact setup")
            with self._conn:
                self._conn.execute("INSERT OR IGNORE INTO identity VALUES(1,?,?)", (strategy_instance_id, setup_sha256))
        self.strategy_instance_id = existing["bot"] if existing is not None else strategy_instance_id
        self.setup_sha256 = existing["setup_sha256"] if existing is not None else setup_sha256

    def close(self) -> None:
        self._conn.close()

    @property
    def state(self) -> SessionState | None:
        row = self._conn.execute("SELECT state_json FROM sessions ORDER BY open_ms DESC LIMIT 1").fetchone()
        return None if row is None else SessionState.model_validate_json(row[0])

    @property
    def has_state(self) -> bool:
        return self._conn.execute("SELECT 1 FROM sessions LIMIT 1").fetchone() is not None

    def commit(self, state: SessionState, step: OptionsStep) -> None:
        if self.strategy_instance_id is None:
            raise ValueError("a writable Options journal needs a sealed bot identity")
        encoded = state.model_dump_json()
        existing = self._conn.execute(
            "SELECT state_json FROM sessions WHERE open_ms=?", (state.session_open_ms,)
        ).fetchone()
        if existing is not None and existing[0] == encoded and not step.decisions:
            return
        with self._conn:
            self._conn.execute(
                "INSERT INTO sessions VALUES(?,?) ON CONFLICT(open_ms) DO UPDATE SET state_json=excluded.state_json",
                (state.session_open_ms, encoded),
            )
            for decision in step.decisions:
                leg = next(leg for leg in state.legs if leg.setup.leg_id == decision.leg_id)
                entry_ms = (
                    decision.entry_decided_at_ms if isinstance(decision, CancelEntry) else leg.order_decided_at_ms
                )
                attempt_id = attempt_identity(
                    self.strategy_instance_id, state.session_open_ms, decision.leg_id, decision.entry_number, entry_ms
                )
                kind = (
                    "open" if isinstance(decision, OpenLeg) else "close" if isinstance(decision, CloseLeg) else "cancel"
                )
                payload = DECISION_ADAPTERS[kind].dump_json(decision).decode()
                identity = semantic_payload_hash({"attempt": attempt_id, "kind": kind, "payload": json.loads(payload)})
                previous = self._conn.execute("SELECT payload FROM outbox WHERE decision_id=?", (identity,)).fetchone()
                if previous is not None and previous[0] != payload:
                    raise ValueError("a durable Options decision was reused with different semantics")
                self._conn.execute(
                    "INSERT OR IGNORE INTO outbox(decision_id,session_open_ms,attempt_id,kind,payload) VALUES(?,?,?,?,?)",
                    (identity, state.session_open_ms, attempt_id, kind, payload),
                )

    def decisions(
        self, *, session_open_ms: int | None = None, pending_only: bool = False
    ) -> tuple[PendingDecision, ...]:
        conditions, arguments = [], []
        if session_open_ms is not None:
            conditions.append("session_open_ms=?")
            arguments.append(session_open_ms)
        if pending_only:
            conditions.append("status='pending'")
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        rows = self._conn.execute("SELECT * FROM outbox" + where + " ORDER BY seq", arguments).fetchall()
        for row in rows:
            expected = semantic_payload_hash(
                {
                    "attempt": row["attempt_id"],
                    "kind": row["kind"],
                    "payload": json.loads(row["payload"]),
                }
            )
            if row["decision_id"] != expected:
                raise ValueError("an Options outbox decision does not match its durable identity")
        return tuple(
            PendingDecision(
                row["decision_id"],
                row["session_open_ms"],
                row["attempt_id"],
                DECISION_ADAPTERS[row["kind"]].validate_json(row["payload"]),
                row["status"],
                bool(row["dispatched"]),
                row["explanation"],
            )
            for row in rows
        )

    def dispatching(self, decision_id: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE outbox SET dispatched=1 WHERE decision_id=? AND status='pending'", (decision_id,)
            )

    def finish(self, decision_id: str, *, discarded: bool = False, explanation: str | None = None) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE outbox SET status=?,explanation=? WHERE decision_id=?",
                ("discarded" if discarded else "submitted", explanation, decision_id),
            )

    def defer(self, decision_id: str, explanation: str) -> None:
        """Keep the unresolved decision and its current refusal visible after a restart."""
        with self._conn:
            self._conn.execute(
                "UPDATE outbox SET explanation=? WHERE decision_id=? AND status='pending'",
                (explanation, decision_id),
            )

    def note(self, **values: object) -> None:
        current = self._conn.execute("SELECT payload FROM status WHERE singleton=1").fetchone()
        payload = {} if current is None else json.loads(current[0])
        payload.update(values)
        encoded = json.dumps(payload, sort_keys=True)
        if current is not None and current[0] == encoded:
            return
        with self._conn:
            self._conn.execute(
                "INSERT INTO status VALUES(1,?) ON CONFLICT(singleton) DO UPDATE SET payload=excluded.payload",
                (encoded,),
            )

    def status(self) -> dict[str, object]:
        row = self._conn.execute("SELECT payload FROM status WHERE singleton=1").fetchone()
        status = {} if row is None else json.loads(row[0])
        state = self.state
        pending = self.decisions(pending_only=True)
        status.update(
            {
                "session_open_ms": None if state is None else state.session_open_ms,
                "session_close_ms": None if state is None else state.session_close_ms,
                "last_minute_ms": None if state is None else state.last_minute_ms,
                "pending_decisions": len(pending),
                "execution_hold_reason": next((row.explanation for row in pending if row.explanation), None),
                "legs": [] if state is None else state.model_dump(mode="json")["legs"],
                "events": [] if state is None else state.model_dump(mode="json")["events"][-100:],
            }
        )
        return status
