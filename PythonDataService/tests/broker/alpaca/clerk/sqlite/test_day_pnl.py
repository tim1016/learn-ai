"""The account-wide day-P&L fact the loss hold judges (ADR 0059 D4).

The broker supplies both sides of the valuation boundary: current equity and
equity at the prior regular-session close. The only local adjustment is the
signed cash Alpaca classifies as deposits or withdrawals after that close.
Every stamp is fixed ``int64 ms UTC``; nothing reads the wall clock.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from app.broker.alpaca.clerk.live_envelope import AccountObservation
from app.broker.alpaca.clerk.money import display_dollars
from app.broker.alpaca.clerk.sqlite.day_pnl import day_pnl_at, observed_day_pnl
from app.broker.contract.models import BrokerActivity
from app.lean_sidecar.trading_calendar import previous_completed_session_close_ms
from tests.broker.alpaca.clerk.sqlite.conftest import NOON

PNL_ATOL = 1e-9
PNL_RTOL = 0.0


def _observation(
    *, current_equity: float | Decimal = 95_000.0, prior_close_equity: float | Decimal | None = 100_000.0
) -> AccountObservation:
    return AccountObservation(
        observed_at_ms=NOON,
        broker_cash_usd=85_000.0,
        cash_available_usd=85_000.0,
        equity_usd=current_equity,
        last_equity_usd=prior_close_equity,
        position_count=1,
    )


def _cash_flow(
    activity_type: str,
    net_amount: float | None,
    *,
    occurred_at_ms: int | None = NOON,
) -> BrokerActivity:
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
        occurred_at_ms=occurred_at_ms,
        observed_at_ms=NOON,
    )


def test_day_pnl_is_current_equity_minus_prior_close_equity() -> None:
    pnl = day_pnl_at(observation=_observation(), cash_flows=[], now_ms=NOON)

    assert pnl.day_start_ms == previous_completed_session_close_ms(NOON)
    assert pnl.day_end_ms == NOON
    assert pnl.current_equity_usd == pytest.approx(
        95_000.0, abs=PNL_ATOL, rel=PNL_RTOL
    )
    assert pnl.prior_close_equity_usd == pytest.approx(
        100_000.0, abs=PNL_ATOL, rel=PNL_RTOL
    )
    assert pnl.net_cash_flow_usd == 0.0
    assert pnl.total_usd == pytest.approx(-5_000.0, abs=PNL_ATOL, rel=PNL_RTOL)
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
    assert pnl.net_cash_flow_usd == pytest.approx(
        net_amount, abs=PNL_ATOL, rel=PNL_RTOL
    )
    assert pnl.total_usd == pytest.approx(0.0, abs=PNL_ATOL, rel=PNL_RTOL)
    assert pnl.known


def test_a_transfer_after_friday_close_is_in_the_tuesday_post_holiday_window() -> None:
    prior_close_ms = previous_completed_session_close_ms(NOON)
    pnl = day_pnl_at(
        observation=_observation(current_equity=110_000.0),
        cash_flows=[
            _cash_flow(
                "CSD",
                10_000.0,
                occurred_at_ms=prior_close_ms + 1,
            )
        ],
        now_ms=NOON,
    )

    assert pnl.day_start_ms == prior_close_ms
    assert pnl.net_cash_flow_usd == pytest.approx(
        10_000.0, abs=PNL_ATOL, rel=PNL_RTOL
    )
    assert pnl.total_usd == pytest.approx(0.0, abs=PNL_ATOL, rel=PNL_RTOL)
    assert pnl.known


def test_after_hours_keeps_the_previous_trading_day_close_as_its_baseline() -> None:
    after_close_ms = NOON + 5 * 60 * 60 * 1_000
    prior_close_ms = previous_completed_session_close_ms(NOON)
    pnl = day_pnl_at(
        observation=_observation(current_equity=110_000.0),
        cash_flows=[_cash_flow("CSD", 10_000.0)],
        now_ms=after_close_ms,
    )

    assert pnl.day_start_ms == prior_close_ms
    assert pnl.net_cash_flow_usd == pytest.approx(
        10_000.0, abs=PNL_ATOL, rel=PNL_RTOL
    )
    assert pnl.total_usd == pytest.approx(0.0, abs=PNL_ATOL, rel=PNL_RTOL)
    assert pnl.known


@pytest.mark.parametrize(
    "activity",
    [
        pytest.param(_cash_flow("CSD", None), id="missing-amount"),
        pytest.param(_cash_flow("CSD", float("nan")), id="non-finite-amount"),
        pytest.param(_cash_flow("CSD", -10.0), id="deposit-with-negative-amount"),
        pytest.param(_cash_flow("CSD", 0.0), id="deposit-with-zero-amount"),
        pytest.param(_cash_flow("CSW", 10.0), id="withdrawal-with-positive-amount"),
        pytest.param(_cash_flow("CSW", 0.0), id="withdrawal-with-zero-amount"),
        pytest.param(_cash_flow("DIV", 10.0), id="unexpected-transfer-type"),
        pytest.param(_cash_flow("CSD", 10.0, occurred_at_ms=None), id="missing-occurred-at"),
        pytest.param(_cash_flow("CSD", 10.0, occurred_at_ms=1), id="before-prior-close"),
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


def test_the_owner_reads_the_exact_day_in_every_custody_world() -> None:
    """#2586: every recorded figure is normalized on its own, then subtracted exactly.

    Owner decision 2026-09-29: exact for all accounts. A real broker's floats
    100000.1 and 99999.9 are the recorded figures, so the day is exactly $0.2,
    not their float difference; simulated custody's exact equities, or an
    exact equity beside a float baseline, give the same. The loss rule's own
    figure is the float difference it always was.
    """
    days = [
        day_pnl_at(observation=_observation(current_equity=current, prior_close_equity=prior), cash_flows=[], now_ms=NOON)
        for current, prior in (
            (100_000.1, 99_999.9),
            (Decimal("100000.1"), Decimal("99999.9")),
            (Decimal("100000.1"), 99_999.9),
        )
    ]

    assert [day.display_total_usd for day in days] == [Decimal("0.2")] * 3
    assert [day.total_usd for day in days] == [0.20000000001164153] * 3
    assert all(day.known for day in days)


def test_a_sealed_float_baseline_beside_exact_equity_still_shows_the_exact_cent() -> None:
    """#2586: clearing a Shadow loss hold mixes exact equity with a sealed float.

    The clearance path re-reads the day against the float baseline the hold
    sealed, beside Shadow's exact equity. Equity of exactly
    $2,000.0049999999999999999 over a $1,999.99 baseline is exactly
    $0.0149999999999999999 -- 1 cent -- whichever type the baseline arrives
    as. Their float difference, 0.015000000000100044, shows 2 cents; the loss
    rule still judges that float.
    """
    session_start = previous_completed_session_close_ms(NOON)
    equity = Decimal("2000.0049999999999999999")
    shadow = replace(
        _observation(current_equity=equity, prior_close_equity=Decimal("1999.99")),
        simulation_session_start_ms=session_start, risk_equity_window_start_ms=session_start,
        risk_cash_flow_evidence_complete=True, risk_cash_flow_window_start_ms=0,
    )

    fresh = observed_day_pnl(observation=shadow, now_ms=NOON)
    retained = observed_day_pnl(
        observation=shadow, now_ms=NOON, retained_start_ms=session_start, retained_equity_usd=1999.99)

    assert fresh.known and retained.known
    assert retained.display_total_usd == fresh.display_total_usd == Decimal("0.0149999999999999999")
    assert display_dollars(retained.display_total_usd) == "0.01"
    assert retained.total_usd == fresh.total_usd == float(equity) - 1999.99 == 0.015000000000100044


def test_each_transfer_is_normalized_on_its_own_never_their_float_sum() -> None:
    """#2586: $3.30 of deposits is 1.10 + 2.20 as recorded, not 3.3000000000000003.

    Equity $10,004.015 over a $10,000.70 close with those two deposits is a
    day of exactly $0.015, shown $0.02 (half-even). The float formula gives
    0.014999999998690061 and normalizing the deposits' float sum gives
    0.0149999999999997 -- both show $0.01. The loss rule's figure is the
    float formula it always was.
    """
    pnl = day_pnl_at(
        observation=_observation(current_equity=10_004.015, prior_close_equity=10_000.7),
        cash_flows=[_cash_flow("CSD", 1.1), _cash_flow("CSD", 2.2)],
        now_ms=NOON,
    )

    assert pnl.known and pnl.cash_flow_count == 2
    assert pnl.display_total_usd == Decimal("0.015")
    assert display_dollars(pnl.display_total_usd) == "0.02"
    assert pnl.net_cash_flow_usd == 1.1 + 2.2 == 3.3000000000000003
    assert pnl.total_usd == 10_004.015 - 10_000.7 - (1.1 + 2.2) == 0.014999999998690061
