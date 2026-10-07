"""Expiry settlement shared by Dry Run and Live Shadow (ADR 0076 decisions 22 and 25).

The simulation writes only its own ledger. The valuation and lot-allocation
math remain the canonical implementations named in each delegated call.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import NamedTuple, Protocol

from app.broker.alpaca.clerk.simulated_option_orders import contract_multipliers
from app.broker.alpaca.clerk.simulated_option_settlement import (
    SETTLEMENT_BAR_MAX_AGE_MS,
    SETTLEMENT_GRACE_MS,
    SIMULATED_CASH_SETTLEMENT_ACTIVITY,
    SIMULATED_EXPIRY_ACTIVITY,
    SIMULATED_OWNER_ENDING_ACTIVITY,
    SIMULATED_OWNER_ENDING_RULE,
    SIMULATED_SETTLEMENT_RULE,
    OwnersWord,
    SettlementPrice,
    SimulatedOptionSettlements,
    SimulatedOutcome,
    SimulatedSettlement,
    SynthesizedOptionSettlement,
    exercise_value,
    settlement_activity_id,
)
from app.broker.alpaca.clerk.sqlite.folds import position_quantity_is_nonzero
from app.broker.alpaca.clerk.synthesized_orders import SynthesizedOrderLedger, SynthesizedOrderRecord, project_positions
from app.broker.alpaca.option_fees import fees_for_option_fill, option_fee_provision_cents
from app.broker.alpaca.regulatory_fees import RateNotPinnedError
from app.broker.contract.models import BrokerOrder, OrderSide
from app.broker.contract.option_events import BrokerOptionEventActivity, OptionActivityStatus
from app.lean_sidecar.trading_calendar import session_open_ms_utc
from app.options.contract import OptionContract
from app.options.deadlines import last_session_close_ms, past_last_session
from app.options.event_allocation import OptionEventKind
from app.services.source_bar_ledger import RetainedSourceBar
from app.utils.session_anchors import et_date_at_ms, et_midnight_ms
from app.utils.timestamps import Clock

logger = logging.getLogger(__name__)

class ClosingBarSource(Protocol):
    """The simulation's own retained market prices, read without inventing a close."""

    def latest_for_symbol(self, symbol: str, *, market_only: bool, at_or_before_ms: int) -> RetainedSourceBar | None: ...

class _AwaitingTheClosingBar(NamedTuple):
    """A contract's last session is over and its closing bar may still arrive: the simulation waits."""

    until_ms: int


class _NoPriceAtTheClose(NamedTuple):
    """Nothing kept says what the underlying closed at. No price comes by itself: only the owner's word ends it."""

    why: str


def settlement_client_order_id(symbol: str) -> str:
    """The id of the one ledger record that takes a contract out of this simulation past its last session."""
    return f"sim-settlement:{symbol}:option"


def _minutes(ms: int) -> str:
    minutes = ms // 60_000
    return f"{minutes} minute{'' if minutes == 1 else 's'}"


def _settlement_price(bar: RetainedSourceBar, *, close_ms: int) -> SettlementPrice:
    """The close of one kept bar as the price a contract is settled from."""
    return SettlementPrice(
        underlying_price=bar.close, bar_ref=bar.bar_ref, bar_provider=bar.provider, bar_end_ms=bar.end_ms,
        bar_before_close_ms=close_ms - bar.end_ms,
    )


def _sale_fee_cents(trade_date: date, *, contracts: int, price: Decimal, multiplier: int) -> int | None:
    """What the option fee model charges one simulated sale taken as a day of its own, or None when it cannot say.

    For the settlement's own record. The account's fees are worked out
    from its fills where every fee is (``fee_evidence``), which settles a
    day over all of that day's option fills, so the day's charge can differ
    from this by a cent a fee type.

    Formula: ``option_fee_provision_cents`` of the one fill: each fee type of a
      sale of ``contracts`` at ``price`` a share, rounded up to the cent, summed.
    Reference: ADR 0076 decision 13; Alpaca's fee schedule as ``option_fees.py`` cites it.
    Canonical implementation: ``app/broker/alpaca/option_fees.py``. Nothing is worked out here.
    Validated against: tests/broker/alpaca/clerk/test_synthetic_broker_option_settlement.py
      (one call sold at 5.25: nine cents, by hand).
    """
    try:
        return option_fee_provision_cents([
            fees_for_option_fill(
                trade_date=trade_date, side=OrderSide.SELL, contracts=Decimal(contracts), premium=price,
                multiplier=multiplier,
            )
        ])
    except RateNotPinnedError:
        # A fee rate is not pinned for that day. The record says the fee is not known, never that it is nothing.
        return None


