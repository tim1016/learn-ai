from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.research.ml.artifact import ChunkRef, DeterministicRuleGenerator, PredictionSetManifest
from app.research.ml.coverage import assert_bar_clock_coverage
from app.research.ml.loader import PredictionCoverageError, PredictionSet

NY = ZoneInfo("America/New_York")


@dataclass
class _FakeBar:
    end_time: datetime


def _bars(n: int, start: datetime = datetime(2024, 5, 1, 9, 30, tzinfo=NY)) -> list[_FakeBar]:
    return [_FakeBar(end_time=start + timedelta(minutes=15 * i)) for i in range(1, n + 1)]


def _to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _pset(timestamps_ms: list[int]) -> PredictionSet:
    manifest = PredictionSetManifest(
        schema_version="1.0",
        prediction_set_id="t",
        symbol="SPY",
        resolution_minutes=15,
        field_names=["prediction"],
        warmup_policy="neutral_zero_until_feature_ready",
        generator=DeterministicRuleGenerator(kind="deterministic_rule", rule_id="x", rule_version="1.0"),
        chunks=[ChunkRef(
            trained_through_ms=timestamps_ms[0] - 1 if timestamps_ms else 0,
            start_ms=timestamps_ms[0] if timestamps_ms else 0,
            end_ms=timestamps_ms[-1] if timestamps_ms else 0,
            row_count=len(timestamps_ms),
            rows_hash="0" * 64,
        )],
        prediction_set_hash="0" * 64,
    )
    index = {ts: {"prediction": 0.0} for ts in timestamps_ms}
    return PredictionSet(manifest=manifest, index=index)


def test_coverage_passes_when_predictions_match_bars_exactly() -> None:
    bars = _bars(3)
    pset = _pset([_to_ms(b.end_time) for b in bars])
    assert_bar_clock_coverage(pset, bars)


def test_coverage_passes_when_predictions_are_a_superset_of_bars() -> None:
    bars = _bars(3)
    extra = bars[0].end_time + timedelta(hours=12)
    timestamps = [_to_ms(b.end_time) for b in bars] + [_to_ms(extra)]
    pset = _pset(sorted(timestamps))
    assert_bar_clock_coverage(pset, bars)


def test_coverage_fails_when_a_bar_has_no_prediction() -> None:
    bars = _bars(3)
    timestamps = [_to_ms(b.end_time) for b in bars[:-1]]
    pset = _pset(timestamps)
    with pytest.raises(PredictionCoverageError, match="missing predictions for 1"):
        assert_bar_clock_coverage(pset, bars)


def test_coverage_error_lists_missing_timestamps() -> None:
    bars = _bars(5)
    timestamps = [_to_ms(b.end_time) for b in bars[:2]]
    pset = _pset(timestamps)
    with pytest.raises(PredictionCoverageError) as exc:
        assert_bar_clock_coverage(pset, bars)
    assert "missing predictions for 3" in str(exc.value)
