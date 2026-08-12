# Bot Gallery (live 20-bot wall) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a bot-gallery page that shows every running bot for an Alpaca account as a live, tick-by-tick candlestick tile (up to 20/page), in a drag-reorder + resize dock, fed by one new aggregated SSE.

**Architecture:** A new Python `GalleryHub` composes the running-bot roster + per-**symbol** live bars (from the shared `LIVE_BAR_AGGREGATOR`) + per-bot fill markers/stats into a versioned, latest-wins document, streamed over one SSE. The Angular side opens a single `EventSource`, keeps bars keyed by symbol (shared), and each thin `BotTileComponent` renders its own lightweight-charts instance, updated **imperatively outside change detection**. The dock uses Angular CDK drag-drop + a custom resize handle over CSS-grid spans.

**Tech Stack:** Python 3.11 / FastAPI / Pydantic v2 (backend); Angular 22 (signals, zoneless, OnPush, standalone) / lightweight-charts v5 / @angular/cdk drag-drop (frontend). Spec: `docs/superpowers/specs/2026-08-11-bot-gallery-design.md`.

## Global Constraints

- **Timestamps are `int64 ms UTC`** on the wire, at rest, and in every DTO field named `*_ms`/`*_at`/`ts`. No ISO strings, no `DateTime`, no naive datetimes. (`.claude/rules/temporal-rigor.md`)
- **Bar resolution for the wall is `1m`.** OHLC values travel as **strings** (exact decimals), matching the existing `ChartBar`.
- **Market data = IBKR** (via `LIVE_BAR_AGGREGATOR`, one subscription per symbol); **execution/account = Alpaca**; history = Polygon (not on the wall).
- **No new runtime dependency.** `@angular/cdk@^22` and `lightweight-charts@^5.1` are already installed.
- **Python:** type hints on every signature; `from __future__ import annotations`; `async def` for I/O; no `print()`; explicit exceptions. Lint project-scope: `ruff check PythonDataService/app/ PythonDataService/tests/`.
- **Angular:** standalone (no `standalone: true`), `ChangeDetectionStrategy.OnPush`, `input()`/`output()`/`inject()`, native control flow, `[class.x]` not `ngClass`, `@for` with `track`. No `console.log`. Lint: `npx eslint Frontend/src/ --max-warnings 0`.
- **Backend-authored code-like tokens** rendered in the UI (reason codes, action ids) go through the shared `receiptLabel` pipe; opaque tokens (sids, order refs) render verbatim.
- **New FastAPI endpoints ⇒ regenerate the committed OpenAPI contract** (`PythonDataService/export_openapi_contract.py`) — a CI gate plain pytest won't catch.
- **Before the first PR push:** run the `thermo-nuclear-code-quality-review` skill and address every major finding; run project-scope lint + the relevant test suites.

---

## File Structure

**Backend (PythonDataService)**
- Create `app/schemas/broker_v2_gallery.py` — `GalleryBotView`, `GalleryBotDelta`, `GallerySymbolBars`, `GalleryLiveSnapshot`, `GalleryLiveUpdate`.
- Create `app/services/broker_v2_panel/gallery_hub.py` — `GalleryHub` (compose snapshot + deltas; ref-counted per account).
- Create `app/routers/broker_v2_gallery.py` — `GET …/gallery/snapshot`, `GET …/gallery/stream`.
- Modify `app/main.py` — `include_router(broker_v2_gallery.router)`.
- Tests: `tests/services/test_gallery_hub.py`, `tests/routers/test_broker_v2_gallery.py`.

**Frontend (Frontend/src/app/components/broker/v2-panel/gallery/)**
- Create `lib/gallery-live-store.service.ts` — one `EventSource` → signals.
- Create `lib/gallery.types.ts` — TS mirrors of the backend DTOs (snake_case).
- Create `lib/gallery-layout.ts` — pure layout model + localStorage persistence + auto-division.
- Create `bot-tile/bot-tile.component.{ts,html,scss}` — thin lightweight-charts tile.
- Create `bot-gallery-dock/bot-gallery-dock.component.{ts,html,scss}` — CDK dock + resize + pagination.
- Create `bot-gallery-page/bot-gallery-page.component.{ts,html,scss}` — route host + toolbar + states.
- Modify `app/app.routes.ts` — add the `…/gallery` route.
- Modify the bots-list page template — add a "Gallery" toggle/link.
- Specs alongside each (`*.spec.ts`).

