from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx

from app.data_lake.ensure_data import _minute_trade_dch
from app.data_lake.option_lake import (
    MAX_VENDOR_REQUESTS_IN_FLIGHT,
    OptionCaptureResult,
    OptionDataFailure,
    option_history_floor,
)
from app.lean_sidecar.trading_calendar import expected_sessions, session_open_ms_utc, session_window_for_date
from app.option_archive import service as service_module
from app.option_archive.planner import UNDERLYINGS, SessionOutcome, archive_history_boundary
from app.option_archive.service import OptionArchiveService
from app.option_archive.settings import ArchiveSettings
from app.option_archive.state import ArchiveState, ArchiveStateDoc
from app.options.contract import OptionContract
from app.options_backtest.option_session_capture import read_session_completeness
from app.services.session_authority import et_minute_of_day_ms
from tests._helpers.fake_lake_catalog import FakeCatalog
from tests._helpers.option_vendor import no_trades, outside_plan, traded
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import stock_bar, store_listing, store_underlying_day, vendor_listing_row
from tests.options_backtest._lake import tmp_lake as tmp_lake

DAY = date(2026, 10, 9)
NOW = et_minute_of_day_ms(date(2026, 10, 12), 120)


class Owned:
    @asynccontextmanager
    async def hold(self, name: str) -> AsyncIterator[bool]:
        yield True


def with_baseline(state: ArchiveState, baseline_ms: int) -> ArchiveState:
    def seed(doc: ArchiveStateDoc) -> None:
        doc.baseline_ms = baseline_ms
    state.update(seed)
    return state


async def seed(fake_catalog: FakeCatalog, tmp_lake: Path, underlying: str, *, day: date = DAY) -> tuple[OptionContract, OptionContract]:
    call = OptionContract.of(root=underlying, expiry_date=day, right="call", strike=500)
    put = OptionContract.of(root=underlying, expiry_date=day, right="put", strike=500)
    await store_listing([vendor_listing_row(call, underlying=underlying), vendor_listing_row(put, underlying=underlying)],
                        underlying=underlying, day=day)
    await store_underlying_day(fake_catalog, tmp_lake, [stock_bar(session_open_ms_utc(day), "500", "500", "500", "500", symbol=underlying)],
                               symbol=underlying, day=day)
    for row in fake_catalog.rows.values():
        if row["market"] == "usa" and row["symbol"] == underlying and row["trading_date"] == day:
            row["data_contract_hash"] = _minute_trade_dch("raw", None, trading_date=day)
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
    state = with_baseline(ArchiveState(tmp_lake / "state.json"), session_open_ms_utc(DAY))
    service = OptionArchiveService(config=ArchiveSettings(ENABLED=True), state=state, clock=lambda: NOW)
    await service.capture_pass()
    health = service.health()
    spy, qqq, iwm = health.underlyings
    assert spy.severity == "failed" and "provider_auth_error" in spy.last_failure
    assert qqq.pending_sessions == iwm.pending_sessions == 0
    assert qqq.last_archived_session_ms == iwm.last_archived_session_ms == session_open_ms_utc(DAY)
    for symbol in ("QQQ", "IWM"):
        session, complete = await asyncio.to_thread(read_session_completeness, symbol, DAY)
        assert complete and len(session.captured) == len(session.confirmed_empty) == 1
    requested_spy = [str(call.request.url) for call in respx.calls if "/ticker/O:SPY" in str(call.request.url)]
    # The refusal stops new requests; tickers already in flight each asked once.
    assert requested_spy and len(set(requested_spy)) == len(requested_spy) <= MAX_VENDOR_REQUESTS_IN_FLIGHT


async def test_window_rollover_records_permanent_gaps_once_and_never_erases_them(tmp_path: Path) -> None:
    baseline = session_open_ms_utc(date(2024, 10, 18))
    state = with_baseline(ArchiveState(tmp_path / "state.json"), baseline)
    current = et_minute_of_day_ms(date(2026, 10, 21), 600)
    service = OptionArchiveService(config=ArchiveSettings(ENABLED=True), state=state, clock=lambda: current)
    await service.capture_pass()
    original = state.read().sessions["SPY"][baseline]
    assert original.outcome is SessionOutcome.LOST and not original.complete
    current = et_minute_of_day_ms(date(2026, 10, 22), 600)
    await service.capture_pass()
    assert state.read().sessions["SPY"][baseline] == original


