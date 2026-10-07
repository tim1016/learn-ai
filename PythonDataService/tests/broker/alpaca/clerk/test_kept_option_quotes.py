"""The option quotes a simulated account was handed: which one is the newest kept (#2913)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from app.broker.alpaca.clerk.kept_option_quotes import KeptOptionQuotes
from app.options.quotes import HeldQuote, QuoteDataType, QuoteSide

SYMBOL = "SPY260918C00585000"


def _quote(bid: str | None, ask: str | None, *, bid_at: int = 1_000, ask_at: int = 1_000) -> HeldQuote:
    return HeldQuote(
        bid=None if bid is None else QuoteSide(Decimal(bid), bid_at),
        ask=None if ask is None else QuoteSide(Decimal(ask), ask_at),
        data_type=QuoteDataType.LIVE,
        generation=4,
    )


def _store(tmp_path: Path) -> KeptOptionQuotes:
    return KeptOptionQuotes.of_account(artifacts_root=tmp_path, account_id="sim:buys-a-call")


def test_the_newest_kept_is_the_one_received_last_not_the_one_written_last(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.keep(SYMBOL, _quote("2.00", "2.04", bid_at=9_000, ask_at=9_000), kept_at_ms=10_000)
    store.keep(SYMBOL, _quote("1.90", "1.94", bid_at=5_000, ask_at=5_000), kept_at_ms=11_000)

    newest = store.newest(SYMBOL)

    assert newest is not None and newest.quote.bid == Decimal("2.00")
    assert newest.received_ms == 9_000
    assert newest.quote.held == _quote("2.00", "2.04", bid_at=9_000, ask_at=9_000)


def test_a_quote_received_after_the_instant_asked_about_is_left_out(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.keep(SYMBOL, _quote("1.90", "1.94", bid_at=5_000, ask_at=5_000), kept_at_ms=6_000)
    store.keep(SYMBOL, _quote("2.00", "2.04", bid_at=9_000, ask_at=9_000), kept_at_ms=10_000)

    at_seven = store.newest(SYMBOL, at_or_before_ms=7_000)

    assert at_seven is not None and at_seven.quote.bid == Decimal("1.90")
    assert store.newest(SYMBOL, at_or_before_ms=4_000) is None


def test_a_side_can_be_asked_for_and_a_quote_without_it_is_passed_over(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.keep(SYMBOL, _quote("1.90", "1.94", bid_at=5_000, ask_at=5_000), kept_at_ms=6_000)
    store.keep(SYMBOL, _quote(None, "2.60", ask_at=9_000), kept_at_ms=10_000)

    with_a_bid = store.newest(SYMBOL, with_side="bid")

    assert with_a_bid is not None and with_a_bid.quote.bid == Decimal("1.90")
    newest = store.newest(SYMBOL)
    assert newest is not None and newest.quote.bid is None


def _rows(tmp_path: Path) -> int:
    return (tmp_path / "accounts" / "alpaca" / "sim:buys-a-call" / "kept_option_quotes.jsonl").read_text().count("\n")


def test_a_quote_unchanged_since_it_was_kept_is_stated_again_as_the_newest_held(tmp_path: Path) -> None:
    """A quiet contract's bid stands for minutes. Each hand-over says it is still the bid."""
    store = _store(tmp_path)
    quote = _quote("1.90", "1.94", bid_at=1_000, ask_at=1_000)

    store.keep(SYMBOL, quote, kept_at_ms=2_000)
    store.keep(SYMBOL, quote, kept_at_ms=62_000)
    store.keep(SYMBOL, quote, kept_at_ms=62_000)
    store.keep(SYMBOL, _quote(None, None), kept_at_ms=63_000)

    stated = store.last_stated(SYMBOL, at_or_before_ms=70_000)
    assert stated is not None and (stated.kept_at_ms, stated.received_ms) == (62_000, 1_000)
    earlier = store.last_stated(SYMBOL, at_or_before_ms=30_000)
    assert earlier is not None and earlier.kept_at_ms == 2_000
    assert store.last_stated(SYMBOL, at_or_before_ms=1_500) is None
    # The same statement twice is one row, and a quote with no side is none.
    assert _rows(tmp_path) == 2


def test_a_quote_put_back_is_written_once_and_says_nothing_of_what_is_current(tmp_path: Path) -> None:
    store = _store(tmp_path)
    old = _quote("1.90", "1.94", bid_at=1_000, ask_at=1_200)

    store.restore(SYMBOL, old)
    store.restore(SYMBOL, old)

    assert _rows(tmp_path) == 1
    newest = store.newest(SYMBOL)
    assert newest is not None and newest.quote.held == old
    # It is kept as of its own receipt, so it is nobody's statement about now.
    assert newest.kept_at_ms == 1_200
    store.keep(SYMBOL, old, kept_at_ms=500_000)
    store.restore(SYMBOL, old)
    assert _rows(tmp_path) == 2


def test_each_contract_has_its_own_newest_and_a_store_never_written_has_none(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert store.newest(SYMBOL) is None
    store.keep(SYMBOL, _quote("1.90", "1.94"), kept_at_ms=2_000)
    assert store.newest("SPY260918C00590000") is None
    # Another handle on the same account reads what the first wrote.
    assert _store(tmp_path).newest(SYMBOL) is not None