def _settlement_order(
    broker_id: str,
    client_order_id: str,
    *,
    symbol: str,
    quantity: float,
    price: float,
    happened_at_ms: int,
    observed_at_ms: int,
) -> BrokerOrder:
    """A settlement in the shape the ledger's position fold reads: a sale of the contracts, filled once, in full.

    It is no order anyone sent. It is written as one so that the
    simulation's positions, which are a fold of its orders, show what the
    settlement did. It is stamped at the close of the contract's last session.
    """
    return BrokerOrder(
        broker=broker_id,
        order_id=f"sim-order:{client_order_id}",
        client_order_id=client_order_id,
        symbol=symbol,
        asset_class="us_option",
        side="sell",
        order_type="market",
        time_in_force="day",
        quantity=quantity,
        limit_price=None,
        stop_price=None,
        extended_hours=False,
        submitted_at_ms=happened_at_ms,
        created_at_ms=happened_at_ms,
        updated_at_ms=happened_at_ms,
        expired_at_ms=None,
        observed_at_ms=observed_at_ms,
        filled_quantity=quantity,
        filled_avg_price=price,
        status="filled",
        filled_at_ms=happened_at_ms,
        canceled_at_ms=None,
    )


_ACTIVITY_CODES: Mapping[SimulatedOutcome, str] = {
    SimulatedOutcome.EXPIRED: SIMULATED_EXPIRY_ACTIVITY,
    SimulatedOutcome.SETTLED_IN_CASH: SIMULATED_CASH_SETTLEMENT_ACTIVITY,
    SimulatedOutcome.ENDED_BY_OWNER: SIMULATED_OWNER_ENDING_ACTIVITY,
}