async def mount_run(fake_catalog, tmp_lake: Path, *, spy_status: int) -> None:
    for symbol in ("SPY", "QQQ", "IWM"):
        call, put = await seed(fake_catalog, tmp_lake, symbol)
        for contract in (call, put):
            url = f"https://api.polygon.io/v2/aggs/ticker/O:{contract.occ_symbol}/range/1/minute/{DAY}/{DAY}"
            response = httpx.Response(spy_status, json={"status": "ERROR"}) if symbol == "SPY" and spy_status != 200 else no_trades(contract.occ_symbol)
            respx.get(url).mock(return_value=response)


def service_for(state_path: Path, clock) -> OptionArchiveService:
    state = with_baseline(ArchiveState(state_path), session_open_ms_utc(DAY))
    return OptionArchiveService(config=ArchiveSettings(ENABLED=True), state=state, clock=clock, ownership=Owned())


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
    assert recovered is SessionOutcome.COMPLETE
    spy = service.health().underlyings[0]
    assert spy.pending_sessions == 0 and spy.last_failure is None
    assert service.state.read().sessions["SPY"][session_open_ms_utc(DAY)].complete


@respx.mock
async def test_unadmitted_container_fails_only_its_underlying_and_is_visible(fake_catalog, tmp_lake: Path) -> None:
    from app.data_lake.path_policy import OptionSessionPath
    await mount_run(fake_catalog, tmp_lake, spy_status=200)
    from app.options_backtest.option_session_capture import capture_option_session
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


async def test_cancelling_a_pass_leaves_a_manual_recovery_it_did_not_start_running(tmp_path: Path) -> None:
    entered = {"SPY": asyncio.Event(), "IWM": asyncio.Event()}
    stopped: set[str] = set()
    async def collector(symbol, day, **kwargs):
        entered[symbol].set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.add(symbol)
    service = service_for(tmp_path / "state.json", lambda: NOW)
    service.capture = collector
    manual = asyncio.create_task(service.capture_session("IWM", session_open_ms_utc(DAY)))
    await entered["IWM"].wait()
    scheduled = asyncio.create_task(service.capture_pass())
    await entered["SPY"].wait()
    scheduled.cancel()
    await asyncio.gather(scheduled, return_exceptions=True)
    assert stopped == {"SPY"} and not manual.done()
    await service.stop()
    await asyncio.gather(manual, return_exceptions=True)
    assert stopped == {"SPY", "IWM"}


async def test_a_failed_snapshot_pass_waits_out_the_retry_backoff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = session_window_for_date(DAY).close_ms_utc + 1_800_000
    service = OptionArchiveService(config=ArchiveSettings(ENABLED=True), state=ArchiveState(tmp_path / "state.json"),
                                   clock=lambda: now, ownership=Owned())
    async def unavailable(*args, **kwargs):
        raise RuntimeError("the evening snapshot could not be stored")
    monkeypatch.setattr(service_module, "observe_chain", unavailable)
    assert service.snapshot_due(now)
    with pytest.raises(RuntimeError, match="could not be stored"):
        await service.scheduler.trigger("snapshot")
    receipt = service.state.read().job("snapshot")
    assert receipt.failure == "the evening snapshot could not be stored" and receipt.retry_after_ms == now + 300_000
    assert not service.snapshot_due(now) and service.snapshot_due(now + 300_000)


