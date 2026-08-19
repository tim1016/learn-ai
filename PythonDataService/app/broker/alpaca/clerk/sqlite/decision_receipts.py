"""Bounded SQLite read/write API for strategy-decision receipts.

Decision receipts are trader-facing strategy evidence. They are intentionally
separate from Clerk custody transitions: recording a decision neither claims
broker custody nor changes account control revision.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.models import DecisionReceiptResource

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository

MAX_DECISION_RECEIPT_READ = 500
MAX_DECISION_RECEIPTS_PER_STRATEGY = 1_000

JsonScalar: TypeAlias = str | int | float | bool | None  # noqa: UP040 (Python 3.11)
JsonValue: TypeAlias = (  # noqa: UP040 (Python 3.11)
    JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
)
DecisionOutcome = Literal[
    "enter_intent",
    "exit_intent",
    "entered",
    "exited",
    "no_action",
    "blocked",
]


class DecisionReceipt(BaseModel):
    """Trader-facing view of one SQLite decision receipt."""

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=1)
    ts_ms: int = Field(ge=0)
    bar_ref: str
    outcome: DecisionOutcome
    reason_code: str
    intent_id: str = ""
    order_ref: str = ""
    indicator_snapshot: dict[str, float | int | str | None] = Field(default_factory=dict)


class DecisionReceiptConflictError(ValueError):
    """One closed bar was replayed with a different strategy decision."""


def append_decision_receipt_row(
    conn: sqlite3.Connection,
    *,
    strategy_instance_id: str,
    outcome: str,
    symbol: str | None,
    intent_id: str | None,
    order_ref: str | None,
    observed_at_ms: int,
    facts_json: str,
) -> DecisionReceiptResource:
    """Allocate and insert one receipt in its own SQLite transaction.

    The repository performs the lease/poison checks and holds its application
    write coordinator before invoking this helper. Keeping allocation and SQL
    here makes this module the sole owner of receipt persistence without
    widening the repository's transaction responsibilities.
    """
    bar_ref = _bar_ref(facts_json)
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = _receipt_for_bar(
            conn,
            strategy_instance_id=strategy_instance_id,
            bar_ref=bar_ref,
        )
        if existing is not None:
            if _same_decision(existing, symbol=symbol, intent_id=intent_id):
                conn.commit()
                return existing
            raise DecisionReceiptConflictError(
                f"closed bar {bar_ref!r} already has a different decision receipt"
            )
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM decision_receipts "
            "WHERE strategy_instance_id = ?",
            (strategy_instance_id,),
        ).fetchone()
        assert row is not None
        seq = int(row[0])
        conn.execute(
            "INSERT INTO decision_receipts "
            "(strategy_instance_id, seq, outcome, symbol, intent_id, order_ref, "
            "observed_at_ms, facts_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                strategy_instance_id,
                seq,
                outcome,
                symbol,
                intent_id,
                order_ref,
                observed_at_ms,
                facts_json,
            ),
        )
        conn.execute(
            "DELETE FROM decision_receipts "
            "WHERE strategy_instance_id = ? AND seq <= ("
            "SELECT COALESCE(MAX(seq), 0) - ? FROM decision_receipts "
            "WHERE strategy_instance_id = ?) ",
            (
                strategy_instance_id,
                MAX_DECISION_RECEIPTS_PER_STRATEGY,
                strategy_instance_id,
            ),
        )
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    return DecisionReceiptResource(
        strategy_instance_id=strategy_instance_id,
        seq=seq,
        outcome=outcome,
        symbol=symbol,
        intent_id=intent_id,
        order_ref=order_ref,
        observed_at_ms=observed_at_ms,
        facts_json=facts_json,
    )


def update_decision_receipt_for_bar(
    conn: sqlite3.Connection,
    *,
    strategy_instance_id: str,
    bar_ref: str,
    outcome: str,
    order_ref: str | None,
    facts_json: str,
) -> DecisionReceiptResource:
    """Replace the final outcome for an already-recorded closed-bar decision."""
    if _bar_ref(facts_json) != bar_ref:
        raise ValueError("final decision receipt facts must preserve the closed bar reference")
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = _receipt_for_bar(
            conn,
            strategy_instance_id=strategy_instance_id,
            bar_ref=bar_ref,
        )
        if existing is None:
            raise DecisionReceiptConflictError(
                f"closed bar {bar_ref!r} has no decision receipt to update"
            )
        conn.execute(
            "UPDATE decision_receipts SET outcome = ?, order_ref = ?, facts_json = ? "
            "WHERE strategy_instance_id = ? AND seq = ?",
            (outcome, order_ref, facts_json, strategy_instance_id, existing.seq),
        )
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    return DecisionReceiptResource(
        strategy_instance_id=existing.strategy_instance_id,
        seq=existing.seq,
        outcome=outcome,
        symbol=existing.symbol,
        intent_id=existing.intent_id,
        order_ref=order_ref,
        observed_at_ms=existing.observed_at_ms,
        facts_json=facts_json,
    )


def _bar_ref(facts_json: str) -> str | None:
    facts = json.loads(facts_json)
    if not isinstance(facts, dict):
        raise ValueError("decision receipt facts must be a JSON object")
    value = facts.get("bar_ref")
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("decision receipt bar_ref must be a non-empty string when provided")
    return value


def _receipt_for_bar(
    conn: sqlite3.Connection,
    *,
    strategy_instance_id: str,
    bar_ref: str | None,
) -> DecisionReceiptResource | None:
    if bar_ref is None:
        return None
    row = conn.execute(
        "SELECT strategy_instance_id, seq, outcome, symbol, intent_id, order_ref, "
        "observed_at_ms, facts_json FROM decision_receipts "
        "WHERE strategy_instance_id = ? AND json_extract(facts_json, '$.bar_ref') = ? "
        "ORDER BY seq DESC LIMIT 1",
        (strategy_instance_id, bar_ref),
    ).fetchone()
    return None if row is None else DecisionReceiptResource(**dict(row))


def _same_decision(
    existing: DecisionReceiptResource,
    *,
    symbol: str | None,
    intent_id: str | None,
) -> bool:
    return existing.symbol == symbol and existing.intent_id == intent_id


class SqliteDecisionReceipts:
    """One strategy instance's typed, bounded decision-receipt surface."""

    def __init__(
        self,
        repository: ClerkSqliteRepository,
        *,
        strategy_instance_id: str,
    ) -> None:
        if not strategy_instance_id:
            raise ValueError("strategy_instance_id must be non-empty")
        self._repository = repository
        self._strategy_instance_id = strategy_instance_id

    def append(
        self,
        *,
        outcome: DecisionOutcome,
        symbol: str | None,
        observed_at_ms: int,
        facts: Mapping[str, JsonValue],
        intent_id: str | None = None,
        order_ref: str | None = None,
    ) -> DecisionReceiptResource:
        """Append one receipt and atomically assign its per-bot sequence."""
        return self._repository.append_decision_receipt(
            strategy_instance_id=self._strategy_instance_id,
            outcome=outcome,
            symbol=symbol,
            intent_id=intent_id,
            order_ref=order_ref,
            observed_at_ms=observed_at_ms,
            facts_json=canonicalize(dict(facts)),
        )

    def update_final_outcome(
        self,
        *,
        bar_ref: str,
        outcome: DecisionOutcome,
        facts: Mapping[str, JsonValue],
        order_ref: str | None = None,
    ) -> DecisionReceiptResource:
        """Replace one closed bar's provisional receipt with its final outcome."""
        return self._repository.update_decision_receipt_for_bar(
            strategy_instance_id=self._strategy_instance_id,
            bar_ref=bar_ref,
            outcome=outcome,
            order_ref=order_ref,
            facts_json=canonicalize(dict(facts)),
        )

    def tail(self, n: int) -> list[DecisionReceiptResource]:
        """Return the bounded newest suffix in ascending sequence order."""
        return self._repository.decision_receipt_tail(
            strategy_instance_id=self._strategy_instance_id,
            limit=_read_limit(n),
        )

    def by_transaction(
        self,
        transaction_ref: str,
        *,
        limit: int = MAX_DECISION_RECEIPT_READ,
    ) -> list[DecisionReceiptResource]:
        """Return a bounded receipt suffix matching an intent or order ref."""
        if not transaction_ref:
            raise ValueError("transaction_ref must be non-empty")
        return self._repository.decision_receipts_by_transaction(
            strategy_instance_id=self._strategy_instance_id,
            transaction_ref=transaction_ref,
            limit=_read_limit(limit),
        )


def _read_limit(requested: int) -> int:
    if requested <= 0:
        raise ValueError(f"receipt read limit must be positive; got {requested}")
    return min(requested, MAX_DECISION_RECEIPT_READ)
