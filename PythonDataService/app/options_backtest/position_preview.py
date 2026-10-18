"""Compose the canonical picker, replay and Strategy Lab payoff for an editable position.

Formula: expiry P&L = 100 × Strategy Lab per-share payoff + replay cash adjustment.
  The adjustment is replay cash − initial cash + the open legs' entry cost. It retains
  realized fills and fees while the canonical expiry payoff values the remaining legs.
Reference: app/services/strategy_engine.py (Hull §11–12) and replay.py's fill accounting.
Canonical implementation: strategy_engine.py for payoff, extremes and roots; replay.py
  for execution, fees and held positions; picking.py for contract selection.
Validated against: tests/options_backtest/test_position_preview.py.

An all-selected position is a snapshot, not a promise that its entries can fill together.
No probability or expected return is inferred. Distinct expiries have no single expiry
payoff. Missing contracts, bars or two-sided quotes never become fabricated premiums.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from decimal import Decimal

from app.models.strategy import StrategyLeg
from app.options.contract import OptionContract
from app.options_backtest.data import RunData
from app.options_backtest.fees import fee_for_policy
from app.options_backtest.market import PickRefused
from app.options_backtest.observation import MinuteObservation
from app.options_backtest.past_chain import chain_session
from app.options_backtest.phrases import plain
from app.options_backtest.picking import pick_contract
from app.options_backtest.replay import replay_session
from app.options_backtest.results import Fill
from app.options_backtest.run import load_session_market
from app.options_backtest.session_market import bars_by_close
from app.schemas.options_position_preview import (
    PositionPreviewCurve,
    PositionPreviewEvent,
    PositionPreviewLeg,
    PositionPreviewRequest,
    PositionPreviewResponse,
    PositionPreviewRisk,
)
from app.services.strategy_engine import (
    compute_max_profit_loss,
    compute_payoff_at_expiry,
    compute_strategy_cost,
    find_breakevens,
)


class PreviewUnavailable(ValueError):
    """A preview cannot be priced from the requested evidence."""


def strategy_legs(legs: Sequence[PositionPreviewLeg]) -> list[StrategyLeg]:
    return [
        StrategyLeg(
            leg_id=leg.leg_id,
            strike=float(leg.strike_usd),
            option_type=leg.right,
            position="long" if leg.side == "buy" else "short",
            premium=float(leg.premium_usd),
            quantity=leg.contracts,
            iv=0,
        )
        for leg in legs
    ]


def expiry_view(
    legs: Sequence[PositionPreviewLeg],
    spot: Decimal,
    *,
    cash_adjustment: Decimal = Decimal(0),
) -> tuple[list[str], list[PositionPreviewCurve], PositionPreviewRisk | None]:
    """Project canonical payoff outputs to contract dollars; no option-price formula is copied."""
    if len({leg.expiry_ms for leg in legs}) > 1:
        return [], [], None
    models = strategy_legs(legs)
    strikes = [Decimal(leg.strike_usd) for leg in legs]
    low = max(Decimal(0), min([spot * Decimal("0.95"), *strikes]) - spot * Decimal("0.01"))
    high = max([spot * Decimal("1.05"), *strikes]) + spot * Decimal("0.01")
    # Include every kink and exact root, so a thin spread cannot disappear between grid samples.
    roots = find_breakevens(models, cash_adjustment=float(cash_adjustment / 100))
    prices = sorted(
        {low + (high - low) * index / 120 for index in range(121)}
        | set(strikes)
        | {Decimal(str(root)) for root in roots if low <= Decimal(str(root)) <= high}
    )

    def dollars(value: float) -> Decimal:
        # Match Strategy Lab compute_payoff_curve: per-share outputs round to four places.
        return Decimal(str(round(value, 4))) * 100

    combined = [plain(dollars(compute_payoff_at_expiry(models, float(price))) + cash_adjustment) for price in prices]
    curves = [PositionPreviewCurve(pnl_usd=combined)]
    for leg, model in zip(legs, models, strict=True):
        curves.append(
            PositionPreviewCurve(
                leg_id=leg.leg_id,
                pnl_usd=[plain(dollars(compute_payoff_at_expiry([model], float(price)))) for price in prices],
            )
        )
    extremes = compute_max_profit_loss(models)
    risk = PositionPreviewRisk(
        entry_debit_usd=plain(dollars(compute_strategy_cost(models))),
        max_profit_usd=None
        if math.isinf(extremes.max_profit)
        else plain(max(Decimal(0), dollars(extremes.max_profit) + cash_adjustment)),
        max_loss_usd=None
        if math.isinf(extremes.max_loss)
        else plain(max(Decimal(0), -(dollars(extremes.max_loss) + cash_adjustment))),
        breakevens_usd=[str(root) for root in roots],
    )
    return [format(price, ".4f").rstrip("0").rstrip(".") for price in prices], curves, risk


def resolved_leg(
    leg_id: str, side: str, contracts: int, contract: OptionContract, premium: Decimal, at_ms: int, source: str
) -> PositionPreviewLeg:
    return PositionPreviewLeg(
        leg_id=leg_id,
        symbol=contract.occ_symbol,
        side=side,
        right=contract.right,
        contracts=contracts,
        strike_usd=str(contract.strike),
        expiry_ms=contract.expiry_ms,
        premium_usd=str(premium),
        price_at_ms=at_ms,
        price_source=source,
    )


def historical_preview(request: PositionPreviewRequest, data: RunData) -> PositionPreviewResponse:
    session = chain_session(request.trading_date_ms)
    setup = request.setup
    if not setup.window_fits(session_open_ms=session.open_ms, session_close_ms=session.close_ms):
        raise PreviewUnavailable("The entry and exit window does not fit this session.")
    entry_ms = setup.entry.at(session_open_ms=session.open_ms, session_close_ms=session.close_ms)
    found = data.underlying_day(session.trading_date)
    if found is None:
        raise PreviewUnavailable(
            "Underlying minute bars are not stored for this session. Use Check data or Past chain to download them."
        )
    bars = bars_by_close(found.value, trading_date=session.trading_date, what="the underlying")
    if entry_ms not in bars:
        raise PreviewUnavailable(
            "The underlying has no bar at the setup's entry minute. No earlier or later price is substituted."
        )
    spot = bars[entry_ms].close
    legs: list[PositionPreviewLeg] = []
    events: list[PositionPreviewEvent] = []
    adjustment = Decimal(0)
    at_ms = entry_ms
    notes = [
        "Expiry payoff, not an expected return. Assumes all selected legs are entered at the displayed minute closes; fees, slippage, momentum, stops and re-entries are excluded."
    ]
    if request.mode == "replay":
        market, _ = load_session_market(setup, session.trading_date, data)
        observations: list[MinuteObservation] = []
        outcome = replay_session(
            setup,
            market,
            cash_usd=setup.starting_cash_usd,
            fee_for=fee_for_policy(setup.fees),
            observer=observations.append,
        )
        at_ms = request.replay_at_ms if request.replay_at_ms is not None else entry_ms
        if not session.open_ms <= at_ms <= session.close_ms or at_ms % 60_000:
            raise PreviewUnavailable("Choose a whole minute within the selected session.")
        observation = next((row for row in reversed(observations) if row.at_ms <= at_ms), None)
        if observation is None:
            raise PreviewUnavailable("This session has no replay observations.")
        at_ms = observation.at_ms
        if at_ms not in bars:
            raise PreviewUnavailable("The underlying has no contemporaneous bar at this replay minute.")
        spot = bars[at_ms].close
        for held in observation.held:
            candidates = [
                trade
                for trade in outcome.trades
                if trade.leg_id == held.leg_id
                and trade.entry.bar_close_ms <= at_ms
                and (not isinstance(trade.exit, Fill) or trade.exit.bar_close_ms > at_ms)
            ]
            if not candidates:
                raise PreviewUnavailable("An open position has no matching entry receipt.")
            trade = candidates[-1]
            legs.append(
                resolved_leg(
                    trade.leg_id,
                    trade.entry.side,
                    trade.contracts,
                    trade.contract,
                    trade.entry.filled,
                    trade.entry.bar_close_ms,
                    "Historical simulated entry fill",
                )
            )
        adjustment = (
            observation.cash_usd
            - setup.starting_cash_usd
            + Decimal(str(compute_strategy_cost(strategy_legs(legs)))) * 100
        )
        events = [
            PositionPreviewEvent(at_ms=event.at_ms, leg_id=event.leg_id, kind=event.kind, detail=event.detail)
            for event in outcome.events
            if event.at_ms <= at_ms
        ]
        notes = [
            "Canonical historical replay: only filled, still-open legs are shown. Combined expiry P&L includes realized cash flows and fees; individual curves show open-leg entry P&L. The curve assumes remaining legs stay to expiry; future stops and re-entries are not forecast."
        ]
    else:
        listing = data.listing(session.trading_date)
        if listing is None:
            raise PreviewUnavailable(
                "The option listing is not stored for this session. Use Past chain to download it."
            )
        for leg in setup.legs:
            picked = pick_contract(leg, listing=listing.value, trading_date=session.trading_date, underlying_price=spot)
            if isinstance(picked, PickRefused):
                raise PreviewUnavailable(f"Leg {leg.leg_id}: {picked.detail}")
            option = data.option_day(picked.contract, session.trading_date)
            if option is None:
                raise PreviewUnavailable(
                    f"{picked.contract.occ_symbol}: minute prices are not stored. Use Check data or Past chain."
                )
            bar = bars_by_close(option.value, trading_date=session.trading_date, what=picked.contract.occ_symbol).get(
                entry_ms
            )
            if bar is None:
                raise PreviewUnavailable(
                    f"{picked.contract.occ_symbol}: no trade in the entry minute. No price is substituted."
                )
            legs.append(
                resolved_leg(
                    leg.leg_id, leg.side, leg.contracts, picked.contract, bar.close, entry_ms, "Stored minute close"
                )
            )
    prices, curves, risk = expiry_view(legs, spot, cash_adjustment=adjustment)
    if risk is None:
        notes.append(
            "These legs have different expiries. There is no single expiry payoff; align their expiries to display this curve."
        )
    return PositionPreviewResponse(
        source="historical",
        mode=request.mode,
        at_ms=at_ms,
        spot_usd=str(spot),
        session_open_ms=session.open_ms,
        session_close_ms=session.close_ms,
        legs=legs,
        prices_usd=prices,
        curves=curves,
        risk=risk,
        notes=notes,
        events=events,
    )
