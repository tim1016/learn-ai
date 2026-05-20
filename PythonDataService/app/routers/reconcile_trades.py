"""PR B (2026-05-19) Phase 4 — POST /api/lean-sidecar/reconcile-trades.

Wraps the canonical ``reconcile_trade_lists`` helper from
``app/services/lean_sidecar_compare_service.py`` and returns the trade-diff
shape spec § 6.5 calls for: ``matched_pairs``, ``python_only``,
``lean_only``, and ``first_divergence``.

The .NET ``RunCompareService.ReconcileTrades`` is the upstream caller; it
delegates here because the canonical reconciliation taxonomy lives in
Python (``DivergenceCategory`` from ``qc_reconciler.py``) and porting that
classification to C# would create a second source of truth.

Trade values arrive as ``Decimal``-typed strings on the wire so values
round-trip without float drift. The endpoint coerces them via
``Decimal(str(value))`` at the boundary before handing them off to the
reconciler.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.lean_sidecar_compare_service import (
    CompareResult,
    DivergenceDto,
    reconcile_trade_lists,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/lean-sidecar", tags=["lean-sidecar"])


class TradePayload(BaseModel):
    """One closed round-trip as the .NET ``BacktestTrade`` row sees it."""

    trade_number: int | None = None
    entry_ms_utc: int
    exit_ms_utc: int
    quantity: str
    entry_price: str
    exit_price: str
    pnl: str | None = None
    signal_reason: str | None = None
    is_synthetic_exit: bool | None = None


class ReconcileTradesRequest(BaseModel):
    left: list[TradePayload] = Field(default_factory=list)
    right: list[TradePayload] = Field(default_factory=list)
    fill_price_atol: str = "0.01"


class MatchedPair(BaseModel):
    trade_number: int
    entry_ts_delta_ms: int
    exit_ts_delta_ms: int
    entry_price_delta: str
    exit_price_delta: str
    qty_delta: str
    pnl_delta: str
    category: str


class UnmatchedTrade(BaseModel):
    trade_number: int
    entry_ms_utc: int
    exit_ms_utc: int
    entry_price: str
    exit_price: str
    quantity: str
    pnl: str


class TradeDivergenceResponse(BaseModel):
    trade_index: int
    what: str
    category: str
    left_value: str
    right_value: str


class ReconcileTradesResponse(BaseModel):
    matched_pairs: list[MatchedPair] = Field(default_factory=list)
    python_only: list[UnmatchedTrade] = Field(default_factory=list)
    lean_only: list[UnmatchedTrade] = Field(default_factory=list)
    first_divergence: TradeDivergenceResponse | None = None


@router.post("/reconcile-trades", response_model=ReconcileTradesResponse)
def reconcile_trades(payload: ReconcileTradesRequest) -> ReconcileTradesResponse:
    """Pair-by-pair reconciliation of two trade lists.

    Trade matching is by 1-indexed sequence (``trade_number`` if present,
    otherwise list position) — the same alignment strategy
    ``reconcile_trade_lists`` uses internally.  Pairs that exist on both
    sides become a ``MatchedPair``; trades present on only one side land
    in ``python_only`` (left-only) or ``lean_only`` (right-only).

    The ``first_divergence`` block is populated when at least one
    ``MatchedPair`` carries a non-``matched`` category; it points at the
    earliest such pair in trade-number order.
    """
    fill_atol = Decimal(payload.fill_price_atol)

    left_dicts = [_to_trade_dict(t, default_index=i) for i, t in enumerate(payload.left, start=1)]
    right_dicts = [_to_trade_dict(t, default_index=i) for i, t in enumerate(payload.right, start=1)]

    left_by_num = {d["trade_number"]: d for d in left_dicts}
    right_by_num = {d["trade_number"]: d for d in right_dicts}

    compare_result: CompareResult = reconcile_trade_lists(
        left_trades=left_dicts,
        right_trades=right_dicts,
        fill_price_atol=fill_atol,
    )

    divergences_by_num: dict[int, list[DivergenceDto]] = {}
    for d in compare_result.divergences:
        if d.trade_number is None:
            continue
        divergences_by_num.setdefault(d.trade_number, []).append(d)

    matched_pairs: list[MatchedPair] = []
    python_only: list[UnmatchedTrade] = []
    lean_only: list[UnmatchedTrade] = []

    all_nums = sorted(set(left_by_num) | set(right_by_num))
    for num in all_nums:
        left_trade = left_by_num.get(num)
        right_trade = right_by_num.get(num)
        if left_trade is not None and right_trade is not None:
            matched_pairs.append(_build_matched_pair(num, left_trade, right_trade, divergences_by_num, fill_atol))
        elif left_trade is not None:
            python_only.append(_to_unmatched(left_trade))
        elif right_trade is not None:
            lean_only.append(_to_unmatched(right_trade))

    first_div = _first_divergence(matched_pairs, divergences_by_num)

    logger.info(
        "reconcile-trades: left=%d right=%d matched=%d python_only=%d lean_only=%d divergences=%d",
        len(left_dicts),
        len(right_dicts),
        len(matched_pairs),
        len(python_only),
        len(lean_only),
        len(compare_result.divergences),
    )

    return ReconcileTradesResponse(
        matched_pairs=matched_pairs,
        python_only=python_only,
        lean_only=lean_only,
        first_divergence=first_div,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_trade_dict(trade: TradePayload, default_index: int) -> dict[str, Any]:
    """Coerce ``TradePayload`` to the ``Decimal``-typed dict the reconciler expects."""
    pnl_str = trade.pnl
    if pnl_str is None:
        # Synthesize PnL from (exit_price - entry_price) * quantity when
        # the caller didn't supply it; the reconciler's PNL_DRIFT check is
        # otherwise meaningless on a half-populated trade.
        synth = (Decimal(trade.exit_price) - Decimal(trade.entry_price)) * Decimal(trade.quantity)
        pnl_str = str(synth)

    return {
        "trade_number": trade.trade_number if trade.trade_number is not None else default_index,
        "entry_ms_utc": int(trade.entry_ms_utc),
        "exit_ms_utc": int(trade.exit_ms_utc),
        "entry_price": Decimal(trade.entry_price),
        "exit_price": Decimal(trade.exit_price),
        "quantity": Decimal(trade.quantity),
        "pnl": Decimal(pnl_str),
    }


def _build_matched_pair(
    trade_number: int,
    left: dict[str, Any],
    right: dict[str, Any],
    divergences_by_num: dict[int, list[DivergenceDto]],
    fill_atol: Decimal,
) -> MatchedPair:
    """Render the per-pair diff row + chosen category."""
    entry_delta_ms = int(right["entry_ms_utc"]) - int(left["entry_ms_utc"])
    exit_delta_ms = int(right["exit_ms_utc"]) - int(left["exit_ms_utc"])
    entry_price_delta = right["entry_price"] - left["entry_price"]
    exit_price_delta = right["exit_price"] - left["exit_price"]
    qty_delta = right["quantity"] - left["quantity"]
    pnl_delta = right["pnl"] - left["pnl"]

    divs = divergences_by_num.get(trade_number, [])
    category = _choose_category(divs, entry_price_delta, exit_price_delta, fill_atol)

    return MatchedPair(
        trade_number=trade_number,
        entry_ts_delta_ms=entry_delta_ms,
        exit_ts_delta_ms=exit_delta_ms,
        entry_price_delta=str(entry_price_delta),
        exit_price_delta=str(exit_price_delta),
        qty_delta=str(qty_delta),
        pnl_delta=str(pnl_delta),
        category=category,
    )


def _choose_category(
    divergences: list[DivergenceDto],
    entry_price_delta: Decimal,
    exit_price_delta: Decimal,
    fill_atol: Decimal,
) -> str:
    """Pick a single category string for a matched pair.

    Preference order matches ``DivergenceCategory`` severity: a structural
    mismatch (direction/quantity/decision) outranks a numerical drift;
    ``fill_price_drift`` outranks ``pnl_drift`` when both are flagged for
    the same trade (PNL drift is usually a downstream consequence of fill
    drift); when no divergence is recorded the pair is ``matched``.
    """
    if not divergences:
        return "matched"

    severity = {
        "DIRECTION_MISMATCH": 0,
        "QUANTITY_MISMATCH": 1,
        "DECISION_MISMATCH": 2,
        "ORDER_TYPE_MISMATCH": 3,
        "FILL_PRICE_DRIFT": 4,
        "COMMISSION_DRIFT": 5,
        "PNL_DRIFT": 6,
        "FIXTURE_INSUFFICIENT": 7,
    }
    chosen = min(divergences, key=lambda d: severity.get(d.category, 99))
    return chosen.category.lower()


def _to_unmatched(trade: dict[str, Any]) -> UnmatchedTrade:
    return UnmatchedTrade(
        trade_number=int(trade["trade_number"]),
        entry_ms_utc=int(trade["entry_ms_utc"]),
        exit_ms_utc=int(trade["exit_ms_utc"]),
        entry_price=str(trade["entry_price"]),
        exit_price=str(trade["exit_price"]),
        quantity=str(trade["quantity"]),
        pnl=str(trade["pnl"]),
    )


def _first_divergence(
    matched_pairs: list[MatchedPair],
    divergences_by_num: dict[int, list[DivergenceDto]],
) -> TradeDivergenceResponse | None:
    """Return the earliest matched-pair divergence as a render-ready record.

    A pair contributes a divergence when its category is anything other
    than ``matched``; ordering is by trade_number ascending so the
    earliest divergent trade surfaces in the compare view's First
    Divergence callout.
    """
    for i, pair in enumerate(matched_pairs):
        if pair.category == "matched":
            continue
        # Decide which field to surface based on the divergences recorded
        # for this trade. fill_price_drift is the most common; quantity /
        # direction take precedence when they exist.
        divs = divergences_by_num.get(pair.trade_number, [])
        what, left_value, right_value = _pick_divergence_field(divs, pair)
        return TradeDivergenceResponse(
            trade_index=i,
            what=what,
            category=pair.category,
            left_value=left_value,
            right_value=right_value,
        )
    return None


def _pick_divergence_field(
    divergences: list[DivergenceDto],
    pair: MatchedPair,
) -> tuple[str, str, str]:
    """Decide which delta on ``pair`` best explains the chosen category."""
    if not divergences:
        return ("pnl_delta", "0", pair.pnl_delta)

    # Surface the divergence's own left/right fill prices when present
    # (most common — fill_price_drift); else fall back to whichever delta
    # field is non-zero.
    div = divergences[0]
    if div.left_fill_price is not None and div.right_fill_price is not None:
        # Prefer the side (entry vs exit) whose delta is non-zero in the
        # pair record so the field label matches what the row shows.
        if Decimal(pair.exit_price_delta) != Decimal("0"):
            return ("exit_price_delta", str(div.left_fill_price), str(div.right_fill_price))
        return ("entry_price_delta", str(div.left_fill_price), str(div.right_fill_price))

    if div.left_quantity is not None and div.right_quantity is not None:
        return ("qty_delta", str(div.left_quantity), str(div.right_quantity))

    return ("pnl_delta", "0", pair.pnl_delta)
