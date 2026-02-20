from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

from app.ml.evaluation.walk_forward import walk_forward_validate
from app.ml.models.schemas import TrainingConfig, TrainingResult, WalkForwardResult
from app.ml.preprocessing.pipeline import DataPipeline
from app.ml.protocols import MarketDataProvider
from app.ml.training.trainer import LSTMTrainer

logger = logging.getLogger(__name__)


class PredictionService:
    """High-level service orchestrating train/predict/validate.

    This is the entry point that both the CLI and a future FastAPI router call.
    """

    def __init__(
        self, provider: MarketDataProvider, model_dir: Path | None = None
    ) -> None:
        self._provider = provider
        self._trainer = LSTMTrainer(model_dir=model_dir or Path("trained_models"))

    def train(
        self, config: TrainingConfig
    ) -> tuple[TrainingResult, np.ndarray, np.ndarray, dict]:
        """Run the full training pipeline.

        Returns:
            Tuple of (TrainingResult, test predictions, test actuals, history dict).
        """
        pipeline = DataPipeline(self._provider)
        X_train, X_test, y_train, y_test, scaler = pipeline.prepare(config)
        result, model, history = self._trainer.train(
            config, X_train, X_test, y_train, y_test
        )

        # Save scaler alongside model
        scaler_path = Path(result.model_path).with_suffix(".scaler.json")
        scaler.save(scaler_path)

        # Generate predictions for visualization
        test_pred = model.predict(X_test, verbose=0).flatten()

        return result, test_pred, y_test, history

    def validate(
        self, config: TrainingConfig, n_folds: int = 5
    ) -> WalkForwardResult:
        """Run walk-forward validation."""
        raw = self._provider.fetch_ohlcv(
            ticker=config.ticker,
            from_date=config.from_date,
            to_date=config.to_date,
        )
        df = pd.DataFrame(raw).sort_values("timestamp").reset_index(drop=True)

        if "returns" in config.features:
            df["returns"] = df["close"].pct_change().fillna(0)

        data = df[config.features].values.astype(np.float64)
        return walk_forward_validate(data, config, n_folds)
