"""PRD #619-B B3 — runtime_producer composition helpers + LiveEngine wiring.

Two layers:

1. **Pure composition** — ``verdict_to_identity``, ``compose_capability``,
   ``compose_posture``, the three ``build_*_block`` helpers, and
   ``build_control_plane_block_from_lease``. Asserted with a Cartesian
   matrix over the identity × capability inputs and a few lease seeding
   cases.

2. **Engine wiring** — confirms that when ``LiveEngine`` is constructed
   with a ``runtime_aggregator``, the producer hooks land on
   ``run()`` start, every bar tick, the verdict-check path, and the
   command-poll tick. Tested via a fake aggregator that records every
   update; we do not exercise the full publisher because that's
   already covered by ``test_engine_runtime_publisher.py``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.ibkr.models import IbkrConnectionHealth
from app.engine.data.trade_bar import TradeBar
from app.engine.live.control_plane import DaemonLease, write_daemon_lease
from app.engine.live.engine_runtime import (
    BarLoopBlock,
    BrokerBlock,
    CommandLoopBlock,
    ControlPlaneBlock,
)
from app.engine.live.runtime_producer import (
    build_bar_loop_block,
    build_broker_block,
    build_command_loop_block,
    build_control_plane_block_from_lease,
    compose_capability,
    compose_posture,
    verdict_to_identity,
)

# ===========================================================================
# Layer 1 — pure composition
# ===========================================================================


@pytest.mark.parametrize(
    "verdict,expected",
    [
        ("paper-only", "PAPER_VERIFIED"),
        ("unsafe", "LIVE_DETECTED"),
        ("unknown", "UNKNOWN"),
        (None, "UNKNOWN"),
        ("", "UNKNOWN"),
        ("something-else", "UNKNOWN"),
    ],
)
def test_verdict_to_identity(verdict: str | None, expected: str) -> None:
    assert verdict_to_identity(verdict) == expected


@pytest.mark.parametrize(
    "run_mode,readonly,expected",
    [
        ("live_paper", False, "PAPER_ORDERS_ENABLED"),
        ("live_paper", True, "READ_ONLY"),
        ("shadow", False, "READ_ONLY"),
        ("shadow", True, "READ_ONLY"),
        ("", False, "UNKNOWN"),
        ("wonky", False, "UNKNOWN"),
        ("", True, "READ_ONLY"),
    ],
)
def test_compose_capability(run_mode: str, readonly: bool, expected: str) -> None:
    assert compose_capability(run_mode=run_mode, readonly=readonly) == expected


@pytest.mark.parametrize(
    "identity,capability,expected",
    [
        ("PAPER_VERIFIED", "PAPER_ORDERS_ENABLED", "PAPER_EXECUTION"),
        ("PAPER_VERIFIED", "READ_ONLY", "PAPER_OBSERVATION"),
        ("PAPER_VERIFIED", "BLOCKED", "UNSAFE"),
        ("PAPER_VERIFIED", "UNKNOWN", "UNKNOWN"),
        ("LIVE_DETECTED", "PAPER_ORDERS_ENABLED", "UNSAFE"),
        ("LIVE_DETECTED", "READ_ONLY", "UNSAFE"),
        ("LIVE_DETECTED", "BLOCKED", "UNSAFE"),
        ("LIVE_DETECTED", "UNKNOWN", "UNSAFE"),
        ("UNKNOWN", "PAPER_ORDERS_ENABLED", "UNKNOWN"),
        ("UNKNOWN", "READ_ONLY", "UNKNOWN"),
        ("UNKNOWN", "BLOCKED", "UNSAFE"),
        ("UNKNOWN", "UNKNOWN", "UNKNOWN"),
    ],
)
def test_compose_posture(identity: str, capability: str, expected: str) -> None:
    assert (
        compose_posture(identity=identity, capability=capability)  # type: ignore[arg-type]
        == expected
    )


def test_build_command_loop_block_paused() -> None:
    block = build_command_loop_block(heartbeat_at_ms=1_700_000_000_000, paused=True)
    assert block.heartbeat_at_ms == 1_700_000_000_000
    assert block.state == "PAUSED"


def test_build_command_loop_block_running() -> None:
    block = build_command_loop_block(heartbeat_at_ms=1_700_000_000_000, paused=False)
    assert block.state == "RUNNING"


def test_build_bar_loop_block_carries_split_heartbeats() -> None:
    block = build_bar_loop_block(
        heartbeat_at_ms=1_700_000_000_000,
        latest_source_bar_ms=1_700_000_000_000 - 60_000,
        expected_interval_ms=60_000,
        source_state="ACTIVE",
        source="ibkr_realtime_bars",
        symbol="SPY",
        subscription_requested_at_ms=1_700_000_000_000 - 120_000,
    )
    assert block.heartbeat_at_ms == 1_700_000_000_000
    assert block.latest_source_bar_ms == 1_700_000_000_000 - 60_000
    assert block.expected_interval_ms == 60_000
    assert block.source_state == "ACTIVE"
    assert block.source == "ibkr_realtime_bars"
    assert block.symbol == "SPY"
    assert block.subscription_requested_at_ms == 1_700_000_000_000 - 120_000


def test_build_broker_block_composes_full_axes() -> None:
    block = build_broker_block(
        verdict_value="paper-only",
        run_mode="live_paper",
        readonly=False,
        connection_state="connected",
        recovery_state="HEALTHY",
        connection_epoch=3,
        client_id=12,
        connected_account="DU0123456",
        port_class="paper_port",
        observation_at_ms=1_700_000_000_000,
        probe_completed_at_ms=1_700_000_000_000 - 100,
        reconnect_attempt=0,
    )
    assert block.identity == "PAPER_VERIFIED"
    assert block.submission_capability == "PAPER_ORDERS_ENABLED"
    assert block.effective_posture == "PAPER_EXECUTION"
    assert block.connection_state == "connected"
    assert block.recovery_state == "HEALTHY"
    assert block.connection_epoch == 3
    assert block.client_id == 12
    assert block.connected_account == "DU0123456"
    assert block.port_class == "paper_port"
    assert block.observation_at_ms == 1_700_000_000_000


def test_build_control_plane_block_with_no_lease(tmp_path: Path) -> None:
    block = build_control_plane_block_from_lease(tmp_path, now_ms=1_700_000_000_000)
    assert block.lease_observed_at_ms == 1_700_000_000_000
    assert block.observed_daemon_boot_id is None


def test_build_control_plane_block_with_none_root() -> None:
    block = build_control_plane_block_from_lease(None, now_ms=1_700_000_000_000)
    assert block.observed_daemon_boot_id is None


def test_build_control_plane_block_reads_existing_lease(tmp_path: Path) -> None:
    write_daemon_lease(
        tmp_path,
        DaemonLease(boot_id="daemon-boot-XYZ", written_at_ms=1_700_000_000_000),
    )

    block = build_control_plane_block_from_lease(tmp_path, now_ms=1_700_000_000_500)

    assert block.lease_observed_at_ms == 1_700_000_000_500
    assert block.observed_daemon_boot_id == "daemon-boot-XYZ"


# ===========================================================================
# Layer 2 — LiveEngine producer hooks land on real call sites
# ===========================================================================


class _RecordingAggregator:
    """Stand-in for ``EngineRuntimeAggregator`` that records every update.

    Tests assert ON THE SEQUENCE OF CALLS, not on the publisher's
    serialized output (already covered by
    ``test_engine_runtime_publisher.py``).
    """

    def __init__(self) -> None:
        self.command_loop_updates: list[CommandLoopBlock] = []
        self.broker_updates: list[BrokerBlock] = []
        self.bar_loop_updates: list[BarLoopBlock] = []
        self.control_plane_updates: list[ControlPlaneBlock] = []

    async def update_command_loop(self, block: CommandLoopBlock) -> None:
        self.command_loop_updates.append(block)

    async def update_broker(self, block: BrokerBlock) -> None:
        self.broker_updates.append(block)

    async def update_bar_loop(self, block: BarLoopBlock) -> None:
        self.bar_loop_updates.append(block)

    async def update_control_plane(self, block: ControlPlaneBlock) -> None:
        self.control_plane_updates.append(block)


def _bar(minute: int) -> TradeBar:
    start = datetime(2026, 5, 4, 14, 0, tzinfo=UTC) + timedelta(minutes=minute)
    return TradeBar(
        symbol="SPY",
        time=start,
        end_time=start + timedelta(minutes=1),
        open=Decimal("500"),
        high=Decimal("500"),
        low=Decimal("500"),
        close=Decimal("500"),
        volume=100,
    )


async def _iter_bars(bars: list[TradeBar]) -> AsyncIterator[TradeBar]:
    for b in bars:
        yield b


@pytest.mark.asyncio
async def test_engine_publishes_initial_blocks_on_run(tmp_path: Path) -> None:
    """At ``run()`` entry the engine seeds command_loop + control_plane
    so the publisher can emit before the first bar arrives."""
    from app.engine.live.config import LiveConfig
    from app.engine.live.live_engine import LiveEngine
    from app.engine.strategy.base import Strategy
    from tests.engine.live.fixtures.fake_broker import FakeBroker

    class _NoopStrategy(Strategy):
        def initialize(self) -> None:
            assert self.ctx is not None
            self.ctx.add_equity("SPY")
            self.ctx.register_consolidator("SPY", timedelta(minutes=1), self.on_bar)

        def on_bar(self, bar: TradeBar) -> None:
            return None

    agg = _RecordingAggregator()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=FakeBroker(),
        output_dir=tmp_path,
        account_id="DU123",
        runtime_aggregator=agg,
    )
    await engine.run(_NoopStrategy(), _iter_bars([_bar(m) for m in range(3)]))

    # Startup: at least one command_loop + one control_plane update
    # before any bar lands.
    assert len(agg.command_loop_updates) >= 1
    assert len(agg.control_plane_updates) >= 1
    # First control-plane block has no lease (artifacts_root_for_lease
    # was not provided), so observed_daemon_boot_id is None.
    assert agg.control_plane_updates[0].observed_daemon_boot_id is None


@pytest.mark.asyncio
async def test_engine_publishes_bar_loop_block_per_bar(tmp_path: Path) -> None:
    from app.engine.live.config import LiveConfig
    from app.engine.live.live_engine import LiveEngine
    from app.engine.strategy.base import Strategy
    from tests.engine.live.fixtures.fake_broker import FakeBroker

    class _NoopStrategy(Strategy):
        def initialize(self) -> None:
            assert self.ctx is not None
            self.ctx.add_equity("SPY")
            self.ctx.register_consolidator("SPY", timedelta(minutes=1), self.on_bar)

        def on_bar(self, bar: TradeBar) -> None:
            return None

    agg = _RecordingAggregator()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=FakeBroker(),
        output_dir=tmp_path,
        account_id="DU123",
        runtime_aggregator=agg,
    )
    bars = [_bar(m) for m in range(5)]
    await engine.run(_NoopStrategy(), _iter_bars(bars))

    # One startup seed (no bar yet, latest_source_bar_ms=None) plus one
    # per bar consumed by the loop. The seed exists so the runtime
    # publisher can emit a coherent snapshot before the first bar.
    assert len(agg.bar_loop_updates) == 6
    assert agg.bar_loop_updates[0].latest_source_bar_ms is None
    # The first bar-driven update (index 1) reflects bar[0]'s end_time.
    assert agg.bar_loop_updates[1].latest_source_bar_ms == int(bars[0].end_time.timestamp() * 1000)
    assert agg.bar_loop_updates[1].expected_interval_ms == 60_000
    assert agg.bar_loop_updates[-1].latest_source_bar_ms == int(bars[-1].end_time.timestamp() * 1000)


@pytest.mark.asyncio
async def test_engine_publishes_first_bar_timeout_when_ibkr_source_is_silent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.engine.live import live_engine as live_engine_mod
    from app.engine.live.config import LiveConfig
    from app.engine.live.live_engine import LiveEngine
    from app.engine.strategy.base import Strategy
    from tests.engine.live.fixtures.fake_broker import FakeBroker

    class _NoopStrategy(Strategy):
        def initialize(self) -> None:
            assert self.ctx is not None
            self.ctx.add_equity("SPY")
            self.ctx.register_consolidator("SPY", timedelta(minutes=1), self.on_bar)

        def on_bar(self, bar: TradeBar) -> None:
            return None

    async def _silent_ibkr_stream(client: object, symbol: str) -> AsyncIterator[object]:
        await asyncio.Event().wait()
        if False:
            yield None

    monkeypatch.setattr(live_engine_mod, "stream_minute_bars", _silent_ibkr_stream)
    monkeypatch.setattr(live_engine_mod, "BAR_SOURCE_FIRST_BAR_TIMEOUT_S", 0.01)
    monkeypatch.setattr(live_engine_mod, "BAR_SOURCE_WATCHDOG_INTERVAL_S", 0.005)

    agg = _RecordingAggregator()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=FakeBroker(),
        output_dir=tmp_path,
        account_id="DU123",
        runtime_aggregator=agg,
    )
    shutdown_event = asyncio.Event()

    async def _stop_after_timeout() -> None:
        await asyncio.sleep(0.05)
        shutdown_event.set()

    trigger_task = asyncio.create_task(_stop_after_timeout())
    result = await asyncio.wait_for(
        engine.run(_NoopStrategy(), shutdown_event=shutdown_event),
        timeout=5.0,
    )
    await trigger_task

    assert result.bars == []
    timeout_blocks = [
        block
        for block in agg.bar_loop_updates
        if block.source_state == "NO_FIRST_BAR_TIMEOUT"
    ]
    assert timeout_blocks
    assert timeout_blocks[-1].latest_source_bar_ms is None
    assert timeout_blocks[-1].source == "ibkr_realtime_bars"
    assert timeout_blocks[-1].symbol == "SPY"
    assert timeout_blocks[-1].first_bar_deadline_ms is not None


@pytest.mark.asyncio
async def test_engine_publishes_broker_block_on_every_verdict_check(
    tmp_path: Path,
) -> None:
    """The verdict-check path always fires the broker producer (halt
    or not). After three bars under a paper-only verdict, three
    broker updates land."""
    from app.engine.live.config import LiveConfig
    from app.engine.live.live_engine import LiveEngine
    from app.engine.strategy.base import Strategy
    from tests.engine.live.fixtures.fake_broker import FakeBroker

    class _NoopStrategy(Strategy):
        def initialize(self) -> None:
            assert self.ctx is not None
            self.ctx.add_equity("SPY")
            self.ctx.register_consolidator("SPY", timedelta(minutes=1), self.on_bar)

        def on_bar(self, bar: TradeBar) -> None:
            return None

    agg = _RecordingAggregator()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=FakeBroker(),
        output_dir=tmp_path,
        account_id="DU123",
        run_mode="live_paper",
        readonly=False,
        verdict_provider=lambda: "paper-only",
        runtime_aggregator=agg,
    )
    await engine.run(_NoopStrategy(), _iter_bars([_bar(m) for m in range(3)]))

    # One startup seed + one per bar's verdict-halt check.
    assert len(agg.broker_updates) == 4
    # The startup seed derives the verdict via the same provider, so
    # ADR-0011 amendment composition holds end-to-end from the very
    # first published block (not only after the first bar).
    seed_block = agg.broker_updates[0]
    assert seed_block.identity == "PAPER_VERIFIED"
    assert seed_block.submission_capability == "PAPER_ORDERS_ENABLED"
    assert seed_block.effective_posture == "PAPER_EXECUTION"


@pytest.mark.asyncio
async def test_engine_publishes_initial_control_plane_block_from_lease(
    tmp_path: Path,
) -> None:
    """When ``artifacts_root_for_lease`` points to a directory with a
    ``daemon_lease.json``, the startup control-plane block carries the
    daemon's boot_id verbatim."""
    from app.engine.live.config import LiveConfig
    from app.engine.live.live_engine import LiveEngine
    from app.engine.strategy.base import Strategy
    from tests.engine.live.fixtures.fake_broker import FakeBroker

    class _NoopStrategy(Strategy):
        def initialize(self) -> None:
            assert self.ctx is not None
            self.ctx.add_equity("SPY")
            self.ctx.register_consolidator("SPY", timedelta(minutes=1), self.on_bar)

        def on_bar(self, bar: TradeBar) -> None:
            return None

    artifacts_root = tmp_path / "artifacts"
    artifacts_root.mkdir()
    write_daemon_lease(
        artifacts_root,
        DaemonLease(boot_id="daemon-from-test", written_at_ms=1_700_000_000_000),
    )

    agg = _RecordingAggregator()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=FakeBroker(),
        output_dir=tmp_path,
        account_id="DU123",
        runtime_aggregator=agg,
        artifacts_root_for_lease=artifacts_root,
    )
    await engine.run(_NoopStrategy(), _iter_bars([_bar(m) for m in range(2)]))

    assert any(cp.observed_daemon_boot_id == "daemon-from-test" for cp in agg.control_plane_updates)


