"""Slice 1F (issue #605) — broker-coupled leg picker DTOs.

`OptionContractMatch` is the wire shape for
``/api/broker/option-contracts/{symbol}`` (one qualified option per row
from IBKR ``reqContractDetails``). The `SymbolMatch` cases retired with
``/api/broker/symbols/search`` (PR-B of #1813, 2026-08-27).

The DTO is response-only; it never travels back over the wire as input,
so the strict-schema invariants here protect the cockpit from silently
consuming a malformed broker payload.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.broker_search import OptionContractMatch


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
