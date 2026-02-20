from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
from tensorflow import keras

from app.ml.evaluation.metrics import calculate_rmse
from app.ml.models.schemas import TrainingConfig, TrainingResult
from app.ml.training.baseline import PersistenceBaseline
from app.ml.training.lstm_model import build_lstm_model

logger = logging.getLogger(__name__)

DEFAULT_MODEL_DIR = Path("trained_models")


class LSTMTrainer:
    """Handles the full training lifecycle."""

    def __init__(self, model_dir: Path = DEFAULT_MODEL_DIR) -> None:
        self._model_dir = model_dir
        self._model_dir.mkdir(parents=True, exist_ok=True)

    def train(
        self,
        config: TrainingConfig,
        X_train: np.ndarray,
        X_test: np.ndarray,
        y_train: np.ndarray,
        y_test: np.ndarray,
    ) -> tuple[TrainingResult, keras.Model, dict]:
        """Train LSTM, compare to baseline, return results.

        Returns:
            Tuple of (TrainingResult, trained model, history dict).
        """
        input_shape = (X_train.shape[1], X_train.shape[2])
        model = build_lstm_model(config, input_shape)

        logger.info(
            f"[ML] Training: {config.epochs} epochs, "
            f"batch_size={config.batch_size}, "
            f"train_samples={X_train.shape[0]}, test_samples={X_test.shape[0]}"
        )

        callbacks = [
            keras.callbacks.EarlyStopping(
                monitor="val_loss",
                patience=10,
                restore_best_weights=True,
            ),
            keras.callbacks.ReduceLROnPlateau(
                monitor="val_loss",
                factor=0.5,
                patience=5,
                min_lr=1e-6,
            ),
        ]

        history = model.fit(
            X_train,
            y_train,
            validation_data=(X_test, y_test),
            epochs=config.epochs,
            batch_size=config.batch_size,
            callbacks=callbacks,
            verbose=1,
        )

        # Evaluate
        train_pred = model.predict(X_train, verbose=0).flatten()
        test_pred = model.predict(X_test, verbose=0).flatten()

        train_rmse = calculate_rmse(y_train, train_pred)
        val_rmse = calculate_rmse(y_test, test_pred)

        # Baseline comparison
        baseline_pred = PersistenceBaseline.predict(y_test)
        baseline_rmse = calculate_rmse(y_test, baseline_pred)

        improvement = (
            ((baseline_rmse - val_rmse) / baseline_rmse) * 100
            if baseline_rmse > 0
            else 0.0
        )

        # Save model
        model_path = self._model_dir / f"{config.ticker}_lstm.keras"
        model.save(model_path)
        logger.info(f"[ML] Model saved to {model_path}")

        best_epoch = int(np.argmin(history.history["val_loss"])) + 1

        result = TrainingResult(
            ticker=config.ticker,
            config=config,
            train_loss=float(history.history["loss"][-1]),
            val_loss=float(min(history.history["val_loss"])),
            train_rmse=round(train_rmse, 6),
            val_rmse=round(val_rmse, 6),
            baseline_rmse=round(baseline_rmse, 6),
            improvement_over_baseline=round(improvement, 2),
            epochs_completed=len(history.history["loss"]),
            best_epoch=best_epoch,
            model_path=str(model_path),
        )

        logger.info(
            f"[ML] Training complete: val_RMSE={val_rmse:.6f}, "
            f"baseline_RMSE={baseline_rmse:.6f}, "
            f"improvement={improvement:.2f}%"
        )

        return result, model, history.history
