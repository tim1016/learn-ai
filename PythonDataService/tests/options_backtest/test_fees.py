"""The fee each policy charges a fill, and the dates the estimate can and cannot be made on.

The estimated fees are worked by hand from the rates the option fee model pins (``app/broker/alpaca/option_fees.py``):

- 2026-10-09: SEC 0.0000206 of a sale's value; TAF 0 (paused); CAT 0.000003 a share, so 0.0003 a contract of 100
  shares; ORF 0.015 a contract; OCC 0.025 a contract.
- 2026-09-30 and 2026-07-01: the same, with TAF 0.00329 a contract.
- Before 2026-07-01 there is no CAT rate; before 2026-01-01 there is no TAF rate and before 2026-02-12 no ORF or OCC
  rate; before 2024-05-22 there is no SEC rate.

Each fee type of each fill is rounded up to the cent, then the types are added.
"""

from __future__ import annotations

import pickle
import random
from datetime import date
from decimal import Context, Decimal, localcontext

import pytest

from app.broker.alpaca.option_fees import fees_for_option_fill, settle_option_session
from app.broker.contract.models import OrderSide
from app.options_backtest.fees import (
    FeeRateNotPublished,
    estimated_fees_available,
    fee_for_policy,
    unpublished_fee_types,
)
from app.options_backtest.results import Fill
from app.options_backtest.setup import FeePolicy
from app.utils.session_anchors import et_midnight_ms
from tests.options_backtest._run_data import D, contract_on

TEN_AM = 10 * 3_600_000
NOT_CHARGED = FeePolicy(kind="not_charged")
ESTIMATED = FeePolicy(kind="estimated")


def _at(day: date) -> int:
    """An instant on ``day`` in New York, ten in the morning."""
    return et_midnight_ms(day) + TEN_AM


def _fill(side: str, contracts: int, price: str, day: date = date(2026, 10, 9), *, multiplier: int = 100) -> Fill:
    return Fill(
        leg_id="leg",
        contract=contract_on(date(2026, 10, 9), "call", 500),
        side=side,  # type: ignore[arg-type]
        contracts=contracts,
        multiplier=multiplier,
        bar_close_ms=_at(day),
        seen=D(price),
        filled=D(price),
    )


def _per_contract(dollars: str) -> FeePolicy:
    return FeePolicy(kind="per_contract", per_contract_usd=D(dollars))


def test_a_policy_that_charges_nothing_charges_nothing_on_a_buy_or_a_sale() -> None:
    fee_for = fee_for_policy(NOT_CHARGED)

    assert fee_for(_fill("buy", 3, "2.00")) == D("0")
    assert fee_for(_fill("sell", 3, "2.00")) == D("0")


def test_a_per_contract_fee_is_charged_on_every_contract_of_a_buy_and_of_a_sale_alike() -> None:
    fee_for = fee_for_policy(_per_contract("0.65"))

    # 0.65 x 3 contracts.
    assert fee_for(_fill("buy", 3, "2.00")) == D("1.95")
    assert fee_for(_fill("sell", 3, "2.00")) == D("1.95")
    assert fee_for(_fill("buy", 1, "9.99")) == D("0.65")


def test_a_per_contract_fee_is_never_rounded_whatever_decimal_context_it_is_asked_in() -> None:
    fee_for = fee_for_policy(_per_contract("0.0125"))

    # 0.0125 x 7 = 0.0875, which a context of two digits would round.
    with localcontext(Context(prec=2)):
        assert fee_for(_fill("buy", 7, "2.00")) == D("0.0875")


