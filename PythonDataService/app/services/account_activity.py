"""The reads behind an account's Activity page (PRD #2560).

Today's money statement is a day figure: realized gains and the change in
open gains since the prior regular-session close -- the anchor of the
account's day P&L -- plus the fees billed today. A period's orders and cash
moves are one bounded broker read that says when it stopped short.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from decimal import Decimal

from app.broker.alpaca.clerk.et_day import ActivityPeriod, activity_period_start_ms
from app.broker.alpaca.clerk.money import display_cents, dollars, money_context
from app.broker.alpaca.clerk.sqlite.custody_subjects import OUTSIDE_ORDER_SUBJECT_PREFIX
from app.broker.alpaca.clerk.sqlite.day_pnl import day_pnl_window_start_ms
from app.broker.alpaca.clerk.sqlite.economic_projection import EconomicProjectionError
from app.broker.alpaca.clerk.sqlite.economic_projection_models import (
    current_position_marks,
    prior_close_position_marks,
)
from app.broker.contract.errors import BrokerError, BrokerEvidenceUnavailable
from app.broker.contract.models import BrokerPosition
from app.broker.contract.ports import BrokerActivityEvidencePort, BrokerReadPort
from app.schemas.account_activity import ActivityPeriodRead, TodayStatement
from app.services.alpaca_fee_reconciliation import read_fee_view
from app.services.clerk_transaction_projection import ClerkTransactionProjectionUnavailable
from app.services.sqlite_account_pnl_attribution import sqlite_account_pnl_attribution
from app.services.sqlite_clerk_compat import active_sqlite_facade
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)


async def activity_period_read(
    port: BrokerReadPort, *, period: ActivityPeriod, page_token: str | None,
) -> ActivityPeriodRead:
    """One bounded read of a period's orders and cash moves, newest first.

    The window opens at the period's own calendar anchor, the same one its
    fees use. ``page_token`` continues a previous read that stopped short.
    """
    if not isinstance(port, BrokerActivityEvidencePort):
        raise BrokerEvidenceUnavailable(
            "This broker cannot list an account's activity by period.",
            broker=port.broker_id,
        )
    observed_at_ms = now_ms_utc()
    period_start_ms = activity_period_start_ms(period, observed_at_ms)
    evidence = await port.read_activity_evidence(page_token=page_token, after_ms=period_start_ms)
    return ActivityPeriodRead(
        period=period, period_start_ms=period_start_ms, observed_at_ms=observed_at_ms, evidence=evidence,
    )


async def today_statement(port: BrokerReadPort) -> TodayStatement:
    """Today's statement for the bots and this app's orders, since the last close.

    One positions read supplies both marks: current prices for the shares
    held now, and the broker's prior-close prices for the shares held at
    that close. Realized and open gains come from the canonical account
    attribution read; fees from the custody fee view at the same revision.
    """
    clerk = active_sqlite_facade("alpaca")
    if clerk is None:
        return _unavailable(
            "Today's figures are unavailable while the account Clerk is offline.",
            since_ms=None, observed_at_ms=now_ms_utc(),
        )
    positions = await _positions_or_none(port)
    held = () if positions is None else positions
    now = clerk.repository.clock()
    since_ms = day_pnl_window_start_ms(now)
    try:
        pnl = await asyncio.to_thread(
            sqlite_account_pnl_attribution,
            account_id=clerk.account_id,
            from_ms=since_ms,
            to_ms=now,
            marks=current_position_marks(held),
            start_marks=prior_close_position_marks(held, closed_at_ms=since_ms),
        )
    except (ClerkTransactionProjectionUnavailable, EconomicProjectionError) as exc:
        logger.warning(
            "Today's statement could not read the account's trades",
            extra={"action": "today_statement_trades_unavailable", "error": str(exc)},
        )
        return _unavailable(
            "The account's trade records could not be read. Refresh to retry.",
            since_ms=since_ms, observed_at_ms=now,
        )
    if pnl is None:
        return _unavailable(
            "Today's figures are not shown on a Shadow account yet.", since_ms=since_ms, observed_at_ms=now,
        )
    fee_view, fees = await asyncio.to_thread(read_fee_view, clerk, period="today")
    if fee_view.authority_revision != pnl.control_revision:
        return _unavailable(
            "The account's records changed while this was read. Refresh to retry.",
            since_ms=since_ms, observed_at_ms=now,
        )
    outside = {share.subject_id for share in fees.shares if share.subject_id.startswith(OUTSIDE_ORDER_SUBJECT_PREFIX)}
    with money_context():
        custody_fees = sum((share.amount for share in fees.shares if share.subject_id not in outside), Decimal(0))
    return compose_today_statement(
        since_ms=since_ms,
        observed_at_ms=now,
        realized_usd=pnl.exact_realized_pnl_total,
        fees_usd=custody_fees if fees.known else None,
        start_open_usd=pnl.exact_start_open_pnl_total,
        open_usd=pnl.exact_open_pnl_total,
        prices_read=positions is not None,
        outside_activity=bool(outside) or fees.unattributed != 0,
    )


async def _positions_or_none(port: BrokerReadPort) -> Sequence[BrokerPosition] | None:
    """The broker's positions, or ``None`` when they cannot be read."""
    try:
        return await port.list_positions()
    except BrokerError as exc:
        logger.warning(
            "Today's statement could not read current positions",
            extra={"action": "today_statement_prices_unavailable", "error": str(exc)},
        )
        return None


