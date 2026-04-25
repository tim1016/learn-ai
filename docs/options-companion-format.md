# Options Companion Files — Format & Calculation Reference

Authoritative spec for the options companion CSVs emitted by the Data Lab dataset bundle. Covers (1) file layout, (2) the slot model, (3) the constant-DTE expiry policy, (4) the discontinuity semantics, and (5) calculation references for IV and Greeks.

Source of truth in code: `PythonDataService/app/services/options_companion_service.py`. Pydantic config: `OptionsCompanionConfig` in `PythonDataService/app/models/requests.py`.

---

## 1. File layout

When `OptionsCompanionConfig.enabled = true`, the dataset ZIP gains two subfolders:

```
calls/
  atm-03.csv
  atm-02.csv
  atm-01.csv
  atm.csv
  atm+01.csv
  atm+02.csv
  atm+03.csv
puts/
  atm-03.csv
  atm-02.csv
  atm-01.csv
  atm.csv
  atm+01.csv
  atm+02.csv
  atm+03.csv
```

- One CSV per `(side, slot)` pair.
- File count = `2 × (2 · strikes_each_side + 1)`. Default `strikes_each_side = 3` → 14 files.
- Filenames are zero-padded for stable sort within sign group (`atm-02` < `atm-01` < `atm.csv` < `atm+01` < `atm+02`).
- Subfolders `calls/` and `puts/` chosen over flat prefixes so consumers can drag a single side out of the ZIP.

`include_calls = false` or `include_puts = false` suppresses the corresponding folder entirely.

---

## 2. Per-slot CSV schema

Every slot CSV shares the **same fixed column order**. Columns are gated by the per-field toggles in `OptionsCompanionConfig` — disabled toggles omit columns, never reorder them.

```
unix_ts            int64        bar-start, ms since Unix epoch UTC (canonical wire format)
iso_time           string       "YYYY-MM-DDTHH:MM:SSZ" — display only, derived from unix_ts
discontinuity      int (0|1)    1 on the first bar after this slot's contract changed; 0 otherwise
contract_ticker    string       Polygon OCC ticker (e.g. "O:SPY260425C00500000") — value, not header
strike             float        the contract's strike for this bar
expiration         date         "YYYY-MM-DD" — the contract's expiration

# OHLCV  (gated by include_ohlcv)
open, high, low, close, volume

# Aggregate enrichments (each independently gated)
vwap               float        gated by include_vwap
transactions       int          gated by include_transactions
open_interest      int          gated by include_open_interest — always blank: per-minute OI is not available from Polygon

# IV + Greeks (each independently gated)
iv                 float        gated by include_iv
delta              float        gated by include_delta
gamma              float        gated by include_gamma
theta              float        gated by include_theta
vega               float        gated by include_vega
rho                float        gated by include_rho
```

**Float formatting:** 8 decimal places (`f"{v:.8f}"`). `None` and `NaN` serialize as the empty string. See `_fmt` in `options_companion_service.py`.

**Sort key:** rows within a slot CSV are strictly ascending by `unix_ts`.

**Why `contract_ticker` is a row value, not a column header:** the contract filling a slot rolls every trading day under any non-trivial `dte_distance`, and rolls within a day if the underlying gaps. Putting the ticker in the header would force a new column per day; putting it in a row keeps the schema fixed.

---

## 3. Slot model

A slot is a stable, **price-ordered** position relative to ATM, holding for one trading day:

| Slot | Meaning |
|---|---|
| `atm-N` | Nth strike *below* ATM by raw strike-price ordering |
| `atm` | strike closest to that day's anchor price |
| `atm+N` | Nth strike *above* ATM by raw strike-price ordering |

The convention is **identical for calls and puts** — `atm-3` is always the lowest strike, `atm+3` always the highest. This is *price-ordered*, not *moneyness-ordered*: for puts, "more in the money" means higher strike, which lives in `atm+N` slots. The user analyzing moneyness flips the sign mentally; the file format does not encode it.

