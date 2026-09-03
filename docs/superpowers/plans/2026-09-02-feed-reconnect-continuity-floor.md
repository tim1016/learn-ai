# Feed Reconnect Continuity — Floor (slices 1–3 + ADR) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A bot run survives a broker-socket interruption that costs it no decision, on the same run, with every affected minute proven complete from real-time data, and still finalizes `FEED_DEATH` with a typed reason otherwise — with no historical substitution path at all in this plan.

**Architecture:** Generation fencing on the IBKR client and subscription registry; a minute assembler that survives interruptions and proves RTH completeness by contribution count; a broker-neutral `ContinuityPolicy` on the feed port authored by the bot layer (decision clock, refuse-everything grant, awaited event sink); a fail-closed interruption loop inside `IbkrMarketDataFeed.stream_bars`; provenance and a run-scoped evidence journal in the source-bar ledger; the replay receipt commits to the events' digest.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, ib_async, SQLite (WAL), pytest + pytest-asyncio (`asyncio_mode=auto`), ruff.

**Spec:** `docs/superpowers/specs/2026-09-02-feed-reconnect-continuity-design.md` (revision 7). Sections cited per task: §4.2 (IBKR rules), §4.3 (port), §4.4 (bot layer), §4.5 (predicate), §4.7 (floor), §6 (matrix), §8 (tests), §10 slices 1–3 and 6.

## Global Constraints

