"""Broker I/O fenced by one exclusive, renewable operation-claim token.

An order in an option contract (ADR 0076) is sent, and looked up, through
the option port. On a real account that port reads strictly, and here every
answer it gives is held to the contract, the side and the contracts the
books asked for before it becomes the order evidence the folds take
(``option_order_evidence``). An answer that cannot be read, or is about
something else, raises a ``BrokerError`` that is not a refusal: the outcome
stays unknown. A simulation's own record of the order is read as it always
was, by the lookup of its ledger.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from app.broker.alpaca.clerk.sqlite.account_open_work import (
    OpenOrdersThenPositions,
    read_open_orders_then_positions,
)
from app.broker.alpaca.clerk.sqlite.option_order_evidence import (
    AskedOptionOrder,
    asked_option_order,
    evidence_of_the_order_asked,
)
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_uncertain, trade_port_folds_simulated_evidence
from app.broker.alpaca.clerk.sqlite.repository import (
    ClerkSqliteRepository,
    ExecutionLeaseLost,
    ExecutionLeaseLostAfterBrokerIO,
    OperationClaimError,
)
from app.broker.contract.errors import BrokerError, BrokerEvidenceUnavailable
from app.broker.contract.models import BrokerActivityEvidence, BrokerOrder, BrokerOrderLeg
from app.broker.contract.options import SingleOptionOrderRequest
from app.broker.contract.ports import BrokerActivityEvidencePort, BrokerReadPort, BrokerTradePort
from app.options.quotes import HeldQuote


@dataclass(frozen=True)
class ClaimedBrokerIO:
    """Renew before/after I/O so a stale attempt cannot fold its response."""

    repo: ClerkSqliteRepository
    effect_operation_id: str
    claim_token: str
    trade: BrokerTradePort

    def _renew(self) -> None:
        if not self.repo.renew_operation_claim(
            effect_operation_id=self.effect_operation_id,
            token=self.claim_token,
        ):
            raise OperationClaimError(
                f"effect_operation {self.effect_operation_id!r} "
                "lost its broker-contact claim"
            )

    def _renew_after_io(self) -> None:
        """The broker already acted. A lease lost here is not "nothing
        applied": the response cannot be folded (folding is itself a
        mutation the lost lease refuses), so the caller must report the
        outcome unknown rather than release its key and invite a retry."""
        try:
            self._renew()
        except ExecutionLeaseLost as lost:
            raise ExecutionLeaseLostAfterBrokerIO(str(lost), account_id=lost.account_id) from lost

    async def submit(self, leg: BrokerOrderLeg | SingleOptionOrderRequest, *, client_order_id: str) -> BrokerOrder:
        """Send one instruction once: a leg in shares, or one option order (ADR 0076).

        Either answer comes back as the order evidence every fold takes.
        """
        self._renew()
        try:
            if isinstance(leg, SingleOptionOrderRequest):
                observed = await self._submit_option_order(leg, client_order_id=client_order_id)
            else:
                observed = await self.trade.submit(leg, client_order_id=client_order_id)
        except asyncio.CancelledError:
            self._renew()
            fold_uncertain(
                self.repo,
                effect_operation_id=self.effect_operation_id,
                order_ref=client_order_id,
                why="Broker submission was cancelled while its transport may still complete.",
            )
            raise
        except BrokerError:
            self._renew()
            raise
        self._renew_after_io()
        return observed

    async def _submit_option_order(self, order: SingleOptionOrderRequest, *, client_order_id: str) -> BrokerOrder:
        """Send one option order and return the broker's answer as order evidence.

        To a real account it is sent once, through the option port. The
        answer is evidence only when it is about the order sent: the same
        contract, side and contracts. Otherwise it raises as evidence that
        is unavailable, which the caller folds as an unknown outcome and
        settles by the exact lookup, never as a refusal: the broker may hold
        the order.

        A simulation executes inside the call. Its own record of the order
        is then read by the exact lookup every resolution uses, and that
        record is what the Clerk folds: its symbol is the contract's, and its
        fill carries the simulation's own execution identity. An answer with
        no record behind it leaves the outcome unknown, to be settled by the
        same lookup.
        """
        answer = await self.trade.submit_option_order(order, client_order_id=client_order_id)
        if not trade_port_folds_simulated_evidence(self.trade):
            return evidence_of_the_order_asked(answer, asked=AskedOptionOrder.of_request(order))
        recorded = await self.trade.get_order_by_client_order_id(client_order_id)
        if recorded is None or recorded.order_id != answer.order_id:
            raise BrokerEvidenceUnavailable(
                "The simulation answered an option order it holds no record of.", broker=self.trade.broker_id
            )
        return recorded

    def _asked_of_a_real_option_order(self, order_ref: str) -> AskedOptionOrder | None:
        """What the books asked a real broker for under ``order_ref``, when it is an order in an option contract.

        ``None`` for an order in shares, and for every order of a simulation,
        whose ledger is read by the one lookup it has.
        """
        if trade_port_folds_simulated_evidence(self.trade):
            return None
        return asked_option_order(self.repo, order_ref)

    def bind_option_close_quotes(
        self,
        client_order_id: str,
        *,
        symbol: str,
        side: Literal["buy", "sell"],
        quotes: Sequence[HeldQuote],
        usable_generation: int | None,
    ) -> bool:
        """Bind a simulated option close to the newest quote kept for its contract, under the operation lease.

        Only a simulation prices a close from a quote it was handed; any
        other port refuses, so such a close never reaches a real broker.
        """
        self._renew()
        bind = getattr(self.trade, "bind_option_close_quotes", None)
        if not callable(bind):
            return False
        bound = bool(bind(client_order_id, symbol=symbol, side=side, quotes=quotes, usable_generation=usable_generation))
        self._renew()
        return bound

    def bind_latest_recovery_bar(self, client_order_id: str, *, symbol: str, side: str) -> bool:
        """Bind optional no-submit recovery evidence under the operation lease."""
        self._renew()
        bind = getattr(self.trade, "bind_latest_recovery_bar", None)
        if not callable(bind):
            return True
        bound = bool(bind(client_order_id, symbol=symbol, side=side))
        self._renew()
        return bound

    def bind_run_end_close_bar(self, client_order_id: str, *, symbol: str) -> bool:
        """Bind a Dry Run's run-end close price under the operation lease.

        Only a simulation can close at a price it already saw; any other port
        refuses, so a run-end close never reaches a real broker.
        """
        self._renew()
        bind = getattr(self.trade, "bind_run_end_close_bar", None)
        if not callable(bind):
            return False
        bound = bool(bind(client_order_id, symbol=symbol))
        self._renew()
        return bound

    async def cancel(self, broker_order_id: str, *, order_ref: str) -> None:
        self._renew()
        try:
            await self.trade.cancel(broker_order_id)
        except asyncio.CancelledError:
            self._renew()
            fold_uncertain(
                self.repo,
                effect_operation_id=self.effect_operation_id,
                order_ref=order_ref,
                why="Broker cancellation was cancelled while its transport may still complete.",
                transition_kind="ORDER_CANCEL_UNCERTAIN",
            )
            raise
        except BrokerError:
            self._renew()
            raise
        self._renew_after_io()

    async def lookup(self, client_order_id: str) -> BrokerOrder | None:
        """The broker's own record of the order sent under ``client_order_id``, or ``None`` when it has none.

        An order in an option contract on a real account is read by the
        option port's strict lookup: an answer it cannot read whole, or one
        about another order, raises and leaves the outcome unknown.
        """
        self._renew()
        try:
            asked = self._asked_of_a_real_option_order(client_order_id)
            if asked is None:
                observed = await self.trade.get_order_by_client_order_id(client_order_id)
            else:
                answer = await self.trade.get_option_order_by_client_order_id(client_order_id)
                observed = None if answer is None else evidence_of_the_order_asked(answer, asked=asked)
        except BrokerError:
            self._renew()
            raise
        self._renew()
        return observed

    async def observe_broker_order(
        self, broker_order_id: str, *, order_ref: str | None = None
    ) -> BrokerOrder | BrokerError | None:
        """Exact evidence for one broker order id, or a value-domain error (#2656).

        The broker-id twin of :meth:`observe_exact`, with the same shape so a
        caller folds a failed read as uncertainty instead of letting it
        escape: it follows a manual order's Alpaca replacement chain, whose
        later members carry no client order id of ours. An answer naming
        another order is an error, never evidence.

        ``order_ref`` names the Clerk's own order the id was recorded for,
        when the caller knows it. An order in an option contract is then read
        by the option port, as :meth:`lookup` reads it.
        """
        self._renew()
        try:
            asked = None if order_ref is None else self._asked_of_a_real_option_order(order_ref)
            if asked is None:
                observed = await self.trade.get_order_by_broker_order_id(broker_order_id)
            else:
                answer = await self.trade.get_option_order_by_broker_order_id(broker_order_id)
                observed = None if answer is None else evidence_of_the_order_asked(answer, asked=asked)
        except BrokerError as exc:
            self._renew()
            return exc
        self._renew()
        if observed is not None and observed.order_id != broker_order_id:
            return BrokerError(
                f"broker returned order_id={observed.order_id!r}, expected {broker_order_id!r}"
            )
        return observed

    async def observe_activity_evidence(
        self,
        read: BrokerActivityEvidencePort,
        *,
        after_ms: int,
        activity_type: str,
        page_token: str | None = None,
    ) -> BrokerActivityEvidence | BrokerError:
        """One bounded read of the account's ``activity_type`` rows since ``after_ms``, or a value-domain error (#2686).

        Read under the claim like every other broker contact of a resolution,
        with :meth:`observe_broker_order`'s shape, so a failed read is the
        caller's to contain and never escapes the pass. The answer says
        whether it proved the window complete; ``page_token`` resumes an
        earlier read's ``next_page_token``.
        """
        self._renew()
        try:
            evidence = await read.read_activity_evidence(
                page_token=page_token, after_ms=after_ms, activity_type=activity_type
            )
        except BrokerError as exc:
            self._renew()
            return exc
        self._renew()
        return evidence

    async def observe_open_orders_then_positions(
        self, read: BrokerReadPort
    ) -> OpenOrdersThenPositions | BrokerError:
        """The account's open orders, then its positions, or a value-domain error (#2839).

        Read under the claim like every other broker contact of a resolution,
        with :meth:`observe_activity_evidence`'s shape: a failed read is the
        caller's to refuse on and never escapes the pass. The error alone
        means the open orders were not read; a positions read that failed
        after them is the error beside the orders.
        """
        self._renew()
        try:
            observed = await read_open_orders_then_positions(read)
        except BrokerError as exc:
            self._renew()
            return exc
        self._renew()
        return observed

    async def observe_exact(
        self, client_order_id: str
    ) -> BrokerOrder | BrokerError | None:
        """Return exact evidence or a value-domain error for uncertainty folding."""
        try:
            observed = await self.lookup(client_order_id)
        except BrokerError as exc:
            return exc
        if observed is not None and observed.client_order_id != client_order_id:
            return BrokerError(
                f"broker returned client_order_id={observed.client_order_id!r}, "
                f"expected {client_order_id!r}"
            )
        return observed


__all__ = ["ClaimedBrokerIO"]
