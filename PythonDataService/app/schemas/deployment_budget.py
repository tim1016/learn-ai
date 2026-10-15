"""UI contracts for one reviewed deployment and its custody-derived money."""
from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from app.broker.alpaca.clerk.account_money import FULL_BAR_BPS, SegmentKind
from app.broker.alpaca.clerk.account_picture import AccountPicture, SpokenForKind
from app.broker.alpaca.clerk.funding import EntryNeed, Figure, FundingAnswer, Term
from app.broker.alpaca.clerk.models import EpochMs
from app.broker.alpaca.clerk.money import consent_cents, dollars
from app.schemas.account_authority import AuthorityKind
from app.schemas.option_deploy import OptionDeployReview
from app.schemas.option_preview import OptionContractView
from app.schemas.traded_asset_acknowledgement import UntestedTradedAssetWarning


class DeploymentBudgetInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    amount_usd: str = Field(min_length=1, max_length=40)
    risk_revision: int = Field(strict=True, ge=0)
    review_token: str | None = Field(default=None, min_length=1, max_length=128)
    live_confirmation: str | None = Field(default=None, max_length=200)

    @field_validator("amount_usd")
    @classmethod
    def validate_amount(cls, value: str) -> str:
        return dollars(consent_cents(value))


class DeployBudgetConsent(BaseModel):
    """Server-resolved transient input, excluded from historical strategy seals."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    committed_cents: int = Field(strict=True, gt=0, le=2**63 - 1)
    risk_revision: int = Field(strict=True, ge=0)
    actor: str = Field(min_length=1)
    request_fingerprint: str = Field(min_length=1)
    world: AuthorityKind


class DeploymentBudgetShortcut(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: Literal["quarter", "half", "all", "position_headroom"]
    label: str
    amount_usd: str
    explanation: str


def _cents(amount_usd: str) -> int:
    """Exact whole cents; a stated figure is never silently truncated."""
    try:
        scaled = Decimal(amount_usd) * 100
    except ArithmeticError as exc:
        raise ValueError(f"not a dollar amount: {amount_usd!r}") from exc
    if not scaled.is_finite() or scaled != scaled.to_integral_value():
        raise ValueError(f"dollar amount is not whole cents: {amount_usd!r}")
    return int(scaled)


class MoneyParts(BaseModel):
    """A bot's slice, shaded. Widths are basis points of the slice itself.

    They sum to 10000, or to 0 when the slice holds nothing (a flat bot
    that overran its budget draws no part; its shortfall is reported).
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    in_shares_usd: str
    in_shares_bps: int = Field(ge=0, le=FULL_BAR_BPS)
    pending_usd: str
    pending_bps: int = Field(ge=0, le=FULL_BAR_BPS)
    free_usd: str
    free_bps: int = Field(ge=0, le=FULL_BAR_BPS)

    @model_validator(mode="after")
    def widths_fill_the_slice(self) -> MoneyParts:
        empty = not any(_cents(value) for value in (self.in_shares_usd, self.pending_usd, self.free_usd))
        if self.in_shares_bps + self.pending_bps + self.free_bps != (0 if empty else FULL_BAR_BPS):
            raise ValueError("a slice's part widths sum to 10000 basis points, or to 0 for an empty slice")
        return self


