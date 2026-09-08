"""Wire shape of one session's predicted-vs-observed fee reconciliation (ADR 0059 D6).

Money crosses this boundary as ``float`` USD (the model is ``Decimal`` inside);
every instant is ``int64 ms UTC``. ``session_open_ms`` is the trading date's
ET session-open anchor; the fill window is the ET calendar day around it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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
