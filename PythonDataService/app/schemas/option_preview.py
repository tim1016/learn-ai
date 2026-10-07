"""The option structure preview's request and answer, and the named-structure catalogue (#2913).

A preview resolves a structure the owner is building against the quotes the
Clerk holds right now, and says what it would pick and what it would cost.
It needs no bot and changes nothing.

Every figure a screen shows is here as a field, and every sentence as text
the server wrote, so no screen does arithmetic or composes prose of its own.
Money is whole cents, and beside each amount of cents is the same amount
written out in dollars (``…_usd``) for a screen to show as it is. A quote's
price and a strike are exact decimals. A time is ``int64 ms UTC``, and an
age is milliseconds at ``quotes_read_ms``.

A leg is known by its ``leg_id`` in every typed field and by its place in
the request, counted from one, in every sentence: "Leg 2".

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
from app.options import order_terms
from app.options.deadlines import CloseTerms
from app.options.entry import DEFAULT_COST_LIMIT_PERCENT, DEFAULT_LEG_GAP_LIMIT_PERCENT
from app.options.named_structures import NamedStructure
from app.schemas.action_plan import ExpirySelector, StrikeSelector

# The most legs a request may carry. More than a structure may have, so that one leg too many is answered
# with the plan's own verdict and not with a malformed request.
_MAX_LEGS_ASKED = 8


def _as_an_owner_writes_it(percent: Decimal) -> Decimal:
    """``1e1`` is 10, ``10.000`` is 10 and ``-0`` is 0: a percent is carried back and printed as written out."""
    if not percent:
        # Nothing, whatever sign it was written with.
        return Decimal(0)
    return Decimal(format(percent.normalize(), "f"))


def _places(step: Decimal | None) -> int:
    """How many decimal places a step allows: six for a millionth."""
    assert step is not None
    return -int(step.normalize().as_tuple().exponent)


# The four order terms, each held to the one range ``app.options.order_terms`` states for it. A percent is
# bounded before it is written out, so that a number too large to write is refused and never formatted.
_GapLimitPercent = Annotated[
    Decimal,
    Field(decimal_places=_places(order_terms.GAP_LIMIT_PERCENT.step)),
    *order_terms.GAP_LIMIT_PERCENT.held_by(Decimal),
    AfterValidator(_as_an_owner_writes_it),
]
_CostLimitPercent = Annotated[
    Decimal,
    Field(decimal_places=_places(order_terms.COST_LIMIT_PERCENT.step)),
    *order_terms.COST_LIMIT_PERCENT.held_by(Decimal),
    AfterValidator(_as_an_owner_writes_it),
]
_EntryWaitSeconds = Annotated[int, *order_terms.ENTRY_WAIT_SECONDS.held_by(int)]
_ExpiryCloseLeadMinutes = Annotated[int, *order_terms.EXPIRY_CLOSE_LEAD_MINUTES.held_by(int)]


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
    # The widest bid-ask gap a leg may have, as a percent of its mid. An entry with a leg quoted wider is skipped.
    gap_limit_percent: _GapLimitPercent = DEFAULT_LEG_GAP_LIMIT_PERCENT
    # The most opening and closing at once may cost, as a percent of what the entry sets aside.
    cost_limit_percent: _CostLimitPercent = DEFAULT_COST_LIMIT_PERCENT
    # How long an entry would wait for its fill, and how long before the close an expiring contract is
    # closed. Between them they set how late in its last session a contract is still picked.
    entry_wait_seconds: _EntryWaitSeconds = order_terms.entry_wait_seconds(CloseTerms())
    expiry_close_lead_minutes: _ExpiryCloseLeadMinutes = order_terms.expiry_close_lead_minutes(CloseTerms())


PreviewState = Literal[
    # The plan's own rules refuse the structure. Nothing is watched for it.
    "plan_refused",
    # This process has no IBKR quote source for options.
    "no_quote_source",
    "feed_not_up",
    # No quote line could be had for the preview's contracts: none is spare, IBKR refused one, or too many
    # previews are open and this one's window was given up. ``ask_again_in_ms`` is set when it is IBKR's
    # allowance that stands in the way: one more line is asked for once a minute while the page asks.
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
    # Its place in the request, counted from one: the "Leg 2" every sentence names it by.
    number: int
    position: Literal["long", "short"]
    ratio: int
    contract: OptionContractView
    quote: OptionPreviewQuote
    # (ask - bid) / mid, in percent, to two places for the eye.
    gap_percent_of_mid: Decimal
    # Whether that gap is wider than the owner's limit, judged exactly: an entry would be skipped for it.
    # None: not judged, because the picks are not a structure an entry could be made of.
    over_gap_limit: bool | None
    gap_sentence: str
    # This system's own delta and implied volatility, from the contract's mid quote. None when the model
    # gave none, or its inputs are not known; ``delta_status`` says which, and ``delta_status_sentence``
    # says it in words.
    delta: float | None
    implied_volatility: float | None
    delta_status: str
    delta_status_sentence: str
    # IBKR's own delta, when it sent one. Beside ours, never used.
    ibkr_delta: float | None
    # What the next 24 hours take from one share of this contract, by this system's own model: below zero
    # for a contract that loses value with a day. The contract's own figure, whichever side the leg takes.
    # None when it is not estimated; ``decay`` says why.
    daily_decay: float | None


class OptionPreviewLegAsked(BaseModel):
    """One leg as it was asked for: its rules, in words."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    leg_id: str
    number: int
    # "Buy" or "Sell".
    side_label: str
    # The rest of the line: "1 call · nearest 0.50 delta, within 0.05 · nearest weekly".
    sentence: str


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
    # Each amount above written out in dollars, to be shown as it is. A premium is written without a sign:
    # ``premium_is_label`` says which way it goes.
    net_price_usd: str
    premium_usd: str
    premium_per_structure_usd: str
    premium_is_label: str
    premium_sentence: str
    broker_requirement_usd: str
    fee_estimate_usd: str
    entry_commitment_usd: str
    round_trip_cost_usd: str


