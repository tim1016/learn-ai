"""A broker's answer about an option order, as the order evidence the Clerk's folds take (ADR 0076).

The option port answers with a ``BrokerOptionOrder``: whole contracts and
exact decimals, read strictly, so an answer it could not read whole never
got this far. The books and every fold that keeps them take a
``BrokerOrder``, the shape a stock order's evidence has always had. This
module is the one conversion between the two, and the one place an answer
is held to the order the books asked for.

- ``option_order_evidence`` turns the answer about an order of one contract
  into its evidence. It returns a tuple of one, and refuses a multi-leg
  parent, which is not one order's evidence.
- ``multi_leg_order_evidence`` turns the answer about one order of several
  legs into a ``MultiLegOrderEvidence``: the parent's own state, with each
  leg's evidence nested under it, so it travels through every caller as the
  order evidence it is and is folded leg by leg (``multi_leg_evidence``).
- ``asked_option_order`` reads what the books asked for under an order
  reference: the contract, the side and the contracts of an order of one
  contract, or the units and each leg's contract and side of an order of
  several legs.
- ``evidence_of_the_order_asked`` is both: the answer's evidence, once it
  names that contract, that side and that many contracts. An answer that
  differs in one of those is about something the books did not ask for, so
  it states no outcome: the order's outcome stays unknown, to be asked
  again. An answer that states another kind of order or another limit price
  is still about our order, found by our own id: both values are logged and
  the evidence is taken, because what the order filled is real (ADR 0076
  decision 19).
- ``look_up_by_client_order_id`` and ``look_up_by_broker_order_id`` are the
  one place that chooses which reader reads a Clerk-owned order: the option
  port's strict reader for an order the books bind to a contract on a real
  account, the stock reader otherwise. Every exact lookup of the Clerk's own
  order goes through them: a resolution's, the pass's, and the operator's
  attestation.

Nothing here is rounded. A count of contracts is a whole number in both
shapes. A price is the broker's exact decimal, written as the float the
books keep every price as; a limit price is compared as the exact decimal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from app.broker.alpaca.clerk.sqlite.contract_terms import order_contract_terms
from app.broker.alpaca.clerk.sqlite.facts import EnterAcceptedFacts, ExitReducingOrderCreatedFacts
from app.broker.alpaca.clerk.sqlite.option_entry import MultiLegOptionEntryFacts, OptionEntryFacts, option_entry_facts
from app.broker.alpaca.clerk.sqlite.option_order_legs import is_multi_leg_parent, leg_parent, order_legs
from app.broker.alpaca.clerk.sqlite.order_evidence import trade_port_folds_simulated_evidence
from app.broker.alpaca.clerk.sqlite.order_projection import read_order_details
from app.broker.contract.errors import BrokerEvidenceUnavailable
from app.broker.contract.models import BrokerOrder
from app.broker.contract.options import BrokerOptionOrder, SingleOptionOrderRequest
from app.broker.contract.ports import BrokerTradePort

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository

logger = logging.getLogger(__name__)

_OPTION_ASSET_CLASS = "us_option"


@dataclass(frozen=True, slots=True)
class AskedOptionOrder:
    """What the books asked the broker for under one order reference."""

    # The contract's OCC symbol.
    symbol: str
    # ``buy`` or ``sell``.
    side: str
    contracts: int
    # ``limit`` or ``market``, and the limit as the exact decimal it was asked at; ``None`` for a market order.
    order_type: str
    limit_price: Decimal | None

    @classmethod
    def of_request(cls, order: SingleOptionOrderRequest) -> AskedOptionOrder:
        return cls(
            symbol=order.contract.occ_symbol, side=order.side.value, contracts=order.quantity,
            order_type=order.order_type.value, limit_price=order.limit_price,
        )

    def words(self) -> str:
        price = "at market" if self.limit_price is None else f"at a limit of {self.limit_price}"
        return f"{self.side} {self.contracts} of {self.symbol}, a {self.order_type} order {price}"

    def what_was_traded(self) -> tuple[str, str, int]:
        """The contract, the side and the contracts: what an answer must name to be about this order."""
        return (self.symbol, self.side, self.contracts)


@dataclass(frozen=True, slots=True)
class AskedMultiLegOrder:
    """What the books asked the broker for under the reference of one order of several legs: the parent's."""

    # Whole structures.
    units: int
    # (the contract's OCC symbol, ``buy`` or ``sell``), in the order's own leg order.
    legs: tuple[tuple[str, str], ...]
    # ``limit`` or ``market``, and the signed net limit as the exact decimal it was asked at.
    order_type: str
    net_limit_price: Decimal | None

    def words(self) -> str:
        price = "at market" if self.net_limit_price is None else f"at a net limit of {self.net_limit_price}"
        legs = ", ".join(f"{side} {symbol}" for symbol, side in self.legs)
        return f"{self.units} of ({legs}) as one order, a {self.order_type} order {price}"

    def what_was_traded(self) -> tuple[int, frozenset[tuple[str, str]]]:
        """The units, and each leg's contract and side: what an answer must name to be about this order.

        A leg's own quantity is not among them. What a leg says for more
        than one structure is not yet a recording, so no leg is held to its
        ratio times the units (ADR 0076 decision 10).
        """
        return (self.units, frozenset(self.legs))


