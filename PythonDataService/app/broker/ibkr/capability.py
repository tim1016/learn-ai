"""Read-only IBKR session/data capability probe.

Slice 0 of issue #1005. This module interrogates broker capability with
read calls and ``whatIf=True`` order previews only. It must never submit a
live order.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.broker.ibkr.api_evidence import (
    IbkrApiEvidenceEvent,
    evidence_request,
    evidence_response,
    get_ibkr_api_evidence_recorder,
)
from app.broker.ibkr.client import BrokerError, IbkrClient, _is_paper_account
from app.schemas.broker_capability import (
    CapabilityDataQuality,
    CapabilityTradeability,
    SessionCapability,
    SessionDataCapability,
    SessionKind,
)
from app.utils.timestamps import now_ms_utc, to_ms_utc

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT_S = 12.0
_MARKET_DATA_SAMPLE_S = 1.0
_ALL_SESSIONS: tuple[SessionKind, ...] = ("RTH", "PRE", "POST", "OVERNIGHT")
_MARKET_DATA_TYPES: dict[int, CapabilityDataQuality] = {
    1: "live",
    2: "frozen",
    3: "delayed",
    4: "delayed_frozen",
}


@dataclass(frozen=True)
class SessionWindow:
    open_ms: int
    close_ms: int


# IBKR reports US-equity ``timeZoneId`` as DST-blind abbreviations (notably
# ``EST``, which ``ZoneInfo`` resolves to a *fixed* UTC-05 zone). Parsing a
# summer schedule in a fixed offset shifts every window by an hour. Map the
# abbreviations IBKR actually emits onto the DST-aware IANA zone so session
# boundaries stay correct across the DST transition (temporal-rigor: "DST via
# the NY zone, never a fixed offset").
_IBKR_TIMEZONE_ALIASES: dict[str, str] = {
    "EST": "America/New_York",
    "EDT": "America/New_York",
    "EST5EDT": "America/New_York",
    "US/Eastern": "America/New_York",
}


def _normalize_ibkr_timezone(time_zone_id: str) -> str:
    return _IBKR_TIMEZONE_ALIASES.get(time_zone_id.strip(), time_zone_id.strip())


def parse_ibkr_schedule(schedule: str, time_zone_id: str) -> list[SessionWindow]:
    """Parse IBKR ``tradingHours`` / ``liquidHours`` into UTC-ms windows.

    The schedule segments are interpreted in the instrument timezone reported
    by IBKR, then converted back to the repo's canonical int64-ms UTC boundary.
    """
    if not schedule.strip():
        raise ValueError("IBKR schedule string is empty.")
    try:
        zone = ZoneInfo(_normalize_ibkr_timezone(time_zone_id))
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown IBKR schedule timezone: {time_zone_id!r}") from exc

    windows: list[SessionWindow] = []
    for raw_segment in schedule.split(";"):
        segment = raw_segment.strip()
        if not segment:
            continue
        try:
            date_part, hours_part = segment.split(":", 1)
        except ValueError as exc:
            raise ValueError(f"malformed IBKR schedule segment: {segment!r}") from exc
        if hours_part == "CLOSED":
            continue
        try:
            open_part, close_part = hours_part.split("-", 1)
            open_dt = _parse_schedule_in_zone(f"{date_part}{open_part}", zone)
            close_dt = _parse_schedule_in_zone(close_part.replace(":", "", 1), zone)
        except ValueError as exc:
            raise ValueError(f"malformed IBKR schedule segment: {segment!r}") from exc
        open_ms = to_ms_utc(open_dt)
        close_ms = to_ms_utc(close_dt)
        if close_ms <= open_ms:
            raise ValueError(f"IBKR schedule segment closes before it opens: {segment!r}")
        windows.append(SessionWindow(open_ms=open_ms, close_ms=close_ms))
    return windows


def classify_entitlement(
    market_data_type: int | None,
    error_codes: list[int] | None = None,
) -> CapabilityDataQuality:
    """Classify the observed IBKR market-data entitlement."""
    if market_data_type in _MARKET_DATA_TYPES:
        return _MARKET_DATA_TYPES[market_data_type]
    codes = set(error_codes or [])
    if 10167 in codes:
        return "delayed"
    if 354 in codes or 10168 in codes:
        return "none"
    return "none"


async def probe_session_data_capability(
    client: IbkrClient,
    *,
    symbol: str,
    as_of_ms: int | None = None,
) -> SessionDataCapability:
    client.require_live()
    account_id = client.connected_account
    if account_id is None:
        raise BrokerError("connected client has no account_id")
    probed_at_ms = as_of_ms or now_ms_utc()
    symbol = symbol.upper()
    evidence_start_seq = _last_evidence_seq()

    contract_details = await _request_contract_details(client, symbol, account_id)
    contract = getattr(contract_details, "contract", None)
    if contract is None:
        raise BrokerError(f"IBKR contract details for {symbol} did not include a contract.")
    con_id = int(getattr(contract, "conId", 0) or 0)
    time_zone_id = str(getattr(contract_details, "timeZoneId", "") or "America/New_York")
    trading_hours = str(getattr(contract_details, "tradingHours", "") or "")
    liquid_hours = str(getattr(contract_details, "liquidHours", "") or "")
    valid_exchanges = str(getattr(contract_details, "validExchanges", "") or "")

    try:
        trading_windows = parse_ibkr_schedule(trading_hours, time_zone_id)
        liquid_windows = parse_ibkr_schedule(liquid_hours, time_zone_id)
    except ValueError as exc:
        raise BrokerError(str(exc)) from exc

    data_quality, market_data_codes = await _sample_market_data_type(
        client,
        contract,
        symbol,
        account_id,
    )
    outside_rth_eligible, what_if_codes = await _preview_outside_rth_limit_order(
        client,
        contract,
        symbol,
        account_id,
    )
    sessions = _build_session_capabilities(
        trading_windows=trading_windows,
        liquid_windows=liquid_windows,
        as_of_ms=probed_at_ms,
        time_zone_id=time_zone_id,
        data_quality=data_quality,
        outside_rth_eligible=outside_rth_eligible,
        evidence_codes=sorted(set(market_data_codes + what_if_codes)),
        valid_exchanges=valid_exchanges,
    )
    return SessionDataCapability(
        symbol=symbol,
        con_id=con_id,
        account_mode="paper" if _is_paper_account(account_id) else "live",
        account_id=account_id,
        probed_at_ms=probed_at_ms,
        time_zone_id=time_zone_id,
        sessions=sessions,
        raw_evidence=_evidence_after(evidence_start_seq, symbol=symbol),
    )


async def _request_contract_details(
    client: IbkrClient,
    symbol: str,
    account_id: str,
) -> object:
    from ib_async import Stock

    contract = Stock(symbol=symbol, exchange="SMART", currency="USD")
    request = evidence_request(
        "reqContractDetailsAsync",
        contract={"symbol": symbol, "secType": "STK", "exchange": "SMART", "currency": "USD"},
    )
    try:
        details = await asyncio.wait_for(
            client.ib.reqContractDetailsAsync(contract),
            timeout=_PROBE_TIMEOUT_S,
        )
    except TimeoutError as exc:
        raise BrokerError(f"IBKR reqContractDetailsAsync timed out for {symbol}.") from exc
    get_ibkr_api_evidence_recorder().record(
        source="capability.req_contract_details",
        account_id=account_id,
        symbol=symbol,
        request=request,
        response=evidence_response(
            "contractDetails",
            fields={"row_count": len(details)},
            objects=details,
        ),
    )
    if not details:
        raise BrokerError(f"IBKR returned no contract details for {symbol}.")
    return details[0]


async def _sample_market_data_type(
    client: IbkrClient,
    contract: object,
    symbol: str,
    account_id: str,
) -> tuple[CapabilityDataQuality, list[int]]:
    codes_before = _last_client_code(client)
    client.ib.reqMarketDataType(1)
    request_event = get_ibkr_api_evidence_recorder().record(
        source="capability.req_market_data_type",
        account_id=account_id,
        symbol=symbol,
        request=evidence_request("reqMarketDataType", market_data_type=1),
        response=evidence_response("marketDataType", fields={"requested": 1}),
    )
    ticker = client.ib.reqMktData(contract, "", False, False)
    get_ibkr_api_evidence_recorder().record(
        source="capability.req_mkt_data",
        account_id=account_id,
        symbol=symbol,
        request=evidence_request("reqMktData", contract={"con_id": int(getattr(contract, "conId", 0) or 0)}),
        response=evidence_response("tickSnapshot", fields={"request_seq": request_event.seq}),
    )
    try:
        await asyncio.sleep(_MARKET_DATA_SAMPLE_S)
    finally:
        client.ib.cancelMktData(contract)
        get_ibkr_api_evidence_recorder().record(
            source="capability.cancel_mkt_data",
            account_id=account_id,
            symbol=symbol,
            request=evidence_request("cancelMktData", contract={"con_id": int(getattr(contract, "conId", 0) or 0)}),
        )

    market_data_type = getattr(ticker, "marketDataType", None)
    codes = _new_client_codes(client, codes_before)
    return classify_entitlement(market_data_type, codes), codes


async def _preview_outside_rth_limit_order(
    client: IbkrClient,
    contract: object,
    symbol: str,
    account_id: str,
) -> tuple[bool, list[int]]:
    from ib_async import LimitOrder

    codes_before = _last_client_code(client)
    order = LimitOrder(action="BUY", totalQuantity=1, lmtPrice=0.01)
    order.outsideRth = True
    order.whatIf = True
    request = evidence_request(
        "whatIfOrderAsync",
        contract={"con_id": int(getattr(contract, "conId", 0) or 0), "symbol": symbol},
        order={"action": "BUY", "totalQuantity": 1, "lmtPrice": 0.01, "outsideRth": True, "whatIf": True},
    )
    try:
        state = await asyncio.wait_for(
            client.ib.whatIfOrderAsync(contract, order),
            timeout=_PROBE_TIMEOUT_S,
        )
    except TimeoutError as exc:
        raise BrokerError(f"IBKR whatIfOrderAsync timed out for {symbol}.") from exc
    codes = _new_client_codes(client, codes_before)
    warning_text = str(getattr(state, "warningText", "") or "")
    get_ibkr_api_evidence_recorder().record(
        source="capability.what_if_outside_rth",
        account_id=account_id,
        symbol=symbol,
        request=request,
        response=evidence_response(
            "whatIfOrder",
            fields={
                "warningText": warning_text,
                "initMarginChange": getattr(state, "initMarginChange", None),
                "commission": getattr(state, "commission", None),
            },
        ),
    )
    return not codes and "reject" not in warning_text.lower(), codes


def _build_session_capabilities(
    *,
    trading_windows: list[SessionWindow],
    liquid_windows: list[SessionWindow],
    as_of_ms: int,
    time_zone_id: str,
    data_quality: CapabilityDataQuality,
    outside_rth_eligible: bool,
    evidence_codes: list[int],
    valid_exchanges: str,
) -> dict[SessionKind, SessionCapability]:
    today_trading = _windows_for_local_day(trading_windows, as_of_ms, time_zone_id)
    today_liquid = _windows_for_local_day(liquid_windows, as_of_ms, time_zone_id)
    rth = today_liquid[0] if today_liquid else None
    windows: dict[SessionKind, SessionWindow | None] = {
        "RTH": rth,
        "PRE": None,
        "POST": None,
        "OVERNIGHT": None,
    }
    if rth is not None:
        for window in today_trading:
            if window.close_ms <= rth.open_ms:
                windows["PRE"] = _merge_window(windows["PRE"], window)
            elif window.open_ms >= rth.close_ms:
                windows["POST"] = _merge_window(windows["POST"], window)
            else:
                if window.open_ms < rth.open_ms:
                    windows["PRE"] = _merge_window(
                        windows["PRE"],
                        SessionWindow(window.open_ms, rth.open_ms),
                    )
                if window.close_ms > rth.close_ms:
                    windows["POST"] = _merge_window(
                        windows["POST"],
                        SessionWindow(rth.close_ms, window.close_ms),
                    )
    for window in today_trading:
        if _crosses_local_midnight(window, time_zone_id):
            windows["OVERNIGHT"] = _merge_window(windows["OVERNIGHT"], window)

    supports_overnight = "OVERNIGHT" in {part.strip().upper() for part in valid_exchanges.split(",")}
    return {
        kind: SessionCapability(
            window_today_open_ms=window.open_ms if window else None,
            window_today_close_ms=window.close_ms if window else None,
            data=data_quality if window else "none",
            tradeable=_tradeability(kind, window, outside_rth_eligible, supports_overnight),
            order_eligible_outside_rth=(outside_rth_eligible if kind != "RTH" else False),
            evidence_codes=evidence_codes,
        )
        for kind, window in windows.items()
    }


def _tradeability(
    kind: SessionKind,
    window: SessionWindow | None,
    outside_rth_eligible: bool,
    supports_overnight: bool,
) -> CapabilityTradeability:
    if window is None:
        if kind == "OVERNIGHT" and supports_overnight:
            return "needs_enablement"
        return "no"
    if kind == "RTH":
        return "yes"
    return "yes" if outside_rth_eligible else "needs_enablement"


def _parse_schedule_in_zone(value: str, zone: ZoneInfo) -> datetime:
    if len(value) != 12:
        raise ValueError(f"expected YYYYMMDDHHMM, got {value!r}")
    return datetime.strptime(value, "%Y%m%d%H%M").replace(tzinfo=zone)


def _windows_for_local_day(
    windows: list[SessionWindow],
    as_of_ms: int,
    time_zone_id: str,
) -> list[SessionWindow]:
    zone = ZoneInfo(time_zone_id)
    day = datetime.fromtimestamp(as_of_ms / 1000, tz=zone).date()
    return [
        window
        for window in windows
        if datetime.fromtimestamp(window.open_ms / 1000, tz=zone).date() == day
        or datetime.fromtimestamp(window.close_ms / 1000, tz=zone).date() == day
    ]


def _crosses_local_midnight(window: SessionWindow, time_zone_id: str) -> bool:
    zone = ZoneInfo(time_zone_id)
    opened = datetime.fromtimestamp(window.open_ms / 1000, tz=zone)
    closed = datetime.fromtimestamp(window.close_ms / 1000, tz=zone)
    return opened.date() != closed.date()


def _merge_window(left: SessionWindow | None, right: SessionWindow) -> SessionWindow:
    if left is None:
        return right
    return SessionWindow(
        open_ms=min(left.open_ms, right.open_ms),
        close_ms=max(left.close_ms, right.close_ms),
    )


def _last_client_code(client: IbkrClient) -> int | None:
    code = getattr(client, "_last_ibkr_code", None)
    return int(code) if isinstance(code, int) else None


def _new_client_codes(client: IbkrClient, before: int | None) -> list[int]:
    after = _last_client_code(client)
    if after is None or after == before:
        return []
    return [after]


def _last_evidence_seq() -> int:
    events = get_ibkr_api_evidence_recorder().backfill(after_seq=0, limit=10_000)
    return events[-1].seq if events else 0


def _evidence_after(after_seq: int, *, symbol: str) -> list[IbkrApiEvidenceEvent]:
    return [
        event
        for event in get_ibkr_api_evidence_recorder().backfill(after_seq=after_seq, limit=500)
        if event.symbol == symbol
    ]
