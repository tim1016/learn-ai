"""Reproducible offline read-latency bench for the Broker V2 panel surfaces (#1801).

Measures the catalog GET and per-bot panel GET against a synthetic fleet of
N bots on a real ``ClerkSqliteRepository`` + real ``SqliteAlpacaClerkFacade``
+ the real router, driven through ASGI — the same production read path the
2026-08-26 audit measured live (T2/O4), minus the live broker and the
running-bot write load. It exists so the read-side cost curve can be
profiled and re-measured after any change without staging a 50-bot fleet.

What it measures:
- catalog GET, sequential: p50 / p95 across ``--requests`` requests
- panel GET, sequential: p50 / p95 across the fleet
- panel GET at ``--concurrency`` (default 10): the F13 case
  (``docs/known-gaps.md`` §9) — per-request latency under concurrent load
  and the wall time per round, which together show whether concurrent
  panel reads serialize
- catalog GET at ``--concurrency``: the same question for the T2 surface
- ``--profile``: cProfile of the catalog reads, top functions by
  cumulative time, filtered to repository code

What it deliberately does NOT measure (live-only, still owed by #1801):
- per-account lock contention between admission work and *running bots'*
  clerk operations (the audit's suspected root needs live write load)
- deploy latency under fleet load (O2)
- the post-mass-stop 77% CPU residue (S12d)

The bot task registry is a minimal in-memory stand-in answering liveness —
in production that answer is a dict lookup, so it is not part of the cost
curve under test. Everything below it (SQLite reads, lifecycle-file reads,
projection, serialization) is the production implementation.

Usage (host venv, repo root)::

    cd PythonDataService
    .venv/bin/python -m scripts.bench_panel_read_latency --rows 94 144
    .venv/bin/python -m scripts.bench_panel_read_latency --rows 144 --profile
"""

from __future__ import annotations

import argparse
import asyncio
import cProfile
import io
import json
import os
import pstats
import statistics
import sys
import tempfile
import time
from pathlib import Path

ACCOUNT_ID = "PA-BENCH-1801"
BROKER = "alpaca"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="bench_panel_read_latency")
    parser.add_argument(
        "--rows",
        type=int,
        nargs="+",
        default=[94, 144],
        help="fleet sizes to bench (default: the audit's idle and loaded row counts)",
    )
    parser.add_argument("--requests", type=int, default=15, help="sequential requests per surface")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=10,
        help="concurrent panel/catalog GETs for the F13 case (default 10)",
    )
    parser.add_argument(
        "--rounds", type=int, default=5, help="rounds of the concurrent case"
    )
    parser.add_argument(
        "--stopped-fraction",
        type=float,
        default=0.5,
        help="fraction of the fleet with a stopped run + terminal lifecycle record",
    )
    parser.add_argument("--profile", action="store_true", help="cProfile the catalog reads")
    parser.add_argument(
        "--profile-limit", type=int, default=25, help="profile rows to print"
    )
    parser.add_argument(
        "--disable-gc",
        action="store_true",
        help="run with the cyclic GC off — separates GC pressure from other "
        "interleaving costs in the concurrent-inflation question",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable results")
    return parser.parse_args(argv)


def _percentiles(samples_ms: list[float]) -> dict[str, float]:
    ordered = sorted(samples_ms)
    return {
        "n": len(ordered),
        "p50_ms": round(statistics.median(ordered), 2),
        "p95_ms": round(ordered[max(0, int(len(ordered) * 0.95) - 1)], 2),
        "max_ms": round(ordered[-1], 2),
    }


def _build_fleet(artifacts_root: Path, rows: int, stopped_fraction: float):
    """Populate a real SQLite authority + file-side lifecycle for N bots."""
    from app.broker.alpaca.clerk.sqlite.commands import submit_start_run, submit_stop_run
    from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
    from app.engine.live.bot_lifecycle_state import (
        BotDutyOutcome,
        BotLifecyclePhase,
        BotLifecycleStateRepo,
        stable_bot_lifecycle_state_path,
    )
    from app.engine.live.desired_state import DesiredState, DesiredStateRepo, stable_desired_state_path

    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=artifacts_root)
    now_ms = repo.clock()
    stopped_count = int(rows * stopped_fraction)
    sids: list[str] = []
    for index in range(rows):
        sid = f"bench-{index:03d}"
        sids.append(sid)
        repo.register_strategy_instance(
            strategy_instance_id=sid,
            symbol="SPY",
            config_hash=f"bench-hash-{index:03d}",
            strategy_key="deployment_validation",
            display_name="Deployment Validation",
            config_json=json.dumps(
                {"mode": "trade", "quantity": 1, "carryover_policy": "FORBID"}
            ),
        )
        submit_start_run(
            repo,
            account_id=ACCOUNT_ID,
            strategy_instance_id=sid,
            lifecycle_run_id=f"run-{sid}",
        )
        stopped = index < stopped_count
        if stopped:
            submit_stop_run(
                repo,
                account_id=ACCOUNT_ID,
                strategy_instance_id=sid,
                lifecycle_run_id=f"run-{sid}",
                operator_reason="BENCH_STOP",
            )
        lifecycle_repo = BotLifecycleStateRepo(
            stable_bot_lifecycle_state_path(artifacts_root, sid)
        )
        lifecycle_repo.update(
            now_ms=now_ms,
            updated_by="bench",
            phase=BotLifecyclePhase.OFF_DUTY if stopped else BotLifecyclePhase.ON_DUTY,
            active_run_id=None if stopped else f"run-{sid}",
            reason="bench_populate",
            duty_outcome=(
                BotDutyOutcome(
                    kind="STOPPED",
                    reason_code="OPERATOR_STOP",
                    recorded_at_ms=now_ms,
                    run_id=f"run-{sid}",
                )
                if stopped
                else None
            ),
        )
        DesiredStateRepo(
            stable_desired_state_path(artifacts_root, sid),
            trusted_root=artifacts_root / "live_state",
        ).set(
            DesiredState.STOPPED if stopped else DesiredState.RUNNING,
            updated_by="bench",
            now_ms=now_ms,
            reason="bench_populate",
        )
    return repo, sids


