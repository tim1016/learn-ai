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
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, Inexact

from app.broker.alpaca.clerk.et_day import ActivityPeriod, activity_period_start_ms, et_day_window_ms
from app.broker.alpaca.clerk.money import dollars, money_context, normalize_money
from app.broker.alpaca.clerk.sqlite.custody_subjects import OUTSIDE_ORDER_SUBJECT_PREFIX
from app.broker.alpaca.clerk.sqlite.economic_projection import (
    EconomicProjectionError,
    MarketMark,
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
from app.broker.contract.errors import BrokerError
from app.broker.contract.models import BrokerActivity, OrderSide
from app.broker.contract.ports import BrokerReadPort
from app.schemas.alpaca_fee_reconciliation import (
    ActivityPeriodStatement,
    DeploymentFeeAttribution,
    DeploymentFeeRow,
    FeeReconciliationVerdict,
    PredictedSessionFees,
    SessionFeeReconciliation,
)
from app.services.alpaca_fee_attribution import FeeAttribution, collapse_activity_deliveries
from app.services.sqlite_clerk_compat import active_sqlite_facade
from app.utils.session_anchors import et_date_at_ms
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

# Alpaca charges at end of day; give the FEE activity a day to post before
# "no observation" becomes a finding rather than a wait.
FEE_POSTING_GRACE_MS = 24 * 60 * 60 * 1000
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
    window_start_ms, window_end_ms = et_day_window_ms(session_open_ms)
    return _Frame(
        broker=broker,
        account_id=account_id,
        session_open_ms=session_open_ms,
        fill_window_start_ms=window_start_ms,
        fill_window_end_ms=window_end_ms,
        fill_count=len(fills),
        sell_fill_count=sum(1 for fill in fills if fill.side == OrderSide.SELL),
        observed_activity_count=len({row.activity_id for row in fee_rows}),
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
    # Duplicate delivery is one charge; conflicting copies fail closed.
    collapsed = collapse_activity_deliveries(fee_rows)
    if collapsed.conflicts:
        return None
    return -sum((normalize_money(row.net_amount) for row in collapsed.unique.values()), _ZERO)


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
    window_start_ms, window_end_ms = et_day_window_ms(session_open_ms)
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


async def deployment_fee_attribution(
    strategy_instance_id: str | None = None,
    *,
    period: ActivityPeriod | None = None,
    port: BrokerReadPort | None = None,
) -> DeploymentFeeAttribution:
    """Account/deployment UI uses exactly the custody fee projection.

    ``period`` narrows the account read to one Activity period's fee days and
    adds that period's statement, its open shares valued at ``port``'s
    current positions.
    """
    if strategy_instance_id is not None:
        from app.services.bot_runner import get_bot_task_registry
        from app.services.broker_v2_panel.panel_data_source import _panel_authority_for_binding

        registry = get_bot_task_registry()
        if registry is not None:
            binding = registry.binding_for_control("alpaca", strategy_instance_id)
            async with _panel_authority_for_binding(registry, binding) as selected:
                if selected is not None:
                    return await asyncio.to_thread(_deployment_fee_view, selected)
    clerk = active_sqlite_facade("alpaca") if strategy_instance_id is None else None
    if clerk is None:
        return DeploymentFeeAttribution(account_id=None, observed_at_ms=now_ms_utc(), authority_revision=None,
            available=False, known=False, rows=[], account_unattributed_usd=None,
            messages=["Fee evidence is unavailable while the account Clerk is offline."], period=period)
    if period is None:
        return await asyncio.to_thread(_deployment_fee_view, clerk)
    marks = await _current_marks(port)
    return await asyncio.to_thread(_period_fee_view, clerk, period, marks)


async def _current_marks(port: BrokerReadPort | None) -> dict[str, MarketMark] | None:
    """The broker's current price for every held symbol, or ``None`` when unreadable."""
    if port is None:
        return None
    try:
        positions = await port.list_positions()
    except BrokerError as exc:
        logger.warning(
            "Activity statement could not read current positions",
            extra={"action": "activity_statement_marks_unavailable", "error": str(exc)},
        )
        return None
    return {
        position.symbol.upper(): MarketMark(price=position.current_price, observed_at_ms=position.observed_at_ms)
        for position in positions
        if position.current_price is not None
    }


def _fee_row_identity(
    subject_id: str, *, bots: Mapping[str, str | None], outside_symbols: Mapping[str, str],
) -> tuple[str | None, str, str | None]:
    """``(strategy_instance_id, label, order_id)``: every row names who owns it.

    A bot is labelled by its own name, never its strategy's (#2560 H19); an
    outside order by its symbol, with the order itself as the identity.
    """
    if subject_id.startswith(OUTSIDE_ORDER_SUBJECT_PREFIX):
        order_id = subject_id.removeprefix(OUTSIDE_ORDER_SUBJECT_PREFIX)
        symbol = outside_symbols.get(order_id)
        return None, "Outside order" if symbol is None else f"Outside order · {symbol}", order_id
    sid = bots.get(subject_id)
    return sid, sid if sid is not None else "Manual orders", None


def _deployment_fee_view(
    clerk: SqliteAlpacaClerkFacade, *, period: ActivityPeriod | None = None,
) -> DeploymentFeeAttribution:
    return _read_fee_view(clerk, period=period)[0]


@money_context()
def _read_fee_view(
    clerk: SqliteAlpacaClerkFacade, *, period: ActivityPeriod | None,
) -> tuple[DeploymentFeeAttribution, FeeAttribution]:
    """The wire view and the canonical projection it was built from, at one revision."""
    repo = clerk.repository
    with repo.write_fence() as conn:
        now = repo.clock()
        period_start_ms = None if period is None else activity_period_start_ms(period, now)
        projection = repo.fee_attribution(now_ms=now, from_ms=period_start_ms)
        meta = repo.control_meta_snapshot()
        bots = {row["subject_id"]: row["strategy_instance_id"] for row in conn.execute(
            "SELECT subject_id, strategy_instance_id FROM custody_subjects"
        )}
        outside_symbols = {row["broker_order_id"]: row["symbol"] for row in conn.execute(
            "SELECT broker_order_id, symbol FROM external_orders"
        )}
        rows = []
        for subject in sorted({share.subject_id for share in projection.shares}):
            sid, label, order_id = _fee_row_identity(subject, bots=bots, outside_symbols=outside_symbols)
            amounts = {state: sum((share.amount for share in projection.shares if share.subject_id == subject and share.state == state), Decimal(0)) for state in ("estimated", "modelled_settled", "observed")}
            rows.append(DeploymentFeeRow(subject_id=subject, strategy_instance_id=sid, label=label, order_id=order_id,
                estimated_usd=str(amounts["estimated"]), modelled_settled_usd=str(amounts["modelled_settled"]),
                observed_usd=str(amounts["observed"]), total_usd=str(projection.total_for(subject))))
        view = DeploymentFeeAttribution(account_id=repo.account_id, observed_at_ms=now, authority_revision=meta.control_revision,
            available=True, known=projection.known, rows=rows, account_unattributed_usd=str(projection.unattributed),
            messages=list(projection.unresolved), period=period, period_start_ms=period_start_ms)
        return view, projection


def _period_fee_view(
    clerk: SqliteAlpacaClerkFacade, period: ActivityPeriod, marks: Mapping[str, MarketMark] | None,
) -> DeploymentFeeAttribution:
    """One Activity period's fees plus its statement, read at one custody revision."""
    view, fees = _read_fee_view(clerk, period=period)
    return view.model_copy(update={"statement": _period_statement(clerk, view, fees, marks)})


def _period_statement(
    clerk: SqliteAlpacaClerkFacade,
    view: DeploymentFeeAttribution,
    fees: FeeAttribution,
    marks: Mapping[str, MarketMark] | None,
) -> ActivityPeriodStatement:
    """The period's statement over the orders the bots and this app placed.

    Outside orders' fees and unmatched account charges are the fee table's to
    show; FIFO never saw those orders, so counting their fees here would set
    one owner's costs against another's gains.
    """
    if view.period_start_ms is None:
        raise ValueError("a period statement needs the period's fee view")
    reader = SqliteEconomicProjectionReader.from_repository(clerk.repository)
    try:
        pnl = reader.account_pnl_attribution(
            from_ms=view.period_start_ms, to_ms=view.observed_at_ms, marks=marks or {},
        )
    except EconomicProjectionError as exc:
        logger.warning(
            "Activity statement could not read the account's trades",
            extra={"action": "activity_statement_trades_unavailable", "error": str(exc)},
        )
        return _statement_unavailable("The account's trade records could not be read. Refresh to retry.")
    finally:
        reader.close()
    if pnl.control_revision != view.authority_revision:
        return _statement_unavailable("The account's records changed while this was read. Refresh to retry.")
    outside = {share.subject_id for share in fees.shares if share.subject_id.startswith(OUTSIDE_ORDER_SUBJECT_PREFIX)}
    with money_context():
        custody_fees = sum((share.amount for share in fees.shares if share.subject_id not in outside), Decimal(0))
    return compose_period_statement(
        realized_usd=pnl.realized_pnl_total,
        fees_usd=custody_fees if fees.known else None,
        open_usd=None if marks is None else pnl.open_pnl_total,
        prices_read=marks is not None,
        outside_activity=bool(outside) or fees.unattributed != 0,
    )


def _statement_unavailable(detail: str) -> ActivityPeriodStatement:
    return ActivityPeriodStatement(
        state="unavailable", detail=detail, realized_usd=None, fees_usd=None, open_usd=None, net_usd=None,
    )


def _cents(amount: float | Decimal) -> int:
    """Whole cents, half-to-even, from an exact decimal reading of ``amount``."""
    with money_context() as context:
        context.traps[Inexact] = False
        return int((normalize_money(amount) * 100).to_integral_value(rounding=ROUND_HALF_EVEN))


def compose_period_statement(
    *,
    realized_usd: float,
    fees_usd: Decimal | None,
    open_usd: float | None,
    prices_read: bool,
    outside_activity: bool,
) -> ActivityPeriodStatement:
    """Compose one Activity period's statement from its canonical parts.

    Formula: ``net = realized - fees + open``, each rounded half-to-even to
      whole cents first, so the four displayed figures add up exactly.
      ``realized`` is FIFO P&L of lots closed in the period; ``fees`` the
      custody fee attribution for the period's fee days; ``open`` FIFO P&L of
      the lots held now at the broker's current prices.
    Reference: PRD #2560 "Activity" (Today's realized, fees, open and net);
      FIFO per ``docs/references/broker-v2-fifo-pnl.md``; fees per
      ``docs/references/alpaca-fee-attribution.md``.
    Canonical implementation: this function (the parts come from
      ``app.broker.alpaca.clerk.fifo_pnl`` and
      ``app.services.alpaca_fee_attribution``).
    Validated against: ``tests/services/test_activity_period_statement.py``.

    A missing part is never read as zero: its figure and the net are
    ``None`` and ``detail`` names why.
    """
    realized = _cents(realized_usd)
    fees = None if fees_usd is None else _cents(fees_usd)
    open_cents = None if open_usd is None else _cents(open_usd)
    net = None if fees is None or open_cents is None else realized - fees + open_cents
    if not prices_read:
        detail: str | None = "Current prices are unavailable, so open gains and the net are not shown. Refresh to retry."
    elif open_cents is None:
        detail = "A held share has no current price, so open gains and the net are not shown."
    elif fees is None:
        detail = "Fees for this period are not final yet, so fees and the net are not shown."
    elif outside_activity:
        detail = "Orders placed outside the bots, and account charges not matched to a bot, are not counted here."
    else:
        detail = None
    return ActivityPeriodStatement(
        state="ready" if net is not None else "unavailable",
        detail=detail,
        realized_usd=dollars(realized),
        fees_usd=None if fees is None else dollars(fees),
        open_usd=None if open_cents is None else dollars(open_cents),
        net_usd=None if net is None else dollars(net),
    )
