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
  differs is about something the books did not ask for, so it states no
  outcome: the order's outcome stays unknown, to be asked again.

Nothing here is rounded. A count of contracts is a whole number in both
shapes. A price is the broker's exact decimal, written as the float the
books keep every price as.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.broker.alpaca.clerk.sqlite.contract_terms import order_contract_terms
from app.broker.alpaca.clerk.sqlite.facts import EnterAcceptedFacts, ExitReducingOrderCreatedFacts
from app.broker.contract.errors import BrokerEvidenceUnavailable
from app.broker.contract.models import BrokerOrder
from app.broker.contract.options import BrokerOptionOrder, SingleOptionOrderRequest

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository

_OPTION_ASSET_CLASS = "us_option"


@dataclass(frozen=True, slots=True)
class AskedOptionOrder:
    """What the books asked the broker for under one order reference."""

    # The contract's OCC symbol.
    symbol: str
    # ``buy`` or ``sell``.
    side: str
    contracts: int

    @classmethod
    def of_request(cls, order: SingleOptionOrderRequest) -> AskedOptionOrder:
        return cls(symbol=order.contract.occ_symbol, side=order.side.value, contracts=order.quantity)


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
        leg = EnterAcceptedFacts.from_facts_json(accepted["facts_json"]).leg
        return AskedOptionOrder(symbol=leg["symbol"].upper(), side=leg["side"].lower(), contracts=int(leg["quantity"]))
    if created is not None:
        reducing = ExitReducingOrderCreatedFacts.from_facts_json(created["facts_json"])
        return AskedOptionOrder(
            symbol=reducing.symbol.upper(), side=reducing.side.lower(), contracts=int(reducing.quantity)
        )
    raise AssertionError(f"order {order_ref!r} is bound to a contract and no record says what it asked for")


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
    """
    (evidence,) = option_order_evidence(answer)
    answered = AskedOptionOrder(symbol=evidence.symbol.upper(), side=evidence.side.lower(), contracts=answer.quantity)
    if answered != asked:
        raise BrokerEvidenceUnavailable(
            "The broker answered about a different option order from the one the account Clerk asked for.",
            broker=answer.broker,
            detail=(
                f"Asked to {asked.side} {asked.contracts} of {asked.symbol}; the answer is to "
                f"{answered.side} {answered.contracts} of {answered.symbol}."
            ),
        )
    return evidence


__all__ = [
    "AskedOptionOrder",
    "asked_option_order",
    "evidence_of_the_order_asked",
    "option_order_evidence",
]