class _BenchBrokerPort:
    """Pure-read fence: #1776 says panel reads contact no broker. Verify it."""

    broker_id = BROKER

    def __init__(self) -> None:
        self.calls = 0

    async def get_account(self):
        from app.broker.contract.models import BrokerAccountSnapshot

        self.calls += 1
        now_ms = 1_700_000_000_000
        return BrokerAccountSnapshot(
            broker=BROKER,
            account_id=ACCOUNT_ID,
            account_mode="paper",
            account_status="ACTIVE",
            currency="USD",
            cash=100_000.0,
            equity=100_000.0,
            buying_power=200_000.0,
            portfolio_value=100_000.0,
            long_market_value=0.0,
            short_market_value=0.0,
            pattern_day_trader=False,
            trading_blocked=False,
            account_blocked=False,
            created_at_ms=now_ms,
            observed_at_ms=now_ms,
        )

    async def list_positions(self) -> list:
        self.calls += 1
        return []

    async def list_orders(self, **_kwargs) -> list:
        self.calls += 1
        return []

    async def list_activities(self, **_kwargs) -> list:
        self.calls += 1
        return []

    async def get_order_by_client_order_id(self, _client_order_id: str):
        return None

    def capabilities(self) -> None:  # pragma: no cover - registry shape only
        raise NotImplementedError


