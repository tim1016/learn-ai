"""Capture a Polygon minute-bar fixture for the LEAN-vs-engine parity test.

Usage:
    python scripts/regenerate_polygon_fixture.py SPY 2025-01-06 2025-01-10

Outputs:
    tests/fixtures/polygon_capture/<symbol>_minute_<from>_<to>/
        bars.json         — raw Polygon bar dicts (timestamp, ohlcv)
        metadata.json     — machine-readable manifest
        attribution.md    — opened in the operator's editor for narrative

Requires:
    POLYGON_API_KEY environment variable set.

After running:
    1. Run the LEAN EMA template against this fixture (operator step:
       `python scripts/probe_lean_ema_trade_count.py <fixture-dir>` —
       to be added when the fixture script is first exercised).
    2. If observed_trade_count == 0, pick a different window. Zero-trade
       fixtures cannot serve as parity receipts.
    3. Edit attribution.md with the rationale for the window.
    4. Commit bars.json + metadata.json + attribution.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from app.services.dataset_service import fetch_bars_chunked
from app.services.polygon_client import PolygonClientService

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "polygon_capture"


def main(symbol: str, from_date: str, to_date: str) -> int:
    if not os.environ.get("POLYGON_API_KEY"):
        print("ERROR: POLYGON_API_KEY env var is required", file=sys.stderr)
        return 2

    polygon = PolygonClientService()
    print(f"Fetching {symbol} 1-minute bars from {from_date} to {to_date}...")
    bars = fetch_bars_chunked(
        polygon=polygon,
        ticker=symbol,
        from_date=from_date,
        to_date=to_date,
        timespan="minute",
        multiplier=1,
        adjusted=False,
    )
    print(f"Received {len(bars)} bars.")

    if not bars:
        print("ERROR: zero bars returned; pick a different window", file=sys.stderr)
        return 3

    fixture_dir = FIXTURE_ROOT / f"{symbol.lower()}_minute_{from_date}_{to_date}"
    fixture_dir.mkdir(parents=True, exist_ok=True)

    bars_json = json.dumps(bars, separators=(",", ":"))
    bars_path = fixture_dir / "bars.json"
    bars_path.write_text(bars_json)
    bars_sha256 = hashlib.sha256(bars_json.encode("utf-8")).hexdigest()

    # observed_trade_count is initially None; the operator updates it
    # after running the EMA template once.
    metadata = {
        "schema_version": 1,
        "symbol": symbol,
        "from_date": from_date,
        "to_date": to_date,
        "timespan": "minute",
        "multiplier": 1,
        "adjusted": False,
        "session_prefilter": "none",
        "bar_count": len(bars),
        "fetched_at_ms_utc": int(datetime.now(UTC).timestamp() * 1000),
        "polygon_sdk_version": _polygon_sdk_version(),
        "bars_sha256": bars_sha256,
        "observed_trade_count": None,
        "observed_first_entry_ms_utc": None,
        "observed_first_exit_ms_utc": None,
    }
    (fixture_dir / "metadata.json").write_text(json.dumps(metadata, indent=2))

    attribution = fixture_dir / "attribution.md"
    if not attribution.exists():
        attribution.write_text(
            f"# Polygon fixture: {symbol} {from_date}..{to_date}\n\n"
            f"**Captured:** {datetime.now(UTC).isoformat()}\n\n"
            "## Why this window\n\n"
            "TODO: explain why this window was chosen (e.g., known to produce >=1 EMA-crossover trade).\n\n"
            "## Observed trade count\n\n"
            "TODO: run the EMA template against this fixture and record the count in metadata.json.\n"
        )

    print(f"Wrote {bars_path} ({len(bars)} bars)")
    print(f"Wrote {fixture_dir / 'metadata.json'} (sha256={bars_sha256[:12]}...)")
    print(f"Edit {attribution} with narrative context, then update metadata.json")
    print("  observed_trade_count, observed_first_entry_ms_utc, observed_first_exit_ms_utc")
    return 0


def _polygon_sdk_version() -> str:
    try:
        from importlib.metadata import version

        return version("polygon-api-client")
    except Exception:
        return "unknown"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("symbol")
    parser.add_argument("from_date")
    parser.add_argument("to_date")
    args = parser.parse_args()
    sys.exit(main(args.symbol, args.from_date, args.to_date))
