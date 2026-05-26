"""PR 1 acceptance bar: byte-identical Monte Carlo persistence.

Per ``docs/architecture/research-artifact-seam.md`` § "Per-PR
acceptance bar", every strangler PR must demonstrate that the
migrated phase writes byte-identical artifact files to what the
pre-seam ``storage.py`` would have written. This test encodes that
contract for Monte Carlo: it constructs a deterministic config +
result (no RNG, fixed values), saves via the new
``save_monte_carlo`` thin delegator, then asserts the on-disk bytes
match the hand-canonicalised JSON produced via
``json.dumps(model.model_dump(mode='json'), ensure_ascii=False)`` —
the same serialisation the pre-seam ``_atomic_write_json`` used.

Later PRs replace this test with their own per-phase analogue
(``test_walk_forward_byte_equivalence.py``, etc.).
"""

from __future__ import annotations

import json
from pathlib import Path

from app.research.monte_carlo import (
    EquityBandPoint,
    MonteCarloConfig,
    MonteCarloResult,
    save_monte_carlo,
)
from app.research.monte_carlo.result import BreachProbability


def _deterministic_config() -> MonteCarloConfig:
    """A fully populated, fixed-value config — no RNG, no defaults."""
    return MonteCarloConfig(
        monte_carlo_id="a" * 32,
        parent_run_id="b" * 32,
        parent_trade_log_hash="t" * 64,
        method="reshuffle",
        simulation_count=1_000,
        projection_trade_count=0,
        initial_equity=100_000.0,
        random_seed=42,
        breach_thresholds=[0.1, 0.2, 0.5],
        created_at_ms=1_736_000_000_000,
    )


def _deterministic_result() -> MonteCarloResult:
    """A fully populated, fixed-value result — same MC id as the config."""
    return MonteCarloResult(
        monte_carlo_id="a" * 32,
        parent_run_id="b" * 32,
        method="reshuffle",
        simulation_count=1_000,
        realised_trade_count=25,
        equity_bands=[
            EquityBandPoint(trade_index=0, p5=100_000.0, p50=100_000.0, p95=100_000.0),
            EquityBandPoint(trade_index=1, p5=99_500.0, p50=100_200.0, p95=101_000.0),
            EquityBandPoint(trade_index=2, p5=99_000.0, p50=100_400.0, p95=102_000.0),
        ],
        drawdown_quantiles={"p5": 0.01, "p50": 0.05, "p95": 0.12},
        terminal_pnl_quantiles={"p5": -100.0, "p50": 500.0, "p95": 1_500.0},
        max_losing_streak_quantiles={"p5": 1, "p50": 2, "p95": 4},
        breach_probabilities=[
            BreachProbability(threshold=0.1, probability=0.25),
            BreachProbability(threshold=0.2, probability=0.05),
            BreachProbability(threshold=0.5, probability=0.0),
        ],
        warnings=[],
        created_at_ms=1_736_000_000_000,
        completed_at_ms=1_736_000_005_000,
        status="completed",
        failure_reason=None,
    )


def test_monte_carlo_save_writes_byte_identical_canonical_json(tmp_path: Path):
    """The bytes on disk match ``json.dumps(model_dump(mode='json'), ensure_ascii=False)``.

    This is the canonical-bytes formula the pre-seam
    ``app.research.monte_carlo.storage._atomic_write_json`` used. The
    new ``ArtifactStore.save`` must preserve it byte-for-byte so the
    PR is genuinely behaviour-preserving (no on-disk schema change,
    no migration cost).
    """
    config = _deterministic_config()
    result = _deterministic_result()

    save_monte_carlo(config, result, root=tmp_path)

    mc_dir = tmp_path / "monte-carlo" / config.monte_carlo_id
    config_bytes = (mc_dir / "config.json").read_bytes()
    result_bytes = (mc_dir / "result.json").read_bytes()

    expected_config_bytes = json.dumps(
        config.model_dump(mode="json"), ensure_ascii=False
    ).encode("utf-8")
    expected_result_bytes = json.dumps(
        result.model_dump(mode="json"), ensure_ascii=False
    ).encode("utf-8")

    assert config_bytes == expected_config_bytes
    assert result_bytes == expected_result_bytes
