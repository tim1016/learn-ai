from __future__ import annotations

import logging

import tensorflow as tf
from tensorflow import keras

from app.ml.models.schemas import TrainingConfig

logger = logging.getLogger(__name__)


def build_lstm_model(
    config: TrainingConfig, input_shape: tuple[int, int]
) -> keras.Model:
    """Build LSTM model based on TrainingConfig.

    Args:
        config: Training configuration with architecture params.
        input_shape: (sequence_length, n_features)

    Returns:
        Compiled Keras model.
    """
    model = keras.Sequential(name=f"lstm_{config.ticker}")

    for i in range(config.lstm_layers):
        return_sequences = i < config.lstm_layers - 1
        if i == 0:
            model.add(
                keras.layers.LSTM(
                    units=config.lstm_units,
                    return_sequences=return_sequences,
                    input_shape=input_shape,
                    name=f"lstm_{i}",
                )
            )
        else:
            model.add(
                keras.layers.LSTM(
                    units=config.lstm_units,
                    return_sequences=return_sequences,
                    name=f"lstm_{i}",
                )
            )
        model.add(keras.layers.Dropout(config.dropout, name=f"dropout_{i}"))

    model.add(keras.layers.Dense(1, name="output"))

    model.compile(
        optimizer=keras.optimizers.Adam(learning_rate=config.learning_rate),
        loss="mse",
        metrics=["mae"],
    )

    logger.info(f"[ML] Built LSTM model: {model.count_params()} parameters")
    model.summary(print_fn=lambda x: logger.info(f"[ML] {x}"))

    return model
