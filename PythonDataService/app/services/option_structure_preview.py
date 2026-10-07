"""An option structure preview: what the owner's rules would pick right now, and what that would cost.

The preview resolves a structure the owner is still building against the
quotes this Clerk holds, and answers with the contracts, the money, what the
structure leans, how far the search reached and how old the quotes were. It
needs no bot, asks the broker nothing and changes nothing (#2913).

**It asks for quotes as a page does.** Its window goes on the process's
option watch list in the lowest class, on a lease of a minute that every ask
renews. It gets only the quote lines that are spare and never one a bot
needs; with none to spare it says so and shows no figures.

**It has no decision bar.** A bot counts strikes from the close of the bar
it decided on. A preview counts from the underlying's live mid quote, and
its answer says so.

**It is the resolver's answer, not a second opinion.** The picks are
``resolve_contracts``'s, and every way it can fail to pick is that
function's own typed refusal or one of the states named here: no quote
source in this process, the feed not up, no quote line, the listing not read
yet, quotes still arriving, no usable quote for the underlying, pricing
inputs not known, the search not done. Only a resolution carries figures.

**Its money is the entry's own.** The figures and every reason an entry
would be refused come from the one function an entry is prepared by
(``app.options.entry.entry_figures``), held to the owner's own gap limit and
cost limit. The contracts are picked under the entry cutoff the owner's own
entry wait and expiry-day close make. The Clerk refuses an entry too close to
the close its position would owe that day, and a position with a sold leg
owes one every session. That is asked too, of the moment the quotes were
read, and is one more of the entry's refusals: flagged, never red.

**Its figures are good for a minute, counted from the read.** They are a
reading of one moment: the second read of the supervisor, which comes after
the rate and the dividends were looked up and so up to a few seconds after
the ask. Past ``FIGURES_GOOD_FOR_MS`` from that read a page shows when the
quotes were read in their place, or asks again. The minute says how long the
figures may be shown as they are. It does not say an entry could be made on
them: an entry needs quotes no older than five seconds.

**A leg is named by its place, and money is said in dollars.** Every
sentence here that came from the plan's rules, the resolver, the structure
check or the money names a leg by its id and counts in cents; each is turned
into the builder's words on its way out
(``app.services.option_preview_words.in_the_builders_words``).

The supervisor is read on the event loop, where its callbacks write, and
nothing here holds the loop for long. The lookups of the rate and the
dividends block, and run in threads kept for them. The search for the
contracts prices every watched strike, and runs in another process
(``app.services.option_search``): in this one it would stop the loop for as
long as it ran (ADR 0076).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from functools import partial
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from app.broker.alpaca.option_fees import OptionRateNotPinnedError, entry_fee_provision_cents
from app.broker.ibkr.market_subscription import DECISION_DATA_MAX_AGE_MS
from app.broker.ibkr.option_quotes import LineUse, WatchRequest
from app.options.contract import OptionContract
from app.options.decay import goes_ex_soon
from app.options.order_terms import close_terms_with
from app.options.preview import StructureFigures, structure_figures
from app.options.quotes import HeldQuote, quote_fault, receipt_times
from app.options.requirement import EntrySize, LegQuote
from app.options.resolver import (
    Chain,
    ContractsNotResolved,
    FeedState,
    ResolverLimits,
    ResolverRefusal,
    UnderlyingObservation,
)
from app.options.structure import ResolvedStructure
from app.options.structure_catalogue import (
    expiry_close_sentence,
    in_a_sentence,
    label,
    orders_in_a_line,
    with_its_article,
)
from app.options.structure_check import StructureRefusal
from app.options.watch import LineClass, NoLine, WindowContract, expiries_wanted, watched_window
from app.schemas.action_plan import ActionPlan, DeltaStrike, LegDistanceStrike, OptionEntryLeg
from app.schemas.option_preview import (
    OptionPreviewAtExpiry,
    OptionPreviewDecay,
    OptionPreviewEntryRefusal,
    OptionPreviewLean,
    OptionPreviewLeg,
    OptionPreviewLegAsked,
    OptionPreviewMoney,
    OptionPreviewOrderTerms,
    OptionPreviewVerdict,
    OptionPreviewWatched,
    OptionPreviewWatchedRange,
    OptionStructurePreview,
    OptionStructurePreviewRequest,
    PreviewState,
)
from app.services.option_model_inputs import ModelInputsBeingRead, ModelInputsRead, off_the_loop
from app.services.option_preview_out_of_range import OutOfRange, RequestSaidBack
from app.services.option_preview_views import (
    at_expiry_view,
    contract_view,
    decay_view,
    entry_refusal_views,
    entry_sentence,
    lean_view,
    leg_view,
    legs_asked_view,
    money_view,
    pricing_inputs_view,
    quote_view,
)
from app.services.option_preview_words import (
    age_in_words,
    expiry_in_words,
    in_the_builders_words,
    leg_names,
    places_in_words,
    plural,
    sentence,
)
from app.services.option_search import PreviewSearches, ResolutionWithDecay, SearchAsk, SearchNotRun
from app.services.option_watch import OptionWatchList
from app.utils.session_anchors import et_date_at_ms
from app.utils.timestamps import Clock, now_ms_utc

if TYPE_CHECKING:
    from app.broker.ibkr.market_liveness import IbkrMarketStatusSource

# How long a page's request for quotes stands after its last ask.
PREVIEW_LEASE_MS = 60_000
# How long a preview's figures may be shown as they are, counted from the read of the quotes they stand on.
# The same length as a page's hold on its quote lines, which is counted from the ask. The read comes after
# the ask, by as long as the rate and the dividends took to look up, so the figures' minute ends that much
# after the hold does.
FIGURES_GOOD_FOR_MS = PREVIEW_LEASE_MS
# How long after a window is first asked for its quotes are said to be arriving. After that, a quote that
# has not come is reported as what it is.
ARRIVAL_GRACE_MS = 10_000
# The most windows pages may hold at once. One more takes the one asked for longest ago.
MAX_PREVIEW_WINDOWS = 4
# How long one ask waits for the rate and the dividends to be looked up before it answers that they are
# not read yet. The lookup goes on, and a later ask finds what it learned.
MODEL_INPUTS_WAIT_S = 4.0
# How many previews may wait on that lookup at once. On a fleet Clerk a preview is worked on the capacity kept
# for commands, so pages' waits are bounded: one more is told at once to ask again (#2913).
MAX_PREVIEWS_WAITING_ON_INPUTS = 4
_ASK_AGAIN_MS = 1_000
_KEY_PREFIX = "preview:"
# The refusals that say the picked contracts are another shape than the one named, as against money that
# cannot be set aside for them.
_SHAPE_REFUSALS = frozenset(str(reason) for reason in StructureRefusal)

# (underlying, the moment, the latest expiry the plan comes to) -> the rate and the dividends, with sources.
ModelInputsReader = Callable[[str, int, date], ModelInputsRead]


class PreviewReason(StrEnum):
    """Why a preview has no figures, where no other typed refusal says it."""

    NO_LEGS = "OPTION_PREVIEW_NO_LEGS"
    PLAN_REFUSED = "OPTION_PREVIEW_PLAN_REFUSED"
    # A ticker with a share class is written another way in a contract symbol, and no pick on it stands yet.
    SHARE_CLASS_UNDERLYING = "OPTION_PREVIEW_SHARE_CLASS_UNDERLYING"
    NO_QUOTE_SOURCE = "OPTION_PREVIEW_NO_QUOTE_SOURCE"
    FEED_NOT_UP = "OPTION_PREVIEW_FEED_NOT_UP"
    LISTING_NOT_READ = "OPTION_PREVIEW_LISTING_NOT_READ"
    QUOTES_ARRIVING = "OPTION_PREVIEW_QUOTES_ARRIVING"
    # IBKR refused a line for a reason other than capacity, such as a missing market-data subscription.
    LINE_REFUSED = "OPTION_PREVIEW_LINE_REFUSED"
    DIVIDENDS_UNKNOWN = "OPTION_PREVIEW_DIVIDENDS_UNKNOWN"
    # The rate and the dividends are still being looked up.
    PRICING_INPUTS_NOT_READ = "OPTION_PREVIEW_PRICING_INPUTS_NOT_READ"
    # Too many pages hold windows, and this one's was given up to a newer one.
    TOO_MANY_OPEN = "OPTION_PREVIEW_TOO_MANY_OPEN"
    # A number in the request is outside the range the catalogue gives for it.
    VALUE_OUT_OF_RANGE = "OPTION_PREVIEW_VALUE_OUT_OF_RANGE"
    # Two legs were given one id, so nothing said of either could tell them apart.
    LEGS_SHARE_AN_ID = "OPTION_PREVIEW_LEGS_SHARE_AN_ID"


def plan_of(request: OptionStructurePreviewRequest) -> ActionPlan:
    """The plan a Deploy of what the owner has built would carry: its legs on its one underlying, and an
    exit that closes each of them. Raises ``ValidationError`` when the plan's own rules refuse it."""
    return ActionPlan.model_validate(
        {
            "structure": request.structure,
            "on_enter": [
                {
                    "leg_id": leg.leg_id,
                    "instrument": {"kind": "option", "underlying": request.underlying},
                    "position": leg.position,
                    "qty_ratio": leg.qty_ratio,
                    "right": leg.right,
                    "strike": leg.strike.model_dump(),
                    "expiry": leg.expiry.model_dump(),
                }
                for leg in request.legs
            ],
            "on_exit": [{"kind": "close_leg", "entry_leg_id": leg.leg_id} for leg in request.legs],
        }
    )


