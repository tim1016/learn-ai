"""Wire shapes for the Options Backtest page's past-chain routes (#3030).

Names are snake_case, and every one starts ``PastChain`` so none can meet a model another route already serves (two
served models of one name make FastAPI rename both). Every temporal value is ``int64 ms UTC``, bounded by
``MAX_TIMESTAMP_MS``. A price or a strike is a string of exact dollars, written without an exponent, and a strike is
also its whole thousandths. Each state is one code of a closed set: the page branches on the code and never parses a
sentence. The ``problem`` text of something unreadable says what could not be read, why in plain words and the kind
of error in brackets; it names no file. A download item's ``detail`` is the lake's own words for a failure, to be
shown as they are.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field

from app.options.contract import MAX_STRIKE_THOUSANDTHS, OptionContract
from app.options_backtest.chain_download import DownloadItem, DownloadReason
from app.options_backtest.classification import ExpiryTag
from app.options_backtest.past_chain import ChainContract, ChainDay, ChainExpiry, ChainSession, ChainSide
from app.utils.session_anchors import MAX_TIMESTAMP_MS

# The same shape the setup's underlying has: an OCC root of one to six letters.
UNDERLYING_PATTERN = r"^[A-Z]{1,6}$"
DOLLARS_PATTERN = r"^[0-9]+(\.[0-9]+)?$"

TimestampMs = Annotated[int, Field(ge=0, le=MAX_TIMESTAMP_MS)]
Dollars = Annotated[str, Field(pattern=DOLLARS_PATTERN)]
Thousandths = Annotated[int, Field(ge=1, le=MAX_STRIKE_THOUSANDTHS)]
Count = Annotated[int, Field(ge=0)]

ListingStateCode = Literal["stored", "not_stored", "unreadable"]
UnderlyingStateCode = Literal["priced", "no_bar_yet", "not_stored", "unreadable"]
PriceStateCode = Literal["traded", "did_not_trade", "not_stored", "unreadable"]
SeriesCode = Literal["daily", "weekly", "monthly", "quarterly", "unknown"]
RightCode = Literal["call", "put"]
ItemKindCode = Literal["listing", "underlying", "contract"]
OutcomeCode = Literal["fetched", "already_stored", "failed"]


def dollars(value: Decimal) -> str:
    """Exact dollars as the lake read them, never in exponent form."""
    return format(value, "f")


class PastChainExpiryTag(BaseModel):
    """The series an expiry date belongs to, and which rule decided it."""

    series: SeriesCode
    version: int = Field(ge=1)
    evidence: str
    # The expiry is the trading date itself. It says nothing about the series.
    expires_today: bool
    days_to_expiry: Count

    @classmethod
    def from_tag(cls, tag: ExpiryTag) -> Self:
        return cls(
            series=tag.series.value,
            version=tag.version,
            evidence=tag.evidence,
            expires_today=tag.expires_today,
            days_to_expiry=tag.days_to_expiry,
        )


class PastChainExpirySummary(BaseModel):
    """One expiry the listing holds: its tag, and how many calls and puts it lists and how many are standard."""

    expiry_ms: TimestampMs
    tag: PastChainExpiryTag
    calls: Count
    puts: Count
    standard_calls: Count
    standard_puts: Count


class PastChainDayResponse(BaseModel):
    """What the listing of one underlying held on one trading date."""

    underlying: str
    session_open_ms: TimestampMs
    session_close_ms: TimestampMs
    listing_state: ListingStateCode
    # The lake's error, when the listing is stored and cannot be read.
    listing_problem: str | None
    expiries: list[PastChainExpirySummary]
    # Stored rows that name no contract a backtest can use or carry no share count; in no count above.
    rows_left_out_unusable: Count
    # Listed contracts expiring before the trading date; a listing as of a date holds none.
    rows_left_out_expired: Count

    @classmethod
    def from_day(cls, day: ChainDay) -> Self:
        return cls(
            underlying=day.underlying,
            session_open_ms=day.session.open_ms,
            session_close_ms=day.session.close_ms,
            listing_state=day.listing.value,
            listing_problem=day.listing_problem,
            expiries=[
                PastChainExpirySummary(
                    expiry_ms=summary.expiry_ms,
                    tag=PastChainExpiryTag.from_tag(summary.tag),
                    calls=summary.calls,
                    puts=summary.puts,
                    standard_calls=summary.standard_calls,
                    standard_puts=summary.standard_puts,
                )
                for summary in day.expiries
            ],
            rows_left_out_unusable=day.rows_left_out_unusable,
            rows_left_out_expired=day.rows_left_out_expired,
        )


class PastChainUnderlyingPrice(BaseModel):
    """The underlying's price at the instant: the close of its last bar by then, with that bar's close time."""

    state: UnderlyingStateCode
    price: Dollars | None
    price_time_ms: TimestampMs | None
    problem: str | None


class PastChainContract(BaseModel):
    """One listed contract and the price it last traded at by the instant, or the plain reason it has none."""

    symbol: str
    strike: Dollars
    strike_thousandths: Thousandths
    # A standard contract of a hundred shares, which is what a backtest trades.
    standard: bool
    # Listed strikes above (positive) or below (negative) the money on the standard ladder; null off it.
    steps_from_the_money: int | None
    price_state: PriceStateCode
    price: Dollars | None
    price_time_ms: TimestampMs | None
    problem: str | None

    @classmethod
    def from_contract(cls, chain: ChainContract) -> Self:
        contract: OptionContract = chain.contract
        return cls(
            symbol=contract.occ_symbol,
            strike=dollars(contract.strike),
            strike_thousandths=contract.strike_thousandths,
            standard=chain.standard,
            steps_from_the_money=chain.steps_from_the_money,
            price_state=chain.state.value,
            price=None if chain.price is None else dollars(chain.price),
            price_time_ms=chain.price_time_ms,
            problem=chain.problem,
        )


class PastChainSide(BaseModel):
    """The calls, or the puts, of one expiry: how many are listed, the money, and the strikes around it."""

    right: RightCode
    listed: Count
    standard_listed: Count
    # Null when there is no underlying price at the instant, or no standard contract of this right to centre on.
    at_the_money_strike: Dollars | None
    at_the_money_strike_thousandths: Thousandths | None
    # Ascending by strike.
    contracts: list[PastChainContract]

    @classmethod
    def from_side(cls, side: ChainSide) -> Self:
        money = side.at_the_money_strike_thousandths
        return cls(
            right=side.right,
            listed=side.listed,
            standard_listed=side.standard_listed,
            at_the_money_strike=None if side.at_the_money_strike is None else dollars(side.at_the_money_strike),
            at_the_money_strike_thousandths=money,
            contracts=[PastChainContract.from_contract(contract) for contract in side.contracts],
        )


class PastChainExpiryResponse(BaseModel):
    """One expiry at one instant of a session: the underlying's price, the money, and strikes around it."""

    underlying: str
    session_open_ms: TimestampMs
    session_close_ms: TimestampMs
    expiry_ms: TimestampMs
    at_ms: TimestampMs
    strikes_each_side: Count
    listing_state: ListingStateCode
    listing_problem: str | None
    # Null when the listing could not be read, so nothing can be said of whether the expiry is in it.
    expiry_listed: bool | None
    tag: PastChainExpiryTag | None
    underlying_price: PastChainUnderlyingPrice
    calls: PastChainSide
    puts: PastChainSide

    @classmethod
    def from_expiry(cls, chain: ChainExpiry, expiry_ms: int) -> Self:
        quote = chain.underlying_quote
        return cls(
            underlying=chain.underlying,
            session_open_ms=chain.session.open_ms,
            session_close_ms=chain.session.close_ms,
            expiry_ms=expiry_ms,
            at_ms=chain.at_ms,
            strikes_each_side=chain.strikes_each_side,
            listing_state=chain.listing.value,
            listing_problem=chain.listing_problem,
            expiry_listed=chain.expiry_listed,
            tag=None if chain.tag is None else PastChainExpiryTag.from_tag(chain.tag),
            underlying_price=PastChainUnderlyingPrice(
                state=quote.state.value,
                price=None if quote.price is None else dollars(quote.price),
                price_time_ms=quote.price_time_ms,
                problem=quote.problem,
            ),
            calls=PastChainSide.from_side(chain.calls),
            puts=PastChainSide.from_side(chain.puts),
        )