class _BenchRegistry:
    """Liveness stand-in: production answers this from an in-memory dict."""

    def __init__(self, receipts_root: Path, sids: list[str], stopped_count: int) -> None:
        self._receipts_root = receipts_root
        self._sids = sids
        self._stopped = set(sids[:stopped_count])

    def _view(self, sid: str):
        from app.schemas.broker_bots import BotStatusView

        stopped = sid in self._stopped
        now_ms = 1_700_000_000_000
        return BotStatusView(
            strategy_instance_id=sid,
            strategy_key="deployment_validation",
            strategy_label="Deployment Validation",
            broker=BROKER,
            symbol="SPY",
            mode="trade",
            quantity=1,
            running=not stopped,
            phase="OFF_DUTY" if stopped else "ON_DUTY",
            desired_state="STOPPED" if stopped else "RUNNING",
            active_run_id=None if stopped else f"run-{sid}",
            duty_outcome=None,
            binding_created_at_ms=now_ms,
            last_transition_at_ms=now_ms,
        )

    async def preview_resume_admission(self, broker: str, sid: str):
        """Resolve custody through the real production projection.

        The stopped-bot panel read is exactly where the per-poll custody
        work happens, so this must run the real seam — only the admission
        policy verdict itself is out of scope for a read bench.
        """
        from app.services.bot_runner import BotRunnerError
        from app.services.bot_start_admission import default_start_custody_projection

        async with default_start_custody_projection(
            self.binding_for_control(broker, sid)
        ):
            pass
        raise BotRunnerError("resume admission policy is not modelled in this bench")

    def status(self, broker: str, sid: str):
        assert broker == BROKER
        return self._view(sid)

    def list_bots(self, broker: str) -> list:
        return [self._view(sid) for sid in self._sids]

    def binding_for_control(self, broker: str, sid: str):
        from types import SimpleNamespace

        return SimpleNamespace(
            strategy_instance_id=sid,
            run_id=f"run-{sid}",
            symbol="SPY",
            use_rth=True,
            strategy_key="deployment_validation",
            sealed_program=None,
            mode="trade",
        )

    def panel_action_receipt_path(self, sid: str) -> Path:
        return self._receipts_root / f"{sid}-panel-action-receipts.json"

    def dry_run_activity(self, broker: str, sid: str) -> list:
        return []

    def bindings_for_broker(self, broker: str) -> list:
        return []


async def _timed_get(client, path: str) -> float:
    started = time.perf_counter()
    response = await client.get(path)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    if response.status_code != 200:
        raise RuntimeError(f"GET {path} -> {response.status_code}: {response.text[:300]}")
    return elapsed_ms


async def _bench_fleet(args: argparse.Namespace, rows: int, artifacts_root: Path) -> dict:
    import httpx
    from fastapi import FastAPI
    from httpx import ASGITransport

    from app.broker.alpaca.clerk.active_authority import (
        ActiveClerkRuntime,
        set_active_clerk_runtime,
    )
    from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
    from app.broker.contract.registry import (
        get_broker_registry,
        reset_broker_registry_for_testing,
    )
    from app.routers.broker_v2_panel import router
    from app.services.bot_runner import set_bot_task_registry
    from app.services.broker_account_snapshot import (
        clear_broker_account_snapshot_cache_for_testing,
    )

    reset_broker_registry_for_testing()
    clear_broker_account_snapshot_cache_for_testing()
    set_active_clerk_runtime(None)

    repo, sids = _build_fleet(artifacts_root, rows, args.stopped_fraction)
    stopped_count = int(rows * args.stopped_fraction)
    port = _BenchBrokerPort()
    get_broker_registry().register(port)  # type: ignore[arg-type]
    set_bot_task_registry(_BenchRegistry(artifacts_root, sids, stopped_count))  # type: ignore[arg-type]
    facade = SqliteAlpacaClerkFacade(repo=repo, read=port, trade=port)  # type: ignore[arg-type]
    set_active_clerk_runtime(ActiveClerkRuntime(authority_kind="sqlite", clerk=facade))

    app = FastAPI()
    app.include_router(router)
    account_root = f"/api/brokers/{BROKER}/accounts/{ACCOUNT_ID}"
    catalog_path = f"{account_root}/bots/catalog"
    panel_paths = [f"{account_root}/bots/{sid}/panel" for sid in sids]

    results: dict = {"rows": rows}
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://bench", timeout=120.0
        ) as client:
            # Warm once so first-touch imports/caches don't skew the samples.
            await _timed_get(client, catalog_path)
            await _timed_get(client, panel_paths[0])
            broker_calls_before = port.calls

            catalog_seq = [
                await _timed_get(client, catalog_path) for _ in range(args.requests)
            ]
            panel_seq = [
                await _timed_get(client, panel_paths[i % len(panel_paths)])
                for i in range(args.requests)
            ]

            concurrent_panel: list[float] = []
            concurrent_panel_walls: list[float] = []
            for round_index in range(args.rounds):
                offset = round_index * args.concurrency
                wall_start = time.perf_counter()
                samples = await asyncio.gather(
                    *(
                        _timed_get(
                            client, panel_paths[(offset + i) % len(panel_paths)]
                        )
                        for i in range(args.concurrency)
                    )
                )
                concurrent_panel_walls.append(
                    (time.perf_counter() - wall_start) * 1000.0
                )
                concurrent_panel.extend(samples)

            concurrent_catalog: list[float] = []
            concurrent_catalog_walls: list[float] = []
            for _ in range(args.rounds):
                wall_start = time.perf_counter()
                samples = await asyncio.gather(
                    *(_timed_get(client, catalog_path) for _ in range(args.concurrency))
                )
                concurrent_catalog_walls.append(
                    (time.perf_counter() - wall_start) * 1000.0
                )
                concurrent_catalog.extend(samples)

            results["catalog_sequential"] = _percentiles(catalog_seq)
            results["panel_sequential"] = _percentiles(panel_seq)
            results["panel_concurrent"] = {
                **_percentiles(concurrent_panel),
                "concurrency": args.concurrency,
                "wall_per_round_ms": round(statistics.median(concurrent_panel_walls), 2),
            }
            results["catalog_concurrent"] = {
                **_percentiles(concurrent_catalog),
                "concurrency": args.concurrency,
                "wall_per_round_ms": round(
                    statistics.median(concurrent_catalog_walls), 2
                ),
            }
            # #1776 read purity: the bench refuses to report numbers from a
            # run whose reads secretly contacted the broker port.
            results["broker_calls_during_bench"] = port.calls - broker_calls_before

            if args.profile:
                results["profile"] = await _profile_catalog(
                    client, catalog_path, args.requests, args.profile_limit
                )
    finally:
        set_active_clerk_runtime(None)
        set_bot_task_registry(None)
        reset_broker_registry_for_testing()
        clear_broker_account_snapshot_cache_for_testing()
        repo.close()
    return results


