from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from app.ml.models.schemas import TrainingConfig
from app.ml.preprocessing.pipeline import DataPipeline
from app.ml.preprocessing.scaler import PriceScaler
from app.ml.preprocessing.windowing import create_sequences, train_test_split_temporal
from app.ml.providers.mock_provider import MockDataProvider


class TestPriceScaler:
    def test_fit_transform_range(self) -> None:
        data = np.array([[10.0], [20.0], [30.0], [40.0], [50.0]])
        scaler = PriceScaler()
        scaled = scaler.fit_transform(data)
        assert scaled.min() >= 0.0
        assert scaled.max() <= 1.0

    def test_inverse_transform_roundtrip(self) -> None:
        data = np.array([[100.0], [200.0], [150.0], [175.0]])
        scaler = PriceScaler()
        scaled = scaler.fit_transform(data)
        recovered = scaler.inverse_transform(scaled)
        np.testing.assert_allclose(data, recovered, atol=1e-10)

    def test_save_load_roundtrip(self, tmp_path: Path) -> None:
        data = np.array([[10.0, 100.0], [20.0, 200.0], [30.0, 300.0]])
        scaler = PriceScaler()
        scaled = scaler.fit_transform(data)

        save_path = tmp_path / "scaler.json"
        scaler.save(save_path)

        new_scaler = PriceScaler()
        new_scaler.load(save_path)

        test_data = np.array([[15.0, 150.0]])
        expected = scaler.transform(test_data)
        actual = new_scaler.transform(test_data)
        np.testing.assert_allclose(expected, actual, atol=1e-10)

    def test_transform_before_fit_raises(self) -> None:
        scaler = PriceScaler()
        with pytest.raises(RuntimeError, match="not fitted"):
            scaler.transform(np.array([[1.0]]))

    def test_inverse_transform_before_fit_raises(self) -> None:
        scaler = PriceScaler()
        with pytest.raises(RuntimeError, match="not fitted"):
            scaler.inverse_transform(np.array([[0.5]]))


class TestCreateSequences:
    def test_shape(self, sample_scaled_data: np.ndarray) -> None:
        X, y = create_sequences(sample_scaled_data, sequence_length=10)
        assert X.shape == (90, 10, 1)
        assert y.shape == (90,)

    def test_target_values(self, sample_scaled_data: np.ndarray) -> None:
        X, y = create_sequences(sample_scaled_data, sequence_length=10)
        for i in range(len(y)):
            assert y[i] == pytest.approx(sample_scaled_data[10 + i, 0], abs=1e-6)

    def test_insufficient_data_raises(self) -> None:
        data = np.random.rand(5, 1).astype(np.float32)
        with pytest.raises(ValueError, match="must exceed"):
            create_sequences(data, sequence_length=10)

    def test_multi_feature(self) -> None:
        data = np.random.rand(50, 3).astype(np.float32)
        X, y = create_sequences(data, sequence_length=10, target_col_index=0)
        assert X.shape == (40, 10, 3)
        assert y.shape == (40,)


class TestTrainTestSplitTemporal:
    def test_split_ratio(self) -> None:
        X = np.random.rand(100, 10, 1)
        y = np.random.rand(100)
        X_train, X_test, y_train, y_test = train_test_split_temporal(X, y, 0.8)
        assert len(X_train) == 80
        assert len(X_test) == 20
        assert len(y_train) == 80
        assert len(y_test) == 20

    def test_no_shuffle(self) -> None:
        X = np.arange(100).reshape(100, 1, 1)
        y = np.arange(100)
        X_train, X_test, _, _ = train_test_split_temporal(X, y, 0.8)
        assert X_train[-1, 0, 0] < X_test[0, 0, 0]


class TestDataPipeline:
    def test_end_to_end(self, mock_provider: MockDataProvider) -> None:
        config = TrainingConfig(
            ticker="TEST",
            from_date="2022-01-01",
            to_date="2024-01-01",
            sequence_length=10,
            epochs=1,
            features=["close"],
        )
        pipeline = DataPipeline(mock_provider)
        X_train, X_test, y_train, y_test, scaler = pipeline.prepare(config)

        assert X_train.ndim == 3
        assert X_train.shape[1] == 10
        assert X_train.shape[2] == 1
        assert len(y_train) == len(X_train)
        assert len(y_test) == len(X_test)

    def test_multifeature(self, mock_provider: MockDataProvider) -> None:
        config = TrainingConfig(
            ticker="TEST",
            from_date="2022-01-01",
            to_date="2024-01-01",
            sequence_length=10,
            epochs=1,
            features=["close", "volume"],
        )
        pipeline = DataPipeline(mock_provider)
        X_train, X_test, y_train, y_test, scaler = pipeline.prepare(config)
        assert X_train.shape[2] == 2

    def test_insufficient_data_raises(self) -> None:
        class TinyProvider:
            def fetch_ohlcv(self, **kwargs) -> list:
                return [
                    {"timestamp": i, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 100}
                    for i in range(5)
                ]

        config = TrainingConfig(
            ticker="TEST",
            from_date="2022-01-01",
            to_date="2024-01-01",
            sequence_length=60,
            epochs=1,
            features=["close"],
        )
        pipeline = DataPipeline(TinyProvider())
        with pytest.raises(ValueError, match="Insufficient data"):
            pipeline.prepare(config)
