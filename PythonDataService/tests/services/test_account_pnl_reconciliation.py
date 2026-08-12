"""C3 reconciliation contract tests (#1469)."""

from __future__ import annotations

import pytest

from app.broker.alpaca.clerk.sqlite.economic_projection_models import AccountPnlAttribution
from app.broker.contract.models import BrokerPortfolioHistory
from app.research.parity.qc_reconciler import DivergenceCategory
from app.services.account_pnl_reconciliation import (
    ACCOUNT_PNL_RECONCILIATION_ATOL,
    ACCOUNT_PNL_RECONCILIATION_RTOL,
    reconcile_broker_curve_to_local_pnl,
)


def _history(equity: list[float]) -> BrokerPortfolioHistory:
    return BrokerPortfolioHistory(
        timestamps=[1_700_000_000_000 + index * 1_000 for index in range(len(equity))],
        equity=equity,
        profit_loss=[value - equity[0] for value in equity] if equity else [],
        base_value=equity[0] if equity else None,
        timeframe="1D",
    )


def _attribution(
    *,
    realized_pnl_total: float,
    open_pnl_total: float | None = 0.0,
    marks_complete: bool = True,
    mark_observed_at_ms: dict[str, int] | None = None,
) -> AccountPnlAttribution:
    return AccountPnlAttribution(
        account_id="PA-C3",
        authority_generation=1,
        control_revision=2,
        from_ms=1_700_000_000_000,
        to_ms=1_700_000_001_000,
        attribution_rows=(),
        realized_pnl_total=realized_pnl_total,
        open_pnl_total=open_pnl_total,
        marks_complete=marks_complete,
        mark_observed_at_ms=mark_observed_at_ms or {},
    )


def test_reconcile_broker_curve_to_local_pnl_matching_flat_books_passes() -> None:
    result = reconcile_broker_curve_to_local_pnl(
        _history([10_000.0, 10_025.0]),
        _attribution(realized_pnl_total=25.0),
    )

    assert result.broker_delta == pytest.approx(
        25.0, abs=ACCOUNT_PNL_RECONCILIATION_ATOL, rel=ACCOUNT_PNL_RECONCILIATION_RTOL
    )
    assert result.local_delta == pytest.approx(
        25.0, abs=ACCOUNT_PNL_RECONCILIATION_ATOL, rel=ACCOUNT_PNL_RECONCILIATION_RTOL
    )
    assert result.residual == pytest.approx(
        0.0, abs=ACCOUNT_PNL_RECONCILIATION_ATOL, rel=ACCOUNT_PNL_RECONCILIATION_RTOL
    )
    assert result.within_tolerance is True
    assert result.atol == ACCOUNT_PNL_RECONCILIATION_ATOL
    assert result.rtol == ACCOUNT_PNL_RECONCILIATION_RTOL
    assert result.divergences == ()


def test_reconcile_broker_curve_to_local_pnl_classifies_injected_delta_as_pnl_drift() -> None:
    result = reconcile_broker_curve_to_local_pnl(
        _history([10_000.0, 10_025.5]),
        _attribution(realized_pnl_total=25.0),
    )

    assert result.residual == pytest.approx(
        0.5, abs=ACCOUNT_PNL_RECONCILIATION_ATOL, rel=ACCOUNT_PNL_RECONCILIATION_RTOL
    )
    assert result.within_tolerance is False
    assert [item.category for item in result.divergences] == [DivergenceCategory.PNL_DRIFT]


def test_reconcile_broker_curve_to_local_pnl_includes_complete_open_pnl() -> None:
    result = reconcile_broker_curve_to_local_pnl(
        _history([10_000.0, 10_030.0]),
        _attribution(
            realized_pnl_total=25.0,
            open_pnl_total=5.0,
            mark_observed_at_ms={'SPY': 1_700_000_001_000},
        ),
    )

    assert result.local_delta == pytest.approx(
        30.0,
        abs=ACCOUNT_PNL_RECONCILIATION_ATOL,
        rel=ACCOUNT_PNL_RECONCILIATION_RTOL,
    )
    assert result.within_tolerance is True


def test_reconcile_broker_curve_to_local_pnl_refuses_mark_from_different_instant() -> None:
    result = reconcile_broker_curve_to_local_pnl(
        _history([10_000.0, 10_030.0]),
        _attribution(
            realized_pnl_total=25.0,
            open_pnl_total=5.0,
            mark_observed_at_ms={'SPY': 1_700_000_001_001},
        ),
    )

    assert result.within_tolerance is False
    assert [item.category for item in result.divergences] == [DivergenceCategory.FIXTURE_INSUFFICIENT]


def test_reconcile_broker_curve_to_local_pnl_refuses_unmarked_open_lots_as_proof() -> None:
    result = reconcile_broker_curve_to_local_pnl(
        _history([10_000.0, 10_025.0]),
        _attribution(realized_pnl_total=25.0, open_pnl_total=None, marks_complete=False),
    )

    assert result.within_tolerance is False
    assert [item.category for item in result.divergences] == [DivergenceCategory.FIXTURE_INSUFFICIENT]


def test_reconcile_broker_curve_to_local_pnl_classifies_empty_curve_as_fixture_insufficient() -> None:
    result = reconcile_broker_curve_to_local_pnl(
        _history([]),
        _attribution(realized_pnl_total=0.0),
    )

    assert result.broker_delta == pytest.approx(
        0.0, abs=ACCOUNT_PNL_RECONCILIATION_ATOL, rel=ACCOUNT_PNL_RECONCILIATION_RTOL
    )
    assert result.within_tolerance is False
    assert [item.category for item in result.divergences] == [DivergenceCategory.FIXTURE_INSUFFICIENT]
