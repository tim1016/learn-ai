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
inputs not known. Only a resolution carries figures.

The supervisor is read on the event loop, where its callbacks write. The
lookups of the rate and the dividends, and the resolution itself, block, and
run off it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import ValidationError

from app.broker.alpaca.option_fees import OptionRateNotPinnedError, fees_for_option_fill, option_fee_provision_cents
from app.broker.contract.models import OrderSide
from app.broker.ibkr.market_subscription import DECISION_DATA_MAX_AGE_MS
from app.broker.ibkr.option_quotes import LineUse, WatchRequest
from app.options.contract import OptionContract
from app.options.deadlines import CloseTerms
from app.options.preview import StructureFigures, structure_figures
from app.options.quotes import HeldQuote, quote_fault
from app.options.requirement import EntrySize, LegQuote
from app.options.resolver import (
    Chain,
    ContractsNotResolved,
    FeedState,
    ResolverLimits,
    ResolverRefusal,
    UnderlyingObservation,
    resolve_contracts,
)
from app.options.structure import ResolvedStructure
from app.options.structure_catalogue import label
from app.options.structure_check import StructureRefusal
from app.options.watch import LineClass, NoLine, WindowContract, expiries_wanted, watched_window
from app.schemas.action_plan import ActionPlan, DeltaStrike, OptionEntryLeg
from app.schemas.option_preview import (
    OptionPreviewVerdict,
    OptionPreviewWatched,
    OptionPreviewWatchedRange,
    OptionStructurePreview,
    OptionStructurePreviewRequest,
    PreviewState,
)
from app.services.option_model_inputs import ModelInputsBeingRead, ModelInputsRead
from app.services.option_preview_views import (
    at_expiry_view,
    contract_view,
    lean_view,
    leg_view,
    money_view,
    pricing_inputs_view,
    quote_view,
    sentence,
)
from app.services.option_watch import OptionWatchList
from app.utils.session_anchors import et_date_at_ms
from app.utils.timestamps import Clock, now_ms_utc

if TYPE_CHECKING:
    from app.broker.ibkr.market_liveness import IbkrMarketStatusSource

logger = logging.getLogger(__name__)

# How long a page's request for quotes stands after its last ask.
PREVIEW_LEASE_MS = 60_000
# How long after a window is first asked for its quotes are said to be arriving. After that, a quote that
# has not come is reported as what it is.
ARRIVAL_GRACE_MS = 10_000
# The most windows pages may hold at once. One more gives up the one asked for longest ago.
MAX_PREVIEW_WINDOWS = 4
# How long one ask waits for the rate and the dividends to be looked up before it answers that they are
# not read yet. The lookup goes on, and a later ask finds what it learned.
MODEL_INPUTS_WAIT_S = 4.0
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
    said = [str(error["msg"]).removeprefix("Value error, ") for error in refused.errors(include_url=False)]
    return " ".join(sentence(text) for text in said)


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
        return reason, (
            "IBKR refused a quote line for this preview: the account's allowance is in use, here or in another "
            f"IBKR session. {cannot_see}"
        )
    return PreviewReason.LINE_REFUSED, sentence(f"No quote line could be had for this preview: {reason}")


def _fee_provision(trade_date: date, structure: ResolvedStructure, markets: dict[str, LegQuote], size: EntrySize) -> int | None:
    """What the entry sets aside for its own fees: each leg filled at the price it is entered at, on
    ``trade_date``. None when a rate is not pinned for that date."""
    fills = [
        fees_for_option_fill(
            trade_date=trade_date,
            side=OrderSide.BUY if leg.position == "long" else OrderSide.SELL,
            contracts=Decimal(size.units * leg.ratio),
            premium=markets[leg.leg_id].ask if leg.position == "long" else markets[leg.leg_id].bid,
            multiplier=size.multiplier,
        )
        for leg in structure.legs
    ]
    try:
        return option_fee_provision_cents(fills)
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


def _watched_view(reading: _Reading) -> OptionPreviewWatched:
    kinds = sorted({(seen.contract.right, seen.contract.expiry_ms) for seen in reading.window})
    ranges = []
    for right, expiry_ms in kinds:
        strikes = sorted(
            contract.strike
            for contract in map(OptionContract.from_occ_symbol, reading.chain.watched)
            if (contract.right, contract.expiry_ms) == (right, expiry_ms)
        )
        if strikes:
            ranges.append(
                OptionPreviewWatchedRange(
                    right=right, expiry_ms=expiry_ms, lowest_strike=strikes[0], highest_strike=strikes[-1], contracts=len(strikes)
                )
            )
    return OptionPreviewWatched(
        ranges=ranges,
        lines_wanted=len(reading.wanted),
        lines_held=len(reading.wanted & reading.chain.watched.keys()),
        lines_refused=len(reading.refused),
        line_budget=reading.use.budget,
        lines_open=reading.use.open,
    )


