"""Native and installation delivery have the same consumer contract."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from ib_async import Option
from ib_async.objects import TickData
from pydantic import ValidationError

from app.broker.ibkr.live.protocol import Frame, SourceEpoch
from app.broker.ibkr.live.quotes import QuoteFailure
from tests.marketdata.shared.test_consumers import Delivery, client_for, quote_update
from tests.marketdata.shared.test_native import Client


@pytest.mark.parametrize("shared", [False, True], ids=["native", "installation"])
async def test_quote_lease_delivers_original_typed_receipts_and_refusals_without_synthetic_broker_errors(
    shared: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.broker.ibkr.live.quote_callbacks.now_ms_utc", lambda: 1000)
    delivery = Delivery()
    client = client_for(delivery) if shared else Client()
    lease = client.live_data.quote(Option(conId=987654, exchange="SMART", currency="USD"))
    updates: asyncio.Queue[Any] = asyncio.Queue()
    failures: list[QuoteFailure] = []
    broker_errors: list[Any] = []
    lease.updates += updates.put_nowait
    lease.failures += failures.append
    client.ib.errorEvent += lambda *args: broker_errors.append(args)
    assert lease.latest is None
    try:
        await asyncio.sleep(0)
        if shared:
            await delivery.publish(quote_update((1, 2, 1), (2, 3, 1)))
        else:
            ticker = client.ib.ticker
            ticker.bid, ticker.ask = 2, 3
            ticker.ticks = [TickData(None, 1, 2, 1), TickData(None, 2, 3, 1)]
            ticker.updateEvent.emit(ticker)
        update = await asyncio.wait_for(updates.get(), 1)
        assert update.received_at_ms == 1000
        assert update.values["bid"] == 2 and update.values["ask"] == 3
        if shared:
            await delivery.publish(
                Frame(kind="error", epoch=None, received_at_ms=2000, reason="refused", error_code=354)
            )
        else:
            client.ib.errorEvent.emit(5, 354, "refused", None)
        assert QuoteFailure("refused", 354) in failures
        assert lease.latest is None
        assert len(broker_errors) == (0 if shared else 1)
    finally:
        lease.release()
        lease.release()
        await client.live_data.close()


def test_observation_requires_a_typed_epoch_and_unchanged_quote_receipt() -> None:
    frame = quote_update((1, 2, 1))
    with pytest.raises(ValidationError, match="source epoch"):
        Frame.model_validate({**frame.model_dump(), "epoch": None})
    with pytest.raises(ValidationError, match="receipt differs"):
        Frame.model_validate({**frame.model_dump(), "received_at_ms": 9999})
    epoch = frame.epoch
    assert isinstance(epoch, SourceEpoch)
    assert epoch.model_copy(update={"generation": epoch.generation + 1}).order_key > epoch.order_key


async def test_shared_bar_request_keeps_watermark_receipts_and_consumer_stall_budget(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging
    from types import SimpleNamespace

    from app.broker.ibkr.bars import IBKRBarSubscriptionStalled
    from app.broker.ibkr.live.protocol import Instrument, SourceBar
    from app.broker.ibkr.live.source import BarRequest
    from tests.marketdata.shared.test_consumers import EPOCH, RTH

    clock = [100.0]
    monkeypatch.setattr("app.broker.ibkr.live.bars.time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr("app.broker.ibkr.live.bars._stall_timer_armed", lambda *args: True)
    delivery = Delivery()
    delivery._underlyings["IWM"] = Instrument(con_id=22, exchange="SMART", currency="USD", sec_type="STK")
    observed: list[tuple[int, int]] = []
    request = BarRequest(
        use_rth=False,
        stall_timeout_s=10,
        consumer_label="test chart",
        no_bar_message="waiting for IWM",
        first_bar_message="IWM arrived",
        last_source_ms=RTH,
        on_source_bar=lambda stamp, receipt: observed.append((stamp, receipt)),
    )
    stream = delivery.raw_bars("IWM", request)
    caplog.set_level(logging.INFO)
    try:
        for offset, receipt in [(0, 1000), (5000, 2000)]:
            pending = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)
            await delivery.publish(
                Frame(
                    kind="bar",
                    epoch=EPOCH,
                    received_at_ms=receipt,
                    bar=SourceBar(start_ms=RTH + offset, open="2", high="2", low="2", close="2", volume="1"),
                ),
                "bars",
            )
            bar = await asyncio.wait_for(pending, 1)
            assert bar.received_at_ms == receipt
        assert observed == [(RTH + 5000, 2000)]
        assert "IWM arrived" in caplog.text
        clock[0] += 11
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        await delivery.publish(Frame(kind="idle", epoch=EPOCH, received_at_ms=3000), "bars")
        with pytest.raises(IBKRBarSubscriptionStalled, match="10s"):
            await asyncio.wait_for(pending, 1)
    finally:
        await stream.aclose()
        await delivery.close()