# ---- words ------------------------------------------------------------------


def _the_plans_own_words(refused: ValidationError) -> str:
    """What the plan's rules said of the legs, a sentence a rule, each said once.

    One of those rules is a pattern a leg's id must match, and its own words
    are the pattern, which is no sentence. It is said by the leg's place: a
    plan's legs and its exits are in the request's order.
    """
    said: list[str] = []
    for error in refused.errors(include_url=False):
        where = error["loc"]
        # The leg's own id, where it is entered and where it is closed. Not the id of a leg its strike is measured from.
        about_an_id = error["type"] == "string_pattern_mismatch" and "strike" not in where
        if about_an_id and where[-1:] in (("leg_id",), ("entry_leg_id",)) and isinstance(where[1], int):
            text = (
                f"Leg {where[1] + 1} has an id a plan cannot carry. The page makes each leg's id: this is its "
                "mistake, and nothing you set."
            )
        else:
            text = sentence(str(error["msg"]).removeprefix("Value error, "))
        if text not in said:
            said.append(text)
    return " ".join(said)


@dataclass(frozen=True, slots=True)
class PlanRefusal:
    """Why no plan can hold a structure as it was asked: the reason, the sentence in the builder's words, and
    the legs it is about."""

    reason: PreviewReason
    why: str
    leg_ids: tuple[str, ...] = ()


