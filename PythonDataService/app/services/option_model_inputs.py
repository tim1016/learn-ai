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

A fleet Clerk holds no Polygon key and no FRED key, by design (ADR 0062). It
asks the coordinator, which holds both and answers with what this same
function read there (``app.services.option_model_inputs_coordinator``). When
the coordinator cannot be reached, or its answer cannot be read, the
dividends are unknown with that reason, and the rate is the fixed default,
which says so and says why.

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
from app.schemas.fleet_option_model_inputs import (
    DividendsNotKnown,
    ModelInputsAnswer,
    ModelInputsBeingLookedUp,
    WhyNotKnown,
)
from app.services.fred_service import RATE_SOURCE_FRED, get_risk_free_rate_and_source
from app.services.option_model_inputs_coordinator import CoordinatorDidNotAnswer, ask_the_coordinator
from app.services.risk_free_rate import DEFAULT_RISK_FREE_RATE
from app.utils.session_anchors import et_date_at_ms

logger = logging.getLogger(__name__)

_NY = ZoneInfo("America/New_York")
_KNOWN_FOR_MS = 60 * 60_000
_ASKED_AGAIN_AFTER_MS = 30_000
# The roles that hold a Polygon key and a FRED key of their own, and read the vendors themselves. Named one by
# one: a role added later holds neither until it says so, and asks the coordinator.
_ROLES_THAT_READ_THE_VENDORS = frozenset({"combined", "fleet_coordinator"})


class DividendsUnknown(Exception):
    """The declared dividends could not be looked up. The message is the reason, in the owner's words."""

    def __init__(self, reason: str, why: WhyNotKnown = WhyNotKnown.LOOKUP_FAILED) -> None:
        super().__init__(reason)
        self.why = why


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
    # The code beside each sentence, for a refusal to carry: why the rate is only the default, and why the
    # dividends are not known. None when the rate is FRED's and when the dividends are known.
    rate_default_why: WhyNotKnown | None = None
    dividends_unknown_why: WhyNotKnown | None = None

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
        raise DividendsUnknown(
            f"Polygon did not answer when asked for {underlying}'s declared dividends.", WhyNotKnown.POLYGON_DID_NOT_ANSWER
        ) from exc
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
                f"Polygon listed a dividend for {underlying} with no ex-date or amount this system could read.",
                WhyNotKnown.POLYGON_ANSWER_UNREADABLE,
            ) from exc
        currency = row.get("currency")
        if not (math.isfinite(cash) and cash > 0) or (currency is not None and str(currency).upper() != "USD"):
            raise DividendsUnknown(
                f"Polygon listed a dividend for {underlying} on {ex_date} that is not an amount in dollars above zero.",
                WhyNotKnown.POLYGON_ANSWER_UNREADABLE,
            )
        if first <= ex_date <= last:
            declared.append(DeclaredDividend(ex_date, cash))
    return tuple(sorted(declared, key=lambda dividend: dividend.ex_date))


def _nowhere_to_look(underlying: str, _first: date, _last: date) -> Sequence[DeclaredDividend]:
    raise DividendsUnknown(
        f"No Polygon key is set to look up {underlying}'s declared dividends with.",
        WhyNotKnown.NO_POLYGON_KEY,
    )


def declared_dividend_lookup() -> DividendLookup:
    """Where this process looks dividends up at a vendor: Polygon when it holds a key of its own, and otherwise
    nowhere. A Clerk holds none, and asks the coordinator instead."""
    if fleet_settings.ROLE in _ROLES_THAT_READ_THE_VENDORS and settings.POLYGON_API_KEY:
        return _from_polygon
    return _nowhere_to_look


@dataclass(frozen=True, slots=True)
class _RateLearned:
    rate: float
    told: str
    # None: the rate is FRED's. Otherwise it is the fixed default, and this is why.
    why_default: WhyNotKnown | None


@dataclass(frozen=True, slots=True)
class _DividendsLearned:
    # Each at the open of its ex-date's session. None: they could not be looked up.
    declared: tuple[DividendOn, ...] | None
    told: str
    read_ms: int
    why_unknown: WhyNotKnown | None