class SimulatedOptionExpiry:
    """Settle one simulation's expired positions with the common cash rule."""

    def __init__(self, *, account_id: str, broker_id: str, ledger: SynthesizedOrderLedger | None,
                 source_bars: ClosingBarSource | None, clock: Clock) -> None:
        self._account_id = account_id
        self.broker_id = broker_id
        self._ledger = ledger
        self._source_bars = source_bars
        self._clock = clock

    def settle_expired_options(
        self, *, after_ms: int, at_the_owners_word: Mapping[str, OwnersWord] | None = None
    ) -> SimulatedOptionSettlements:
        """Settle every contract this simulation holds past its last session, and say what it settled and what it could not.

        The one step that settles. It writes each settlement to this
        simulation's own order ledger, once, and then hands back every
        settlement dated in the window, the ones an earlier run made among
        them: a settlement the Clerk has not booked yet is handed over
        until it has. Everything is read under the ledger's own lock, so
        what is handed back is what the ledger holds.

        A contract is settled in cash at its exercise value, or expires at
        nothing (``simulated_option_settlement``). It is never exercised
        into shares. A contract whose closing price is still awaited, or
        that has none, stays held and is named in the answer.

        ``at_the_owners_word`` is the owner's word to end a contract that
        nothing will settle, by contract symbol. The price is asked for
        once more first. Only a contract that still has none is let go of,
        at nothing. One that has a price by now is settled by the rule, and
        the owner's word for it is not used.
        """
        if self._ledger is None:
            return SimulatedOptionSettlements()
        at_ms = self._clock()
        window_start_ms = et_midnight_ms(et_date_at_ms(after_ms))
        not_settled: dict[str, str] = {}
        waiting: dict[str, int] = {}
        with self._ledger.transaction() as records:
            latest = list(self._ledger.latest_records_from(records).values())
            multipliers = contract_multipliers(latest)
            held = project_positions([record.order for record in latest])
            for symbol, (quantity, _notional) in held.items():
                # A contract is a symbol this ledger opened with a multiplier. Shares are never settled.
                if symbol not in multipliers or not position_quantity_is_nonzero(quantity):
                    continue
                contract = OptionContract.from_occ_symbol(symbol)
                if not past_last_session(contract.expiry_date, at_ms):
                    continue
                unsettled = self._settle_one(
                    records, contract, held=quantity, multiplier=multipliers[symbol], at_ms=at_ms
                )
                word = (at_the_owners_word or {}).get(symbol)
                if isinstance(unsettled, _NoPriceAtTheClose) and word is not None:
                    self._let_go_at_the_owners_word(
                        records, contract, contracts=int(quantity), multiplier=multipliers[symbol], at_ms=at_ms,
                        word=word, why=unsettled.why,
                    )
                elif isinstance(unsettled, _AwaitingTheClosingBar):
                    waiting[symbol] = unsettled.until_ms
                elif unsettled is not None:
                    not_settled[symbol] = unsettled.why if isinstance(unsettled, _NoPriceAtTheClose) else unsettled
            settled = tuple(
                SimulatedSettlement(
                    event=self._settlement_as_an_event(record.settlement, observed_at_ms=at_ms),
                    record=record.settlement,
                )
                for record in self._ledger.latest_records_from(records).values()
                if record.settlement is not None
                and et_midnight_ms(record.settlement.contract.expiry_date) >= window_start_ms
            )
        return SimulatedOptionSettlements(settled=settled, not_settled=not_settled, waiting=waiting)


    def _settlement_as_an_event(
        self, settlement: SynthesizedOptionSettlement, *, observed_at_ms: int
    ) -> BrokerOptionEventActivity:
        """One settlement of the ledger in the shape an option event travels in.

        It travels as an expiry, whatever its outcome: it ends a bought
        position and moves no shares, and the rule that chooses lots knows
        an expiry (``app/options/event_allocation.py``). The price a cash
        settlement was booked at is in the simulation's own record, never
        on this row, which states no cash.
        """
        contract = settlement.contract
        activity_id = settlement_activity_id(contract.occ_symbol)
        sold = settlement.outcome is SimulatedOutcome.SETTLED_IN_CASH
        return BrokerOptionEventActivity(
            broker=self.broker_id,
            activity_id=activity_id,
            activity_type=_ACTIVITY_CODES[settlement.outcome],
            kind=OptionEventKind.EXPIRED,
            status=OptionActivityStatus.EXECUTED,
            activity_date_ms=et_midnight_ms(contract.expiry_date),
            contract=contract,
            position_change=-settlement.contracts,
            net_amount=None if sold else Decimal(0),
            group_id=activity_id,
            created_at_ms=settlement.settled_at_ms,
            observed_at_ms=observed_at_ms,
        )


    def _price_at_the_close(
        self, contract: OptionContract, *, close_ms: int, at_ms: int
    ) -> SettlementPrice | _AwaitingTheClosingBar | _NoPriceAtTheClose:
        """The price a contract is settled from, the wait for it, or why there is none.

        Only a bar the market delivered for the underlying counts, and only
        one that ended inside the contract's last session. The bar that
        ends exactly at the close is the price at once. Without it the
        simulation waits out the grace, in case that bar is still on its
        way, and then takes the newest bar of the session when it is not
        too old to stand for the close.
        """
        assert self._source_bars is not None
        root = contract.root
        open_ms = session_open_ms_utc(et_date_at_ms(close_ms - 1))
        bar = self._source_bars.latest_for_symbol(root, market_only=True, at_or_before_ms=close_ms)
        in_the_session = bar is not None and bar.end_ms > open_ms
        if bar is not None and in_the_session and bar.end_ms == close_ms:
            return _settlement_price(bar, close_ms=close_ms)
        if at_ms < close_ms + SETTLEMENT_GRACE_MS:
            return _AwaitingTheClosingBar(until_ms=close_ms + SETTLEMENT_GRACE_MS)
        if bar is None or not in_the_session:
            return _NoPriceAtTheClose(
                f"This simulation kept no price of {root} from the contract's last session, so it cannot say "
                "what the contract was worth at the close."
            )
        if close_ms - bar.end_ms > SETTLEMENT_BAR_MAX_AGE_MS:
            return _NoPriceAtTheClose(
                f"The newest price of {root} this simulation kept from the contract's last session is from "
                f"{_minutes(close_ms - bar.end_ms)} before the close. Its rule takes none older than "
                f"{_minutes(SETTLEMENT_BAR_MAX_AGE_MS)}, so it cannot say what the contract was worth at the close."
            )
        return _settlement_price(bar, close_ms=close_ms)


    def _settle_one(
        self,
        records: list[SynthesizedOrderRecord],
        contract: OptionContract,
        *,
        held: float,
        multiplier: int,
        at_ms: int,
    ) -> _AwaitingTheClosingBar | _NoPriceAtTheClose | str | None:
        """Settle one expired contract in the ledger. None when it was settled; else the wait, or why it cannot be.

        A contract with no price at the close is the one kind the owner can
        end at their word. Any other reason is in plain words: a holding
        this simulation settles in no way.

        The figures are ``simulated_option_settlement.exercise_value``'s. One
        record takes the option position out of the ledger at the booked
        price, stamped at the close of the contract's last session. No
        shares move.
        """
        assert self._ledger is not None
        symbol = contract.occ_symbol
        if held < 0 or not float(held).is_integer():
            return "This simulation settles only whole contracts it bought."
        if self._source_bars is None:
            return "This simulation keeps no prices of the underlying, so it cannot say what the contract was worth at the close."
        close_ms = last_session_close_ms(contract.expiry_date)
        price = self._price_at_the_close(contract, close_ms=close_ms, at_ms=at_ms)
        if not isinstance(price, SettlementPrice):
            return price
        contracts = int(held)
        worth = exercise_value(
            contract, underlying_price=price.underlying_price, contracts=contracts, multiplier=multiplier
        )
        sold = worth.booked > 0
        settlement = SynthesizedOptionSettlement(
            contract=contract,
            outcome=SimulatedOutcome.SETTLED_IN_CASH if sold else SimulatedOutcome.EXPIRED,
            contracts=contracts, multiplier=multiplier, last_session_close_ms=close_ms, settled_at_ms=at_ms,
            rule=SIMULATED_SETTLEMENT_RULE, price=price, exercise_value=worth.worked_out, booked_price=worth.booked,
            cash_cents=worth.cash_cents,
            fee_cents=_sale_fee_cents(
                et_date_at_ms(close_ms - 1), contracts=contracts, price=worth.booked, multiplier=multiplier
            ) if sold else None,
        )
        self._ledger.append_locked(
            records,
            order=_settlement_order(
                self.broker_id, settlement_client_order_id(symbol), symbol=symbol, quantity=float(contracts),
                price=float(worth.booked), happened_at_ms=close_ms, observed_at_ms=at_ms,
            ),
            settlement=settlement,
        )
        logger.info(
            "the simulation settled a contract it held past its last session",
            extra={
                "action": "simulated_option_settled", "account_id": self._account_id, "symbol": symbol,
                "outcome": settlement.outcome.value, "contracts": contracts,
                "underlying_price": str(price.underlying_price), "booked_price": str(worth.booked),
                "bar_ref": price.bar_ref, "bar_before_close_ms": price.bar_before_close_ms,
            },
        )
        return None


    def _let_go_at_the_owners_word(
        self,
        records: list[SynthesizedOrderRecord],
        contract: OptionContract,
        *,
        contracts: int,
        multiplier: int,
        at_ms: int,
        word: OwnersWord,
        why: str,
    ) -> None:
        """Take a contract with no price at the close out of the ledger, at nothing, at the owner's word.

        One record, named as a settlement of the contract is named. It
        takes the whole position out, so a price that turns up later finds
        nothing to settle.
        """
        assert self._ledger is not None
        symbol = contract.occ_symbol
        close_ms = last_session_close_ms(contract.expiry_date)
        self._ledger.append_locked(
            records,
            order=_settlement_order(
                self.broker_id, settlement_client_order_id(symbol), symbol=symbol, quantity=float(contracts),
                price=0.0, happened_at_ms=close_ms, observed_at_ms=at_ms,
            ),
            settlement=SynthesizedOptionSettlement(
                contract=contract, outcome=SimulatedOutcome.ENDED_BY_OWNER, contracts=contracts,
                multiplier=multiplier, last_session_close_ms=close_ms, settled_at_ms=at_ms,
                rule=SIMULATED_OWNER_ENDING_RULE, booked_price=Decimal(0), cash_cents=0, owner=word,
                why_no_settlement=why,
            ),
        )
        logger.warning(
            "the simulation let go of an expired contract it had no price for, at the owner's word",
            extra={
                "action": "simulated_option_ended_by_owner", "account_id": self._account_id, "symbol": symbol,
                "contracts": contracts, "operator": word.operator,
            },
        )


