# Lane B: IBKR market data is live machinery (#2080, #2077) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The IB Gateway is the bar source Alpaca bots consume. Fix the overnight reconnect log storm with an outage-scoped log budget and a durable unreachable-since anchor; document the dependency; do not rename env vars now.

**Architecture:** See the lane report below; every task carries its own files, failing test, implementation and commit.

**Tech Stack:** Python 3.12 / FastAPI / pytest (PythonDataService), Angular 22 / Vitest (Frontend), Podman Compose, GitHub Actions.

**Spec:** the GitHub issues named in the title, plus `docs/broker-clerk-fleet-authority.md` §6 (trust register). Master sequencing, owner decisions, deploy windows and the delivery protocol live in `2026-09-14-fleet-trust-register-fixes.md` — read it first; its Global Constraints apply to every task here.

## Global Constraints

Inherited verbatim from `2026-09-14-fleet-trust-register-fixes.md` § Global Constraints. In particular: never switch branches in `/Users/inkant/learn-ai` (the clerks bind-mount it); every `./restart.sh` drops both clerks; import-time changes need a no-bot window; run the thermo review once before the first push; fleet tests run unsandboxed.

## Corrections applied by the integrating session

- The premise of #2080 (gateway decommissioned) is wrong; this lane was re-aimed mid-investigation after the clerks connected to the gateway at 12:34 UTC on 2026-09-14.
- Owner action outside code: enable IB Gateway auto-restart (Configure > Settings > Lock and Exit > Auto restart). A gateway logged out at the first RTH trigger kills a running bot (FEED_DEATH).

---

*The report below was produced by an Opus planning agent on 2026-09-14 against origin/master `e2fdb23f` plus the branches of PR #2081 and PR #2078. Every file:line claim was verified by that agent at that time; re-verify after those PRs merge.*

---

# IBKR residue — verified facts, decisions, and a plan for #2080 / #2077

Re-aimed for the corrected premise: **IB Gateway is live infrastructure, not residue.** It is the bar source every Alpaca bot consumes. `IBKR_BROKER_ENABLED=true` and the connect loop are both load-bearing. Every option that stops the client connecting is now disqualified; I verified the mechanism anyway so the disqualification is evidence-backed rather than assumed.

---

## 1. Verified facts

### A — What `IBKR_BROKER_ENABLED` actually gates

- **CONFIRMED** — the setting is `IbkrSettings.broker_enabled`, `PythonDataService/app/broker/ibkr/config.py:92`, env `IBKR_BROKER_ENABLED`, default `True`, prefix `IBKR_` (`config.py:41-46`).
- **CONFIRMED** — there are exactly **two** read sites in the whole repo: `app/main.py:675` (`if _ROLE_RUNS_CLERK and ibkr_settings.broker_enabled:`) and `app/routers/broker_dependencies.py:13` (`return not get_settings().broker_enabled`). No third.
- **CONFIRMED** — the true branch at `main.py:675-736` installs five things, not one: (1) `IbkrClient` + `set_client` (676-682); (2) `set_desired_connected(connect_on_startup)` (689); (3) the startup `connect()` (692); (4) `AutoReconnectMonitor(...).start()` + `set_monitor` (719-726), with `LIVE_BAR_AGGREGATOR.resubscribe_all` as recovery callback (723); (5) `set_market_data_feed(IbkrMarketDataFeed(ibkr_client))` (`main.py:735`). Memory's `733-735` citation is **CONFIRMED** (733 is the import, 735 the install).
- **CONFIRMED** — `main.py:746-750` is the comment memory quotes, verbatim: "without the shared MarketDataFeed a deploy fails with a typed 503".
- **CONFIRMED** — `main.py:756-762` builds `BotTaskRegistry(artifacts_root=Path(ibkr_settings.live_runs_root).parent, feed_resolver=get_market_data_feed, supported_broker_ids=frozenset({"alpaca"}), ...)`. Exact lines 757-762.
- **CONFIRMED** — `broker_dependencies.is_broker_disabled()` gates only IBKR HTTP routes: `broker.py:150,223` and `require_connected_client()` at `broker.py:279,304,334,394,499` plus `broker_capability.py:26`. No Alpaca route reads it.

### B — What the Alpaca bot runner does with `MarketDataFeed`

- **CONFIRMED, and stronger than "presence check"** — the bot **streams its decision bars through the feed**: `app/services/bot_runtime.py:156` (`async for bar in feed.stream_bars(...)`), `app/services/bot_trade_strategy.py:584` (same, trade path), `app/services/bot_trade_strategy_warmup.py:144` (`await feed.recent_closed_bars(...)`). IBKR is the live bar source for Alpaca execution; Alpaca supplies execution only.
- **CONFIRMED** — admission is a **health** check, not a presence check. `app/services/bot_start_admission.py:618-624` returns `UNAVAILABLE` for a missing feed; `:626` calls `feed.health(symbol)`; `:666-668` maps `not health.connected → "UNAVAILABLE"`. `app/services/run_admission.py:337-346` refuses any operation whose `market_data.state != "AVAILABLE"`, **before** the `mode != "dry_run"` carve-out at `:365`, so log_only and dry_run are refused too.
- **CONFIRMED by execution** — I ran `market_data_admission_fact` against both shapes:
  - disconnected `IbkrMarketDataFeed` → `state=UNAVAILABLE`, reason `"IBKR connection lost"`
  - `feed=None` → `state=UNAVAILABLE`, reason `"The process-level market-data feed is not installed."`
  Both become `MARKET_DATA_UNAVAILABLE` → `MarketDataFeedUnavailableError` (`app/services/bot_runner_errors.py:99-111`, `:43` `http_status = 503`) → HTTP 503 at `app/routers/broker_bots.py:61-73`.
  **This is the correction that matters:** the typed 503 is caused by *feed health*, not by the flag. A gateway-down clerk already refuses every Alpaca deploy today. Flipping `IBKR_BROKER_ENABLED=false` would not have "started" that failure; it would have made it permanent.
