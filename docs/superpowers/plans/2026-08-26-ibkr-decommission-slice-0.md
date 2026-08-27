# IBKR Decommission Slice 0 (feed seam) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract a broker-neutral data-feed seam in `PythonDataService` so later slices can delete the IBKR account/order/session control plane without breaking the Alpaca Broker V2 live chart/gallery, Alpaca Start/Resume admission, the global Angular health banner, or the retained IBKR options-chain/surface pages.

**Architecture:** Pure extraction and repointing — no new abstractions, no behavior changes, no deletions. Four bar-only types move out of a mixed `models.py` into a bar-only module; a capability service gets renamed (not relocated on disk) to say "market data" instead of "broker"; one artifact-root helper moves from an account-bucket module to an already-retained config module; one function's prose gets rewritten; one new structural test pins the result. Two known residual couplings (order-error buffering, `safety_verdict`) are deliberately deferred to Slices 4 and 3 respectively, because each has a second live consumer outside this slice's scope — removing them now would break a still-registered endpoint. The structural test encodes both as named exceptions.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic v2, pytest.

**Spec:** `docs/superpowers/specs/2026-08-26-ibkr-decommission-slice-0-design.md` — read it before starting; this plan implements it task-by-task and does not repeat its reasoning.

## Global Constraints

- No new abstractions beyond what the spec calls for — no new `ChartBar`-style translation type, no new protocol for the options routes. This is a "simple clean up operation" per explicit operator instruction.
- No file in the account/order/session bucket is deleted or has its behavior changed in this plan. Where a mechanical import-path fix is unavoidable in a retiring-bucket file (e.g. a test file that monkeypatches a symbol being moved), make only that one-line fix — nothing else in that file.
- The structural test (Task 5) must encode exactly two named exceptions and no more: `client.py` → `order_error_stream.OrderErrorEvent` (closes in Slice 4) and `models.py` → `app.broker.safety_verdict.BrokerSafetyVerdict` (closes in Slice 3). Any other account/order/session import from a retained module is a real regression, not a third exception to add.
- Any storage-root or artifact-path change must resolve to the byte-identical directory as before, or it doesn't happen in this slice — durable on-disk state is never silently moved or orphaned (the `#1811` lesson).
- Run before every commit that touches Python: `ruff check PythonDataService/app/ PythonDataService/tests/` (project scope, not per-file).
- Run before push (Task 6, not per-task): full suite `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" ./.venv/bin/python -m pytest tests -q`, both linters, `thermo-nuclear-code-quality-review` skill, OpenAPI regeneration check.
- Follow existing patterns in every file touched. Don't reformat or restyle code you're not changing.
- All commands below assume `cwd = /Users/inkant/learn-ai/PythonDataService` unless stated otherwise. Branch: `decommission/ibkr-feed-seam-1813` (already created off `origin/master`, current HEAD is the two design-doc commits).

---

### Task 1: Extract bar-only types from `models.py` into `bar_models.py`

**Files:**
- Create: `PythonDataService/app/broker/ibkr/bar_models.py`
- Modify: `PythonDataService/app/broker/ibkr/models.py` (remove the four type definitions)
- Modify: `PythonDataService/app/broker/ibkr/bars.py`
- Modify: `PythonDataService/app/schemas/broker_v2_panel.py`
- Modify: `PythonDataService/app/services/bar_persistence.py`
- Modify: `PythonDataService/app/services/live_bar_aggregator.py:45`
- Modify: `PythonDataService/app/services/live_chart_window.py:18`
- Modify: `PythonDataService/app/services/broker_v2_panel/chart_projection_service.py:29`
- Modify: `PythonDataService/app/routers/broker.py` (the `IbkrBarsSnapshot` import)
- Modify tests: `PythonDataService/tests/test_live_bar_aggregator.py`, `tests/broker/v2panel/test_chart_projection.py`, `tests/marketdata/test_feed.py`, `tests/services/test_bar_persistence.py`, `tests/services/test_live_chart_window.py`, `tests/services/test_bar_timestamp_rigor.py`

**Interfaces:**
- Produces: `app.broker.ibkr.bar_models.IbkrMinuteBar`, `.IbkrBarsSnapshot`, `.BarProvenance`, `.BarSessionPhase` — identical shape to the current `app.broker.ibkr.models` versions, same field names and types, same docstrings. Every later task that touches bar types imports from `bar_models`, not `models`.

- [ ] **Step 1: Re-verify the importer list before editing**

Run, from `PythonDataService/`:

```bash
for t in IbkrMinuteBar IbkrBarsSnapshot BarProvenance BarSessionPhase; do
  echo "=== $t ==="
  grep -rln "\b$t\b" app/ tests/ | grep -v "app/broker/ibkr/models.py"
done
```

