# Engine Lab Overhaul — Mission-Control Workbench, Backend-Authored Verdicts, First-Class Parity

**Date:** 2026-07-12
**Status:** Approved design (user-approved via decision Q&A + plan review)
**Base:** branches from `codex/lean-sidecar-default-ema-template` (depends on the LEAN Jobs-API unification landed there)

## 1. Problem

The Engine Lab (`/engine`) hosts two numerically-equivalent backtest engines (Python in-process + LEAN sidecar) unified over the Jobs API, but the surface is a hodgepodge of five tabs (Configure / Results / History / Docs / Replay):

- Pressing Run does `run(); activeTab.set('1')` (`lean-engine.component.html:296`) — navigating away from the SSE progress banner, which only renders inside the Configure tab. Live progress is never visible.
- LEAN results never render on the Results tab at all ("v1" gap); History is the only inspection surface for LEAN runs.
- The run "readiness verdict" is computed by a 465-line **frontend** util (`readiness-score.util.ts`) — violating the repo's backend-authored-verdict architecture (ADR-0013/0014/0025) and silently re-grading old runs whenever the util changes.
- The platform's defining feature — same strategy, two engines, one agreement verdict — has no surface, despite a working compare pipeline (`Backend/Controllers/CompareController.cs` + `PythonDataService/app/routers/reconcile_trades.py` + the 8-category `DivergenceCategory` taxonomy) sitting unlinked at `/runs/compare`.
- Results have two sources of truth: Python renders an in-memory job-result blob; LEAN only lands in Postgres. Equity curves are not persisted at all.
- Configure-time asymmetry: Python takes registry strategies + free-form params; LEAN takes 4 hard-coded templates or a raw algorithm-source textarea.

## 2. Locked decisions

1. **Persisted run = single source of truth.** Both engines render results from Postgres via one routable `/engine/runs/:id` surface. Equity curve gets persisted. History-click and fresh-run completion navigate to the same view.
2. **Backend-authored RunVerdict, frozen at completion, versioned.** The Python service authors one RunVerdict for both engines at run completion; persisted as JSONB on `StrategyExecution`; never recomputed on read. Legacy rows render honest-empty ("No verdict — pre-versioning run").
3. **Parity is first-class.** Sibling linking via explicit `parity_group_id` + backend-authored parity verdict using the existing `DivergenceCategory` taxonomy. Parity verdicts are frozen evidence.
4. **Mission-control workbench, no tabs.** Left rail = configure; center stage transforms in place (idle → live phase timeline + log tail → verdict card → "Open full report →"); history below. Run never navigates away.
5. **Engine Lab = validation factory.** Unified strategy catalog (draft + validated visible in Engine Lab; bots/Strategy Lab see validated only). Parity evidence pinned to run IDs feeds `StrategyValidationEntry`; promotion stays **human-flagged** (ADR-0023) — Engine Lab makes producing evidence one click.
6. **Strategies are authored in-repo as versioned code.** The LEAN algorithm-source textarea UI is deleted. `algorithm_source` survives only on the sync test endpoint's request model.
7. Free-hand deletions granted: tabs die; Docs moves to `/engine/docs`; Replay relocates under run detail.

**Verified corrections to prior assumptions:** LEAN *does* produce an equity curve (`normalized_parser.py:224` → `NormalizedEquityPoint`); a full cross-engine compare pipeline already exists end-to-end (reuse + refactor, not build); `Program.cs:219` uses `EnsureCreated()` so new migrations need explicit dev application.

## 3. Target architecture

### 3.1 Routes (`Frontend/src/app/app.routes.ts`)

| Route | Component |
|---|---|
| `engine` | `EngineWorkbenchComponent` (new) — replaces `LeanEngineComponent` |
| `engine/runs/:id` | `EngineRunDetailComponent` (new) — single run-detail surface, route param via input binding |
| `engine/compare` | `RunsCompareComponent` (existing, refactored + moved; `runs/compare` becomes redirect) |
| `engine/docs` | `LeanEngineDocsComponent` (existing 919-line component, re-routed to its own lazy route) |

### 3.2 New component tree — `Frontend/src/app/components/engine-lab/`

Templates < 80 lines each; no file near the 1k thermo threshold.

