"""FastAPI application entry point"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from app.broker.ibkr.client import (
    BrokerError,
    ConnectionRefusedDueToSentinelError,
    IbkrClient,
    set_client,
)
from app.config import settings
from app.routers import (
    account_reconciliation,
    aggregates,
    baselines,
    bot_events,
    broker,
    broker_account_truth,
    broker_capability,
    broker_session,
    chart,
    cohort_batch_launch,
    data_quality,
    dataset,
    edge,
    engine,
    golden_fixtures,
    indicator_reliability,
    indicators,
    iv30,
    iv_recorder,
    jobs,
    lean_sidecar,
    lifecycle_projection,
    market_monitor,
    monte_carlo,
    options,
    portfolio,
    quantlib_options,
    research,
    research_divergence,
    research_runs,
    sanitize,
    snapshot,
    spec_strategy,
    strategy,
    strategy_validation,
    tickers,
    volatility,
    walk_forward,
)
from app.routers import (
    broker_activity as broker_activity_router,
)
from app.routers import (
    live_instances as live_instances_router,
)
from app.routers import (
    live_runs as live_runs_router,
)
from app.security.data_plane_control import (
    require_data_plane_control_secret,
    require_data_plane_control_secret_always,
)
from app.services.account_reconciliation import AccountReconciliationService
from app.services.account_truth_refresh import AccountTruthRefreshLoop, account_truth_artifacts_root
from app.services.fleet_contamination import record_account_journal_parity_observation
from app.utils.error_handlers import polygon_exception_handler

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events.

    The IBKR client is connected best-effort: a failure here logs and
    leaves the broker endpoints in a 503 state, but the rest of the
    service still boots. The ONLY failure that aborts startup is the
    paper-vs-live sentinel mismatch — that's a safety violation and
    must not be silently absorbed.

    When broker is disabled: /health returns HTTP 200 with disabled=True
    (not 503 — Angular HttpClient routes 503 to error path); /diagnose
    returns DiagnosticReportDisabled; all other broker endpoints return 503.
    """
    logger.info(f"Starting Polygon Data Service on {settings.HOST}:{settings.PORT}")
    logger.info(f"Polygon API Key configured: {bool(settings.POLYGON_API_KEY)}")

    from app.broker.ibkr.config import get_settings as get_ibkr_settings

    ibkr_settings = get_ibkr_settings()
    ibkr_client: IbkrClient | None = None
    # Auto-reconnect monitor (broker-stability hardening). Started after the
    # initial connect attempt regardless of its outcome — even a startup
    # failure should auto-retry rather than wait for an operator click.
    from app.broker.ibkr.auto_reconnect_monitor import (
        AutoReconnectMonitor,
        set_monitor,
    )

    monitor: AutoReconnectMonitor | None = None
    account_truth_refresh_loop = None

    if ibkr_settings.broker_enabled:
        ibkr_client = IbkrClient()
        # Install the client immediately so /health reports the
        # disconnected-but-available state and POST /api/broker/connect can
        # drive the lifecycle from the Status page. Without this, a soft-fail
        # auto-connect leaves _client=None and the only fix is restarting
        # the container.
        set_client(ibkr_client)
        # The operator-intended state is set BEFORE the connect attempt so
        # the monitor (started further down) knows whether to retry on
        # initial-connect failure: when ``connect_on_startup`` is True the
        # operator wants a live link and a soft-fail should auto-retry;
        # when False the operator wants the client idle until they click
        # Connect — the monitor must NOT auto-connect against that intent.
        ibkr_client.set_desired_connected(ibkr_settings.connect_on_startup)
        if ibkr_settings.connect_on_startup:
            try:
                await ibkr_client.connect()
                logger.info("IBKR client connected; broker endpoints available.")
            except ConnectionRefusedDueToSentinelError:
                # Hard fail — never proceed past a paper/live mismatch.
                logger.exception("IBKR sentinel mismatch — aborting startup.")
                raise
            except (BrokerError, OSError) as exc:
                # Soft fail — Gateway is probably not running locally. Broker
                # endpoints will return 503 until POST /api/broker/connect.
                # The auto-reconnect monitor below picks it up on the next tick.
                logger.warning(
                    "IBKR client could not connect (%s). Auto-reconnect monitor will retry; "
                    "POST /api/broker/connect or the Status page will also drive a manual attempt.",
                    exc,
                )
        else:
            logger.info(
                "IBKR auto-connect disabled (IBKR_CONNECT_ON_STARTUP=false). "
                "Use POST /api/broker/connect or the Status page to establish the connection."
            )
        # The monitor is started even when initial connect failed — it will
        # observe the disconnected state and retry per the backoff policy.
        # Slice 3 / ADR 0011 amendment — the broker-activity publisher
        # registry's reconnect-recovery sweep rides the same chain as
        # the bar aggregator's resubscribe-all. Order inside the wrapped
        # chain: bar aggregator first (restore market-data subscriptions
        # so the engine sees prices again ASAP), then the broker-activity
        # sweep (replay the day's executions to catch anything missed
        # mid-drop).
        #
        # Slice 3 follow-up — ``run_recovery_chain`` wraps the whole
        # chain in a process-wide submission halt
        # (``any_recovery_active()`` returns True for the entire
        # window). The per-publisher sweep flag alone only covers the
        # sweep slice, so a slow bar resubscribe used to leave
        # ``place_paper_order`` enabled and a submission landing
        # mid-resubscribe could be picked up by the subsequent sweep and
        # mis-authored as a ``reconnect_recovery`` row — the wrapper
        # closes that hole.
        from app.services.broker_activity_publisher_registry import (
            get_publisher_registry as get_broker_activity_publisher_registry,
        )
        from app.services.live_bar_aggregator import LIVE_BAR_AGGREGATOR

        async def _sweep_broker_activity_after_reconnect() -> None:
            await get_broker_activity_publisher_registry().sweep_all_for_recovery()

        async def _run_post_reconnect_recovery_chain() -> None:
            await get_broker_activity_publisher_registry().run_recovery_chain(
                [
                    LIVE_BAR_AGGREGATOR.resubscribe_all,
                    _sweep_broker_activity_after_reconnect,
                ]
            )

        monitor = AutoReconnectMonitor(
            ibkr_client,
            recovery_callbacks=[_run_post_reconnect_recovery_chain],
        )
        monitor.start()
        set_monitor(monitor)

        artifacts_root = account_truth_artifacts_root(ibkr_settings)
        live_runs_root = Path(ibkr_settings.live_runs_root)
        reconciliation_service = AccountReconciliationService(artifacts_root=artifacts_root)

        async def _ensure_connected_account_service(account_id: str) -> object:
            from app.engine.live import host_daemon_client

            health = await host_daemon_client.ensure_account_clerk(
                ibkr_settings.live_runner_daemon_url,
                account_id,
                ibkr_host=ibkr_settings.host,
            )
            reconciliation_service.ensure_automatic_reconciliation(account_id=account_id)
            return health

        account_truth_refresh_loop = AccountTruthRefreshLoop(
            client=ibkr_client,
            artifacts_root=artifacts_root,
            account_truth_observer=reconciliation_service.observe_account_truth,
            account_truth_failure_observer=reconciliation_service.observe_account_truth_failure,
            account_journal_observer=lambda account_id: record_account_journal_parity_observation(
                live_runs_root,
                account_id=account_id,
            ),
            account_service_ensurer=_ensure_connected_account_service,
        )
        account_truth_refresh_loop.start()
    else:
        set_client(None)
        set_monitor(None)
        logger.info(
            "IBKR broker disabled (IBKR_BROKER_ENABLED=false). Broker endpoints disabled. Live-runs router available."
        )

    # ── PRD #619-C2 — daemon connectivity monitor ──────────────────
    # The host live-runner daemon is independent of the IBKR broker:
    # start the connectivity monitor regardless of broker_enabled, so
    # operator-surface composers always have a typed connectivity state
    # to read.
    from app.engine.live.daemon_connectivity_monitor import (
        DaemonConnectivityMonitor,
    )
    from app.engine.live.daemon_connectivity_monitor import (
        set_monitor as set_daemon_monitor,
    )
    from app.engine.live.host_daemon_client import fetch_health

    daemon_monitor: DaemonConnectivityMonitor | None = None
    daemon_url = (ibkr_settings.live_runner_daemon_url or "").strip()
    if daemon_url:
        def _now_ms() -> int:
            return int(time.time() * 1000)

        async def _probe():
            # Monitor discards the full envelope — it only needs the typed
            # result, which already carries ``observed_daemon_boot_id``.
            result, _health = await fetch_health(daemon_url)
            return result

        daemon_monitor = DaemonConnectivityMonitor(probe=_probe, now_ms=_now_ms)
        await daemon_monitor.start()
        set_daemon_monitor(daemon_monitor)
    else:
        set_daemon_monitor(None)
        logger.info(
            "Host daemon URL not configured (live_runner_daemon_url is empty); "
            "daemon connectivity monitor not started."
        )

    # ADR-0028 Stage 2 — each visible bot gets one producer-owned status
    # snapshot before the API begins serving normal reads. The producer owns
    # broker-activity bootstrap and periodic assembly; status GET only reads
    # its stored document.
    await live_instances_router.start_surface_hubs()

    from app.services.cohort_evidence import get_cohort_evidence_sampler_registry
    from app.services.cohort_launch import (
        get_cohort_launch_scheduler_registry,
        resume_open_cohort_evidence_samplers,
        resume_open_cohort_launch_schedulers,
    )

    readonly_default = live_instances_router._resolve_readonly_default(ibkr_settings)

    await resume_open_cohort_launch_schedulers(
        artifacts_root=account_truth_artifacts_root(ibkr_settings),
        live_runs_root=Path(ibkr_settings.live_runs_root),
        run_roll_call=live_instances_router.run_roll_call,
        start_run=live_instances_router.start_run,
        visible_runs_by_instance=live_instances_router._visible_runs_by_instance,
        run_account_id=live_instances_router._run_dir_account_id,
        run_live_config=live_instances_router._cohort_live_config_for_run,
        start_request_for_run=lambda run_dir: live_instances_router._cohort_start_request_for_run(
            run_dir,
            readonly_default=readonly_default,
        ),
        now_ms=lambda: int(time.time() * 1_000),
        evidence_samplers=get_cohort_evidence_sampler_registry(),
        launch_schedulers=get_cohort_launch_scheduler_registry(),
    )

    await resume_open_cohort_evidence_samplers(
        artifacts_root=account_truth_artifacts_root(ibkr_settings),
        live_runs_root=Path(ibkr_settings.live_runs_root),
        visible_runs_by_instance=live_instances_router._visible_runs_by_instance,
        now_ms=lambda: int(time.time() * 1_000),
        evidence_samplers=get_cohort_evidence_sampler_registry(),
    )

    try:
        yield
    finally:
        await get_cohort_launch_scheduler_registry().stop_all()
        await get_cohort_evidence_sampler_registry().stop_all()
        await live_instances_router.stop_surface_hubs()
        await bot_events.get_bot_event_stream_service().stop_all()
        # Stop the daemon monitor first — its probe traffic stops cleanly
        # before any other shutdown step can race it.
        if daemon_monitor is not None:
            await daemon_monitor.stop()
            set_daemon_monitor(None)
        if account_truth_refresh_loop is not None:
            await account_truth_refresh_loop.stop()
        # ADR 0014 — stop every broker-activity publisher before tearing
        # down the broker connection so each publisher's WAL append +
        # subscriber drain completes cleanly. Safe to call even when no
        # publishers were registered (registry stop_all is a no-op).
        from app.services.broker_activity_publisher_registry import (
            get_publisher_registry,
        )

        await get_publisher_registry().stop_all()
        # Stop the broker monitor BEFORE disconnecting so a tick-in-flight
        # doesn't observe the close and immediately try to reconnect.
        if monitor is not None:
            await monitor.stop()
            set_monitor(None)
        if ibkr_client is not None and ibkr_client.is_connected():
            await ibkr_client.disconnect()
        set_client(None)
        logger.info("Shutting down Polygon Data Service")


