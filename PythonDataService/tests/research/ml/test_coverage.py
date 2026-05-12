from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.engine.strategy.spec.schema import PredictionRef
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
        chunks=[
            ChunkRef(
                trained_through_ms=timestamps_ms[0] - 1 if timestamps_ms else 0,
                start_ms=timestamps_ms[0] if timestamps_ms else 0,
                end_ms=timestamps_ms[-1] if timestamps_ms else 0,
                row_count=len(timestamps_ms),
                rows_hash="0" * 64,
            )
        ],
        prediction_set_hash="0" * 64,
    )
    index = {ts: {"prediction": 0.0} for ts in timestamps_ms}
    return PredictionSet(manifest=manifest, index=index)


def _ref(*, lookup: str = "exact_bar_close", field: str = "prediction") -> PredictionRef:
    return PredictionRef.model_validate({"id": "p", "prediction_set_id": "t", "field": field, "lookup": lookup})


def _pset_with_fields(rows: list[tuple[int, dict[str, float]]]) -> PredictionSet:
    """Like _pset but allows per-row field control (for missing-field tests)."""
    manifest = PredictionSetManifest(
        schema_version="1.0",
        prediction_set_id="t",
        symbol="SPY",
        resolution_minutes=15,
        field_names=["prediction", "confidence"],
        warmup_policy="neutral_zero_until_feature_ready",
        generator=DeterministicRuleGenerator(kind="deterministic_rule", rule_id="x", rule_version="1.0"),
        chunks=[
            ChunkRef(
                trained_through_ms=rows[0][0] - 1 if rows else 0,
                start_ms=rows[0][0] if rows else 0,
                end_ms=rows[-1][0] if rows else 0,
                row_count=len(rows),
                rows_hash="0" * 64,
            )
        ],
        prediction_set_hash="0" * 64,
    )
    index = {ts: dict(row) for ts, row in rows}
    return PredictionSet(manifest=manifest, index=index)


def test_coverage_passes_when_predictions_match_bars_exactly() -> None:
    bars = _bars(3)
    pset = _pset([_to_ms(b.end_time) for b in bars])
    assert_bar_clock_coverage(pset, bars, refs=[_ref()])


def test_coverage_passes_when_predictions_are_a_superset_of_bars() -> None:
    bars = _bars(3)
    extra = bars[0].end_time + timedelta(hours=12)
    timestamps = [_to_ms(b.end_time) for b in bars] + [_to_ms(extra)]
    pset = _pset(sorted(timestamps))
    assert_bar_clock_coverage(pset, bars, refs=[_ref()])


def test_coverage_fails_when_a_bar_has_no_prediction() -> None:
    bars = _bars(3)
    timestamps = [_to_ms(b.end_time) for b in bars[:-1]]
    pset = _pset(timestamps)
    with pytest.raises(PredictionCoverageError, match=r"exact_bar_close.*no prediction row"):
        assert_bar_clock_coverage(pset, bars, refs=[_ref()])


def test_coverage_error_lists_missing_timestamps() -> None:
    bars = _bars(5)
    timestamps = [_to_ms(b.end_time) for b in bars[:2]]
    pset = _pset(timestamps)
    first_missing_ts = _to_ms(bars[2].end_time)
    with pytest.raises(PredictionCoverageError) as exc:
        assert_bar_clock_coverage(pset, bars, refs=[_ref()])
    assert str(first_missing_ts) in str(exc.value)


def test_coverage_exact_bar_close_missing_row_raises_descriptive_error() -> None:
    bars = _bars(3)
    timestamps = [_to_ms(b.end_time) for b in bars[:-1]]
    pset = _pset(timestamps)
    refs = [_ref(lookup="exact_bar_close", field="prediction")]
    with pytest.raises(PredictionCoverageError, match=r"exact_bar_close.*no prediction row at fired bar"):
        assert_bar_clock_coverage(pset, bars, refs=refs)


def test_coverage_exact_bar_close_missing_field_raises() -> None:
    bars = _bars(2)
    rows = [(_to_ms(b.end_time), {"prediction": 0.0}) for b in bars]
    pset = _pset_with_fields(rows)
    refs = [_ref(lookup="exact_bar_close", field="confidence")]
    with pytest.raises(PredictionCoverageError, match=r"missing field 'confidence'.*available"):
        assert_bar_clock_coverage(pset, bars, refs=refs)


def test_coverage_next_after_no_later_row_raises_with_fired_ts_in_message() -> None:
    """For next_after_bar_close, every fired bar must have a strictly-greater
    row. With predictions covering only the fired bars themselves, the LAST
    fired bar has no successor and coverage must fail."""
    bars = _bars(3)
    timestamps = [_to_ms(b.end_time) for b in bars]
    pset = _pset(timestamps)
    refs = [_ref(lookup="next_after_bar_close", field="prediction")]
    last_fired_ts = _to_ms(bars[-1].end_time)
    with pytest.raises(PredictionCoverageError, match=rf"next_after_bar_close.*{last_fired_ts}"):
        assert_bar_clock_coverage(pset, bars, refs=refs)


def test_coverage_next_after_later_row_missing_field_reports_matched_ts() -> None:
    """If the next-row exists but lacks the required field, the error must
    name both the fired ts AND the matched next-row ts (so the user can
    locate the corrupt row in the prediction set)."""
    bars = _bars(2)
    rows = [
        (_to_ms(bars[0].end_time), {"prediction": 1.0, "confidence": 0.5}),
        (_to_ms(bars[1].end_time), {"prediction": 2.0, "confidence": 0.6}),
        (_to_ms(bars[1].end_time) + 1, {"prediction": 3.0}),
    ]
    pset = _pset_with_fields(rows)
    refs = [_ref(lookup="next_after_bar_close", field="confidence")]
    matched_ts = _to_ms(bars[1].end_time) + 1
    with pytest.raises(
        PredictionCoverageError,
        match=rf"matched next row at ts_ms={matched_ts}.*missing field 'confidence'",
    ):
        assert_bar_clock_coverage(pset, bars, refs=refs)


def test_coverage_mixed_lookup_modes_validates_both() -> None:
    """Spec with two refs (one exact, one next_after) is valid only when both
    constraints hold on every fired bar simultaneously."""
    bars = _bars(2)
    rows = [
        (_to_ms(bars[0].end_time), {"prediction": 1.0}),
        (_to_ms(bars[1].end_time), {"prediction": 2.0}),
    ]
    pset = _pset_with_fields(rows)
    refs = [
        _ref(lookup="exact_bar_close", field="prediction"),
        _ref(lookup="next_after_bar_close", field="prediction"),
    ]
    with pytest.raises(PredictionCoverageError, match=r"next_after_bar_close"):
        assert_bar_clock_coverage(pset, bars, refs=refs)


def test_coverage_passes_under_next_after_when_set_extends_one_row_past_bars() -> None:
    bars = _bars(3)
    timestamps = [_to_ms(b.end_time) for b in bars]
    timestamps.append(timestamps[-1] + 1)
    pset = _pset(sorted(timestamps))
    refs = [_ref(lookup="next_after_bar_close", field="prediction")]
    assert_bar_clock_coverage(pset, bars, refs=refs)