class PriceRangeView(BaseModel):
    """Underlying prices from one to another, both included."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    from_price: Decimal
    # None: every price from ``from_price`` up.
    to_price: Decimal | None


class OptionPreviewSketchPoint(BaseModel):
    """One point of the profit-and-loss line at expiry: the underlying's price, and what the entry comes to there."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    price: Decimal
    # Above zero is gained, below zero lost, for the structures asked.
    result_cents: int


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
    # The loss and the gain written out in dollars. None where the figure is.
    loss_usd: str | None
    gain_usd: str | None
    # The line to draw: straight between each point and the next, lowest price first. It starts below the
    # lowest strike and ends above the highest, with every strike between. Empty when nothing is estimated.
    sketch: list[OptionPreviewSketchPoint]


class OptionPreviewDecay(BaseModel):
    """What the next 24 hours take from the entry, by this system's own model, with the underlying's price,
    each leg's volatility and the rate held. An estimate. Three such days pass over a weekend."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # False: nothing below is estimated, and ``reason_code`` says why.
    estimated: bool
    # ``OPTION_DECAY_…``: the pricing inputs are not known, a leg has no volatility, or the model has no
    # price for a leg now or a day later.
    reason_code: str | None
    # The legs with no figure, when it is not estimated.
    leg_ids: list[str]
    # For the structures asked, to the nearest cent. Below zero is lost, above zero gained.
    per_24h_cents: int | None
    # The size of it written out in dollars; ``direction`` says which way.
    per_24h_usd: str | None
    direction: Literal["loses", "gains", "neither"] | None
    sentence: str
    assumptions_sentence: str
    # Said when a sold call's figure counts a dividend going ex inside the day: a gain early assignment takes.
    warning_sentence: str | None
    # Said when the underlying goes ex within a few days: the figure is rougher then.
    near_ex_date_sentence: str | None


class OptionPreviewEntryRefusal(BaseModel):
    """One reason an entry on these picks and quotes would be refused."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason_code: str
    leg_ids: list[str]
    sentence: str


