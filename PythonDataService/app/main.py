"""FastAPI application entry point"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from contextlib import asynccontextmanager
import logging

from app.config import settings
from app.routers import aggregates, sanitize, indicators, options, snapshot, market_monitor, tickers, strategy, research, dataset, data_quality, chart, rule_based_backtest, backtest, validation_study, engine, quantlib_options
from app.utils.error_handlers import polygon_exception_handler

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events"""
    logger.info(f"Starting Polygon Data Service on {settings.HOST}:{settings.PORT}")
    logger.info(f"Polygon API Key configured: {bool(settings.POLYGON_API_KEY)}")
    yield
    logger.info("Shutting down Polygon Data Service")


app = FastAPI(
    title="Polygon Data Service",
    description="Data fetching and sanitization service for Polygon.io market data",
    version="1.0.0",
    lifespan=lifespan
)

# GZip middleware for large chart responses
app.add_middleware(GZipMiddleware, minimum_size=1000)

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
app.include_router(research.router, prefix="/api/research", tags=["research"])
app.include_router(dataset.router, prefix="/api/dataset", tags=["dataset"])
app.include_router(data_quality.router, prefix="/api/data-quality", tags=["data-quality"])
app.include_router(chart.router, prefix="/api/chart", tags=["chart"])
app.include_router(rule_based_backtest.router, prefix="/api/backtest/rule-based", tags=["rule-based-backtest"])
app.include_router(backtest.router, prefix="/api/backtest", tags=["backtest"])
app.include_router(validation_study.router, prefix="/api/validation-study", tags=["validation-study"])
app.include_router(engine.router, prefix="/api/engine", tags=["engine"])
app.include_router(quantlib_options.router, prefix="/api/quantlib", tags=["quantlib"])

# Exception handler
app.add_exception_handler(Exception, polygon_exception_handler)


@app.get("/health")
async def health_check():
    """Health check endpoint for Docker"""
    return {"status": "healthy", "service": "polygon-data-service"}


@app.get("/")
async def root():
    """Root endpoint with API info"""
    return {
        "service": "Polygon Data Service",
        "version": "1.0.0",
        "docs": "/docs",
        "health": "/health"
    }
