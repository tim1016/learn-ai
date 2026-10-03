"""Broker I/O fenced by one exclusive, renewable operation-claim token."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.broker.alpaca.clerk.sqlite.account_open_work import (
    AccountOpenWork,
    read_open_orders_then_positions,
)
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_uncertain
from app.broker.alpaca.clerk.sqlite.repository import (
    ClerkSqliteRepository,
    ExecutionLeaseLost,
    ExecutionLeaseLostAfterBrokerIO,
    OperationClaimError,
)
from app.broker.contract.errors import BrokerError
from app.broker.contract.models import BrokerActivityEvidence, BrokerOrder, BrokerOrderLeg
from app.broker.contract.ports import BrokerActivityEvidencePort, BrokerReadPort, BrokerTradePort


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

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        self._renew()
        try:
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
        self._renew()
        try:
            observed = await self.trade.get_order_by_client_order_id(client_order_id)
        except BrokerError:
            self._renew()
            raise
        self._renew()
        return observed

    async def observe_broker_order(
        self, broker_order_id: str
    ) -> BrokerOrder | BrokerError | None:
        """Exact evidence for one broker order id, or a value-domain error (#2656).

        The broker-id twin of :meth:`observe_exact`, with the same shape so a
        caller folds a failed read as uncertainty instead of letting it
        escape: it follows a manual order's Alpaca replacement chain, whose
        later members carry no client order id of ours. An answer naming
        another order is an error, never evidence.
        """
        self._renew()
        try:
            observed = await self.trade.get_order_by_broker_order_id(broker_order_id)
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
    ) -> AccountOpenWork | BrokerError:
        """The account's open orders, then its positions, or a value-domain error (#2839).

        Read under the claim like every other broker contact of a resolution,
        with :meth:`observe_activity_evidence`'s shape: a failed read is the
        caller's to refuse on and never escapes the pass.
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