@respx.mock
async def test_full_staging_budget_pauses_the_pass_and_remains_actionable(
    fake_catalog, tmp_lake: Path,
) -> None:
    calls = []
    detail = "Archive staging capacity reached; review disk space and raise OPTION_ARCHIVE_STAGING_MAX_BYTES."
    async def capture(symbol, day, **kwargs):
        calls.append((symbol, day))
        return OptionCaptureResult.failed(OptionDataFailure("staging_capacity", detail, symbol, day))
    service = service_for(tmp_lake / "state.json", lambda: NOW)
    service.capture = capture
    await service.scheduler.trigger("capture")
    assert calls == [("SPY", DAY)]
    receipt = service.state.read().job("capture")
    assert receipt.phase == "capacity_pause" and receipt.completed_day_ms is None
    assert not service.capture_due(NOW + 1)
    assert service.capture_due(NOW + 300_001)
    assert service.state.read().sessions["SPY"][session_open_ms_utc(DAY)].outcome is SessionOutcome.CAPACITY_PAUSED
    assert not service.state.read().provider_blocks
    assert all(item.severity == "at_risk" and item.phase == "capacity_pause" and detail in item.reason
               for item in service.health().underlyings)
    await mount_run(fake_catalog, tmp_lake, spy_status=200)
    service.capture = None
    assert await service.capture_session("SPY", session_open_ms_utc(DAY)) is SessionOutcome.COMPLETE
    spy = service.health().underlyings[0]
    assert spy.phase == "waiting" and spy.severity == "healthy"


def complete_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.options_backtest import option_session_capture
    monkeypatch.setattr(option_session_capture, "read_session_completeness", lambda *args, **kwargs: (SimpleNamespace(
        listed_count=1, answered_count=1, confirmed_empty=(), captured=("one",)), True))


async def test_a_session_the_plan_no_longer_serves_is_a_lost_gap_not_a_standing_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    complete_sessions(monkeypatch)
    async def capture(symbol, day, **kwargs):
        if symbol == "SPY":
            return OptionCaptureResult.failed(OptionDataFailure("outside_data_plan", "the plan no longer serves it", symbol, day))
        return SimpleNamespace(failure=None)
    service = service_for(tmp_path / "state.json", lambda: NOW)
    service.capture = capture
    await service.capture_pass()
    spy = service.health().underlyings[0]
    assert spy.severity == "at_risk" and spy.last_failure is None and spy.lost_sessions == 1


@pytest.mark.parametrize("blocked_reason", ["provider_auth_error", "provider_entitlement_error"])
async def test_provider_block_survives_other_retries_and_new_nights_until_correct_session_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocked_reason: str,
) -> None:
    complete_sessions(monkeypatch)
    first = date(2026, 10, 8)
    calls = []
    spy_failure = blocked_reason
    async def capture(symbol, day, **kwargs):
        calls.append((symbol, day))
        reason = spy_failure if symbol == "SPY" else "provider_rate_limited"
        return OptionCaptureResult.failed(OptionDataFailure(reason, "provider response", symbol, day)) if reason else SimpleNamespace(failure=None)
    current = NOW
    service = service_for(tmp_path / "state.json", lambda: current)
    with_baseline(service.state, session_open_ms_utc(first))
    service.capture = capture
    await service.capture_pass()
    assert [day for symbol, day in calls if symbol == "SPY"] == [first]
    current += 300_001
    assert service.capture_due(current)
    await service.capture_pass()
    current = et_minute_of_day_ms(date(2026, 10, 13), 120)
    await service.capture_pass()
    assert [day for symbol, day in calls if symbol == "SPY"] == [first]
    await service.capture_session("SPY", session_open_ms_utc(DAY), retry_exhausted=True)
    assert [day for symbol, day in calls if symbol == "SPY"] == [first]
    spy_failure = "provider_rate_limited"
    await service.capture_session("SPY", session_open_ms_utc(first), retry_exhausted=True)
    assert service.state.read().provider_blocks["SPY"].reason == blocked_reason
    spy_failure = None
    await service.capture_session("SPY", session_open_ms_utc(first), retry_exhausted=True)
    assert "SPY" not in service.state.read().provider_blocks
    await service.capture_pass()
    assert ("SPY", DAY) in calls


