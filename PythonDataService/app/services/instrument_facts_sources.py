"""The four sources behind the instrument facts: a live line, the vendor snapshot, the lake's closes and the archive.

Each adapter answers with a reading or a ``Miss`` that says why; none raises into the caller, so one failed source
costs only the fields it feeds (PRD #3490 R08). None of them starts work per symbol that a list page could not
afford: the live read opens no line, the snapshot is one vendor request for the whole batch, and the lake read is
two committed minute files per symbol.

IBKR is the read-only live feed and Polygon the delayed one. No Alpaca market data is read here.
"""

from __future__ import annotations

import asyncio
import logging
import math
import zipfile
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from app.data_lake.admission import LakeAdmissionError
from app.data_lake.catalog_client import CatalogUnavailableError
from app.data_lake.path_policy import resolve_lake_root
from app.data_lake.types import PriceAdjustmentMode, is_lake_addressable_symbol
from app.engine.data.lean_format import LeanMinuteDataReader
from app.schemas.option_archive import OptionArchiveHealth
from app.services.instrument_facts_service import Miss, Observation, Reading
from app.utils.session_anchors import MAX_TIMESTAMP_MS

if TYPE_CHECKING:
    from app.broker.ibkr.bar_models import IbkrMinuteBar
    from app.option_archive.service import OptionArchiveService
    from app.services.live_bar_aggregator import LiveLineStatus

logger = logging.getLogger(__name__)

#: A live line's newest closed minute may be this old and still be the live price: one minute bar's period, twice,
#: so a bar that is a little late to close does not flip the label to delayed for a second.
LIVE_QUOTE_MAX_AGE_MS = 2 * 60_000
#: The vendor stamps a snapshot with the nanosecond it was updated; one stamped further ahead than this is wrong.
SNAPSHOT_FUTURE_TOLERANCE_MS = 60_000
#: How long the one vendor snapshot request may take, pacing included, before the rows fall back to the last close. A
#: header or a list page waits at most this long on a slow vendor (the SDK's own timeouts are 10 s each, with retries).
SNAPSHOT_DEADLINE_S = 5.0
_NANOSECONDS_PER_MILLISECOND = 1_000_000
#: Nanoseconds since the epoch for 1973 and for the end of year 9999: outside it, the stamp is not nanoseconds.
_MIN_PLAUSIBLE_NS = 10**17
_MAX_PLAUSIBLE_NS = MAX_TIMESTAMP_MS * _NANOSECONDS_PER_MILLISECOND
#: The same bounds in milliseconds, for the minute bar's own start.
_MIN_PLAUSIBLE_MS = _MIN_PLAUSIBLE_NS // _NANOSECONDS_PER_MILLISECOND
_MINUTE_MS = 60_000


class LiveBars(Protocol):
    """The slice of ``LiveBarAggregator`` a read needs: lines already open, never a new one."""

    def status(self, symbol: str) -> LiveLineStatus: ...

    def snapshot(self, symbol: str, since_ms: int | None = None) -> list[IbkrMinuteBar]: ...


class AggregatorLiveQuoteSource:
    """The live price of a symbol whose 1-minute line this process already streams.

    Reads the aggregator's buffers and nothing else, so a list of twenty rows opens no line. A line that is not
    streaming, has no closed bar yet, or whose newest bar is older than the bound is not a live price.
    """

    def __init__(self, bars: LiveBars, *, max_age_ms: int = LIVE_QUOTE_MAX_AGE_MS) -> None:
        self._bars = bars
        self._max_age_ms = max_age_ms

    def latest(self, symbol: str, now_ms: int) -> Reading:
        line = self._bars.status(symbol)
        if line.status != "streaming":
            return Miss(f"No live line is streaming {symbol} in this process (line state: {line.status}).")
        buffered = self._bars.snapshot(symbol)
        if not buffered:
            return Miss(f"The live line for {symbol} has not closed a minute bar yet.")
        newest = buffered[-1]
        age_ms = now_ms - newest.end_ms
        if age_ms < 0:
            return Miss(f"The newest live bar for {symbol} ends after now, so it cannot be read as the live price.")
        if age_ms > self._max_age_ms:
            return Miss(
                f"The newest live bar for {symbol} closed {age_ms // 1000} s ago, past the "
                f"{self._max_age_ms // 1000} s a live price may be."
            )
        return Observation(price=float(newest.close), as_of_ms=newest.end_ms)


