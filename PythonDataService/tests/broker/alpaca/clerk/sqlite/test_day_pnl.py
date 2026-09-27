"""The account-wide day-P&L fact the loss hold judges (ADR 0059 D4).

The broker supplies both sides of the valuation boundary: current equity and
equity at the prior regular-session close. The only local adjustment is the
signed cash Alpaca classifies as today's deposits or withdrawals. Every stamp
is fixed ``int64 ms UTC``; nothing reads the wall clock.
"""

from __future__ import annotations

import pytest

from app.broker.alpaca.clerk.et_day import et_day_window_ms
from app.broker.alpaca.clerk.live_envelope import AccountObservation
from app.broker.alpaca.clerk.sqlite.day_pnl import day_pnl_at
from app.broker.contract.models import BrokerActivity
from tests.broker.alpaca.clerk.sqlite.conftest import NOON


def _observation(
    *, current_equity: float = 95_000.0, prior_close_equity: float | None = 100_000.0
) -> AccountObservation:
    return AccountObservation(
        observed_at_ms=NOON,
        broker_cash_usd=85_000.0,
        cash_available_usd=85_000.0,
        equity_usd=current_equity,
        last_equity_usd=prior_close_equity,
        position_count=1,
    )


def _cash_flow(activity_type: str, net_amount: float | None) -> BrokerActivity:
    return BrokerActivity(
        broker="alpaca",
        activity_id=f"{activity_type}-{net_amount}",
        activity_type=activity_type,
        category="non_trade_activity",
        symbol=None,
        side=None,
        quantity=None,
        price=None,
        net_amount=net_amount,
        occurred_at_ms=NOON,
        observed_at_ms=NOON,
    )


def test_day_pnl_is_current_equity_minus_prior_close_equity() -> None:
    pnl = day_pnl_at(observation=_observation(), cash_flows=[], now_ms=NOON)

    assert (pnl.day_start_ms, pnl.day_end_ms) == et_day_window_ms(NOON)
    assert pnl.current_equity_usd == pytest.approx(95_000.0)
    assert pnl.prior_close_equity_usd == pytest.approx(100_000.0)
    assert pnl.net_cash_flow_usd == 0.0
    assert pnl.total_usd == pytest.approx(-5_000.0)
    assert pnl.known


@pytest.mark.parametrize(
    ("current_equity", "activity_type", "net_amount"),
    [
        pytest.param(110_000.0, "CSD", 10_000.0, id="deposit"),
        pytest.param(90_000.0, "CSW", -10_000.0, id="withdrawal"),
    ],
)
def test_same_day_deposits_and_withdrawals_are_not_pnl(
    current_equity: float,
    activity_type: str,
    net_amount: float,
) -> None:
    pnl = day_pnl_at(
        observation=_observation(current_equity=current_equity),
        cash_flows=[_cash_flow(activity_type, net_amount)],
        now_ms=NOON,
    )

    assert pnl.cash_flow_count == 1
    assert pnl.net_cash_flow_usd == pytest.approx(net_amount)
    assert pnl.total_usd == pytest.approx(0.0)
    assert pnl.known


@pytest.mark.parametrize(
    "activity",
    [
        pytest.param(_cash_flow("CSD", None), id="missing-amount"),
        pytest.param(_cash_flow("CSD", float("nan")), id="non-finite-amount"),
        pytest.param(_cash_flow("DIV", 10.0), id="unexpected-transfer-type"),
    ],
)
def test_incomplete_cash_flow_evidence_makes_the_fact_unknown(
    activity: BrokerActivity,
) -> None:
    pnl = day_pnl_at(
        observation=_observation(),
        cash_flows=[activity],
        now_ms=NOON,
    )

    assert not pnl.known


def test_a_missing_prior_close_baseline_cannot_produce_day_pnl() -> None:
    with pytest.raises(ValueError, match="prior-close equity"):
        day_pnl_at(
            observation=_observation(prior_close_equity=None),
            cash_flows=[],
            now_ms=NOON,
        )
