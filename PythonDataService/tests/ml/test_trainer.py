from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from app.ml.models.schemas import TrainingConfig
from app.ml.preprocessing.pipeline import DataPipeline
from app.ml.providers.mock_provider import MockDataProvider
from app.ml.training.trainer import LSTMTrainer


@pytest.mark.slow
class TestLSTMTrainer:
    def test_train_completes(self, tmp_path: Path) -> None:
        provider = MockDataProvider(seed=42)
        config = TrainingConfig(
            ticker="TEST",
            from_date="2022-01-01",
            to_date="2024-01-01",
            sequence_length=10,
            epochs=2,
            batch_size=16,
            features=["close"],
        )

        pipeline = DataPipeline(provider)
        X_train, X_test, y_train, y_test, scaler = pipeline.prepare(config)

        trainer = LSTMTrainer(model_dir=tmp_path)
        result, model, history = trainer.train(config, X_train, X_test, y_train, y_test)

        assert result.val_rmse > 0
        assert result.baseline_rmse > 0
        assert result.epochs_completed >= 1
        assert result.best_epoch >= 1
        assert Path(result.model_path).exists()

    def test_train_produces_history(self, tmp_path: Path) -> None:
        provider = MockDataProvider(seed=42)
        config = TrainingConfig(
            ticker="TEST",
            from_date="2022-01-01",
            to_date="2024-01-01",
            sequence_length=10,
            epochs=2,
            batch_size=16,
            features=["close"],
        )

        pipeline = DataPipeline(provider)
        X_train, X_test, y_train, y_test, scaler = pipeline.prepare(config)

        trainer = LSTMTrainer(model_dir=tmp_path)
        result, model, history = trainer.train(config, X_train, X_test, y_train, y_test)

        assert "loss" in history
        assert "val_loss" in history
        assert len(history["loss"]) >= 1
