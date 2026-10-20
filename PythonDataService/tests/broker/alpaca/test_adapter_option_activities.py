"""Alpaca's assignment, exercise and expiry activities → typed option events, and the read that walks them.

The rows are Alpaca's published examples
(``tests/fixtures/alpaca_documented/option_activities``): documentation, not
recordings. No account has been assigned, has exercised or has let an option
expire under this code, so nothing here shows what Alpaca's API sends.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.broker.alpaca.adapter import alpaca_option_activity_type, from_alpaca_option_activity
from app.broker.alpaca.broker import AlpacaBroker
from app.broker.contract.errors import BrokerEvidenceUnavailable
from app.broker.contract.option_events import (
    BrokerOptionEventActivity,
    BrokerOptionEventResume,
    BrokerOptionShareTradeActivity,
    OptionActivityStatus,
    share_trade_disagreements,
)
from app.options.contract import OptionContract
from app.options.event_allocation import OptionEventKind, expected_share_trade
from app.utils.session_anchors import et_midnight_ms
from tests.broker.alpaca.conftest import load_alpaca_fixture_file
from tests.broker.alpaca.documented_examples import documented

_OBSERVED = 1_700_000_000_000
_PAGE = 100


def _examples() -> dict[str, dict[str, list[dict[str, Any]]]]:
    return copy.deepcopy(documented("option_activities", "documented_activities.json"))


def _documented_types() -> dict[str, list[str]]:
    return documented("option_activities", "documented_activity_types.json")


def _row(product: str, heading: str, index: int = 0, **changes: Any) -> dict[str, Any]:
    """One documented row, with ``changes`` laid over it; a change of ``...`` removes the field."""
    row = _examples()[product][heading][index]
    for field, value in changes.items():
        if value is ...:
            row.pop(field, None)
        else:
            row[field] = value
    return row


def _exercise(**changes: Any) -> dict[str, Any]:
    return _row("trading_api", "option_exercise", **changes)


def _assignment(**changes: Any) -> dict[str, Any]:
    return _row("trading_api", "option_assignment", **changes)


def _expiry(**changes: Any) -> dict[str, Any]:
    return _row("trading_api", "otm_option_expiry", **changes)


def _share_trade(**changes: Any) -> dict[str, Any]:
    return _row("trading_api", "option_assignment", 1, **changes)


_EVERY_DOCUMENTED_ROW = {
    f"{product}/{heading}/{index}": row
    for product, examples in _examples().items()
    for heading, rows in examples.items()
    for index, row in enumerate(rows)
}


# ── The documented rows, field by field ─────────────────────────────────────


def test_the_documented_exercise_maps_every_field() -> None:
    event = from_alpaca_option_activity(_exercise(), observed_at_ms=_OBSERVED)

    assert isinstance(event, BrokerOptionEventActivity)
    assert event.broker == "alpaca"
    assert event.activity_id == "20190801011955195::5f596936-6f23-4cef-bdf1-3806aae57dbf"
    assert event.activity_type == "OPEXC"
    assert event.kind is OptionEventKind.EXERCISED
    assert event.status is OptionActivityStatus.EXECUTED
    # A date becomes 00:00 New York time on it, once, here.
    assert event.activity_date_ms == et_midnight_ms(date(2023, 7, 21))
    # The contract is parsed from the symbol, and spells back to it.
    assert event.contract == OptionContract.from_occ_symbol("AAPL230721C00150000")
    assert event.contract.occ_symbol == "AAPL230721C00150000"
    # Two bought contracts taken from the account.
    assert event.position_change == -2
    assert event.contracts == 2
    assert event.net_amount == Decimal("0")
    assert event.group_id is None
    assert event.created_at_ms is None
    assert event.observed_at_ms == _OBSERVED


def test_the_documented_assignment_takes_from_a_sold_position() -> None:
    event = from_alpaca_option_activity(_assignment(), observed_at_ms=_OBSERVED)

    assert isinstance(event, BrokerOptionEventActivity)
    assert event.activity_type == "OPASN"
    assert event.kind is OptionEventKind.ASSIGNED
    assert event.activity_date_ms == et_midnight_ms(date(2023, 7, 1))
    assert event.contract.occ_symbol == "AAPL230721C00150000"
    assert event.position_change == 2
    assert event.contracts == 2
    assert event.net_amount == Decimal("0")


def test_the_documented_expiry_removes_a_bought_position() -> None:
    event = from_alpaca_option_activity(_expiry(), observed_at_ms=_OBSERVED)

    assert isinstance(event, BrokerOptionEventActivity)
    assert event.activity_type == "OPEXP"
    assert event.kind is OptionEventKind.EXPIRED
    assert event.activity_date_ms == et_midnight_ms(date(2023, 7, 21))
    assert event.position_change == -2
    assert event.contracts == 2


def test_the_documented_share_trade_names_the_underlying_with_shares_price_and_cash() -> None:
    trade = from_alpaca_option_activity(_share_trade(), observed_at_ms=_OBSERVED)

    assert isinstance(trade, BrokerOptionShareTradeActivity)
    assert trade.broker == "alpaca"
    assert trade.activity_id == "20190801011955195::5f596936-6f23-4cef-bdf1-3806aae57dbf"
    assert trade.activity_type == "OPTRD"
    assert trade.status is OptionActivityStatus.EXECUTED
    assert trade.activity_date_ms == et_midnight_ms(date(2023, 7, 1))
    # The stock, not the contract.
    assert trade.underlying_symbol == "AAPL"
    # 200 shares delivered at $150 for $30,000 received.
    assert trade.shares == -200
    assert trade.price == Decimal("150")
    assert trade.net_amount == Decimal("30000")
    assert trade.group_id is None
    assert trade.created_at_ms is None
    assert trade.observed_at_ms == _OBSERVED


@pytest.mark.parametrize("name", sorted(_EVERY_DOCUMENTED_ROW))
def test_every_documented_row_is_read_whole(name: str) -> None:
    row = copy.deepcopy(_EVERY_DOCUMENTED_ROW[name])

    activity = from_alpaca_option_activity(row, observed_at_ms=_OBSERVED)

    assert activity.activity_id == row["id"]
    assert activity.activity_type == row["activity_type"]
    if row["activity_type"] == "OPTRD":
        assert isinstance(activity, BrokerOptionShareTradeActivity)
        assert (activity.underlying_symbol, activity.shares) == (row["symbol"], int(row["qty"]))
        assert (activity.price, activity.net_amount) == (Decimal(row["price"]), Decimal(row["net_amount"]))
    else:
        assert isinstance(activity, BrokerOptionEventActivity)
        assert (activity.contract.occ_symbol, activity.position_change) == (row["symbol"], int(row["qty"]))


def test_a_half_dollar_strike_and_price_are_read_exactly() -> None:
    event = from_alpaca_option_activity(_row("broker_api", "exercising_call"), observed_at_ms=_OBSERVED)
    trade = from_alpaca_option_activity(_row("broker_api", "exercising_call", 1), observed_at_ms=_OBSERVED)

    assert isinstance(event, BrokerOptionEventActivity)
    assert isinstance(trade, BrokerOptionShareTradeActivity)
    assert event.contract.strike == Decimal("6.5")
    assert (trade.shares, trade.price, trade.net_amount) == (100, Decimal("6.5"), Decimal("-650"))


def test_a_figure_alpaca_writes_as_a_number_reaches_the_model_as_the_exact_type_it_declares() -> None:
    """The pages print every figure as text. The models take no float, so the adapter must not hand one over."""
    event = from_alpaca_option_activity(
        _row("broker_api", "exercising_call", qty=-1.0, net_amount=0.0), observed_at_ms=_OBSERVED
    )
    trade = from_alpaca_option_activity(
        _row("broker_api", "exercising_call", 1, qty=100.0, price=6.5, net_amount=-650), observed_at_ms=_OBSERVED
    )

    assert isinstance(event, BrokerOptionEventActivity)
    assert isinstance(trade, BrokerOptionShareTradeActivity)
    assert (event.position_change, type(event.position_change), event.net_amount) == (-1, int, Decimal("0"))
    assert (trade.shares, type(trade.shares)) == (100, int)
    assert (trade.price, trade.net_amount) == (Decimal("6.5"), Decimal("-650"))


@pytest.mark.parametrize(
    ("written", "day"),
    [("2023-07-21", date(2023, 7, 21)), ("2024-03-01", date(2024, 3, 1)), ("2024-11-03", date(2024, 11, 3))],
    ids=["summer", "winter", "the day the clocks go back"],
)
def test_an_activitys_date_is_new_york_midnight_in_summer_and_in_winter(written: str, day: date) -> None:
    event = from_alpaca_option_activity(_assignment(date=written, symbol="AAPL241220C00150000"), observed_at_ms=_OBSERVED)

    assert event.activity_date_ms == et_midnight_ms(day)


def test_what_the_reference_adds_to_a_row_is_read_when_it_is_there() -> None:
    event = from_alpaca_option_activity(
        _assignment(group_id="5c5f5a3a-0d0c-4b2a-9b9d-0f8d2f0c6a11", created_at="2023-07-02T08:15:04.650275Z", currency="USD"),
        observed_at_ms=_OBSERVED,
    )

    assert event.group_id == "5c5f5a3a-0d0c-4b2a-9b9d-0f8d2f0c6a11"
    # 04:15 New York on the 2nd, the morning after the date the row carries.
    assert event.created_at_ms == 1_688_285_704_650
    assert event.activity_date_ms == et_midnight_ms(date(2023, 7, 1))


def test_an_event_that_states_no_cash_reads_as_stating_none() -> None:
    event = from_alpaca_option_activity(_expiry(net_amount=...), observed_at_ms=_OBSERVED)

    assert isinstance(event, BrokerOptionEventActivity)
    assert event.net_amount is None


# ── The two spellings of an exercise ────────────────────────────────────────


def test_alpacas_two_pages_spell_the_exercise_differently() -> None:
    types = _documented_types()

    assert "OPEXC" in types["reference_option_activity_types"]
    assert "OPXRC" not in types["reference_option_activity_types"]
    assert "OPXRC" in types["account_activities_page_option_activity_types"]
    assert "OPEXC" not in types["account_activities_page_option_activity_types"]


@pytest.mark.parametrize("code", ["OPEXC", "OPXRC"])
def test_an_exercise_is_read_under_either_spelling_and_keeps_the_one_it_came_with(code: str) -> None:
    event = from_alpaca_option_activity(_exercise(activity_type=code), observed_at_ms=_OBSERVED)

    assert isinstance(event, BrokerOptionEventActivity)
    assert event.kind is OptionEventKind.EXERCISED
    assert event.activity_type == code
    assert event.position_change == -2


def test_every_option_code_either_page_lists_is_read_or_refused_by_name() -> None:
    types = _documented_types()
    listed = {*types["reference_option_activity_types"], *types["account_activities_page_option_activity_types"]}
    read, refused = set(), set()

    for code in sorted(listed):
        if code == "OPTRD":
            row = _share_trade()
        else:
            # An assignment adds back to a sold position; every other event takes from a bought one.
            row = _expiry(activity_type=code, qty="1" if code == "OPASN" else "-1")
        assert alpaca_option_activity_type(row) == code
        try:
            from_alpaca_option_activity(row, observed_at_ms=_OBSERVED)
        except ValueError as exc:
            assert f"{code!r} is an option activity this app does not read" in str(exc)
            refused.add(code)
        else:
            read.add(code)

    assert read == {"OPASN", "OPEXC", "OPXRC", "OPEXP", "OPTRD"}
    # An option corporate action and a cash deliverable: listed by the reference, read by nothing here.
    assert refused == {"OPCA", "OPCSH"}


def test_the_statuses_read_are_the_three_the_reference_lists() -> None:
    assert [status.value for status in OptionActivityStatus] == _documented_types()["reference_non_trade_activity_statuses"]


@pytest.mark.parametrize("status", ["correct", "canceled"])
def test_a_row_the_broker_no_longer_stands_by_is_read_as_such(status: str) -> None:
    event = from_alpaca_option_activity(_exercise(status=status), observed_at_ms=_OBSERVED)

    assert event.status is OptionActivityStatus(status)


# ── A row that cannot be read whole is not read at all ──────────────────────

_UNREADABLE_EVENTS: dict[str, dict[str, Any]] = {
    "no id": {"id": ...},
    "a blank id": {"id": "  "},
    "an id that is not text": {"id": 20190801011955195},
    "no status": {"status": ...},
    "a status the reference does not list": {"status": "pending"},
    "no date": {"date": ...},
    "a date with a time": {"date": "2023-07-21T00:00:00Z"},
    "a date written without dashes": {"date": "20230721"},
    "a date that is not one": {"date": "2023-02-30"},
    "no symbol": {"symbol": ...},
    "the underlying where the contract belongs": {"symbol": "AAPL"},
    "an adjusted contract's root": {"symbol": "AAPL1230721C00150000"},
    "no quantity": {"qty": ...},
    "a quantity of none": {"qty": "0"},
    "a part of a contract": {"qty": "-1.5"},
    "a quantity that is not a number": {"qty": "two"},
    "a quantity that is a boolean": {"qty": True},
    "a quantity that is not finite": {"qty": "-Infinity"},
    "an exercise that adds contracts": {"qty": "2"},
    "cash that is not a number": {"net_amount": "none"},
    "cash of its own": {"net_amount": "-30000"},
    "another currency": {"currency": "EUR"},
    "a group id that is not text": {"group_id": 7},
    "a creation time that is not one": {"created_at": "yesterday"},
    "a time of its own beside the date": {"transaction_time": "2023-07-21T13:30:00Z"},
    "a time of its own that is not one": {"transaction_time": ""},
}


def test_an_assignment_that_removes_contracts_is_refused() -> None:
    """An assignment adds back to a sold position; one signed the other way is not an assignment the pages show."""
    with pytest.raises(ValueError, match="an assignment takes from a sold position"):
        from_alpaca_option_activity(_assignment(qty="-2"), observed_at_ms=_OBSERVED)


def test_an_expiry_dated_before_its_contract_expires_is_refused() -> None:
    """The documented contract expires 2023-07-21. A row that has it expire the day before is not an expiry."""
    with pytest.raises(ValueError, match="this expiry is dated 2023-07-20"):
        from_alpaca_option_activity(_expiry(date="2023-07-20"), observed_at_ms=_OBSERVED)


def test_an_expiry_reported_after_the_expiry_date_is_read() -> None:
    event = from_alpaca_option_activity(_expiry(date="2023-07-22"), observed_at_ms=_OBSERVED)

    assert event.activity_date_ms == et_midnight_ms(date(2023, 7, 22))


def test_an_expiry_is_read_with_either_sign() -> None:
    """The pages show a bought option expiring. A sold one is taken to be signed the other way, and is read."""
    bought = from_alpaca_option_activity(_expiry(qty="-3"), observed_at_ms=_OBSERVED)
    sold = from_alpaca_option_activity(_expiry(qty="3"), observed_at_ms=_OBSERVED)

    assert (bought.position_change, bought.contracts) == (-3, 3)
    assert (sold.position_change, sold.contracts) == (3, 3)


_UNREADABLE_SHARE_TRADES: dict[str, dict[str, Any]] = {
    "no price": {"price": ...},
    "a price of nothing": {"price": "0"},
    "a price below nothing": {"price": "-150"},
    "a price that is not a number": {"price": "strike"},
    "no cash": {"net_amount": ...},
    "no shares": {"qty": ...},
    "a part of a share": {"qty": "-200.5"},
    "no shares at all": {"qty": "0"},
    "no symbol": {"symbol": ...},
    "a blank symbol": {"symbol": ""},
    "no status": {"status": ...},
    "no date": {"date": ...},
    "no id": {"id": ...},
    "another currency": {"currency": "CAD"},
    "a time of its own beside the date": {"transaction_time": "2023-07-01T13:30:00Z"},
}


@pytest.mark.parametrize(
    ("make_row", "damage"),
    [pytest.param(_exercise, changes, id=f"event/{name}") for name, changes in sorted(_UNREADABLE_EVENTS.items())]
    + [
        pytest.param(_share_trade, changes, id=f"share-trade/{name}")
        for name, changes in sorted(_UNREADABLE_SHARE_TRADES.items())
    ],
)
def test_an_activity_that_cannot_be_read_whole_is_refused_whole(
    make_row: Callable[..., dict[str, Any]], damage: dict[str, Any]
) -> None:
    with pytest.raises((TypeError, ValueError)):
        from_alpaca_option_activity(make_row(**damage), observed_at_ms=_OBSERVED)


@pytest.mark.parametrize("payload", [None, "OPEXC", ["OPEXC"], {}, {"activity_type": None}, {"activity_type": 7}])
def test_something_that_is_not_an_activity_row_is_not_an_option_activity(payload: Any) -> None:
    assert alpaca_option_activity_type(payload) is None
    with pytest.raises(ValueError, match="not an option activity"):
        from_alpaca_option_activity(payload, observed_at_ms=_OBSERVED)


def test_a_fill_and_a_journal_entry_are_not_option_activities() -> None:
    fill, journal = load_alpaca_fixture_file("activities", "activities.json")

    assert alpaca_option_activity_type(fill) is None
    assert alpaca_option_activity_type(journal) is None


def test_a_code_no_page_lists_is_named_and_not_passed_over() -> None:
    """A third spelling of the exercise would begin ``OP`` too, and must not go unseen."""
    row = _exercise(activity_type="OPEXR")

    assert alpaca_option_activity_type(row) == "OPEXR"
    with pytest.raises(ValueError, match="'OPEXR' is an option activity this app does not read"):
        from_alpaca_option_activity(row, observed_at_ms=_OBSERVED)


_CODES_NOT_WRITTEN_EXACTLY = ["opexp", " OPEXP", "OPEXP ", "Opasn", "opexc", "\tOPXRC", "optrd", " opca "]


@pytest.mark.parametrize("code", _CODES_NOT_WRITTEN_EXACTLY)
def test_a_code_not_written_as_alpaca_writes_it_is_still_an_option_activity_and_is_read_as_nothing(code: str) -> None:
    """A lower-case or padded ``OPEXP`` may be an expiry. It is named as unread, never passed over as a fee might be."""
    row = _expiry(activity_type=code)

    # The code is kept as it arrived.
    assert alpaca_option_activity_type(row) == code
    with pytest.raises(ValueError, match="is not written as Alpaca's pages write an option activity code"):
        from_alpaca_option_activity(row, observed_at_ms=_OBSERVED)


@pytest.mark.parametrize("code", ["FEE", "fee", " DIV ", "JNLC", "CSD", "P", "O", "", "  "])
def test_a_code_that_does_not_begin_as_an_option_code_does_is_not_an_option_activity(code: str) -> None:
    assert alpaca_option_activity_type(_expiry(activity_type=code)) is None


# ── Taken once: the id ──────────────────────────────────────────────────────


def test_the_same_activity_read_twice_has_the_same_id_and_says_the_same_thing() -> None:
    row = _row("broker_api", "put_assignment")

    first = from_alpaca_option_activity(copy.deepcopy(row), observed_at_ms=_OBSERVED)
    second = from_alpaca_option_activity(copy.deepcopy(row), observed_at_ms=_OBSERVED + 86_400_000)

    assert first.activity_id == second.activity_id == "20240301000000000::fcd92e5c-46e4-4c6e-8866-d56cd7d2bde2"
    assert first.model_dump(exclude={"observed_at_ms"}) == second.model_dump(exclude={"observed_at_ms"})


# ── Alpaca's share trade against the contract's own terms ───────────────────


@pytest.mark.parametrize(
    "heading", ["exercising_call", "exercising_put", "call_assignment", "put_assignment"]
)
def test_each_share_trade_alpaca_publishes_agrees_with_its_events_contract(heading: str) -> None:
    event = from_alpaca_option_activity(_row("broker_api", heading), observed_at_ms=_OBSERVED)
    reported = from_alpaca_option_activity(_row("broker_api", heading, 1), observed_at_ms=_OBSERVED)
    assert isinstance(event, BrokerOptionEventActivity)
    assert isinstance(reported, BrokerOptionShareTradeActivity)

    expected = expected_share_trade(event.kind, event.contract, contracts=event.contracts, multiplier=100)

    assert expected is not None
    assert share_trade_disagreements(expected, reported) == ()


def test_the_price_alpaca_prints_against_its_own_sentence_is_named_as_a_difference() -> None:
    """The Trading API page's exercised calls: 200 shares for 30000 at a strike of 150, and a printed price of 90."""
    event = from_alpaca_option_activity(_exercise(), observed_at_ms=_OBSERVED)
    reported = from_alpaca_option_activity(_row("trading_api", "option_exercise", 1), observed_at_ms=_OBSERVED)
    assert isinstance(event, BrokerOptionEventActivity)
    assert isinstance(reported, BrokerOptionShareTradeActivity)

    expected = expected_share_trade(event.kind, event.contract, contracts=event.contracts, multiplier=100)

    assert expected is not None
    assert share_trade_disagreements(expected, reported) == ("the reported price is 90, and the strike is 150",)


