"""#2354: the gallery must not spend the shared real-time-bar pacer on a line TWS reports dead.

During a TWS 1100 (socket up, feed down) the gallery used to restart every
shown symbol's line about twice a second. Each restart took a slot of the
process-wide pacer (60 new ``reqRealTimeBars`` per 600 s) that the bots'
``IbkrMarketDataFeed`` shares, so after the 1102 the bots' resubscribe found
the budget empty and the run was refused. Adapted from the #2336 probe.

Real code under test: GalleryHub (5s resolution) -> LiveBarAggregator -> stream_raw_5s_bars /
stream_minute_bars -> _RealtimeBarSubscriptionRegistry + _RealtimeBarRequestPacer (60 new
reqRealTimeBars per 600 s), and the bot side IbkrMarketDataFeed.stream_bars with a real
ContinuityPolicy / ContinuityLoop.

Faked edges only:
* IBKR client / ib_async IB: FakeClient + FakeIB. ``is_connected`` / ``connection_lost`` /
  ``connection_generation`` stand in for the real socket + TWS 1100/1102 state.
  qualifyContractsAsync answers immediately (ASSUMPTION: TWS answers reqContractDetails
  during an 1100 -- the one vendor leg not verifiable in-process).
  reqRealTimeBars returns a live list the fake market appends a 5 s bar to every 5 s
  while connectivity is up; cancelRealTimeBars stops appending.
* Clock: one virtual clock drives the pacer, now_ms_utc in bars/continuity/feed.
* Gallery catalog: tests.services.test_gallery_hub fakes.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import app.broker.ibkr.bars as bars_mod
import app.marketdata.ibkr_continuity as cont_mod
import app.marketdata.ibkr_feed as feed_mod
import app.services.live_bar_aggregator as agg_mod
from app.marketdata.feed import ContinuityEventRef, ContinuityPolicy, MarketDataFeedError, SubstitutionRefusal
from app.marketdata.ibkr_feed import IbkrMarketDataFeed
from app.services.broker_v2_panel.gallery_hub import GalleryHub
from app.services.decision_session import RunDecisionSession
from app.services.live_bar_aggregator import LiveBarAggregator
from tests.services.test_gallery_hub import _Cat2, _FakeCatalogSource

ET = ZoneInfo("America/New_York")
CONIDS = {"SPY": 756733, "QQQ": 320227571, "IWM": 9579970}


def et_ms(h: int, m: int, s: int = 0) -> int:
    return int(datetime(2026, 9, 2, h, m, s, tzinfo=ET).timestamp() * 1000)


class World:
    def __init__(self, start_ms: int) -> None:
        self.now_ms = start_ms
        self.lines: list[dict] = []
        self.req_log: list[tuple[int, str, bool]] = []  # (ms, symbol, use_rth)
        self.cancels = 0
        self.qualify_calls = 0


class FakeIB:
    def __init__(self, world: World, client: FakeClient) -> None:
        self.w = world
        self.c = client

    async def qualifyContractsAsync(self, contract):
        self.w.qualify_calls += 1
        contract.conId = CONIDS[contract.symbol]
        return [contract]

    def reqRealTimeBars(self, contract, bar_size, what, useRTH=True):
        lst: list = []
        self.w.lines.append(
            {"symbol": contract.symbol, "bars": lst, "active": True, "gen": self.c.connection_generation}
        )
        self.w.req_log.append((self.w.now_ms, contract.symbol, useRTH))
        return lst

    def cancelRealTimeBars(self, lst):
        self.w.cancels += 1
        for line in self.w.lines:
            if line["bars"] is lst:
                line["active"] = False


class FakeClient:
    def __init__(self, world: World) -> None:
        self.w = world
        self.connected = True
        self.connection_lost = False
        self.connection_generation = 1
        self.data_loss_epoch = 0
        self.settings = SimpleNamespace(realtime_bar_max_active=100, feed_continuity_enabled=True)
        self.ib = FakeIB(world, self)

    def is_connected(self) -> bool:
        return self.connected

    def require_connected(self) -> None:
        if not self.connected:
            raise agg_mod.NotConnectedError("IBKR client is not connected.")


def market_tick(world: World, client: FakeClient) -> None:
    """At each 5 s boundary, IBKR appends the bar that just closed to every live line."""
    if world.now_ms % 5_000 != 0 or not client.connected or client.connection_lost:
        return
    start = world.now_ms - 5_000
    raw = SimpleNamespace(
        time=datetime.fromtimestamp(start / 1000, tz=UTC),
        open=Decimal("1"), high=Decimal("1"), low=Decimal("1"), close=Decimal("1"), volume=1,
    )
    for line in world.lines:
        if line["active"] and line["gen"] == client.connection_generation:
            line["bars"].append(raw)


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch):
    def make(start_ms: int):
        world = World(start_ms)
        client = FakeClient(world)
        pacer = bars_mod._RealtimeBarRequestPacer(
            clock=lambda: world.now_ms / 1000, sleep=lambda _s: asyncio.sleep(0.002)
        )
        registry = bars_mod._RealtimeBarSubscriptionRegistry(pacer)
        monkeypatch.setattr(bars_mod, "_REALTIME_BAR_SUBSCRIPTIONS", registry)
        monkeypatch.setattr(agg_mod, "get_client", lambda: client)
        for mod in (bars_mod, cont_mod, feed_mod):
            monkeypatch.setattr(mod, "now_ms_utc", lambda: world.now_ms)
        monkeypatch.setattr(cont_mod, "get_monitor", lambda: None)
        monkeypatch.setattr(cont_mod, "WAIT_POLL_S", 0.002)
        return world, client, pacer

    return make


def gallery_hub(aggregator: LiveBarAggregator, symbols: list[str]) -> GalleryHub:
    rows = [_Cat2(f"bot-{s}", s, True, 0.0, 0.0, 0) for s in symbols]
    return GalleryHub(
        broker="alpaca", account_id="PA3", catalog_source=_FakeCatalogSource(rows),
        aggregator=aggregator, resolution="5s",
    )


async def run_seconds(world, client, n, *, hub=None, outage=None, on_second=None, step_real_s=0.004):
    """Advance virtual time 1 s at a time; the gallery stream polls once per second (router loop)."""
    for _ in range(n):
        world.now_ms += 1_000
        if outage is not None:
            client.connection_lost = outage[0] <= world.now_ms < outage[1]
        market_tick(world, client)
        if hub is not None:
            await hub.build_update({}, known_sids=set())
        if on_second is not None:
            on_second()
        for _ in range(5):
            await asyncio.sleep(0)
        await asyncio.sleep(step_real_s)


# --------------------------------------------------------------------------- A: socket down


async def test_a_socket_down_gallery_sends_nothing_to_ibkr(rig) -> None:
    world, client, pacer = rig(et_ms(10, 0))
    agg = LiveBarAggregator()
    hub = gallery_hub(agg, ["SPY", "QQQ", "IWM"])
    client.connected = False
    await run_seconds(world, client, 60, hub=hub)
    assert len(world.req_log) == 0 and len(pacer._request_times) == 0
    await agg.shutdown()


# --------------------------------------------------------------------------- B: 1100 soft loss


@pytest.mark.parametrize("symbols", [["SPY"], ["SPY", "QQQ", "IWM"]])
async def test_b_1100_gallery_does_not_burn_the_shared_pacer(rig, symbols) -> None:
    world, client, pacer = rig(et_ms(10, 0))
    agg = LiveBarAggregator()
    hub = gallery_hub(agg, symbols)
    await run_seconds(world, client, 10, hub=hub)  # healthy: lines open once
    healthy_reqs = len(world.req_log)
    t0 = world.now_ms
    exhausted_at = None

    def watch() -> None:
        nonlocal exhausted_at
        if exhausted_at is None and len(pacer._request_times) >= 60:
            exhausted_at = world.now_ms

    await run_seconds(world, client, 60, hub=hub, outage=(t0, t0 + 60_000), on_second=watch)
    # Only the line a stream was already opening as the 1100 began reaches
    # IBKR, once per symbol; before #2354 every one-second poll restarted it
    # and the budget hit 60/60 within a minute.
    assert exhausted_at is None
    assert len(world.req_log) - healthy_reqs <= len(symbols)
    assert len(pacer._request_times) <= 2 * len(symbols)
    await agg.shutdown()


# --------------------------------------------------------------------------- C: bot recovery


def _next_trigger_1m(last_end: int) -> int:
    return (last_end // 60_000 + 1) * 60_000


class Sink:
    def __init__(self) -> None:
        self.events = []

    async def __call__(self, event):
        self.events.append(event)
        return ContinuityEventRef(run_id="run-1", evidence_seq=len(self.events))


async def _bot_scenario(rig, *, with_gallery: bool, gallery_symbols: list[str], outage=None):
    """RTH-kind run (streams use_rth=False, sees PRE minutes) on SPY from 09:05 ET.

    A TWS 1100 soft loss before the open, restored (1102) before 09:30. Gallery shows SPY.
    """
    world, client, _pacer = rig(et_ms(9, 5))
    feed = IbkrMarketDataFeed(client)
    sink = Sink()
    policy = ContinuityPolicy(
        session=RunDecisionSession(kind="rth", window=None),
        next_trigger_ms=_next_trigger_1m,
        substitution_grant=lambda s, e: SubstitutionRefusal(reason="SUBSTITUTION_NOT_AUTHORIZED"),
        record_event=sink,
    )
    delivered: list[int] = []
    outcome: dict = {}

    async def bot() -> None:
        try:
            async for bar in feed.stream_bars("SPY", use_rth=False, continuity=policy):
                delivered.append(bar.start_ms)
        except MarketDataFeedError as exc:
            outcome["error"] = f"{exc} reason={getattr(exc, 'reason', None)}"
            outcome["at"] = world.now_ms

    agg = LiveBarAggregator()
    hub = gallery_hub(agg, gallery_symbols) if with_gallery else None
    task = asyncio.create_task(bot())
    outage = outage or (et_ms(9, 24, 2), et_ms(9, 25, 2))
    await run_seconds(world, client, 35 * 60, hub=hub, outage=outage)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await agg.shutdown()
    bot_reqs = [(ms, s) for ms, s, rth in world.req_log if not rth]

    def fmt(ms):
        return datetime.fromtimestamp(ms / 1000, tz=ET).strftime("%H:%M:%S")

    return {
        "bot_line_requests": [fmt(ms) for ms, _ in bot_reqs],
        "gallery_line_requests": sum(1 for _, _, rth in world.req_log if rth),
        "delivered_last": fmt(delivered[-1] + 60_000) if delivered else None,
        "delivered_rth": sum(1 for s in delivered if s >= et_ms(9, 30)),
        "outcome": outcome.get("error"),
        "outcome_at": fmt(outcome["at"]) if "at" in outcome else None,
        "events": [(e.kind, getattr(e, "reason", None)) for e in sink.events],
    }


OUTAGES = {
    "pre_0924_60s": (et_ms(9, 24, 2), et_ms(9, 25, 2)),
    "pre_0926_60s": (et_ms(9, 26, 2), et_ms(9, 27, 2)),
    "pre_0927_40s": (et_ms(9, 27, 2), et_ms(9, 27, 42)),
}


@pytest.mark.slow
@pytest.mark.parametrize("name", list(OUTAGES))
async def test_c_baseline_no_gallery_bot_recovers(rig, name) -> None:
    r = await _bot_scenario(rig, with_gallery=False, gallery_symbols=[], outage=OUTAGES[name])
    assert r["outcome"] is None and r["delivered_rth"] >= 9


@pytest.mark.slow
@pytest.mark.parametrize("name", list(OUTAGES))
async def test_c_with_gallery_bot_still_recovers(rig, name) -> None:
    r = await _bot_scenario(rig, with_gallery=True, gallery_symbols=["SPY"], outage=OUTAGES[name])
    # Before #2354 the bot's post-1102 resubscribe found the pacer emptied by the gallery.
    assert r["outcome"] is None and r["delivered_rth"] >= 9, r
