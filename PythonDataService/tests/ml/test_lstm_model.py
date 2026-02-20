from __future__ import annotations

import numpy as np

from app.ml.models.schemas import TrainingConfig
from app.ml.training.lstm_model import build_lstm_model


def _make_config(**overrides) -> TrainingConfig:
    defaults = {
        "ticker": "TEST",
        "from_date": "2022-01-01",
        "to_date": "2024-01-01",
    }
    defaults.update(overrides)
    return TrainingConfig(**defaults)


class TestBuildLstmModel:
    def test_default_config_output_shape(self) -> None:
        config = _make_config()
        model = build_lstm_model(config, input_shape=(60, 1))
        assert model.output_shape == (None, 1)

    def test_default_config_layer_count(self) -> None:
        config = _make_config()
        model = build_lstm_model(config, input_shape=(60, 1))
        # 2 LSTM + 2 Dropout + 1 Dense = 5 layers
        assert len(model.layers) == 5

    def test_custom_layers(self) -> None:
        config = _make_config(lstm_layers=3)
        model = build_lstm_model(config, input_shape=(60, 1))
        # 3 LSTM + 3 Dropout + 1 Dense = 7 layers
        assert len(model.layers) == 7

    def test_model_is_compiled(self) -> None:
        config = _make_config()
        model = build_lstm_model(config, input_shape=(60, 1))
        assert model.optimizer is not None

    def test_model_accepts_input(self) -> None:
        config = _make_config()
        model = build_lstm_model(config, input_shape=(60, 1))
        dummy_input = np.zeros((1, 60, 1), dtype=np.float32)
        output = model.predict(dummy_input, verbose=0)
        assert output.shape == (1, 1)

    def test_multifeature_input(self) -> None:
        config = _make_config(features=["close", "volume"])
        model = build_lstm_model(config, input_shape=(60, 2))
        dummy_input = np.zeros((1, 60, 2), dtype=np.float32)
        output = model.predict(dummy_input, verbose=0)
        assert output.shape == (1, 1)
