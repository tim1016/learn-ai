"""The rate and the dividends an option on one underlying is priced with at one moment, each with its source.

The American pricer (``app.options.american``) takes a rate and the cash
dividends still to go ex before expiry, and has no default for either: a
stock priced as paying nothing must be one somebody looked up. This is the
one place they are looked up, for a preview now and for a bot's own entries
later (#2913).

- **The rate** is the repo's existing one: FRED's Treasury bill rates,
  interpolated to the days left to the latest expiry
  (``app.services.fred_service``). When FRED gives none, that service falls
  back to a fixed default with no recorded origin, and the source says so.
- **The dividends** are the cash dividends *declared* with an ex-date from
  today to the latest expiry. Only a declared dividend can be known. None
  declared is not none paid, and the source says that too. When the lookup
  fails, or this process has nowhere to look, the dividends are **unknown**:
  ``None``, never an empty list.

A fleet Clerk has nowhere to look. It boots with no Polygon key by design
(ADR 0062), and the coordinator does not serve it dividends yet. Until it
does, a Clerk's answer is "unknown", with that reason.

Both lookups block, so a caller on an event loop runs this in a thread. What
it learns is kept for an hour. What it could not learn, and a rate that is
only the default, is asked again after half a minute.
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.config import fleet_settings, settings
from app.lean_sidecar.trading_calendar import session_open_ms_utc
from app.options.resolver import DividendOn, ModelInputs
from app.services.fred_service import RATE_SOURCE_FRED, get_risk_free_rate_and_source
from app.services.risk_free_rate import DEFAULT_RISK_FREE_RATE
from app.utils.session_anchors import et_date_at_ms

logger = logging.getLogger(__name__)

_NY = ZoneInfo("America/New_York")
_KNOWN_FOR_MS = 60 * 60_000
_ASKED_AGAIN_AFTER_MS = 30_000
# The roles that hold a Polygon key of their own. Named one by one: a role added later has none until it says so.
_ROLES_WITH_A_POLYGON_KEY = frozenset({"combined", "fleet_coordinator"})


class DividendsUnknown(Exception):
    """The declared dividends could not be looked up. The message is the reason, in the owner's words."""


class ModelInputsBeingRead(Exception):
    """Another caller is looking these up right now, and this one chose not to wait for it."""


@dataclass(frozen=True, slots=True)
class DeclaredDividend:
    ex_date: date
    # Dollars per share.
    cash: float


# (days to the latest expiry, the New York date) -> (the rate, ``fred_service``'s word for where it came from).
RateLookup = Callable[[int, date], tuple[float, str]]
# (underlying, first ex-date, last ex-date) -> the cash dividends declared in that span. Raises
# ``DividendsUnknown`` when it cannot say; an empty answer means it looked and none is declared.
DividendLookup = Callable[[str, date, date], Sequence[DeclaredDividend]]


@dataclass(frozen=True, slots=True)
class ModelInputsRead:
    """What was learned for one underlying at one moment."""

    rate: float
    rate_source: str
    # True when FRED gave no rate and this is the fixed default, which has no recorded origin.
    rate_is_default: bool
    # None: the dividends could not be looked up. Never an assumed "none".
    dividends: tuple[DividendOn, ...] | None
    # What was looked up, where and when; or why it could not be.
    dividends_source: str
    # When the dividends were looked up: this answer may be up to an hour old.
    dividends_read_ms: int

    @property
    def model(self) -> ModelInputs | None:
        """The pricing model's inputs, or None while the dividends are not known."""
        if self.dividends is None:
            return None
        return ModelInputs(
            rate=self.rate,
            rate_source=self.rate_source,
            dividends=self.dividends,
            dividends_source=self.dividends_source,
        )

    def unverified_model(self) -> ModelInputs:
        """Inputs for a caller that must hand the resolver a model while the dividends are not known, and
        shows nothing the model says. The source carries the warning into whatever evidence is kept."""
        if self.dividends is not None:
            raise ValueError("the dividends are known; use the model they give")
        return ModelInputs(
            rate=self.rate,
            rate_source=self.rate_source,
            dividends=(),
            dividends_source=f"NOT KNOWN, priced as none and not to be shown: {self.dividends_source}",
        )


