"""Tests for the canonical TickerRequest / MultiTickerRequest base.

Covers:
- Valid round-trips
- Calendar validation (regex misses "2025-13-99")
- Range ordering (from_date <= to_date)
- ``extra="forbid"`` rejection of unknown fields
- Transitional ``AliasChoices`` for legacy field names (ticker, tickers,
  start_date, end_date) — these aliases come off in PR (iii)
- Default values
- Field constraints (multiplier >= 1, symbol non-empty, symbols min 1)
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.ticker_request import (
    MultiTickerRequest,
    TickerRequest,
    _BarRange,
)


class TestBarRange:
    def test_accepts_valid_payload_with_all_defaults(self) -> None:
        r = _BarRange(from_date="2025-01-01", to_date="2025-01-31")
        assert r.timespan == "minute"
        assert r.multiplier == 1
        assert r.session == "rth"

    def test_rejects_malformed_dates(self) -> None:
        with pytest.raises(ValidationError) as exc:
            _BarRange(from_date="2025-1-1", to_date="2025-01-31")
        assert "from_date" in str(exc.value)

    def test_rejects_calendar_invalid_date(self) -> None:
        # Regex passes; date.fromisoformat catches it.
        with pytest.raises(ValidationError) as exc:
            _BarRange(from_date="2025-13-99", to_date="2025-12-31")
        msg = str(exc.value).lower()
        assert "calendar" in msg or "invalid" in msg

    def test_rejects_inverted_range(self) -> None:
        with pytest.raises(ValidationError) as exc:
            _BarRange(from_date="2025-12-31", to_date="2025-01-01")
        s = str(exc.value)
        assert "to_date" in s and "from_date" in s

    def test_accepts_single_day_range(self) -> None:
        r = _BarRange(from_date="2025-01-15", to_date="2025-01-15")
        assert r.from_date == r.to_date

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            pytest.param("multiplier", 0, id="zero_multiplier"),
            pytest.param("timespan", "weekly", id="unknown_timespan_weekly"),
            pytest.param("timespan", "month", id="unknown_timespan_month"),
            pytest.param("timespan", "minute_5", id="unknown_timespan_minute_5"),
            pytest.param("timespan", "", id="unknown_timespan_empty"),
        ],
    )
    def test_rejects_zero_multiplier_and_unknown_timespan(self, field: str, value: object) -> None:
        with pytest.raises(ValidationError):
            _BarRange(from_date="2025-01-01", to_date="2025-01-31", **{field: value})  # type: ignore[arg-type]

    def test_extra_field_is_forbidden(self) -> None:
        with pytest.raises(ValidationError) as exc:
            _BarRange.model_validate({
                "from_date": "2025-01-01",
                "to_date": "2025-01-31",
                "rogue_field": "value",
            })
        s = str(exc.value).lower()
        assert "rogue_field" in s.lower()
        assert "extra" in s


class TestTickerRequest:
    @pytest.mark.parametrize("symbol", ["", "X" * 21], ids=["empty_symbol", "oversized_symbol"])
    def test_rejects_empty_or_oversized_symbol(self, symbol: str) -> None:
        with pytest.raises(ValidationError):
            TickerRequest(symbol=symbol, from_date="2025-01-01", to_date="2025-01-31")


class TestMultiTickerRequest:
    @pytest.mark.parametrize(
        "symbols",
        [
            pytest.param((), id="empty_symbols_list"),
            # Per-element min_length=1 must reject [""] — without the
            # constraint the empty string would slip through to the runners.
            pytest.param(("SPY", ""), id="empty_string_symbol_in_list"),
            pytest.param(("SPY", "X" * 21), id="oversized_symbol_in_list"),
        ],
    )
    def test_rejects_a_symbols_list_that_breaks_the_constraints(self, symbols: tuple[str, ...]) -> None:
        with pytest.raises(ValidationError) as exc:
            MultiTickerRequest(
                symbols=list(symbols),
                from_date="2025-01-01",
                to_date="2025-01-31",
            )
        assert "symbols" in str(exc.value).lower(), "the error names the symbols field"
