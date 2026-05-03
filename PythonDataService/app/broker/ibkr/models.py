"""Pydantic v2 wire models for IBKR data.

Per ``docs/architecture/iv-ownership-research.md`` and the project
``numerical-rigor`` rules:

* All timestamps are ``int64`` ms since Unix epoch UTC. ib_async returns
  ``datetime`` objects; conversion to ms happens at this seam (the
  models module is the boundary where IBKR types become repo types).
* Greeks naming follows the existing engine convention: ``delta``,
  ``gamma``, ``theta`` (per-day, negative for long options), ``vega``
  (per-1-vol-point), and ``iv`` is annualised.
* IBKR can return ``-1`` or ``NaN`` as sentinel "no model" values for
  Greeks and IV. The wire model stores ``None`` in those cases — the
  conversion helper in this file does the translation.
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

OptionRight = Literal["C", "P"]


def _coerce_optional_float(value: float | None) -> float | None:
    """Treat IBKR's NaN / -1 sentinels as ``None``.

    ib_async surfaces "no model could compute this" as ``nan``, and a
    handful of legacy fields use ``-1.0``. We funnel both into ``None``
    so downstream consumers can rely on "value present ⇒ trustworthy
    number." The ``-1`` rule is conservative for Greeks (a real delta
    can be -1 for a deep ITM put — we never apply this rule to delta).
    """
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return float(value)


def _coerce_iv(value: float | None) -> float | None:
    """IV-specific coercion: ``-1.0`` is also a sentinel here."""
    out = _coerce_optional_float(value)
    if out is None:
        return None
    if out < 0.0:
        return None
    return out


class IbkrAccountSummary(BaseModel):
    """Snapshot of an IBKR account.

    The ``account_id`` is what the paper-vs-live sentinel runs against;
    paper account IDs begin with ``DU``.
    """

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    account_id: str
    is_paper: bool = Field(
        ...,
        description="True iff account_id starts with 'DU'.",
    )
    base_currency: str = "USD"
    cash_balance: float | None = None
    net_liquidation: float | None = None
    fetched_at_ms: int = Field(..., description="UTC milliseconds since epoch.")


class IbkrOptionQuote(BaseModel):
    """One option contract's tick snapshot.

    Greeks are sourced from IBKR's ``modelGreeks`` field by default; if
    the ``modelGreeks`` block is missing the producer falls back to
    ``bidGreeks`` / ``askGreeks`` and records that in ``greeks_source``.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    expiry_ms: int
    strike: float
    right: OptionRight

    # Quote
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    bid_size: int | None = None
    ask_size: int | None = None

    # IBKR-computed analytics. May be None when IBKR's model can't compute.
    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    underlying_price: float | None = None
    greeks_source: Literal["model", "bid", "ask", "last", "none"] = "none"

    # Stamp of when this snapshot was assembled. Sourced from
    # ``Ticker.time`` if present, else process clock at conversion time.
    ts_ms: int


class IbkrChainSnapshot(BaseModel):
    """A point-in-time slice of one expiry's chain.

    Emitted by the option-chain stream once per debounce window (default
    a few hundred ms). Consumers diff successive snapshots to render an
    animated table.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    expiry_ms: int
    underlying_price: float | None = None
    quotes: list[IbkrOptionQuote]
    as_of_ms: int


class IbkrConnectionHealth(BaseModel):
    """Diagnostic snapshot used by ``GET /api/broker/health``.

    The router never raises on disconnect; it returns this with
    ``connected=False`` so the UI can render the disconnected state and
    surface a reconnect button.
    """

    model_config = ConfigDict(frozen=True)

    mode: Literal["paper", "live"]
    host: str
    port: int
    client_id: int
    connected: bool
    account_id: str | None = None
    is_paper: bool | None = None
    server_version: int | None = None
    fetched_at_ms: int


__all__ = [
    "IbkrAccountSummary",
    "IbkrChainSnapshot",
    "IbkrConnectionHealth",
    "IbkrOptionQuote",
    "OptionRight",
    "_coerce_iv",
    "_coerce_optional_float",
]
