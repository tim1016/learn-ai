"""Wire shape of deployment fee attribution.

Deployment fee attribution preserves Decimal USD as strings at the boundary;
every instant is ``int64 ms UTC``.
"""

from __future__ import annotations

from pydantic import BaseModel

from app.broker.alpaca.clerk.et_day import ActivityPeriod
from app.broker.alpaca.clerk.models import EpochMs


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


class DeploymentFeeAttribution(BaseModel):
    """Custody fee ownership, over a deployment's lifetime or one Activity period.

    ``period`` and ``period_start_ms`` echo the account read's Activity
    period (the ET midnight of its first fee day); both are ``None`` on a
    lifetime read.
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
