from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from app.ml.evaluation.walk_forward import walk_forward_validate
from app.ml.models.api_schemas import ModelInfo, TrainJobResult, ValidateJobResult, ValidateFoldResult
from app.ml.models.schemas import TrainingConfig, TrainingResult, WalkForwardResult
from app.ml.preprocessing.pipeline import DataPipeline
from app.ml.protocols import MarketDataProvider
from app.ml.training.trainer import LSTMTrainer

logger = logging.getLogger(__name__)


class PredictionService:
    """High-level service orchestrating train/predict/validate.

    This is the entry point that the CLI, FastAPI router, and future API call.
    """

    def __init__(
        self, provider: MarketDataProvider, model_dir: Path | None = None
    ) -> None:
        self._provider = provider
        self._model_dir = model_dir or Path("trained_models")
        self._trainer = LSTMTrainer(model_dir=self._model_dir)

    def train(
        self, config: TrainingConfig
    ) -> tuple[TrainingResult, np.ndarray, np.ndarray, dict]:
        """Run the full training pipeline.

        Returns:
            Tuple of (TrainingResult, test predictions, test actuals, history dict).
        """
        pipeline = DataPipeline(self._provider)
        X_train, X_test, y_train, y_test, scaler, stationarity = pipeline.prepare(config)
        result, model, history = self._trainer.train(
            config, X_train, X_test, y_train, y_test
        )

        # Attach stationarity results to training result
        if stationarity is not None:
            result.stationarity_adf_pvalue = round(stationarity.adf_pvalue, 6)
            result.stationarity_kpss_pvalue = round(stationarity.kpss_pvalue, 6)
            result.stationarity_is_stationary = stationarity.is_stationary

        # Save scaler alongside model
        scaler_path = Path(result.model_path).with_suffix(".scaler.json")
        scaler.save(scaler_path)

        # Save model metadata JSON for list_models()
        meta_path = Path(result.model_path).with_suffix(".meta.json")
        meta = {
            "ticker": result.ticker,
            "val_rmse": result.val_rmse,
            "train_rmse": result.train_rmse,
            "baseline_rmse": result.baseline_rmse,
            "improvement": result.improvement_over_baseline,
            "epochs_completed": result.epochs_completed,
            "best_epoch": result.best_epoch,
            "sequence_length": config.sequence_length,
            "features": config.features,
            "scaler_type": config.scaler_type,
            "log_returns": config.log_returns,
            "winsorize": config.winsorize,
            "stationarity_adf_pvalue": result.stationarity_adf_pvalue,
            "stationarity_kpss_pvalue": result.stationarity_kpss_pvalue,
            "stationarity_is_stationary": result.stationarity_is_stationary,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        meta_path.write_text(json.dumps(meta, indent=2))

        # Generate predictions for visualization
        test_pred = model.predict(X_test, verbose=0).flatten()

        return result, test_pred, y_test, history

    def train_for_api(self, config: TrainingConfig) -> TrainJobResult:
        """Run training and return chart-ready API response."""
        result, test_pred, y_test, history = self.train(config)
        residuals = (y_test - test_pred).tolist()

        return TrainJobResult(
            ticker=result.ticker,
            val_rmse=result.val_rmse,
            train_rmse=result.train_rmse,
            baseline_rmse=result.baseline_rmse,
            improvement=result.improvement_over_baseline,
            epochs_completed=result.epochs_completed,
            best_epoch=result.best_epoch,
            model_id=Path(result.model_path).stem,
            actual_values=y_test.tolist(),
            predicted_values=test_pred.tolist(),
            history_loss=[float(v) for v in history["loss"]],
            history_val_loss=[float(v) for v in history["val_loss"]],
            residuals=residuals,
            stationarity_adf_pvalue=result.stationarity_adf_pvalue,
            stationarity_kpss_pvalue=result.stationarity_kpss_pvalue,
            stationarity_is_stationary=result.stationarity_is_stationary,
        )

    def validate(
        self, config: TrainingConfig, n_folds: int = 5
    ) -> WalkForwardResult:
        """Run walk-forward validation."""
        raw = self._provider.fetch_ohlcv(
            ticker=config.ticker,
            from_date=config.from_date,
            to_date=config.to_date,
            timespan=config.timespan,
            multiplier=config.multiplier,
        )
        df = pd.DataFrame(raw).sort_values("timestamp").reset_index(drop=True)

        if "returns" in config.features:
            df["returns"] = df["close"].pct_change().fillna(0)

        if "log_return" in config.features or config.log_returns:
            df["log_return"] = np.log(df["close"] / df["close"].shift(1))

        # Apply feature shifting to prevent look-ahead bias
        feature_cols = list(config.features)
        df[feature_cols] = df[feature_cols].shift(1)
        df = df.dropna().reset_index(drop=True)

        data = df[feature_cols].values.astype(np.float64)
        return walk_forward_validate(data, config, n_folds)

    def validate_for_api(self, config: TrainingConfig, n_folds: int = 5) -> ValidateJobResult:
        """Run validation and return API-ready response."""
        result = self.validate(config, n_folds)
        return ValidateJobResult(
            ticker=result.ticker,
            num_folds=result.num_folds,
            avg_rmse=result.avg_rmse,
            avg_mae=result.avg_mae,
            avg_mape=result.avg_mape,
            avg_directional_accuracy=result.avg_directional_accuracy,
            avg_sharpe_ratio=result.avg_sharpe_ratio,
            avg_max_drawdown=result.avg_max_drawdown,
            avg_profit_factor=result.avg_profit_factor,
            fold_results=[
                ValidateFoldResult(**f) for f in result.fold_results
            ],
        )

    def list_models(self) -> list[ModelInfo]:
        """List all trained models with metadata."""
        models: list[ModelInfo] = []
        for meta_path in sorted(self._model_dir.glob("*.meta.json")):
            try:
                meta = json.loads(meta_path.read_text())
                model_id = meta_path.stem.replace(".meta", "")
                models.append(ModelInfo(
                    model_id=model_id,
                    ticker=meta["ticker"],
                    created_at=meta.get("created_at", "unknown"),
                    val_rmse=meta["val_rmse"],
                    train_rmse=meta["train_rmse"],
                    baseline_rmse=meta["baseline_rmse"],
                    improvement=meta["improvement"],
                    epochs_completed=meta["epochs_completed"],
                    best_epoch=meta["best_epoch"],
                    sequence_length=meta.get("sequence_length", 60),
                    features=meta.get("features", ["close"]),
                ))
            except Exception as e:
                logger.warning(f"[ML] Failed to read model metadata {meta_path}: {e}")
        return models
