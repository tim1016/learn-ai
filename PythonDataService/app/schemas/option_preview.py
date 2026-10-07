"""The option structure preview's request and answer, and the named-structure catalogue (#2913).

A preview resolves a structure the owner is building against the quotes the
Clerk holds right now, and says what it would pick and what it would cost.
It needs no bot and changes nothing.

Every figure a screen shows is here as a field, and every sentence about
money as text the server wrote, so no screen does arithmetic or composes
prose of its own. Money is whole cents. A quote's price and a strike are
exact decimals. A time is ``int64 ms UTC``, and an age is milliseconds at
``quotes_read_ms``.

``state`` is a closed set. Only ``resolved`` carries figures; every other
state carries its sentence and its reason code and no figure. A reason code
is the value of the typed refusal it came from (the resolver's, the
structure check's, the money's, a quote line's, a search that was not run)
or one of ``OPTION_PREVIEW_…``; it is a plain string here so that a refusal
added later does not move this contract.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from app.broker.alpaca.clerk.models import EpochMs
from app.broker.contract.models import US_EQUITY_SYMBOL_PATTERN
from app.options.entry import DEFAULT_COST_LIMIT_PERCENT
from app.options.named_structures import NamedStructure
from app.schemas.action_plan import ExpirySelector, StrikeSelector

# The most legs a request may carry. More than a structure may have, so that one leg too many is answered
# with the plan's own verdict and not with a malformed request.
_MAX_LEGS_ASKED = 8


def _as_an_owner_writes_it(percent: Decimal) -> Decimal:
    """``1e1`` is 10, and ``10.000`` is 10: a percent is carried back and printed as written out."""
    return Decimal(format(percent.normalize(), "f"))


# A percent the owner sets. No finer than a millionth of a percent: a limit is compared exactly, in that unit
# (``app.options.requirement.round_trip_cost_is_over_limit``), and a finer one has no meaning there.
_OwnersPercent = Annotated[Decimal, Field(decimal_places=6), AfterValidator(_as_an_owner_writes_it)]


class OptionLegRules(BaseModel):
    """One leg as the owner builds it: an Action Plan option leg without its instrument, which is the
    structure's one underlying."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    leg_id: Annotated[str, Field(min_length=1, max_length=64)]
    # ``long`` is bought, ``short`` is sold.
    position: Literal["long", "short"]
    qty_ratio: Annotated[int, Field(ge=1, le=1000)]
    right: Literal["call", "put"]
    strike: StrikeSelector
    expiry: ExpirySelector


class OptionStructurePreviewRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # Names this page's request for quotes. The same key asked again renews it; it lapses a minute after
    # the last ask. A page makes one up and keeps it while it is open.
    preview_key: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")]
    underlying: Annotated[str, Field(pattern=US_EQUITY_SYMBOL_PATTERN)]
    # What the bot listens to, when the page knows it. Only the warning about leaning against a buy signal
    # reads it.
    listens_to: Annotated[str, Field(pattern=US_EQUITY_SYMBOL_PATTERN)] | None = None
    # The named structure the legs are still an example of. None: Custom.
    structure: NamedStructure | None = None
    legs: Annotated[list[OptionLegRules], Field(max_length=_MAX_LEGS_ASKED)]
    structures: Annotated[int, Field(ge=1, le=100)] = 1
    # The widest bid-ask gap a leg may have, as a percent of its mid. Carried back; no leg is judged by it yet.
    gap_limit_percent: Annotated[_OwnersPercent, Field(gt=0, le=100)] | None = None
    # The most opening and closing at once may cost, as a percent of what the entry sets aside.
    cost_limit_percent: Annotated[_OwnersPercent, Field(ge=0, le=100)] = DEFAULT_COST_LIMIT_PERCENT


