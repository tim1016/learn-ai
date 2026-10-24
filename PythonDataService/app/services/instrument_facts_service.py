"""The facts an instrument header and a Markets list row carry: the latest price with its basis and as-of time, and the day move.

Formula: change = price - comparison_close; change_percent = 100 * change / comparison_close
Reference: the definition of a day change, measured from the close of the last completed session dated before the
  price's own ET day. The inputs are read, never derived here: a price from the live feed, the vendor's delayed
  snapshot or the lake's last completed close; a close from the lake. External sources, not independently validated.
Canonical implementation: this file (``day_move``). The Angular header and list render these numbers and compute none.
Validated against: tests/services/test_instrument_facts_service.py::test_day_move_matches_hand_computed_cases

The price is the freshest source that answers, and the answer names which:

1. ``live_quote``: the newest closed 1-minute bar of a live IBKR line this process already holds open. The read never
   opens a line, so asking for a list page costs no market-data subscription. The data plane may hold no Gateway
   session; then no line is open and the next source answers.
2. ``delayed_snapshot``: the vendor's snapshot (15-minute delayed on this plan), read in one paced batch request for
   every row that needs it, within a deadline, and only when it observed a trade after the last completed close. Its
   as-of time is the end of the minute the price belongs to; the snapshot's own update stamp is only the bound on it,
   because a quote can move that stamp after the last trade.
3. ``last_close``: the regular-hours close of the last completed session, read from the lake. The canonical calendar
   decides which session that is, so a half-day, a holiday and a weekend need no literal anywhere.

A source that was asked and did not answer is listed on the quote (``not_used``) with its reason, so a price that is
not live says why. When none answers the quote is ``unavailable`` and carries the same reasons; the instrument still
opens. The chart window is not an input: a window cannot change what the latest price is.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

from app.broker.contract.models import US_EQUITY_SYMBOL_PATTERN
from app.data_lake.types import PriceAdjustmentMode
from app.lean_sidecar.completed_sessions import latest_completed_sessions
from app.lean_sidecar.trading_calendar import SessionWindow
from app.option_archive.planner import UNDERLYINGS as ARCHIVED_UNDERLYINGS
from app.schemas.instrument_facts import (
    MAX_FACT_SYMBOLS,
    AvailableQuote,
    BasisNotUsed,
    ComparisonClose,
    DayMove,
    InstrumentFacts,
    InstrumentFactsResponse,
    InstrumentQuote,
    OptionsArchiveFact,
    QuoteBasis,
    UnavailableQuote,
)
from app.schemas.option_archive import OptionArchiveHealth, UnderlyingArchiveHealth
from app.utils.session_anchors import et_date_at_ms
from app.utils.timestamps import now_ms_utc

#: The lake tree every close is read from: the one the instrument place opens, so a header's held span and its
#: close are the same bars.
CLOSES_ADJUSTMENT: PriceAdjustmentMode = "polygon_split_adjusted"


@dataclass(frozen=True)
class Observation:
    """A price and the instant it was observed."""

    price: float
    as_of_ms: int


@dataclass(frozen=True)
class Miss:
    """A source that was asked and could not answer, in a sentence."""

    reason: str


type Reading = Observation | Miss


class LiveQuoteSource(Protocol):
    def latest(self, symbol: str, now_ms: int) -> Reading:
        """The live price of a line already open for ``symbol``; never opens one."""
        ...


class SnapshotSource(Protocol):
    async def latest(self, symbols: Sequence[str], now_ms: int) -> Mapping[str, Reading]:
        """One batch read of the delayed snapshot for ``symbols``."""
        ...


class CloseSource(Protocol):
    def closes(self, symbols: Sequence[str], sessions: Sequence[date]) -> Mapping[str, Mapping[date, float | Miss]]:
        """Each symbol's regular-hours close for each session. Blocking: callers run it off the event loop."""
        ...


