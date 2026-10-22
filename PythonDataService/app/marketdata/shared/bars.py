"""Shared raw delivery into the existing per-consumer minute assembler."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from app.broker.ibkr.bars import (
    IBKRBarInterrupted,
    IBKRBarRequestDeadlineExceeded,
    IBKRBarSubscriptionStalled,
    _LeasedBar,
)
from app.broker.ibkr.contracts import qualify_underlying
from app.broker.ibkr.minute_assembler import IBKRBarStreamError, _session_phase_for_ms
from app.marketdata.shared.protocol import FeedUnavailable, Instrument, Subscription
from app.utils.timestamps import now_ms_utc


async def shared_raw_bars(
    client: Any,
    symbol: str,
    *,
    use_rth: bool,
    request_deadline_ms: int | None,
    stall_deferred: Callable[[], bool] | None = None,
) -> AsyncIterator[_LeasedBar | None]:
    shared = client.shared_feed
    symbol = symbol.upper()
    identity = shared.underlyings.get(symbol)
    if identity is None:
        lookup_timeout = 5 if request_deadline_ms is None else max(0, (request_deadline_ms - now_ms_utc()) / 1000)
        try:
            contract = await asyncio.wait_for(qualify_underlying(client, symbol), timeout=lookup_timeout)
        except TimeoutError as exc:
            raise IBKRBarRequestDeadlineExceeded("Shared source contract lookup exceeded its deadline") from exc
        identity = Instrument(
            con_id=int(contract.conId),
            exchange=contract.exchange,
            currency=contract.currency,
            sec_type=contract.secType,
        )
        if len(shared.underlyings) >= 256:
            evicted = next(iter(shared.underlyings))
            shared.underlyings.pop(evicted)
            shared.bar_receipts.pop(evicted, None)
        shared.underlyings[symbol] = identity
    key = Subscription(kind="bars", instrument=identity)
    timeout = None if request_deadline_ms is None else max(0, (request_deadline_ms - now_ms_utc()) / 1000)
    first = True
    try:
        async with aclosing(shared.stream(key)) as frames:
            while True:
                # Only acquisition is bounded by the request deadline; once a
                # source answers, the existing continuity/decision clocks rule.
                if first and timeout is not None:
                    frame = await asyncio.wait_for(anext(frames), timeout=timeout)
                else:
                    frame = await anext(frames)
                first = False
                if frame.kind == "error":
                    if frame.cause == "stalled":
                        # The shared watchdog retires the physical line. Each
                        # assembler still owes its own sparse-minute emit (#2376).
                        while stall_deferred is not None and stall_deferred():
                            if not shared.is_connected():
                                raise IBKRBarInterrupted(
                                    "Shared source disconnected during minute emission", cause="socket_down"
                                )
                            yield None
                            await asyncio.sleep(0.1)
                        raise IBKRBarSubscriptionStalled(frame.reason)
                    if frame.cause is not None:
                        raise IBKRBarInterrupted(frame.reason, cause=frame.cause)
                    raise IBKRBarStreamError(frame.reason)
                if frame.kind == "idle":
                    yield None
                    continue
                if frame.bar is None:
                    raise IBKRBarStreamError("Shared bar source returned a non-bar observation")
                bar = frame.bar
                if use_rth and _session_phase_for_ms(bar.start_ms) != "RTH":
                    yield None
                    continue
                shared.bar_receipts[symbol.upper()] = frame.received_at_ms
                yield _LeasedBar(
                    SimpleNamespace(
                        time=bar.start_ms,
                        open=Decimal(bar.open),
                        high=Decimal(bar.high),
                        low=Decimal(bar.low),
                        close=Decimal(bar.close),
                        volume=Decimal(bar.volume),
                    ),
                    bar.venue,
                    shared.bar_generation(frame.epoch),
                )
    except TimeoutError as exc:
        raise IBKRBarRequestDeadlineExceeded("Shared source acquisition exceeded the continuity deadline") from exc
    except (FeedUnavailable, StopAsyncIteration) as exc:
        raise IBKRBarInterrupted("Shared source delivery interrupted", cause="socket_down") from exc
