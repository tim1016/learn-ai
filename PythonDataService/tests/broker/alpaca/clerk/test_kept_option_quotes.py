"""The option quotes a simulated account was handed: which one is the newest kept, and how few are kept (#2913)."""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.clerk import kept_option_quotes
from app.broker.alpaca.clerk.kept_option_quotes import (
    COMPACT_ABOVE_ROWS,
    KeptOptionQuote,
    KeptOptionQuotes,
    KeptOptionQuotesUnavailable,
    rows_still_answerable,
)
from app.broker.alpaca.clerk.option_effects import RecordedQuote
from app.broker.contract.errors import BrokerUnavailable
from app.options.quotes import HeldQuote, QuoteDataType, QuoteSide
from app.services.session_authority import et_minute_of_day_ms

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


def test_the_newest_quote_alone_prices_a_close_and_an_older_bid_prices_nothing_once_a_newer_quote_has_none(
    tmp_path: Path,
) -> None:
    """Bid $1.90 at five seconds. At nine seconds the newest quote held has an ask and no bid.

    A sale made between the two is filled at $1.90. One made after the second
    has nothing to be filled from: the $1.90 is what was bid before, and
    nothing kept says it still is. A purchase to close is filled at the ask
    that did arrive. A later quote with a bid prices the sale again.
    """
    store = _store(tmp_path)
    store.keep(SYMBOL, _quote("1.90", "1.94", bid_at=5_000, ask_at=5_000), kept_at_ms=6_000)
    store.keep(SYMBOL, _quote(None, "2.60", ask_at=9_000), kept_at_ms=10_000)

    between = store.closing_quote(SYMBOL, side="sell", at_ms=7_000)
    assert between is not None and between.quote.bid == Decimal("1.90")
    assert store.prices_a_close(SYMBOL, side="sell", at_ms=7_000)
    assert store.closing_quote(SYMBOL, side="sell", at_ms=10_000) is None
    assert not store.prices_a_close(SYMBOL, side="sell", at_ms=10_000)
    to_buy_back = store.closing_quote(SYMBOL, side="buy", at_ms=10_000)
    assert to_buy_back is not None and to_buy_back.quote.ask == Decimal("2.60")
    assert store.prices_a_close(SYMBOL, side="buy", at_ms=10_000)

    store.keep(SYMBOL, _quote("0.80", "0.86", bid_at=12_000, ask_at=12_000), kept_at_ms=12_500)

    bid_again = store.closing_quote(SYMBOL, side="sell", at_ms=13_000)
    assert bid_again is not None and bid_again.quote.bid == Decimal("0.80")
    assert store.prices_a_close(SYMBOL, side="sell", at_ms=13_000)


def test_a_newest_quote_with_nothing_on_a_side_is_a_quote_to_fill_from_and_no_price(tmp_path: Path) -> None:
    """Nothing bid is a bid of zero that arrived. The sale has a quote, and the quote has no price for it."""
    store = _store(tmp_path)
    store.keep(SYMBOL, _quote("1.90", "1.94", bid_at=5_000, ask_at=5_000), kept_at_ms=6_000)
    store.keep(SYMBOL, _quote("0", "0.05", bid_at=9_000, ask_at=9_000), kept_at_ms=10_000)

    newest = store.closing_quote(SYMBOL, side="sell", at_ms=10_000)

    assert newest is not None and newest.quote.bid == Decimal(0)
    assert not store.prices_a_close(SYMBOL, side="sell", at_ms=10_000)


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



# ── what is kept is bounded, and reading it costs what is kept, not what was ever handed over ──

TUESDAY, WEDNESDAY = date(2026, 9, 8), date(2026, 9, 9)
TUESDAY_OPEN = et_minute_of_day_ms(TUESDAY, 9 * 60 + 30)
TUESDAY_CLOSE = et_minute_of_day_ms(TUESDAY, 16 * 60)
WEDNESDAY_OPEN = et_minute_of_day_ms(WEDNESDAY, 9 * 60 + 30)
EVERY_MS = 3_000
FIFTY = tuple(f"SPY260918C00{500 + place:03d}000" for place in range(50))
# What a file of fifty contracts may hold: it is cut once it passes the floor, and a hand-over adds fifty rows.
FIFTY_BOUND_ROWS = COMPACT_ABOVE_ROWS + len(FIFTY)


