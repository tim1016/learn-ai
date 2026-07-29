from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.broker.ibkr.models import IbkrMinuteBar
from app.lean_sidecar.trading_calendar import session_window_for_date
from app.schemas.artifact_io import atomic_write_pydantic_artifact
from app.schemas.offline_replay import (
    OfflineReplayCreateRequest,
    OfflineReplaySessionResponse,
)
from app.services.offline_replay_clock import OfflineReplayClock, ReplayTickPermission
from app.services.offline_replay_data import PreparedOfflineReplay
from app.services.offline_replay_service import OfflineReplayService, _BotRecord

_WARMUP_MINUTES = 225


def _stream(symbol: str, *, open_ms: int, count: int) -> tuple[IbkrMinuteBar, ...]:
    bars: list[IbkrMinuteBar] = []
    for index in range(count):
        start_ms = open_ms + index * 60_000
        price = Decimal("100") + Decimal(index) / Decimal("100")
        bars.append(
            IbkrMinuteBar(
                symbol=symbol,
                start_ms=start_ms,
                end_ms=start_ms + 60_000,
                open=price,
                high=price + 1,
                low=price - 1,
                close=price + Decimal("0.5"),
                volume=1_000,
                fetched_at_ms=1,
                source="polygon",
                provenance="polygon_historical",
                venue="POLYGON",
                session_phase="RTH",
                use_rth=True,
            )
        )
    return tuple(bars)


class _PreparedData:
    def __init__(self, prepared: PreparedOfflineReplay) -> None:
        self.prepared = prepared

    def prepare(self, **_: object) -> PreparedOfflineReplay:
        return self.prepared


async def _no_sleep(_: float) -> None:
    return None


def _fast_clock(speed: int) -> OfflineReplayClock:
    return OfflineReplayClock(speed, sleep=_no_sleep)  # type: ignore[arg-type]


class _ScriptedClock:
    """Yield one fixed playback permission per bar; warm-up always runs.

    Popping exactly one permission per playback ``before_tick`` pins the drive
    loop to a single clock check per bar, which is what makes one STEP advance
    one bar. The previous double-``before_tick`` loop consumed two permissions
    per non-first bar, so this script would exhaust and stall before the run
    completed.
    """

    def __init__(self, playback_permissions: list[ReplayTickPermission]) -> None:
        self._script = iter(playback_permissions)
        self.stop_requested = False

    @property
    def speed(self) -> int:
        return 60

    async def before_tick(self, *, warmup: bool) -> ReplayTickPermission:
        if warmup:
            return ReplayTickPermission.RUN
        permission = next(self._script, ReplayTickPermission.STOP)
        if permission is ReplayTickPermission.STOP:
            self.stop_requested = True
        return permission

    async def pace(self, permission: ReplayTickPermission) -> None:
        return None


@pytest.mark.asyncio
async def test_two_symbol_session_runs_canonical_engine_and_persists_evidence(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="app.engine.live.live_engine")
    window = session_window_for_date(date(2026, 7, 28))
    playback_minutes = 30
    total_minutes = _WARMUP_MINUTES + playback_minutes
    prepared = PreparedOfflineReplay(
        session=window,
        warmup_minutes=_WARMUP_MINUTES,
        playback_minutes=playback_minutes,
        warmup_end_ms=window.open_ms_utc + _WARMUP_MINUTES * 60_000,
        playback_end_ms=window.open_ms_utc + total_minutes * 60_000,
        bars_by_symbol={
            "SPY": _stream("SPY", open_ms=window.open_ms_utc, count=total_minutes),
            "TSLA": _stream("TSLA", open_ms=window.open_ms_utc, count=total_minutes),
        },
        data_sha256_by_symbol={"SPY": "spy-digest", "TSLA": "tsla-digest"},
    )
    service = OfflineReplayService(
        data_service=_PreparedData(prepared),  # type: ignore[arg-type]
        root=tmp_path,
        clock_factory=_fast_clock,  # type: ignore[arg-type]
    )

    created = await service.create_session(
        OfflineReplayCreateRequest(
            session_date_ms=window.open_ms_utc,
            playback_minutes=playback_minutes,
            speed=60,
        )
    )
    assert created.status == "preparing"

    for _ in range(500):
        session = service.get_session(created.session_id)
        if session.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("offline replay did not reach a terminal state")

    assert session.status == "completed", session.failure_message
    assert session.playhead_ms == prepared.playback_end_ms
    assert [bot.symbol for bot in session.bots] == ["SPY", "TSLA"]
    assert all(bot.status == "completed" for bot in session.bots)
    assert all(bot.bars_processed == total_minutes for bot in session.bots)
    assert session.bots[0].data_sha256 == "spy-digest"
    assert session.bots[1].data_sha256 == "tsla-digest"

    session_dir = tmp_path / created.session_id
    assert (session_dir / "session.json").is_file()
    for symbol in ("spy", "tsla"):
        bot_dir = session_dir / "bots" / symbol
        assert (bot_dir / "input_bars.parquet").exists()
        assert (bot_dir / "decisions.parquet").exists()
        assert (bot_dir / "equity_curve.parquet").exists()


