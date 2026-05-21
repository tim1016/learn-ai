"""FastAPI application entry point"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.broker.ibkr.client import (
    BrokerError,
    ConnectionRefusedDueToSentinelError,
    IbkrClient,
    set_client,
)
from app.config import settings
from app.routers import (
    aggregates,
    baselines,
    broker,
    chart,
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
    lean_lint,
    lean_sidecar,
    market_monitor,
    monte_carlo,
    options,
    portfolio,
    quantlib_options,
    reconcile_trades,
    research,
    research_divergence,
    research_runs,
    sanitize,
    snapshot,
    spec_strategy,
    strategy,
    tickers,
    volatility,
    walk_forward,
)
from app.routers import (
    live_runs as live_runs_router,
)
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

    if ibkr_settings.broker_enabled:
        ibkr_client = IbkrClient()
        # Install the client immediately so /health reports the
        # disconnected-but-available state and POST /api/broker/connect can
        # drive the lifecycle from the Status page. Without this, a soft-fail
        # auto-connect leaves _client=None and the only fix is restarting
        # the container.
        set_client(ibkr_client)
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
                logger.warning(
                    "IBKR client could not connect (%s). Use POST /api/broker/connect or the Status page to retry.",
                    exc,
                )
        else:
            logger.info(
                "IBKR auto-connect disabled (IBKR_CONNECT_ON_STARTUP=false). "
                "Use POST /api/broker/connect or the Status page to establish the connection."
            )
    else:
        set_client(None)
        logger.info(
            "IBKR broker disabled (IBKR_BROKER_ENABLED=false). Broker endpoints disabled. Live-runs router available."
        )

    try:
        yield
    finally:
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

# CORS middleware for C# backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.get_allowed_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
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
app.include_router(dataset.router, prefix="/api/dataset", tags=["dataset"])
app.include_router(data_quality.router, prefix="/api/data-quality", tags=["data-quality"])
app.include_router(volatility.router, prefix="/api/volatility", tags=["volatility"])
app.include_router(engine.router, prefix="/api/engine", tags=["engine"])
# LEAN Sidecar Lab — data-plane API in front of the launcher service.
# Phase 2a exposes only the trusted sample; Phase 3+ unlocks user
# algorithm source. See docs/architecture/lean-sidecar-lab.md.
app.include_router(lean_sidecar.router, prefix="/api/lean-sidecar", tags=["lean-sidecar"])
# PR B.5 (2026-05-19) — ruff-backed lint endpoint for the unified Engine Lab's
# LEAN script editor. Carries its own ``/api/lean-sidecar`` prefix on the
# router, mirroring ``reconcile_trades.router`` above.
app.include_router(lean_lint.router)
# PR B (2026-05-19) Phase 4 — POST /api/lean-sidecar/reconcile-trades.
# Wraps the canonical ``reconcile_trade_lists`` helper so the .NET
# ``RunCompareService`` can delegate trade-by-trade reconciliation to
# Python instead of porting ``DivergenceCategory`` to C#.  The router
# carries its own ``/api/lean-sidecar`` prefix; no additional mount
# prefix is required.
app.include_router(reconcile_trades.router)
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
app.include_router(broker.router)
# Golden fixture catalog — reads manifest.json + artifacts/fixture-validation/latest.json.
# No live computation at request time (see docs/process/autonomous-decisions.md D-010).
app.include_router(golden_fixtures.router, prefix="/api", tags=["golden-fixtures"])
# Live paper-trading run observer (read-only). Three-layer caching:
# Layer 1: 15 s TTL on dir listing; Layer 2: mtime-signature LRU on status;
# Layer 3: inode-tracked incremental deque on log tail.
app.include_router(live_runs_router.router, prefix="/api/live-runs", tags=["live-runs"])

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
    return {"status": "healthy", "service": "polygon-data-service"}


@app.get("/")
async def root():
    """Root endpoint with API info"""
    return {"service": "Polygon Data Service", "version": "1.0.0", "docs": "/docs", "health": "/health"}