def _path(tmp_path: Path) -> Path:
    return tmp_path / "accounts" / "alpaca" / "sim:buys-a-call" / "kept_option_quotes.jsonl"


def _as_after_a_restart() -> None:
    """Forget what this process holds in memory of every file, as a new process has."""
    kept_option_quotes._HELD.clear()


def _hand_over(store: KeptOptionQuotes, symbols: tuple[str, ...], *, at_ms: int, bid: str = "1.90") -> None:
    """The runner states the newest quote it holds for each contract, received 200 ms before."""
    quote = _quote(bid, "1.94", bid_at=at_ms - 200, ask_at=at_ms - 200)
    store.keep_all(dict.fromkeys(symbols, quote), kept_at_ms=at_ms)


def _held_in_memory(store: KeptOptionQuotes) -> int:
    """How many rows this process holds in memory of the store's file."""
    return kept_option_quotes._HELD[str(store.path)].rows


def _hand_overs(store: KeptOptionQuotes, symbols: tuple[str, ...], *, first_ms: int, count: int) -> int:
    """``count`` hand-overs three seconds apart; returns when the last was made.

    After each, the writer holds in memory no more than the file may hold. A
    writer that kept the rows a cut took out of the file would cut again at
    every hand-over, over a list that only grows: each of these tests would
    then run for many minutes, so the loop stops at the first such hand-over.
    """
    for step in range(count):
        _hand_over(store, symbols, at_ms=first_ms + step * EVERY_MS)
        assert _held_in_memory(store) <= COMPACT_ABOVE_ROWS + len(symbols), (
            "the writer still holds rows that a cut took out of the file"
        )
    return first_ms + (count - 1) * EVERY_MS


def test_half_an_hour_of_hand_overs_for_fifty_contracts_leaves_a_small_file_that_stops_growing(
    tmp_path: Path,
) -> None:
    """Fifty contracts every three seconds: 10,000 rows handed over in ten minutes, 30,000 in thirty.

    The file is no bigger after thirty minutes than after ten, and the writer
    never reads its own file back to make a hand-over.
    """
    store = _store(tmp_path)

    _hand_overs(store, FIFTY, first_ms=TUESDAY_OPEN, count=200)
    rows_at_ten_minutes = _rows(tmp_path)
    last_ms = _hand_overs(store, FIFTY, first_ms=TUESDAY_OPEN + 200 * EVERY_MS, count=400)

    assert rows_at_ten_minutes <= FIFTY_BOUND_ROWS
    assert _rows(tmp_path) <= FIFTY_BOUND_ROWS, "30,000 rows were handed over"
    assert _path(tmp_path).stat().st_size < 200_000
    assert store.rows_parsed == 0, "a hand-over parsed rows of the file it had just written"
    # Every contract's newest quote and last hand-over are the last ones made.
    for symbol in (FIFTY[0], FIFTY[-1]):
        newest, stated = store.newest(symbol, at_or_before_ms=last_ms), store.last_stated(symbol, at_or_before_ms=last_ms)
        assert newest is not None and newest.received_ms == last_ms - 200
        assert stated is not None and stated.kept_at_ms == last_ms
    assert store.rows_parsed == 0, "a mark read in the process that wrote the file parsed it again"


def test_after_a_restart_one_read_of_the_small_file_answers_every_mark_until_it_changes(tmp_path: Path) -> None:
    """A new process holds nothing in memory. Its first question reads the file once: hundreds of rows, not 30,000."""
    last_ms = _hand_overs(_store(tmp_path), FIFTY, first_ms=TUESDAY_OPEN, count=600)
    in_the_file = _rows(tmp_path)
    _as_after_a_restart()
    reopened = _store(tmp_path)

    marks = [reopened.last_stated(symbol, at_or_before_ms=last_ms, with_side="bid") for symbol in FIFTY]

    assert all(mark is not None and mark.kept_at_ms == last_ms for mark in marks)
    assert reopened.rows_parsed == in_the_file <= FIFTY_BOUND_ROWS
    # Another handle on the same file, as the account's reading makes for each mark, reads nothing more.
    again = _store(tmp_path).last_stated(FIFTY[7], at_or_before_ms=last_ms, with_side="bid")
    assert again is not None and _store(tmp_path).rows_parsed == in_the_file
    # A hand-over by this process is added to what is held, and the file is still not read back.
    _hand_over(reopened, FIFTY, at_ms=last_ms + EVERY_MS)
    after = _store(tmp_path).last_stated(FIFTY[7], at_or_before_ms=last_ms + EVERY_MS)
    assert after is not None and after.kept_at_ms == last_ms + EVERY_MS
    assert reopened.rows_parsed == in_the_file


