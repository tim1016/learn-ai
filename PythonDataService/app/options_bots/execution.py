"""Clerk execution observations translated to Options rule facts.

Formula: cumulative average = sum(executed quantity * execution price) / sum(quantity).
Reference: the definition of a quantity-weighted average over the Clerk's effective
    execution records (ADR 0079). This is not a price estimated from market bars.
Canonical implementation: this file for a single Options attempt's observed
    execution average; position accounting remains the Clerk's.
Validated against: tests/options_bots/test_runtime.py, hand-worked partial fills.

The weighted average uses a fixed fifty-digit, half-even decimal context. A
repeating rational average must be represented finitely; this rounding is far
below the broker's price precision, and is independent of the caller context.
No fee or P&L is inferred from these observations.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from app.broker.alpaca.clerk.options_legs import OptionsLegSnapshot
from app.broker.alpaca.clerk.sqlite.entry_outcome import PROVEN_STANDINGS
from app.broker.alpaca.clerk.sqlite.option_decision_fills import OptionDecisionFill
from app.options.contract import OptionContract
from app.options_backtest.results import Fill
from app.options_bots.session import ExecutionOutcome
from app.schemas.signal_program_seal import semantic_payload_hash

_OBSERVED_AVERAGE = Context(prec=50, rounding=ROUND_HALF_EVEN)


def cumulative_fill(leg_id: str, rows: Sequence[OptionDecisionFill]) -> Fill | None:
    if not rows:
        return None
    first = rows[0]
    with localcontext(_OBSERVED_AVERAGE):
        quantity = Decimal(0)
        value = Decimal(0)
        for row in rows:
            contracts, price = Decimal(str(row.quantity)), Decimal(str(row.price))
            if (
                (row.contract, row.side, row.multiplier) != (first.contract, first.side, first.multiplier)
                or not contracts.is_finite()
                or contracts <= 0
                or contracts != contracts.to_integral_value()
                or not price.is_finite()
                or price < 0
            ):
                raise ValueError(
                    "Options execution records must describe positive whole fills of one contract and side"
                )
            quantity += contracts
            value += contracts * price
        price = value / quantity
    return Fill(
        leg_id,
        OptionContract.from_occ_symbol(first.contract),
        first.side,
        int(quantity),
        first.multiplier,
        max(row.filled_at_ms for row in rows),
        price,
        price,
    )


def observed_outcomes(
    snapshot: OptionsLegSnapshot, *, entry_number: int, entry_decided_at_ms: int
) -> tuple[ExecutionOutcome, ...]:
    if snapshot.entry_decision_id is None:
        return ()
    opening = cumulative_fill(snapshot.leg_id, snapshot.entry_fills)
    closed = cumulative_fill(snapshot.leg_id, snapshot.close_fills)
    terminal = snapshot.standing in PROVEN_STANDINGS
    results = []
    for kind, fill in (("entry", opening), ("close", closed)):
        if kind == "close" and fill is None:
            continue
        identity = semantic_payload_hash(
            {
                "attempt": snapshot.attempt_id,
                "kind": kind,
                "fills": [asdict(row) for row in (snapshot.entry_fills if kind == "entry" else snapshot.close_fills)],
                "terminal": terminal if kind == "entry" else False,
            }
        )
        results.append(
            ExecutionOutcome(
                identity,
                snapshot.leg_id,
                entry_number,
                kind,
                fill,
                terminal=terminal if kind == "entry" else False,
                decided_at_ms=entry_decided_at_ms,
            )
        )
    return tuple(results)
