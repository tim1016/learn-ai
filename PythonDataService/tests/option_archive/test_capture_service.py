from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import httpx
import respx

from app.data_lake.ensure_data import _minute_trade_dch
from app.data_lake.option_session import read_option_session
from app.lean_sidecar.trading_calendar import session_open_ms_utc
from app.option_archive.planner import wall_clock_ms
from app.option_archive.service import OptionArchiveService
from app.option_archive.settings import ArchiveSettings
from app.option_archive.state import ArchiveState
from app.options.contract import OptionContract
from tests._helpers.option_vendor import no_trades, traded
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import stock_bar, store_listing, store_underlying_day, vendor_listing_row
from tests.options_backtest._lake import tmp_lake as tmp_lake

DAY = date(2026, 10, 9)
NOW = wall_clock_ms(date(2026, 10, 12), 120)


async def seed(fake_catalog, tmp_lake: Path, underlying: str) -> tuple[OptionContract, OptionContract]:
    call = OptionContract.of(root=underlying, expiry_date=DAY, right="call", strike=500)
    put = OptionContract.of(root=underlying, expiry_date=DAY, right="put", strike=500)
    await store_listing([vendor_listing_row(call, underlying=underlying), vendor_listing_row(put, underlying=underlying)],
                        underlying=underlying, day=DAY)
    await store_underlying_day(fake_catalog, tmp_lake, [stock_bar(session_open_ms_utc(DAY), "500", "500", "500", "500", symbol=underlying)],
                               symbol=underlying, day=DAY)
    for row in fake_catalog.rows.values():
        if row["market"] == "usa" and row["symbol"] == underlying:
            row["data_contract_hash"] = _minute_trade_dch("raw", None, trading_date=DAY)
    return call, put


@respx.mock
async def test_full_pass_captures_all_three_roots_traded_and_empty_and_keeps_failure_isolated(fake_catalog, tmp_lake: Path) -> None:
    for symbol in ("SPY", "QQQ", "IWM"):
        call, put = await seed(fake_catalog, tmp_lake, symbol)
        for contract, empty in ((call, False), (put, True)):
            url = f"https://api.polygon.io/v2/aggs/ticker/O:{contract.occ_symbol}/range/1/minute/{DAY}/{DAY}"
            if symbol == "SPY":
                response = httpx.Response(401, json={"status": "NOT_AUTHORIZED", "message": "key unavailable"})
            else:
                response = no_trades(contract.occ_symbol) if empty else traded(contract.occ_symbol,
                    [{"t": session_open_ms_utc(DAY), "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}])
            respx.get(url).mock(return_value=response)
    state = ArchiveState(tmp_lake / "state.json")
    state.update(lambda data: data.update(baseline_ms=session_open_ms_utc(DAY)))
    service = OptionArchiveService(config=ArchiveSettings(ENABLED=True), state=state, clock=lambda: NOW)
    await service.capture_pass()
    health = service.health()
    spy, qqq, iwm = health.underlyings
    assert spy.severity == "failed" and "provider_auth_error" in spy.last_failure
    assert qqq.pending_sessions == iwm.pending_sessions == 0
    assert qqq.last_archived_session_ms == iwm.last_archived_session_ms == session_open_ms_utc(DAY)
    for symbol in ("QQQ", "IWM"):
        session = await asyncio.to_thread(read_option_session, symbol, DAY)
        assert session.complete and len(session.captured) == len(session.confirmed_empty) == 1
    requested_spy = [call for call in respx.calls if "/ticker/O:SPY" in str(call.request.url)]
    assert len(requested_spy) == 1


async def test_window_rollover_records_permanent_gaps_once_and_never_erases_them(tmp_path: Path) -> None:
    state = ArchiveState(tmp_path / "state.json")
    baseline = session_open_ms_utc(date(2024, 10, 18))
    state.update(lambda data: data.update(baseline_ms=baseline))
    current = wall_clock_ms(date(2026, 10, 21), 600)
    service = OptionArchiveService(config=ArchiveSettings(ENABLED=True), state=state, clock=lambda: current)
    await service.capture_pass()
    original = state.read()["sessions"]["SPY"][str(baseline)]
    assert original["lost"] and not original["complete"]
    current = wall_clock_ms(date(2026, 10, 22), 600)
    await service.capture_pass()
    assert state.read()["sessions"]["SPY"][str(baseline)] == original
