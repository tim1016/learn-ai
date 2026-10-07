"""What IBKR lists for an underlying's options: the chain's definition, with no quote in it.

IBKR names a chain's expiries in one answer and its contracts one expiry at
a time. Both are facts of the listing, read once and kept for the day; they
are not market data and draw on no quote line.

A contract is identified by the OCC symbol IBKR gives it as its local symbol,
which is the form the broker that takes the order uses. The symbol is parsed,
never rebuilt from the contract's fields, and a contract whose fields
disagree with its own symbol is left out.

What the listing cannot say is taken as standard and proven elsewhere: a
stock or ETF option is counted as American, and a contract in the
underlying's own trading class as delivering one share per unit of its
multiplier. The broker's contract reference is asked about each picked
contract before any order (ADR 0076). Contracts in another trading class,
which is how an adjusted contract is listed, are not part of the chain.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.broker.ibkr.client import IbkrClient
from app.broker.ibkr.contracts import qualify_underlying
from app.options.contract import OptionContract
from app.options.resolver import Listing

logger = logging.getLogger(__name__)

EXPIRIES_TIMEOUT_S = 10
CONTRACTS_TIMEOUT_S = 30
_RIGHTS = {"C": "call", "P": "put"}


@dataclass(frozen=True, slots=True)
class ListedContract:
    """One listed contract, with the IBKR contract a quote is asked for under."""

    listing: Listing
    ibkr_contract: Any


def occ_root(symbol: str) -> str:
    """The root a ticker's standard contracts are listed under: the ticker with its share-class mark removed."""
    return "".join(character for character in symbol.upper() if character.isalnum())


async def listed_expiries(client: IbkrClient, symbol: str) -> tuple[date, ...]:
    """Every date ``symbol`` lists standard option contracts for, soonest first."""
    stock = await asyncio.wait_for(qualify_underlying(client, symbol), timeout=EXPIRIES_TIMEOUT_S)
    chains = await asyncio.wait_for(
        client.ib.reqSecDefOptParamsAsync(
            underlyingSymbol=stock.symbol,
            futFopExchange="",
            underlyingSecType=stock.secType,
            underlyingConId=stock.conId,
        ),
        timeout=EXPIRIES_TIMEOUT_S,
    )
    root = occ_root(symbol)
    return tuple(
        sorted(
            {
                datetime.strptime(expiry, "%Y%m%d").date()
                for chain in chains or ()
                # The same chain is reported once for each exchange that lists it.
                if occ_root(chain.tradingClass) == root
                for expiry in chain.expirations
            }
        )
    )


def _listed(detail: Any, root: str, expiry: date) -> ListedContract | None:
    """The contract one IBKR contract detail describes, or None when it is not one of the chain's."""
    described = detail.contract
    try:
        contract = OptionContract.from_occ_symbol("".join(str(described.localSymbol).split()))
        multiplier = int(Decimal(str(described.multiplier)))
        strike = Decimal(str(described.strike))
    except (ValueError, InvalidOperation):
        return None
    agrees = (
        contract.root == root
        and occ_root(str(described.tradingClass)) == root
        and contract.expiry_date == expiry
        and contract.right == _RIGHTS.get(described.right)
        and contract.strike == strike
    )
    if not agrees:
        return None
    return ListedContract(
        Listing(contract, multiplier=multiplier, shares_delivered=multiplier, exercise="american"), described
    )


async def listed_contracts(client: IbkrClient, symbol: str, expiry: date) -> dict[str, ListedContract]:
    """Every contract ``symbol`` lists for ``expiry`` in its own trading class, by OCC symbol."""
    from ib_async import Option

    details = await asyncio.wait_for(
        client.ib.reqContractDetailsAsync(
            Option(
                symbol=symbol,
                lastTradeDateOrContractMonth=expiry.strftime("%Y%m%d"),
                exchange="SMART",
                currency="USD",
            )
        ),
        timeout=CONTRACTS_TIMEOUT_S,
    )
    root = occ_root(symbol)
    listed: dict[str, ListedContract] = {}
    left_out = 0
    for detail in details or ():
        contract = _listed(detail, root, expiry)
        if contract is None:
            left_out += 1
            continue
        listed[contract.listing.contract.occ_symbol] = contract
    if left_out:
        logger.info(
            "Left out listed option contracts that are not the underlying's own",
            extra={"action": "option_chain_contracts_left_out", "symbol": symbol, "count": left_out},
        )
    return listed
