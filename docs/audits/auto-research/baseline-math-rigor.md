# Math rigor baseline — learn-ai

**Status:** in-progress (all 10 phases touched at headline level; per-file triage owed for Phase 3 TS, Phase 4 4-field-blocks, Phase 8 DTOs, Phase 9 TS)
**Started:** 2026-05-05
**Last updated:** 2026-05-06
**Run count:** 7
**Generator:** `.claude/skills/auto-research-tick` (baseline mode)

> This document is **frozen** once the baseline completes. Live state moves to a separate `current-state.md` after hardening. Do not edit this doc by hand once frozen except to append entries to the **Remediation log** at the bottom.

## 0. Executive summary

_Filled at the end of the first sweep and updated after every subsequent run that closes the loop on a phase. Running tallies live here; details live below._

| Severity | Open | Deferred | Closed | Total |
|---|---|---|---|---|
| P0 | 2 | 0 | 0 | 2 |
| P1 | 17 | 0 | 2 | 19 (1 status=awaiting-human) |
| P2 | 9 | 0 | 2 | 11 |
| P3 | 2 | 0 | 0 | 2 |

**Files audited:** All 10 phases touched. Phase 1 substantially complete (registry cross-check + subtree inventory + drift detection). Phase 2 items 2 + 5 verified; items 1 + 4 already deferred per Phase 1 findings (F-0010, F-0011, F-0018). Phase 3 .NET fully triaged (F-0021 + F-0022); Python ingestion fully triaged (F-0023 + F-0024); TS rollup with per-file triage owed (F-0020). Phase 4 headline (F-0027) — file-by-file 4-field-block triage owed. Phase 5 headline (F-0026 — fixture coverage). Phase 6 substantially clean (F-0025). Phase 7 subsumed by Phase 3 P0. Phase 8 sample (F-0032 — decimal→double); per-canonical tracing + DTO file-by-file owed. Phase 9 sample of `lean-engine.component.ts` confirms F-0028 severity; 7 high-suspicion files owed. Phase 10 done (F-0030 reference notes + F-0031 warmup).
**Files skipped:** Per-file triage in Phases 3 (TS), 4, 8 (DTOs), 9 (TS).
**Phases complete:** 10/10 at headline level; 0/10 file-by-file. Baseline status remains `in-progress` because per-file triage is owed; transition to `baseline-complete-awaiting-remediation` is a human judgment call about whether headline coverage is sufficient given the §6 hardening gate scope.

## 1. Posture vs. `.claude/rules/numerical-rigor.md`

For each rule, one row: **holding** / **violated** / **partial**, with the count of supporting findings and a one-line summary.

| Rule | Holding? | Findings | Notes |
|---|---|---|---|
| Equivalence levels declared per port | TBD | — | Phase 4 |
| Golden fixtures present and attributed | **violated** | F-0026 | Only 3 fixtures on disk; iv30 missing attribution |
| Tolerances explicit (no default `np.allclose`) | **mostly holding** | F-0025 | One bare `np.isclose` in edge_score.py |
| Tolerances justified when loosened | **mostly holding** | F-0025 | One .NET `Assert.Equal(.., delta:4)` without rationale |
| Timestamp canonical format `int64 ms UTC` at all boundaries | **violated** | F-0009, F-0019, F-0020 | sanitizer emits ISO-Z at wire; trade_comparison silently UTC-stamps naive strings |
| Timestamp ban-list clean (Python) | **violated** | F-0009, F-0019, F-0023, F-0024, F-0033 | All 14 non-ingestion candidates triaged: 10 confirmed in violation. Ingestion: 4 files confirmed (sanitizer, dataset_service, polygon_ingest, polygon_client). |
| Timestamp ban-list clean (.NET) | **violated** | F-0020, F-0021 (P0), F-0022 | All 4 candidate files confirmed violators; 2 are ingestion-path P0 |
| Timestamp ban-list clean (TypeScript) | **violated** | F-0020, F-0034 | 45 files triaged into 5 tiers. Tier 1 (P1, ~17 occurrences) = engine-replay services; risk is conditional on producer-side wire format. Tier 2-4 mostly benign or display. |
| Fail-fast ingestion (no silent dedup / forward-fill) | **violated** | F-0009, F-0023 | sanitizer silent dedup; dataset_service silent forward-fill (P0) |
| Sovereignty (no runtime calls into `references/`) | TBD | — | Phase 4 |
| Math Provenance Contract: 4-field block on canonical math | **violated** | F-0027 | Confirmed via repo-wide grep: only **2 files** in all of `PythonDataService/app/` have any 4-field marker (`indicators/rsi.py`, `services/strategies/lean_statistics.py`) |
| Single canonical per concept (no silent duplicates) | **partial** | F-0001/F-0002/F-0004/F-0005/F-0007/F-0008 | Multiple unregistered canonical math subtrees discovered |
| Authority hierarchy: Python is the home of canonical math (rule 5) | **partial** | F-0010, F-0011 | PositionEngine FIFO + SnapshotService drawdown both compute math in .NET; not registered as legacy-ok |
| Warmup behavior documented per indicator | **partial** | F-0031 | 5 of 7 indicators document warmup; `macd.py` missing |
| Reconciliation reports exist for reconciled ports | **partial** | F-0030 | 24 of 24 indicator notes present; ~15 strategy/stat/portfolio notes missing |

