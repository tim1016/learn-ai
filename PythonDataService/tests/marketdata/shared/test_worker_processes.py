"""Actual quote/bar adapters share installation delivery across OS processes."""

from __future__ import annotations

import asyncio
import multiprocessing
import socket
import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from types import SimpleNamespace
from typing import Any
from urllib.request import urlopen

from eventkit import Event
from ib_async import Option

from app.broker.ibkr.bars import stream_minute_bars, stream_raw_5s_bars
from app.broker.ibkr.config import IbkrSettings
from app.broker.ibkr.live.connection import SharedConnection
from app.broker.ibkr.live.hub import FeedHub
from app.broker.ibkr.live.protocol import Frame, Instrument, SourceBar, SourceHealth, Subscription
from app.broker.ibkr.live.server import create_app
from app.broker.ibkr.minute_assembler import MinuteAssembler
from app.broker.ibkr.option_quotes import OptionLine
from app.utils.timestamps import now_ms_utc
from tests._helpers.live_source import unused_qualifier
from tests.marketdata.shared.test_consumers import EPOCH, RTH, quote_update


def serve_workers(listener: socket.socket, events: Any, begin: Any) -> None:
    import uvicorn

    class Source:
        async def stream(self, key: Subscription) -> AsyncIterator[Frame]:
            events.put(("opened", key.kind, key.instrument.con_id))
            try:
                while not begin.is_set():
                    yield Frame(kind="idle", epoch=EPOCH, received_at_ms=now_ms_utc())
                    await asyncio.sleep(0.02)
                if key.kind == "bars":
                    for start in [RTH - 90 * 60_000, *(RTH + i * 5000 for i in range(12))]:
                        yield Frame(
                            kind="bar",
                            epoch=EPOCH,
                            received_at_ms=start + 5000,
                            bar=SourceBar(start_ms=start, open="2.1", high="2.2", low="2", close="2.1", volume="3"),
                        )
                        await asyncio.sleep(0.01)
                receipt = 1000
                while True:
                    yield (
                        quote_update((1, 2, 1), (2, 3, 1), received=receipt)
                        if key.kind == "quotes"
                        else Frame(kind="idle", epoch=EPOCH, received_at_ms=now_ms_utc())
                    )
                    receipt += 1
                    await asyncio.sleep(0.05)
            finally:
                events.put(("closed", key.kind, key.instrument.con_id))

    def health() -> SourceHealth:
        return SourceHealth(connected=True, epoch=EPOCH, observed_at_ms=now_ms_utc())

    uvicorn.Server(uvicorn.Config(create_app(FeedHub(Source()), "test-secret", health), log_level="error")).run(
        sockets=[listener]
    )


def run_worker(url: str, mode: str, events: Any, release: Any, probe: Any) -> None:
    async def run() -> None:
        class ObservedConnection(SharedConnection):
            async def stream(self, key: Subscription) -> AsyncIterator[Frame]:
                first = True
                async with aclosing(super().stream(key)) as frames:
                    async for frame in frames:
                        if first:
                            events.put(("subscribed", mode, key.kind))
                            first = False
                        yield frame

        shared = ObservedConnection(
            IbkrSettings(_env_file=None, shared_feed_url=url, shared_feed_token="test-secret"), qualify=unused_qualifier
        )
        shared._underlyings["IWM"] = Instrument(con_id=22, exchange="SMART", currency="USD", sec_type="STK")
        client = SimpleNamespace(live_data=shared, mode=mode, ib=SimpleNamespace(errorEvent=Event()))
        shared.start()
        lease = client.live_data.quote(Option(conId=987654, symbol="QQQ", exchange="SMART", currency="USD"))
        line = OptionLine(contract=None, incomplete_since_ms=0)
        complete = asyncio.Event()

        def receive(ticker: Any) -> None:
            line.ingest(ticker)
            if line.has_a_whole_quote:
                complete.set()
                if probe.is_set():
                    probe.clear()
                    events.put(("alive", mode, line.bid.received_ms))

        lease.updates += receive
        bars = (
            stream_raw_5s_bars(client, "IWM", use_rth=False)
            if mode == "paper"
            else stream_minute_bars(client, "IWM", use_rth=True, assembler=MinuteAssembler())
        )
        try:
            bar = await asyncio.wait_for(anext(bars), 15)
            await asyncio.wait_for(complete.wait(), 5)
            events.put(
                ("result", mode, bar.start_ms, str(bar.close), bar.volume, line.bid.received_ms, line.ask.received_ms)
            )
            await asyncio.to_thread(release.wait, 15)
        finally:
            await bars.aclose()
            lease.release()
            await asyncio.sleep(0)
            await shared.close()

    asyncio.run(run())


def test_all_modes_share_option_prices_and_raw_or_derived_bars_across_processes() -> None:
    context = multiprocessing.get_context("spawn")
    events, begin = context.Queue(), context.Event()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    source = context.Process(target=serve_workers, args=(listener, events, begin))
    modes = ["paper", "live_shadow", "live", "dry_run"]
    releases, probes = [context.Event() for _ in modes], [context.Event() for _ in modes]
    url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    workers = [
        context.Process(target=run_worker, args=(url, mode, events, release, probe))
        for mode, release, probe in zip(modes, releases, probes, strict=True)
    ]
    seen: list[Any] = []
    source.start()
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                with urlopen(url + "/health", timeout=0.2):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise AssertionError("test source did not start") from None
                time.sleep(0.05)
        for worker in workers:
            worker.start()
        while sum(e[0] == "subscribed" for e in seen) < 8:
            seen.append(events.get(timeout=15))
        assert sorted(e for e in seen if e[0] == "opened") == [("opened", "bars", 22), ("opened", "quotes", 987654)]
        begin.set()
        while sum(e[0] == "result" for e in seen) < 4:
            seen.append(events.get(timeout=15))
        for event in (e for e in seen if e[0] == "result"):
            _, mode, start, close, volume, bid_ms, ask_ms = event
            assert (start, close, volume) == ((RTH - 90 * 60_000, "2.1", 3) if mode == "paper" else (RTH, "2.1", 36))
            assert bid_ms == ask_ms and bid_ms >= 1000
        # A worker crash drops only its leases; peers keep receiving real callbacks.
        workers[0].kill()
        workers[0].join(5)
        probes[1].set()
        assert events.get(timeout=5)[:2] == ("alive", "live_shadow")
        for release in releases[1:]:
            release.set()
        for worker in workers[1:]:
            worker.join(5)
            assert worker.exitcode == 0
        assert {events.get(timeout=5), events.get(timeout=5)} == {("closed", "bars", 22), ("closed", "quotes", 987654)}
    finally:
        for worker, release in zip(workers, releases, strict=True):
            if worker.pid is not None and worker.is_alive():
                release.set()
        for process in [*workers, source]:
            if process.pid is not None:
                process.join(1)
                if process.is_alive():
                    process.kill()
                    process.join(5)
        listener.close()
        events.close()