class ArchiveSource(Protocol):
    def health(self) -> OptionArchiveHealth | Miss:
        """The archive's per-underlying receipts. Blocking: callers run it off the event loop."""
        ...


class InvalidSymbolsError(ValueError):
    """The ``symbols`` a caller asked for are not a bounded list of distinct US-equity symbols."""


def parse_symbols(raw: str) -> list[str]:
    """``"SPY,QQQ"`` as a list of distinct US-equity symbols, in the order asked."""
    symbols = [part.strip() for part in raw.split(",")]
    invalid = [symbol for symbol in symbols if re.fullmatch(US_EQUITY_SYMBOL_PATTERN, symbol) is None]
    if invalid:
        raise InvalidSymbolsError(f"These are not US-equity symbols: {', '.join(repr(symbol) for symbol in invalid[:5])}.")
    if len(set(symbols)) != len(symbols):
        raise InvalidSymbolsError("A symbol is named more than once.")
    if len(symbols) > MAX_FACT_SYMBOLS:
        raise InvalidSymbolsError(f"{len(symbols)} symbols asked; one read answers at most {MAX_FACT_SYMBOLS}.")
    return symbols


def day_move(price: float, comparison_close: float, *, comparison_session_close_ms: int) -> DayMove | Miss:
    """``price`` measured from ``comparison_close``, in decimal so a change reads 6.92 and not 6.920000000000073."""
    if not (math.isfinite(price) and math.isfinite(comparison_close)) or comparison_close <= 0:
        return Miss("The comparison close is not a usable price, so no move is stated.")
    change = Decimal(repr(price)) - Decimal(repr(comparison_close))
    return DayMove(
        change=float(change),
        change_percent=float(change * 100 / Decimal(repr(comparison_close))),
        comparison_close=ComparisonClose(price=comparison_close, session_close_ms=comparison_session_close_ms),
    )


def archive_fact(underlying: UnderlyingArchiveHealth, *, capturing: bool) -> OptionsArchiveFact:
    """What the archive holds of one underlying: some days, or every required day.

    ``complete`` means every session the archive requires is stored. It does not say a narrower window a page
    asks about is covered; the span it states is the archive's own.
    """
    missing = underlying.pending_sessions + underlying.lost_sessions
    if underlying.last_archived_session_ms is None:
        kind = "none_stored"
    elif missing == 0:
        kind = "complete"
    else:
        kind = "some_days"
    return OptionsArchiveFact(
        kind=kind,
        earliest_required_session_ms=underlying.earliest_required_session_ms,
        last_archived_session_ms=underlying.last_archived_session_ms,
        complete_through_session_ms=underlying.complete_through_session_ms,
        missing_sessions=missing,
        capturing=capturing,
        reason=None,
    )


def _archive_unavailable(reason: str) -> OptionsArchiveFact:
    return OptionsArchiveFact(
        kind="unavailable",
        earliest_required_session_ms=None,
        last_archived_session_ms=None,
        complete_through_session_ms=None,
        missing_sessions=None,
        capturing=None,
        reason=reason,
    )


class InstrumentFactsService:
    """Answers one bounded batch of symbols with each symbol's facts; a failed source costs only its own fields."""

    def __init__(
        self,
        *,
        live: LiveQuoteSource,
        snapshots: SnapshotSource,
        closes: CloseSource,
        archive: ArchiveSource,
        clock: Callable[[], int] = now_ms_utc,
    ) -> None:
        self._live = live
        self._snapshots = snapshots
        self._closes = closes
        self._archive = archive
        self._clock = clock

    async def facts(self, symbols: Sequence[str]) -> InstrumentFactsResponse:
        now_ms = self._clock()
        previous, last = latest_completed_sessions(now_ms, 2)

        live = {symbol: self._live.latest(symbol, now_ms) for symbol in symbols}
        wanting_snapshot = [symbol for symbol, reading in live.items() if isinstance(reading, Miss)]
        snapshots = await self._snapshots.latest(wanting_snapshot, now_ms) if wanting_snapshot else {}
        tracked = any(symbol in ARCHIVED_UNDERLYINGS for symbol in symbols)
        closes, archive = await asyncio.gather(
            asyncio.to_thread(self._closes.closes, symbols, (previous.session_date, last.session_date)),
            asyncio.to_thread(self._archive.health) if tracked else _nothing(),
        )

        return InstrumentFactsResponse(
            generated_ms=now_ms,
            closes_adjustment=CLOSES_ADJUSTMENT,
            facts=[
                InstrumentFacts(
                    symbol=symbol,
                    quote=_quote(
                        live=live[symbol],
                        snapshot=snapshots.get(symbol, Miss("The vendor returned no snapshot for this symbol.")),
                        closes=closes.get(symbol, {}),
                        sessions=(previous, last),
                    ),
                    options_archive=_archive_for(symbol, archive),
                )
                for symbol in symbols
            ],
        )


