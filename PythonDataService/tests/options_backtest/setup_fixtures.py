"""Builders and database fixtures shared by the setup description, saved-setup and route tests (#3030).

``conn`` and ``unique`` are the research suites' fixtures: they run only
against a database attested as disposable (``POSTGRES_URL_IS_EPHEMERAL=1``)
and skip without one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import pytest

from app.options_backtest.setup import MS_PER_MINUTE, OptionsBacktestSetup
from tests.research.conftest import conn as conn
from tests.research.conftest import unique as unique


def leg(**changes: Any) -> dict[str, Any]:
    """The JSON of one leg: a plain at-the-money call, expiring this week, with no rules of its own."""
    return {
        "leg_id": "call",
        "right": "call",
        "contracts": 1,
        "expiry": {"kind": "this_week"},
        "strike": {"kind": "atm"},
        **changes,
    }


def setup_body(**changes: Any) -> dict[str, Any]:
    """The JSON of a setup as the page sends it: one plain leg, in at 5 minutes after the open, out 46 before the close."""
    return {
        "underlying": "SPY",
        "entry": {"anchor": "open", "offset_ms": 5 * MS_PER_MINUTE},
        "exit": {"anchor": "close", "offset_ms": 46 * MS_PER_MINUTE},
        "square_off": "complete",
        "legs": [leg()],
        "fees": {"kind": "not_charged"},
        "starting_cash_usd": "10000",
        **changes,
    }


def setup(**changes: Any) -> OptionsBacktestSetup:
    return OptionsBacktestSetup.model_validate(setup_body(**changes))


@pytest.fixture
async def tag(conn: asyncpg.Connection, unique: str) -> AsyncIterator[str]:
    """A per-test tag every saved name ends with; the rows it names are removed afterwards."""
    yield unique
    await conn.execute("DELETE FROM research_options_backtest_setups WHERE name LIKE $1", f"%{unique}")
