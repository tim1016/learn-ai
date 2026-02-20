from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.ml.evaluation.walk_forward import walk_forward_validate
from app.ml.models.schemas import TrainingConfig
from app.ml.providers.mock_provider import MockDataProvider


@pytest.mark.slow
class TestWalkForwardValidation:
    def test_basic_validation(self) -> None:
        provider = MockDataProvider(seed=42)
        raw = provider.fetch_ohlcv("TEST", "2022-01-01", "2024-01-01")
        df = pd.DataFrame(raw).sort_values("timestamp").reset_index(drop=True)
        data = df[["close"]].values.astype(np.float64)

        config = TrainingConfig(
            ticker="TEST",
            from_date="2022-01-01",
            to_date="2024-01-01",
            sequence_length=10,
            epochs=2,
            batch_size=16,
            features=["close"],
        )

        result = walk_forward_validate(data, config, n_folds=2)

        assert result.num_folds == 2
        assert result.avg_rmse > 0
        assert len(result.fold_results) == 2

    def test_fold_size_too_small_raises(self) -> None:
        data = np.random.rand(30, 1).astype(np.float64)
        config = TrainingConfig(
            ticker="TEST",
            from_date="2022-01-01",
            to_date="2024-01-01",
            sequence_length=20,
            epochs=1,
            features=["close"],
        )
        with pytest.raises(ValueError, match="too small"):
            walk_forward_validate(data, config, n_folds=5)