def asked_option_order(repo: ClerkSqliteRepository, order_ref: str) -> AskedOptionOrder | AskedMultiLegOrder | None:
    """What the books asked for under ``order_ref``, or ``None`` for an order in shares.

    Three answers. An order the books bind to one contract asked for that
    contract: on the record that created it, an entry's acceptance or a
    reducing order's creation. The parent of an order of several legs is
    bound to no contract and is told by its legs: it asked for them, from
    the books' own rows of them (ADR 0076). Every other order is in shares.

    A leg's own row is a part of its parent's order and was never asked of
    a broker by itself: asking about one is the caller's mistake, and is
    refused by name.
    """
    with repo.write_fence() as conn:
        legs = order_legs(conn, order_ref)
        if legs:
            line = read_order_details(conn, (order_ref,))[order_ref]
            assert line.quantity is not None and line.order_type is not None, f"parent {order_ref!r} has no order line"
            return AskedMultiLegOrder(
                units=int(line.quantity),
                legs=tuple((leg.symbol, leg.side) for leg in legs),
                order_type=line.order_type,
                net_limit_price=_net_limit_asked(repo, order_ref),
            )
        if leg_parent(conn, order_ref) is not None:
            raise ValueError(
                f"{order_ref!r} is a leg of an order of several legs. A broker is asked about the order that was "
                "sent, its parent, never about a leg by the books' own reference."
            )
        if order_contract_terms(conn, order_ref) is None:
            return None
        accepted = repo.first_order_transition(order_ref=order_ref, transition_kind="ENTER_ACCEPTED")
        created = repo.first_order_transition(order_ref=order_ref, transition_kind="EXIT_REDUCING_ORDER_CREATED")
    if accepted is not None:
        entry = option_entry_facts(EnterAcceptedFacts.from_facts_json(accepted["facts_json"]))
        assert isinstance(entry, OptionEntryFacts), (
            f"entry {order_ref!r} is bound to a contract and was not accepted as an entry in one contract"
        )
        return AskedOptionOrder.of_request(entry.order)
    if created is not None:
        reducing = ExitReducingOrderCreatedFacts.from_facts_json(created["facts_json"])
        return AskedOptionOrder(
            symbol=reducing.symbol.upper(), side=reducing.side.lower(), contracts=int(reducing.quantity),
            order_type=reducing.order_type.lower(),
            # The books keep a reducing order's limit as a float of whole cents: its shortest
            # written form is the decimal it was asked at.
            limit_price=None if reducing.limit_price is None else Decimal(str(reducing.limit_price)),
        )
    raise AssertionError(f"order {order_ref!r} is bound to a contract and no record says what it asked for")


def _net_limit_asked(repo: ClerkSqliteRepository, parent_order_ref: str) -> Decimal | None:
    """The signed net limit the parent ``parent_order_ref`` was asked at, or ``None`` for a market order.

    An entry keeps it in its acceptance. A closing order of several legs is
    a market order and has none.
    """
    accepted = repo.first_order_transition(order_ref=parent_order_ref, transition_kind="ENTER_ACCEPTED")
    if accepted is None:
        return None
    entry = option_entry_facts(EnterAcceptedFacts.from_facts_json(accepted["facts_json"]))
    assert isinstance(entry, MultiLegOptionEntryFacts), f"parent {parent_order_ref!r} was not accepted as several legs"
    return entry.net_limit_price


