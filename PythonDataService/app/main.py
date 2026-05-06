"""FastAPI application entry point"""

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
    broker,
    chart,
    data_quality,
    dataset,
    edge,
    engine,
    indicator_reliability,
    indicators,
    iv30,
    iv_recorder,
    jobs,
    market_monitor,
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
    tickers,
    volatility,
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
    """
    logger.info(f"Starting Polygon Data Service on {settings.HOST}:{settings.PORT}")
    logger.info(f"Polygon API Key configured: {bool(settings.POLYGON_API_KEY)}")

    ibkr_client = IbkrClient()
    try:
        await ibkr_client.connect()
        set_client(ibkr_client)
        logger.info("IBKR client connected; broker endpoints available.")
    except ConnectionRefusedDueToSentinelError:
        # Hard fail — never proceed past a paper/live mismatch.
        logger.exception("IBKR sentinel mismatch — aborting startup.")
        raise
    except (BrokerError, OSError) as exc:
        # Soft fail — Gateway is probably not running locally. Broker
        # endpoints will return 503 until a future reconnect.
        logger.warning(
            "IBKR client could not connect (%s). Broker endpoints will return 503.",
            exc,
        )
        set_client(None)

    try:
        yield
    finally:
        if ibkr_client.is_connected():
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
# Research-pipeline run ledger (Phase A of build-alpha-style features 1-8).
app.include_router(research_runs.router, prefix="/api/research/strategy-runs", tags=["research-runs"])
app.include_router(dataset.router, prefix="/api/dataset", tags=["dataset"])
app.include_router(data_quality.router, prefix="/api/data-quality", tags=["data-quality"])
app.include_router(volatility.router, prefix="/api/volatility", tags=["volatility"])
app.include_router(engine.router, prefix="/api/engine", tags=["engine"])
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
