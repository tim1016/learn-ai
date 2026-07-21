"""Tests for the deterministic-rule generator: rsi_14_centered.

The rule is: prediction = rsi_14(close) / 100.0 - 0.5
Bars before RSI is ready emit prediction = 0.0 (warmup_policy:
neutral_zero_until_feature_ready).
"""
from __future__ import annotations

import json
from datetime import date as Date
from pathlib import Path

import pytest

from app.research.ml.generators.deterministic_rule import compute_rsi_14_centered_predictions


def test_first_thirteen_bars_emit_zero() -> None:
    closes = [100.0 + i * 0.1 for i in range(20)]
    timestamps_ms = [1000 * i for i in range(20)]
    rows = compute_rsi_14_centered_predictions(closes, timestamps_ms)
    assert all(r["prediction"] == 0.0 for r in rows[:13])


def test_rsi_warmup_zero_count_is_pinned() -> None:
    """Pin the exact number of bars that emit prediction=0.0 due to RSI warmup.

    Observed: pandas_ta RSI-14 NaN only at index 0 (not indices 0-13).
    The ``idx < 13`` gate is an explicit design choice that pins 13 warmup
    zeros (indices 0-12); index 13 is the first non-zero output.

    Drift in this count signals either a pandas_ta behaviour change or an
    intentional threshold change. Either way, the E2E hash fixture in
    ``tests/research/ml/fixtures/e2e_known_hashes.json`` must also be
    regenerated with a provenance note.
    """
    closes = [100.0 + i * 0.1 for i in range(50)]
    timestamps_ms = [1000 * i for i in range(50)]
    rows = compute_rsi_14_centered_predictions(closes, timestamps_ms)
    zero_count = sum(1 for r in rows if r["prediction"] == 0.0)
    assert zero_count == 13, (
        f"RSI warmup zero-count drifted to {zero_count}; "
        "if intentional (e.g. pandas_ta upgrade or threshold change), update this assertion "
        "and the docstring on compute_rsi_14_centered_predictions, "
        "and regenerate tests/research/ml/fixtures/e2e_known_hashes.json with a provenance note"
    )


def test_warmed_bars_have_nonzero_prediction() -> None:
    closes = [100.0 + i * 0.1 for i in range(20)]
    timestamps_ms = [1000 * i for i in range(20)]
    rows = compute_rsi_14_centered_predictions(closes, timestamps_ms)
    assert any(r["prediction"] != 0.0 for r in rows[14:])


def test_predictions_are_in_range_minus_half_to_plus_half() -> None:
    closes = [100.0 + i * 0.1 for i in range(50)]
    timestamps_ms = [1000 * i for i in range(50)]
    rows = compute_rsi_14_centered_predictions(closes, timestamps_ms)
    for r in rows:
        assert -0.5 <= r["prediction"] <= 0.5


def test_deterministic_for_same_input() -> None:
    closes = [100.0 + i * 0.1 for i in range(20)]
    timestamps_ms = [1000 * i for i in range(20)]
    a = compute_rsi_14_centered_predictions(closes, timestamps_ms)
    b = compute_rsi_14_centered_predictions(closes, timestamps_ms)
    assert a == b


def test_row_shape() -> None:
    closes = [100.0]
    timestamps_ms = [0]
    rows = compute_rsi_14_centered_predictions(closes, timestamps_ms)
    assert rows[0] == {"timestamp_ms": 0, "symbol": "SPY", "prediction": 0.0}


def test_symbol_passes_through_arg() -> None:
    rows = compute_rsi_14_centered_predictions([100.0], [0], symbol="QQQ")
    assert rows[0]["symbol"] == "QQQ"


# ----- CLI orchestration --------------------------------------------


from app.research.ml.generate_prediction_set import generate_prediction_set  # noqa: E402