@pytest.mark.slow
def test_a_whole_day_of_hand_overs_for_fifty_contracts_leaves_a_keep_and_a_mark_read_cheap(tmp_path: Path) -> None:
    """09:30 to 16:00, every three seconds, fifty contracts: 7,800 hand-overs and 390,000 rows.

    Kept whole, that is a file of 80 MB that every mark read parsed from end
    to end. Cut back as it grows, it is the size it was after the first
    minute, the writer has parsed none of it, and a new process reads it once.
    """
    store = _store(tmp_path)

    last_ms = _hand_overs(store, FIFTY, first_ms=TUESDAY_OPEN, count=7_800)

    assert last_ms == TUESDAY_CLOSE - EVERY_MS
    assert _rows(tmp_path) <= FIFTY_BOUND_ROWS and _path(tmp_path).stat().st_size < 200_000
    assert store.rows_parsed == 0
    _as_after_a_restart()
    mark = _store(tmp_path).last_stated(FIFTY[0], at_or_before_ms=TUESDAY_CLOSE, with_side="bid")
    assert mark is not None and mark.kept_at_ms == last_ms
    assert _store(tmp_path).rows_parsed <= FIFTY_BOUND_ROWS


def test_the_mark_at_yesterdays_close_is_still_answered_after_todays_hand_overs_cut_the_file(tmp_path: Path) -> None:
    """A contract held overnight starts the next day from the last bid handed over before the close.

    That hand-over is made at 15:59:57 on Tuesday. Wednesday morning's
    hand-overs cut the file many times over. The mark at Tuesday's close is
    still the one made at 15:59:57, at its own bid, and nothing later.
    """
    store = _store(tmp_path)
    _hand_overs(store, (SYMBOL,), first_ms=TUESDAY_CLOSE - 300 * EVERY_MS, count=299)
    _hand_over(store, (SYMBOL,), at_ms=TUESDAY_CLOSE - EVERY_MS, bid="2.00")
    # After the close too: these are no part of the close's mark.
    _hand_overs(store, (SYMBOL,), first_ms=TUESDAY_CLOSE + 1_000, count=20)

    _hand_overs(store, (SYMBOL,), first_ms=WEDNESDAY_OPEN, count=2_000)

    assert _rows(tmp_path) <= COMPACT_ABOVE_ROWS + 1
    for handle in (store, _store(tmp_path)):
        at_the_close = handle.last_stated(SYMBOL, at_or_before_ms=TUESDAY_CLOSE, with_side="bid")
        assert at_the_close is not None
        assert (at_the_close.kept_at_ms, at_the_close.quote.bid) == (TUESDAY_CLOSE - EVERY_MS, Decimal("2.00"))
    _as_after_a_restart()
    reread = _store(tmp_path).last_stated(SYMBOL, at_or_before_ms=TUESDAY_CLOSE, with_side="bid")
    assert reread is not None and reread.kept_at_ms == TUESDAY_CLOSE - EVERY_MS


def test_the_entrys_own_quote_put_back_after_the_file_was_cut_is_kept_once_more_and_never_twice(
    tmp_path: Path,
) -> None:
    """A close puts the entry's quote back each time it is tried. It costs one row, however often."""
    store = _store(tmp_path)
    entry = _quote("1.90", "1.94", bid_at=TUESDAY_OPEN - 300, ask_at=TUESDAY_OPEN - 300)
    store.keep(SYMBOL, entry, kept_at_ms=TUESDAY_OPEN)
    _hand_overs(store, (SYMBOL,), first_ms=TUESDAY_OPEN + EVERY_MS, count=1_500)
    rows = _rows(tmp_path)

    for _ in range(5):
        store.restore(SYMBOL, entry)

    assert _rows(tmp_path) == rows + 1
    # It is older than every hand-over since, so it is the newest of nothing.
    newest = store.newest(SYMBOL)
    assert newest is not None and newest.received_ms > TUESDAY_OPEN


