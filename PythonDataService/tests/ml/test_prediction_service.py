from __future__ import annotations

from pathlib import Path

import pytest

from app.ml.models.schemas import TrainingConfig
from app.ml.providers.mock_provider import MockDataProvider
from app.ml.services.prediction_service import PredictionService


@pytest.mark.slow
class TestPredictionService:
    def test_train_returns_result(self, tmp_path: Path) -> None:
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

        service = PredictionService(provider, model_dir=tmp_path)
        result, test_pred, y_test, history = service.train(config)

        assert result.ticker == "TEST"
        assert result.val_rmse > 0
        assert result.baseline_rmse > 0
        assert len(test_pred) == len(y_test)
        assert Path(result.model_path).exists()

        # Scaler should be saved alongside model
        scaler_path = Path(result.model_path).with_suffix(".scaler.json")
        assert scaler_path.exists()

    def test_train_saves_model_and_scaler(self, tmp_path: Path) -> None:
        provider = MockDataProvider(seed=42)
        config = TrainingConfig(
            ticker="SVC",
            from_date="2022-01-01",
            to_date="2024-01-01",
            sequence_length=10,
            epochs=2,
            batch_size=16,
            features=["close"],
        )

        service = PredictionService(provider, model_dir=tmp_path)
        result, _, _, _ = service.train(config)

        model_path = Path(result.model_path)
        assert model_path.exists()
        assert model_path.suffix == ".keras"

        scaler_path = model_path.with_suffix(".scaler.json")
        assert scaler_path.exists()
