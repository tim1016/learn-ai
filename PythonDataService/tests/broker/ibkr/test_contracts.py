"""Tests for app.broker.ibkr.contracts — boundary conversions only.

Network-touching helpers (qualify_underlying, list_strikes, etc.) are
out of scope for unit tests; they're covered by the integration suite
that runs against a live Gateway in dev.
"""

from __future__ import annotations

from app.broker.ibkr.contracts import (
    expiry_ms_to_yyyymmdd,
    yyyymmdd_to_expiry_ms,
)


def test_expiry_round_trips() -> None:
    yyyymmdd = "20260619"
    ms = yyyymmdd_to_expiry_ms(yyyymmdd)
    assert expiry_ms_to_yyyymmdd(ms) == yyyymmdd


def test_expiry_ms_at_midnight_utc_renders_correct_day() -> None:
    """Build the timestamp from the canonical date so the test does not
    depend on a hand-computed unix-epoch literal."""
    from datetime import UTC, datetime

    ms = int(datetime(2026, 5, 15, tzinfo=UTC).timestamp() * 1000)
    assert expiry_ms_to_yyyymmdd(ms) == "20260515"


def test_expiry_just_before_midnight_renders_previous_day_in_utc() -> None:
    """A timestamp at 23:59 UTC on day N renders as N — confirms we use
    the UTC date, not local."""
    from datetime import UTC, datetime

    ms = int(datetime(2026, 5, 15, 23, 59, 59, tzinfo=UTC).timestamp() * 1000)
    assert expiry_ms_to_yyyymmdd(ms) == "20260515"