def _read(
    plan: ActionPlan,
    underlying: str,
    *,
    quotes: IbkrMarketStatusSource,
    strikes_each_side: int,
    arriving: bool,
    now: int,
) -> _Reading | _NoFigures:
    """Read the supervisor once. Everything a resolution is handed comes from this one moment."""
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
    if use.budget == 0:
        return _NoFigures("no_quote_line", *_no_line_words(NoLine.OVER_BUDGET, use))
    chain = quotes.option_chain(underlying)
    not_read = _NoFigures(
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
    )


def _lines_stand_in_the_way(reading: _Reading, arriving: bool) -> _NoFigures | None:
    """When none of the preview's contracts holds a line: why there is nothing to resolve against."""
    if not reading.wanted or reading.wanted & reading.chain.watched.keys():
        return None
    if reading.refused:
        return _NoFigures(
            "no_quote_line", *_no_line_words(next(iter(reading.refused.values())), reading.use), reading=reading
        )
    if arriving:
        return _NoFigures(
            "quotes_arriving",
            PreviewReason.QUOTES_ARRIVING,
            "Quotes are arriving. The quote lines for these contracts are being opened.",
            passes_by_itself=True,
            reading=reading,
        )
    return None


def _not_resolved(refused: ContractsNotResolved, reading: _Reading, arriving: bool) -> _NoFigures:
    """A resolver's refusal, as the state a preview names it by."""
    contract = refused.contract
    fault = None if refused.fault is None else str(refused.fault)
    if refused.reason is ResolverRefusal.SEARCH_INCOMPLETE:
        symbol = None if contract is None else contract.occ_symbol
        # With no contract named, a rule by delta found nothing of its kind watched at all.
        without = reading.refused.get(symbol) if symbol is not None else next(iter(reading.refused.values()), None)
        if without is not None:
            return _NoFigures(
                "no_quote_line", *_no_line_words(without, reading.use), leg_ids=refused.leg_ids, contract=contract, reading=reading
            )
        if arriving and (symbol is None or symbol in reading.wanted or symbol in reading.chain.watched):
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


