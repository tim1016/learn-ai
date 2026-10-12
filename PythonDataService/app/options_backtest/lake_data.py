"""The data lake read for the Options Backtest, as the run core's ``RunData`` (#3030).

Formula: with ``s`` a stored bar's start in ms UTC, the bar closing at ``s + 60 000`` holds the trades in
    ``[s, s + 60 000)``. That one addition is made here, for the underlying's bars and for every option's,
    and nowhere else in the feature.
Reference: the vendor stamps a bar with the start of its minute and the lake stores it so (``OptionBar.start_ms``,
  #3030); ``app/options_backtest/market.py`` names a bar by its close.
Canonical implementation: this file.
Validated against: tests/options_backtest/test_lake_data.py.

Three reads, one per kind of stored file:

* ``underlying_day``: the underlying's raw minute bars of the regular session, read the way every stock backtest
  reads them (``LeanMinuteDataReader``) from the lake's raw tree and never from the split-adjusted one. That reader
  cannot tell a day it does not hold from a day with no bar, so the lake's own coverage rule decides
  (``check_availability``): a day with no file, or a file with no bar in the regular session, is not held and is
  ``None``. There is no "captured and empty" for the underlying.
* ``listing``: the stored listing as the picker's ``Listing``. A stored row can be a ``ListedContract`` only if its
  ticker names a contract and it has a share count; a row that cannot is left out, and ``StoredListing.rows_left_out``
  says how many. The picker therefore never sees such a row, so it cannot say a leg's expiry "is listed only as
  non-standard" when the only rows that say so are among those left out.
* ``option_day``: one contract's stored bars. ``None`` is a day not captured; no bars is a day captured on which the
  contract did not trade.

Bars. For an option every stored bar of the date is handed over, any hour (an option prints until 16:15); for the
underlying, the bars of the regular session. In both, every bar must be one a replay can place: its close (its start
plus a minute) is on a whole minute and falls on the trading date's New York date. The lake stores a bar whose start
is anywhere inside the date, so it can hold one stamped off the minute or starting in the last minute of the date, and
``session_market.bars_by_close`` refuses a session that holds one, as a whole. So a day with such a bar is unreadable,
as a damaged file is: the read raises the kind's own error (``UnderlyingDayUnreadable``, ``OptionBarsFileError``) and
hands nothing over. A series run without that bar would have a minute missing, one a stop could have been hit in, and
nothing in its result would say so; nothing is left out or repaired.

Every answer carries the sha256 of the exact bytes that were decoded. A damaged file, a file the catalog does not
admit and an unreachable catalog are errors and never ``None``: ``OptionBarsFileError`` and ``OptionListingFileError``
and ``LakeAdmissionError`` and ``CatalogUnavailableError`` are the lake's own and pass through unchanged. The stock
reader names no error of its own for a damaged file, so the failures it lets out of a decode (its own tuple, the one
``check_availability`` reports an unreadable file by) become one ``UnderlyingDayUnreadable``, cause chained.

Blocking. The readers ask the catalog through the lake's admission and need a thread with no running event loop, so
call them from a worker thread, and from an ``async def`` only through ``asyncio.to_thread``. Each method says so at
once and not only on a day that has a file. Nothing is cached: a day is read from its file on every ask.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app.data_lake.admission import LakeAdmissionError, read_committed_bytes
from app.data_lake.option_bars import OptionBar, OptionBarsFileError, read_option_day_bars
from app.data_lake.option_lake import option_read_root
from app.data_lake.option_listing import read_option_listing
from app.data_lake.types import is_lake_addressable_symbol

# What the stock reader lets out for a file it cannot decode, as ``check_availability`` reports an unreadable file by. It
# is private to the engine, which this feature may not edit; importing the one tuple keeps a copy here from drifting, and
# a rename there fails this import at once.
from app.engine.data.availability import _DECODE_ERRORS as _STOCK_FILE_ERRORS
from app.engine.data.lean_format import LeanMinuteDataReader
from app.engine.data.trade_bar import TradeBar
from app.options.contract import OptionContract
from app.options_backtest.data import Captured
from app.options_backtest.market import MinuteBar
from app.options_backtest.picking import ListedContract, Listing
from app.options_backtest.setup import MS_PER_MINUTE
from app.utils.session_anchors import et_date_at_ms


class UnderlyingDayUnreadable(ValueError):
    """The underlying's stored minute file is there and cannot be decoded; the stock reader's error is the cause."""


@dataclass(frozen=True)
class StoredListing(Listing):
    """A ``Listing`` read from the lake, which also says how many of its stored rows are not in it."""

    # Stored rows with no parsed contract (an adjusted root, a ticker that disagrees with its row) or no share count.
    rows_left_out: int = 0


class _AdmittedBytes:
    """The lake's admission, which keeps the exact bytes it admitted so the pin is the hash of what was decoded."""

    def __init__(self) -> None:
        self.payload: bytes | None = None

    def __call__(self, path: Path) -> bytes:
        self.payload = read_committed_bytes(path)
        return self.payload


def _require_a_worker_thread() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError(
        "LakeRunData reads files and asks the catalog, and blocks: call it from a worker thread "
        "(asyncio.to_thread), never on the event loop"
    )


