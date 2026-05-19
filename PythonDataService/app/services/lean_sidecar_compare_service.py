"""Compare two backtest-run trade lists and classify divergences.

Pure compute function: takes two trade arrays + tolerance config, returns
a classified divergence list. No DB access; .NET fetches trades from
Postgres and sends them via POST /api/lean-sidecar/compare (Task 3.2).

Each trade dict represents a closed round-trip:
    entry_ms_utc    int       bar-open timestamp for entry fill (int64 ms UTC)
    exit_ms_utc     int       bar-open timestamp for exit fill (int64 ms UTC)
    entry_price     Decimal   fill price at entry
    exit_price      Decimal   fill price at exit
    quantity        Decimal   share count (positive)
    pnl             Decimal   realized P&L net of fees
    trade_number    int       1-indexed sequence within the run
    signal_reason   str       human-readable reason logged by the strategy
    is_synthetic_exit bool    True when the exit is a mark-to-market estimate
    fee             Decimal   (optional) brokerage fee; required when assert_fees=True

IMPORTANT: Imports ``DivergenceCategory`` from ``qc_reconciler`` as the
canonical taxonomy; no duplicate enum exists in this module.

Design note — FIXTURE_INSUFFICIENT and ORDER_TYPE_MISMATCH:
    This service operates on pre-matched trade *lists*, not on raw price-
    history fixtures or LEAN order objects.  Two categories therefore
    cannot be fully classified at this layer:

    * FIXTURE_INSUFFICIENT — requires access to the captured price-history
      bar series to verify that each fill price is explainable.  That check
      lives in ``qc_reconciler._audit_fixture``, which is invoked by the
      full ``reconcile_qc_aapl_phase3`` pipeline.  The compare service
      operates on already-reconciled-against-fixture inputs: callers are
      expected to have already gated on fixture sufficiency before posting
      trade lists here.  The category is imported so it can pass through
      unchanged if an upstream tool stamps a divergence with it.

    * ORDER_TYPE_MISMATCH — the trade-list payload carries round-trip P&L
      data, not raw order-type codes.  The order-type field lives in LEAN's
      ``/backtests/orders/read`` response, which the ``qc_reconciler``
      pipeline processes.  Adding it to the compare-service payload would
      require a schema change in both the Backend DTO and the Postgres
      trades table.  If/when that field is added to the trade-list payload,
      classify here as: ``order_type != "MARKET"`` → ORDER_TYPE_MISMATCH.

Formula (PNL):
    realized_pnl = (exit_price - entry_price) * quantity

Canonical implementation: this file.
Reference: .claude/rules/numerical-rigor.md → "Trade-level reconciliation
    taxonomy"; LEAN EMA-crossover plan Task 3.1.
Validated against: tests/services/test_lean_sidecar_compare_service.py
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

# Canonical taxonomy — do NOT redefine here.
from app.research.parity.qc_reconciler import DivergenceCategory

logger = logging.getLogger(__name__)

_ZERO = Decimal("0")


@dataclass
class DivergenceDto:
    """One classified disagreement between two trade lists."""

    category: str  # DivergenceCategory.name (uppercase, e.g. "DECISION_MISMATCH")
    trade_number: int | None
    ms_utc: int | None
    message: str
    left_fill_price: Decimal | None = None
    right_fill_price: Decimal | None = None
    left_quantity: Decimal | None = None
    right_quantity: Decimal | None = None


@dataclass
class CompareResult:
    """Result of reconcile_trade_lists."""

    divergences: list[DivergenceDto] = field(default_factory=list)
    first_divergence_ms_utc: int | None = None


def reconcile_trade_lists(
    left_trades: Sequence[dict[str, Any]],
    right_trades: Sequence[dict[str, Any]],
    *,
    fill_price_atol: Decimal | float = Decimal("0.01"),
    commission_atol: Decimal | float = Decimal("0.001"),
    assert_fees: bool = False,
) -> CompareResult:
    """Classify every disagreement between two trade lists.

    Each trade dict must have: entry_ms_utc, exit_ms_utc, entry_price,
    exit_price, quantity, pnl. trade_number is optional (defaults to
    1-indexed list position if absent).

    When ``assert_fees=True``, each trade dict must also have a ``fee``
    field (Decimal or float).  Pairs whose fee difference exceeds
    ``commission_atol`` are emitted as COMMISSION_DRIFT divergences.

    Alignment strategy: trades are matched by 1-indexed sequence position
    (trade_number if present, otherwise list index). A trade present on
    only one side is a DECISION_MISMATCH. Matched trades are compared
    field-by-field using the tolerance rules from numerical-rigor.md.

    Returns CompareResult with a divergence list and the earliest
    divergent timestamp.
    """
    left_by_num = _index_by_trade_number(left_trades)
    right_by_num = _index_by_trade_number(right_trades)

    all_keys = sorted(set(left_by_num) | set(right_by_num))
    atol = Decimal(str(fill_price_atol))
    fee_atol = Decimal(str(commission_atol))

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

        _compare_trade_pair(left, right, key, atol, fee_atol, assert_fees, divergences)

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


def _to_decimal(value: Any) -> Decimal:
    """Coerce a numeric value to Decimal without silent loss."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _compare_trade_pair(
    left: dict[str, Any],
    right: dict[str, Any],
    trade_number: int,
    fill_price_atol: Decimal,
    commission_atol: Decimal,
    assert_fees: bool,
    out: list[DivergenceDto],
) -> None:
    """Append any divergences found between a matched pair of trades."""
    entry_ms = int(left["entry_ms_utc"])

    # Quantity — check direction first (DIRECTION_MISMATCH), then magnitude
    # (QUANTITY_MISMATCH).  Direction is derived from the sign of the signed
    # quantity field; the trade-list payload carries positive quantities for
    # long-only runs, but sign is checked defensively.
    left_qty = _to_decimal(left["quantity"])
    right_qty = _to_decimal(right["quantity"])

    left_sign = math.copysign(1, float(left_qty))
    right_sign = math.copysign(1, float(right_qty))
    if left_sign != right_sign and left_qty != _ZERO and right_qty != _ZERO:
        out.append(
            DivergenceDto(
                category=DivergenceCategory.DIRECTION_MISMATCH.name,
                trade_number=trade_number,
                ms_utc=entry_ms,
                message=(
                    f"trade #{trade_number}: left qty sign={'positive' if left_sign > 0 else 'negative'} "
                    f"right qty sign={'positive' if right_sign > 0 else 'negative'}"
                ),
                left_quantity=left_qty,
                right_quantity=right_qty,
            )
        )
    elif abs(left_qty) != abs(right_qty):
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
    left_entry = _to_decimal(left["entry_price"])
    right_entry = _to_decimal(right["entry_price"])
    if abs(left_entry - right_entry) > fill_price_atol:
        out.append(
            DivergenceDto(
                category=DivergenceCategory.FILL_PRICE_DRIFT.name,
                trade_number=trade_number,
                ms_utc=entry_ms,
                message=(f"trade #{trade_number} entry: |{left_entry} - {right_entry}| > {fill_price_atol}"),
                left_fill_price=left_entry,
                right_fill_price=right_entry,
            )
        )

    # Exit fill price drift
    exit_ms = int(left["exit_ms_utc"])
    left_exit = _to_decimal(left["exit_price"])
    right_exit = _to_decimal(right["exit_price"])
    if abs(left_exit - right_exit) > fill_price_atol:
        out.append(
            DivergenceDto(
                category=DivergenceCategory.FILL_PRICE_DRIFT.name,
                trade_number=trade_number,
                ms_utc=exit_ms,
                message=(f"trade #{trade_number} exit: |{left_exit} - {right_exit}| > {fill_price_atol}"),
                left_fill_price=left_exit,
                right_fill_price=right_exit,
            )
        )

    # Commission drift — only when assert_fees=True (Branch A).
    # Both sides must carry a ``fee`` field; if either is absent the caller
    # did not supply fee data and this check is silently skipped.
    if assert_fees:
        left_fee_raw = left.get("fee")
        right_fee_raw = right.get("fee")
        if left_fee_raw is not None and right_fee_raw is not None:
            left_fee = _to_decimal(left_fee_raw)
            right_fee = _to_decimal(right_fee_raw)
            if abs(left_fee - right_fee) > commission_atol:
                out.append(
                    DivergenceDto(
                        category=DivergenceCategory.COMMISSION_DRIFT.name,
                        trade_number=trade_number,
                        ms_utc=entry_ms,
                        message=(
                            f"trade #{trade_number}: left_fee={left_fee} "
                            f"right_fee={right_fee} "
                            f"diff={abs(left_fee - right_fee)} > atol={commission_atol}"
                        ),
                    )
                )

    # PNL drift — propagated atol = (|qty_l| + |qty_r|) * fill_price_atol
    left_pnl = _to_decimal(left["pnl"])
    right_pnl = _to_decimal(right["pnl"])
    propagated_atol = (abs(left_qty) + abs(right_qty)) * fill_price_atol
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
