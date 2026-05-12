"""Phase 3 acceptance: AAPL single-symbol trade-level parity vs QC.

Skipped on master until ``tests/fixtures/golden/qc-aapl-phase3/`` is
committed. The fixture-landing PR (#219) supplies the QC artifacts; this
test activates automatically once they're present.

``_build_our_fills`` imports the PR #215 prediction set into a temp
artifact root, runs the canonical AAPL ``StrategySpec`` through
``run_strategy_spec`` with ``fill_mode="next_bar_open"`` and
``commission_per_order=0``, and adapts each ``LoggedTrade`` round-trip
into two ``OurFill`` records (entry buy + exit sell) using
``IbkrEquityCommissionModel`` for per-fill fees.

See ``docs/superpowers/specs/2026-05-11-phase3-pnl-parity-design.md`` §3.
"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.engine.strategy.spec import StrategySpec
from app.research.ml.generators.quantconnect_fixture import import_qc_fixture
from app.research.parity.fixture_data_reader import fixture_data_source_factory
from app.research.parity.ibkr_commission import IbkrEquityCommissionModel
from app.research.parity.qc_reconciler import (
    OurFill,
    Tolerances,
    reconcile_qc_aapl_phase3,
)
from app.research.runs.runner import RunRequest, run_strategy_spec

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
    """Single-symbol AAPL degenerate of QC's full ranking algorithm.

    Mirrors design spec §2.3 verbatim: long entry when ``qc_pred > 0``,
    flat exit when ``qc_pred <= 0``, ``SetHoldings(1.0)`` sizing.
    """
    return StrategySpec.model_validate(
        {
            "schema_version": "1.0",
            "name": "QC AAPL Phase 3 trade-level parity",
            "symbols": ["AAPL"],
            "resolution": {"period_minutes": 1440},
            "indicators": [],
            "predictions": [
                {
                    "id": "qc_pred",
                    "prediction_set_id": _PREDICTION_SET_ID,
                    "field": "prediction",
                },
            ],
            "entry": {
                "logic": "AND",
                "conditions": [
                    {
                        "kind": "PredictionComparison",
                        "prediction": "qc_pred",
                        "op": ">",
                        "value": 0.0,
                    },
                ],
                "size": {"kind": "SetHoldings", "fraction": 1.0},
                "pyramiding": 1,
            },
            "position": {"kind": "EQUITY_LONG"},
            "survival": [],
            "exit": {
                "logic": "OR",
                "conditions": [
                    {
                        "kind": "PredictionComparison",
                        "prediction": "qc_pred",
                        "op": "<=",
                        "value": 0.0,
                    },
                ],
            },
        }
    )


def _build_our_fills(tmp_path: Path) -> list[OurFill]:
    """Replay the AAPL spec through ``run_strategy_spec`` and adapt trades to fills.

    Steps:
    1. Import the QC predictions fixture (PR #215) into a temp artifact root.
    2. Point the runner at that root via ``LEARN_AI_PREDICTION_ARTIFACTS_ROOT``.
    3. Build the AAPL spec and run with ``fill_mode="next_bar_open"``,
       ``commission_per_order=0`` (fees are computed reconciler-side).
    4. Convert each ``LoggedTrade`` round-trip into two ``OurFill`` records,
       sizing positions via the canonical ``SetHoldings(1.0)`` rule
       (``qty = floor(initial_cash / entry_price)``).
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

    # Point the runner at our temp prediction-set root so it loads our
    # imported set rather than the production artifacts dir.
    previous_root = os.environ.get("LEARN_AI_PREDICTION_ARTIFACTS_ROOT")
    os.environ["LEARN_AI_PREDICTION_ARTIFACTS_ROOT"] = str(artifacts_root)
    try:
        request = RunRequest(
            spec=_aapl_spec(),
            # Window covers the QC fill date plus one prior bar (for warmup)
            # and one trailing bar (so NEXT_BAR_OPEN has a target if a signal
            # fires on the QC fill date).
            start_date=date(2026, 2, 9),
            end_date=date(2026, 2, 12),
            initial_cash=float(_INITIAL_CASH),
            fill_mode="next_bar_open",
            commission_per_order=0.0,
        )
        factory = fixture_data_source_factory(_PRICES, symbol="AAPL")
        _ledger, result = run_strategy_spec(
            request,
            data_source_factory=factory,
            data_root_revision="qc-aapl-phase3-fixture",
        )
    finally:
        if previous_root is None:
            os.environ.pop("LEARN_AI_PREDICTION_ARTIFACTS_ROOT", None)
        else:
            os.environ["LEARN_AI_PREDICTION_ARTIFACTS_ROOT"] = previous_root

    commission = IbkrEquityCommissionModel()
    fills: list[OurFill] = []
    for trade in result.trades:
        entry_price = Decimal(str(trade.entry_price))
        exit_price = Decimal(str(trade.exit_price))
        # Canonical SetHoldings(1.0) sizing: integer shares such that
        # qty × entry_price ≤ initial_cash. Matches the simplest engine
        # path; QC may apply a small cash-buffer factor that diverges by
        # 1-2 shares — that surfaces as QUANTITY_MISMATCH, not a silent
        # discrepancy.
        qty = int(_INITIAL_CASH / entry_price)
        fills.append(
            OurFill(
                symbol="AAPL",
                side="buy",
                fill_qty=qty,
                fill_price=entry_price,
                fill_time_ms=trade.entry_time_ms,
                fee=commission.fee(quantity=qty, fill_price=entry_price),
            )
        )
        fills.append(
            OurFill(
                symbol="AAPL",
                side="sell",
                fill_qty=-qty,
                fill_price=exit_price,
                fill_time_ms=trade.exit_time_ms,
                fee=commission.fee(quantity=qty, fill_price=exit_price),
            )
        )
    return fills


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Phase 3.0 fixture is single-day (2026-02-10 only); QC fills intraday at "
        "09:31 ET while our engine's NEXT_BAR_OPEN fills at the next daily bar's "
        "open. The resulting DECISION_MISMATCH on (buy, 2026-02-10) is expected "
        "and validates the reconciler pipeline end-to-end. Phase 3.5 will close "
        "the acceptance gate via a multi-day fixture + intraday-trigger fill "
        "mode. See docs/references/reconciliations/qc-aapl-phase3.md."
    ),
)
def test_qc_aapl_phase3_trade_level_parity(tmp_path: Path, write_recon_report: bool) -> None:
    our_fills = _build_our_fills(tmp_path)
    report = reconcile_qc_aapl_phase3(
        qc_orders_path=_ORDERS,
        qc_price_history_path=_PRICES,
        our_fills=our_fills,
        tolerances=Tolerances.phase3_default(),
        # Branch A: qc_orders.json carries non-zero orderFeeAmount values.
        assert_fees=True,
    )
    if report.status != "passed" or write_recon_report:
        _ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
        (_ARTIFACTS_DIR / "qc-aapl-phase3-latest.md").write_text(report.render_markdown())
    assert report.status == "passed", (
        f"reconciliation failed; report written to {_ARTIFACTS_DIR / 'qc-aapl-phase3-latest.md'}"
    )