# ── The read ────────────────────────────────────────────────────────────────


class _NonTradeClient:
    """The client seam: pages of raw non-trade activities, keyed by the page token that asks for them."""

    def __init__(self, pages: dict[str | None, list[Any]]) -> None:
        self.pages = pages
        self.asked: list[tuple[int, str | None]] = []

    async def list_non_trade_activities(self, *, limit: int, page_token: str | None = None) -> list[Any]:
        if len(self.asked) >= 6:
            raise AssertionError("the read followed its cursor without end")
        self.asked.append((limit, page_token))
        return self.pages[page_token]


def _broker(pages: dict[str | None, list[Any]]) -> tuple[AlpacaBroker, _NonTradeClient]:
    client = _NonTradeClient(pages)
    return AlpacaBroker(client=client), client  # type: ignore[arg-type]


def _fee(day: str, serial: int) -> dict[str, Any]:
    """A regulatory fee row, as the account's other non-trade activity."""
    return {
        "id": f"{day.replace('-', '')}000000000::fee-{serial:04d}",
        "activity_type": "FEE",
        "date": day,
        "net_amount": "-0.01",
        "description": "REG fee",
        "status": "executed",
    }


def _fees(day: str, count: int, *, start: int = 0) -> list[dict[str, Any]]:
    return [_fee(day, start + serial) for serial in range(count)]