- **CONFIRMED** — `IbkrMarketDataFeed.health()` (`app/marketdata/ibkr_feed.py:378`) computes `connected = self._client.is_connected() and not self._client.connection_lost`. `IbkrClient.is_connected` is `bool(self._ib.isConnected())` (`client.py:564-565`).
- **CONFIRMED** — memory's None-safe consumers: `app/broker/alpaca/clerk/stream_health.py:206-209` and `app/services/bot_start_admission.py:156-166`. Both are None-safe, but **`market_data_channel_health`** (`stream_health.py:48-67`) returns `healthy=False` for *either* a missing feed *or* a disconnected one, so the dual-health submission gate refuses order submission in both cases. "Merely degrade" overstates it.
- **CONFIRMED** — the reconnect loop is started by `main.py:719-726`, **not** by the feed. `AutoReconnectMonitor._tick` (`auto_reconnect_monitor.py:275`) returns immediately when `not self._client.desired_connected`. Options that "make the feed lazy" would not touch the loop at all.
- **CONFIRMED** — a running bot survives a gateway drop *bounded by its own decision clock*: `app/services/feed_continuity_policy.py:112-139` authors a `ContinuityPolicy` whose deadline is `session.next_trigger_function(timeframe_ms)`; `app/marketdata/ibkr_continuity.py:346-365` raises `MarketDataFeedError(reason="DECISION_BAR_MISSED")` when the deadline passes, and `app/services/bot_runner.py:1528-1540` finalises the run as a crash with `reason_code="FEED_DEATH"`. **An overnight blackout that is still in effect at the first RTH trigger kills the running bot.**
- **CONFIRMED** — `PolygonReplayMarketDataFeed` (`app/broker/alpaca/clerk/sqlite/qualification_polygon_replay.py:87-130`) already satisfies the protocol without being IBKR, proving the port is genuinely structural.

### C — Where the log volume comes from

- **CONFIRMED** — `app/broker/ibkr/client.py:410-448`: one `logger.info("[STEP 1/3] IBKR connect attempt %d/%d …")` (`:415`) and one `logger.warning("IBKR connect attempt %d failed: %s")` (`:444`) per attempt, `connect_attempts` default 3 (`config.py:85`).
- **CONFIRMED** — the two ERROR lines are ib_async's own, both from `logging.getLogger("ib_async.client")` inside one `except` block: `.venv/.../ib_async/client.py:232` (`API connection failed: …`) and `:235` (`Make sure API port on TWS/IBG is open`, emitted only for `ConnectionRefusedError`). They are a strict duplicate of information our own WARNING already carries.
- **CONFIRMED** — cadence is the breaker's open-state probe, not a tight loop: `OPEN_PROBE_INTERVAL_S = 60.0` (`auto_reconnect_monitor.py:92`), `MAX_RECONNECT_ATTEMPTS = 10` (`:89`). 30 min ÷ 60 s = 30 cycles × 3 attempts = 90; issue #2080 counts 87/87/29×3. Arithmetic matches exactly.
- **DEFECT FOUND, not in either issue** — every open-circuit probe re-stamps the transition clock. `_last_transition_ms = now_ms_utc()` at `auto_reconnect_monitor.py:591,597,604,645,650,656`, all reachable from `_probe_open_circuit_if_due` (`:621`). `build_broker_health` (`app/broker/ibkr/health.py:42`) publishes that as `last_transition_ms`, and the cockpit derives "since". **During an 8-hour blackout the operator sees "hard_down, 12 s ago", once a minute, forever.** There is no durable outage anchor anywhere.
- **WRONG (memory `ibkr-gateway-nightly-blackout` is stale on two points)** — `is_ibkr_north_america_reset_window` and `broker_session_events.py` do not exist on master; and nothing under `app/` writes `connection_events.jsonl` any more. Both were retired with #1813. The measurement methodology in that memory is no longer reproducible from live data.

### D — The `IBKR_*` names carrying Alpaca data

All grep results below are the complete set on master.

