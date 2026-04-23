"""Tests for app.research.divergence.preflight."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.research.divergence.preflight import (
    IndicatorRequest,
    PreflightRequest,
    run_preflight,
)


def _req(
    *,
    timeframe: str = "15m",
    indicators: list[IndicatorRequest] | None = None,
    session_filter: str = "rth_only",
    warmup_days: int = 100,
    dividend_adjustment: bool = False,
) -> PreflightRequest:
    return PreflightRequest(
        strategy_name="unit_test",
        symbol="SPY",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 2, 1),
        timeframe=timeframe,
        indicators=indicators if indicators is not None else [IndicatorRequest(name="ema", length=20)],
        session_filter=session_filter,  # type: ignore[arg-type]
        warmup_days=warmup_days,
        dividend_adjustment=dividend_adjustment,
    )


def test_run_preflight_all_ok_when_fully_compliant(tmp_path: Path):
    # Seed the cache dir so the data-availability checks pass.
    tf_dir = tmp_path / "15m"
    (tf_dir / "polygon").mkdir(parents=True)
    (tf_dir / "polygon" / "spy_15m.parquet").write_bytes(b"")
    (tf_dir / "tv").mkdir(parents=True)
    (tf_dir / "tv" / "spy_15m.parquet").write_bytes(b"")

    result = run_preflight(_req(), cache_root=tmp_path)

    assert result.overall == "ok"
    assert all(c.status == "ok" for c in result.checks)


def test_run_preflight_unspecified_session_yields_warning(tmp_path: Path):
    result = run_preflight(_req(session_filter="unspecified"), cache_root=tmp_path)

    assert result.overall in {"warning", "blocking"}
    session_check = next(c for c in result.checks if c.id == "session_filter")
    assert session_check.status == "warning"


def test_run_preflight_full_session_is_blocking(tmp_path: Path):
    result = run_preflight(_req(session_filter="full_session"), cache_root=tmp_path)

    assert result.overall == "blocking"
    session_check = next(c for c in result.checks if c.id == "session_filter")
    assert session_check.status == "blocking"
    assert session_check.docs_link is not None


def test_run_preflight_short_warmup_is_warning(tmp_path: Path):
    # EMA(50) on 15m needs ~8 days; 5 days is "warmup < needed_days and >= needed_days//2"
    result = run_preflight(
        _req(indicators=[IndicatorRequest(name="ema", length=50)], warmup_days=5),
        cache_root=tmp_path,
    )

    warmup_check = next(c for c in result.checks if c.id == "warmup")
    assert warmup_check.status in {"warning", "blocking"}


def test_run_preflight_no_warmup_is_blocking_for_long_ema(tmp_path: Path):
    result = run_preflight(
        _req(indicators=[IndicatorRequest(name="ema", length=200)], warmup_days=0),
        cache_root=tmp_path,
    )

    warmup_check = next(c for c in result.checks if c.id == "warmup")
    assert warmup_check.status == "blocking"
    assert result.overall == "blocking"


def test_run_preflight_non_canonical_indicator_period_warns(tmp_path: Path):
    result = run_preflight(
        _req(indicators=[IndicatorRequest(name="ema", length=37)], warmup_days=200),
        cache_root=tmp_path,
    )

    indicator_check = next(c for c in result.checks if c.id == "indicator_params")
    assert indicator_check.status == "warning"
    assert "ema(37)" in indicator_check.message.lower() or "EMA(37)" in indicator_check.message


def test_run_preflight_dividend_adjustment_true_warns(tmp_path: Path):
    result = run_preflight(
        _req(dividend_adjustment=True), cache_root=tmp_path,
    )

    div_check = next(c for c in result.checks if c.id == "dividend_adjustment")
    assert div_check.status == "warning"


def test_run_preflight_summary_counts_statuses(tmp_path: Path):
    # Full session → 1 blocking; unspecified cache → 2 warnings (polygon, tv).
    result = run_preflight(_req(session_filter="full_session"), cache_root=tmp_path)

    assert "1 blocking" in result.summary
    assert "blocking issue" in result.summary


@pytest.mark.parametrize(
    "indicator,length,expected_ok",
    [
        ("ema", 20, True),
        ("sma", 20, True),
        ("rsi", 14, True),
        ("macd", 12, True),
        ("not_a_real_indicator", 14, False),
    ],
)
def test_run_preflight_unknown_indicator_name_warns(
    indicator: str, length: int, expected_ok: bool, tmp_path: Path
):
    result = run_preflight(
        _req(indicators=[IndicatorRequest(name=indicator, length=length)], warmup_days=200),
        cache_root=tmp_path,
    )

    indicator_check = next(c for c in result.checks if c.id == "indicator_params")
    if expected_ok:
        assert indicator_check.status == "ok"
    else:
        assert indicator_check.status == "warning"
        assert "unknown indicator" in indicator_check.message