---

## Task 1: Gallery DTOs (backend schemas)

**Files:**
- Create: `PythonDataService/app/schemas/broker_v2_gallery.py`
- Test: `PythonDataService/tests/schemas/test_broker_v2_gallery.py`

**Interfaces:**
- Consumes: existing `ChartBar`, `ChartFillMarker` from `app/schemas/broker_v2_panel.py`.
- Produces:
  - `GalleryBotView(sid: str, symbol: str, label: str, running: bool, phase: str, desired_state: str, needs_attention: bool, realized_pnl_today: float, open_pnl: float, fills_today: int, last_bar_at_ms: int | None, primary_action: GalleryPrimaryAction)`
  - `GalleryPrimaryAction(action_id: str, label: str, enabled: bool, disabled_reason: str | None)`
  - `GallerySymbolBars(symbol: str, bars: list[ChartBar])`
  - `GalleryLiveSnapshot(stream_epoch: str, surface_version: int, as_of_ms: int, resolution: str, bots: list[GalleryBotView], symbols: list[GallerySymbolBars], markers: dict[str, list[ChartFillMarker]])`
  - `GalleryLiveUpdate(surface_version: int, as_of_ms: int, symbols: list[GallerySymbolBars], markers_delta: dict[str, list[ChartFillMarker]], bots_delta: list[GalleryBotDelta], removed_sids: list[str])`
  - `GalleryBotDelta(sid: str, realized_pnl_today: float, open_pnl: float, fills_today: int, phase: str, desired_state: str, needs_attention: bool, running: bool, last_bar_at_ms: int | None, primary_action: GalleryPrimaryAction)`

- [ ] **Step 1: Write the failing test**

```python
# tests/schemas/test_broker_v2_gallery.py
from __future__ import annotations
from app.schemas.broker_v2_gallery import GalleryLiveSnapshot, GalleryBotView, GalleryPrimaryAction, GallerySymbolBars
from app.schemas.broker_v2_panel import ChartBar

def _bar() -> ChartBar:
    return ChartBar(start_ms=1_700_000_000_000, end_ms=1_700_000_060_000, open="1.0", high="1.2", low="0.9", close="1.1", volume=100, source="ibkr")

def test_snapshot_round_trips_and_is_snake_case():
    snap = GalleryLiveSnapshot(
        stream_epoch="e1", surface_version=3, as_of_ms=1_700_000_060_000, resolution="1m",
        bots=[GalleryBotView(sid="Aug11-02", symbol="SPY", label="ORB", running=True, phase="ON_DUTY",
                             desired_state="RUNNING", needs_attention=False, realized_pnl_today=142.0,
                             open_pnl=-8.0, fills_today=12, last_bar_at_ms=1_700_000_060_000,
                             primary_action=GalleryPrimaryAction(action_id="stop", label="Stop", enabled=True, disabled_reason=None))],
        symbols=[GallerySymbolBars(symbol="SPY", bars=[_bar()])],
        markers={"Aug11-02": []},
    )
    dumped = snap.model_dump()
    assert dumped["bots"][0]["realized_pnl_today"] == 142.0
    assert dumped["symbols"][0]["bars"][0]["start_ms"] == 1_700_000_000_000
    assert GalleryLiveSnapshot.model_validate(dumped).surface_version == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `podman exec polygon-data-service python -m pytest tests/schemas/test_broker_v2_gallery.py -v`
Expected: FAIL — `ModuleNotFoundError: app.schemas.broker_v2_gallery`.

- [ ] **Step 3: Write the schemas**

```python
# app/schemas/broker_v2_gallery.py
from __future__ import annotations
from pydantic import BaseModel, Field
from app.schemas.broker_v2_panel import ChartBar, ChartFillMarker