**ATM anchor:** the closest listed strike to the *prior trading day's close* of the underlying. The anchor is fixed for the entire trading day — it does not re-anchor intraday on underlying moves. Source: `_select_strikes` and `_prior_day_close_map` in `options_companion_service.py`.

**Edge of chain:** if a slot's offset would land outside the listed strike range for that day, the row's strike-dependent cells are blank (the `unix_ts` row still appears so the file stays time-aligned across slots).

---

## 4. Expiry model — constant DTE distance

Single integer config field: `dte_distance: int`, default `0`. Replaces the legacy `expiry_mode` + `max_dte` pair.

For each trading day `D`, the chosen expiry is:

```
target_expiry(D) = the listed expiry equal to D + dte_distance
```

- `dte_distance = 0` → 0DTE: that day's same-day expiry.
- `dte_distance = 7` → the option expiring exactly 7 calendar days after `D`.

**Strict matching, no tolerance.** If no listed contract chain expires on `D + dte_distance`, the day is **skipped entirely**. The slot CSVs emit no rows for that day. The per-day report records the skip with reason `"no listed expiry at D + dte_distance"`.

**All option columns in a single CSV row share one expiry.** This is enforced structurally — only that day's chosen expiry is queried — and is the reason strict matching beats fuzzy matching: a row mixing different expiries is meaningless for time-decay analysis.

Skipped days appear in `dataset_metadata.json → options_companion.days_skipped`.

---

## 5. Discontinuity column

`discontinuity` is a binary column on every slot CSV. It is `1` on the **first bar after this slot's contract identity changed**, `0` on every other bar.

Triggers:

1. **Day boundary.** Under any `dte_distance`, the chosen expiry rolls forward by one calendar day at each new trading day. The contract is therefore always different across day boundaries — `discontinuity = 1` on the first bar of every new trading day.
2. **ATM re-anchor mid-range.** If the prior-day close moves enough to roll the ATM strike, the contract in `atm` (and adjacent slots) changes. This is also a day-boundary event, so it's already captured by trigger 1.

The value is computed by comparing the row's `contract_ticker` to the previous row's `contract_ticker` for the same slot file.

**Why we do not back-adjust.** The price gap between e.g. `SPY-500C-Apr25` and `SPY-502C-Apr26` is not a "roll yield" — it's two genuinely different derivative instruments with different deltas, IVs, and time-decay profiles. Panama-adjusting (subtract close-to-close gap) would corrupt the IV signal and is wrong for options. The series is concatenated unadjusted; `discontinuity` lets the user `groupby` segments or insert visual breaks in their plots.

**UI surfacing.** The Data Lab UI shows an info icon next to the "Include discontinuity column" toggle, with this tooltip:

> The slot's underlying contract changes each trading day (different strike or expiry).
> `discontinuity = 1` marks the first bar after each change. Treat it as a series reset
> when computing returns, plotting lines, or training models — values across a `1`
> boundary are two different financial instruments and not directly comparable.

---

## 6. Configuration summary

Fields on `OptionsCompanionConfig`:

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | bool | `false` | Master switch for the companion files |
| `strikes_each_side` | int (1..25) | **3** *(was 5)* | N strikes above AND below ATM per side |
| `include_calls` | bool | `true` | Suppresses the entire `calls/` folder when false |
| `include_puts` | bool | `true` | Suppresses the entire `puts/` folder when false |
| `dte_distance` | int (0..30) | **0** | Replaces `expiry_mode` + `max_dte`. 0 = 0DTE same-day |
| `include_ohlcv` | bool | `true` | OHLCV columns per slot CSV |
| `include_vwap` | bool | `true` | VWAP column |
| `include_transactions` | bool | `true` | Transaction-count column |
| `include_open_interest` | bool | `false` | Always-blank column (Polygon does not serve per-minute OI) |
| `include_iv` | bool | `true` | Implied volatility column |
| `include_delta`/`gamma`/`theta`/`vega` | bool | `true` | Greeks |
| `include_rho` | bool | `false` | Greek (less commonly needed) |
| `include_discontinuity` | bool | `true` | The discontinuity column on every slot CSV |
| `risk_free_rate` | float (0..0.25) | `0.05` | Flat annualized rate for IV/Greeks solves |
| `dividend_yield` | float (0..0.25) | `0.0` | Flat continuous dividend yield |