def _row(seq: int, *, kept_at: int, received: int, bid: str | None = "1.90", ask: str | None = "1.94", symbol: str = SYMBOL) -> KeptOptionQuote:
    quote = _quote(bid, ask, bid_at=received, ask_at=received)
    return KeptOptionQuote(seq=seq, symbol=symbol, quote=RecordedQuote.of(quote), kept_at_ms=kept_at)


def test_what_is_kept_through_a_cut_is_what_a_later_reader_can_ask_for_and_no_more() -> None:
    noon = et_minute_of_day_ms(TUESDAY, 12 * 60)
    rows = [
        _row(1, kept_at=noon, received=noon - 300),
        # The last one received with a bid. A newer quote has none, so this bid prices nothing any more.
        _row(2, kept_at=noon + 3_000, received=noon + 2_900),
        _row(3, kept_at=noon + 6_000, received=noon + 2_000),
        # An ask alone, and the newest by receipt: a purchase is priced from it, and no sale is priced at all.
        _row(4, kept_at=noon + 9_000, received=noon + 8_900, bid=None),
        # The last hand-over made for this contract.
        _row(5, kept_at=noon + 12_000, received=noon + 2_500),
        # Another contract's only row.
        _row(6, kept_at=noon + 13_000, received=noon + 12_900, symbol=FIFTY[0]),
        # Stamped a minute after the latest hand-over in the file: it may yet become the newest.
        _row(7, kept_at=noon + 10_000, received=noon + 74_000),
        # Handed over last of all, as of its own old receipt. Kept, so that its sequence is never given out twice.
        _row(8, kept_at=noon - 5_000, received=noon - 5_000),
    ]

    still = rows_still_answerable(rows, as_of_ms=noon + 13_000)

    # Rows 1 and 3 are behind in both. Row 2 is not kept for its bid: no close is priced from it again.
    assert [row.seq for row in still] == [4, 5, 6, 7, 8]


def test_a_cut_keeps_one_mark_for_each_sessions_close_and_the_last_one_made_before_it() -> None:
    monday_close = et_minute_of_day_ms(date(2026, 9, 14), 16 * 60)
    friday_close = et_minute_of_day_ms(date(2026, 9, 11), 16 * 60)
    rows = [
        _row(1, kept_at=friday_close - 6_000, received=friday_close - 6_200),
        _row(2, kept_at=friday_close, received=friday_close - 200),  # at the close itself: Friday's mark
        _row(3, kept_at=friday_close + 1, received=friday_close - 100),  # a millisecond after it: Monday's, unless...
        _row(4, kept_at=monday_close - 3_000, received=monday_close - 3_200),  # ...a later one is made before Monday's
        _row(5, kept_at=monday_close + 60_000, received=monday_close + 59_800),
    ]

    still = rows_still_answerable(rows, as_of_ms=monday_close + 60_000)

    assert [row.seq for row in still] == [2, 4, 5]


def test_a_cut_is_made_as_of_the_writers_clock_and_never_as_of_a_stamp_ahead_of_it() -> None:
    """A quote put back as of its own receipt can be stamped ahead of the clock.

    Row 2 is: its receipt and its stamp are a minute after now. The newest
    quote received by now is row 1, and the last handed over by now is row
    3. A cut made as of row 2's stamp would keep row 2 for both and drop
    them. A reader asking about now is answered from rows 1 and 3.
    """
    now = et_minute_of_day_ms(TUESDAY, 12 * 60)
    rows = [
        _row(1, kept_at=now - 9_000, received=now - 500, bid="2.20"),
        _row(2, kept_at=now + 60_000, received=now + 60_000, bid="9.00"),
        _row(3, kept_at=now - 3_000, received=now - 8_000, bid="2.10"),
        _row(4, kept_at=now - 1_000, received=now - 1_200, symbol=FIFTY[0]),
    ]

    still = rows_still_answerable(rows, as_of_ms=now)

    assert [row.seq for row in still] == [1, 2, 3, 4]


