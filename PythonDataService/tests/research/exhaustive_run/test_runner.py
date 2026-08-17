"""Orchestration tests for the linked Exhaustive Run evidence tracks."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.engine.strategy.spec import load_spec_from_path
from app.research.exhaustive_run.errors import ExhaustiveRunSourceError
from app.research.exhaustive_run.runner import _validate_source, run_exhaustive_analysis
from app.research.runs.hashing import hash_payload
from app.research.runs.result import BacktestRunResult, RunMetrics, RunTrade
from app.research.walk_forward.result import (
    FoldResult,
    ParameterCandidateConfig,
    ParameterSearchConfig,
    TrainingCandidateResult,
    WalkForwardConfig,
    WalkForwardResult,
)
from app.research.walk_forward.spy_ema import (
    DEFAULT_GAP_THRESHOLDS_BPS,
    SPY_EMA_PROTOCOL_END_MS,
    SPY_EMA_PROTOCOL_START_MS,
    frozen_spy_ema_v1_request,
    materialize_gap_threshold,
)


def _metrics(*, sharpe: float, total_return: float, trades: int) -> RunMetrics:
    return RunMetrics(
        total_trades=trades,
        winning_trades=trades,
        losing_trades=0,
        win_rate=1.0,
        total_return_pct=total_return,
        max_drawdown_pct=0.01,
        sharpe_ratio=sharpe,
    )


def _source() -> tuple[WalkForwardConfig, WalkForwardResult]:
    fixture = load_spec_from_path(
        Path(__file__).resolve().parents[3]
        / "app/engine/strategy/spec/fixtures/spy_ema_normalized_gap.spec.json"
    )
    specs = [materialize_gap_threshold(fixture, gap) for gap in DEFAULT_GAP_THRESHOLDS_BPS]
    candidates = [
        ParameterCandidateConfig(
            parameters={"gap_bps": gap},
            strategy_spec_hash=hash_payload(spec.model_dump(mode="json")),
            strategy_spec_json=spec.model_dump(mode="json"),
        )
        for gap, spec in zip(DEFAULT_GAP_THRESHOLDS_BPS, specs, strict=True)
    ]
    policy = frozen_spy_ema_v1_request().split_policy
    fixture_hash = hash_payload(fixture.model_dump(mode="json"))
    config = WalkForwardConfig(
        walk_forward_id="a" * 32,
        strategy_spec_hash=fixture_hash,
        strategy_spec_json=fixture.model_dump(mode="json"),
        symbol="SPY",
        resolution_minutes=15,
        start_ms=SPY_EMA_PROTOCOL_START_MS,
        end_ms=SPY_EMA_PROTOCOL_END_MS,
        initial_cash=100_000.0,
        fill_mode="next_bar_open",
        commission_per_order=0.0,
        slippage_per_share=0.0,
        random_seed=0,
        split_policy=policy.describe(),
        parameter_search=ParameterSearchConfig(
            candidates=candidates,
            min_trades=5,
        ),
        protocol_id="spy-ema-normalized-gap",
        protocol_version="1.0",
        created_at_ms=1,
    )
    folds = []
    for window in policy.folds(config.start_ms, config.end_ms):
        training = [
            TrainingCandidateResult(
                parameters={"gap_bps": gap},
                train_run_id=f"train-{window.fold_index}-{gap:g}",
                train_metrics=_metrics(
                    sharpe=2.0 if gap == 1.0 else 9.0,
                    total_return=0.02 if gap == 1.0 else 0.09,
                    trades=20 if gap == 1.0 else 1,
                ),
                eligible=gap == 1.0,
                ineligibility_reason=None if gap == 1.0 else "too few trades",
            )
            for gap in DEFAULT_GAP_THRESHOLDS_BPS
        ]
        folds.append(
            FoldResult(
                fold_index=window.fold_index,
                train_start_ms=window.train_start_ms,
                train_end_ms=window.train_end_ms,
                test_start_ms=window.test_start_ms,
                test_end_ms=window.test_end_ms,
                test_run_id=f"source-test-{window.fold_index}",
                test_metrics=_metrics(sharpe=1.0, total_return=0.01, trades=2),
                test_trade_count=2,
                selected_parameters={"gap_bps": 1.0},
                training_candidates=training,
                status="completed",
            )
        )
    result = WalkForwardResult(
        walk_forward_id=config.walk_forward_id,
        strategy_spec_hash=fixture_hash,
        split_policy=config.split_policy,
        folds=folds,
        status="completed",
        created_at_ms=1,
        completed_at_ms=2,
    )
    return config, result


def _trade(exit_ms: int) -> RunTrade:
    return RunTrade(
        trade_number=exit_ms,
        entry_time_ms=exit_ms - 1,
        entry_price=100.0,
        exit_time_ms=exit_ms,
        exit_price=101.0,
        pnl_pts=1.0,
        pnl_pct=0.01,
        result="WIN",
        bars_held=1,
    )


def test_runner_reuses_training_receipts_and_builds_both_evidence_tracks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.research.exhaustive_run.runner as runner

    source_config, source_result = _source()
    captured: dict[str, Any] = {"progress": []}
    full_metrics = _metrics(sharpe=1.5, total_return=0.25, trades=40)
    full_result = BacktestRunResult(
        run_id="full-run",
        initial_cash=100_000.0,
        final_equity=125_000.0,
        trades=[_trade(SPY_EMA_PROTOCOL_START_MS + 1), _trade(SPY_EMA_PROTOCOL_END_MS - 1)],
        metrics=full_metrics,
        bars_consumed=100,
    )

    def fake_run_strategy(request, **_kwargs):
        captured["full_request"] = request
        return SimpleNamespace(
            run_id="full-run",
            status="completed",
            failure_reason=None,
        ), full_result

    def fake_run_walk_forward(request, **kwargs):
        captured["stability_request"] = request
        for current in range(1, 19):
            kwargs["on_progress"](current, 18, f"fold {current}")
        fixed_folds = [
            fold.model_copy(
                update={
                    "test_run_id": f"fixed-test-{fold.fold_index}",
                    "training_candidates": [],
                    "selected_parameters": {},
                }
            )
            for fold in source_result.folds
        ]
        return SimpleNamespace(walk_forward_id="fixed-wf"), SimpleNamespace(
            walk_forward_id="fixed-wf",
            status="completed",
            failure_reason=None,
            folds=fixed_folds,
            pct_profitable_folds=1.0,
            mean_oos_sharpe=1.0,
            median_oos_sharpe=1.0,
            alpha_decay=0.0,
        )

    monkeypatch.setattr(runner, "load_walk_forward", lambda *_args, **_kwargs: (source_config, source_result))
    monkeypatch.setattr(runner, "_validated_source_revision", lambda *_args, **_kwargs: "revision")
    monkeypatch.setattr(runner, "run_strategy_spec", fake_run_strategy)
    monkeypatch.setattr(runner, "save_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "run_walk_forward", fake_run_walk_forward)
    monkeypatch.setattr(runner, "save_walk_forward", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runner,
        "save_exhaustive_run",
        lambda config, result, **_kwargs: captured.update(config=config, result=result),
    )

    config, result = run_exhaustive_analysis(
        source_config.walk_forward_id,
        data_source_factory=lambda *_args: None,
        on_progress=lambda current, total, message: captured["progress"].append(
            (current, total, message)
        ),
    )

    assert config.data_root_revision == "revision"
    assert result.status == "completed"
    assert len(result.selections) == 18
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.appearance_count == 18
    assert candidate.full_period_metrics == full_metrics
    assert candidate.trade_recency is not None
    assert candidate.trade_recency.recent_trade_count == 1
    assert candidate.forward_stability is not None
    assert len(candidate.forward_stability.folds) == 18
    assert candidate.forward_stability.mean_fold_retention == pytest.approx(0.5, abs=1e-12, rel=0)
    assert captured["full_request"].start_ms == SPY_EMA_PROTOCOL_START_MS
    assert captured["full_request"].end_ms == SPY_EMA_PROTOCOL_END_MS
    assert captured["progress"][-1][0:2] == (19, 19)


def test_source_requires_every_canonical_fold_to_be_completed() -> None:
    config, result = _source()
    failed_fold = result.folds[0].model_copy(
        update={"status": "failed", "failure_reason": "missing TEST bars"}
    )
    incomplete = result.model_copy(update={"folds": [failed_fold, *result.folds[1:]]})

    with pytest.raises(ExhaustiveRunSourceError, match="18 completed"):
        _validate_source(config, incomplete)


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"symbol": "QQQ"}, "configuration"),
        ({"resolution_minutes": 30}, "configuration"),
        ({"initial_cash": 50_000.0}, "configuration"),
        ({"fill_mode": "signal_bar_close"}, "configuration"),
        ({"commission_per_order": 1.0}, "configuration"),
        ({"slippage_per_share": 0.01}, "configuration"),
        ({"random_seed": 1}, "configuration"),
    ],
)
def test_source_rejects_noncanonical_engine_configuration(
    update: dict[str, object],
    message: str,
) -> None:
    config, result = _source()

    with pytest.raises(ExhaustiveRunSourceError, match=message):
        _validate_source(config.model_copy(update=update), result)


def test_source_rejects_noncanonical_split_policy() -> None:
    config, result = _source()
    changed = config.model_copy(
        update={"split_policy": config.split_policy.model_copy(update={"train_days": 90})}
    )

    with pytest.raises(ExhaustiveRunSourceError, match="configuration"):
        _validate_source(changed, result)


def test_source_rejects_noncanonical_candidate_specification() -> None:
    config, result = _source()
    assert config.parameter_search is not None
    changed_candidate = config.parameter_search.candidates[0].model_copy(
        update={"parameters": {"gap_bps": 1.5}}
    )
    changed_search = config.parameter_search.model_copy(
        update={"candidates": [changed_candidate, *config.parameter_search.candidates[1:]]}
    )

    with pytest.raises(ExhaustiveRunSourceError, match="candidate grid"):
        _validate_source(config.model_copy(update={"parameter_search": changed_search}), result)


def test_source_rejects_noncanonical_fold_boundaries() -> None:
    config, result = _source()
    changed_fold = result.folds[0].model_copy(
        update={"test_start_ms": result.folds[0].test_start_ms + 1}
    )

    with pytest.raises(ExhaustiveRunSourceError, match="18 completed canonical"):
        _validate_source(result=result.model_copy(update={"folds": [changed_fold, *result.folds[1:]]}), config=config)