async def _profile_catalog(client, catalog_path: str, requests: int, limit: int) -> str:
    """cProfile the catalog read loop; return the top rows by cumulative time."""
    profiler = cProfile.Profile()
    profiler.enable()
    for _ in range(requests):
        await _timed_get(client, catalog_path)
    profiler.disable()
    buffer = io.StringIO()
    stats = pstats.Stats(profiler, stream=buffer)
    stats.sort_stats("cumulative").print_stats(r"app[\\/]", limit)
    return buffer.getvalue()


def _print_table(results: dict) -> None:
    rows = results["rows"]
    print(f"\n## {rows} rows")
    print("| surface | n | p50 ms | p95 ms | max ms | wall/round ms |")
    print("|---|---|---|---|---|---|")
    for key, label in (
        ("catalog_sequential", "catalog GET (sequential)"),
        ("panel_sequential", "panel GET (sequential)"),
        ("panel_concurrent", "panel GET (concurrent)"),
        ("catalog_concurrent", "catalog GET (concurrent)"),
    ):
        surface = results[key]
        wall = surface.get("wall_per_round_ms", "—")
        print(
            f"| {label} | {surface['n']} | {surface['p50_ms']} | "
            f"{surface['p95_ms']} | {surface['max_ms']} | {wall} |"
        )
    print(f"broker calls during bench: {results['broker_calls_during_bench']}")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    # The stopped-bot panel read logs its (deliberately unmodelled) resume
    # admission verdict on every request; keep bench output readable.
    import logging

    logging.getLogger("app.services.broker_v2_panel.panel_data_source").setLevel(
        logging.ERROR
    )
    all_results = []
    if args.disable_gc:
        import gc

        gc.disable()
    for rows in args.rows:
        with tempfile.TemporaryDirectory(prefix=f"bench-panel-{rows}-") as tmp:
            artifacts_root = Path(tmp)
            # The lifecycle-file readers resolve their root from settings;
            # point them at this fixture before any app import caches it.
            os.environ["IBKR_LIVE_RUNS_ROOT"] = str(artifacts_root / "live_runs")
            from app.broker.ibkr.config import reset_settings_for_testing

            reset_settings_for_testing()
            results = asyncio.run(_bench_fleet(args, rows, artifacts_root))
            all_results.append(results)
            if args.json:
                print(json.dumps(results, indent=2))
            else:
                _print_table(results)
                if args.profile and "profile" in results:
                    print("\n### cProfile (catalog reads, cumulative, app code)\n")
                    print(results["profile"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
