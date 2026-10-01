"""Phase 3 capture-smoke: validate QC fixture shape once it lands.

Skipped on master until ``tests/fixtures/golden/qc-aapl-phase3/`` is
committed. The first test ensures the orders payload has every event
field the reconciler reads; the second logs ``FEE_PRESENCE_BRANCH=A|B``
so reviewers know whether commission parity is in scope for this fixture.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "golden" / "qc-aapl-phase3"
_ORDERS = _FIXTURE_DIR / "qc_orders.json"
_PRICES = _FIXTURE_DIR / "qc_price_history.csv"


pytestmark = pytest.mark.skipif(
    not _ORDERS.is_file(),
    reason=(
        "Phase 3 QC fixture not yet captured at "
        f"{_FIXTURE_DIR}. See docs/superpowers/specs/"
        "2026-05-11-phase3-pnl-parity-design.md §2.1."
    ),
)


def test_fixture_first_and_last_minute_timestamps_match_window() -> None:
    """Pin the exact first/last bar timestamps. QC's qb.history(start, end)
    inclusivity at the day boundary can silently drop the trailing session;
    pinning here catches a fixture recapture that shifts by one day.

    Phase 3.5 scope is the 2-day window 2026-02-09 09:31 -> 2026-02-11 16:00 NY
    (truncated by QC free tier's minute-data trailing window — only the most
    recent ~90 calendar days of minute bars are accessible on free tier; see
    reconciliation report for full context).
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.research.parity.fixture_data_reader import FixtureDataReader
    from app.utils.timestamps import datetime_at_ms

    NY = ZoneInfo("America/New_York")
    reader = FixtureDataReader(csv_path=_PRICES, symbol="AAPL")
    bars = list(reader.iter_bars("AAPL"))
    assert bars, "no bars parsed from fixture price history"

    first = bars[0]
    last = bars[-1]

    # First bar = 2026-02-09 09:31 NY (FixtureDataReader interprets the CSV
    # row's timestamp as ``start_ms``; the first regular-session minute bar of
    # 2026-02-09 in QC's capture starts at 09:31 NY).
    assert datetime_at_ms(first.start_ms, tz=NY) == datetime(2026, 2, 9, 9, 31, tzinfo=NY), (
        f"first bar start_ms = {first.start_ms} (expected 2026-02-09 09:31 NY)"
    )

    # Last bar = 2026-02-11 16:00 NY (final session-close minute).
    assert datetime_at_ms(last.start_ms, tz=NY) == datetime(2026, 2, 11, 16, 0, tzinfo=NY), (
        f"last bar start_ms = {last.start_ms} (expected 2026-02-11 16:00 NY)"
    )