def test_the_newest_bid_by_now_is_the_same_after_a_cut_as_before_it_when_a_quote_was_put_back_ahead_of_the_clock(
    tmp_path: Path,
) -> None:
    """The entry's own quote is put back stamped a minute ahead of the Clerk's clock. Then the file is cut.

    Before the cut the newest bid received by now is $2.20. It is $2.20
    after it. And once the clock has passed the quote put back, that one is
    the newest: nothing stamped ahead was lost either.
    """
    store = _store(tmp_path)
    now = TUESDAY_OPEN + 600 * EVERY_MS
    ahead = _quote("9.00", "9.06", bid_at=now + 60_000, ask_at=now + 60_000)
    store.restore(SYMBOL, ahead)
    _hand_overs(store, (SYMBOL, FIFTY[0]), first_ms=TUESDAY_OPEN, count=250)
    # The last hand-over before the file is cut. The other contract's row is written after this one's.
    in_time = _quote("2.20", "2.26", bid_at=now - 500, ask_at=now - 500)
    store.keep_all({SYMBOL: in_time, FIFTY[0]: in_time}, kept_at_ms=now - 3_000)
    before = store.closing_quote(SYMBOL, side="sell", at_ms=now)
    rows_before = _rows(tmp_path)

    _hand_overs(store, (FIFTY[0],), first_ms=now - 2_000, count=40)

    assert _rows(tmp_path) < rows_before, "the file was never cut"
    for handle in (store, _store(tmp_path)):
        after = handle.closing_quote(SYMBOL, side="sell", at_ms=now)
        assert before is not None and after is not None
        assert after.quote.bid == before.quote.bid == Decimal("2.20")
        stated = handle.last_stated(SYMBOL, at_or_before_ms=now, with_side="bid")
        assert stated is not None and (stated.kept_at_ms, stated.quote.bid) == (now - 3_000, Decimal("2.20"))
        later = handle.closing_quote(SYMBOL, side="sell", at_ms=now + 60_000)
        assert later is not None and later.quote.bid == Decimal("9.00")


def test_a_cut_made_while_the_newest_quote_has_no_bid_brings_no_older_bid_back_to_price_a_sale(
    tmp_path: Path,
) -> None:
    """Bid $2.00, then a quote with an ask alone, received later than any other. Then the file is cut, many times over.

    The quote with the ask alone is put back as of its own receipt, as an
    exit decision's is, and the older $2.00 is handed over once more after
    it: so the newest quote is the last one handed over for neither side,
    and is kept only for being the newest. A cut that kept the last quote
    with a bid and dropped it would make $2.00 the newest again, and a sale
    would be priced at it. No sale is priced after the cuts, in the process
    that made them or in a new one, until a quote with a bid is kept.
    """
    store = _store(tmp_path)
    bid_two = _quote("2.00", "2.06", bid_at=TUESDAY_OPEN - 200, ask_at=TUESDAY_OPEN - 200)
    store.keep(SYMBOL, bid_two, kept_at_ms=TUESDAY_OPEN)
    store.restore(SYMBOL, _quote(None, "2.10", ask_at=TUESDAY_OPEN + 2_800))
    store.keep(SYMBOL, bid_two, kept_at_ms=TUESDAY_OPEN + 3_000)
    rows_before = _rows(tmp_path)

    now = _hand_overs(store, (FIFTY[0],), first_ms=TUESDAY_OPEN + 6_000, count=3 * COMPACT_ABOVE_ROWS)

    assert _rows(tmp_path) < rows_before + 3 * COMPACT_ABOVE_ROWS, "the file was never cut"
    for restarted in (False, True):
        if restarted:
            _as_after_a_restart()
        handle = _store(tmp_path) if restarted else store
        assert handle.closing_quote(SYMBOL, side="sell", at_ms=now) is None
        assert not handle.prices_a_close(SYMBOL, side="sell", at_ms=now)
        assert handle.prices_a_close(SYMBOL, side="buy", at_ms=now)
    store.keep(SYMBOL, _quote("0.80", "0.86", bid_at=now + 800, ask_at=now + 800), kept_at_ms=now + 1_000)
    sold_at = store.closing_quote(SYMBOL, side="sell", at_ms=now + 1_000)
    assert sold_at is not None and sold_at.quote.bid == Decimal("0.80")


