"""Option structures of several legs, for the tests of their entry on the account Clerk's books (#2913).

The two spreads the PRD works by hand, on SPY calls that expire 2026-09-18,
a hundred shares a contract:

- **paid**: buy the 585 call at its ask, $3.14, and sell the 590 call at its
  bid, $1.20. Net $1.94 paid a share: $194.00 a structure, no requirement.
- **received**: sell the 580 call at its bid, $5.31, and buy the 585 call at
  its ask, $2.24. Net $3.07 received a share: $307.00 a structure, against a
  requirement of the $5 between the strikes, $500.00. $193.00 is set aside.

``worked_entry`` builds an entry's facts from the canonical functions
themselves (``prepare_entry``, ``entry_fee_provision_cents``,
``opening_order``), so the books can be tested without the Clerk's
admission, and admission can be held to the same figures.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any

from app.broker.alpaca.clerk.option_effects import OptionEntryEvidence
from app.broker.alpaca.clerk.sqlite.option_entry import MultiLegOptionEntryFacts
from app.broker.alpaca.option_fees import entry_fee_provision_cents
from app.broker.contract.options import MultiLegOptionOrderRequest, opening_order
from app.options.contract import OptionContract
from app.options.deadlines import CloseTerms
from app.options.entry import prepare_entry
from app.options.quotes import HeldQuote, QuoteDataType, QuoteSide
from app.options.requirement import EntrySize, LegQuote
from app.options.structure_check import resolved_structure
from app.schemas.action_plan import ActionPlan
from app.utils.session_anchors import et_date_at_ms

GENERATION = 4
EXPIRY = date(2026, 9, 18)
EIGHT, TEN = Decimal(8), Decimal(10)

# A pick: the contract a leg's rule came to, and its bid and ask.
Pick = tuple[OptionContract, str, str]


def call(strike: int, *, expiry: date = EXPIRY) -> OptionContract:
    return OptionContract.of(root="SPY", expiry_date=expiry, right="call", strike=strike)


def put(strike: int, *, expiry: date = EXPIRY) -> OptionContract:
    return OptionContract.of(root="SPY", expiry_date=expiry, right="put", strike=strike)


def leg(
    leg_id: str, *, position: str = "long", right: str = "call", ratio: int = 1, strike: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "leg_id": leg_id,
        "instrument": {"kind": "option", "underlying": "SPY"},
        "position": position,
        "qty_ratio": ratio,
        "right": right,
        "strike": strike or {"selector": "atm"},
        "expiry": {"selector": "nearest_weekly"},
    }


def away_from(leg_id: str, dollars: float) -> dict[str, Any]:
    """A strike so many dollars from another leg's."""
    return {"selector": "leg_distance", "leg_id": leg_id, "dollars": dollars}


def plan(*legs: dict[str, Any]) -> ActionPlan:
    return ActionPlan.model_validate(
        {"on_enter": list(legs), "on_exit": [{"kind": "close_leg", "entry_leg_id": each["leg_id"]} for each in legs]}
    )


def quote(bid: str, ask: str, *, received_ms: int, generation: int = GENERATION) -> HeldQuote:
    return HeldQuote(
        bid=QuoteSide(Decimal(bid), received_ms),
        ask=QuoteSide(Decimal(ask), received_ms),
        data_type=QuoteDataType.LIVE,
        generation=generation,
    )


def evidence(now_ms: int, picks: Mapping[str, Pick], **changes: Any) -> OptionEntryEvidence:
    """What a runner hands over for a structure: each leg's pick, its multiplier and a quote received 300 ms ago."""
    return replace(
        OptionEntryEvidence(
            picked={leg_id: contract for leg_id, (contract, _bid, _ask) in picks.items()},
            multipliers=dict.fromkeys(picks, 100),
            quotes={leg_id: quote(bid, ask, received_ms=now_ms - 300) for leg_id, (_contract, bid, ask) in picks.items()},
            generation=GENERATION, rechecked_at_ms=now_ms - 100, leg_gap_limit_percent=EIGHT, cost_limit_percent=TEN,
        ),
        **changes,
    )


# The PRD's call spread bought for $1.94.
PAID_SPREAD = plan(leg("long_call"), leg("short_call", position="short", strike=away_from("long_call", 5.0)))
PAID_PICKS: Mapping[str, Pick] = {"long_call": (call(585), "3.10", "3.14"), "short_call": (call(590), "1.20", "1.24")}
# The PRD's $5-wide call spread sold for $3.07.
RECEIVED_SPREAD = plan(leg("short_call", position="short"), leg("long_call", strike=away_from("short_call", 5.0)))
RECEIVED_PICKS: Mapping[str, Pick] = {
    "short_call": (call(580), "5.31", "5.35"), "long_call": (call(585), "2.20", "2.24"),
}
# Two bought legs: a straddle at 585. The call at $3.14 and the put at $2.86: $6.00 paid a share.
STRADDLE = plan(leg("long_call"), leg("long_put", right="put"))
STRADDLE_PICKS: Mapping[str, Pick] = {"long_call": (call(585), "3.10", "3.14"), "long_put": (put(585), "2.82", "2.86")}


def worked_entry(
    action_plan: ActionPlan, picks: Mapping[str, Pick], *, units: int, now_ms: int, terms: CloseTerms | None = None
) -> MultiLegOptionEntryFacts:
    """An entry of several legs as the canonical functions work it out, with no Clerk in between."""
    handed = evidence(now_ms, picks)
    structure = resolved_structure(action_plan, handed.picked)
    markets = {
        leg_id: LegQuote(bid=held.bid.price, ask=held.ask.price)  # type: ignore[union-attr]
        for leg_id, held in handed.quotes.items()
    }
    fees = entry_fee_provision_cents(
        structure, markets, EntrySize(multiplier=100, units=units), trade_date=et_date_at_ms(now_ms)
    )
    prepared = prepare_entry(
        action_plan, picked=handed.picked, multipliers=handed.multipliers, quotes=handed.quotes, units=units,
        fee_provision_cents=fees, leg_gap_limit_percent=EIGHT, cost_limit_percent=TEN,
    )
    order = opening_order(prepared.structure, units=units, net_limit_price=prepared.net_limit_price)
    assert isinstance(order, MultiLegOptionOrderRequest)
    return MultiLegOptionEntryFacts.of(
        prepared, order=order, fee_provision_cents=fees, leg_gap_limit_percent=EIGHT, cost_limit_percent=TEN,
        quote_generation=GENERATION, rechecked_at_ms=now_ms - 100, admitted_at_ms=now_ms,
        terms=terms or CloseTerms(),
    )
