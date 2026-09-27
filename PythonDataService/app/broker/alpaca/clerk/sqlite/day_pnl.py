"""The account-wide day-P&L fact the loss hold judges (ADR 0059 D4).

Formula: ``day_pnl = current_equity − prior_close_equity
  − Σ today's cash deposits and withdrawals``.
Reference: ADR 0059 Decision 4, amended 2026-09-24; Alpaca Account Object
  (``last_equity`` is equity at the previous trading day's 16:00 ET close),
  https://docs.alpaca.markets/us/v1.1/docs/account-plans; Alpaca Account
  Activities (``CSD`` deposit, ``CSW`` withdrawal, signed ``net_amount``),
  https://docs.alpaca.markets/us/docs/account-activities.
Canonical implementation: this file.
Validated against: ``tests/broker/alpaca/clerk/sqlite/test_day_pnl.py`` and
  ``tests/broker/alpaca/clerk/sqlite/test_live_envelope_sync.py``.

The broker's equity makes this account-wide without composing incompatible
lot horizons: an overnight position contributes only its change since the
prior regular-session close, while an external order is already reflected in
the same account value. ``TRANS`` is Alpaca's aggregate activity filter for
the two owner cash-flow types; anything unexpected or without a finite signed
amount makes the fact unknown rather than silently treating cash flow as P&L.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from app.broker.alpaca.clerk.et_day import et_day_window_ms
from app.broker.alpaca.clerk.live_envelope import AccountObservation
from app.broker.contract.models import BrokerActivity

CASH_TRANSFER_ACTIVITY_FILTER = "TRANS"
CASH_TRANSFER_ACTIVITY_TYPES: frozenset[str] = frozenset({"CSD", "CSW"})


@dataclass(frozen=True)
class DayPnl:
    day_start_ms: int
    # The ET day's end for the window label; the figures below span
    # [day_start_ms, now_ms], not [day_start_ms, day_end_ms].
    day_end_ms: int
    current_equity_usd: float
    prior_close_equity_usd: float
    net_cash_flow_usd: float
    cash_flow_count: int
    cash_flows_known: bool

    @property
    def total_usd(self) -> float:
        return (
            self.current_equity_usd
            - self.prior_close_equity_usd
            - self.net_cash_flow_usd
        )

    @property
    def known(self) -> bool:
        return self.cash_flows_known


def day_pnl_at(
    *,
    observation: AccountObservation,
    cash_flows: Sequence[BrokerActivity],
    now_ms: int,
) -> DayPnl:
    if observation.last_equity_usd is None:
        raise ValueError("day P&L needs the broker's prior-close equity")
    day_start_ms, day_end_ms = et_day_window_ms(now_ms)
    usable_amounts = [
        activity.net_amount
        for activity in cash_flows
        if activity.activity_type in CASH_TRANSFER_ACTIVITY_TYPES
        and activity.net_amount is not None
        and math.isfinite(activity.net_amount)
    ]
    cash_flows_known = len(usable_amounts) == len(cash_flows)
    return DayPnl(
        day_start_ms=day_start_ms,
        day_end_ms=day_end_ms,
        current_equity_usd=observation.equity_usd,
        prior_close_equity_usd=observation.last_equity_usd,
        net_cash_flow_usd=sum(usable_amounts),
        cash_flow_count=len(cash_flows),
        cash_flows_known=cash_flows_known,
    )


__all__ = [
    "CASH_TRANSFER_ACTIVITY_FILTER",
    "CASH_TRANSFER_ACTIVITY_TYPES",
    "DayPnl",
    "day_pnl_at",
]
