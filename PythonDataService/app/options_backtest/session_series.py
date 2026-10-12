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

Reasons a series is not served, each with words of its own that say only what is true of it (``SERIES_WORDS``):

* ``session_skipped``: the session was skipped on its window, so the run read no bars for it and kept nothing to check
  them against;
* ``no_pin``: the run kept no hash of this series (a contract the session's pins do not name);
* ``gone``: the lake no longer holds the file;
* ``changed``: the file is not the one the run read (its hash is not the pin);
* ``not_confirmed``: the lake's catalog does not admit the file's bytes (no receipt for them, or one being rebuilt). Nobody
  saw the file change, and the bytes cannot be checked against what the run read, so it is not said to have changed;
* ``unreadable``: the file is there and cannot be read: it is damaged, holds a bar no session can place, or the
  operating system refuses it (permission, an I/O error, a file that vanishes as it is read). The page gets the fixed
  words and nothing of the fault, so each such series is logged once, with the kind of error and no path;
* ``lake_unavailable``: the lake's catalog could not be asked. This says nothing about the file or the run. Asking can
  take as long as the admission waits (35 seconds) and a session charts up to 168 contracts, so the catalog is asked no
  more once it could not be: the first series that finds it out of reach, and every series not yet read after it, are
  withheld for this reason, and the series already read keep what they got. Only this reason does that. A file that is
  gone, changed, not confirmed or unreadable is one series' own fault, and the rest are still read.

Blocking: it reads files and asks the catalog, so call it from a worker thread.
"""

from __future__ import annotations

import logging
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
from app.options_backtest.results import EventKind, SessionOutcome, SkipReason
from app.options_backtest.session_result import SessionResult
from app.utils.session_anchors import et_date_at_ms

logger = logging.getLogger(__name__)


class SeriesReason(StrEnum):
    SESSION_SKIPPED = "session_skipped"
    NO_PIN = "no_pin"
    GONE = "gone"
    CHANGED = "changed"
    NOT_CONFIRMED = "not_confirmed"
    UNREADABLE = "unreadable"
    LAKE_UNAVAILABLE = "lake_unavailable"


# Words for each reason, shown to the owner as they are. None names a date, a path or a file.
SERIES_WORDS: dict[SeriesReason, str] = {
    SeriesReason.SESSION_SKIPPED: "This session was skipped, so no bars were read for it.",
    SeriesReason.NO_PIN: "The run kept no record of reading these bars, so there is nothing to check them against.",
    SeriesReason.GONE: "The stored bars are no longer in the lake.",
    SeriesReason.CHANGED: "The stored bars have changed since this run.",
    SeriesReason.NOT_CONFIRMED: "The stored bars cannot be confirmed against what this run read.",
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


def read_session_series(session: SessionResult, *, run_id: int, underlying: str, data: RunData) -> SessionSeries:
    """The session's series, each read from ``data`` and shown only if its hash is the one the run pinned.

    ``run_id`` is the run the session belongs to; it only names the run in the log of a series that cannot be read. The
    underlying is read first and then each contract by its symbol; once the catalog could not be asked, the series not
    yet read are not read (see the module docstring).
    """
    trading_date = et_date_at_ms(session.session_open_ms)
    pins = session.pins
    pinned = {day.contract: day.sha256 for day in pins.option_days}
    # A session skipped on its window read nothing, so the run pinned nothing: that is not a pin gone missing.
    skipped = session.outcome.skip_reason is SkipReason.WINDOW_DOES_NOT_FIT
    # What is read, in the order it is read: (symbol, contract, pin, the reason for no pin).
    wanted: list[tuple[str, OptionContract | None, str | None, SeriesReason]] = [
        (underlying, None, pins.underlying_sha256, SeriesReason.SESSION_SKIPPED if skipped else SeriesReason.NO_PIN),
        *(
            (contract.occ_symbol, contract, pinned.get(contract), SeriesReason.NO_PIN)
            for contract in contracts_to_chart(session.outcome)
        ),
    ]
    read: list[Series] = []
    catalog_is_out_of_reach = False
    for symbol, contract, pin, no_pin in wanted:
        series = _series(
            session,
            run_id=run_id,
            symbol=symbol,
            contract=contract,
            pin=pin,
            no_pin=no_pin,
            trading_date=trading_date,
            data=data,
            ask_the_catalog=not catalog_is_out_of_reach,
        )
        catalog_is_out_of_reach = catalog_is_out_of_reach or series.reason is SeriesReason.LAKE_UNAVAILABLE
        read.append(series)
    return SessionSeries(underlying=read[0], contracts=tuple(read[1:]))


def _series(
    session: SessionResult,
    *,
    run_id: int,
    symbol: str,
    contract: OptionContract | None,
    pin: str | None,
    no_pin: SeriesReason,
    trading_date: date,
    data: RunData,
    ask_the_catalog: bool,
) -> Series:
    def not_shown(reason: SeriesReason) -> Series:
        return Series(symbol, contract, (), reason)

    if pin is None:
        return not_shown(no_pin)
    if not ask_the_catalog:
        # An earlier series found the catalog out of reach. This one is not read: asking would wait out the same outage.
        return not_shown(SeriesReason.LAKE_UNAVAILABLE)
    try:
        captured = data.underlying_day(trading_date) if contract is None else data.option_day(contract, trading_date)
    except CatalogUnavailableError:
        return not_shown(SeriesReason.LAKE_UNAVAILABLE)
    except LakeAdmissionError:
        return not_shown(SeriesReason.NOT_CONFIRMED)
    except (UnderlyingDayUnreadable, OptionBarsFileError, OSError) as exc:
        # OSError: the file or its folder refuses to be read, or vanishes between the lake's check and its read. The
        # underlying's reader names its own errors; a contract's read lets the operating system's out.
        # The page gets the reason's fixed words. The kind of error is logged, so a permission can be told from a damaged
        # file; the error's own text is not, since it names the file by its path on the server's disk.
        logger.warning(
            "a stored series cannot be read",
            extra={
                "action": "options_backtest_series_unreadable",
                "run_id": run_id,
                "session_open_ms": session.session_open_ms,
                "series": symbol,
                "error_type": type(exc).__name__,
            },
        )
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
