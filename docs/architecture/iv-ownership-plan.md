# Session Handoff — IV Ownership & Price Normalization Architecture

**Status:** Plan (not yet implemented). 7 steps, ~7 dev-days, mergeable as 7 PRs.
**Created:** 2026-04-27, after PR #33 (`docs/architecture/volatility-methodology.md`).
**Polygon tier assumed:** Stocks Starter + Options Starter. **No plan upgrade required.**
**Hand to:** a new Claude session with no context. Read this doc first, then the "read-first" list below.

---

## 0. Read-first list (in order)

A new session should load these before touching code:

1. **This doc.** Single source of truth for the plan.
2. **`docs/architecture/volatility-methodology.md`** — what shipped in PR #33; the IV/RV/VRP machinery this plan extends.
3. **`.claude/rules/numerical-rigor.md`** — the disclosure / fail-fast / no-silent-synthesis philosophy this plan is constrained by.
4. **`CLAUDE.md`** — guiding philosophy point 3 ("sovereignty over the math") and point 5 ("single source of truth").
5. **`docs/architecture/edge-feature-design.md`** — broader edge-route context.

Skim only when relevant:

- `app/volatility/solver.py`, `app/volatility/vix_replication.py`, `app/volatility/iv30_health.py` — the math this plan wraps with provenance.
- `app/routers/edge.py` — the production VRP path (`realized_vs_iv_series`).
- `app/routers/snapshot.py` — the live chain-snapshot endpoint.

---

## 1. TL;DR

After PR #33 the volatility engine is mathematically validated end-to-end (SPY VIX-replication agrees with published CBOE VIX within 19 bps) but has a **production data-contract gap**: the realtime VRP route accepts a caller-supplied `iv_series` parameter, the historical IV pipeline is not built, and price normalization is implicit (real OPRA in live snapshots, synthesized in fixtures, with no type-level distinction).

The plan lands:

- An explicit, typed price-normalization contract (`NormalizedOptionPrice` with provenance tags).
- Separation of price-source from IV-source (a category violation in the first draft).
- Variance-contribution-weighted synthetic exposure in IV30 provenance (not naive count).
- Two new live IV30 endpoints (`/iv30/vix-style`, `/iv30/parametric`).
- A multi-snapshot daily IV recorder that **stores raw bid/ask** and **recomputes IV internally** (does not trust Polygon's IV field).
- Continuous confidence-based gating in the VRP signal generator (replaces a binary cutoff).
- Wiring `compute_iv30_health` into the regime classifier with a unified confidence weight.
- Closing the existing frontend BS parity-test debt.

End-state: no IV number anywhere in the system is a bare float; every IV30 carries `Iv30Provenance`; signals scale by a continuous confidence; the historical IV pipeline is built forward-only from the day the recorder ships.

---

## 2. How we got here (compressed conversation)

The plan emerged from three rounds of external critique. Recording it here so a new session can see the reasoning, not just the conclusion.

**Round 1 — original critique.**
Argued that the system "outsources IV" by accepting `iv_series` as router input, despite owning a validated solver, VIX replication, and IV30 constructor. Proposed a polymorphic `OptionPriceAdapter.get_mid_price(contract)` that internally branches on `has_bid_ask`.

**Round 1 rebuttal (committed to in this plan).**
The `iv_series` parameter is a wiring gap, not an architectural commitment — §8.3 of the methodology doc explicitly lists "IV-from-OptionIvSnapshots historical pipeline" as out-of-scope-for-PR-#33. The polymorphic adapter is exactly what the numerical-rigor rules forbid: it collapses two different data regimes (real OPRA mid vs synthesized close-proxy) into one branch hidden behind a method name, destroying provenance. Live data already uses real bid/ask via the snapshot endpoint (§9.1).

**Round 2 — refined critique.**
Conceded on the polymorphic adapter and the wiring-gap framing. Pointed out that we "rejected the wrong abstraction without writing down the right one" — the system still has two implicit data contracts (live = real, fixture = synthesized) with no type-level normalization in production. Proposed a typed `NormalizedOptionPrice(mid, source, spread_estimate)` dataclass and gating signals by source.

**Round 3 — second refinement (the most operationally important).**
Five issues with the first draft of the plan:

1. **`polygon_computed_iv` in `PriceSource` is a category violation.** Mixes price-level and derived-volatility-level provenance. Split into `PriceSource` and `IvSource`.
2. **`pct_synthetic` count-based is misleading.** Two chains can both be 100% synthetic but differ wildly in quality if synthesis lands on ATM (decent) vs deep OTM (garbage). VIX replication is a weighted integral; what matters is variance-contribution-weighted synthetic exposure, not contract count.
3. **Recorder sampling bias.** A single 16:15 ET snapshot makes recorded IV30 = "close-IV30," which biases VRP toward close-vs-forward-RV rather than average-IV-vs-RV. Multi-snapshot per day fixes this.
4. **Binary gating is crude when continuous data is available.** Health score and synthetic exposure are continuous; the signal generator should scale, not threshold.
5. **Provenance stops at aggregate.** Per-strike contributions are useful for debugging skew anomalies and replication disagreements. Make optional via `debug=True`.

This plan incorporates all five.

---

## 3. The Polygon-tier reality

This plan is shaped by what we have, not what we wish we had. **Do not assume historical NBBO.**

| Endpoint | Available? | Shape | Provenance tag |
|---|---|---|---|
| `GET /v3/snapshot/options/{ticker}` | ✅ Yes | Real NBBO bid/ask, Polygon-computed IV per contract | `opra_mid` |
| `GET /v2/aggs/ticker/{contract}/range/...` (EOD) | ✅ Yes | OHLCV per contract per day | (close only — synthesis required) |
| `GET /v2/aggs/ticker/{contract}/range/15/minute/...` | ✅ Yes (limited) | OHLCV per contract per 15-min bar | (close only — synthesis required, often thin) |
| `GET /v3/quotes/{contract}` (historical NBBO) | ❌ Not on Starter | Tick-level historical NBBO | n/a |
| `GET /v3/trades/{contract}` (historical OPRA trades) | ❌ Not on Starter | Tick-level trades | n/a |

**The implication:** we cannot retroactively reconstruct clean historical option NBBO. Three honest paths exist:

1. **Live recompute** — hit the snapshot endpoint, run our solver. Real bid/ask, real IV. No history.
2. **Forward-only recorder** — schedule snapshots, persist them, build history going forward.
3. **EOD synthesis** — `bid = close − h, ask = close + h` from EOD aggregates. Tagged `synthetic_close_proxy`. Strictly inferior; only for pre-recorder backfill.

Step C, D, and E in the plan correspond exactly to these three paths, each producing a differently-tagged output that flows through the same downstream math.

---

## 4. The plan — 7 steps

### Step A — Define the production data contracts (`NormalizedOptionPrice`, `IvSource`)

**Files:** new `app/volatility/price_normalization.py`, new `app/volatility/iv_provenance.py`.

**Content:**

```python
# price_normalization.py
PriceSource = Literal[
    "opra_mid",              # snapshot bid/ask, real-time
    "opra_mid_recorded",     # snapshot bid/ask, recorded by our cron
    "synthetic_close_proxy", # bid=close-h, ask=close+h, h per disclosed rule
]

@dataclass(frozen=True)
class NormalizedOptionPrice:
    mid: float
    source: PriceSource
    spread_estimate: float | None        # half-spread; None if unknown
    spread_synthetic: bool               # True if spread came from a rule
    half_spread_rule: str | None         # e.g. "max($0.05, 0.5%·close)"
    quality_score: float                 # 0..1; see §4 of this plan

# Constructors — caller-explicit, no polymorphism
def from_snapshot_quote(bid: float, ask: float) -> NormalizedOptionPrice: ...
def from_recorded_snapshot(bid: float, ask: float) -> NormalizedOptionPrice: ...
def from_eod_close(close: float, *, rule: str = "max($0.05, 0.5%·close)") -> NormalizedOptionPrice: ...
```

```python
# iv_provenance.py — kept separate from price provenance (Round 3 issue #1)
IvSource = Literal[
    "internal_solver",       # our 3-tier Newton/QL/brentq chain
    "polygon_field",         # Polygon's snapshot-included IV (used only when explicitly opted-in)
]

@dataclass(frozen=True)
class IvProvenance:
    iv_source: IvSource
    price_source_mix: dict[PriceSource, float]   # share by count
    variance_contribution_synthetic: float       # share by variance contribution (Round 3 #2)
    strike_coverage_score: float                 # 0..1; wing depth + gap detection
    per_strike_contributions: list[dict] | None  # opt-in via debug=True (Round 3 #5)
```

**Why two enums:** Round 3 issue #1. `polygon_computed_iv` from the first draft mixed levels. `PriceSource` describes inputs to the solver; `IvSource` describes which solver produced the output. They cannot collapse without breaking comparability.

**`quality_score` rationale:** even within `synthetic_close_proxy`, an ATM contract has a more reliable close-as-mid than a deep OTM nickel-bid. Score lets future code degrade gracefully without changing the source enum.

**Acceptance:** unit tests assert (a) you cannot construct `NormalizedOptionPrice` without a source tag, (b) `from_eod_close` records the rule string, (c) ruff + mypy clean, (d) round-trip serialization preserves all fields.

**Effort:** 0.5 day.

---

### Step B — Plumb provenance through the IV solver call sites

**Files:** edit `app/volatility/vix_replication.py`, `app/engine/edge/features_realtime/iv30_constructor.py`, `app/volatility/solver.py` call sites.

**Approach:** the solver math (`σ → BS price`) stays pure; provenance lives in wrappers. Each call site that consumes a chain accepts `list[NormalizedOptionPrice]` instead of `list[float]`, and emits an `IvProvenance` alongside the IV30 number.

**Variance-contribution-weighted synthetic share** (Round 3 issue #2). The VIX replication formula is:

$$\sigma^2_T = \frac{2}{T} \sum_i \underbrace{\frac{\Delta K_i}{K_i^2} e^{rT} Q(K_i)}_{w_i \cdot Q(K_i)} - \frac{1}{T}\left(\frac{F}{K_0} - 1\right)^2$$

Define the per-strike variance contribution:

$$c_i = \frac{2}{T} \cdot \frac{\Delta K_i}{K_i^2} e^{rT} Q(K_i)$$

Then:

$$\text{variance\_contribution\_synthetic} = \frac{\sum_i c_i \cdot \mathbb{1}[\text{strike } i \text{ is synthetic}]}{\sum_i c_i}$$

This is what flows into `IvProvenance.variance_contribution_synthetic`. The naive count `pct_synthetic_count` stays available as a secondary diagnostic. **The operational metric (used for gating) is the weighted one.**

**`strike_coverage_score`** (Round 3 upgrade): a 0..1 score of how far into the wings the chain reaches before the two-consecutive-zero-bid truncation rule fires. Specifically: `min(1, (sigma_wings_covered / 5))` where `sigma_wings_covered` is how many standard deviations OTM on each side the chain extends, averaged across calls/puts. Low score means VIX replication is wing-truncated; surface this so debugging knows where to look.

**Acceptance:**

- Existing golden-fixture test extended: SPY 2024-12-20 fixture is `synthetic_close_proxy` end-to-end, asserts `variance_contribution_synthetic == 1.0`, `strike_coverage_score >= 0.8`.
- New synthetic test: half the chain `opra_mid` (ATM cluster), half `synthetic_close_proxy` (wings). Assert `variance_contribution_synthetic > 0.5` (wings dominate VIX replication weight).
- Per-strike debug payload validates against a frozen JSON fixture.

**Effort:** 1 day.

---

### Step C — Live `/api/edge/iv30/{vix-style,parametric}` endpoints

**Files:** new `app/routers/iv30.py`, register in `app/main.py`. Frontend service consumer in `Frontend/src/app/components/edge/services/edge-api.service.ts`.

**Behavior:** hit the live snapshot, run Step A's `from_snapshot_quote` for every leg, run Step B's instrumented replication / parametric construction, return `{iv30, iv_provenance}`. `iv_source = "internal_solver"`, `price_source_mix = {"opra_mid": 1.0}`, `variance_contribution_synthetic = 0`.

**Why this lands first after A/B:** it's the cleanest demonstration that the contracts work end-to-end with zero synthetic content. It also gives the UI a real-time "our IV30 vs CBOE VIX" overlay, which is the missing diagnostic surface from PR #33.

**Acceptance:**

- Smoke test against live SPY snapshot returns IV30 within 50 bps of published VIX on a normal day.
- Provenance object reports `pct_opra_mid >= 0.95` (a few stale legs are tolerable).
- E2E test with mocked snapshot returns deterministic IV30.

**Effort:** 1 day.

---

### Step D — Multi-snapshot daily IV recorder

**Files:** new `app/services/iv_recorder.py`, new EF migration for `recorded_iv_snapshots` table, new cron entry (or a scheduled job inside the FastAPI app — TBD, see §6 open questions).

**Schedule** (Round 3 issue #3 — sampling bias). At minimum **three snapshots per session**, ET:

- **09:35** — captures opening IV (after the 9:30 imbalance settles)
- **12:30** — captures mid-session
- **16:00** — captures close

A daily IV30 is then the **time-weighted average** of these three (or a configurable aggregation; record raw and let the consumer choose). Three samples is the elbow on cost vs bias reduction; two would already be a 2× improvement over PR #33's implicit "close-only".

**What we store** (Round 3 emphasis — sovereignty over the math). For each snapshot:

- `(date, time_et, ticker)` — composite key
- **Raw bid/ask per contract** — so future improvements to the IV math can re-derive without re-fetching
- `spot_at_snapshot`
- `r_at_snapshot, q_at_snapshot` (from `rate_dividend_service`)
- Computed `iv30_vix_style`, `iv30_parametric` (so reads are cheap)
- Full `IvProvenance` blob (JSONB)
- `iv_source = "internal_solver"` — **we never store Polygon's IV field**. Even if Polygon's value is in the snapshot response, we ignore it and recompute. (Round 3: "you still don't truly own IV unless you can recompute it.")

**Acceptance:**

- One week of recorded values exists for SPY.
- `realized_vs_iv_series` router reads from this table when `request.iv_series` is omitted, and reports `iv_source: "recorded_internal"` in the response.
- Schema migration is reversible.
- Recorder failure (Polygon outage, schema drift) logs structured error and skips the slot — does not retry tightly, does not poison subsequent slots.

**Effort:** 1.5–2 days (cron orchestration + migration + recompute pipeline).

---

### Step E — Continuous confidence-based gating

**Files:** edit `app/engine/edge/vrp.py:vrp_signal`, edit `app/routers/edge.py` response model.

**Replace** (Round 3 issue #4): the binary `pct_synthetic > 0.5 → suppress signal` rule.

**With:**

```python
confidence = iv30_health_score * (1 - variance_contribution_synthetic)
z_scaled = z_raw * confidence

action = sign(z_scaled) if abs(z_scaled) > threshold else 0

# Hard gate only at the extreme:
if confidence < 0.1:
    action = 0
    explanation = {
        "reason": "confidence below floor",
        "iv30_health_score": ...,
        "variance_contribution_synthetic": ...,
        "confidence": ...,
    }
```

**Decision-explanation logging** (Round 3 upgrade #3). Every gated row in the response carries an `explanation` blob. The UI banner reads this blob and shows the dominant reason.

**Why multiplicative:** stability and trust-in-inputs are roughly independent failure modes — a healthy chain with all synthetic data is still untrustworthy; a real OPRA chain that's wildly unstable across refits is also untrustworthy. Both must be high for confidence to be high. Additive doesn't capture this.

**Acceptance:**

- Test: `health_score = 1, variance_contribution_synthetic = 0` → `confidence = 1`, `z_scaled = z_raw`, action unchanged.
- Test: `health_score = 0.5, variance_contribution_synthetic = 0.5` → `confidence = 0.25`, action requires `|z_raw| > 4` to fire.
- Test: `confidence < 0.1` → action forced to 0, explanation present.
- Backwards compat: existing VRP integration test passes (the synthetic test data has `health = 1, synth = 0`).

**Effort:** 1 day.

---

### Step F — Wire `compute_iv30_health` into the regime classifier

**Files:** edit `app/engine/edge/regime/regime_classifier.py` (or wherever the IV30 features feed in).

**Behavior:** at every refit, call `compute_iv30_health(chain)` to get the breakdown, combine with `variance_contribution_synthetic` into the same continuous confidence as Step E, and weight features by:

```python
feature_weight = max(0, 2 * health.score - 1) * (1 - variance_contribution_synthetic)
```

This closes the §5.3 "available helper, not yet consumed" debt and shares the gating logic with the VRP signal generator (single source of truth for "how trustworthy is this IV30").

**Acceptance:**

- Regime classifier test with synthetic-heavy chain returns `feature_weight ≈ 0`.
- With clean chain and high stability returns `≈ 1`.
- Regression test on existing regime fixtures passes (clean inputs).

**Effort:** 0.5 day.

---

### Step G — Frontend `black-scholes.ts` parity test

**Files:** `Frontend/src/app/services/black-scholes.spec.ts` (new), `Frontend/test-fixtures/bs-parity-grid.json` (new — generated from `py_vollib`).

**Behavior:** same 576-case grid as `tests/volatility/test_solver_parity_pyvollib.py`. Frontend pricer must match `py_vollib` to the same tolerances (price `<1e-8`, IV `<5e-5` on vega `>0.01`).

**Why now:** §8.3 lists this as owed. Lifting the single-source-of-truth rule to allow the mirror is conditional on the parity test existing. Until it does, the mirror is a liability.

**Acceptance:**

- Test runs in Vitest and CI.
- Grid fixture is checked in and regenerable via a script in `Frontend/scripts/`.
- Failure mode is loud (named contract with delta).

**Effort:** 0.5 day.

---

## 5. Sequencing & dependencies

```
                     A — data contracts
                          /         \
                         /           \
                    B — provenance    G — frontend BS parity
                         |              (independent leaf)
              ┌──────────┼──────────┐
              │          │          │
         C — live      D — recorder  F — regime wiring
         endpoints     (cron + DB)        (uses Step B output)
              │          │
              └────┬─────┘
                   │
              E — continuous gating
              (consumes provenance + health)
```

**Critical path:** A → B → C lands the live-IV30-with-provenance win in ~3 days. D unblocks real historical analytics ~30 sessions after it ships, so wire it fast even if E waits.

**Total:** 6.0–6.5 dev-days.

---

## 6. Open questions to resolve with the user before starting

These are real decisions, not bikeshedding. Surface them in the first reply of the new session:

1. **Recorder snapshot schedule.** Plan defaults to 09:35 / 12:30 / 16:00 ET. Confirm or adjust. Two snapshots is the floor; four+ marginal benefit small.
2. **Recorder execution host.** Cron entry on the host running PythonDataService, or an in-process scheduler (e.g., `apscheduler`)? In-process is simpler but couples reliability to FastAPI uptime.
3. **Storage location.** New table in the existing Postgres? Schema-isolated (`iv_recorder.snapshots`)? Time-partitioned by month?
4. **Confidence floor.** Plan defaults `confidence < 0.1 → hard gate`. Should this be configurable per-route?
5. **`quality_score` semantics for `synthetic_close_proxy`.** Naive: `1.0` for ATM, decay outward. More principled: `1 - (half_spread / mid)`. Pick one to ship first; both are valid.
6. **Ticker scope for the recorder.** SPY only, or SPY/QQQ/IWM/DIA from day one? The roadmap covers all four eventually; recorder cost is linear in tickers.
7. **Polygon plan upgrade.** Not required, but Options Developer/Advanced gives historical NBBO. Worth pricing? If we upgrade later, Step E's synthetic backfill becomes a `from_historical_quote` constructor and the contract doesn't change — the architecture is upgrade-compatible by design.

---

## 7. What we explicitly do NOT do

Listed because the temptation to do them will recur:

- **No `OptionPriceAdapter`** with a hidden `if has_bid_ask else synthesize` branch. Constructors live at the call site.
- **No retroactive synthetic-only backfill** of pre-recorder VRP signals presented as "real history." Synthetic-only periods are flagged in the response and the UI degrades.
- **No storage of Polygon's IV field as an IV value.** The recorder stores raw bid/ask and recomputes via our solver. (We may store Polygon's IV field as a *diagnostic field*, alongside ours, to monitor for drift. Different concept.)
- **No Polygon plan upgrade as a prerequisite.** This plan ships entirely on Starter.
- **No backwards-compat shim for `request.iv_series`** after Step D ships. If the recorder is wired and `iv_series` is still in the API surface, document it as deprecated and remove in a follow-up. The architectural goal is one IV path, not three.

---

## 8. File map (current → after this plan)

### New files

| Path | Purpose |
|---|---|
| `PythonDataService/app/volatility/price_normalization.py` | `NormalizedOptionPrice`, `PriceSource`, constructors |
| `PythonDataService/app/volatility/iv_provenance.py` | `IvProvenance`, `IvSource` |
| `PythonDataService/app/routers/iv30.py` | Live VIX-style + parametric endpoints |
| `PythonDataService/app/services/iv_recorder.py` | Multi-snapshot recorder service |
| `PythonDataService/migrations/<n>_recorded_iv_snapshots.sql` | Postgres table |
| `PythonDataService/tests/volatility/test_price_normalization.py` | Contract tests |
| `PythonDataService/tests/volatility/test_iv_provenance.py` | Provenance + variance-weight tests |
| `PythonDataService/tests/integration/test_iv_recorder.py` | Recorder smoke + recompute determinism |
| `Frontend/src/app/services/black-scholes.spec.ts` | Parity test |
| `Frontend/test-fixtures/bs-parity-grid.json` | py_vollib-generated grid |

### Edited files

| Path | Change |
|---|---|
| `PythonDataService/app/volatility/vix_replication.py` | Accept `NormalizedOptionPrice`, emit `IvProvenance` with variance-weighted synthetic + strike coverage |
| `PythonDataService/app/engine/edge/features_realtime/iv30_constructor.py` | Same |
| `PythonDataService/app/engine/edge/vrp.py` | Continuous confidence gating in `vrp_signal` |
| `PythonDataService/app/engine/edge/regime/regime_classifier.py` | Wire `compute_iv30_health` + provenance weight |
| `PythonDataService/app/routers/edge.py` | Read from recorder when `iv_series` omitted; emit `iv_source` + `confidence` + `explanation` |
| `PythonDataService/app/main.py` | Register `iv30` router; wire scheduler if in-process |
| `Frontend/src/app/components/edge/services/edge-api.service.ts` | Project new response fields |
| `Frontend/src/app/components/edge/realized-vs-iv/realized-vs-iv.component.{ts,html}` | Render decision-explanation banner |

### Untouched (intentionally)

- `app/volatility/solver.py` — solver math is pure, provenance-blind.
- `app/volatility/iv30_health.py` — `compute_iv30_health` already returns the right shape; only its callers change.
- `app/services/fred_service.py`, `app/services/dividend_service.py` — unaffected.
- All daily 4-estimator code (`realized_vol.py`, `forward_rv.py`) — unaffected; kept as chip overlays.

---

## 9. Acceptance — the whole-plan smoke test

After all 7 steps merge, this end-to-end scenario should pass:

1. New deployment, no recorder data yet.
2. UI loads `/edge/realized-vs-iv` for SPY.
3. Response carries `iv_source: "synthetic_eod_proxy"` (or whatever the backfill mode is named), `confidence: 0.3` (low — no recorder data, all synthetic backfill), `vrp_z` series unsuppressed but visibly faded in the UI, banner reads "synthetic-heavy backfill, signals attenuated."
4. Recorder runs for 30 sessions.
5. Same UI request now has `iv_source: "recorded_internal"`, `confidence: 0.85`, signals at full strength.
6. Same response also includes the live `/iv30/vix-style` overlay, plotted alongside the recorded series, with the gap (skew premium) labeled.
7. The user can click into a single bar and see the per-strike contributions (Step B `debug=True` payload).

If any of those steps doesn't hold, the architecture isn't done.

---

## 10. References

- `docs/architecture/volatility-methodology.md` — PR #33, the foundation this plan extends.
- `docs/references/iv-rv-basis-alignment.md` — Step 1 deep-dive on the basis math.
- `docs/architecture/edge-feature-design.md` — broader edge-route design.
- `docs/math-sources-of-truth.md` — registry of canonical math implementations.
- `.claude/rules/numerical-rigor.md` — disclosure / fail-fast philosophy.
- `.claude/rules/python.md` — FastAPI + Pydantic v2 + ruff conventions for new files.
- `.claude/rules/testing.md` — pre-push test hygiene; project-scope ruff.
- `tests/fixtures/golden/iv30/spy-2024-12-20-chain.{parquet,meta.json}` — anchor fixture, attribution sidecar.

---

## 11. For the new session — first three actions

1. **Read this doc and the read-first list (§0).** Don't start coding.
2. **Surface the §6 open questions to the user.** At minimum #1 (snapshot schedule), #2 (execution host), #3 (storage location), #6 (ticker scope). Wait for answers.
3. **Open Step A as the first PR.** It's purely additive (new module, no edits to existing code), unblocks everything else, and validates the contract design before plumbing it.