class PastChainDownloadRequest(BaseModel):
    """What to capture into the lake for one trading date. Contracts are named by OCC symbol."""

    model_config = ConfigDict(extra="forbid")

    underlying: str = Field(pattern=UNDERLYING_PATTERN)
    # Any instant on the New York trading date; the page sends New York midnight.
    trading_date_ms: TimestampMs
    # The expiry the page is looking at. When given, every contract must expire on it. It captures nothing itself.
    expiry_ms: TimestampMs | None = None
    contracts: list[str] = Field(default_factory=list)


class PastChainDownloadItem(BaseModel):
    """One thing the download was for, and how it came out."""

    kind: ItemKindCode
    # The underlying's ticker, or the contract's OCC symbol.
    subject: str
    outcome: OutcomeCode
    reason: DownloadReason | None = Field(
        description=(
            "The lake's typed reason, when it failed. `lease_timeout` is not missing data: another request is "
            "fetching the same thing at this moment and it will be stored when that one finishes, so show it as "
            "'being fetched by another request; ask again' and read the chain again."
        )
    )
    detail: str | None

    @classmethod
    def from_item(cls, item: DownloadItem) -> Self:
        return cls(
            kind=item.kind.value,
            subject=item.subject,
            outcome=item.outcome.value,
            reason=item.reason,
            detail=item.detail,
        )


class PastChainDownloadResponse(BaseModel):
    """Every item of a download: the listing, the underlying, then each contract named, in that order."""

    underlying: str
    session_open_ms: TimestampMs
    session_close_ms: TimestampMs
    items: list[PastChainDownloadItem]

    @classmethod
    def from_items(cls, underlying: str, session: ChainSession, items: tuple[DownloadItem, ...]) -> Self:
        return cls(
            underlying=underlying,
            session_open_ms=session.open_ms,
            session_close_ms=session.close_ms,
            items=[PastChainDownloadItem.from_item(item) for item in items],
        )
