"""C3 reconciliation contract tests (#1469)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.broker.alpaca.clerk.sqlite.economic_projection_models import (
    AccountPnlAttribution,
    ExecutionCoverage,
    FeeFidelity,
)
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


def _exact(value: float | None) -> Decimal | None:
    """A literal float fixture as the exact total C2 carries."""
    return None if value is None else Decimal(str(value))


def _attribution(
    *,
    realized_pnl_total: float,
    start_open_pnl_total: float | None = 0.0,
    open_pnl_total: float | None = 0.0,
    fee_total: float | None = 0.0,
    fee_fidelity: FeeFidelity = "reported",
    execution_coverage: ExecutionCoverage = "complete",
    marks_complete: bool = True,
    start_mark_observed_at_ms: dict[str, int] | None = None,
    mark_observed_at_ms: dict[str, int] | None = None,
) -> AccountPnlAttribution:
    return AccountPnlAttribution(
        account_id="PA-C3",
        authority_generation=1,
        control_revision=2,
        from_ms=1_700_000_000_000,
        to_ms=1_700_000_001_000,
        attribution_rows=(),
        exact_realized_pnl_total=_exact(realized_pnl_total),
        exact_start_open_pnl_total=_exact(start_open_pnl_total),
        exact_open_pnl_total=_exact(open_pnl_total),
        fee_total=fee_total,
        fee_fidelity=fee_fidelity,
        execution_coverage=execution_coverage,
        marks_complete=marks_complete,
        start_mark_observed_at_ms=start_mark_observed_at_ms or {},
        mark_observed_at_ms=mark_observed_at_ms or {},
    )


@pytest.mark.parametrize(
    ("broker_delta", "residual", "within_tolerance", "categories"),
    [
        pytest.param(25.0, 0.0, True, (), id="matching-flat-books-pass"),
        pytest.param(
            25.5,
            0.5,
            False,
            (DivergenceCategory.PNL_DRIFT,),
            id="injected-delta-is-classified-as-pnl-drift",
        ),
    ],
)
def test_reconcile_broker_curve_to_local_pnl_flat_books_pass_or_drift(
    broker_delta: float,
    residual: float,
    within_tolerance: bool,
    categories: tuple[DivergenceCategory, ...],
) -> None:
    result = reconcile_broker_curve_to_local_pnl(
        _history([10_000.0, 10_000.0 + broker_delta]),
        _attribution(realized_pnl_total=25.0),
    )

    assert result.broker_delta == pytest.approx(
        broker_delta, abs=ACCOUNT_PNL_RECONCILIATION_ATOL, rel=ACCOUNT_PNL_RECONCILIATION_RTOL
    ), "broker delta is the curve's last equity minus its first"
    assert result.local_delta == pytest.approx(
        25.0, abs=ACCOUNT_PNL_RECONCILIATION_ATOL, rel=ACCOUNT_PNL_RECONCILIATION_RTOL
    ), "local delta is the flat book's realized P&L"
    assert result.residual == pytest.approx(
        residual, abs=ACCOUNT_PNL_RECONCILIATION_ATOL, rel=ACCOUNT_PNL_RECONCILIATION_RTOL
    ), "residual is broker delta minus local delta"
    assert result.within_tolerance is within_tolerance, "within_tolerance follows the residual"
    assert result.atol == ACCOUNT_PNL_RECONCILIATION_ATOL, "the result reports its absolute tolerance"
    assert result.rtol == ACCOUNT_PNL_RECONCILIATION_RTOL, "the result reports its relative tolerance"
    assert tuple(item.category for item in result.divergences) == categories, "divergence categories"


@pytest.mark.parametrize(
    ("mark_observed_at_ms", "within_tolerance", "categories"),
    [
        pytest.param(1_700_000_001_000, True, (), id="includes-complete-open-pnl"),
        pytest.param(1_700_000_001_001, True, (), id="accepts-mark-in-same-timeframe-bucket"),
        pytest.param(
            1_700_086_401_000,
            False,
            (DivergenceCategory.FIXTURE_INSUFFICIENT,),
            id="refuses-mark-in-later-timeframe-bucket",
        ),
    ],
)
def test_reconcile_broker_curve_to_local_pnl_judges_open_pnl_marks_by_timeframe_bucket(
    mark_observed_at_ms: int,
    within_tolerance: bool,
    categories: tuple[DivergenceCategory, ...],
) -> None:
    result = reconcile_broker_curve_to_local_pnl(
        _history([10_000.0, 10_030.0]),
        _attribution(
            realized_pnl_total=25.0,
            open_pnl_total=5.0,
            mark_observed_at_ms={"SPY": mark_observed_at_ms},
        ),
    )

    assert result.local_delta == pytest.approx(
        30.0,
        abs=ACCOUNT_PNL_RECONCILIATION_ATOL,
        rel=ACCOUNT_PNL_RECONCILIATION_RTOL,
    ), "local delta includes the complete open P&L"
    assert result.within_tolerance is within_tolerance, "within_tolerance holds only when each mark falls in its timeframe bucket"
    assert tuple(item.category for item in result.divergences) == categories, "divergence categories"


def test_reconcile_broker_curve_to_local_pnl_subtracts_start_open_pnl_and_fees() -> None:
    result = reconcile_broker_curve_to_local_pnl(
        _history([10_000.0, 10_004.0]),
        _attribution(
            realized_pnl_total=3.0,
            start_open_pnl_total=10.0,
            open_pnl_total=12.0,
            fee_total=1.0,
            start_mark_observed_at_ms={"SPY": 1_700_000_000_001},
            mark_observed_at_ms={"SPY": 1_700_000_001_001},
        ),
    )

    assert result.local_delta == pytest.approx(4.0)
    assert result.within_tolerance is True


@pytest.mark.parametrize(
    ("fee_total", "fee_fidelity", "execution_coverage", "detail_fragment"),
    [
        pytest.param(None, "not_reported", "complete", "fees", id="refuses-unknown-fees"),
        pytest.param(
            0.0,
            "reported",
            "incomplete",
            "complete broker account",
            id="refuses-incomplete-execution-coverage",
        ),
    ],
)
def test_reconcile_broker_curve_to_local_pnl_refuses_a_book_that_cannot_prove_the_curve(
    fee_total: float | None,
    fee_fidelity: FeeFidelity,
    execution_coverage: ExecutionCoverage,
    detail_fragment: str,
) -> None:
    result = reconcile_broker_curve_to_local_pnl(
        _history([10_000.0, 10_025.0]),
        _attribution(
            realized_pnl_total=25.0,
            fee_total=fee_total,
            fee_fidelity=fee_fidelity,
            execution_coverage=execution_coverage,
        ),
    )

    assert result.within_tolerance is False, "the curve is not proven"
    assert (
        result.divergences[0].category is DivergenceCategory.FIXTURE_INSUFFICIENT
    ), "the refusal is classified FIXTURE_INSUFFICIENT"
    assert detail_fragment in result.divergences[0].detail, "the detail names what is missing"


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
