"""Slice 1F (issue #605) — broker-coupled leg picker DTOs.

`SymbolMatch` is the wire shape for ``/api/broker/symbols/search`` (one
matching contract per row from IBKR ``reqMatchingSymbols``).
`OptionContractMatch` is the wire shape for
``/api/broker/option-contracts/{symbol}`` (one qualified option per row
from IBKR ``reqContractDetails``).

Both DTOs are response-only; they never travel back over the wire as
input, so the strict-schema invariants here protect the cockpit from
silently consuming a malformed broker payload.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.broker_search import OptionContractMatch, SymbolMatch


def test_symbol_match_round_trips() -> None:
    raw = {
        "symbol": "SPY",
        "name": "SPDR S&P 500 ETF Trust",
        "exchange": "ARCA",
        "currency": "USD",
        "sec_type": "STK",
        "derivative_sec_types": ["OPT", "FOP"],
    }

    parsed = SymbolMatch.model_validate(raw)

    assert parsed.model_dump() == raw


def test_symbol_match_rejects_unknown_keys() -> None:
    """Strict schema — IBKR payload drift should surface, not silently round-trip."""

    raw = {
        "symbol": "SPY",
        "name": "SPDR S&P 500 ETF Trust",
        "exchange": "ARCA",
        "currency": "USD",
        "sec_type": "STK",
        "derivative_sec_types": [],
        "marketName": "SPDR",  # unexpected camelCase key
    }
    with pytest.raises(ValidationError, match=r"marketName"):
        SymbolMatch.model_validate(raw)


def test_symbol_match_blank_symbol_rejected() -> None:
    raw = {
        "symbol": "",
        "name": "x",
        "exchange": "ARCA",
        "currency": "USD",
        "sec_type": "STK",
        "derivative_sec_types": [],
    }
    with pytest.raises(ValidationError, match=r"symbol"):
        SymbolMatch.model_validate(raw)


def test_option_contract_match_round_trips() -> None:
    raw = {
        "con_id": 123456789,
        "symbol": "SPY",
        "local_symbol": "SPY   251219C00650000",
        "trading_class": "SPY",
        "exchange": "SMART",
        "currency": "USD",
        "expiry_ms": 1_766_188_800_000,
        "strike": 650.0,
        "right": "C",
        "multiplier": 100,
    }

    parsed = OptionContractMatch.model_validate(raw)

    assert parsed.model_dump() == raw


def test_option_contract_match_rejects_invalid_right() -> None:
    raw = {
        "con_id": 1,
        "symbol": "SPY",
        "local_symbol": "x",
        "trading_class": "SPY",
        "exchange": "SMART",
        "currency": "USD",
        "expiry_ms": 1,
        "strike": 100.0,
        "right": "X",  # only C or P
        "multiplier": 100,
    }
    with pytest.raises(ValidationError, match=r"right"):
        OptionContractMatch.model_validate(raw)


def test_option_contract_match_rejects_non_positive_strike() -> None:
    raw = {
        "con_id": 1,
        "symbol": "SPY",
        "local_symbol": "x",
        "trading_class": "SPY",
        "exchange": "SMART",
        "currency": "USD",
        "expiry_ms": 1,
        "strike": 0.0,
        "right": "C",
        "multiplier": 100,
    }
    with pytest.raises(ValidationError, match=r"strike"):
        OptionContractMatch.model_validate(raw)


def test_option_contract_match_rejects_non_positive_con_id() -> None:
    """`conId` is the broker's canonical contract identity — zero / negative
    would mean the contract didn't qualify."""

    raw = {
        "con_id": 0,
        "symbol": "SPY",
        "local_symbol": "x",
        "trading_class": "SPY",
        "exchange": "SMART",
        "currency": "USD",
        "expiry_ms": 1,
        "strike": 100.0,
        "right": "C",
        "multiplier": 100,
    }
    with pytest.raises(ValidationError, match=r"con_id"):
        OptionContractMatch.model_validate(raw)
