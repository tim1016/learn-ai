"""Synthetic, offline characterization of Stocks review findings at 10b5f31b.

Run with the review guarded launcher from this clone's PythonDataService.
Tests assert the observed defects, not proposed replacement behavior.
"""
from __future__ import annotations

import asyncio
import io
import math
import tempfile
import unittest
import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID
from zoneinfo import ZoneInfo

import pandas as pd

from app.data_lake import ensure_data as lake
from app.data_lake.factor_files import FactorRow
from app.data_lake.polygon_fetcher import PolygonBar
from app.data_lake.types import ArtifactIdentity
from app.engine.data.lean_format import _parse_csv_bytes, write_lean_day_zip
from app.engine.data.trade_bar import TradeBar
from app.lean_sidecar.trading_calendar import session_windows_ms_utc
from app.models.requests import DatasetGenerationRequest
from app.routers import dataset
from app.services import chart_service as chart
from app.services import return_distribution_service as returns
from app.services.dataset_plan_service import prepare_generation_request

ET = ZoneInfo("America/New_York")


def minute_bars(day: date, price: float, count: int = 390) -> list[dict]:
    start = int(datetime(day.year, day.month, day.day, 9, 30, tzinfo=ET).timestamp() * 1000)
    return [
        {"timestamp": start + i * 60_000, "open": price, "high": price,
         "low": price, "close": price, "volume": 1, "transactions": 1}
        for i in range(count)
    ]


