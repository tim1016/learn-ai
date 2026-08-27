"""Broker search response DTOs — Slice 1F (issue #605).

`OptionContractMatch` wraps one qualified option from
``IB.reqContractDetailsAsync(Option(...))``. Its sibling `SymbolMatch`
retired with ``/api/broker/symbols/search`` and
``app/broker/ibkr/symbol_search.py`` (PR-B of #1813, 2026-08-27).

Response-only — the cockpit consumes it, never echoes it back. Strict
(``extra='forbid'``) so an IBKR payload drift surfaces as a 422 response
on the proxy boundary rather than silently round-tripping a mystery
field into the picker dropdown.

Wire-format note: ``expiry_ms`` is ``int64`` ms UTC per the repo's
timestamp policy. IBKR ``Contract.lastTradeDateOrContractMonth`` is
``YYYYMMDD``; the conversion happens in the IBKR wrapper, not here.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class OptionContractMatch(BaseModel):
    """One row from ``/api/broker/option-contracts/{symbol}`` — a qualified
    option contract whose ``con_id`` is the broker-canonical identity the
    Slice 4 resolver will persist against the ``leg_id``."""

    model_config = ConfigDict(extra="forbid")

    con_id: int = Field(gt=0)
    symbol: str = Field(min_length=1)
    local_symbol: str = Field(min_length=1)
    trading_class: str = Field(min_length=1)
    exchange: str
    currency: str
    expiry_ms: int = Field(gt=0)
    strike: float = Field(gt=0)
    right: Literal["C", "P"]
    multiplier: int = Field(gt=0)
