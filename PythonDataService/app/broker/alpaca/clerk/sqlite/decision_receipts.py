"""Bounded SQLite read/write API for strategy-decision receipts.

Decision receipts are trader-facing strategy evidence. They are intentionally
separate from Clerk custody transitions: recording a decision neither claims
broker custody nor changes account control revision.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import TYPE_CHECKING, TypeAlias

from app.broker.alpaca.clerk.decision_journal import DecisionOutcome
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.models import DecisionReceiptResource

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository

MAX_DECISION_RECEIPT_READ = 500

JsonScalar: TypeAlias = str | int | float | bool | None  # noqa: UP040 (Python 3.11)
JsonValue: TypeAlias = (  # noqa: UP040 (Python 3.11)
    JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
)


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
    conn.execute("BEGIN IMMEDIATE")
    try:
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