**workbench/**
- `engine-workbench.component` (~180 ts / ~65 html) — layout shell: rail | stage above history; provides `LaunchOrchestrator` + `EngineRunDockSource` (same provider pattern as `lean-engine.component.ts:233-240`)
- `run-launch-rail.component` (~260/80) — catalog picker, engine choice python|lean|both, symbol/window (reuse `shared/ticker-range-picker`), fill/cash/commission, Run; preflight gates rendered via shared `operator-blocker-list`
- `rail-strategy-picker.component` (~120) — catalog cards: per-engine impl badges, validation-state chip, param form (port `paramEntries` logic from `lean-engine.component.ts:556-565, 705-735`)
- `run-stage.component` (~90/50) — `@switch` idle | live | verdict | failed
- `run-stage-live.component` (~140) — phase timeline mirroring `app/jobs/phases.py` taxonomies + log tail from `JobsService`; dual side-by-side timelines for "Both"
- `run-stage-verdict.component` (~100) — verdict headline + grade chip + "Open full report →" routerLink; for Both: two mini-cards + "Compare engines"
- `launch-orchestrator.service` (~260, component-provided) — replaces `runPython` (L765-812), `runLean` (L827-898), `wireEngineJobEffect`, `wireLeanJobEffect` with ONE job-watch parameterized by job type; owns DataPolicy composition (port `composeDataPolicy` L456-484), session-open math (port L985-1024), `parity_group_id` minting for Both
- `preflight.service` (~140) — launcher diagnose + data availability → `OperatorBlocker[]` (fix_here/wait dispositions)

**run-detail/**
- `engine-run-detail.component` (~160/70) — `rxResource` on route id → GraphQL `backtestRun(id)`
- `run-verdict-card.component` (~110/75) — **verbatim** renderer of persisted RunVerdict JSON; zero grading logic; null → "No verdict — pre-versioning run" (SCSS ring salvaged from readiness-score-card)
- `run-kpi-strip.component` (~110) — 7-KPI hero, keeps `metric-grade.util.ts` (per-metric traffic lights ≠ composite verdict)
- `run-chart-panel.component` (~130) — reuses `engine-chart` fed by persisted equity JSONB + re-derived chart bars; missing bars → honest-empty + "Fetch data" affordance
- `run-parity-panel.component` (~120) — siblings by `parity_group_id`, persisted parity verdicts, "Compare engines" action
- `run-evidence-sections.component` (~80/70) — accordion: fee analytics (port `feePerTrade`/`feeDragPct` from `engine-results.component.ts:163-174`), `lean-statistics` (reused), insight summary (persisted), trade log drawer
- `run-detail.service` (~150)

**Top-level:** `catalog.service` (~90) — `GET /api/engine/catalog`

**Reused unchanged:** `engine-chart/`, `lean-statistics/`, `insight-panel/` (summary half), `tv-compat-panel/` (moves into rail as warning-level preflight accordion), `engine-lab-run-history/` (row click → navigate to run detail), `engine-replay-v2/`, `engine-run-dock-source.ts`, `jobs.service.ts`

### 3.3 Deletions (complete list)

- `components/lean-engine/lean-engine.component.{ts,html,scss,spec.ts}` (1435+413 lines — tabs + the UX bug die here)
- `components/lean-script-editor/` (5 files) + `services/lean-lint.service.ts` (sole consumer is the editor) — textarea authoring deleted
- `readiness-score.util.ts` (465 lines) — **only after** the golden parity gate (Slice 1) is green; its temporary golden spec goes with it
- `readiness-score-card.component.ts` grading call (`computeReadiness`) — body replaced by verbatim renderer
- `components/lean-engine/engine-results/*` — decomposed into run-detail children
- PrimeNG tabs imports in engine lab; `engine/docs → engine` redirect (replaced by real route)
- Legacy phase ids `loading_bars`/`simulating`/`computing_stats` — one deploy cycle after Slice 3
- `LeanAlgorithmMode`/`leanSource`/`useCustomLeanAlgorithm` machinery

## 4. Schema & DTO design

### 4.1 RunVerdict (new `app/schemas/run_verdict.py`, JSONB on `StrategyExecution.RunVerdictJson`)

```
RunVerdict { verdict_version:int=1, engine:"python"|"lean", generated_at_ms:int64,
  composite:int|null, grade:"A+".."F"|null,
  signal:"Deploy"|"Paper-trade"|"Iterate"|"Rework"|"Reject"|null,
  headline:str, red_flags:[stable tokens], dimensions:[{key,label,weight,score,summary,sub_scores}],
  missing_metrics:[str], normalized_weights:bool,
  cleanliness:{is_clean,is_reconciliation_grade,error_counts}|null }  # LEAN only
```

- Grading ported 1:1 from `readiness-score.util.ts` → `app/services/run_verdict_service.py` (provenance block per learn-ai-validation; `RUN_VERDICT_VERSION = 1`, bumped only on rule changes; never recomputed on read).
- **Cleanliness folding (LEAN):** `is_clean == False` → red flag `lean_run_not_clean`, force `signal="Rework"`, headline prepends the gating-error warning; composite/grade still computed for transparency. Non-reconciliation-grade (benchmark missing) → note in `missing_metrics` only.
- **Authoring points:** Python — in `execute_engine_backtest` after stats (~`engine.py:1016`), added to `EngineBacktestResponse` + `_save_study_sync` body. LEAN — in `build_persist_payload` (`lean_sidecar_persistence.py:506`) with `classified_errors` threaded from `run_trusted_sample`; failed-run payloads get a Reject verdict with cleanliness attached (so NULL verdict unambiguously = pre-versioning).
- **Known semantic fix (not a blind port):** LEAN trade-level Sharpe/Sortino are hard zeros (`lean_sidecar_persistence.py:421-425`); the LEAN input adapter maps them to `None` (unavailable) so the trade-gap sub-score doesn't misgrade — documented + dedicated test.

### 4.2 Numerical-rigor gate for the port

One shared golden fixture (~20 curated stat payloads covering every threshold edge: Sharpe 2.99/3.0/3.01, PF 4, win-rate 0.85, all-null dims, Infinity PF, zero-drawdown Calmar guard):
1. Temporary TS spec asserts `computeReadiness` matches the fixture.
2. `tests/services/test_run_verdict_parity.py` asserts `compute_run_verdict` matches the same fixture.
3. Both green in the same CI run → TS util deleted; pytest + fixture stay as the permanent regression anchor.

### 4.3 Equity curve — downsampled JSONB (`EquityCurveJson`)

Raw Python curves are per input minute bar (`app/engine/engine.py:416-423`): 3-month ≈ 24.6k pts, 2-year ≈ 196k pts — too big raw. New `app/engine/results/equity_downsample.py` (provenance + tests incl. drawdown-trough preservation): keep strategy-bar closes + all trade entry/exit timestamps + running extrema + first/last; hard cap 10,000 pts with stride fallback. Honest receipt envelope:

```json
{"cadence":"strategy_bar_close","downsample":{"policy":"strategy_bar_close+trade_marks+extrema","raw_points":24570,"kept_points":1685},
 "points":[{"t":1748528100000,"e":100234.55}]}
```

LEAN persists `normalized.equity_curve` through the same envelope, `cadence:"lean_chart_sampling"`. Statistics keep consuming the FULL curve — downsampling is display-only. Curves are never point-diffed cross-engine (cadences differ; parity is trade-level). All timestamps int64 ms UTC (temporal rigor).

### 4.4 Chart bars — re-derive, don't persist

Fully derivable from `DataPolicyJson` + data on disk. New `POST /api/engine/chart-bars` reusing the engine's own readers/consolidators (single canonical consolidation implementation). Missing data → 404-with-detail → honest-empty chart + "Fetch data" affordance (existing `ensure_range` plumbing, `engine.py:795-837`).

### 4.5 Insights

Not persisted today (live response only). Persist small `InsightSummaryJson` in the same migration; per-insight rows intentionally not persisted (unbounded growth, low read value). Run detail renders the summary half of insight-panel; table half honest-empty.

### 4.6 Parity

- **Sibling linking:** explicit `ParityGroupId varchar(64) NULL` + index on `StrategyExecution` (config-hash rejected: not auditable, silently couples unrelated runs). Minted by the launch orchestrator for "Both"; threaded through both request models → both persist payloads. Both = two ordinary jobs, no third job.
- **Parity verdict:** new table `ParityVerdicts (Id, LeftExecutionId FK, RightExecutionId FK, ParityGroupId, VerdictVersion, Status "passed"|"failed"|"incomparable", VerdictJson jsonb, CreatedAtUtc, UNIQUE(left,right))`. Flow: .NET `POST /api/runs/parity-verdicts {left,right}` reuses `CompareController.BuildCompareAsync` internals (compat gate + summary deltas + Python reconcile-trades) → new Python `POST /api/parity/verdict` authors prose (headline, 8-category counts, tolerances, first divergence, incomparable_reasons — Python owns taxonomy + copy per the `RunCompareService.cs` comment) → persist, idempotent on pair, frozen. Compat-gate failure → `status="incomparable"` with recorded reasons.
- **RunsCompareComponent: reuse + refactor** (165 ts / 186 html, already rxResource/OnPush): move route, add persisted-verdict header rendered verbatim, pipe id-like fields through `receiptLabel`.

### 4.7 Strategy catalog (new `app/schemas/strategy_catalog.py` + `app/services/strategy_catalog_service.py` + router)

```
StrategyCatalogEntry { strategy_key, display_name, description,
  implementations:[{engine, kind:"python_registry"|"lean_template", ref, source_path, source_sha256}],
  params_schema, supported_resolutions, validation_state, deployable,
  behavioral_equivalence|null, parity_evidence:[{left_execution_id,right_execution_id,parity_verdict_id,status,pinned_at_ms}] }
```

Joins three sources: `_STRATEGY_REGISTRY` (`app/engine/strategy/registry.py`), the LEAN template vocabulary (`lean_sidecar.py:265`), and the validation manifest (`app/services/strategy_validation_manifest.py:146`). Cross-engine mapping in static `app/data/strategy_catalog_links.json` (today only `ema_crossover` ↔ `spy_ema_crossover` truly pairs; unpaired entries disable LEAN/Both in the rail). Endpoints: `GET /api/engine/catalog`; `POST /api/engine/catalog/{key}/pin-parity-evidence` extending `StrategyEvidenceSnapshot` with `parity_verdict_ref` + `pinned_run_ids`. **Promotion stays the human-flagged `StrategyValidationFlagEvent` flow in the existing strategy-validation UI.** Catalog stays FastAPI-direct (config-plane, same as existing `/api/engine/strategies`).

### 4.8 GraphQL (Hot Chocolate v15)

New `Backend/GraphQL/BacktestRunDetailQuery.cs`: `[GraphQLName("backtestRun")] backtestRun(id: Int!)` → `BacktestRunDetailType`: all node fields + `symbol` (Ticker nav), KPI columns, `fillMode`, `durationMs`, `leanStatisticsJson`, `verdictJson: String` (raw string — frozen versioned artifact, typed client-side via TS mirror `Frontend/src/app/api/run-verdict.types.ts`, same pattern as `operator-blocker.types.ts`), `verdictVersion/Grade/Signal`, `equityCurve: [{t: Long!, e: Decimal!}]` (parsed server-side), `insightSummaryJson`, `parityGroupId`, `trades` (int64 ms via existing `UnixMs` helper; explicit `[GraphQLName("pnL")]`), `paritySiblings`, `parityVerdicts`.

DataLoaders: none needed for single-run detail; the history list gains denormalized `VerdictGrade`/`VerdictSignal`/`ParityGroupId` scalars in the `GetBacktestRuns` projection + `ParitySiblingsDataLoader` (grouped, keyed on ParityGroupId) for sibling badges.

### 4.9 EF migration (one, in Slice 1): `AddRunVerdictEquityCurveAndParity`

`StrategyExecutions` += `RunVerdictJson jsonb`, `VerdictVersion int`, `VerdictGrade varchar(4)`, `VerdictSignal varchar(16)`, `EquityCurveJson jsonb`, `InsightSummaryJson jsonb`, `ParityGroupId varchar(64)` + index (all NULL). New `ParityVerdicts` table.

**⚠ Flag:** `Program.cs:219` uses `EnsureCreated()` — the migration will NOT auto-apply to an existing dev volume. Apply via `dotnet ef database update` or pgdata volume reset. Optional (needs user sign-off, do NOT assume): switch to `Database.Migrate()`.

### 4.10 Compat

- Legacy rows: all new columns NULL → per-section honest-empty ("No verdict — pre-versioning run", "Equity curve not recorded", no parity panel). No backfill.
- `POST /api/studies` (`StudiesApi.cs`) + `PersistLeanRunPayload` gain optional null-tolerant fields (PR-B synthesize-legacy pattern).
- Sync `/api/lean-sidecar/trusted-runs` untouched (test infra).
- New `run-stage-live` keeps the legacy phase-id map one deploy cycle, then deleted.

## 5. Delivery slices (tracer bullets, each shippable + demoable)

**Slice 1 — Verdict engine + schema (ugliest risk first; backend-only, ships dark).**
`run_verdict.py`, `run_verdict_service.py` (port + provenance), golden fixture + temporary TS golden spec + pytest parity; wire into `EngineBacktestResponse`/`_save_study_sync` + `build_persist_payload` (+ `classified_errors` threading); the combined EF migration; `SaveStudyRequest`/`PersistLeanRunPayload` + both persistence services accept new fields.
*Demo:* run both engines → `psql` shows `RunVerdictJson` with grade/headline on both rows; legacy rows NULL.

**Slice 2 — Equity persistence + run-detail route (read path).**
`equity_downsample.py` + tests; both persist payloads attach equity + insight summary; `POST /api/engine/chart-bars`; .NET `backtestRun(id)` + detail types; FE `/engine/runs/:id` component tree incl. verbatim verdict card; history row click navigates here.
*Demo:* history click → full report (verdict, equity chart, trades, fees, LEAN stats) rendered 100% from Postgres; pre-migration row renders honest-empty; missing bars → honest-empty + fetch affordance.

**Slice 3 — Workbench shell; delete tabs, textarea, TS grader.**
`EngineWorkbenchComponent` + rail + stage + `launch-orchestrator.service` + `preflight.service` (OperatorBlocker gates); LEAN terminal path navigates via `strategy_execution_id` from the job result (`lean_sidecar_service.py:249-252`) — closes the "LEAN has no results" gap; Docs → `/engine/docs`; all §3.3 deletions land (TS grader gated on Slice-1 parity green).
*Demo:* run Python from `/engine` — stage morphs idle → phase timeline → verdict card in place, no tab jump; run LEAN — identical treatment; mid-run refresh → dock reattaches.

**Slice 4 — Parity: Both-run + parity verdicts.**
`parity_group_id` threading; "Both" launch (two jobs, one group id, identical DataPolicy) + dual-timeline stage; Python `POST /api/parity/verdict`; .NET `POST /api/runs/parity-verdicts` + `ParityVerdictType` + siblings + DataLoader; RunsCompare refactor → `/engine/compare` with persisted-verdict header; run-detail parity panel.
*Demo:* Both run → two linked rows → Compare engines → authored, persisted parity verdict; re-open instant (frozen — verify no second reconcile-trades call); incomparable pair yields explanatory verdict.

**Slice 5 — Strategy catalog + validation wiring.**
Catalog schemas/service/endpoint + `strategy_catalog_links.json`; rail catalog picker with validation-state chips; pin-parity-evidence endpoint + `StrategyEvidenceSnapshot` extension; link into the existing strategy-validation UI for human flagging.
*Demo:* rail lists all catalog strategies with per-engine badges; pin a Slice-4 parity verdict to a strategy; flag `accepted_for_deploy` in strategy-validation with evidence referencing pinned runs.

**Slice 6 — Replay relocation + cleanup.**
Replay panel under run detail (Python rows only; run id = study id, synthesize `study` input from detail query), flag kept; delete legacy phase ids; dead reconstruction paths (`onStudySelected` L1383-1427); stale "See Docs tab" copy.
*Demo:* open a Python run detail → Replay replays from persisted trades + refetched bars.

## 6. Verification

Per slice:
- Python: `podman exec polygon-data-service python -m pytest tests/ -v -k "verdict or equity or parity or catalog"` (big suites: sibling container from the same image); `ruff check PythonDataService/app/ PythonDataService/tests/`
- .NET: `cd Backend.Tests && dotnet test`; `dotnet format podman.sln --verify-no-changes`
- Frontend: `podman exec my-frontend npx ng test`; `npx eslint Frontend/src/ --max-warnings 0`. Testing Library specs assert rendered output (verdict card renders headline verbatim from a JSON fixture; honest-empty for null).
- **Slice-1 gate:** golden fixture green on BOTH runtimes in the same CI run before any TS deletion.
- PythonDataService hot-reload is broken on macOS+podman — `podman restart polygon-data-service` before treating manual runs as authoritative.

End-to-end (after Slice 4): compose up + host LEAN launcher → `/engine` → ema_crossover, SPY, 1M → Both → dual timelines → verdict cards → open run detail (cross-check vs `psql`) → Compare engines → parity verdict persists → pin evidence → flag in strategy-validation. Legacy-row check: a pre-migration run renders three honest-empty sections, no console errors.

## 7. Non-goals

- No automatic promotion of strategies — validation flagging stays human (ADR-0023).
- No point-wise equity-curve diffing across engines (cadences differ; parity is trade-level).
- No per-insight row persistence (summary only).
- No backfill of verdicts/equity for legacy runs (honest-empty instead).
- No change to the sync `/api/lean-sidecar/trusted-runs` test endpoint.
- Switching `EnsureCreated()` → `Database.Migrate()` is flagged but not assumed — separate decision.
