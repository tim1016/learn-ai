from __future__ import annotations

import numpy as np


class PersistenceBaseline:
    """Naive baseline: predict that tomorrow's value equals today's value.

    For scaled data, the prediction at time t is simply the value at t-1.
    This provides a minimum bar that the LSTM must beat to be useful.
    """

    @staticmethod
    def predict(y_test: np.ndarray) -> np.ndarray:
        """Generate persistence predictions.

        Shifts the test array by 1 step: prediction[i] = y_test[i-1].
        The first prediction uses y_test[0] (predicts no change).
        """
        predictions = np.empty_like(y_test)
        predictions[0] = y_test[0]
        predictions[1:] = y_test[:-1]
        return predictions
