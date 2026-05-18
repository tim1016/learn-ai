"""Phase 5c — tests for synthetic minute-quote staging.

The trusted-sample fixture historically emitted ``Cannot find file:
...quote.zip`` log lines because LEAN's default minute subscription
requests both trade and quote bars but the staging only wrote trade
zips. Phase 5c writes a synthetic quote zip (bid=ask=trade-close,
size=0) so the log goes clean.

These tests exercise the writer + the staging seam without spawning
LEAN; the E2E "no failed_data_requests" assertion lives in the
requires_lean_image gated suite.
"""

from __future__ import annotations

import zipfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.engine.data.lean_format import (
    EASTERN,
    PRICE_SCALE,
    write_lean_quote_day_zip,
)
from app.engine.data.trade_bar import TradeBar
from app.lean_sidecar.staging import stage_quote_bars
from app.lean_sidecar.workspace import Workspace


def _bar(et_hour: int, et_minute: int, close: float, *, d: date = date(2025, 1, 6)) -> TradeBar:
    """Construct a 1-minute trade bar at the given ET clock time."""
    from datetime import timedelta

    start = datetime(d.year, d.month, d.day, et_hour, et_minute, tzinfo=EASTERN)
    return TradeBar(
        symbol="SPY",
        time=start,
        end_time=start + timedelta(minutes=1),
        open=Decimal(str(close)),
        high=Decimal(str(close)),
        low=Decimal(str(close)),
        close=Decimal(str(close)),
        volume=100,
    )


def _read_zip_lines(path: Path) -> list[str]:
    with zipfile.ZipFile(path, "r") as zf:
        names = zf.namelist()
        assert len(names) == 1, f"expected exactly one CSV in zip, got {names}"
        return zf.read(names[0]).decode("utf-8").splitlines()


class TestWriteLeanQuoteDayZip:
    def test_path_layout_matches_lean_minute_quote_contract(self, tmp_path: Path) -> None:
        """LEAN looks for ``equity/usa/minute/<symbol>/YYYYMMDD_quote.zip``.
        A wrong path means LEAN keeps emitting the original log noise."""
        bars = [_bar(9, 30, 580.50)]
        path = write_lean_quote_day_zip(tmp_path, "SPY", date(2025, 1, 6), bars)
        assert path == tmp_path / "equity" / "usa" / "minute" / "spy" / "20250106_quote.zip"
        assert path.exists()

    def test_csv_filename_matches_lean_contract(self, tmp_path: Path) -> None:
        bars = [_bar(9, 30, 580.50)]
        path = write_lean_quote_day_zip(tmp_path, "SPY", date(2025, 1, 6), bars)
        with zipfile.ZipFile(path, "r") as zf:
            assert zf.namelist() == ["20250106_spy_minute_quote.csv"]

    def test_row_format_is_eleven_columns(self, tmp_path: Path) -> None:
        """LEAN minute-quote rows are 11 columns:
        ms, bid_o, bid_h, bid_l, bid_c, bid_size, ask_o, ask_h, ask_l, ask_c, ask_size.
        Anything else and LEAN's parser silently produces empty bars."""
        bars = [_bar(9, 30, 580.50)]
        path = write_lean_quote_day_zip(tmp_path, "SPY", date(2025, 1, 6), bars)
        lines = _read_zip_lines(path)
        assert len(lines) == 1
        cols = lines[0].split(",")
        assert len(cols) == 11

    def test_bid_equals_ask_zero_spread(self, tmp_path: Path) -> None:
        """The synthesis is zero-spread by design. Reviewer or auditor
        seeing a non-zero spread should suspect the bid/ask got mixed
        with something other than the trade close."""
        bars = [_bar(9, 30, 580.50)]
        path = write_lean_quote_day_zip(tmp_path, "SPY", date(2025, 1, 6), bars)
        cols = _read_zip_lines(path)[0].split(",")
        bid_close = cols[4]
        ask_close = cols[9]
        assert bid_close == ask_close
        # Price must be deci-cent-scaled per the trade-zip contract.
        assert bid_close == str(int(Decimal("580.50") * PRICE_SCALE))

    def test_bid_ask_sizes_are_zero(self, tmp_path: Path) -> None:
        """We don't have real bid/ask sizes; emitting 0 makes the
        synthesis explicit. Non-zero sizes would falsely claim depth."""
        bars = [_bar(9, 30, 580.50)]
        path = write_lean_quote_day_zip(tmp_path, "SPY", date(2025, 1, 6), bars)
        cols = _read_zip_lines(path)[0].split(",")
        assert cols[5] == "0"  # bid_size
        assert cols[10] == "0"  # ask_size

    def test_ms_offset_from_et_midnight(self, tmp_path: Path) -> None:
        """The ms-since-ET-midnight encoding must match the trade-zip
        contract exactly — a quote bar at 09:30 ET is 34_200_000 ms
        after ET midnight, regardless of UTC offset (DST or otherwise)."""
        bars = [_bar(9, 30, 580.50)]
        path = write_lean_quote_day_zip(tmp_path, "SPY", date(2025, 1, 6), bars)
        cols = _read_zip_lines(path)[0].split(",")
        # 9 * 3600 + 30 * 60 = 34_200 seconds = 34_200_000 ms
        assert cols[0] == str(34_200_000)

    def test_multiple_bars_one_per_row_in_order(self, tmp_path: Path) -> None:
        bars = [_bar(9, 30, 580.50), _bar(9, 31, 580.75), _bar(9, 32, 580.60)]
        path = write_lean_quote_day_zip(tmp_path, "SPY", date(2025, 1, 6), bars)
        lines = _read_zip_lines(path)
        assert len(lines) == 3
        # Each row's bid_close = ask_close = trade_close × PRICE_SCALE.
        closes_scaled = [int(Decimal(str(b.close)) * PRICE_SCALE) for b in bars]
        for row, expected_close in zip(lines, closes_scaled, strict=True):
            cols = row.split(",")
            assert int(cols[4]) == expected_close
            assert int(cols[9]) == expected_close


