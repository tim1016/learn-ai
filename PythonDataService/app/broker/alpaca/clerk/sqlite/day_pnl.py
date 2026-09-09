"""The account-wide day-P&L fact the loss hold judges (ADR 0059 D4).

Formula: ``day_pnl = Σ realized FIFO closed-lot P&L in [ET midnight, now]
  − Σ reported fees on those fills + Σ broker-observed unrealized_pl``.
Reference: ADR 0059 Decision 4; ``CONTEXT.md`` § Day P&L.
Canonical implementation: this file, over ``fifo_pnl``'s FIFO through
  ``SqliteEconomicProjectionReader.account_pnl_attribution``.
Validated against: ``tests/broker/alpaca/clerk/sqlite/test_day_pnl.py``.

The fact is *unknown*, never zero, when an external order was observed
today: its realized P&L is not journaled (plan R5). Unrealized P&L is the
broker's own figure per position, so no marks are needed here.

``execution_coverage`` is the projection's own verdict on the evidence the
realized number rests on; it is carried for consumers (the sync's log line,
the operator) and does not make the fact unknown — only an external order
observed today does (plan R5).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.broker.alpaca.clerk.et_day import et_day_window_ms
from app.broker.alpaca.clerk.live_envelope import AccountObservation
from app.broker.alpaca.clerk.sqlite.economic_projection import SqliteEconomicProjectionReader
from app.broker.alpaca.clerk.sqlite.economic_projection_models import ExecutionCoverage
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository


@dataclass(frozen=True)
class DayPnl:
    day_start_ms: int
    # The ET day's end for the window label; the figures below span
    # [day_start_ms, now_ms], not [day_start_ms, day_end_ms].
    day_end_ms: int
    realized_usd: float
    fee_usd: float
    fee_fidelity: Literal["reported", "not_reported"]
    execution_coverage: ExecutionCoverage
    unrealized_usd: float
    external_orders_today: int

    @property
    def total_usd(self) -> float:
        return self.realized_usd - self.fee_usd + self.unrealized_usd

    @property
    def known(self) -> bool:
        return self.external_orders_today == 0


def day_pnl_at(
    reader: SqliteEconomicProjectionReader,
    repo: ClerkSqliteRepository,
    *,
    observation: AccountObservation,
    now_ms: int,
) -> DayPnl:
    day_start_ms, day_end_ms = et_day_window_ms(now_ms)
    attribution = reader.account_pnl_attribution(from_ms=day_start_ms, to_ms=now_ms)
    return DayPnl(
        day_start_ms=day_start_ms,
        day_end_ms=day_end_ms,
        realized_usd=attribution.realized_pnl_total,
        fee_usd=attribution.fee_total if attribution.fee_total is not None else 0.0,
        fee_fidelity=attribution.fee_fidelity,
        execution_coverage=attribution.execution_coverage,
        unrealized_usd=observation.unrealized_pl_usd,
        external_orders_today=repo.external_orders_observed_since(since_ms=day_start_ms),
    )


__all__ = ["DayPnl", "day_pnl_at"]
