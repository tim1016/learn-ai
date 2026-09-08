"""Predicted-vs-observed session fee reconciliation (ADR 0059 D6).

Observed FEE activities are the truth; the canonical model predicts them. This
module prices one ET trade date's effective fills with
``app.broker.alpaca.regulatory_fees``, sums the FEE activities Alpaca posted for
that date, and reports the difference with a verdict. Alpaca posts fees at end
of day dated the trade date (ET midnight anchor), so the observation window is
the ET calendar day, not the RTH session — extended-hours fills bill the same day.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from app.broker.alpaca.clerk.sqlite.economic_projection import (
    EconomicProjectionError,
    SqliteEconomicProjectionReader,
)
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.regulatory_fees import (
    FillFees,
    RateNotPinnedError,
    SessionFees,
    fees_for_fill,
    settle_session,
)
from app.broker.contract.models import BrokerActivity, OrderSide
from app.broker.contract.ports import BrokerReadPort
from app.schemas.alpaca_fee_reconciliation import (
    FeeReconciliationVerdict,
    PredictedSessionFees,
    SessionFeeReconciliation,
)
from app.services.sqlite_clerk_compat import active_sqlite_facade
from app.utils.session_anchors import et_date_at_ms, et_midnight_ms
from app.utils.timestamps import now_ms_utc

# Alpaca charges at end of day; give the FEE activity a day to post before
# "no observation" becomes a finding rather than a wait.
FEE_POSTING_GRACE_MS = 24 * 60 * 60 * 1000
_ONE_DAY = timedelta(days=1)
_CENT = Decimal("0.01")
_ZERO = Decimal("0")
# Per-trade rounding can only push the observed charge UP (Σ ceil(aᵢ) ≥ ceil(Σ aᵢ)),
# so only end-of-day rounding — 3 components, one cent each — explains an
# observation BELOW the model.
_BELOW_MODEL_BAND = Decimal("-0.03")
_FEE_ACTIVITY_TYPE = "FEE"
_INCOMPLETE_READ_WHY = (
    "the broker activity read did not reach the start of this trade date, so the "
    "day's FEE rows cannot be shown to be complete; a young account with no "
    "earlier activity also lands here"
)


@dataclass(frozen=True)
class SessionFill:
    """The three facts about a fill the fee model prices."""

    side: OrderSide
    quantity: Decimal
    fill_price: Decimal


def _et_day_window_ms(session_open_ms: int) -> tuple[int, int]:
    """The trade date's ET calendar day as ``[ET midnight, next ET midnight)``."""
    trade_date = et_date_at_ms(session_open_ms)
    return et_midnight_ms(trade_date), et_midnight_ms(trade_date + _ONE_DAY)


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
    window_start_ms, window_end_ms = _et_day_window_ms(session_open_ms)
    return _Frame(
        broker=broker,
        account_id=account_id,
        session_open_ms=session_open_ms,
        fill_window_start_ms=window_start_ms,
        fill_window_end_ms=window_end_ms,
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


def _unobserved_reason(
    *,
    has_fills: bool,
    has_fee_rows: bool,
    covered: bool,
    now_ms: int,
    window_end_ms: int,
) -> tuple[FeeReconciliationVerdict, str]:
    """Why the day's charge is unknown, when no observed total could be summed.

    ``covered`` gates every claim below it, not just a comparison: an incomplete
    read cannot prove "no fills posted" (``no_fills``) any more than it can prove
    "not posted yet" (``pending``) — both require the read to have demonstrably
    reached past the window start.
    """
    if not covered and not has_fee_rows:
        return "unobserved", _INCOMPLETE_READ_WHY
    if not has_fills and not has_fee_rows:
        return "no_fills", "no fills and no FEE activity on this trade date"
    if has_fee_rows:
        return (
            "unobserved",
            "a FEE activity for this trade date carries no net_amount; "
            "the observed charge is unknown",
        )
    if now_ms < window_end_ms + FEE_POSTING_GRACE_MS:
        return (
            "pending",
            "Alpaca has not posted this trade date's FEE activity yet "
            "(charged at end of day)",
        )
    return (
        "unobserved",
        "no FEE activity was posted for this trade date within a day of its end",
    )


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
        if activity.activity_type == _FEE_ACTIVITY_TYPE
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
    # The broker's activity read is newest-first and bounded, so an in-window FEE
    # set is only demonstrably complete when the read also returned something
    # older than the window. Without that, the day's rows may be truncated and no
    # claim below — a comparison, "no fills posted", or "not posted yet" — may be
    # published with confidence. Hoisted above the try-block: every arm below,
    # including the unpredictable-session one, gates its claim on it.
    covered = any(
        activity.occurred_at_ms is not None
        and activity.occurred_at_ms < frame.fill_window_start_ms
        for activity in fee_activities
    )
    try:
        settled = settle_session(priced)
    except RateNotPinnedError as exc:
        why = (
            f"no pinned rate for {', '.join(exc.components)} on this trade date; "
            "the model cannot predict this session"
        )
        if not covered:
            why = f"{why}; {_INCOMPLETE_READ_WHY}"
        return frame.verdict(
            "rate_unpinned",
            why,
            observed_total_usd=float(observed) if covered and observed is not None else None,
            unpinned_components=exc.components,
        )
    predicted = _predicted(settled)
    # 3 cents of end-of-day rounding plus, per sell, the extra cent each of SEC and
    # TAF could carry under the per-trade-rounding reading of Alpaca's support page.
    tolerance = _CENT * (3 + 2 * frame.sell_fill_count)
    if observed is not None and covered:
        delta = observed - settled.total
        within = _BELOW_MODEL_BAND <= delta <= tolerance
        why = (
            "observed charge agrees with the model within tolerance"
            if within
            else "observed charge differs from the model by more than the tolerance"
        )
        if tolerance >= settled.total:
            why = (
                f"{why}; the tolerance band (${tolerance:.2f}) is at least the predicted "
                f"charge (${settled.total:.2f}), so agreement here proves little; validate "
                "on low-sell-count sessions"
            )
        if not within:
            why = (
                f"{why}; fills placed outside the Clerk (external orders) are not priced "
                "and would also show as drift"
            )
        return frame.verdict(
            "within_tolerance" if within else "drift",
            why,
            predicted=predicted,
            observed_total_usd=float(observed),
            delta_usd=float(delta),
            tolerance_usd=float(tolerance),
        )
    verdict, why = (
        ("unobserved", _INCOMPLETE_READ_WHY)
        if observed is not None
        else _unobserved_reason(
            has_fills=bool(fills),
            has_fee_rows=bool(fee_rows),
            covered=covered,
            now_ms=now_ms,
            window_end_ms=frame.fill_window_end_ms,
        )
    )
    return frame.verdict(verdict, why, predicted=predicted, tolerance_usd=float(tolerance))


def _read_session_fills(
    clerk: SqliteAlpacaClerkFacade,
    *,
    from_ms: int,
    to_ms: int,
) -> list[SessionFill]:
    """Read the ET day's effective account fills. Blocking SQLite: call in a thread."""
    reader = SqliteEconomicProjectionReader.from_repository(clerk.repository)
    try:
        records = reader.account_fill_window(from_ms=from_ms, to_ms=to_ms)
    finally:
        reader.close()
    return [
        SessionFill(
            side=record.side,
            quantity=Decimal(str(record.quantity)),
            fill_price=Decimal(str(record.fill_price)),
        )
        for record in records
    ]


async def session_fee_reconciliation(
    *,
    broker: str,
    port: BrokerReadPort,
    session_open_ms: int,
    now_ms: int | None = None,
) -> SessionFeeReconciliation:
    """Reconcile one trade date: SQLite fills priced by the model vs Alpaca's FEE rows."""
    observed_at_ms = now_ms_utc() if now_ms is None else now_ms
    clerk = active_sqlite_facade("alpaca")
    if clerk is None:
        frame = _frame(
            broker=broker,
            account_id=None,
            session_open_ms=session_open_ms,
            fills=(),
            fee_rows=(),
            now_ms=observed_at_ms,
        )
        return frame.verdict(
            "unavailable",
            "no active SQLite Clerk authority; the session's fills cannot be read",
        )
    window_start_ms, window_end_ms = _et_day_window_ms(session_open_ms)
    try:
        fills = await asyncio.to_thread(
            _read_session_fills,
            clerk,
            from_ms=window_start_ms,
            to_ms=window_end_ms,
        )
    except EconomicProjectionError as exc:
        frame = _frame(
            broker=broker,
            account_id=clerk.account_id,
            session_open_ms=session_open_ms,
            fills=(),
            fee_rows=(),
            now_ms=observed_at_ms,
        )
        return frame.verdict(
            "unavailable",
            f"the session's fills could not be read from the SQLite Clerk: {exc}",
        )
    # Read from the epoch, not from the window start: the port's own filter drops
    # everything older than ``after_ms``, and the completeness check above needs
    # to see whether the bounded read reached past this day's start.
    activities = await port.list_activities(after_ms=0)
    return reconcile_session_fees(
        broker=broker,
        account_id=clerk.account_id,
        session_open_ms=session_open_ms,
        fills=fills,
        fee_activities=activities,
        now_ms=observed_at_ms,
    )
