"""API endpoints for LSTM stock price predictions."""
from fastapi import APIRouter, HTTPException, status
import logging

from app.ml.job_manager import job_manager
from app.ml.models.api_schemas import (
    JobStatusResponse,
    JobSubmitResponse,
    ModelInfo,
    TrainRequest,
    ValidateRequest,
)
from app.ml.models.schemas import TrainingConfig
from app.ml.providers.mock_provider import MockDataProvider
from app.ml.providers.polygon_provider import PolygonDataProvider
from app.ml.services.prediction_service import PredictionService

router = APIRouter()
logger = logging.getLogger(__name__)


def _get_service(mock: bool = False) -> PredictionService:
    """Create a PredictionService with the appropriate provider."""
    provider = MockDataProvider() if mock else PolygonDataProvider()
    return PredictionService(provider)


def _run_training(
    ticker: str,
    from_date: str,
    to_date: str,
    epochs: int,
    sequence_length: int,
    features: str,
    mock: bool,
) -> dict:
    """Synchronous training function to run in background thread."""
    service = _get_service(mock)
    config = TrainingConfig(
        ticker=ticker,
        from_date=from_date,
        to_date=to_date,
        epochs=epochs,
        sequence_length=sequence_length,
        features=features.split(","),
    )
    result = service.train_for_api(config)
    return result.model_dump()


def _run_validation(
    ticker: str,
    from_date: str,
    to_date: str,
    folds: int,
    epochs: int,
    sequence_length: int,
    mock: bool,
) -> dict:
    """Synchronous validation function to run in background thread."""
    service = _get_service(mock)
    config = TrainingConfig(
        ticker=ticker,
        from_date=from_date,
        to_date=to_date,
        epochs=epochs,
        sequence_length=sequence_length,
    )
    result = service.validate_for_api(config, n_folds=folds)
    return result.model_dump()


@router.post("/train", response_model=JobSubmitResponse)
async def start_training(request: TrainRequest) -> JobSubmitResponse:
    """Start an LSTM training job in the background."""
    logger.info(f"[ML] Training request: {request.ticker}")

    job_id = job_manager.submit(
        _run_training,
        ticker=request.ticker,
        from_date=request.from_date,
        to_date=request.to_date,
        epochs=request.epochs,
        sequence_length=request.sequence_length,
        features=request.features,
        mock=request.mock,
    )

    return JobSubmitResponse(job_id=job_id)


@router.post("/validate", response_model=JobSubmitResponse)
async def start_validation(request: ValidateRequest) -> JobSubmitResponse:
    """Start a walk-forward validation job in the background."""
    logger.info(f"[ML] Validation request: {request.ticker}")

    job_id = job_manager.submit(
        _run_validation,
        ticker=request.ticker,
        from_date=request.from_date,
        to_date=request.to_date,
        folds=request.folds,
        epochs=request.epochs,
        sequence_length=request.sequence_length,
        mock=request.mock,
    )

    return JobSubmitResponse(job_id=job_id)


@router.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job_status(job_id: str) -> JobStatusResponse:
    """Get the status and results of a job."""
    job = job_manager.get_status(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id} not found",
        )

    return JobStatusResponse(
        job_id=job_id,
        status=job["status"],
        result=job.get("result"),
        error=job.get("error"),
        created_at=job.get("created_at"),
        completed_at=job.get("completed_at"),
    )


@router.get("/models", response_model=list[ModelInfo])
async def list_models() -> list[ModelInfo]:
    """List all trained models with their metadata."""
    service = _get_service(mock=False)
    return service.list_models()