# (days to the latest expiry, the New York date) -> the rate, from one source.
_RateSource = Callable[[int, date], _RateLearned]
# (underlying, first ex-date, last ex-date, the moment asked) -> the declared dividends, from one source.
_DividendSource = Callable[[str, date, date, int], _DividendsLearned]


def _the_default(because: str, why: WhyNotKnown) -> _RateLearned:
    told = f"The fixed default of {DEFAULT_RISK_FREE_RATE:.1%}, because {because}. The default has no recorded origin."
    return _RateLearned(DEFAULT_RISK_FREE_RATE, told, why)


def _rate_from(lookup: RateLookup) -> _RateSource:
    def learn(days: int, today: date) -> _RateLearned:
        rate, source = lookup(days, today)
        if source != RATE_SOURCE_FRED:
            return _the_default("FRED gave no rate", WhyNotKnown.FRED_GAVE_NO_RATE)
        return _RateLearned(rate, f"FRED Treasury bill rates, interpolated to {days} days, as published by {today}.", None)

    return learn


def _dividends_from(lookup: DividendLookup) -> _DividendSource:
    def learn(underlying: str, first: date, last: date, at_ms: int) -> _DividendsLearned:
        try:
            declared = tuple(lookup(underlying, first, last))
        except DividendsUnknown as unknown:
            return _DividendsLearned(None, str(unknown), at_ms, unknown.why)
        span = f"an ex-date from {first} to {last}"
        told = (
            f"{len(declared)} cash dividend{'' if len(declared) == 1 else 's'} declared for {underlying} with "
            f"{span}, read {_when(at_ms)}."
            if declared
            else f"No cash dividend is declared for {underlying} with {span}, read {_when(at_ms)}. "
            "None declared is not none paid: one declared after that is not counted."
        )
        going_ex = tuple(
            DividendOn(ex_ms=session_open_ms_utc(dividend.ex_date), cash=dividend.cash) for dividend in declared
        )
        return _DividendsLearned(going_ex, told, at_ms, None)

    return learn


class _AskedOfTheCoordinator:
    """One read's ask of the coordinator. Its answer holds both facts, so it is asked for once."""

    def __init__(self, underlying: str, at_ms: int, latest_expiry: date, wait: bool) -> None:
        self._asking = (underlying, at_ms, latest_expiry, wait)
        self._heard: ModelInputsAnswer | CoordinatorDidNotAnswer | None = None

    def _answer(self) -> ModelInputsAnswer | CoordinatorDidNotAnswer:
        if self._heard is None:
            underlying, at_ms, latest_expiry, wait = self._asking
            try:
                heard = ask_the_coordinator(underlying, at_ms=at_ms, latest_expiry=latest_expiry, wait=wait)
            except CoordinatorDidNotAnswer as silent:
                heard = silent
            if isinstance(heard, ModelInputsBeingLookedUp):
                raise ModelInputsBeingRead(f"the coordinator is looking up the rate and the dividends for {underlying}")
            self._heard = heard
        return self._heard

    def rate(self, _days: int, _today: date) -> _RateLearned:
        heard = self._answer()
        if isinstance(heard, CoordinatorDidNotAnswer):
            return _the_default(str(heard), heard.why)
        return _RateLearned(float(heard.rate.value), heard.rate.sentence, heard.rate.why_default)

    def dividends(self, underlying: str, _first: date, _last: date, at_ms: int) -> _DividendsLearned:
        heard = self._answer()
        if isinstance(heard, CoordinatorDidNotAnswer):
            return _DividendsLearned(None, f"{underlying}'s declared dividends are not known: {heard}.", at_ms, heard.why)
        said = heard.dividends
        if isinstance(said, DividendsNotKnown):
            return _DividendsLearned(None, said.sentence, at_ms, said.why)
        going_ex = tuple(DividendOn(ex_ms=dividend.ex_ms, cash=float(dividend.cash)) for dividend in said.declared)
        return _DividendsLearned(going_ex, said.sentence, said.read_ms, None)


