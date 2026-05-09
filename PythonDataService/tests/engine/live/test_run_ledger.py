"""Tests for app.engine.live.run_ledger.

Identity hash is computed by app.research.runs.hashing.canonical_json
+ SHA-256, which already has its own correctness tests under
tests/research/runs/test_hashing.py. These tests cover the live-runtime
wiring on top: identity payload composition, file-hashing on disk,
deterministic round-trip, and FileNotFoundError on missing inputs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.engine.live.run_ledger import (
    LiveRunLedger,
    build_ledger,
    compute_run_id,
    read_ledger,
    write_ledger,
)


def _make_inputs(tmp_path: Path) -> tuple[Path, Path]:
    spec = tmp_path / "spec.json"
    qc_copy = tmp_path / "qc_audit.py"
    spec.write_text('{"strategy": "spy_ema_crossover"}', encoding="utf-8")
    qc_copy.write_text("# QC audit copy of SpyEmaCrossoverAlgorithm\n", encoding="utf-8")
    return spec, qc_copy


def test_compute_run_id_is_deterministic_for_same_payload() -> None:
    payload = {
        "code_sha": "abc123",
        "strategy_spec_sha256": "deadbeef",
        "qc_audit_copy_sha256": "cafe",
        "qc_cloud_backtest_id": "bt-1",
        "account_id": "DU111",
        "start_date_ms": 1_700_000_000_000,
        "live_config": {"symbol": "SPY"},
    }
    a = compute_run_id(**payload)
    b = compute_run_id(**payload)
    assert a == b
    assert len(a) == 64


def test_compute_run_id_changes_when_any_field_changes() -> None:
    base = {
        "code_sha": "abc123",
        "strategy_spec_sha256": "deadbeef",
        "qc_audit_copy_sha256": "cafe",
        "qc_cloud_backtest_id": "bt-1",
        "account_id": "DU111",
        "start_date_ms": 1_700_000_000_000,
        "live_config": {"symbol": "SPY"},
    }
    base_id = compute_run_id(**base)

    diffs = [
        {**base, "code_sha": "def456"},
        {**base, "strategy_spec_sha256": "feedface"},
        {**base, "qc_audit_copy_sha256": "babe"},
        {**base, "qc_cloud_backtest_id": "bt-2"},
        {**base, "account_id": "DU222"},
        {**base, "start_date_ms": 1_700_000_000_001},
        {**base, "live_config": {"symbol": "QQQ"}},
    ]
    for diff in diffs:
        assert compute_run_id(**diff) != base_id, f"identity collision on {diff}"


def test_build_ledger_hashes_inputs_and_assigns_run_id(tmp_path: Path) -> None:
    spec, qc_copy = _make_inputs(tmp_path)

    ledger = build_ledger(
        code_sha="abc123",
        strategy_spec_path=spec,
        qc_audit_copy_path=qc_copy,
        qc_cloud_backtest_id="bt-1",
        account_id="DU111",
        start_date_ms=1_700_000_000_000,
        live_config={"symbol": "SPY"},
    )
    assert isinstance(ledger, LiveRunLedger)
    assert len(ledger.run_id) == 64
    assert ledger.code_sha == "abc123"
    assert ledger.account_id == "DU111"
    assert len(ledger.strategy_spec_sha256) == 64
    assert len(ledger.qc_audit_copy_sha256) == 64


def test_build_ledger_raises_on_missing_strategy_spec(tmp_path: Path) -> None:
    _, qc_copy = _make_inputs(tmp_path)
    with pytest.raises(FileNotFoundError) as exc:
        build_ledger(
            code_sha="abc123",
            strategy_spec_path=tmp_path / "missing.json",
            qc_audit_copy_path=qc_copy,
            qc_cloud_backtest_id="bt-1",
            account_id="DU111",
            start_date_ms=1_700_000_000_000,
            live_config={},
        )
    assert "strategy_spec_path" in str(exc.value)


def test_build_ledger_raises_on_missing_qc_audit_copy(tmp_path: Path) -> None:
    spec, _ = _make_inputs(tmp_path)
    with pytest.raises(FileNotFoundError) as exc:
        build_ledger(
            code_sha="abc123",
            strategy_spec_path=spec,
            qc_audit_copy_path=tmp_path / "missing.py",
            qc_cloud_backtest_id="bt-1",
            account_id="DU111",
            start_date_ms=1_700_000_000_000,
            live_config={},
        )
    assert "qc_audit_copy_path" in str(exc.value)


def test_write_then_read_ledger_round_trips(tmp_path: Path) -> None:
    spec, qc_copy = _make_inputs(tmp_path)
    ledger = build_ledger(
        code_sha="abc123",
        strategy_spec_path=spec,
        qc_audit_copy_path=qc_copy,
        qc_cloud_backtest_id="bt-1",
        account_id="DU111",
        start_date_ms=1_700_000_000_000,
        live_config={"symbol": "SPY"},
    )
    out = tmp_path / "live_runs" / ledger.run_id / "run_ledger.json"
    write_ledger(out, ledger)

    loaded = read_ledger(out)
    assert loaded.run_id == ledger.run_id
    assert loaded.code_sha == ledger.code_sha
    assert loaded.strategy_spec_sha256 == ledger.strategy_spec_sha256
    assert loaded.qc_audit_copy_sha256 == ledger.qc_audit_copy_sha256
    assert loaded.live_config == {"symbol": "SPY"}


def test_write_ledger_produces_canonical_bytes(tmp_path: Path) -> None:
    """Ledger JSON bytes are deterministic — same inputs ⇒ same SHA-256.

    This is what § 6.5's ``run_ledger`` hash field in the daily Markdown
    relies on. If the JSON serialization order ever drifts, downstream
    hash verification breaks silently.
    """
    spec, qc_copy = _make_inputs(tmp_path)
    ledger = build_ledger(
        code_sha="abc123",
        strategy_spec_path=spec,
        qc_audit_copy_path=qc_copy,
        qc_cloud_backtest_id="bt-1",
        account_id="DU111",
        start_date_ms=1_700_000_000_000,
        live_config={"symbol": "SPY", "force_flat_minutes": 5},
    )
    p1 = tmp_path / "a" / "ledger.json"
    p2 = tmp_path / "b" / "ledger.json"
    write_ledger(p1, ledger)
    write_ledger(p2, ledger)
    assert p1.read_bytes() == p2.read_bytes()