def _synthetic_bars_provider(num_bars: int):
    """Return a callable that yields (close, timestamp_ms) pairs the generator
    can consume. The CLI's real path will call into the LEAN reader; this
    factory swaps a fixed synthetic series for unit tests.
    """

    def provider(*, symbol: str, start: Date, end: Date, resolution_minutes: int):
        for i in range(num_bars):
            close = 100.0 + i * 0.1
            timestamp_ms = 1714521600000 + i * resolution_minutes * 60_000
            yield close, timestamp_ms

    return provider


def test_generate_writes_artifact_round_trip(tmp_path: Path) -> None:
    """Generator -> loader round-trip: hash check passes."""
    out_dir = tmp_path / "artifacts" / "predictions"
    out_dir.mkdir(parents=True)

    set_id = generate_prediction_set(
        rule="rsi_14_centered",
        symbol="SPY",
        start=Date(2024, 5, 1),
        end=Date(2024, 5, 2),
        resolution_minutes=15,
        artifacts_root=out_dir,
        bars_provider=_synthetic_bars_provider(num_bars=20),
    )

    assert set_id == "pred_spy_rsi_14_centered_2024-05-01_2024-05-02_15m_v1"
    set_dir = out_dir / set_id
    assert (set_dir / "manifest.json").is_file()

    from app.research.ml.loader import PredictionSet

    pset = PredictionSet.load(set_dir)
    assert pset.manifest.prediction_set_id == set_id
    assert pset.manifest.warmup_policy == "neutral_zero_until_feature_ready"


def test_generate_is_byte_identical_on_repeat(tmp_path: Path) -> None:
    """Same inputs -> same prediction_set_hash."""
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    out_a.mkdir()
    out_b.mkdir()

    set_id_a = generate_prediction_set(
        rule="rsi_14_centered",
        symbol="SPY",
        start=Date(2024, 5, 1),
        end=Date(2024, 5, 2),
        resolution_minutes=15,
        artifacts_root=out_a,
        bars_provider=_synthetic_bars_provider(num_bars=20),
    )
    set_id_b = generate_prediction_set(
        rule="rsi_14_centered",
        symbol="SPY",
        start=Date(2024, 5, 1),
        end=Date(2024, 5, 2),
        resolution_minutes=15,
        artifacts_root=out_b,
        bars_provider=_synthetic_bars_provider(num_bars=20),
    )
    assert set_id_a == set_id_b

    a_manifest = json.loads((out_a / set_id_a / "manifest.json").read_text())
    b_manifest = json.loads((out_b / set_id_b / "manifest.json").read_text())
    assert a_manifest["prediction_set_hash"] == b_manifest["prediction_set_hash"]


# ----- LEAN-backed CLI smoke ----------------------------------------
import os  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402


@pytest.mark.slow
def test_cli_generates_real_artifact_for_one_day(tmp_path: Path) -> None:
    """Smoke test: run the CLI against one locally cached LEAN session."""
    cache_root = Path(__file__).resolve().parents[3] / "lean-cache"
    spy_minute_dir = cache_root / "equity" / "usa" / "minute" / "spy"
    archives = sorted(spy_minute_dir.glob("2*_trade.zip"))
    if not archives:
        pytest.skip("local LEAN SPY minute cache unavailable; export a session to run this smoke test")

    session_date = Date.fromisoformat(
        f"{archives[0].name[:4]}-{archives[0].name[4:6]}-{archives[0].name[6:8]}"
    )
    cmd = [
        sys.executable, "-m", "app.research.ml.generate_prediction_set",
        "--rule", "rsi_14_centered",
        "--symbol", "SPY",
        "--start", session_date.isoformat(),
        "--end", session_date.isoformat(),
        "--resolution-minutes", "15",
        "--artifacts-root", str(tmp_path),
    ]
    env = os.environ.copy()
    env["LEAN_DATA_CACHE"] = str(cache_root)
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120)
    assert result.returncode == 0, result.stderr
    set_id = result.stdout.strip()
    assert (tmp_path / set_id / "manifest.json").is_file()
