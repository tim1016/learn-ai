"""Shared raw delivery into the existing per-consumer minute assembler."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from app.broker.ibkr.bars import (
    IBKRBarInterrupted,
    IBKRBarRequestDeadlineExceeded,
    IBKRBarSubscriptionStalled,
    _BarDeliveryLogger,
    _stall_timer_armed,
)
from app.broker.ibkr.live.protocol import FeedUnavailable, Subscription
from app.broker.ibkr.live.source import RawBarReceipt
from app.broker.ibkr.minute_assembler import IBKRBarStreamError, IBKRImpossibleBarError, _session_phase_for_ms
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)


async def shared_raw_bars(
    shared: Any,
    symbol: str,
    *,
    use_rth: bool,
    stall_timeout_s: float,
    consumer_label: str,
    no_bar_message: str,
    first_bar_message: str,
    last_source_ms: int | None = None,
    on_source_bar: Callable[[int, int], None] | None = None,
    request_deadline_ms: int | None = None,
    stall_deferred: Callable[[], bool] | None = None,
) -> AsyncIterator[RawBarReceipt | None]:
    symbol = symbol.upper()
    lookup_timeout = 5 if request_deadline_ms is None else max(0, (request_deadline_ms - now_ms_utc()) / 1000)
    try:
        identity = await shared.underlying(symbol, lookup_timeout)
    except TimeoutError as exc:
        raise IBKRBarRequestDeadlineExceeded("Shared source contract lookup exceeded its deadline") from exc
    delivery = _BarDeliveryLogger(symbol=symbol, con_id=identity.con_id, use_rth=use_rth)
    delivery.log_subscribed(initial_bar_count=0, multiplexed=True, consumer_count=1)
    last_progress = time.monotonic()
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
                    if frame.cause == "impossible_bar":
                        raise IBKRImpossibleBarError(frame.reason)
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
                    if not _stall_timer_armed(use_rth, last_source_ms):
                        last_progress = time.monotonic()
                    elif time.monotonic() - last_progress >= stall_timeout_s and not (
                        stall_deferred and stall_deferred()
                    ):
                        raise IBKRBarSubscriptionStalled(
                            f"Shared subscription for {symbol} stalled for {stall_timeout_s:g}s"
                        )
                    delivery.maybe_log_no_bar(
                        bar_count=0,
                        connected=shared.is_connected(),
                        connection_lost=not shared.is_connected(),
                        message=no_bar_message,
                    )
                    yield None
                    continue
                if frame.bar is None:
                    raise IBKRBarStreamError("Shared bar source returned a non-bar observation")
                bar = frame.bar
                if use_rth and _session_phase_for_ms(bar.start_ms) != "RTH":
                    yield None
                    continue
                if last_source_ms is None or bar.start_ms > last_source_ms:
                    last_progress, last_source_ms = time.monotonic(), bar.start_ms
                    delivery.log_first_bar(bar_count=1, message=first_bar_message)
                    if on_source_bar is not None:
                        on_source_bar(bar.start_ms, frame.received_at_ms)
                yield RawBarReceipt(
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
                    frame.received_at_ms,
                )
    except TimeoutError as exc:
        raise IBKRBarRequestDeadlineExceeded("Shared source acquisition exceeded the continuity deadline") from exc
    except (FeedUnavailable, StopAsyncIteration) as exc:
        raise IBKRBarInterrupted("Shared source delivery interrupted", cause="socket_down") from exc

    finally:
        logger.debug("Released shared bar consumer", extra={"consumer": consumer_label, "symbol": symbol})
