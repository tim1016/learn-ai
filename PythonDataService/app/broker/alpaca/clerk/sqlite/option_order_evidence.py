"""A broker's answer about an option order, as the order evidence the Clerk's folds take (ADR 0076).

The option port answers with a ``BrokerOptionOrder``: whole contracts and
exact decimals, read strictly, so an answer it could not read whole never
got this far. The books and every fold that keeps them take a
``BrokerOrder``, the shape a stock order's evidence has always had. This
module is the one conversion between the two, and the one place an answer
is held to the order the books asked for.

- ``option_order_evidence`` turns one answer into the evidence of each order
  it speaks for. It returns a tuple: a single-leg order is one order, and a
  multi-leg parent will be one for each leg once the books can tell several
  orders of one entry apart. Until then a multi-leg answer is refused.
- ``asked_option_order`` reads what the books asked for under an order
  reference: the contract, the side and the contracts.
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
from app.broker.alpaca.clerk.sqlite.option_entry import option_entry_facts
from app.broker.alpaca.clerk.sqlite.order_evidence import trade_port_folds_simulated_evidence
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


def asked_option_order(repo: ClerkSqliteRepository, order_ref: str) -> AskedOptionOrder | None:
    """What the books asked for under ``order_ref``, or ``None`` for an order that is not in an option contract.

    An order is in an option contract when the books bind it to one. What it
    asked for is on the record that created it: an entry's acceptance, or a
    reducing order's creation.
    """
    with repo.write_fence() as conn:
        if order_contract_terms(conn, order_ref) is None:
            return None
        accepted = repo.first_order_transition(order_ref=order_ref, transition_kind="ENTER_ACCEPTED")
        created = repo.first_order_transition(order_ref=order_ref, transition_kind="EXIT_REDUCING_ORDER_CREATED")
    if accepted is not None:
        entry = option_entry_facts(EnterAcceptedFacts.from_facts_json(accepted["facts_json"]))
        assert entry is not None, f"entry {order_ref!r} is bound to a contract and was not accepted as an option entry"
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


def asked_of_a_real_option_order(
    repo: ClerkSqliteRepository, trade: BrokerTradePort, order_ref: str
) -> AskedOptionOrder | None:
    """What the books asked a real broker for under ``order_ref``, when it is an order in an option contract.

    ``None`` for an order in shares, and for every order of a simulation,
    whose ledger is read by the one lookup it has.
    """
    if trade_port_folds_simulated_evidence(trade):
        return None
    return asked_option_order(repo, order_ref)


async def look_up_by_client_order_id(
    trade: BrokerTradePort, client_order_id: str, *, asked: AskedOptionOrder | None
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
    return None if answer is None else evidence_of_the_order_asked(answer, asked=asked)


async def look_up_by_broker_order_id(
    trade: BrokerTradePort, broker_order_id: str, *, asked: AskedOptionOrder | None, held_to_it: bool = True
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
    return evidence_of_the_order_asked(answer, asked=asked) if held_to_it else option_order_evidence(answer)[0]


def option_order_evidence(order: BrokerOptionOrder) -> tuple[BrokerOrder, ...]:
    """The order evidence of each order ``order`` speaks for: one, for a single-leg order.

    A multi-leg parent is refused as evidence nothing can be made of yet:
    its legs fill as orders of their own, which the books do not keep apart.
    """
    if order.multi_leg or order.contract is None or order.side is None:
        raise BrokerEvidenceUnavailable(
            "The broker answered with a multi-leg option order, which the account Clerk does not take yet.",
            broker=order.broker,
            detail="Its legs are orders of their own, and the books keep one order for one entry.",
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


def evidence_of_the_order_asked(answer: BrokerOptionOrder, *, asked: AskedOptionOrder) -> BrokerOrder:
    """``answer`` as order evidence, once it is about the order the books asked for.

    Raises ``BrokerEvidenceUnavailable``, an unknown outcome, when the answer
    names another contract, another side or another number of contracts: a
    fill counted from it would be counted against the wrong order.

    An answer that states another kind of order or another limit price is
    taken, and both values are logged. It was found by our own order id, and
    what it filled is real: refusing it would leave a filled entry's bot
    with a close that is never sent (ADR 0076 decision 19). The limit is
    compared as the exact decimal, so ``1.9`` and ``1.90`` are one price.
    """
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
    "AskedOptionOrder",
    "asked_of_a_real_option_order",
    "asked_option_order",
    "evidence_of_the_order_asked",
    "look_up_by_broker_order_id",
    "look_up_by_client_order_id",
    "option_order_evidence",
]
