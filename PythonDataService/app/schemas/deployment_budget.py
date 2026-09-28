"""UI contracts for one reviewed deployment and its custody-derived money."""
from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.broker.alpaca.clerk.account_money import FULL_BAR_BPS, SegmentKind
from app.broker.alpaca.clerk.models import EpochMs
from app.broker.alpaca.clerk.money import consent_cents, dollars
from app.schemas.account_authority import AuthorityKind


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
    holds money.
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
    segments: tuple[MoneySegment, ...] = ()

    @model_validator(mode="after")
    def conserves_every_dollar(self) -> AccountMoneyView:
        headline = (self.total_usd, self.cash_usd, self.free_to_deploy_usd, self.in_bots_usd,
                    self.held_by_stopped_usd, self.outside_bots_usd, self.account_charges_usd,
                    self.settling_usd, self.stopped_holding_count)
        shortfall = self.account_shortfall_usd
        if self.state != "ready":
            notes = (shortfall, self.open_pnl_usd, self.open_pnl_detail)
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


class DeploymentBudgetPreview(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # ``awaiting_price``: the review asked IBKR for the instrument and no fresh
    # quote has arrived yet -- transient, so the client re-checks.
    state: Literal["ready", "unavailable", "awaiting_price"]
    detail: str
    world: AuthorityKind
    # The account whose money this budget comes from. ``None`` for Dry Run:
    # its simulated cash lives in the bot's own ``sim:`` account, which is
    # named from the bot at Deploy, so no real account number applies (H18).
    custody_account_id: str | None = None
    # How the bot will be named -- the backend names it at Deploy (#2551).
    bot_name_note: str = "The bot is named at Deploy."
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


class DeploymentBudgetView(BaseModel):
    """All dollars are authored by Python, including display rounding."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["ready", "unavailable", "legacy"]
    detail: str
    strategy_instance_id: str
    world: AuthorityKind
    committed_usd: str | None = None
    realized_gross_usd: str | None = None
    fees_usd: str | None = None
    position_cost_usd: str | None = None
    pending_orders_usd: str | None = None
    outstanding_cash_usd: str | None = None
    free_usd: str | None = None
    released_usd: str | None = None
    shortfall_usd: str | None = None
    entry_eligible: bool = False
    observed_at_ms: EpochMs | None = None
    # This bot's slice, shaded exactly as its segment on the account's bar.
    parts: MoneyParts | None = None


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
