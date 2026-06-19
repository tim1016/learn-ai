"""Broker search response DTOs — Slice 1F (issue #605).

`SymbolMatch` wraps one ``ContractDescription`` from
``IB.reqMatchingSymbolsAsync(pattern)``. `OptionContractMatch` wraps one
qualified option from ``IB.reqContractDetailsAsync(Option(...))``.

Both are response-only — the cockpit consumes them, never echoes them
back. Strict (``extra='forbid'``) so an IBKR payload drift surfaces as a
422 response on the proxy boundary rather than silently round-tripping a
mystery field into the picker dropdown.

Wire-format note: ``expiry_ms`` is ``int64`` ms UTC per the repo's
timestamp policy. IBKR ``Contract.lastTradeDateOrContractMonth`` is
``YYYYMMDD``; the conversion happens in the IBKR wrapper, not here.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SymbolMatch(BaseModel):
    """One row from ``/api/broker/symbols/search``."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1)
    name: str
    exchange: str
    currency: str
    sec_type: Literal["STK", "OPT", "FUT", "FOP", "IND", "CASH", "BOND", "CFD", "CMDTY"]
    derivative_sec_types: list[str] = Field(default_factory=list)


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
