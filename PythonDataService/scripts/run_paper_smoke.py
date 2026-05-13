"""Smoke runner for IBKR paper trading — bypasses init-ledger/pre-flight.

Throwaway script for proving the live engine + real IBKR Gateway path
works end-to-end before committing to a full production run via
``python -m app.engine.live.run start``. Designed to be obviously safe:

  * ``--readonly`` is ON by default (no broker orders place; the engine
    drains the strategy's pending orders without calling place_order).
  * ``--duration-min`` defaults to 5 (asyncio.wait_for cap).
  * ``--max-orders-per-day`` defaults to 2 — even in ``--no-readonly``
    mode this caps the blast radius if the strategy misbehaves.
  * Writes outputs under ``PythonDataService/live_runs_smoke/<ts>/`` so
    smoke artifacts never collide with a production ``live_runs/<run_id>/``.
  * Bypasses ``init-ledger`` and ``pre-flight`` gates by design — the
    operator must verify ``/api/broker/health`` and ``/api/broker/diagnose``
    separately before running this. The production ``run.py start``
    enforces those gates plus halt-flag / poisoned-flag refusal.

The smoke runner exists to flush out integration bugs between
``LiveEngine`` and a real IBKR Gateway connection — bugs the FakeBroker
unit tests can't surface (TWS-specific request pacing, real-bar timing,
session-boundary edge cases, etc.). It is **not** a substitute for the
production CLI; delete it once Phase 8 hardening (signal handling,
log rotation, exception-flatten, FakeBroker shutdown integration test)
lands.

Usage (inside the polygon-data-service container's /app/ workdir):

    python -m scripts.run_paper_smoke --duration-min 5
    python -m scripts.run_paper_smoke --no-readonly --duration-min 10
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from app.broker.ibkr.client import IbkrClient
from app.engine.live.config import LiveConfig
from app.engine.live.live_engine import LiveEngine
from app.engine.strategy.algorithms.spy_ema_crossover import SpyEmaCrossoverAlgorithm

logger = logging.getLogger("scripts.run_paper_smoke")


async def _run_with_timeout(engine: LiveEngine, strategy: SpyEmaCrossoverAlgorithm, duration_min: int) -> None:
    """Run the engine, cancel after ``duration_min`` minutes via wait_for.

    Cancellation propagates through the async bar source; the engine's
    ``try/finally`` flushes artifact writers and stops the broker event
    stream. ``--readonly`` mode means no broker orders were placed, so
    there's nothing to flatten on cancel — wait_for cancellation is the
    intended stop mechanism. ``--no-readonly`` mode leaves any open
    position on the books; the operator must manually flatten via
    ``/api/broker/orders`` or ``python -m app.engine.live.run emergency-flatten``.
    """
    timeout_s = duration_min * 60
    try:
        await asyncio.wait_for(engine.run(strategy), timeout=timeout_s)
    except TimeoutError:
        logger.info("Smoke run reached --duration-min=%d cap; stopping", duration_min)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="scripts.run_paper_smoke",
        description=(
            "Smoke runner for IBKR paper — bypasses init-ledger and "
            "pre-flight. Throwaway; use 'python -m app.engine.live.run start' "
            "for production runs."
        ),
    )
    parser.add_argument(
        "--duration-min",
        type=int,
        default=5,
        help="Maximum minutes to run before stopping (default: 5).",
    )
    readonly_group = parser.add_mutually_exclusive_group()
    readonly_group.add_argument(
        "--readonly",
        dest="readonly",
        action="store_true",
        default=True,
        help="DEFAULT — drain pending orders without calling broker.place_order.",
    )
    readonly_group.add_argument(
        "--no-readonly",
        dest="readonly",
        action="store_false",
        help="Explicitly enable broker order placement. Use only after a clean readonly run.",
    )
    parser.add_argument(
        "--max-orders-per-day",
        type=int,
        default=2,
        help="Per-day order cap (default: 2 — conservative for smoke).",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("PythonDataService/live_runs_smoke"),
        help="Output root for the smoke run (default: live_runs_smoke/).",
    )
    return parser.parse_args()


async def _drive(args: argparse.Namespace, output_dir: Path) -> int:
    client = IbkrClient()
    health = await client.connect()
    logger.info(
        "Connected: is_paper=%s account=%s host=%s port=%s",
        health.is_paper,
        client.connected_account,
        health.host,
        health.port,
    )
    try:
        config = LiveConfig()
        engine = LiveEngine(
            client,
            config,
            output_dir=output_dir,
            account_id=client.connected_account or "",
            readonly=args.readonly,
            max_orders_per_day=args.max_orders_per_day,
        )
        strategy = SpyEmaCrossoverAlgorithm()
        await _run_with_timeout(engine, strategy, args.duration_min)
        return 0
    finally:
        await client.disconnect()


def main() -> int:
    args = _parse_args()
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_dir = args.output_root / f"smoke-{timestamp}"
    output_dir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    logger.info(
        "Smoke run starting; output_dir=%s readonly=%s duration_min=%d",
        output_dir,
        args.readonly,
        args.duration_min,
    )

    rc = asyncio.run(_drive(args, output_dir))
    logger.info("Smoke run complete; artifacts in %s (exit=%d)", output_dir, rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
