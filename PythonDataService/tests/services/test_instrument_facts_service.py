"""The facts behind an instrument header and a Markets list row: price basis, as-of and day move (#3502).

The sources are fakes at the service's own seams (live line, vendor snapshot, lake closes, archive receipts); each
adapter has its own test against its real transport. Dates sit in October 2026 and November 2026: Monday the 19th
to Friday the 23rd, and the Thanksgiving week whose Friday is a half-day.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from fractions import Fraction
from zoneinfo import ZoneInfo

import pytest

from app.schemas.instrument_facts import AvailableQuote, UnavailableQuote
from app.schemas.option_archive import ArchiveGap, OptionArchiveHealth, UnderlyingArchiveHealth
from app.services.instrument_facts_service import (
    CLOSES_ADJUSTMENT,
    InstrumentFactsService,
    InvalidSymbolsError,
    Miss,
    Observation,
    Reading,
    archive_fact,
    day_move,
    parse_symbols,
)

_ET = ZoneInfo("America/New_York")


def et_ms(day: date, hour: int, minute: int = 0) -> int:
    return int(datetime(day.year, day.month, day.day, hour, minute, tzinfo=_ET).timestamp() * 1000)


class FakeLive:
    def __init__(self, readings: Mapping[str, Reading] | None = None) -> None:
        self.readings = dict(readings or {})
        self.asked: list[str] = []

    def latest(self, symbol: str, now_ms: int) -> Reading:
        self.asked.append(symbol)
        return self.readings.get(symbol, Miss(f"No live line is streaming {symbol}."))


class FakeSnapshots:
    def __init__(self, readings: Mapping[str, Reading] | None = None) -> None:
        self.readings = dict(readings or {})
        self.batches: list[list[str]] = []

    async def latest(self, symbols: Sequence[str], now_ms: int) -> Mapping[str, Reading]:
        self.batches.append(list(symbols))
        return {symbol: self.readings.get(symbol, Miss("The vendor snapshot could not be read.")) for symbol in symbols}


class FakeCloses:
    def __init__(self, table: Mapping[tuple[str, date], float] | None = None) -> None:
        self.table = dict(table or {})
        self.calls: list[tuple[list[str], list[date]]] = []

    def closes(self, symbols: Sequence[str], sessions: Sequence[date]) -> Mapping[str, Mapping[date, float | Miss]]:
        self.calls.append((list(symbols), list(sessions)))
        return {
            symbol: {day: self.table.get((symbol, day), Miss(f"The lake holds no bars for {symbol} on {day}.")) for day in sessions}
            for symbol in symbols
        }


class FakeArchive:
    def __init__(self, result: OptionArchiveHealth | Miss | None = None) -> None:
        self.result = result if result is not None else Miss("The option archive is not running in this process.")
        self.reads = 0

    def health(self) -> OptionArchiveHealth | Miss:
        self.reads += 1
        return self.result


def make_service(
    now_ms: int,
    *,
    live: FakeLive | None = None,
    snapshots: FakeSnapshots | None = None,
    closes: FakeCloses | None = None,
    archive: FakeArchive | None = None,
) -> InstrumentFactsService:
    return InstrumentFactsService(
        live=live or FakeLive(),
        snapshots=snapshots or FakeSnapshots(),
        closes=closes or FakeCloses(),
        archive=archive or FakeArchive(),
        clock=lambda: now_ms,
    )


def available(quote: object) -> AvailableQuote:
    assert isinstance(quote, AvailableQuote), quote
    return quote


THU = date(2026, 10, 22)
WED = date(2026, 10, 21)
TUE = date(2026, 10, 20)
FRI = date(2026, 10, 23)


# --- the basis ladder ------------------------------------------------------------------------------------------------


async def test_a_live_line_gives_a_live_quote_and_the_vendor_is_never_asked() -> None:
    now = et_ms(THU, 11)
    snapshots = FakeSnapshots()
    service = make_service(
        now,
        live=FakeLive({"SPY": Observation(760.0, et_ms(THU, 10, 59))}),
        snapshots=snapshots,
        closes=FakeCloses({("SPY", WED): 758.0, ("SPY", TUE): 750.0}),
    )

    response = await service.facts(["SPY"])

    quote = available(response.facts[0].quote)
    assert (quote.basis, quote.price, quote.as_of_ms) == ("live_quote", 760.0, et_ms(THU, 10, 59))
    assert quote.not_used == []
    assert snapshots.batches == []


async def test_without_a_live_line_the_vendor_snapshot_is_the_price_and_says_why_it_is_not_live() -> None:
    now = et_ms(THU, 11)
    service = make_service(
        now,
        snapshots=FakeSnapshots({"SPY": Observation(759.5, et_ms(THU, 10, 45))}),
        closes=FakeCloses({("SPY", WED): 758.0, ("SPY", TUE): 750.0}),
    )

    quote = available((await service.facts(["SPY"])).facts[0].quote)

    assert (quote.basis, quote.price, quote.as_of_ms) == ("delayed_snapshot", 759.5, et_ms(THU, 10, 45))
    assert [(item.basis, "SPY" in item.reason) for item in quote.not_used] == [("live_quote", True)]


async def test_a_snapshot_that_observed_nothing_after_the_last_close_is_not_the_latest_price() -> None:
    # Saturday: the vendor still holds Friday's 15:00 print, before Friday's close.
    now = et_ms(date(2026, 10, 24), 12)
    service = make_service(
        now,
        snapshots=FakeSnapshots({"SPY": Observation(755.0, et_ms(FRI, 15))}),
        closes=FakeCloses({("SPY", FRI): 759.97, ("SPY", THU): 766.89}),
    )

    quote = available((await service.facts(["SPY"])).facts[0].quote)

    assert (quote.basis, quote.price) == ("last_close", 759.97)
    assert [item.basis for item in quote.not_used] == ["live_quote", "delayed_snapshot"]


async def test_with_no_fresher_source_the_price_is_the_last_completed_close_at_the_calendars_close() -> None:
    now = et_ms(date(2026, 10, 24), 12)
    service = make_service(now, closes=FakeCloses({("SPY", FRI): 759.97, ("SPY", THU): 766.89}))

    quote = available((await service.facts(["SPY"])).facts[0].quote)

    assert (quote.basis, quote.price, quote.as_of_ms) == ("last_close", 759.97, et_ms(FRI, 16))
    assert quote.move is not None
    assert quote.move.comparison_close.price == 766.89
    assert quote.move.comparison_close.session_close_ms == et_ms(THU, 16)


async def test_after_a_half_day_the_last_close_is_the_early_close_and_the_move_skips_the_holiday() -> None:
    # Black Friday 2026-11-27 closes at 13:00 ET; Thanksgiving, the 26th, is no session.
    friday, wednesday = date(2026, 11, 27), date(2026, 11, 25)
    service = make_service(et_ms(friday, 14), closes=FakeCloses({("SPY", friday): 700.0, ("SPY", wednesday): 690.0}))

    quote = available((await service.facts(["SPY"])).facts[0].quote)

    assert quote.as_of_ms == et_ms(friday, 13)
    assert quote.move is not None
    assert quote.move.comparison_close.session_close_ms == et_ms(wednesday, 16)


async def test_an_after_hours_price_is_measured_from_the_prior_days_close_not_the_close_it_follows() -> None:
    now = et_ms(THU, 17, 30)
    service = make_service(
        now,
        snapshots=FakeSnapshots({"SPY": Observation(761.0, et_ms(THU, 17, 20))}),
        closes=FakeCloses({("SPY", THU): 760.0, ("SPY", WED): 758.0}),
    )

    quote = available((await service.facts(["SPY"])).facts[0].quote)

    assert quote.basis == "delayed_snapshot"
    assert quote.move is not None
    assert quote.move.comparison_close.price == 758.0
    assert quote.move.comparison_close.session_close_ms == et_ms(WED, 16)


# --- failures cost only their own fields -------------------------------------------------------------------------------


async def test_when_every_source_fails_the_quote_is_unavailable_and_says_what_each_said() -> None:
    service = make_service(et_ms(THU, 11))

    quote = (await service.facts(["SPY"])).facts[0].quote

    assert isinstance(quote, UnavailableQuote)
    assert [item.basis for item in quote.not_used] == ["live_quote", "delayed_snapshot", "last_close"]
    assert all(item.reason for item in quote.not_used)


async def test_one_symbols_failed_source_leaves_the_others_standing() -> None:
    service = make_service(
        et_ms(date(2026, 10, 24), 12),
        closes=FakeCloses({("SPY", FRI): 759.97, ("SPY", THU): 766.89}),
    )

    response = await service.facts(["SPY", "QQQ"])

    assert [fact.quote.status for fact in response.facts] == ["available", "unavailable"]
    assert response.facts[0].quote.price == 759.97  # type: ignore[union-attr]


async def test_a_price_with_no_comparison_close_keeps_its_price_and_says_why_it_has_no_move() -> None:
    service = make_service(
        et_ms(THU, 11),
        live=FakeLive({"SPY": Observation(760.0, et_ms(THU, 10, 59))}),
        closes=FakeCloses({("SPY", TUE): 750.0}),  # the lake lacks Wednesday, the close to measure from
    )

    quote = available((await service.facts(["SPY"])).facts[0].quote)

    assert quote.price == 760.0
    assert quote.move is None
    assert quote.move_unavailable_reason is not None
    assert str(WED) in quote.move_unavailable_reason


# --- a list page is one batch ------------------------------------------------------------------------------------------


async def test_twenty_rows_cost_one_snapshot_batch_and_one_lake_read_and_ask_the_live_line_once_each() -> None:
    symbols = [f"S{letter}" for letter in "ABCDEFGHIJKLMNOPQRST"]
    live = FakeLive({symbols[0]: Observation(10.0, et_ms(THU, 10, 59))})
    snapshots, closes = FakeSnapshots(), FakeCloses()
    service = make_service(et_ms(THU, 11), live=live, snapshots=snapshots, closes=closes)

    response = await service.facts(symbols)

    assert [fact.symbol for fact in response.facts] == symbols
    assert live.asked == symbols
    assert snapshots.batches == [symbols[1:]]
    assert len(closes.calls) == 1
    assert closes.calls[0][0] == symbols
    assert response.closes_adjustment == CLOSES_ADJUSTMENT


# --- the archive -------------------------------------------------------------------------------------------------------


def underlying(**fields: object) -> UnderlyingArchiveHealth:
    base: dict[str, object] = {
        "underlying": "SPY",
        "severity": "healthy",
        "reason": "ok",
        "pending_sessions": 0,
        "partial_sessions": 0,
        "lost_sessions": 0,
        "gaps": [],
        "phase": "waiting",
    }
    return UnderlyingArchiveHealth(**{**base, **fields})  # type: ignore[arg-type]


def health(*rows: UnderlyingArchiveHealth, enabled: bool = True) -> OptionArchiveHealth:
    placeholder = {"severity": "healthy", "reason": "ok"}
    return OptionArchiveHealth(
        enabled=enabled,
        generated_ms=1,
        attention=False,
        underlyings=list(rows),
        backup=placeholder,  # type: ignore[arg-type]
        test_restore=placeholder,  # type: ignore[arg-type]
    )


def test_an_archive_with_every_required_day_is_complete_and_one_with_gaps_is_only_some_days() -> None:
    gap = ArchiveGap(session_ms=5, permanent=False, days_left=3, reason="pending", recorded_ms=6)
    every_day = archive_fact(
        underlying(last_archived_session_ms=et_ms(THU, 9, 30), earliest_required_session_ms=et_ms(TUE, 9, 30), complete_through_session_ms=et_ms(THU, 9, 30)),
        capturing=True,
    )
    some_days = archive_fact(
        underlying(last_archived_session_ms=et_ms(THU, 9, 30), pending_sessions=2, lost_sessions=1, gaps=[gap]),
        capturing=True,
    )
    nothing = archive_fact(underlying(pending_sessions=4), capturing=False)

    assert (every_day.kind, every_day.missing_sessions) == ("complete", 0)
    assert (some_days.kind, some_days.missing_sessions) == ("some_days", 3)
    assert (nothing.kind, nothing.capturing) == ("none_stored", False)


async def test_only_an_archived_underlying_carries_an_archive_fact_and_the_receipts_are_read_once() -> None:
    archive = FakeArchive(health(underlying(last_archived_session_ms=et_ms(THU, 9, 30)), underlying(underlying="QQQ")))
    service = make_service(et_ms(THU, 11), archive=archive)

    response = await service.facts(["SPY", "AAPL", "QQQ"])

    assert [(fact.symbol, fact.options_archive.kind if fact.options_archive else None) for fact in response.facts] == [
        ("SPY", "complete"),
        ("AAPL", None),
        ("QQQ", "none_stored"),
    ]
    assert archive.reads == 1


async def test_unreadable_receipts_leave_only_the_archive_fact_unavailable() -> None:
    service = make_service(
        et_ms(date(2026, 10, 24), 12),
        closes=FakeCloses({("SPY", FRI): 759.97, ("SPY", THU): 766.89}),
        archive=FakeArchive(Miss("The option archive's receipts cannot be read.")),
    )

    fact = (await service.facts(["SPY"])).facts[0]

    assert fact.options_archive is not None
    assert (fact.options_archive.kind, fact.options_archive.reason) == ("unavailable", "The option archive's receipts cannot be read.")
    assert fact.quote.status == "available"


async def test_a_page_with_no_archived_underlying_never_reads_the_receipts() -> None:
    archive = FakeArchive()
    await make_service(et_ms(THU, 11), archive=archive).facts(["AAPL", "MSFT"])

    assert archive.reads == 0


# --- the move arithmetic -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("price", "close"),
    [(760.0, 758.0), (759.97, 766.89), (10.1, 10.0), (0.4523, 0.4519), (1234.5678, 1234.5678)],
)
def test_day_move_matches_hand_computed_cases(price: float, close: float) -> None:
    moved = day_move(price, close, comparison_session_close_ms=1)

    assert not isinstance(moved, Miss)
    exact_change = Fraction(str(price)) - Fraction(str(close))
    assert moved.change == pytest.approx(float(exact_change), abs=1e-9, rel=0)
    assert moved.change_percent == pytest.approx(float(exact_change / Fraction(str(close)) * 100), abs=1e-9, rel=0)
    assert moved.comparison_close.price == close


def test_a_move_reads_as_the_two_prices_say_not_with_float_noise() -> None:
    moved = day_move(759.97, 766.89, comparison_session_close_ms=1)

    assert not isinstance(moved, Miss)
    assert moved.change == -6.92


@pytest.mark.parametrize("close", [0.0, -1.0, float("nan"), float("inf")])
def test_no_move_is_stated_from_a_close_that_is_not_a_price(close: float) -> None:
    assert isinstance(day_move(10.0, close, comparison_session_close_ms=1), Miss)


# --- the symbols a caller may ask about ------------------------------------------------------------------------------------


def test_symbols_are_trimmed_and_kept_in_the_order_asked() -> None:
    assert parse_symbols("SPY, QQQ ,BRK.B") == ["SPY", "QQQ", "BRK.B"]


@pytest.mark.parametrize("raw", ["", "SPY,,QQQ", "spy", "SPY,SPY", "TOOLONGSYMBOL", "SPY;DROP", ",".join(f"A{c}" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ")])
def test_a_list_that_is_not_bounded_distinct_us_equity_symbols_is_refused(raw: str) -> None:
    with pytest.raises(InvalidSymbolsError):
        parse_symbols(raw)
