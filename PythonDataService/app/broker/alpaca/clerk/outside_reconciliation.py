"""Evidence-only reconciliation of closed outside crypto positions (#3311).

Formula: remaining quantity = sum(signed outside fills) + sum(CFEE qty).
Reference: https://docs.alpaca.markets/docs/crypto-fees — a buy's fee is
  charged in the received crypto asset; CFEE records the negative quantity.
Canonical implementation: this file. No fee rate is inferred or applied.
Validated against: tests/broker/alpaca/clerk/test_outside_reconciliation.py
  and tests/broker/v2panel/test_budget_deploy.py.

Date-only fee records cannot establish intraday FIFO placement. Only an
exactly closed population is resolved. A partial balance keeps its original
cost and asks for evidence; it is never assigned an invented sale price.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from app.broker.alpaca.clerk.fills import FillRecord
from app.broker.alpaca.clerk.money import ZERO, MoneyInputError, money_context, normalize_money
from app.broker.contract.models import BrokerActivity, OrderSide
from app.utils.session_anchors import et_date_at_ms


@dataclass(frozen=True)
class OutsideReconciliation:
    symbol: str
    state: Literal["resolved", "unresolved"]
    detail: str
    next_action: str | None
    evidence_ids: tuple[str, ...]


@money_context()
def reconcile_outside_quantities(
    fills: Sequence[FillRecord], fees: Sequence[BrokerActivity], *, excluded_symbols: set[str],
) -> tuple[OutsideReconciliation, ...]:
    """Resolve only a whole, exclusively outside, USD-quoted crypto population.

    The caller has proved complete, conflict-free activity coverage.
    Account cash, trade fills and the append-only evidence are never changed.
    Excluded symbols have Clerk-owned or pre-custody fills, or incomplete order
    mapping: their account-wide fee cannot safely be assigned to this population.
    """
    symbols = {fill.symbol for fill in fills if fill.symbol.endswith("/USD")}
    grouped: dict[str, list[BrokerActivity]] = {}
    for row in fees:
        raw_symbol = row.symbol or "Unknown asset"
        candidates = {symbol for symbol in symbols if raw_symbol in (symbol, symbol.replace("/", ""))}
        symbol = next(iter(candidates)) if len(candidates) == 1 else raw_symbol
        grouped.setdefault(symbol, []).append(row)
    answers = []
    for symbol, rows in sorted(grouped.items()):
        records = [fill for fill in fills if fill.symbol == symbol]
        valid = symbol in symbols and symbol not in excluded_symbols
        first_day = min((et_date_at_ms(fill.filled_at_ms) for fill in records), default=None)
        quantity = ZERO
        try:
            for row in rows:
                debit = normalize_money(row.quantity)
                valid = valid and debit < ZERO and row.net_amount == 0 and row.occurred_at_ms is not None
                valid = valid and first_day is not None and et_date_at_ms(row.occurred_at_ms) >= first_day
                quantity += debit
        except MoneyInputError:
            valid = False
        residual = sum((normalize_money(fill.quantity) * (1 if fill.side is OrderSide.BUY else -1)
                        for fill in records), ZERO)
        resolved = valid and residual > ZERO and residual + quantity == ZERO
        detail = (
            f"Outside {symbol} fills reconciled: {len(rows)} recorded crypto fees account for "
            f"{format(residual, 'f')} units left by the fills. No holding remains from this history. "
            "Original fills and fee records are retained."
            if resolved else
            f"Outside {symbol} quantity fees cannot yet reconcile the recorded fills. "
            "The original purchase cost remains on the books; no correction has been inferred."
        )
        answers.append(OutsideReconciliation(
            symbol, "resolved" if resolved else "unresolved", detail,
            None if resolved else "Reconcile account activity, then compare the complete fill and crypto-fee records at Alpaca.",
            tuple(sorted(row.activity_id for row in rows)),
        ))
    return tuple(answers)
