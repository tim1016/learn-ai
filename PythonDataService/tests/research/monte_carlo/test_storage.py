"""Monte Carlo file-backed storage tests.

Mirrors the Phase A / C / WF storage suites — round-trip, atomic
writes, filtering, path-traversal defence — adapted to the MC
directory shape (``<root>/monte-carlo/<mc_id>/{config,result}.json``).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.research.monte_carlo import (
    MonteCarloAlreadyExistsError,
    MonteCarloConfig,
    MonteCarloCorruptError,
    MonteCarloResult,
    list_monte_carlos,
    load_monte_carlo,
    save_monte_carlo,
)


def _make_config(**overrides) -> MonteCarloConfig:
    base: dict = {
        "monte_carlo_id": "a" * 32,
        "parent_run_id": "p" * 32,
        "parent_trade_log_hash": "t" * 64,
        "method": "reshuffle",
        "simulation_count": 1000,
        "projection_trade_count": 0,
        "initial_equity": 100_000.0,
        "random_seed": 0,
        "breach_thresholds": [0.1, 0.2],
        "created_at_ms": 1736000000000,
    }
    base.update(overrides)
    return MonteCarloConfig(**base)


def _make_result(**overrides) -> MonteCarloResult:
    base: dict = {
        "monte_carlo_id": "a" * 32,
        "parent_run_id": "p" * 32,
        "method": "reshuffle",
        "simulation_count": 1000,
        "realised_trade_count": 25,
        "equity_bands": [],
        "drawdown_quantiles": {"p5": 0.01, "p50": 0.05, "p95": 0.12},
        "terminal_pnl_quantiles": {"p5": -100.0, "p50": 500.0, "p95": 1500.0},
        "max_losing_streak_quantiles": {"p5": 1, "p50": 2, "p95": 4},
        "breach_probabilities": [],
        "warnings": [],
        "created_at_ms": 1736000000000,
        "completed_at_ms": 1736000005000,
        "status": "completed",
        "failure_reason": None,
    }
    base.update(overrides)
    return MonteCarloResult(**base)


# ---------------------------------------------------------------------------
# Round-trip.
# ---------------------------------------------------------------------------
def test_save_then_load_round_trips(tmp_path: Path):
    config = _make_config()
    result = _make_result()

    mc_dir = save_monte_carlo(config, result, root=tmp_path)
    assert mc_dir == tmp_path / "monte-carlo" / config.monte_carlo_id
    assert (mc_dir / "config.json").is_file()
    assert (mc_dir / "result.json").is_file()

    loaded_config, loaded_result = load_monte_carlo(
        config.monte_carlo_id, root=tmp_path
    )
    assert loaded_config.model_dump() == config.model_dump()
    assert loaded_result.model_dump() == result.model_dump()


# ---------------------------------------------------------------------------
# Failure modes.
# ---------------------------------------------------------------------------
def test_save_refuses_to_overwrite(tmp_path: Path):
    config = _make_config()
    result = _make_result()
    save_monte_carlo(config, result, root=tmp_path)
    with pytest.raises(MonteCarloAlreadyExistsError):
        save_monte_carlo(config, result, root=tmp_path)


def test_save_rejects_id_mismatch(tmp_path: Path):
    config = _make_config()
    result = _make_result(monte_carlo_id="z" * 32)
    with pytest.raises(ValueError, match="monte_carlo_id"):
        save_monte_carlo(config, result, root=tmp_path)


def test_load_corrupt_config_raises(tmp_path: Path):
    config = _make_config()
    result = _make_result()
    save_monte_carlo(config, result, root=tmp_path)
    (tmp_path / "monte-carlo" / config.monte_carlo_id / "config.json").write_text(
        "{not valid json"
    )
    with pytest.raises(MonteCarloCorruptError, match=r"config\.json"):
        load_monte_carlo(config.monte_carlo_id, root=tmp_path)


# ---------------------------------------------------------------------------
# Path-traversal defense.
# ---------------------------------------------------------------------------
def test_load_with_malformed_id_raises(tmp_path: Path):
    bad_ids = [
        "../../../etc/passwd",
        "..",
        "/",
        "abc/../def",
        "abc def",
        "ABCDEFABCDEFABCDEFABCDEFABCDEFAB",  # uppercase
        "a" * 31,
        "a" * 33,
        "-" * 32,
    ]
    for bad in bad_ids:
        with pytest.raises(ValueError):
            load_monte_carlo(bad, root=tmp_path)


def test_save_with_malformed_id_raises(tmp_path: Path):
    config = _make_config(monte_carlo_id="../escape")
    result = _make_result(monte_carlo_id="../escape")
    with pytest.raises(ValueError):
        save_monte_carlo(config, result, root=tmp_path)


# ---------------------------------------------------------------------------
# Listing & filtering.
# ---------------------------------------------------------------------------
def test_list_skips_corrupt_config(tmp_path: Path, caplog):
    cfg = _make_config()
    result = _make_result()
    save_monte_carlo(cfg, result, root=tmp_path)

    bad_dir = tmp_path / "monte-carlo" / "corrupt-mc-dir-not-a-uuid"
    bad_dir.mkdir(parents=True)
    (bad_dir / "config.json").write_text("{not valid json")
    (bad_dir / "result.json").write_text("{}")

    # After the seam migration the corrupt-skip warning fires from
    # ``app.research.artifact.store`` rather than this phase's
    # ``storage`` module — but it still carries the ``[MC]`` prefix
    # the descriptor declares via ``log_tag="MC"`` so operator grep
    # patterns are preserved.
    with caplog.at_level(logging.WARNING):
        listed = list_monte_carlos(root=tmp_path)
    assert [c.monte_carlo_id for c in listed] == [cfg.monte_carlo_id]
    assert any(
        rec.message.startswith("[MC]") and "skipping corrupt" in rec.message
        for rec in caplog.records
    )
