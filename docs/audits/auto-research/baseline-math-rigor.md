# Math rigor baseline — learn-ai

**Status:** in-progress
**Started:** 2026-05-05
**Last updated:** 2026-05-06
**Run count:** 3
**Generator:** `.claude/skills/auto-research-tick` (baseline mode)

> This document is **frozen** once the baseline completes. Live state moves to a separate `current-state.md` after hardening. Do not edit this doc by hand once frozen except to append entries to the **Remediation log** at the bottom.

## 0. Executive summary

_Filled at the end of the first sweep and updated after every subsequent run that closes the loop on a phase. Running tallies live here; details live below._

| Severity | Open | Deferred | Closed | Total |
|---|---|---|---|---|
| P0 | 1 | 0 | 0 | 1 |
| P1 | 14 | 0 | 0 | 14 (1 status=awaiting-human) |
| P2 | 7 | 0 | 0 | 7 |
| P3 | 0 | 0 | 0 | 0 |

**Files audited:** Phase 1 substantially complete — registry rows cross-checked; major engine/research/volatility subtrees inventoried; Backend secondary services + Python secondary services + parallel strategy implementations + migration-plan drift verified. Phase 3 ban-list grep run cross-stack (rolled up in F-0020).
**Files skipped:** Per-file triage of the F-0020 ban-list candidates deferred to Phase 3 ticks.
**Phases complete:** 1/10 substantially (Phase 1 cursor advanced to "Phase 1 complete pending P3 rollup classification"); Phase 3 grep prep done; Phases 2/4/5/6/7/8/9/10 not yet swept.

## 1. Posture vs. `.claude/rules/numerical-rigor.md`

For each rule, one row: **holding** / **violated** / **partial**, with the count of supporting findings and a one-line summary.

| Rule | Holding? | Findings | Notes |
|---|---|---|---|
| Equivalence levels declared per port | TBD | — | Phase 4 |
| Golden fixtures present and attributed | TBD | — | Phase 5 |
| Tolerances explicit (no default `np.allclose`) | TBD | — | Phase 6 |
| Tolerances justified when loosened | TBD | — | Phase 6 |
| Timestamp canonical format `int64 ms UTC` at all boundaries | **violated** | F-0009, F-0019, F-0020 | sanitizer emits ISO-Z at wire; trade_comparison silently UTC-stamps naive strings |
| Timestamp ban-list clean (Python) | **violated** | F-0020 | 19 candidate files; sanitizer + rule_based_backtest + trade_comparison confirmed |
| Timestamp ban-list clean (.NET) | **violated** | F-0020, F-0021 (P0), F-0022 | All 4 candidate files confirmed violators; 2 are ingestion-path P0 |
| Timestamp ban-list clean (TypeScript) | **partial** | F-0020 | 45 candidate files; mostly display/test but ~10 cross-wire surfaces need triage |
| Fail-fast ingestion (no silent dedup / forward-fill) | TBD | — | Phase 7 |
| Sovereignty (no runtime calls into `references/`) | TBD | — | Phase 4 |
| Math Provenance Contract: 4-field block on canonical math | TBD | — | Phase 4 |
| Single canonical per concept (no silent duplicates) | **partial** | F-0001/F-0002/F-0004/F-0005/F-0007/F-0008 | Multiple unregistered canonical math subtrees discovered |
| Authority hierarchy: Python is the home of canonical math (rule 5) | **partial** | F-0010, F-0011 | PositionEngine FIFO + SnapshotService drawdown both compute math in .NET; not registered as legacy-ok |
| Warmup behavior documented per indicator | TBD | — | Phase 10 |
| Reconciliation reports exist for reconciled ports | TBD | — | Phase 10 |

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
| F-0003 | P1 | open | inventory | `app/research/options/bs_solver.py` cited by engine-authority-map line 27 — file does not exist | [findings/F-0003](findings/F-0003-engine-authority-map-cites-missing-bs-solver.md) |
| F-0004 | P1 | open | inventory | `app/services/strategy_engine.py::AnalyzeOptionsStrategy` — canonical per map, no registry row; outputs render to Strategy Lab UI | [findings/F-0004](findings/F-0004-strategy-engine-py-no-registry-row.md) |
| F-0005 | P1 | open | inventory | `app/engine/options/pricer.py` — undocumented in-engine pricing dispatcher (`PricingMode` QUANTLIB_ONLY/MARKET_PREFERRED/MARKET_REQUIRED) | [findings/F-0005](findings/F-0005-engine-options-pricer-undocumented.md) |
| F-0006 | P1 | open | inventory | Sharpe / max-drawdown / fill-model registry rows point at directory `PythonDataService/app/engine/` instead of `app/engine/results/statistics.py` and `app/engine/execution/*.py` | [findings/F-0006](findings/F-0006-results-statistics-vague-canonical-path.md) |
| F-0007 | P1 | open | inventory | `app/volatility/` — 12 of 14 modules unregistered; includes `vix_replication.py`, `fitting.py`, `surface.py`, `basis.py`, `iv30_health.py` | [findings/F-0007](findings/F-0007-volatility-subtree-mostly-unregistered.md) |
| F-0008 | P1 | open | inventory | `app/research/validation/` — `ic.py`, `quantile.py`, `robustness.py` unregistered in both registry and authority map | [findings/F-0008](findings/F-0008-research-validation-subtree-unregistered.md) |
| F-0012 | P2 | open | inventory | 4 Backend transport-only services (`SanitizationService`, `ResearchService`, `SpecStrategyService`, `PortfolioService`) need explicit transport rows | [findings/F-0012](findings/F-0012-backend-transport-services-unregistered.md) |
| F-0013 | P2 | open | inventory | `Backend/Services/Implementation/PortfolioValidationService.cs` runtime validation suite — needs authority-map classification | [findings/F-0013](findings/F-0013-portfolio-validation-service-unregistered.md) |
| F-0014 | P2 | open | inventory | `app/services/{data_quality_service,validation_service}.py` compute QC metrics + report-tolerance thresholds — unregistered | [findings/F-0014](findings/F-0014-data-quality-validation-services-unregistered.md) |
| F-0015 | P2 | open | inventory | `app/research/features/{options_features,ta_features}.py` — feature-engineering math; `ta_features.py::compute_rsi_14` is a third RSI consumer (pandas-ta) | [findings/F-0015](findings/F-0015-research-features-unregistered.md) |
| F-0016 | P2 | open | inventory | `app/engine/strategy/algorithms/spy_strategy_{a,b,c}.py` — three RSI-range strategy variants with no registry rows | [findings/F-0016](findings/F-0016-spy-strategy-abc-unregistered.md) |
| F-0017 | P2 | open | inventory | `app/research/divergence/strategies/{s1,s2,s3}_*.py` — vectorized parallels of engine canonicals; need disposition (legacy-ok or divergence-research-only) | [findings/F-0017](findings/F-0017-divergence-strategies-parallel-implementations.md) |
| F-0018 | P2 | open | inventory | `math-sources-of-truth.md` § "Known rule-5 non-compliance" item 3 says Phase 2.3 partial; migration plan says Phase 2.3 shipped 2026-04-27 (commit `334d419`). Drift. | [findings/F-0018](findings/F-0018-migration-plan-vs-registry-phase-2-3-drift.md) |

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

