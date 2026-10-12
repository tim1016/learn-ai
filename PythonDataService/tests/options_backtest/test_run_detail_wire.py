"""What the page is sent of a run's trades, session and comparison: exact text, the ceiling on instants, and words tied
to the enums they come from (#3030).

The trades below are written by hand with the scale of every figure chosen on purpose, so the text is what was saved
and no replay arithmetic decides it.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import get_args

import pytest
from pydantic import ValidationError

from app.options.contract import OptionContract
from app.options_backtest.results import ExitReason, Fill, LegTrade, Mark
from app.options_backtest.saved_runs import StoredTrade
from app.schemas import options_backtest_run_detail as wire

D = Decimal
OPEN_MS = 1_791_379_800_000
CALL = OptionContract.of(root="SPY", expiry_date=date(2026, 10, 7), right="call", strike=D("500.5"))


def _sold() -> LegTrade:
    return LegTrade(
        leg_id="call",
        entry_number=1,
        contract=CALL,
        contracts=2,
        multiplier=100,
        starting_price=D("4.00"),
        momentum_level=D("5.0000"),
        stop_level=D("4.080"),
        entry_decided_at_ms=OPEN_MS + 600_000,
        entry=Fill("call", CALL, "buy", 2, 100, OPEN_MS + 660_000, D("5.1"), D("5.10")),
        exit_reason=ExitReason.STOP,
        exit_group=3,
        exit_decided_at_ms=OPEN_MS + 1_200_000,
        exit=Fill("call", CALL, "sell", 2, 100, OPEN_MS + 1_260_000, D("3.9"), D("3.90")),
        entry_fee_usd=D("1.30"),
        exit_fee_usd=D("1.30"),
        pnl_usd=D("-242.60"),
    )


def _marked() -> LegTrade:
    return LegTrade(
        leg_id="put",
        entry_number=0,
        contract=CALL,
        contracts=1,
        multiplier=100,
        starting_price=None,
        momentum_level=None,
        stop_level=None,
        entry_decided_at_ms=OPEN_MS + 300_000,
        entry=Fill("put", CALL, "buy", 1, 100, OPEN_MS + 360_000, D("2.00"), D("2.00")),
        exit_reason=ExitReason.MARKED_NOT_SOLD,
        exit_group=1,
        exit_decided_at_ms=OPEN_MS + 3_600_000,
        exit=Mark(OPEN_MS + 6_000_000, D("1.75")),
        entry_fee_usd=D("0.65"),
        exit_fee_usd=D("0"),
        pnl_usd=D("-25.65"),
    )


def test_a_sold_trade_is_sent_with_every_figure_as_the_text_it_was_saved_as_and_no_mark() -> None:
    sent = wire.OptionsBacktestTradeResponse.from_stored(StoredTrade(OPEN_MS, 4, _sold()))

    assert sent.model_dump() == {
        "session_open_ms": OPEN_MS,
        "trade_number": 4,
        "leg_id": "call",
        "entry_number": 1,
        "contract": {
            "symbol": "SPY261007C00500500",
            "underlying": "SPY",
            "right": "call",
            "strike_usd": "500.5",
            "expiry_ms": OPEN_MS + 390 * 60_000,
        },
        "contracts": 2,
        "multiplier": 100,
        "starting_price_usd": "4.00",
        "momentum_level_usd": "5.0000",
        "stop_level_usd": "4.080",
        "entry_decided_at_ms": OPEN_MS + 600_000,
        "entry": {"bar_close_ms": OPEN_MS + 660_000, "seen_usd": "5.1", "filled_usd": "5.10"},
        "exit_reason": "stop",
        "exit_group": 3,
        "exit_decided_at_ms": OPEN_MS + 1_200_000,
        "exit_kind": "sale",
        "sale": {"bar_close_ms": OPEN_MS + 1_260_000, "seen_usd": "3.9", "filled_usd": "3.90"},
        "mark": None,
        "entry_fee_usd": "1.30",
        "exit_fee_usd": "1.30",
        "pnl_usd": "-242.60",
    }


def test_a_leg_marked_and_not_sold_is_sent_as_a_mark_with_no_sale_and_no_exit_fee() -> None:
    sent = wire.OptionsBacktestTradeResponse.from_stored(StoredTrade(OPEN_MS, 0, _marked()))

    assert (sent.exit_kind, sent.sale, sent.exit_reason) == ("mark", None, "marked_not_sold")
    assert sent.mark == wire.OptionsBacktestTradeMarkResponse(bar_close_ms=OPEN_MS + 6_000_000, price_usd="1.75")
    assert (sent.starting_price_usd, sent.momentum_level_usd, sent.stop_level_usd) == (None, None, None)
    assert (sent.exit_fee_usd, sent.pnl_usd) == ("0", "-25.65")


def test_a_figure_is_written_in_plain_digits_whatever_form_the_decimal_holds_it_in() -> None:
    # 1E+1 is ten and 1E-7 is a tenth of a millionth: neither may reach the page in exponent form or through a float.
    trade = _sold()
    odd = LegTrade(
        **{
            **trade.__dict__,
            "starting_price": D("1E+1"),
            "momentum_level": D("1E-7"),
            "stop_level": D("0E-7"),
            "pnl_usd": D("-1.5E+3"),
        }
    )

    sent = wire.OptionsBacktestTradeResponse.from_stored(StoredTrade(OPEN_MS, 0, odd))

    assert (sent.starting_price_usd, sent.momentum_level_usd, sent.stop_level_usd, sent.pnl_usd) == (
        "10",
        "0.0000001",
        "0.0000000",
        "-1500",
    )


def test_a_trade_that_names_both_a_sale_and_a_mark_or_neither_cannot_be_made() -> None:
    sent = wire.OptionsBacktestTradeResponse.from_stored(StoredTrade(OPEN_MS, 0, _sold()))
    both = sent.model_dump() | {"mark": {"bar_close_ms": OPEN_MS, "price_usd": "1"}}
    neither = sent.model_dump() | {"sale": None}
    wrong_one = sent.model_dump() | {"exit_kind": "mark"}

    for broken in (both, neither, wrong_one):
        with pytest.raises(ValidationError):
            wire.OptionsBacktestTradeResponse.model_validate(broken)


def test_the_page_size_the_server_holds_is_one_the_response_can_carry() -> None:
    assert wire.TRADES_PAGE_DEFAULT <= wire.TRADES_PAGE_MAX
    with pytest.raises(ValidationError):
        wire.OptionsBacktestTradesResponse(run_id=1, total=0, offset=0, limit=wire.TRADES_PAGE_MAX + 1, trades=[])


@pytest.mark.parametrize(
    ("words", "members"),
    [(wire.ExitReasonWord, [member.value for member in ExitReason])],
)
def test_each_closed_set_a_trade_uses_is_exactly_the_members_of_the_enum_it_comes_from(
    words: object, members: list[str]
) -> None:
    assert list(get_args(words)) == members
