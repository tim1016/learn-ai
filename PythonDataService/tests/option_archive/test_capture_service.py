from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import httpx
import pytest
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


async def mount_run(fake_catalog, tmp_lake: Path, *, spy_status: int) -> None:
    for symbol in ("SPY", "QQQ", "IWM"):
        call, put = await seed(fake_catalog, tmp_lake, symbol)
        for contract in (call, put):
            url = f"https://api.polygon.io/v2/aggs/ticker/O:{contract.occ_symbol}/range/1/minute/{DAY}/{DAY}"
            response = httpx.Response(spy_status, json={"status": "ERROR"}) if symbol == "SPY" and spy_status != 200 else no_trades(contract.occ_symbol)
            respx.get(url).mock(return_value=response)


def service_for(state_path: Path, clock) -> OptionArchiveService:
    state = ArchiveState(state_path)
    state.update(lambda data: data.update(baseline_ms=session_open_ms_utc(DAY)))
    return OptionArchiveService(config=ArchiveSettings(ENABLED=True), state=state, clock=clock)


@respx.mock
async def test_transient_failure_retries_within_the_same_night_and_manual_recovery_updates_health(fake_catalog, tmp_lake: Path) -> None:
    await mount_run(fake_catalog, tmp_lake, spy_status=429)
    current = NOW
    service = service_for(tmp_lake / "state.json", lambda: current)
    await service.capture_pass()
    current += 300_001
    assert service.capture_due(current), "a transient edge failure must remain eligible tonight"
    for contract in (OptionContract.of(root="SPY", expiry_date=DAY, right="call", strike=500),
                     OptionContract.of(root="SPY", expiry_date=DAY, right="put", strike=500)):
        respx.get(f"https://api.polygon.io/v2/aggs/ticker/O:{contract.occ_symbol}/range/1/minute/{DAY}/{DAY}").mock(return_value=no_trades(contract.occ_symbol))
    recovered = await service.capture_session("SPY", session_open_ms_utc(DAY), retry_exhausted=True)
    assert recovered.failure is None
    spy = service.health().underlyings[0]
    assert spy.pending_sessions == 0 and spy.last_failure is None
    assert service.state.read()["sessions"]["SPY"][str(session_open_ms_utc(DAY))]["complete"]


@respx.mock
async def test_unadmitted_container_fails_only_its_underlying_and_is_visible(fake_catalog, tmp_lake: Path) -> None:
    from app.data_lake.path_policy import OptionSessionPath
    await mount_run(fake_catalog, tmp_lake, spy_status=200)
    from app.data_lake.option_session_capture import capture_option_session
    assert (await capture_option_session("SPY", DAY, now_ms=NOW)).failure is None
    path = tmp_lake / "lake/raw" / str(OptionSessionPath("SPY", DAY).relative_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"unadmitted container")
    service = service_for(tmp_lake / "state.json", lambda: NOW)
    await service.capture_pass()
    spy, qqq, iwm = service.health().underlyings
    assert spy.severity == "failed" and spy.last_failure
    assert qqq.pending_sessions == iwm.pending_sessions == 0


async def test_owner_cancellation_and_shutdown_drain_the_session_collector(tmp_path: Path) -> None:
    entered, stopped = asyncio.Event(), asyncio.Event()
    async def collector(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    service = service_for(tmp_path / "state.json", lambda: NOW)
    service.capture = collector
    task = asyncio.create_task(service.capture_pass())
    await entered.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert stopped.is_set() and not service._session_tasks
    entered.clear()
    stopped.clear()
    manual = asyncio.create_task(service.capture_session("SPY", session_open_ms_utc(DAY)))
    await entered.wait()
    await service.stop()
    await asyncio.gather(manual, return_exceptions=True)
    assert stopped.is_set() and not service._session_tasks


@pytest.mark.parametrize("blocked_reason", ["provider_auth_error", "provider_entitlement_error"])
async def test_provider_block_survives_other_retries_and_new_nights_until_correct_session_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocked_reason: str,
) -> None:
    from types import SimpleNamespace

    from app.data_lake import option_session
    from app.data_lake.option_lake import OptionCaptureResult, OptionDataFailure
    monkeypatch.setattr(option_session, "read_option_session", lambda *args: SimpleNamespace(
        complete=True, listed_count=1, answered_count=1, confirmed_empty=(), captured=("one",)))
    first = date(2026, 10, 8)
    calls = []
    spy_failure = blocked_reason
    async def capture(symbol, day, **kwargs):
        calls.append((symbol, day))
        reason = spy_failure if symbol == "SPY" else "provider_rate_limited"
        return OptionCaptureResult.failed(OptionDataFailure(reason, "provider response", symbol, day)) if reason else SimpleNamespace(failure=None)
    current = NOW
    service = service_for(tmp_path / "state.json", lambda: current)
    service.state.update(lambda data: data.update(baseline_ms=session_open_ms_utc(first)))
    service.capture = capture
    await service.capture_pass()
    assert [day for symbol, day in calls if symbol == "SPY"] == [first]
    current += 300_001
    assert service.capture_due(current)
    await service.capture_pass()
    current = wall_clock_ms(date(2026, 10, 13), 120)
    await service.capture_pass()
    assert [day for symbol, day in calls if symbol == "SPY"] == [first]
    await service.capture_session("SPY", session_open_ms_utc(DAY), retry_exhausted=True)
    assert [day for symbol, day in calls if symbol == "SPY"] == [first]
    spy_failure = "provider_rate_limited"
    await service.capture_session("SPY", session_open_ms_utc(first), retry_exhausted=True)
    assert service.state.read()["provider_blocks"]["SPY"]["reason"] == blocked_reason
    spy_failure = None
    await service.capture_session("SPY", session_open_ms_utc(first), retry_exhausted=True)
    assert "SPY" not in service.state.read()["provider_blocks"]
    await service.capture_pass()
    assert ("SPY", DAY) in calls
