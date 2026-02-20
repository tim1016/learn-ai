from __future__ import annotations

from typing import Any, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field


class TrainRequest(BaseModel):
    """API request to start LSTM training."""

    ticker: str = Field(..., min_length=1, max_length=20)
    from_date: str = Field(..., description="Start date YYYY-MM-DD")
    to_date: str = Field(..., description="End date YYYY-MM-DD")
    epochs: int = Field(50, ge=1, le=500)
    sequence_length: int = Field(60, ge=10, le=252)
    features: str = Field("close", description="Comma-separated feature list")
    mock: bool = Field(False, description="Use mock data instead of Polygon")
    scaler_type: Literal["minmax", "standard", "robust"] = Field(
        "standard", description="Scaler type"
    )
    log_returns: bool = Field(False, description="Use log returns")
    winsorize: bool = Field(False, description="Clip extreme values")
    timespan: str = Field("day", description="Aggregation timespan: minute, hour, day")
    multiplier: int = Field(1, ge=1, le=60, description="Timespan multiplier")


class ValidateRequest(BaseModel):
    """API request to start walk-forward validation."""

    ticker: str = Field(..., min_length=1, max_length=20)
    from_date: str = Field(..., description="Start date YYYY-MM-DD")
    to_date: str = Field(..., description="End date YYYY-MM-DD")
    folds: int = Field(5, ge=2, le=10)
    epochs: int = Field(20, ge=1, le=200)
    sequence_length: int = Field(60, ge=10, le=252)
    mock: bool = Field(False, description="Use mock data instead of Polygon")
    scaler_type: Literal["minmax", "standard", "robust"] = Field(
        "standard", description="Scaler type"
    )
    log_returns: bool = Field(False, description="Use log returns")
    winsorize: bool = Field(False, description="Clip extreme values")
    timespan: str = Field("day", description="Aggregation timespan: minute, hour, day")
    multiplier: int = Field(1, ge=1, le=60, description="Timespan multiplier")


class JobSubmitResponse(BaseModel):
    """Response after submitting a job."""

    job_id: str
    status: str = "pending"


class TrainJobResult(BaseModel):
    """Training results with chart-ready data."""

    ticker: str
    val_rmse: float
    train_rmse: float
    baseline_rmse: float
    improvement: float
    epochs_completed: int
    best_epoch: int
    model_id: str
    actual_values: List[float]
    predicted_values: List[float]
    history_loss: List[float]
    history_val_loss: List[float]
    residuals: List[float]
    stationarity_adf_pvalue: Optional[float] = None
    stationarity_kpss_pvalue: Optional[float] = None
    stationarity_is_stationary: Optional[bool] = None


class ValidateFoldResult(BaseModel):
    """Single fold result from walk-forward validation."""

    fold: int
    train_size: int
    test_size: int
    rmse: float
    mae: float
    mape: float
    directional_accuracy: float
    sharpe_ratio: Optional[float] = None
    max_drawdown: Optional[float] = None
    profit_factor: Optional[float] = None


class ValidateJobResult(BaseModel):
    """Walk-forward validation results."""

    ticker: str
    num_folds: int
    avg_rmse: float
    avg_mae: float
    avg_mape: float
    avg_directional_accuracy: float
    avg_sharpe_ratio: Optional[float] = None
    avg_max_drawdown: Optional[float] = None
    avg_profit_factor: Optional[float] = None
    fold_results: List[ValidateFoldResult]


class JobStatusResponse(BaseModel):
    """Job status with optional results."""

    job_id: str
    status: str
    result: Optional[Any] = None
    error: Optional[str] = None
    created_at: Optional[str] = None
    completed_at: Optional[str] = None


class ModelInfo(BaseModel):
    """Metadata for a saved model."""

    model_id: str
    ticker: str
    created_at: str
    val_rmse: float
    train_rmse: float
    baseline_rmse: float
    improvement: float
    epochs_completed: int
    best_epoch: int
    sequence_length: int
    features: List[str]
