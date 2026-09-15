# Daily return distribution study

**Concept**: Take the minute bars a symbol already has in the data lake, compute each trading day's return three ways (close-to-close, session-only, overnight gap), and histogram the days into fixed-width "baskets" — the empirical distribution of daily returns, the study shape of Fama (1965) §I.B and Mandelbrot (1963). Clicking a basket opens the days inside it, each decomposed across the full 24-hour cycle (overnight gap, pre-market, morning, afternoon, after-hours), plus a minute-candle view of the selected day.

**Reference**: Fama, "The Behavior of Stock-Market Prices" (1965) §I.B and Mandelbrot (1963) for the study shape (histogram of daily changes; fat tails vs the normal). Moment estimators: the adjusted Fisher-Pearson standardized coefficients G1/G2 — the `bias=False` definitions documented at https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.skew.html. Historical VaR/CVaR: McNeil, Frey & Embrechts, *Quantitative Risk Management* (2e) §2.2–2.3 (empirical quantile and tail-expectation estimators). No vendored code was ported; the definitions are standard and the oracle is scipy/numpy.

**Canonical implementation**: `PythonDataService/app/research/return_distribution.py` (all math, pure functions over bars + factor rows), `app/services/return_distribution_service.py` (lake read + orchestration, off the event loop per #1943), `app/routers/return_distribution.py` (`POST /api/research/return-distribution`). Registry rows in `docs/math-sources-of-truth.md` and `docs/architecture/engine-authority-map.md`.

**Validated against**: `PythonDataService/tests/research/test_return_distribution.py` — hand-computed anchors/segments on synthetic extended-hours days (including the 2024-07-03 half-day), factor-file as-of semantics pinned with a dividend-restoration case and a 2:1 split, log-space identities, bin-edge membership, and a live scipy/numpy oracle for every statistic. Golden fixture `tests/fixtures/golden/return-distribution/RD-001`: 60 real NYSE sessions of deterministic synthetic bars through the full pipeline, with an independent numpy/scipy recomputation from `input.arrow` compared at `atol=1e-9, rtol=0` (`tests/fixtures/test_return_distribution_golden.py`). Tolerance note: the oracle (numpy pairwise / scipy) and the canonical module (`math.fsum` loops) evaluate identical closed forms with different summation orders — divergence is ulp-level (~1e-13 at n=60), four orders below the pinned tolerance; bin counts are exact integer equality.

## Return kinds and session segments

| Return | Definition (anchors) | Additivity |
|---|---|---|
| `close_to_close` (default) | previous scheduled RTH close → this session's RTH close | = overnight + session (log-exact) |
| `session` | RTH open → RTH close | = morning + afternoon (**by construction**: `afternoon_log = session_log − morning_log`) |
| `overnight` | previous RTH close → RTH open | the gap component of close-to-close |
| `pre_market` | first extended bar open → RTH open | supplementary; the traded portion of the overnight gap |
| `morning` | RTH open → close of last bar starting before 12:00 ET | |
| `afternoon` | 12:00-ET boundary → scheduled close | half-days close at their scheduled close (13:00 ET), never a 16:00 literal |
| `after_hours` | RTH close → last extended bar close | supplementary |

All returns are computed in **log space** and displayed as simple percent via `expm1`. The log-space identities hold exactly; in displayed percent the same sums hold to second order (geometric compounding: morning 1.2% + afternoon 0.8% ≈ day 2.0%), the standard convention for decomposing a day's move. Segment `None` means the segment genuinely produced no bars (e.g. no pre-market trades), never zero.

Session boundaries come only from `app/lean_sidecar/trading_calendar.py`; the study's own constants are the 12:00 ET morning/afternoon split and the 04:00–20:00 ET extended-hours envelope the lake captures.

## Baskets (bins)

Fixed width (0.5 default, 1.0 option), edges at multiples of the width so **0 is always a bin edge**, inner bins `[lo, hi)` spanning ±5% by default, plus two **open edge bins**: `v < −span` on the left and `v ≥ +span` on the right. Membership is lower-inclusive on inner bins — `v = −5.0` lands in `[−5.0, −4.5)`, `v = +5.0` in the right edge bin — so the union covers ℝ with no overlap. The wild days stay visible by design; clipping them is what the original hand-run study got wrong.

## Statistics

All in simple-percent units, on each kind's series: N, mean, sample std (ddof=1), annualized vol = σ·√252, skew G1, excess kurtosis G2 (Fisher), historical VaR-95 = linear-interpolated 5th percentile (numpy `method="linear"`), CVaR-95 = mean of the values at or below VaR-95, best/worst day with dates. The bell-curve overlay is the expected count per bin under `Normal(mean, std)` fitted to the same series — `N·(Φ(hi) − Φ(lo))`, Φ via `erf`, identical to `lean_statistics._normal_cdf`.

## Corporate-action adjustment

The study reads the **raw** lake root (deepest coverage) and multiplies each day's anchors by the lake's own LEAN factor-file `price_factor · split_factor` as of that date (a row dated D covers data through D; D's successor uses the next row; data before the first row uses the first row — LEAN's own application). Splits and dividends are therefore both removed from the return series. When no factor file exists the response says `adjustment: "raw"` with a warning — unadjusted series can show corporate-action days as outlier moves. Lake prices are deci-cent quantized (1e-4 grid), the same precision seam the chart documents; at 0.5% bin width this is four orders below decision-relevant scale.

## On-demand capture

The study is capture-on-demand: it always *computes* over lake bytes, but when the requested window has completed sessions the lake does not hold, they are delta-fetched into the lake first through `ensure_data` — the same machinery the Data Lab chart seam and the capture script use, run as a `chart`-type best-effort run with **factor and map files included**, so a newly captured symbol gets the split+dividend adjustment from its first request. The capture span is the completed-session prefix of the read window (14-day lead-in included, clamped before the still-forming session — the same calendar split the chart composer uses). Catalog claims coalesce concurrent studies of the same cold symbol into one fetch; the capture budget is 600 s, and provider/catalog/timeout failures are contained into the response's `meta.capture` receipt rather than failing the request — the study then computes over whatever landed. A capture that still leaves the window empty (unknown ticker, entitlement or auth failure, catalog down) is a typed `NOT_CAPTURED` 404 whose `capture_note` carries the reason; a symbol the lake can never address (path-unsafe per the writer's alphabet) is the same typed error without a capture attempt.

## Coverage semantics

The service reads a 14-calendar-day lead-in before `from_date` so the window's first session has a previous close to return against. `coverage` reports requested (scheduled) vs returned sessions, missing sessions (scheduled but not captured even after the on-demand capture), and excluded sessions (captured but without RTH bars). Fewer than 30 captured sessions is a typed `INSUFFICIENT_COVERAGE` 400. First-time captures of a two-year window can take tens of seconds; the frontend loading state says so.
