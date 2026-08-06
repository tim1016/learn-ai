"""Broker I/O fenced by one exclusive, renewable operation-claim token."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.broker.alpaca.clerk.sqlite.order_evidence import fold_uncertain
from app.broker.alpaca.clerk.sqlite.repository import (
    ClerkSqliteRepository,
    OperationClaimError,
)
from app.broker.contract.errors import BrokerError
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg
from app.broker.contract.ports import BrokerTradePort


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
        self._renew()
        return observed

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
        self._renew()

    async def lookup(self, client_order_id: str) -> BrokerOrder | None:
        self._renew()
        try:
            observed = await self.trade.get_order_by_client_order_id(client_order_id)
        except BrokerError:
            self._renew()
            raise
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