- **CONFIRMED misnamed — `IBKR_LIVE_RUNS_ROOT`.** Declared `config.py:108`. Read at `main.py:758` (`…parent` = the bot artifacts root), `app/broker/alpaca/clerk/fleet_boot.py:400`, `app/services/market_data_capability_service.py:22`, and via `live_artifacts_root()` (`config.py:194-204`) at `main.py:442,446,466`, `app/routers/brokers.py:64,765`, `app/broker_configuration/prior_obligations.py:55,143`, `app/services/live_arming_admission.py:24,67`, `app/services/broker_v2_panel/sqlite_roster_status.py:26,88,142`, `app/services/broker_v2_panel/panel_data_source.py:34,164`, `PythonDataService/scripts/manage_alpaca_shadow.py:312-314`, `PythonDataService/scripts/manage_alpaca_arming.py:573`, `PythonDataService/scripts/bench_panel_read_latency.py:622`. Compose: `compose.fleet.yaml:30`, `compose.yaml:216`. Docs: `docs/design/fleet-a2-lane-inventory.md:19-20`, `docs/runbooks/fleet-d-two-clerk-rollout.md:80`, `docs/runbooks/fleet-directory-unavailable.md:107,118`. Tests: `tests/scripts/test_bench_panel_read_latency.py:26-31`, `tests/broker/fleet/test_a2_alpaca_lane.py:291`, plus `tests/contracts/test_alpaca_active_authority_wiring.py`, `tests/services/test_alpaca_bot_identity.py`. Content is 100 % Alpaca: `live_state/` bindings, lifecycle, arming seals, Clerk custody.
- **PARTLY misnamed — `IBKR_LIVE_BARS_ROOT`.** Declared `config.py:116`; read at `app/services/live_bar_aggregator.py:464` and `fleet_boot.py:401`; compose `compose.fleet.yaml:31`. The *bars* in it genuinely are IBKR `reqRealTimeBars` output (`live_bar_aggregator.py` module docstring, lines 1-30). Only its **location** is Alpaca-lane-local. It is misplaced, not misnamed.
- **NOT misnamed — `IBKR_READONLY`.** Declared `config.py:89`; exactly one read site, `app/broker/ibkr/client.py:427`, passed straight to `ib_async.IB.connectAsync(readonly=…)`. It is a real IBKR client parameter. **Renaming it would be wrong.** The issue text is incorrect to group it with the other two.
- **NOT misnamed** — `IBKR_HOST`, `IBKR_PORT`, `IBKR_CLIENT_ID`, `IBKR_BROKER_ENABLED`, `IBKR_MODE`, `IBKR_CONNECT_ON_STARTUP`, `IBKR_FEED_CONTINUITY_ENABLED`, `IBKR_REALTIME_BAR_MAX_ACTIVE` all address the real IBKR client.
- **Durable values: CONFIRMED NONE AT RISK from a rename.** A rename changes the variable *name*; the *value* (`/app/artifacts/alpaca_clerk/live_runs`) is what is persisted. `configuration_hash` hashes only the binding (`app/services/bot_carryover.py:80-96`) — no roots. `ClerkRecord.volume_root` (`app/broker/fleet/service.py:314`) records the **clerk volume** (from `ALPACA_CLERK_DIR`, `app/broker/alpaca/config.py:94`), not these roots. The only durable path strings are qualification-evidence fields (`qualification_storage_recovery.py:195,202`), which are derived from the clerk dir.
- **The real rename hazard is deploy skew, not persistence.** If the image reads only `ALPACA_*` while compose still sets `IBKR_*`, `live_runs_root` silently falls back to the default `/app/artifacts/live_runs`. In `clerk_agent` role that fails closed — `_fence_writable_roots` (`fleet_boot.py:385-412`, called from `:180`) refuses boot. In **`combined`** role (`compose.yaml`, the dev/legacy stack) the fence never runs and the process silently adopts an empty tree.
- **CONFIRMED** — no existing test pins any env var in `compose.fleet.yaml`. The nearest is `tests/scripts/test_run_broker_fleet_compose_qualification.py:37-45` (string assertions) and `tests/broker/fleet/test_provider_conformance.py:253-266`.
- **CONFIRMED mechanism** — PyYAML cannot `safe_load` `compose.fleet.yaml` (`!override` tag at line 159), but a `SafeLoader` subclass with `add_multi_constructor('!', …)` loads it and resolves both merge keys correctly. I verified this and read back both clerks' fully merged env.

---

## 2. Decisions for the owner

### C — #2080 ranking, under the corrected premise

| # | Option | Blast radius | Verdict |
|---|---|---|---|
| 1 | `IBKR_CONNECT_ENABLED=false` on the clerks | **Catastrophic.** `IBKR_CONNECT_ON_STARTUP=false` already exists (`config.py:103`) and would do exactly this: `set_desired_connected(False)` at `main.py:689` makes `_tick` return at `auto_reconnect_monitor.py:275` forever. Silence bought by permanently severing the bar source. | **Reject.** |
| 2 | Non-IBKR `MarketDataFeed` for Alpaca | A real Alpaca-data feed is a build (entitlements, IEX vs SIP, bar assembly, continuity, session-phase stamping, `capability_account_id`); `alpaca-py==0.42.0` is already a dep so it is feasible. A "no external feed" stub is worse than useless — it would make admission pass while no bars arrive. | **Defer to its own issue.** Not a #2080 fix. |
| 3 | Lazy `IbkrMarketDataFeed` | Misdiagnosis: the feed never starts the loop; `main.py:719-726` does. Zero effect on the churn. | **Reject.** |
| **4** | **Outage-scoped log budget + a durable outage anchor** | Two files plus one new small module; no lifecycle, cadence or admission change. Fixes the noise *and* the "hard_down 12 s ago" lie. | **Recommend.** |

**Recommendation:** option 4 — collapse the repeated connect-failure logging into one WARNING per outage plus a periodic counter, drop ib_async's duplicate ERROR pair while suppressed, and publish `unreachable_since_ms` so the operator sees one true fact instead of 350 lines. *Reason:* the outage is expected and recurring until the owner enables IB Gateway's Auto restart, so the right fix makes an expected state legible rather than shortening or hiding it.

**Do not change the retry cadence.** *Reason:* the 1/2/4/8/16/32/60×4 ladder and the 60 s open probe are a deliberate, regression-tested outcome of the 2026-08-25 blackout analysis; re-opening them trades a settled recovery guarantee for log volume that option 4 already removes.

### D — the rename slice