def asked_of_a_real_option_order(
    repo: ClerkSqliteRepository, trade: BrokerTradePort, order_ref: str
) -> AskedOptionOrder | AskedMultiLegOrder | None:
    """What the books asked the option port for under ``order_ref``, when the option port is what answers about it.

    ``None`` for an order in shares. ``None`` too for an order in one
    contract on a simulation, whose ledger is read by the one lookup it has.

    The parent of an order of several legs is answered on every port, a
    simulation's included: a parent names no symbol, so only the option
    reader can state it, with its legs nested under it. No stock reader is
    ever asked about one.
    """
    if not trade_port_folds_simulated_evidence(trade):
        return asked_option_order(repo, order_ref)
    with repo.write_fence() as conn:
        on_a_simulation_only_a_parent = is_multi_leg_parent(conn, order_ref)
    return asked_option_order(repo, order_ref) if on_a_simulation_only_a_parent else None


async def _as_evidence(
    trade: BrokerTradePort, answer: BrokerOptionOrder, *, asked: AskedOptionOrder | AskedMultiLegOrder
) -> BrokerOrder:
    """``answer`` held to what was asked; a simulation's order of several legs with its own record of each leg."""
    evidence = evidence_of_the_order_asked(answer, asked=asked)
    if isinstance(evidence, MultiLegOrderEvidence) and trade_port_folds_simulated_evidence(trade):
        return await with_the_simulations_own_leg_records(trade, evidence)
    return evidence


async def look_up_by_client_order_id(
    trade: BrokerTradePort, client_order_id: str, *, asked: AskedOptionOrder | AskedMultiLegOrder | None
) -> BrokerOrder | None:
    """The broker's record of the order sent under ``client_order_id``, or ``None`` when it has none.

    ``asked`` is :func:`asked_of_a_real_option_order` for the Clerk's own
    order the lookup is about. With one, the option port's strict reader
    answers and the answer is held to it: one it cannot read whole, or one
    about another order, raises and leaves the outcome unknown. Without
    one the stock reader answers.
    """
    if asked is None:
        return await trade.get_order_by_client_order_id(client_order_id)
    answer = await trade.get_option_order_by_client_order_id(client_order_id)
    return None if answer is None else await _as_evidence(trade, answer, asked=asked)


async def look_up_by_broker_order_id(
    trade: BrokerTradePort,
    broker_order_id: str,
    *,
    asked: AskedOptionOrder | AskedMultiLegOrder | None,
    held_to_it: bool = True,
) -> BrokerOrder | None:
    """The broker's record of the order it holds under ``broker_order_id``: :func:`look_up_by_client_order_id`'s twin.

    ``held_to_it=False`` is for an id a person typed (the operator's
    attestation): the option reader still reads it, but an order that is not
    the one asked for comes back as what it is, for the caller to refuse in
    its own words, instead of raising as an answer nothing can be made of.
    """
    if asked is None:
        return await trade.get_order_by_broker_order_id(broker_order_id)
    answer = await trade.get_option_order_by_broker_order_id(broker_order_id)
    if answer is None:
        return None
    return await _as_evidence(trade, answer, asked=asked) if held_to_it else option_order_evidence(answer)[0]


class MultiLegOrderEvidence(BrokerOrder):
    """The answer about one option order of several legs, as order evidence: the parent's state, and each leg's.

    It is a ``BrokerOrder``, so no caller between the port and the folds
    changes type, and the folds hand it to the one fold that takes it apart
    (``multi_leg_evidence.fold_multi_leg_order_evidence``).

    The parent's own fields: ``symbol`` and ``side`` are empty, because a
    parent has neither. ``quantity`` is whole structures. ``filled_quantity``
    is zero and ``filled_avg_price`` is absent: a parent has no fill of its
    own, so nothing ever counts one against it. Its ``limit_price`` is
    absent too: a net price is not a price a share.
    """

    # Each leg's own evidence: its contract, side, broker order id, status, filled quantity and average price.
    legs: tuple[BrokerOrder, ...]
    # The parent's own count of whole structures filled.
    units_filled: int
    # Signed, as the broker states it: paid above zero, received below.
    net_filled_avg_price: Decimal | None


