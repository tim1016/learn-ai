"""PR 2 acceptance bar: byte-identical baselines persistence.

Per ``docs/architecture/research-artifact-seam.md`` § "Per-PR
acceptance bar", every strangler PR must demonstrate that the
migrated phase writes byte-identical artifact files to what the
pre-seam ``storage.py`` would have written. This test encodes that
contract for baselines: it constructs a deterministic config +
result (no RNG, fixed values), saves via the new ``save_baseline``
thin delegator, then asserts the on-disk bytes match the
hand-canonicalised JSON produced via
``json.dumps(model.model_dump(mode='json'), ensure_ascii=False)`` —
the same serialisation the pre-seam ``_atomic_write_json`` used.

Both ``baseline_id`` and ``parent_run_id`` use lowercase-hex 32-char
fixtures (distinct values) so the test exercises the two-hex-id
path that surfaced the auto-scan bug PR 1 fixed (commit ``1146a95``).
"""

from __future__ import annotations

import json
from pathlib import Path

from app.research.baselines import (
    BaselineConfig,
    BaselineResult,
    save_baseline,
)


def _deterministic_config() -> BaselineConfig:
    """A fully populated, fixed-value config — no RNG, no defaults."""
    return BaselineConfig(
        baseline_id="a" * 32,
        parent_run_id="b" * 32,
        parent_trade_log_hash="t" * 64,
        method="random_ema_windows",
        sample_count=100,
        random_seed=42,
        method_params={"fast_range": [3, 12], "slow_range": [10, 30]},
        target_metrics=["sharpe_ratio", "total_return_pct"],
        created_at_ms=1_736_000_000_000,
    )


def _deterministic_result() -> BaselineResult:
    """A fully populated, fixed-value result — same baseline id as the config."""
    return BaselineResult(
        baseline_id="a" * 32,
        parent_run_id="b" * 32,
        method="random_ema_windows",
        sample_count=100,
        baselines=[],
        null_distributions=[],
        warnings=[],
        created_at_ms=1_736_000_000_000,
        completed_at_ms=1_736_000_005_000,
        status="completed",
        failure_reason=None,
    )


def test_baselines_save_writes_byte_identical_canonical_json(tmp_path: Path):
    """The bytes on disk match ``json.dumps(model_dump(mode='json'), ensure_ascii=False)``.

    This is the canonical-bytes formula the pre-seam
    ``app.research.baselines.storage._atomic_write_json`` used. The
    new ``ArtifactStore.save`` must preserve it byte-for-byte so the
    PR is genuinely behaviour-preserving (no on-disk schema change,
    no migration cost).
    """
    config = _deterministic_config()
    result = _deterministic_result()

    save_baseline(config, result, root=tmp_path)

    baseline_dir = tmp_path / "baselines" / config.baseline_id
    config_bytes = (baseline_dir / "config.json").read_bytes()
    result_bytes = (baseline_dir / "result.json").read_bytes()

    expected_config_bytes = json.dumps(
        config.model_dump(mode="json"), ensure_ascii=False
    ).encode("utf-8")
    expected_result_bytes = json.dumps(
        result.model_dump(mode="json"), ensure_ascii=False
    ).encode("utf-8")

    assert config_bytes == expected_config_bytes
    assert result_bytes == expected_result_bytes