class TestStageQuoteBars:
    def test_writes_one_zip_per_day(self, tmp_path: Path) -> None:
        ws = Workspace(run_id="ut_quote_staging", artifacts_root=tmp_path, root=tmp_path / "ut")
        ws.ensure_layout()
        bars_by_date = [
            (date(2025, 1, 6), [_bar(9, 30, 580.50)]),
            (date(2025, 1, 7), [_bar(9, 30, 581.00)]),
        ]
        paths = stage_quote_bars(ws, symbol="SPY", bars_by_date=bars_by_date)
        assert len(paths) == 2
        for p in paths:
            assert p.exists()
            assert p.suffix == ".zip"
            assert "_quote.zip" in p.name

    def test_revalidates_symbol_at_staging_boundary(self, tmp_path: Path) -> None:
        """Defense-in-depth: staging re-runs validate_symbol so a
        bypass at any upstream layer doesn't reach a filesystem path."""
        from app.lean_sidecar.workspace import SymbolValidationError

        ws = Workspace(run_id="ut_quote_bad", artifacts_root=tmp_path, root=tmp_path / "ut")
        ws.ensure_layout()
        with pytest.raises(SymbolValidationError):
            stage_quote_bars(ws, symbol="../etc/passwd", bars_by_date=[])

    def test_lives_alongside_trade_zips(self, tmp_path: Path) -> None:
        """Quote zips must land in the SAME ``equity/usa/minute/<sym>/``
        dir as the trade zips — LEAN looks up both by the same path
        prefix. Different paths and LEAN finds neither."""
        from app.lean_sidecar.staging import stage_minute_bars

        ws = Workspace(run_id="ut_quote_colocated", artifacts_root=tmp_path, root=tmp_path / "ut")
        ws.ensure_layout()
        bars_by_date = [(date(2025, 1, 6), [_bar(9, 30, 580.50)])]
        trade_paths = stage_minute_bars(ws, symbol="SPY", bars_by_date=bars_by_date)
        quote_paths = stage_quote_bars(ws, symbol="SPY", bars_by_date=bars_by_date)
        assert trade_paths[0].parent == quote_paths[0].parent

    def test_empty_iterable_yields_no_zips(self, tmp_path: Path) -> None:
        """An empty bars_by_date is a valid (no-op) input — never raise."""
        ws = Workspace(run_id="ut_quote_empty", artifacts_root=tmp_path, root=tmp_path / "ut")
        ws.ensure_layout()
        paths = stage_quote_bars(ws, symbol="SPY", bars_by_date=[])
        assert paths == ()


def test_ms_encoding_does_not_drift_with_utc_offset() -> None:
    """A quote bar at 09:30 ET on a date inside DST and one outside DST
    must both encode to 34_200_000 ms — the ET-midnight reference is
    the authority, not UTC."""
    # 2025-03-09 is the spring-forward Sunday in the US; 2025-03-10 is
    # the first DST day. 2025-02-10 is well outside DST.
    et = ZoneInfo("America/New_York")
    for d in (date(2025, 2, 10), date(2025, 3, 10)):
        from datetime import timedelta

        start = datetime(d.year, d.month, d.day, 9, 30, tzinfo=et)
        bar = TradeBar(
            symbol="SPY",
            time=start,
            end_time=start + timedelta(minutes=1),
            open=Decimal("100"),
            high=Decimal("100"),
            low=Decimal("100"),
            close=Decimal("100"),
            volume=1,
        )
        # Use a temp dir per loop iteration
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            path = write_lean_quote_day_zip(Path(td), "SPY", d, [bar])
            ms_col = _read_zip_lines(path)[0].split(",")[0]
            assert int(ms_col) == 34_200_000, f"DST drift on {d}: got {ms_col}"
