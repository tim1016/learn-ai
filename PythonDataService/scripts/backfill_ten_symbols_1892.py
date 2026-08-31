"""One-off driver for issue #1892: re-backfill the ten lake symbols in both
price-adjustment modes against the real data-lake catalog and lake volume.

Wraps the existing app.data_lake.backfill.run_backfill() orchestration
(issue #1836) — no new fetch/claim/write logic. For each (symbol, mode) pair
in RANGES x MODES it builds a DataRunSpec over the symbol's recovered
on-disk span (see the issue and the accompanying PR description for how
each range was recovered from the pre-wipe files' names) and runs one
backfill, logging per-day progress and a final JSON summary line.

Not a FastAPI route and not part of the app import graph — run directly
with the host venv, pointed at whichever Postgres/lake root the caller's
environment selects:

    PythonDataService/.venv/bin/python -m scripts.backfill_ten_symbols_1892 \\
        --symbol SPY --mode raw

Omit --symbol/--mode to run every (symbol, mode) combination in RANGES x
MODES sequentially. Issue: #1892. Part of: #1885.

--rollup-only (follow-up to #1892, fixing the rollup-window truncation bug
in app.data_lake.backfill.run_backfill): minute-bar coverage for these ten
symbols is already complete, so re-running the full per-day run_backfill()
loop would just repeat ~2,600 no-op catalog cache hits with a Phase-0
metadata bootstrap attempt on every single day. Instead this mode calls
app.data_lake.ensure_data.ensure_data() directly, once per (symbol, mode),
with a spec spanning the symbol's full RANGES span and
include_factor_files/include_map_files/include_daily_trade left at their
DataRunSpec default of True — exactly the one follow-up call run_backfill's
own rollup step would make (see backfill._rollup_spec), without the
per-day loop around it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import date
from typing import Literal
from uuid import uuid4

from app.data_lake.backfill import BackfillDayProgress, BackfillResult, BackfillWaitProgress, run_backfill
from app.data_lake.ensure_data import ensure_data
from app.data_lake.types import (
    DataAvailabilityResult,
    DataRunSpec,
    PriceAdjustmentMode,
    trading_date_to_calendar_anchor_ms,
)
from app.lean_sidecar.config import PINNED_LEAN_IMAGE_DIGEST

logger = logging.getLogger("backfill_ten_symbols_1892")

# Recovered from the pre-wipe on-disk file names under data-lake-volume/lake/
# (union of both modes' minute-trade zip dates per symbol) — see the issue
# for the recovery method. The real catalog held zero rows for nine of these
# ten symbols and for the entire raw root before this backfill; the matching
# orphaned on-disk files were discarded first (wipe-and-re-backfill, not
# merge/patch — ADR 0049 amendment, #1886) so the fixed writer produces
# catalog rows and files together over the exact same range.
RANGES: dict[str, tuple[date, date]] = {
    "SPY": (date(2024, 5, 20), date(2026, 8, 28)),
    "AAPL": (date(2026, 7, 13), date(2026, 8, 28)),
    "DIA": (date(2024, 6, 3), date(2026, 4, 30)),
    "MSFT": (date(2026, 7, 28), date(2026, 8, 28)),
    "NVDA": (date(2024, 6, 5), date(2024, 6, 12)),
    "TSLA": (date(2026, 8, 27), date(2026, 8, 28)),
    "GE": (date(2026, 7, 30), date(2026, 8, 28)),
    "SLV": (date(2024, 8, 29), date(2025, 2, 27)),
    "STRL": (date(2026, 7, 28), date(2026, 8, 28)),
    "IWM": (date(2026, 7, 29), date(2026, 8, 28)),
}

MODES: tuple[PriceAdjustmentMode, ...] = ("raw", "polygon_split_adjusted")


def _on_day_progress(symbol: str, mode: str):
    def _cb(progress: BackfillDayProgress) -> None:
        noteworthy = progress.day_index == 1 or progress.day_index % 20 == 0 or progress.day_index == progress.total_days
        if noteworthy or progress.failures:
            logger.info(
                "[%s/%s] day %d/%d %s: fetched=%d reused=%d failed=%d",
                symbol, mode, progress.day_index, progress.total_days, progress.trading_date,
                progress.fetched_count, progress.reused_count, len(progress.failures),
            )
        for failure in progress.failures:
            logger.error(
                "    FAILURE [%s/%s] %s %s %s %s: %s - %s",
                symbol, mode, failure.artifact_kind, failure.symbol,
                failure.trading_date, failure.data_type, failure.reason, failure.detail,
            )

    return _cb


def _on_wait(symbol: str, mode: str):
    def _cb(progress: BackfillWaitProgress) -> None:
        logger.info(
            "[%s/%s] waiting on in-flight lease: %s %s %s attempt=%d",
            symbol, mode, progress.trading_date, progress.symbol, progress.data_type, progress.attempt,
        )

    return _cb


def _summarize(result: BackfillResult) -> dict[str, object]:
    return {
        "total_sessions": result.total_sessions,
        "days_completed": result.days_completed,
        "days_with_failures": result.days_with_failures,
        "days_unattempted": result.days_unattempted,
        "fetched": result.fetched_artifact_count,
        "reused": result.reused_artifact_count,
        "overall_status": result.overall_status,
        "n_failures": len(result.failures),
        "failure_reasons": sorted({failure.reason for failure in result.failures}),
    }


def _build_spec(symbol: str, mode: PriceAdjustmentMode, start: date, end: date, *, requester: str) -> DataRunSpec:
    if PINNED_LEAN_IMAGE_DIGEST is None:
        raise RuntimeError("PINNED_LEAN_IMAGE_DIGEST is unset for this host architecture")

    return DataRunSpec(
        request_id=uuid4(),
        run_type="python_lab",
        requester=requester,
        market="usa",
        symbols=[symbol],
        start_trading_date_ms=trading_date_to_calendar_anchor_ms(start),
        end_trading_date_ms=trading_date_to_calendar_anchor_ms(end),
        price_adjustment_mode=mode,
        lean_image_digest=PINNED_LEAN_IMAGE_DIGEST,
    )


async def _run_one(symbol: str, mode: PriceAdjustmentMode, start: date, end: date) -> dict[str, object]:
    spec = _build_spec(symbol, mode, start, end, requester="backfill-1892")
    logger.info("=== Starting %s/%s: %s .. %s ===", symbol, mode, start, end)
    result = await run_backfill(spec, on_day_progress=_on_day_progress(symbol, mode), on_wait=_on_wait(symbol, mode))
    summary = _summarize(result)
    logger.info("=== Done %s/%s: %s ===", symbol, mode, json.dumps(summary, default=str))
    return summary


def _summarize_availability(result: DataAvailabilityResult) -> dict[str, object]:
    return {
        "overall_status": result.overall_status,
        "artifact_kinds_produced": sorted({a.artifact_kind for a in result.artifacts}),
        "fetched": result.fetched_artifact_count,
        "reused": result.reused_artifact_count,
        "refreshed": result.refreshed_artifact_count,
        "n_failures": len(result.failures),
        "failure_reasons": sorted({failure.reason for failure in result.failures}),
    }


async def _run_rollup_only(symbol: str, mode: PriceAdjustmentMode, start: date, end: date) -> dict[str, object]:
    """The rollup step alone (factor_file, map_file, daily-trade), over the
    symbol's already-backfilled full range — see the --rollup-only note in
    the module docstring."""
    spec = _build_spec(symbol, mode, start, end, requester="rollup-1892")
    logger.info("=== Rollup %s/%s: %s .. %s ===", symbol, mode, start, end)
    result = await ensure_data(spec)
    summary = _summarize_availability(result)
    logger.info("=== Done rollup %s/%s: %s ===", symbol, mode, json.dumps(summary, default=str))
    for failure in result.failures:
        logger.error(
            "    FAILURE [%s/%s] %s %s %s %s: %s - %s",
            symbol, mode, failure.artifact_kind, failure.symbol,
            failure.trading_date, failure.data_type, failure.reason, failure.detail,
        )
    return summary


async def _main(
    only_symbol: str | None, only_mode: Literal["raw", "polygon_split_adjusted"] | None, rollup_only: bool
) -> None:
    results: dict[str, dict[str, object]] = {}
    for symbol, (start, end) in RANGES.items():
        if only_symbol is not None and symbol != only_symbol:
            continue
        for mode in MODES:
            if only_mode is not None and mode != only_mode:
                continue
            key = f"{symbol}/{mode}"
            try:
                results[key] = (
                    await _run_rollup_only(symbol, mode, start, end)
                    if rollup_only
                    else await _run_one(symbol, mode, start, end)
                )
            except Exception as exc:  # top-level driver: one bad combo must not abort the whole batch
                logger.exception("=== %s RAISED ===", key)
                results[key] = {"error": repr(exc)}

    logger.info("===== FINAL SUMMARY =====")
    for key, summary in results.items():
        logger.info("%s: %s", key, json.dumps(summary, default=str))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", choices=sorted(RANGES), default=None, help="restrict to one symbol")
    parser.add_argument("--mode", choices=MODES, default=None, help="restrict to one price-adjustment mode")
    parser.add_argument(
        "--rollup-only",
        action="store_true",
        help="skip the per-day backfill loop; call ensure_data() once per combo for factor/map/daily-trade only",
    )
    args = parser.parse_args()
    # WARNING for everything else (the data-lake modules are chatty at INFO
    # over a multi-thousand-day backfill); INFO for this driver alone, so the
    # operator still sees per-day progress and every artifact failure.
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    logger.setLevel(logging.INFO)
    asyncio.run(_main(args.symbol, args.mode, args.rollup_only))


if __name__ == "__main__":
    main()