@pytest.mark.asyncio
async def test_engine_with_no_aggregator_is_a_noop(tmp_path: Path) -> None:
    """A LiveEngine without a runtime_aggregator must not raise from
    any producer hook — replay tests / synthetic engines must remain
    free of the wire."""
    from app.engine.live.config import LiveConfig
    from app.engine.live.live_engine import LiveEngine
    from app.engine.strategy.base import Strategy
    from tests.engine.live.fixtures.fake_broker import FakeBroker

    class _NoopStrategy(Strategy):
        def initialize(self) -> None:
            assert self.ctx is not None
            self.ctx.add_equity("SPY")
            self.ctx.register_consolidator("SPY", timedelta(minutes=1), self.on_bar)

        def on_bar(self, bar: TradeBar) -> None:
            return None

    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=FakeBroker(),
        output_dir=tmp_path,
        account_id="DU123",
        verdict_provider=lambda: "paper-only",
    )

    # Must run without raising — every producer hook is None-guarded.
    await engine.run(_NoopStrategy(), _iter_bars([_bar(m) for m in range(2)]))


class _StubIbkrSettings:
    """Minimum surface ``_publish_broker_block`` reads off
    ``client.settings``: ``port`` (used by ``classify_port``) plus
    ``mode`` (read by ``_validate_paper_client`` on engine.run entry)."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.mode = "paper"


class _StubIbkrClient:
    """Minimum IbkrClient surface for the regression test.

    Only the two methods the broker producer touches are implemented:
    ``probe()`` (sets ``_last_probe_ms``; ``_probe_and_publish_broker_block``
    is what calls this) and ``health()`` (returns a connected
    ``IbkrConnectionHealth`` carrying that probe timestamp through to
    the broker block's ``probe_completed_at_ms``).
    """

    def __init__(self, *, now_ms_fn: Callable[[], int]) -> None:
        self.settings = _StubIbkrSettings(port=4002)  # paper port
        self.connected_account = "DU123"  # _validate_paper_client gate
        self._last_probe_ms: int | None = None
        self.probe_calls = 0
        self._now_ms = now_ms_fn

    async def probe(self, *, timeout_s: float = 4.0) -> None:
        self.probe_calls += 1
        self._last_probe_ms = self._now_ms()

    def health(self) -> IbkrConnectionHealth:
        return IbkrConnectionHealth(
            mode="paper",
            host="127.0.0.1",
            port=4002,
            client_id=12,
            connected=True,
            account_id="DU123",
            is_paper=True,
            server_version=178,
            fetched_at_ms=self._now_ms(),
            connection_state="connected",
            recovery_state="LINK_INTERRUPTED",
            last_transition_ms=self._now_ms(),
            last_probe_ms=self._last_probe_ms,
        )


class _StubReconnectMonitor:
    is_hard_down = False
    is_attempting = True
    is_recovering = False
    recovery_state = "RECONNECTING"
    current_attempt = 2
    successful_reconnect_count = 1

    def __init__(self, *, last_transition_ms: int) -> None:
        self.last_transition_ms = last_transition_ms


@pytest.mark.asyncio
async def test_engine_broker_block_uses_injected_monitor_overlay(
    tmp_path: Path,
) -> None:
    from app.engine.live.config import LiveConfig
    from app.engine.live.live_engine import LiveEngine
    from app.engine.strategy.base import Strategy
    from tests.engine.live.fixtures.fake_broker import FakeBroker

    class _NoopStrategy(Strategy):
        def initialize(self) -> None:
            assert self.ctx is not None
            self.ctx.add_equity("SPY")
            self.ctx.register_consolidator("SPY", timedelta(minutes=1), self.on_bar)

        def on_bar(self, bar: TradeBar) -> None:
            return None

    now_ms = int(time.time() * 1000)
    client = _StubIbkrClient(now_ms_fn=lambda: now_ms)
    monitor = _StubReconnectMonitor(last_transition_ms=now_ms + 5)
    agg = _RecordingAggregator()
    engine = LiveEngine(
        client,  # type: ignore[arg-type]  # stub matches only the methods used
        LiveConfig(),
        broker=FakeBroker(),
        output_dir=tmp_path,
        account_id="DU123",
        run_mode="live_paper",
        readonly=False,
        verdict_provider=lambda: "paper-only",
        runtime_aggregator=agg,
        broker_monitor=monitor,
    )

    await engine.run(_NoopStrategy(), _iter_bars([]))

    seed_block = agg.broker_updates[0]
    assert seed_block.connection_state == "reconnecting"
    assert seed_block.recovery_state == "RECONNECTING"
    assert seed_block.reconnect_attempt == 2


@pytest.mark.asyncio
async def test_engine_first_runtime_snapshot_coheres_without_bars(
    tmp_path: Path,
) -> None:
    """Regression: pre-market deploys must NOT be marked POSTURE_DEMOTED.

    Before this fix the engine seeded only ``command_loop`` and
    ``control_plane`` at startup; ``broker`` and ``bar_loop`` only landed
    inside the bar loop. The aggregator returns ``None`` from
    ``snapshot()`` until all four blocks have been populated, so no
    ``engine_runtime.json`` was ever written before the first minute
    bar arrived from IBKR — which pre-market is hours away. The
    cockpit then read ``ENGINE_RUNTIME_MISSING`` and blocked Resume
    with ``POSTURE_DEMOTED``, conflating a healthy waiting-for-market
    engine with a crashed one.

    This test pins the fix: after ``run()`` completes — even with zero
    bars — the engine's aggregator emits a coherent snapshot, and the
    freshness evaluator on that snapshot (with the session calendar
    reporting CLOSED, as it would pre-market) returns
    ``posture_demoted=False``.
    """
    from app.engine.live.config import LiveConfig
    from app.engine.live.engine_runtime_publisher import EngineRuntimeAggregator
    from app.engine.live.live_engine import LiveEngine
    from app.engine.strategy.base import Strategy
    from app.services.runtime_freshness import evaluate_runtime_freshness
    from tests.engine.live.fixtures.fake_broker import FakeBroker

    class _NoopStrategy(Strategy):
        def initialize(self) -> None:
            assert self.ctx is not None
            self.ctx.add_equity("SPY")
            self.ctx.register_consolidator("SPY", timedelta(minutes=1), self.on_bar)

        def on_bar(self, bar: TradeBar) -> None:
            return None

    # The engine publishes block heartbeats with real wall-clock
    # ``time.time()``. Use the same clock for both the stub's probe
    # timestamp and the freshness evaluation so heartbeat ages are
    # non-negative — otherwise ``posture_demoted=False`` can pass by
    # accident for blocks that would actually be stale under a real
    # ``now_ms``.
    now_ms = int(time.time() * 1000)
    client = _StubIbkrClient(now_ms_fn=lambda: now_ms)

    aggregator = EngineRuntimeAggregator(
        strategy_instance_id="sid-fresh",
        run_id="run-fresh",
        pid=1,
        process_start_identity="child-fresh",
        expected_daemon_boot_id=None,
    )
    engine = LiveEngine(
        client,  # type: ignore[arg-type]  # stub matches only the methods used
        LiveConfig(),
        broker=FakeBroker(),
        output_dir=tmp_path,
        account_id="DU123",
        run_mode="live_paper",
        readonly=False,
        verdict_provider=lambda: "paper-only",
        runtime_aggregator=aggregator,
    )

    # Zero bars — pre-market state. The bar loop exits immediately on
    # the source-exhausted branch; only the startup hooks run.
    await engine.run(_NoopStrategy(), _iter_bars([]))

    # The startup hook must have actually probed the client; without the
    # probe call, ``_last_probe_ms`` stays None and the broker block's
    # ``probe_completed_at_ms`` would be None — which is exactly the
    # BROKER_PROBE_MISSING regression this fix prevents.
    assert client.probe_calls >= 1

    # The startup hook must also persist ``verdict_snapshot.json``. The
    # Resume guard reads that file via ``read_broker_safety_verdict``
    # and treats absence as identity=UNKNOWN, which routes to
    # BROKER_SAFETY_UNKNOWN and blocks Resume — a regression that an
    # earlier version of this fix only addressed in the runtime
    # aggregator path, leaving Resume still blocked on truly fresh
    # deploys.
    verdict_snapshot_path = tmp_path / "verdict_snapshot.json"
    assert verdict_snapshot_path.is_file(), (
        "verdict_snapshot.json was not written by the startup hook; "
        "Resume would remain blocked on BROKER_SAFETY_UNKNOWN until "
        "the first bar reaches _check_verdict_transition_halt"
    )

    # The aggregator must have a coherent snapshot to hand the publisher.
    snapshot = await aggregator.snapshot(snapshot_seq=0, written_at_ms=now_ms)
    assert snapshot is not None, (
        "fresh-run snapshot is None; the startup hooks did not populate "
        "all four blocks and the publisher will refuse to write "
        "engine_runtime.json"
    )
    assert snapshot.broker.client_id == 12
    assert snapshot.broker.recovery_state == "LINK_INTERRUPTED"
    assert snapshot.broker.probe_completed_at_ms == now_ms

    # The freshness evaluator with session_state=CLOSED (pre-market or
    # after-hours) must NOT demote posture: bar_loop becomes
    # NOT_APPLICABLE from the calendar, and the other three blocks were
    # freshly seeded above.
    freshness = evaluate_runtime_freshness(snapshot, now_ms=now_ms, session_state="CLOSED")
    assert freshness.posture_demoted is False, f"fresh-run still demotes posture; reasons={freshness}"
    assert freshness.bar_loop.state == "NOT_APPLICABLE"
