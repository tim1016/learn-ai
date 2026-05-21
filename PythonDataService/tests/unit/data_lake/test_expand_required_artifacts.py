"""Tests for ensure_data.expand_required_artifacts (Slice 1a).

Spec-update note: include_lean_metadata has been removed from DataRunSpec;
LEAN metadata is an unconditional Phase 0 prerequisite staged separately
(Slice 1c). expand_required_artifacts never emits metadata artifacts.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from app.data_lake.ensure_data import expand_required_artifacts
from app.data_lake.types import DataRunSpec


def _base_spec(**overrides) -> DataRunSpec:
    payload = {
        "request_id": UUID("12345678-1234-5678-1234-567812345678"),
        "run_type": "python_lab",
        "symbols": ["SPY"],
        "start_trading_date": date(2024, 5, 20),
        "end_trading_date": date(2024, 5, 24),
        "lean_image_digest": "sha256:test",
    }
    payload.update(overrides)
    return DataRunSpec(**payload)


def test_single_symbol_one_week_trade_only():
    required, non_sessions = expand_required_artifacts(_base_spec())
    # 5 trading days x 1 minute-trade + 1 factor + 1 map + 1 daily-trade = 8
    kinds = [a.artifact_kind for a in required]
    assert kinds.count("time_series_bars") == 6  # 5 minute + 1 daily
    assert kinds.count("factor_file") == 1
    assert kinds.count("map_file") == 1
    assert "metadata" not in kinds
    assert non_sessions == []


def test_quote_inclusion_doubles_minute_artifacts():
    required, _ = expand_required_artifacts(_base_spec(data_types=["trade", "quote"]))
    minute_artifacts = [a for a in required if a.artifact_kind == "time_series_bars" and a.resolution == "minute"]
    assert len(minute_artifacts) == 10  # 5 trade + 5 quote


def test_holiday_week_produces_non_sessions():
    spec = _base_spec(
        start_trading_date=date(2024, 5, 25),  # Sat
        end_trading_date=date(2024, 5, 27),  # Memorial Day Mon
    )
    required, non_sessions = expand_required_artifacts(spec)
    # No minute-bar artifacts when there are no trading sessions.
    # The daily derived artifact is per-symbol (not per-session) and still appears.
    minute_bars = [a for a in required if a.artifact_kind == "time_series_bars" and a.resolution == "minute"]
    assert minute_bars == []
    assert len(non_sessions) == 3


def test_daily_artifact_has_null_trading_date():
    required, _ = expand_required_artifacts(_base_spec())
    daily = [a for a in required if a.artifact_kind == "time_series_bars" and a.resolution == "daily"]
    assert len(daily) == 1
    assert daily[0].trading_date is None
    assert daily[0].provider == "learn_ai_derived"


def test_quote_artifacts_use_learn_ai_derived_provider():
    required, _ = expand_required_artifacts(_base_spec(data_types=["trade", "quote"]))
    quote_artifacts = [a for a in required if a.artifact_kind == "time_series_bars" and a.data_type == "quote"]
    assert all(a.provider == "learn_ai_derived" for a in quote_artifacts)
    trade_artifacts = [
        a
        for a in required
        if a.artifact_kind == "time_series_bars" and a.data_type == "trade" and a.resolution == "minute"
    ]
    assert all(a.provider == "polygon" for a in trade_artifacts)


def test_factor_and_map_excluded_when_disabled():
    required, _ = expand_required_artifacts(_base_spec(include_factor_files=False, include_map_files=False))
    kinds = {a.artifact_kind for a in required}
    assert "factor_file" not in kinds
    assert "map_file" not in kinds