app = FastAPI(
    title="Polygon Data Service",
    description="Data fetching and sanitization service for Polygon.io market data",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=settings.get_trusted_hosts(),
)


# CORS middleware for C# backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.get_allowed_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
DATA_PLANE_CONTROL_DEPENDENCIES = [Depends(require_data_plane_control_secret)]
PROTECTED_DATA_PLANE_READ_DEPENDENCIES = [Depends(require_data_plane_control_secret_always)]

app.include_router(aggregates.router, prefix="/api/aggregates", tags=["aggregates"])
app.include_router(sanitize.router, prefix="/api", tags=["sanitize"])
app.include_router(indicators.router, prefix="/api/indicators", tags=["indicators"])
app.include_router(options.router, prefix="/api/options", tags=["options"])
app.include_router(snapshot.router, prefix="/api/snapshot", tags=["snapshot"])
app.include_router(market_monitor.router, prefix="/api/market", tags=["market"])
app.include_router(tickers.router, prefix="/api/tickers", tags=["tickers"])
app.include_router(strategy.router, prefix="/api/strategy", tags=["strategy"])
app.include_router(spec_strategy.router, prefix="/api/spec-strategy", tags=["spec-strategy"])
app.include_router(research.router, prefix="/api/research", tags=["research"])
app.include_router(indicator_reliability.router, prefix="/api/research", tags=["research"])
# Research-pipeline walk-forward (Phase C). Registered BEFORE
# ``research_runs`` so the literal ``/walk-forward`` segment wins
# against the ``GET /{run_id}`` route on the parent router.
app.include_router(
    walk_forward.router,
    prefix="/api/research/strategy-runs/walk-forward",
    tags=["research-walk-forward"],
)
# Research-pipeline Monte Carlo (Phase D). Same pre-research_runs
# placement so the literal ``/monte-carlo`` segment wins.
app.include_router(
    monte_carlo.router,
    prefix="/api/research/strategy-runs/monte-carlo",
    tags=["research-monte-carlo"],
)
# Research-pipeline null baselines (Phase E1). Same pre-research_runs
# placement so the literal ``/baselines`` segment wins.
app.include_router(
    baselines.router,
    prefix="/api/research/strategy-runs/baselines",
    tags=["research-baselines"],
)
# Research-pipeline run ledger (Phase A of build-alpha-style features 1-8).
app.include_router(research_runs.router, prefix="/api/research/strategy-runs", tags=["research-runs"])
# Trading-calendar preview — sibling endpoint under ``/api/research`` so
# the date-picker UI can surface skipped sessions before a run is
# submitted. Lives in a separate ``APIRouter`` instance from the
# strategy-runs router because their prefixes differ.
app.include_router(
    research_runs.calendar_router,
    prefix="/api/research",
    tags=["research-trading-calendar"],
)
app.include_router(dataset.router, prefix="/api/dataset", tags=["dataset"])
app.include_router(data_quality.router, prefix="/api/data-quality", tags=["data-quality"])
app.include_router(volatility.router, prefix="/api/volatility", tags=["volatility"])
app.include_router(engine.router, prefix="/api/engine", tags=["engine"])
# LEAN Sidecar Lab — data-plane API in front of the launcher service.
# Phase 2a exposes only the trusted sample; Phase 3+ unlocks user
# algorithm source. See docs/architecture/lean-sidecar-lab.md.
app.include_router(lean_sidecar.router, prefix="/api/lean-sidecar", tags=["lean-sidecar"])
app.include_router(chart.router, prefix="/api/chart", tags=["chart"])
# Portfolio scenario / live-Greeks. Phase 2 of numerical-authority migration:
# Python becomes canonical for portfolio Greeks; .NET becomes a passthrough.
app.include_router(portfolio.router, prefix="/api/portfolio", tags=["portfolio"])
# QuantLib option pricing endpoints (/status, /price, /strategy, /compare).
# Registration was dropped by 88b48ac (IV-surface refactor) on 2026-04-12;
# the four endpoints silently 404'd until pricing-lab surfaced it.
app.include_router(quantlib_options.router, prefix="/api/quantlib", tags=["quantlib"])
# Internal job orchestration (Redis-backed). Mounted under /api/jobs-internal;
# the public surface is the .NET /api/jobs facade in Backend/Jobs/JobsApi.cs.
app.include_router(jobs.router, prefix="/api/jobs-internal", tags=["jobs-internal"])
# Edge router carries its own /api/edge prefix.
app.include_router(edge.router)
# Live IV30 endpoints (vix-style + parametric) — Step C of IV-ownership plan.
# Router carries its own /api/edge/iv30 prefix.
app.include_router(iv30.router)
# IV recorder (POST /api/iv-recorder/snapshot, GET .../series/{ticker}) —
# Step D of IV-ownership plan. Driven by .NET cron; not in-process.
app.include_router(iv_recorder.router)
# /research/data-divergence/* — dashboard + matrix endpoints. The router
# carries its own prefix so we mount it bare.
app.include_router(research_divergence.router)
# Interactive Brokers paper-trading endpoints (Phase 1: read-only chain).
# Router carries its own /api/broker prefix.
app.include_router(broker.router, dependencies=DATA_PLANE_CONTROL_DEPENDENCIES)
# IBKR account/session capability probe (issue #1005 Slice 0).
app.include_router(broker_capability.router, dependencies=DATA_PLANE_CONTROL_DEPENDENCIES)
# Account Truth and account-wide broker ledger endpoints.
app.include_router(broker_account_truth.router, dependencies=DATA_PLANE_CONTROL_DEPENDENCIES)
# Account-scoped reconciliation and recovery triage endpoints.
app.include_router(account_reconciliation.router, dependencies=DATA_PLANE_CONTROL_DEPENDENCIES)
# Operator-authorized deliberate cohort batch launch receipts.
app.include_router(cohort_batch_launch.router, dependencies=DATA_PLANE_CONTROL_DEPENDENCIES)
# Broker session mirror — read-only roster/SSE observatory with sensitive runtime data.
app.include_router(broker_session.router, dependencies=PROTECTED_DATA_PLANE_READ_DEPENDENCIES)
# Golden fixture catalog — reads manifest.json + artifacts/fixture-validation/latest.json.
# No live computation at request time (see docs/process/autonomous-decisions.md D-010).
app.include_router(golden_fixtures.router, prefix="/api", tags=["golden-fixtures"])
app.include_router(
    strategy_validation.router,
    prefix="/api/strategy-validation",
    tags=["strategy-validation"],
    dependencies=DATA_PLANE_CONTROL_DEPENDENCIES,
)
# Authored bot-event stream backfill (ADR 0024 / PRD #928). This is run-scoped
# historical evidence; live delivery comes in a later SSE slice.
app.include_router(
    bot_events.router,
    prefix="/api/live-runs",
    tags=["bot-events"],
    dependencies=DATA_PLANE_CONTROL_DEPENDENCIES,
)
# Live paper-trading run observer (read-only). Three-layer caching:
# Layer 1: 15 s TTL on dir listing; Layer 2: mtime-signature LRU on status;
# Layer 3: inode-tracked incremental deque on log tail.
app.include_router(
    live_runs_router.router,
    prefix="/api/live-runs",
    tags=["live-runs"],
    dependencies=DATA_PLANE_CONTROL_DEPENDENCIES,
)
app.include_router(
    live_instances_router.router,
    prefix="/api/live-instances",
    tags=["live-instances"],
    dependencies=PROTECTED_DATA_PLANE_READ_DEPENDENCIES,
)
app.include_router(lifecycle_projection.router)
# ADR 0014 — broker-activity reconciliation surface (SSE + REST backfill).
# The router carries its own ``/api/live-instances`` prefix internally
# (so the path is sibling to the live-instances router), keeping the
# operator-facing URL space consistent.
app.include_router(
    broker_activity_router.router,
    dependencies=PROTECTED_DATA_PLANE_READ_DEPENDENCIES,
)

# Data lake (Slice 1a) — gated by DATA_LAKE_ENABLED.
# When disabled, the prefix has no registered routes; clients get 404.
if settings.DATA_LAKE_ENABLED:
    from app.routers import data_lake as data_lake_router

    app.include_router(data_lake_router.router)
    logger.info("data lake routes ENABLED")
else:
    logger.info("data lake routes disabled (set DATA_LAKE_ENABLED=true to enable)")

# Exception handler
app.add_exception_handler(Exception, polygon_exception_handler)


@app.get("/health")
async def health_check():
    """Health check endpoint for Docker"""
    return {
        "status": "healthy",
        "service": "polygon-data-service",
        # git HEAD baked in at build (GIT_COMMIT_SHA); None if unset. Lets the
        # operator console confirm the data plane matches master and flag drift
        # against the host daemon's live git_sha.
        "git_sha": settings.GIT_COMMIT_SHA or None,
    }


@app.get("/")
async def root():
    """Root endpoint with API info"""
    return {"service": "Polygon Data Service", "version": "1.0.0", "docs": "/docs", "health": "/health"}
