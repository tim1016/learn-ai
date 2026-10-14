"""Broker I/O fenced by one exclusive, renewable operation-claim token.

An order in an option contract (ADR 0076) is sent, and looked up, through
the option port. On a real account that port reads strictly, and here every
answer it gives is held to the contract, the side and the contracts the
books asked for before it becomes the order evidence the folds take
(``option_order_evidence``). An answer that cannot be read, or is about
something else, raises a ``BrokerError`` that is not a refusal: the outcome
stays unknown. A simulation's own record of an order of one contract is read
as it always was, by the lookup of its ledger.

One order of several legs is sent once, under its parent's client order id,
and is looked up through the option port on every port, a simulation's
included: a parent names no symbol, so no stock reader can state it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from app.broker.alpaca.clerk.sqlite.account_open_work import (
    OpenOrdersThenPositions,
    read_open_orders_then_positions,
)
from app.broker.alpaca.clerk.sqlite.option_order_evidence import (
    AskedMultiLegOrder,
    AskedOptionOrder,
    MultiLegOrderEvidence,
    asked_of_a_real_option_order,
    evidence_of_the_order_asked,
    look_up_by_broker_order_id,
    look_up_by_client_order_id,
    with_the_simulations_own_leg_records,
)
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_uncertain, trade_port_folds_simulated_evidence
from app.broker.alpaca.clerk.sqlite.repository import (
    ClaimLostAfterBrokerIO,
    ClerkSqliteRepository,
    ExecutionLeaseLost,
    ExecutionLeaseLostAfterBrokerIO,
    OperationClaimError,
)
from app.broker.alpaca.send_timing import ORDER_SEND_CLAIM_TTL_MS
from app.broker.contract.errors import BrokerError, BrokerEvidenceUnavailable
from app.broker.contract.models import BrokerActivityEvidence, BrokerOrder, BrokerOrderLeg
from app.broker.contract.options import MultiLegOptionOrderRequest, OptionOrderRequest, SingleOptionOrderRequest
from app.broker.contract.ports import BrokerActivityEvidencePort, BrokerReadPort, BrokerTradePort
from app.options.quotes import HeldQuote

_SEND_CLAIM_RENEWAL_S = ORDER_SEND_CLAIM_TTL_MS / 4_000
_SEND_CLAIM_RETRY_S = 1.0
logger = logging.getLogger(__name__)


def _lost_after_broker_io(
    lost: ExecutionLeaseLost | OperationClaimError, message: str
) -> ExecutionLeaseLostAfterBrokerIO | ClaimLostAfterBrokerIO:
    """The error for a lease, or a claim, found lost once a broker call was made.

    A lease loss stays a lease loss. A claim loss stays an
    ``OperationClaimError``: the sweep defers such an effect and moves on to
    the next, and it is no reason to fail the pass.
    """
    if isinstance(lost, ExecutionLeaseLost):
        return ExecutionLeaseLostAfterBrokerIO(message, account_id=lost.account_id)
    return ClaimLostAfterBrokerIO(message)


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
        """A broker call was made. A lease or a claim lost here is not
        "nothing applied": the response cannot be folded (folding is itself a
        mutation the lost lease refuses), so the caller must report the
        outcome unknown rather than release its key and invite a retry. A
        plain ``OperationClaimError`` would read as "another owner holds it,
        nothing was sent", which is only true of a claim never acquired."""
        try:
            self._renew()
        except (ExecutionLeaseLost, OperationClaimError) as lost:
            raise _lost_after_broker_io(lost, str(lost)) from lost

    def _fold_unknown_of_cancelled_call(
        self, *, order_ref: str, why: str, transition_kind: str = "ORDER_SUBMIT_UNCERTAIN"
    ) -> None:
        """Fold the unknown outcome of a call cancelled while its transport may still complete.

        The caller re-raises its ``CancelledError``. A lease or claim already
        gone refuses the fold, and that refusal must not replace the
        cancellation: a task whose cancel surfaces as another error is not
        cancelled, and wedges the task group above it. The order's effect
        stays open, so the reconciliation sweep's lookup settles it.
        """
        try:
            self._renew()
            fold_uncertain(
                self.repo,
                effect_operation_id=self.effect_operation_id,
                order_ref=order_ref,
                why=why,
                transition_kind=transition_kind,
            )
        except (ExecutionLeaseLost, OperationClaimError):
            logger.error(
                "alpaca broker call was cancelled after its lease or operation claim was lost; "
                "its unknown outcome is not folded",
                extra={
                    "action": "claimed_io_cancelled_unfolded",
                    "effect_operation_id": self.effect_operation_id,
                    "order_ref": order_ref,
                },
                exc_info=True,
            )

    async def submit(self, leg: BrokerOrderLeg | OptionOrderRequest, *, client_order_id: str) -> BrokerOrder:
        """Send one instruction once: a leg in shares, or one option order, of one contract or of several legs (ADR 0076).

        Each answer comes back as the order evidence every fold takes. An
        order of several legs comes back as a ``MultiLegOrderEvidence``: the
        parent's state, with each leg's evidence under it.
        """
        self._renew()
        finished = asyncio.Event()

        async def keep_claim() -> ExecutionLeaseLost | OperationClaimError | None:
            delay = _SEND_CLAIM_RENEWAL_S
            while not finished.is_set():
                try:
                    await asyncio.wait_for(finished.wait(), timeout=delay)
                except TimeoutError:
                    try:
                        self._renew()
                    except (ExecutionLeaseLost, OperationClaimError) as exc:
                        logger.critical(
                            "alpaca order send lost its operation claim while broker I/O was active",
                            extra={"effect_operation_id": self.effect_operation_id},
                            exc_info=True,
                        )
                        return exc
                    except Exception:
                        logger.exception(
                            "alpaca order send claim renewal failed; retrying while broker I/O remains active",
                            extra={"effect_operation_id": self.effect_operation_id},
                        )
                        delay = _SEND_CLAIM_RETRY_S
                    else:
                        delay = _SEND_CLAIM_RENEWAL_S
            return None

        renewal = asyncio.create_task(keep_claim())
        cancelled = False
        try:
            try:
                if isinstance(leg, MultiLegOptionOrderRequest):
                    observed = await self._submit_multi_leg_order(leg, client_order_id=client_order_id)
                elif isinstance(leg, SingleOptionOrderRequest):
                    observed = await self._submit_option_order(leg, client_order_id=client_order_id)
                else:
                    observed = await self.trade.submit(leg, client_order_id=client_order_id)
            except asyncio.CancelledError:
                cancelled = True
                self._fold_unknown_of_cancelled_call(
                    order_ref=client_order_id,
                    why="Broker submission was cancelled while its transport may still complete.",
                )
                raise
            except BrokerError:
                # The call was made, and a transport error leaves its outcome unknown.
                self._renew_after_io()
                raise
            self._renew_after_io()
            return observed
        finally:
            finished.set()
            claim_loss = await renewal
            # A cancelled send stays a cancellation; its lost claim is logged by the renewal and the cancel handler.
            if claim_loss is not None and not cancelled:
                raise _lost_after_broker_io(
                    claim_loss,
                    f"effect_operation {self.effect_operation_id!r} lost its claim during broker submission",
                ) from claim_loss

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

    async def _submit_multi_leg_order(self, order: MultiLegOptionOrderRequest, *, client_order_id: str) -> BrokerOrder:
        """Send one order of several legs once, under the parent's client order id, and return its answer as evidence.

        The answer is evidence only when it is about the order sent: the
        parent's units and each leg's contract and side. A leg's own
        quantity is not held to its ratio times the units (ADR 0076 decision
        10). An answer about something else raises as evidence that is
        unavailable: an unknown outcome, settled by the exact lookup, never
        a refusal, because the broker may hold the order.

        A simulation executes inside the call, every leg or none. Each
        leg's evidence is then the simulation's own record of that leg, read
        by the leg's client order id, so it carries the simulation's
        execution identity.
        """
        answer = await self.trade.submit_option_order(order, client_order_id=client_order_id)
        evidence = evidence_of_the_order_asked(
            answer,
            asked=AskedMultiLegOrder(
                units=order.quantity,
                legs=tuple((leg.contract.occ_symbol, leg.side.value) for leg in order.legs),
                order_type=order.order_type.value,
                net_limit_price=order.net_limit_price,
            ),
        )
        assert isinstance(evidence, MultiLegOrderEvidence)
        if trade_port_folds_simulated_evidence(self.trade):
            return await with_the_simulations_own_leg_records(self.trade, evidence)
        return evidence

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

        Only a simulation prices a close from a quote it was handed, and
        any other port answers that it bound none. A real account's close is
        a market order the broker fills: its caller sends it with no quote
        and never asks this.
        """
        self._renew()
        bind = getattr(self.trade, "bind_option_close_quotes", None)
        if not callable(bind):
            return False
        bound = bool(bind(client_order_id, symbol=symbol, side=side, quotes=quotes, usable_generation=usable_generation))
        self._renew()
        return bound

    def bind_latest_recovery_bar(
        self, client_order_id: str, *, symbol: str, side: str, extended_hours: bool = False
    ) -> bool:
        """Bind optional no-submit recovery evidence under the operation lease."""
        self._renew()
        bind = getattr(self.trade, "bind_latest_recovery_bar", None)
        if not callable(bind):
            return True
        bound = bool(bind(client_order_id, symbol=symbol, side=side, extended_hours=extended_hours))
        self._renew()
        return bound

    def bind_decision_bar(
        self, client_order_id: str, *, bar_identity: str, decided_at_ms: int, symbol: str
    ) -> bool:
        """Rebind a decision's own pricing bar under the operation lease (#2919)."""
        self._renew()
        bind = getattr(self.trade, "bind_decision_bar", None)
        if not callable(bind):
            return True
        bound = bool(bind(client_order_id, bar_identity=bar_identity, decided_at_ms=decided_at_ms, symbol=symbol))
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
            self._fold_unknown_of_cancelled_call(
                order_ref=order_ref,
                why="Broker cancellation was cancelled while its transport may still complete.",
                transition_kind="ORDER_CANCEL_UNCERTAIN",
            )
            raise
        except BrokerError:
            # The call was made, and a transport error leaves its outcome unknown.
            self._renew_after_io()
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
            asked = asked_of_a_real_option_order(self.repo, self.trade, client_order_id)
            observed = await look_up_by_client_order_id(self.trade, client_order_id, asked=asked)
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
            asked = None if order_ref is None else asked_of_a_real_option_order(self.repo, self.trade, order_ref)
            observed = await look_up_by_broker_order_id(self.trade, broker_order_id, asked=asked)
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
