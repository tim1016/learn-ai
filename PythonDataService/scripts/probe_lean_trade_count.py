"""One-shot probe: run LEAN against a Polygon fixture and report trade count.

Used to fill in ``observed_trade_count`` / ``observed_first_entry_ms_utc`` /
``observed_first_exit_ms_utc`` in a captured fixture's ``metadata.json`` — the
parity test's skip-guard refuses to run with those fields null, but they can
only be discovered by running LEAN once. This script is the one-shot bridge.

Usage:
    cd PythonDataService
    python scripts/probe_lean_trade_count.py spy_minute_2025-01-06_2025-01-10

Requires:
    LEAN_LAUNCHER_URL set; LEAN launcher up.

After running:
    Copy the printed values into ``metadata.json`` under their respective keys,
    then commit the fixture directory.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "polygon_capture"

from app.lean_sidecar import polygon_canonical  # noqa: E402
from app.lean_sidecar.trading_calendar import (  # noqa: E402
    next_trading_day,
    session_open_ms_utc,
)
from app.routers.lean_sidecar import TrustedRunRequestModel  # noqa: E402
from app.services.lean_sidecar_persistence import pair_order_events  # noqa: E402
from app.services.lean_sidecar_service import (  # noqa: E402
    TrustedRunRequest,
    run_trusted_sample,
)


async def main(fixture_name: str) -> int:
    fixture_dir = FIXTURE_ROOT / fixture_name
    if not fixture_dir.exists():
        print(f"ERROR: fixture {fixture_dir} not found", file=sys.stderr)
        return 2

    meta = json.loads((fixture_dir / "metadata.json").read_text())
    symbol = meta["symbol"]
    from_date = date.fromisoformat(meta["from_date"])
    to_date = date.fromisoformat(meta["to_date"])

    # Swap the live Polygon factory for the fixture replay so LEAN sees the
    # exact bars we captured. Monkey-patch lives only inside this process.
    fixture_provider = polygon_canonical.RecordedPolygonFixtureProvider(fixture_dir)
    polygon_canonical.get_default_provider = lambda: fixture_provider  # type: ignore[assignment]

    # Validate via the router's TrustedRunRequestModel so the probe and
    # real HTTP traffic apply the same P2.5 session-open exclusive-end
    # contract — ``end_ms_utc`` is 09:30 ET of the NEXT trading day
    # after ``to_date``, never a calendar +1 day.
    run_id = f"probe-{uuid.uuid4().hex[:8]}"
    model = TrustedRunRequestModel(
        run_id=run_id,
        symbol=symbol,
        start_ms_utc=session_open_ms_utc(from_date),
        end_ms_utc=session_open_ms_utc(next_trading_day(to_date)),
        starting_cash=100_000.0,
        template="ema_crossover",
        data_source="polygon",
        bar_minutes=15,
        session="regular",
        adjustment="raw",
    )
    request = TrustedRunRequest(
        run_id=model.run_id,
        symbol=model.symbol.upper(),
        start_ms_utc=model.start_ms_utc,
        end_ms_utc=model.end_ms_utc,
        starting_cash=model.starting_cash,
        algorithm_source=model.algorithm_source,
        template=model.template,
        data_source=model.data_source,
        bar_minutes=model.bar_minutes,
        session=model.session,
        adjustment=model.adjustment,
    )

    print(f"Running LEAN with fixture {fixture_name}...")
    result = await run_trusted_sample(request)
    if result.exit_code != 0:
        print(f"ERROR: LEAN exit_code={result.exit_code}", file=sys.stderr)
        print("Log tail:", result.log_tail[-500:], file=sys.stderr)
        return 3

    if result.normalized is None or not result.normalized.order_events:
        print("observed_trade_count: 0")
        print("observed_first_entry_ms_utc: null")
        print("observed_first_exit_ms_utc: null")
        print()
        print("WARNING: zero LEAN order events — pick a different window.")
        return 0

    # NormalizedOrderEvent is Pydantic; pair_order_events expects dicts.
    events_as_dicts = [ev.model_dump() for ev in result.normalized.order_events]
    paired, open_lot = pair_order_events(events_as_dicts)

    if open_lot is not None:
        print(
            f"WARNING: LEAN ended with an unmatched open lot at "
            f"entry_ms_utc={open_lot.entry_ms_utc} — check OnEndOfAlgorithm",
            file=sys.stderr,
        )

    if not paired:
        print("observed_trade_count: 0")
        print("observed_first_entry_ms_utc: null")
        print("observed_first_exit_ms_utc: null")
        return 0

    first = paired[0]
    print()
    print(f"Run complete. Workspace: {result.workspace_root}")
    print()
    print(f"Update {fixture_dir / 'metadata.json'} with:")
    print(f'  "observed_trade_count": {len(paired)},')
    print(f'  "observed_first_entry_ms_utc": {first.entry_ms_utc},')
    print(f'  "observed_first_exit_ms_utc": {first.exit_ms_utc},')
    print()
    print(f"All paired trades: {len(paired)}")
    for i, t in enumerate(paired, 1):
        print(
            f"  #{i}: entry_ms={t.entry_ms_utc} @${t.entry_price:.2f}, "
            f"exit_ms={t.exit_ms_utc} @${t.exit_price:.2f}, "
            f"qty={t.quantity}, pnl=${t.pnl:.2f}"
        )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("fixture_name", help="e.g. spy_minute_2025-01-06_2025-01-10")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.fixture_name)))
