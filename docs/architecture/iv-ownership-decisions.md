# IV Ownership Plan — Decisions Made in User's Absence

**Status:** Decisions log, recorded 2026-04-27.
**Author:** Claude (acting as architect/dev/analyst per user grant).
**Plan reference:** [`iv-ownership-plan.md`](./iv-ownership-plan.md).
**Scope of this PR:** Steps A and B of the 7-step plan.

This document records:

1. Answers to the §6 open questions, with the reasoning.
2. What landed in this PR vs. what was deferred and why.
3. The order I recommend for follow-up PRs, with file pointers.

---

## 1. Answers to §6 open questions

These are decisions, not proposals. If any of them is wrong, override and I'll reverse on the follow-up.

### Q1 — Recorder snapshot schedule

**Decision:** **09:35 / 12:30 / 16:00 ET** (the plan's default).

**Why:** the plan's own rationale already does the work — three samples is the elbow on cost vs. sampling-bias reduction (Round 3 issue #3). Two would be a 2× bias improvement over close-only; four+ has marginal returns. 09:35 dodges the opening 5-minute imbalance. 16:00 captures the print. 12:30 is mid-session and away from any London/Asia-handoff weirdness.

**How to apply:** the recorder's slot table is configurable in code; if a future operator wants four slots, they edit one constant.

### Q2 — Recorder execution host

**Decision:** **External cron orchestrated by the .NET `JobsController`, which calls a new POST endpoint on the Python service.**

**Why:** the existing pattern in this repo is exactly this — `app/routers/jobs.py` documents that .NET mints `job_id`s, coordinates via Redis, and Python runs in `app/jobs/runner.py:run_in_thread()` and emits progress to Redis. Putting the recorder on this rail keeps:

- One operational story for "what's running on a schedule": .NET.
- Recoverability: a missed slot is a re-fired job, not a process restart.
- Observability: existing job dashboards see it.

**Rejected alternative:** in-process `apscheduler` in FastAPI. Simpler one-time setup, but couples reliability to FastAPI uptime, doesn't survive deploy restarts cleanly, and creates a *second* scheduling story in the codebase. The cost of the cleaner architecture is one resolver-shaped HTTP endpoint, which is small.

**How to apply:** Step D (recorder) lands as:
- A POST endpoint in Python, e.g. `POST /api/iv-recorder/snapshot` (auth-gated; idempotent on `(date, slot, ticker)`).
- A `RecurringJob` registration in `Backend/Jobs/JobsApi.cs` that fires this endpoint at the three slot times.
- The endpoint calls `polygon_client.list_snapshot_options_chain()`, persists raw bid/ask, runs the solver, and writes the row.

### Q3 — Storage location

**Decision:** **Single Postgres table `recorded_iv_snapshots` in the existing schema, no schema-isolation, no partitioning at first.**

**Why:** premature partitioning is a maintenance tax with no current payoff. Three rows per ticker per session, four tickers, ~252 sessions/yr = ~3000 rows/yr/4-tickers. We are years away from the row count where partitioning matters.

**How to apply:** when row count crosses ~10M, add monthly range partitioning by `snapshot_ts`. Until then, a btree index on `(ticker, snapshot_ts)` is sufficient.

**Schema** (concrete proposal for Step D):

```sql
CREATE TABLE recorded_iv_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    ticker          TEXT NOT NULL,
    snapshot_ts     BIGINT NOT NULL,        -- int64 ms UTC (per CLAUDE.md rule)
    slot            TEXT NOT NULL,          -- '09:35' | '12:30' | '16:00'
    spot            DOUBLE PRECISION NOT NULL,
    rate            DOUBLE PRECISION NOT NULL,
    dividend        DOUBLE PRECISION NOT NULL,
    iv30_vix_style  DOUBLE PRECISION,       -- nullable on solver failure
    iv30_parametric DOUBLE PRECISION,
    iv_provenance   JSONB NOT NULL,
    raw_chain       JSONB NOT NULL,         -- per-contract bid/ask/strike/expiry/type
    UNIQUE (ticker, snapshot_ts)
);
CREATE INDEX recorded_iv_snapshots_ticker_ts ON recorded_iv_snapshots (ticker, snapshot_ts);
```

Storing `raw_chain` as JSONB (not a normalized child table) keeps the recorder write path one INSERT and lets us re-derive IV with future solver improvements without touching the schema. This matches the plan's "we never store Polygon's IV field — we recompute" rule.

### Q4 — Confidence floor

**Decision:** **`confidence < 0.1` is the hard-gate floor; configurable per-route via Pydantic settings.**

**Why:** floor must exist (the plan is right that continuous gating still needs an extreme-case kill) but should not be hardcoded — it's a policy parameter, and the same floor may not suit RVRP signals vs. regime classification. Default `0.1` matches the plan.

**How to apply:** add `IV_CONFIDENCE_FLOOR_DEFAULT: float = 0.1` to `app/config.py`; let route-specific Pydantic request models override it.

### Q5 — `quality_score` for `synthetic_close_proxy`

**Decision:** **`quality_score = 1 - (half_spread / mid)` for any source, including synthetic.**

**Why:** the principled formula is data-driven and source-agnostic. An ATM `synthetic_close_proxy` with a $5 mid and $0.05 half-spread scores 0.99. A deep-OTM `synthetic_close_proxy` with a $0.10 mid and $0.05 half-spread scores 0.5. That's the right shape. The naive "1.0 ATM, decay outward" rule needs a model of "where ATM is" and bakes the answer in; this formula falls out of the data.

**Bonus:** the same formula works unchanged for `opra_mid` (real spreads are usually tighter than synthetic ones, so the score will naturally be higher there).

**How to apply:** implemented in `from_snapshot_quote` and `from_eod_close` in this PR.

### Q6 — Ticker scope for the recorder

**Decision:** **SPY only at first. Add QQQ/IWM/DIA after the first 30 sessions of clean SPY data.**

**Why:** the recorder is the highest-novelty piece of infrastructure in the plan. Validating it on one ticker before fanning out has two benefits: (1) cuts Polygon API cost during burn-in, (2) lets us catch any per-ticker edge cases (chain quality, tier truncations) without four-way confounding.

**How to apply:** the recorder's ticker list is config-driven; expanding is a one-line settings change once the SPY data is clean for 30 sessions.

### Q7 — Polygon plan upgrade

**Decision:** **No upgrade. Stay on Stocks Starter + Options Starter.**

**Why:** the plan ships entirely on Starter. The architecture is upgrade-compatible by design — if we later move to Options Developer/Advanced, the historical-NBBO path becomes a `from_historical_quote` constructor in `price_normalization.py`, the `PriceSource` enum gains one variant, and downstream consumers are unaffected. We can revisit when the recorder has been live for a quarter and the cost/value of historical NBBO is concrete.

**How to apply:** none for now. This PR's `PriceSource` literal is `("opra_mid", "opra_mid_recorded", "synthetic_close_proxy")` — adding a fourth value is one line.

---

## 2. What's in this PR (Steps A + B), what's not

### In this PR

- **Step A** — `app/volatility/price_normalization.py`, `app/volatility/iv_provenance.py`. Pure additive: the `NormalizedOptionPrice`, `NormalizedOptionQuote`, `IvProvenance` dataclasses with constructors and tests. Existing code unchanged.
- **Step B** — `app/volatility/vix_replication.py` gains `replicate_expiry_variance_with_provenance` and `vix_style_iv30_with_provenance` *alongside* the existing functions (not replacing). Provenance includes:
  - `price_source_mix` (count-based share)
  - `variance_contribution_synthetic` (the weighted measure — Round 3 issue #2)
  - `strike_coverage_score` (the wing-depth diagnostic — Round 3 upgrade)
  - `per_strike_contributions` (opt-in via `debug=True` — Round 3 issue #5)
- **Tests:** new `test_price_normalization.py`, new `test_iv_provenance.py`, extended `test_vix_replication.py` with two new test classes (golden-fixture-with-provenance + half-and-half synthetic-vs-real).
- **Existing tests:** untouched. They still pass against the unchanged legacy entry points.
- **Decisions doc:** this file.

### Deferred to follow-up PRs

| Step | Why deferred | Effort |
|---|---|---|
| **C — Live `/iv30/{vix-style,parametric}` endpoints** | Smoke test needs a live Polygon snapshot; can't be CI-deterministic without recorded fixture infrastructure that doesn't exist yet. Lands cleanly after this PR's contracts merge. | 1 day |
| **D — Multi-snapshot recorder** | Requires (a) Postgres migration, (b) .NET cron registration, (c) integration test plan. Too much surface for one night without coordination on Q2/Q3 above. | 1.5–2 days |
| **E — Continuous confidence gating in `vrp_signal`** | Depends on Step B being merged so callers can pass `IvProvenance`. Logically the next PR after this one. | 1 day |
| **F — Wire `compute_iv30_health` into regime classifier** | Same wiring shape as E; lands cleanest in the same PR as E so the confidence formula has one definition site. | 0.5 day |
| **G — Frontend `black-scholes.ts` parity test** | Independent leaf; the deprecated frontend BS already has a Python-side cross-engine parity test (`test_bs_cross_engine_parity.py`) that pins the same math. The frontend Vitest is owed for full closure but does not block any other step. Can land in parallel with any of C/D/E/F. | 0.5 day |

### Recommended follow-up sequence

1. **PR-2:** Step C — exposes contracts via HTTP, gives the UI a real-time IV30 to plot.
2. **PR-3:** Step E + F together — single confidence-formula site, both consumers wired at once.
3. **PR-4:** Step D — recorder. Largest infra change; lands after the contracts/consumers are stable so we don't change two interfaces at once.
4. **PR-5:** Step G — frontend parity test. Anytime; truly independent.

---

## 3. Things I considered but did not do

Listed for traceability:

- **Did not refactor existing `replicate_expiry_variance` to call the provenance variant internally.** That would mean rebuilding `OptionQuote → NormalizedOptionQuote` on every call, which is wasted work. The legacy path stays bare-float-fast; the provenance path is opt-in. Single source of truth is preserved by both paths sharing a private `_walk_strikes` helper.
- **Did not change `compute_iv30_health` signature.** Per plan §8, the callers change, not the helper. This PR does not touch `iv30_health.py`.
- **Did not extend `iv30_constructor.py`.** The plan's Step B mentions it, but its signature already takes scalar σ values per expiry, not chains — there are no per-strike option prices to tag. The provenance is upstream of this function (the chains feeding the σ values), and this PR's `vix_style_iv30_with_provenance` is what carries that. `iv30_atm_50d` stays unchanged.
- **Did not implement the `polygon_field` IV source path.** The plan explicitly says "we never store Polygon's IV field as an IV value" — `IvSource = "polygon_field"` exists in the enum for monitoring/diagnostic purposes, but this PR has no production path that emits it. Adding the diagnostic comparison is a follow-up alongside Step D.

---

## 4. Conventions adopted in this PR (load-bearing for follow-ups)

- **`int64 ms UTC` rule honored.** No new timestamp formats introduced. The recorder's eventual schema (proposed in Q3 above) uses `BIGINT` for `snapshot_ts`.
- **Frozen dataclasses.** `NormalizedOptionPrice`, `NormalizedOptionQuote`, `IvProvenance` are all `@dataclass(frozen=True)` — they are values, not entities.
- **Constructors named for *source*, not for *shape*.** `from_snapshot_quote(bid, ask)`, `from_eod_close(close)` — the call site declares the data regime, no polymorphism. This is the architectural commitment from the plan's Round 1 rebuttal.
- **`per_strike_contributions` is opt-in.** Default `debug=False` returns `IvProvenance.per_strike_contributions = None`. Enabling it adds list of per-strike dicts (strike, kind, dK, Q, c_i, source). Useful for skew-anomaly debugging; not in the hot path.
- **`half_spread_rule` is a string, not a function.** When we synthesize a spread, we record the *rule text* (e.g. `"max($0.05, 0.5%·close)"`) so a future reader can reproduce it without spelunking. Round-trip-serialization is preserved.

---

## 5. If something I decided here is wrong

Override priority:

1. **Q2 (recorder host)** is the most consequential and the one I'd most want a sanity check on. If you'd rather have an in-process scheduler, the change is small — a startup hook in `app/main.py` lifespan + `apscheduler` in `requirements-light.txt`.
2. **Q5 (`quality_score` formula)** — if you want the naive ATM-decay version, it's a one-function change in `price_normalization.py`. Tests would need their thresholds adjusted.
3. **Everything else** is reversible at the configuration / one-PR level.
