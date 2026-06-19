"""IBKR symbol search — Slice 1F (issue #605).

Wraps ``IB.reqMatchingSymbolsAsync(pattern)`` and maps each
``ContractDescription`` onto the repo-native ``SymbolMatch`` DTO.

The boundary work is two pieces:

* Drop rows whose ``secType`` is outside the
  ``SymbolMatch.sec_type`` allowlist. IBKR can emit values our wire
  schema does not model (``WAR``, ``BILL``, exotic CFDs); the picker
  is better off surfacing the rest than crashing the response on a
  ``ValidationError``.
* Optional filter by ``sec_type`` so the cockpit can request only
  stocks (``STK``) when the operator is declaring a stock leg's
  underlying — the picker dropdown should not include futures
  symbols when a stock-only seat is open.

Rate-limit + cache discipline live in the router (Slice 1F §4).
"""

from __future__ import annotations

import logging
from typing import get_args

from app.broker.ibkr.client import IbkrClient
from app.schemas.broker_search import SymbolMatch

logger = logging.getLogger(__name__)

_ALLOWED_SEC_TYPES: frozenset[str] = frozenset(
    get_args(SymbolMatch.model_fields["sec_type"].annotation)
)


async def search_symbols(
    client: IbkrClient,
    pattern: str,
    *,
    sec_type: str | None = None,
) -> list[SymbolMatch]:
    """Search IBKR's symbol catalog for ``pattern``.

    ``pattern`` is whatever the operator typed (typically 1–5 chars).
    Empty pattern short-circuits to ``[]`` — IBKR would otherwise
    return an "invalid request" error and the cockpit just wants an
    empty dropdown.

    ``sec_type`` (optional) further narrows the result. ``None`` (the
    default) returns all rows; passing ``"STK"`` keeps only stock
    matches, etc.

    Caller is responsible for connection state; this raises
    ``NotConnectedError`` if IBKR is offline.
    """
    pattern = pattern.strip()
    if not pattern:
        return []

    client.require_connected()
    raw = await client.ib.reqMatchingSymbolsAsync(pattern)

    out: list[SymbolMatch] = []
    for desc in raw:
        ibkr_sec_type = getattr(desc.contract, "secType", "")
        if ibkr_sec_type not in _ALLOWED_SEC_TYPES:
            logger.debug(
                "search_symbols dropping unsupported secType %r for symbol %r",
                ibkr_sec_type,
                getattr(desc.contract, "symbol", ""),
            )
            continue
        if sec_type is not None and ibkr_sec_type != sec_type:
            continue
        out.append(
            SymbolMatch(
                symbol=desc.contract.symbol,
                name=getattr(desc.contract, "description", "") or "",
                exchange=getattr(desc.contract, "primaryExchange", "") or "",
                currency=getattr(desc.contract, "currency", "") or "",
                sec_type=ibkr_sec_type,
                derivative_sec_types=list(getattr(desc, "derivativeSecTypes", []) or []),
            )
        )
    return out