async def _nothing() -> None:
    return None


def _archive_for(symbol: str, archive: OptionArchiveHealth | Miss | None) -> OptionsArchiveFact | None:
    if symbol not in ARCHIVED_UNDERLYINGS or archive is None:
        return None
    if isinstance(archive, Miss):
        return _archive_unavailable(archive.reason)
    for underlying in archive.underlyings:
        if underlying.underlying == symbol:
            return archive_fact(underlying, capturing=archive.enabled)
    return _archive_unavailable(f"The archive's receipts hold no entry for {symbol}.")


def _quote(
    *,
    live: Reading,
    snapshot: Reading,
    closes: Mapping[date, float | Miss],
    sessions: tuple[SessionWindow, SessionWindow],
) -> InstrumentQuote:
    previous, last = sessions
    not_used: list[BasisNotUsed] = []
    basis: QuoteBasis
    if isinstance(live, Observation):
        basis, observed = "live_quote", live
    else:
        not_used.append(BasisNotUsed(basis="live_quote", reason=live.reason))
        if isinstance(snapshot, Observation) and snapshot.as_of_ms > last.close_ms_utc:
            basis, observed = "delayed_snapshot", snapshot
        else:
            reason = (
                "The vendor snapshot holds nothing newer than the last completed close."
                if isinstance(snapshot, Observation)
                else snapshot.reason
            )
            not_used.append(BasisNotUsed(basis="delayed_snapshot", reason=reason))
            close = closes.get(last.session_date, Miss(f"The lake was not asked for {last.session_date.isoformat()}."))
            if isinstance(close, Miss):
                not_used.append(BasisNotUsed(basis="last_close", reason=close.reason))
                return UnavailableQuote(
                    reason="No source supplied a price: the live feed, the vendor snapshot and the lake each said why below.",
                    not_used=not_used,
                )
            basis, observed = "last_close", Observation(price=close, as_of_ms=last.close_ms_utc)

    move, why_not = _move_for(observed, closes, (previous, last))
    return AvailableQuote(
        price=observed.price,
        basis=basis,
        as_of_ms=observed.as_of_ms,
        move=move,
        move_unavailable_reason=why_not,
        not_used=not_used,
    )


def _move_for(
    observed: Observation,
    closes: Mapping[date, float | Miss],
    sessions: tuple[SessionWindow, SessionWindow],
) -> tuple[DayMove | None, str | None]:
    """The move from the close of the last completed session dated before the price's own ET day."""
    price_day = et_date_at_ms(observed.as_of_ms)
    comparison = next((window for window in reversed(sessions) if window.session_date < price_day), None)
    if comparison is None:
        return None, "No completed session precedes the day this price was observed, so there is no close to measure from."
    close = closes.get(comparison.session_date, Miss(f"The lake was not asked for {comparison.session_date.isoformat()}."))
    if isinstance(close, Miss):
        return None, close.reason
    moved = day_move(observed.price, close, comparison_session_close_ms=comparison.close_ms_utc)
    return (None, moved.reason) if isinstance(moved, Miss) else (moved, None)
