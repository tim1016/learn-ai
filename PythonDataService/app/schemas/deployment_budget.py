"""UI contracts for one reviewed deployment and its custody-derived money."""
from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


FULL_BAR_BPS = 10_000
MoneySegmentKind = Literal["bot", "stopped", "outside", "charges", "new", "free"]


def _cents(amount_usd: str) -> int:
    return int(Decimal(amount_usd) * 100)


class MoneyParts(BaseModel):
    """A bot's slice, shaded. Widths are basis points of the slice itself."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    in_shares_usd: str
    in_shares_bps: int = Field(ge=0, le=FULL_BAR_BPS)
    pending_usd: str
    pending_bps: int = Field(ge=0, le=FULL_BAR_BPS)
    free_usd: str
    free_bps: int = Field(ge=0, le=FULL_BAR_BPS)

    @model_validator(mode="after")
    def widths_fill_the_slice(self) -> MoneyParts:
        if self.in_shares_bps + self.pending_bps + self.free_bps != FULL_BAR_BPS:
            raise ValueError("a slice's part widths must sum to 10000 basis points")
        return self


class MoneySegment(BaseModel):
    """One place the account's money is, in display order on the money bar.

    ``label`` is the legend's words for the slice; ``share_bps`` its width.
    ``parts`` and ``shortfall_usd`` belong to a running bot; ``released_usd``
    and ``still_claimed_usd`` to a stopped bot that still holds money.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: MoneySegmentKind
    strategy_instance_id: str | None = None
    label: str
    amount_usd: str
    share_bps: int = Field(ge=0, le=FULL_BAR_BPS)
    parts: MoneyParts | None = None
    shortfall_usd: str | None = None
    released_usd: str | None = None
    still_claimed_usd: str | None = None

    @model_validator(mode="after")
    def carries_its_kind_fields(self) -> MoneySegment:
        bot, stopped = self.kind == "bot", self.kind == "stopped"
        if (self.strategy_instance_id is not None) != (bot or stopped):
            raise ValueError("only a bot or stopped slice names a bot")
        if (self.parts is not None, self.shortfall_usd is not None) != (bot, bot):
            raise ValueError("parts and shortfall_usd belong to a running bot's slice")
        if (self.released_usd is not None, self.still_claimed_usd is not None) != (stopped, stopped):
            raise ValueError("released_usd and still_claimed_usd belong to a stopped bot's slice")
        return self


class AccountMoneyView(BaseModel):
    """Where one account's money is: the single read every money bar draws.

    Python authors every dollar string and every width. When ``ready``, the
    segments sum exactly (in cents) to ``total_usd``, their widths to 10000
    basis points, and ``free_to_deploy_usd`` is the Deploy preview's
    unreserved cash. Otherwise every figure is absent and ``detail`` names
    the reason and its fix -- an unknown is never shown as $0.
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
    # Notes beside the bar, never slices of it: Alpaca's equity less the
    # bar's total, the equity itself and today's account P&L.
    open_pnl_usd: str | None = None
    equity_usd: str | None = None
    today_pnl_usd: str | None = None
    segments: tuple[MoneySegment, ...] = ()

    @model_validator(mode="after")
    def conserves_every_dollar(self) -> AccountMoneyView:
        figures = (self.total_usd, self.cash_usd, self.free_to_deploy_usd, self.in_bots_usd,
                   self.held_by_stopped_usd, self.outside_bots_usd, self.account_charges_usd)
        if self.state != "ready":
            if self.segments or any(value is not None for value in (*figures, self.open_pnl_usd, self.equity_usd, self.today_pnl_usd)):
                raise ValueError("an unavailable money read carries a reason, never figures")
            return self
        total = self.total_usd
        if total is None or any(value is None for value in figures) or self.observed_at_ms is None or not self.segments:
            raise ValueError("a ready money read carries every headline figure and its segments")
        if sum(_cents(segment.amount_usd) for segment in self.segments) != _cents(total):
            raise ValueError("the money bar's segments must sum exactly to total_usd")
        if sum(segment.share_bps for segment in self.segments) != FULL_BAR_BPS:
            raise ValueError("the money bar's widths must sum to 10000 basis points")
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
    # The account's money bar with this Deploy drawn in: the proposed budget
    # is a ``new`` slice carved out of free to deploy (no amount yet: today's
    # bar). ``None`` when the preview itself is not ready, and for Dry Run,
    # which never uses the account's money.
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
    # When the Deploy was first claimed; ``None`` for a bot deployed before
    # names were backend-authored, which has no submission record.
    first_deployed_at_ms: EpochMs | None = None
    # Deploy again's display-only lineage: the bot this one follows.
    replaces_strategy_instance_id: str | None = None


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