def multi_leg_order_evidence(answer: BrokerOptionOrder) -> MultiLegOrderEvidence:
    """``answer``, a multi-leg parent with its legs, as the order evidence the folds take."""
    if not answer.multi_leg:
        raise BrokerEvidenceUnavailable(
            "The broker answered with an option order of one contract where one of several legs was asked about.",
            broker=answer.broker,
        )
    when = {
        "submitted_at_ms": answer.submitted_at_ms, "created_at_ms": answer.created_at_ms,
        "updated_at_ms": answer.updated_at_ms, "filled_at_ms": answer.filled_at_ms,
        "canceled_at_ms": answer.canceled_at_ms, "expired_at_ms": answer.expired_at_ms,
    }
    return MultiLegOrderEvidence(
        broker=answer.broker,
        order_id=answer.order_id,
        client_order_id=answer.client_order_id,
        symbol="",
        asset_class=_OPTION_ASSET_CLASS,
        side="",
        order_type=answer.order_type,
        time_in_force=answer.time_in_force,
        quantity=float(answer.quantity),
        filled_quantity=0.0,
        limit_price=None,
        stop_price=None,
        filled_avg_price=None,
        status=answer.status,
        **when,
        observed_at_ms=answer.observed_at_ms,
        legs=tuple(
            BrokerOrder(
                broker=answer.broker,
                order_id=leg.order_id,
                client_order_id=leg.client_order_id,
                symbol=leg.contract.occ_symbol,
                asset_class=_OPTION_ASSET_CLASS,
                side=leg.side.value,
                order_type=answer.order_type,
                time_in_force=answer.time_in_force,
                quantity=None if leg.quantity is None else float(leg.quantity),
                filled_quantity=float(leg.filled_quantity),
                limit_price=None,
                stop_price=None,
                filled_avg_price=None if leg.filled_avg_price is None else float(leg.filled_avg_price),
                status=leg.status,
                submitted_at_ms=leg.submitted_at_ms,
                created_at_ms=leg.created_at_ms,
                updated_at_ms=leg.updated_at_ms,
                filled_at_ms=leg.filled_at_ms,
                canceled_at_ms=leg.canceled_at_ms,
                expired_at_ms=leg.expired_at_ms,
                observed_at_ms=answer.observed_at_ms,
            )
            for leg in answer.legs
        ),
        units_filled=answer.filled_quantity,
        net_filled_avg_price=answer.filled_avg_price,
    )


async def with_the_simulations_own_leg_records(
    trade: BrokerTradePort, evidence: MultiLegOrderEvidence
) -> MultiLegOrderEvidence:
    """``evidence`` from a simulation, with each leg's evidence read from the simulation's own record of that leg.

    A simulation keeps a record for each leg, under the leg's own client
    order id, and that record carries the simulation's execution identity:
    what the Clerk folds a simulated fill by. A leg with no record behind
    it leaves the outcome unknown.
    """
    legs = []
    for leg in evidence.legs:
        recorded = None if leg.client_order_id is None else await trade.get_order_by_client_order_id(leg.client_order_id)
        if recorded is None or recorded.order_id != leg.order_id:
            raise BrokerEvidenceUnavailable(
                "The simulation answered about a leg of an option order it holds no record of.",
                broker=evidence.broker,
                detail=f"Leg {leg.client_order_id!r} of order {evidence.client_order_id!r}.",
            )
        legs.append(recorded)
    return evidence.model_copy(update={"legs": tuple(legs)})


def option_order_evidence(order: BrokerOptionOrder) -> tuple[BrokerOrder, ...]:
    """The order evidence of the one order ``order`` speaks for: an order of one contract.

    A multi-leg parent is refused here: it is not one order's evidence. It
    is read by :func:`multi_leg_order_evidence`, for a caller that asked
    about an order of several legs.
    """
    if order.multi_leg or order.contract is None or order.side is None:
        raise BrokerEvidenceUnavailable(
            "The broker answered with a multi-leg option order where an order of one contract was asked about.",
            broker=order.broker,
            detail="Its legs are orders of their own, and the books asked about one contract.",
        )
    return (
        BrokerOrder(
            broker=order.broker,
            order_id=order.order_id,
            client_order_id=order.client_order_id,
            symbol=order.contract.occ_symbol,
            asset_class=_OPTION_ASSET_CLASS,
            side=order.side.value,
            order_type=order.order_type,
            time_in_force=order.time_in_force,
            quantity=float(order.quantity),
            filled_quantity=float(order.filled_quantity),
            limit_price=None if order.limit_price is None else float(order.limit_price),
            stop_price=None,
            filled_avg_price=None if order.filled_avg_price is None else float(order.filled_avg_price),
            status=order.status,
            submitted_at_ms=order.submitted_at_ms,
            created_at_ms=order.created_at_ms,
            updated_at_ms=order.updated_at_ms,
            filled_at_ms=order.filled_at_ms,
            canceled_at_ms=order.canceled_at_ms,
            expired_at_ms=order.expired_at_ms,
            observed_at_ms=order.observed_at_ms,
        ),
    )


