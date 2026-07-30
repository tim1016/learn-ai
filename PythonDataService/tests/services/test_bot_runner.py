"""Tests for app.services.bot_runner — the in-container bot task registry.

Covers issue #1260 acceptance criteria:
- deploy → running asyncio task + durable ON_DUTY evidence readable without
  the runner (raw artifact files).
- stop → durable STOPPED desired-state, clean task exit, OFF_DUTY evidence.
- simulated crash → typed durable crash evidence distinct from a clean stop;
  the registry reaps and never renders the bot healthy.
- daemon-free by construction (no daemon-client / subprocess imports).
- container-side artifact paths only (everything under the tmp_path root).
- broker-tagged bindings.
- restart-intensity guard reusing the canonical policy semantics.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.engine.live.account_artifacts import RestartIntensityPolicy
from app.marketdata.feed import MarketDataBar, MarketDataFeedError
from app.services.bot_runner import (
    BotAlreadyRunningError,
    BotTaskRegistry,
    InvalidStrategyInstanceIdError,
    MarketDataFeedUnavailableError,
    RestartIntensityRefusedError,
    UnknownBotError,
)

_SID = "alpaca-skeleton-1"
_T0 = 1_700_000_000_000


def _bar(start_ms: int, symbol: str = "SPY") -> MarketDataBar:
    return MarketDataBar(
        symbol=symbol,
        start_ms=start_ms,
        end_ms=start_ms + 60_000,
        open=Decimal("400"),
        high=Decimal("401"),
        low=Decimal("399"),
        close=Decimal("400.5"),
        volume=100,
        fetched_at_ms=start_ms + 500,
        feed_id="ibkr",
        session_phase="RTH",
    )


class _FakeFeed:
    """MarketDataFeed test double.

    ``mode``:
    - ``finite``  — yield the given bars, then end (BAR_STREAM_ENDED path).
    - ``hold``    — yield the bars, then wait forever (stop/cancel paths).
    - ``crash``   — yield the bars, then raise ``error``.
    """

    feed_id = "fake"

    def __init__(self, bars: list[MarketDataBar], *, mode: str = "hold", error: Exception | None = None) -> None:
        self._bars = bars
        self._mode = mode
        self._error = error
        self.bars_consumed = 0

    async def stream_bars(self, symbol: str, *, use_rth: bool = True):
        for bar in self._bars:
            self.bars_consumed += 1
            yield bar
        if self._mode == "crash":
            assert self._error is not None
            raise self._error
        if self._mode == "hold":
            await asyncio.Event().wait()

    def health(self):  # pragma: no cover - not exercised by the runner
        raise NotImplementedError


def _registry(
    tmp_path: Path,
    feed: _FakeFeed | None,
    *,
    policy: RestartIntensityPolicy | None = None,
) -> BotTaskRegistry:
    return BotTaskRegistry(
        tmp_path,
        feed_resolver=lambda: feed,
        restart_policy=policy or RestartIntensityPolicy(threshold=100),
        # Boot recovery has its own suite (test_boot_recovery.py).
        boot_recovery_required=False,
    )


def _lifecycle_json(tmp_path: Path, sid: str = _SID) -> dict:
    path = tmp_path / "live_state" / sid / "lifecycle_state.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _desired_json(tmp_path: Path, sid: str = _SID) -> dict:
    path = tmp_path / "live_state" / sid / "desired_state.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _binding_json(tmp_path: Path, sid: str = _SID) -> dict:
    path = tmp_path / "live_state" / sid / "broker_binding.json"
    return json.loads(path.read_text(encoding="utf-8"))


async def _wait_for(predicate, *, timeout_s: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


# ── deploy: running task + durable ON_DUTY evidence ───────────────────


@pytest.mark.asyncio
async def test_deploy_produces_running_task_and_durable_on_duty_evidence(tmp_path: Path) -> None:
    feed = _FakeFeed([_bar(_T0)], mode="hold")
    registry = _registry(tmp_path, feed)

    view = await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    assert view.running is True
    assert view.phase == "ON_DUTY"
    assert view.desired_state == "RUNNING"
    assert view.broker == "alpaca"
    assert view.active_run_id is not None

    # Durable evidence readable WITHOUT the runner (raw files).
    lifecycle = _lifecycle_json(tmp_path)
    assert lifecycle["phase"] == "ON_DUTY"
    assert lifecycle["active_run_id"] == view.active_run_id
    desired = _desired_json(tmp_path)
    assert desired["desired_state"] == "RUNNING"
    binding = _binding_json(tmp_path)
    assert binding["broker"] == "alpaca"
    assert binding["symbol"] == "SPY"
    assert binding["mode"] == "log_only"
    assert binding["quantity"] == 1
    assert isinstance(binding["created_at_ms"], int)

    await registry.stop("alpaca", _SID)


@pytest.mark.asyncio
async def test_deployed_bot_consumes_bars_and_logs_decisions(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    feed = _FakeFeed([_bar(_T0), _bar(_T0 + 60_000)], mode="hold")
    registry = _registry(tmp_path, feed)

    with caplog.at_level("INFO", logger="app.services.bot_runner"):
        await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
        await _wait_for(lambda: feed.bars_consumed == 2)
        await registry.stop("alpaca", _SID)

    decisions = [r for r in caplog.records if getattr(r, "action", None) == "bot_decision"]
    assert len(decisions) == 2
    assert decisions[0].decision == "HOLD"
    assert decisions[0].bar_start_ms == _T0


@pytest.mark.asyncio
async def test_deploy_while_running_is_refused(tmp_path: Path) -> None:
    feed = _FakeFeed([], mode="hold")
    registry = _registry(tmp_path, feed)
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    with pytest.raises(BotAlreadyRunningError):
        await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    await registry.stop("alpaca", _SID)


@pytest.mark.asyncio
async def test_deploy_without_feed_is_typed_503(tmp_path: Path) -> None:
    registry = _registry(tmp_path, None)

    with pytest.raises(MarketDataFeedUnavailableError):
        await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")


@pytest.mark.asyncio
async def test_deploy_rejects_unsafe_strategy_instance_id(tmp_path: Path) -> None:
    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))

    with pytest.raises(InvalidStrategyInstanceIdError):
        await registry.deploy(broker="alpaca", strategy_instance_id="../escape", symbol="SPY")


# ── stop: Button-Rule exit with durable intent first ──────────────────


@pytest.mark.asyncio
async def test_stop_writes_durable_intent_and_off_duty_evidence(tmp_path: Path) -> None:
    feed = _FakeFeed([_bar(_T0)], mode="hold")
    registry = _registry(tmp_path, feed)
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    view = await registry.stop("alpaca", _SID, reason="drill")

    assert view.running is False
    assert view.phase == "OFF_DUTY"
    assert view.desired_state == "STOPPED"
    assert view.duty_outcome is not None
    assert view.duty_outcome.kind == "STOPPED"
    assert view.duty_outcome.reason_code == "OPERATOR_STOP"

    # Raw artifacts agree without the runner.
    assert _desired_json(tmp_path)["desired_state"] == "STOPPED"
    lifecycle = _lifecycle_json(tmp_path)
    assert lifecycle["phase"] == "OFF_DUTY"
    assert lifecycle["active_run_id"] is None
    assert lifecycle["duty_outcome"]["kind"] == "STOPPED"


@pytest.mark.asyncio
async def test_stop_of_unknown_bot_is_404(tmp_path: Path) -> None:
    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))

    with pytest.raises(UnknownBotError):
        await registry.stop("alpaca", "never-deployed")


# ── crash: typed durable evidence distinct from a clean stop ──────────


@pytest.mark.asyncio
async def test_crash_records_typed_evidence_and_reaps(tmp_path: Path) -> None:
    feed = _FakeFeed([_bar(_T0)], mode="crash", error=RuntimeError("boom"))
    registry = _registry(tmp_path, feed)
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    await _wait_for(lambda: not registry.status("alpaca", _SID).running)

    view = registry.status("alpaca", _SID)
    assert view.running is False  # reaped — never rendered healthy
    assert view.phase == "OFF_DUTY"
    assert view.duty_outcome is not None
    assert view.duty_outcome.kind == "CRASHED"
    assert view.duty_outcome.reason_code == "RuntimeError"
    # Crash is distinct from a clean stop and preserves operator intent.
    assert view.desired_state == "RUNNING"

    lifecycle = _lifecycle_json(tmp_path)
    assert lifecycle["duty_outcome"]["kind"] == "CRASHED"


@pytest.mark.asyncio
async def test_feed_death_records_feed_death_crash(tmp_path: Path) -> None:
    feed = _FakeFeed([_bar(_T0)], mode="crash", error=MarketDataFeedError("gateway lost"))
    registry = _registry(tmp_path, feed)
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    await _wait_for(lambda: not registry.status("alpaca", _SID).running)

    view = registry.status("alpaca", _SID)
    assert view.duty_outcome is not None
    assert view.duty_outcome.kind == "CRASHED"
    assert view.duty_outcome.reason_code == "FEED_DEATH"


@pytest.mark.asyncio
async def test_kill_without_stop_intent_is_exited_unverified(tmp_path: Path) -> None:
    feed = _FakeFeed([], mode="hold")
    registry = _registry(tmp_path, feed)
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    managed_task = registry._bots[_SID].task
    managed_task.cancel()  # a kill: no stop intent recorded
    await asyncio.wait({managed_task})
    await _wait_for(lambda: _SID not in registry._bots)

    view = registry.status("alpaca", _SID)
    assert view.running is False
    assert view.duty_outcome is not None
    assert view.duty_outcome.kind == "EXITED_UNVERIFIED"
    assert view.duty_outcome.reason_code == "CANCELLED_WITHOUT_STOP_INTENT"


@pytest.mark.asyncio
async def test_bar_stream_end_is_exited_unverified(tmp_path: Path) -> None:
    feed = _FakeFeed([_bar(_T0)], mode="finite")
    registry = _registry(tmp_path, feed)
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    await _wait_for(lambda: not registry.status("alpaca", _SID).running)

    view = registry.status("alpaca", _SID)
    assert view.duty_outcome is not None
    assert view.duty_outcome.kind == "EXITED_UNVERIFIED"
    assert view.duty_outcome.reason_code == "BAR_STREAM_ENDED"


# ── restart intensity (canonical policy semantics, per bot) ───────────


@pytest.mark.asyncio
async def test_restart_intensity_refuses_thresholdth_start(tmp_path: Path) -> None:
    feed = _FakeFeed([], mode="hold")
    policy = RestartIntensityPolicy(threshold=3, window_ms=300_000)
    registry = _registry(tmp_path, feed, policy=policy)

    # Starts 1 and 2 pass (projected 1, 2 < 3); start 3 projects to the
    # threshold and is refused — mirrors project_restart_intensity_gate.
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    await registry.stop("alpaca", _SID)
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    await registry.stop("alpaca", _SID)

    with pytest.raises(RestartIntensityRefusedError):
        await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")


@pytest.mark.asyncio
async def test_restart_intensity_window_expiry_allows_restart(tmp_path: Path) -> None:
    feed = _FakeFeed([], mode="hold")
    policy = RestartIntensityPolicy(threshold=2, window_ms=1_000)
    clock = {"now": _T0}
    registry = BotTaskRegistry(
        tmp_path,
        feed_resolver=lambda: feed,
        restart_policy=policy,
        now_ms=lambda: clock["now"],
        boot_recovery_required=False,
    )

    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    await registry.stop("alpaca", _SID)
    with pytest.raises(RestartIntensityRefusedError):
        await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    clock["now"] = _T0 + 2_000  # window has passed
    view = await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    assert view.running is True
    await registry.stop("alpaca", _SID)


# ── listing and broker tags ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_bots_filters_by_broker_tag(tmp_path: Path) -> None:
    feed = _FakeFeed([], mode="hold")
    registry = _registry(tmp_path, feed)
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    assert [v.strategy_instance_id for v in registry.list_bots("alpaca")] == [_SID]
    assert registry.list_bots("ibkr") == []

    await registry.stop("alpaca", _SID)
    # Stopped bots remain on the roster (artifact-derived), just not running.
    listed = registry.list_bots("alpaca")
    assert len(listed) == 1
    assert listed[0].running is False


@pytest.mark.asyncio
async def test_status_for_wrong_broker_is_404(tmp_path: Path) -> None:
    feed = _FakeFeed([], mode="hold")
    registry = _registry(tmp_path, feed)
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    try:
        with pytest.raises(UnknownBotError):
            registry.status("ibkr", _SID)
    finally:
        await registry.stop("alpaca", _SID)


# ── shutdown ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_all_preserves_operator_intent(tmp_path: Path) -> None:
    feed = _FakeFeed([], mode="hold")
    registry = _registry(tmp_path, feed)
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    await registry.stop_all()
    await _wait_for(lambda: _SID not in registry._bots)

    view = registry.status("alpaca", _SID)
    assert view.running is False
    assert view.duty_outcome is not None
    assert view.duty_outcome.kind == "STOPPED"
    assert view.duty_outcome.reason_code == "SERVICE_SHUTDOWN"
    # Operator intent untouched: the bot still WANTS to run after a restart.
    assert view.desired_state == "RUNNING"


# ── daemon-free and container-side by construction ────────────────────


def test_runner_is_daemon_free_by_construction() -> None:
    """P10 / L1: no host daemon, host socket, or subprocess in the runner path.

    Asserted against the actual import graph (AST), not raw text, so docs
    may name the banned machinery without tripping the guard.
    """
    import ast

    import app.routers.broker_bots as router_mod
    import app.services.bot_runner as runner_mod

    banned = ("host_daemon", "daemon_client", "daemon_transport", "subprocess", "multiprocessing")
    for mod in (runner_mod, router_mod):
        tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported.append(node.module)
        for name in imported:
            assert not any(term in name for term in banned), (
                f"{mod.__name__} imports banned module {name!r}"
            )


@pytest.mark.asyncio
async def test_all_artifacts_are_written_under_the_container_root(tmp_path: Path) -> None:
    feed = _FakeFeed([_bar(_T0)], mode="hold")
    registry = _registry(tmp_path, feed)
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    await registry.stop("alpaca", _SID)

    written = sorted(
        p.relative_to(tmp_path).as_posix()
        for p in tmp_path.rglob("*")
        # .lock sidecars belong to the canonical repos' advisory-lock protocol.
        if p.is_file() and p.suffix != ".lock"
    )
    assert written == [
        f"live_state/{_SID}/broker_binding.json",
        f"live_state/{_SID}/desired_state.json",
        f"live_state/{_SID}/lifecycle_state.json",
    ]


# ── trade bot ─────────────────────────────────────────────────────────────────
#
# 2024-01-02 is a regular NYSE trading day (Tuesday after New Year's).
# All bar timestamps below are int64 ms UTC (temporal-rigor rule).
#
# ET = EST on 2024-01-02 (UTC-5):
#   session_open  = 09:30 ET = 14:30 UTC = 1_704_205_800_000 ms
#   session_close = 16:00 ET = 21:00 UTC = 1_704_229_200_000 ms
#   window_start  = open  + 15min = 1_704_206_700_000 ms  (09:45 ET)
#   window_end    = close - 15min = 1_704_228_300_000 ms  (15:45 ET)
#
# Verified against the canonical calendar module (session_window_for_date).
# bar.end_ms is the bar-close boundary per MarketDataBar semantics.

_SESSION_OPEN_MS  = 1_704_205_800_000   # 2024-01-02 09:30 ET (EST = UTC-5)
_SESSION_CLOSE_MS = 1_704_229_200_000   # 2024-01-02 16:00 ET
_WIN_START_MS     = _SESSION_OPEN_MS  + 15 * 60 * 1_000   # 09:45 ET = 1_704_206_700_000
_WIN_END_MS       = _SESSION_CLOSE_MS - 15 * 60 * 1_000   # 15:45 ET = 1_704_228_300_000


def _trade_bar(
    end_ms: int,
    *,
    open_price: str = "400.00",
    close_price: str = "401.00",
    symbol: str = "SPY",
) -> MarketDataBar:
    """A single 1-minute bar whose end_ms (bar-close) falls at a specific instant."""
    return MarketDataBar(
        symbol=symbol,
        start_ms=end_ms - 60_000,
        end_ms=end_ms,
        open=Decimal(open_price),
        high=Decimal(close_price),
        low=Decimal(open_price),
        close=Decimal(close_price),
        volume=500,
        fetched_at_ms=end_ms + 100,
        feed_id="fake",
        session_phase="RTH",
    )


def _red_bar(end_ms: int, symbol: str = "SPY") -> MarketDataBar:
    """A bar where close < open (red candle — no green streak contribution)."""
    return _trade_bar(end_ms, open_price="401.00", close_price="400.00", symbol=symbol)


def _green_bar(end_ms: int, symbol: str = "SPY") -> MarketDataBar:
    """A bar where close > open (green candle)."""
    return _trade_bar(end_ms, open_price="400.00", close_price="401.00", symbol=symbol)


class _FakeLegResult:
    """Minimal per-leg result duck-type (only order_ref is accessed by the bot)."""

    def __init__(self, order_ref: str) -> None:
        self.order_ref = order_ref


class _FakeSubmitResult:
    """Minimal submit-result duck-type for the trade bot (accesses results[0].order_ref)."""

    def __init__(self, order_ref: str) -> None:
        self.results = [_FakeLegResult(order_ref)]


class _FakeClerk:
    """Minimal AlpacaClerk double that captures submit_for_instance calls."""

    def __init__(self, *, should_raise: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._should_raise = should_raise

    async def submit_for_instance(
        self, *, strategy_instance_id: str, legs: list
    ) -> _FakeSubmitResult:
        if self._should_raise is not None:
            raise self._should_raise

        call = {
            "strategy_instance_id": strategy_instance_id,
            "legs": [{"side": leg.side.value, "quantity": leg.quantity} for leg in legs],
        }
        self.calls.append(call)
        return _FakeSubmitResult(
            order_ref=f"learn-ai/{strategy_instance_id}/v1:fake{len(self.calls):02d}"
        )


def _install_fake_clerk(monkeypatch: pytest.MonkeyPatch, clerk: _FakeClerk) -> None:
    """Patch the process-level Alpaca clerk for the duration of a test."""
    import app.broker.alpaca.clerk.clerk as clerk_mod

    monkeypatch.setattr(clerk_mod, "_clerk", clerk)


# ── entry after exactly 2 green bars in-window ────────────────────────────────


@pytest.mark.asyncio
async def test_trade_bot_enters_after_two_green_bars_in_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clerk = _FakeClerk()
    _install_fake_clerk(monkeypatch, clerk)

    # One red bar then two green bars inside the detection window, then hold.
    bars = [
        _red_bar(_WIN_START_MS + 60_000),
        _green_bar(_WIN_START_MS + 120_000),
        _green_bar(_WIN_START_MS + 180_000),
    ]
    feed = _FakeFeed(bars, mode="hold")
    registry = _registry(tmp_path, feed)

    await registry.deploy(
        broker="alpaca", strategy_instance_id=_SID, symbol="SPY", mode="trade", quantity=2
    )
    await _wait_for(lambda: feed.bars_consumed == 3)
    await registry.stop("alpaca", _SID)

    # One BUY after the second consecutive green bar.
    assert len(clerk.calls) == 1
    assert clerk.calls[0]["legs"][0]["side"] == "buy"
    assert clerk.calls[0]["legs"][0]["quantity"] == 2.0
    assert clerk.calls[0]["strategy_instance_id"] == _SID


# ── no entry before detection window ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_trade_bot_no_entry_before_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clerk = _FakeClerk()
    _install_fake_clerk(monkeypatch, clerk)

    # Two green bars strictly before the 09:45 ET window start.
    pre_window_1 = _SESSION_OPEN_MS + 60_000   # 09:31 ET
    pre_window_2 = _SESSION_OPEN_MS + 120_000  # 09:32 ET
    bars = [
        _green_bar(pre_window_1),
        _green_bar(pre_window_2),
    ]
    feed = _FakeFeed(bars, mode="hold")
    registry = _registry(tmp_path, feed)

    await registry.deploy(
        broker="alpaca", strategy_instance_id=_SID, symbol="SPY", mode="trade"
    )
    await _wait_for(lambda: feed.bars_consumed == 2)
    await registry.stop("alpaca", _SID)

    assert clerk.calls == []


# ── exit 3 bars after entry ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_trade_bot_exits_three_bars_after_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clerk = _FakeClerk()
    _install_fake_clerk(monkeypatch, clerk)

    base = _WIN_START_MS + 60_000
    bars = [
        _green_bar(base),
        _green_bar(base + 60_000),        # entry triggered after this bar
        _red_bar(base + 120_000),          # in-position bar 1
        _red_bar(base + 180_000),          # in-position bar 2
        _green_bar(base + 240_000),        # in-position bar 3 → exit
    ]
    feed = _FakeFeed(bars, mode="hold")
    registry = _registry(tmp_path, feed)

    await registry.deploy(
        broker="alpaca", strategy_instance_id=_SID, symbol="SPY", mode="trade", quantity=3
    )
    await _wait_for(lambda: feed.bars_consumed == 5)
    await registry.stop("alpaca", _SID)

    assert len(clerk.calls) == 2
    assert clerk.calls[0]["legs"][0]["side"] == "buy"
    assert clerk.calls[1]["legs"][0]["side"] == "sell"
    assert clerk.calls[1]["legs"][0]["quantity"] == 3.0


# ── window-end flatten when holding ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_trade_bot_flattens_at_window_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clerk = _FakeClerk()
    _install_fake_clerk(monkeypatch, clerk)

    base = _WIN_START_MS + 60_000
    bars = [
        _green_bar(base),
        _green_bar(base + 60_000),                  # triggers BUY
        _red_bar(base + 120_000),                   # bar 1 in position
        _green_bar(_WIN_END_MS + 1),                # past window end → FLATTEN
    ]
    feed = _FakeFeed(bars, mode="hold")
    registry = _registry(tmp_path, feed)

    await registry.deploy(
        broker="alpaca", strategy_instance_id=_SID, symbol="SPY", mode="trade", quantity=1
    )
    await _wait_for(lambda: feed.bars_consumed == 4)
    await registry.stop("alpaca", _SID)

    assert len(clerk.calls) == 2
    assert clerk.calls[0]["legs"][0]["side"] == "buy"
    assert clerk.calls[1]["legs"][0]["side"] == "sell"


# ── quantity plumbed through correctly ───────────────────────────────────────


@pytest.mark.asyncio
async def test_trade_bot_quantity_plumbed_from_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clerk = _FakeClerk()
    _install_fake_clerk(monkeypatch, clerk)

    base = _WIN_START_MS + 60_000
    bars = [_green_bar(base), _green_bar(base + 60_000)]
    feed = _FakeFeed(bars, mode="hold")
    registry = _registry(tmp_path, feed)

    await registry.deploy(
        broker="alpaca", strategy_instance_id=_SID, symbol="SPY", mode="trade", quantity=7
    )
    await _wait_for(lambda: feed.bars_consumed == 2)
    await registry.stop("alpaca", _SID)

    assert clerk.calls[0]["legs"][0]["quantity"] == 7.0

    # Binding artifact also carries quantity.
    binding = _binding_json(tmp_path)
    assert binding["quantity"] == 7
    assert binding["mode"] == "trade"


# ── submit exception → task errors (no silent handler) ───────────────────────


@pytest.mark.asyncio
async def test_trade_bot_submit_exception_crashes_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.broker.contract.errors import BrokerError

    error = BrokerError("forced failure", detail="test")
    clerk = _FakeClerk(should_raise=error)
    _install_fake_clerk(monkeypatch, clerk)

    base = _WIN_START_MS + 60_000
    bars = [_green_bar(base), _green_bar(base + 60_000)]
    feed = _FakeFeed(bars, mode="finite")
    registry = _registry(tmp_path, feed)

    await registry.deploy(
        broker="alpaca", strategy_instance_id=_SID, symbol="SPY", mode="trade"
    )
    await _wait_for(lambda: not registry.status("alpaca", _SID).running)

    view = registry.status("alpaca", _SID)
    assert view.duty_outcome is not None
    assert view.duty_outcome.kind == "CRASHED"
    assert view.duty_outcome.reason_code == "BrokerError"


# ── log_only behavior unchanged ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_log_only_bot_unchanged_after_trade_mode_added(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression: adding trade mode must not alter log_only behavior."""
    feed = _FakeFeed([_bar(_T0), _bar(_T0 + 60_000)], mode="hold")
    registry = _registry(tmp_path, feed)

    with caplog.at_level("INFO", logger="app.services.bot_runner"):
        await registry.deploy(
            broker="alpaca",
            strategy_instance_id=_SID,
            symbol="SPY",
            mode="log_only",
        )
        await _wait_for(lambda: feed.bars_consumed == 2)
        await registry.stop("alpaca", _SID)

    decisions = [r for r in caplog.records if getattr(r, "action", None) == "bot_decision"]
    assert len(decisions) == 2
    assert all(d.decision == "HOLD" for d in decisions)

    binding = _binding_json(tmp_path)
    assert binding["mode"] == "log_only"
