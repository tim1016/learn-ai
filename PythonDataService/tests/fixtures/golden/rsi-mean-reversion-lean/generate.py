"""Extract a compact, hash-pinned LEAN oracle for RSI Mean Reversion."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pyarrow as pa

from app.engine.data.lean_format import LeanMinuteDataReader
from app.utils.timestamps import ny_datetime

SOURCE_RUN_ID = "companion-pg-1ef2ef27539b413a9628"
SOURCE_ALGORITHM_SHA256 = "f9f1488f1b4e654770973d14aae8c5f6c37e2176a3217fe1be63bfd72e1d644c"
SOURCE_INPUT_SHA256 = "e60e61548e6d30838f0593e772ad8f67c8a891c4b9ff2830b284ccb8a340e7c0"
SOURCE_IMAGE_DIGEST = "sha256:3dd003372f1ef1981b4e80038e3f1c557f1fe414d1be531f485ef870f81a5771"
SOURCE_COMMIT = "261366a7e26ae942df858ab20df4fef8fa07de67"
# The compact window starts at the source run's own start date, so RSI warmup
# is identical on both sides; only the tail is truncated, at a bar where the
# strategy is flat (no end-of-algorithm liquidation the LEAN run never made).
START_DATE = date(2026, 2, 2)
END_DATE = date(2026, 2, 25)
END_MS_UTC = 1_772_053_200_000  # 2026-02-25T16:00:00 ET — that session's close.
EXPECTED_BAR_COUNT = 6_630
EXPECTED_FILL_COUNT = 6


def main() -> None:
    service_root = Path(__file__).resolve().parents[4]
    source_root = service_root / "artifacts" / "lean-sidecar" / SOURCE_RUN_ID
    manifest = _read_json(source_root / "manifest.json")
    normalized = _read_json(source_root / "normalized" / "result.json")
    _validate_source(manifest)

    # Unlike ENG-007, the bars come from the run's own staged workspace rather
    # than ``lean-cache``: this was a lake-mode run, so the workspace holds the
    # exact archive bytes LEAN read and ``lean-cache`` holds re-encoded copies
    # whose hashes no longer match the receipt.
    data_root = source_root / "workspace" / "data"
    reader = LeanMinuteDataReader(data_root, session="regular")
    bars = list(reader.iter_bars("SPY", START_DATE, END_DATE))
    if len(bars) != EXPECTED_BAR_COUNT:
        raise RuntimeError(f"expected {EXPECTED_BAR_COUNT:,} regular-session minute bars, found {len(bars)}")
    _validate_bar_archives(manifest, data_root, bars)

    filled_events = [
        {
            "ms_utc": event["ms_utc"],
            "direction": event["direction"],
            "fill_price": str(event["fill_price"]),
            "fill_quantity": int(event["fill_quantity"]),
            "order_fee": str(event["order_fee_amount"]),
        }
        for event in normalized["order_events"]
        if event["status"] == "filled" and event["ms_utc"] <= END_MS_UTC
    ]
    if len(filled_events) != EXPECTED_FILL_COUNT:
        raise RuntimeError(f"expected {EXPECTED_FILL_COUNT} LEAN fills in compact window, found {len(filled_events)}")
    if sum(event["fill_quantity"] for event in filled_events) != 0:
        raise RuntimeError("compact window does not end flat — final equity would not equal residual cash")

    fixture_dir = service_root / "tests/fixtures/golden/strategy-parity/ENG-009/v1"
    output_payload = {
        "source_run_id": SOURCE_RUN_ID,
        "lean_source_commit": SOURCE_COMMIT,
        "lean_image_digest": SOURCE_IMAGE_DIGEST,
        "parameters": {"window": "14", "oversold": "30", "overbought": "70", "resolution_minutes": "15"},
        "filled_order_events": filled_events,
        "final_equity": str(_final_equity(filled_events)),
    }
    _write_bars(fixture_dir / "input.arrow", bars)
    _write_json(fixture_dir / "output.json", output_payload)


def _validate_source(manifest: dict) -> None:
    expected = {
        "run_id": SOURCE_RUN_ID,
        "algorithm_source_sha256": SOURCE_ALGORITHM_SHA256,
        "input_snapshot_sha256": SOURCE_INPUT_SHA256,
        "lean_image_digest": SOURCE_IMAGE_DIGEST,
        "starting_capital": 100_000.0,
    }
    for field, value in expected.items():
        if manifest.get(field) != value:
            raise RuntimeError(f"source manifest {field} no longer matches the pinned receipt")
    provenance = manifest.get("lean_runtime_provenance", {})
    if provenance.get("source_commit") != SOURCE_COMMIT:
        raise RuntimeError("source manifest no longer references the pinned LEAN commit")
    expected_parameters = {
        "adjustment": "raw",
        "bar_minutes": 15,
        "end_date": "2026-04-30",
        "session": "regular",
        "start_date": "2026-02-02",
        "starting_cash": 100_000.0,
        "symbol": "SPY",
    }
    if manifest.get("parameters") != expected_parameters:
        raise RuntimeError("source manifest parameters no longer match the pinned receipt")


def _validate_bar_archives(manifest: dict, data_root: Path, bars: list) -> None:
    expected_hashes = manifest.get("staged_zip_sha256", {})
    trading_dates = sorted({ny_datetime(bar.start_ms).date() for bar in bars})
    for trading_date in trading_dates:
        relative = Path("equity/usa/minute/spy") / f"{trading_date:%Y%m%d}_trade.zip"
        archive = data_root / relative
        actual = hashlib.sha256(archive.read_bytes()).hexdigest()
        if expected_hashes.get(relative.as_posix()) != actual:
            raise RuntimeError(f"bar archive hash differs from LEAN receipt: {relative}")


def _final_equity(events: list[dict]) -> Decimal:
    cash = Decimal("100000")
    for event in events:
        quantity = Decimal(str(event["fill_quantity"]))
        cash -= quantity * Decimal(event["fill_price"])
        cash -= Decimal(event["order_fee"])
    return cash


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_bars(path: Path, bars: list) -> None:
    price_type = pa.decimal128(18, 6)
    table = pa.table(
        {
            "symbol": pa.array([bar.symbol for bar in bars], type=pa.string()),
            "time_ms_utc": pa.array([bar.start_ms for bar in bars], type=pa.int64()),
            "end_time_ms_utc": pa.array([bar.end_ms for bar in bars], type=pa.int64()),
            "open": pa.array([bar.open for bar in bars], type=price_type),
            "high": pa.array([bar.high for bar in bars], type=price_type),
            "low": pa.array([bar.low for bar in bars], type=price_type),
            "close": pa.array([bar.close for bar in bars], type=price_type),
            "volume": pa.array([bar.volume for bar in bars], type=pa.int64()),
        }
    )
    with pa.ipc.new_file(path, table.schema) as writer:
        writer.write_table(table)


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
