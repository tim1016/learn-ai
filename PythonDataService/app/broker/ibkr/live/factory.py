"""Select live delivery once, when the IBKR client is constructed."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.broker.ibkr.live.source import LiveSource

if TYPE_CHECKING:
    from app.broker.ibkr.client import IbkrClient


def create_live_source(client: IbkrClient) -> LiveSource:
    if client.settings.shared_feed_url is not None:
        from app.broker.ibkr.contracts import qualify_underlying
        from app.broker.ibkr.live.connection import SharedConnection
        from app.broker.ibkr.live.protocol import Instrument

        async def qualify(symbol: str) -> Instrument:
            contract = await qualify_underlying(client, symbol)
            return Instrument(
                con_id=int(contract.conId),
                exchange=contract.exchange,
                currency=contract.currency,
                sec_type=contract.secType,
            )

        return SharedConnection(client.settings, qualify=qualify)
    from app.broker.ibkr.live.local import NativeLiveSource

    return NativeLiveSource(client)