class SnapshotBatchClient(Protocol):
    def get_stock_snapshots(self, tickers: Sequence[str]) -> list[dict[str, Any]]: ...


class PolygonSnapshotSource:
    """The vendor's delayed snapshot: one paced request for every symbol that wants it, with a deadline of its own.

    The client paces the request against the plan's per-minute allowance (``get_stock_snapshots`` takes the same
    throttle every other vendor call does). ``deadline_s`` bounds the whole call, pacing included: a vendor that does
    not answer in time costs the rows their snapshot, said as a reason, and not the page its answer. The request
    itself runs on a worker thread that cannot be interrupted, so a late answer is dropped, not used.
    """

    def __init__(self, client: SnapshotBatchClient, *, deadline_s: float = SNAPSHOT_DEADLINE_S) -> None:
        self._client = client
        self._deadline_s = deadline_s

    async def latest(self, symbols: Sequence[str], now_ms: int) -> Mapping[str, Reading]:
        if not symbols:
            return {}
        try:
            rows = await asyncio.wait_for(
                asyncio.to_thread(self._client.get_stock_snapshots, list(symbols)), timeout=self._deadline_s
            )
        except TimeoutError:
            logger.warning(
                "The vendor snapshot did not answer in time",
                extra={"action": "instrument_facts_snapshot_timeout", "symbols": len(symbols), "deadline_s": self._deadline_s},
            )
            reason = f"The vendor snapshot did not answer within {self._deadline_s:g} s."
            return {symbol: Miss(reason) for symbol in symbols}
        except Exception as exc:
            # The vendor SDK raises its own errors and urllib3's; whichever it is, the snapshot is unavailable for this
            # batch and the rows fall back to the last close, saying so. The failure itself is logged in full.
            logger.warning(
                "The vendor snapshot could not be read",
                extra={"action": "instrument_facts_snapshot_failed", "symbols": len(symbols), "error": str(exc)},
                exc_info=True,
            )
            reason = f"The vendor snapshot could not be read ({type(exc).__name__})."
            return {symbol: Miss(reason) for symbol in symbols}
        by_symbol = {row.get("ticker"): row for row in rows}
        return {
            symbol: _snapshot_reading(by_symbol[symbol], now_ms)
            if symbol in by_symbol
            else Miss(f"The vendor holds no snapshot for {symbol} right now.")
            for symbol in symbols
        }


def _positive_price(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) and value > 0 else None


