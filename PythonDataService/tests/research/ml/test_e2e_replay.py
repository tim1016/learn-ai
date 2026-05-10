"""End-to-end determinism + replay tests.

Runs the deterministic-rule generator on a fixed window, asserts the
prediction_set_hash matches a committed fixture, regenerates and confirms
the hash is unchanged.

If either committed hash drifts, that's a regression (or an intentional
bump that requires updating the fixture file with justification).
"""

from __future__ import annotations

import json
from datetime import date as Date
from pathlib import Path

import pytest

from app.research.ml.generate_prediction_set import generate_prediction_set

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "e2e_known_hashes.json"


@pytest.fixture
def known_hashes() -> dict:
    return json.loads(_FIXTURE_PATH.read_text())


def _synthetic_bars_provider(num_bars: int = 30):
    def provider(*, symbol, start, end, resolution_minutes):
        for i in range(num_bars):
            close = 100.0 + i * 0.1
            timestamp_ms = 1714521600000 + i * resolution_minutes * 60_000
            yield close, timestamp_ms
    return provider


def test_e2e_prediction_set_hash_matches_fixture(tmp_path: Path, known_hashes: dict) -> None:
    set_id = generate_prediction_set(
        rule="rsi_14_centered", symbol="SPY",
        start=Date(2024, 5, 1), end=Date(2024, 5, 2),
        resolution_minutes=15,
        artifacts_root=tmp_path,
        bars_provider=_synthetic_bars_provider(),
    )
    manifest = json.loads((tmp_path / set_id / "manifest.json").read_text())
    assert manifest["prediction_set_hash"] == known_hashes["prediction_set_hash"], (
        "prediction_set_hash drifted; if intentional, update "
        "tests/research/ml/fixtures/e2e_known_hashes.json with justification"
    )


def test_e2e_regenerate_produces_same_hash(tmp_path: Path) -> None:
    """Generating a fresh artifact twice produces the same hash."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()

    set_id = generate_prediction_set(
        rule="rsi_14_centered", symbol="SPY",
        start=Date(2024, 5, 1), end=Date(2024, 5, 2),
        resolution_minutes=15, artifacts_root=a, bars_provider=_synthetic_bars_provider(),
    )
    set_id_b = generate_prediction_set(
        rule="rsi_14_centered", symbol="SPY",
        start=Date(2024, 5, 1), end=Date(2024, 5, 2),
        resolution_minutes=15, artifacts_root=b, bars_provider=_synthetic_bars_provider(),
    )
    assert set_id == set_id_b

    h_a = json.loads((a / set_id / "manifest.json").read_text())["prediction_set_hash"]
    h_b = json.loads((b / set_id_b / "manifest.json").read_text())["prediction_set_hash"]
    assert h_a == h_b