- Every temporal value in flight, at rest, or on the wire is `int64 ms UTC`. No ISO strings, no naive datetimes, no `datetime.now()` without `tz=`, no `pd.to_datetime` without `utc=True`.
- No hardcoded session times (`time(9,30)` / `time(16,0)` / `04:00` / `20:00`) — session structure comes from `app/lean_sidecar/trading_calendar.py` (`session_open_ms_utc`, `session_close_ms_utc`, `is_trading_day`, `next_trading_day`, `session_window_for_date`).
- Decision-bucket floor is the ET-anchored floor used by `app/engine/consolidators/trade_bar_consolidator.py`. **Amended by ruling P5 during execution:** that file and `app/utils/timestamps.py` are sealed program artifacts (`registry.artifact_paths`) and must not change on this branch, so the floor is duplicated once, in `app/services/decision_clock.py::floor_to_period_ms_et`, with a provenance block naming the consolidator as canonical and a load-bearing parity test (CLAUDE.md philosophy #5). Task 6's original text ("exactly one implementation") is superseded.
- `continuity=None` on `stream_bars` must preserve today's behavior exactly (existing AC4 tests in `tests/marketdata/test_feed.py` stay unchanged and green).
- The chart aggregator (`app/services/live_bar_aggregator.py`) is not modified and its behavior does not change.
- Constants (verbatim from the spec): RTH contributions per minute `12` (= `60_000 // 5_000`); delivery allowance `20_000` ms; wait poll `250` ms; kill switch env `IBKR_FEED_CONTINUITY_ENABLED` (default `true`).
- Provenance literals: `"realtime"`, `"realtime_across_reconnect"`, `"historical_substitute"`, `"history"`. Event kinds: `"interruption"`, `"recovered"`, `"gap"`, `"substituted"`, `"refused"`. Interruption causes: `"socket_down"`, `"soft_loss_1100"`, `"stall"`, `"generation_changed"`.
- Typed feed refusal reasons used in this plan: `DECISION_BAR_MISSED`, `SOURCE_MINUTE_UNRESOLVABLE`, `SUBSTITUTION_NOT_AUTHORIZED`, `SUBSTITUTION_PATH_UNAVAILABLE`, `CONTINUITY_EVIDENCE_UNWRITABLE`, `DECISION_LATE`. Every refusal reaches the run as `MarketDataFeedError` whose message starts with `<REASON>: `, and the run finalizes `FEED_DEATH` exactly as today.
- Structured logging only: `logger.<level>("…", extra={"action": "<snake_case>", …})`. No `print`. No silent `except`.
- Pydantic v2 (`model_validator`, `ConfigDict`), frozen models for facts; `from __future__ import annotations` in every module.
- Follow the patterns already in each file; do not reformat untouched code. Keep `app/broker/ibkr/bars.py` from growing: aggregation primitives move out (Task 3).
- Tests: `tests/<area>/test_<thing>.py`, `test_<function>_<scenario>` names, no `print`, pristine output.
- Commands (run from `PythonDataService/` inside this worktree, using the main checkout's venv):
  - tests: `DATA_PLANE_CONTROL_SECRET="" /Users/inkant/learn-ai/PythonDataService/.venv/bin/python -m pytest <paths> -q -p no:cacheprovider`
  - lint: `/Users/inkant/learn-ai/PythonDataService/.venv/bin/ruff check app/ tests/`
  - contract: `/Users/inkant/learn-ai/PythonDataService/.venv/bin/python scripts/export_openapi_contract.py --check` (regenerate without `--check` when it fails after a schema change; commit `contracts/openapi/python-data-service.openapi.json`).
- Commit messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. Commit with plain `git add <paths>` and `git commit -m` from the worktree root; never `cd` elsewhere.
- Rulings recorded by the controller for this plan (binding):
  - R1: `decision_session="all"` gets **no** continuity policy in this plan (the canonical calendar exposes RTH only; extended windows are broker-proven, not scheduled — `app/services/session_authority.py`). Such bindings keep today's fail-fast; a structured log names the reason.
  - R2: The first minute of generation 1 (deploy mid-minute) is emitted exactly as today (provenance `"realtime"`), not treated as unresolvable; only minutes that span an interruption or fall inside an interruption window are subject to the unresolvable rule (spec §9 Q4 is open).
  - R3: With no substitution path, a `SubstitutionGrant` can never be honored; if a grant function ever returns one, the feed refuses with `SUBSTITUTION_PATH_UNAVAILABLE` (fail closed), never fetches.

---

## File map

| File | Responsibility after this plan |
|---|---|
| `app/broker/ibkr/client.py` | + `connection_generation` (monotonic, +1 per successful `connect()`) |
| `app/broker/ibkr/minute_assembler.py` (new) | Aggregation primitives moved from `bars.py` (`DuplicatePolicy`, `LiveBarCounters`, `_Contribution`, `_MinuteAccumulator`, `_handle_duplicate`, `aggregate_realtime_bar`) + `MinuteAssembler` (survives interruptions, counts contributions, flushes complete minutes) |
| `app/broker/ibkr/bar_models.py` | `IbkrMinuteBar` + `contribution_count`, `spans_interruption` |
| `app/broker/ibkr/bars.py` | Re-exports the moved primitives; `IBKRBarInterrupted`; generation on lease/subscription/key; stale release never cancels; acquisition re-check; liveness check every iteration; `stream_minute_bars(..., assembler=None)` |
| `app/broker/ibkr/config.py` | `feed_continuity_enabled: bool = True` |
| `app/marketdata/feed.py` | Port: `MarketDataBar` provenance fields, `FeedContinuityEvent`, `ContinuityEventRef`, `SubstitutionGrant`/`SubstitutionRefusal`, `ContinuityPolicy`, `MarketDataFeedError(reason=…)`, `stream_bars(..., continuity=None)` |
| `app/marketdata/ibkr_continuity.py` (new) | The interruption loop helpers: wait-for-healthy under deadline, resolve emitted/missed minutes, event emission through the sink |
| `app/marketdata/ibkr_feed.py` | `stream_bars` dispatches to the legacy loop (`continuity=None` or kill switch off) or the continuity loop |
| `app/utils/timestamps.py` | + `floor_to_period_ms_et(timestamp_ms, period_ms)` (single ET floor) |
| `app/engine/consolidators/trade_bar_consolidator.py` | `_floor_to_period_ms` delegates to the shared floor |
| `app/services/decision_clock.py` (new) | `decision_timeframe_ms_for_binding`, `rth_trigger_instants`, `next_trigger_ms`, `rth_next_trigger_function` |
| `app/services/source_bar_ledger.py` | Migration: bar provenance columns; `source_stream_events`; `source_evidence_journal`; `append(bar, run_id=…)`, `append_event`, `events`, `evidence_end_seq`; `RetainedSourceBar` + provenance/run/evidence fields; `RetainedContinuityEvent` |
| `app/services/feed_continuity_policy.py` (new) | `continuity_policy_for(binding, ledger)` — bot-layer author of the policy (trigger fn, refuse-all grant, run-scoped sink); `FeedContinuityRefused` |
| `app/services/bot_trade_strategy.py` | `_RetainedSourceBarFeed` takes `run_id` + `continuity`, forwards it, admits on delivery, appends with `run_id`; `run_trade_bot`/`run_dry_run_bot` build the policy |
| `app/services/bot_runtime.py` | `PauseAwareFeed.stream_bars` forwards `continuity` |
| `app/services/run_replay_proof.py` | imports the seal timeframe reader from `decision_clock`; `bar_set_digest` provenance rule; `continuity_event_digest`; events read; `evidence_end_seq` snapshot and bound |
| `app/schemas/run_replay.py` | `RunReplayReceipt` + `continuity_event_digest`, `evidence_end_seq` |
| `app/broker/alpaca/clerk/sqlite/qualification_polygon_replay.py`, `tests/_helpers/bot_runner/doubles.py` | accept the `continuity` kwarg |
| `docs/architecture/adrs/0053-feed-continuity-same-run-recovery.md` (new), `docs/references/feed-reconnect-continuity.md` (new) | Decision record and reference note |

---

### Task 1: `IbkrClient.connection_generation`

**Files:**
- Modify: `app/broker/ibkr/client.py` (fields near line 265; `connect()` success point near line 500 where `self._connected_account = account_id`)
- Test: `tests/broker/ibkr/test_client_connection_generation.py` (new)

**Interfaces:**
- Produces: `IbkrClient.connection_generation -> int` (property; `0` before the first successful connect; `+1` after each successful `connect()`, i.e. after the sentinel checks pass).

- [ ] **Step 1: Write the failing test**

```python
"""connection_generation increments once per successful connect (spec §4.2 rule 1)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.broker.ibkr import client as client_module
from app.broker.ibkr.client import IbkrClient


class _FakeIbClient:
    def serverVersion(self) -> int:
        return 178


class _FakeIb:
    def __init__(self) -> None:
        self.client = _FakeIbClient()
        self._connected = False

    async def connectAsync(self, **kwargs) -> None:
        self._connected = True

    def isConnected(self) -> bool:
        return self._connected

    def disconnect(self) -> None:
        self._connected = False

    def managedAccounts(self) -> list[str]:
        return ["DU123456"]


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> IbkrClient:
    monkeypatch.setattr(client_module, "apply_tcp_keepalive", lambda ib: None)
    instance = IbkrClient()
    monkeypatch.setattr(instance, "_ib", _FakeIb())
    return instance


async def test_connection_generation_starts_at_zero(client: IbkrClient) -> None:
    assert client.connection_generation == 0


async def test_connection_generation_increments_per_successful_connect(client: IbkrClient) -> None:
    await client.connect()
    assert client.connection_generation == 1

    await client.disconnect()
    await client.connect()
    assert client.connection_generation == 2


async def test_connection_generation_unchanged_on_failed_connect(
    client: IbkrClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _refuse(**kwargs) -> None:
        raise OSError("refused")

    monkeypatch.setattr(client._ib, "connectAsync", _refuse)
    with pytest.raises(Exception):
        await client.connect()
    assert client.connection_generation == 0
```

- [ ] **Step 2: Run it to verify it fails**

Run: `DATA_PLANE_CONTROL_SECRET="" /Users/inkant/learn-ai/PythonDataService/.venv/bin/python -m pytest tests/broker/ibkr/test_client_connection_generation.py -q -p no:cacheprovider`
Expected: FAIL — `AttributeError: 'IbkrClient' object has no attribute 'connection_generation'`.

If `IbkrClient()` needs settings the test environment lacks, construct it as the existing client tests do (look in `tests/broker/ibkr/` for an `IbkrClient(` construction and copy its settings handling); keep the assertions unchanged. The fake IB's `errorEvent` hook: `IbkrClient.__init__` does `self._ib.errorEvent += self._on_ib_error` on the real `IB()` before the monkeypatch, so the fake needs no `errorEvent`.

- [ ] **Step 3: Implement**

In `__init__`, next to `self._connection_lost: bool = False`:

```python
        # Monotonic count of successful ``connect()`` calls. Every real-time
        # bar lease records the generation it was acquired under so a lease
        # from a previous socket can be fenced (spec #1921 §4.2 rule 1).
        self._connection_generation: int = 0
```

In `connect()`, immediately after `self._connected_account = account_id` (before `self.mark_recovery_succeeded()`):

```python
        self._connection_generation += 1
```

Add the accessor next to `connection_lost`:

```python
    @property
    def connection_generation(self) -> int:
        """Number of successful connects this process has made; fences stale leases."""
        return self._connection_generation
```

- [ ] **Step 4: Run the test to verify it passes**

Run the same command. Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add app/broker/ibkr/client.py tests/broker/ibkr/test_client_connection_generation.py
git commit -m "feat(ibkr): count successful connects as a connection generation

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Generation fencing in the real-time bar registry

**Files:**
- Modify: `app/broker/ibkr/bars.py` — exceptions (lines 70–75), `_SubscriptionKey` (134), `_RealtimeBarSubscription` (137–142), `_RealtimeBarLease` (145–176), `_RealtimeBarSubscriptionRegistry.acquire/release/invalidate` (200–318), `_check_realtime_subscription_liveness` (459–488), `stream_minute_bars` loop (1005–1035), `stream_raw_5s_bars` loop (same shape)
- Test: `tests/broker/ibkr/test_bars.py` (extend `_FakeClient`; add tests at the end)

**Interfaces:**
- Consumes: `IbkrClient.connection_generation` (Task 1). Fakes without the attribute are treated as generation `0` via `_client_generation(client) = int(getattr(client, "connection_generation", 0))`.
- Produces: `class IBKRBarInterrupted(IBKRBarStreamError)` with attribute `cause: str` ∈ {`"socket_down"`, `"soft_loss_1100"`, `"generation_changed"`}; `_RealtimeBarSubscription.generation: int`; `_RealtimeBarLease.generation: int`; key `(id(client), generation, con_id, bar_size, what_to_show, use_rth)`.

- [ ] **Step 1: Write the failing tests** (append to `tests/broker/ibkr/test_bars.py`; add `IBKRBarInterrupted`, `_REALTIME_BAR_SUBSCRIPTIONS`, `_RealtimeBarSubscriptionRegistry` to the imports)

```python
class _GenClient(_FakeClient):
    """Fake client whose generation the test can bump."""

    def __init__(self, *, connected: bool = True, connection_lost: bool = False) -> None:
        super().__init__(connected=connected, connection_lost=connection_lost)
        self.connection_generation = 1


@pytest.fixture(autouse=True)
def _fresh_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.broker.ibkr.bars._REALTIME_BAR_SUBSCRIPTIONS", _RealtimeBarSubscriptionRegistry())


@pytest.mark.asyncio
async def test_stale_generation_lease_raises_interrupted_even_when_socket_is_back() -> None:
    client = _GenClient()
    client.ib.bars = []  # nothing to deliver: force the idle branch
    stream = stream_minute_bars(client, "SPY", use_rth=True, stall_timeout_s=60.0)
    first = asyncio.ensure_future(stream.__anext__())
    await asyncio.sleep(0.15)  # one idle iteration under generation 1
    client.connection_generation = 2  # reconnect happened; socket reports connected
    with pytest.raises(IBKRBarInterrupted) as excinfo:
        await asyncio.wait_for(first, timeout=2.0)
    assert excinfo.value.cause == "generation_changed"
    await stream.aclose()


@pytest.mark.asyncio
async def test_socket_down_raises_interrupted_with_cause() -> None:
    client = _GenClient()
    client.ib.bars = []
    stream = stream_minute_bars(client, "SPY", use_rth=True)
    first = asyncio.ensure_future(stream.__anext__())
    await asyncio.sleep(0.15)
    client._connected = False
    with pytest.raises(IBKRBarInterrupted) as excinfo:
        await asyncio.wait_for(first, timeout=2.0)
    assert excinfo.value.cause == "socket_down"
    assert "IBKR connection lost" in str(excinfo.value)
    await stream.aclose()


@pytest.mark.asyncio
async def test_stale_lease_release_never_cancels_on_the_new_socket() -> None:
    client = _GenClient()
    client.ib.bars = []
    stream = stream_minute_bars(client, "SPY", use_rth=True)
    first = asyncio.ensure_future(stream.__anext__())
    await asyncio.sleep(0.15)
    client.connection_generation = 2
    with pytest.raises(IBKRBarInterrupted):
        await asyncio.wait_for(first, timeout=2.0)
    await stream.aclose()  # releases the stale lease
    assert client.ib.realtime_bar_cancel_count == 0


@pytest.mark.asyncio
async def test_acquire_after_generation_change_opens_a_new_line() -> None:
    from app.broker.ibkr import bars as bars_module

    client = _GenClient()
    contract = SimpleNamespace(conId=1, symbol="SPY", secType="STK")
    registry = bars_module._REALTIME_BAR_SUBSCRIPTIONS
    lease_old = await registry.acquire(client, contract, bar_size=5, what_to_show="TRADES", use_rth=True)
    client.connection_generation = 2
    lease_new = await registry.acquire(client, contract, bar_size=5, what_to_show="TRADES", use_rth=True)
    assert lease_new.multiplexed is False
    assert lease_new.generation == 2
    assert lease_old.generation == 1
    assert client.ib.realtime_bar_request_count == 2
    lease_new.release()
    lease_old.release()
    assert client.ib.realtime_bar_cancel_count == 1  # only the live generation cancelled


@pytest.mark.asyncio
async def test_acquire_restarts_when_generation_moves_during_pacing(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.broker.ibkr import bars as bars_module

    client = _GenClient()
    contract = SimpleNamespace(conId=1, symbol="SPY", secType="STK")
    registry = bars_module._REALTIME_BAR_SUBSCRIPTIONS

    async def _bump_generation() -> None:
        client.connection_generation = 2

    monkeypatch.setattr(registry._pacer, "acquire", _bump_generation)
    lease = await registry.acquire(client, contract, bar_size=5, what_to_show="TRADES", use_rth=True)
    assert lease.generation == 2
    lease.release()
```

- [ ] **Step 2: Run to verify they fail**

Run: `DATA_PLANE_CONTROL_SECRET="" /Users/inkant/learn-ai/PythonDataService/.venv/bin/python -m pytest tests/broker/ibkr/test_bars.py -q -p no:cacheprovider -k "generation or interrupted or stale"`
Expected: FAIL — `ImportError: cannot import name 'IBKRBarInterrupted'`.

- [ ] **Step 3: Implement**

Exceptions:

```python
class IBKRBarInterrupted(IBKRBarStreamError):
    """A broker line stopped for a survivable reason: socket down, 1100 soft loss, or reconnect.

    Consumers with a continuity policy recover from this; everyone else treats it
    as the fatal ``IBKRBarStreamError`` it subclasses.
    """

    def __init__(
        self, message: str, *, cause: Literal["socket_down", "soft_loss_1100", "generation_changed"]
    ) -> None:
        super().__init__(message)
        self.cause = cause


def _client_generation(client: object) -> int:
    return int(getattr(client, "connection_generation", 0))
```

Key and dataclasses:

```python
_SubscriptionKey = tuple[int, int, int, int, str, bool]  # id(client), generation, conId, barSize, what, useRTH


@dataclass
class _RealtimeBarSubscription:
    client: IbkrClient
    bars: list[object]
    generation: int
    consumer_count: int = 1
    invalidated: bool = False
```

`_RealtimeBarLease` gains `generation: int` (populate from the subscription in both `acquire` branches).

`acquire`: compute `generation = _client_generation(client)` at the top of each pass of the `while True` loop; evict stale entries first; build the key with the generation. After `await self._pacer.acquire()`, if `_client_generation(client) != generation`, resolve the pending future, drop it from `_pending`, and restart the acquisition from the top (a loop, not recursion). The `_max_active_for_client` reservation count only counts keys whose generation equals the current one.

```python
    def _evict_older_generations(self, client: IbkrClient, generation: int) -> None:
        """Drop registry entries whose socket is gone; never send a cancel for them."""
        stale = [key for key in self._subscriptions if key[0] == id(client) and key[1] < generation]
        for key in stale:
            subscription = self._subscriptions.pop(key)
            subscription.invalidated = True
            logger.info(
                "Evicted real-time-bar subscription from a previous connection generation",
                extra={"action": "ibkr_realtime_bar_generation_evicted", "generation": key[1], "con_id": key[2]},
            )
```

`release` and `invalidate`: before calling `cancelRealTimeBars`, guard:

```python
        if subscription.generation != _client_generation(subscription.client):
            # The reqId may already belong to a subscription on the new socket
            # (ib_async restarts reqIds on reconnect): never cancel across generations.
            self._subscriptions.pop(key, None)
            return False
```

`_check_realtime_subscription_liveness`:

```python
    if lease.generation != _client_generation(client):
        raise IBKRBarInterrupted(
            f"IBKR connection was re-established while streaming {symbol} 5-second bars; "
            "this lease belongs to the previous socket.",
            cause="generation_changed",
        )
    connected = client.is_connected()
    connection_lost = client.connection_lost
    if not connected:
        raise IBKRBarInterrupted(
            f"IBKR connection lost while streaming {symbol} 5-second bars; "
            "halting rather than hanging on a dead feed.",
            cause="socket_down",
        )
    if connection_lost:
        raise IBKRBarInterrupted(
            f"IBKR connectivity lost (code 1100) while streaming {symbol} 5-second bars; "
            "halting rather than streaming a dead feed.",
            cause="soft_loss_1100",
        )
```

`stream_minute_bars` and `stream_raw_5s_bars`: call the liveness check at the top of every iteration (before `if index >= len(bars)`), keeping `maybe_log_no_bar` and the `await asyncio.sleep(0.1)` inside the idle branch.

- [ ] **Step 4: Run the whole bars suite**

Run: `DATA_PLANE_CONTROL_SECRET="" /Users/inkant/learn-ai/PythonDataService/.venv/bin/python -m pytest tests/broker/ibkr/test_bars.py tests/marketdata/test_feed.py -q -p no:cacheprovider`
Expected: all pass (the existing "IBKR connection lost" match in `test_feed.py` still holds because the socket-down message is unchanged).

- [ ] **Step 5: Commit**

```bash
git add app/broker/ibkr/bars.py tests/broker/ibkr/test_bars.py
git commit -m "feat(ibkr): fence real-time bar leases by connection generation

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: `MinuteAssembler` — aggregation that survives an interruption

**Files:**
- Create: `app/broker/ibkr/minute_assembler.py`
- Modify: `app/broker/ibkr/bars.py` (remove the moved definitions, re-export them, thread `assembler` through `stream_minute_bars`), `app/broker/ibkr/bar_models.py` (`IbkrMinuteBar` fields)
- Test: `tests/broker/ibkr/test_minute_assembler.py` (new); `tests/broker/ibkr/test_bars.py` keeps importing `aggregate_realtime_bar`, `LiveBarCounters` from `bars` (re-export)

**Interfaces:**
- Produces: `class MinuteAssembler` with `feed(raw_bar, *, symbol, generation, venue, use_rth) -> IbkrMinuteBar | None`, `flush_if_complete() -> IbkrMinuteBar | None`, `open_minute_start_ms -> int | None`, `last_source_ms: int | None`, `counters: LiveBarCounters`; constant `RTH_CONTRIBUTIONS_PER_MINUTE = 60_000 // 5_000`; `IbkrMinuteBar.contribution_count: int | None = None`, `IbkrMinuteBar.spans_interruption: bool = False`; `stream_minute_bars(client, symbol, *, use_rth=True, on_source_bar=None, stall_timeout_s=…, assembler: MinuteAssembler | None = None)`.

- [ ] **Step 1: Write the failing tests**

```python
"""MinuteAssembler survives an interruption and proves completeness by count (spec §4.2 rules 2–3)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from app.broker.ibkr.minute_assembler import RTH_CONTRIBUTIONS_PER_MINUTE, MinuteAssembler

_MINUTE = datetime(2026, 9, 2, 19, 0, 0, tzinfo=UTC)  # 15:00 ET, RTH


def _raw(second: int, close: str = "100", volume: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        time=_MINUTE.replace(second=second),
        open=Decimal(close), high=Decimal(close), low=Decimal(close), close=Decimal(close), volume=volume,
    )


def _next_minute_raw() -> SimpleNamespace:
    return SimpleNamespace(
        time=_MINUTE.replace(minute=1, second=0),
        open=Decimal("101"), high=Decimal("101"), low=Decimal("101"), close=Decimal("101"), volume=1,
    )


def test_contributions_from_two_generations_merge_into_one_complete_minute() -> None:
    assembler = MinuteAssembler()
    for second in range(0, 45, 5):  # 9 bars under generation 1
        assert assembler.feed(_raw(second), symbol="SPY", generation=1, venue="ARCA", use_rth=True) is None
    for second in range(45, 60, 5):  # 3 bars under generation 2
        assert assembler.feed(_raw(second), symbol="SPY", generation=2, venue="ARCA", use_rth=True) is None
    emitted = assembler.feed(_next_minute_raw(), symbol="SPY", generation=2, venue="ARCA", use_rth=True)
    assert emitted is not None
    assert emitted.contribution_count == RTH_CONTRIBUTIONS_PER_MINUTE == 12
    assert emitted.spans_interruption is True
    assert emitted.volume == 12


def test_lost_contribution_is_visible_in_the_count() -> None:
    assembler = MinuteAssembler()
    for second in (0, 5, 10, 15, 20, 25, 30, 35, 40, 50, 55):  # 45 missing
        assembler.feed(_raw(second), symbol="SPY", generation=1 if second < 45 else 2, venue=None, use_rth=True)
    emitted = assembler.feed(_next_minute_raw(), symbol="SPY", generation=2, venue=None, use_rth=True)
    assert emitted is not None
    assert emitted.contribution_count == 11
    assert emitted.spans_interruption is True


def test_redelivered_bar_after_reconnect_is_absorbed_idempotently() -> None:
    assembler = MinuteAssembler()
    assembler.feed(_raw(0), symbol="SPY", generation=1, venue=None, use_rth=True)
    assembler.feed(_raw(0), symbol="SPY", generation=2, venue=None, use_rth=True)  # exact redelivery
    assert assembler.counters.skipped_duplicate == 1
    emitted = assembler.feed(_next_minute_raw(), symbol="SPY", generation=2, venue=None, use_rth=True)
    assert emitted is not None and emitted.contribution_count == 1


def test_single_generation_minute_does_not_span_an_interruption() -> None:
    assembler = MinuteAssembler()
    for second in range(0, 60, 5):
        assembler.feed(_raw(second), symbol="SPY", generation=1, venue=None, use_rth=True)
    emitted = assembler.feed(_next_minute_raw(), symbol="SPY", generation=1, venue=None, use_rth=True)
    assert emitted is not None
    assert emitted.spans_interruption is False
    assert emitted.contribution_count == 12


def test_flush_if_complete_emits_only_a_full_open_minute() -> None:
    assembler = MinuteAssembler()
    for second in range(0, 55, 5):
        assembler.feed(_raw(second), symbol="SPY", generation=1, venue=None, use_rth=True)
    assert assembler.flush_if_complete() is None  # 11/12
    assembler.feed(_raw(55), symbol="SPY", generation=1, venue=None, use_rth=True)
    flushed = assembler.flush_if_complete()
    assert flushed is not None and flushed.contribution_count == 12
    assert assembler.open_minute_start_ms is None
    assert assembler.flush_if_complete() is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `DATA_PLANE_CONTROL_SECRET="" /Users/inkant/learn-ai/PythonDataService/.venv/bin/python -m pytest tests/broker/ibkr/test_minute_assembler.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: app.broker.ibkr.minute_assembler`.

- [ ] **Step 3: Implement**

Create `app/broker/ibkr/minute_assembler.py`. Move from `bars.py` verbatim (with the imports they need): `DuplicatePolicy`, `LiveBarCounters`, `_Contribution`, `_MinuteAccumulator`, `_decimal_attr`, `_volume_attr`, `_bar_time_ms`, `_contribution`, `_handle_duplicate`, `aggregate_realtime_bar`, and the helpers only they use (`_to_utc_ms`, `_minute_start_ms`, `_session_phase_for_ms`). In `bars.py`, replace the moved definitions with a re-export so every existing importer keeps working:

```python
from app.broker.ibkr.minute_assembler import (  # noqa: F401 — re-exported for existing importers
    DuplicatePolicy,
    LiveBarCounters,
    MinuteAssembler,
    _MinuteAccumulator,
    _minute_start_ms,
    _session_phase_for_ms,
    _to_utc_ms,
    aggregate_realtime_bar,
)
```

Add to `_MinuteAccumulator`: `generations: set[int] = field(default_factory=set)`; `aggregate_realtime_bar` gains keyword `generation: int = 0` and adds it to the accumulator that receives the contribution; `_MinuteAccumulator.to_model()` sets `contribution_count=len(self.contributions)` and `spans_interruption=len(self.generations) > 1`. Add to `IbkrMinuteBar`:

```python
    contribution_count: int | None = Field(
        default=None, ge=0, description="5-second bars folded into this minute; None when unknown (historical)."
    )
    spans_interruption: bool = Field(
        default=False, description="Contributions arrived over more than one connection generation."
    )
```

The assembler:

```python
RTH_CONTRIBUTIONS_PER_MINUTE: int = 60_000 // 5_000
"""IBKR pushes one 5-second TRADES bar every 5 s in RTH (measured 12/12 on 2026-09-02)."""


@dataclass
class MinuteAssembler:
    """Fold 5-second bars into closed minutes across subscription generations.

    Owned by the consumer of ``stream_minute_bars`` so an interruption (socket
    drop, 1100 soft loss, stall replacement) never discards the open minute;
    contributions are keyed by source timestamp, so bars from the old and the
    new socket merge deterministically and a redelivery is absorbed by the
    ``live_idempotent`` policy.
    """

    current: _MinuteAccumulator | None = None
    last_source_ms: int | None = None
    counters: LiveBarCounters = field(default_factory=LiveBarCounters)

    @property
    def open_minute_start_ms(self) -> int | None:
        return None if self.current is None else self.current.start_ms

    def feed(
        self, raw_bar: object, *, symbol: str, generation: int, venue: str | None, use_rth: bool
    ) -> IbkrMinuteBar | None:
        self.current, emitted, self.last_source_ms = aggregate_realtime_bar(
            self.current,
            raw_bar,
            symbol=symbol,
            last_source_ms=self.last_source_ms,
            policy="live_idempotent",
            counters=self.counters,
            venue=venue,
            use_rth=use_rth,
            provenance="ibkr_realtime",
            generation=generation,
        )
        return emitted

    def flush_if_complete(self) -> IbkrMinuteBar | None:
        """Emit the open minute now iff it already holds every RTH contribution."""
        if self.current is None or len(self.current.contributions) < RTH_CONTRIBUTIONS_PER_MINUTE:
            return None
        emitted = self.current.to_model()
        self.current = None
        return emitted
```

`stream_minute_bars`: accept `assembler: MinuteAssembler | None = None`; `assembler = assembler or MinuteAssembler()`; replace the local `current`/`last_source_ms`/`counters` with the assembler (`emitted = assembler.feed(raw_bar, symbol=sym, generation=lease.generation, venue=venue, use_rth=use_rth)`; progress detection compares `assembler.last_source_ms` before and after; the `finally` debug log reads `assembler.counters`).

- [ ] **Step 4: Run the suites**

Run: `DATA_PLANE_CONTROL_SECRET="" /Users/inkant/learn-ai/PythonDataService/.venv/bin/python -m pytest tests/broker/ibkr/ tests/marketdata/test_feed.py -q -p no:cacheprovider`; then `grep -rl "live_bar_aggregator" tests/ | head` and run those files too.
Expected: all pass; `wc -l app/broker/ibkr/bars.py` is smaller than before.

- [ ] **Step 5: Commit**

```bash
git add app/broker/ibkr/minute_assembler.py app/broker/ibkr/bars.py app/broker/ibkr/bar_models.py tests/broker/ibkr/test_minute_assembler.py tests/broker/ibkr/test_bars.py
git commit -m "feat(ibkr): minute assembler that survives interruptions and counts contributions

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Port types — provenance, events, `ContinuityPolicy`

**Files:**
- Modify: `app/marketdata/feed.py`; `app/marketdata/ibkr_feed.py` (`stream_bars` signature + `_translate` provenance mapping only); `app/services/bot_runtime.py` (`PauseAwareFeed.stream_bars`); `app/services/bot_trade_strategy.py` (`_RetainedSourceBarFeed.stream_bars` signature only); `app/broker/alpaca/clerk/sqlite/qualification_polygon_replay.py` (accept kwarg); `tests/_helpers/bot_runner/doubles.py` (`_FakeFeed.stream_bars` accepts kwarg)
- Test: `tests/marketdata/test_feed.py` (append)

**Interfaces (produces, verbatim):**

```python
BarProvenanceTag = Literal["realtime", "realtime_across_reconnect", "historical_substitute", "history"]
ContinuityEventKind = Literal["interruption", "recovered", "gap", "substituted", "refused"]
InterruptionCause = Literal["socket_down", "soft_loss_1100", "stall", "generation_changed"]
DecisionSession = Literal["rth", "all"]

class MarketDataFeedError(Exception):
    def __init__(self, message: str, *, reason: str | None = None) -> None  # str(error) is f"{reason}: {message}" when reason given

class MarketDataBar(BaseModel):  # + fields
    provenance: BarProvenanceTag = "realtime"
    authorization_id: str | None = None
    continuity_event_ref: str | None = None

class FeedContinuityEvent(BaseModel):  # frozen
    kind: ContinuityEventKind; feed_id: str; symbol: str; observed_at_ms: int
    cause: InterruptionCause | None = None
    generation_from: int | None = None; generation_to: int | None = None
    window_start_ms: int | None = None; window_end_ms: int | None = None
    bar_identity: str | None = None; authorization_id: str | None = None
    reason: str | None = None; last_delivered_end_ms: int | None = None; deadline_ms: int | None = None

class ContinuityEventRef(BaseModel):  # frozen
    run_id: str; evidence_seq: int
    def ref(self) -> str: return f"{self.run_id}:{self.evidence_seq}"

class SubstitutionGrant(BaseModel):  # frozen
    authorization_id: str; window_start_ms: int; window_end_ms: int

class SubstitutionRefusal(BaseModel):  # frozen
    reason: Literal["SUBSTITUTION_NOT_AUTHORIZED", "SUBSTITUTION_SHAPE_UNPROVEN", "SUBSTITUTION_WARMUP_TAINTED"]

@dataclass(frozen=True)
class ContinuityPolicy:
    decision_session: DecisionSession
    next_trigger_ms: Callable[[int], int]
    substitution_grant: Callable[[int, int], SubstitutionGrant | SubstitutionRefusal]
    record_event: Callable[[FeedContinuityEvent], Awaitable[ContinuityEventRef]]
    delivery_allowance_ms: int = 20_000
    def deadline_ms(self, last_delivered_end_ms: int) -> int: return self.next_trigger_ms(last_delivered_end_ms) + self.delivery_allowance_ms
    def is_trigger_ms(self, end_ms: int) -> bool: return self.next_trigger_ms(end_ms - 1) == end_ms
```

`MarketDataFeed.stream_bars(self, symbol: str, *, use_rth: bool = True, continuity: ContinuityPolicy | None = None)`.

- [ ] **Step 1: Write the failing tests** (append to `tests/marketdata/test_feed.py`; add a helper `_make_ibkr_bar_with(**overrides)` next to `_make_ibkr_bar` that builds the same `SimpleNamespace` with `contribution_count=12`, `spans_interruption=False`, `provenance="ibkr_realtime"` defaults overridden by `overrides`; also add `contribution_count=12, spans_interruption=False` to `_make_ibkr_bar`)

```python
def test_market_data_bar_provenance_defaults_to_realtime() -> None:
    bar = MarketDataBar(
        symbol="SPY", start_ms=0, end_ms=60_000, open=Decimal("1"), high=Decimal("1"),
        low=Decimal("1"), close=Decimal("1"), volume=0, fetched_at_ms=60_000, feed_id="ibkr",
    )
    assert bar.provenance == "realtime"
    assert bar.authorization_id is None and bar.continuity_event_ref is None


def test_market_data_feed_error_carries_a_typed_reason() -> None:
    error = MarketDataFeedError("deadline passed", reason="DECISION_BAR_MISSED")
    assert error.reason == "DECISION_BAR_MISSED"
    assert str(error) == "DECISION_BAR_MISSED: deadline passed"
    assert MarketDataFeedError("plain").reason is None


def test_continuity_policy_deadline_and_trigger_detection() -> None:
    from app.marketdata.feed import ContinuityPolicy, SubstitutionRefusal

    async def _sink(event):  # pragma: no cover - never called here
        raise AssertionError

    def _next_trigger(last_end: int) -> int:
        # Fake decision clock: triggers at k * 15 min + 60 s; smallest one strictly after last_end.
        candidate = (last_end // 900_000) * 900_000 + 60_000
        return candidate if candidate > last_end else candidate + 900_000

    policy = ContinuityPolicy(
        decision_session="rth",
        next_trigger_ms=_next_trigger,
        substitution_grant=lambda start, end: SubstitutionRefusal(reason="SUBSTITUTION_NOT_AUTHORIZED"),
        record_event=_sink,
    )
    assert policy.delivery_allowance_ms == 20_000
    assert policy.deadline_ms(900_000) == 960_000 + 20_000
    assert policy.is_trigger_ms(1_860_000) is True
    assert policy.is_trigger_ms(1_800_000) is False


def test_translate_maps_ibkr_provenance_to_port_provenance() -> None:
    assert IbkrMarketDataFeed._translate(_make_ibkr_bar()).provenance == "realtime"
    spanning = IbkrMarketDataFeed._translate(_make_ibkr_bar_with(spans_interruption=True))
    assert spanning.provenance == "realtime_across_reconnect"
    historical = IbkrMarketDataFeed._translate(_make_ibkr_bar_with(provenance="ibkr_historical"))
    assert historical.provenance == "history"


async def test_stream_bars_accepts_continuity_none_and_behaves_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    bar = _make_ibkr_bar()

    async def fake_source(_client, _symbol, *, use_rth=True, on_source_bar=None, **_kwargs):
        yield bar

    monkeypatch.setattr("app.marketdata.ibkr_feed.stream_minute_bars", fake_source)
    feed = IbkrMarketDataFeed(_fake_connected_client())
    observed = await anext(feed.stream_bars("SPY", continuity=None))
    assert observed.start_ms == bar.start_ms
```

- [ ] **Step 2: Run to verify they fail**

Run: `DATA_PLANE_CONTROL_SECRET="" /Users/inkant/learn-ai/PythonDataService/.venv/bin/python -m pytest tests/marketdata/test_feed.py -q -p no:cacheprovider -k "provenance or reason or continuity_policy or translate_maps or behaves_as_before"`
Expected: FAIL (missing fields / names).

- [ ] **Step 3: Implement**

In `feed.py`, add the types exactly as in Interfaces (Pydantic `BaseModel` with `model_config = ConfigDict(frozen=True)` for the event/ref/grant/refusal; `ContinuityPolicy` is a `dataclasses.dataclass(frozen=True)` because it holds callables). `MarketDataFeedError`:

```python
class MarketDataFeedError(Exception):
    """Fatal feed error: connection death, a refused continuity recovery, or an invariant violation."""

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(f"{reason}: {message}" if reason else message)
        self.reason = reason
```

`IbkrMarketDataFeed._translate`:

```python
        if ibkr_bar.provenance == "ibkr_historical":
            provenance: BarProvenanceTag = "history"
        elif getattr(ibkr_bar, "spans_interruption", False):
            provenance = "realtime_across_reconnect"
        else:
            provenance = "realtime"
```

`IbkrMarketDataFeed.stream_bars(self, symbol, *, use_rth=True, continuity=None)`: accept and ignore `continuity` in this task (Task 7 uses it). `PauseAwareFeed.stream_bars`, `_RetainedSourceBarFeed.stream_bars`, the Polygon replay delegate and `_FakeFeed.stream_bars` accept `continuity: ContinuityPolicy | None = None` and forward it to their source (`_FakeFeed` stores it as `self.continuity_seen` and otherwise ignores it).

- [ ] **Step 4: Run the suites**

Run: `DATA_PLANE_CONTROL_SECRET="" /Users/inkant/learn-ai/PythonDataService/.venv/bin/python -m pytest tests/marketdata/ tests/services/bot_runner/test_registry_lifecycle.py tests/services/test_run_trade_bot_source_bars.py -q -p no:cacheprovider`; then `grep -rl "qualification_polygon_replay" tests/ | head` and run those files.
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/marketdata/feed.py app/marketdata/ibkr_feed.py app/services/bot_runtime.py app/services/bot_trade_strategy.py app/broker/alpaca/clerk/sqlite/qualification_polygon_replay.py tests/_helpers/bot_runner/doubles.py tests/marketdata/test_feed.py
git commit -m "feat(marketdata): continuity policy, provenance and continuity events on the feed port

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Ledger provenance, continuity events and the evidence journal

**Files:**
- Modify: `app/services/source_bar_ledger.py`
- Test: `tests/services/test_source_bar_ledger.py` (append)

**Interfaces:**
- Consumes: `FeedContinuityEvent`, `ContinuityEventRef` (Task 4).
- Produces:
  - `RetainedSourceBar` + `provenance: str = "realtime"`, `authorization_id: str | None = None`, `continuity_event_ref: str | None = None`, `run_id: str | None = None`, `evidence_seq: int | None = None`
  - `class RetainedContinuityEvent(BaseModel)` (frozen): `seq: int`, `run_id: str`, `evidence_seq: int`, plus every `FeedContinuityEvent` field
  - `SourceBarLedger.append(bar, *, run_id: str | None = None)`, `append_history(bar, *, run_id: str | None = None)`
  - `SourceBarLedger.append_event(event: FeedContinuityEvent, *, run_id: str) -> ContinuityEventRef`
  - `SourceBarLedger.events(*, run_id: str, evidence_end_seq: int | None = None) -> list[RetainedContinuityEvent]`
  - `SourceBarLedger.evidence_end_seq() -> int | None`
- Schema (in `_create_schema` for new files plus an idempotent `_migrate_evidence_schema_if_needed()` run in `__init__` right after `_create_schema`):

```sql
ALTER TABLE source_bars ADD COLUMN provenance TEXT NOT NULL DEFAULT 'realtime';
ALTER TABLE source_bars ADD COLUMN authorization_id TEXT;
ALTER TABLE source_bars ADD COLUMN continuity_event_ref TEXT;
CREATE TABLE IF NOT EXISTS source_stream_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('interruption','recovered','gap','substituted','refused')),
    feed_id TEXT NOT NULL, symbol TEXT NOT NULL, observed_at_ms INTEGER NOT NULL CHECK(observed_at_ms >= 0),
    cause TEXT, generation_from INTEGER, generation_to INTEGER,
    window_start_ms INTEGER, window_end_ms INTEGER, bar_identity TEXT, authorization_id TEXT,
    reason TEXT, last_delivered_end_ms INTEGER, deadline_ms INTEGER
);
CREATE TABLE IF NOT EXISTS source_evidence_journal (
    evidence_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    kind TEXT NOT NULL CHECK(kind IN ('bar','event')),
    bar_seq INTEGER REFERENCES source_bars(seq),
    event_seq INTEGER REFERENCES source_stream_events(seq),
    observed_at_ms INTEGER NOT NULL CHECK(observed_at_ms >= 0),
    CHECK((kind = 'bar' AND bar_seq IS NOT NULL AND event_seq IS NULL) OR (kind = 'event' AND event_seq IS NOT NULL AND bar_seq IS NULL))
);
CREATE INDEX IF NOT EXISTS source_evidence_journal_run ON source_evidence_journal(run_id, evidence_seq);
```

The `ALTER TABLE`s run only when `PRAGMA table_info(source_bars)` lacks the column. Back-fill: for every `source_bars` row with no journal row, insert `('bar', seq, NULL run_id, fetched_at_ms)` in `seq` order, inside one `BEGIN IMMEDIATE`.

- [ ] **Step 1: Write the failing tests** (append to `tests/services/test_source_bar_ledger.py`; add `from app.marketdata.feed import FeedContinuityEvent`)

```python
def _event(kind: str = "interruption", observed_at_ms: int = 1_700_000_000_500, **extra) -> FeedContinuityEvent:
    return FeedContinuityEvent(kind=kind, feed_id="ibkr", symbol="SPY", observed_at_ms=observed_at_ms, **extra)


def test_bars_and_events_share_one_causal_order_per_run(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="acct")
    try:
        first = ledger.append(_bar(start_ms=1_700_000_000_000), run_id="run-a")
        ref = ledger.append_event(_event(cause="socket_down", generation_from=1), run_id="run-a")
        second = ledger.append(_bar(start_ms=1_700_000_060_000), run_id="run-a")
        assert first.run_id == "run-a" and first.evidence_seq is not None
        assert ref.run_id == "run-a" and ref.ref() == f"run-a:{ref.evidence_seq}"
        assert first.evidence_seq < ref.evidence_seq < second.evidence_seq
        events = ledger.events(run_id="run-a")
        assert [e.kind for e in events] == ["interruption"]
        assert events[0].cause == "socket_down" and events[0].generation_from == 1
        assert ledger.evidence_end_seq() == second.evidence_seq
        assert ledger.events(run_id="run-a", evidence_end_seq=first.evidence_seq) == []
    finally:
        ledger.close()


def test_provenance_and_continuity_columns_persist(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="acct")
    try:
        substitute = _bar(start_ms=1_700_000_000_000).model_copy(
            update={"provenance": "historical_substitute", "authorization_id": "auth-1", "continuity_event_ref": "run-a:7"}
        )
        retained = ledger.append(substitute, run_id="run-a")
        assert retained.provenance == "historical_substitute"
        assert retained.authorization_id == "auth-1"
        assert retained.continuity_event_ref == "run-a:7"
        assert ledger.bars(provider="polygon-minute", symbol="SPY")[0].provenance == "historical_substitute"
    finally:
        ledger.close()


def test_pre_channel_ledger_migrates_with_a_journal_row_per_bar(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="acct")
    ledger.append(_bar(start_ms=1_700_000_000_000))
    ledger.append(_bar(start_ms=1_700_000_060_000))
    path = ledger.path
    ledger.close()
    conn = sqlite3.connect(path)
    conn.executescript("DROP TABLE source_evidence_journal; DROP TABLE source_stream_events;")
    conn.execute("ALTER TABLE source_bars DROP COLUMN provenance")
    conn.execute("ALTER TABLE source_bars DROP COLUMN authorization_id")
    conn.execute("ALTER TABLE source_bars DROP COLUMN continuity_event_ref")
    conn.commit()
    conn.close()

    reopened = SourceBarLedger(artifacts_root=tmp_path, account_id="acct")
    try:
        bars = reopened.bars(provider="polygon-minute", symbol="SPY")
        assert [b.provenance for b in bars] == ["realtime", "realtime"]
        assert [b.run_id for b in bars] == [None, None]
        assert bars[0].evidence_seq is not None and bars[0].evidence_seq < bars[1].evidence_seq
        assert reopened.evidence_end_seq() == bars[1].evidence_seq
    finally:
        reopened.close()


def test_event_and_journal_rows_roll_back_together(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="acct")
    try:
        bogus = _event(kind="interruption").model_copy(update={"kind": "bogus"})
        with pytest.raises(sqlite3.IntegrityError):
            ledger.append_event(bogus, run_id="run-a")
        assert ledger.events(run_id="run-a") == []
        assert ledger.evidence_end_seq() is None
    finally:
        ledger.close()
```

(`_bar` in this file uses `feed_id="polygon-minute"`; `model_copy(update=...)` on a frozen Pydantic model returns a new instance without re-validation — the bogus kind is refused by the SQL `CHECK`, which is the point.)

- [ ] **Step 2: Run to verify they fail**

Run: `DATA_PLANE_CONTROL_SECRET="" /Users/inkant/learn-ai/PythonDataService/.venv/bin/python -m pytest tests/services/test_source_bar_ledger.py -q -p no:cacheprovider -k "causal or provenance_and or pre_channel or roll_back"`
Expected: FAIL (`TypeError: append() got an unexpected keyword argument 'run_id'`, missing methods).

- [ ] **Step 3: Implement**

`RetainedSourceBar`: add the five fields with defaults; `from_market_bar` copies `provenance`, `authorization_id`, `continuity_event_ref` from the bar. `_retained_row` reads the columns (use `row.keys()` membership for the two journal fields, which come from a LEFT JOIN). `bars()` and `latest()` select:

```sql
SELECT b.*, j.run_id AS run_id, j.evidence_seq AS evidence_seq
FROM source_bars b LEFT JOIN source_evidence_journal j ON j.kind = 'bar' AND j.bar_seq = b.seq
WHERE b.provider = ? AND b.symbol = ? ORDER BY b.seq ASC
```

`_append(bar, *, delivery, run_id)`: after the `INSERT INTO source_bars` (add the three new columns to the column list), insert the journal row in the same transaction:

```python
                self._conn.execute(
                    "INSERT INTO source_evidence_journal (run_id, kind, bar_seq, observed_at_ms) "
                    "VALUES (?, 'bar', ?, ?)",
                    (run_id, seq, candidate.fetched_at_ms),
                )
                evidence_seq = int(self._conn.execute("SELECT last_insert_rowid()").fetchone()[0])
```

and return `candidate.model_copy(update={"seq": seq, "run_id": run_id, "evidence_seq": evidence_seq})`. The idempotent early-return branch (same identity, same payload) returns the existing row via the joined query.

`append_event`:

```python
    def append_event(self, event: FeedContinuityEvent, *, run_id: str) -> ContinuityEventRef:
        """Persist one continuity fact and its journal position in one transaction."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._conn.execute(
                    """
                    INSERT INTO source_stream_events (
                        run_id, kind, feed_id, symbol, observed_at_ms, cause, generation_from, generation_to,
                        window_start_ms, window_end_ms, bar_identity, authorization_id, reason,
                        last_delivered_end_ms, deadline_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id, event.kind, event.feed_id, event.symbol, event.observed_at_ms, event.cause,
                        event.generation_from, event.generation_to, event.window_start_ms, event.window_end_ms,
                        event.bar_identity, event.authorization_id, event.reason, event.last_delivered_end_ms,
                        event.deadline_ms,
                    ),
                )
                event_seq = int(cursor.lastrowid)
                journal = self._conn.execute(
                    "INSERT INTO source_evidence_journal (run_id, kind, event_seq, observed_at_ms) "
                    "VALUES (?, 'event', ?, ?)",
                    (run_id, event_seq, event.observed_at_ms),
                )
                evidence_seq = int(journal.lastrowid)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return ContinuityEventRef(run_id=run_id, evidence_seq=evidence_seq)
```

`events(run_id, evidence_end_seq)`: join events to the journal, filter `run_id` and `evidence_seq <= bound` when given, order by `evidence_seq`. `evidence_end_seq()`: `SELECT MAX(evidence_seq) FROM source_evidence_journal`. `_migrate_evidence_schema_if_needed()`: `PRAGMA table_info(source_bars)` → `ALTER TABLE` for each missing column; `CREATE TABLE IF NOT EXISTS` for the two tables; then back-fill journal rows for bars lacking one (`SELECT b.seq, b.fetched_at_ms FROM source_bars b LEFT JOIN source_evidence_journal j ON j.kind = 'bar' AND j.bar_seq = b.seq WHERE j.evidence_seq IS NULL ORDER BY b.seq`). The legacy JSONL importer inserts journal rows for what it imports too.

- [ ] **Step 4: Run the ledger and its consumers**

Run: `DATA_PLANE_CONTROL_SECRET="" /Users/inkant/learn-ai/PythonDataService/.venv/bin/python -m pytest tests/services/test_source_bar_ledger.py tests/services/test_run_trade_bot_source_bars.py tests/services/test_bot_binding_authority_source_bars.py tests/services/test_run_replay_proof_assembly.py tests/services/test_run_replay_receipt_store.py tests/broker/alpaca/clerk/ -q -p no:cacheprovider -x`
Expected: all pass (`RetainedSourceBar` constructors elsewhere keep working through the defaults).

- [ ] **Step 5: Commit**

```bash
git add app/services/source_bar_ledger.py tests/services/test_source_bar_ledger.py
git commit -m "feat(ledger): provenance columns, continuity events and a run-scoped evidence journal

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Decision clock — one ET floor, trigger instants, seal timeframe reader

**Files:**
- Create: `app/services/decision_clock.py`
- Modify: `app/utils/timestamps.py` (+ `floor_to_period_ms_et`), `app/engine/consolidators/trade_bar_consolidator.py` (`_floor_to_period_ms` delegates), `app/services/run_replay_proof.py` (import `decision_timeframe_ms_for_binding`, delete `_seal_decision_timeframe_ms`)
- Test: `tests/services/test_decision_clock.py` (new); the existing consolidator tests (`grep -rl "TradeBarConsolidator" tests/`) stay green

**Interfaces (produces):**

```python
# app/utils/timestamps.py
def floor_to_period_ms_et(timestamp_ms: int, period_ms: int) -> int  # ET-wall-clock floor, returns int64 ms UTC

# app/services/decision_clock.py
SOURCE_BAR_MS = 60_000
def decision_timeframe_ms_for_binding(binding: BrokerBotBinding) -> int | None   # seal.configured_signal.data.decision_timeframe_ms, None without a seal
def rth_trigger_instants(session_date: date, *, timeframe_ms: int) -> list[int]  # bucket_end + 60_000 per bucket, except the last bucket -> session_close
def next_trigger_ms(last_delivered_end_ms: int, *, timeframe_ms: int, decision_session: DecisionSession) -> int  # smallest trigger > L; rolls to the next session; only "rth" supported (R1)
def rth_next_trigger_function(timeframe_ms: int) -> Callable[[int], int]
```

- [ ] **Step 1: Write the failing tests**

```python
"""Decision clock: trigger instants on the calendar (spec §4.4, §4.5)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.engine.consolidators.trade_bar_consolidator import _floor_to_period_ms
from app.lean_sidecar.trading_calendar import session_close_ms_utc, session_open_ms_utc
from app.services.decision_clock import next_trigger_ms, rth_trigger_instants
from app.utils.timestamps import floor_to_period_ms_et, to_ms_utc

_ET = ZoneInfo("America/New_York")
_TF = 15 * 60_000
_REGULAR = date(2026, 9, 2)      # Wednesday, regular session
_EARLY = date(2026, 11, 27)      # day after Thanksgiving: 13:00 ET close
_FRIDAY = date(2026, 9, 4)       # next session is Tue 2026-09-08 (Labor Day 09-07)


def _et(d: date, hour: int, minute: int) -> int:
    return to_ms_utc(datetime(d.year, d.month, d.day, hour, minute, tzinfo=_ET))


def test_shared_floor_matches_the_consolidators_floor_across_dst() -> None:
    for d in (date(2026, 3, 8), date(2026, 11, 1), _REGULAR):
        ts = _et(d, 13, 7)
        assert floor_to_period_ms_et(ts, _TF) == _floor_to_period_ms(ts, timedelta(minutes=15)) == _et(d, 13, 0)


def test_rth_trigger_instants_regular_session() -> None:
    triggers = rth_trigger_instants(_REGULAR, timeframe_ms=_TF)
    assert triggers[0] == _et(_REGULAR, 9, 46)          # 09:30–09:45 bucket fires on the 09:45 minute's close
    assert triggers[-1] == session_close_ms_utc(_REGULAR)  # last bucket: forced flush at the close
    assert len(triggers) == 26


def test_rth_trigger_instants_early_close() -> None:
    triggers = rth_trigger_instants(_EARLY, timeframe_ms=_TF)
    assert triggers[-1] == session_close_ms_utc(_EARLY) == _et(_EARLY, 13, 0)
    assert len(triggers) == 14


def test_next_trigger_after_last_delivered_minute() -> None:
    L = _et(_REGULAR, 15, 0)   # last delivered minute 14:59–15:00 -> the 14:45–15:00 decision is still pending
    assert next_trigger_ms(L, timeframe_ms=_TF, decision_session="rth") == _et(_REGULAR, 15, 1)
    L = _et(_REGULAR, 15, 1)   # the 15:00 minute delivered -> next pending is the 15:00–15:15 decision
    assert next_trigger_ms(L, timeframe_ms=_TF, decision_session="rth") == _et(_REGULAR, 15, 16)
    L = _et(_REGULAR, 15, 59)
    assert next_trigger_ms(L, timeframe_ms=_TF, decision_session="rth") == session_close_ms_utc(_REGULAR)


def test_next_trigger_rolls_to_the_next_session() -> None:
    after_close = session_close_ms_utc(_FRIDAY)
    expected = session_open_ms_utc(date(2026, 9, 8)) + _TF + 60_000
    assert next_trigger_ms(after_close, timeframe_ms=_TF, decision_session="rth") == expected
    pre_market = _et(_REGULAR, 5, 10)
    assert next_trigger_ms(pre_market, timeframe_ms=_TF, decision_session="rth") == _et(_REGULAR, 9, 46)


def test_one_minute_timeframe() -> None:
    L = _et(_REGULAR, 15, 0)
    assert next_trigger_ms(L, timeframe_ms=60_000, decision_session="rth") == _et(_REGULAR, 15, 1)


def test_all_session_is_refused_in_this_slice() -> None:
    with pytest.raises(NotImplementedError):
        next_trigger_ms(_et(_REGULAR, 5, 10), timeframe_ms=_TF, decision_session="all")
```

- [ ] **Step 2: Run to verify they fail**

Run: `DATA_PLANE_CONTROL_SECRET="" /Users/inkant/learn-ai/PythonDataService/.venv/bin/python -m pytest tests/services/test_decision_clock.py -q -p no:cacheprovider`
Expected: FAIL — `ImportError` for `floor_to_period_ms_et` / `decision_clock`.

- [ ] **Step 3: Implement**

`app/utils/timestamps.py` (add `timedelta`, `ZoneInfo` imports if missing):

```python
_ET = ZoneInfo("America/New_York")
_EPOCH_NAIVE = datetime(1970, 1, 1)


def floor_to_period_ms_et(timestamp_ms: int, period_ms: int) -> int:
    """Floor ``timestamp_ms`` to ``period_ms`` on the America/New_York wall clock.

    The single decision-bucket floor for the repo (consolidator and decision
    clock). Reading the ET wall clock, flooring it as an epoch offset, and
    converting back is what LEAN's floor of an exchange-local naive DateTime
    amounts to; for periods under a day it equals flooring raw UTC ms, for a
    day or longer it keeps the ET trading date.
    """
    if period_ms <= 0:
        raise ValueError("period_ms must be positive")
    naive_et = ny_datetime(timestamp_ms).replace(tzinfo=None)
    naive_et_ms = int((naive_et - _EPOCH_NAIVE).total_seconds() * 1000)
    floored = _EPOCH_NAIVE + timedelta(milliseconds=(naive_et_ms // period_ms) * period_ms)
    return to_ms_utc(floored.replace(tzinfo=_ET))
```

Consolidator: `_floor_to_period_ms(timestamp_ms, period)` becomes `return floor_to_period_ms_et(timestamp_ms, int(period.total_seconds() * 1000))` (keep a one-line docstring pointing at the shared function; delete the local `_EASTERN`/`_EPOCH_NAIVE` if unused).

`app/services/decision_clock.py`:

```python
"""Decision clock for continuity: when the next decision is due (spec #1921 §4.4).

A decision for bucket K fires when the consolidator receives the first source
minute of K+1, which closes 60 s after K's end -- except a session's last
bucket, which the runner force-flushes on the bar closing at the session
close. Only the regular session is supported: the canonical calendar proves
RTH; extended windows are broker-proven capabilities (session_authority), so
``decision_session="all"`` is refused here (controller ruling R1).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

from app.lean_sidecar.trading_calendar import (
    is_trading_day,
    next_trading_day,
    session_close_ms_utc,
    session_open_ms_utc,
)
from app.marketdata.feed import DecisionSession
from app.services.bot_binding_repository import BrokerBotBinding
from app.utils.timestamps import floor_to_period_ms_et, ny_datetime

SOURCE_BAR_MS = 60_000


def decision_timeframe_ms_for_binding(binding: BrokerBotBinding) -> int | None:
    """The seal-attested decision clock width; None for an unsealed binding."""
    seal = binding.sealed_program
    if seal is None:
        return None
    return int(seal.configured_signal.data.decision_timeframe_ms)


def rth_trigger_instants(session_date: date, *, timeframe_ms: int) -> list[int]:
    open_ms = session_open_ms_utc(session_date)
    close_ms = session_close_ms_utc(session_date)
    triggers: list[int] = []
    bucket_start = floor_to_period_ms_et(open_ms, timeframe_ms)
    while bucket_start < close_ms:
        bucket_end = bucket_start + timeframe_ms
        triggers.append(close_ms if bucket_end >= close_ms else bucket_end + SOURCE_BAR_MS)
        bucket_start = bucket_end
    return triggers


def next_trigger_ms(last_delivered_end_ms: int, *, timeframe_ms: int, decision_session: DecisionSession) -> int:
    if decision_session != "rth":
        raise NotImplementedError("decision_session='all' has no calendar-proven trigger set yet (ruling R1)")
    session_date = ny_datetime(last_delivered_end_ms).date()
    if not is_trading_day(session_date):
        session_date = next_trading_day(session_date)
    while True:
        for trigger in rth_trigger_instants(session_date, timeframe_ms=timeframe_ms):
            if trigger > last_delivered_end_ms:
                return trigger
        session_date = next_trading_day(session_date)


def rth_next_trigger_function(timeframe_ms: int) -> Callable[[int], int]:
    return lambda last_end: next_trigger_ms(last_end, timeframe_ms=timeframe_ms, decision_session="rth")
```

`run_replay_proof.py`: replace `_seal_decision_timeframe_ms(binding)` with `decision_timeframe_ms_for_binding(binding)` imported from `app.services.decision_clock`; delete the local function.

- [ ] **Step 4: Run**

Run: `DATA_PLANE_CONTROL_SECRET="" /Users/inkant/learn-ai/PythonDataService/.venv/bin/python -m pytest tests/services/test_decision_clock.py tests/services/test_run_replay_proof_assembly.py tests/services/test_run_replay_fidelity.py $(grep -rl "TradeBarConsolidator" tests/ | tr '\n' ' ') -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/utils/timestamps.py app/engine/consolidators/trade_bar_consolidator.py app/services/decision_clock.py app/services/run_replay_proof.py tests/services/test_decision_clock.py
git commit -m "feat(services): decision clock with one ET floor and calendar-backed trigger instants

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: The feed's fail-closed continuity loop

**Files:**
- Create: `app/marketdata/ibkr_continuity.py`
- Modify: `app/marketdata/ibkr_feed.py` (`stream_bars`), `app/broker/ibkr/config.py` (`feed_continuity_enabled`)
- Test: `tests/marketdata/test_feed_continuity.py` (new)

**Interfaces:**
- Consumes: `IBKRBarInterrupted`, `IBKRBarSubscriptionStalled`, `stream_minute_bars(..., assembler=)` (Tasks 2–3); `MinuteAssembler`, `RTH_CONTRIBUTIONS_PER_MINUTE` (Task 3); port types (Task 4); `get_monitor()` from `app.broker.ibkr.auto_reconnect_monitor`.
- Produces: `IbkrMarketDataFeed.stream_bars(symbol, *, use_rth=True, continuity=None)` honoring the policy; `IbkrSettings.feed_continuity_enabled: bool = True` (env `IBKR_FEED_CONTINUITY_ENABLED`).

Behavior (spec §4.2 rules 3, 6, 7, 8, 9; rulings R2, R3):

1. `continuity is None` or `not self._client.settings.feed_continuity_enabled` → the existing generator body, unchanged (stall replacement loop; interruption → `MarketDataFeedError(str(exc))`). Log once per stream when the switch disables an offered policy: `action="marketdata_continuity_disabled"`.
2. With a policy: one `MinuteAssembler` per consumer stream; `L: int | None = None` (last delivered `end_ms`); `generation = client.connection_generation` at entry.
   - Run `stream_minute_bars(..., assembler=assembler)`; for each emitted `IbkrMinuteBar`: first, if `L` is set and `bar.start_ms > L`, every minute window in `[L, bar.start_ms)` is a **wholly missed** unresolvable window, resolved in order before the bar itself. Then the bar: if `spans_interruption` and (`session_phase != "RTH"` or `contribution_count` is `None` or `< 12`) → **unresolvable window** `[bar.start_ms, bar.end_ms)`; else translate, deliver (yield) and set `L = bar.end_ms`.
   - Unresolvable window: `inside = policy.decision_session == "rth" and session_state_at_ms(now_ms=window_start).phase == "RTH"` (`app.services.session_authority.session_state_at_ms`, as `bars.py` uses). Outside → `await record(gap event)`, set `L = window_end`, omit. Inside → `verdict = policy.substitution_grant(start, end)`; a `SubstitutionRefusal` → `await record(refused event, reason=verdict.reason)` → `raise MarketDataFeedError(..., reason=verdict.reason)`; a `SubstitutionGrant` (R3) → `await record(refused, reason="SUBSTITUTION_PATH_UNAVAILABLE")` → raise with that reason. Log `action="marketdata_continuity_refused"`.
   - `except (IBKRBarInterrupted, IBKRBarSubscriptionStalled) as exc`: `cause = "stall"` for the stall, else `exc.cause`. If `L is None` and `assembler.open_minute_start_ms is None` → `raise MarketDataFeedError(str(exc))` (rule 6: nothing to continue from). If `L is None` → `L = assembler.open_minute_start_ms`. `await record(interruption: cause, generation_from, last_delivered_end_ms=L, deadline_ms=policy.deadline_ms(L))`; `complete = assembler.flush_if_complete()` → if not None, run it through the same per-bar resolution and yield; then `await wait_for_healthy(...)`; on success `await record(recovered: generation_from, generation_to, last_delivered_end_ms=L)`, remember its ref, update `generation`, re-enter `stream_minute_bars` with the same assembler. `NotConnectedError` on re-entry → back into `wait_for_healthy` (still interrupted).
   - `except IBKRBarStreamError` (anything else) → `MarketDataFeedError(str(exc))` as today.
   - Any exception raised by `policy.record_event` → `MarketDataFeedError(str(exc), reason="CONTINUITY_EVIDENCE_UNWRITABLE")`; the pending bar is not yielded.
3. `wait_for_healthy`: poll every `0.25` s; `if now_ms_utc() >= policy.deadline_ms(L)`: `await record(refused: reason="DECISION_BAR_MISSED", last_delivered_end_ms=L, deadline_ms=…)` then `raise MarketDataFeedError(..., reason="DECISION_BAR_MISSED")`. Healthy iff `client.is_connected() and not client.connection_lost and (monitor is None or monitor.recovery_state == "HEALTHY")` with `monitor = get_monitor()`.
4. Delivered bars with provenance `realtime_across_reconnect` carry `continuity_event_ref = <recovered ref>.ref()`.

- [ ] **Step 1: Write the failing tests**

```python
"""Fail-closed continuity inside IbkrMarketDataFeed.stream_bars (spec §4.2, §4.5, §8)."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.broker.ibkr.bars import IBKRBarInterrupted, IBKRBarSubscriptionStalled
from app.marketdata import ibkr_feed as feed_module
from app.marketdata.feed import (
    ContinuityEventRef,
    ContinuityPolicy,
    FeedContinuityEvent,
    MarketDataFeedError,
    SubstitutionGrant,
    SubstitutionRefusal,
)
from app.marketdata.ibkr_feed import IbkrMarketDataFeed

_MINUTE0 = 1_788_375_600_000  # 2026-09-02 15:00:00 ET
_TF = 900_000


def _ibkr_bar(start_ms: int, *, contribution_count: int = 12, spans_interruption: bool = False, phase: str = "RTH"):
    return SimpleNamespace(
        symbol="SPY", start_ms=start_ms, end_ms=start_ms + 60_000,
        open=Decimal("1"), high=Decimal("1"), low=Decimal("1"), close=Decimal("1"), volume=12,
        fetched_at_ms=start_ms + 60_000, source="ibkr", provenance="ibkr_realtime", venue="ARCA",
        session_phase=phase, use_rth=True, contribution_count=contribution_count,
        spans_interruption=spans_interruption,
    )


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[FeedContinuityEvent] = []
        self.fail = False

    async def __call__(self, event: FeedContinuityEvent) -> ContinuityEventRef:
        if self.fail:
            raise OSError("journal unwritable")
        self.events.append(event)
        return ContinuityEventRef(run_id="run-1", evidence_seq=len(self.events))


def _next_trigger(last_end: int) -> int:
    # Fake decision clock: triggers at k * 15 min + 60 s; smallest one strictly after last_end.
    candidate = (last_end // _TF) * _TF + 60_000
    return candidate if candidate > last_end else candidate + _TF


def _policy(sink: _RecordingSink, *, grant=None) -> ContinuityPolicy:
    return ContinuityPolicy(
        decision_session="rth",
        next_trigger_ms=_next_trigger,
        substitution_grant=grant or (lambda s, e: SubstitutionRefusal(reason="SUBSTITUTION_NOT_AUTHORIZED")),
        record_event=sink,
    )


def _client(*, generation: int = 1) -> MagicMock:
    client = MagicMock()
    client.is_connected.return_value = True
    client.connection_lost = False
    client.connection_generation = generation
    client.settings = SimpleNamespace(feed_continuity_enabled=True)
    return client


class _Source:
    """Scripted stream_minute_bars: each call yields its scripted items; an exception item is raised."""

    def __init__(self, *calls: list) -> None:
        self.calls = list(calls)
        self.assemblers: list = []
        self.invocations = 0

    def __call__(self, _client, _symbol, *, use_rth=True, on_source_bar=None, assembler=None, **_kw):
        self.assemblers.append(assembler)
        script = self.calls[self.invocations] if self.invocations < len(self.calls) else []
        self.invocations += 1

        async def _gen():
            for item in script:
                if isinstance(item, BaseException):
                    raise item
                if on_source_bar is not None:
                    on_source_bar(item.start_ms)
                yield item

        return _gen()


@pytest.fixture(autouse=True)
def _no_monitor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.marketdata.ibkr_continuity.get_monitor", lambda: None)
    monkeypatch.setattr("app.marketdata.ibkr_continuity.now_ms_utc", lambda: _MINUTE0 + 45_000)
    monkeypatch.setattr("app.marketdata.ibkr_continuity.session_state_at_ms",
                        lambda now_ms: SimpleNamespace(phase="RTH" if now_ms >= _MINUTE0 - 3_600_000 else "PRE"))


async def _collect(feed: IbkrMarketDataFeed, policy: ContinuityPolicy, n: int) -> list:
    out = []
    async for bar in feed.stream_bars("SPY", continuity=policy):
        out.append(bar)
        if len(out) == n:
            break
    return out


async def test_count_complete_interruption_resumes_on_the_same_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    sink = _RecordingSink()
    source = _Source(
        [_ibkr_bar(_MINUTE0 - 60_000), IBKRBarInterrupted("socket", cause="socket_down")],
        [_ibkr_bar(_MINUTE0, contribution_count=12, spans_interruption=True), _ibkr_bar(_MINUTE0 + 60_000)],
    )
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    feed = IbkrMarketDataFeed(_client())

    bars = await _collect(feed, _policy(sink), 3)

    assert [b.provenance for b in bars] == ["realtime", "realtime_across_reconnect", "realtime"]
    assert [e.kind for e in sink.events] == ["interruption", "recovered"]
    assert sink.events[0].cause == "socket_down" and sink.events[0].last_delivered_end_ms == _MINUTE0
    assert bars[1].continuity_event_ref == "run-1:2"
    assert source.assemblers[0] is source.assemblers[1]  # the same assembler survived


async def test_interruption_before_any_delivered_bar_is_fatal_as_today(monkeypatch: pytest.MonkeyPatch) -> None:
    sink = _RecordingSink()
    source = _Source([IBKRBarInterrupted("x", cause="socket_down")], [_ibkr_bar(_MINUTE0)])
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    feed = IbkrMarketDataFeed(_client())
    with pytest.raises(MarketDataFeedError) as excinfo:
        await _collect(feed, _policy(sink), 1)
    assert excinfo.value.reason is None
    assert sink.events == []


async def test_incomplete_minute_inside_rth_is_refused_with_the_grant_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    sink = _RecordingSink()
    source = _Source(
        [_ibkr_bar(_MINUTE0 - 60_000), IBKRBarInterrupted("x", cause="socket_down")],
        [_ibkr_bar(_MINUTE0, contribution_count=11, spans_interruption=True)],
    )
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    feed = IbkrMarketDataFeed(_client())
    with pytest.raises(MarketDataFeedError) as excinfo:
        await _collect(feed, _policy(sink), 2)
    assert excinfo.value.reason == "SUBSTITUTION_NOT_AUTHORIZED"
    assert [e.kind for e in sink.events] == ["interruption", "recovered", "refused"]
    assert sink.events[-1].window_start_ms == _MINUTE0 and sink.events[-1].reason == "SUBSTITUTION_NOT_AUTHORIZED"


async def test_a_grant_is_never_honored_in_this_slice(monkeypatch: pytest.MonkeyPatch) -> None:
    sink = _RecordingSink()
    source = _Source(
        [_ibkr_bar(_MINUTE0 - 60_000), IBKRBarInterrupted("x", cause="socket_down")],
        [_ibkr_bar(_MINUTE0, contribution_count=11, spans_interruption=True)],
    )
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    feed = IbkrMarketDataFeed(_client())

    def _grant(s: int, e: int) -> SubstitutionGrant:
        return SubstitutionGrant(authorization_id="a", window_start_ms=s, window_end_ms=e)

    with pytest.raises(MarketDataFeedError) as excinfo:
        await _collect(feed, _policy(sink, grant=_grant), 2)
    assert excinfo.value.reason == "SUBSTITUTION_PATH_UNAVAILABLE"


async def test_unresolvable_minute_outside_rth_is_a_gap_and_the_run_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    sink = _RecordingSink()
    pre = _MINUTE0 - 8 * 3_600_000  # 07:00 ET
    source = _Source(
        [_ibkr_bar(pre - 60_000, phase="PRE"), IBKRBarInterrupted("x", cause="socket_down")],
        [_ibkr_bar(pre, contribution_count=3, spans_interruption=True, phase="PRE"), _ibkr_bar(pre + 60_000, phase="PRE")],
    )
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    monkeypatch.setattr("app.marketdata.ibkr_continuity.now_ms_utc", lambda: pre + 30_000)
    feed = IbkrMarketDataFeed(_client())
    bars = await _collect(feed, _policy(sink), 2)
    assert [b.start_ms for b in bars] == [pre - 60_000, pre + 60_000]
    assert [e.kind for e in sink.events] == ["interruption", "recovered", "gap"]
    assert sink.events[-1].window_start_ms == pre


async def test_wholly_missed_minutes_are_resolved_before_the_next_bar(monkeypatch: pytest.MonkeyPatch) -> None:
    sink = _RecordingSink()
    source = _Source(
        [_ibkr_bar(_MINUTE0 - 60_000), IBKRBarInterrupted("x", cause="socket_down")],
        [_ibkr_bar(_MINUTE0 + 120_000)],  # 15:00 and 15:01 never assembled at all
    )
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    feed = IbkrMarketDataFeed(_client())
    with pytest.raises(MarketDataFeedError) as excinfo:
        await _collect(feed, _policy(sink), 2)
    assert excinfo.value.reason == "SUBSTITUTION_NOT_AUTHORIZED"
    assert sink.events[-1].kind == "refused" and sink.events[-1].window_start_ms == _MINUTE0


async def test_deadline_passing_during_the_wait_is_decision_bar_missed(monkeypatch: pytest.MonkeyPatch) -> None:
    sink = _RecordingSink()
    source = _Source([_ibkr_bar(_MINUTE0 - 60_000), IBKRBarInterrupted("x", cause="socket_down")], [])
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    client = _client()
    client.is_connected.return_value = False
    monkeypatch.setattr("app.marketdata.ibkr_continuity.now_ms_utc", lambda: _MINUTE0 + 60_000 + 20_001)
    feed = IbkrMarketDataFeed(client)
    with pytest.raises(MarketDataFeedError) as excinfo:
        await _collect(feed, _policy(sink), 2)
    assert excinfo.value.reason == "DECISION_BAR_MISSED"
    assert sink.events[-1].kind == "refused" and sink.events[-1].reason == "DECISION_BAR_MISSED"


def _restore_after(client: MagicMock, attribute: str):
    async def _sleep(_seconds: float) -> None:
        setattr(client, attribute, False)

    return _sleep


async def test_soft_loss_1100_waits_for_restore_then_resumes(monkeypatch: pytest.MonkeyPatch) -> None:
    sink = _RecordingSink()
    source = _Source(
        [_ibkr_bar(_MINUTE0 - 60_000), IBKRBarInterrupted("1100", cause="soft_loss_1100")],
        [_ibkr_bar(_MINUTE0, contribution_count=12, spans_interruption=True)],
    )
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    client = _client()
    client.connection_lost = True
    monkeypatch.setattr("app.marketdata.ibkr_continuity.asyncio.sleep", _restore_after(client, "connection_lost"))
    feed = IbkrMarketDataFeed(client)
    bars = await _collect(feed, _policy(sink), 2)
    assert bars[1].provenance == "realtime_across_reconnect"
    assert sink.events[0].cause == "soft_loss_1100"


async def test_stall_enters_the_same_choreography(monkeypatch: pytest.MonkeyPatch) -> None:
    sink = _RecordingSink()
    source = _Source(
        [_ibkr_bar(_MINUTE0 - 60_000), IBKRBarSubscriptionStalled("stalled")],
        [_ibkr_bar(_MINUTE0, contribution_count=12, spans_interruption=True)],
    )
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    feed = IbkrMarketDataFeed(_client())
    bars = await _collect(feed, _policy(sink), 2)
    assert bars[1].provenance == "realtime_across_reconnect"
    assert sink.events[0].cause == "stall"


async def test_sink_failure_is_fatal_and_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    sink = _RecordingSink()
    sink.fail = True
    source = _Source([_ibkr_bar(_MINUTE0 - 60_000), IBKRBarInterrupted("x", cause="socket_down")], [])
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    feed = IbkrMarketDataFeed(_client())
    with pytest.raises(MarketDataFeedError) as excinfo:
        await _collect(feed, _policy(sink), 2)
    assert excinfo.value.reason == "CONTINUITY_EVIDENCE_UNWRITABLE"


async def test_kill_switch_restores_todays_fail_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    sink = _RecordingSink()
    source = _Source([_ibkr_bar(_MINUTE0 - 60_000), IBKRBarInterrupted("x", cause="socket_down")])
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    client = _client()
    client.settings = SimpleNamespace(feed_continuity_enabled=False)
    feed = IbkrMarketDataFeed(client)
    with pytest.raises(MarketDataFeedError) as excinfo:
        await _collect(feed, _policy(sink), 2)
    assert excinfo.value.reason is None
    assert sink.events == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `DATA_PLANE_CONTROL_SECRET="" /Users/inkant/learn-ai/PythonDataService/.venv/bin/python -m pytest tests/marketdata/test_feed_continuity.py -q -p no:cacheprovider`
Expected: FAIL (`ModuleNotFoundError: app.marketdata.ibkr_continuity`, then behavioral failures).

- [ ] **Step 3: Implement**

`app/broker/ibkr/config.py`, after `broker_enabled`:

```python
    # Kill switch for same-run feed continuity (#1921). ``False`` makes the
    # shared MarketDataFeed ignore any ContinuityPolicy a consumer presents and
    # fail fast on every interruption exactly as before the feature existed.
    feed_continuity_enabled: bool = True
```

`app/marketdata/ibkr_continuity.py` — helpers only (no generator):

```python
"""Continuity helpers for IbkrMarketDataFeed.stream_bars (spec #1921 §4.2 rules 3, 7, 9).

The feed keeps the generator; this module owns the wait under the consumer's
deadline, the resolution of minutes an interruption touched, and the awaited
event emission. No historical substitution exists here: a granted window is
refused with ``SUBSTITUTION_PATH_UNAVAILABLE`` (controller ruling R3).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from app.broker.ibkr.auto_reconnect_monitor import get_monitor
from app.broker.ibkr.client import IbkrClient
from app.broker.ibkr.minute_assembler import RTH_CONTRIBUTIONS_PER_MINUTE
from app.marketdata.feed import (
    ContinuityEventRef,
    ContinuityPolicy,
    FeedContinuityEvent,
    MarketDataFeedError,
    SubstitutionGrant,
    SubstitutionRefusal,
)
from app.services.session_authority import session_state_at_ms
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

WAIT_POLL_S = 0.25
SOURCE_BAR_MS = 60_000


@dataclass
class ContinuityState:
    """Per-consumer continuity bookkeeping owned by one stream_bars call."""

    feed_id: str
    symbol: str
    policy: ContinuityPolicy
    last_delivered_end_ms: int | None = None
    generation: int = 0
    last_recovered_ref: ContinuityEventRef | None = None

    async def record(self, event: FeedContinuityEvent) -> ContinuityEventRef:
        try:
            return await self.policy.record_event(event)
        except Exception as exc:
            raise MarketDataFeedError(
                f"continuity evidence for {self.symbol} could not be written: {exc}",
                reason="CONTINUITY_EVIDENCE_UNWRITABLE",
            ) from exc

    def event(self, kind: str, **fields: object) -> FeedContinuityEvent:
        return FeedContinuityEvent(
            kind=kind, feed_id=self.feed_id, symbol=self.symbol, observed_at_ms=now_ms_utc(), **fields
        )


def _healthy(client: IbkrClient) -> bool:
    if not client.is_connected() or client.connection_lost:
        return False
    monitor = get_monitor()
    return monitor is None or monitor.recovery_state == "HEALTHY"


async def wait_for_healthy(client: IbkrClient, state: ContinuityState) -> None:
    """Block until the socket is healthy again or the consumer's deadline passes."""
    assert state.last_delivered_end_ms is not None
    deadline_ms = state.policy.deadline_ms(state.last_delivered_end_ms)
    while not _healthy(client):
        if now_ms_utc() >= deadline_ms:
            await state.record(
                state.event(
                    "refused",
                    reason="DECISION_BAR_MISSED",
                    last_delivered_end_ms=state.last_delivered_end_ms,
                    deadline_ms=deadline_ms,
                )
            )
            logger.error(
                "Feed continuity refused: decision bar missed",
                extra={
                    "action": "marketdata_continuity_refused",
                    "symbol": state.symbol,
                    "reason": "DECISION_BAR_MISSED",
                    "deadline_ms": deadline_ms,
                },
            )
            raise MarketDataFeedError(
                f"{state.symbol} was not recovered before {deadline_ms}", reason="DECISION_BAR_MISSED"
            )
        await asyncio.sleep(WAIT_POLL_S)


def inside_decision_session(state: ContinuityState, window_start_ms: int) -> bool:
    return state.policy.decision_session == "rth" and session_state_at_ms(now_ms=window_start_ms).phase == "RTH"


async def resolve_unresolvable_window(state: ContinuityState, window_start_ms: int, window_end_ms: int) -> None:
    """Rule 3 for one minute nothing real-time can prove complete: gap outside the session, refusal inside."""
    if not inside_decision_session(state, window_start_ms):
        await state.record(
            state.event(
                "gap",
                window_start_ms=window_start_ms,
                window_end_ms=window_end_ms,
                last_delivered_end_ms=state.last_delivered_end_ms,
            )
        )
        logger.warning(
            "Feed continuity omitted an unresolvable minute outside the decision session",
            extra={"action": "marketdata_gap_omitted", "symbol": state.symbol, "window_start_ms": window_start_ms},
        )
        state.last_delivered_end_ms = window_end_ms
        return
    verdict = state.policy.substitution_grant(window_start_ms, window_end_ms)
    reason = verdict.reason if isinstance(verdict, SubstitutionRefusal) else "SUBSTITUTION_PATH_UNAVAILABLE"
    if isinstance(verdict, SubstitutionGrant):
        logger.error(
            "A substitution grant was offered but no substitution path exists (fail closed)",
            extra={"action": "marketdata_continuity_refused", "symbol": state.symbol, "reason": reason},
        )
    await state.record(
        state.event(
            "refused",
            reason=reason,
            window_start_ms=window_start_ms,
            window_end_ms=window_end_ms,
            last_delivered_end_ms=state.last_delivered_end_ms,
        )
    )
    logger.error(
        "Feed continuity refused: minute cannot be proven complete",
        extra={"action": "marketdata_continuity_refused", "symbol": state.symbol, "reason": reason,
               "window_start_ms": window_start_ms},
    )
    raise MarketDataFeedError(
        f"minute {window_start_ms}..{window_end_ms} for {state.symbol} cannot be proven complete", reason=reason
    )


def is_unresolvable(bar) -> bool:
    """An emitted minute is unresolvable iff it spans an interruption and cannot be proven complete by count."""
    if not getattr(bar, "spans_interruption", False):
        return False
    if bar.session_phase != "RTH":
        return True
    count = getattr(bar, "contribution_count", None)
    return count is None or count < RTH_CONTRIBUTIONS_PER_MINUTE
```

`ibkr_feed.py` `stream_bars`: keep the existing body as `_stream_bars_legacy(...)` (private async generator, unchanged) and add `_stream_bars_with_continuity(...)`; the public `stream_bars` picks one:

```python
        if continuity is None or not getattr(self._client.settings, "feed_continuity_enabled", True):
            if continuity is not None:
                logger.warning(
                    "Feed continuity disabled by IBKR_FEED_CONTINUITY_ENABLED; failing fast on interruptions",
                    extra={"action": "marketdata_continuity_disabled", "symbol": normalized_symbol},
                )
            async for bar in self._stream_bars_legacy(normalized_symbol, use_rth=use_rth):
                yield bar
            return
        async for bar in self._stream_bars_with_continuity(normalized_symbol, use_rth=use_rth, policy=continuity):
            yield bar
```

`_stream_bars_with_continuity` (attach/detach bookkeeping identical to the legacy path):

```python
        state = ContinuityState(
            feed_id=self.feed_id, symbol=symbol, policy=policy,
            generation=int(getattr(self._client, "connection_generation", 0)),
        )
        assembler = MinuteAssembler()
        try:
            while True:
                try:
                    async for ibkr_bar in stream_minute_bars(
                        self._client, symbol, use_rth=use_rth,
                        on_source_bar=lambda ms: self._observe_source_bar(symbol, ms), assembler=assembler,
                    ):
                        async for bar in self._resolve_emitted(state, liveness, ibkr_bar):
                            yield bar
                    break
                except (IBKRBarInterrupted, IBKRBarSubscriptionStalled) as exc:
                    cause = "stall" if isinstance(exc, IBKRBarSubscriptionStalled) else exc.cause
                    if state.last_delivered_end_ms is None:
                        if assembler.open_minute_start_ms is None:
                            raise MarketDataFeedError(str(exc)) from exc  # rule 6: nothing to continue from
                        state.last_delivered_end_ms = assembler.open_minute_start_ms
                    await state.record(state.event(
                        "interruption", cause=cause, generation_from=state.generation,
                        last_delivered_end_ms=state.last_delivered_end_ms,
                        deadline_ms=policy.deadline_ms(state.last_delivered_end_ms),
                    ))
                    logger.warning("Feed interrupted; attempting same-run continuity",
                                   extra={"action": "marketdata_interruption_observed", "symbol": symbol, "cause": cause})
                    complete = assembler.flush_if_complete()
                    if complete is not None:
                        async for bar in self._resolve_emitted(state, liveness, complete):
                            yield bar
                    liveness.first_bar_seen = False
                    await wait_for_healthy(self._client, state)
                    new_generation = int(getattr(self._client, "connection_generation", 0))
                    state.last_recovered_ref = await state.record(state.event(
                        "recovered", generation_from=state.generation, generation_to=new_generation,
                        last_delivered_end_ms=state.last_delivered_end_ms,
                    ))
                    logger.info("Feed continuity recovered",
                                extra={"action": "marketdata_interruption_recovered", "symbol": symbol,
                                       "generation_to": new_generation})
                    state.generation = new_generation
                except NotConnectedError as exc:
                    if state.last_delivered_end_ms is None:
                        raise MarketDataFeedError(str(exc)) from exc
                    await wait_for_healthy(self._client, state)  # re-entry raced the reconnect; keep waiting
                except IBKRBarStreamError as exc:
                    raise MarketDataFeedError(str(exc)) from exc
        finally:
            ...  # detach bookkeeping exactly as in the legacy path
```

`_resolve_emitted(state, liveness, ibkr_bar)` (async generator on the feed): first, if `state.last_delivered_end_ms is not None and ibkr_bar.start_ms > state.last_delivered_end_ms`, iterate the wholly missed windows `for start in range(state.last_delivered_end_ms, ibkr_bar.start_ms, SOURCE_BAR_MS): await resolve_unresolvable_window(state, start, start + SOURCE_BAR_MS)` (a refusal raises; a gap advances `state.last_delivered_end_ms`). Then, if `is_unresolvable(ibkr_bar)`: `await resolve_unresolvable_window(state, ibkr_bar.start_ms, ibkr_bar.end_ms)` and yield nothing. Otherwise translate, set `continuity_event_ref=state.last_recovered_ref.ref()` when provenance is `realtime_across_reconnect` (use `bar.model_copy(update=...)`), update the liveness watermarks and `first_bar_seen` exactly as the legacy path does, set `state.last_delivered_end_ms = bar.end_ms`, yield.

- [ ] **Step 4: Run**

Run: `DATA_PLANE_CONTROL_SECRET="" /Users/inkant/learn-ai/PythonDataService/.venv/bin/python -m pytest tests/marketdata/ tests/broker/ibkr/ -q -p no:cacheprovider`
Expected: all pass, including every pre-existing test in `tests/marketdata/test_feed.py`.

- [ ] **Step 5: Commit**

```bash
git add app/marketdata/ibkr_continuity.py app/marketdata/ibkr_feed.py app/broker/ibkr/config.py tests/marketdata/test_feed_continuity.py
git commit -m "feat(marketdata): fail-closed same-run continuity across feed interruptions

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: Bot layer — policy author, run-scoped sink, admission on delivery

**Files:**
- Create: `app/services/feed_continuity_policy.py`
- Modify: `app/services/bot_trade_strategy.py` (`_RetainedSourceBarFeed`, `run_trade_bot`, `run_dry_run_bot`)
- Test: `tests/services/test_feed_continuity_policy.py` (new), `tests/services/test_run_trade_bot_source_bars.py` (append), `tests/services/bot_runner/test_registry_lifecycle.py` (append), `tests/_helpers/bot_runner/doubles.py` (`_FakeFeed` mode `interrupt`)

**Interfaces:**
- Consumes: Tasks 4–7.
- Produces:

```python
# app/services/feed_continuity_policy.py
class FeedContinuityRefused(MarketDataFeedError): ...   # reason always set
def continuity_policy_for(binding: BrokerBotBinding, ledger: SourceBarLedger) -> ContinuityPolicy | None
# app/services/bot_trade_strategy.py
class _RetainedSourceBarFeed:
    def __init__(self, source: MarketDataFeed, ledger: SourceBarLedger, *, run_id: str, continuity: ContinuityPolicy | None = None) -> None
```

Behavior:
- `continuity_policy_for`: returns `None` and logs `action="feed_continuity_not_offered"` with `reason` when `binding.sealed_program is None` (`"unsealed_binding"`), when `binding.use_rth is False` (`"all_session_not_supported"`, R1), or when `decision_timeframe_ms_for_binding(binding)` is `None` (`"no_decision_timeframe"`). Otherwise `ContinuityPolicy(decision_session="rth", next_trigger_ms=rth_next_trigger_function(tf), substitution_grant=_refuse_everything, record_event=_sink)` where `_refuse_everything(start, end)` returns `SubstitutionRefusal(reason="SUBSTITUTION_NOT_AUTHORIZED")` and `async def _sink(event): return ledger.append_event(event, run_id=binding.run_id)`.
- `_RetainedSourceBarFeed.stream_bars(symbol, *, use_rth=True, continuity=None)` ignores the caller's `continuity` and passes **its own** `self._continuity` to the source: `async for bar in self._source.stream_bars(symbol, use_rth=False, continuity=self._continuity)`. Per bar: if `self._continuity is not None and bar.provenance != "realtime" and self._continuity.is_trigger_ms(bar.end_ms) and now_ms_utc() > bar.end_ms + self._continuity.delivery_allowance_ms`: `await self._continuity.record_event(FeedContinuityEvent(kind="refused", feed_id=bar.feed_id, symbol=bar.symbol, observed_at_ms=now_ms_utc(), reason="DECISION_LATE", window_start_ms=bar.start_ms, window_end_ms=bar.end_ms, bar_identity=f"{bar.feed_id}:{bar.symbol}:{bar.start_ms}:{bar.end_ms}"))` then `raise FeedContinuityRefused(f"trigger bar {bar.start_ms}..{bar.end_ms} delivered after the allowance", reason="DECISION_LATE")`. Then `self._ledger.append(bar, run_id=self._run_id)` and the existing session filter.
- `run_trade_bot` / `run_dry_run_bot`: `policy = None if source_bars is None else continuity_policy_for(binding, source_bars)`; `_RetainedSourceBarFeed(feed, source_bars, run_id=binding.run_id, continuity=policy)`.
- `_FakeFeed` gains mode `"interrupt"`: yields its first bar; then, if `continuity` was given, awaits `continuity.record_event(FeedContinuityEvent(kind="interruption", feed_id="fake", symbol=symbol, observed_at_ms=now_ms_utc(), cause="socket_down"))` and `record_event(... kind="recovered" ...)`; yields the remaining bars with `provenance="realtime_across_reconnect"` (`model_copy`); then waits forever like `"hold"`. It always records `self.continuity_seen = continuity`.

- [ ] **Step 1: Write the failing tests**

`tests/services/test_feed_continuity_policy.py`:

```python
from __future__ import annotations

from pathlib import Path

from app.marketdata.feed import FeedContinuityEvent, SubstitutionRefusal
from app.services.feed_continuity_policy import continuity_policy_for
from app.services.source_bar_ledger import SourceBarLedger


def _sealed_rth_binding():
    """Build a sealed RTH binding the way the replay-proof tests do.

    Search tests/ for `sealed_program=` (e.g. tests/services/test_run_replay_proof_assembly.py
    or tests/_helpers/bot_runner/custody.py) and reuse that builder here; the binding must have
    a non-None `sealed_program` whose `configured_signal.data.decision_timeframe_ms == 900_000`,
    `use_rth=True`, and a `run_id`.
    """
    raise NotImplementedError  # replace with the reused builder


def test_unsealed_binding_gets_no_policy(tmp_path: Path) -> None:
    binding = _sealed_rth_binding().model_copy(update={"sealed_program": None})
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="acct")
    try:
        assert continuity_policy_for(binding, ledger) is None
    finally:
        ledger.close()


def test_all_session_binding_gets_no_policy(tmp_path: Path) -> None:
    binding = _sealed_rth_binding().model_copy(update={"use_rth": False})
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="acct")
    try:
        assert continuity_policy_for(binding, ledger) is None
    finally:
        ledger.close()


async def test_sealed_rth_binding_gets_a_refuse_everything_policy_with_a_run_scoped_sink(tmp_path: Path) -> None:
    binding = _sealed_rth_binding()
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="acct")
    try:
        policy = continuity_policy_for(binding, ledger)
        assert policy is not None and policy.decision_session == "rth"
        assert policy.substitution_grant(0, 60_000) == SubstitutionRefusal(reason="SUBSTITUTION_NOT_AUTHORIZED")
        ref = await policy.record_event(
            FeedContinuityEvent(kind="interruption", feed_id="ibkr", symbol="SPY", observed_at_ms=1)
        )
        assert ref.run_id == binding.run_id
        assert [e.kind for e in ledger.events(run_id=binding.run_id)] == ["interruption"]
        assert policy.next_trigger_ms(1_788_375_600_000) == 1_788_375_600_000 + 60_000  # 15:00 ET 2026-09-02 -> 15:01
    finally:
        ledger.close()
```

The `raise NotImplementedError` in `_sealed_rth_binding` is a placeholder for the implementer to replace with the repo's existing sealed-binding builder; the three tests must pass with a real binding.

Append to `tests/services/test_run_trade_bot_source_bars.py` (reuse that file's imports; `_T0`, `_bar`, `_FakeFeed` come from the same helpers the file already uses — if it lacks a fake feed, import `_FakeFeed` from `tests._helpers.bot_runner.doubles` and `_bar`/`_T0` from `tests.services.bot_runner._support`):

```python
async def test_retained_feed_appends_bars_with_the_run_id_and_provenance(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="acct")
    try:
        feed = _RetainedSourceBarFeed(_FakeFeed([_bar(_T0)], mode="finite"), ledger, run_id="run-x")
        async for _ in feed.stream_bars("SPY", use_rth=True):
            pass
        retained = ledger.bars(provider="fake", symbol="SPY")
        assert [b.run_id for b in retained] == ["run-x"]
        assert retained[0].provenance == "realtime"
    finally:
        ledger.close()


async def test_late_non_realtime_trigger_bar_is_refused_as_decision_late(tmp_path: Path, monkeypatch) -> None:
    from app.services import bot_trade_strategy as module
    from app.services.feed_continuity_policy import FeedContinuityRefused

    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="acct")
    events: list = []

    async def _sink(event):
        events.append(event)
        return ContinuityEventRef(run_id="run-x", evidence_seq=len(events))

    policy = ContinuityPolicy(
        decision_session="rth", next_trigger_ms=lambda last: _T0 + 60_000,
        substitution_grant=lambda s, e: SubstitutionRefusal(reason="SUBSTITUTION_NOT_AUTHORIZED"), record_event=_sink,
    )
    late = _bar(_T0).model_copy(update={"provenance": "realtime_across_reconnect"})
    monkeypatch.setattr(module, "now_ms_utc", lambda: _T0 + 60_000 + 20_001)
    try:
        feed = _RetainedSourceBarFeed(_FakeFeed([late], mode="finite"), ledger, run_id="run-x", continuity=policy)
        with pytest.raises(FeedContinuityRefused) as excinfo:
            async for _ in feed.stream_bars("SPY", use_rth=True):
                pass
        assert excinfo.value.reason == "DECISION_LATE"
        assert events[-1].kind == "refused" and events[-1].reason == "DECISION_LATE"
        assert ledger.bars(provider="fake", symbol="SPY") == []
    finally:
        ledger.close()


async def test_late_non_trigger_bar_is_admitted(tmp_path: Path, monkeypatch) -> None:
    from app.services import bot_trade_strategy as module

    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="acct")
    events: list = []

    async def _sink(event):
        events.append(event)
        return ContinuityEventRef(run_id="run-x", evidence_seq=len(events))

    policy = ContinuityPolicy(
        decision_session="rth", next_trigger_ms=lambda last: _T0 + 15 * 60_000,  # _T0 + 60_000 is not a trigger
        substitution_grant=lambda s, e: SubstitutionRefusal(reason="SUBSTITUTION_NOT_AUTHORIZED"), record_event=_sink,
    )
    late = _bar(_T0).model_copy(update={"provenance": "realtime_across_reconnect"})
    monkeypatch.setattr(module, "now_ms_utc", lambda: _T0 + 60_000 + 20_001)
    try:
        feed = _RetainedSourceBarFeed(_FakeFeed([late], mode="finite"), ledger, run_id="run-x", continuity=policy)
        async for _ in feed.stream_bars("SPY", use_rth=True):
            pass
        assert len(ledger.bars(provider="fake", symbol="SPY")) == 1
        assert events == []
    finally:
        ledger.close()
```

Append to `tests/services/bot_runner/test_registry_lifecycle.py`, using the same registry/deploy helpers the file's existing `test_feed_death_records_feed_death_crash` uses:

```python
@pytest.mark.asyncio
async def test_count_complete_interruption_keeps_the_run_running(tmp_path: Path) -> None:
    feed = _FakeFeed([_bar(_T0), _bar(_T0 + 60_000)], mode="interrupt")
    registry = _registry(tmp_path, feed)
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    await _wait_for(lambda: feed.bars_consumed == 2)

    view = registry.status("alpaca", _SID)
    assert view.running is True
    assert view.duty_outcome is None
```

If the deploy in this file yields an unsealed binding, `continuity_policy_for` returns `None` and `_FakeFeed` sees `continuity=None`; the test still asserts the run survives the fake's interruption because `_FakeFeed` in `interrupt` mode never raises. Keep it as the lifecycle pin.

- [ ] **Step 2: Run to verify they fail**

Run: `DATA_PLANE_CONTROL_SECRET="" /Users/inkant/learn-ai/PythonDataService/.venv/bin/python -m pytest tests/services/test_feed_continuity_policy.py tests/services/test_run_trade_bot_source_bars.py tests/services/bot_runner/test_registry_lifecycle.py -q -p no:cacheprovider -k "continuity or run_id or decision_late or admitted or interruption"`
Expected: FAIL (module missing; `run_id` kwarg unknown).

- [ ] **Step 3: Implement** per Behavior above. The `_RetainedSourceBarFeed` constructor becomes keyword-only for `run_id` and `continuity`; update the two constructions in `run_trade_bot` and `run_dry_run_bot` and every direct construction in tests (`grep -rn "_RetainedSourceBarFeed(" tests/`).

- [ ] **Step 4: Run**

Run: `DATA_PLANE_CONTROL_SECRET="" /Users/inkant/learn-ai/PythonDataService/.venv/bin/python -m pytest tests/services/test_feed_continuity_policy.py tests/services/test_run_trade_bot_source_bars.py tests/services/bot_runner/ tests/services/test_source_bar_ledger.py tests/services/test_signal_program_crash_replay.py tests/services/test_run_replay_receipt_end_to_end.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/services/feed_continuity_policy.py app/services/bot_trade_strategy.py tests/services/test_feed_continuity_policy.py tests/services/test_run_trade_bot_source_bars.py tests/services/bot_runner/test_registry_lifecycle.py tests/_helpers/bot_runner/doubles.py
git commit -m "feat(bots): author the feed continuity policy per run and admit recovered bars on delivery

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: Replay receipt commits to the continuity events

**Files:**
- Modify: `app/schemas/run_replay.py` (`RunReplayReceipt`), `app/services/run_replay_proof.py` (`bar_set_digest`, new `continuity_event_digest`, `write_pending`, `_skeleton`, `_compute`, `_final_receipt`, `bounded_replay_bars`), `contracts/openapi/python-data-service.openapi.json` (regenerated)
- Test: `tests/services/test_run_replay_receipt_store.py`, `tests/services/test_run_replay_proof_assembly.py` (append)

**Interfaces:**
- Produces: `RunReplayReceipt.continuity_event_digest: str | None = None`, `RunReplayReceipt.evidence_end_seq: int | None = None`; `continuity_event_digest(events: Sequence[RetainedContinuityEvent]) -> str` (sha256 of canonical JSON — `sort_keys=True, separators=(",", ":")` — of `[{kind, symbol, observed_at_ms, cause, generation_from, generation_to, window_start_ms, window_end_ms, bar_identity, authorization_id, reason, last_delivered_end_ms, deadline_ms}]` in `evidence_seq` order); `bar_set_digest` adds `"provenance": bar.provenance` to a row's payload **only when** `bar.provenance != "realtime"`; `bounded_replay_bars(bars, *, ledger_end_seq, terminal_recorded_at_ms, evidence_end_seq=None)` — when `evidence_end_seq` is given, bars with an `evidence_seq` are kept iff `evidence_seq <= evidence_end_seq`, and bars without one fall back to the `ledger_end_seq` rule.

- [ ] **Step 1: Write the failing tests** (append to `tests/services/test_run_replay_proof_assembly.py`; build `_retained(seq, evidence_seq=None, provenance="realtime")` with `RetainedSourceBar(...)` and `_event(seq, evidence_seq, kind)` with `RetainedContinuityEvent(...)` helpers in the module; for the legacy-receipt test copy the receipt fixture dict already used in `tests/services/test_run_replay_receipt_store.py`)

```python
def test_bar_set_digest_is_unchanged_for_realtime_streams_and_changes_with_a_substitute() -> None:
    bars = [_retained(seq=1), _retained(seq=2)]
    before = bar_set_digest(bars)
    assert bar_set_digest([b.model_copy(update={"provenance": "realtime"}) for b in bars]) == before
    with_substitute = bar_set_digest([bars[0], bars[1].model_copy(update={"provenance": "historical_substitute"})])
    assert with_substitute != before


def test_continuity_event_digest_is_stable_and_order_sensitive() -> None:
    events = [_event(seq=1, evidence_seq=3, kind="interruption"), _event(seq=2, evidence_seq=5, kind="recovered")]
    assert continuity_event_digest(events) == continuity_event_digest(list(events))
    assert continuity_event_digest(events) != continuity_event_digest(events[:1])
    assert continuity_event_digest(events) != continuity_event_digest(list(reversed(events)))


def test_bounded_replay_bars_prefers_the_evidence_bound() -> None:
    bars = [_retained(seq=1, evidence_seq=1), _retained(seq=2, evidence_seq=4), _retained(seq=3, evidence_seq=6)]
    kept = bounded_replay_bars(bars, ledger_end_seq=3, terminal_recorded_at_ms=None, evidence_end_seq=4)
    assert [b.seq for b in kept] == [1, 2]


def test_receipt_without_new_fields_still_parses() -> None:
    receipt = RunReplayReceipt.model_validate(_legacy_receipt_dict())
    assert receipt.continuity_event_digest is None and receipt.evidence_end_seq is None
```

And in `tests/services/test_run_replay_receipt_store.py`, next to the existing `write_pending` test, add `test_pending_receipt_snapshots_the_evidence_end_seq`: build a `SourceBarLedger` under the service's `artifacts_root` for the binding's ledger account (`ledger_account_id_for(binding)`), append two bars and one event for the binding's `run_id`, call `service.write_pending(binding, run_id)`, read the receipt back and assert `receipt.evidence_end_seq == ledger.evidence_end_seq()` and `receipt.ledger_end_seq == 2`.

- [ ] **Step 2: Run to verify they fail** — the two files with `-k "digest or evidence_bound or evidence_end_seq or still_parses"`.

- [ ] **Step 3: Implement.** `_skeleton` gains `evidence_end_seq: int | None = None`. `write_pending` snapshots `ledger.evidence_end_seq()` next to `latest.seq`. In `_compute`, read `events = ledger.events(run_id=run_record.run_id, evidence_end_seq=evidence_bound)` inside `_compute_sync` on the same open ledger (where `evidence_bound = stored.evidence_end_seq if stored else None`, falling back to `ledger.evidence_end_seq()` when None), pass `evidence_end_seq=evidence_bound` to `bounded_replay_bars`, and hand `events` to `_final_receipt`, which sets `"continuity_event_digest": continuity_event_digest(events)` and `"evidence_end_seq": evidence_bound`. Then regenerate the contract: `/Users/inkant/learn-ai/PythonDataService/.venv/bin/python scripts/export_openapi_contract.py` (run from `PythonDataService/`; it writes `../contracts/openapi/python-data-service.openapi.json`), then `… --check` must pass.

- [ ] **Step 4: Run** — `DATA_PLANE_CONTROL_SECRET="" /Users/inkant/learn-ai/PythonDataService/.venv/bin/python -m pytest tests/services/test_run_replay_receipt_store.py tests/services/test_run_replay_proof_assembly.py tests/services/test_run_replay_proof_service.py tests/services/test_run_replay_receipt_end_to_end.py tests/services/test_run_replay_stop_trigger.py -q -p no:cacheprovider` plus `$(grep -rl "run_replay" tests/routers | tr '\n' ' ')`, plus the contract `--check`.

- [ ] **Step 5: Commit**

```bash
git add app/schemas/run_replay.py app/services/run_replay_proof.py ../contracts/openapi/python-data-service.openapi.json tests/services/test_run_replay_receipt_store.py tests/services/test_run_replay_proof_assembly.py
git commit -m "feat(replay): commit the run receipt to its continuity events and evidence bound

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: ADR 0053 and the reference note

**Files:**
- Create: `docs/architecture/adrs/0053-feed-continuity-same-run-recovery.md`, `docs/references/feed-reconnect-continuity.md`

- [ ] **Step 1: Write the ADR** in the house format (copy the header shape of `docs/architecture/adrs/0052-archive-is-an-operator-declared-terminal-exit.md`: title, `**Status:** Accepted 2026-09-02`, Provenance, Decision drivers, Related, `## Context`, `## Decision` (numbered), `## Consequences`). Decisions to record: (1) same-run continuity inside the feed under a consumer-authored `ContinuityPolicy`; auto-Resume via recovery callbacks rejected; (2) deadline = next trigger + 20 s operational delivery allowance, decision lateness zero; (3) completeness by RTH contribution count (12 per minute, measured 2026-09-02); unresolvable minutes are refused inside the decision session and surfaced as gaps outside it; (4) historical substitution is fail-closed and absent from this build; the authorization design (shape-bounded, single-session, exhaustive settlement-tree proof) is recorded in the spec §4.6/§4.8 and not implemented; (5) generation fencing on client, lease, registry, cancel and acquisition; (6) provenance on bars and ledger rows, continuity events as an awaited run-scoped channel, one evidence journal, receipt digest; (7) `decision_session="all"` deferred (R1); the first minute of a deploy keeps today's behavior (R2). Cite ADR 0018 D5 and ADR 0046 as untouched and the spec as the full record.
- [ ] **Step 2: Write the reference note**: the 2026-09-02 measurements (5-second contiguity 1017/1020 RTH minutes across SPY/TSLA/AAPL; historical-vs-realtime close identical 1053/1054 with the two clean TSLA divergences at 14:14 and 14:23), the journald retrieval recipe (`podman machine ssh 'journalctl -o short-iso --utc CONTAINER_NAME=polygon-data-service --since "<VM-local CDT time>"'`), and one line per reason code (`DECISION_BAR_MISSED`, `SOURCE_MINUTE_UNRESOLVABLE`, `SUBSTITUTION_NOT_AUTHORIZED`, `SUBSTITUTION_PATH_UNAVAILABLE`, `CONTINUITY_EVIDENCE_UNWRITABLE`, `DECISION_LATE`) and per event kind.
- [ ] **Step 3: Commit**

```bash
git add docs/architecture/adrs/0053-feed-continuity-same-run-recovery.md docs/references/feed-reconnect-continuity.md
git commit -m "docs: ADR 0053 feed continuity and the reconnect reference note

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 11: Verification sweep

- [ ] **Step 1: Lint at project scope**: `/Users/inkant/learn-ai/PythonDataService/.venv/bin/ruff check app/ tests/` → zero findings (fix any in a `chore(lint)` commit).
- [ ] **Step 2: Targeted suites** (every surface touched and every consumer of a shared helper edited):

```
DATA_PLANE_CONTROL_SECRET="" /Users/inkant/learn-ai/PythonDataService/.venv/bin/python -m pytest \
  tests/broker/ibkr tests/marketdata tests/services/test_source_bar_ledger.py tests/services/test_decision_clock.py \
  tests/services/test_feed_continuity_policy.py tests/services/test_run_trade_bot_source_bars.py \
  tests/services/test_bot_binding_authority_source_bars.py tests/services/bot_runner \
  tests/services/test_run_replay_receipt_store.py tests/services/test_run_replay_proof_assembly.py \
  tests/services/test_run_replay_proof_service.py tests/services/test_run_replay_receipt_end_to_end.py \
  tests/services/test_run_replay_stop_trigger.py tests/services/test_run_replay_fidelity.py \
  tests/services/test_run_replay_engine_parity.py tests/services/test_signal_program_crash_replay.py \
  tests/broker/alpaca/clerk/sqlite \
  -q -p no:cacheprovider
```

  plus `$(grep -rl "TradeBarConsolidator\|live_bar_aggregator\|market_data_feed" tests/ | tr '\n' ' ')`.
  Expected: all pass. Anything failing that also fails on `origin/master` at the same path is pre-existing: list it in the report; anything else is yours.
- [ ] **Step 3: Contract**: `/Users/inkant/learn-ai/PythonDataService/.venv/bin/python scripts/export_openapi_contract.py --check` → exit 0.
- [ ] **Step 4: Report** the three results verbatim in the task report; no commit unless lint fixes were needed.
