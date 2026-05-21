"""Trading-session calendar.

Slice 1a uses a hardcoded US-equity holiday list good enough for the EMA-crossover
smoke test window. Slice 1c replaces this with a parser of the staged LEAN
market-hours-database.json, with the same public signature.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 4.5
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

from app.data_lake.types import NonSessionRecord

# Hardcoded US-equity full-day market holidays for the Slice 1a smoke window.
# Source: NYSE official calendar. Slice 1c replaces this with the LEAN
# market-hours-database to get unlimited range + early-close metadata.
_USA_FULL_HOLIDAYS: frozenset[date] = frozenset(
    {
        # 2024
        date(2024, 1, 1),  # New Year's Day
        date(2024, 1, 15),  # MLK Day
        date(2024, 2, 19),  # Presidents Day
        date(2024, 3, 29),  # Good Friday
        date(2024, 5, 27),  # Memorial Day
        date(2024, 6, 19),  # Juneteenth
        date(2024, 7, 4),  # Independence Day
        date(2024, 9, 2),  # Labor Day
        date(2024, 11, 28),  # Thanksgiving
        date(2024, 12, 25),  # Christmas
        # 2025
        date(2025, 1, 1),
        date(2025, 1, 20),  # MLK Day
        date(2025, 2, 17),  # Presidents Day
        date(2025, 4, 18),  # Good Friday
        date(2025, 5, 26),  # Memorial Day
        date(2025, 6, 19),
        date(2025, 7, 4),
        date(2025, 9, 1),
        date(2025, 11, 27),
        date(2025, 12, 25),
        # 2026
        date(2026, 1, 1),
        date(2026, 1, 19),
        date(2026, 2, 16),
        date(2026, 4, 3),
        date(2026, 5, 25),
        date(2026, 6, 19),
        date(2026, 7, 3),  # observed
        date(2026, 9, 7),
        date(2026, 11, 26),
        date(2026, 12, 25),
    }
)


def _parse_holiday_date(raw: str) -> date:
    """Parse a LEAN holiday date string.

    LEAN's canonical format is M/d/yyyy (e.g. '1/1/2024', '12/25/2024').
    ISO format YYYY-MM-DD is also accepted for test fixtures and forward
    compatibility.
    """
    if "-" in raw and raw.index("-") == 4:
        # ISO format: YYYY-MM-DD
        return datetime.strptime(raw, "%Y-%m-%d").date()
    # LEAN native format: M/d/yyyy
    return datetime.strptime(raw, "%m/%d/%Y").date()


def _parse_market_hours_holidays(mh_db_path: Path) -> frozenset[date]:
    """Read the LEAN market-hours JSON and return USA-equity full-day holidays.

    LEAN's JSON shape (simplified):
      {
        "entries": {
          "Equity-usa-[*]": {
            "holidays": ["1/1/2024", ...],
            "earlyCloses": {"7/3/2024": "13:00:00", ...},
            ...
          }
        }
      }

    LEAN encodes holiday strings in M/d/yyyy format. Early closes are NOT
    holidays — they remain trading sessions in v1.
    """
    payload = json.loads(mh_db_path.read_text())
    entry = (payload.get("entries") or {}).get("Equity-usa-[*]", {})
    holidays = entry.get("holidays") or []
    out: set[date] = set()
    for h in holidays:
        out.add(_parse_holiday_date(h))
    return frozenset(out)


def trading_sessions_for(
    market: str,
    start_trading_date: date,
    end_trading_date: date,
    market_hours_db_path: Path | None = None,
) -> tuple[list[date], list[NonSessionRecord]]:
    """Return (sessions, non_sessions) for the inclusive window.

    Half-day early closes ARE sessions in v1 (full-minute coverage for the
    truncated window); only full closures map to non-sessions.

    When market_hours_db_path is provided and the file exists, holidays are
    read from the staged LEAN market-hours JSON. Otherwise falls back to the
    hardcoded _USA_FULL_HOLIDAYS list.
    """
    if market != "usa":
        raise ValueError(f"market {market!r} not supported in Slice 1c")

    holidays = (
        _parse_market_hours_holidays(market_hours_db_path)
        if market_hours_db_path is not None and market_hours_db_path.is_file()
        else _USA_FULL_HOLIDAYS
    )

    sessions: list[date] = []
    non_sessions: list[NonSessionRecord] = []
    current = start_trading_date
    while current <= end_trading_date:
        if current.weekday() >= 5:
            non_sessions.append(NonSessionRecord(market=market, trading_date=current, reason="weekend"))
        elif current in holidays:
            non_sessions.append(NonSessionRecord(market=market, trading_date=current, reason="market_holiday"))
        else:
            sessions.append(current)
        current += timedelta(days=1)
    return sessions, non_sessions
