"""Wire shape of one session's predicted-vs-observed fee reconciliation (ADR 0059 D6).

The legacy session comparison uses ``float`` USD. Deployment fee attribution
preserves Decimal USD as strings at the boundary;
every instant is ``int64 ms UTC``. ``session_open_ms`` is the trading date's
ET session-open anchor; the fill window is the ET calendar day around it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.broker.alpaca.clerk.et_day import ActivityPeriod
from app.broker.alpaca.clerk.models import EpochMs

FeeReconciliationVerdict = Literal[
    "within_tolerance",
    "drift",
    "pending",
    "unobserved",
    "no_fills",
    "rate_unpinned",
    "unavailable",
]


class PredictedSessionFees(BaseModel):
    """The model's end-of-day charge for the session, per component."""

    model_config = ConfigDict(frozen=True)

    sec_usd: float = Field(ge=0)
    taf_usd: float = Field(ge=0)
    cat_usd: float = Field(ge=0)
    total_usd: float = Field(ge=0)


class SessionFeeReconciliation(BaseModel):
    """Predicted fees for one ET trade date against the FEE activities Alpaca posted."""

    model_config = ConfigDict(frozen=True)

    broker: str
    account_id: str | None
    session_open_ms: EpochMs
    fill_window_start_ms: EpochMs
    fill_window_end_ms: EpochMs
    fill_count: int = Field(ge=0)
    sell_fill_count: int = Field(ge=0)
    predicted: PredictedSessionFees | None
    observed_total_usd: float | None
    observed_activity_count: int = Field(ge=0)
    delta_usd: float | None
    tolerance_usd: float | None
    verdict: FeeReconciliationVerdict
    why: str
    unpinned_components: list[str]
    observed_at_ms: EpochMs


class DeploymentFeeRow(BaseModel):
    """Server-owned fee totals; decimal USD is preserved on the wire.

    ``label`` names who owns the fees: the bot's own name, or an outside
    order's symbol. ``order_id`` is the broker order an outside row belongs
    to, so two outside rows are never indistinguishable.
    """

    subject_id: str
    strategy_instance_id: str | None
    label: str
    order_id: str | None = None
    estimated_usd: str
    modelled_settled_usd: str
    observed_usd: str
    total_usd: str


class ActivityPeriodStatement(BaseModel):
    """One Activity period's money statement, every amount authored here.

    Covers the orders the bots and this app placed. ``net_usd`` is exactly
    ``realized_usd - fees_usd + open_usd`` in cents. An amount the backend
    cannot know is ``None`` with ``detail`` saying why -- never zero -- and
    ``state`` is ``ready`` only when all four are known.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["ready", "unavailable"]
    detail: str | None
    realized_usd: str | None
    fees_usd: str | None
    open_usd: str | None
    net_usd: str | None


class DeploymentFeeAttribution(BaseModel):
    """Custody fee ownership, over a deployment's lifetime or one Activity period.

    ``period`` and ``period_start_ms`` echo the account read's Activity
    period (the ET midnight it opens at); ``statement`` is that period's
    money statement. All three are ``None`` on a lifetime read.
    """

    account_id: str | None
    observed_at_ms: EpochMs
    authority_revision: int | None
    available: bool
    known: bool
    rows: list[DeploymentFeeRow]
    account_unattributed_usd: str | None
    messages: list[str]
    period: ActivityPeriod | None = None
    period_start_ms: EpochMs | None = None
    statement: ActivityPeriodStatement | None = None
