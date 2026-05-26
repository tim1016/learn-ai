"""PR 3 acceptance bar: byte-identical walk-forward persistence.

Per ``docs/architecture/research-artifact-seam.md`` § "Per-PR
acceptance bar", every strangler PR must demonstrate that the
migrated phase writes byte-identical artifact files to what the
pre-seam ``storage.py`` would have written. This test encodes that
contract for walk-forward: it constructs a deterministic config +
result (no RNG, fixed values), saves via the new
``save_walk_forward`` thin delegator, then asserts the on-disk
bytes match the hand-canonicalised JSON produced via
``json.dumps(model.model_dump(mode='json'), ensure_ascii=False)`` —
the same serialisation the pre-seam ``_atomic_write_json`` used.

The fixture intentionally uses BOTH ``walk_forward_id`` and
``parent_run_id`` as 32-lowercase-hex strings (``"a"*32`` and
``"b"*32`` respectively). This exercises the two-hex-id path that
caught the PR-1 ``_extract_id`` auto-scan bug (commit ``1146a95``):
an earlier implementation walked every Pydantic field whose value
matched ``id_pattern`` and raised "multiple distinct id-shaped
fields" when more than one matched. The explicit
``descriptor.id_field="walk_forward_id"`` lookup in the post-fix
store must select the right field even when a sibling field shares
the regex shape.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.research.runs.result import EquityCurvePoint, RunMetrics
from app.research.walk_forward import (
    FoldResult,
    SplitPolicySpec,
    WalkForwardConfig,
    WalkForwardResult,
    save_walk_forward,
)


def _deterministic_config() -> WalkForwardConfig:
    """A fully populated, fixed-value config — no RNG, no defaults."""
    return WalkForwardConfig(
        walk_forward_id="a" * 32,
        parent_run_id="b" * 32,
        strategy_spec_hash="d" * 64,
        strategy_spec_json={"name": "test_spec", "version": 1},
        symbol="SPY",
        resolution_minutes=15,
        start_ms=1_704_160_800_000,
        end_ms=1_735_714_800_000,
        initial_cash=100_000.0,
        fill_mode="signal_bar_close",
        commission_per_order=0.0,
        slippage_per_share=0.0,
        random_seed=42,
        split_policy=SplitPolicySpec(kind="chronological"),
        created_at_ms=1_736_000_000_000,
    )


def _deterministic_result() -> WalkForwardResult:
    """A fully populated, fixed-value result — same WF id as the config."""
    metrics = RunMetrics(
        total_trades=10,
        winning_trades=6,
        losing_trades=4,
        win_rate=0.6,
        total_return_pct=5.0,
        max_drawdown_pct=3.0,
        sharpe_ratio=1.2,
        sortino_ratio=1.5,
        profit_factor=2.0,
        expectancy_pct=0.5,
        payoff_ratio=1.5,
        exposure_pct=80.0,
        avg_trade_bars=4.0,
    )
    return WalkForwardResult(
        walk_forward_id="a" * 32,
        parent_run_id="b" * 32,
        strategy_spec_hash="d" * 64,
        split_policy=SplitPolicySpec(kind="chronological"),
        folds=[
            FoldResult(
                fold_index=0,
                train_start_ms=1_704_160_800_000,
                train_end_ms=1_710_000_000_000,
                test_start_ms=1_710_000_000_000,
                test_end_ms=1_715_000_000_000,
                test_run_id="c" * 32,
                test_metrics=metrics,
                test_trade_count=10,
                status="completed",
                failure_reason=None,
                selected_parameters={},
            ),
        ],
        combined_oos_equity_curve=[
            EquityCurvePoint(timestamp_ms=1_710_000_000_000, equity=100_000.0),
            EquityCurvePoint(timestamp_ms=1_715_000_000_000, equity=105_000.0),
        ],
        mean_oos_sharpe=1.2,
        median_oos_sharpe=1.2,
        pct_profitable_folds=1.0,
        oos_retention=1.0,
        alpha_decay=0.0,
        warnings=[],
        created_at_ms=1_736_000_000_000,
        completed_at_ms=1_736_000_005_000,
        status="completed",
        failure_reason=None,
    )


def test_walk_forward_save_writes_byte_identical_canonical_json(tmp_path: Path):
    """The bytes on disk match ``json.dumps(model_dump(mode='json'), ensure_ascii=False)``.

    This is the canonical-bytes formula the pre-seam
    ``app.research.walk_forward.storage._atomic_write_json`` used. The
    new ``ArtifactStore.save`` must preserve it byte-for-byte so the
    PR is genuinely behaviour-preserving (no on-disk schema change,
    no migration cost).
    """
    config = _deterministic_config()
    result = _deterministic_result()

    save_walk_forward(config, result, root=tmp_path)

    wf_dir = tmp_path / "walk-forward" / config.walk_forward_id
    config_bytes = (wf_dir / "config.json").read_bytes()
    result_bytes = (wf_dir / "result.json").read_bytes()

    expected_config_bytes = json.dumps(
        config.model_dump(mode="json"), ensure_ascii=False
    ).encode("utf-8")
    expected_result_bytes = json.dumps(
        result.model_dump(mode="json"), ensure_ascii=False
    ).encode("utf-8")

    assert config_bytes == expected_config_bytes
    assert result_bytes == expected_result_bytes
