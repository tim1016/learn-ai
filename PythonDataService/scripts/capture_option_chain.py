"""Freeze an option chain as a Clerk's IBKR supervisor holds it, and measure how often its quotes are usable.

Two things only a session with the Gateway can say (ADR 0076 decision 9):

- the chain a contract resolution is tested against, frozen as a fixture: the
  listing, and for each watched contract the bid and the ask with the time
  each was received;
- how often, second by second, a watched contract's quote could be used under
  the decision-data budget, was stale, or had not arrived.

It watches one named structure's starting window on one underlying, through
the same supervisor and option lines a Clerk uses, as a preview: the lowest
class of line. **It draws on the IBKR username's shared quote lines for as
long as it runs**, so run it only when that is agreed, with a client id no
Clerk uses, from a container that reaches IB Gateway::

    python -m scripts.capture_option_chain --symbol SPY --structure iron_condor \\
        --seconds 120 --budget 60 --client-id 93 --out /tmp/option-chain

Writes ``chain.json`` (the frozen chain) and ``usability.json`` (the tally)
into ``--out``. As of 2026-10-06 this has never been run against a live
connection: its counting and its file format are tested, its connection is
not.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections import Counter
from pathlib import Path
from typing import Any

from app.broker.ibkr.client import IbkrClient
from app.broker.ibkr.config import get_settings
from app.broker.ibkr.market_liveness import IbkrMarketStatusSource
from app.broker.ibkr.market_subscription import DECISION_DATA_MAX_AGE_MS
from app.broker.ibkr.option_quotes import OptionQuoteLines, WatchRequest
from app.options.expiry import option_expiry_anchor_ms
from app.options.named_structures import NamedStructure
from app.options.quotes import NOT_YET_ARRIVED, HeldQuote, quote_fault
from app.options.resolver import Chain, WatchedContract
from app.options.structure_catalogue import starting_plan
from app.options.watch import LineClass
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

USABLE, NOT_ARRIVED = "usable", "not_arrived"


def how_it_stands(watched: WatchedContract, *, now: int, generation: int, max_age_ms: int) -> str:
    """How one watched contract's quote stands for a bought leg at ``now``: usable, not arrived, or its fault."""
    if watched.quote is None:
        return NOT_ARRIVED
    fault = quote_fault(watched.quote, at_ms=now, generation=generation, max_age_ms=max_age_ms, needs_a_bid=False)
    if fault is None:
        return USABLE
    return NOT_ARRIVED if fault in NOT_YET_ARRIVED else str(fault)


class Usability:
    """A count, for each watched contract, of the samples at which its quote stood each way."""

    def __init__(self) -> None:
        self.samples = 0
        self.by_contract: dict[str, Counter[str]] = {}

    def sample(self, chain: Chain | None, *, now: int, generation: int, max_age_ms: int) -> None:
        self.samples += 1
        for symbol, watched in (chain.watched if chain is not None else {}).items():
            stood = how_it_stands(watched, now=now, generation=generation, max_age_ms=max_age_ms)
            self.by_contract.setdefault(symbol, Counter())[stood] += 1

    def as_json(self, *, max_age_ms: int) -> dict[str, Any]:
        seen = sum(sum(counts.values()) for counts in self.by_contract.values())
        usable = sum(counts[USABLE] for counts in self.by_contract.values())
        return {
            "samples": self.samples,
            "max_quote_age_ms": max_age_ms,
            "usable_share_of_contract_samples": None if not seen else round(usable / seen, 4),
            "contracts": {symbol: dict(sorted(counts.items())) for symbol, counts in sorted(self.by_contract.items())},
        }


def _quote_json(quote: HeldQuote | None) -> dict[str, Any] | None:
    if quote is None:
        return None
    return {
        "bid": None if quote.bid is None else str(quote.bid.price),
        "bid_received_ms": None if quote.bid is None else quote.bid.received_ms,
        "ask": None if quote.ask is None else str(quote.ask.price),
        "ask_received_ms": None if quote.ask is None else quote.ask.received_ms,
        "data_type": str(quote.data_type),
        "generation": quote.generation,
    }


def frozen_chain(
    symbol: str, chain: Chain, *, underlying: HeldQuote | None, generation: int, captured_at_ms: int
) -> dict[str, Any]:
    """The chain as it is held at one moment, in a form a fixture can keep. Prices are exact decimals as text."""
    assert chain.expiries is not None
    return {
        "symbol": symbol,
        "captured_at_ms": captured_at_ms,
        "generation": generation,
        "expiries_ms": [option_expiry_anchor_ms(day) for day in chain.expiries],
        "listings": [
            {
                "occ_symbol": listing.contract.occ_symbol,
                "multiplier": listing.multiplier,
                "shares_delivered": listing.shares_delivered,
                "exercise": listing.exercise,
            }
            for listing in sorted(chain.listings, key=lambda listing: listing.contract.occ_symbol)
        ],
        "underlying": _quote_json(underlying),
        "watched": {
            contract: {"quote": _quote_json(watched.quote), "vendor_delta": watched.vendor_delta}
            for contract, watched in sorted(chain.watched.items())
        },
    }


async def _capture(args: argparse.Namespace) -> None:
    symbol = args.symbol.upper()
    plan = starting_plan(NamedStructure(args.structure), symbol)
    client = IbkrClient(get_settings().model_copy(update={"client_id": args.client_id}))
    lines = OptionQuoteLines(
        requests=lambda: (WatchRequest(symbol, LineClass.PREVIEW, plan),),
        budget=args.budget,
        strikes_each_side=args.strikes_each_side,
    )
    status = IbkrMarketStatusSource(symbols=lambda: (), client=lambda: client, options=lines)
    usability = Usability()
    await client.connect()
    try:
        for _ in range(args.seconds):
            await status()
            usability.sample(
                lines.chain(symbol), now=now_ms_utc(), generation=lines.generation, max_age_ms=DECISION_DATA_MAX_AGE_MS
            )
            await asyncio.sleep(1)
        chain = lines.chain(symbol)
        if chain is None:
            raise SystemExit(f"IBKR named no option expiries for {symbol} in {args.seconds} seconds")
        args.out.mkdir(parents=True, exist_ok=True)
        frozen = frozen_chain(
            symbol,
            chain,
            underlying=status.underlying_quote(symbol),
            generation=lines.generation,
            captured_at_ms=now_ms_utc(),
        )
        use = lines.line_use()
        tally = usability.as_json(max_age_ms=DECISION_DATA_MAX_AGE_MS) | {
            "lines": {"budget": use.budget, "open": use.open, "ceiling": use.ceiling, "without": dict(use.without)}
        }
        (args.out / "chain.json").write_text(json.dumps(frozen, indent=1))
        (args.out / "usability.json").write_text(json.dumps(tally, indent=1))
        logger.info(
            "option chain captured",
            extra={"out": str(args.out), "watched": len(chain.watched), "lines_open": use.open},
        )
    finally:
        await status.close()
        await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--structure", default=NamedStructure.IRON_CONDOR.value, choices=[name.value for name in NamedStructure])
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument("--budget", type=int, required=True, help="the most option quote lines to take from the shared allowance")
    parser.add_argument("--strikes-each-side", type=int, default=10)
    parser.add_argument("--client-id", type=int, required=True, help="an IBKR client id no Clerk uses")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_capture(args))


if __name__ == "__main__":
    main()