@respx.mock
async def test_missed_night_catches_up_at_the_vendor_edge_and_restart_preserves_peers_and_lost_coverage(
    fake_catalog: FakeCatalog, tmp_lake: Path,
) -> None:
    today = date(2026, 10, 21)
    days = expected_sessions(date(2024, 10, 21), date(2024, 10, 24))
    edge, *_, paused_day = days
    assert archive_history_boundary(today) == edge < option_history_floor(today)
    current = et_minute_of_day_ms(today, 180)
    metadata: dict[str, tuple[str, date, OptionContract]] = {}
    for day in days:
        for symbol in UNDERLYINGS:
            for contract in await seed(fake_catalog, tmp_lake, symbol, day=day):
                path = f"/v2/aggs/ticker/O:{contract.occ_symbol}/range/1/minute/{day}/{day}"
                metadata[path] = symbol, day, contract
    calls: list[tuple[str, date, str]] = []
    resume = False

    def answer(request: httpx.Request) -> httpx.Response:
        nonlocal current
        symbol, day, contract = metadata[request.url.path]
        calls.append((symbol, day, contract.right))
        if symbol == "SPY":
            return httpx.Response(401, json={"status": "NOT_AUTHORIZED", "message": "key unavailable"})
        if symbol == "IWM" and day == edge:
            return outside_plan()
        if day == paused_day and symbol == ("IWM" if resume else "QQQ"):
            if resume:
                current = session_window_for_date(today).close_ms_utc + config.SNAPSHOT_DELAY_MS
            elif contract.right == "put":
                current = session_window_for_date(today).open_ms_utc
        return (no_trades(contract.occ_symbol) if contract.right == "put" else
                traded(contract.occ_symbol, [{"t": session_open_ms_utc(day), "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]))

    respx.get(url__regex=r"https://api\.polygon\.io/v2/aggs/ticker/O:.*").mock(side_effect=answer)
    state = with_baseline(ArchiveState(tmp_lake / "state.json"), session_open_ms_utc(edge))
    config = ArchiveSettings(ENABLED=True)
    service = OptionArchiveService(config=config, state=state, clock=lambda: current)
    await service.capture_pass()
    assert state.read().job("capture").phase == "rth_pause"
    assert [day for _, day, _ in calls] == sorted(day for _, day, _ in calls)
    assert {symbol for symbol, day, _ in calls if day == edge} == set(UNDERLYINGS)
    assert {(symbol, day) for symbol, day, _ in calls if symbol == "SPY"} == {("SPY", edge)}
    for day in days[:-1]:
        for symbol in ("QQQ", "IWM"):
            stored, complete = await asyncio.to_thread(read_session_completeness, symbol, day)
            if symbol == "IWM" and day == edge:
                assert stored is None and not complete
            else:
                assert complete and stored is not None and len(stored.captured) == len(stored.confirmed_empty) == 1
    lost = state.read().sessions["IWM"][session_open_ms_utc(edge)]
    assert lost.outcome is SessionOutcome.LOST and lost.reason is not None and lost.reason.startswith("outside_data_plan:")
    assert lost.recorded_ms == et_minute_of_day_ms(today, 180)
    assert (await asyncio.to_thread(read_session_completeness, "QQQ", paused_day)) == (None, False)

    # Reopen only the durable state. The real collector resumes its validated
    # ticker files without fetching them again, then yields to the evening window.
    previous_calls = list(calls)
    current = session_window_for_date(today).close_ms_utc + 60_000
    resume = True
    restarted = OptionArchiveService(config=config, state=ArchiveState(state.path), clock=lambda: current)
    await restarted.capture_pass()
    # The evening window opens on IWM's call; its put, already in flight, still answers.
    resumed = calls[len(previous_calls):]
    assert ("IWM", paused_day, "call") in resumed and set(resumed) <= {("IWM", paused_day, "call"), ("IWM", paused_day, "put")}
    assert restarted.state.read().job("capture").phase == "snapshot_pause"
    _, complete = await asyncio.to_thread(read_session_completeness, "QQQ", paused_day)
    assert complete
    before_retry = list(calls)
    await restarted.capture_pass()
    assert calls == before_retry
    assert restarted.state.read().sessions["IWM"][session_open_ms_utc(edge)] == lost

    current = session_window_for_date(today + timedelta(days=1)).open_ms_utc
    await restarted.capture_pass()
    assert calls == before_retry
    assert restarted.state.read().sessions["IWM"][session_open_ms_utc(edge)] == lost
    assert restarted.health().underlyings[2].lost_sessions == 1
    _, retained_complete = await asyncio.to_thread(read_session_completeness, "QQQ", edge)
    assert retained_complete