def plan_or_refusal(request: OptionStructurePreviewRequest) -> ActionPlan | PlanRefusal:
    """The plan a Deploy of what the owner has built would carry, or why no plan can hold it.

    The checks a structure passes before anything is resolved for it: it
    has a leg, its underlying has no share class, no two legs share an id,
    no leg measures its strike from a leg that is not there, and the plan's
    own rules take it. It reads no quote and watches nothing, so a preview
    and a Deploy ask the same function and cannot differ (ADR 0076 decision
    24). Every sentence names a leg by its place.
    """
    order = [leg.leg_id for leg in request.legs]
    if not request.legs:
        return PlanRefusal(PreviewReason.NO_LEGS, "Add a leg. A structure has one to four.")
    if not request.underlying.isalpha():
        return PlanRefusal(
            PreviewReason.SHARE_CLASS_UNDERLYING,
            f"Options on a ticker with a share class, such as {request.underlying}, are not handled yet.",
        )
    shared = [leg_id for leg_id in dict.fromkeys(order) if order.count(leg_id) > 1]
    if shared:
        # Each id that was given twice, with the places of the legs that were given it.
        told = [
            f"{places_in_words([number for number, given in enumerate(order, start=1) if given == leg_id])} "
            "were given one id."
            for leg_id in shared
        ]
        return PlanRefusal(
            PreviewReason.LEGS_SHARE_AN_ID, f"{' '.join(told)} Each leg has an id of its own.", tuple(shared)
        )
    adrift = [
        leg.leg_id
        for leg in request.legs
        if isinstance(leg.strike, LegDistanceStrike) and leg.strike.leg_id not in order
    ]
    if adrift:
        # The plan refuses this too, and names the missing leg by an id nobody was shown. Said here by place.
        one = len(adrift) == 1
        return PlanRefusal(
            PreviewReason.PLAN_REFUSED,
            f"{leg_names(adrift, order)} {'measures its strike' if one else 'measure their strikes'} from a leg "
            "that is not in this structure. Choose the leg to measure from again.",
            tuple(adrift),
        )
    try:
        return plan_of(request)
    except ValidationError as refused:
        return PlanRefusal(PreviewReason.PLAN_REFUSED, in_the_builders_words(_the_plans_own_words(refused), order))


def _no_line_words(reason: str, use: LineUse) -> tuple[str, str]:
    """The reason code and the sentence for a preview that could not have a quote line."""
    cannot_see = "The count is this system's own. It cannot see what other IBKR sessions use."
    if reason == NoLine.OVER_BUDGET:
        if use.budget == 0:
            return reason, f"This Clerk's budget of option quote lines is zero, so nothing can be previewed here. {cannot_see}"
        return reason, (
            f"This system counts no IBKR quote line free for a preview: its budget of {use.budget} is taken by "
            f"contracts its bots hold or watch. {cannot_see}"
        )
    if reason == NoLine.NO_CAPACITY:
        # True whether IBKR refused a line of this preview's, or the preview was asked nothing for because
        # bots hold every line IBKR is known to grant.
        return reason, (
            "No IBKR quote line is free for this preview. IBKR has refused this system a line since it last "
            "connected, because the account's allowance was in use, here or in another IBKR session, and the "
            "lines it is known to grant are taken. One more is asked for once a minute, and a bot's contracts "
            f"come before a preview's. {cannot_see}"
        )
    return PreviewReason.LINE_REFUSED, sentence(f"No quote line could be had for this preview: {reason}")


def _fee_provision(
    trade_date: date, structure: ResolvedStructure, markets: Mapping[str, LegQuote], size: EntrySize
) -> int | None:
    """What the entry sets aside for its own fees on ``trade_date``. None when a rate is not pinned for that date."""
    try:
        return entry_fee_provision_cents(structure, markets, size, trade_date=trade_date)
    except OptionRateNotPinnedError:
        return None


# ---- one reading of the supervisor -------------------------------------------


@dataclass(frozen=True, slots=True)
class _Reading:
    """What the supervisor held at one moment, once it is enough to resolve against."""

    now: int
    feed: FeedState
    use: LineUse
    chain: Chain
    underlying_quote: HeldQuote
    # The underlying's live mid: what this preview calls the money.
    money: Decimal
    today: date
    # The contracts this preview asked to have watched.
    window: tuple[WindowContract, ...]
    latest_expiry: date
    # True while the window was asked for so lately that its quotes may still be on their way.
    arriving: bool

    @property
    def wanted(self) -> frozenset[str]:
        return frozenset(seen.contract.occ_symbol for seen in self.window)

    @property
    def refused(self) -> dict[str, str]:
        """The wanted contracts that were refused a quote line, and why."""
        return {symbol: self.use.without[symbol] for symbol in sorted(self.wanted) if symbol in self.use.without}


@dataclass(frozen=True, slots=True)
class _NoFigures:
    """A state that shows no figure: why, in words and in code."""

    state: PreviewState
    reason_code: str
    notice: str
    passes_by_itself: bool = False
    fault: str | None = None
    leg_ids: tuple[str, ...] = ()
    contract: OptionContract | None = None
    reading: _Reading | None = None


