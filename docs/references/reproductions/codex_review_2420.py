"""Offline characterizations for research #2420; no production changes.

Run only through codex_review_2420_guard.py with a sanitized environment.
These assert the observed baseline behavior, including the defects, rather
than claiming a fix. Every lake byte and catalog response is synthetic.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest

from app.data_lake import ensure_data as ensure
from app.data_lake.factor_files import build_factor_file_bytes
from app.data_lake.lean_writer import MinuteTradeBar, build_minute_trade_zip_bytes
from app.data_lake.map_files import build_map_file_bytes
from app.data_lake.path_policy import LeanFactorFilePath, LeanMapFilePath, LeanMinuteBarPath
from app.data_lake.polygon_corp_actions import SplitEvent
from app.data_lake.types import ArtifactIdentity, ArtifactRecord, DataRunSpec, trading_date_to_calendar_anchor_ms
from app.engine.data.lean_format import LeanMinuteDataReader
from app.lean_sidecar.lake_mount import _existing_corporate_action_files
from app.lean_sidecar.trading_calendar import session_windows_ms_utc
from app.services import return_distribution_service as study


ROOT_ID = UUID("00000000-0000-0000-0000-000000002420")
SYMBOL = "SYNTH"
ET = ZoneInfo("America/New_York")
START = date(2024, 5, 1)
NARROW_END = date(2024, 5, 31)
END = date(2024, 6, 28)
SPLIT_DAY = date(2024, 6, 3)


def _ms(day: date) -> int:
    return int(datetime.combine(day, datetime.min.time(), timezone.utc).timestamp() * 1000)


def _spec(mode: str = "raw") -> DataRunSpec:
    return DataRunSpec(
        request_id=uuid4(), run_type="chart", requester="offline-review-2420",
        symbols=[SYMBOL], start_trading_date_ms=trading_date_to_calendar_anchor_ms(START),
        end_trading_date_ms=trading_date_to_calendar_anchor_ms(END),
        price_adjustment_mode=mode, lean_image_digest="sha256:" + "0" * 64,
    )


def _record(identity: ArtifactIdentity, relative: str, payload: bytes, dch: str) -> ArtifactRecord:
    return ArtifactRecord(
        **identity.model_dump(), id=2420, data_contract_hash=dch, file_path=relative,
        file_sha256=sha256(payload).hexdigest(), row_count=2, first_bar_start_ms=0,
        last_bar_start_ms=0, file_size_bytes=len(payload),
    )


def _seed_day(root: Path, day: date, value: Decimal) -> Path:
    window = session_windows_ms_utc(day, day)[0]
    # All RTH minutes, including exact open and close: the test does not
    # rely on the incomplete-anchor defect documented in the first pass.
    bars = [
        MinuteTradeBar(
            datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(ET),
            value, value, value, value, 10,
        )
        for ms in range(window.open_ms_utc, window.close_ms_utc, 60_000)
    ]
    path = root / LeanMinuteBarPath("usa", SYMBOL, day, "trade").relative_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_minute_trade_zip_bytes(SYMBOL, day.strftime("%Y%m%d"), bars))
    return path


@pytest.mark.asyncio
async def test_study_claims_adjusted_with_a_factor_file_ending_before_the_split(tmp_path):
    windows = session_windows_ms_utc(START - timedelta(days=14), END)
    closes = {}
    for window in windows:
        value = Decimal(100 if window.session_date < SPLIT_DAY else 50)
        closes[window.session_date] = value
        _seed_day(tmp_path, window.session_date, value)
    factor = tmp_path / LeanFactorFilePath("usa", SYMBOL).relative_path()
    factor.parent.mkdir(parents=True, exist_ok=True)

    def write_factors(end):
        factor.write_bytes(build_factor_file_bytes(
            SYMBOL, [SplitEvent(SPLIT_DAY.isoformat(), 1, 2)], [],
            START, end, closes,
        ))

    async def compute():
        return await study.compute_return_distribution(
            symbol=SYMBOL, from_ms_utc=_ms(START), to_ms_utc=_ms(END),
            bin_width_pct=0.5, span_pct=5, lake_root=tmp_path,
        )

    capture = AsyncMock(side_effect=AssertionError("capture must be stubbed/offline"))
    with patch.object(study, "_capture_missing_sessions", capture):
        write_factors(NARROW_END)
        narrow = await compute()
        write_factors(END)
        complete = await compute()
    capture.assert_not_awaited()
    assert narrow.capture.status == "not_attempted"
    assert narrow.result.adjustment == complete.result.adjustment == "split_and_dividend"
    assert narrow.missing_sessions == narrow.excluded_sessions == 0
    assert narrow.warnings == []
    narrow_day = next(day for day in narrow.result.days if day.trading_date == SPLIT_DAY)
    complete_day = next(day for day in complete.result.days if day.trading_date == SPLIT_DAY)
    # Exact synthetic identity oracle: 100 raw / two shares == 50 raw.
    assert narrow_day.close_to_close_pct == pytest.approx(-50.0, abs=1e-12, rel=0)
    assert complete_day.close_to_close_pct == pytest.approx(0.0, abs=1e-12, rel=0)


@pytest.mark.asyncio
async def test_map_window_is_cached_and_exposed_after_its_end_date(tmp_path):
    identity = ArtifactIdentity(
        artifact_kind="map_file", market="usa", symbol=SYMBOL, provider="polygon",
        price_adjustment_mode="raw", data_root_id=ROOT_ID,
    )
    payload = build_map_file_bytes(SYMBOL, [], START, NARROW_END, "nyse")
    relative = str(LeanMapFilePath("usa", SYMBOL).relative_path())
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    prior = _record(identity, relative, payload, ensure._map_file_dch("raw"))
    fetch = AsyncMock(side_effect=AssertionError("vendor calls forbidden"))
    with (
        patch.object(ensure.catalog_client, "claim_corp_action_artifact", AsyncMock(return_value=None)),
        patch.object(ensure.catalog_client, "select_complete_corp_action_artifact", AsyncMock(return_value=prior)),
        patch.object(ensure, "fetch_ticker_events", fetch),
    ):
        actual, failure, reused = await ensure._process_map_file_artifact(identity, _spec())
    assert actual is prior and failure is None and reused
    fetch.assert_not_awaited()
    assert path.read_bytes().splitlines()[-1] == b"20240531,synth,nyse"
    assert _existing_corporate_action_files(tmp_path, SYMBOL, "map") == (path,)
    # LEAN's official MapFile source treats the last row as DelistingDate
    # and HasData(date > DelistingDate) as false. We do not execute LEAN.
    assert date(2024, 6, 3) > NARROW_END


@pytest.mark.asyncio
async def test_adjusted_capture_reuses_a_pre_split_vintage(tmp_path):
    mode = "polygon_split_adjusted"
    old_day = date(2024, 5, 31)
    old_path = _seed_day(tmp_path, old_day, Decimal(100))
    _seed_day(tmp_path, SPLIT_DAY, Decimal(50))
    identity = ArtifactIdentity(
        artifact_kind="time_series_bars", market="usa", symbol=SYMBOL,
        trading_date=old_day, resolution="minute", data_type="trade", provider="polygon",
        price_adjustment_mode=mode, data_root_id=ROOT_ID,
    )
    prior = _record(identity, str(old_path.relative_to(tmp_path)), old_path.read_bytes(), ensure._minute_trade_dch(mode))
    fetch = AsyncMock(side_effect=AssertionError("vendor calls forbidden"))
    with (
        patch.object(ensure.catalog_client, "claim_minute_bar", AsyncMock(return_value=None)),
        patch.object(ensure.catalog_client, "select_coverage_minute_bars", AsyncMock(return_value=[prior])),
        patch.object(ensure, "fetch_minute_trade_aggregates", fetch),
    ):
        actual, failure, reused = await ensure._process_minute_trade_artifact(identity, _spec(mode))
    assert actual is prior and failure is None and reused
    fetch.assert_not_awaited()
    reader = LeanMinuteDataReader([tmp_path], session="regular")
    previous = reader.read_day(SYMBOL, old_day)[-1].close
    current = reader.read_day(SYMBOL, SPLIT_DAY)[-1].close
    assert previous == 100 and current == 50
    # On a common post-split share basis, both prices would be 50.
    assert (current / previous - 1) * 100 == Decimal(-50)