class MoneySegment(BaseModel):
    """One place the account's money is, in display order on the money bar.

    ``label`` is the legend's words for the slice; ``share_bps`` its width.
    ``parts`` belong to a running bot, and ``shortfall_usd`` to one that spent
    beyond its balance -- absent, never "0.00", when nothing is short;
    ``released_usd`` and ``still_claimed_usd`` to a stopped bot that still
    holds money. ``released_usd`` is what its Stop released, as the Stop
    recorded it (#2555); ``released_estimated`` marks a Stop that recorded
    none, whose figure is an estimate from the bot's money now.
    ``palette_index`` is a bot's stable colour slot (its registration order
    on the account), the same on every surface that draws that bot.
    ``settling`` is sale proceeds on their way into cash: not yet free to
    deploy, never a shortfall.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: SegmentKind
    strategy_instance_id: str | None = None
    label: str
    amount_usd: str
    share_bps: int = Field(ge=0, le=FULL_BAR_BPS)
    parts: MoneyParts | None = None
    shortfall_usd: str | None = None
    released_usd: str | None = None
    released_estimated: bool = False
    still_claimed_usd: str | None = None
    palette_index: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def carries_its_kind_fields(self) -> MoneySegment:
        bot, stopped = self.kind == "bot", self.kind == "stopped"
        if (self.strategy_instance_id is not None) != (bot or stopped):
            raise ValueError("only a bot or stopped slice names a bot")
        if (self.palette_index is not None) != (bot or stopped):
            raise ValueError("a bot or stopped slice, and only one, carries its bot's palette index")
        if (self.parts is not None) != bot or (self.shortfall_usd is not None and not bot):
            raise ValueError("parts and shortfall_usd belong to a running bot's slice")
        if self.shortfall_usd is not None and _cents(self.shortfall_usd) <= 0:
            raise ValueError("a shortfall is stated only when something is short")
        if (self.released_usd is not None, self.still_claimed_usd is not None) != (stopped, stopped):
            raise ValueError("released_usd and still_claimed_usd belong to a stopped bot's slice")
        if self.released_estimated and not stopped:
            raise ValueError("only a stopped bot's release can be an estimate")
        return self


class AccountMoneyView(BaseModel):
    """Where one account's money is: the single read every money bar draws.

    Python authors every dollar string and every width. When ``ready``, the
    segments sum exactly (in cents) to ``total_usd`` plus
    ``account_shortfall_usd`` (what the bots' and orders' claims exceed the
    account by; absent unless overdrawn), their widths to 10000 basis points,
    and ``free_to_deploy_usd`` is the Deploy preview's unreserved cash.
    Otherwise the bar's figures are absent and ``detail`` names the reason --
    an unknown is never shown as $0 -- while the broker's own ``equity_usd``
    and ``today_pnl_usd`` stay whenever they are known.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["ready", "unavailable", "legacy"]
    detail: str
    world: AuthorityKind
    account_id: str
    observed_at_ms: EpochMs | None = None
    total_usd: str | None = None
    cash_usd: str | None = None
    free_to_deploy_usd: str | None = None
    in_bots_usd: str | None = None
    held_by_stopped_usd: str | None = None
    outside_bots_usd: str | None = None
    account_charges_usd: str | None = None
    settling_usd: str | None = None
    account_shortfall_usd: str | None = None
    stopped_holding_count: int | None = Field(default=None, ge=0)
    # Notes beside the bar, never slices of it. Open P&L is canonical FIFO
    # over every held lot at current marks; with no mark it is absent and
    # ``open_pnl_detail`` says why.
    open_pnl_usd: str | None = None
    open_pnl_detail: str | None = None
    equity_usd: str | None = None
    today_pnl_usd: str | None = None
    # A broker-flat reading can disagree with historical outside fills. Keep
    # the book amount visible, but do not present it as a confirmed holding.
    broker_position_disagreement: str | None = None
    segments: tuple[MoneySegment, ...] = ()
    # Why Deploy would refuse a new bot on this account's money right now
    # (a missing daily loss limit, a loss hold, no fresh reading), in the
    # words the Deploy preview refuses with. Absent when Deploy would admit
    # one, so free to deploy never reads as spendable when it is not (PRD
    # #2560 story 12). Beside a ready bar only: otherwise ``detail`` speaks.
    deploy_refusal: str | None = None
    # What the account owns, what is spoken for and where it is concentrated (#3101): the account's own money
    # read carries it, and the Deploy review's bar does not. Absent, never half-drawn, when it could not be
    # worked out, and ``picture_detail`` says why. Beside a ready bar only.
    picture: AccountPictureView | None = None
    picture_detail: str | None = None

    @model_validator(mode="after")
    def conserves_every_dollar(self) -> AccountMoneyView:
        headline = (self.total_usd, self.cash_usd, self.free_to_deploy_usd, self.in_bots_usd,
                    self.held_by_stopped_usd, self.outside_bots_usd, self.account_charges_usd,
                    self.settling_usd, self.stopped_holding_count)
        shortfall = self.account_shortfall_usd
        if self.state != "ready":
            notes = (shortfall, self.open_pnl_usd, self.open_pnl_detail, self.broker_position_disagreement,
                     self.deploy_refusal, self.picture, self.picture_detail)
            if self.segments or any(value is not None for value in (*headline, *notes)):
                raise ValueError("a money read that cannot draw its bar carries a reason, never bar figures")
            if self.state == "legacy" and (self.equity_usd, self.today_pnl_usd, self.observed_at_ms) != (None, None, None):
                raise ValueError("a legacy money read carries only its Settings action")
            if (self.equity_usd, self.today_pnl_usd) != (None, None) and self.observed_at_ms is None:
                raise ValueError("broker figures carry the instant they were observed")
            return self
        total = self.total_usd
        if total is None or any(value is None for value in headline) or self.observed_at_ms is None or not self.segments:
            raise ValueError("a ready money read carries every headline figure and its segments")
        if shortfall is not None and _cents(shortfall) <= 0:
            raise ValueError("an account shortfall is stated only when the account is overdrawn")
        if (self.open_pnl_usd is None) == (self.open_pnl_detail is None):
            raise ValueError("open P&L is either a figure or the reason there is none")
        if self.picture is not None and self.picture_detail is not None:
            raise ValueError("the account's picture is either shown or the reason there is none")
        if sum(_cents(segment.amount_usd) for segment in self.segments) != _cents(total) + (0 if shortfall is None else _cents(shortfall)):
            raise ValueError("the money bar's segments must sum exactly to total_usd plus account_shortfall_usd")
        if sum(segment.share_bps for segment in self.segments) != FULL_BAR_BPS:
            raise ValueError("the money bar's widths must sum to 10000 basis points")
        if self.stopped_holding_count != sum(1 for segment in self.segments if segment.kind == "stopped"):
            raise ValueError("stopped_holding_count is the bar's stopped slices")
        free = [segment for segment in self.segments if segment.kind == "free"]
        if len(free) != 1 or free[0] is not self.segments[-1]:
            raise ValueError("free to deploy is the bar's last slice, exactly once")
        return self


