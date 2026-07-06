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

import asyncio
import logging

from app.broker.ibkr.api_evidence import (
    evidence_request,
    evidence_response,
    get_ibkr_api_evidence_recorder,
)
from app.broker.ibkr.client import BrokerError, IbkrClient, _is_paper_account
from app.broker.ibkr.models import (
    IbkrAccountSummary,
    IbkrPosition,
    IbkrPositionsSnapshot,
    OptionRight,
    SecType,
)
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

_POSITIONS_TIMEOUT_S = 8.0
_POSITIONS_LOCK_ATTR = "_learn_ai_positions_request_lock"
_POSITIONS_CACHE_FETCHED_AT_ATTR = "_learn_ai_positions_cache_fetched_at_ms"
_POSITIONS_TIMEOUT_EVENT_ATTR = "_learn_ai_positions_timeout_event_ms"
_ACCOUNT_SUMMARY_TIMEOUT_S = 8.0
_ACCOUNT_SUMMARY_TAGS = (
    "AccountType,NetLiquidation,TotalCashValue,SettledCash,"
    "AccruedCash,BuyingPower,EquityWithLoanValue,"
    "PreviousDayEquityWithLoanValue,GrossPositionValue,RegTEquity,"
    "RegTMargin,SMA,InitMarginReq,MaintMarginReq,AvailableFunds,"
    "ExcessLiquidity,Cushion,FullInitMarginReq,FullMaintMarginReq,"
    "FullAvailableFunds,FullExcessLiquidity,LookAheadNextChange,"
    "LookAheadInitMarginReq,LookAheadMaintMarginReq,"
    "LookAheadAvailableFunds,LookAheadExcessLiquidity,"
    "HighestSeverity,DayTradesRemaining,DayTradesRemainingT+1,"
    "DayTradesRemainingT+2,DayTradesRemainingT+3,"
    "DayTradesRemainingT+4,Leverage,$LEDGER:ALL"
)

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


def _cancel_account_summary_request(client: IbkrClient, req_id: int) -> None:
    cancel = getattr(getattr(client.ib, "client", None), "cancelAccountSummary", None)
    if not callable(cancel):
        return
    try:
        cancel(req_id)
    except Exception as exc:
        logger.debug("IBKR cancelAccountSummary raised after summary timeout: %s", exc)


async def _fetch_account_summary_rows(
    client: IbkrClient,
    *,
    account_id: str,
    timeout_s: float,
) -> list:
    ib_client = getattr(client.ib, "client", None)
    wrapper = getattr(client.ib, "wrapper", None)
    get_req_id = getattr(ib_client, "getReqId", None)
    req_account_summary = getattr(ib_client, "reqAccountSummary", None)
    start_req = getattr(wrapper, "startReq", None)
    acct_summary = getattr(wrapper, "acctSummary", None)
    if (
        not callable(get_req_id)
        or not callable(req_account_summary)
        or not callable(start_req)
        or not isinstance(acct_summary, dict)
    ):
        rows = await asyncio.wait_for(
            client.ib.accountSummaryAsync(account_id),
            timeout=timeout_s,
        )
        return list(rows)

    req_id = int(get_req_id())
    future = start_req(req_id)
    req_account_summary(req_id, "All", _ACCOUNT_SUMMARY_TAGS)
    try:
        await asyncio.wait_for(future, timeout=timeout_s)
    finally:
        _cancel_account_summary_request(client, req_id)
    return [v for v in acct_summary.values() if v.account == account_id]


async def fetch_account_summary(
    client: IbkrClient,
    *,
    timeout_s: float = _ACCOUNT_SUMMARY_TIMEOUT_S,
) -> IbkrAccountSummary:
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

    try:
        rows = await _fetch_account_summary_rows(
            client,
            account_id=account_id,
            timeout_s=timeout_s,
        )
    except TimeoutError as exc:
        raise BrokerError(
            f"IBKR account summary request timed out after {timeout_s:g}s."
        ) from exc
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


def _cached_positions(client: IbkrClient) -> list:
    positions = getattr(client.ib, "positions", None)
    if not callable(positions):
        raise BrokerError("IBKR positions cache is unavailable on this client.")
    try:
        return list(positions())
    except Exception as exc:
        raise BrokerError(f"IBKR positions cache read failed: {exc}") from exc