class OptionPreviewOrderTerms(BaseModel):
    """The order terms this preview was asked with, and what they come to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    gap_limit_percent: Decimal
    entry_wait_seconds: int
    cost_limit_percent: Decimal
    expiry_close_lead_minutes: int
    # Where the expiry-day close falls against the hour Alpaca says it may sell an expiring position in.
    expiry_close_sentence: str


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
    # "Calls expiring Oct 9 '26: strikes 583 to 592."
    sentence: str


class OptionPreviewWatched(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ranges: list[OptionPreviewWatchedRange]
    # How far the search reached, and how many of the contracts asked for hold a quote line.
    sentence: str
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
    # Set when the state is one that passes by itself: ask again after this long. A second at least and a
    # minute at most, so that a page never has to guard against a wait of nothing.
    ask_again_in_ms: Annotated[int, Field(ge=1_000, le=60_000)] | None

    underlying: str
    structure: NamedStructure | None
    # The structure's name as a list writes it ("Bull call spread", or "Custom"), and as it stands before
    # its underlying in a sentence ("a bull call spread on").
    structure_label: str
    structure_in_a_sentence: str
    structures: int
    gap_limit_percent: Decimal
    cost_limit_percent: Decimal
    # The four order terms asked with. None when a number in the request was out of its range.
    order_terms: OptionPreviewOrderTerms | None
    # Each leg as it was asked for, in the request's order. Empty when a number in the request was out of
    # its range.
    legs_asked: list[OptionPreviewLegAsked]

    # When this process read its quotes for this answer, and the feed's generation then. The generation,
    # ``underlying_quote``, ``the_money`` and ``watched`` are set together: all of them or none.
    quotes_read_ms: EpochMs
    feed_generation: int | None
    # The age of the oldest quote side the picks stand on, the underlying's included. None with no picks.
    oldest_quote_age_ms: int | None
    quotes_read_sentence: str | None
    # How long the figures may be shown as they are. Past ``figures_good_until_ms`` a page shows when the
    # quotes were read in place of the figures, with ``figures_stale_sentence``, or asks again.
    # ``figures_good_for_ms`` is the same span counted from ``quotes_read_ms``, for a page that will not
    # trust its own clock against this one. All three are set only with figures.
    figures_good_until_ms: EpochMs | None
    figures_good_for_ms: int | None
    figures_stale_sentence: str | None
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
    # What a day takes. None unless the picks are a structure.
    decay: OptionPreviewDecay | None
    # Every reason an entry on these picks and quotes would be refused, in the order an entry asks: picks
    # that are not the structure built, each leg quoted wider than the gap limit, money that cannot be set
    # aside, a round-trip cost over its limit. Empty: none of them. An entry is also held to the budget, to
    # the account and to the quotes of its own moment, which are not asked here.
    entry_refusals: list[OptionPreviewEntryRefusal]
    # Whether an entry on these quotes would be skipped, in a sentence. None without figures.
    entry_sentence: str | None
    watched: OptionPreviewWatched | None
    pricing_inputs: OptionPreviewPricingInputs | None
    preview_sentence: str


class NamedStructureStart(BaseModel):
    """One named structure as the builder offers it: what it is, and the legs it starts from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    structure: NamedStructure
    # As a list writes it: "Bull call spread".
    label: str
    # As it stands before its underlying in a sentence: "a bull call spread on".
    in_a_sentence: str
    description: str
    legs: list[OptionLegRules]


