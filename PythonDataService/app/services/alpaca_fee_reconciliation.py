"""Deployment fee attribution over the Clerk's fee evidence.

Observed FEE activities are the truth; this module attributes them to the
deployments that incurred them, stopped deployments included.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from decimal import Decimal

from app.broker.alpaca.clerk.et_day import ActivityPeriod, activity_period_start_ms
from app.broker.alpaca.clerk.money import money_context
from app.broker.alpaca.clerk.sqlite.custody_subjects import OUTSIDE_ORDER_SUBJECT_PREFIX
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.schemas.alpaca_fee_reconciliation import (
    DeploymentFeeAttribution,
    DeploymentFeeRow,
)
from app.services.alpaca_fee_attribution import FeeAttribution
from app.services.broker_v2_panel.panel_errors import PanelUnavailableError
from app.services.sqlite_clerk_compat import active_sqlite_facade
from app.utils.timestamps import now_ms_utc


async def deployment_fee_attribution(
    strategy_instance_id: str | None = None,
    *,
    period: ActivityPeriod | None = None,
) -> DeploymentFeeAttribution:
    """Account/deployment UI uses exactly the custody fee projection.

    ``period`` narrows the account read to one Activity period's fee days.
    """
    if strategy_instance_id is not None:
        from app.services.bot_runner import get_bot_task_registry
        from app.services.broker_v2_panel.panel_data_source import _panel_authority_for_binding

        registry = get_bot_task_registry()
        if registry is not None:
            binding = registry.binding_for_control("alpaca", strategy_instance_id)
            try:
                async with _panel_authority_for_binding(registry, binding) as selected:
                    if selected is not None:
                        return (await asyncio.to_thread(read_fee_view, selected, period=None))[0]
            except PanelUnavailableError as exc:
                # This bot's own custody cannot be read right now -- a Dry Run
                # still being restored (#2684), or held elsewhere: its fees
                # are unavailable, in its own words, never a 500.
                return _unavailable_fee_attribution(
                    str(exc) if exc.detail is None else f"{exc} {exc.detail}", period=period
                )
    clerk = active_sqlite_facade("alpaca") if strategy_instance_id is None else None
    if clerk is None:
        return _unavailable_fee_attribution("Fee evidence is unavailable while the account Clerk is offline.", period=period)
    return (await asyncio.to_thread(read_fee_view, clerk, period=period))[0]


def _unavailable_fee_attribution(message: str, *, period: ActivityPeriod | None) -> DeploymentFeeAttribution:
    return DeploymentFeeAttribution(account_id=None, observed_at_ms=now_ms_utc(), authority_revision=None,
        available=False, known=False, rows=[], account_unattributed_usd=None, messages=[message], period=period)


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


@money_context()
def read_fee_view(
    clerk: SqliteAlpacaClerkFacade, *, period: ActivityPeriod | None,
) -> tuple[DeploymentFeeAttribution, FeeAttribution]:
    """The wire view and the canonical projection it was built from, at one revision.

    ``period`` keeps only that Activity period's fee days; ``None`` reads the
    lifetime.
    """
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