def _plausible_ms(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and _MIN_PLAUSIBLE_MS <= value <= MAX_TIMESTAMP_MS


def _snapshot_reading(row: Mapping[str, Any], now_ms: int) -> Reading:
    """The snapshot's latest trade price and the instant that price belongs to.

    The price is the newest minute bar's close, else the day's close so far. The vendor clears a snapshot at
    midnight ET and repopulates it from about 04:00, so before that it holds no price and the read falls through.

    A minute bar's close is stated as of the end of its own minute, never later than the snapshot's update stamp
    (a minute still forming is no later than the vendor's own clock). The update stamp alone is not the price's time:
    a quote can move it after the last trade. The day bar has no time of its own, so a price taken from it carries
    the snapshot's update stamp, the only time the vendor gives for it, which is an upper bound on when it traded.
    """
    minute: Mapping[str, Any] = row.get("min") or {}
    day: Mapping[str, Any] = row.get("day") or {}
    minute_price = _positive_price(minute.get("close"))
    price = minute_price or _positive_price(day.get("close"))
    if price is None:
        return Miss("The vendor snapshot carries no trade price yet today.")
    updated = row.get("updated")
    if not isinstance(updated, int) or isinstance(updated, bool) or not _MIN_PLAUSIBLE_NS <= updated <= _MAX_PLAUSIBLE_NS:
        return Miss("The vendor snapshot carries no readable observation time.")
    updated_ms = updated // _NANOSECONDS_PER_MILLISECOND
    if updated_ms > now_ms + SNAPSHOT_FUTURE_TOLERANCE_MS:
        return Miss("The vendor snapshot is stamped after now, so its time cannot be trusted.")
    if minute_price is None:
        return Observation(price=price, as_of_ms=updated_ms)
    started = minute.get("timestamp")
    if not _plausible_ms(started):
        return Miss("The vendor snapshot's minute bar carries no readable time, so when its price traded is not known.")
    return Observation(price=price, as_of_ms=min(started + _MINUTE_MS, updated_ms))


class LakeCloseSource:
    """Regular-hours closes from the lake's committed minute files for one adjustment tree.

    The close of a session is the close of its last regular-hours minute bar, which the reader's ``regular``
    session filter already bounds by the calendar's own close, early closes included. A file the lake does not hold
    or cannot admit is a ``Miss`` for that session alone.
    """

    def __init__(
        self,
        mode: PriceAdjustmentMode,
        *,
        root_for: Callable[[PriceAdjustmentMode], Path] = resolve_lake_root,
        admission: Callable[[Path], bytes] | None = None,
    ) -> None:
        self._mode = mode
        self._root_for = root_for
        self._admission = admission

    def closes(self, symbols: Sequence[str], sessions: Sequence[date]) -> Mapping[str, Mapping[date, float | Miss]]:
        reader = (
            LeanMinuteDataReader([self._root_for(self._mode)], session="regular")
            if self._admission is None
            else LeanMinuteDataReader([self._root_for(self._mode)], session="regular", admission=self._admission)
        )
        return {symbol: {day: self._close(reader, symbol, day) for day in sessions} for symbol in symbols}

    @staticmethod
    def _close(reader: LeanMinuteDataReader, symbol: str, day: date) -> float | Miss:
        if not is_lake_addressable_symbol(symbol):
            return Miss(f"{symbol} is not a symbol the lake can hold.")
        try:
            bars = reader.read_day(symbol, day)
        # ValueError covers a mixed corporate-action version and a non-numeric time, ArithmeticError a non-numeric
        # price (``decimal.InvalidOperation``), IndexError an empty archive.
        except (
            LakeAdmissionError,
            CatalogUnavailableError,
            OSError,
            zipfile.BadZipFile,
            ValueError,
            ArithmeticError,
            IndexError,
        ) as exc:
            logger.warning(
                "The lake could not give a session close",
                extra={"action": "instrument_facts_close_unreadable", "symbol": symbol, "session": day.isoformat(), "error": str(exc)},
            )
            return Miss(f"The lake could not read {symbol} for {day.isoformat()} ({type(exc).__name__}).")
        if not bars:
            return Miss(f"The lake holds no regular-hours bars for {symbol} on {day.isoformat()}.")
        return float(max(bars, key=lambda bar: bar.start_ms).close)


class ServiceArchiveSource:
    """The archive's health, from the service the data plane installed at startup, if it installed one."""

    def __init__(self, service: OptionArchiveService | None) -> None:
        self._service = service

    def health(self) -> OptionArchiveHealth | Miss:
        if self._service is None:
            return Miss("The option archive is not running in this process.")
        try:
            return self._service.health()
        except (OSError, ValueError) as exc:
            logger.warning(
                "The option archive's receipts could not be read",
                extra={"action": "instrument_facts_archive_unreadable", "error": str(exc)},
            )
            return Miss("The option archive's receipts cannot be read; inspect the data lake.")
