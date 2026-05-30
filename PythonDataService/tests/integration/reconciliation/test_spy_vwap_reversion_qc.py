"""Trade-by-trade reconciliation: SpyVwapReversionAlgorithm vs QuantConnect (PR-K).

Runs the Python port through the BacktestEngine over the committed SPY 1-min
fixture (the same Polygon-sourced LEAN minute bars QC's window covers) and
reconciles its fills against the QuantConnect orders golden fixture via the
qc_reconciler taxonomy. Self-contained — no lean-cache, no network.

QC fills market orders at the signal bar's close, so the engine runs in
``SIGNAL_BAR_CLOSE`` fill mode. Commission is non-gating (Branch-B: the QC
backtest export carries no per-order fee).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from app.engine.data.lean_format import LeanMinuteDataReader
from app.engine.engine import BacktestEngine
from app.engine.execution.fill_model import FillModel
from app.engine.execution.order import FillMode
from app.engine.strategy.algorithms.spy_vwap_reversion import SpyVwapReversionAlgorithm
from app.research.parity.qc_reconciler import (
    DivergenceCategory,
    OurFill,
    Tolerances,
    _align_fills,
    _classify_divergences,
    _parse_qc_orders,
)

_FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "golden" / "spy-vwap-reversion-qc"
_LEAN_CACHE = _FIXTURE_DIR / "lean-cache"
_QC_ORDERS = _FIXTURE_DIR / "qc_orders.json"

# Math-correctness gating set: a mismatch here means the port took a DIFFERENT
# decision than QC (missed/extra/flipped/mis-sized trade) — a real port bug.
# FILL_PRICE_DRIFT is deliberately NOT here: it is the QC-vs-Polygon vendor
# data floor (see the data-source-floor test + the reconciliation report).
_DECISION_GATING = {
    DivergenceCategory.DECISION_MISMATCH,
    DivergenceCategory.DIRECTION_MISMATCH,
    DivergenceCategory.QUANTITY_MISMATCH,
    DivergenceCategory.ORDER_TYPE_MISMATCH,
    DivergenceCategory.FIXTURE_INSUFFICIENT,
}

# Accepted fill-price floor for the QC(QuantConnect data) vs ours(Polygon
# lean-cache) reconciliation. Max observed drift $0.29 — two entries whose
# close sat <1¢ from the lower band crossed one bar earlier under QC's
# marginally-different vendor prices (receipts in
# docs/references/reconciliations/spy-vwap-reversion.md). $0.30 ≈ 0.06% of
# SPY's ~$512 price: small relative to range, data-source, documented — the
# only conditions under which numerical-rigor.md permits loosening.
_DATA_SOURCE_FILL_ATOL = Decimal("0.30")


def _run_engine_fills() -> list[OurFill]:
    reader = LeanMinuteDataReader(_LEAN_CACHE)
    result = BacktestEngine(
        data_source=reader,
        fill_model=FillModel(mode=FillMode.SIGNAL_BAR_CLOSE),
    ).run(SpyVwapReversionAlgorithm())
    fills: list[OurFill] = []
    for e in result.order_events:
        qty = int(e.fill_quantity)  # signed
        fills.append(
            OurFill(
                symbol="SPY",
                side="buy" if qty > 0 else "sell",
                fill_qty=qty,
                fill_price=Decimal(str(e.fill_price)),
                fill_time_ms=int(e.time.timestamp() * 1000),
                fee=Decimal("0"),
            )
        )
    return fills


def test_spy_vwap_reversion_decisions_match_quantconnect() -> None:
    """Decision-level parity: the port takes the SAME trades as QC — same
    count, same (date, side, quantity) per trade. Proves the VWAP / sigma /
    band / signal math is a faithful port. Residual fill-price drift is
    vendor data and is asserted separately below."""
    our_fills = _run_engine_fills()
    qc_fills = _parse_qc_orders(_QC_ORDERS)

    assert len(our_fills) == len(qc_fills) == 10  # 5 long round-trips

    pairs = _align_fills(qc_fills, our_fills)
    divergences = _classify_divergences(pairs, Tolerances(), assert_fees=False)

    decision_breaches = [d for d in divergences if d.category in _DECISION_GATING]
    assert not decision_breaches, "port took different decisions than QC:\n" + "\n".join(
        f"  {d.category}: {d.detail}" for d in decision_breaches
    )


def test_fill_price_drift_within_documented_data_source_floor() -> None:
    """The only residual divergence is FILL_PRICE_DRIFT, bounded by the
    QC-vs-Polygon vendor floor ($0.30). At the strict $0.01 tolerance the
    sole gating category is FILL_PRICE_DRIFT (never a decision mismatch);
    at the documented floor it clears entirely."""
    our_fills = _run_engine_fills()
    qc_fills = _parse_qc_orders(_QC_ORDERS)
    pairs = _align_fills(qc_fills, our_fills)

    strict = _classify_divergences(pairs, Tolerances(), assert_fees=False)
    strict_categories = {d.category for d in strict}
    assert strict_categories <= {DivergenceCategory.FILL_PRICE_DRIFT}, (
        f"unexpected non-price divergence: {strict_categories}"
    )

    at_floor = _classify_divergences(
        pairs, Tolerances(fill_price_atol=_DATA_SOURCE_FILL_ATOL), assert_fees=False
    )
    assert not at_floor, "fill drift exceeded the documented data-source floor:\n" + "\n".join(
        f"  {d.detail}" for d in at_floor
    )


def test_engine_window_matches_qc_window() -> None:
    # Guards against a silently-truncated fixture: the engine sees the full
    # 5-session window the QC backtest covered.
    reader = LeanMinuteDataReader(_LEAN_CACHE)
    bars = list(reader.iter_bars("SPY", date(2024, 3, 4), date(2024, 3, 8)))
    assert len(bars) == 1950  # 5 RTH sessions × 390 min