## 2. Canonical math inventory

Cross-check between `docs/math-sources-of-truth.md` and the actual code.

- **Listed and present, canonical file matches:** Most listed canonical paths verified (SMA/EMA/RSI in `engine/indicators/`, `bs_greeks.py`, `quantlib_pricer.py`, `volatility/solver.py`, `iv_builder.py`, `fred_service.py`, `portfolio_scenario.py`, strategy algorithms, divergence analysis, indicator_reliability, plus Backend services). Full pass: pending.
- **Listed but canonical file missing or moved:** 1 known so far — `app/research/options/bs_solver.py` cited by `engine-authority-map.md:27` but does not exist (F-0003).
- **Unlisted canonical math discovered in code:** 6 substantial gaps (F-0001 edge subtree, F-0002 research/signal subtree, F-0004 strategy_engine.py, F-0005 engine/options/pricer.py, F-0007 volatility subtree, F-0008 research/validation subtree). Backend services secondary enumeration and small Python services (`data_quality_service.py`, `sanitizer.py`, `dividend_service.py`, etc.) still pending — next tick.
- **Listed as canonical but no provenance block on the file:** Phase 4 work — not assessed in this run.
- **Listed with `pending-fixture` / `pending-migration` and still pending:** Per registry: Greek cross-engine parity, IV cross-engine parity, IV term-structure interpolation, trade divergence, dividend adjustment (CRSP placeholder), plus 5 known rule-5 violations enumerated in registry §"Known rule-5 non-compliance" (Phase 3 deferred, Phase 4 deferred).

## 3. Findings index

Full per-finding files live in `docs/audits/auto-research/findings/`. Sort here is **dependency-ordered, severity sub-sorted** per the recommendation plan in §5.

### 3.1 Inventory & source-of-truth gaps