@pytest.mark.parametrize(
    ("side", "contracts", "price", "expected"),
    [
        # Buy 2 at 1.50 on 2026-10-09: CAT 2 x 100 x 0.000003 = 0.0006 up to 0.01; ORF 2 x 0.015 = 0.03;
        # OCC 2 x 0.025 = 0.05; no SEC or TAF on a buy. 0.01 + 0.03 + 0.05 = 0.09.
        ("buy", 2, "1.50", "0.09"),
        # Sell 2 at 1.45: SEC 2 x 1.45 x 100 x 0.0000206 = 0.005974 up to 0.01; TAF 2 x 0 = 0; and the buy's
        # CAT 0.01, ORF 0.03, OCC 0.05. 0.01 + 0 + 0.01 + 0.03 + 0.05 = 0.10.
        ("sell", 2, "1.45", "0.10"),
        # Sell 10 at 12.00: SEC 10 x 12 x 100 x 0.0000206 = 0.2472 up to 0.25; TAF 0; CAT 10 x 100 x 0.000003 = 0.003
        # up to 0.01; ORF 10 x 0.015 = 0.15; OCC 10 x 0.025 = 0.25. 0.25 + 0 + 0.01 + 0.15 + 0.25 = 0.66.
        ("sell", 10, "12.00", "0.66"),
    ],
)
def test_an_estimated_fee_adds_each_fee_type_rounded_up_to_the_cent(
    side: str, contracts: int, price: str, expected: str
) -> None:
    assert fee_for_policy(ESTIMATED)(_fill(side, contracts, price)) == D(expected)


def test_an_estimated_fee_on_a_date_when_the_taf_is_live_includes_it() -> None:
    day = date(2026, 9, 30)
    fee_for = fee_for_policy(ESTIMATED)

    # Sell 3 at 2.00: SEC 3 x 2.00 x 100 x 0.0000206 = 0.01236 up to 0.02; TAF 3 x 0.00329 = 0.00987 up to 0.01;
    # CAT 3 x 100 x 0.000003 = 0.0009 up to 0.01; ORF 3 x 0.015 = 0.045 up to 0.05; OCC 3 x 0.025 = 0.075 up to 0.08.
    assert fee_for(_fill("sell", 3, "2.00", day)) == D("0.17")  # 0.02 + 0.01 + 0.01 + 0.05 + 0.08
    # The same three contracts bought owe CAT, ORF and OCC only: 0.01 + 0.05 + 0.08.
    assert fee_for(_fill("buy", 3, "2.00", day)) == D("0.14")


def test_the_first_day_with_every_rate_published_is_priced() -> None:
    # Sell 1 at 1.00 on 2026-07-01: SEC 100 x 0.0000206 = 0.00206 up to 0.01; TAF 0.00329 up to 0.01; CAT 0.0003
    # up to 0.01; ORF 0.015 up to 0.02; OCC 0.025 up to 0.03. 0.01 + 0.01 + 0.01 + 0.02 + 0.03 = 0.08.
    assert fee_for_policy(ESTIMATED)(_fill("sell", 1, "1.00", date(2026, 7, 1))) == D("0.08")


def test_a_sale_that_fills_at_nothing_owes_no_sec_fee_and_the_per_contract_ones_all_the_same() -> None:
    fee_for = fee_for_policy(ESTIMATED)

    # Sell 1 at 0.00 on 2026-10-09: SEC is a share of the sale's value, which is nothing; TAF 0; CAT 0.0003 up to
    # 0.01; ORF 0.015 up to 0.02; OCC 0.025 up to 0.03. 0 + 0 + 0.01 + 0.02 + 0.03 = 0.06.
    assert fee_for(_fill("sell", 1, "0.00")) == D("0.06")
    # At a cent the sale has a value, however small, and its SEC fee 100 x 0.01 x 0.0000206 = 0.0000206 rounds up to
    # 0.01: 0.07.
    assert fee_for(_fill("sell", 1, "0.01")) == D("0.07")
    assert fee_for(_fill("buy", 1, "0.00")) == D("0.06")