**Recommendation: do not rename now; document `IBKR_LIVE_RUNS_ROOT` and `IBKR_LIVE_BARS_ROOT` as deliberate legacy names in `config.py` and `docs/ibkr-integration-authority.md`, and scope the rename to `IBKR_LIVE_RUNS_ROOT` only, deferred behind the Alpaca-feed build.** *Reason:* the rename buys no runtime behaviour, carries a silent-empty-tree failure mode in `combined` role, and `IBKR_LIVE_BARS_ROOT` genuinely holds IBKR bars — so two of the three names the issue calls misnamed are not, and the one that is should move when the module that owns it moves.

Task 4 below still gives you the full alias-based rename if you want it; Task 3 is the cheap documentation alternative.

### E — doc classifications

- **`docs/ibkr-integration-authority.md` — keep canonical, amend.** *Reason:* it is the only doc that owns the surviving read/market-data boundary, and today it under-describes that boundary as evidence-only — the exact gap that let #2077 and #2080 read a live dependency as residue.
- **`docs/architecture/ibkr-integration-tdd.md` — keep as `supporting`, no change.** *Reason:* it is already correctly framed as retired-actuation rationale pointing at the authority doc, and `docs/*/architecture/**` is auto-classified `supporting` by `scripts/check_documentation_contract.py:88-104`.
- **`docs/runbooks/ibkr-setup-guide.md` — keep as a current runbook, amend.** *Reason:* it is the only operator procedure for the connection Alpaca execution depends on; retiring it would leave the nightly-relogin and Auto-restart steps undocumented.

`pytest tests/contracts` must pass after any docs change — `scripts/check_documentation_contract.py:125-151` resolves every repo-local link in canonical, protected-canonical, in-flight and `docs/runbooks/**` docs, and `:236-250` pins retired paths. Note both PRs already move these files: #2078 rewrites `docs/doc-authority.md:157,159,162` rows and `docs/runbooks/ibkr-setup-guide.md:9`. **Rebase any docs task onto `origin/docs/broker-clerk-fleet-authority`.**

---

## 3. Task breakdown

### Task 1 — pin the outage anchor (regression first)

**Files**
- create `PythonDataService/tests/broker/ibkr/test_connect_outage_budget.py`
- modify `PythonDataService/app/broker/ibkr/auto_reconnect_monitor.py`
- modify `PythonDataService/app/broker/ibkr/health.py`
- modify `PythonDataService/app/broker/ibkr/models.py`
- modify `contracts/openapi/python-data-service.openapi.json` (regenerated, not hand-edited)

**Failing test** (append to the new file):

```python
"""#2080 — a long outage must report one age, not the age of the last probe."""

from __future__ import annotations

import pytest

from app.broker.ibkr.auto_reconnect_monitor import AutoReconnectMonitor
from tests.broker.ibkr.test_auto_reconnect_monitor import _FakeClient


@pytest.mark.asyncio
async def test_open_breaker_probes_do_not_reset_the_outage_anchor() -> None:
    clock = {"now": 1_700_000_000_000}
    client = _FakeClient(is_connected=False, connection_lost=False, desired_connected=True)
    monitor = AutoReconnectMonitor(
        client,
        poll_interval_s=0.01,
        initial_backoff_s=0.0,
        max_backoff_s=0.0,
        max_reconnect_attempts=1,
        open_probe_interval_s=0.0,
        now_ms=lambda: clock["now"],
    )

    monitor.start()
    await _wait_for(lambda: monitor.is_hard_down)
    anchored = monitor.unreachable_since_ms
    assert anchored == 1_700_000_000_000

    clock["now"] += 8 * 60 * 60 * 1000  # an eight-hour gateway blackout
    probes = client.connect_calls
    await _wait_for(lambda: client.connect_calls > probes)
    await monitor.stop()

    assert monitor.unreachable_since_ms == anchored


@pytest.mark.asyncio
async def test_a_successful_connect_clears_the_outage_anchor() -> None:
    client = _FakeClient(is_connected=False, connection_lost=False, desired_connected=True)
    monitor = AutoReconnectMonitor(client, poll_interval_s=0.01, initial_backoff_s=0.0)

    monitor.start()
    await _wait_for(lambda: monitor.unreachable_since_ms is not None)
    client.set_connected(True)
    await _wait_for(lambda: monitor.unreachable_since_ms is None)
    await monitor.stop()
```

(`_wait_for` and `_FakeClient` are the existing helpers in `tests/broker/ibkr/test_auto_reconnect_monitor.py`; hoist both into `tests/broker/ibkr/_support.py` in this commit rather than importing across test modules.)

**Run / expected failure**
```
cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/ibkr/test_connect_outage_budget.py -q
```
→ `AttributeError: 'AutoReconnectMonitor' object has no attribute 'unreachable_since_ms'`, and `AutoReconnectMonitor.__init__` rejects `now_ms=`.

**Minimal implementation**
1. `auto_reconnect_monitor.py.__init__` (after line 155): add `now_ms: Callable[[], int] = now_ms_utc` parameter, store as `self._now_ms`, and `self._unreachable_since_ms: int | None = None`. Replace the ten bare `now_ms_utc()` calls in this class with `self._now_ms()`.
2. Add `@property unreachable_since_ms(self) -> int | None: return self._unreachable_since_ms` beside `is_hard_down` (`:183-185`).
3. In `_attempt_under_lifecycle_lock` (`:385`): on the `"failed"` return path set `self._unreachable_since_ms = self._unreachable_since_ms or self._now_ms()`; on `"succeeded"` set it to `None`. Do **not** touch `_last_transition_ms`.
4. `models.py:640` area: add `unreachable_since_ms: int | None = None` to `IbkrConnectionHealth`, documented as "int64 ms UTC of the first failed connect of the current outage; `None` when reachable. Unlike `last_transition_ms`, an open-breaker probe does not reset it."
5. `health.py:build_broker_health` (`:42`): pass `unreachable_since_ms=getattr(monitor, "unreachable_since_ms", None)` into `_with_condition`.
6. Regenerate the contract: `cd PythonDataService && .venv/bin/python scripts/export_openapi_contract.py`.