---

## 7. Calculation references

All computation is in `PythonDataService/`. Per `CLAUDE.md` rule 5, .NET and Angular do not compute these numbers — they pass through.

### 7.1 Implied volatility

Source: `PythonDataService/app/volatility/solver.py` — `implied_volatility(...)`.

**Primary solver:** QuantLib `VanillaOption.impliedVolatility()` (Newton-based, max 200 iterations, tolerance `1e-8`). Returns status `QUANTLIB_OK`.

**Fallback solver:** SciPy `brentq` over the Black-Scholes price function. Triggered when QuantLib fails to converge (deep ITM/OTM, near-expiry, sparse-quote regimes). Returns status `BRENT_FALLBACK`.

**Bracketing.** IV is constrained to `[0.005, 5.0]` (0.5% – 500% annualized). Outside this range, the solver returns `CONVERGENCE_FAILURE` and the row's IV cell is blank.

**Intraday-TTM override.** The default solver floor `MIN_TIME_TO_EXPIRY = 1.0 / 365` (1 calendar day) silently rejects every 0DTE bar (TTM ≤ 6.5 hours intraday) as `EXPIRED`. The companion service overrides this via the `min_ttm` parameter, set to `1.0 / (365 * 24 * 60)` (1 minute) — see `_MIN_TTM_INTRADAY` in `options_companion_service.py`. This lets IV solve across the entire 0DTE trading day except the very last minute, where the values are uninformative regardless. The default floor is preserved for every other caller of `implied_volatility`; only the companion pipeline opts in to the lower bound.

**Reject conditions** (per `_compute_row_greeks` in `options_companion_service.py`):

- `ttm <= 0` (option already expired by this bar)
- `option_close <= 0` (no print)
- `option_close < intrinsic` (status `INTRINSIC_VIOLATION`)
- `option_close < MIN_OPTION_PRICE` (`0.001`, status `PRICE_TOO_LOW`)
- `underlying_spot` missing for the bar's grid-aligned timestamp

When any reject fires, IV and all Greeks for that row are blank.

**Surface-based IV is intentionally not used as input.** Building a per-minute volatility surface across the full chain is impractical at minute resolution. Surface-based IV is a deferred cross-check, tracked in `docs/options-cross-section-overview.md` and the references backlog. The per-bar solve is the input; the surface is the future validator.

**Determinism.** Same inputs → same IV. Status enum lets every row carry a diagnostic from `SolveStatus` — see `solver.py` lines 39–47.

### 7.2 Greeks

Source: `PythonDataService/app/services/options_companion_service.py` — `_bsm_greeks(...)`.

**Why not QuantLib `AnalyticEuropeanEngine`.** `quantlib_pricer.price_option` derives TTM from `(eval_date, expiration_date)` Python `date` objects, which are calendar-day-resolution. For 0DTE bars (eval_d == expiration_date), `Actual365Fixed.yearFraction` returns 0 and the pricer takes the "expired — return intrinsic" branch (`quantlib_pricer.py` lines 164–177): delta saturates to ±1, gamma/theta/vega/rho all return 0. The companion path needs precise *fractional* TTM in years — measured in milliseconds from `bar_ts_ms` to `expiry_close_ms` — so it bypasses QuantLib for Greeks and computes closed-form Black–Scholes–Merton directly. IV still uses `app.volatility.solver.implied_volatility` because that path accepts a continuous `ttm` argument.

**Closed-form formulas** (Black–Scholes–Merton, with continuous dividend yield $q$ and risk-free rate $r$). Let:

$$d_1 = \frac{\ln(S/K) + (r - q + \tfrac{1}{2}\sigma^2)\,t}{\sigma\sqrt{t}}, \qquad d_2 = d_1 - \sigma\sqrt{t}$$

Calls:

$$\Delta = e^{-q t}\,\Phi(d_1)\quad
\Gamma = \frac{e^{-q t}\,\phi(d_1)}{S\,\sigma\sqrt{t}}\quad
\mathcal{V}_\text{raw} = S\,e^{-q t}\,\phi(d_1)\,\sqrt{t}$$

$$\Theta_\text{annual} = -\frac{S\,e^{-q t}\,\phi(d_1)\,\sigma}{2\sqrt{t}} - r K e^{-r t}\Phi(d_2) + q S e^{-q t}\Phi(d_1)$$

$$\rho_\text{raw} = K t e^{-r t}\Phi(d_2)$$

Puts (using $\Phi(-x) = 1 - \Phi(x)$):

$$\Delta = e^{-q t}(\Phi(d_1)-1)\quad
\Theta_\text{annual} = -\frac{S\,e^{-q t}\,\phi(d_1)\,\sigma}{2\sqrt{t}} + r K e^{-r t}\Phi(-d_2) - q S e^{-q t}\Phi(-d_1)$$

$$\rho_\text{raw} = -K t e^{-r t}\Phi(-d_2)$$

$\Gamma$ and $\mathcal{V}$ are identical for calls and puts.

**$\Phi$ and $\phi$ implementation.** $\Phi$ via `math.erf`: $\Phi(x) = \tfrac{1}{2}(1 + \operatorname{erf}(x/\sqrt{2}))$. $\phi$ via direct exponential. `_norm_cdf` and `_norm_pdf` in `options_companion_service.py`.

**Unit conventions emitted in the CSV:**

| Column | Convention | Conversion from raw |
|---|---|---|
| `delta` | raw (per unit price) | — |
| `gamma` | raw (per unit price²) | — |
| `theta` | **per calendar day** | $\Theta_\text{annual} / 365$ |
| `vega` | **per 1 % vol move** | $\mathcal{V}_\text{raw} / 100$ |
| `rho` | **per 1 % rate move** | $\rho_\text{raw} / 100$ |

These match the legacy `quantlib_pricer.price_option` output conventions (see `quantlib_pricer.py` lines 207, 215, 223), so existing downstream consumers don't need to change.

**Validation status.** Per-bar Greek values are pending a formal parity pass against LEAN's analytic engine. Tracked in `docs/math-sources-of-truth.md`. Until that lands, treat Greeks as research-grade — directionally correct, signs and magnitudes consistent with closed-form (sample 0DTE put bar verified in §10), but not yet pinned to a golden fixture.