| ID | Sev | Status | Area | Subject | Link |
|---|---|---|---|---|---|
| F-0001 | P1 | open | inventory | `app/engine/edge/` subtree (~25 files) — entirely unregistered; engine-authority-map declares canonical | [findings/F-0001](findings/F-0001-engine-edge-subtree-unregistered.md) |
| F-0002 | P1 | open | inventory | `app/research/signal/` subtree — unregistered; engine-authority-map declares canonical | [findings/F-0002](findings/F-0002-research-signal-subtree-unregistered.md) |
| F-0003 | P1 | **fixed-verified** | inventory | ~~`app/research/options/bs_solver.py` cited by engine-authority-map line 27 — file does not exist~~ Closed 2026-05-06: dead reference removed. | [findings/F-0003](findings/F-0003-engine-authority-map-cites-missing-bs-solver.md) |
| F-0004 | P1 | open | inventory | `app/services/strategy_engine.py::AnalyzeOptionsStrategy` — canonical per map, no registry row; outputs render to Strategy Lab UI | [findings/F-0004](findings/F-0004-strategy-engine-py-no-registry-row.md) |
| F-0005 | P1 | open | inventory | `app/engine/options/pricer.py` — undocumented in-engine pricing dispatcher (`PricingMode` QUANTLIB_ONLY/MARKET_PREFERRED/MARKET_REQUIRED) | [findings/F-0005](findings/F-0005-engine-options-pricer-undocumented.md) |
| F-0006 | P1 | **fixed-verified** | inventory | ~~Sharpe / max-drawdown / fill-model registry rows point at directory `PythonDataService/app/engine/`~~ Closed 2026-05-06: rows tightened to pinpoint files. (Provenance blocks on those files still owed via F-0027.) | [findings/F-0006](findings/F-0006-results-statistics-vague-canonical-path.md) |
| F-0007 | P1 | open | inventory | `app/volatility/` — 12 of 14 modules unregistered; includes `vix_replication.py`, `fitting.py`, `surface.py`, `basis.py`, `iv30_health.py` | [findings/F-0007](findings/F-0007-volatility-subtree-mostly-unregistered.md) |
| F-0008 | P1 | open | inventory | `app/research/validation/` — `ic.py`, `quantile.py`, `robustness.py` unregistered in both registry and authority map | [findings/F-0008](findings/F-0008-research-validation-subtree-unregistered.md) |
| F-0012 | P2 | open | inventory | 4 Backend transport-only services (`SanitizationService`, `ResearchService`, `SpecStrategyService`, `PortfolioService`) need explicit transport rows | [findings/F-0012](findings/F-0012-backend-transport-services-unregistered.md) |
| F-0013 | P2 | open | inventory | `Backend/Services/Implementation/PortfolioValidationService.cs` runtime validation suite — needs authority-map classification | [findings/F-0013](findings/F-0013-portfolio-validation-service-unregistered.md) |
| F-0014 | P2 | open | inventory | `app/services/{data_quality_service,validation_service}.py` compute QC metrics + report-tolerance thresholds — unregistered | [findings/F-0014](findings/F-0014-data-quality-validation-services-unregistered.md) |
| F-0015 | P2 | open | inventory | `app/research/features/{options_features,ta_features}.py` — feature-engineering math; `ta_features.py::compute_rsi_14` is a third RSI consumer (pandas-ta) | [findings/F-0015](findings/F-0015-research-features-unregistered.md) |
| F-0016 | P2 | open | inventory | `app/engine/strategy/algorithms/spy_strategy_{a,b,c}.py` — three RSI-range strategy variants with no registry rows | [findings/F-0016](findings/F-0016-spy-strategy-abc-unregistered.md) |
| F-0017 | P2 | open | inventory | `app/research/divergence/strategies/{s1,s2,s3}_*.py` — vectorized parallels of engine canonicals; need disposition (legacy-ok or divergence-research-only) | [findings/F-0017](findings/F-0017-divergence-strategies-parallel-implementations.md) |
| F-0018 | P2 | **fixed-verified** | inventory | ~~Phase 2.3 drift~~ Closed 2026-05-06: registry item 3 rewritten to ✅ CLOSED. | [findings/F-0018](findings/F-0018-migration-plan-vs-registry-phase-2-3-drift.md) |
| F-0029 | P2 | **fixed-verified** | inventory | ~~Hardcoded `0.043` count drift~~ Closed 2026-05-06: registry item 5 rewritten with all 6 file:line locations. (Code-side migration to FRED still deferred.) | [findings/F-0029](findings/F-0029-hardcoded-risk-free-rate-additional-locations.md) |

### 3.2 Python math-authority violations

| ID | Sev | Status | Area | Subject | Link |
|---|---|---|---|---|---|
| F-0010 | P1 | open | python-authority | `Backend/Services/Implementation/PositionEngine.cs` — FIFO lot accounting + realized PnL math in .NET, not in registry | [findings/F-0010](findings/F-0010-position-engine-fifo-accounting-in-dotnet.md) |
| F-0011 | P1 | open | python-authority | `Backend/Services/Implementation/SnapshotService.cs::ComputeDrawdownSeries` — third drawdown implementation; registry only knows about Python canonical + `BacktestService.cs` legacy | [findings/F-0011](findings/F-0011-snapshot-service-drawdown-in-dotnet.md) |

### 3.3 Timestamp boundary violations