def _no_line(reason: str, use: LineUse, **found: Any) -> _NoFigures:
    """A preview that could not have a quote line. Kept out by IBKR's allowance it is waiting: one more line is
    asked for once a minute while it asks, so it asks again. Kept out by the Clerk's own budget, or by a
    refusal of another kind, it waits for nothing that passes by itself."""
    code, notice = _no_line_words(reason, use)
    return _NoFigures("no_quote_line", code, notice, passes_by_itself=reason == NoLine.NO_CAPACITY, **found)


def _watched_view(reading: _Reading) -> OptionPreviewWatched:
    watched = [OptionContract.from_occ_symbol(symbol) for symbol in reading.chain.watched]
    kinds = sorted({(seen.contract.right, seen.contract.expiry_ms) for seen in reading.window})
    ranges = []
    for right, expiry_ms in kinds:
        of_its_kind = sorted(
            (contract for contract in watched if (contract.right, contract.expiry_ms) == (right, expiry_ms)),
            key=lambda contract: contract.strike_thousandths,
        )
        if of_its_kind:
            lowest, highest = of_its_kind[0].strike, of_its_kind[-1].strike
            ranges.append(
                OptionPreviewWatchedRange(
                    right=right,
                    expiry_ms=expiry_ms,
                    lowest_strike=lowest,
                    highest_strike=highest,
                    contracts=len(of_its_kind),
                    sentence=(
                        f"{'Calls' if right == 'call' else 'Puts'} expiring {expiry_in_words(of_its_kind[0].expiry_date)}: "
                        f"strikes {lowest:f} to {highest:f}."
                    ),
                )
            )
    wanted, held, refused = len(reading.wanted), len(reading.wanted & reading.chain.watched.keys()), len(reading.refused)
    said = f"{held:,} of the {plural(wanted, 'contract')} these rules can pick from {'holds' if held == 1 else 'hold'} a quote line."
    if refused:
        said += f" {refused:,} could not have one."
    return OptionPreviewWatched(
        ranges=ranges,
        sentence=said,
        lines_wanted=wanted,
        lines_held=held,
        lines_refused=refused,
        line_budget=reading.use.budget,
        lines_open=reading.use.open,
    )


def _read(
    plan: ActionPlan,
    underlying: str,
    *,
    quotes: IbkrMarketStatusSource,
    strikes_each_side: int,
    since_ms: int,
    now: int,
) -> _Reading | _NoFigures:
    """Read the supervisor once. Everything a resolution is handed comes from this one moment.

    ``since_ms`` is when this window was first asked for: how long its
    quotes have had to arrive.
    """
    feed, use = quotes.option_feed(), quotes.option_line_use()
    if feed is None or use is None:
        return _NoFigures(
            "no_quote_source", PreviewReason.NO_QUOTE_SOURCE, "This process has no IBKR quote source for options."
        )
    if feed.unhealthy is not None:
        return _NoFigures(
            "feed_not_up",
            PreviewReason.FEED_NOT_UP,
            sentence(f"The quote feed is not up: {feed.unhealthy}"),
            passes_by_itself=True,
        )
    arriving = now - since_ms <= ARRIVAL_GRACE_MS
    # With no line spare for a page the supervisor asks IBKR for nothing on its account, so what is missing
    # below is not on its way: it is missing for that reason.
    no_line = None if use.pages_not_served is None else _no_line(use.pages_not_served, use)
    chain = quotes.option_chain(underlying)
    not_read = no_line or _NoFigures(
        "listing_not_read",
        PreviewReason.LISTING_NOT_READ,
        f"IBKR's list of {underlying} option contracts has not been read yet.",
        passes_by_itself=True,
    )
    if chain is None:
        return not_read
    held = quotes.underlying_quote(underlying)
    fault = (
        None
        if held is None
        else quote_fault(
            held, at_ms=now, generation=feed.generation, max_age_ms=DECISION_DATA_MAX_AGE_MS, needs_a_bid=True
        )
    )
    if held is None or fault is not None:
        if no_line is not None:
            return no_line
        if arriving:
            return _NoFigures(
                "quotes_arriving",
                PreviewReason.QUOTES_ARRIVING,
                f"Quotes are arriving. None for {underlying} can be used yet.",
                passes_by_itself=True,
            )
        return _NoFigures(
            "underlying_quote_unusable",
            ResolverRefusal.UNDERLYING_QUOTE_UNUSABLE,
            f"No live quote for {underlying} has arrived."
            if held is None
            else f"{underlying}'s own quote cannot be used: it fails as {fault}.",
            fault=None if fault is None else str(fault),
            passes_by_itself=True,
        )
    assert held.bid is not None and held.ask is not None
    money = (held.bid.price + held.ask.price) / 2
    today = et_date_at_ms(now)
    named = chain.expiry_dates
    listed = (
        named
        if named is not None
        else tuple(sorted({listing.contract.expiry_date for listing in chain.listings if listing.is_standard}))
    )
    come_to = expiries_wanted(plan, listed, today)
    if come_to - {listing.contract.expiry_date for listing in chain.listings}:
        return not_read
    return _Reading(
        now=now,
        feed=feed,
        use=use,
        chain=chain,
        underlying_quote=held,
        money=money,
        today=today,
        window=watched_window(plan, chain=chain, money=money, today=today, strikes_each_side=strikes_each_side),
        latest_expiry=max(come_to, default=today),
        arriving=arriving,
    )


