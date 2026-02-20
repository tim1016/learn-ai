from __future__ import annotations

import numpy as np
import pytest

from app.ml.models.schemas import TrainingConfig
from app.ml.providers.mock_provider import MockDataProvider


@pytest.fixture
def mock_provider() -> MockDataProvider:
    return MockDataProvider(seed=42)


@pytest.fixture
def minimal_config() -> TrainingConfig:
    """Fast training config for tests."""
    return TrainingConfig(
        ticker="TEST",
        from_date="2022-01-01",
        to_date="2024-01-01",
        sequence_length=10,
        epochs=2,
        batch_size=16,
        features=["close"],
    )


@pytest.fixture
def sample_scaled_data() -> np.ndarray:
    """100 data points, 1 feature, in [0, 1] range."""
    rng = np.random.default_rng(42)
    return rng.random((100, 1)).astype(np.float32)