_FRIDAY = et_midnight_ms(date(2024, 3, 1))
_QS_ROWS = [
    _row("broker_api", "call_assignment"),
    _row("broker_api", "call_assignment", 1),
    _row("broker_api", "put_expiration"),
    _row("broker_api", "exercising_call"),
    _row("broker_api", "exercising_call", 1),
]


async def test_the_read_asks_for_the_accounts_non_trade_activities_a_page_at_a_time() -> None:
    broker, client = _broker({None: []})

    evidence = await broker.read_option_event_activities(after_ms=_FRIDAY)

    assert client.asked == [(_PAGE, None)]
    assert (evidence.events, evidence.share_trades, evidence.unreadable) == ((), (), ())
    assert evidence.history_complete is True
    assert evidence.read_whole is True


async def test_option_events_and_their_share_trades_are_picked_out_from_the_other_activities() -> None:
    journal = load_alpaca_fixture_file("activities", "activities.json")[1]
    rows = [
        _fee("2024-03-01", 1),
        *_QS_ROWS[:3],
        _fee("2024-02-28", 2),
        *_QS_ROWS[3:],
        {**journal, "id": "20240226000000000::journal", "date": "2024-02-26"},
    ]
    broker, _client = _broker({None: rows})

    evidence = await broker.read_option_event_activities(after_ms=et_midnight_ms(date(2024, 2, 26)))

    assert [(event.kind, event.contract.occ_symbol, event.position_change) for event in evidence.events] == [
        (OptionEventKind.ASSIGNED, "QS240301C00004500", 1),
        (OptionEventKind.EXPIRED, "QS240301P00000500", -1),
        (OptionEventKind.EXERCISED, "QS240301C00006500", -1),
    ]
    assert [(trade.underlying_symbol, trade.shares, trade.price) for trade in evidence.share_trades] == [
        ("QS", -100, Decimal("4.5")),
        ("QS", 100, Decimal("6.5")),
    ]
    assert evidence.unreadable == ()
    assert evidence.history_complete is True
    assert evidence.read_whole is True