def test_restart_marks_an_unfinished_durable_session_interrupted(
    tmp_path: Path,
) -> None:
    window = session_window_for_date(date(2026, 7, 28))
    persisted = OfflineReplaySessionResponse(
        session_id="unfinished",
        status="running",
        strategy_key="ema_crossover_signal",
        session_date_ms=window.open_ms_utc,
        session_open_ms=window.open_ms_utc,
        session_close_ms=window.close_ms_utc,
        warmup_end_ms=window.open_ms_utc + _WARMUP_MINUTES * 60_000,
        playback_end_ms=window.open_ms_utc + 285 * 60_000,
        playhead_ms=window.open_ms_utc + 230 * 60_000,
        playback_minutes=60,
        speed=60,
        created_at_ms=1,
        started_at_ms=2,
        completed_at_ms=None,
        symbols=["SPY", "TSLA"],
        bots=[
            {
                "symbol": symbol,
                "status": "running",
                "run_id": f"unfinished-{symbol.lower()}",
                "bars_processed": 230,
                "decisions_emitted": 1,
                "orders_submitted": 0,
                "fills_observed": 0,
                "open_position_quantity": 0,
                "final_equity_usd": Decimal("100000"),
                "data_sha256": f"{symbol.lower()}-digest",
            }
            for symbol in ("SPY", "TSLA")
        ],
    )
    path = tmp_path / persisted.session_id / "session.json"
    atomic_write_pydantic_artifact(path, persisted)

    restarted = OfflineReplayService(
        data_service=_PreparedData,  # type: ignore[arg-type]
        root=tmp_path,
        now_ms=lambda: 3,
    )
    recovered = restarted.get_session("unfinished")

    assert recovered.status == "interrupted"
    assert recovered.completed_at_ms == 3
    assert recovered.failure_code == "SERVICE_RESTARTED"
    # The parent and its bots must agree: no bot may remain "running" once the
    # owning session is archived interrupted (there is no task to advance it).
    assert all(bot.status == "failed" for bot in recovered.bots)
    assert all(bot.failure_code == "SERVICE_RESTARTED" for bot in recovered.bots)
    assert OfflineReplaySessionResponse.model_validate_json(
        path.read_text(encoding="utf-8")
    ).status == "interrupted"


