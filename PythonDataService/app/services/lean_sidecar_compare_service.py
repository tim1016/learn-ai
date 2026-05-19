"""Compare two backtest-run trade lists and classify divergences.

Pure compute function: takes two trade arrays + tolerance config, returns
a classified divergence list. No DB access; .NET fetches trades from
Postgres and sends them via POST /api/lean-sidecar/compare (Task 3.2).

Each trade dict represents a closed round-trip:
    entry_ms_utc  int       bar-open timestamp for entry fill (int64 ms UTC)
    exit_ms_utc   int       bar-open timestamp for exit fill (int64 ms UTC)
    entry_price   float     fill price at entry
    exit_price    float     fill price at exit
    quantity      float     share count (positive)
    pnl           float     realized P&L net of fees
    trade_number  int       1-indexed sequence within the run
    signal_reason str       human-readable reason logged by the strategy
    is_synthetic_exit bool  True when the exit is a mark-to-market estimate

IMPORTANT: Imports ``DivergenceCategory`` from ``qc_reconciler`` as the
canonical taxonomy; no duplicate enum exists in this module.

Formula (PNL):
    realized_pnl = (exit_price - entry_price) * quantity

Canonical implementation: this file.
Reference: .claude/rules/numerical-rigor.md → "Trade-level reconciliation
    taxonomy"; LEAN EMA-crossover plan Task 3.1.
Validated against: tests/services/test_lean_sidecar_compare_service.py
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

# Canonical taxonomy — do NOT redefine here.
from app.research.parity.qc_reconciler import DivergenceCategory

logger = logging.getLogger(__name__)


@dataclass
class DivergenceDto:
    """One classified disagreement between two trade lists."""

    category: str  # DivergenceCategory.name (uppercase, e.g. "DECISION_MISMATCH")
    trade_number: int | None
    ms_utc: int | None
    message: str
    left_fill_price: float | None = None
    right_fill_price: float | None = None
    left_quantity: float | None = None
    right_quantity: float | None = None


@dataclass
class CompareResult:
    """Result of reconcile_trade_lists."""

    divergences: list[DivergenceDto] = field(default_factory=list)
    first_divergence_ms_utc: int | None = None


def reconcile_trade_lists(
    left_trades: Sequence[dict[str, Any]],
    right_trades: Sequence[dict[str, Any]],
    *,
    fill_price_atol: float = 0.01,
    assert_fees: bool = False,
) -> CompareResult:
    """Classify every disagreement between two trade lists.

    Each trade dict must have: entry_ms_utc, exit_ms_utc, entry_price,
    exit_price, quantity, pnl. trade_number is optional (defaults to
    1-indexed list position if absent).

    Alignment strategy: trades are matched by 1-indexed sequence position
    (trade_number if present, otherwise list index). A trade present on
    only one side is a DECISION_MISMATCH. Matched trades are compared
    field-by-field using the tolerance rules from numerical-rigor.md.

    ``assert_fees`` is accepted for API compatibility with the QC reconciler
    but has no effect (trade dicts carry net PnL, not raw fees).

    Returns CompareResult with a divergence list and the earliest
    divergent timestamp.
    """
    left_by_num = _index_by_trade_number(left_trades)
    right_by_num = _index_by_trade_number(right_trades)

    all_keys = sorted(set(left_by_num) | set(right_by_num))
    atol = Decimal(str(fill_price_atol))

    divergences: list[DivergenceDto] = []

    for key in all_keys:
        left = left_by_num.get(key)
        right = right_by_num.get(key)

        if left is None or right is None:
            present_side = left if left is not None else right
            ms = (
                int(present_side["entry_ms_utc"])  # type: ignore[index]
                if present_side is not None
                else None
            )
            divergences.append(
                DivergenceDto(
                    category=DivergenceCategory.DECISION_MISMATCH.name,
                    trade_number=key,
                    ms_utc=ms,
                    message=(f"trade #{key} present only on {'left' if right is None else 'right'} side"),
                )
            )
            continue

        _compare_trade_pair(left, right, key, atol, divergences)

    first_ms = min((d.ms_utc for d in divergences if d.ms_utc is not None), default=None)

    logger.info(
        "reconcile_trade_lists: %d left trades, %d right trades, %d divergences",
        len(left_trades),
        len(right_trades),
        len(divergences),
    )

    return CompareResult(divergences=divergences, first_divergence_ms_utc=first_ms)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _index_by_trade_number(
    trades: Sequence[dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    """Return a dict keyed by trade_number (1-indexed if absent)."""
    out: dict[int, dict[str, Any]] = {}
    for i, trade in enumerate(trades, start=1):
        key = int(trade.get("trade_number", i))
        out[key] = trade
    return out


def _compare_trade_pair(
    left: dict[str, Any],
    right: dict[str, Any],
    trade_number: int,
    fill_price_atol: Decimal,
    out: list[DivergenceDto],
) -> None:
    """Append any divergences found between a matched pair of trades."""
    entry_ms = int(left["entry_ms_utc"])

    # Quantity mismatch
    left_qty = float(left["quantity"])
    right_qty = float(right["quantity"])
    if abs(left_qty - right_qty) > 0:
        out.append(
            DivergenceDto(
                category=DivergenceCategory.QUANTITY_MISMATCH.name,
                trade_number=trade_number,
                ms_utc=entry_ms,
                message=(f"trade #{trade_number}: left qty={left_qty} right qty={right_qty}"),
                left_quantity=left_qty,
                right_quantity=right_qty,
            )
        )

    # Entry fill price drift
    left_entry = Decimal(str(left["entry_price"]))
    right_entry = Decimal(str(right["entry_price"]))
    if abs(left_entry - right_entry) > fill_price_atol:
        out.append(
            DivergenceDto(
                category=DivergenceCategory.FILL_PRICE_DRIFT.name,
                trade_number=trade_number,
                ms_utc=entry_ms,
                message=(f"trade #{trade_number} entry: |{left_entry} - {right_entry}| > {fill_price_atol}"),
                left_fill_price=float(left_entry),
                right_fill_price=float(right_entry),
            )
        )

    # Exit fill price drift
    exit_ms = int(left["exit_ms_utc"])
    left_exit = Decimal(str(left["exit_price"]))
    right_exit = Decimal(str(right["exit_price"]))
    if abs(left_exit - right_exit) > fill_price_atol:
        out.append(
            DivergenceDto(
                category=DivergenceCategory.FILL_PRICE_DRIFT.name,
                trade_number=trade_number,
                ms_utc=exit_ms,
                message=(f"trade #{trade_number} exit: |{left_exit} - {right_exit}| > {fill_price_atol}"),
                left_fill_price=float(left_exit),
                right_fill_price=float(right_exit),
            )
        )

    # PNL drift — propagated atol = (|qty_l| + |qty_r|) * fill_price_atol
    left_pnl = Decimal(str(left["pnl"]))
    right_pnl = Decimal(str(right["pnl"]))
    propagated_atol = (Decimal(str(abs(left_qty))) + Decimal(str(abs(right_qty)))) * fill_price_atol
    if abs(left_pnl - right_pnl) > propagated_atol:
        out.append(
            DivergenceDto(
                category=DivergenceCategory.PNL_DRIFT.name,
                trade_number=trade_number,
                ms_utc=entry_ms,
                message=(
                    f"trade #{trade_number}: left_pnl={left_pnl} "
                    f"right_pnl={right_pnl} "
                    f"diff={abs(left_pnl - right_pnl)} > atol={propagated_atol}"
                ),
            )
        )


__all__ = [
    "CompareResult",
    "DivergenceDto",
    "reconcile_trade_lists",
]
