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
from pydantic import Field, ValidationError

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

    def test_rejects_zero_multiplier(self) -> None:
        with pytest.raises(ValidationError):
            _BarRange(from_date="2025-01-01", to_date="2025-01-31", multiplier=0)

    def test_rejects_negative_multiplier(self) -> None:
        with pytest.raises(ValidationError):
            _BarRange(from_date="2025-01-01", to_date="2025-01-31", multiplier=-1)

    @pytest.mark.parametrize("ts", ["minute", "hour", "day"])
    def test_accepts_supported_timespans(self, ts: str) -> None:
        r = _BarRange(from_date="2025-01-01", to_date="2025-01-31", timespan=ts)  # type: ignore[arg-type]
        assert r.timespan == ts

    @pytest.mark.parametrize("ts", ["weekly", "month", "minute_5", ""])
    def test_rejects_unknown_timespan(self, ts: str) -> None:
        with pytest.raises(ValidationError):
            _BarRange(from_date="2025-01-01", to_date="2025-01-31", timespan=ts)  # type: ignore[arg-type]

    def test_accepts_legacy_start_end_date_aliases(self) -> None:
        # Transitional — to be removed in PR (iii)
        r = _BarRange.model_validate({
            "start_date": "2025-01-01",
            "end_date": "2025-01-31",
        })
        assert r.from_date == "2025-01-01"
        assert r.to_date == "2025-01-31"

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
    def test_accepts_canonical_symbol_field(self) -> None:
        r = TickerRequest(symbol="SPY", from_date="2025-01-01", to_date="2025-01-31")
        assert r.symbol == "SPY"

    def test_accepts_legacy_ticker_alias(self) -> None:
        r = TickerRequest.model_validate({
            "ticker": "SPY",
            "from_date": "2025-01-01",
            "to_date": "2025-01-31",
        })
        assert r.symbol == "SPY"

    def test_accepts_all_legacy_aliases_combined(self) -> None:
        r = TickerRequest.model_validate({
            "ticker": "SPY",
            "start_date": "2025-01-01",
            "end_date": "2025-01-31",
        })
        assert r.symbol == "SPY"
        assert r.from_date == "2025-01-01"
        assert r.to_date == "2025-01-31"

    def test_rejects_empty_symbol(self) -> None:
        with pytest.raises(ValidationError):
            TickerRequest(symbol="", from_date="2025-01-01", to_date="2025-01-31")

    def test_rejects_oversized_symbol(self) -> None:
        with pytest.raises(ValidationError):
            TickerRequest(
                symbol="X" * 21,
                from_date="2025-01-01",
                to_date="2025-01-31",
            )

    def test_serializes_to_canonical_field_names(self) -> None:
        r = TickerRequest(symbol="SPY", from_date="2025-01-01", to_date="2025-01-31")
        d = r.model_dump()
        assert "symbol" in d and "ticker" not in d
        assert "from_date" in d and "start_date" not in d
        assert "to_date" in d and "end_date" not in d

    def test_extra_field_is_forbidden(self) -> None:
        with pytest.raises(ValidationError) as exc:
            TickerRequest.model_validate({
                "symbol": "SPY",
                "from_date": "2025-01-01",
                "to_date": "2025-01-31",
                "rogue_field": "value",
            })
        assert "rogue_field" in str(exc.value).lower()


class TestMultiTickerRequest:
    def test_accepts_canonical_symbols_field(self) -> None:
        r = MultiTickerRequest(
            symbols=["SPY", "QQQ"],
            from_date="2025-01-01",
            to_date="2025-01-31",
        )
        assert r.symbols == ["SPY", "QQQ"]

    def test_accepts_legacy_tickers_alias(self) -> None:
        r = MultiTickerRequest.model_validate({
            "tickers": ["SPY", "QQQ"],
            "from_date": "2025-01-01",
            "to_date": "2025-01-31",
        })
        assert r.symbols == ["SPY", "QQQ"]

    def test_rejects_empty_symbols_list(self) -> None:
        with pytest.raises(ValidationError):
            MultiTickerRequest(
                symbols=[],
                from_date="2025-01-01",
                to_date="2025-01-31",
            )

    def test_extra_field_is_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            MultiTickerRequest.model_validate({
                "symbols": ["SPY"],
                "from_date": "2025-01-01",
                "to_date": "2025-01-31",
                "extra": True,
            })

    def test_rejects_empty_string_symbol_in_list(self) -> None:
        # Per-element min_length=1 must reject [""] — without the
        # constraint the empty string would slip through to the runners.
        with pytest.raises(ValidationError) as exc:
            MultiTickerRequest(
                symbols=["SPY", ""],
                from_date="2025-01-01",
                to_date="2025-01-31",
            )
        assert "symbols" in str(exc.value).lower()

    def test_rejects_oversized_symbol_in_list(self) -> None:
        with pytest.raises(ValidationError):
            MultiTickerRequest(
                symbols=["SPY", "X" * 21],
                from_date="2025-01-01",
                to_date="2025-01-31",
            )


class TestInheritance:
    """Smoke test — confirm subclasses can override defaults explicitly
    (per-route default preservation pattern from the spec)."""

    def test_subclass_can_override_multiplier_default(self) -> None:
        class FifteenMinuteRequest(TickerRequest):
            multiplier: int = Field(15, ge=1)

        r = FifteenMinuteRequest(
            symbol="SPY",
            from_date="2025-01-01",
            to_date="2025-01-31",
        )
        assert r.multiplier == 15

    def test_subclass_can_override_session_default(self) -> None:
        class ExtendedSessionRequest(TickerRequest):
            session: str = Field("extended")  # type: ignore[assignment]

        r = ExtendedSessionRequest(
            symbol="SPY",
            from_date="2025-01-01",
            to_date="2025-01-31",
        )
        assert r.session == "extended"