| ID | Sev | Status | Area | Subject | Link |
|---|---|---|---|---|---|
| F-0009 | P1 | awaiting-human | timestamp | `app/services/sanitizer.py:79` emits ISO-Z string at the wire; line 57 silently drops duplicates. Cross-refs prior audit `computational-fidelity-2026-04-22.md` top-10 #1/#2. | [findings/F-0009](findings/F-0009-sanitizer-iso-timestamp-wire.md) |
| F-0019 | P1 | open | timestamp | `app/services/trade_comparison.py::_parse_ts` accepts 3 naive formats and silently `replace(tzinfo=UTC)`s them — same anti-pattern the .NET ban list calls out | [findings/F-0019](findings/F-0019-trade-comparison-naive-strptime-utc-assumption.md) |
| F-0020 | P1 | open | timestamp | **Phase 3 rollup** — 19 Python + 4 .NET + 45 TS files match ban-list patterns. Per-file triage deferred to Phase 3 ticks. Pinpoints prior-audit-known violators. | [findings/F-0020](findings/F-0020-timestamp-ban-list-rollup.md) |
| F-0021 | **P0** | open | timestamp | `MarketDataService.cs:451` (aggregate ingestion) + `StudiesApi.cs:294-298` (`ParseUtc`) — banned `AssumeUniversal\|AdjustToUniversal` pattern silently coerces naive strings to UTC | [findings/F-0021](findings/F-0021-dotnet-ingestion-datetime-parse-assumeuniversal.md) |
| F-0022 | P1 | open | timestamp | `Query.cs` (4 occurrences), `MarketDataService.cs` (date-range params, 6 occurrences), `ResearchService.cs` (2 occurrences) — `DateTime.Parse(fromDate).ToUniversalTime()` silently treats naive input as local time | [findings/F-0022](findings/F-0022-dotnet-query-parameter-datetime-parse.md) |
| F-0023 | **P0** | open | ingestion | `dataset_service.py::forward_fill_gaps` (lines 489-565) silently fills missing minute bars with prev-close + zero-volume. Default `forward_fill=True` at 4 call sites. Direct violation of fail-fast ingestion rule. | [findings/F-0023](findings/F-0023-dataset-service-forward-fill-gaps.md) |
| F-0024 | P1 | open | timestamp | More ban-list violations in Python ingestion paths: `polygon_ingest.py:226` ISO-Z emission, `dataset_service.py:851` `datetime.utcfromtimestamp`, `dataset_service.py:939/1139` `datetime.utcnow`, `polygon_client.py:625/628/676` naive `datetime.now()` | [findings/F-0024](findings/F-0024-additional-iso-z-emission-and-banned-utcfromtimestamp.md) |
| F-0033 | P1 | open | timestamp | Phase 3 Python non-ingestion rollup: 10 files in violation. ISO-Z emissions in `options_companion_service.py`, `validation_study.py`, `engine_runner.py`, `cache.py`. `pd.to_datetime` without `utc=True` in `iv_builder.py:413`, `contract_finder.py:128`. `datetime.utcnow()` in 5 places. Naive `datetime.now()` x4 in `volatility.py` router. | [findings/F-0033](findings/F-0033-python-non-ingestion-banlist-violations.md) |
| F-0034 | P1 | open | timestamp | Phase 3 TS rollup. 45 candidate files triaged into 5 tiers. Tier 1 P1: ~17 occurrences in engine-replay services treat `bar.timestamp` as a string and parse via `new Date(...)`. If wire is `int64 ms` (the goal), idempotent; if naive ISO (current per F-0009/F-0033), browser-shift bug. Resolves transitively when Step 3.1 wire-format change lands. | [findings/F-0034](findings/F-0034-frontend-naive-date-parse-rollup.md) |

### 3.4 Provenance & reference gaps

| ID | Sev | Status | Area | Subject | Link |
|---|---|---|---|---|---|
| F-0027 | P1 | open | provenance | 4-field provenance block missing across nearly all canonical math: `app/engine/indicators/` (1 of 7 files have any field), `app/services/` (1 hit only, in `strategies/lean_statistics.py`), `app/volatility/` (0 of 14 files). | [findings/F-0027](findings/F-0027-provenance-block-near-universally-missing.md) |

### 3.5 Golden fixture gaps

| ID | Sev | Status | Area | Subject | Link |
|---|---|---|---|---|---|
| F-0026 | P1 | open | fixture | Only 3 fixture directories on disk (`bs-price-cross-engine`, `iv30`, `portfolio-scenario-3leg`). Most canonical math (SMA, EMA, RSI, all strategies, Sharpe, drawdown) has no fixture. `iv30/` is missing `attribution.md`. | [findings/F-0026](findings/F-0026-fixture-coverage-gap-most-canonicals-have-no-fixture.md) |

### 3.6 Tolerance hygiene

| ID | Sev | Status | Area | Subject | Link |
|---|---|---|---|---|---|
| F-0025 | P2 | open | tolerance | Sweep nearly clean. `edge_score.py:82` bare `np.isclose`; `PositionEngineTests.cs:332` precision-4 not justified; `test_regime_clustering.py:41` missing `rtol`. | [findings/F-0025](findings/F-0025-tolerance-hygiene-rollup.md) |

### 3.7 Ingestion fidelity

| ID | Sev | Status | Area | Subject | Link |
|---|---|---|---|---|---|
| F-0023 | **P0** | open | ingestion | `dataset_service.py::forward_fill_gaps` silently fabricates missing bars with prev-close + zero-volume. Default-on at 4 call sites. (Cross-listed under §3.3 timestamp because gaps surface as ingestion + boundary issue.) | [findings/F-0023](findings/F-0023-dataset-service-forward-fill-gaps.md) |

### 3.8 Wire fidelity (Python → Backend → GraphQL → Frontend)