class GalleryPrimaryAction(BaseModel):
    action_id: str
    label: str
    enabled: bool
    disabled_reason: str | None = None

class GalleryBotView(BaseModel):
    sid: str
    symbol: str
    label: str
    running: bool
    phase: str
    desired_state: str
    needs_attention: bool
    realized_pnl_today: float
    open_pnl: float
    fills_today: int
    last_bar_at_ms: int | None = None
    primary_action: GalleryPrimaryAction

class GalleryBotDelta(GalleryBotView):
    pass  # same shape; a delta carries a full bot view for the changed bot

class GallerySymbolBars(BaseModel):
    symbol: str
    bars: list[ChartBar] = Field(default_factory=list)

class GalleryLiveSnapshot(BaseModel):
    stream_epoch: str
    surface_version: int
    as_of_ms: int
    resolution: str = "1m"
    bots: list[GalleryBotView]
    symbols: list[GallerySymbolBars]
    markers: dict[str, list[ChartFillMarker]] = Field(default_factory=dict)

class GalleryLiveUpdate(BaseModel):
    surface_version: int
    as_of_ms: int
    symbols: list[GallerySymbolBars] = Field(default_factory=list)
    markers_delta: dict[str, list[ChartFillMarker]] = Field(default_factory=dict)
    bots_delta: list[GalleryBotDelta] = Field(default_factory=list)
    removed_sids: list[str] = Field(default_factory=list)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `podman exec polygon-data-service python -m pytest tests/schemas/test_broker_v2_gallery.py -v`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
ruff check PythonDataService/app/schemas/broker_v2_gallery.py PythonDataService/tests/schemas/test_broker_v2_gallery.py
git add PythonDataService/app/schemas/broker_v2_gallery.py PythonDataService/tests/schemas/test_broker_v2_gallery.py
git commit -m "feat(gallery): add aggregated gallery DTOs"
```

---

## Task 2: GalleryHub — snapshot composition

**Files:**
- Create: `PythonDataService/app/services/broker_v2_panel/gallery_hub.py`
- Test: `PythonDataService/tests/services/test_gallery_hub.py`

**Grounding (verified upstream, read before implementing):**
- Running roster: `app/services/broker_v2_panel/panel_data_source.py::get_catalog` (~L492-555) → `BotCatalogView` (fields incl. `strategy_instance_id`, `symbol`, `running`, `strategy_label`, `realized_pnl_today`, `open_pnl`, `fills_today`, `needs_attention`).
- Live bars: `app/services/live_bar_aggregator.py::LIVE_BAR_AGGREGATOR` — `ensure_subscribed(symbol)` (idempotent) and `snapshot(symbol, since_ms=None)`; returns the per-symbol 1m ring-buffer bars.
- Bar mapping to `ChartBar`: reuse the existing mapping in `app/services/broker_v2_panel/panel_chart_data_source.py` (the live pane already converts aggregator bars → `ChartBar`). Factor the mapping into a shared helper if it isn't already callable.

**Interfaces:**
- Produces:
  - `class GalleryHub` with `async def build_snapshot(self) -> GalleryLiveSnapshot`
  - `def running_symbols(catalog: list[BotCatalogView]) -> list[str]` (pure; deduped, order-stable)

- [ ] **Step 1: Write the failing test** (pure symbol-dedup helper first — no I/O)

```python
# tests/services/test_gallery_hub.py
from __future__ import annotations
from app.services.broker_v2_panel.gallery_hub import running_symbols

class _Cat:
    def __init__(self, sid, symbol, running):
        self.strategy_instance_id = sid; self.symbol = symbol; self.running = running

