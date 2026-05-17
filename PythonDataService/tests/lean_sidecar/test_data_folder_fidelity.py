"""LEAN data-folder round-trip fidelity test.

Per ``docs/architecture/lean-sidecar-lab.md`` §"LEAN data-folder
fidelity" (non-negotiable #9): write a tiny deterministic price series
through ``lean_format.write_lean_day_zip``, then read it back through
``lean_format.LeanMinuteDataReader``. The reader/writer pair is the
contract; if a future change breaks deci-cent encoding or
ms-since-midnight time alignment, this test catches it without needing
a live LEAN container.

A second integration test (``test_runner_e2e.py``) runs the same
fixture through the actual LEAN container when the image is available
and asserts the algorithm's observed prices match the intended dollar
prices within the LEAN quantization floor (atol=0.0001).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.engine.data.lean_format import (
    EASTERN,
    PRICE_SCALE,
    LeanMinuteDataReader,
    write_lean_day_zip,
)
from app.engine.data.trade_bar import TradeBar
from app.lean_sidecar.staging import stage_minute_bars
from app.lean_sidecar.workspace import resolve_workspace


def _make_minute_bars(
    symbol: str,
    trading_date: date,
    *,
    open_price: float = 100.00,
    increment: float = 0.01,
    count: int = 10,
) -> list[TradeBar]:
    """Build a deterministic minute-bar series starting 09:30 ET.

    Prices increment by ``increment`` each minute so the test can
    assert exact deci-cent round-trip without any floating-point
    ambiguity at the writer/reader seam.
    """
    bars: list[TradeBar] = []
    market_open = datetime(
        trading_date.year,
        trading_date.month,
        trading_date.day,
        9,
        30,
        tzinfo=EASTERN,
    )
    for i in range(count):
        start = market_open + timedelta(minutes=i)
        close_price = open_price + (i * increment)
        bars.append(
            TradeBar(
                symbol=symbol,
                time=start,
                end_time=start + timedelta(minutes=1),
                open=Decimal(str(close_price - increment / 2)),
                high=Decimal(str(close_price + increment / 2)),
                low=Decimal(str(close_price - increment)),
                close=Decimal(str(close_price)),
                volume=1000 + i,
            )
        )
    return bars


class TestDataFolderRoundTrip:
    """The reader/writer pair must round-trip prices and timestamps exactly.

    A failure here is the LEAN data-folder contract being broken on our
    side — the sidecar's runner is not even involved. Any change to
    ``lean_format.py`` that fails this test invalidates every staged
    workspace produced by ``staging.stage_minute_bars``.
    """

    def test_prices_round_trip_exact_in_deci_cents(self, tmp_path: Path) -> None:
        symbol = "SPY"
        trading_date = date(2025, 1, 6)
        bars = _make_minute_bars(symbol, trading_date)

        write_lean_day_zip(tmp_path, symbol, trading_date, bars)
        reader = LeanMinuteDataReader(tmp_path)
        round_tripped = reader.read_day(symbol, trading_date)

        assert len(round_tripped) == len(bars)
        for original, got in zip(bars, round_tripped, strict=True):
            # deci-cent round-trip: original price * 10000 must equal
            # the integer stored on disk; read-back must equal the
            # truncated-to-deci-cent original.
            assert int(got.close * PRICE_SCALE) == int(original.close * PRICE_SCALE), (
                f"deci-cent round-trip drift at {original.time}"
            )
            assert got.open == (Decimal(int(original.open * PRICE_SCALE)) / PRICE_SCALE)
            assert got.volume == original.volume

    def test_timestamps_round_trip_in_eastern_tz(self, tmp_path: Path) -> None:
        symbol = "QQQ"
        trading_date = date(2025, 1, 7)
        bars = _make_minute_bars(symbol, trading_date)

        write_lean_day_zip(tmp_path, symbol, trading_date, bars)
        reader = LeanMinuteDataReader(tmp_path)
        round_tripped = reader.read_day(symbol, trading_date)

        for original, got in zip(bars, round_tripped, strict=True):
            assert got.time.tzinfo is not None
            assert got.time.utcoffset() == original.time.utcoffset()
            assert got.time == original.time

    def test_staging_writes_in_expected_lean_layout(self, tmp_artifacts_root: Path) -> None:
        """Verify ``staging.stage_minute_bars`` lands files at the
        canonical LEAN path ``equity/usa/minute/<symbol>/<YYYYMMDD>_trade.zip``.

        This is the test that catches a layout regression before any
        LEAN container is spawned — if the path is wrong, LEAN silently
        emits "no data" and the run is green-but-empty.
        """
        ws = resolve_workspace("fixture_layout_check", tmp_artifacts_root)
        symbol = "SPY"
        trading_date = date(2025, 1, 6)
        bars = _make_minute_bars(symbol, trading_date)

        paths = stage_minute_bars(
            ws,
            symbol=symbol,
            bars_by_date=[(trading_date, bars)],
        )
        assert len(paths) == 1
        rel = paths[0].relative_to(ws.data_dir).as_posix()
        assert rel == "equity/usa/minute/spy/20250106_trade.zip"

    def test_naive_or_utc_input_is_normalized_to_eastern(self, tmp_path: Path) -> None:
        """Bars supplied in UTC must serialize as the equivalent ET ms.

        The writer applies the ET conversion; if a refactor drops the
        ``.astimezone(EASTERN)`` call, this test catches it.
        """
        symbol = "AAPL"
        trading_date = date(2025, 1, 8)
        # 14:30 UTC = 09:30 ET on 2025-01-08.
        utc_open = datetime(2025, 1, 8, 14, 30, tzinfo=ZoneInfo("UTC"))
        bar = TradeBar(
            symbol=symbol,
            time=utc_open,
            end_time=utc_open + timedelta(minutes=1),
            open=Decimal("100.00"),
            high=Decimal("100.05"),
            low=Decimal("99.95"),
            close=Decimal("100.02"),
            volume=500,
        )
        write_lean_day_zip(tmp_path, symbol, trading_date, [bar])
        reader = LeanMinuteDataReader(tmp_path)
        round_tripped = reader.read_day(symbol, trading_date)

        assert len(round_tripped) == 1
        got = round_tripped[0]
        # The read-back time is the same instant (in ET).
        assert got.time == utc_open.astimezone(EASTERN)

    @pytest.mark.parametrize(
        "open_price,expected_disk_value",
        [
            (100.00, 1_000_000),
            (123.45, 1_234_500),
            (0.0001, 1),  # The LEAN quantization floor.
        ],
    )
    def test_deci_cent_scale_matches_lean(
        self,
        tmp_path: Path,
        open_price: float,
        expected_disk_value: int,
    ) -> None:
        """Prices on disk are ``price * 10000`` as integers (LEAN convention).

        Anything below the quantization floor (0.0001) cannot round-trip
        exactly; that floor is documented in the ADR §"LEAN quantization
        floor" and respected by the reconciliation tolerance.
        """
        symbol = "TST"
        trading_date = date(2025, 1, 9)
        bars = [
            TradeBar(
                symbol=symbol,
                time=datetime(2025, 1, 9, 9, 30, tzinfo=EASTERN),
                end_time=datetime(2025, 1, 9, 9, 31, tzinfo=EASTERN),
                open=Decimal(str(open_price)),
                high=Decimal(str(open_price)),
                low=Decimal(str(open_price)),
                close=Decimal(str(open_price)),
                volume=1,
            )
        ]
        write_lean_day_zip(tmp_path, symbol, trading_date, bars)
        reader = LeanMinuteDataReader(tmp_path)
        round_tripped = reader.read_day(symbol, trading_date)
        assert int(round_tripped[0].open * PRICE_SCALE) == expected_disk_value
