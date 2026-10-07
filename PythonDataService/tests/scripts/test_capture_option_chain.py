"""The option chain capture: what it freezes, and what it counts."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest

from app.options.contract import OptionContract
from app.options.expiry import option_expiry_date
from app.options.quotes import HeldQuote, QuoteDataType, QuoteFault, QuoteSide
from app.options.resolver import Chain, Listing, UnderlyingObservation, WatchedContract, resolve_contracts
from app.options.watch import LineClass
from scripts.capture_option_chain import NOT_ARRIVED, USABLE, Usability, frozen_chain

# The supervisor against a stand-in IBKR connection, and the resolver's own scene.
from tests.broker.ibkr.test_option_quotes import MONEY, WatchRequest, _bot, _call, _settle, _watching
from tests.options import test_resolver as scenes


def _thawed_quote(kept: dict[str, Any] | None) -> HeldQuote | None:
    if kept is None:
        return None

    def side(name: str) -> QuoteSide | None:
        return None if kept[name] is None else QuoteSide(Decimal(kept[name]), kept[f"{name}_received_ms"])

    return HeldQuote(side("bid"), side("ask"), QuoteDataType(kept["data_type"]), kept["generation"])


def _thawed(kept: dict[str, Any]) -> tuple[Chain, HeldQuote | None]:
    """The chain and the underlying's quote a frozen file describes."""
    chain = Chain(
        listings=tuple(
            Listing(
                OptionContract.from_occ_symbol(listing["occ_symbol"]),
                multiplier=listing["multiplier"],
                shares_delivered=listing["shares_delivered"],
                exercise=listing["exercise"],
            )
            for listing in kept["listings"]
        ),
        watched={
            contract: WatchedContract(_thawed_quote(watched["quote"]), watched["vendor_delta"])
            for contract, watched in kept["watched"].items()
        },
        expiries=tuple(option_expiry_date(expiry_ms) for expiry_ms in kept["expiries_ms"]),
    )
    return chain, _thawed_quote(kept["underlying"])


async def test_a_frozen_chain_read_back_gives_the_pick_the_live_one_gave(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fixture is worth keeping only if a resolution on it is the resolution that was made on the day."""
    scene = scenes._scene()
    plan = scenes._one("call", {"selector": "delta", "target": 0.30})
    status, lines, ibkr, _ = _watching(monkeypatch, WatchRequest("SPY", LineClass.PREVIEW, plan, MONEY), reach=12)
    await _settle(status)
    ibkr.now = scene.selection_ms - 400
    for name in ibkr.open_option_lines():
        quote = scene.chain.watched[name].quote
        ibkr.tick(name, bid=float(quote.bid.price), ask=float(quote.ask.price))
    ibkr.tick("SPY", bid=float(scene.underlying.quote.bid.price), ask=float(scene.underlying.quote.ask.price))
    ibkr.now = scene.selection_ms

    written = json.dumps(
        frozen_chain(
            "SPY",
            lines.chain("SPY"),
            underlying=status.underlying_quote("SPY"),
            generation=lines.generation,
            captured_at_ms=ibkr.now,
        )
    )
    chain, underlying = _thawed(json.loads(written))

    resolution = resolve_contracts(
        plan,
        chain=chain,
        underlying=UnderlyingObservation(scene.underlying.pricing_bar_close, underlying),
        feed=status.option_feed(),
        model=scene.model,
        limits=scene.limits,
        decision_bar_close_ms=scene.decision_bar_close_ms,
        selection_ms=scene.selection_ms,
    )

    assert resolution.picked == scene.resolve(plan).picked
    held = lines.chain("SPY")
    assert (dict(chain.watched), chain.expiries) == (dict(held.watched), held.expiries)
    assert set(chain.listings) == set(held.listings)
    assert json.loads(written)["captured_at_ms"] == scene.selection_ms


async def test_the_tally_counts_each_second_a_quote_was_usable_stale_or_not_arrived(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three calls over eight seconds: one ticks every second, one ticked once and went quiet, one never did."""
    status, lines, ibkr, _ = _watching(monkeypatch, _bot())
    await _settle(status)
    busy, quiet, silent = (_call(strike).occ_symbol for strike in (585, 584, 583))
    ibkr.tick(quiet, bid=1.00, ask=1.02)
    usability = Usability()

    for _ in range(8):
        ibkr.tick(busy, bid=4.10, ask=4.14)
        usability.sample(lines.chain("SPY"), now=ibkr.now, generation=lines.generation, max_age_ms=5_000)
        ibkr.now += 1_000

    tally = usability.as_json(max_age_ms=5_000)
    assert tally["contracts"][busy] == {USABLE: 8}
    # Received at the first second: usable for the six samples up to five seconds old, stale after.
    assert tally["contracts"][quiet] == {USABLE: 6, str(QuoteFault.STALE): 2}
    assert tally["contracts"][silent] == {NOT_ARRIVED: 8}
    assert (tally["samples"], tally["max_quote_age_ms"]) == (8, 5_000)
    # Five watched contracts, eight samples each; fourteen of the forty usable.
    assert tally["usable_share_of_contract_samples"] == 0.35