def _lines_stand_in_the_way(reading: _Reading) -> _NoFigures | None:
    """When none of the preview's contracts holds a line: why there is nothing to resolve against."""
    if not reading.wanted or reading.wanted & reading.chain.watched.keys():
        return None
    if reading.refused:
        return _no_line(next(iter(reading.refused.values())), reading.use, reading=reading)
    if reading.arriving:
        return _NoFigures(
            "quotes_arriving",
            PreviewReason.QUOTES_ARRIVING,
            "Quotes are arriving. The quote lines for these contracts are being opened.",
            passes_by_itself=True,
            reading=reading,
        )
    return None


def _not_resolved(refused: ContractsNotResolved, reading: _Reading) -> _NoFigures:
    """A resolver's refusal, as the state a preview names it by."""
    contract = refused.contract
    fault = None if refused.fault is None else str(refused.fault)
    if refused.reason is ResolverRefusal.SEARCH_INCOMPLETE:
        symbol = None if contract is None else contract.occ_symbol
        # With no contract named, a rule by delta found nothing of its kind watched at all.
        without = reading.refused.get(symbol) if symbol is not None else next(iter(reading.refused.values()), None)
        if without is not None:
            return _no_line(without, reading.use, leg_ids=refused.leg_ids, contract=contract, reading=reading)
        if reading.arriving and (symbol is None or symbol in reading.wanted or symbol in reading.chain.watched):
            return _NoFigures(
                "quotes_arriving",
                PreviewReason.QUOTES_ARRIVING,
                "Quotes are arriving. Not every contract these rules can pick has one yet.",
                passes_by_itself=True,
                fault=fault,
                leg_ids=refused.leg_ids,
                contract=contract,
                reading=reading,
            )
    return _NoFigures(
        "not_resolved",
        str(refused.reason),
        sentence(str(refused)),
        fault=fault,
        leg_ids=refused.leg_ids,
        contract=contract,
        reading=reading,
    )


def _search_not_done(not_run: SearchNotRun, reading: _Reading) -> _NoFigures:
    """A search with no answer for this ask. Never a refusal of the structure, and never a figure."""
    reason = not_run.reason
    if reason is reason.LANE_BUSY:
        notice = "Other previews' contracts are being worked out first. This one's turn has not come."
    elif reason.still_running:
        notice = "The contracts these rules pick are still being worked out."
    else:
        notice = sentence(f"The search for these contracts could not be run: {not_run.detail}. Nothing is shown on a guess")
    return _NoFigures("search_not_done", str(reason), notice, passes_by_itself=reason.still_running, reading=reading)


class _InputsWaits:
    """How many previews are waiting on the rate and the dividends right now. Read and moved on the event loop
    alone."""

    def __init__(self) -> None:
        self.now = 0


_inputs_waits = _InputsWaits()


def _inputs_not_read(underlying: str, reading: _Reading) -> _NoFigures:
    return _NoFigures(
        "pricing_inputs_unknown",
        PreviewReason.PRICING_INPUTS_NOT_READ,
        f"The rate and the dividends for {underlying} are still being looked up.",
        passes_by_itself=True,
        reading=reading,
    )


# ---- the answer -----------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class _Found:
    """What one answer found. A state carries its own parts, and the rest stay empty."""

    state: PreviewState
    verdict: OptionPreviewVerdict
    reason_code: str | None = None
    notice: str | None = None
    fault: str | None = None
    leg_ids: tuple[str, ...] = ()
    contract: OptionContract | None = None
    # Set for a state that passes by itself: ask again after this long.
    ask_again_in_ms: int | None = None
    # The reading the answer stands on, when the supervisor was read far enough for one.
    reading: _Reading | None = None
    inputs: ModelInputsRead | None = None
    oldest_quote_age_ms: int | None = None
    legs: tuple[OptionPreviewLeg, ...] = ()
    money: OptionPreviewMoney | None = None
    at_expiry: OptionPreviewAtExpiry | None = None
    lean: OptionPreviewLean | None = None
    lean_unknown_sentence: str | None = None
    decay: OptionPreviewDecay | None = None
    entry_refusals: tuple[OptionPreviewEntryRefusal, ...] = ()
    entry_sentence: str | None = None