@pytest.mark.parametrize(
    "window_start_after_midnight_ms",
    [0, (15 * 60 + 59) * 60_000],
    ids=[
        "an activity dated before the window is left out",
        "an activity dated on the day the window starts is kept whatever the hour",
    ],
)
async def test_the_window_leaves_out_the_activities_dated_before_it_and_keeps_those_of_its_first_day(
    window_start_after_midnight_ms: int,
) -> None:
    """A row has a date and no time, so it cannot be said to fall before 15:59 on its own day."""
    broker, _client = _broker({None: _QS_ROWS})

    evidence = await broker.read_option_event_activities(after_ms=_FRIDAY + window_start_after_midnight_ms)

    # The exercise and its share trade are dated 2024-02-27.
    assert [event.activity_date_ms for event in evidence.events] == [_FRIDAY, _FRIDAY], (
        "the events dated on the window's first day are kept and the earlier one is left out"
    )
    assert [trade.activity_date_ms for trade in evidence.share_trades] == [_FRIDAY], (
        "the share trade dated on the window's first day is kept and the earlier one is left out"
    )


async def test_a_row_that_cannot_be_read_whole_is_listed_as_unreadable_and_the_rest_are_still_read() -> None:
    damaged = _row("broker_api", "put_expiration", qty="-0.5")
    broker, _client = _broker({None: [_QS_ROWS[0], _QS_ROWS[1], damaged]})

    evidence = await broker.read_option_event_activities(after_ms=_FRIDAY)

    assert [event.activity_id for event in evidence.events] == [_QS_ROWS[0]["id"]]
    assert [trade.activity_id for trade in evidence.share_trades] == [_QS_ROWS[1]["id"]]
    (unreadable,) = evidence.unreadable
    assert unreadable.activity_id == damaged["id"]
    assert unreadable.activity_type == "OPEXP"
    assert unreadable.activity_date_ms == _FRIDAY
    # The symbol is the row's own text, unparsed.
    assert unreadable.symbol == "QS240301P00000500"
    assert "'qty' must be a whole number other than zero" in unreadable.reason
    # It carries nothing of what happened: no count, no cash, no parsed contract.
    assert set(unreadable.model_dump()) == {
        "broker",
        "activity_id",
        "activity_type",
        "activity_date_ms",
        "symbol",
        "reason",
    }
    # The page was short, so the walk is complete; the read is still not whole.
    assert evidence.history_complete is True
    assert evidence.read_whole is False