def _sources(
    underlying: str, at_ms: int, latest_expiry: date, wait: bool, rate: RateLookup | None, dividends: DividendLookup | None
) -> tuple[_RateSource, _DividendSource]:
    """Where this read learns each fact: from a lookup handed in; else from the vendors, in a process that holds
    their keys; else from the coordinator."""
    if fleet_settings.ROLE in _ROLES_THAT_READ_THE_VENDORS:
        return _rate_from(rate or _fred_rate), _dividends_from(dividends or declared_dividend_lookup())
    asked = _AskedOfTheCoordinator(underlying, at_ms, latest_expiry, wait)
    return (_rate_from(rate) if rate else asked.rate), (_dividends_from(dividends) if dividends else asked.dividends)


@dataclass(frozen=True, slots=True)
class _Kept[T]:
    value: T
    until_ms: int


_lookups = threading.Lock()
_rates: dict[tuple[int, date], _Kept[_RateLearned]] = {}
_dividends: dict[tuple[str, date, date], _Kept[_DividendsLearned]] = {}


def clear_cache() -> None:
    """Forget every lookup kept (test helper)."""
    with _lookups:
        _rates.clear()
        _dividends.clear()


def _still_kept[T](kept: _Kept[T] | None, at_ms: int) -> _Kept[T] | None:
    return None if kept is None or at_ms >= kept.until_ms else kept


def _when(at_ms: int) -> str:
    return f"{datetime.fromtimestamp(at_ms / 1000, tz=UTC).astimezone(_NY):%Y-%m-%d %H:%M:%S} New York time"


def _rate(days: int, today: date, at_ms: int, source: _RateSource) -> _RateLearned:
    kept = _still_kept(_rates.get((days, today)), at_ms)
    if kept is None:
        learned = source(days, today)
        kept = _rates[(days, today)] = _Kept(
            learned, at_ms + (_KNOWN_FOR_MS if learned.why_default is None else _ASKED_AGAIN_AFTER_MS)
        )
    return kept.value


def _declared(underlying: str, first: date, last: date, at_ms: int, source: _DividendSource) -> _DividendsLearned:
    kept = _still_kept(_dividends.get((underlying, first, last)), at_ms)
    if kept is None:
        learned = source(underlying, first, last, at_ms)
        # The hour runs from when the vendor was read. What the coordinator had already kept a while is kept
        # here only for the rest of it.
        until_ms = at_ms + _ASKED_AGAIN_AFTER_MS if learned.declared is None else learned.read_ms + _KNOWN_FOR_MS
        kept = _dividends[(underlying, first, last)] = _Kept(learned, until_ms)
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
    What is already kept is answered without waiting either way. On a Clerk
    the lookup is one ask of the coordinator, which is told whether this
    caller waits and holds to it the same way.
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
            rate_source, dividend_source = _sources(symbol, at_ms, latest_expiry, wait, rate, dividends)
            learned_rate = _rate(days, today, at_ms, rate_source)
            learned_dividends = (
                None if nothing_to_look_up else _declared(symbol, today, latest_expiry, at_ms, dividend_source)
            )
        finally:
            _lookups.release()
    else:
        learned_rate = kept_rate.value
        learned_dividends = None if kept_dividends is None else kept_dividends.value
    going_ex = learned_dividends or _DividendsLearned(
        (), f"Nothing was looked up: {latest_expiry} is already past.", at_ms, None
    )
    still_to_go_ex = None
    if going_ex.declared is not None:
        still_to_go_ex = tuple(dividend for dividend in going_ex.declared if dividend.ex_ms > at_ms)
    return ModelInputsRead(
        rate=learned_rate.rate,
        rate_source=learned_rate.told,
        rate_is_default=learned_rate.why_default is not None,
        dividends=still_to_go_ex,
        dividends_source=going_ex.told,
        dividends_read_ms=going_ex.read_ms,
        rate_default_why=learned_rate.why_default,
        dividends_unknown_why=going_ex.why_unknown,
    )