@pytest.mark.asyncio
async def test_single_step_advances_exactly_one_playback_bar(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression: one STEP per playback bar must feed every playback bar.

    The earlier loop called ``before_tick`` twice per non-first playback bar,
    so the step budget was consumed by the first check and the second check
    blocked — a single Step press advanced no bar on every bar but the first.
    With one clock check per bar, a script of exactly one STEP per bar drives
    the run to completion.
    """
    caplog.set_level(logging.WARNING, logger="app.engine.live.live_engine")
    window = session_window_for_date(date(2026, 7, 28))
    playback_bars = 4
    total_minutes = _WARMUP_MINUTES + playback_bars
    prepared = PreparedOfflineReplay(
        session=window,
        warmup_minutes=_WARMUP_MINUTES,
        playback_minutes=playback_bars,
        warmup_end_ms=window.open_ms_utc + _WARMUP_MINUTES * 60_000,
        playback_end_ms=window.open_ms_utc + total_minutes * 60_000,
        bars_by_symbol={
            "SPY": _stream("SPY", open_ms=window.open_ms_utc, count=total_minutes),
            "TSLA": _stream("TSLA", open_ms=window.open_ms_utc, count=total_minutes),
        },
        data_sha256_by_symbol={"SPY": "spy-digest", "TSLA": "tsla-digest"},
    )
    script = [ReplayTickPermission.STEP] * playback_bars
    service = OfflineReplayService(
        data_service=_PreparedData(prepared),  # type: ignore[arg-type]
        root=tmp_path,
        clock_factory=lambda _speed: _ScriptedClock(list(script)),  # type: ignore[arg-type]
    )

    created = await service.create_session(
        OfflineReplayCreateRequest(
            session_date_ms=window.open_ms_utc,
            playback_minutes=30,
            speed=60,
        )
    )

    for _ in range(500):
        session = service.get_session(created.session_id)
        if session.status in {"completed", "failed", "stopped"}:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("offline replay did not reach a terminal state")

    assert session.status == "completed", session.failure_message
    assert session.playhead_ms == prepared.playback_end_ms
    assert all(bot.bars_processed == total_minutes for bot in session.bots)


@pytest.mark.asyncio
async def test_finalize_cancels_a_stuck_bot_and_preserves_a_crash_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crashed bot keeps its real failure code; a stalled bot is cancelled.

    Guards the timeout path: an engine that never settles must not leave the
    replay hanging on an unbounded gather, and a crash must not be relabelled.
    """
    monkeypatch.setattr(
        "app.services.offline_replay_service._BOT_PROCESS_TIMEOUT_S", 0.05
    )
    record = SimpleNamespace(
        bots={
            "SPY": _BotRecord(
                symbol="SPY", run_id="r-spy", initial_cash=Decimal("100000"), status="running"
            ),
            "TSLA": _BotRecord(
                symbol="TSLA", run_id="r-tsla", initial_cash=Decimal("100000"), status="running"
            ),
        }
    )

    async def crash() -> None:
        raise ValueError("engine boom")

    async def hang() -> None:
        await asyncio.Event().wait()

    tasks = {
        "SPY": asyncio.create_task(crash()),
        "TSLA": asyncio.create_task(hang()),
    }

    await OfflineReplayService._finalize_bot_tasks(record, tasks)  # type: ignore[arg-type]

    assert record.bots["SPY"].status == "failed"
    assert record.bots["SPY"].failure_code == "ValueError"
    assert record.bots["TSLA"].status == "failed"
    assert record.bots["TSLA"].failure_code == "BOT_PROCESS_TIMEOUT"
    assert all(task.done() for task in tasks.values())


@pytest.mark.asyncio
async def test_create_session_rejects_a_non_trading_day(tmp_path: Path) -> None:
    service = OfflineReplayService(
        data_service=_PreparedData,  # type: ignore[arg-type]
        root=tmp_path,
    )
    saturday_ms = int(datetime(2026, 7, 25, 13, 30, tzinfo=UTC).timestamp() * 1000)

    with pytest.raises(ValueError, match="completed NYSE session"):
        await service.create_session(
            OfflineReplayCreateRequest(
                session_date_ms=saturday_ms,
                playback_minutes=30,
                speed=60,
            )
        )


@pytest.mark.asyncio
async def test_create_session_rejects_a_session_not_yet_completed(tmp_path: Path) -> None:
    window = session_window_for_date(date(2026, 7, 28))
    service = OfflineReplayService(
        data_service=_PreparedData,  # type: ignore[arg-type]
        root=tmp_path,
        now_ms=lambda: window.open_ms_utc + 1_000,
    )

    with pytest.raises(ValueError, match="has not completed yet"):
        await service.create_session(
            OfflineReplayCreateRequest(
                session_date_ms=window.open_ms_utc,
                playback_minutes=30,
                speed=60,
            )
        )