@pytest.mark.parametrize(
    ("damage", "reason"),
    [
        ({"qty": "two"}, "Alpaca field 'qty' is not a number: 'two'"),
        ({"qty": ...}, "Alpaca field 'qty' must be a number, got None"),
        ({"net_amount": "none"}, "Alpaca field 'net_amount' is not a number: 'none'"),
        ({"symbol": "QS1240301P00000500"}, "is not an option contract symbol"),
        ({"status": ...}, "Alpaca field 'status' must be a non-blank string, got None"),
    ],
    ids=["a count in words", "no count", "cash in words", "an adjusted contract", "no status"],
)
async def test_whatever_is_wrong_with_an_option_rows_figures_makes_that_row_unreadable_and_no_other(
    damage: dict[str, Any], reason: str
) -> None:
    damaged = _row("broker_api", "put_expiration", **damage)
    broker, _client = _broker({None: [_QS_ROWS[0], damaged]})

    evidence = await broker.read_option_event_activities(after_ms=_FRIDAY)

    assert [event.activity_id for event in evidence.events] == [_QS_ROWS[0]["id"]]
    (unreadable,) = evidence.unreadable
    assert unreadable.activity_id == damaged["id"]
    assert reason in unreadable.reason


@pytest.mark.parametrize(
    ("serial", "odd_fields", "fee_position"),
    [
        pytest.param(7, (("net_amount", "n/a"), ("qty", "?")), 0, id="a figure that will not parse"),
        pytest.param(3, (("transaction_time", "2024-02-01T15:00:00Z"),), 1, id="an instant weeks before the window"),
    ],
)
async def test_what_is_odd_on_some_other_activity_does_not_refuse_the_read(
    serial: int, odd_fields: tuple[tuple[str, str], ...], fee_position: int
) -> None:
    """A fee row is only walked past: its amount is not this read's to parse, and an instant weeks before the window refuses nothing."""
    odd_fee = {**_fee("2024-03-01", serial), **dict(odd_fields)}
    rows: list[Any] = [_QS_ROWS[2]]
    rows.insert(fee_position, odd_fee)
    broker, _client = _broker({None: rows})

    evidence = await broker.read_option_event_activities(after_ms=_FRIDAY)

    assert [event.activity_id for event in evidence.events] == [_QS_ROWS[2]["id"]], (
        "the option event is still read beside the odd fee row"
    )
    assert evidence.read_whole is True, "the odd fee row does not make the read less than whole"


