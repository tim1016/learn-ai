"""Extract a compact, hash-pinned LEAN oracle for EMA Crossover 2 bps."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pyarrow as pa

from app.engine.data.lean_format import LeanMinuteDataReader

SOURCE_RUN_ID = "companion-pg-e4edc7a8afa84d9ea55a"
SOURCE_ALGORITHM_SHA256 = "1a85f9d4cc0ca2607077615c78582191a11ec06c1b40b8edf59de3f52d64360b"
SOURCE_INPUT_SHA256 = "6d6df3745cca0cab1b7f930315b171b4fbceb93dc4d139ff943a3be48dd4c88f"
SOURCE_IMAGE_DIGEST = "sha256:3dd003372f1ef1981b4e80038e3f1c557f1fe414d1be531f485ef870f81a5771"
SOURCE_COMMIT = "261366a7e26ae942df858ab20df4fef8fa07de67"
START_DATE = date(2026, 2, 17)
END_DATE = date(2026, 2, 26)
END_MS_UTC = 1_772_168_399_999


def main() -> None:
    service_root = Path(__file__).resolve().parents[4]
    source_root = service_root / "artifacts" / "lean-sidecar" / SOURCE_RUN_ID
    manifest = _read_json(source_root / "manifest.json")
    normalized = _read_json(source_root / "normalized" / "result.json")
    _validate_source(manifest)

    data_root = service_root / "lean-cache" / "polygon-raw"
    reader = LeanMinuteDataReader(data_root, session="regular")
    bars = list(reader.iter_bars("SPY", START_DATE, END_DATE))
    if len(bars) != 3_120:
        raise RuntimeError(f"expected 3,120 regular-session minute bars, found {len(bars)}")
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
    if len(filled_events) != 6:
        raise RuntimeError(f"expected six LEAN fills in compact window, found {len(filled_events)}")

    fixture_dir = service_root / "tests/fixtures/golden/strategy-parity/ENG-007/v1"
    output_payload = {
        "source_run_id": SOURCE_RUN_ID,
        "lean_source_commit": SOURCE_COMMIT,
        "lean_image_digest": SOURCE_IMAGE_DIGEST,
        "parameters": {"gap_bps": "2", "rsi_min": "50", "rsi_max": "70"},
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
        "end_date": "2026-08-14",
        "session": "regular",
        "start_date": "2026-02-17",
        "starting_cash": 100_000.0,
        "symbol": "SPY",
    }
    if manifest.get("parameters") != expected_parameters:
        raise RuntimeError("source manifest parameters no longer match the pinned receipt")


def _validate_bar_archives(manifest: dict, data_root: Path, bars: list) -> None:
    expected_hashes = manifest.get("staged_zip_sha256", {})
    trading_dates = sorted({bar.time.date() for bar in bars})
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
            "time_ms_utc": pa.array(
                [int(bar.time.timestamp() * 1_000) for bar in bars],
                type=pa.int64(),
            ),
            "end_time_ms_utc": pa.array(
                [int(bar.end_time.timestamp() * 1_000) for bar in bars],
                type=pa.int64(),
            ),
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