# ---- the answer -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Asked:
    """What was asked, and the parts of every answer that do not depend on what was found."""

    request: OptionStructurePreviewRequest
    now: int

    def _answer(self, **found: object) -> OptionStructurePreview:
        request = self.request
        answer: dict[str, object] = {
            "reason_code": None,
            "notice": None,
            "fault": None,
            "leg_ids": [],
            "contract": None,
            "ask_again_in_ms": None,
            "underlying": request.underlying,
            "structure": request.structure,
            "structure_label": "Custom" if request.structure is None else label(request.structure),
            "structures": request.structures,
            "gap_limit_percent": request.gap_limit_percent,
            "cost_limit_percent": request.cost_limit_percent,
            "quotes_read_ms": self.now,
            "feed_generation": None,
            "oldest_quote_age_ms": None,
            "underlying_quote": None,
            "the_money": None,
            "the_money_sentence": None,
            "legs": [],
            "money": None,
            "at_expiry": None,
            "lean": None,
            "lean_unknown_sentence": None,
            "watched": None,
            "pricing_inputs": None,
            "preview_sentence": "A preview. The bot picks again at every entry.",
        }
        answer |= found
        if answer["reason_code"] is not None:
            answer["reason_code"] = str(answer["reason_code"])
        return OptionStructurePreview.model_validate(answer)

    def _deployable(self, plan: ActionPlan) -> dict[str, object]:
        legs = [leg for leg in plan.on_enter if isinstance(leg, OptionEntryLeg)]
        said = ["It goes to Alpaca as one order."]
        if any(leg.position == "short" for leg in legs):
            said.append("Every sold leg has a bought leg of its kind to cover it.")
        said.append(
            {1: "The exit closes it.", 2: "The exit closes both legs."}.get(len(legs), f"The exit closes all {len(legs)} legs.")
        )
        return {"can_be_deployed": True, "deploy_sentence": " ".join(said), "deploy_reason_code": None}

    def _of_the_reading(self, reading: _Reading) -> dict[str, object]:
        underlying = self.request.underlying
        return {
            "quotes_read_ms": reading.now,
            "feed_generation": reading.feed.generation,
            "underlying_quote": quote_view(reading.underlying_quote, reading.now),
            "the_money": reading.money,
            "the_money_sentence": (
                f"Strikes are counted from {underlying}'s live mid quote, {reading.money}. "
                "A bot counts from the close of the bar it decided on."
            ),
            "watched": _watched_view(reading),
        }

    def plan_refused(self, reason: PreviewReason, why: str) -> OptionStructurePreview:
        return self._answer(
            state="plan_refused",
            reason_code=reason,
            notice=why,
            verdict=OptionPreviewVerdict(
                can_be_deployed=False,
                deploy_sentence=f"This structure cannot be deployed. {why}",
                deploy_reason_code=str(reason),
                resolves_as_built=None,
                resolves_sentence="Nothing is resolved for a structure that cannot be deployed.",
                resolves_reason_code=None,
                leg_ids=[],
                red=True,
            ),
        )

    def without_figures(
        self, plan: ActionPlan, why: _NoFigures, inputs: ModelInputsRead | None = None
    ) -> OptionStructurePreview:
        return self._answer(
            state=why.state,
            reason_code=why.reason_code,
            notice=why.notice,
            fault=why.fault,
            leg_ids=list(why.leg_ids),
            contract=None if why.contract is None else contract_view(why.contract),
            ask_again_in_ms=_ASK_AGAIN_MS if why.passes_by_itself else None,
            verdict=OptionPreviewVerdict(
                **self._deployable(plan),
                resolves_as_built=None,
                resolves_sentence=(
                    "Right now its rules do not come to a contract for every leg."
                    if why.state == "not_resolved"
                    else "What it resolves to right now is not known."
                ),
                resolves_reason_code=None,
                leg_ids=[],
                red=False,
            ),
            pricing_inputs=None if inputs is None else pricing_inputs_view(inputs),
            **({} if why.reading is None else self._of_the_reading(why.reading)),
        )

    def resolved(
        self, plan: ActionPlan, reading: _Reading, figures: StructureFigures, inputs: ModelInputsRead
    ) -> OptionStructurePreview:
        request, name = self.request, plan.structure
        broken = figures.not_as_built
        if broken is None:
            resolves = (
                "Right now every leg comes to a contract, and the structure can be entered."
                if name is None
                else f"Right now it resolves to a {name.in_words}."
            )
        elif name is not None and broken.reason in _SHAPE_REFUSALS:
            resolves = sentence(f"Right now this is not a {name.in_words}: {broken.detail}")
        else:
            resolves = sentence(f"Right now this cannot be entered as built: {broken.detail}")
        lean_unknown = None
        if figures.lean is None and figures.size is not None:
            lean_unknown = (
                f"No lean is shown: the dividends for {request.underlying} are not known, so no delta is worked out."
                if inputs.dividends is None
                else f"No lean is shown: no delta could be worked out for leg {figures.lean_unknown_leg_id} from its quote."
            )
        return self._answer(
            state="resolved",
            verdict=OptionPreviewVerdict(
                **self._deployable(plan),
                resolves_as_built=broken is None,
                resolves_sentence=resolves,
                resolves_reason_code=None if broken is None else broken.reason,
                leg_ids=[] if broken is None else list(broken.leg_ids),
                red=broken is not None,
            ),
            legs=[leg_view(leg, reading.now) for leg in figures.legs],
            money=money_view(figures, request.cost_limit_percent),
            at_expiry=at_expiry_view(figures, request.underlying),
            oldest_quote_age_ms=max(
                age
                for quote in (reading.underlying_quote, *(leg.quote for leg in figures.legs))
                for age in (reading.now - side.received_ms for side in (quote.bid, quote.ask) if side is not None)
            ),
            lean=lean_view(figures, request.underlying, request.listens_to),
            lean_unknown_sentence=lean_unknown,
            pricing_inputs=pricing_inputs_view(inputs),
            **self._of_the_reading(reading),
        )


def _make_room(watch: OptionWatchList, key: str) -> None:
    """Keep pages to ``MAX_PREVIEW_WINDOWS`` windows: a new one gives up the one asked for longest ago."""
    others = [held for held in watch.on_lease() if held.startswith(_KEY_PREFIX) and held != key]
    for stale in others[: max(len(others) - (MAX_PREVIEW_WINDOWS - 1), 0)]:
        watch.unwatch(stale)