class FigureTermView(BaseModel):
    """One labelled amount a figure is made of, signed."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    amount_usd: str

    @classmethod
    def from_term(cls, term: Term) -> FigureTermView:
        return cls(label=term.label, amount_usd=dollars(term.amount_cents))


class FigureView(BaseModel):
    """One number the owner compares against another, with its source and age (#3097).

    ``known`` and ``stale`` carry an amount and the terms that add up to it;
    a stale amount is the last one worked out. ``unknown`` carries neither,
    never "0.00", and ``source`` says where it would come from.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["known", "stale", "unknown"]
    amount_usd: str | None = None
    source: str
    as_of_ms: EpochMs | None = None
    terms: tuple[FigureTermView, ...] = ()

    @classmethod
    def from_figure(cls, figure: Figure) -> FigureView:
        return cls(
            state=figure.state,
            amount_usd=None if figure.amount_cents is None else dollars(figure.amount_cents),
            source=figure.source, as_of_ms=figure.as_of_ms,
            terms=tuple(FigureTermView.from_term(term) for term in figure.terms),
        )

    @model_validator(mode="after")
    def terms_add_up_to_the_amount(self) -> FigureView:
        if self.state == "unknown":
            if self.amount_usd is not None or self.terms:
                raise ValueError("an unknown figure states no amount and no terms")
        elif self.amount_usd is None:
            raise ValueError("a known or stale figure states its amount")
        elif sum(_cents(term.amount_usd) for term in self.terms) != _cents(self.amount_usd):
            raise ValueError("a figure's terms add up to its amount")
        return self


class EntryNeedView(BaseModel):
    """What one entry commits, and the parts that make it."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    commitment_usd: str
    terms: tuple[FigureTermView, ...]

    @classmethod
    def from_need(cls, need: EntryNeed) -> EntryNeedView:
        return cls(
            commitment_usd=dollars(need.commitment_cents),
            terms=tuple(FigureTermView.from_term(term) for term in need.terms),
        )

    @model_validator(mode="after")
    def parts_add_up_to_the_commitment(self) -> EntryNeedView:
        if sum(_cents(term.amount_usd) for term in self.terms) != _cents(self.commitment_usd):
            raise ValueError("an entry's parts add up to its commitment")
        return self


class FundingView(BaseModel):
    """Whether a bot can fund an entry and which limit binds (``funding.funding_answer``, #3097).

    The Clerk's admission reads the same answer, so the same entry at the same
    moment is never promised here and refused by the Clerk. ``admits`` is the
    Clerk's own rule, in whole cents: ``None`` when there is no priced entry
    or a limit it depends on is not known, and ``False`` with no entry priced
    only when a known limit of the bot's own has under a cent of room, since
    every entry commits at least that. ``binding`` is the known limit with
    the least room; ``fits`` is absent when it cannot be said, and
    ``shortfall_usd`` unless something is short, never "0.00".
    ``broker_buying_power`` is absent for a bot with no broker figure (a Dry
    Run, a Live Shadow). Every dollar and every word is authored by Python.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    free_budget: FigureView
    account_headroom: FigureView
    broker_buying_power: FigureView | None = None
    need: EntryNeedView | None = None
    admits: bool | None = None
    binding: Literal["bot_budget", "account_headroom", "broker_buying_power"] | None = None
    shortfall_usd: str | None = None
    fits: int | None = Field(default=None, ge=0)
    # ``fits`` in words, with the caveat that every bot competes for the same account headroom (#3099).
    fits_sentence: str | None = None
    sentence: str
    action: str | None = None

    @classmethod
    def from_answer(cls, answer: FundingAnswer) -> FundingView:
        return cls(
            free_budget=FigureView.from_figure(answer.free_budget),
            account_headroom=FigureView.from_figure(answer.account_headroom),
            broker_buying_power=None if answer.broker_buying_power is None else FigureView.from_figure(answer.broker_buying_power),
            need=None if answer.need is None else EntryNeedView.from_need(answer.need),
            admits=answer.admits, binding=answer.binding,
            shortfall_usd=dollars(answer.shortfall_cents) if answer.shortfall_cents else None,
            fits=answer.fits, fits_sentence=answer.fits_sentence, sentence=answer.sentence, action=answer.action,
        )


class DeploymentBudgetPreview(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # ``awaiting_price``: the feed is connected, the review asked IBKR for the
    # instrument, and no fresh quote has arrived yet -- transient, so the
    # client re-checks. A disconnected feed is ``unavailable`` with its own
    # copy, never a wait it cannot ride out (#2559).
    state: Literal["ready", "unavailable", "awaiting_price"]
    detail: str
    world: AuthorityKind
    # The account whose money this budget comes from. ``None`` for Dry Run:
    # its simulated cash lives in the bot's own ``sim:`` account, which is
    # named from the bot at Deploy, so no real account number applies (H18).
    custody_account_id: str | None = None
    # How the bot will be named -- the backend names it at Deploy (#2551).
    bot_name_note: str = "The bot is named at Deploy."
    # A warning, never a refusal (#2622): one short line naming the other
    # bots in this account that already trade the chosen symbol. ``None``
    # when there are none, and for Dry Run, which never trades the account.
    same_symbol_note: str | None = None
    # What the strategy's evidence does not cover, when the bot trades another
    # asset than it listens to (#2912). ``None`` when it trades its own symbol.
    traded_asset_warning: UntestedTradedAssetWarning | None = None
    # Confirm's "Trades" line for these settings, written here so no screen
    # composes it: what the bot listens to, what it trades when that is
    # something else, and how many shares. ``None`` on a preview no route
    # served.
    trades_summary: str | None = None
    # What Deploy says of a bot that trades an option structure (#2913): its
    # money rows, what stops the Deploy, and what the owner should know.
    # ``None`` for every bot in shares, and then left out of the review's
    # dump (``_omit_an_absent_option_review``).
    option_review: OptionDeployReview | None = None
    # The previewed amount as Python normalizes it: what Deploy sets aside.
    budget_usd: str | None = None
    observed_at_ms: EpochMs | None = None
    minimum_budget_usd: str | None = None
    unreserved_usd: str | None = None
    estimated_price_usd: str | None = None
    risk_revision: int = Field(default=0, ge=0)
    risk_limits_summary: str = "Account risk evidence is unavailable."
    shortcuts: tuple[DeploymentBudgetShortcut, ...] = ()
    review_token: str | None = None
    confirmation_text: str | None = None
    # The account's money bar with this Deploy drawn in: an admitted budget
    # is a ``new`` slice carved out of free to deploy. With no amount yet, or
    # an amount refused (too little, more than free, stale risk revision --
    # the refusal is ``detail``), it is today's bar, so the Money step always
    # has one to draw. ``None`` when the account itself could not be read,
    # and for Dry Run, which never uses the account's money.
    money_after: AccountMoneyView | None = None
    # Whether this budget can be set aside and fund one entry, and which limit binds: the one answer the
    # review, the option review beside it and the Clerk's re-check at the commit read (#3099). ``None``
    # before the account's side can be worked out.
    funding: FundingView | None = None

    # No return annotation: the review's published schema stays this model's own fields.
    @model_serializer(mode="wrap")
    def _omit_an_absent_option_review(self, handler: SerializerFunctionWrapHandler):
        """A bot in shares is reviewed in exactly the bytes it was before ``option_review`` existed."""
        dumped = handler(self)
        if dumped.get("option_review") is None:
            dumped.pop("option_review", None)
        return dumped


class BudgetStatementLine(BaseModel):
    """One line of a bot's money statement, worded and valued by Python."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    amount_usd: str
    # The statement's running total (the bot's balance), set apart when shown.
    total: bool = False


class ExitLegQuoteView(BaseModel):
    """How far the quote behind one held contract's exit price can be trusted (#3100).

    ``state`` is the leg's own: unknown when the side its close fills at is no
    price, stale when that side is past the age a held contract's mark is
    allowed, known otherwise. ``age_ms`` is that side's age and is absent
    when no side arrived; ``missing`` lists every side that has not arrived;
    ``gap_usd`` is the ask less the bid in dollars a share, exactly as
    quoted, and absent unless both sides arrived.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    contract: OptionContractView
    # Signed whole contracts held: bought above zero, sold below.
    contracts: int
    state: Literal["known", "stale", "unknown"]
    age_ms: int | None = None
    missing: tuple[Literal["bid", "ask"], ...] = ()
    gap_usd: str | None = None


class ExitEstimateView(BaseModel):
    """What getting out of the bot's held option position costs at the quotes held, before fees (#3100).

    ``net_close`` is closing everything as one order and ``gross_buy_back``
    buying the sold legs back alone: two figures, because Alpaca can ask
    for far more buying power for the second than the first suggests. Each
    is known, stale or unknown with its terms and source, never "0.00" for
    a figure that is not known. ``quotes`` says how far each leg's quote can
    be trusted, and ``conditions`` what is not settled. It informs and
    blocks nothing. Every dollar and every word is authored by Python.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    net_close: FigureView
    gross_buy_back: FigureView
    quotes: tuple[ExitLegQuoteView, ...]
    conditions: tuple[str, ...]


class OwnedView(BaseModel):
    """What the account owns: its cash and its marked equity, two figures that are never read as one."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    broker_cash: FigureView
    marked_equity: FigureView
    note: str


class SpokenForLineView(BaseModel):
    """One claim on the cash; its figure's terms are what it is made of, by bot where it is a bot's."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: SpokenForKind
    label: str
    figure: FigureView


class SpokenForView(BaseModel):
    """The cash, partitioned (#3101). ``partition`` is the cash and the lines add up to it, or it is unknown."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    partition: FigureView
    lines: tuple[SpokenForLineView, ...]
    sentence: str

    @model_validator(mode="after")
    def lines_add_up_to_the_cash(self) -> SpokenForView:
        if self.partition.state != "unknown" and self.partition.amount_usd is not None:
            lines = sum(_cents(line.figure.amount_usd) for line in self.lines if line.figure.amount_usd is not None)
            if lines != _cents(self.partition.amount_usd):
                raise ValueError("the lines of what is spoken for add up to the cash")
        return self


class CollateralGroupView(BaseModel):
    """The collateral of one underlying and expiry taken alone.

    ``expiry_ms`` is the expiry date's own anchor, a date and not an instant:
    it is shown as a date in the exchange's time, never in the viewer's.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    underlying: str
    expiry_ms: EpochMs
    collateral_usd: str


class ConcentrationView(BaseModel):
    """Where the collateral is: each underlying and expiry alone, and the account's own figure beside them."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    account_collateral: FigureView
    groups: tuple[CollateralGroupView, ...] = ()
    expiry_note: str | None = None


class ObligationsView(BaseModel):
    """What assignment could oblige, gross and apart from any loss, and what closing everything costs."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    assignment: FigureView
    assignment_note: str
    close_everything: FigureView
    bots_holding: int = Field(ge=0)
    bots_unpriced: int = Field(ge=0)
    conditions: tuple[str, ...] = ()


class AccountPictureView(BaseModel):
    """What the account owns, what is spoken for, where it is concentrated and what it is obliged to (#3101).

    Every dollar and every word is authored by Python, and every figure keeps
    its source, its age and its state. It informs and blocks nothing.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    owned: OwnedView
    spoken_for: SpokenForView
    concentration: ConcentrationView
    obligations: ObligationsView
    note: str

    @classmethod
    def from_picture(cls, picture: AccountPicture) -> AccountPictureView:
        return cls(
            owned=OwnedView(
                broker_cash=FigureView.from_figure(picture.owned.broker_cash),
                marked_equity=FigureView.from_figure(picture.owned.marked_equity),
                note=picture.owned.note,
            ),
            spoken_for=SpokenForView(
                partition=FigureView.from_figure(picture.spoken_for.partition),
                lines=tuple(
                    SpokenForLineView(kind=line.kind, label=line.label, figure=FigureView.from_figure(line.figure))
                    for line in picture.spoken_for.lines
                ),
                sentence=picture.spoken_for.sentence,
            ),
            concentration=ConcentrationView(
                account_collateral=FigureView.from_figure(picture.concentration.account_collateral),
                groups=tuple(
                    CollateralGroupView(
                        underlying=group.underlying, expiry_ms=group.expiry_ms,
                        collateral_usd=dollars(group.collateral_cents),
                    )
                    for group in picture.concentration.groups
                ),
                expiry_note=picture.concentration.expiry_note,
            ),
            obligations=ObligationsView(
                assignment=FigureView.from_figure(picture.obligations.assignment),
                assignment_note=picture.obligations.assignment_note,
                close_everything=FigureView.from_figure(picture.obligations.close_everything),
                bots_holding=picture.obligations.bots_holding, bots_unpriced=picture.obligations.bots_unpriced,
                conditions=picture.obligations.conditions,
            ),
            note=picture.note,
        )


# The money view names the picture, which is defined here with the figures it is made of; the review embeds the view.
AccountMoneyView.model_rebuild()
DeploymentBudgetPreview.model_rebuild()


class DeploymentBudgetView(BaseModel):
    """All dollars are authored by Python, including display rounding.

    ``headline`` and ``detail`` say where this bot's money stands in the
    owner's words -- holding a position is a normal state, never worded as a
    fault (hurdle H25). ``statement`` is the bot's money as the owner reads
    it, in order: a running bot's budget, results, balance and where the
    balance is; a stopped bot's balance, what its Stop released (the Stop's
    recorded figure, #2555), what came back or was charged since, signed,
    and what is still held, which add up to that balance less any "Over its
    budget by". It is empty unless ``state`` is ``ready``.
    ``segment`` is this bot's slice exactly as the account's money bar draws
    it (a running ``bot`` or a ``stopped`` slice), widened to fill a bar of
    its own; ``None`` when the bot is finished and holds no money any more.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["ready", "unavailable", "legacy"]
    headline: str
    detail: str
    strategy_instance_id: str
    world: AuthorityKind
    committed_usd: str | None = None
    entry_eligible: bool = False
    observed_at_ms: EpochMs | None = None
    segment: MoneySegment | None = None
    statement: tuple[BudgetStatementLine, ...] = ()
    # The fine print under the statement, when there is any.
    note: str | None = None
    # Whether the bot can fund its next entry and which limit binds: the
    # answer the Clerk's admission reads (#3097). Only a running bot has one.
    funding: FundingView | None = None
    # What getting out of its held option position costs at the quotes held (#3100). Present only while
    # the bot holds an option contract, a stopped bot's included.
    exit_estimate: ExitEstimateView | None = None

    @model_validator(mode="after")
    def segment_is_this_bots_own(self) -> DeploymentBudgetView:
        if self.segment is not None and self.segment.strategy_instance_id != self.strategy_instance_id:
            raise ValueError("a bot's money view carries only that bot's own slice")
        return self


class BudgetDeployCommandReceipt(BaseModel):
    """Recoverable durable result; process absence never fabricates launch."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["pending", "deployed", "failed"]
    outcome: Literal["pending", "success", "failure"]
    receipt_id: str
    recorded_at_ms: EpochMs
    command_id: str
    # The backend-authored bot name (#2551); a pre-#2551 bot keeps its own.
    strategy_instance_id: str
    run_id: str
    account_id: str
    world: AuthorityKind
    committed_usd: str
    message: str
    explanation: str
    next_action: str
    # The custody commit's own instant: when this bot was first deployed.
    first_deployed_at_ms: EpochMs
    # Deploy again's display-only lineage: the bot this one follows.
    replaces_strategy_instance_id: str | None = None


class DeploySubmissionUncommitted(BaseModel):
    """The recovery read's answer for a key whose Deploy has not committed.

    ``status`` is what this clerk knows, never a guess: ``in_flight`` while
    this process is sending that Deploy now, whether or not it has named its
    bot yet; ``not_committed`` when nothing is sending it and custody holds no
    commit for the name it claimed -- nothing was set aside. A committed
    Deploy answers with its ``BudgetDeployCommandReceipt`` instead, and a key
    never claimed and not being sent is a 404.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["in_flight", "not_committed"]
    submission_key: str
    # The name the key holds now; ``None`` while its Deploy is being sent and
    # has not named its bot yet. A not-committed name is never reused: the
    # key's next Deploy is named from its own minute.
    strategy_instance_id: str | None
    # When that name was claimed; ``None`` exactly when there is no name.
    claimed_at_ms: EpochMs | None
    message: str
    explanation: str
    next_action: str

    @model_validator(mode="after")
    def _named_when_claimed(self) -> DeploySubmissionUncommitted:
        if (self.strategy_instance_id is None) != (self.claimed_at_ms is None):
            raise ValueError("strategy_instance_id and claimed_at_ms are both present or both absent")
        if self.status == "not_committed" and self.strategy_instance_id is None:
            raise ValueError("a not-committed Deploy names the bot it claimed")
        return self


class BudgetAuthorityState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["legacy", "budget"]
    account_id: str
    authorization_version: int
    active_run_count: int
    review_token: str
    detail: str


class BudgetAuthorityApplyRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    review_token: str = Field(min_length=1, max_length=128)