def _evidence_of_the_multi_leg_order_asked(answer: BrokerOptionOrder, *, asked: AskedMultiLegOrder) -> MultiLegOrderEvidence:
    """A multi-leg parent's answer as order evidence, once it is about the order of several legs the books asked for.

    Held to the parent's units and to each leg's contract and side. A leg's
    own quantity is not held to its ratio times the units: what a leg says
    for more than one structure is not yet a recording (ADR 0076 decision
    10). An answer that states another kind of order or another net limit is
    taken and logged, as for an order of one contract; one that states no
    limit states nothing to differ from.
    """
    evidence = multi_leg_order_evidence(answer)
    answered = AskedMultiLegOrder(
        units=answer.quantity,
        legs=tuple((leg.contract.occ_symbol, leg.side.value) for leg in answer.legs),
        order_type=answer.order_type.lower(),
        net_limit_price=answer.limit_price,
    )
    if answered.what_was_traded() != asked.what_was_traded():
        raise BrokerEvidenceUnavailable(
            "The broker answered about a different option order from the one the account Clerk asked for.",
            broker=answer.broker,
            detail=f"Asked for {asked.words()}; the answer is for {answered.words()}.",
        )
    if answered.order_type != asked.order_type or (
        answered.net_limit_price is not None and answered.net_limit_price != asked.net_limit_price
    ):
        logger.warning(
            "The broker states another kind of order or another net limit than the account Clerk sent; its answer is taken.",
            extra={
                "action": "option_order_answer_states_other_terms",
                "broker": answer.broker,
                "client_order_id": answer.client_order_id,
                "broker_order_id": answer.order_id,
                "asked_order_type": asked.order_type,
                "answered_order_type": answered.order_type,
                "asked_limit_price": None if asked.net_limit_price is None else str(asked.net_limit_price),
                "answered_limit_price": None if answered.net_limit_price is None else str(answered.net_limit_price),
            },
        )
    return evidence


def evidence_of_the_order_asked(
    answer: BrokerOptionOrder, *, asked: AskedOptionOrder | AskedMultiLegOrder
) -> BrokerOrder:
    """``answer`` as order evidence, once it is about the order the books asked for.

    For an order of several legs the evidence is a ``MultiLegOrderEvidence``,
    held to the parent's units and to each leg's contract and side
    (:func:`_evidence_of_the_multi_leg_order_asked`). What follows is the
    rule for an order of one contract.

    Raises ``BrokerEvidenceUnavailable``, an unknown outcome, when the answer
    names another contract, another side or another number of contracts: a
    fill counted from it would be counted against the wrong order.

    An answer that states another kind of order or another limit price is
    taken, and both values are logged. It was found by our own order id, and
    what it filled is real: refusing it would leave a filled entry's bot
    with a close that is never sent (ADR 0076 decision 19). The limit is
    compared as the exact decimal, so ``1.9`` and ``1.90`` are one price.
    """
    if isinstance(asked, AskedMultiLegOrder):
        return _evidence_of_the_multi_leg_order_asked(answer, asked=asked)
    (evidence,) = option_order_evidence(answer)
    answered = AskedOptionOrder(
        symbol=evidence.symbol.upper(), side=evidence.side.lower(), contracts=answer.quantity,
        order_type=answer.order_type.lower(), limit_price=answer.limit_price,
    )
    if answered.what_was_traded() != asked.what_was_traded():
        raise BrokerEvidenceUnavailable(
            "The broker answered about a different option order from the one the account Clerk asked for.",
            broker=answer.broker,
            detail=f"Asked to {asked.words()}; the answer is to {answered.words()}.",
        )
    if answered != asked:
        logger.warning(
            "The broker states another kind of order or another limit than the account Clerk sent; its answer is taken.",
            extra={
                "action": "option_order_answer_states_other_terms",
                "broker": answer.broker,
                "client_order_id": answer.client_order_id,
                "broker_order_id": answer.order_id,
                "symbol": asked.symbol,
                "asked_order_type": asked.order_type,
                "answered_order_type": answered.order_type,
                "asked_limit_price": None if asked.limit_price is None else str(asked.limit_price),
                "answered_limit_price": None if answered.limit_price is None else str(answered.limit_price),
            },
        )
    return evidence


__all__ = [
    "AskedMultiLegOrder",
    "AskedOptionOrder",
    "MultiLegOrderEvidence",
    "asked_of_a_real_option_order",
    "asked_option_order",
    "evidence_of_the_order_asked",
    "look_up_by_broker_order_id",
    "look_up_by_client_order_id",
    "multi_leg_order_evidence",
    "option_order_evidence",
    "with_the_simulations_own_leg_records",
]
