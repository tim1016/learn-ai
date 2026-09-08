"""Predicted-vs-observed session fee reconciliation (ADR 0059 D6).

Observed FEE activities are the truth; the canonical model predicts them. This
module prices one ET trade date's effective fills with
``app.broker.alpaca.regulatory_fees``, sums the FEE activities Alpaca posted for
that date, and reports the difference with a verdict. Alpaca posts fees at end
of day dated the trade date (ET midnight anchor), so the observation window is
the ET calendar day, not the RTH session — extended-hours fills bill the same day.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from app.broker.alpaca.regulatory_fees import (
    FillFees,
    RateNotPinnedError,
    SessionFees,
    fees_for_fill,
    settle_session,
)
from app.broker.contract.models import BrokerActivity, OrderSide
from app.schemas.alpaca_fee_reconciliation import (
    FeeReconciliationVerdict,
    PredictedSessionFees,
    SessionFeeReconciliation,
)
from app.utils.session_anchors import et_date_at_ms, et_midnight_ms

# Alpaca charges at end of day; give the FEE activity a day to post before
# "no observation" becomes a finding rather than a wait.
FEE_POSTING_GRACE_MS = 24 * 60 * 60 * 1000
_ONE_DAY = timedelta(days=1)
_CENT = Decimal("0.01")
_ZERO = Decimal("0")


@dataclass(frozen=True)
class SessionFill:
    """The three facts about a fill the fee model prices."""

    side: OrderSide
    quantity: Decimal
    fill_price: Decimal


@dataclass(frozen=True)
class _Frame:
    """Everything a verdict shares, computed once per reconciliation."""

    broker: str
    account_id: str | None
    session_open_ms: int
    fill_window_start_ms: int
    fill_window_end_ms: int
    fill_count: int
    sell_fill_count: int
    observed_activity_count: int
    observed_at_ms: int

    def verdict(
        self,
        verdict: FeeReconciliationVerdict,
        why: str,
        *,
        predicted: PredictedSessionFees | None = None,
        observed_total_usd: float | None = None,
        delta_usd: float | None = None,
        tolerance_usd: float | None = None,
        unpinned_components: Sequence[str] = (),
    ) -> SessionFeeReconciliation:
        return SessionFeeReconciliation(
            broker=self.broker,
            account_id=self.account_id,
            session_open_ms=self.session_open_ms,
            fill_window_start_ms=self.fill_window_start_ms,
            fill_window_end_ms=self.fill_window_end_ms,
            fill_count=self.fill_count,
            sell_fill_count=self.sell_fill_count,
            predicted=predicted,
            observed_total_usd=observed_total_usd,
            observed_activity_count=self.observed_activity_count,
            delta_usd=delta_usd,
            tolerance_usd=tolerance_usd,
            verdict=verdict,
            why=why,
            unpinned_components=list(unpinned_components),
            observed_at_ms=self.observed_at_ms,
        )


def _frame(
    *,
    broker: str,
    account_id: str | None,
    session_open_ms: int,
    fills: Sequence[SessionFill],
    fee_rows: Sequence[BrokerActivity],
    now_ms: int,
) -> _Frame:
    trade_date = et_date_at_ms(session_open_ms)
    return _Frame(
        broker=broker,
        account_id=account_id,
        session_open_ms=session_open_ms,
        fill_window_start_ms=et_midnight_ms(trade_date),
        fill_window_end_ms=et_midnight_ms(trade_date + _ONE_DAY),
        fill_count=len(fills),
        sell_fill_count=sum(1 for fill in fills if fill.side == OrderSide.SELL),
        observed_activity_count=len(fee_rows),
        observed_at_ms=now_ms,
    )


def _predicted(settled: SessionFees) -> PredictedSessionFees:
    return PredictedSessionFees(
        sec_usd=float(settled.sec),
        taf_usd=float(settled.taf),
        cat_usd=float(settled.cat),
        total_usd=float(settled.total),
    )


def _observed_total(fee_rows: Sequence[BrokerActivity]) -> Decimal | None:
    """The day's charge as a positive amount, or ``None`` when it cannot be known."""
    if not fee_rows or any(row.net_amount is None for row in fee_rows):
        return None
    return -sum((Decimal(str(row.net_amount)) for row in fee_rows), _ZERO)


def reconcile_session_fees(
    *,
    broker: str,
    account_id: str,
    session_open_ms: int,
    fills: Sequence[SessionFill],
    fee_activities: Sequence[BrokerActivity],
    now_ms: int,
) -> SessionFeeReconciliation:
    """Compare the model's end-of-day charge with the FEE activities dated the trade date."""
    trade_date = et_date_at_ms(session_open_ms)
    fee_rows = [
        activity
        for activity in fee_activities
        if activity.activity_type == "FEE"
        and activity.occurred_at_ms is not None
        and et_date_at_ms(activity.occurred_at_ms) == trade_date
    ]
    frame = _frame(
        broker=broker,
        account_id=account_id,
        session_open_ms=session_open_ms,
        fills=fills,
        fee_rows=fee_rows,
        now_ms=now_ms,
    )
    priced: list[FillFees] = [
        fees_for_fill(
            trade_date=trade_date,
            side=fill.side,
            quantity=fill.quantity,
            fill_price=fill.fill_price,
        )
        for fill in fills
    ]
    observed = _observed_total(fee_rows)
    try:
        settled = settle_session(priced)
    except RateNotPinnedError as exc:
        return frame.verdict(
            "rate_unpinned",
            f"no pinned rate for {', '.join(exc.components)} on this trade date; the model cannot predict this session",
            observed_total_usd=None if observed is None else float(observed),
            unpinned_components=exc.components,
        )
    predicted = _predicted(settled)
    # 3 cents of end-of-day rounding plus, per sell, the extra cent each of SEC and
    # TAF could carry under the per-trade-rounding reading of Alpaca's support page.
    tolerance = _CENT * (3 + 2 * frame.sell_fill_count)
    if observed is None:
        if not fills and not fee_rows:
            return frame.verdict("no_fills", "no fills and no FEE activity on this trade date", predicted=predicted, tolerance_usd=float(tolerance))
        if fee_rows:
            return frame.verdict("unobserved", "a FEE activity for this trade date carries no net_amount; the observed charge is unknown", predicted=predicted, tolerance_usd=float(tolerance))
        if now_ms < frame.fill_window_end_ms + FEE_POSTING_GRACE_MS:
            return frame.verdict("pending", "Alpaca has not posted this trade date's FEE activity yet (charged at end of day)", predicted=predicted, tolerance_usd=float(tolerance))
        return frame.verdict("unobserved", "no FEE activity was posted for this trade date within a day of its end", predicted=predicted, tolerance_usd=float(tolerance))
    delta = observed - settled.total
    within = abs(delta) <= tolerance
    return frame.verdict(
        "within_tolerance" if within else "drift",
        "observed charge agrees with the model within tolerance" if within else "observed charge differs from the model by more than the tolerance",
        predicted=predicted,
        observed_total_usd=float(observed),
        delta_usd=float(delta),
        tolerance_usd=float(tolerance),
    )
