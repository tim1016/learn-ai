"""Account snapshot and positions read API.

Phase 2a surface (sync, no streaming):

* ``fetch_account_summary(client) -> IbkrAccountSummary`` — single round
  trip, cash + margin + day P&L.
* ``fetch_positions(client) -> IbkrPositionsSnapshot`` — open positions
  list, stocks + options share one model.

Both wrap ``ib_async`` calls (``reqAccountSummary`` / ``positions``) and
translate IBKR's untyped string-tag dictionary into the strict Pydantic
wire models. The engine's reconciliation pass diffs these against
``PortfolioService`` to surface fill-model and Greeks-model errors.

Phase 2b (out of scope here) adds the SSE P&L streams in ``pnl.py``.
"""

from __future__ import annotations

import logging

from app.broker.ibkr.api_evidence import (
    evidence_request,
    evidence_response,
    get_ibkr_api_evidence_recorder,
)
from app.broker.ibkr.client import IbkrClient, _is_paper_account
from app.broker.ibkr.models import (
    IbkrAccountSummary,
    IbkrPosition,
    IbkrPositionsSnapshot,
    OptionRight,
    SecType,
)
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)


# IBKR's reqAccountSummary returns string-keyed tags. ib_async subscribes
# to the standard tag set on connect; we just consume the ones Phase 2a
# needs from the result rows. Authoritative tag list:
# https://interactivebrokers.github.io/tws-api/account_summary_tags.html
# Tags consumed below: TotalCashValue, NetLiquidation, BuyingPower,
# InitMarginReq, MaintMarginReq, ExcessLiquidity, EquityWithLoanValue,
# AvailableFunds, RealizedPnL, UnrealizedPnL.


def _coerce_float_or_none(value: str | float | None) -> float | None:
    """IBKR returns summary values as strings; coerce safely.

    Empty strings and non-numeric markers like ``""`` or ``"BASE"`` become
    ``None``. We never raise from here — bad numbers shouldn't take down
    a snapshot.
    """
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def fetch_account_summary(client: IbkrClient) -> IbkrAccountSummary:
    """Fetch a one-shot account summary.

    Uses ``reqAccountSummaryAsync`` against the connected account ID.
    The returned ``AccountValue`` rows are flattened into a single
    ``IbkrAccountSummary``. Currency-specific rows for the base currency
    win; rows for other currencies are ignored (Phase 2a is USD-only).
    """
    client.require_connected()
    account_id = client.connected_account
    if account_id is None:
        # Should be impossible after require_connected, but make the
        # invariant explicit for the type checker and for safety.
        raise RuntimeError("connected client has no account_id")

    rows = await client.ib.accountSummaryAsync(account_id)
    get_ibkr_api_evidence_recorder().record(
        source="account.fetch_account_summary",
        account_id=account_id,
        request=evidence_request("accountSummaryAsync", account_id=account_id),
        response=evidence_response(
            "accountSummary",
            fields={"row_count": len(rows)},
            objects=rows,
        ),
    )

    # Build a lookup keyed by tag, but only for the account+base currency
    # we care about. IBKR streams back rows for each currency the account
    # holds — for Phase 2a we narrow to the base (USD) by ignoring
    # non-empty currency rows that disagree.
    base_currency = "USD"
    by_tag: dict[str, str] = {}
    for row in rows:
        if row.account != account_id:
            continue
        if row.currency and row.currency != base_currency and row.currency != "BASE":
            continue
        by_tag[row.tag] = row.value
        if row.tag == "AccountType" and row.currency:
            base_currency = row.currency

    is_paper = _is_paper_account(account_id)
    return IbkrAccountSummary(
        account_id=account_id,
        is_paper=is_paper,
        base_currency=base_currency,
        cash_balance=_coerce_float_or_none(by_tag.get("TotalCashValue")),
        net_liquidation=_coerce_float_or_none(by_tag.get("NetLiquidation")),
        buying_power=_coerce_float_or_none(by_tag.get("BuyingPower")),
        init_margin=_coerce_float_or_none(by_tag.get("InitMarginReq")),
        maint_margin=_coerce_float_or_none(by_tag.get("MaintMarginReq")),
        excess_liquidity=_coerce_float_or_none(by_tag.get("ExcessLiquidity")),
        equity_with_loan_value=_coerce_float_or_none(by_tag.get("EquityWithLoanValue")),
        available_funds=_coerce_float_or_none(by_tag.get("AvailableFunds")),
        unrealized_pnl=_coerce_float_or_none(by_tag.get("UnrealizedPnL")),
        realized_pnl=_coerce_float_or_none(by_tag.get("RealizedPnL")),
        # day_pnl is not in reqAccountSummary; it arrives via reqPnL stream
        # (Phase 2b). Leave None here.
        day_pnl=None,
        fetched_at_ms=now_ms_utc(),
    )