| ID | Sev | Status | Area | Subject | Link |
|---|---|---|---|---|---|
| F-0032 | P3 | open | wire | **Triage update 2026-05-06:** DTO audit shows `double` properties match Python's `float64` computation precision throughout. Severity dropped P2 → P3. Held open for auditability concern (loose `Dictionary<string, object?>` typing and registry doesn't document the precision contract). | [findings/F-0032](findings/F-0032-decimal-to-double-narrowing-at-wire.md) |

### 3.9 Frontend consumption / display-only violations

| ID | Sev | Status | Area | Subject | Link |
|---|---|---|---|---|---|
| F-0028 | P2 | open (triage complete) | frontend-consumption | Rollup: 108 hits across 30 TS files. **Triage update 2026-05-06:** all 8 high-suspicion files inspected — confirmed display-only / form-input parsing. No P0/P1 violations. | [findings/F-0028](findings/F-0028-frontend-numeric-parse-rollup.md) |

### 3.10 Documentation & auditability polish

| ID | Sev | Status | Area | Subject | Link |
|---|---|---|---|---|---|
| F-0030 | P2 | open | documentation | Reference notes well-covered for indicators (24 of 24); missing for ~15 strategy/statistic/portfolio rows. Including 3 `(verify)` references that need confirmation or demotion. | [findings/F-0030](findings/F-0030-reference-notes-missing-for-many-registry-cited-references.md) |
| F-0031 | P3 | open | documentation | Warmup docstring missing on `macd.py`. 5 of 7 indicators have warmup notes; rollup placeholder for any future indicator gaps. | [findings/F-0031](findings/F-0031-warmup-docstring-coverage.md) |

## 4. Coverage map

What was audited, what was skipped, and why.

| Area | Scope | Status | Notes |
|---|---|---|---|
| `PythonDataService/app/engine/` | full | — | — |
| `PythonDataService/app/services/` | full | — | — |
| `PythonDataService/app/research/` | full | — | — |
| `PythonDataService/app/routers/` | wire-fidelity only | — | — |
| `PythonDataService/tests/` | tolerance + fixture audit | — | — |
| `Backend/Services/` | math-authority + wire | — | — |
| `Backend/Models/DTOs/` | wire fidelity (timestamps, dtypes) | — | — |
| `Backend.Tests/` | tolerance hygiene only | — | — |
| `Frontend/src/app/` | consumption + display-only | — | — |
| `references/` | vendored-immutability | — | — |
| `docs/references/` | reference-note completeness | — | — |
| `docs/math-sources-of-truth.md` | inventory cross-check | — | — |
| `docs/architecture/engine-authority-map.md` | drift vs reality | — | — |
| `docs/architecture/numerical-authority-migration-plan.md` | drift vs reality | — | — |
| `.claude/rules/` | self-consistency | — | — |
| `.codex/rules/` (if present) | self-consistency | — | — |

## 5. Recommendation plan (dependency-ordered)

Concrete remediation steps, smallest-cost-first within each group. Severity tags reflect open findings as of the run-3 + run-4 + run-5 + run-6 set (28 findings, 2 P0 / 17 P1 / 9 P2).

### Step 1 — Canonical math inventory / source-of-truth gaps

**Smallest-edit items first** (1-line registry edits):

1.1 (P1, 1-line) — Fix `engine-authority-map.md:27` — the `bs_solver.py` reference that doesn't exist (F-0003). Trim the dead reference.

1.2 (P2, 1-line per item) — Update registry's "Known rule-5 non-compliance" item 3 to reflect Phase 2.3 shipped (F-0018).

1.3 (P1, registry edits across multiple rows) — Replace directory-only canonical paths with pinpoint files (F-0006): Sharpe → `engine/results/statistics.py`; max drawdown → same; bar consolidation → `engine/consolidators/...`; fill models → `engine/execution/...`.

**Larger inventory work** (per-row registry additions):

1.4 (P1, ~25 row additions) — Add concept rows for `app/engine/edge/` subtree (F-0001) — VRP, realized vol, regime clustering, edge score, etc. Each row needs a Reference (paper or internal) and Validated-against (test or `NONE — pending`).

1.5 (P1, ~8 row additions) — Add concept rows for `app/research/signal/` (F-0002) and `app/research/validation/` (F-0008) — IC, walk-forward, quantile, robustness.

1.6 (P1, ~12 row additions) — Add concept rows for `app/volatility/` (F-0007) — surface fitting (cite SVI/SABR), VIX replication (cite Demeterfi-Derman-Kamal-Zou), basis, IV30, normalization, conventions.

1.7 (P1, 1 row each) — Add row for `app/services/strategy_engine.py` (F-0004), `app/engine/options/pricer.py` (F-0005).