**Run / expected pass**
```
cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/ibkr/ -q
cd PythonDataService && .venv/bin/python scripts/export_openapi_contract.py --check
ruff check PythonDataService/app/ PythonDataService/tests/
```
→ all green; `--check` clean because the snapshot was regenerated in the same commit.

**Commit:** `fix(ibkr): anchor the outage clock so a long blackout reports its real age`

---

### Task 2 — collapse the connect-failure log storm

**Files**
- create `PythonDataService/app/broker/ibkr/connect_log_budget.py`
- modify `PythonDataService/app/broker/ibkr/client.py`
- create `PythonDataService/tests/broker/ibkr/test_connect_log_budget.py`

**Failing test**

```python
"""#2080 — 350 lines per clerk per 30 min of an expected outage is an ops hazard."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.broker.ibkr.client import BrokerError, IbkrClient
from app.broker.ibkr.config import IbkrSettings
from app.broker.ibkr.connect_log_budget import CONNECT_LOG_BUDGET


@pytest.fixture(autouse=True)
def _fresh_budget():
    CONNECT_LOG_BUDGET.reset_for_testing()
    yield
    CONNECT_LOG_BUDGET.reset_for_testing()


def _refusing_ib() -> tuple:
    fake_ib = MagicMock()
    fake_ib.connectAsync = AsyncMock(side_effect=ConnectionRefusedError(111, "Connection refused"))
    fake_ib.disconnect = MagicMock(return_value=None)
    fake_ib.isConnected = MagicMock(return_value=False)
    fake_ib.client = MagicMock()
    return fake_ib, MagicMock(return_value=fake_ib)


@pytest.mark.asyncio
async def test_a_sustained_outage_logs_once_not_once_per_attempt(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = IbkrSettings(mode="paper", port=4002, connect_attempts=1, _env_file=None)
    fake_ib, fake_class = _refusing_ib()
    caplog.set_level(logging.WARNING, logger="app.broker.ibkr.client")

    with patch("ib_async.IB", fake_class):
        client = IbkrClient(settings)
        for _ in range(10):
            with pytest.raises(BrokerError):
                await client.connect()

    failures = [r for r in caplog.records if "IBKR connect attempt" in r.getMessage()]
    assert len(failures) == 1
    assert failures[0].__dict__["action"] == "ibkr_connect_failed"


@pytest.mark.asyncio
async def test_the_suppressed_count_is_reported_when_the_window_elapses(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = IbkrSettings(mode="paper", port=4002, connect_attempts=1, _env_file=None)
    fake_ib, fake_class = _refusing_ib()
    clock = {"now": 1_700_000_000_000}
    CONNECT_LOG_BUDGET.reset_for_testing(now_ms=lambda: clock["now"])
    caplog.set_level(logging.WARNING, logger="app.broker.ibkr.client")

    with patch("ib_async.IB", fake_class):
        client = IbkrClient(settings)
        for _ in range(5):
            with pytest.raises(BrokerError):
                await client.connect()
        clock["now"] += 15 * 60 * 1000
        with pytest.raises(BrokerError):
            await client.connect()

    summaries = [r for r in caplog.records if r.__dict__.get("action") == "ibkr_connect_still_failing"]
    assert len(summaries) == 1
    assert summaries[0].__dict__["suppressed_attempts"] == 4


def test_the_ib_async_duplicate_errors_are_dropped_only_while_suppressed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ib_logger = logging.getLogger("ib_async.client")
    caplog.set_level(logging.ERROR, logger="ib_async.client")

    ib_logger.error("API connection failed: ConnectionRefusedError(111, 'Connection refused')")
    ib_logger.error("Make sure API port on TWS/IBG is open")
    CONNECT_LOG_BUDGET.note_failure(ConnectionRefusedError(111, "Connection refused"))
    ib_logger.error("API connection failed: ConnectionRefusedError(111, 'Connection refused')")
    ib_logger.error("Make sure API port on TWS/IBG is open")
    ib_logger.error("API connection failed: TimeoutError()")

    messages = [r.getMessage() for r in caplog.records]
    assert messages.count("Make sure API port on TWS/IBG is open") == 1
    assert "API connection failed: TimeoutError()" in messages
```

**Run / expected failure**
```
cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/ibkr/test_connect_log_budget.py -q
```
→ `ModuleNotFoundError: No module named 'app.broker.ibkr.connect_log_budget'`.

**Minimal implementation**

`app/broker/ibkr/connect_log_budget.py` (new, ~70 lines):
- `SUPPRESSION_WINDOW_MS: int = 900_000` (15 min).
- `class ConnectLogBudget` with `_outage_started_ms`, `_last_reported_ms`, `_suppressed`, `_last_shape: str | None`, `_now_ms`.
  - `note_failure(exc) -> LogVerdict` returning one of `"report_first"`, `"suppress"`, `"report_summary"`. Reports when the outage is new, when `repr(type(exc))+str(getattr(exc,"errno",""))` differs from `_last_shape`, or when `now - _last_reported_ms >= SUPPRESSION_WINDOW_MS`; otherwise increments `_suppressed` and suppresses.
  - `note_success()` — resets everything, returns the outage duration for one INFO.
  - `suppressing` property — True iff the last verdict was `"suppress"`.
  - `reset_for_testing(now_ms=now_ms_utc)`.
