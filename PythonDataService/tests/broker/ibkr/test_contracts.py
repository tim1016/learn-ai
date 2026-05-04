"""Tests for app.broker.ibkr.contracts — boundary conversions only.

Network-touching helpers (qualify_underlying, list_strikes, etc.) are
out of scope for unit tests; they're covered by the integration suite
that runs against a live Gateway in dev.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.broker.ibkr import contracts as contracts_module
from app.broker.ibkr.contracts import (
    build_chain_contracts,
    expiry_ms_to_yyyymmdd,
    list_qualified_strikes,
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


def _mock_client_with_qualify_result(qualified: list):
    async def qualify(*_contracts):
        return qualified

    return SimpleNamespace(
        require_connected=lambda: None,
        ib=SimpleNamespace(qualifyContractsAsync=qualify),
    )


async def test_build_chain_contracts_strips_none_placeholders() -> None:
    # Regression: ib_async's qualifyContractsAsync can return a
    # length-matching list with None entries for unqualifiable strikes.
    # Without filtering, market_data.py's length guard saw a "complete"
    # chain and crashed on ``None.strike`` while indexing tickers.
    qualified_with_gap = [
        SimpleNamespace(conId=1, strike=100.0, right="C"),
        SimpleNamespace(conId=2, strike=100.0, right="P"),
        None,
        SimpleNamespace(conId=4, strike=105.0, right="P"),
    ]
    client = _mock_client_with_qualify_result(qualified_with_gap)

    result = await build_chain_contracts(client, "SPY", 1_800_000_000_000, [100.0, 105.0])

    assert all(c is not None for c in result)
    assert len(result) == 3


async def test_build_chain_contracts_returns_full_list_when_all_qualify() -> None:
    qualified = [
        SimpleNamespace(conId=1, strike=100.0, right="C"),
        SimpleNamespace(conId=2, strike=100.0, right="P"),
        SimpleNamespace(conId=3, strike=105.0, right="C"),
        SimpleNamespace(conId=4, strike=105.0, right="P"),
    ]
    client = _mock_client_with_qualify_result(qualified)

    result = await build_chain_contracts(client, "SPY", 1_800_000_000_000, [100.0, 105.0])

    assert len(result) == 4
    assert [c.conId for c in result] == [1, 2, 3, 4]


def _mock_client_qualify_by_right(by_right: dict[str, list]):
    """Mock client whose qualifyContractsAsync inspects the first contract's
    `right` attribute and returns the matching pre-canned list."""

    async def qualify(*contracts):
        if not contracts:
            return []
        return by_right[contracts[0].right]

    return SimpleNamespace(
        require_connected=lambda: None,
        ib=SimpleNamespace(qualifyContractsAsync=qualify),
    )


async def test_list_qualified_strikes_requires_both_call_and_put_legs(monkeypatch) -> None:
    # Metadata claims four strikes; IBKR qualifies the call legs at 540/545
    # but the put leg at 545 cannot be qualified. Only 540 has both legs and
    # is therefore the only strike the chain stream can subscribe to without
    # tripping its partial-qualification guard.
    async def fake_list_strikes(_client, _symbol, _expiry_ms):
        return [540.0, 541.0, 542.0, 545.0]

    monkeypatch.setattr(contracts_module, "list_strikes", fake_list_strikes)

    client = _mock_client_qualify_by_right({
        "C": [
            SimpleNamespace(conId=1, strike=540.0, right="C"),
            None,
            None,
            SimpleNamespace(conId=4, strike=545.0, right="C"),
        ],
        "P": [
            SimpleNamespace(conId=11, strike=540.0, right="P"),
            None,
            None,
            None,  # 545 put fails to qualify → 545 must be dropped
        ],
    })

    result = await list_qualified_strikes(client, "SPY", 1_800_000_000_000)

    assert result == [540.0]


async def test_list_qualified_strikes_returns_intersection_when_both_legs_qualify(monkeypatch) -> None:
    async def fake_list_strikes(_client, _symbol, _expiry_ms):
        return [540.0, 545.0]

    monkeypatch.setattr(contracts_module, "list_strikes", fake_list_strikes)

    client = _mock_client_qualify_by_right({
        "C": [
            SimpleNamespace(conId=1, strike=540.0, right="C"),
            SimpleNamespace(conId=2, strike=545.0, right="C"),
        ],
        "P": [
            SimpleNamespace(conId=11, strike=540.0, right="P"),
            SimpleNamespace(conId=12, strike=545.0, right="P"),
        ],
    })

    result = await list_qualified_strikes(client, "SPY", 1_800_000_000_000)

    assert result == [540.0, 545.0]


async def test_list_qualified_strikes_returns_empty_when_metadata_empty(monkeypatch) -> None:
    async def fake_list_strikes(_client, _symbol, _expiry_ms):
        return []

    monkeypatch.setattr(contracts_module, "list_strikes", fake_list_strikes)
    client = _mock_client_with_qualify_result([])

    result = await list_qualified_strikes(client, "SPY", 1_800_000_000_000)

    assert result == []
