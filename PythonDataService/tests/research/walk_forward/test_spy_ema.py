"""Versioned SPY EMA normalized-gap protocol tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from app.engine.strategy.spec import load_spec_from_path
from app.engine.strategy.spec.schema import ConstOperand, DifferenceBps
from app.engine.strategy.spec.tests._parity_helpers import (
    FakeDataReader,
    build_minute_bars,
    closes_for_spy_ema,
)
from app.research.runs.storage import list_runs
from app.research.walk_forward.splits import (
    ChronologicalSplitPolicy,
    RollingSplitPolicy,
    date_str_to_ms,
)
from app.research.walk_forward.spy_ema import (
    SPY_EMA_PROTOCOL_ID,
    SPY_EMA_PROTOCOL_VERSION,
    SpyEmaPipelineRequest,
    frozen_spy_ema_v1_request,
    materialize_gap_threshold,
    run_spy_ema_pipeline,
)

_FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "app"
    / "engine"
    / "strategy"
    / "spec"
    / "fixtures"
    / "spy_ema_normalized_gap.spec.json"
)


def test_materialize_gap_threshold_changes_only_the_typed_threshold() -> None:
    base = load_spec_from_path(_FIXTURE)

    candidate = materialize_gap_threshold(base, 7.5)

    assert candidate is not base
    comparison = candidate.entry.conditions[1]
    assert isinstance(comparison.left, DifferenceBps)
    assert isinstance(comparison.right, ConstOperand)
    assert comparison.right.value == 7.5
    assert base.entry.conditions[1].right.value == 4.0


def test_pipeline_request_requires_an_ordered_unique_threshold_grid() -> None:
    split = RollingSplitPolicy(train_days=180, test_days=30, step_days=30)

    with pytest.raises(ValueError, match="ascending order"):
        SpyEmaPipelineRequest(
            start_ms=date_str_to_ms("2024-08-01"),
            end_ms=date_str_to_ms("2026-08-01"),
            split_policy=split,
            thresholds_bps=(2.0, 1.0),
        )

    with pytest.raises(ValueError, match="unique"):
        SpyEmaPipelineRequest(
            start_ms=date_str_to_ms("2024-08-01"),
            end_ms=date_str_to_ms("2026-08-01"),
            split_policy=split,
            thresholds_bps=(1.0, 1.0),
        )

    with pytest.raises(ValueError, match="thresholds must be in"):
        SpyEmaPipelineRequest(
            start_ms=date_str_to_ms("2024-08-01"),
            end_ms=date_str_to_ms("2026-08-01"),
            split_policy=split,
            thresholds_bps=(1.0, float("nan")),
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("initial_cash", float("inf"), "initial_cash must be finite"),
        ("commission_per_order", float("nan"), "commission_per_order must be finite"),
        ("slippage_per_share", float("inf"), "slippage_per_share must be finite"),
    ],
)
def test_pipeline_request_rejects_non_finite_money_inputs(
    field: str,
    value: float,
    message: str,
) -> None:
    split = RollingSplitPolicy(train_days=180, test_days=30, step_days=30)

    with pytest.raises(ValueError, match=message):
        SpyEmaPipelineRequest(
            start_ms=date_str_to_ms("2024-08-01"),
            end_ms=date_str_to_ms("2026-08-01"),
            split_policy=split,
            **{field: value},
        )


def test_materialize_gap_threshold_rejects_non_finite_value() -> None:
    base = load_spec_from_path(_FIXTURE)

    with pytest.raises(ValueError, match="finite"):
        materialize_gap_threshold(base, float("nan"))


def test_default_protocol_produces_eighteen_oos_folds() -> None:
    request = frozen_spy_ema_v1_request()
    split = request.split_policy

    assert (
        len(
            split.folds(
                request.start_ms,
                request.end_ms,
            )
        )
        == 18
    )
    assert request.protocol_id == SPY_EMA_PROTOCOL_ID
    assert request.protocol_version == SPY_EMA_PROTOCOL_VERSION


def test_pipeline_persists_control_training_and_oos_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bars = [replace(bar, symbol="SPY") for bar in build_minute_bars(closes_for_spy_ema(5000))]

    def factory(_symbol: str, _start: date, _end: date) -> FakeDataReader:
        return FakeDataReader(bars=bars)

    revision_calls: list[tuple[str | None, date | None, date | None]] = []

    def resolve_revision(*, symbol=None, start_date=None, end_date=None) -> str:
        revision_calls.append((symbol, start_date, end_date))
        return "pinned-rev"

    monkeypatch.setattr(
        "app.research.walk_forward.spy_ema.resolve_data_root_revision",
        resolve_revision,
    )
    progress: list[tuple[int, int, str]] = []

    pipeline = run_spy_ema_pipeline(
        SpyEmaPipelineRequest(
            start_ms=date_str_to_ms("2024-01-02"),
            end_ms=date_str_to_ms("2024-02-22"),
            split_policy=ChronologicalSplitPolicy(train_pct=0.7),
            thresholds_bps=(1.0, 10.0),
            min_train_trades=1,
        ),
        data_source_factory=factory,
        artifacts_root=tmp_path,
        on_progress=lambda current, total, message: progress.append((current, total, message)),
    )

    assert pipeline.control_ledger.status == "completed"
    assert pipeline.walk_forward_result.status == "completed"
    assert pipeline.walk_forward_config.parent_run_id == pipeline.control_ledger.run_id
    assert revision_calls == [("SPY", date(2024, 1, 2), date(2024, 2, 22))]
    assert "pinned-rev" in pipeline.control_ledger.data_snapshot_id
    assert (tmp_path / pipeline.control_ledger.run_id / "ledger.json").is_file()
    assert (tmp_path / "walk-forward" / pipeline.walk_forward_config.walk_forward_id / "result.json").is_file()

    fold = pipeline.walk_forward_result.folds[0]
    children = list_runs(
        root=tmp_path,
        parent_run_id=pipeline.walk_forward_config.walk_forward_id,
    )
    assert {child.run_id for child in children} == {
        fold.test_run_id,
        *(candidate.train_run_id for candidate in fold.training_candidates),
    }
    assert all("pinned-rev" in child.data_snapshot_id for child in children)
    assert [current for current, _, _ in progress] == [1, 2, 3, 4]
    assert {total for _, total, _ in progress} == {4}
    assert "canonical control" in progress[0][2]
    assert "training candidate 1 of 2" in progress[1][2]
    assert "training candidate 2 of 2" in progress[2][2]
    assert "out-of-sample fold 1 of 1" in progress[3][2]