def test_running_symbols_dedupes_and_keeps_only_running():
    cat = [_Cat("a","SPY",True), _Cat("b","SPY",True), _Cat("c","QQQ",True), _Cat("d","IWM",False)]
    assert running_symbols(cat) == ["SPY", "QQQ"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `podman exec polygon-data-service python -m pytest tests/services/test_gallery_hub.py::test_running_symbols_dedupes_and_keeps_only_running -v`
Expected: FAIL — import error.

- [ ] **Step 3: Implement the pure helper + hub skeleton**

```python
# app/services/broker_v2_panel/gallery_hub.py
from __future__ import annotations
from app.schemas.broker_v2_panel import BotCatalogView
from app.schemas.broker_v2_gallery import (
    GalleryBotView, GalleryPrimaryAction, GallerySymbolBars, GalleryLiveSnapshot,
)

def running_symbols(catalog: list[BotCatalogView]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for c in catalog:
        if getattr(c, "running", False) and c.symbol not in seen:
            seen.add(c.symbol)
            out.append(c.symbol)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `podman exec polygon-data-service python -m pytest tests/services/test_gallery_hub.py::test_running_symbols_dedupes_and_keeps_only_running -v`
Expected: PASS.

- [ ] **Step 5: Write the failing snapshot-composition test** (fake catalog source + fake aggregator injected)

```python
# append to tests/services/test_gallery_hub.py
import pytest
from app.services.broker_v2_panel.gallery_hub import GalleryHub

class _FakeCatalogSource:
    def __init__(self, rows): self._rows = rows
    async def get_catalog(self, broker, account_id): return self._rows

class _FakeAggregator:
    def __init__(self): self.subscribed = []
    def ensure_subscribed(self, symbol): self.subscribed.append(symbol)
    def snapshot(self, symbol, since_ms=None):
        return [type("B", (), {"start_ms":1_700_000_000_000,"end_ms":1_700_000_060_000,
                               "open":1.0,"high":1.2,"low":0.9,"close":1.1,"volume":100,"source":"ibkr"})()]

@pytest.mark.asyncio
async def test_build_snapshot_subscribes_once_per_symbol_and_projects_bots():
    rows = [_Cat2("Aug11-02","SPY",True,142.0,-8.0,12), _Cat2("Aug11-03","SPY",True,10.0,0.0,3)]
    hub = GalleryHub(broker="alpaca", account_id="PA3", catalog_source=_FakeCatalogSource(rows), aggregator=_FakeAggregator())
    snap = await hub.build_snapshot()
    assert [s.symbol for s in snap.symbols] == ["SPY"]          # deduped
    assert {b.sid for b in snap.bots} == {"Aug11-02","Aug11-03"}
    assert snap.surface_version == 1
    assert snap.resolution == "1m"
```

Add a richer catalog stub `_Cat2` mirroring `BotCatalogView` fields (`strategy_instance_id, symbol, running, strategy_label, realized_pnl_today, open_pnl, fills_today, needs_attention`).

- [ ] **Step 6: Run to verify it fails** → then implement `GalleryHub.build_snapshot`

```python
# add to gallery_hub.py
import time
from app.services.broker_v2_panel.panel_chart_data_source import aggregator_bars_to_chart_bars  # factor/reuse the existing live mapping

class GalleryHub:
    def __init__(self, *, broker: str, account_id: str, catalog_source, aggregator):
        self._broker = broker
        self._account_id = account_id
        self._catalog_source = catalog_source
        self._aggregator = aggregator
        self._epoch = f"{broker}:{account_id}"
        self._version = 0

    def _primary_action(self, row: BotCatalogView) -> GalleryPrimaryAction:
        # running -> Stop, otherwise Resume. Enablement mirrors row_action; disabled_reason from status.
        running = getattr(row, "running", False)
        return GalleryPrimaryAction(
            action_id="stop" if running else "resume",
            label="Stop" if running else "Resume",
            enabled=True,
            disabled_reason=None,
        )

    def _project_bot(self, row: BotCatalogView) -> GalleryBotView:
        return GalleryBotView(
            sid=row.strategy_instance_id, symbol=row.symbol, label=getattr(row, "strategy_label", ""),
            running=getattr(row, "running", False), phase=getattr(row, "phase", ""),
            desired_state=getattr(row, "desired_state", ""), needs_attention=getattr(row, "needs_attention", False),
            realized_pnl_today=getattr(row, "realized_pnl_today", 0.0), open_pnl=getattr(row, "open_pnl", 0.0),
            fills_today=getattr(row, "fills_today", 0), last_bar_at_ms=None,
            primary_action=self._primary_action(row),
        )

    async def build_snapshot(self) -> GalleryLiveSnapshot:
        catalog = await self._catalog_source.get_catalog(self._broker, self._account_id)
        running = [c for c in catalog if getattr(c, "running", False)]
        symbols = running_symbols(catalog)
        symbol_bars: list[GallerySymbolBars] = []
        for sym in symbols:
            self._aggregator.ensure_subscribed(sym)
            raw = self._aggregator.snapshot(sym)
            symbol_bars.append(GallerySymbolBars(symbol=sym, bars=aggregator_bars_to_chart_bars(raw)))
        self._version += 1
        return GalleryLiveSnapshot(
            stream_epoch=self._epoch, surface_version=self._version, as_of_ms=int(time.time() * 1000),
            resolution="1m", bots=[self._project_bot(c) for c in running], symbols=symbol_bars, markers={},
        )
```

> If `aggregator_bars_to_chart_bars` doesn't yet exist as an importable helper, extract it from the live-pane mapping in `panel_chart_data_source.py` in this task (small refactor) and add a unit test for it.

- [ ] **Step 7: Run tests to verify pass**

Run: `podman exec polygon-data-service python -m pytest tests/services/test_gallery_hub.py -v`
Expected: PASS (all).

- [ ] **Step 8: Lint + commit**

```bash
ruff check PythonDataService/app/services/broker_v2_panel/gallery_hub.py PythonDataService/tests/services/test_gallery_hub.py
git add PythonDataService/app/services/broker_v2_panel/gallery_hub.py PythonDataService/app/services/broker_v2_panel/panel_chart_data_source.py PythonDataService/tests/services/test_gallery_hub.py
git commit -m "feat(gallery): GalleryHub snapshot composition with per-symbol bar dedup"
```

---

## Task 3: GalleryHub — versioned deltas

**Files:**
- Modify: `PythonDataService/app/services/broker_v2_panel/gallery_hub.py`
- Test: `PythonDataService/tests/services/test_gallery_hub.py`

**Interfaces:**
- Produces: `async def build_update(self, since_bar_ms: dict[str, int]) -> GalleryLiveUpdate` — new/changed bars per symbol (via `snapshot(sym, since_ms=...)`), changed bot stats, `removed_sids`; bumps `surface_version`.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_build_update_returns_only_new_bars_and_bumps_version():
    rows = [_Cat2("Aug11-02","SPY",True,142.0,-8.0,12)]
    agg = _FakeAggregator()
    hub = GalleryHub(broker="alpaca", account_id="PA3", catalog_source=_FakeCatalogSource(rows), aggregator=agg)
    first = await hub.build_snapshot()
    upd = await hub.build_update(since_bar_ms={"SPY": 1_700_000_060_000})
    assert upd.surface_version == first.surface_version + 1
    assert all(b.symbol == "SPY" for b in upd.symbols)
```

- [ ] **Step 2: Run to verify fail** → **Step 3: implement `build_update`** (calls `aggregator.snapshot(sym, since_ms=since_bar_ms.get(sym))`, re-projects bots, diffs roster for `removed_sids`, `self._version += 1`).
- [ ] **Step 4: Run to verify pass.**
- [ ] **Step 5: Commit** `feat(gallery): versioned deltas for GalleryHub`.

---

## Task 4: SSE endpoints (`/gallery/snapshot` + `/gallery/stream`)

**Files:**
- Create: `PythonDataService/app/routers/broker_v2_gallery.py`
- Modify: `PythonDataService/app/main.py` (include router)
- Test: `PythonDataService/tests/routers/test_broker_v2_gallery.py`

**Grounding:** copy the SSE framing/versioning shape from `app/routers/live_instances.py::stream_fleet_roster` (~L1753-1791): `event: snapshot`/`update`/`reset`/`end`, `id: {epoch}:{version}`, `: keepalive` every 15s, `StreamingResponse(media_type="text/event-stream")`. Mount pattern from `broker_v2_panel.py` (prefix `/api/brokers`).

**Interfaces:**
- `GET /api/brokers/{broker}/accounts/{account_id}/gallery/snapshot` → `GalleryLiveSnapshot`
- `GET /api/brokers/{broker}/accounts/{account_id}/gallery/stream` → SSE

- [ ] **Step 1: Write the failing endpoint test (snapshot GET)** using `httpx.AsyncClient` + `ASGITransport`, with the `GalleryHub` catalog source + aggregator overridden via FastAPI dependency to fakes.

```python
# tests/routers/test_broker_v2_gallery.py
import pytest, httpx
from httpx import ASGITransport
from app.main import app

@pytest.mark.asyncio
async def test_gallery_snapshot_returns_running_bots(monkeypatch):
    # patch the hub factory to use fakes (see conftest wiring below)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.get("/api/brokers/alpaca/accounts/PA3/gallery/snapshot")
    assert r.status_code == 200
    body = r.json()
    assert body["resolution"] == "1m"
    assert "bots" in body and "symbols" in body
```

- [ ] **Step 2: Run to verify fail** (404 — route not mounted).
- [ ] **Step 3: Implement the router** (a `get_gallery_hub(broker, account_id)` dependency returning a ref-counted per-account `GalleryHub`; the snapshot handler returns `await hub.build_snapshot()`; the stream handler yields the snapshot then loops `build_update` on the hub's change signal with a 15s keepalive and `reset` on epoch change) and `app.include_router(broker_v2_gallery.router)` in `main.py`.
- [ ] **Step 4: Write a stream smoke test** — open the SSE, assert the first `event: snapshot` frame parses as `GalleryLiveSnapshot`, then close.
- [ ] **Step 5: Run to verify pass.**
- [ ] **Step 6: Regenerate the OpenAPI contract**

```bash
podman exec polygon-data-service python export_openapi_contract.py
git add contracts/  # the regenerated snapshot
```

- [ ] **Step 7: Lint + commit** `feat(gallery): aggregated /gallery snapshot+stream endpoints`.

---

## Task 5: Frontend live store + types

**Files:**
- Create: `Frontend/src/app/components/broker/v2-panel/gallery/lib/gallery.types.ts`
- Create: `Frontend/src/app/components/broker/v2-panel/gallery/lib/gallery-live-store.service.ts`
- Test: `…/lib/gallery-live-store.service.spec.ts`

**Grounding:** EventSource lifecycle pattern from `Frontend/src/app/components/broker/account-desk/account-desk-holdings-store.service.ts`. Action pipeline from `…/v2-panel/lib/broker-v2-panel.service.ts::runBotAction` (base `/api/brokers/{broker}/accounts/{accountId}`).

**Interfaces:**
- Produces `GalleryLiveStore` (`providedIn` component) with signals:
  - `bots = signal<GalleryBotView[]>([])`
  - `barsBySymbol = signal<Map<string, ChartBar[]>>(new Map())`
  - `markersBySid = signal<Map<string, ChartFillMarker[]>>(new Map())`
  - `status = signal<'connecting'|'live'|'stale'|'error'>('connecting')`
  - methods `start(broker, accountId)`, `stop()`

- [ ] **Step 1: Write the failing test** — feed a fake `EventSource` a `snapshot` then an `update` message; assert `bots()` and `barsBySymbol().get('SPY')` reflect the merge and that `update` appends the new bar (last-bar replace on equal `start_ms`).

```typescript
// gallery-live-store.service.spec.ts (sketch)
it('merges snapshot then update deltas', () => {
  const store = TestBed.inject(GalleryLiveStore);
  store.__ingest({ type: 'snapshot', /* GalleryLiveSnapshot */ });
  store.__ingest({ type: 'update', /* GalleryLiveUpdate with new SPY bar */ });
  expect(store.bots().length).toBe(2);
  expect(store.barsBySymbol().get('SPY')!.at(-1)!.start_ms).toBe(/* newest */);
});
```

(Expose a small `__ingest(evt)` seam so the parser is unit-testable without a real `EventSource`.)

- [ ] **Step 2: Run to verify fail** → **Step 3: implement** the store (open `EventSource`, route `snapshot`/`update`/`reset` to `__ingest`, merge bars by `start_ms` with last-wins on the forming bar, dedupe markers by `order_ref`, set `status`; 5s poll fallback to `/gallery/snapshot` if the stream errors, mirroring `BotPanelLiveStore`).
- [ ] **Step 4: Run to verify pass.**
- [ ] **Step 5: Lint + commit**

```bash
npx eslint Frontend/src/app/components/broker/v2-panel/gallery/ --max-warnings 0
git add Frontend/src/app/components/broker/v2-panel/gallery/lib/
git commit -m "feat(gallery): live store with one aggregated EventSource"
```

---

## Task 6: BotTileComponent (lightweight-charts tile)

**Files:**
- Create: `…/gallery/bot-tile/bot-tile.component.{ts,html,scss,spec.ts}`

**Grounding:** lightweight-charts v5 usage + markers from `Frontend/src/app/components/broker/v2-panel/dual-pane-chart/dual-pane-chart.component.ts` (series creation, `createSeriesMarkers`, `series.update()`). Anatomy **B** from the spec.

**Interfaces:**
- Consumes inputs: `bot = input.required<GalleryBotView>()`, `bars = input.required<ChartBar[]>()`, `markers = input<ChartFillMarker[]>([])`.
- Emits: `action = output<{ sid: string; actionId: string }>()` (the guarded quick action); tile-body click navigates via `Router`.

- [ ] **Step 1: Write the failing test** — render with a `GalleryBotView` + bars; assert the header shows the symbol + formatted price, the footer shows realized/open P&L, and clicking the guarded action opens the confirm then emits `action`. (Testing Library; mock `Router`.)
- [ ] **Step 2: Run to verify fail** → **Step 3: implement** — `OnPush`, create the chart in `afterNextRender`, `effect(() => this.chart.update(lastBar))` reading `bars()` (imperative, outside CD); candlestick + volume + `setMarkers`; header/footer per anatomy B; single posture action from `bot().primary_action` with an inline confirm; disabled-with-reason when `!enabled`.
- [ ] **Step 4: Run to verify pass.**
- [ ] **Step 5: Lint + commit** `feat(gallery): live bot tile on lightweight-charts`.

---

## Task 7: Layout model + BotGalleryDockComponent

**Files:**
- Create: `…/gallery/lib/gallery-layout.ts` (+ `.spec.ts`)
- Create: `…/gallery/bot-gallery-dock/bot-gallery-dock.component.{ts,html,scss,spec.ts}`

**Interfaces:**
- `gallery-layout.ts` (pure): `autoDivision(n: number): { cols: number; rows: number }` (`cols = ceil(sqrt(n))`); `loadLayout(accountId): TileLayout[]`; `saveLayout(accountId, layout)`; `resetLayout(accountId)`; `paginate<T>(items: T[], page: number, size = 20): { pageItems: T[]; pages: number }`.

- [ ] **Step 1: Write failing pure-layout tests** — `autoDivision(20) === {cols:5, rows:4}`; `paginate` of 25 items → page 0 has 20, `pages === 2`; save→load round-trips via a localStorage mock.
- [ ] **Step 2: Run fail → Step 3: implement** `gallery-layout.ts`. → **Step 4: pass.**
- [ ] **Step 5: Write failing dock test** — given 6 bots, renders 6 tiles in a `ceil(sqrt)` grid; a CDK drop reorders and persists; a resize sets `--col-span`; "Reset" restores auto grid; pagination controls appear for >20.
- [ ] **Step 6: Run fail → Step 7: implement** the dock — `cdkDropList` reorder, custom corner resize handle writing CSS-grid spans, `@for` with `track bot.sid`, pagination footer, "Reset layout"; persists via `gallery-layout.ts`. → **Step 8: pass.**
- [ ] **Step 9: Lint + commit** `feat(gallery): CDK resizable dock with pagination + persistence`.

---

## Task 8: Page host, route, states, and the list-page toggle

**Files:**
- Create: `…/gallery/bot-gallery-page/bot-gallery-page.component.{ts,html,scss,spec.ts}`
- Modify: `Frontend/src/app/app.routes.ts`
- Modify: the bots-list page template (`…/v2-panel/bots-list-page/…`) — add the "Gallery" toggle/link.

**Interfaces:**
- Route: `brokers/:broker/accounts/:accountId/gallery` → lazy `loadComponent` → `BotGalleryPageComponent`; route params bound to `input()`s (`broker`, `accountId`).

- [ ] **Step 1: Write failing tests** — page provides `GalleryLiveStore`, calls `start(broker, accountId)`; renders the dock when `bots()` non-empty; renders the empty state at 0 bots; renders a "delayed" indicator when `status()==='stale'`; the action output routes through `BrokerV2PanelService.runBotAction`.
- [ ] **Step 2: Run fail → Step 3: implement** page host (toolbar with "Today · 1m" label + reset + pagination wiring; states loading/empty/stale/error; wire tile `action` → `runBotAction` with optimistic pending) + add the route + add the list-page "Gallery" toggle. → **Step 4: pass.**
- [ ] **Step 5: Project-scope lint + full frontend test run**

```bash
npx eslint Frontend/src/ --max-warnings 0
podman exec my-frontend npx ng test --watch=false --include='**/gallery/**/*.spec.ts'
```

- [ ] **Step 6: Commit** `feat(gallery): gallery page, route, and list-page toggle`.

---

## Task 9: End-to-end verification + pre-PR gate

- [ ] **Step 1:** Restart the data-plane so the new router loads (`podman restart polygon-data-service`; hot-reload is unreliable per project notes).
- [ ] **Step 2:** Drive the real page in a browser: navigate to `…/accounts/<acct>/gallery`, confirm tiles render live, drag/resize persists across reload, pagination works past 20, a guarded quick action round-trips through `runBotAction`, and feed-stale shows honestly. Use the `verify` skill.
- [ ] **Step 3:** Backend suite: `podman exec polygon-data-service python -m pytest tests/ -k gallery -v`; confirm OpenAPI contract is committed and the contract CI gate passes locally.
- [ ] **Step 4:** Baseline pre-existing failures per `.claude/rules/testing.md`; surface any in the PR description.
- [ ] **Step 5:** Run the `thermo-nuclear-code-quality-review` skill; address every **major** finding before push.
- [ ] **Step 6:** Open the PR (branch off master first).

---

## Self-Review

**Spec coverage:** aggregated stream (T1–T4) ✓ · lightweight-charts tile anatomy B (T6) ✓ · CDK resizable dock (T7) ✓ · paginate >20 (T7/T8) ✓ · localStorage layout (T7) ✓ · guarded quick action via existing pipeline (T6/T8) ✓ · running-bots-this-account scope (T2) ✓ · route + list toggle (T8) ✓ · int64 ms UTC / OHLC strings (T1) ✓ · OpenAPI contract (T4) ✓ · states empty/stale (T8) ✓. SP2/SP3 intentionally out of scope.

**Placeholder scan:** the two upstream helpers referenced by name — `aggregator_bars_to_chart_bars` (T2, extract-if-missing with its own test) and the `get_gallery_hub` dependency (T4, defined in that task) — are the only forward references and both have a defining task. No TBD/TODO left.

**Type consistency:** `GalleryBotView`/`GalleryPrimaryAction`/`GallerySymbolBars`/`ChartBar` names are identical across backend (T1–T4) and the TS mirrors (T5–T8); `runBotAction` and `barsBySymbol`/`markersBySid` signatures match between store (T5) and consumers (T6/T8).