class NamedStructureGroup(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    structures: list[NamedStructureStart]


class OptionValueBounds(BaseModel):
    """What a number typed into a box may be: the range the server holds it to. A form can refuse a number
    outside it before asking. A number that reaches the preview all the same is refused in words."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # None: no bound on that side.
    min: Decimal | None
    # False: the number must be above ``min``, not at it.
    min_included: bool
    max: Decimal | None
    max_included: bool
    # The finest step it is taken in: 1 for a whole number. None: any number.
    step: Decimal | None
    # The range in words: "above 0 and below 1".
    sentence: str


class OptionRuleValue(BaseModel):
    """What the box beside a rule holds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # The field of the rule's selector the box edits.
    field: str
    unit: Literal["delta", "strikes", "dollars", "percent", "days", "date"]
    # The unit as a screen writes it beside the box.
    unit_label: str
    # A ``date`` is bounded as its 16:00 New York anchors.
    bounds: OptionValueBounds


class OptionRuleTolerance(BaseModel):
    """How far from its target a rule's pick may be: the builder's "Within"."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # The field of the rule's selector the "Within" box edits.
    field: str
    unit: Literal["delta", "dollars", "percent"]
    unit_label: str
    # What a leg starts with: the Action Plan's own default for the rule.
    default: float
    bounds: OptionValueBounds


class OptionRuleOtherLeg(BaseModel):
    """The other leg a rule is measured from: the builder's "From"."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # The field of the rule's selector that names the other leg, by that leg's ``leg_id``.
    field: str
    unit: Literal["leg"]
    # The unit as a screen writes it beside the box.
    unit_label: str


class OptionStrikeRule(BaseModel):
    """One way of choosing a leg's strike, as the builder offers it. ``value``, ``measured_from`` and
    ``tolerance`` between them name every field the Action Plan's rule has beside its ``selector``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # What the leg's ``strike.selector`` says when this rule is chosen.
    selector: Literal["delta", "atm_offset", "leg_distance", "percent"]
    label: str
    value: OptionRuleValue
    # None: the rule is measured from no other leg.
    measured_from: OptionRuleOtherLeg | None
    # None: the rule names its strike exactly, and has no "Within".
    tolerance: OptionRuleTolerance | None


class OptionExpiryRule(BaseModel):
    """One way of choosing a leg's expiry, as the builder offers it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # What the leg's ``expiry.selector`` says when this rule is chosen.
    selector: Literal["min_dte", "nearest_weekly", "same_day", "absolute"]
    label: str
    # None: the rule needs nothing beside it. A ``date`` is the date's 16:00 New York anchor.
    value: OptionRuleValue | None


class OptionPercentTerm(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    unit_label: str
    default: Decimal
    bounds: OptionValueBounds
    # What the term does, for the "(i)" beside it.
    help: str


class OptionWholeNumberTerm(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    unit_label: str
    default: int
    bounds: OptionValueBounds
    help: str


class OptionOrderTermDefaults(BaseModel):
    """The option order terms a Deploy starts from. Each is fixed for the bot's life once it is deployed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # The widest bid-ask gap a leg may have at entry, as a percent of its mid.
    gap_limit_percent: OptionPercentTerm
    # How long a resting entry waits for its fill before it is cancelled.
    entry_wait_seconds: OptionWholeNumberTerm
    # The most opening and closing at once may cost, as a percent of what the entry sets aside.
    cost_limit_percent: OptionPercentTerm
    # How long before the session close on a held contract's expiry date the position is closed.
    expiry_close_lead_minutes: OptionWholeNumberTerm


class OptionOrderTermNotes(BaseModel):
    """What How says under the option order terms, for every option bot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entries_sentence: str
    exits_sentence: str
    hours_sentence: str
    # What Alpaca's own page says it may do to an expiring position. Not verified on the account. A preview's
    # ``order_terms.expiry_close_sentence`` says where the owner's own close falls against it.
    alpaca_sentence: str


class OptionStructureCatalogue(BaseModel):
    """What the builder offers: the named structures in their groups, the rules a leg may be given, and the
    order terms a Deploy starts from. Custom starts from no legs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    groups: list[NamedStructureGroup]
    custom_label: str
    custom_in_a_sentence: str
    custom_description: str
    max_legs: int
    # How many contracts of a leg one structure may hold, and how many structures a preview may be asked for.
    leg_quantity_bounds: OptionValueBounds
    structures_bounds: OptionValueBounds
    # In the order the builder lists them.
    strike_rules: list[OptionStrikeRule]
    expiry_rules: list[OptionExpiryRule]
    order_terms: OptionOrderTermDefaults
    order_term_notes: OptionOrderTermNotes