def _answer(
    said_back: RequestSaidBack,
    found: _Found,
    *,
    now: int,
    order_terms: OptionPreviewOrderTerms | None,
    legs_asked: list[OptionPreviewLegAsked],
) -> OptionStructurePreview:
    """One answer: what was found, beside the parts of the request every answer says back.

    ``now`` is when the ask was taken up: what an answer with no reading of
    the supervisor is stamped with. ``order_terms`` and ``legs_asked`` are a
    request's own terms and legs said back. A request refused before it
    stood has none to say back.
    """
    reading = found.reading
    read_ms = now if reading is None else reading.now
    with_figures = found.state == "resolved"
    return OptionStructurePreview(
        state=found.state,
        reason_code=found.reason_code,
        notice=found.notice,
        fault=found.fault,
        leg_ids=list(found.leg_ids),
        contract=None if found.contract is None else contract_view(found.contract),
        ask_again_in_ms=found.ask_again_in_ms,
        underlying=said_back.underlying,
        structure=said_back.structure,
        structure_label=label(said_back.structure),
        structure_in_a_sentence=in_a_sentence(said_back.structure),
        structures=said_back.structures,
        gap_limit_percent=said_back.gap_limit_percent,
        cost_limit_percent=said_back.cost_limit_percent,
        order_terms=order_terms,
        legs_asked=legs_asked,
        quotes_read_ms=read_ms,
        feed_generation=None if reading is None else reading.feed.generation,
        oldest_quote_age_ms=found.oldest_quote_age_ms,
        quotes_read_sentence=(
            None
            if found.oldest_quote_age_ms is None
            else f"IBKR live quotes. The oldest of them was {age_in_words(found.oldest_quote_age_ms)} old when they were read."
        ),
        # Counted from the read of the quotes, not from when the ask was taken up.
        figures_good_until_ms=read_ms + FIGURES_GOOD_FOR_MS if with_figures else None,
        figures_good_for_ms=FIGURES_GOOD_FOR_MS if with_figures else None,
        figures_stale_sentence=(
            f"These figures are from quotes read more than {age_in_words(FIGURES_GOOD_FOR_MS)} ago. "
            "Refresh to read them again."
            if with_figures
            else None
        ),
        underlying_quote=None if reading is None else quote_view(reading.underlying_quote, reading.now),
        the_money=None if reading is None else reading.money,
        the_money_sentence=(
            None
            if reading is None
            else f"Strikes are counted from {said_back.underlying}'s live mid quote, {reading.money}. "
            "A bot counts from the close of the bar it decided on."
        ),
        verdict=found.verdict,
        legs=list(found.legs),
        money=found.money,
        at_expiry=found.at_expiry,
        lean=found.lean,
        lean_unknown_sentence=found.lean_unknown_sentence,
        decay=found.decay,
        entry_refusals=list(found.entry_refusals),
        entry_sentence=found.entry_sentence,
        watched=None if reading is None else _watched_view(reading),
        pricing_inputs=None if found.inputs is None else pricing_inputs_view(found.inputs),
        preview_sentence="A preview. The bot picks again at every entry.",
    )


def _cannot_be_deployed(
    reason: PreviewReason,
    why: str,
    *,
    leg_ids: tuple[str, ...] = (),
    cannot: str = "This structure cannot be deployed.",
) -> _Found:
    """What an answer finds of a structure that is refused before anything is resolved for it."""
    return _Found(
        state="plan_refused",
        reason_code=str(reason),
        notice=why,
        leg_ids=leg_ids,
        verdict=OptionPreviewVerdict(
            can_be_deployed=False,
            deploy_sentence=f"{cannot} {why}",
            deploy_reason_code=str(reason),
            resolves_as_built=None,
            resolves_sentence="Nothing is resolved for a structure that cannot be deployed.",
            resolves_reason_code=None,
            leg_ids=list(leg_ids),
            red=True,
        ),
    )


