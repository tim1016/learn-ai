"""Walk-forward file-backed storage tests.

Mirrors the Phase A storage suite — round-trip, atomic writes,
filtering, path-traversal defense — adapted to the WF directory
shape (``<root>/walk-forward/<wf_id>/{config,result}.json``).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from app.research.walk_forward import (
    SplitPolicySpec,
    WalkForwardAlreadyExistsError,
    WalkForwardConfig,
    WalkForwardCorruptError,
    WalkForwardNotFoundError,
    WalkForwardResult,
    list_walk_forwards,
    load_walk_forward,
    save_walk_forward,
)


def _make_config(**overrides) -> WalkForwardConfig:
    base: dict = {
        "walk_forward_id": "a" * 32,
        "parent_run_id": None,
        "strategy_spec_hash": "d" * 64,
        "strategy_spec_json": {"name": "test_spec"},
        "symbol": "TEST",
        "resolution_minutes": 15,
        "start_ms": 1704160800000,
        "end_ms": 1735714800000,
        "initial_cash": 100_000.0,
        "fill_mode": "signal_bar_close",
        "commission_per_order": 0.0,
        "slippage_per_share": 0.0,
        "random_seed": 0,
        "split_policy": SplitPolicySpec(kind="chronological"),
        "created_at_ms": 1736000000000,
    }
    base.update(overrides)
    return WalkForwardConfig(**base)


def _make_result(**overrides) -> WalkForwardResult:
    base: dict = {
        "walk_forward_id": "a" * 32,
        "parent_run_id": None,
        "strategy_spec_hash": "d" * 64,
        "split_policy": SplitPolicySpec(kind="chronological"),
        "folds": [],
        "combined_oos_equity_curve": [],
        "mean_oos_sharpe": None,
        "median_oos_sharpe": None,
        "pct_profitable_folds": None,
        "oos_retention": None,
        "alpha_decay": None,
        "warnings": [],
        "created_at_ms": 1736000000000,
        "completed_at_ms": 1736000005000,
        "status": "completed",
        "failure_reason": None,
    }
    base.update(overrides)
    return WalkForwardResult(**base)


# ---------------------------------------------------------------------------
# Round-trip.
# ---------------------------------------------------------------------------
def test_save_then_load_round_trips(tmp_path: Path):
    config = _make_config()
    result = _make_result()

    wf_dir = save_walk_forward(config, result, root=tmp_path)
    assert wf_dir == tmp_path / "walk-forward" / config.walk_forward_id
    assert (wf_dir / "config.json").is_file()
    assert (wf_dir / "result.json").is_file()

    loaded_config, loaded_result = load_walk_forward(
        config.walk_forward_id, root=tmp_path
    )
    assert loaded_config.model_dump() == config.model_dump()
    assert loaded_result.model_dump() == result.model_dump()


def test_save_writes_canonical_json(tmp_path: Path):
    config = _make_config()
    result = _make_result()
    save_walk_forward(config, result, root=tmp_path)

    cfg_payload = json.loads(
        (tmp_path / "walk-forward" / config.walk_forward_id / "config.json").read_text()
    )
    res_payload = json.loads(
        (tmp_path / "walk-forward" / config.walk_forward_id / "result.json").read_text()
    )
    assert cfg_payload["walk_forward_id"] == config.walk_forward_id
    assert res_payload["walk_forward_id"] == config.walk_forward_id


# ---------------------------------------------------------------------------
# Failure modes.
# ---------------------------------------------------------------------------
def test_load_missing_walk_forward_raises(tmp_path: Path):
    with pytest.raises(WalkForwardNotFoundError):
        load_walk_forward("b" * 32, root=tmp_path)


def test_save_refuses_to_overwrite(tmp_path: Path):
    config = _make_config()
    result = _make_result()
    save_walk_forward(config, result, root=tmp_path)
    with pytest.raises(WalkForwardAlreadyExistsError):
        save_walk_forward(config, result, root=tmp_path)


def test_save_replace_overwrites(tmp_path: Path):
    config = _make_config()
    result = _make_result()
    save_walk_forward(config, result, root=tmp_path)
    new_result = _make_result(failure_reason="manually overridden", status="failed")
    save_walk_forward(config, new_result, root=tmp_path, replace=True)
    _, loaded = load_walk_forward(config.walk_forward_id, root=tmp_path)
    assert loaded.failure_reason == "manually overridden"
    assert loaded.status == "failed"


def test_save_rejects_id_mismatch(tmp_path: Path):
    config = _make_config()
    result = _make_result(walk_forward_id="z" * 32)
    with pytest.raises(ValueError, match="walk_forward_id"):
        save_walk_forward(config, result, root=tmp_path)


def test_load_corrupt_config_raises(tmp_path: Path):
    config = _make_config()
    result = _make_result()
    save_walk_forward(config, result, root=tmp_path)
    (tmp_path / "walk-forward" / config.walk_forward_id / "config.json").write_text(
        "{not valid json"
    )
    with pytest.raises(WalkForwardCorruptError, match=r"config\.json"):
        load_walk_forward(config.walk_forward_id, root=tmp_path)


# ---------------------------------------------------------------------------
# Path-traversal defense.
# ---------------------------------------------------------------------------
def test_load_with_malformed_id_raises_value_error(tmp_path: Path):
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
            load_walk_forward(bad, root=tmp_path)


def test_save_with_malformed_id_raises_value_error(tmp_path: Path):
    config = _make_config(walk_forward_id="../escape")
    result = _make_result(walk_forward_id="../escape")
    with pytest.raises(ValueError):
        save_walk_forward(config, result, root=tmp_path)


# ---------------------------------------------------------------------------
# Listing & filtering.
# ---------------------------------------------------------------------------
def test_list_empty_returns_empty(tmp_path: Path):
    assert list_walk_forwards(root=tmp_path) == []


def test_list_orders_by_created_at_desc(tmp_path: Path):
    older_cfg = _make_config(walk_forward_id="a" * 32, created_at_ms=1_700_000_000_000)
    older_result = _make_result(
        walk_forward_id="a" * 32, created_at_ms=1_700_000_000_000
    )
    newer_cfg = _make_config(walk_forward_id="b" * 32, created_at_ms=1_800_000_000_000)
    newer_result = _make_result(
        walk_forward_id="b" * 32, created_at_ms=1_800_000_000_000
    )
    save_walk_forward(older_cfg, older_result, root=tmp_path)
    save_walk_forward(newer_cfg, newer_result, root=tmp_path)

    listed = list_walk_forwards(root=tmp_path)
    assert [c.walk_forward_id for c in listed] == [newer_cfg.walk_forward_id, older_cfg.walk_forward_id]


def test_list_filter_by_parent_run_id(tmp_path: Path):
    a = _make_config(walk_forward_id="a" * 32, parent_run_id="parent-1")
    a_result = _make_result(walk_forward_id="a" * 32, parent_run_id="parent-1")
    b = _make_config(walk_forward_id="b" * 32, parent_run_id="parent-2")
    b_result = _make_result(walk_forward_id="b" * 32, parent_run_id="parent-2")
    save_walk_forward(a, a_result, root=tmp_path)
    save_walk_forward(b, b_result, root=tmp_path)

    filtered = list_walk_forwards(root=tmp_path, parent_run_id="parent-1")
    assert [c.walk_forward_id for c in filtered] == ["a" * 32]


def test_list_filter_by_spec_hash_and_since_ms(tmp_path: Path):
    a = _make_config(
        walk_forward_id="a" * 32,
        strategy_spec_hash="hash-1" + "0" * 58,
        created_at_ms=1_700_000_000_000,
    )
    b = _make_config(
        walk_forward_id="b" * 32,
        strategy_spec_hash="hash-2" + "0" * 58,
        created_at_ms=1_800_000_000_000,
    )
    save_walk_forward(a, _make_result(walk_forward_id=a.walk_forward_id, strategy_spec_hash=a.strategy_spec_hash), root=tmp_path)
    save_walk_forward(b, _make_result(walk_forward_id=b.walk_forward_id, strategy_spec_hash=b.strategy_spec_hash), root=tmp_path)

    by_hash = list_walk_forwards(root=tmp_path, spec_hash=a.strategy_spec_hash)
    assert [c.walk_forward_id for c in by_hash] == [a.walk_forward_id]

    by_since = list_walk_forwards(root=tmp_path, since_ms=1_750_000_000_000)
    assert [c.walk_forward_id for c in by_since] == [b.walk_forward_id]


def test_list_skips_corrupt_config(tmp_path: Path, caplog):
    cfg = _make_config()
    result = _make_result()
    save_walk_forward(cfg, result, root=tmp_path)

    bad_dir = tmp_path / "walk-forward" / "corrupt-wf-dir-not-a-uuid"
    bad_dir.mkdir(parents=True)
    (bad_dir / "config.json").write_text("{not valid json")
    (bad_dir / "result.json").write_text("{}")

    # After the seam migration the corrupt-skip warning fires from
    # ``app.research.artifact.store`` rather than this phase's
    # ``storage`` module — but it still carries the ``[WF]`` prefix
    # the descriptor declares via ``log_tag="WF"`` so operator grep
    # patterns are preserved.
    with caplog.at_level(logging.WARNING):
        listed = list_walk_forwards(root=tmp_path)
    assert [c.walk_forward_id for c in listed] == [cfg.walk_forward_id]
    assert any(
        rec.message.startswith("[WF]") and "skipping corrupt" in rec.message
        for rec in caplog.records
    )