def test_putting_a_quote_back_never_cuts_the_file(tmp_path: Path) -> None:
    """Its stamp is its own receipt, which says nothing of what time it is. The next hand-over cuts, by its clock."""
    store = _store(tmp_path)
    _hand_overs(store, (SYMBOL,), first_ms=TUESDAY_OPEN, count=COMPACT_ABOVE_ROWS)
    assert _rows(tmp_path) == COMPACT_ABOVE_ROWS

    store.restore(SYMBOL, _quote("9.00", "9.06", bid_at=WEDNESDAY_OPEN, ask_at=WEDNESDAY_OPEN))

    assert _rows(tmp_path) == COMPACT_ABOVE_ROWS + 1
    _hand_over(store, (SYMBOL,), at_ms=TUESDAY_OPEN + COMPACT_ABOVE_ROWS * EVERY_MS)
    assert _rows(tmp_path) < COMPACT_ABOVE_ROWS


def test_a_quote_put_back_by_a_caller_that_says_what_time_it_is_cuts_a_file_that_has_grown(tmp_path: Path) -> None:
    """The Clerk's keeping puts back what a line shows while one of its sides has still to arrive, and can do
    so all day with no hand-over between. It knows the time, and that the quote was received by then, so the
    file is cut as of that time and does not grow without end. The newest quote is still the one put back."""
    store = _store(tmp_path)
    last = _hand_overs(store, (SYMBOL,), first_ms=TUESDAY_OPEN, count=COMPACT_ABOVE_ROWS)
    assert _rows(tmp_path) == COMPACT_ABOVE_ROWS
    now = last + 5_000

    store.restore(SYMBOL, _quote(None, "2.04", ask_at=now - 200), now_ms=now)

    assert _rows(tmp_path) < COMPACT_ABOVE_ROWS, "the file was never cut"
    newest = store.newest(SYMBOL, at_or_before_ms=now)
    assert newest is not None and (newest.quote.bid, newest.quote.ask, newest.kept_at_ms) == (None, Decimal("2.04"), now - 200)
    assert not store.prices_a_close(SYMBOL, side="sell", at_ms=now)


@pytest.mark.parametrize(
    ("bid", "ask", "marked_from"),
    [
        pytest.param(None, "2.04", Decimal("1.90"), id="only an ask: the whole quote stated before still stands"),
        pytest.param("2.00", None, None, id="only a bid: nothing is a mark until a whole quote is stated"),
    ],
)
def test_a_bought_contract_is_marked_from_a_whole_statement_and_never_from_one_side_put_back(
    tmp_path: Path, bid: str | None, ask: str | None, marked_from: Decimal | None
) -> None:
    """A mark is a bid, stated with its ask. A line's quote put back while a side was still to arrive is the
    newest quote by receipt, and prices a close alone, but it is no mark: not even its own bid, however
    lately it ticked. An older whole quote's bid does not stand in once the line has shown a newer bid."""
    store = _store(tmp_path)
    store.keep(SYMBOL, _quote("1.90", "1.94", bid_at=9_000, ask_at=9_000), kept_at_ms=10_000)

    store.restore(SYMBOL, _quote(bid, ask, bid_at=12_000, ask_at=12_000), now_ms=12_500)

    mark = store.stated_mark(SYMBOL, at_or_before_ms=12_500)
    assert (None if mark is None else mark.quote.bid) == marked_from
    assert mark is None or (mark.quote.ask, mark.kept_at_ms) == (Decimal("1.94"), 10_000)
    newest = store.newest(SYMBOL, at_or_before_ms=12_500)
    assert newest is not None and newest.kept_at_ms == 12_000
    # A whole quote stated after it is the mark again.
    store.keep(SYMBOL, _quote("2.00", "2.04", bid_at=13_000, ask_at=13_000), kept_at_ms=13_500)
    again = store.stated_mark(SYMBOL, at_or_before_ms=13_500)
    assert again is not None and (again.quote.bid, again.kept_at_ms) == (Decimal("2.00"), 13_500)
    assert store.stated_mark("SPY260918C00590000", at_or_before_ms=13_500) is None


