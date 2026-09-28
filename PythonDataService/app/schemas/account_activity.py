"""Wire shapes for an account's Activity page (PRD #2560).

Two reads beside the period's fees: the period's orders and cash moves, as far
as one bounded broker read reached, and Today's money statement. Every amount
is a decimal USD string authored here; the browser adds nothing up.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.broker.alpaca.clerk.et_day import ActivityPeriod
from app.broker.alpaca.clerk.models import EpochMs
from app.broker.contract.models import BrokerActivityEvidence


class ActivityPeriodRead(BaseModel):
    """One Activity period's orders and cash moves, newest first.

    ``evidence`` is one bounded broker read of the window that opens at
    ``period_start_ms``. When ``evidence.history_complete`` is false the
    period holds older rows this read did not reach; ``next_page_token``
    reads the next older stretch, and the page must say the list is partial.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    period: ActivityPeriod
    period_start_ms: EpochMs
    observed_at_ms: EpochMs
    evidence: BrokerActivityEvidence


class TodayStatement(BaseModel):
    """Today's money for the bots and this app's orders, since the last close.

    ``since_ms`` is the prior regular-session close the figures run from --
    the same close the account's previous-close equity describes. Realized
    gains and the change in open gains span ``(since_ms, observed_at_ms]``;
    fees are those billed on today's ET date. ``net_usd`` is exactly
    ``realized_usd - fees_usd + open_change_usd`` in cents. Orders placed
    outside the bots are not included, so this is never the account's day
    P&L. An amount that cannot be known is ``None`` with ``detail`` saying
    why -- never zero -- and ``state`` is ``ready`` only when all four are
    known.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["ready", "unavailable"]
    detail: str | None
    since_ms: EpochMs | None
    observed_at_ms: EpochMs
    realized_usd: str | None
    fees_usd: str | None
    open_change_usd: str | None
    net_usd: str | None


__all__ = ["ActivityPeriodRead", "TodayStatement"]
