"""Field-captured market-data validation for offline replay."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import date
from decimal import Decimal
from pathlib import Path
from zipfile import ZipFile

import pytest

from app.engine.live.live_artifact_io import parquet_row_count
from app.lean_sidecar.trading_calendar import session_window_for_date
from app.schemas.offline_replay import OfflineReplayCreateRequest, OfflineReplaySpeed
from app.services.offline_replay_clock import OfflineReplayClock
from app.services.offline_replay_service import OfflineReplayService

_FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "offline_replay" / "spy-tsla-2026-07-31"
_METADATA = json.loads((_FIXTURE_ROOT / "metadata.json").read_text(encoding="utf-8"))


async def _no_sleep(_: float) -> None:
    return None


def _fast_clock(speed: OfflineReplaySpeed) -> OfflineReplayClock:
    return OfflineReplayClock(speed, sleep=_no_sleep)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _zip_row_count(path: Path) -> int:
    with ZipFile(path) as archive:
        members = archive.namelist()
        assert len(members) == 1
        with archive.open(members[0]) as rows:
            return sum(1 for _ in rows)


def _assert_fixture_archives() -> None:
    for expected in _METADATA["symbols"].values():
        archive_path = _FIXTURE_ROOT / expected["archive"]
        assert _file_sha256(archive_path) == expected["archive_sha256"]
        assert _zip_row_count(archive_path) == expected["archive_bar_count"]


@pytest.mark.asyncio
async def test_field_capture_replays_while_the_next_market_session_is_open(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A completed capture remains deterministic during later market hours."""
    caplog.set_level(logging.WARNING, logger="app.engine.live.live_engine")
    cache_root = _FIXTURE_ROOT / "cache"
    _assert_fixture_archives()
    monkeypatch.setenv("LEAN_DATA_CACHE", str(cache_root))
    monkeypatch.setenv("LEAN_DATA_ROOT", str(tmp_path / "no-reference-data"))

    # 2026-08-03 10:00 America/New_York: the next NYSE session is in progress.
    market_hours_window = session_window_for_date(date(2026, 8, 3))
    simulated_market_hours_ms = market_hours_window.open_ms_utc + 30 * 60_000
    service = OfflineReplayService(
        root=tmp_path / "artifacts",
        now_ms=lambda: simulated_market_hours_ms,
        clock_factory=_fast_clock,
    )

    created = await service.create_session(
        OfflineReplayCreateRequest(
            session_date_ms=_METADATA["session_open_ms"],
            playback_minutes=60,
            speed=60,
            auto_fetch=False,
        )
    )

    for _ in range(1_000):
        session = service.get_session(created.session_id)
        if session.status in {"completed", "failed", "stopped"}:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("offline replay did not finish within 10 seconds")

    assert session.status == "completed", session.failure_message
    assert session.failure_code is None
    assert market_hours_window.open_ms_utc < simulated_market_hours_ms < market_hours_window.close_ms_utc
    assert session.session_close_ms < simulated_market_hours_ms
    expected_symbols = _METADATA["symbols"]
    assert {bot.symbol for bot in session.bots} == set(expected_symbols)
    replay_bar_counts = {expected["replay_bar_count"] for expected in expected_symbols.values()}
    assert len(replay_bar_counts) == 1
    replay_bar_count = replay_bar_counts.pop()
    assert session.playhead_ms == _METADATA["session_open_ms"] + replay_bar_count * 60_000

    for bot in session.bots:
        expected = expected_symbols[bot.symbol]
        assert bot.status == "completed"
        assert bot.failure_code is None
        assert bot.bars_processed == expected["replay_bar_count"]
        assert bot.data_sha256 == expected["replay_data_sha256"]
        assert bot.decisions_emitted == expected["expected_decisions"]
        assert bot.orders_submitted == expected["expected_orders"]
        assert bot.fills_observed == expected["expected_fills"]
        assert bot.open_position_quantity == 0
        assert bot.final_equity_usd == Decimal(expected["expected_final_equity_usd"])

        bot_root = tmp_path / "artifacts" / session.session_id / "bots" / bot.symbol.lower()
        assert parquet_row_count(bot_root / "input_bars.parquet") == expected["replay_bar_count"]
        assert parquet_row_count(bot_root / "decisions.parquet") == expected["expected_decisions"]
        assert parquet_row_count(bot_root / "equity_curve.parquet") == expected["replay_bar_count"]