async def test_a_sign_the_kind_cannot_have_is_explained_in_plain_words() -> None:
    reversed_exercise = _row("broker_api", "exercising_call", qty="1", date="2024-03-01")
    broker, _client = _broker({None: [reversed_exercise]})

    evidence = await broker.read_option_event_activities(after_ms=_FRIDAY)

    assert evidence.events == ()
    (unreadable,) = evidence.unreadable
    assert unreadable.reason == "an exercise takes from a bought position; this one adds 1"


@pytest.mark.parametrize("code", _CODES_NOT_WRITTEN_EXACTLY)
async def test_an_option_code_not_written_exactly_is_listed_as_unreadable_and_not_skipped(code: str) -> None:
    odd = _row("broker_api", "put_expiration", activity_type=code)
    broker, _client = _broker({None: [_QS_ROWS[0], odd]})

    evidence = await broker.read_option_event_activities(after_ms=_FRIDAY)

    assert [event.activity_id for event in evidence.events] == [_QS_ROWS[0]["id"]]
    (unreadable,) = evidence.unreadable
    # The code is listed as it arrived, so the owner sees what Alpaca sent.
    assert (unreadable.activity_id, unreadable.activity_type) == (odd["id"], code)
    assert "is not written as Alpaca's pages write an option activity code" in unreadable.reason
    assert evidence.read_whole is False


@pytest.mark.parametrize(
    "transaction_time",
    [
        # Midnight UTC on the row's own date: the evening before in New York, so a day before the window.
        "2024-03-01T00:00:00Z",
        "2024-03-01T15:00:00Z",
        # Weeks before the window.
        "2024-02-01T15:00:00Z",
        "",
    ],
    ids=["the evening before in New York", "that afternoon", "weeks before", "blank"],
)
async def test_an_option_row_that_states_a_time_is_unreadable_and_is_not_placed_outside_its_own_date(
    transaction_time: str,
) -> None:
    """The row is dated the day the window starts. Its instant must not skip it and leave the read whole."""
    timed = _row("broker_api", "put_expiration", transaction_time=transaction_time)
    broker, _client = _broker({None: [_QS_ROWS[0], timed]})

    evidence = await broker.read_option_event_activities(after_ms=_FRIDAY)

    assert [event.activity_id for event in evidence.events] == [_QS_ROWS[0]["id"]]
    (unreadable,) = evidence.unreadable
    assert unreadable.activity_id == timed["id"]
    # It is placed by the date it carries, not by the instant.
    assert unreadable.activity_date_ms == _FRIDAY
    assert "'transaction_time' is on an option activity" in unreadable.reason
    assert evidence.read_whole is False


@pytest.mark.parametrize(
    "transaction_time",
    [..., "2024-01-05T15:00:00Z"],
    ids=[
        "an unreadable row with no date is kept because it cannot be placed outside the window",
        "an option row that states a time and no date cannot be placed outside the window",
    ],
)
async def test_an_option_row_with_no_date_cannot_be_placed_outside_the_window(transaction_time: Any) -> None:
    undated = _row("broker_api", "put_expiration", date=..., transaction_time=transaction_time)
    broker, _client = _broker({None: [undated]})

    evidence = await broker.read_option_event_activities(after_ms=_FRIDAY)

    (unreadable,) = evidence.unreadable
    assert (unreadable.activity_id, unreadable.activity_date_ms) == (undated["id"], None), (
        "the row with no date is listed as unreadable and kept, with no date"
    )
    assert evidence.read_whole is False, "a row that could not be placed makes the read not whole"


@pytest.mark.parametrize("status", ["correct", "canceled"])
async def test_a_row_the_broker_no_longer_stands_by_is_listed_and_the_read_is_not_whole(status: str) -> None:
    not_stood_by = _row("broker_api", "put_expiration", status=status)
    broker, _client = _broker({None: [_QS_ROWS[0], _QS_ROWS[1], not_stood_by]})

    evidence = await broker.read_option_event_activities(after_ms=_FRIDAY)

    assert [event.activity_id for event in evidence.events] == [_QS_ROWS[0]["id"], not_stood_by["id"]]
    assert [activity.activity_id for activity in evidence.not_executed] == [not_stood_by["id"]]
    assert evidence.unreadable == ()
    # The walk reached the window's start, and the read still does not explain everything.
    assert evidence.history_complete is True
    assert evidence.read_whole is False


async def test_an_expiry_dated_before_its_contract_expires_is_listed_as_unreadable() -> None:
    """``QS240301P00000500`` expires 2024-03-01; the row has it expire on 2024-02-29."""
    early = _row("broker_api", "put_expiration", date="2024-02-29")
    broker, _client = _broker({None: [_QS_ROWS[0], early]})

    evidence = await broker.read_option_event_activities(after_ms=et_midnight_ms(date(2024, 2, 26)))

    assert [event.activity_id for event in evidence.events] == [_QS_ROWS[0]["id"]]
    (unreadable,) = evidence.unreadable
    assert unreadable.activity_id == early["id"]
    assert "this expiry is dated 2024-02-29" in unreadable.reason
    assert evidence.read_whole is False