@dataclass(frozen=True, slots=True)
class _Asked:
    """A request that stood, and the parts of every answer to it that do not depend on what was found."""

    request: OptionStructurePreviewRequest
    # When the ask was taken up: what an answer with no reading of the supervisor is stamped with.
    now: int

    @property
    def order(self) -> list[str]:
        """The legs' ids in the request's order: a leg's place in it is the number every sentence names it by."""
        return [leg.leg_id for leg in self.request.legs]

    def said(self, text: str) -> str:
        """A sentence that came from the plan's rules, the resolver, the structure check or the money, in the
        builder's words: its legs named by their places, and its money in dollars."""
        return in_the_builders_words(text, self.order)

    def _answer(self, found: _Found) -> OptionStructurePreview:
        request = self.request
        return _answer(
            RequestSaidBack.of(request),
            found,
            now=self.now,
            order_terms=OptionPreviewOrderTerms(
                gap_limit_percent=request.gap_limit_percent,
                entry_wait_seconds=request.entry_wait_seconds,
                cost_limit_percent=request.cost_limit_percent,
                expiry_close_lead_minutes=request.expiry_close_lead_minutes,
                expiry_close_sentence=expiry_close_sentence(request.expiry_close_lead_minutes),
                orders_sentence=orders_in_a_line(
                    gap_limit_percent=request.gap_limit_percent, entry_wait_seconds=request.entry_wait_seconds
                ),
            ),
            legs_asked=legs_asked_view(request),
        )

    def _verdict(
        self,
        plan: ActionPlan,
        *,
        resolves_as_built: bool | None,
        resolves_sentence: str,
        resolves_reason_code: str | None = None,
        leg_ids: tuple[str, ...] = (),
    ) -> OptionPreviewVerdict:
        """The verdict on a structure that can be deployed: that half in words, and what it resolves to now."""
        legs = [leg for leg in plan.on_enter if isinstance(leg, OptionEntryLeg)]
        said = ["It goes to Alpaca as one order."]
        if any(leg.position == "short" for leg in legs):
            said.append("Every sold leg has a bought leg of its kind to cover it.")
        said.append(
            {1: "The exit closes it.", 2: "The exit closes both legs."}.get(len(legs), f"The exit closes all {len(legs)} legs.")
        )
        return OptionPreviewVerdict(
            can_be_deployed=True,
            deploy_sentence=" ".join(said),
            deploy_reason_code=None,
            resolves_as_built=resolves_as_built,
            resolves_sentence=resolves_sentence,
            resolves_reason_code=resolves_reason_code,
            leg_ids=list(leg_ids),
            # Red when what it resolves to is not the structure built. Not known is not red.
            red=resolves_as_built is False,
        )

    def plan_refused(
        self, reason: PreviewReason, why: str, *, leg_ids: tuple[str, ...] = ()
    ) -> OptionStructurePreview:
        return self._answer(_cannot_be_deployed(reason, why, leg_ids=leg_ids))

    def without_figures(
        self, plan: ActionPlan, why: _NoFigures, inputs: ModelInputsRead | None = None
    ) -> OptionStructurePreview:
        return self._answer(
            _Found(
                state=why.state,
                reason_code=str(why.reason_code),
                notice=self.said(why.notice),
                fault=why.fault,
                leg_ids=why.leg_ids,
                contract=why.contract,
                ask_again_in_ms=_ASK_AGAIN_MS if why.passes_by_itself else None,
                verdict=self._verdict(
                    plan,
                    resolves_as_built=None,
                    resolves_sentence=(
                        "Right now its rules do not come to a contract for every leg."
                        if why.state == "not_resolved"
                        else "What it resolves to right now is not known."
                    ),
                ),
                reading=why.reading,
                inputs=inputs,
            )
        )

    def resolved(
        self, plan: ActionPlan, reading: _Reading, figures: StructureFigures, inputs: ModelInputsRead
    ) -> OptionStructurePreview:
        request, name = self.request, plan.structure
        broken = figures.not_as_built
        if broken is None and figures.entry_refusals:
            # A leg quoted too wide, a cost over its limit, or a moment past the entry cutoff: flagged, not red,
            # and never said to be enterable.
            comes_to = "every leg comes to a contract" if name is None else f"it resolves to {with_its_article(name)}"
            resolves = f"Right now {comes_to}, but an entry would be skipped. The reasons are listed with the entry."
        elif broken is None:
            resolves = (
                "Right now every leg comes to a contract, and the structure can be entered."
                if name is None
                else f"Right now it resolves to {with_its_article(name)}."
            )
        elif name is not None and broken.reason in _SHAPE_REFUSALS:
            resolves = sentence(f"Right now this is not {with_its_article(name)}: {self.said(broken.detail)}")
        else:
            resolves = sentence(f"Right now this cannot be entered as built: {self.said(broken.detail)}")
        lean_unknown = None
        if figures.lean is None and figures.size is not None:
            if inputs.dividends is None:
                lean_unknown = (
                    f"No lean is shown: the dividends for {request.underlying} are not known, so no delta is worked out."
                )
            else:
                assert figures.lean_unknown_leg_id is not None
                without = leg_names([figures.lean_unknown_leg_id], self.order).lower()
                lean_unknown = f"No lean is shown: no delta could be worked out for {without} from its quote."
        stood_on = (reading.underlying_quote, *(leg.quote for leg in figures.legs))
        money = money_view(figures, request.cost_limit_percent)
        return self._answer(
            _Found(
                state="resolved",
                verdict=self._verdict(
                    plan,
                    resolves_as_built=broken is None,
                    resolves_sentence=resolves,
                    resolves_reason_code=None if broken is None else broken.reason,
                    leg_ids=() if broken is None else broken.leg_ids,
                ),
                reading=reading,
                inputs=inputs,
                oldest_quote_age_ms=reading.now - min(when for quote in stood_on for when in receipt_times(quote)),
                legs=tuple(
                    leg_view(leg, reading.now, number=number, gap_limit_percent=request.gap_limit_percent)
                    for number, leg in enumerate(figures.legs, start=1)
                ),
                money=money,
                at_expiry=at_expiry_view(figures, request.underlying),
                lean=lean_view(figures, request.underlying, request.listens_to),
                lean_unknown_sentence=lean_unknown,
                decay=decay_view(
                    figures,
                    request.underlying,
                    self.order,
                    goes_ex_soon=goes_ex_soon((paid.ex_ms for paid in inputs.dividends or ()), reading.now),
                ),
                entry_refusals=tuple(
                    entry_refusal_views(
                        figures,
                        self.order,
                        gap_limit_percent=request.gap_limit_percent,
                        cost_sentence=None if money is None else money.cost_sentence,
                    )
                ),
                entry_sentence=entry_sentence(figures.entry_refusals, not_as_built=broken is not None),
            )
        )


def preview_of_numbers_out_of_range(
    refused: OutOfRange, *, watch: OptionWatchList, clock: Clock = now_ms_utc
) -> OptionStructurePreview:
    """The answer to a request whose numbers are outside their ranges: what cannot be deployed, and why, box
    by box. Nothing is resolved for it, and whatever quote lines its page held are given back."""
    watch.unwatch(_KEY_PREFIX + refused.said_back.preview_key)
    found = _cannot_be_deployed(
        PreviewReason.VALUE_OUT_OF_RANGE,
        " ".join(refused.sentences),
        leg_ids=refused.leg_ids,
        cannot="This cannot be deployed as it is.",
    )
    # The request did not stand, so its rules and its terms are not said back as if they did.
    return _answer(refused.said_back, found, now=clock(), order_terms=None, legs_asked=[])