1.8 (P2, transport rows) — Add transport-only rows for 4 Backend services (F-0012); validation-only row for `PortfolioValidationService.cs` (F-0013).

1.9 (P2, ~7 rows) — Cover `data_quality_service.py` + `validation_service.py` (F-0014); `options_features.py` + `ta_features.py` with RSI duplicates note (F-0015); `spy_strategy_a/b/c.py` (F-0016); divergence s1/s2/s3 disposition (F-0017).

### Step 2 — Python math-authority violations

2.1 (P1) — Classify `PositionEngine.cs` FIFO accounting (F-0010). Decide: legacy-ok with parity test, or move to Python.
2.2 (P1) — Classify `SnapshotService.cs::ComputeDrawdownSeries` (F-0011) as duplicate of `engine/results/statistics.py`. Add to registry's max-drawdown row, status pending-migration.

### Step 3 — Timestamp boundary violations  (cluster of cluster of related fixes)

**Wire-format change (the foundational fix that enables several other closes):**

3.1 (P0 + sequencing) — Change the Python ↔ .NET wire format from ISO strings to `int64 ms UTC` for every timestamp field. Closes F-0009 (sanitizer ISO-Z), F-0021 (.NET ingestion `AssumeUniversal|AdjustToUniversal`), F-0024 (Python ban-list locations including `polygon_ingest.py:226`, `dataset_service.py:851`).

3.2 (P0) — Same-PR-as-3.1: replace the dataset_service `forward_fill_gaps` default (F-0023). Make synthetic-bar generation opt-in. Surface gaps in response payload.

3.3 (P1) — Replace `DateTime.Parse(fromDate).ToUniversalTime()` query-parameter parsing (F-0022) with `DateTimeOffset.ParseExact("yyyy-MM-dd", InvariantCulture)`. 12 occurrences across `Query.cs`, `MarketDataService.cs`, `ResearchService.cs`.

3.4 (P1) — Fix `trade_comparison.py::_parse_ts` (F-0019). Drop naive formats; require explicit offset or accept `int` ms-epoch.

3.5 (P2 + per-file triage) — TS `new Date(<var>)` mass triage (45 candidate files in F-0020). Most are display-only. Confirm + roll up.

### Step 4 — Provenance & reference gaps

4.1 (P1) — Add 4-field provenance block to canonical math files (F-0027). Recommended: bulk PR for `app/engine/indicators/` (7 files, mechanical) + bulk PR for the 5 named services (`bs_greeks`, `quantlib_pricer`, `strategy_engine`, `portfolio_scenario`, `fred_service`); burn-down-on-touch for the rest.

### Step 5 — Golden fixture gaps

5.1 (P2, 1 file) — Add `attribution.md` to `iv30/` fixture.
5.2 (P1, multi-week) — Backfill golden fixtures for canonical math marked `pending-fixture` in registry (F-0026). Per the registry's own burn-down rule, this is touch-driven, not all-at-once.

### Step 6 — Tolerance hygiene

6.1 (P2) — Three-line fix per F-0025 — make `edge_score.py:82` explicit, document `PositionEngineTests.cs:332` precision-4 choice, add `rtol=0` to `test_regime_clustering.py:41`.

### Step 7 — Ingestion fidelity

7.1 — Subsumed by Step 3.1 + 3.2 (the forward-fill default and the wire-format change close most of this).

### Step 8 — Wire fidelity (Python → Backend → GraphQL → Frontend)

8.1 — **Not swept** in this baseline. Owed in the next round of ticks. Phase 8 needs per-canonical-output tracing — defer until Steps 1–4 reduce the surface.

### Step 9 — Frontend consumption / display-only

9.1 (P2 + per-file triage) — F-0028 rollup. Triage 8 high-suspicion files first.

### Step 10 — Documentation & auditability polish

10.1 — Owed: cross-check that `docs/references/<name>.md` exists for every reference cited in the registry (F-0007 implies many are missing for the volatility subtree); confirm warmup docstrings on every indicator (F-0027 implies many are missing).

---

**Strategic notes:**

- **Step 3.1 is the highest-leverage fix** — closing the wire format simultaneously closes 5 findings (F-0009, F-0021, F-0024, parts of F-0019, F-0020 .NET subset) and removes a class of future regression. It is also the most invasive change.
- **Step 1 looks like a lot of small registry edits** because it is — but the actual math is all already on disk. The work is documenting reality.
- **Step 4 (provenance) is the largest by file count** but each file edit is mechanical. A scripted PR could touch 10+ files at once.
- **The §6 hardening gate** has 10 boxes. Closing F-0023 + F-0021 (the two P0s), F-0027 (provenance block universally), F-0026 (fixture coverage), and the wire-format Step 3.1 covers ~6 of them. The other 4 (warmup docstrings, reference-note completeness, sovereignty no-runtime-references) are smaller.