@pytest.mark.parametrize("code", ["OPCA", "OPCSH"])
async def test_an_option_activity_of_a_kind_this_app_does_not_read_is_listed_and_not_skipped(code: str) -> None:
    corporate_action = {
        "id": f"20240301000000000::{code.lower()}",
        "activity_type": code,
        "date": "2024-03-01",
        "net_amount": "133",
        "symbol": "FIZZ",
        "qty": "-7",
        "status": "executed",
    }
    broker, _client = _broker({None: [corporate_action, _QS_ROWS[2]]})

    evidence = await broker.read_option_event_activities(after_ms=_FRIDAY)

    assert [event.activity_id for event in evidence.events] == [_QS_ROWS[2]["id"]]
    (unreadable,) = evidence.unreadable
    assert (unreadable.activity_id, unreadable.activity_type) == (corporate_action["id"], code)
    assert unreadable.symbol == "FIZZ"
    assert unreadable.reason == f"Alpaca activity type '{code}' is an option activity this app does not read"
    assert evidence.read_whole is False


@pytest.mark.parametrize(
    "row",
    [
        _row("broker_api", "put_expiration", id=...),
        _row("broker_api", "put_expiration", id="   "),
        _row("broker_api", "put_expiration", date="not a date"),
        {**_fee("2024-03-01", 9), "activity_type": None},
        {**_fee("2024-03-01", 9), "id": None},
        "OPEXP",
        None,
    ],
    ids=[
        "no id",
        "a blank id",
        "a date that cannot be read",
        "another activity with no code",
        "another activity with no id",
        "a row that is not an object",
        "no row at all",
    ],
)
async def test_a_row_that_cannot_be_placed_in_the_walk_refuses_the_whole_read(row: Any) -> None:
    broker, _client = _broker({None: [_QS_ROWS[0], row]})

    with pytest.raises(BrokerEvidenceUnavailable, match="activity data this app could not read"):
        await broker.read_option_event_activities(after_ms=_FRIDAY)


async def test_an_event_and_its_share_trade_printed_under_one_id_are_both_kept() -> None:
    """Alpaca's Trading API examples give every row one id. If an account's rows do too, neither is lost."""
    event, share_trade = _examples()["trading_api"]["option_assignment"]
    assert event["id"] == share_trade["id"]
    broker, _client = _broker({None: [event, share_trade]})

    evidence = await broker.read_option_event_activities(after_ms=et_midnight_ms(date(2023, 7, 1)))

    assert [found.activity_id for found in evidence.events] == [event["id"]]
    assert [found.activity_id for found in evidence.share_trades] == [event["id"]]


async def test_a_row_alpaca_repeats_is_kept_once() -> None:
    broker, _client = _broker({None: [_QS_ROWS[2], copy.deepcopy(_QS_ROWS[2])]})

    evidence = await broker.read_option_event_activities(after_ms=_FRIDAY)

    assert [event.activity_id for event in evidence.events] == [_QS_ROWS[2]["id"]]


async def test_one_id_carrying_two_different_events_makes_the_read_unusable() -> None:
    other_count = {**_QS_ROWS[2], "qty": "-4"}
    broker, _client = _broker({None: [_QS_ROWS[2], other_count]})

    with pytest.raises(BrokerEvidenceUnavailable, match="conflicting duplicate"):
        await broker.read_option_event_activities(after_ms=_FRIDAY)


async def test_the_same_window_read_twice_gives_the_same_ids() -> None:
    first_broker, _first = _broker({None: copy.deepcopy(_QS_ROWS)})
    second_broker, _second = _broker({None: copy.deepcopy(_QS_ROWS)})
    window = et_midnight_ms(date(2024, 2, 26))

    first = await first_broker.read_option_event_activities(after_ms=window)
    second = await second_broker.read_option_event_activities(after_ms=window)

    assert [event.activity_id for event in first.events] == [event.activity_id for event in second.events]
    assert [trade.activity_id for trade in first.share_trades] == [trade.activity_id for trade in second.share_trades]
    assert len({event.activity_id for event in first.events}) == 3


# ── Paging ──────────────────────────────────────────────────────────────────


async def test_a_full_page_is_followed_from_its_last_rows_id_until_a_short_page() -> None:
    first_page = [_QS_ROWS[0], *_fees("2024-03-01", _PAGE - 1)]
    broker, client = _broker({None: first_page, first_page[-1]["id"]: [_QS_ROWS[2]]})

    evidence = await broker.read_option_event_activities(after_ms=_FRIDAY)

    assert client.asked == [(_PAGE, None), (_PAGE, first_page[-1]["id"])]
    assert [event.activity_id for event in evidence.events] == [_QS_ROWS[0]["id"], _QS_ROWS[2]["id"]]
    assert evidence.history_complete is True
    assert evidence.resume is None


def _full_pages(*days: str) -> dict[str | None, list[Any]]:
    """One full page of fee rows for each date given, each asked for by the last id of the page before it."""
    pages: dict[str | None, list[Any]] = {}
    token: str | None = None
    for page_number, day in enumerate(days):
        page = _fees(day, _PAGE, start=page_number * _PAGE)
        pages[token] = page
        token = page[-1]["id"]
    return pages


def _last_id(pages: dict[str | None, list[Any]]) -> str:
    """The id that asks for the page after the last one in ``pages``."""
    return list(pages.values())[-1][-1]["id"]


async def test_a_walk_that_runs_out_of_pages_is_not_complete_and_says_where_to_resume() -> None:
    pages = _full_pages("2024-03-01", "2024-03-01", "2024-03-01", "2024-03-01")
    broker, client = _broker(pages)

    evidence = await broker.read_option_event_activities(after_ms=_FRIDAY)

    # Three pages a call, and the fourth is left for the next one.
    assert [asked_token for _limit, asked_token in client.asked] == [None, pages[None][-1]["id"], "20240301000000000::fee-0199"]
    assert evidence.history_complete is False
    assert evidence.read_whole is False
    # Where to go on from, and the oldest time the third page placed: the next page is held to it.
    assert evidence.resume == BrokerOptionEventResume(
        page_token="20240301000000000::fee-0299", last_page_oldest_ms=_FRIDAY
    )