@pytest.mark.parametrize(
    ("side", "price", "expected"),
    [
        # Buy 1,000 contracts of 10 shares at 1.50 on 2026-10-09: CAT 1,000 x 10 x 0.000003 = 0.03 (a multiplier of 100
        # would make it 0.30); ORF 1,000 x 0.015 = 15.00; OCC 1,000 x 0.025 = 25.00. 0.03 + 15.00 + 25.00 = 40.03.
        ("buy", "1.50", "40.03"),
        # Sell them at 1.45: SEC 1,000 x 1.45 x 10 x 0.0000206 = 0.2987 up to 0.30 (a multiplier of 100 would make it
        # 2.987 up to 2.99); TAF 0; CAT 0.03; ORF 15.00; OCC 25.00. 0.30 + 0 + 0.03 + 15.00 + 25.00 = 40.33.
        ("sell", "1.45", "40.33"),
    ],
)
def test_the_contracts_own_multiplier_counts_in_the_fee_not_a_hundred_shares(
    side: str, price: str, expected: str
) -> None:
    fill = _fill(side, 1000, price, multiplier=10)

    assert fee_for_policy(ESTIMATED)(fill) == D(expected)


@pytest.mark.parametrize("price", ["0.01", "0.50", "1", "2", "7.25", "100"])
def test_only_the_sec_fee_depends_on_the_price_so_a_stand_in_price_for_a_sale_at_nothing_is_harmless(
    price: str,
) -> None:
    # The estimate for a sale at nothing asks the model at a stand-in price and sets SEC to nothing; that is
    # right only while no other fee type moves with the price. This holds the model to that.
    at = _at(date(2026, 9, 30))

    def accruals(premium: str):
        return fees_for_option_fill(
            trade_date_ms=at, side=OrderSide.SELL, contracts=D(3), premium=D(premium), multiplier=100
        )

    one, other = accruals("1"), accruals(price)
    assert (one.taf, one.cat, one.orf, one.occ) == (other.taf, other.cat, other.orf, other.occ)


def test_an_estimated_fee_does_not_depend_on_the_decimal_context_it_is_asked_in() -> None:
    with localcontext(Context(prec=3)):
        assert fee_for_policy(ESTIMATED)(_fill("sell", 2, "1.45")) == D("0.10")


@pytest.mark.parametrize(
    ("day", "types"),
    [
        (date(2026, 6, 30), ("cat",)),  # the last day without a CAT rate
        (date(2025, 12, 31), ("taf", "cat", "orf", "occ")),
        (date(2024, 1, 2), ("sec", "taf", "cat", "orf", "occ")),
        (date(2026, 7, 1), ()),
        (date(2026, 9, 30), ()),
        (date(2026, 10, 9), ()),
    ],
)
def test_the_model_is_asked_which_fee_types_it_cannot_price_on_a_date(day: date, types: tuple[str, ...]) -> None:
    assert unpublished_fee_types(day) == types
    assert estimated_fees_available(day) is (types == ())


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_a_fill_on_a_date_without_a_published_rate_raises_naming_the_date_and_the_fee_types(side: str) -> None:
    fee_for = fee_for_policy(ESTIMATED)

    with pytest.raises(FeeRateNotPublished) as raised:
        fee_for(_fill(side, 2, "1.50", date(2026, 6, 30)))

    assert raised.value.trading_date == date(2026, 6, 30)
    assert raised.value.components == ("cat",)
    assert "2026-06-30" in str(raised.value)


def test_a_fee_rate_that_is_not_published_survives_a_trip_between_processes_with_its_date_and_types() -> None:
    # A pickle is how an exception crosses a process boundary.
    original = FeeRateNotPublished(date(2025, 12, 31), ("taf", "cat"))

    restored = pickle.loads(pickle.dumps(original))

    assert isinstance(restored, FeeRateNotPublished)
    assert (restored.trading_date, restored.components) == (date(2025, 12, 31), ("taf", "cat"))
    assert str(restored) == str(original)
    assert "2025-12-31" in str(restored) and "taf, cat" in str(restored)