- `class _IbAsyncConnectNoiseFilter(logging.Filter)` — `filter()` returns `False` only when `CONNECT_LOG_BUDGET.suppressing` **and** `record.levelno == logging.ERROR` **and** the message is `"Make sure API port on TWS/IBG is open"` or starts with `"API connection failed: ConnectionRefusedError"`. Everything else passes.
- Module scope: `CONNECT_LOG_BUDGET = ConnectLogBudget()` and `logging.getLogger("ib_async.client").addFilter(_IbAsyncConnectNoiseFilter())`.

`app/broker/ibkr/client.py`:
- import `CONNECT_LOG_BUDGET`.
- `connect()` `:415` — demote the per-attempt `[STEP 1/3]` INFO to `logger.debug` when `CONNECT_LOG_BUDGET.suppressing`.
- `connect()` `:443-447` — replace the unconditional `logger.warning` with a branch on `CONNECT_LOG_BUDGET.note_failure(exc)`: `"report_first"` → today's WARNING plus `extra={"action": "ibkr_connect_failed", "unreachable_since_ms": …}`; `"report_summary"` → `logger.warning("IBKR still unreachable …", extra={"action": "ibkr_connect_still_failing", "suppressed_attempts": n, "unreachable_since_ms": …})`; `"suppress"` → `logger.debug(...)`.
- after the successful `break` (`:437`) — `if (ms := CONNECT_LOG_BUDGET.note_success()) is not None: logger.info("IBKR reachable again after %d ms", ms, extra={"action": "ibkr_connect_recovered"})`.

**Run / expected pass**
```
cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/ibkr/ -q
ruff check PythonDataService/app/ PythonDataService/tests/
```

**Commit:** `fix(ibkr): one warning per outage instead of 350 lines per 30 minutes`

---

### Task 3 — name the dependency in the docs (the cheap #2077 answer)

**Files**
- modify `docs/ibkr-integration-authority.md`
- modify `PythonDataService/app/broker/ibkr/config.py`
- modify `docs/runbooks/ibkr-setup-guide.md`
- modify `Frontend/src/assets/docs/ibkr-setup-guide.md`

**Failing test** — extend `PythonDataService/tests/contracts/test_documentation_contract.py`:

```python
def test_the_ibkr_authority_states_that_alpaca_execution_depends_on_this_feed() -> None:
    """#2077/#2080 both misread a live dependency as residue. The doc must say it."""
    authority = (REPOSITORY_ROOT / "docs/ibkr-integration-authority.md").read_text(encoding="utf-8")
    assert "Alpaca" in authority
    assert "IbkrMarketDataFeed" in authority
    assert "IBKR_BROKER_ENABLED" in authority


def test_the_served_ibkr_guide_carries_no_retired_order_capable_guidance() -> None:
    served = (REPOSITORY_ROOT / "Frontend/src/assets/docs/ibkr-setup-guide.md").read_text(encoding="utf-8")
    for retired in FORBIDDEN_CURRENT_GUIDANCE:
        assert retired not in served
```

**Run / expected failure**
```
cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/contracts/test_documentation_contract.py -q
```
→ first test fails on `"IbkrMarketDataFeed" in authority`; second fails on `IBKR_READONLY=false` present at `Frontend/src/assets/docs/ibkr-setup-guide.md:36,73`.

**Minimal implementation**
1. `docs/ibkr-integration-authority.md` — new section after "Product boundary": *"Market data is load-bearing for Alpaca execution."* State that `main.py:735` installs `IbkrMarketDataFeed` as the process-wide `MarketDataFeed`, that Alpaca bots stream decision bars through it (`bot_runtime.py:156`, `bot_trade_strategy.py:584`), that `IBKR_BROKER_ENABLED=false` therefore stops Alpaca deploys with `MARKET_DATA_UNAVAILABLE`, and that `IBKR_LIVE_RUNS_ROOT`/`IBKR_LIVE_BARS_ROOT` are **deliberate legacy names** for Alpaca-lane-local roots.
2. `config.py:105-116` — replace the two field comments with the same statement, each naming the issue (`#2077`) so the next reader does not re-derive it.
3. `docs/runbooks/ibkr-setup-guide.md` — add a "Nightly relogin" step: IB Gateway → Configure → Settings → Lock and Exit → **Auto restart**, and state that with it off the clerks log a connect failure per minute and a running bot dies at the next decision trigger.
4. `Frontend/src/assets/docs/ibkr-setup-guide.md` — delete the `IBKR_READONLY=false` row (`:73`) and line `:36`; replace the "Read-Only API / Disabled for order-capable paper testing" row with "Enabled — IBKR order actuation is retired (#1583); this connection is read-only evidence and market data".

**Run / expected pass**
```
cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/contracts -q
```

**Commit:** `docs(ibkr): say plainly that Alpaca execution rides the IBKR market-data feed`

---

### Task 4 — the rename slice (only if the owner overrides the recommendation)

Scope: `IBKR_LIVE_RUNS_ROOT` → `ALPACA_LANE_LIVE_RUNS_ROOT` **only**. Leave `IBKR_LIVE_BARS_ROOT` and `IBKR_READONLY` alone — facts D.2 and D.3 above.

