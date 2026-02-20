from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


class PredictionTarget(str, Enum):
    CLOSE_PRICE = "close_price"
    RETURNS = "returns"


class TrainingConfig(BaseModel):
    """Configuration for LSTM training."""

    ticker: str = Field(..., min_length=1, max_length=20)
    from_date: str = Field(..., description="Start date YYYY-MM-DD")
    to_date: str = Field(..., description="End date YYYY-MM-DD")
    target: PredictionTarget = PredictionTarget.CLOSE_PRICE
    sequence_length: int = Field(
        60, ge=10, le=252, description="Lookback window in days"
    )
    train_split: float = Field(0.8, gt=0.5, lt=1.0)
    epochs: int = Field(50, ge=1, le=500)
    batch_size: int = Field(32, ge=1, le=512)
    lstm_units: int = Field(50, ge=10, le=256)
    lstm_layers: int = Field(2, ge=1, le=4)
    dropout: float = Field(0.2, ge=0.0, le=0.5)
    learning_rate: float = Field(0.001, gt=0.0, le=0.1)
    features: List[str] = Field(
        default=["close"], description="Columns to use as features"
    )

    @field_validator("features")
    @classmethod
    def validate_features(cls, v: List[str]) -> List[str]:
        valid = {"open", "high", "low", "close", "volume", "vwap", "returns"}
        invalid = set(v) - valid
        if invalid:
            raise ValueError(f"Invalid features: {invalid}. Must be subset of {valid}")
        return v


class TrainingResult(BaseModel):
    """Result from a completed training run."""

    ticker: str
    config: TrainingConfig
    train_loss: float
    val_loss: float
    train_rmse: float
    val_rmse: float
    baseline_rmse: float
    improvement_over_baseline: float
    epochs_completed: int
    best_epoch: int
    model_path: Optional[str] = None


class PredictionRequest(BaseModel):
    """Request to generate a prediction."""

    ticker: str = Field(..., min_length=1, max_length=20)
    model_path: str = Field(..., description="Path to trained model file")
    horizon: int = Field(1, ge=1, le=30, description="Days ahead to predict")


class PredictionResult(BaseModel):
    """Result of a prediction."""

    ticker: str
    predictions: List[float]
    horizon: int
    confidence_note: str = (
        "LSTM predictions are experimental and not financial advice"
    )


class WalkForwardResult(BaseModel):
    """Result from walk-forward validation."""

    ticker: str
    num_folds: int
    avg_rmse: float
    avg_mae: float
    avg_mape: float
    avg_directional_accuracy: float
    fold_results: List[dict]
