from __future__ import annotations

import numpy as np

from app.ml.training.baseline import PersistenceBaseline


class TestPersistenceBaseline:
    def test_prediction_shape(self) -> None:
        y = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        pred = PersistenceBaseline.predict(y)
        assert pred.shape == y.shape

    def test_prediction_values(self) -> None:
        y = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        pred = PersistenceBaseline.predict(y)
        # First prediction = first actual (no change assumed)
        assert pred[0] == 1.0
        # Subsequent: pred[i] = y[i-1]
        assert pred[1] == 1.0
        assert pred[2] == 2.0
        assert pred[3] == 3.0
        assert pred[4] == 4.0

    def test_constant_array(self) -> None:
        y = np.array([5.0, 5.0, 5.0, 5.0])
        pred = PersistenceBaseline.predict(y)
        np.testing.assert_array_equal(pred, y)