**Files**
- modify `PythonDataService/app/broker/ibkr/config.py`
- modify `compose.fleet.yaml`, `compose.yaml`
- create `PythonDataService/tests/contracts/test_lane_root_env_aliases.py`
- modify `PythonDataService/scripts/bench_panel_read_latency.py:622`, `PythonDataService/tests/scripts/test_bench_panel_read_latency.py:26-31`
- modify `docs/design/fleet-a2-lane-inventory.md:19-20`, `docs/runbooks/fleet-d-two-clerk-rollout.md:80`, `docs/runbooks/fleet-directory-unavailable.md:107,118`

**Failing test**

```python
"""#2077 — both spellings must resolve to one effective setting, and compose must be pinned."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.broker.ibkr.config import IbkrSettings

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


class _ComposeLoader(yaml.SafeLoader):
    pass


_ComposeLoader.add_multi_constructor(
    "!", lambda loader, suffix, node: loader.construct_mapping(node)
)


@pytest.mark.parametrize("name", ["IBKR_LIVE_RUNS_ROOT", "ALPACA_LANE_LIVE_RUNS_ROOT"])
def test_both_spellings_produce_the_same_effective_root(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("IBKR_LIVE_RUNS_ROOT", raising=False)
    monkeypatch.delenv("ALPACA_LANE_LIVE_RUNS_ROOT", raising=False)
    monkeypatch.setenv(name, "/app/artifacts/alpaca_clerk/live_runs")

    assert IbkrSettings(_env_file=None).live_runs_root == "/app/artifacts/alpaca_clerk/live_runs"


def test_the_new_name_wins_when_both_are_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IBKR_LIVE_RUNS_ROOT", "/app/artifacts/stale")
    monkeypatch.setenv("ALPACA_LANE_LIVE_RUNS_ROOT", "/app/artifacts/alpaca_clerk/live_runs")

    assert IbkrSettings(_env_file=None).live_runs_root == "/app/artifacts/alpaca_clerk/live_runs"


def test_the_legacy_spelling_warns_once(monkeypatch, caplog) -> None:
    monkeypatch.delenv("ALPACA_LANE_LIVE_RUNS_ROOT", raising=False)
    monkeypatch.setenv("IBKR_LIVE_RUNS_ROOT", "/app/artifacts/alpaca_clerk/live_runs")

    from app.broker.ibkr import config as ibkr_config

    ibkr_config.reset_settings_for_testing()
    with caplog.at_level("WARNING", logger="app.broker.ibkr.config"):
        ibkr_config.get_settings()

    assert [r for r in caplog.records if r.__dict__.get("action") == "legacy_env_name"]


@pytest.mark.parametrize("service", ["alpaca-live-clerk", "alpaca-paper-clerk"])
def test_the_fleet_clerks_set_the_new_name_and_no_legacy_one(service: str) -> None:
    compose = yaml.load(
        (REPOSITORY_ROOT / "compose.fleet.yaml").read_text(encoding="utf-8"), Loader=_ComposeLoader
    )
    env = compose["services"][service]["environment"]

    assert env["ALPACA_LANE_LIVE_RUNS_ROOT"] == "/app/artifacts/alpaca_clerk/live_runs"
    assert "IBKR_LIVE_RUNS_ROOT" not in env
    # These two are genuinely IBKR and must survive the rename untouched.
    assert env["IBKR_BROKER_ENABLED"] == "true"
    assert env["IBKR_READONLY"] == "true"
```

**Run / expected failure**
```
cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/contracts/test_lane_root_env_aliases.py -q
```
→ `test_both_spellings…[ALPACA_LANE_LIVE_RUNS_ROOT]` gets the `/app/artifacts/live_runs` default; the compose tests get `KeyError: 'ALPACA_LANE_LIVE_RUNS_ROOT'`.

**Minimal implementation**
1. `config.py:108` — `live_runs_root: str = Field(default="/app/artifacts/live_runs", validation_alias=AliasChoices("ALPACA_LANE_LIVE_RUNS_ROOT", "IBKR_LIVE_RUNS_ROOT"))`. A field-level `validation_alias` overrides `env_prefix`, so both names are spelled in full and precedence follows `AliasChoices` order. Import `AliasChoices` from `pydantic`.
2. `config.py:186-191` — in `get_settings()`, before returning, call a new `_warn_on_legacy_env_names()` that logs one `logger.warning(..., extra={"action": "legacy_env_name", "legacy": "IBKR_LIVE_RUNS_ROOT", "replacement": "ALPACA_LANE_LIVE_RUNS_ROOT"})` when the legacy name is in `os.environ` and the new one is not. Add `logger = logging.getLogger(__name__)` to the module.
3. `compose.fleet.yaml:30` → `ALPACA_LANE_LIVE_RUNS_ROOT: /app/artifacts/alpaca_clerk/live_runs`. `compose.yaml:216` → `- ALPACA_LANE_LIVE_RUNS_ROOT=/app/artifacts/live_runs`.
4. `scripts/bench_panel_read_latency.py:622` and its test — new name.
5. Docs: the four doc lines listed above.

**Run / expected pass**
```
cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/contracts tests/broker/fleet/test_a2_alpaca_lane.py tests/scripts/test_bench_panel_read_latency.py -q
ruff check PythonDataService/app/ PythonDataService/tests/
```

`tests/broker/fleet/test_a2_alpaca_lane.py` is the fleet-boots test: `_fence_satisfying_roots` (`:283-294`) constructs `IbkrSettings(live_runs_root=…)` by field name, so it is alias-agnostic and proves boot still fences correctly.

**Commit:** `refactor(config): name the Alpaca lane root for what it holds, with a legacy alias`

---

## 4. Grouping advice