async def preview_option_structure(
    request: OptionStructurePreviewRequest,
    *,
    quotes: IbkrMarketStatusSource | None,
    watch: OptionWatchList,
    model_inputs: ModelInputsReader,
    searches: PreviewSearches,
    strikes_each_side: int,
    clock: Clock = now_ms_utc,
) -> OptionStructurePreview:
    """What ``request``'s structure would pick right now from the quotes ``quotes`` holds, and its figures.

    ``watch`` is the list ``quotes`` keeps its lines to: the preview's window
    is put on it, on a lease. ``strikes_each_side`` is the reach that
    supervisor watches a window with, so that the preview knows which
    contracts it asked for. ``searches`` is where the search for the
    contracts runs: a worker process, never this one. An answer is always
    this ask's own search, of the quotes this ask read: a search that took
    too long to wait for is not shown to a later ask.
    """
    key = _KEY_PREFIX + request.preview_key
    asked = _Asked(request, clock())
    plan = plan_or_refusal(request)
    if isinstance(plan, PlanRefusal):
        # A structure that cannot be deployed has nothing worth a quote line.
        watch.unwatch(key)
        return asked.plan_refused(plan.reason, plan.why, leg_ids=plan.leg_ids)
    if quotes is None:
        return asked.without_figures(
            plan,
            _NoFigures("no_quote_source", PreviewReason.NO_QUOTE_SOURCE, "This process has no IBKR quote source for options."),
        )
    since_ms = watch.watch_on_lease(
        key,
        WatchRequest(underlying=request.underlying, line_class=LineClass.PREVIEW, plan=plan),
        lease_ms=PREVIEW_LEASE_MS,
        at_most=MAX_PREVIEW_WINDOWS,
    )
    if since_ms is None:
        return asked.without_figures(
            plan,
            _NoFigures(
                "no_quote_line",
                PreviewReason.TOO_MANY_OPEN,
                f"Too many previews are open. {MAX_PREVIEW_WINDOWS} may hold quote lines at once, and this one's "
                "were given up to a newer one. Close another, then refresh this one.",
            ),
        )
    # The search's worker loads while the quotes arrive.
    searches.warm()

    def read() -> _Reading | _NoFigures:
        return _read(
            plan, request.underlying, quotes=quotes, strikes_each_side=strikes_each_side, since_ms=since_ms, now=clock()
        )

    # Read once to learn which expiries the plan comes to, look the model's inputs up for them, which can
    # take seconds the first time, then read again: a resolution is handed quotes as they stand now.
    first = read()
    if isinstance(first, _Reading):
        first = _lines_stand_in_the_way(first) or first
    if isinstance(first, _NoFigures):
        return asked.without_figures(plan, first)
    if _inputs_waits.now >= MAX_PREVIEWS_WAITING_ON_INPUTS:
        return asked.without_figures(plan, _inputs_not_read(request.underlying, first))
    _inputs_waits.now += 1
    try:
        inputs = await asyncio.wait_for(
            off_the_loop(partial(model_inputs, request.underlying, first.now, first.latest_expiry)),
            timeout=MODEL_INPUTS_WAIT_S,
        )
    except (TimeoutError, ModelInputsBeingRead):
        return asked.without_figures(plan, _inputs_not_read(request.underlying, first))
    finally:
        _inputs_waits.now -= 1
    reading = read()
    if isinstance(reading, _NoFigures):
        return _Asked(request, clock()).without_figures(plan, reading, inputs)
    blocked = _lines_stand_in_the_way(reading)
    if blocked is not None:
        return asked.without_figures(plan, blocked, inputs)

    model = inputs.model
    if model is None:
        if any(isinstance(leg, OptionEntryLeg) and isinstance(leg.strike, DeltaStrike) for leg in plan.on_enter):
            unknown = _NoFigures(
                "pricing_inputs_unknown",
                PreviewReason.DIVIDENDS_UNKNOWN,
                f"The dividends {request.underlying} pays before expiry are not known, and a leg here is picked by "
                f"delta, which needs them. Nothing is picked on a guess. {inputs.dividends_source}",
                reading=reading,
            )
            return asked.without_figures(plan, unknown, inputs)
        # No rule here picks by delta, so the picks do not depend on the model. Nothing it says is shown.
        model = inputs.unverified_model()
    # The close terms a bot deployed with these two times would carry. The request sets no other, so the
    # sold-leg close lead, the cancel allowance and the margin are the ones a Deploy starts from.
    close_terms = close_terms_with(
        entry_wait_seconds=request.entry_wait_seconds, expiry_close_lead_minutes=request.expiry_close_lead_minutes
    )
    found = await searches.search(
        key,
        SearchAsk(
            plan=plan,
            chain=reading.chain,
            underlying=UnderlyingObservation(pricing_bar_close=reading.money, quote=reading.underlying_quote),
            feed=reading.feed,
            model=model,
            # The entry cutoff a bot deployed with these two times would be held to on a contract's expiry day.
            limits=ResolverLimits(
                max_quote_age_ms=DECISION_DATA_MAX_AGE_MS, entry_cutoff_lead_ms=close_terms.entry_cutoff_lead_ms
            ),
            decision_bar_close_ms=reading.now,
            selection_ms=reading.now,
            # Priced in the same pass, in the worker. Not asked for on a model that is not known.
            daily_decay=inputs.dividends is not None,
        ),
    )
    if isinstance(found, SearchNotRun):
        return asked.without_figures(plan, _search_not_done(found, reading), inputs)
    if isinstance(found, ContractsNotResolved):
        return asked.without_figures(plan, _not_resolved(found, reading), inputs)
    today = reading.today
    decays = None
    if isinstance(found, ResolutionWithDecay):
        found, decays = found.resolution, found.daily_decay
    figures = structure_figures(
        plan,
        found,
        units=request.structures,
        leg_gap_limit_percent=request.gap_limit_percent,
        cost_limit_percent=request.cost_limit_percent,
        fee_provision=partial(_fee_provision, today),
        model_is_known=inputs.dividends is not None,
        # The Clerk refuses an entry too close to the close its position would owe today, a sold leg's
        # included. Asked here of the moment these quotes were read (ADR 0076 decision 17).
        at_ms=reading.now,
        close_terms=close_terms,
        daily_decay=decays,
    )
    return asked.resolved(plan, reading, figures, inputs)