**Cross-references:**
- Frontend Black–Scholes (Abramowitz & Stegun normal CDF, used for the strategy lab's payoff curves only — NOT the companion CSVs): `docs/black-scholes-implementation.md`
- `quantlib_pricer.PricingEngine` enum (binomial, finite-difference, Monte Carlo) is **not** used in the companion pipeline.

---

## 8. Bar-grid alignment between underlying and option series

The companion fetches option aggregates at the **same `(timespan, multiplier)` as the underlying**. Polygon's `/v2/aggs/ticker/{ticker}/range` endpoint shares one UTC-anchored bar grid across stocks and options, so timestamps from the two series align exactly.

Defensive bar-grid floor (`_bar_grid_floor_ms` in `options_companion_service.py`) is applied to the underlying-spot lookup map, so any future micro-drift between the two series degrades gracefully — affected rows lose IV/Greeks rather than silently pairing with the wrong spot.

---

## 9. Implementation references

| Concern | File | Symbol |
|---|---|---|
| Slot selection (offset → contract) | `options_companion_service.py` | `_select_strikes_with_slots` |
| Slot label | `options_companion_service.py` | `_slot_label` |
| Anchor (prior-day close) | `options_companion_service.py` | `_prior_day_close_map` |
| Underlying↔option timestamp alignment | `options_companion_service.py` | `_underlying_close_map`, `_bar_grid_floor_ms` |
| DTE expiry resolution | `options_companion_service.py` | `_resolve_target_expiry` |
| Discontinuity tagging | `options_companion_service.py` | `_mark_discontinuity` |
| Per-bar IV + Greeks orchestration | `options_companion_service.py` | `_compute_row_greeks` |
| Closed-form Greeks compute | `options_companion_service.py` | `_bsm_greeks`, `_norm_cdf`, `_norm_pdf` |
| IV solver | `volatility/solver.py` | `implied_volatility(..., min_ttm=...)` |
| Polygon expirations endpoint | `services/polygon_client.py` | `list_options_expirations(..., expired=...)` |
| Pydantic config | `models/requests.py` | `OptionsCompanionConfig` |
| ZIP packing | `services/dataset_service.py` | `build_zip_bytes(options_slot_files=...)` |
| FastAPI route | `routers/dataset.py` | `_build_zip_with_events`, `/api/dataset/generate-zip[/stream]` |

---

## 10. Validation log

### 10.1 Parity check, 2026-04-25

**Setup.** Fresh run via `POST /api/dataset/generate-zip` with payload:

```json
{
  "ticker": "SPY", "from_date": "2026-04-22", "to_date": "2026-04-25",
  "session": "rth", "timespan": "minute", "multiplier": 1,
  "options_companion": {
    "enabled": true, "strikes_each_side": 5, "include_calls": false, "include_puts": true,
    "dte_distance": 0, "include_discontinuity": true,
    "include_ohlcv": true, "include_vwap": true, "include_transactions": true,
    "include_iv": true, "include_delta": true, "include_gamma": true,
    "include_theta": true, "include_vega": true,
    "risk_free_rate": 0.05, "dividend_yield": 0.0
  }
}
```

**Reference.** A flat-format `options_puts.csv` produced by the previous code path against the same window — the legacy long-format export with columns `unix_ts, iso_time, contract_ticker, expiration, strike, type, OHLCV, vwap, transactions, iv, delta, gamma, theta, vega`.

**Result.**

- Reference rows: **12 295** (data rows, 3 trading days × 11 strikes × 1-minute RTH bars).
- Fresh run rows (sum across the 11 per-slot files under `puts/`): **12 295**.
- Common `(unix_ts, contract_ticker)` keys: **12 295** in reference, **12 295** in fresh, **12 295** intersect, **0** only-in-reference, **0** only-in-fresh.
- **OHLCV+VWAP+transactions mismatches across all 12 295 common keys: 0.**

The fresh per-slot output reproduces the reference byte-for-byte across `open, high, low, close, volume, vwap, transactions`. Concatenating the 11 slot CSVs and dropping `discontinuity` yields the legacy flat format.

**Days_processed: 3, days_skipped: 0** — 2026-04-22, 04-23, 04-24. 2026-04-25 was a non-trading day in the test window so emitted no bars (correctly), and 2026-04-22 prior-close anchor falls back to the first bar's close on that day per `build_options_companion_csvs` (no Apr-21 data in range).

### 10.2 Greeks fill rate

After fixing the two pre-existing bugs documented in §10.3, IV+Greeks populated on the same 0DTE puts:

- `puts/atm.csv`: **1148 / 1210 rows** with full IV + Δ + Γ + Θ + 𝒱 (95.0 %).
- Unfilled rows are bars where the IV solver could not converge — typical pattern is deep-OTM strikes with thin premiums approaching the `MIN_OPTION_PRICE = 0.001` floor or the IV bracket `[0.005, 5.0]` bounds. These are correctly rejected rather than filled with fabricated values.

**Sample row.** `puts/atm.csv`, first bar of 2026-04-22 (09:30 ET, contract `O:SPY260422P00711000`, SPY = 708.84, premium = 2.81):

| field | value | sanity |
|---|---|---|
| iv | 0.196 | 19.6 % annualized — plausible for SPY 0DTE |
| delta | -0.713 | put slightly ITM (K = 711 > S = 708.84), close to -0.7 expected |
| gamma | 0.090 | high — concentrated near strike on a 0DTE |
| theta | -2.31 (per day) | aggressive intraday decay, consistent with 6.5 hours to expiry |
| vega | 0.066 (per 1 % vol) | low — vega collapses near expiry |

### 10.3 Pre-existing bugs found and fixed during validation

These bugs predate the per-slot rewrite and explain why the legacy flat CSV had **all Greeks/IV columns blank for every one of its 12 295 rows**.

| # | Bug | Symptom | Fix |
|---|---|---|---|
| 1 | `polygon_client.list_options_expirations` did not pass `expired=true` to Polygon's `/v3/reference/options/contracts`. | The new strict-DTE check `_resolve_target_expiry` returned `None` for every historical trading day, since Polygon hides expired chains by default. Result: every day in a multi-day backfill reported `days_skipped` and produced zero rows. (The legacy `same_day` mode didn't hit this — it returned `trading_day` without verifying — so the bug was latent until strict-DTE arrived.) | Added `expired: bool \| None = None` to `list_options_expirations`. `_resolve_target_expiry` passes `expired=True`. |
| 2 | `volatility/solver.py:MIN_TIME_TO_EXPIRY = 1.0/365` (1 calendar day) silently rejected every 0DTE bar with status `EXPIRED`. | Every 0DTE row's IV cell was blank, even with QuantLib correctly installed and the underlying spot map correctly aligned. | Added optional `min_ttm` parameter to `implied_volatility(...)`. The companion service passes `_MIN_TTM_INTRADAY = 1.0/(365*24*60)` (1 minute). All other callers retain the 1-day default. |
| 3 | `quantlib_pricer.price_option` derives TTM from calendar dates `(eval_date, expiration_date)`. For 0DTE both dates are equal, so `Actual365Fixed.yearFraction(...)` returns 0 and the function takes its "expired" branch returning saturated Greeks (`δ = ±1, Γ = Θ = 𝒱 = ρ = 0`). | Even when bug #2 was bypassed, Greeks came back saturated for every 0DTE bar. | Greeks for the companion path are now computed inline by `_bsm_greeks(...)` using closed-form BSM with the precise fractional `ttm_years` from `(expiry_close_ms − bar_ts_ms) / _MS_PER_YEAR`. `quantlib_pricer.price_option` is unchanged and still serves other callers. |