_Phases 2 (math-authority deeper sweep), 8 (wire fidelity), and 10 (doc polish) are owed in subsequent ticks; the headlines are captured but not exhaustively swept._

## 6. Definition of "rigor restored" (the hardening gate)

The nightly auto-research cron is **not** scheduled until every box below is checked. This is the contract.

- [ ] All P0 findings closed (`fixed-verified`).
- [ ] All P1 findings closed or `deferred` with a documented reason in the per-finding doc.
- [ ] `docs/math-sources-of-truth.md` is regenerated, reviewed, and matches the actual code.
- [ ] Every canonical math file in the registry carries the 4-field provenance block (`Formula` / `Reference` / `Canonical implementation` / `Validated against`).
- [ ] Every entry marked `pending-fixture` in the registry has either a fixture or an explicit `deferred` row in this doc.
- [ ] Tolerance audit clean: no `np.allclose` / `np.isclose` without explicit `atol` and `rtol` in canonical-math tests; loosened tolerances justified in their docstring or test file.
- [ ] Timestamp ban-list grep clean across `PythonDataService/`, `Backend/`, `Frontend/src/`.
- [ ] Reference notes (`docs/references/<name>.md`) exist for every reconciled port.
- [ ] Warmup behavior is documented in the module docstring of every indicator.
- [ ] No runtime imports from `references/`.

## 7. Runs

| # | Date | Phase(s) touched | Findings opened | Findings closed | Notes |
|---|---|---|---|---|---|
| 1 | 2026-05-05 | 1 (partial) | 8 (F-0001..F-0008, all P1) | 0 | Phase 1 inventory: major subtree gaps + authority-map drift identified. Backend secondary inventory and migration-plan drift check deferred to next tick. |
| 2 | 2026-05-05 | 1 (substantially complete), 3 (grep prep) | 12 (F-0009..F-0020 — 5 P1 + 7 P2) | 0 | Phase 1 continuation: Backend secondary services classified, PythonDataService secondary services classified, divergence parallels found, migration-plan drift confirmed. Phase 3 ban-list grep run cross-stack (rolled up in F-0020). Per-file Phase 3 triage deferred. |
| 3 | 2026-05-06 | 3 (.NET subset) | 2 (F-0021 P0 + F-0022 P1) | 0 | Phase 3 .NET triage of F-0020's 4 candidates. Both ingestion-path occurrences (`MarketDataService.cs:451` + `StudiesApi.cs:294-298 ParseUtc`) confirmed P0 — banned `AssumeUniversal\|AdjustToUniversal` pattern. Query-parameter occurrences (Query.cs, MarketDataService.cs date-range, ResearchService.cs) consolidated into one P1. **First P0 of the baseline.** |
| 4 | 2026-05-06 | 3 (Python ingestion subset), 5, 6 | 4 (F-0023 P0 + F-0024 P1 + F-0025 P2 + F-0026 P1) | 0 | Phase 3 Python ingestion triage; Phase 5 fixture audit; Phase 6 tolerance audit. F-0023 forward-fill in `dataset_service.py` is **second P0**. Fixture coverage shockingly thin (3 fixtures on disk vs dozens of canonical math rows). Tolerance audit largely clean. |
| 5 | 2026-05-06 | 4 (provenance), 9 (frontend rollup) | 2 (F-0027 P1 + F-0028 P2) | 0 | Phase 4 reveals near-universal absence of 4-field provenance block. Phase 9 grep returns 108 candidate hits across 30 TS files — rolled up. §5 recommendation plan populated. **End of overnight burn.** |
| 6 | 2026-05-06 | 2 (verification), 9 (sample), 10 (sweep) | 3 (F-0029 P2 + F-0030 P2 + F-0031 P3) | 0 | Phase 2 item 2 verified clean (only 2 production callers of `black-scholes.ts`). Phase 2 item 5 has 6 hardcoded `0.043` constants vs registry's count of 4 (F-0029). Phase 9 sample of lean-engine.component.ts confirms F-0028 severity classification (mostly display-only). Phase 10 sweep finds reference notes for indicators well-covered, ~15 missing for strategy/stat/portfolio rows (F-0030); MACD warmup docstring missing (F-0031, first P3). |
| 7 | 2026-05-06 | 8 (sample) | 1 (F-0032 P2) | 0 | Phase 8 sample touches: PolygonService.cs casts `decimal → double` on every Python request (~15 occurrences across single-leg + multi-leg shapes). 3 DTO files have `double`/`float` properties for inbound. Direction matters — outbound is parameter-narrowing (acceptable), inbound is canonical-narrowing (P1-candidate, P2 here pending per-DTO-file audit). |
| 8 | 2026-05-06 | 3 (Py non-ingestion full), 4 (full grep), 8 (DTOs full), 9 (high-suspicion full) | 1 (F-0033 P1) | 0 | **Per-file triage round.** Phase 3 Python non-ingestion: 10 of 14 files confirmed in violation (rollup F-0033). Phase 4 grep across `PythonDataService/app/`: only 2 files have any 4-field marker (confirms F-0027 even more starkly). Phase 8 DTO audit: severity drops P2 → P3 (DTOs match Python `float64`). Phase 9 high-suspicion files: all display-only or legitimate parsing; no P0/P1 surprises. |
| 9 | 2026-05-06 | 3 (TS full triage) | 1 (F-0034 P1) | 0 | **Phase 3 TS triage complete.** 45 candidate files classified into 5 severity tiers. Tier 1 P1 = ~17 engine-replay-service occurrences contingent on producer-side wire fix. Tier 2 P2 = date-only query params (mostly benign). Tier 3 P3 = display-only (out of scope). Tier 4 P3 = `engine-chart.component.ts:345` defensive band-aid (good pattern; informal evidence of producer bug). Tier 5 = tests/mocks (skipped). |