def _unavailable(detail: str, *, since_ms: int | None, observed_at_ms: int) -> TodayStatement:
    return TodayStatement(
        state="unavailable", detail=detail, since_ms=since_ms, observed_at_ms=observed_at_ms,
        realized_usd=None, fees_usd=None, open_change_usd=None, net_usd=None,
    )


def compose_today_statement(
    *,
    since_ms: int,
    observed_at_ms: int,
    realized_usd: Decimal,
    fees_usd: Decimal | None,
    start_open_usd: Decimal | None,
    open_usd: Decimal | None,
    prices_read: bool,
    outside_activity: bool,
) -> TodayStatement:
    """Compose Today's statement from its canonical parts.

    Formula: ``net = realized - fees + (open_end - open_start)``, each
      displayed part rounded half-to-even to whole cents first, so the four
      figures add up exactly. ``realized`` is FIFO P&L of lots closed in
      ``(since, now]``; ``open_start`` values the lots held at the prior
      regular-session close at that close's prices; ``open_end`` the lots
      held now at current prices; ``fees`` the custody fee attribution for
      today's ET fee day, bots and this app's orders only. Every part is
      exact: FIFO's exact totals (``AccountPnlAttribution.exact_*``), never
      their float views, whose one rounding can cross a half cent (#2556).
    Reference: ``CONTEXT.md`` "Account day P&L" (the prior-close anchor, and
      why lifetime unrealized P&L is not a day figure); the C3 local delta in
      ``app/services/account_pnl_reconciliation.py``; FIFO per
      ``docs/references/broker-v2-fifo-pnl.md``; fees per
      ``docs/references/alpaca-fee-attribution.md``.
    Canonical implementation: this function (the parts come from
      ``SqliteEconomicProjectionReader.account_pnl_attribution`` through
      ``sqlite_account_pnl_attribution`` and from
      ``alpaca_fee_reconciliation.read_fee_view``).
    Validated against: ``tests/services/test_today_statement.py`` and
      ``tests/broker/alpaca/clerk/sqlite/test_fee_attribution_view.py``.

    A missing part is never read as zero: its figure and the net are
    ``None`` and ``detail`` names why.
    """
    realized = display_cents(realized_usd)
    fees = None if fees_usd is None else display_cents(fees_usd)
    change: int | None = None
    if start_open_usd is not None and open_usd is not None:
        with money_context():
            change = display_cents(open_usd - start_open_usd)
    net = None if fees is None or change is None else realized - fees + change
    detail: str | None
    if change is None and not prices_read:
        detail = "Current prices could not be read, so the change in open gains and the net are not shown. Refresh to retry."
    elif open_usd is None:
        detail = "A share held now has no current price, so the change in open gains and the net are not shown."
    elif start_open_usd is None:
        detail = (
            "A share held at the last close has no closing price here, "
            "so the change in open gains and the net are not shown."
        )
    elif fees is None:
        detail = "Today's fees are not final yet, so fees and the net are not shown."
    elif outside_activity:
        detail = "Orders placed outside the bots, and account charges not matched to a bot, are not counted here."
    else:
        detail = None
    return TodayStatement(
        state="ready" if net is not None else "unavailable",
        detail=detail,
        since_ms=since_ms,
        observed_at_ms=observed_at_ms,
        realized_usd=dollars(realized),
        fees_usd=None if fees is None else dollars(fees),
        open_change_usd=None if change is None else dollars(change),
        net_usd=None if net is None else dollars(net),
    )


__all__ = ["activity_period_read", "compose_today_statement", "today_statement"]
