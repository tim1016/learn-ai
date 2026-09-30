"""The account-wide day-P&L fact the loss hold judges (ADR 0059 D4).

Formula: ``day_pnl = current_equity − prior_close_equity
  − Σ cash deposits and withdrawals after that prior close``.
Reference: ADR 0059 Decision 4, amended 2026-09-24; Alpaca Account Object
  (``last_equity`` is equity at the previous trading day's 16:00 ET close),
  https://docs.alpaca.markets/us/v1.1/docs/account-plans; Alpaca Account
  Activities (``CSD`` deposit, ``CSW`` withdrawal, signed ``net_amount``),
  https://docs.alpaca.markets/us/docs/account-activities.
Canonical implementation: this file.
Validated against: ``tests/broker/alpaca/clerk/sqlite/test_day_pnl.py``,
  ``tests/broker/alpaca/clerk/sqlite/test_live_envelope_sync.py`` and, for
  simulated custody's exact figure, the ``Fraction`` oracle in
  ``tests/broker/alpaca/clerk/sqlite/test_simulated_account.py``.

The broker's equity makes this account-wide without composing incompatible
lot horizons: an overnight position contributes only its change since the
prior regular-session close, while an external order is already reflected in
the same account value. ``TRANS`` is Alpaca's aggregate activity filter for
the two owner cash-flow types; deposits must be strictly positive and
withdrawals strictly negative. Anything unexpected or without a finite,
direction-consistent signed amount makes the fact unknown rather than silently
treating cash flow as P&L.

Two figures, one formula. The loss hold compares and seals ``total_usd``,
float arithmetic in every custody world. What the owner reads is
``display_total_usd``, the same formula in exact ``Decimal`` for every account
(#2586): each recorded figure -- a broker float, simulated custody's exact
equity or baseline, one transfer amount -- is normalized on its own, so no
float subtraction or sum ever chooses the cent the owner sees.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal

from app.broker.alpaca.clerk.live_envelope import AccountObservation
from app.broker.alpaca.clerk.money import ZERO, money_context, normalize_money
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.models import BrokerActivity
from app.lean_sidecar.trading_calendar import previous_completed_session_close_ms
from app.utils.session_anchors import et_date_at_ms, et_midnight_ms

CASH_TRANSFER_ACTIVITY_FILTER = "TRANS"
CASH_TRANSFER_ACTIVITY_TYPES: frozenset[str] = frozenset({"CSD", "CSW"})


@dataclass(frozen=True)
class DayPnl:
    day_start_ms: int
    # The observation instant; the figures below span
    # (day_start_ms, day_end_ms].
    day_end_ms: int
    # Each is a broker float or simulated custody's exact ``Decimal``, in any
    # mix: a sealed loss hold's float baseline stands in on the clearance path.
    current_equity_usd: float | Decimal
    prior_close_equity_usd: float | Decimal
    # Each usable deposit (positive) and withdrawal (negative), as recorded.
    cash_flow_amounts_usd: tuple[float, ...]
    cash_flow_count: int
    cash_flows_known: bool

    @property
    def net_cash_flow_usd(self) -> float:
        return sum(self.cash_flow_amounts_usd)

    @property
    def total_usd(self) -> float:
        """The figure the loss hold compares and seals: float arithmetic.

        An exact simulated equity enters as its nearest float, so one loss
        rule judges every custody world, byte for byte as before #2586.
        """
        return (
            float(self.current_equity_usd)
            - float(self.prior_close_equity_usd)
            - self.net_cash_flow_usd
        )

    @property
    def display_total_usd(self) -> Decimal:
        """Today's P&L as the owner reads it: exact, rounded once by the caller.

        Every recorded figure is normalized on its own and the formula runs
        in exact ``Decimal``, for every account and every mix of float and
        exact inputs (#2586). Never a loss-hold input.
        """
        with money_context():
            return (
                normalize_money(self.current_equity_usd)
                - normalize_money(self.prior_close_equity_usd)
                - sum((normalize_money(amount) for amount in self.cash_flow_amounts_usd), ZERO)
            )

    @property
    def known(self) -> bool:
        return self.cash_flows_known and all(math.isfinite(v) for v in (self.current_equity_usd, self.prior_close_equity_usd, self.net_cash_flow_usd, self.total_usd))


def day_pnl_at(
    *,
    observation: AccountObservation,
    cash_flows: Sequence[BrokerActivity],
    now_ms: int,
    cash_flow_evidence_complete: bool = True,
    retained_start_ms: int | None = None,
) -> DayPnl:
    if observation.last_equity_usd is None:
        raise ValueError("day P&L needs the broker's prior-close equity")
    if observation.equity_usd is None:
        raise ValueError("day P&L needs current account equity")
    day_start_ms = retained_start_ms if retained_start_ms is not None else (observation.simulation_session_start_ms if observation.simulation_session_start_ms is not None else day_pnl_window_start_ms(now_ms))
    usable_amounts = [
        activity.net_amount
        for activity in cash_flows
        if activity.activity_type in CASH_TRANSFER_ACTIVITY_TYPES
        and activity.net_amount is not None
        and math.isfinite(activity.net_amount)
        and (
            (activity.activity_type == "CSD" and activity.net_amount > 0.0)
            or (activity.activity_type == "CSW" and activity.net_amount < 0.0)
        )
        and activity.occurred_at_ms is not None
        and day_start_ms < activity.occurred_at_ms <= now_ms
    ]
    cash_flows_known = cash_flow_evidence_complete and len(usable_amounts) == len(cash_flows)
    return DayPnl(
        day_start_ms=day_start_ms,
        day_end_ms=now_ms,
        current_equity_usd=observation.equity_usd,
        prior_close_equity_usd=observation.last_equity_usd,
        cash_flow_amounts_usd=tuple(usable_amounts),
        cash_flow_count=len(cash_flows),
        cash_flows_known=cash_flows_known,
    )


def day_pnl_window_start_ms(now_ms: int) -> int:
    """Return the prior trading day's close backing Alpaca ``last_equity``.

    Anchor the search at the current ET calendar day's midnight rather than
    ``now_ms``. Otherwise the boundary advances to today's close during the
    after-hours window while ``last_equity`` still describes the prior trading
    day, splitting one P&L fact across two different horizons.
    """
    current_et_day_start_ms = et_midnight_ms(et_date_at_ms(now_ms))
    return previous_completed_session_close_ms(current_et_day_start_ms)


__all__ = [
    "CASH_TRANSFER_ACTIVITY_FILTER",
    "CASH_TRANSFER_ACTIVITY_TYPES",
    "DayPnl",
    "day_pnl_at",
    "day_pnl_window_start_ms",
]


def risk_fill_sequence(repo: ClerkSqliteRepository) -> int:
    """Executions invalidate observations before another commitment."""
    with repo._write_lock:
        return int(repo._conn.execute("SELECT COALESCE(MAX(recorded_transition_sequence), 0) FROM fills").fetchone()[0])


def reading_covers_executions(repo: ClerkSqliteRepository, observation: AccountObservation) -> bool:
    """Whether ``observation`` was read after every execution the Clerk has recorded.

    The one execution-watermark rule (#2543): a reading taken before an
    execution cannot say what that execution did to the account, so it admits
    no new exposure until a newer reading covers it. Admission, the sync's
    judgement of its own reading and the reading an ENTER waits for (#2623)
    all ask this one question.
    """
    return observation.risk_fill_sequence == risk_fill_sequence(repo)


def risk_evidence_ready(repo: ClerkSqliteRepository, *, now_ms: int) -> bool:
    """Canonical fees/coverage gate spending without subtracting fees from equity twice."""
    return repo.fee_attribution(now_ms=now_ms).known


def observed_day_pnl(*, observation: AccountObservation, now_ms: int, retained_start_ms: int | None = None, retained_equity_usd: float | None = None) -> DayPnl:
    """Read the immutable observation's transfer evidence, never a broker endpoint."""
    start = retained_start_ms if retained_start_ms is not None else (observation.simulation_session_start_ms if observation.simulation_session_start_ms is not None else day_pnl_window_start_ms(now_ms))
    current_start = observation.simulation_session_start_ms if observation.simulation_session_start_ms is not None else day_pnl_window_start_ms(now_ms)
    complete = observation.risk_equity_window_start_ms == current_start and observation.risk_cash_flow_evidence_complete and observation.risk_cash_flow_window_start_ms is not None and observation.risk_cash_flow_window_start_ms <= start
    flows = tuple(flow for flow in observation.risk_cash_flows if flow.occurred_at_ms is None or flow.occurred_at_ms > start)
    observed = observation if retained_equity_usd is None else replace(observation, last_equity_usd=retained_equity_usd)
    return day_pnl_at(observation=observed, cash_flows=flows, now_ms=now_ms, cash_flow_evidence_complete=complete, retained_start_ms=start)