- **PR A = Tasks 1 + 2** (`fix/ibkr-outage-log-budget`, closes #2080). One coherent story: an expected outage becomes one legible fact instead of 350 lines. Branch off `origin/master`. Task 1 must land before Task 2 because Task 2's WARNING payload carries `unreachable_since_ms`.
- **PR B = Task 3** (`docs/ibkr-market-data-authority`, closes the doc half of #2077). **Branch off `origin/docs/broker-clerk-fleet-authority`, not master** — #2078 rewrites `docs/doc-authority.md:157-162` and `docs/runbooks/ibkr-setup-guide.md:9`; branching off master guarantees a conflict in both files.
- **PR C = Task 4**, only on an explicit owner override, and only after PR A and PR B. It is the one PR that can silently re-home a durable tree.
- **Sequencing against the two open PRs:** #2081 touches `PythonDataService/app/main.py:964-977` and `restart.sh`; PR A touches `auto_reconnect_monitor.py`, `health.py`, `models.py`, `client.py` — **no overlap**, can go in parallel. #2078 touches only docs and Frontend — overlaps PR B only.
- **Deploy safety — every step is unsafe to ship via `./restart.sh` while the Live lane carries a running bot.** `restart.sh:21` runs `podman compose down` and `:56` `podman compose up -d --force-recreate`; both on master and on #2081's branch. That destroys and recreates *every* container including `alpaca-live-clerk`, regardless of whether the change is Python or a single compose env line (compose env is fixed at container create, so there is no hot path). A restart mid-session cancels the bot task; `bot_runner.py:1515-1526` finalises it as `STOPPED` if stop intent exists and `EXITED_UNVERIFIED / CANCELLED_WITHOUT_STOP_INTENT` otherwise. **Rule: Stop every Live bot, confirm `OFF_DUTY`, then restart, then Resume — or restart outside RTH.** Task 1's `models.py` change additionally alters the `/api/broker/health` wire shape, so Backend/Frontend want the same deploy window.
- Per `CLAUDE.md`, invoke `thermo-nuclear-code-quality-review` before the first push of each PR and clear every **major** finding.

---

## 5. Risks the issue text missed

1. **An unattended gateway blackout kills running bots, it does not merely degrade them.** `ibkr_continuity.py:346-365` raises `DECISION_BAR_MISSED` once the run's next decision trigger passes, and `bot_runner.py:1528-1540` finalises the run `FEED_DEATH`. A bot left on-duty overnight with the gateway logged off dies at the first RTH trigger. #2080 frames the blackout as a logging problem; it is an availability problem whose only real fix is the **operator** enabling IB Gateway → Configure → Settings → Lock and Exit → **Auto restart**. Neither issue records that action item. Claude cannot flip it — GUI, credential-adjacent.
2. **The cockpit lies about outage age.** Every 60 s open probe re-stamps `_last_transition_ms` (`auto_reconnect_monitor.py:591-656`), so `last_transition_ms` on `/api/broker/health` reports the age of the last probe. An eight-hour blackout renders as "hard_down, 12 s ago". Task 1 is the fix.
3. **`IBKR_READONLY` is not misnamed and must not be renamed.** Single read site, `client.py:427`, passed to `ib_async.IB.connectAsync(readonly=…)`. #2077 lists it as Alpaca-carrying; it is not.
4. **`IBKR_LIVE_BARS_ROOT` is misplaced, not misnamed** — the aggregator's content is genuinely IBKR `reqRealTimeBars` output (`live_bar_aggregator.py:1-30`); only the directory is lane-local.
5. **The rename's real failure mode is `combined` role, not persistence.** In `clerk_agent` the writable-root fence (`fleet_boot.py:385-412`) refuses boot on a missed override. In `combined` (`compose.yaml`, dev stack) nothing fences, so a skewed image/compose pair silently adopts an empty `/app/artifacts/live_runs`. Alias-with-warning (Task 4 step 1-2) is what prevents this; a hard rename does not.
6. **A live operator UI page serves retired, safety-relevant guidance.** `Frontend/src/assets/docs/ibkr-setup-guide.md:36,73` still instructs operators to set `IBKR_READONLY=false` and disable IBKR's Read-Only API "for order-capable paper testing". IBKR order actuation was retired by #1583. `scripts/check_documentation_contract.py:50` forbids exactly that string — but `_markdown_files` (`:66-67`) only walks `docs/`, so the served copy escapes the contract entirely. Route: `Frontend/src/app/components/docs/ibkr-setup-guide-page.component.ts:25,68`.
7. **Two memory notes are now stale and will mislead the next agent.** `reference_ibkr_broker_enabled_is_load_bearing_for_alpaca.md` says the churn "is the price of working deploys" and calls the gateway permanently gone — the conclusion (never flip the flag) is right, the reasoning is wrong. `ibkr_gateway_nightly_blackout.md` cites `is_ibkr_north_america_reset_window` / `broker_session_events.py` and an actively-appended `connection_events.jsonl`; none exists on master (retired with #1813), so its analysis is no longer reproducible.
8. **Nothing anywhere pins the fleet compose env.** No test asserts `IBKR_BROKER_ENABLED` stays `"true"` on the clerks. A future "IBKR cleanup" PR can flip it and pass CI; the fleet then answers 503 to every deploy with no test failing. Task 4's compose test closes this even if the rename is rejected — worth landing that one assertion in PR A regardless.
9. **The `combined` service in `compose.yaml` still points at the *shared* `/app/artifacts/live_runs`** (`compose.yaml:216`) while both clerks use lane-local volumes. Anyone who runs the legacy-combined profile beside the fleet gets two processes with different roots and one `BotTaskRegistry` identity guard — outside this lane's scope, but it is the next tripwire on this path.