async def preview_option_structure(
    request: OptionStructurePreviewRequest,
    *,
    quotes: IbkrMarketStatusSource | None,
    watch: OptionWatchList,
    model_inputs: ModelInputsReader,
    strikes_each_side: int,
    clock: Clock = now_ms_utc,
) -> OptionStructurePreview:
    """What ``request``'s structure would pick right now from the quotes ``quotes`` holds, and its figures.

    ``watch`` is the list ``quotes`` keeps its lines to: the preview's window
    is put on it, on a lease. ``strikes_each_side`` is the reach that
    supervisor watches a window with, so that the preview knows which
    contracts it asked for.
    """
    key = _KEY_PREFIX + request.preview_key
    asked = _Asked(request, clock())
    if not request.legs:
        watch.unwatch(key)
        return asked.plan_refused(PreviewReason.NO_LEGS, "Add a leg. A structure has one to four.")
    if not request.underlying.isalpha():
        watch.unwatch(key)
        return asked.plan_refused(
            PreviewReason.SHARE_CLASS_UNDERLYING,
            f"Options on a ticker with a share class, such as {request.underlying}, are not handled yet.",
        )
    try:
        plan = plan_of(request)
    except ValidationError as refused:
        # A structure that cannot be deployed has nothing worth a quote line.
        watch.unwatch(key)
        return asked.plan_refused(PreviewReason.PLAN_REFUSED, _the_plans_own_words(refused))
    if quotes is None:
        return asked.without_figures(
            plan,
            _NoFigures("no_quote_source", PreviewReason.NO_QUOTE_SOURCE, "This process has no IBKR quote source for options."),
        )
    _make_room(watch, key)
    since_ms = watch.watch_on_lease(
        key,
        WatchRequest(underlying=request.underlying, line_class=LineClass.PREVIEW, plan=plan),
        lease_ms=PREVIEW_LEASE_MS,
    )

    def read() -> _Reading | _NoFigures:
        now = clock()
        return _read(
            plan,
            request.underlying,
            quotes=quotes,
            strikes_each_side=strikes_each_side,
            arriving=now - since_ms <= ARRIVAL_GRACE_MS,
            now=now,
        )

    # Read once to learn which expiries the plan comes to, look the model's inputs up for them, which can
    # take seconds the first time, then read again: a resolution is handed quotes as they stand now.
    first = read()
    if isinstance(first, _Reading):
        first = _lines_stand_in_the_way(first, first.now - since_ms <= ARRIVAL_GRACE_MS) or first
    if isinstance(first, _NoFigures):
        return asked.without_figures(plan, first)
    try:
        inputs = await asyncio.wait_for(
            asyncio.to_thread(model_inputs, request.underlying, first.now, first.latest_expiry),
            timeout=MODEL_INPUTS_WAIT_S,
        )
    except (TimeoutError, ModelInputsBeingRead):
        not_read = _NoFigures(
            "pricing_inputs_unknown",
            PreviewReason.PRICING_INPUTS_NOT_READ,
            f"The rate and the dividends for {request.underlying} are still being looked up.",
            passes_by_itself=True,
            reading=first,
        )
        return asked.without_figures(plan, not_read)
    reading = read()
    if isinstance(reading, _NoFigures):
        return _Asked(request, clock()).without_figures(plan, reading, inputs)
    asked = _Asked(request, reading.now)
    arriving = reading.now - since_ms <= ARRIVAL_GRACE_MS
    blocked = _lines_stand_in_the_way(reading, arriving)
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
    try:
        resolution = await asyncio.to_thread(
            resolve_contracts,
            plan,
            chain=reading.chain,
            underlying=UnderlyingObservation(pricing_bar_close=reading.money, quote=reading.underlying_quote),
            feed=reading.feed,
            model=model,
            limits=ResolverLimits(
                max_quote_age_ms=DECISION_DATA_MAX_AGE_MS, entry_cutoff_lead_ms=CloseTerms().entry_cutoff_lead_ms
            ),
            decision_bar_close_ms=reading.now,
            selection_ms=reading.now,
        )
    except ContractsNotResolved as refused:
        return asked.without_figures(plan, _not_resolved(refused, reading, arriving), inputs)
    figures = structure_figures(
        plan,
        resolution,
        units=request.structures,
        cost_limit_percent=request.cost_limit_percent,
        fee_provision=lambda structure, markets, size: _fee_provision(reading.today, structure, dict(markets), size),
        model_is_known=inputs.dividends is not None,
    )
    return asked.resolved(plan, reading, figures, inputs)