Confirm the result matches the file list above. If it doesn't (the tree may have moved since this plan was written), update the file list in this task before proceeding — do not silently skip a newly-found importer.

- [ ] **Step 2: Read the exact current definitions**

```bash
sed -n '459,503p' app/broker/ibkr/models.py
```

Confirm the four definitions still match what's below (field-for-field) before copying them — if a field was added/changed since this plan was written, carry the *current* definition forward, not this plan's snapshot.

- [ ] **Step 3: Create `bar_models.py`**

```python
"""Broker-neutral bar and bar-snapshot wire models for the IBKR feed.

Split out of ``app/broker/ibkr/models.py`` (IBKR decommission Slice 0,
issue #1813) so the live-chart/gallery/bar-aggregator path can depend
on bar types without importing account/order/session models from the
same file. See
``docs/superpowers/specs/2026-08-26-ibkr-decommission-slice-0-design.md``.

All timestamps are ``int64`` ms UTC per the project's numerical-rigor
rules.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

BarProvenance = Literal["ibkr_realtime", "ibkr_historical", "polygon_historical", "mixed"]
BarSessionPhase = Literal["PRE", "RTH", "POST", "OVERNIGHT", "CLOSED", "UNKNOWN"]


class IbkrMinuteBar(BaseModel):
    """One closed 1-minute TRADES bar from IBKR real-time bars.

    IBKR delivers 5-second bars via ``reqRealTimeBars``. The broker
    boundary aggregates those into closed 1-minute bars and stores all
    boundary timestamps as ``int64`` ms UTC.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    start_ms: int = Field(..., description="UTC milliseconds since epoch, inclusive.")
    end_ms: int = Field(..., description="UTC milliseconds since epoch, exclusive.")
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    fetched_at_ms: int
    source: Literal["ibkr", "polygon", "mixed"] = "ibkr"
    provenance: BarProvenance = "ibkr_realtime"
    venue: str | None = None
    session_phase: BarSessionPhase = "UNKNOWN"
    use_rth: bool | None = None


class IbkrBarsSnapshot(BaseModel):
    """A snapshot of the live 1-min OHLCV ring buffer for one symbol.

    ``status`` reports the aggregator's subscription health so the UI can
    show "Subscribing…" / "Streaming" / "Error: …" instead of an
    inscrutable empty chart.
    """

    symbol: str
    status: Literal["idle", "subscribing", "streaming", "errored", "resubscribing"]
    last_error: str | None = None
    last_bar_ms: int | None = None
    bars: list[IbkrMinuteBar] = Field(default_factory=list)
```

- [ ] **Step 4: Remove the four definitions from `models.py`**

Delete the block from `BarProvenance = Literal[...]` through the end of the `IbkrBarsSnapshot` class (the block you just verified in Step 2). Then grep within `models.py` itself for the four names to confirm nothing else in the file references them:

```bash
grep -n "IbkrMinuteBar\|IbkrBarsSnapshot\|BarProvenance\|BarSessionPhase" app/broker/ibkr/models.py
```

Expected: no matches. If there is a match (e.g. another class field typed as one of these), add `from app.broker.ibkr.bar_models import <name>` to `models.py`'s own imports rather than leaving a broken reference.

- [ ] **Step 5: Repoint the three verified call sites exactly**

These three imports were read in full during planning — apply these exact diffs:

`app/services/live_bar_aggregator.py:45`, change:
```python
from app.broker.ibkr.models import IbkrMinuteBar
```
to:
```python
from app.broker.ibkr.bar_models import IbkrMinuteBar
```

`app/services/live_chart_window.py:18`, change:
```python
from app.broker.ibkr.models import BarProvenance, BarSessionPhase, IbkrMinuteBar
```
to:
```python
from app.broker.ibkr.bar_models import BarProvenance, BarSessionPhase, IbkrMinuteBar
```

`app/services/broker_v2_panel/chart_projection_service.py:29`, change:
```python
from app.broker.ibkr.models import IbkrMinuteBar
```
to:
```python
from app.broker.ibkr.bar_models import IbkrMinuteBar
```

- [ ] **Step 6: Repoint the remaining production importers**

For the rest of the Files list above (`bars.py`, `schemas/broker_v2_panel.py`, `bar_persistence.py`, `routers/broker.py`) — whose exact current import line wasn't captured verbatim during planning — open each file, find the import statement pulling `IbkrMinuteBar`/`IbkrBarsSnapshot`/`BarProvenance`/`BarSessionPhase` from `app.broker.ibkr.models`, and change the source module to `app.broker.ibkr.bar_models`. If a file imports both bar types and other (non-bar) names from `models.py` in one statement, split it into two import statements — one from `bar_models`, one still from `models` for the remaining names. Do not change anything else in these files.

