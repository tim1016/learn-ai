"""Account loss evidence: FIFO gross P&L minus canonical dated fees.

Formula: ``day_pnl = Σ realized FIFO closed-lot P&L in [ET midnight, now]
  − canonical observed/estimated/modelled fees + broker-observed unrealized_pl``.
Reference: PRD #2540, ADR 0060's effective-risk amendment and
  ``docs/references/alpaca-live-envelope.md``.
Canonical implementation: this file, consuming ``fifo_pnl`` and the sole
  ``custody_fee_attribution`` projection in one custody writer snapshot.
Validated against: ``tests/broker/alpaca/clerk/sqlite/test_day_pnl.py``.

Unknown fees, incomplete execution coverage, nonfinite figures or active
unjournaled external orders make the whole fact unknown, never zero. A retained
hold uses the same authority from its original session start; no fee or realized
loss disappears simply because the calendar rolled over. Unrealized P&L is the
broker's observed figure; missing/nonfinite position marks fail upstream.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from app.broker.alpaca.clerk.et_day import et_day_window_ms
from app.broker.alpaca.clerk.live_envelope import AccountObservation
from app.broker.alpaca.clerk.money import money_context
from app.broker.alpaca.clerk.sqlite.economic_projection import SqliteEconomicProjectionReader
from app.broker.alpaca.clerk.sqlite.economic_projection_models import ExecutionCoverage
from app.broker.alpaca.clerk.sqlite.external_orders import unfoldable_broker_orders_active_since
from app.broker.alpaca.clerk.sqlite.fee_evidence import custody_fee_attribution
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository


@dataclass(frozen=True)
class DayPnl:
    day_start_ms: int
    # The ET day's end for the window label; the figures below span
    # [day_start_ms, now_ms], not [day_start_ms, day_end_ms].
    day_end_ms: int
    realized_usd: float
    fee_usd: float | None
    fee_fidelity: Literal["observed", "estimated", "modelled_settled", "unknown"]
    execution_coverage: ExecutionCoverage
    unrealized_usd: float
    external_orders_today: int
    unfoldable_orders_today: int

    @property
    def total_usd(self) -> float | None:
        return self.realized_usd - self.fee_usd + self.unrealized_usd if self.known else None

    @property
    def known(self) -> bool:
        return (
            self.external_orders_today == 0
            and self.unfoldable_orders_today == 0
            and self.execution_coverage == "complete"
            and self.fee_usd is not None
            and all(math.isfinite(value) for value in (self.realized_usd, self.fee_usd, self.unrealized_usd))
            and math.isfinite(self.realized_usd - self.fee_usd + self.unrealized_usd)
        )



def risk_fill_sequence(repo: ClerkSqliteRepository) -> int:
    """Current execution watermark, including corrections and coverage recovery."""
    with repo._write_lock:
        return int(repo._conn.execute("SELECT COALESCE(MAX(recorded_transition_sequence), 0) FROM fills").fetchone()[0])


@money_context()
def day_pnl_at(
    reader: SqliteEconomicProjectionReader,
    repo: ClerkSqliteRepository,
    *,
    observation: AccountObservation,
    now_ms: int,
    retained_start_ms: int | None = None,
) -> DayPnl:
    day_start_ms, day_end_ms = et_day_window_ms(now_ms)
    if retained_start_ms is not None:
        day_start_ms = retained_start_ms
    with repo._write_lock:
        attribution = reader.account_pnl_attribution(from_ms=day_start_ms, to_ms=now_ms)
        # FIFO includes now; the fee projection uses a half-open date window.
        fees = custody_fee_attribution(repo._conn, now_ms=now_ms, from_ms=day_start_ms, to_ms=now_ms + 1)
        states = {share.state for share in fees.shares}
        fee_fidelity = (
            "unknown" if not fees.known else
            "estimated" if "estimated" in states else
            "modelled_settled" if "modelled_settled" in states else "observed"
        )
        return DayPnl(
            day_start_ms=day_start_ms,
            day_end_ms=day_end_ms,
            realized_usd=attribution.realized_pnl_total,
            fee_usd=float(sum((share.amount for share in fees.shares), Decimal(0))) if fees.known else None,
            fee_fidelity=fee_fidelity,
            execution_coverage=attribution.execution_coverage,
            unrealized_usd=observation.unrealized_pl_usd,
            external_orders_today=repo.external_orders_observed_since(since_ms=day_start_ms),
            unfoldable_orders_today=unfoldable_broker_orders_active_since(repo, since_ms=day_start_ms),
        )


__all__ = ["DayPnl", "day_pnl_at", "risk_fill_sequence"]