PreviewState = Literal[
    # The plan's own rules refuse the structure. Nothing is watched for it.
    "plan_refused",
    # This process has no IBKR quote source for options.
    "no_quote_source",
    "feed_not_up",
    # No quote line could be had for the preview's contracts: none is spare, IBKR refused one, or too many
    # previews are open and this one's window was given up.
    "no_quote_line",
    # IBKR's list of the underlying's expiries or contracts has not been read yet. Ask again.
    "listing_not_read",
    # Lines are open and their first quotes have not all arrived. Ask again.
    "quotes_arriving",
    "underlying_quote_unusable",
    # The rate and the dividends are not read yet (ask again), or the dividends could not be looked up and
    # a leg picks by delta.
    "pricing_inputs_unknown",
    # The search for the contracts has not finished, or could not be run. ``reason_code`` says which, and
    # ``ask_again_in_ms`` is set while it is only still running. Never a refusal of the structure.
    "search_not_done",
    # The rules did not come to a contract for every leg: the resolver's own refusal.
    "not_resolved",
    "resolved",
]


class OptionContractView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # The contract's code at the broker.
    occ_symbol: str
    underlying: str
    # The expiry date's 16:00 New York anchor: a date, to be shown in the date-only New York mode.
    expiry_ms: EpochMs
    right: Literal["call", "put"]
    strike: Decimal


class OptionPreviewQuote(BaseModel):
    """A two-sided quote as it was held when it was read: each side with its own age."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    bid: Decimal
    bid_age_ms: int
    ask: Decimal
    ask_age_ms: int


class OptionPreviewLeg(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    leg_id: str
    position: Literal["long", "short"]
    ratio: int
    contract: OptionContractView
    quote: OptionPreviewQuote
    # (ask - bid) / mid, in percent, to two places for the eye.
    gap_percent_of_mid: Decimal
    # This system's own delta and implied volatility, from the contract's mid quote. None when the model
    # gave none, or its inputs are not known; ``delta_status`` says which.
    delta: float | None
    implied_volatility: float | None
    delta_status: str
    # IBKR's own delta, when it sent one. Beside ours, never used.
    ibkr_delta: float | None


class OptionPreviewMoney(BaseModel):
    """What entering would mean in cash, for the structures asked. Whole cents."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    structures: int
    multiplier: int
    # What one structure is entered at, per share: each bought leg's ask less each sold leg's bid.
    # Positive is paid, negative received.
    net_price: Decimal
    premium_is: Literal["paid", "received", "nothing"]
    # Signed as the net price is, for the structures asked, and for one.
    net_entry_premium_cents: int
    net_entry_premium_per_structure_cents: int
    # Alpaca's requirement by its published rule. Premiums are left out, so it is not what is set aside.
    broker_requirement_cents: int
    # An estimate until Alpaca reports the fees.
    fee_estimate_cents: int
    # What the bot sets aside: requirement plus premium plus the fee estimate.
    entry_commitment_cents: int
    # What opening and closing at once would cost: every leg's bid-ask gap, by its ratio.
    round_trip_cost_cents: int
    # That, as a percent of what is set aside, to two places for the eye.
    round_trip_cost_percent: Decimal
    cost_limit_percent: Decimal
    # Compared exactly. An entry over the limit is skipped.
    over_cost_limit: bool
    cost_sentence: str
    fees_sentence: str


class PriceRangeView(BaseModel):
    """Underlying prices from one to another, both included."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    from_price: Decimal
    # None: every price from ``from_price`` up.
    to_price: Decimal | None


class OptionPreviewAtExpiry(BaseModel):
    """Estimates, for the structures asked: every leg held to one shared expiry, never exercised or assigned
    early, settled at its exercise value, before fees. None of them bounds a market exit before expiry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # False when the legs do not share an expiry: nothing below is estimated.
    estimated: bool
    loss_cents: int | None
    # The underlying prices the loss estimate is the result at.
    loss_holds: list[PriceRangeView]
    # None with ``gain_has_no_limit``, or when nothing is estimated.
    gain_cents: int | None
    gain_has_no_limit: bool
    gain_holds: list[PriceRangeView]
    # To the nearest cent.
    break_even_prices: list[Decimal]
    # The three lines above as the server says them. None when nothing is estimated.
    loss_sentence: str | None
    gain_sentence: str | None
    break_even_sentence: str | None
    assumptions_sentence: str
    warning_sentence: str


