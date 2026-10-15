"""The Polygon SDK's options-chain snapshot, filtering expiries the way the vendor does.

The live IV30 routes and the recorder were tested by replacing
``PolygonClientService.list_snapshot_options_chain`` itself, so the client's own
expiry default -- today's single date, which the IV30 pairing drops as 0 days --
never ran and every live call answered "got 0 expiries" (#3221). This stands in
for ``polygon.RESTClient`` instead, one layer down, so the client's parameter
handling runs for real against a filter with the vendor's semantics:

* no ``expiration_date*`` parameter: every expiry of the chain;
* ``expiration_date``: that one date, exactly;
* ``expiration_date.gte`` / ``expiration_date.lte``: inclusive bounds.

A parameter this stand-in does not model fails loudly, so a client change cannot
slide past a filter nobody is applying.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

from app.options.expiry import option_expiry_date

# ``limit`` is a page size: it moves how many requests a read takes, not which contracts match.
_MODELLED = {"expiration_date", "expiration_date.gte", "expiration_date.lte", "limit"}


class VendorFilteringSnapshotSdk:
    """Holds a chain in the client's own contract shape and serves it as SDK snapshot objects."""

    def __init__(self, contracts: list[dict[str, Any]], *, spot: float) -> None:
        self._contracts = contracts
        self._spot = spot
        self.requests: list[dict[str, str]] = []

    def list_snapshot_options_chain(
        self, underlying_asset: str, params: dict[str, str] | None = None, **_unused: Any
    ) -> Iterator[SimpleNamespace]:
        requested = dict(params or {})
        unmodelled = set(requested) - _MODELLED
        assert not unmodelled, f"the fake vendor does not apply {sorted(unmodelled)}"
        assert int(requested.get("limit", 10)) <= 250, "the vendor's largest snapshot page is 250"
        self.requests.append(requested)

        for contract in self._contracts:
            expiry = option_expiry_date(contract["expiration_ms"]).isoformat()
            if "expiration_date" in requested and expiry != requested["expiration_date"]:
                continue
            if "expiration_date.gte" in requested and expiry < requested["expiration_date.gte"]:
                continue
            if "expiration_date.lte" in requested and expiry > requested["expiration_date.lte"]:
                continue
            quote = contract.get("last_quote") or {}
            yield SimpleNamespace(
                underlying_asset=SimpleNamespace(ticker=underlying_asset, price=self._spot, change_to_break_even=0.0),
                details=SimpleNamespace(
                    ticker=contract["ticker"],
                    contract_type=contract["contract_type"],
                    strike_price=contract["strike_price"],
                    expiration_date=expiry,
                ),
                last_quote=SimpleNamespace(bid=quote.get("bid"), ask=quote.get("ask")),
                implied_volatility=contract.get("implied_volatility"),
            )