async def test_a_resumed_walk_goes_on_from_where_the_last_call_stopped_and_completes() -> None:
    pages = _full_pages("2024-03-01", "2024-03-01", "2024-03-01")
    broker, client = _broker({**pages, _last_id(pages): [_QS_ROWS[2]]})
    stopped = await broker.read_option_event_activities(after_ms=_FRIDAY)

    evidence = await broker.read_option_event_activities(after_ms=_FRIDAY, resume=stopped.resume)

    # The second call asks for the fourth page and no page it was already given.
    assert [asked_token for _limit, asked_token in client.asked][3:] == ["20240301000000000::fee-0299"]
    assert [event.activity_id for event in evidence.events] == [_QS_ROWS[2]["id"]]
    assert evidence.history_complete is True
    assert evidence.resume is None


async def test_a_resumed_page_newer_than_the_page_before_it_refuses_the_read() -> None:
    """Three pages dated the 29th, then a row dated March 1st: out of order, though a call's page bound lies between."""
    window = et_midnight_ms(date(2024, 2, 26))
    pages = _full_pages("2024-02-29", "2024-02-29", "2024-02-29")
    broker, _client = _broker({**pages, _last_id(pages): [_QS_ROWS[2]]})
    stopped = await broker.read_option_event_activities(after_ms=window)
    assert stopped.resume is not None
    assert stopped.resume.last_page_oldest_ms == et_midnight_ms(date(2024, 2, 29))

    with pytest.raises(BrokerEvidenceUnavailable, match="not newest-first"):
        await broker.read_option_event_activities(after_ms=window, resume=stopped.resume)


async def test_a_resumed_walk_is_proven_by_its_first_page_when_the_page_before_it_crossed_the_windows_start() -> None:
    """The third page reaches back past the window, and the call stops there. The next page confirms it, alone."""
    newest = _full_pages("2024-03-01", "2024-03-01")
    crossing = [*_fees("2024-03-01", 50, start=200), *_fees("2024-02-29", _PAGE - 50, start=250)]
    confirming = _fees("2024-02-28", _PAGE, start=300)
    broker, client = _broker(
        {
            **newest,
            _last_id(newest): crossing,
            crossing[-1]["id"]: confirming,
            confirming[-1]["id"]: [{"must": "not be read"}],
        }
    )
    stopped = await broker.read_option_event_activities(after_ms=_FRIDAY)
    assert stopped.history_complete is False

    evidence = await broker.read_option_event_activities(after_ms=_FRIDAY, resume=stopped.resume)

    assert [asked_token for _limit, asked_token in client.asked][3:] == [crossing[-1]["id"]]
    assert evidence.history_complete is True


async def test_a_resumed_walk_is_not_proven_by_a_page_that_only_reached_the_windows_first_day() -> None:
    """The third page ends on the day the window starts, which is not past it. Rows of that day may lie further back."""
    newest = _full_pages("2024-03-01", "2024-03-01", "2024-03-01")
    fourth = _fees("2024-03-01", _PAGE, start=300)
    broker, client = _broker({**newest, _last_id(newest): fourth, fourth[-1]["id"]: [_QS_ROWS[2]]})
    stopped = await broker.read_option_event_activities(after_ms=_FRIDAY)

    evidence = await broker.read_option_event_activities(after_ms=_FRIDAY, resume=stopped.resume)

    # The fourth page does not end the walk; the event on the fifth is found.
    assert [asked_token for _limit, asked_token in client.asked][3:] == [_last_id(newest), fourth[-1]["id"]]
    assert [event.activity_id for event in evidence.events] == [_QS_ROWS[2]["id"]]
    assert evidence.history_complete is True


async def test_a_walk_that_stops_on_a_page_with_an_undated_row_says_that_page_proved_no_order() -> None:
    """A row with no date cannot be placed, so its page gives the next one nothing to be held to, as within a call."""
    newest = _full_pages("2024-03-01", "2024-03-01")
    undated = _row("broker_api", "put_expiration", date=...)
    third = [*_fees("2024-03-01", _PAGE - 1, start=200), undated]
    broker, _client = _broker({**newest, _last_id(newest): third})

    evidence = await broker.read_option_event_activities(after_ms=_FRIDAY)

    assert evidence.resume == BrokerOptionEventResume(page_token=undated["id"], last_page_oldest_ms=None)


async def test_the_window_is_proven_by_the_page_after_the_one_that_crossed_its_start() -> None:
    """A full page that reaches back past the window does not end the walk; the next dated page does."""
    crossing = [_QS_ROWS[0], *_fees("2024-03-01", 50), *_fees("2024-02-29", _PAGE - 51, start=50)]
    confirming = _fees("2024-02-28", _PAGE, start=200)
    broker, client = _broker(
        {None: crossing, crossing[-1]["id"]: confirming, confirming[-1]["id"]: [{"must": "not be read"}]}
    )

    evidence = await broker.read_option_event_activities(after_ms=_FRIDAY)

    assert [asked_token for _limit, asked_token in client.asked] == [None, crossing[-1]["id"]]
    assert [event.activity_id for event in evidence.events] == [_QS_ROWS[0]["id"]]
    assert evidence.history_complete is True


async def test_a_page_that_is_not_newest_first_cannot_prove_the_window() -> None:
    broker, _client = _broker({None: [_fee("2024-02-27", 1), _QS_ROWS[0]]})

    with pytest.raises(BrokerEvidenceUnavailable, match="not newest-first"):
        await broker.read_option_event_activities(after_ms=et_midnight_ms(date(2024, 2, 26)))


async def test_a_cursor_that_does_not_move_ends_the_walk_as_incomplete() -> None:
    page = _fees("2024-03-01", _PAGE)
    broker, client = _broker({None: page, page[-1]["id"]: page})

    evidence = await broker.read_option_event_activities(after_ms=_FRIDAY)

    assert len(client.asked) == 2
    assert evidence.history_complete is False
    # Nothing to go on from: the same cursor would ask for the same page.
    assert evidence.resume is None
