"""Phase 3.5 acceptance: AAPL single-symbol trade-level parity vs QC.

Skipped on master until ``tests/fixtures/golden/qc-aapl-phase3/`` is
committed. The fixture-landing PR (#219) supplies the QC artifacts; this
test activates automatically once they're present.

Scope: 2-day window (2026-02-09 → 2026-02-11). QC free tier's minute-data
trailing window (~90 days, per https://www.quantconnect.com/forum/discussion/19781/getting-data-with-free-plan/)
truncated the achievable backtest to this window; 1 entry fill fires on
2026-02-10, no exit (positive prediction every day). Full round-trip P&L
coverage is not pursued (decision 2026-05-12 — see authority doc § 10).

``_build_our_fills`` imports the prediction set, runs ``BacktestEngine``
directly (bypassing ``run_strategy_spec``) so it captures order_events
including the open-position entry that ``run_strategy_spec``'s trade log
would miss (``LoggedTrade`` only records closed round-trips). Commission
is computed reconciler-side via ``IbkrEquityCommissionModel``.

See ``docs/ml-predictions-authority.md`` §3 and
``docs/references/reconciliations/qc-aapl-phase3.md``.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.engine.strategy.spec import SpecAlgorithm, StrategySpec
from app.research.ml.generators.quantconnect_fixture import import_qc_fixture
from app.research.parity.ibkr_commission import IbkrEquityCommissionModel
from app.research.parity.qc_reconciler import (
    OurFill,
    Tolerances,
    reconcile_qc_aapl_phase3,
)

_FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "golden" / "qc-aapl-phase3"
_ORDERS = _FIXTURE_DIR / "qc_orders.json"
_PRICES = _FIXTURE_DIR / "qc_price_history.csv"
_ARTIFACTS_DIR = Path(__file__).resolve().parents[3] / "artifacts" / "reconciliations"

_QC_PREDICTIONS_FIXTURE = (
    Path(__file__).resolve().parents[2] / "fixtures" / "golden" / "qc-precomputed-predictions" / "qc_export.json"
)

_PREDICTION_SET_ID = "qc_aapl_phase3_acceptance"
_INITIAL_CASH = Decimal("100000")

# Provenance constants for the prediction-set import — mirror PR #215's
# attribution.md so the imported set hashes deterministically.
_QC_TUTORIAL_URL = (
    "https://www.quantconnect.com/docs/v2/writing-algorithms/importing-data/streaming-data/precomputed-ml-predictions"
)
_QC_EXPORTED_AT_MS = 1778469503771  # 2026-05-06 22:25:03 UTC
_QC_WINDOW_START_MS = 1770757200000  # 2026-02-10 16:00 ET (NY anchor)
_QC_WINDOW_END_MS = 1773345600000  # 2026-03-12 16:00 ET
_QC_DATASET_ID = "QuantConnect/USEquity-Daily"
_QC_VERSIONS = {
    "sklearn": "1.6.1",
    "numpy": "1.26.4",
    "pandas": "2.3.3",
    "lean": "unknown",
}


pytestmark = pytest.mark.skipif(
    not _ORDERS.is_file(),
    reason="Phase 3 QC fixture not yet captured (see capture-smoke test)",
)


def _aapl_spec() -> StrategySpec:
    """Single-symbol AAPL Path A spec.

    Phase 3.5 changes from Phase 3.0:
    - lookup="next_after_bar_close" (Path A data timing)
    - fill_mode → "next_session_open" set on the engine directly below
    """
    return StrategySpec.model_validate(
        {
            "schema_version": "1.0",
            "name": "QC AAPL Phase 3.5 trade-level parity",
            "symbols": ["AAPL"],
            "resolution": {"period_minutes": 1440},
            "indicators": [],
            "predictions": [
                {
                    "id": "qc_pred",
                    "prediction_set_id": _PREDICTION_SET_ID,
                    "field": "prediction",
                    "lookup": "next_after_bar_close",
                },
            ],
            "entry": {
                "logic": "AND",
                "conditions": [{"kind": "PredictionComparison", "prediction": "qc_pred", "op": ">", "value": 0.0}],
                "size": {"kind": "SetHoldings", "fraction": 1.0},
                "pyramiding": 1,
            },
            "position": {"kind": "EQUITY_LONG"},
            "survival": [],
            "exit": {
                "logic": "OR",
                "conditions": [{"kind": "PredictionComparison", "prediction": "qc_pred", "op": "<=", "value": 0.0}],
            },
        }
    )


def _build_our_fills(tmp_path: Path) -> list[OurFill]:
    """Run our engine directly (bypassing run_strategy_spec) and extract
    every fill from engine_result.order_events.

    Phase 3.5 scope: single entry fill on 2026-02-10 morning. No exit
    fires (position stays open at backtest end with all-positive predictions
    in the 2-day trailing-window-truncated window). We can't use strategy.trade_log
    because that only captures closed round-trips; we need every fill
    including the open-position entry. Bypassing run_strategy_spec is the
    cleanest path — runner integration is covered by
    test_runner_with_predictions.py.
    """
    artifacts_root = tmp_path / "predictions"
    artifacts_root.mkdir(parents=True, exist_ok=True)
    import_qc_fixture(
        qc_export_path=_QC_PREDICTIONS_FIXTURE,
        prediction_set_id=_PREDICTION_SET_ID,
        output_root=artifacts_root,
        symbol="AAPL",
        qc_tutorial_url=_QC_TUTORIAL_URL,
        qc_exported_at_ms=_QC_EXPORTED_AT_MS,
        qc_calendar_window_start_ms=_QC_WINDOW_START_MS,
        qc_calendar_window_end_ms=_QC_WINDOW_END_MS,
        qc_dataset_id=_QC_DATASET_ID,
        qc_versions=_QC_VERSIONS,
    )

    from decimal import Decimal as _D

    from app.engine.engine import BacktestEngine
    from app.engine.execution.fill_model import FillModel
    from app.engine.execution.order import Direction, FillMode
    from app.research.ml.loader import PredictionSet
    from app.research.parity.fixture_data_reader import FixtureDataReader
    from app.utils.timestamps import to_ms_utc

    # Load the prediction set directly.
    prediction_set = PredictionSet.load(artifacts_root / _PREDICTION_SET_ID)

    # Build the strategy and engine.
    spec = _aapl_spec()
    strategy = SpecAlgorithm(spec, prediction_set=prediction_set)

    # Monkey-patch initialize to override start/end dates and cash after
    # the default initialize logic runs (registers consolidator, builds
    # indicators etc). Same pattern the runner uses internally.
    orig_init = strategy.initialize

    def _patched_init() -> None:
        orig_init()
        strategy.set_start_date(2026, 2, 9)
        strategy.set_end_date(2026, 2, 12)
        strategy.set_cash(float(_INITIAL_CASH))

    strategy.initialize = _patched_init  # type: ignore[method-assign]

    data_source = FixtureDataReader(csv_path=_PRICES, symbol="AAPL")
    engine = BacktestEngine(
        data_source=data_source,
        fill_model=FillModel(
            mode=FillMode.NEXT_SESSION_OPEN,
            commission_per_order=_D("0"),
            slippage_per_share=_D("0"),
        ),
    )

    engine_result = engine.run(strategy)

    commission = IbkrEquityCommissionModel()
    fills: list[OurFill] = []
    for event in engine_result.order_events:
        side = "buy" if event.direction is Direction.LONG else "sell"
        fee = commission.fee(quantity=abs(event.fill_quantity), fill_price=event.fill_price)
        fills.append(
            OurFill(
                symbol=event.symbol,
                side=side,
                fill_qty=event.fill_quantity,
                fill_price=event.fill_price,
                fill_time_ms=to_ms_utc(event.time),
                fee=fee,
            )
        )
    return fills


def test_qc_aapl_phase3_trade_level_parity(tmp_path: Path) -> None:
    """Phase 3.5 acceptance gate.

    Single-fill scope: QC free tier's minute-data trailing window (~90 days,
    per https://www.quantconnect.com/forum/discussion/19781/getting-data-with-free-plan/)
    truncates the achievable backtest to the 2-day window 2026-02-09 → 2026-02-11.
    Result: 1 entry fill on 2026-02-10 morning, no exit (positive prediction
    every day in the window). Round-trip P&L coverage is not pursued
    (decision 2026-05-12 — see authority doc § 10 and the reconciliation
    report for the full rationale).

    The (R8) invariant validates that our engine's NEXT_SESSION_OPEN +
    PredictionRef.lookup="next_after_bar_close" produces the same fill
    as QC at the same minute (within bid-ask spread imprecision).
    """
    # Branch A guard: assert_fees=True is only valid when the fixture has
    # non-zero orderFeeAmount events. Catches a silent failure mode where a
    # future Branch B re-capture would change the meaning of this test
    # without surfacing the change.
    payload = json.loads(_ORDERS.read_text())
    has_nonzero_fee = any(
        event.get("orderFeeAmount") is not None and float(event["orderFeeAmount"]) != 0.0
        for order in payload["orders"]
        for event in order.get("events", [])
    )
    assert has_nonzero_fee, (
        f"Fixture at {_ORDERS} is Branch B (no non-zero fees); "
        f"assert_fees=True is invalid. Re-capture in Branch A mode "
        f"or set assert_fees=False explicitly."
    )

    our_fills = _build_our_fills(tmp_path)
    # Widened fill_price_atol = $0.10 to cover bid-ask spread imprecision:
    # QC fills at ASK; our engine fills at bar.open (last-trade approximation).
    # Default $0.01 is too tight for OHLC-bar-based engine vs QC's
    # bid/ask-aware simulator.
    #
    # qty_atol = 2 to cover SetHoldings rounding: our engine sizes off the
    # last daily-bar close (274.37) when the signal fires; QC sizes off the
    # actual fill price (273.24). Both are valid LEAN-consistent approaches;
    # the 1-share difference (364 vs 365) is within cash-buffer rounding.
    #
    # Both tolerances are documented in
    # docs/references/reconciliations/qc-aapl-phase3.md.
    tolerances = Tolerances(
        fill_price_atol=Decimal("0.10"),
        commission_atol=Decimal("0.01"),
        per_share_pnl_atol=Decimal("0.01"),
        pnl_floor_atol=Decimal("0.01"),
        qty_atol=2,
    )
    report = reconcile_qc_aapl_phase3(
        qc_orders_path=_ORDERS,
        qc_price_history_path=_PRICES,
        our_fills=our_fills,
        tolerances=tolerances,
        assert_fees=True,
    )

    # Render report unconditionally — green-run rendering helps reviewers
    # and makes the artifact self-documenting.
    _ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    (_ARTIFACTS_DIR / "qc-aapl-phase3-latest.md").write_text(report.render_markdown(), encoding="utf-8")

    assert report.status == "passed", (
        f"reconciliation failed; report written to {_ARTIFACTS_DIR / 'qc-aapl-phase3-latest.md'}"
    )

    # Pin the single aligned fill row. Values from first green run (2026-05-12).
    # QC: qty=365, price=273.238170408 (from qc_orders.json)
    # Ours: qty=364, price=273.178225656 (open of 09:32 Feb 10 bar; fill at
    #   second minute of day-2 per NEXT_SESSION_OPEN defer semantics)
    # Qty diff of 1 share is within qty_atol=2 (SetHoldings sizes off daily
    # close 274.37 while QC sizes off fill price 273.24).
    # Price diff = |273.238 - 273.178| = $0.06 < fill_price_atol=$0.10 (bid-ask spread).
    assert len(report.pairs) == 1, f"expected 1 aligned pair (single-fill Phase 3.5 scope); got {len(report.pairs)}"
    pair = report.pairs[0]
    assert pair.qc is not None and pair.ours is not None
    assert pair.trading_date == date(2026, 2, 10)
    assert pair.side == "buy"
    assert pair.qc.fill_qty == 365
    assert pair.qc.fill_price == Decimal("273.238170408")
    assert pair.ours.fill_qty == 364
    assert pair.ours.fill_price == Decimal("273.178225656")