def test_after_a_cut_the_writer_holds_in_memory_what_the_file_holds(tmp_path: Path) -> None:
    store = _store(tmp_path)

    _hand_overs(store, (SYMBOL,), first_ms=TUESDAY_OPEN, count=COMPACT_ABOVE_ROWS + 1)

    assert _rows(tmp_path) < COMPACT_ABOVE_ROWS, "the file was never cut"
    assert _held_in_memory(store) == _rows(tmp_path)


def test_a_file_swapped_for_another_as_long_and_stamped_the_same_instant_is_read_again(tmp_path: Path) -> None:
    """Put back from a copy, or rewritten by another process: the same length, the same time, another file.

    What is held in memory is of the file that was there. The bid in the
    file now is $2.90, and that is the one read.
    """
    store = _store(tmp_path)
    _hand_over(store, (SYMBOL,), at_ms=TUESDAY_OPEN, bid="1.90")
    held = store.newest(SYMBOL)
    assert held is not None and held.quote.bid == Decimal("1.90")
    path = _path(tmp_path)
    was = path.stat()
    another = path.with_name("put-back.jsonl")
    another.write_text(path.read_text().replace('"1.90"', '"2.90"'))
    os.utime(another, ns=(was.st_atime_ns, was.st_mtime_ns))
    os.replace(another, path)
    now = path.stat()
    assert (now.st_size, now.st_mtime_ns) == (was.st_size, was.st_mtime_ns) and now.st_ino != was.st_ino

    read = store.newest(SYMBOL)

    assert read is not None and read.quote.bid == Decimal("2.90")


# ── a file that cannot be read, and one a crash left torn ────────────────


def test_a_line_a_crash_left_half_written_is_passed_over_and_cut_off_by_the_next_hand_over(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _hand_over(store, (SYMBOL,), at_ms=TUESDAY_OPEN)
    with open(_path(tmp_path), "a", encoding="utf-8") as torn:
        torn.write('{"seq":2,"symbol":"SPY2609')
    _as_after_a_restart()
    reopened = _store(tmp_path)

    before = reopened.last_stated(SYMBOL, at_or_before_ms=TUESDAY_OPEN + 60_000)
    _hand_over(reopened, (SYMBOL,), at_ms=TUESDAY_OPEN + EVERY_MS, bid="2.00")

    assert before is not None and before.kept_at_ms == TUESDAY_OPEN
    assert _path(tmp_path).read_text().count("\n") == 2 and _path(tmp_path).read_text().endswith("\n")
    _as_after_a_restart()
    after = _store(tmp_path).last_stated(SYMBOL, at_or_before_ms=TUESDAY_OPEN + 60_000)
    assert after is not None and (after.seq, after.quote.bid) == (2, Decimal("2.00"))


def test_a_whole_line_that_is_not_a_kept_quote_makes_the_quotes_unavailable_by_name(tmp_path: Path) -> None:
    """Not a bare runtime error: the same typed "unavailable" a broker that cannot answer raises."""
    store = _store(tmp_path)
    _hand_over(store, (SYMBOL,), at_ms=TUESDAY_OPEN)
    with open(_path(tmp_path), "a", encoding="utf-8") as spoiled:
        spoiled.write('{"seq":2,"symbol":"SPY260918C00585000","quote":"not a quote","kept_at_ms":1}\n')
    _as_after_a_restart()
    reopened = _store(tmp_path)

    for ask in (
        lambda: reopened.newest(SYMBOL),
        lambda: reopened.last_stated(SYMBOL, at_or_before_ms=TUESDAY_OPEN + 60_000),
        lambda: _hand_over(reopened, (SYMBOL,), at_ms=TUESDAY_OPEN + EVERY_MS),
        lambda: reopened.restore(SYMBOL, _quote("1.90", "1.94")),
    ):
        with pytest.raises(KeptOptionQuotesUnavailable) as unavailable:
            ask()
        assert isinstance(unavailable.value, BrokerUnavailable)
    assert _rows(tmp_path) == 2, "nothing was written over a file that could not be read"

