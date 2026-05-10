"""Bar-clock coverage check.

The engine only evaluates strategy logic when ``TradeBarConsolidator``
emits a bar — never on a wall-clock grid. So predictions are required
for every emitted bar, not for every minute between start and end.

This helper sits at the run-pipeline boundary (where the data source
and consolidator are known) and asserts the loaded prediction set
covers every bar the engine will see. Predictions for bars the engine
won't see (e.g. predictions written for a 24x7 calendar grid against a
market-hours stream) are allowed — they're a superset, not a mismatch.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Protocol

from app.research.ml.loader import PredictionCoverageError, PredictionSet

logger = logging.getLogger(__name__)


class _BarLike(Protocol):
    """Anything with an ``end_time: datetime`` (TradeBar duck-types fine)."""

    @property
    def end_time(self): ...


def assert_bar_clock_coverage(
    prediction_set: PredictionSet,
    bar_stream: Iterable[_BarLike],
) -> None:
    """Raise ``PredictionCoverageError`` if any emitted bar lacks a prediction.

    ``bar_stream`` must be the bars the run will actually evaluate —
    typically obtained by running the run's data source through the
    same ``TradeBarConsolidator`` configuration the engine will use.
    Iterating consumes the stream once.
    """
    expected_ms: set[int] = {int(bar.end_time.timestamp() * 1000) for bar in bar_stream}
    have_ms: set[int] = set(prediction_set.index.keys())

    missing = expected_ms - have_ms
    extra = have_ms - expected_ms

    if extra:
        logger.info(
            "[ML] prediction_set %s has %d predictions for bars the engine will not evaluate; "
            "this is allowed but suggests the artifact and run window may be misaligned",
            prediction_set.manifest.prediction_set_id,
            len(extra),
        )

    if missing:
        sample = sorted(missing)[:5]
        raise PredictionCoverageError(
            f"prediction_set {prediction_set.manifest.prediction_set_id!r} missing predictions "
            f"for {len(missing)} emitted bars; first {len(sample)}: {sample}"
        )