Per-run summaries in `docs/audits/auto-research/runs/YYYY-MM-DD.md` (created on first run).

## 8. Methodology

- **Read-only.** This baseline does not edit production code, tests, or fixtures. The only writes are under `docs/audits/auto-research/`.
- **Vendored references only.** External fetches require explicit human approval (recorded in the relevant per-finding doc with URL + commit/tag + reason).
- **Static-first.** Targeted `pytest -k` / `dotnet test --filter` / `vitest run -t` only when a static finding needs verification. Full test suites only at the end of a run if writing tests is later authorized.
- **Container-aware.** If a container is required for a check and it's down, the check is recorded as `not run, container down` rather than skipped silently.
- **Resumable.** State lives in `state.json`; runs may span multiple nights. Findings are deduplicated by `(area, file, finding_type)`.

## 9. Severity taxonomy

- **P0** — Active numerical corruption or timestamp boundary violation in a live/canonical path; parity failure above documented tolerance on deployed canonical math.
- **P1** — Missing provenance or golden fixture for canonical math; tolerance loosened without justification; non-Python layer computing authoritative math without a parity-tested mirror.
- **P2** — Missing/weak attribution, stale fixture, incomplete reference note, weak edge-case coverage, suspicious dtype drift.
- **P3** — Documentation polish, naming, minor auditability improvements where math is currently correct. Rolled up in `findings/P3-rollup.md` rather than per-finding files.

## 10. Out of scope

The baseline does **not** examine:

- Frontend visual regression / styling
- General UI polish
- Performance / latency
- Security review
- Broad strategy profitability or correctness
- Live trading behavior
- Dependency upgrades
- Refactors unrelated to numerical fidelity

Strategy logic is in scope **only** when it reveals a math-authority violation, a timestamp violation, or a primitive-calculation parity issue.

## 11. Remediation log

_Append-only. One row per finding closed. The baseline is **frozen** once the hardening gate is clear; this section is the only one that grows after that point._

| Date | Finding | Closed by | Verification | Commit / PR |
|---|---|---|---|---|
| 2026-05-06 | F-0003 (P1) — engine-authority-map cites missing bs_solver.py | Removed dead reference at `engine-authority-map.md:27` | grep `bs_solver` returns no matches in `docs/` | this commit |
| 2026-05-06 | F-0006 (P1) — Sharpe/MaxDrawdown/Bar-consolidation registry rows used directory-only paths | Tightened to specific files: `engine/results/statistics.py` for stats; `engine/engine.py` + `consolidators/` + `execution/` split for the bar-consolidation row | grep `app/engine/\` \|` (directory-only) returns no matches | this commit |
| 2026-05-06 | F-0018 (P2) — Phase 2.3 drift (registry partial vs migration plan shipped) | Item 3 of registry rewritten to ✅ CLOSED, citing commit `334d419` (2026-04-27) | Both docs now agree | this commit |
| 2026-05-06 | F-0029 (P2) — Hardcoded `0.043` count drift (registry knew 4, actual is 6) | Item 5 of registry rewritten with all 6 production locations + file:line | Diff visible at `docs/math-sources-of-truth.md` § Known rule-5 non-compliance | this commit |