class OptionPreviewLean(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # Per share of one structure, from this system's own deltas.
    net_delta: float
    # The shares of the underlying the position moves like, for one structure and for the structures asked.
    # Below zero: like shares sold short.
    shares_per_structure: float
    shares: float
    direction: Literal["up", "down", "about_flat"]
    # A structure that moves like fewer shares than this, either way, is called about flat.
    about_flat_below_shares_per_structure: float
    sentence: str
    # True when the structure gains as the underlying falls: against a buy signal.
    gains_when_underlying_falls: bool
    warning_sentence: str | None


class OptionPreviewWatchedRange(BaseModel):
    """How far the search reached for one kind of contract on one expiry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    right: Literal["call", "put"]
    expiry_ms: EpochMs
    lowest_strike: Decimal
    highest_strike: Decimal
    contracts: int


class OptionPreviewWatched(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ranges: list[OptionPreviewWatchedRange]
    # Of the contracts this preview asked to have watched: how many, how many hold a quote line, and how
    # many were refused one.
    lines_wanted: int
    lines_held: int
    lines_refused: int
    # This Clerk's own count. It cannot see what other IBKR sessions use.
    line_budget: int
    lines_open: int


class OptionPreviewDividend(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # The open of the session the stock first trades without it.
    ex_ms: EpochMs
    cash_per_share: float


class OptionPreviewPricingInputs(BaseModel):
    """What the pricing model was given beside the quotes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rate: float
    rate_source: str
    # True when FRED gave no rate and this is the fixed default, which has no recorded origin.
    rate_is_default: bool
    # False: the dividends could not be looked up. No delta is shown then, and none is assumed.
    dividends_known: bool
    dividends: list[OptionPreviewDividend]
    dividends_source: str
    dividends_read_ms: EpochMs


class OptionPreviewVerdict(BaseModel):
    """The builder's verdict, in two halves."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Whether the structure can be deployed at all: one order at the broker, every sold leg covered, the
    # exit closing every leg.
    can_be_deployed: bool
    deploy_sentence: str
    deploy_reason_code: str | None
    # Whether what it resolves to right now is the structure built. None: not known right now.
    resolves_as_built: bool | None
    resolves_sentence: str
    resolves_reason_code: str | None
    # The legs the half that failed names.
    leg_ids: list[str]
    # True when either half fails. Not known is not red.
    red: bool


class OptionStructurePreview(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    state: PreviewState
    # Why, for every state but ``resolved``.
    reason_code: str | None
    notice: str | None
    # The quote test or the model answer a refusal turned on, when it turned on one.
    fault: str | None
    leg_ids: list[str]
    contract: OptionContractView | None
    # Set when the state is one that passes by itself: ask again after this long.
    ask_again_in_ms: int | None

    underlying: str
    structure: NamedStructure | None
    structure_label: str
    structures: int
    gap_limit_percent: Decimal | None
    cost_limit_percent: Decimal

    # When this process read its quotes for this answer, and the feed's generation then.
    quotes_read_ms: EpochMs
    feed_generation: int | None
    # The age of the oldest quote side the picks stand on, the underlying's included. None with no picks.
    oldest_quote_age_ms: int | None
    underlying_quote: OptionPreviewQuote | None
    # "The money" a preview counts strikes from: the underlying's live mid. A bot uses its decision bar's close.
    the_money: Decimal | None
    the_money_sentence: str | None

    verdict: OptionPreviewVerdict
    legs: list[OptionPreviewLeg]
    money: OptionPreviewMoney | None
    at_expiry: OptionPreviewAtExpiry | None
    lean: OptionPreviewLean | None
    # Why there is no lean, when the structure resolved and has none.
    lean_unknown_sentence: str | None
    watched: OptionPreviewWatched | None
    pricing_inputs: OptionPreviewPricingInputs | None
    preview_sentence: str


class NamedStructureStart(BaseModel):
    """One named structure as the builder offers it: what it is, and the legs it starts from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    structure: NamedStructure
    label: str
    description: str
    legs: list[OptionLegRules]


class NamedStructureGroup(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    structures: list[NamedStructureStart]


class OptionStructureCatalogue(BaseModel):
    """The builder's left column: the named structures in their groups. Custom starts from no legs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    groups: list[NamedStructureGroup]
    custom_label: str
    custom_description: str
    max_legs: int