def _positions_request_lock(client: IbkrClient) -> asyncio.Lock:
    lock = getattr(client, _POSITIONS_LOCK_ATTR, None)
    if lock is None:
        lock = asyncio.Lock()
        setattr(client, _POSITIONS_LOCK_ATTR, lock)
    return lock


def _client_event_ms(client: IbkrClient) -> int | None:
    event_ms = getattr(client, "_last_event_ms", None)
    return event_ms if isinstance(event_ms, int) else None


def _positions_timeout_guard_active(client: IbkrClient) -> bool:
    timed_out_event_ms = getattr(client, _POSITIONS_TIMEOUT_EVENT_ATTR, None)
    if not isinstance(timed_out_event_ms, int):
        return False
    event_ms = _client_event_ms(client)
    if event_ms is not None and event_ms > timed_out_event_ms:
        delattr(client, _POSITIONS_TIMEOUT_EVENT_ATTR)
        return False
    return True


def _mark_positions_timed_out(client: IbkrClient) -> None:
    setattr(
        client,
        _POSITIONS_TIMEOUT_EVENT_ATTR,
        _client_event_ms(client) or now_ms_utc(),
    )


def _cancel_positions_request(client: IbkrClient) -> None:
    cancel = getattr(getattr(client.ib, "client", None), "cancelPositions", None)
    if not callable(cancel):
        return
    try:
        cancel()
    except Exception as exc:
        logger.debug("IBKR cancelPositions raised after positions timeout: %s", exc)


def _cached_position_rows_with_timestamp(client: IbkrClient) -> tuple[list, int]:
    fetched_at_ms = getattr(client, _POSITIONS_CACHE_FETCHED_AT_ATTR, None)
    if not isinstance(fetched_at_ms, int):
        raise BrokerError(
            "IBKR positions cache freshness is unknown; refusing to use cached positions."
        )
    return _cached_positions(client), fetched_at_ms


async def _fetch_position_rows(
    client: IbkrClient,
    *,
    timeout_s: float,
    allow_cache_fallback: bool,
) -> tuple[list, bool, int]:
    if _positions_timeout_guard_active(client):
        if allow_cache_fallback:
            raw, fetched_at_ms = _cached_position_rows_with_timestamp(client)
            return raw, True, fetched_at_ms
        raise BrokerError(
            "IBKR positions request previously timed out; reconnect IBKR before "
            "retrying live positions."
        )
    try:
        raw = await asyncio.wait_for(client.ib.reqPositionsAsync(), timeout=timeout_s)
        fetched_at_ms = now_ms_utc()
        setattr(client, _POSITIONS_CACHE_FETCHED_AT_ATTR, fetched_at_ms)
        return raw, False, fetched_at_ms
    except TimeoutError as exc:
        _cancel_positions_request(client)
        _mark_positions_timed_out(client)
        if not allow_cache_fallback:
            raise BrokerError(
                "IBKR reqPositionsAsync timed out; live positions are unavailable."
            ) from exc
        raw, fetched_at_ms = _cached_position_rows_with_timestamp(client)
        logger.warning(
            "IBKR reqPositionsAsync timed out; using synchronized positions cache",
            extra={"timeout_s": timeout_s, "cached_row_count": len(raw)},
        )
        return raw, True, fetched_at_ms


async def fetch_positions(
    client: IbkrClient,
    *,
    timeout_s: float = _POSITIONS_TIMEOUT_S,
    allow_cache_fallback: bool = False,
) -> IbkrPositionsSnapshot:
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

    # ib_async keys reqPositionsAsync on the static "positions" slot, so
    # overlapping calls can cross-contaminate if a timed-out response arrives
    # late. Serialize per client while still retrying the live request on the
    # next snapshot; cache fallback is only evidence for the current read.
    async with _positions_request_lock(client):
        raw, used_cache_fallback, fetched_at_ms = await _fetch_position_rows(
            client,
            timeout_s=timeout_s,
            allow_cache_fallback=allow_cache_fallback,
        )
    get_ibkr_api_evidence_recorder().record(
        source="account.fetch_positions",
        account_id=account_id,
        request=evidence_request("reqPositionsAsync"),
        response=evidence_response(
            "position",
            fields={
                "row_count": len(raw),
                "cache_fallback": used_cache_fallback,
                "cache_fallback_allowed": allow_cache_fallback,
            },
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
        used_cache_fallback=used_cache_fallback,
    )