def _fred_rate(days: int, today: date) -> tuple[float, str]:
    return get_risk_free_rate_and_source(dte_days=days, observation_date=today.isoformat())


_polygon: Any = None


def _from_polygon(underlying: str, first: date, last: date) -> Sequence[DeclaredDividend]:
    global _polygon
    if _polygon is None:
        from app.services.polygon_client import PolygonClientService

        _polygon = PolygonClientService()
    try:
        rows = _polygon.list_dividends(
            ticker=underlying, ex_dividend_date_gte=first.isoformat(), ex_dividend_date_lte=last.isoformat()
        )
    except Exception as exc:
        logger.warning(
            "Polygon did not answer a dividend lookup",
            extra={"action": "option_dividends_lookup_failed", "symbol": underlying, "error_type": type(exc).__name__},
        )
        raise DividendsUnknown(f"Polygon did not answer when asked for {underlying}'s declared dividends.") from exc
    return declared_dividends_from_polygon(underlying, rows, first, last)


def declared_dividends_from_polygon(
    underlying: str, rows: Sequence[Mapping[str, Any]], first: date, last: date
) -> tuple[DeclaredDividend, ...]:
    """Polygon's dividend rows as declared cash dividends with an ex-date from ``first`` to ``last``.

    Every kind of cash distribution counts: each takes its amount off the
    price on its ex-date. A row that cannot be read makes the whole answer
    unknown: leaving it out would be pricing as if it were not there.
    """
    declared = []
    for row in rows:
        try:
            ex_date = date.fromisoformat(str(row["ex_dividend_date"]))
            cash = float(row["cash_amount"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DividendsUnknown(
                f"Polygon listed a dividend for {underlying} with no ex-date or amount this system could read."
            ) from exc
        currency = row.get("currency")
        if not (math.isfinite(cash) and cash > 0) or (currency is not None and str(currency).upper() != "USD"):
            raise DividendsUnknown(
                f"Polygon listed a dividend for {underlying} on {ex_date} that is not an amount in dollars above zero."
            )
        if first <= ex_date <= last:
            declared.append(DeclaredDividend(ex_date, cash))
    return tuple(sorted(declared, key=lambda dividend: dividend.ex_date))


def _nowhere_to_look(underlying: str, _first: date, _last: date) -> Sequence[DeclaredDividend]:
    raise DividendsUnknown(
        f"This Clerk has nowhere to look up {underlying}'s declared dividends: "
        "it holds no Polygon key, and the coordinator does not serve it dividends yet."
    )


def declared_dividend_lookup() -> DividendLookup:
    """Where this process looks dividends up: Polygon when it holds a key of its own, and otherwise nowhere."""
    if fleet_settings.ROLE in _ROLES_WITH_A_POLYGON_KEY and settings.POLYGON_API_KEY:
        return _from_polygon
    return _nowhere_to_look


@dataclass(frozen=True, slots=True)
class _Kept[T]:
    value: T
    until_ms: int


_lookups = threading.Lock()
_rates: dict[tuple[int, date], _Kept[tuple[float, str, bool]]] = {}
_dividends: dict[tuple[str, date, date], _Kept[tuple[tuple[DeclaredDividend, ...] | None, str, int]]] = {}


def clear_cache() -> None:
    """Forget every lookup kept (test helper)."""
    with _lookups:
        _rates.clear()
        _dividends.clear()


def _still_kept[T](kept: _Kept[T] | None, at_ms: int) -> _Kept[T] | None:
    return None if kept is None or at_ms >= kept.until_ms else kept


def _when(at_ms: int) -> str:
    return f"{datetime.fromtimestamp(at_ms / 1000, tz=UTC).astimezone(_NY):%Y-%m-%d %H:%M:%S} New York time"


def _rate(days: int, today: date, at_ms: int, lookup: RateLookup) -> tuple[float, str, bool]:
    kept = _still_kept(_rates.get((days, today)), at_ms)
    if kept is None:
        rate, source = lookup(days, today)
        is_default = source != RATE_SOURCE_FRED
        if is_default:
            told = (
                f"The fixed default of {DEFAULT_RISK_FREE_RATE:.1%}, because FRED gave no rate. "
                "The default has no recorded origin."
            )
        else:
            told = f"FRED Treasury bill rates, interpolated to {days} days, as published by {today}."
        kept = _rates[(days, today)] = _Kept(
            (rate, told, is_default), at_ms + (_ASKED_AGAIN_AFTER_MS if is_default else _KNOWN_FOR_MS)
        )
    return kept.value


def _declared(
    underlying: str, first: date, last: date, at_ms: int, lookup: DividendLookup
) -> tuple[tuple[DeclaredDividend, ...] | None, str, int]:
    kept = _still_kept(_dividends.get((underlying, first, last)), at_ms)
    if kept is None:
        try:
            declared = tuple(lookup(underlying, first, last))
        except DividendsUnknown as unknown:
            kept = _Kept((None, str(unknown), at_ms), at_ms + _ASKED_AGAIN_AFTER_MS)
        else:
            span = f"an ex-date from {first} to {last}"
            told = (
                f"{len(declared)} cash dividend{'' if len(declared) == 1 else 's'} declared for {underlying} with "
                f"{span}, read {_when(at_ms)}."
                if declared
                else f"No cash dividend is declared for {underlying} with {span}, read {_when(at_ms)}. "
                "None declared is not none paid: one declared after that is not counted."
            )
            kept = _Kept((declared, told, at_ms), at_ms + _KNOWN_FOR_MS)
        _dividends[(underlying, first, last)] = kept
    return kept.value


def read_option_model_inputs(
    underlying: str,
    *,
    at_ms: int,
    latest_expiry: date,
    rate: RateLookup | None = None,
    dividends: DividendLookup | None = None,
    wait: bool = True,
) -> ModelInputsRead:
    """The rate and the declared dividends for pricing options on ``underlying`` at ``at_ms``.

    ``latest_expiry`` is the last expiry date anything priced can have. The
    rate is the one for the days left to it, and the dividends are the ones
    with an ex-date from today to it. A dividend whose ex-date's session has
    already opened is in the stock's price, and is left out.

    One lookup runs at a time. A caller that finds one running waits for it,
    or with ``wait`` false raises ``ModelInputsBeingRead`` at once: a page
    that asks every second must not stack a waiting thread on each ask.
    What is already kept is answered without waiting either way.
    """
    symbol = underlying.upper()
    today = et_date_at_ms(at_ms)
    days = max((latest_expiry - today).days, 1)
    nothing_to_look_up = latest_expiry < today
    kept_rate = _still_kept(_rates.get((days, today)), at_ms)
    kept_dividends = _still_kept(_dividends.get((symbol, today, latest_expiry)), at_ms)
    if kept_rate is None or (kept_dividends is None and not nothing_to_look_up):
        if not _lookups.acquire(blocking=wait):
            raise ModelInputsBeingRead(f"the rate and the dividends for {symbol} are being looked up")
        try:
            learned_rate = _rate(days, today, at_ms, rate or _fred_rate)
            learned_dividends = (
                None
                if nothing_to_look_up
                else _declared(symbol, today, latest_expiry, at_ms, dividends or declared_dividend_lookup())
            )
        finally:
            _lookups.release()
    else:
        learned_rate = kept_rate.value
        learned_dividends = None if kept_dividends is None else kept_dividends.value
    rate_now, rate_source, rate_is_default = learned_rate
    declared, told, read_ms = learned_dividends or (
        (),
        f"Nothing was looked up: {latest_expiry} is already past.",
        at_ms,
    )
    still_to_go_ex = None
    if declared is not None:
        still_to_go_ex = tuple(
            DividendOn(ex_ms=ex_ms, cash=dividend.cash)
            for dividend in declared
            if (ex_ms := session_open_ms_utc(dividend.ex_date)) > at_ms
        )
    return ModelInputsRead(
        rate=rate_now,
        rate_source=rate_source,
        rate_is_default=rate_is_default,
        dividends=still_to_go_ex,
        dividends_source=told,
        dividends_read_ms=read_ms,
    )
