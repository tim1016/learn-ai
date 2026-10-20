"""Atomic Options decision outbox and rule snapshots, never an account ledger.

The Clerk owns orders, money and custody. This local SQLite journal records the
rules' progress and guarantees a decision is durable before its first delivery.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import TypeAdapter

from app.options_backtest.market import MinuteBar
from app.options_bots.observations import ConsumedMinute, ObservedOrder
from app.options_bots.session import (
    DECISION_ADAPTERS,
    CancelEntry,
    CloseLeg,
    OpenLeg,
    OptionsDecision,
    OptionsStep,
    SessionState,
)
from app.schemas.option_chart import ChartRuleLine
from app.schemas.signal_program_seal import semantic_payload_hash

JOURNAL_FILENAME = "options_bot.sqlite3"
logger = logging.getLogger(__name__)
_BAR = TypeAdapter(MinuteBar | None)


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
            CREATE TABLE IF NOT EXISTS consumed_minutes (
                session_open_ms INTEGER NOT NULL, symbol TEXT NOT NULL, close_ms INTEGER NOT NULL,
                bar_json TEXT NOT NULL, read_at_ms INTEGER NOT NULL, corrections INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(session_open_ms,symbol,close_ms));
            CREATE TABLE IF NOT EXISTS rule_lines (
                session_open_ms INTEGER NOT NULL, symbol TEXT NOT NULL, leg_id TEXT NOT NULL,
                entry_number INTEGER NOT NULL, kind TEXT NOT NULL, price TEXT NOT NULL,
                from_ms INTEGER NOT NULL, to_ms INTEGER);
            CREATE TABLE IF NOT EXISTS observed_orders (
                session_open_ms INTEGER NOT NULL, order_ref TEXT NOT NULL, symbol TEXT NOT NULL,
                leg_id TEXT NOT NULL, entry_number INTEGER NOT NULL, kind TEXT NOT NULL,
                filled_at_ms INTEGER NOT NULL, exit_reason TEXT,
                PRIMARY KEY(session_open_ms,order_ref,symbol));
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

    def commit(
        self, state: SessionState, step: OptionsStep, *,
        minutes: Sequence[ConsumedMinute] = (), read_at_ms: int | None = None,
        orders: Sequence[ObservedOrder] = (),
    ) -> None:
        if self.strategy_instance_id is None:
            raise ValueError("a writable Options journal needs a sealed bot identity")
        encoded = state.model_dump_json()
        existing = self._conn.execute(
            "SELECT state_json FROM sessions WHERE open_ms=?", (state.session_open_ms,)
        ).fetchone()
        if existing is not None and existing[0] == encoded and not step.decisions and not minutes and not orders:
            return
        with self._conn:
            instant = read_at_ms if read_at_ms is not None else state.last_minute_ms
            self._save_minutes(state.session_open_ms, minutes, instant)
            self._save_orders(state.session_open_ms, orders)
            if instant is not None:
                self._save_rules(state, instant)
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

    def _save_minutes(self, session: int, minutes: Sequence[ConsumedMinute], instant: int | None) -> None:
        for minute in minutes:
            if instant is None:
                raise ValueError("a consumed minute needs its read instant")
            payload = _BAR.dump_json(minute.bar).decode()
            key = (session, minute.symbol, minute.close_ms)
            old = self._conn.execute(
                "SELECT bar_json FROM consumed_minutes WHERE session_open_ms=? AND symbol=? AND close_ms=?", key
            ).fetchone()
            if old is None:
                self._conn.execute("INSERT INTO consumed_minutes VALUES(?,?,?,?,?,0)", (*key, payload, instant))
            elif old[0] != payload:
                self._conn.execute(
                    "UPDATE consumed_minutes SET corrections=corrections+1 WHERE session_open_ms=? AND symbol=? AND close_ms=?", key
                )
                logger.warning("Consumed option minute correction ignored", extra={
                    "action": "options_consumed_minute_correction_ignored", "symbol": minute.symbol,
                    "close_ms": minute.close_ms, "strategy_instance_id": self.strategy_instance_id,
                })

    def _save_rules(self, state: SessionState, instant: int) -> None:
        desired = {}
        for leg in state.legs:
            attempt, position = leg.attempt, leg.position
            if attempt is None or leg.phase.value not in {"waiting", "ordered", "held"}:
                continue
            number = leg.entries_filled if position is None else position.entry_number
            levels: dict[str, Decimal | None] = {
                "starting_price": attempt.starting_price if leg.phase.value == "waiting" else None,
                "momentum": attempt.momentum_level if leg.phase.value == "waiting" else None,
                "stop": None if position is None or position.contracts_held == 0 else position.stop_level,
            }
            for kind, price in levels.items():
                if price is not None:
                    desired[(attempt.contract.occ_symbol, leg.setup.leg_id, number, kind)] = str(price)
        active = self._conn.execute(
            "SELECT rowid,* FROM rule_lines WHERE session_open_ms=? AND to_ms IS NULL", (state.session_open_ms,)
        ).fetchall()
        for row in active:
            key = (row["symbol"], row["leg_id"], row["entry_number"], row["kind"])
            if desired.get(key) == row["price"]:
                del desired[key]
            else:
                end = instant if row["kind"] == "stop" else state.last_minute_ms
                if row["kind"] == "stop":
                    leg = next(leg for leg in state.legs if leg.setup.leg_id == row["leg_id"])
                    if key in desired and leg.position is not None:
                        end = leg.position.entry.bar_close_ms
                    else:
                        closed = self._conn.execute(
                            "SELECT MAX(filled_at_ms) FROM observed_orders WHERE session_open_ms=? AND symbol=? AND leg_id=? AND entry_number=? AND kind='close'",
                            (state.session_open_ms, row["symbol"], row["leg_id"], row["entry_number"]),
                        ).fetchone()[0]
                        end = closed if closed is not None else instant
                self._conn.execute("UPDATE rule_lines SET to_ms=? WHERE rowid=?", (end, row["rowid"]))
        for (symbol, leg_id, number, kind), price in desired.items():
            start = instant
            leg = next(leg for leg in state.legs if leg.setup.leg_id == leg_id)
            if kind == "stop" and leg.position is not None:
                start = leg.position.entry.bar_close_ms
            elif kind != "stop":
                start = next(event.at_ms for event in reversed(state.events)
                             if event.kind.value == "starting_price_taken" and event.leg_id == leg_id
                             and event.entry_number == number)
            self._conn.execute("INSERT INTO rule_lines VALUES(?,?,?,?,?,?,?,NULL)",
                               (state.session_open_ms, symbol, leg_id, number, kind, price, start))

    def _save_orders(self, session: int, orders: Sequence[ObservedOrder]) -> None:
        decisions = self.decisions(session_open_ms=session)
        for order in orders:
            closing = next((row.decision for row in decisions
                            if row.attempt_id == order.attempt_id and isinstance(row.decision, CloseLeg)), None)
            reason = None if order.kind == "entry" else closing.reason.value if closing is not None else "clerk_reduction"
            self._conn.execute(
                "INSERT INTO observed_orders VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(session_open_ms,order_ref,symbol) DO UPDATE SET filled_at_ms=MAX(filled_at_ms,excluded.filled_at_ms)",
                (session, order.order_ref, order.symbol, order.leg_id, order.entry_number, order.kind, order.filled_at_ms, reason),
            )

    def fill_reasons(self, session: int) -> dict[tuple[str, str], str | None]:
        return {(row["order_ref"], row["symbol"]): row["exit_reason"] for row in self._conn.execute(
            "SELECT * FROM observed_orders WHERE session_open_ms=?", (session,)
        )}

    def order_legs(self, session: int) -> dict[tuple[str, str], str]:
        return {(row["order_ref"], row["symbol"]): row["leg_id"] for row in self._conn.execute(
            "SELECT * FROM observed_orders WHERE session_open_ms=?", (session,)
        )}

    def sessions(self, from_ms: int, to_ms: int) -> tuple[SessionState, ...]:
        return tuple(SessionState.model_validate_json(row[0]) for row in self._conn.execute(
            "SELECT state_json FROM sessions WHERE open_ms<? AND open_ms>? ORDER BY open_ms", (to_ms, from_ms - 86_400_000)
        ) if json.loads(row[0])["session_close_ms"] > from_ms)

    def minutes(self, session_open_ms: int) -> tuple[ConsumedMinute, ...]:
        return tuple(ConsumedMinute(row["symbol"], row["close_ms"], _BAR.validate_json(row["bar_json"]))
                     for row in self._conn.execute(
                         "SELECT * FROM consumed_minutes WHERE session_open_ms=? ORDER BY close_ms,symbol", (session_open_ms,)
                     ))

    def ignored_corrections(self) -> int:
        return self._conn.execute("SELECT COALESCE(SUM(corrections),0) FROM consumed_minutes").fetchone()[0]

    def rules(self, session_open_ms: int, symbol: str) -> list[ChartRuleLine]:
        return [ChartRuleLine(**{key: row[key] for key in ("kind", "price", "from_ms", "to_ms", "leg_id", "entry_number")})
                for row in self._conn.execute(
                    "SELECT * FROM rule_lines WHERE session_open_ms=? AND symbol=? ORDER BY from_ms,rowid", (session_open_ms, symbol)
                )]

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
                "events": [] if state is None else state.model_dump(mode="json")["events"],
            }
        )
        return status
