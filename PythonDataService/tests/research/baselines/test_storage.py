"""Baselines file-backed storage tests.

Mirrors the Phase A/C/D storage suites — round-trip, atomic writes,
filtering, path-traversal defence — adapted to the baselines
directory shape (``<root>/baselines/<baseline_id>/{config,result}.json``).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.research.baselines import (
    BaselineAlreadyExistsError,
    BaselineConfig,
    BaselineCorruptError,
    BaselineResult,
    list_baselines,
    load_baseline,
    save_baseline,
)


def _make_config(**overrides) -> BaselineConfig:
    base: dict = {
        "baseline_id": "a" * 32,
        "parent_run_id": "p" * 32,
        "parent_trade_log_hash": "t" * 64,
        "method": "buy_and_hold",
        "sample_count": 1,
        "random_seed": 0,
        "method_params": {},
        "target_metrics": ["sharpe_ratio", "total_return_pct"],
        "created_at_ms": 1736000000000,
    }
    base.update(overrides)
    return BaselineConfig(**base)


def _make_result(**overrides) -> BaselineResult:
    base: dict = {
        "baseline_id": "a" * 32,
        "parent_run_id": "p" * 32,
        "method": "buy_and_hold",
        "sample_count": 1,
        "baselines": [],
        "null_distributions": [],
        "warnings": [],
        "created_at_ms": 1736000000000,
        "completed_at_ms": 1736000005000,
        "status": "completed",
        "failure_reason": None,
    }
    base.update(overrides)
    return BaselineResult(**base)


# ---------------------------------------------------------------------------
# Round-trip.
# ---------------------------------------------------------------------------
def test_save_then_load_round_trips(tmp_path: Path):
    config = _make_config()
    result = _make_result()
    bdir = save_baseline(config, result, root=tmp_path)
    assert bdir == tmp_path / "baselines" / config.baseline_id
    assert (bdir / "config.json").is_file()
    assert (bdir / "result.json").is_file()

    loaded_config, loaded_result = load_baseline(config.baseline_id, root=tmp_path)
    assert loaded_config.model_dump() == config.model_dump()
    assert loaded_result.model_dump() == result.model_dump()


# ---------------------------------------------------------------------------
# Failure modes.
# ---------------------------------------------------------------------------
def test_save_refuses_to_overwrite(tmp_path: Path):
    config = _make_config()
    result = _make_result()
    save_baseline(config, result, root=tmp_path)
    with pytest.raises(BaselineAlreadyExistsError):
        save_baseline(config, result, root=tmp_path)


def test_save_rejects_id_mismatch(tmp_path: Path):
    config = _make_config()
    result = _make_result(baseline_id="z" * 32)
    with pytest.raises(ValueError, match="baseline_id"):
        save_baseline(config, result, root=tmp_path)


def test_load_corrupt_config_raises(tmp_path: Path):
    config = _make_config()
    result = _make_result()
    save_baseline(config, result, root=tmp_path)
    (tmp_path / "baselines" / config.baseline_id / "config.json").write_text(
        "{not valid json"
    )
    with pytest.raises(BaselineCorruptError, match=r"config\.json"):
        load_baseline(config.baseline_id, root=tmp_path)


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
            load_baseline(bad, root=tmp_path)


def test_save_with_malformed_id_raises(tmp_path: Path):
    config = _make_config(baseline_id="../escape")
    result = _make_result(baseline_id="../escape")
    with pytest.raises(ValueError):
        save_baseline(config, result, root=tmp_path)


# ---------------------------------------------------------------------------
# Listing.
# ---------------------------------------------------------------------------
def test_list_filter_by_method(tmp_path: Path):
    a = _make_config(baseline_id="a" * 32, method="buy_and_hold")
    b = _make_config(baseline_id="b" * 32, method="random_ema_windows")
    save_baseline(a, _make_result(baseline_id=a.baseline_id, method="buy_and_hold"), root=tmp_path)
    save_baseline(
        b, _make_result(baseline_id=b.baseline_id, method="random_ema_windows"),
        root=tmp_path,
    )
    bh = list_baselines(root=tmp_path, method="buy_and_hold")
    assert [c.baseline_id for c in bh] == ["a" * 32]


def test_list_skips_corrupt_config(tmp_path: Path, caplog):
    cfg = _make_config()
    result = _make_result()
    save_baseline(cfg, result, root=tmp_path)
    bad_dir = tmp_path / "baselines" / "corrupt-bd-not-a-uuid"
    bad_dir.mkdir(parents=True)
    (bad_dir / "config.json").write_text("{not valid json")
    (bad_dir / "result.json").write_text("{}")

    # After the seam migration the corrupt-skip warning fires from
    # ``app.research.artifact.store`` rather than this phase's
    # ``storage`` module — but it still carries the ``[BASELINES]``
    # prefix the descriptor declares via ``log_tag="BASELINES"`` so
    # operator grep patterns are preserved.
    with caplog.at_level(logging.WARNING):
        listed = list_baselines(root=tmp_path)
    assert [c.baseline_id for c in listed] == [cfg.baseline_id]
    assert any(
        rec.message.startswith("[BASELINES]") and "skipping corrupt" in rec.message
        for rec in caplog.records
    )
