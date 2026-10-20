"""Tests for ensure_data.expand_required_artifacts (Slice 1a).

Spec-update note: include_lean_metadata has been removed from DataRunSpec;
LEAN metadata is an unconditional Phase 0 prerequisite staged separately
(Slice 1c). expand_required_artifacts never emits metadata artifacts.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from app.data_lake.ensure_data import expand_required_artifacts
from app.data_lake.types import DataRunSpec, trading_date_to_calendar_anchor_ms


def _base_spec(**overrides) -> DataRunSpec:
    payload = {
        "request_id": UUID("12345678-1234-5678-1234-567812345678"),
        "run_type": "python_lab",
        "symbols": ["SPY"],
        "start_trading_date_ms": trading_date_to_calendar_anchor_ms(date(2024, 5, 20)),
        "end_trading_date_ms": trading_date_to_calendar_anchor_ms(date(2024, 5, 24)),
        "lean_image_digest": "sha256:test",
    }
    payload.update(overrides)
    return DataRunSpec(**payload)


def test_single_symbol_one_week_trade_only():
    required, non_sessions = expand_required_artifacts(_base_spec())
    # 5 trading days x 1 minute-trade + 1 factor + 1 map + 1 daily-trade = 8
    kinds = [a.artifact_kind for a in required]
    assert kinds.count("time_series_bars") == 6, "5 minute + 1 daily time_series_bars artifacts"
    assert kinds.count("factor_file") == 1, "one factor file"
    assert kinds.count("map_file") == 1, "one map file"
    assert "metadata" not in kinds, "no metadata artifacts are emitted"
    assert non_sessions == [], "a full trading week has no non-sessions"
    # The daily artifact is per-symbol (not per-session), so it carries no trading date.
    daily = [a for a in required if a.artifact_kind == "time_series_bars" and a.resolution == "daily"]
    assert len(daily) == 1, "exactly one daily artifact"
    assert daily[0].trading_date is None, "daily artifact has a null trading date"
    assert daily[0].provider == "learn_ai_derived", "daily artifact uses the learn_ai_derived provider"


def test_quote_inclusion_doubles_minute_artifacts_with_per_kind_providers():
    required, _ = expand_required_artifacts(_base_spec(data_types=["trade", "quote"]))
    minute_artifacts = [a for a in required if a.artifact_kind == "time_series_bars" and a.resolution == "minute"]
    assert len(minute_artifacts) == 10, "5 trade + 5 quote minute artifacts"
    quote_artifacts = [a for a in required if a.artifact_kind == "time_series_bars" and a.data_type == "quote"]
    assert all(a.provider == "learn_ai_derived" for a in quote_artifacts), "quote artifacts use the learn_ai_derived provider"
    trade_artifacts = [
        a
        for a in required
        if a.artifact_kind == "time_series_bars" and a.data_type == "trade" and a.resolution == "minute"
    ]
    assert all(a.provider == "polygon" for a in trade_artifacts), "minute trade artifacts use the polygon provider"


def test_holiday_week_produces_non_sessions():
    spec = _base_spec(
        start_trading_date_ms=trading_date_to_calendar_anchor_ms(date(2024, 5, 25)),  # Sat
        end_trading_date_ms=trading_date_to_calendar_anchor_ms(date(2024, 5, 27)),  # Memorial Day Mon
    )
    required, non_sessions = expand_required_artifacts(spec)
    # No minute-bar artifacts when there are no trading sessions.
    # The daily derived artifact is per-symbol (not per-session) and still appears.
    minute_bars = [a for a in required if a.artifact_kind == "time_series_bars" and a.resolution == "minute"]
    assert minute_bars == []
    assert len(non_sessions) == 3


def test_factor_and_map_excluded_when_disabled():
    required, _ = expand_required_artifacts(_base_spec(include_factor_files=False, include_map_files=False))
    kinds = {a.artifact_kind for a in required}
    assert "factor_file" not in kinds
    assert "map_file" not in kinds


def test_daily_trade_excluded_when_disabled():
    required, _ = expand_required_artifacts(_base_spec(include_daily_trade=False))
    daily = [a for a in required if a.artifact_kind == "time_series_bars" and a.resolution == "daily"]
    assert daily == []
    # Minute-trade artifacts are untouched by the flag — only the
    # whole-range rollup is opted out.
    minute_bars = [a for a in required if a.artifact_kind == "time_series_bars" and a.resolution == "minute"]
    assert len(minute_bars) == 5
