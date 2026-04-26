# Handoff: Vol-Surface Dashboard — Audit Verification & TDD Rewrite

**From:** Claude (web chat session, 2026-04-25)
**To:** Claude Code (running locally on Inkant's machine)
**Repo:** learn-ai (Inkant Awasthi)
**User:** Inkant — Software Developer at Alpine Testing Solutions, building learn-ai as a quant research/trading platform on the side

---

## What this conversation produced

Three artifacts you should pull before starting:

1. **`vol-surface-dashboard-tdd.md`** — Initial TDD covering three build paths (full / MVP / API-only). The math sections (§3), Angular plan (§6), and test fixture plan (§7) are durable. The data-layer sections (§4-5) are now known to be wrong because they didn't account for existing infrastructure.

2. **`learn-ai-options-audit.md`** — Audit of what already exists in the repo. Performed by reading a fresh `git clone` of master. Found ~60-70% of the planned work already exists. This document is the new ground truth.

3. **This handoff** — what to do next.

If Inkant doesn't have those files locally, ask him to download them from the conversation and place them in `docs/architecture/` (or wherever feels right).

---

## Current state of the project

**Goal:** Build a volatility surface & regime dashboard for SPY and QQQ. On-demand refresh, Angular UI, FastAPI + Hot Chocolate GraphQL backend. The user wants it for context-aware structure selection in manual small-size live options trading.

**Decisions locked in across the conversation:**

- **Scope:** SPY + QQQ only for v1. Other tickers (IWM, AAPL, MSFT, NVDA, TSLA, GLD, SLV) deferred to v1.1.
- **Path:** Path 2 (MVP) recommended over Path 1 (full backfill) and Path 3 (API only). User said "give me the TDD covering all three" so we kept all three documented, but the analysis pointed strongly to Path 2.
- **Polygon tier:** Options Starter — unlimited calls, 2 years of history.
- **Caching philosophy:** Read-through cache, serve from DB if present, stale-check in background, refetch and update if changed. ARQ or similar real background-task system, not in-process asyncio.
- **Storage:** Same Postgres instance, namespaced via Postgres schemas (`market_data.*`, `options.*`, `analytics.*`, `cache.*`). User asked for a recommendation and accepted this.
- **Underlying-bars migration:** Yes, migrate existing bars to the new schema layout in the same PR.
- **Output format:** Web dashboard in Angular UI (rules out Path 3).
- **Cadence:** On-demand, not scheduled refresh.
- **Theory-level for the TDD:** "Familiar with concepts, fuzzy on math" — formulas with one-paragraph explanations and Hull/Gatheral citations. Not a textbook chapter.

**Decisions still open:**

The audit produced four open questions for Inkant. They block the TDD rewrite:

1. Is `app/research/options/iv_builder.py` invoked on a schedule, or only on-demand when a research report is requested? Need to check the .NET caller (`Backend/Services/Implementation/ResearchService.cs`) and any scheduled-task config (Hangfire? Quartz? cron?).

2. What does `Frontend/src/app/components/options-history/` currently render? Does it already plot 30-day IV over time? If so, parts of the new dashboard reduce to "add panels alongside what exists."

3. Two `implied_volatility` functions exist: `app/volatility/solver.py` (QuantLib + Brent, well-tested) and `app/research/options/bs_solver.py` (separate impl). Both used in production. Was this intentional? This violates `CLAUDE.md` § 5 and should be resolved before adding more IV-dependent code.

4. Did the audit miss anything? Modules not searched for, recent commits that change the picture, schema details visible only in the live database.

---

## Your job, Claude Code

**Phase A — Verify the audit (~30-60 minutes):**

1. Read `learn-ai-options-audit.md` end-to-end. It's based on the public master at the time of clone. Check that:
   - The IV solver inventory matches reality (`app/volatility/solver.py`, callers list, the duplicate in `app/research/options/bs_solver.py`)
   - `OptionsIvSnapshot` writers in `Backend/Services/Implementation/ResearchService.cs:516` still exist and look as described
   - The volatility router endpoints listed (build, build-from-ticker, grid, smiles, diagnostics, query, export, batch-summary) are still mounted in `app/main.py`
   - No `garman_klass` / `GK` / `realized vol` Garman-Klass implementation exists (close-to-close exists in `app/research/features/ta_features.py`)
   - The Polygon read-through cache truly does NOT exist (only request throttling does)

2. Answer the four open questions:
   - **Q1 (scheduled iv_builder):** Search Backend for Hangfire, Quartz, IHostedService, BackgroundService, RecurringJob, or any cron-style scheduling. Also check `compose.yaml` and any deployment config. Report whether iv_builder runs on a schedule or only on-demand.
   - **Q2 (options-history component):** Read `Frontend/src/app/components/options-history/*.ts` and `*.html`. Summarize what it shows, what data it fetches, what charts it renders. Note any overlap with what the new dashboard would display.
   - **Q3 (duplicate solver):** Diff `app/volatility/solver.py::implied_volatility` against `app/research/options/bs_solver.py::implied_volatility`. Same algorithm? Different conventions? Same author? Look at git blame to see if one was meant to deprecate the other. Make a recommendation: consolidate, document the split, or leave alone.
   - **Q4 (missed items):** Run a fresh search for things the audit might have skipped:
     - `grep -rn "implied_volatility\|surface\|skew\|term_structure\|iv_rank\|vol_regime"` across the whole repo
     - `git log --since="3 months ago" --oneline` — recent activity that changes the picture
     - List anything substantial that I missed

3. **Live DB check:** Run against the actual Postgres:
   ```sql
   -- Existing data inventory
   SELECT COUNT(*), MIN("TradingDate"), MAX("TradingDate")
   FROM "OptionsIvSnapshots";

   SELECT COUNT(*), MIN("Timestamp"::date), MAX("Timestamp"::date)
   FROM "StockAggregates"
   WHERE "TickerId" IN (SELECT "Id" FROM "Tickers" WHERE "Symbol" IN ('SPY', 'QQQ'));

   -- Schema check
   \dt
   ```

4. Produce a delta document — `learn-ai-options-audit-delta.md` — covering:
   - Confirmed findings (audit was right)
   - Corrected findings (audit was wrong, here's what's actually true)
   - New findings (audit missed these)
   - Answers to the four open questions
   - Live DB state (counts, date ranges, existing schema)
   - Recommendation for whether to proceed with TDD rewrite or address cleanup first

**Phase B — TDD rewrite (~1-2 hours, after Inkant reviews the delta):**

Rewrite `vol-surface-dashboard-tdd.md` against verified reality. Specifically:

- **§ 4 (Data layer):** Now includes the unified Polygon read-through cache, ARQ-based background staleness, Postgres schema-namespacing strategy, and the migration plan for existing `StockAggregates` → `market_data.underlying_bars_daily`. Existing `OptionsIvSnapshot` becomes layer-1 cache; new `analytics.surface_metrics_daily` table is layer-2.

- **§ 5 (Module layout):** Drop the `app/services/options_iv/` package — the IV inverter and snapshot fetcher already exist. New code is mostly:
  - `app/services/polygon_cache/` — the read-through cache abstraction
  - `app/services/options_iv/parity_dividend.py` — forward-implied dividend yield (the one piece of IV math that's genuinely new)
  - `app/services/options_iv/rate_curve.py` — multi-tenor rate interpolation
  - `app/volatility/realized_vol.py` — Garman-Klass
  - `app/volatility/percentiles.py` — IV-rank, IV-percentile
  - `app/volatility/regime.py` — rule-based classifier
  - `app/volatility/persistence.py` — DB writer for surface_metrics_daily

- **§ 8 (Phasing):** Update the timeline. Audit revealed less work than expected for IV inversion, but caching layer is genuinely net-new — net effect roughly cancels out. MVP estimate ~2.5-3.5 weeks.

- **Pre-TDD-implementation cleanup:** Add a § 0 (or appendix) covering:
  - Resolve duplicate IV solver
  - Document the disk-cache-vs-DB-persistence boundary in CLAUDE.md
  - Document the existing iv_builder pipeline before extending it

- **Sections that don't change:**
  - § 3 (Math foundations) — formulas with citations all stand
  - § 6 (Angular dashboard) — component design stands; verify against existing options-history component first
  - § 7 (Test/fixture plan) — golden-fixture strategy stands
  - § 9 (Open questions for review) — most resolved, some remain

**Phase C — Begin implementation (only after Inkant signs off on the rewritten TDD):**

Don't start coding until Inkant has reviewed the rewritten TDD. The build order, once approved:

1. Cleanup phase: resolve duplicate solver, document boundaries
2. Schema migrations + Postgres schema namespacing
3. Polygon read-through cache abstraction (foundational — everything depends on it)
4. ARQ background staleness mechanism
5. Migrate underlying-bars fetcher onto the new cache
6. Multi-tenor IV extraction extending existing 30d
7. Garman-Klass realized vol
8. IV-rank / IV-percentile (returns null/partial in Path 2 until history accumulates)
9. Regime classifier
10. FastAPI endpoints
11. GraphQL schema
12. Angular dashboard

---

## Critical context: things to know about the codebase

These are repo conventions you must respect, drawn from `CLAUDE.md` and observed patterns:

- **Math lives in Python.** `.NET` is transport. Angular renders. Never compute in C# or TypeScript.
- **One authority per number.** Don't add a third IV solver. Use `app/volatility/solver.py`.
- **Timestamps at boundaries are int64 ms UTC.** No ISO strings in API contracts. The original TDD had `datetime.utcnow().isoformat()` — that's wrong; correct it on rewrite.
- **Snake_case JSON.** Pydantic models, FastAPI request/response.
- **Numerical rigor:** every new computational module needs a golden fixture in `tests/fixtures/golden/`, an attribution file in `docs/references/`, tolerance-pinned tests. Reference impls: py_vollib for IV, QuantLib for Greeks, `arch` library or Sinclair textbook for realized vol.
- **PrimeNG v20 + Angular signals** for new UI. Standalone components, OnPush, zoneless.
- **lightweight-charts** is the chart library for time-series (already used in PayoffChart). recharts acceptable for non-time-series.
- **GraphQL via Hot Chocolate** in .NET. Apollo Angular on the client. Resolvers are thin passthroughs to FastAPI; no math in C#.
- **Don't break existing callers.** `polygon_client.py`, `volatility/solver.py`, `volatility/surface.py` are all in active production use. Wrap, don't modify.

---

## What success looks like at each phase

**Phase A done when:** Inkant has read the delta document, confirmed the picture, and answered "yes proceed with TDD rewrite" or "address X first."

**Phase B done when:** Inkant has read the rewritten TDD, the open questions list is short or empty, and he says "build it."

**Phase C done when:** the dashboard renders SPY and QQQ surface metrics on-demand with regime classification, all golden fixtures pass, integration tests with live Polygon pass, the user can place an informed structure-selection trade based on what he sees on screen.

---

## Tone & approach guidance

A few things from the conversation that will help you work with Inkant well:

- He values **directness over hedging.** When something is wrong, say so. When the original plan won't work, say so. He explicitly thanked the previous session for being direct about the original screener plan's flaws.
- He's **honest about gaps in his own memory.** He told us "I clearly forgot what I'd built, I'd rather be honest about that." Treat this as an asset — he's not defensive, he's collaborative.
- He has a **Ph.D. and academic engineering background.** Don't dumb down math; do explain conventions and unit choices.
- He **asks for help with prompts and ambitious plans frequently.** Be willing to push back on scope. The original screener plan was 5x bigger than it should have been; the audit revealed similar over-scoping. Trust your judgment to recommend smaller.
- **Ask questions before designing.** This conversation made progress because we asked the user 15+ questions across 6 rounds. Don't assume — verify.
- **Check the cloned repo before assuming what exists.** The previous Claude almost designed a parallel system. The audit caught it. You should keep that habit.

---

## Open question for Inkant before Phase A

Just one: **does the audit document at `learn-ai-options-audit.md` accurately describe what's in the repo, modulo the four flagged open questions?** If yes, proceed with Phase A. If there's something obviously wrong on first read, fix that first before going deeper.

Good luck. The math is solid; the architecture is mostly already built; the work that remains is real but bounded. Don't let it sprawl.
