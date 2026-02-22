"""FastAPI application entry point"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import logging

from app.config import settings
from app.routers import aggregates, sanitize, indicators, options, snapshot, market_monitor, tickers, predictions, strategy
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
app.include_router(predictions.router, prefix="/api/predictions", tags=["predictions"])
app.include_router(strategy.router, prefix="/api/strategy", tags=["strategy"])

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
