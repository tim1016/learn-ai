"""Phase 3 acceptance: AAPL single-symbol trade-level parity vs QC.

Skipped on master until ``tests/fixtures/golden/qc-aapl-phase3/`` is
committed. When the fixture lands, ``_build_our_fills`` must be
implemented to:

1. Load the QC prediction set captured in PR #215 (``qc_aapl_gbm_v001``).
2. Build the single-symbol AAPL ``StrategySpec`` from the design spec §2.3.
3. Call ``run_strategy_spec`` with ``fill_mode="next_bar_open"`` and
   ``commission_per_order=0`` against the fixture's CSV.
4. Normalize the resulting trade log into ``[OurFill]`` (one per
   open-order side, with ``trading_date`` set from the engine's fill
   time and ``fee`` set to ``IbkrEquityCommissionModel.fee(...)`` of
   that fill).

This test is committed in the same PR that ships the reconciler so the
acceptance contract is enforced from day 1; only ``_build_our_fills``
remains for the fixture-landing PR.

See ``docs/superpowers/specs/2026-05-11-phase3-pnl-parity-design.md`` §3, §5.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.research.parity.qc_reconciler import (
    OurFill,
    Tolerances,
    reconcile_qc_aapl_phase3,
)

_FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "golden" / "qc-aapl-phase3"
_ORDERS = _FIXTURE_DIR / "qc_orders.json"
_PRICES = _FIXTURE_DIR / "qc_price_history.csv"
_ARTIFACTS_DIR = Path(__file__).resolve().parents[3] / "artifacts" / "reconciliations"


pytestmark = pytest.mark.skipif(
    not _ORDERS.is_file(),
    reason="Phase 3 QC fixture not yet captured (see capture-smoke test)",
)


def _build_our_fills() -> list[OurFill]:
    """Replay AAPL spec through ``run_strategy_spec`` and adapt trades to fills.

    Filled in by the fixture-landing PR; depends on the captured
    prediction-set window matching the orders window.
    """
    raise NotImplementedError(
        "Phase 3 fixture capture must precede acceptance-test implementation. "
        "See docs/superpowers/specs/2026-05-11-phase3-pnl-parity-design.md §2.3."
    )


def test_qc_aapl_phase3_trade_level_parity(write_recon_report: bool) -> None:
    our_fills = _build_our_fills()
    report = reconcile_qc_aapl_phase3(
        qc_orders_path=_ORDERS,
        qc_price_history_path=_PRICES,
        our_fills=our_fills,
        tolerances=Tolerances.phase3_default(),
        # Flip to True only on Branch A after capture-smoke confirms QC
        # records non-zero orderFeeAmount values.
        assert_fees=False,
    )
    if report.status != "passed" or write_recon_report:
        _ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
        (_ARTIFACTS_DIR / "qc-aapl-phase3-latest.md").write_text(report.render_markdown())
    assert report.status == "passed", (
        f"reconciliation failed; report written to {_ARTIFACTS_DIR / 'qc-aapl-phase3-latest.md'}"
    )