### 3.4 Provenance & reference gaps
_(none yet)_

### 3.5 Golden fixture gaps
_(none yet)_

### 3.6 Tolerance hygiene
_(none yet)_

### 3.7 Ingestion fidelity
_(none yet)_

### 3.8 Wire fidelity (Python → Backend → GraphQL → Frontend)
_(none yet)_

### 3.9 Frontend consumption / display-only violations
_(none yet)_

### 3.10 Documentation & auditability polish
_(none yet)_

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

The order matters: each step unblocks the next. Severity is the sub-sort within each step.

1. **Canonical math inventory / source-of-truth gaps** — fix `docs/math-sources-of-truth.md` first; everything downstream depends on a correct registry.
2. **Python math-authority violations** — every authoritative number must have its canonical in Python (rule 5) or carry an explicit, parity-tested justification.
3. **Timestamp boundary violations** — `int64 ms UTC` at every wire and storage point; ban-list clean across all layers.
4. **Provenance & reference gaps** — every canonical math file carries the 4-field block.
5. **Golden fixture gaps** — every canonical math has a fixture under `tests/fixtures/golden/<name>/` with attribution.
6. **Tolerance hygiene** — every float comparison declares `atol`/`rtol`; loosened tolerances are justified.
7. **Ingestion fidelity** — Polygon/IBKR ingestion preserves timestamp, dtype, ordering, monotonicity, and surfaces duplicates rather than silencing them.
8. **Wire fidelity** — Python → Backend → GraphQL → Frontend signal preserves the value without recomputation, narrowing, or string mutation.
9. **Frontend consumption / display-only violations** — UI displays without recomputing; `DatePipe` / `toFixed` / chart formatters are display-only and never round-tripped.
10. **Documentation & auditability polish** — reference notes complete, reconciliation reports present, warmup documented per indicator.

_Per-step recommendations populate after the first sweep._

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
