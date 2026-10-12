"""The price series a one-day timeline draws for a saved session, each checked against what the run read (#3030).

Formula: none. A point is a bar's close time and close price, as the lake holds them; nothing is calculated, filled in or
  carried forward.
Reference: ``app/options_backtest/lake_data.py`` (the one place a stored bar's start becomes its close) and
  ``session_result.py`` (the pins a run keeps).
Canonical implementation: this file.
Validated against: tests/options_backtest/test_session_series.py.

A run stored the sha256 of every file it read for a session. Before a series is served, the file as it is now is read and
its hash compared with the pin. A series whose file changed, is gone or cannot be read is not served, and the answer says
which and why; it is never drawn from changed data, and a saved run's trades are never recomputed from it. Each series
is judged on its own, so one changed contract does not hide the rest.

Which series: the underlying, and each contract the session traded or took a starting price in. Which points: every bar
whose close is after the session's open and at or before its close, which is every bar the replay could have read. An
option prints until 16:15; those bars the replay never read are not drawn.

Reasons a series is not served:

* ``no_pin``: the run kept no hash of it (a session skipped on its window read nothing), so there is nothing to check it
  against and it is not served unchecked;
* ``gone``: the lake no longer holds the file;
* ``changed``: the file is not the one the run read (another hash, or bytes the lake's catalog does not admit);
* ``unreadable``: the file is there and is damaged;
* ``lake_unavailable``: the lake's catalog could not be asked. This says nothing about the file or the run.

Blocking: it reads files and asks the catalog, so call it from a worker thread.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from app.data_lake.admission import LakeAdmissionError
from app.data_lake.catalog_client import CatalogUnavailableError
from app.data_lake.option_bars import OptionBarsFileError
from app.options.contract import OptionContract
from app.options_backtest.data import RunData
from app.options_backtest.lake_data import UnderlyingDayUnreadable
from app.options_backtest.results import EventKind, SessionOutcome
from app.options_backtest.session_result import SessionResult
from app.utils.session_anchors import et_date_at_ms


class SeriesReason(StrEnum):
    NO_PIN = "no_pin"
    GONE = "gone"
    CHANGED = "changed"
    UNREADABLE = "unreadable"
    LAKE_UNAVAILABLE = "lake_unavailable"


# Words for each reason. None names a date, a path or a file: they are shown to the owner as they are.
SERIES_WORDS: dict[SeriesReason, str] = {
    SeriesReason.NO_PIN: "The run kept no record of reading these bars, so there is nothing to check them against.",
    SeriesReason.GONE: "The stored bars are no longer in the lake.",
    SeriesReason.CHANGED: "The stored bars have changed since this run.",
    SeriesReason.UNREADABLE: "The stored bars cannot be read now.",
    SeriesReason.LAKE_UNAVAILABLE: "The lake could not be reached, so the bars cannot be checked against this run now.",
}


@dataclass(frozen=True)
class SeriesPoint:
    at_ms: int
    close: Decimal


@dataclass(frozen=True)
class Series:
    """One instrument's closes across the session, or the reason they are not shown."""

    # The underlying's ticker, or a contract's OCC symbol.
    symbol: str
    # ``None`` for the underlying.
    contract: OptionContract | None
    # Empty unless the series is shown.
    points: tuple[SeriesPoint, ...]
    reason: SeriesReason | None

    @property
    def shown(self) -> bool:
        return self.reason is None


@dataclass(frozen=True)
class SessionSeries:
    underlying: Series
    contracts: tuple[Series, ...]


def contracts_to_chart(outcome: SessionOutcome) -> tuple[OptionContract, ...]:
    """The contracts the session traded or took a starting price in, by OCC symbol."""
    found = {trade.contract for trade in outcome.trades}
    found.update(
        event.contract
        for event in outcome.events
        if event.kind is EventKind.STARTING_PRICE_TAKEN and event.contract is not None
    )
    return tuple(sorted(found, key=lambda contract: contract.occ_symbol))


def read_session_series(session: SessionResult, *, underlying: str, data: RunData) -> SessionSeries:
    """The session's series, each read from ``data`` and shown only if its hash is the one the run pinned."""
    trading_date = et_date_at_ms(session.session_open_ms)
    pins = session.pins
    pinned = {day.contract: day.sha256 for day in pins.option_days}
    return SessionSeries(
        underlying=_series(
            session,
            symbol=underlying,
            contract=None,
            pin=pins.underlying_sha256,
            trading_date=trading_date,
            data=data,
        ),
        contracts=tuple(
            _series(
                session,
                symbol=contract.occ_symbol,
                contract=contract,
                pin=pinned.get(contract),
                trading_date=trading_date,
                data=data,
            )
            for contract in contracts_to_chart(session.outcome)
        ),
    )


def _series(
    session: SessionResult,
    *,
    symbol: str,
    contract: OptionContract | None,
    pin: str | None,
    trading_date: date,
    data: RunData,
) -> Series:
    def not_shown(reason: SeriesReason) -> Series:
        return Series(symbol, contract, (), reason)

    if pin is None:
        return not_shown(SeriesReason.NO_PIN)
    try:
        captured = data.underlying_day(trading_date) if contract is None else data.option_day(contract, trading_date)
    except CatalogUnavailableError:
        return not_shown(SeriesReason.LAKE_UNAVAILABLE)
    except LakeAdmissionError:
        return not_shown(SeriesReason.CHANGED)
    except (UnderlyingDayUnreadable, OptionBarsFileError):
        return not_shown(SeriesReason.UNREADABLE)
    if captured is None:
        return not_shown(SeriesReason.GONE)
    if captured.sha256 != pin:
        return not_shown(SeriesReason.CHANGED)
    points = tuple(
        SeriesPoint(bar.close_ms, bar.close)
        for bar in captured.value
        if session.session_open_ms < bar.close_ms <= session.session_close_ms
    )
    return Series(symbol, contract, points, None)