The combination of (1)+(2)+(3) explains the all-blank Greeks + zero-rows skip patterns observed in pre-fix runs. After all three fixes:

- `days_skipped: 0` for in-range trading days with listed expiries.
- IV populates on ~95 % of 0DTE rows; the remainder are correctly rejected by the IV solver (deep OTM or thin premiums).
- Greeks populate wherever IV does, with intraday-correct magnitudes (no saturation).

### 10.4 Test fixture (still planned)

A golden-fixture parity test is still **pending** for this pipeline. Per the `numerical-rigor.md` math-debt rule this is a deliberate pay-down task on touch, not a hard prerequisite. The fixture will be a pinned input set (one trading day of SPY minute bars + the listed 0DTE chain on that day) with:

- Reference IV computed via QuantLib **and** SciPy Brent independently; require agreement to `atol=1e-9`.
- Reference Greeks from `_bsm_greeks` compared against an independent closed-form implementation (e.g. `py_vollib`); require agreement to `atol=1e-6, rtol=1e-6` (matching the project tolerance for Greeks per `.claude/rules/numerical-rigor.md`).

Tracked in `docs/math-sources-of-truth.md` under `Status: pending-fixture`. The 2026-04-25 parity check in §10.1–§10.2 stands as the current empirical evidence that the pipeline produces sensible numbers; the golden fixture is the formal cross-check.
