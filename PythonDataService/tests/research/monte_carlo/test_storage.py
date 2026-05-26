"""Monte Carlo file-backed storage tests.

Mirrors the Phase A / C / WF storage suites — round-trip, atomic
writes, filtering, path-traversal defence — adapted to the MC
directory shape (``<root>/monte-carlo/<mc_id>/{config,result}.json``).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from app.research.monte_carlo import (
    MonteCarloAlreadyExistsError,
    MonteCarloConfig,
    MonteCarloCorruptError,
    MonteCarloNotFoundError,
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


def test_save_writes_canonical_json(tmp_path: Path):
    config = _make_config()
    result = _make_result()
    save_monte_carlo(config, result, root=tmp_path)

    cfg_payload = json.loads(
        (tmp_path / "monte-carlo" / config.monte_carlo_id / "config.json").read_text()
    )
    res_payload = json.loads(
        (tmp_path / "monte-carlo" / config.monte_carlo_id / "result.json").read_text()
    )
    assert cfg_payload["monte_carlo_id"] == config.monte_carlo_id
    assert res_payload["monte_carlo_id"] == config.monte_carlo_id


# ---------------------------------------------------------------------------
# Failure modes.
# ---------------------------------------------------------------------------
def test_load_missing_raises(tmp_path: Path):
    with pytest.raises(MonteCarloNotFoundError):
        load_monte_carlo("b" * 32, root=tmp_path)


def test_save_refuses_to_overwrite(tmp_path: Path):
    config = _make_config()
    result = _make_result()
    save_monte_carlo(config, result, root=tmp_path)
    with pytest.raises(MonteCarloAlreadyExistsError):
        save_monte_carlo(config, result, root=tmp_path)


def test_save_replace_overwrites(tmp_path: Path):
    config = _make_config()
    result = _make_result()
    save_monte_carlo(config, result, root=tmp_path)
    new_result = _make_result(failure_reason="manually overridden", status="failed")
    save_monte_carlo(config, new_result, root=tmp_path, replace=True)
    _, loaded = load_monte_carlo(config.monte_carlo_id, root=tmp_path)
    assert loaded.failure_reason == "manually overridden"
    assert loaded.status == "failed"


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
def test_list_empty(tmp_path: Path):
    assert list_monte_carlos(root=tmp_path) == []


def test_list_orders_by_created_at_desc(tmp_path: Path):
    older_cfg = _make_config(monte_carlo_id="a" * 32, created_at_ms=1_700_000_000_000)
    older_result = _make_result(
        monte_carlo_id="a" * 32, created_at_ms=1_700_000_000_000
    )
    newer_cfg = _make_config(monte_carlo_id="b" * 32, created_at_ms=1_800_000_000_000)
    newer_result = _make_result(
        monte_carlo_id="b" * 32, created_at_ms=1_800_000_000_000
    )
    save_monte_carlo(older_cfg, older_result, root=tmp_path)
    save_monte_carlo(newer_cfg, newer_result, root=tmp_path)

    listed = list_monte_carlos(root=tmp_path)
    assert [c.monte_carlo_id for c in listed] == [
        newer_cfg.monte_carlo_id,
        older_cfg.monte_carlo_id,
    ]


def test_list_filter_by_parent_run_id(tmp_path: Path):
    a = _make_config(monte_carlo_id="a" * 32, parent_run_id="parent-1")
    b = _make_config(monte_carlo_id="b" * 32, parent_run_id="parent-2")
    save_monte_carlo(
        a, _make_result(monte_carlo_id=a.monte_carlo_id, parent_run_id=a.parent_run_id),
        root=tmp_path,
    )
    save_monte_carlo(
        b, _make_result(monte_carlo_id=b.monte_carlo_id, parent_run_id=b.parent_run_id),
        root=tmp_path,
    )

    filtered = list_monte_carlos(root=tmp_path, parent_run_id="parent-1")
    assert [c.monte_carlo_id for c in filtered] == ["a" * 32]


def test_list_filter_by_method(tmp_path: Path):
    a = _make_config(monte_carlo_id="a" * 32, method="reshuffle")
    b = _make_config(monte_carlo_id="b" * 32, method="resample")
    save_monte_carlo(
        a, _make_result(monte_carlo_id=a.monte_carlo_id, method="reshuffle"),
        root=tmp_path,
    )
    save_monte_carlo(
        b, _make_result(monte_carlo_id=b.monte_carlo_id, method="resample"),
        root=tmp_path,
    )

    reshuffles = list_monte_carlos(root=tmp_path, method="reshuffle")
    assert [c.monte_carlo_id for c in reshuffles] == ["a" * 32]


def test_list_filter_by_since_ms(tmp_path: Path):
    a = _make_config(monte_carlo_id="a" * 32, created_at_ms=1_700_000_000_000)
    b = _make_config(monte_carlo_id="b" * 32, created_at_ms=1_800_000_000_000)
    save_monte_carlo(
        a, _make_result(monte_carlo_id=a.monte_carlo_id, created_at_ms=a.created_at_ms),
        root=tmp_path,
    )
    save_monte_carlo(
        b, _make_result(monte_carlo_id=b.monte_carlo_id, created_at_ms=b.created_at_ms),
        root=tmp_path,
    )

    by_since = list_monte_carlos(root=tmp_path, since_ms=1_750_000_000_000)
    assert [c.monte_carlo_id for c in by_since] == [b.monte_carlo_id]


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