def _ibkr_position_to_model(
    pos,
    account_id: str,
    fetched_at_ms: int,
) -> IbkrPosition:
    """``ib_async.Position`` → ``IbkrPosition``.

    Splits option-specific fields from the contract; non-option contracts
    leave them ``None``. The contract carries both string fields (right,
    expiry as YYYYMMDD) and numeric fields (strike, multiplier).
    """
    contract = pos.contract
    sec_type: SecType = contract.secType  # type: ignore[assignment]

    expiry_ms: int | None = None
    strike: float | None = None
    right: OptionRight | None = None
    multiplier = 1

    if sec_type in {"OPT", "FOP"}:
        # Lazy import to avoid forcing contracts.py if account-only used.
        from app.broker.ibkr.contracts import yyyymmdd_to_expiry_ms

        if contract.lastTradeDateOrContractMonth:
            try:
                expiry_ms = yyyymmdd_to_expiry_ms(contract.lastTradeDateOrContractMonth[:8])
            except ValueError:
                expiry_ms = None
        strike = float(contract.strike) if contract.strike else None
        if contract.right in {"C", "P"}:
            right = contract.right  # type: ignore[assignment]
        try:
            multiplier = int(contract.multiplier) if contract.multiplier else 100
        except (TypeError, ValueError):
            multiplier = 100
    elif sec_type == "STK":
        multiplier = 1

    return IbkrPosition(
        account_id=account_id,
        con_id=int(contract.conId),
        symbol=contract.symbol,
        sec_type=sec_type,
        exchange=contract.exchange or contract.primaryExchange or None,
        currency=contract.currency or "USD",
        expiry_ms=expiry_ms,
        strike=strike,
        right=right,
        multiplier=multiplier,
        quantity=float(pos.position),
        avg_cost=float(pos.avgCost),
        # Live marks come from a separate market-data subscription; leave
        # None for the bare positions fetch.
        market_price=None,
        market_value=None,
        fetched_at_ms=fetched_at_ms,
    )


async def fetch_positions(client: IbkrClient) -> IbkrPositionsSnapshot:
    """All open positions for the connected account.

    ``reqPositionsAsync`` returns positions across all accounts the user
    has access to; we filter to the connected account so Phase 2a is
    single-account-clean (multi-account is a Phase 2.5 follow-up,
    matching the multi-account-FA note in client.py).
    """
    client.require_connected()
    account_id = client.connected_account
    if account_id is None:
        raise RuntimeError("connected client has no account_id")

    raw = await client.ib.reqPositionsAsync()
    fetched_at_ms = now_ms_utc()
    get_ibkr_api_evidence_recorder().record(
        source="account.fetch_positions",
        account_id=account_id,
        request=evidence_request("reqPositionsAsync"),
        response=evidence_response(
            "position",
            fields={"row_count": len(raw)},
            objects=raw,
        ),
    )

    positions: list[IbkrPosition] = []
    for pos in raw:
        if pos.account != account_id:
            continue
        # IBKR sends a row with quantity=0 when a position closes; skip.
        if float(pos.position) == 0.0:
            continue
        try:
            positions.append(_ibkr_position_to_model(pos, account_id, fetched_at_ms))
        except Exception as exc:
            logger.warning(
                "Skipping unparseable position for con_id=%s: %s",
                getattr(pos.contract, "conId", "?"),
                exc,
            )

    return IbkrPositionsSnapshot(
        account_id=account_id,
        is_paper=_is_paper_account(account_id),
        positions=positions,
        fetched_at_ms=fetched_at_ms,
    )
