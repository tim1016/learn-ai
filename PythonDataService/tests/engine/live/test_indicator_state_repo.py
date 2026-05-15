"""Tests for IndicatorStateRepo — atomic write, advisory lock, newer-check."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.engine.live.indicator_state import (
    IndicatorStateEnvelope,
    IndicatorStateRepo,
)


def _make_envelope(last_bar_ms: int, captured_at_ms: int = 1_700_000_000_000) -> IndicatorStateEnvelope:
    return IndicatorStateEnvelope(
        schema_version=1,
        strategy_key="spy_ema_crossover",
        symbol="SPY",
        consolidator_period_min=15,
        last_consolidated_bar_end_ms=last_bar_ms,
        captured_at_ms=captured_at_ms,
        captured_reason="force_flat",
        code_sha="abc",
        strategy_spec_sha="def",
        payload={"ema5": {"is_ready": True}},
    )


def test_read_missing_returns_none(tmp_path: Path) -> None:
    repo = IndicatorStateRepo(tmp_path / "missing.json")
    assert repo.read() is None


def test_write_then_read_round_trip(tmp_path: Path) -> None:
    repo = IndicatorStateRepo(tmp_path / "state.json")
    env = _make_envelope(last_bar_ms=1_700_000_000_000)
    repo.write(env)
    loaded = repo.read()
    assert loaded == env


def test_write_creates_parent_directory(tmp_path: Path) -> None:
    deep_path = tmp_path / "nested" / "dir" / "state.json"
    repo = IndicatorStateRepo(deep_path)
    env = _make_envelope(last_bar_ms=1_700_000_000_000)
    repo.write(env)
    assert deep_path.exists()


def test_corrupt_json_read_raises(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{ not json")
    repo = IndicatorStateRepo(path)
    # Per spec: corrupt JSON is one of the failure modes the validation
    # ladder catches. Repo.read raises; callers convert to receipt.
    with pytest.raises(Exception):
        repo.read()


def test_is_newer_than_existing_true_when_no_existing(tmp_path: Path) -> None:
    repo = IndicatorStateRepo(tmp_path / "missing.json")
    new = _make_envelope(last_bar_ms=1_700_000_000_000)
    assert repo.is_strictly_newer_than_on_disk(new) is True


def test_is_newer_than_existing_false_when_equal(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    repo = IndicatorStateRepo(path)
    base = _make_envelope(last_bar_ms=1_700_000_000_000)
    repo.write(base)
    same_bar = _make_envelope(last_bar_ms=1_700_000_000_000)
    assert repo.is_strictly_newer_than_on_disk(same_bar) is False


def test_is_newer_than_existing_false_when_older(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    repo = IndicatorStateRepo(path)
    repo.write(_make_envelope(last_bar_ms=1_700_000_000_000))
    older = _make_envelope(last_bar_ms=1_500_000_000_000)
    assert repo.is_strictly_newer_than_on_disk(older) is False


def test_is_newer_than_existing_true_when_strictly_newer(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    repo = IndicatorStateRepo(path)
    repo.write(_make_envelope(last_bar_ms=1_700_000_000_000))
    newer = _make_envelope(last_bar_ms=1_800_000_000_000)
    assert repo.is_strictly_newer_than_on_disk(newer) is True


def test_sha256_of_on_disk(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    repo = IndicatorStateRepo(path)
    assert repo.sha256_of_on_disk() is None  # missing file
    repo.write(_make_envelope(last_bar_ms=1_700_000_000_000))
    sha = repo.sha256_of_on_disk()
    assert sha is not None and len(sha) == 64  # hex sha-256


def test_atomic_write_does_not_leak_tmp_on_success(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    repo = IndicatorStateRepo(path)
    repo.write(_make_envelope(last_bar_ms=1_700_000_000_000))
    # The .tmp file should not survive a successful write.
    tmp_siblings = list(tmp_path.glob("*.tmp*"))
    assert tmp_siblings == []


def test_lock_file_created_on_write(tmp_path: Path) -> None:
    """Confirms the locking path runs — the .lock file is present after a write."""
    repo = IndicatorStateRepo(tmp_path / "state.json")
    repo.write(_make_envelope(last_bar_ms=1_700_000_000_000))
    assert (tmp_path / "state.json.lock").exists()