def test_a_buy_before_the_taf_began_names_only_what_a_buy_would_owe() -> None:
    # A buy owes no SEC fee and no TAF, so only CAT, ORF and OCC are missing for it; a sale also lacks the TAF.
    fee_for = fee_for_policy(ESTIMATED)

    with pytest.raises(FeeRateNotPublished) as bought:
        fee_for(_fill("buy", 1, "1.00", date(2025, 12, 31)))
    with pytest.raises(FeeRateNotPublished) as sold:
        fee_for(_fill("sell", 1, "1.00", date(2025, 12, 31)))

    assert bought.value.components == ("cat", "orf", "occ")
    assert sold.value.components == ("taf", "cat", "orf", "occ")


def _settled(fills: list[Fill]) -> Decimal:
    return settle_option_session(
        [
            fees_for_option_fill(
                trade_date_ms=fill.bar_close_ms,
                side=OrderSide.BUY if fill.side == "buy" else OrderSide.SELL,
                contracts=D(fill.contracts),
                premium=fill.filled,
                multiplier=fill.multiplier,
            )
            for fill in fills
        ]
    ).total


def _charged_per_fill(fills: list[Fill]) -> Decimal:
    fee_for = fee_for_policy(ESTIMATED)
    return sum((fee_for(fill) for fill in fills), start=D(0))


def test_a_buy_and_its_sale_cost_a_cent_more_charged_per_fill_than_settled_as_the_day() -> None:
    day = [_fill("buy", 2, "1.50"), _fill("sell", 2, "1.45")]

    # By fill: 0.09 + 0.10 = 0.19. Settled as a day: SEC 0.005974 up to 0.01; TAF 0; CAT 0.0006 + 0.0006 = 0.0012 up
    # to 0.01; ORF 0.03 + 0.03 = 0.06; OCC 0.05 + 0.05 = 0.10. 0.01 + 0 + 0.01 + 0.06 + 0.10 = 0.18. The extra cent is
    # CAT, which each fill rounded up and the day rounded once.
    assert _charged_per_fill(day) == D("0.19")
    assert _settled(day) == D("0.18")


def test_five_one_contract_buys_cost_eight_cents_more_charged_per_fill_than_settled_as_the_day() -> None:
    day = [_fill("buy", 1, "1.00") for _ in range(5)]

    # By fill each buy owes CAT 0.0003 up to 0.01, ORF 0.015 up to 0.02, OCC 0.025 up to 0.03: 0.06, five times 0.30.
    # As a day: CAT 5 x 0.0003 = 0.0015 up to 0.01; ORF 5 x 0.015 = 0.075 up to 0.08; OCC 5 x 0.025 = 0.125 up to
    # 0.13: 0.22.
    assert _charged_per_fill(day) == D("0.30")
    assert _settled(day) == D("0.22")


def test_a_lone_fill_costs_exactly_what_its_day_settles_to() -> None:
    day = [_fill("sell", 10, "12.00")]

    assert _charged_per_fill(day) == _settled(day) == D("0.66")


def test_over_any_day_per_fill_is_never_less_than_the_settlement_and_at_most_a_cent_a_fill_per_type_more() -> None:
    # Each fee type's sum of rounded-up parts is at least the rounded-up sum, and each part adds under a cent, so
    # n fills leave at most n - 1 cents over per type: five types, 5 x (n - 1) cents at the most.
    rng = random.Random(20261012)
    for trial in range(300):
        day = rng.choice([date(2026, 7, 1), date(2026, 9, 30), date(2026, 10, 9), date(2027, 3, 3)])
        fills = [
            _fill(
                rng.choice(["buy", "sell"]),
                rng.randint(1, 40),
                f"{rng.uniform(0.01, 30):.2f}",
                day,
            )
            for _ in range(rng.randint(1, 12))
        ]
        settled, per_fill = _settled(fills), _charged_per_fill(fills)
        assert settled <= per_fill <= settled + D("0.01") * 5 * (len(fills) - 1), f"trial {trial} on {day}"