class StocksCharacterization(unittest.TestCase):
    def test_same_workspace_window_exports_previous_session(self) -> None:
        start = int(datetime(2024, 7, 2, tzinfo=UTC).timestamp() * 1000)
        end = start + 86_400_000 - 1
        request = DatasetGenerationRequest(
            ticker="SYNTH", from_date="2024-07-02", to_date="2024-07-02",
            start_ms_utc=start, end_ms_utc=end, session="rth", warmup=False,
            include_previous_close=False,
        )
        prepared = prepare_generation_request(request)
        self.assertEqual(prepared.from_date, "2024-07-01")
        self.assertEqual(chart.resolve_request_dates(None, None, start, end),
                         ("2024-07-02", "2024-07-02"))
        bars = minute_bars(date(2024, 7, 1), 100) + minute_bars(date(2024, 7, 2), 200)
        with patch.object(dataset, "fetch_bars_chunked", return_value=bars):
            frame, _, _ = dataset._fetch_and_process(prepared)
        self.assertEqual(len(frame), 780)
        self.assertLess(int(frame.timestamp.min()), start)
        self.assertEqual(int((frame.timestamp < start).sum()), 390)

    def test_chart_discards_warmup_before_ema(self) -> None:
        bars = minute_bars(date(2024, 7, 1), 100) + minute_bars(date(2024, 7, 2), 200)
        chart._resample_cache.clear()
        chart._indicator_cache.clear()
        with patch.object(chart, "_fetch_chart_bars", return_value=(bars, {})) as fetch:
            result = chart.get_chart_data(
                "SYNTH", "2024-07-02", "2024-07-02", "1m", session="rth",
                indicators=[{"name": "ema", "params": {"length": 5}}],
            )
        self.assertLess(fetch.call_args.args[1], "2024-07-02")
        data = result["indicators"][0]["data"]
        self.assertIsNone(data[0]["value"])
        self.assertEqual(data[4]["value"], 200)
        # Independent EMA recurrence: after a constant-100 warmup, five
        # observations at 200 leave (2/3)^5 of the original difference.
        expected = 200 - 100 * (2 / 3) ** 5
        self.assertTrue(math.isclose(expected, 186.83127572016463, rel_tol=0, abs_tol=1e-9))
        self.assertGreater(abs(data[4]["value"] - expected), 13)

    def test_rth_four_hour_bars_do_not_start_at_session_open(self) -> None:
        frame = pd.DataFrame(minute_bars(date(2024, 7, 2), 100))
        actual = chart._resample_bars(frame, "4h", "rth")
        labels = [datetime.fromtimestamp(t / 1000, tz=ET).strftime("%H:%M") for t in actual.timestamp]
        self.assertEqual(labels, ["08:30", "12:30"])
        self.assertEqual(actual.volume.tolist(), [180, 210])
        # A session-anchored 4h grid is 09:30 / 13:30 (240 / 150 minutes).
        self.assertNotEqual(actual.volume.tolist(), [240, 150])

    def test_lake_publishes_wrong_day_impossible_duplicate_bars(self) -> None:
        day = date(2024, 7, 2)
        # Requested July 2, but vendor fixture contains July 3. OHLC envelope
        # and volume are also impossible, and timestamps are duplicated.
        wrong_ms = minute_bars(date(2024, 7, 3), 100, 1)[0]["timestamp"]
        bad = PolygonBar(wrong_ms, 100, 90, 110, 120, -1, 100, 1)
        identity = ArtifactIdentity(
            artifact_kind="time_series_bars", market="usa", symbol="SYNTH",
            trading_date=day, resolution="minute", data_type="trade",
            provider="polygon", price_adjustment_mode="raw",
            data_root_id=UUID(int=1),
        )
        publication = AsyncMock(return_value=("a" * 64, None))
        with tempfile.TemporaryDirectory(prefix="stocks-2414-") as temp:
            root = Path(temp)
            with (
                patch.object(lake.catalog_client, "claim_minute_bar", AsyncMock(return_value=1)),
                patch.object(lake, "fetch_minute_trade_aggregates", AsyncMock(return_value=[bad, bad])),
                patch.object(lake, "_writable_lake_roots", return_value=(root, root)),
                patch.object(lake, "_publish_under_lease", publication),
            ):
                record, failure, _ = asyncio.run(lake._process_minute_trade_artifact(
                    identity, SimpleNamespace(price_adjustment_mode="raw")
                ))
        self.assertIsNone(failure)
        self.assertEqual(record.row_count, 2)
        publication.assert_awaited_once()
        payload = publication.call_args.kwargs["payload"]
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            parsed = _parse_csv_bytes(archive.read(archive.namelist()[0]), "SYNTH", day)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0].start_ms, parsed[1].start_ms)
        self.assertEqual(parsed[0].start_ms, wrong_ms - 86_400_000)
        self.assertLess(parsed[0].high, parsed[0].low)
        self.assertEqual(parsed[0].volume, -1)

    def test_return_study_treats_noon_only_capture_as_complete_sessions(self) -> None:
        start, end = date(2024, 5, 1), date(2024, 6, 28)
        windows = session_windows_ms_utc(start, end)
        self.assertGreaterEqual(len(windows), 31)
        with tempfile.TemporaryDirectory(prefix="stocks-2414-") as temp:
            root = Path(temp) / "partial"
            complete_root = Path(temp) / "complete"
            for window in windows:
                noon = window.open_ms_utc + 150 * 60_000
                bar = TradeBar(symbol="SYNTH", start_ms=noon, end_ms=noon + 60_000,
                               open=Decimal(100), high=Decimal(101), low=Decimal(100),
                               close=Decimal(101), volume=1)
                write_lean_day_zip(root, "SYNTH", window.session_date, [bar])
                first = TradeBar(symbol="SYNTH", start_ms=window.open_ms_utc,
                                 end_ms=window.open_ms_utc + 60_000,
                                 open=Decimal(90), high=Decimal(90), low=Decimal(90),
                                 close=Decimal(90), volume=1)
                last = TradeBar(symbol="SYNTH", start_ms=window.close_ms_utc - 60_000,
                                end_ms=window.close_ms_utc,
                                open=Decimal(110), high=Decimal(110), low=Decimal(110),
                                close=Decimal(110), volume=1)
                write_lean_day_zip(complete_root, "SYNTH", window.session_date, [first, bar, last])
            with patch.object(returns, "read_factor_rows", return_value=[
                FactorRow(date(2050, 1, 1), Decimal(1), Decimal(1))
            ]):
                outcome = returns._compute_sync(
                    symbol="SYNTH", from_date=start, to_date=end,
                    bin_width_pct=0.25, span_pct=5, lake_root=root,
                    capture=returns.CaptureReceipt("not_attempted", 0),
                )
                complete = returns._compute_sync(
                    symbol="SYNTH", from_date=start, to_date=end,
                    bin_width_pct=0.25, span_pct=5, lake_root=complete_root,
                    capture=returns.CaptureReceipt("not_attempted", 0),
                )
        self.assertEqual(outcome.missing_sessions, 0)
        self.assertEqual(outcome.excluded_sessions, 0)
        self.assertEqual(len(outcome.result.days), len(windows))
        session = next(kind for kind in outcome.result.kinds if kind.kind == "session")
        self.assertTrue(math.isclose(session.stats.mean_pct, 1, rel_tol=0, abs_tol=1e-9))
        complete_session = next(kind for kind in complete.result.kinds if kind.kind == "session")
        self.assertTrue(math.isclose(complete_session.stats.mean_pct, (110 / 90 - 1) * 100,
                                     rel_tol=0, abs_tol=1e-9))
        self.assertFalse(any("partial" in warning or "minute" in warning for warning in outcome.warnings))


if __name__ == "__main__":
    unittest.main(verbosity=2)