- [ ] **Step 7: Repoint every test importer**

Same grep-and-repoint operation as Step 6, applied to the six test files listed above.

- [ ] **Step 8: Run the touched tests**

```bash
./.venv/bin/python -m pytest tests/test_live_bar_aggregator.py tests/broker/v2panel/test_chart_projection.py tests/marketdata/test_feed.py tests/services/test_bar_persistence.py tests/services/test_live_chart_window.py tests/services/test_bar_timestamp_rigor.py -v
```

Expected: all pass, unchanged from before this task (this is a pure import-path move — any new failure or new pass means something beyond an import path changed, and that's a bug to find before continuing).

- [ ] **Step 9: Lint the touched files**

```bash
ruff check app/broker/ibkr/bar_models.py app/broker/ibkr/models.py app/broker/ibkr/bars.py app/schemas/broker_v2_panel.py app/services/bar_persistence.py app/services/live_bar_aggregator.py app/services/live_chart_window.py app/services/broker_v2_panel/chart_projection_service.py app/routers/broker.py
```

Expected: zero warnings, including no unused-import warnings on `models.py` (confirms Step 4 didn't leave a dangling import).

- [ ] **Step 10: Commit**

```bash
git add app/broker/ibkr/bar_models.py app/broker/ibkr/models.py app/broker/ibkr/bars.py app/schemas/broker_v2_panel.py app/services/bar_persistence.py app/services/live_bar_aggregator.py app/services/live_chart_window.py app/services/broker_v2_panel/chart_projection_service.py app/routers/broker.py tests/test_live_bar_aggregator.py tests/broker/v2panel/test_chart_projection.py tests/marketdata/test_feed.py tests/services/test_bar_persistence.py tests/services/test_live_chart_window.py tests/services/test_bar_timestamp_rigor.py
git commit -m "refactor(ibkr): extract bar-only types out of the mixed models.py

IbkrMinuteBar, IbkrBarsSnapshot, BarProvenance, and BarSessionPhase move
to a new bar_models.py so the live-chart/gallery/bar-aggregator path
stops depending on a file that also holds account/order/session types.
Pure extraction — no field or behavior changes.

Part of the IBKR control-plane decommission (#1813), Slice 0."
```

---

### Task 2: Rehome the shared artifact-root helper off the account-bucket `account_truth_refresh.py`

**Files:**
- Modify: `PythonDataService/app/broker/ibkr/config.py` (add `market_data_artifacts_root()`)
- Modify: `PythonDataService/app/services/broker_v2_panel/panel_data_source.py:209-215`
- Modify: `PythonDataService/app/services/broker_v2_panel/sqlite_roster_status.py:87,141`
- Modify: `PythonDataService/tests/broker/v2panel/test_sqlite_roster_source.py:156,217,291,340`

**Interfaces:**
- Produces: `app.broker.ibkr.config.market_data_artifacts_root() -> Path` — resolves to `Path(get_settings().live_runs_root).parent`, the identical directory `account_truth_artifacts_root()` in `account_truth_refresh.py` already computes. `account_truth_refresh.py` itself, and its other five callers, are untouched and keep using the old function — this task does not delete it.

- [ ] **Step 1: Add the helper next to `get_settings()`**

Open `app/broker/ibkr/config.py`, and immediately after the `get_settings()` function (currently ending around line 211+), add:

```python
def market_data_artifacts_root() -> Path:
    """Return the artifacts root shared by market-data capability
    snapshots and bot lifecycle/binding evidence readers.

    Identical to ``account_truth_refresh.account_truth_artifacts_root()``
    — same underlying setting, same resolved directory — but lives in
    this already-retained feed config module so its callers don't
    import an account-bucket module for a path lookup. See
    ``docs/superpowers/specs/2026-08-26-ibkr-decommission-slice-0-design.md``.
    """
    return Path(get_settings().live_runs_root).parent
```

Add `from pathlib import Path` to the top-of-file imports if `config.py` doesn't already import `Path` (check first — `IbkrSettings` fields are plain `str`/`int`/`bool`, so `Path` may not yet be imported there).

- [ ] **Step 2: Repoint `panel_data_source.py`**

Read the current import and call site:

```bash
grep -n "account_truth_artifacts_root\|^from app" app/services/broker_v2_panel/panel_data_source.py | head -20
```

Change the import of `account_truth_artifacts_root` from `app.services.account_truth_refresh` to `from app.broker.ibkr.config import market_data_artifacts_root`, and change the one call site (`_run_evidence_repository`, currently `return live_state_binding_repository(account_truth_artifacts_root())`) to call `market_data_artifacts_root()` instead. Update the function's docstring line that names `account_truth_artifacts_root()` to name the new function instead.

- [ ] **Step 3: Repoint `sqlite_roster_status.py`**

Same operation: change the import to `from app.broker.ibkr.config import market_data_artifacts_root`, and update both call sites (the one building `stable_bot_lifecycle_state_path(...)` and the one building `live_state_binding_repository(...)`) to call `market_data_artifacts_root()`.

- [ ] **Step 4: Fix the monkeypatch targets in the test file**

`test_sqlite_roster_source.py` patches the symbol by name in the module's own namespace (`monkeypatch.setattr(sqlite_roster_status, "account_truth_artifacts_root", lambda: tmp_path)`) at four call sites (lines 156, 217, 291, 340 as of this plan — re-grep to confirm line numbers haven't shifted). Change each to `monkeypatch.setattr(sqlite_roster_status, "market_data_artifacts_root", lambda: tmp_path)`, matching the renamed import in `sqlite_roster_status.py`.

- [ ] **Step 5: Run the affected tests**

```bash
./.venv/bin/python -m pytest tests/broker/v2panel/test_sqlite_roster_source.py -v
```

Expected: all pass. (There is no dedicated `panel_data_source` test file — its coverage is indirect, through the panel router tests exercised in Task 6's full run.)

- [ ] **Step 6: Lint and commit**

```bash
ruff check app/broker/ibkr/config.py app/services/broker_v2_panel/panel_data_source.py app/services/broker_v2_panel/sqlite_roster_status.py
git add app/broker/ibkr/config.py app/services/broker_v2_panel/panel_data_source.py app/services/broker_v2_panel/sqlite_roster_status.py tests/broker/v2panel/test_sqlite_roster_source.py
git commit -m "refactor(ibkr): move the shared artifacts-root helper off account_truth_refresh

panel_data_source.py and sqlite_roster_status.py no longer import an
account-bucket module for a path lookup. market_data_artifacts_root()
in the already-retained config.py resolves to the identical directory
account_truth_refresh.account_truth_artifacts_root() computes today —
no storage location changes.

Part of #1813 Slice 0."
```

---

### Task 3: Rename `broker_capability_service.py` to say "market data," not "broker"

**Files:**
- Modify (rename): `PythonDataService/app/services/broker_capability_service.py` → `PythonDataService/app/services/market_data_capability_service.py`
- Modify: `PythonDataService/app/routers/broker_capability.py`
- Modify: `PythonDataService/app/broker/alpaca/clerk/sqlite/runtime.py:107,718`
- Modify: `PythonDataService/app/services/bot_trade_strategy.py:50`
- Modify: `PythonDataService/app/services/bot_runner.py:154,294,309`
- Modify: `PythonDataService/app/services/broker_v2_panel/panel_data_source.py`
- Modify: `PythonDataService/app/services/broker_v2_panel/market_pulse.py`
- Modify (rename): `PythonDataService/tests/services/test_broker_capability_service.py` → `tests/services/test_market_data_capability_service.py`
- Modify: `PythonDataService/tests/routers/test_broker_capability.py`
- Modify: `PythonDataService/tests/broker/ibkr/test_capability.py`
- Modify: `PythonDataService/tests/services/test_bot_start_admission.py`
- Modify: `PythonDataService/tests/services/test_market_liveness.py`

**Interfaces:**
- Produces: `app.services.market_data_capability_service.MarketDataCapabilityService` (renamed from `BrokerCapabilityService`), `.get_market_data_capability_service()` (renamed from `get_broker_capability_service()`), `.extended_phase_proven_at_ms(...)` (name unchanged — already broker-neutral). **The storage root computation is byte-identical to today**: `Path(get_settings().live_runs_root) / "_broker" / "session_capabilities"` — do not change this path. (`_broker` stays in the path literal; renaming that path segment would move the directory and orphan existing snapshots, which this task explicitly does not do — see Global Constraints.)

- [ ] **Step 1: Re-verify the importer list**

```bash
grep -rln "broker_capability_service" app/ tests/
```

Confirm it matches the Files list above.

- [ ] **Step 2: Rename the source file and its symbols**

```bash
git mv app/services/broker_capability_service.py app/services/market_data_capability_service.py
```

Inside the renamed file, change:
- Module docstring/comments referring to "broker capability" → "market-data capability" (light touch — don't rewrite unrelated docstring content).
- `class BrokerCapabilityService:` → `class MarketDataCapabilityService:`
- `_SERVICE = BrokerCapabilityService()` → `_SERVICE = MarketDataCapabilityService()`
- `def get_broker_capability_service() -> BrokerCapabilityService:` → `def get_market_data_capability_service() -> MarketDataCapabilityService:`
- Leave `extended_phase_proven_at_ms`, `SessionDataCapability`, `probe_session_data_capability`, and every method name (`probe`, `read_latest`, `read_latest_for`, `persist`, `_read_snapshot`, `_safe_snapshot_dir`) unchanged — only the class/module-level singleton/getter names change.
- **Leave `self._root = root or Path(settings.live_runs_root) / "_broker" / "session_capabilities"` exactly as it is.**

- [ ] **Step 3: Repoint every production importer**

For each of `routers/broker_capability.py`, `broker/alpaca/clerk/sqlite/runtime.py`, `services/bot_trade_strategy.py`, `services/bot_runner.py`, `services/broker_v2_panel/panel_data_source.py`, `services/broker_v2_panel/market_pulse.py`: change `from app.services.broker_capability_service import ...` to `from app.services.market_data_capability_service import ...`, and rename any imported `BrokerCapabilityService`/`get_broker_capability_service` reference to `MarketDataCapabilityService`/`get_market_data_capability_service` at both the import and every call site in that file.

- [ ] **Step 4: Rename and repoint the dedicated unit test file**

```bash
git mv tests/services/test_broker_capability_service.py tests/services/test_market_data_capability_service.py
```

Update its imports and every `BrokerCapabilityService`/`get_broker_capability_service` reference the same way as Step 3.

- [ ] **Step 5: Repoint the remaining test importers**

For `tests/routers/test_broker_capability.py`, `tests/broker/ibkr/test_capability.py`, `tests/services/test_bot_start_admission.py`, `tests/services/test_market_liveness.py`: same import-path and symbol-name update as Step 3. `test_broker_capability.py` has direct references to fix at its import block and at `app.dependency_overrides[get_broker_capability_service] = ...` plus three `BrokerCapabilityService(root=tmp_path)` constructions — rename all of them.

- [ ] **Step 6: Run the affected tests**

```bash
./.venv/bin/python -m pytest tests/services/test_market_data_capability_service.py tests/routers/test_broker_capability.py tests/broker/ibkr/test_capability.py tests/services/test_bot_start_admission.py tests/services/test_market_liveness.py -v
```

Expected: all pass, same pass/fail set as before the rename.

- [ ] **Step 7: Lint and commit**

```bash
ruff check app/services/market_data_capability_service.py app/routers/broker_capability.py app/broker/alpaca/clerk/sqlite/runtime.py app/services/bot_trade_strategy.py app/services/bot_runner.py app/services/broker_v2_panel/panel_data_source.py app/services/broker_v2_panel/market_pulse.py
git add -A app/services/market_data_capability_service.py app/services/broker_capability_service.py app/routers/broker_capability.py app/broker/alpaca/clerk/sqlite/runtime.py app/services/bot_trade_strategy.py app/services/bot_runner.py app/services/broker_v2_panel/panel_data_source.py app/services/broker_v2_panel/market_pulse.py tests/services/test_market_data_capability_service.py tests/services/test_broker_capability_service.py tests/routers/test_broker_capability.py tests/broker/ibkr/test_capability.py tests/services/test_bot_start_admission.py tests/services/test_market_liveness.py
git commit -m "refactor(ibkr): rename broker_capability_service to market_data_capability_service

This is a market-data entitlement (which symbol/account can stream
extended-hours data), not broker control. Pure rename — class, module,
and getter names only. The storage root's resolved directory is
unchanged; renaming it would silently orphan existing capability
snapshots on disk.

Part of #1813 Slice 0."
```

---

### Task 4: Rewrite the connection-health condition prose

**Files:**
- Modify: `PythonDataService/app/broker/ibkr/health.py:149-244` (`_broker_health_condition`)
- Modify: `PythonDataService/tests/broker/ibkr/test_health.py`

**Interfaces:**
- Consumes: nothing new — `_broker_health_condition(health: IbkrConnectionHealth, *, operator_disconnected: bool) -> BrokerHealthCondition` keeps its exact signature and every `code`/`severity` value. Only the `title`/`summary`/`remediation` **string content** changes.
- Produces: the same function, same signature, same `code` values (`DATA_PLANE_BROKER_CONNECTED`, `_SOFT_LOST`, `_SUBSCRIPTIONS_STALE`, `_DATA_FARM_DEGRADED`, `_RECONNECTING`, `_RECOVERING`, `_HARD_DOWN`, `_DISABLED`, `_DISCONNECTED`) — later slices and the frontend key off `connection_state` and `code`, not prose, so nothing downstream needs to change.

- [ ] **Step 1: Read the current function in full**

```bash
sed -n '149,244p' app/broker/ibkr/health.py
```

Confirm it still matches the text below before editing — if wording drifted since this plan was written, edit the *current* text with the same intent (drop account/order/reconciliation language, keep everything else), not this plan's snapshot verbatim.

- [ ] **Step 2: Rewrite the account-flavored branches**

Replace the `"connected"` branch's summary:

```python
        return BrokerHealthCondition(
            code="DATA_PLANE_BROKER_CONNECTED",
            severity="ok",
            title=f"Data-plane {account_kind} session connected",
            summary=(
                f"The FastAPI data-plane IBKR client is connected to {account}. "
                "Market data can stream for this session."
            ),
        )
```

Replace the `"hard_down"` branch's summary (drop the "Account positions... reconciliation evidence" sentence):

```python
        return BrokerHealthCondition(
            code="DATA_PLANE_BROKER_HARD_DOWN",
            severity="critical",
            title="Data-plane broker session down",
            summary=(
                "IB Gateway/TWS may be logged in, but the FastAPI data-plane IBKR client is not connected. "
                "Market data cannot stream and reconnect status cannot refresh. "
                "The monitor keeps retrying on a slow cadence and reconnects on its own once the gateway accepts connections."
            ),
            remediation=(
                "Reconnection is automatic and retries indefinitely; no operator click is required. "
                "If this persists, confirm IB Gateway is running, logged in, and has API access enabled."
            ),
        )
```

Replace the `"reconnecting"` branch's remediation ("trusting refreshed account evidence" → feed-only language):

```python
            remediation="Wait for reconnect to complete before trusting streamed market data.",
```

Replace the `"recovering"` branch's remediation ("submitting or reconciling" is order/account language):

```python
            remediation="Wait for recovery to complete before relying on streamed market data.",
```

Replace the default (`"disconnected"`) branch's summary ("account evidence cannot refresh" → feed language):

```python
            summary=(
                "The FastAPI data-plane IBKR client is disconnected by operator request."
                if operator_disconnected
                else (
                    "The FastAPI data-plane IBKR client is disconnected. IB Gateway/TWS may still be logged in, "
                    "but market data cannot stream until this app session connects."
                )
            ),
```

Leave every other branch (`soft_lost`, `subscriptions_stale`, `degraded_data_farm`, `disabled`) unchanged — their current prose is already connection/feed-scoped, not account-flavored.

- [ ] **Step 3: Update `test_health.py`'s assertions**

```bash
grep -n "account-level broker evidence\|Account positions\|trusting refreshed account evidence\|submitting or reconciling\|account evidence cannot refresh" tests/broker/ibkr/test_health.py
```

For each match, update the expected string in the assertion to the new wording from Step 2. Do not change any assertion on `code`, `severity`, or `connection_state` — those are unchanged.

- [ ] **Step 4: Run the test**

```bash
./.venv/bin/python -m pytest tests/broker/ibkr/test_health.py -v
```

Expected: all pass.

- [ ] **Step 5: Lint and commit**

```bash
ruff check app/broker/ibkr/health.py
git add app/broker/ibkr/health.py tests/broker/ibkr/test_health.py
git commit -m "refactor(ibkr): rewrite connection-health condition prose to drop account-evidence language

_broker_health_condition described connection states in terms of
account-level broker evidence, positions, and reconciliation —
control-plane framing on a feed-only payload. Rewords to describe
connection/feed state only. No code/severity/signature change; the
safety_verdict field itself is untouched (deferred to Slice 3, see the
design doc — it has a second live caller in broker_session_mirror.py
that hasn't retired yet).

Part of #1813 Slice 0."
```

---

### Task 5: Add the structural import-boundary test

**Files:**
- Create: `PythonDataService/tests/structural/test_ibkr_feed_boundary.py` (create `tests/structural/` and its `__init__.py` if the directory doesn't already exist — check first)

**Interfaces:**
- Consumes: nothing from earlier tasks by name — this test walks real module source files by path, so it automatically reflects whatever Tasks 1–4 left behind.
- Produces: a regression guard every later slice (1 through 6) reads and tightens. When Slice 3 removes `safety_verdict`, delete that exception line from this test. When Slice 4 removes the order-error buffer, delete that one too. When both are gone, the exception list is empty and the test proves full purity.

- [ ] **Step 1: Check for an existing structural test directory**

```bash
ls tests/structural/ 2>/dev/null || echo "does not exist yet"
```

If it doesn't exist, this task creates it (with an empty `__init__.py` if the test suite's collection convention requires one — check `tests/broker/__init__.py` or a sibling directory for the convention already in use in this repo before deciding).

- [ ] **Step 2: Write the test**

```python
"""Structural regression guard for the IBKR feed/control-plane boundary.

Issue #1813 (IBKR control-plane decommission), Slice 0. Walks the
retained feed-side modules' import statements and asserts none of them
reach into the account/order/session bucket, except two named,
temporary exceptions — each with a second live consumer outside this
slice's scope, tracked to close in a later slice. See
``docs/superpowers/specs/2026-08-26-ibkr-decommission-slice-0-design.md``.

This is deliberately a source-level import scan (``ast``), not a
runtime ``sys.modules`` inspection — it catches an import statement
even in a code path that isn't exercised by the rest of the test suite.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[2] / "app"

# Modules whose entire import graph must stay clear of the account/order/
# session bucket (module dotted-path prefixes below), except the named
# exceptions in _ALLOWED_EXCEPTIONS.
RETAINED_FEED_MODULES = [
    "app.marketdata.feed",
    "app.marketdata.ibkr_feed",
    "app.broker.ibkr.bar_models",
    "app.broker.ibkr.bars",
    "app.broker.ibkr.client",
    "app.broker.ibkr.config",
    "app.broker.ibkr.event_codes",
    "app.broker.ibkr.health",
    "app.broker.ibkr.keepalive",
    "app.broker.ibkr.models",
    "app.broker.ibkr.recovery_state_machine",
    "app.broker.ibkr.auto_reconnect_monitor",
    "app.broker.ibkr.contracts",
    "app.broker.ibkr.market_data",
    "app.broker.ibkr.surface",
    "app.broker.ibkr.symbol_search",
    "app.services.market_data_capability_service",
]

# Dotted-path prefixes considered "account/order/session bucket" — a
# retained module importing anything under these prefixes is a real
# regression unless explicitly allow-listed below.
BANNED_PREFIXES = (
    "app.broker.ibkr.account",
    "app.broker.ibkr.account_recovery",
    "app.broker.ibkr.account_truth",
    "app.broker.ibkr.account_truth_freshness",
    "app.broker.ibkr.order_history",
    "app.broker.ibkr.order_previews",
    "app.broker.ibkr.orders",
    "app.broker.ibkr.pnl",
    "app.broker.ibkr.order_error_stream",
    "app.broker.ibkr.order_evidence",
    "app.broker.ibkr.order_projection",
    "app.broker.safety_verdict",
    "app.services.account_truth_refresh",
    "app.services.account_reconciliation",
    "app.services.account_safety_access",
    "app.services.account_safety_snapshot",
    "app.services.account_truth_snapshot",
    "app.services.account_event_journal",
    "app.services.broker_activity_publisher",
    "app.services.broker_activity_reconciler",
    "app.services.broker_activity_reconstruction",
    "app.services.broker_session_history",
    "app.services.broker_session_mirror",
    "app.services.broker_session_reconciler",
    "app.services.journal_recovery",
    "app.services.host_capability",
    "app.services.activity_evidence_matching",
    "app.services.bot_event_rejection_bridge",
)

# (importing_module, banned_import) pairs allowed to remain, each with
# the slice that closes it. Remove the tuple when that slice lands.
_ALLOWED_EXCEPTIONS = {
    ("app.broker.ibkr.client", "app.broker.ibkr.order_error_stream"),  # closes in Slice 4
    ("app.broker.ibkr.models", "app.broker.safety_verdict"),  # closes in Slice 3
}


def _module_path(dotted: str) -> Path:
    return APP_ROOT.joinpath(*dotted.split(".")[1:]).with_suffix(".py")


def _imported_modules(source_path: Path) -> set[str]:
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


@pytest.mark.parametrize("dotted_module", RETAINED_FEED_MODULES)
def test_retained_feed_module_has_no_unlisted_account_order_session_import(dotted_module: str) -> None:
    source_path = _module_path(dotted_module)
    assert source_path.exists(), f"{dotted_module} does not resolve to {source_path}"
    imported = _imported_modules(source_path)
    violations = []
    for imported_module in imported:
        if not imported_module.startswith(BANNED_PREFIXES):
            continue
        if (dotted_module, imported_module) in _ALLOWED_EXCEPTIONS:
            continue
        violations.append(imported_module)
    assert not violations, (
        f"{dotted_module} imports account/order/session-bucket module(s) {violations} "
        "with no tracked exception — see _ALLOWED_EXCEPTIONS in this test."
    )


def test_no_stale_allowed_exceptions() -> None:
    """Every entry in _ALLOWED_EXCEPTIONS must reflect a real, current import.

    Prevents the exception list from silently outliving the code it was
    written for — if Slice 3 or 4 removes the import another way, this
    fails loudly instead of leaving a permissive dead entry.
    """
    for dotted_module, banned_import in _ALLOWED_EXCEPTIONS:
        source_path = _module_path(dotted_module)
        imported = _imported_modules(source_path)
        assert banned_import in imported, (
            f"_ALLOWED_EXCEPTIONS names ({dotted_module!r}, {banned_import!r}) but {dotted_module} "
            "no longer imports it — delete this stale exception."
        )
```

- [ ] **Step 3: Run it**

```bash
./.venv/bin/python -m pytest tests/structural/test_ibkr_feed_boundary.py -v
```

Expected: all parametrized cases pass, including the two exception cases resolving correctly. **If a case fails on a module you didn't expect to be impure**, that's real signal — either Task 1–4 missed something, or `RETAINED_FEED_MODULES`/`BANNED_PREFIXES` needs a correction. Do not add a third exception to make it pass without understanding why; investigate the actual import first.

- [ ] **Step 4: Lint and commit**

```bash
ruff check tests/structural/test_ibkr_feed_boundary.py
git add tests/structural/
git commit -m "test(ibkr): add the feed/control-plane import-boundary regression guard

Walks the retained feed-side modules' imports and fails on any
account/order/session-bucket dependency outside two named, tracked
exceptions (order-error buffering, closing Slice 4; safety_verdict,
closing Slice 3). This is the executable proof of Slice 0's acceptance
criterion, and the checklist Slices 3/4 use to confirm they actually
closed the gap.

Part of #1813 Slice 0."
```

---

### Task 6: Full verification, thermo review, and PR

**Files:** none (verification only).

- [ ] **Step 1: Confirm the options-data routes already comply**

```bash
./.venv/bin/python -m pytest tests/structural/test_ibkr_feed_boundary.py -k "contracts or market_data or surface or symbol_search" -v
```

Expected: pass (this is already covered by Task 5's parametrization — this step is a targeted spot-check before the full run, not new test code).

- [ ] **Step 2: Run the full pytest suite**

```bash
cd /Users/inkant/learn-ai/PythonDataService && DATA_PLANE_CONTROL_SECRET="" ./.venv/bin/python -m pytest tests -q
```

If the only failure is the known flaky LEAN e2e (`Benchmark and performance series has N misaligned values`), re-run that single test in isolation to confirm it's the flake, not a regression, before treating the suite as green. Any other failure is this branch's to fix before continuing.

- [ ] **Step 3: Project-scope lint**

```bash
ruff check app/ tests/
```

Expected: zero warnings.

- [ ] **Step 4: Frontend — confirm no change needed**

```bash
cd /Users/inkant/learn-ai && grep -n "safety_verdict" Frontend/src/app/api/broker.types.ts Frontend/src/app/api/broker-models.ts Frontend/src/app/services/broker-health.service.ts Frontend/src/app/shell/broker-banner.component.ts
npx eslint Frontend/src/ --max-warnings 0
```

Expected: `safety_verdict` still present in the generated types (untouched this slice, per the design doc), and eslint clean. No Frontend files should have been modified by this plan — if `git status` shows a Frontend change at this point, that's a scope leak to investigate.

- [ ] **Step 5: OpenAPI contract check**

Run whatever this repo's OpenAPI regeneration script is (check `PythonDataService/scripts/` for `export_openapi_contract.py` or similarly named) and diff the result against `contracts/openapi/python-data-service.openapi.json`. No field/schema/route change is expected in this slice (the `safety_verdict` field survives on `IbkrConnectionHealth` until Slice 3) — if the diff is non-empty, read it before deciding whether to commit the regenerated contract or investigate an unintended change.

- [ ] **Step 6: Invoke the thermo-nuclear code quality review**

Per this repo's `CLAUDE.md` hard rule, invoke the `thermo-nuclear-code-quality-review` skill on this branch's diff before the first push that opens the PR. Address every **major** finding in-branch before pushing; minor findings are optional.

- [ ] **Step 7: Push and open the PR**

```bash
git push -u origin decommission/ibkr-feed-seam-1813
```

Open a PR against `master` (branch-protected — never push directly to it). PR description should include: the scope (Slice 0 of #1813 — establishes the feed seam, no deletions), the two deliberately deferred items with their tracking slices (safety_verdict → Slice 3, order-error buffer → Slice 4), and a pointer to both spec-doc commits for full reasoning. Link issue #1813.

- [ ] **Step 8: Confirm no pre-existing-failure masking**

If Task 6 Step 2 surfaced any pre-existing failure not caused by this branch (beyond the known LEAN flake), it must be named explicitly in the PR description per this repo's pre-push test-suite hygiene rule — not silently absorbed into "tests pass."