def _named_by_close(
    stored: Sequence[OptionBar | TradeBar],
    trading_date: date,
    *,
    subject: str,
    unreadable: type[ValueError],
) -> tuple[MinuteBar, ...]:
    """The stored bars as ``MinuteBar``s named by their close, or ``unreadable`` when any cannot be placed.

    A bar cannot be placed when its close is not on a whole minute or is not on ``trading_date`` (New York). The
    error names ``subject``, how many of the stored bars cannot be placed and why the first cannot; no bar is left out.
    """
    named: list[MinuteBar] = []
    unplaceable = 0
    first = ""
    for bar in stored:
        # The one place a stored start becomes a close.
        close_ms = bar.start_ms + MS_PER_MINUTE
        if close_ms % MS_PER_MINUTE:
            why = "is not on a whole minute"
        elif et_date_at_ms(close_ms) != trading_date:
            why = "closes on another New York date"
        else:
            named.append(MinuteBar(close_ms=close_ms, open=bar.open, high=bar.high, low=bar.low, close=bar.close))
            continue
        unplaceable += 1
        first = first or f"the first starts at {bar.start_ms} ms and {why}"
    if unplaceable:
        raise unreadable(f"{subject}: {unplaceable} of {len(stored)} stored bars cannot be placed in a session; {first}")
    return tuple(named)


class LakeRunData:
    """``RunData`` over the data lake, for one underlying.

    ``lake_root`` reads from another raw lake root (``<base>/lake/raw``, such as an alternate mount) instead of the
    configured one; the underlying's file and the options' files are all read from that one root.
    """

    def __init__(self, underlying: str, *, lake_root: Path | None = None) -> None:
        if not is_lake_addressable_symbol(underlying):
            raise ValueError(f"the underlying must be an upper-case ticker the lake can hold, got {underlying!r}")
        self._underlying = underlying
        self._lake_root = lake_root

    def underlying_day(self, trading_date: date) -> Captured[tuple[MinuteBar, ...]] | None:
        """The underlying's raw minute bars of the regular session on ``trading_date``, or ``None`` when not held.

        Not held is the lake's own coverage rule: no file, or a file with no bar inside the regular session. The
        bars come from the lake's raw tree alone, never the split-adjusted one. Raises ``UnderlyingDayUnreadable``
        for a file that cannot be decoded and for a day holding a bar no replay can place (the module docstring says
        which), ``LakeAdmissionError`` for bytes the catalog does not admit and ``CatalogUnavailableError`` when the
        catalog cannot be asked. A thread with a running event loop is a ``RuntimeError``.
        """
        _require_a_worker_thread()
        admitted = _AdmittedBytes()
        reader = LeanMinuteDataReader(option_read_root(self._lake_root), session="regular", admission=admitted)
        try:
            bars = reader.read_day(self._underlying, trading_date)
        except LakeAdmissionError:
            # In the stock tuple too, and the lake's own: it passes through as it is.
            raise
        except _STOCK_FILE_ERRORS as exc:
            raise UnderlyingDayUnreadable(
                f"the stored {self._underlying} minute file for {trading_date.isoformat()} cannot be decoded "
                f"({type(exc).__name__}: {exc})"
            ) from exc
        if admitted.payload is None or not bars:
            return None
        named = _named_by_close(
            bars,
            trading_date,
            subject=f"the stored {self._underlying} minute bars for {trading_date.isoformat()}",
            unreadable=UnderlyingDayUnreadable,
        )
        return Captured(value=named, sha256=hashlib.sha256(admitted.payload).hexdigest())

    def listing(self, trading_date: date) -> Captured[StoredListing] | None:
        """The stored listing of the underlying as of ``trading_date``, or ``None`` when none is stored.

        The value is a ``StoredListing``: complete (the lake stores a listing whole or not at all), with every stored
        row that can be a ``ListedContract`` and ``rows_left_out`` counting those that cannot. A listing none of whose
        rows can be one is a captured listing with no contracts. Raises ``OptionListingFileError``,
        ``LakeAdmissionError`` and ``CatalogUnavailableError`` as the lake does, and ``RuntimeError`` off a worker
        thread.
        """
        _require_a_worker_thread()
        stored = read_option_listing(self._underlying, trading_date, lake_root=self._lake_root)
        if stored is None:
            return None
        contracts = tuple(
            ListedContract(contract=row.contract, multiplier=row.shares_per_contract, standard=row.standard)
            for row in stored.rows
            if row.contract is not None and row.shares_per_contract is not None
        )
        return Captured(
            value=StoredListing(
                underlying=stored.underlying,
                as_of=stored.as_of,
                contracts=contracts,
                complete=True,
                rows_left_out=len(stored.rows) - len(contracts),
            ),
            sha256=stored.sha256,
        )

    def option_day(self, contract: OptionContract, trading_date: date) -> Captured[tuple[MinuteBar, ...]] | None:
        """One contract's stored bars on ``trading_date``; ``None`` when not captured, empty when it never traded.

        Every bar the lake holds for the date, whatever the hour (an option prints until 16:15). Raises
        ``OptionBarsFileError`` for a damaged file and for a day holding a bar no replay can place (the module docstring
        says which), ``LakeAdmissionError`` and ``CatalogUnavailableError`` as the lake does, and ``RuntimeError`` off a
        worker thread.
        """
        _require_a_worker_thread()
        stored = read_option_day_bars(contract, trading_date, lake_root=self._lake_root)
        if stored is None:
            return None
        named = _named_by_close(
            stored.bars,
            trading_date,
            subject=f"the stored bars of {contract.occ_symbol} for {trading_date.isoformat()}",
            unreadable=OptionBarsFileError,
        )
        return Captured(value=named, sha256=stored.sha256)
