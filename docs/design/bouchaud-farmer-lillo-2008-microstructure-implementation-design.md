# Bouchaud-Farmer-Lillo 2008 Microstructure Implementation Design

Audience: Opus implementation agent.

This document is intentionally implementation-facing. It is not a narrative summary for humans. Treat it as the build spec for porting the practical, testable parts of Jean-Philippe Bouchaud, J. Doyne Farmer, and Fabrizio Lillo, "How markets slowly digest changes in supply and demand", arXiv:0809.0822v1, into learn-ai.

## Source Authority

Primary source:

- Paper: Jean-Philippe Bouchaud, J. Doyne Farmer, Fabrizio Lillo, "How markets slowly digest changes in supply and demand", arXiv:0809.0822v1.
- Vendored source: `references/arxiv-0809.0822v1/source/arXiv-0809.0822v1.tar.gz`.
- TeX entry point inside the archive: `handbook20.tex`.
- Reference note: `docs/references/bouchaud-farmer-lillo-2008-market-impact.md`.
- Paper-reported validation fixture: `PythonDataService/tests/fixtures/golden/microstructure/BFL-2008-MICRO-001/v1/`.

Authority hierarchy for this port:

1. Vendored arXiv source in `references/arxiv-0809.0822v1/source/`.
2. The formulas and empirical benchmark values extracted into `BFL-2008-MICRO-001`.
3. This design document.
4. Model knowledge.

If this design conflicts with the vendored TeX, follow the TeX and update this document in the same PR as the implementation.

## Non-Negotiables

- Python owns all math. New computations live under `PythonDataService/`.
- No .NET or Angular math implementation for these studies. Backend and Frontend may expose, persist, and visualize Python outputs only.
- Every implemented formula gets a provenance block with four fields: `Formula`, `Reference`, `Canonical implementation`, `Validated against`.
- Every new math concept gets a row in `docs/math-sources-of-truth.md`.
- Every new engine path or research runner gets a row in `docs/architecture/engine-authority-map.md`.
- Every timestamp crossing a boundary is `int64 ms UTC`.
- Event-time indices are integer event numbers, not timestamps. If serialized, event indices remain integers and quote/trade timestamps remain `int64 ms UTC`.
- Do not claim strict empirical equivalence to the authors' results unless the original raw data is available and used to regenerate the fixture.
- The paper-reported validation fixture in this PR is a benchmark fixture, not a raw-market-data equivalence fixture. It can be used to keep literature targets visible and test parser/output shape, but not to prove our data reproduces LSE/PSE/NYSE studies.

## Goal

Implement the parts of the paper that make learn-ai materially better:

1. Adaptive execution planning for "buy/sell Q shares over horizon T" using the paper's transient-impact optimal schedule as the first baseline.
2. Transaction cost modeling that separates spread cost, concave market impact, timing risk, and missed-fill risk.
3. Microstructure diagnostics for order-flow memory, market impact, liquidity shock recovery, spread-impact balance, and liquidity-volatility coupling.
4. Optional later order-book simulation and market-ecology studies when suitable quote/order-book data exists.

The first practical product should answer:

> Given side, total shares, symbol, start time, end time, and live/recent market state, produce child order instructions that minimize expected implementation shortfall subject to completion and risk constraints.

The first production-quality implementation should be broker-agnostic. It should generate a plan and simulated child orders. Live/paper routing to IBKR is a later integration layer.

## Data Reality Check

Important current constraint: learn-ai does not have tick-level trade, quote, or order-book data today. Therefore the initial implementation must be a bar-compatible execution planner and simulator. It may use modeled spread/impact assumptions and optional user-supplied bid/ask snapshots, but it must not claim empirical validation of the paper's market microstructure studies.

The paper is primarily about trade-by-trade and quote/order-book microstructure. Our repo currently has strong bar-based research infrastructure and some broker/order abstractions, but most paper studies require data richer than OHLCV bars.

### Data Classes

Class A: OHLCV bars.

- Fields: `ts_ms`, `open`, `high`, `low`, `close`, `volume`.
- Can support: schedule generation, bar-level realized volatility, bar-level participation constraints, approximate spread if a spread model is supplied externally.
- Cannot support: trade-sign autocorrelation, individual trade impact, bid/ask spread dynamics, order-book gaps, virtual impact, queue depth.

Class B: top-of-book quotes plus trades.

- Trade fields: `ts_ms`, `price`, `size`, `side` or enough fields to infer sign, exchange/venue if available.
- Quote fields: `ts_ms`, `bid`, `ask`, `bid_size`, `ask_size`.
- Can support: signed order-flow memory, immediate response, spread-impact relation, spread shock recovery, market/limit order phase diagram approximations.
- Cannot fully support: virtual impact beyond best quote, order-book shape, large gap distribution.

Class C: level-2/order-book event stream.

- Event fields: `ts_ms`, `event_type`, `side`, `price`, `size`, `order_id` if available, full depth snapshots or reconstructable book deltas.
- Can support: virtual impact, gap distribution, order-placement distance distribution, cancellation model, zero/low-intelligence order-book simulator calibration.

Implementation must require the correct data class at the API/schema boundary. Do not silently degrade a Class C study to Class A data.

### Immediate Bar-Only Track

Until tick/quote/order-book data exists, Opus should implement only the track below:

```text
Bar data + configured/modelled liquidity assumptions
  -> static optimal execution schedule
  -> integer child-order allocation
  -> adaptive urgency policy using elapsed time, fills, bar volume, modeled spread, and modeled volatility
  -> expected cost attribution with model_status="modeled" or "unavailable"
  -> comparison against TWAP/VWAP-like baselines on bar data
```

Do not implement empirical order-flow memory, individual trade impact curves, spread-impact phase diagrams, spread shock recovery, virtual impact, or order-book shape as production features until a Class B or Class C dataset is present. Synthetic tests for formulas are allowed, but they are formula tests only.

## Implementation Map

Create a new package:

```text
PythonDataService/app/research/microstructure/
  __init__.py
  schemas.py
  order_flow.py
  impact.py
  liquidity.py
  spread_impact.py
  order_book.py
  benchmarks.py
  validation.py
```

Create execution modules:

```text
PythonDataService/app/engine/execution/
  optimal_schedule.py
  adaptive_order.py
  execution_cost.py
```

Create tests:

```text
PythonDataService/tests/research/microstructure/
  test_order_flow.py
  test_impact.py
  test_liquidity.py
  test_spread_impact.py
  test_order_book.py
  test_bfl_paper_benchmarks.py

PythonDataService/tests/engine/execution/
  test_optimal_schedule.py
  test_adaptive_order.py
  test_execution_cost.py
```

Create docs as implementation lands:

```text
docs/references/microstructure-order-flow-long-memory.md
docs/references/microstructure-market-impact.md
docs/references/microstructure-spread-impact-phase-diagram.md
docs/references/microstructure-liquidity-volatility.md
docs/references/microstructure-optimal-execution.md
docs/references/reconciliations/<dataset-specific-name>.md
```

Expose only after core tests exist:

```text
PythonDataService/app/routers/microstructure.py
PythonDataService/app/schemas/microstructure.py
```

No GraphQL or Angular work in Phase 1. Backend passthrough and UI can be separate PRs after Python contracts stabilize.

## Naming Rules

Use paper notation in implementation internals where it improves traceability:

- `epsilon`: trade sign in `{-1, +1}`.
- `v`: trade size.
- `V`: hidden order size.
- `C_lag`: sign autocorrelation.
- `gamma`: order-flow autocorrelation decay exponent.
- `H`: Hurst exponent, `H = 1 - gamma / 2`.
- `alpha`: Pareto tail exponent.
- `psi`: concave impact exponent, `E[r | v] proportional to v**psi`.
- `R_lag`: lagged response/impact.
- `G_0`: single-trade propagator/impact kernel.
- `S`: bid-ask spread.
- `sigma_1`: volatility per trade.
- `phi_t`: execution schedule density.
- `beta`: impact-decay exponent.

External response models can use more descriptive field names but must include these aliases where useful in metadata.

## Paper Sections To Implement

### 1. Long-Memory In Order Flow

Paper sections:

- Section 4.1, empirical evidence for long-memory of order flow.
- Section 4.3, strategic order-splitting model.
- Section 4.4, membership-code validation.
- Section 4.5, heavy tails in volume.

Core formulas:

- Sign series: `epsilon_i = +1` for buy market orders, `-1` for sell market orders.
- Long-memory covariance: `Gamma(tau) ~ tau**(-gamma) * L(tau)`, with `0 < gamma < 1`.
- Hurst relation: `H = 1 - gamma / 2`.
- Hidden-order size tail: `P(V) ~ alpha / V**(alpha + 1)`.
- Model-implied sign autocorrelation: `C_tau ~ (K**(alpha - 2) / alpha) * tau**(-(alpha - 1))`.
- Exponent link: `alpha = gamma + 1`.

Implement:

```python
def trade_sign_autocorrelation(epsilon: npt.ArrayLike, max_lag: int) -> pd.DataFrame:
    ...

def fit_power_law_decay(lags: npt.ArrayLike, values: npt.ArrayLike, *, min_lag: int, max_lag: int | None = None) -> PowerLawFit:
    ...

def hurst_from_gamma(gamma: float) -> float:
    ...

def hidden_order_tail_from_gamma(gamma: float) -> float:
    ...

def simulate_lillo_order_splitting(
    *,
    n_events: int,
    K: int,
    alpha: float,
    min_size: int,
    seed: int,
) -> pd.DataFrame:
    ...
```

Important implementation details:

- `epsilon` must contain only `-1` and `+1`. Reject zeros for pure trade-sign studies.
- Autocorrelation is event-time, not wall-clock.
- For long series, implement FFT-based autocorrelation if needed, but start with an obvious deterministic method for fixture tests.
- Power-law fitting must specify the lag window. Do not fit log-log slopes over all lags by default.
- Return fit diagnostics: slope, intercept, gamma, r2, n_points, lag_min, lag_max.
- Do not use a p-value from ordinary least squares as proof of long memory. The paper itself notes long-memory error bars are difficult.

Validation:

- Unit test `hurst_from_gamma(0.6) == 0.7` at `atol=1e-12, rtol=0`.
- Unit test `hidden_order_tail_from_gamma(0.57) == 1.57`.
- Synthetic fixture: simulate order splitting with `alpha=1.5`, `K` fixed, seed fixed, and assert fitted gamma is near `0.5` within a documented behavioral tolerance.
- Paper benchmark fixture: `BFL-2008-MICRO-001` contains paper-reported LSE `H approximately 0.7`, `gamma approximately 0.6`, average measured `gamma=0.57`, predicted `gamma=0.59`.

Cannot validate without external data:

- Vodafone LSE 1999-2002 autocorrelation curve.
- Same-member versus different-member autocorrelation separation.
- Hidden-order membership-code results.

### 2. Heavy-Tailed Volume And Hidden Orders

Paper sections:

- Section 4.5, heavy tails in volume.
- Section 11.1, identifying hidden orders.

Core formulas:

- Block-trade volume tail: `P(V > x) ~ x**(-3/2)`.
- Hidden-order reported tails:
  - `P(V > x) ~ x**(-2)`.
  - `P(N > x) ~ x**(-1.8)`.
  - `P(T > x) ~ x**(-1.3)`.
- Hidden-order scaling:
  - `N ~ V**1.1`.
  - `T ~ V**1.9`.
  - `N ~ T**0.66`.

Implement:

```python
def hill_tail_exponent(x: npt.ArrayLike, *, tail_fraction: float) -> TailFit:
    ...

def fit_hidden_order_scaling(hidden_orders: pd.DataFrame) -> HiddenOrderScaling:
    ...
```

Input contract for hidden-order analysis:

```text
hidden_orders columns:
  hidden_order_id: str
  broker_or_member_code: str | None
  side: {-1, +1}
  start_ts_ms: int64
  end_ts_ms: int64
  volume: float
  n_trades: int
  duration_ms: int64
```

Validation:

- Synthetic Pareto fixture with known alpha and fixed seed.
- Paper benchmark fixture includes reported hidden-order tail/scaling exponents.

Cannot validate without external data:

- Vaglica et al. Spanish Stock Exchange hidden-order inference.
- Broker-code lognormal individual-broker distribution claim.

### 3. Individual Transaction Impact

Paper sections:

- Section 5.1, impact of individual transactions.
- Section 6.1, why individual transaction impact is concave.

Core formulas:

- Conditional impact: `E[r | v] = epsilon * v**psi / lambda`.
- LSE empirical fit: `psi approximately 0.3` for several highly capitalized LSE stocks.
- Selective-liquidity explanation:
  - `E[r | v] = P(+ | v) * E[r_nonzero]`.
  - Under the simple model, `P(+ | v)` equals the CDF of the volume at the opposite best.

Implement:

```python
def classify_trade_signs(...): ...

def individual_trade_impact(
    trades: pd.DataFrame,
    quotes: pd.DataFrame,
    *,
    lag_events: int = 1,
    volume_bins: int | Sequence[float] = 20,
) -> pd.DataFrame:
    ...

def fit_concave_impact(impact_by_bin: pd.DataFrame) -> ImpactPowerLawFit:
    ...

def selective_liquidity_probability(
    trades: pd.DataFrame,
    quotes: pd.DataFrame,
    *,
    volume_bins: int | Sequence[float] = 20,
) -> pd.DataFrame:
    ...
```

Input requirements:

- Trades and quotes must be synchronized.
- Quotes must include midpoint immediately before and after the trade or enough quote events to reconstruct them.
- Trade signs must be observed or inferred with a documented Lee-Ready or quote rule. Inference must be marked in output metadata.

Validation:

- Synthetic quote/trade fixture where impact is generated as `v**0.3`; assert fitted `psi`.
- Paper benchmark fixture includes `psi approximately 0.3`, `psi approximately 0.5` for small volumes, `psi approximately 0.2` for large volumes, and VOD/LSE `psi=0.3`.

Cannot validate without external data:

- True versus virtual impact from LSE order book snapshots.
- Opposite-best penetration percentages for AZN/LSE.

### 4. Aggregate Transaction Impact

Paper sections:

- Section 5.2, impact of aggregate transactions.
- Section 6.8, aggregated impact.

Core formulas:

- Aggregate signed volume over event window `N`: `Q_N = sum_{i=1..N} epsilon_{t+i} * v_{t+i}`.
- Aggregate return: `R_N = sum_{i=1..N} r_{t+i}`.
- Conditional aggregate impact: `R(Q, N) = E[R_N | Q_N = Q]`.

Implement:

```python
def aggregate_signed_volume_impact(
    trades: pd.DataFrame,
    *,
    event_windows: Sequence[int],
    q_bins: int | Sequence[float],
) -> dict[int, pd.DataFrame]:
    ...

def aggregate_impact_linearity_score(curve: pd.DataFrame) -> AggregateImpactShape:
    ...
```

Validation:

- Synthetic fixture where individual impact is concave but aggregate impact becomes more linear as `N` increases.
- Paper benchmark fixture records the qualitative author validation: increasing linearity and decreasing slope as `N` grows.

Cannot validate without external data:

- AZN LSE 2000-2002 aggregate impact curves in Figure `AZN_compare_aggregate_shift` and `AZN_compare_aggregate_renorm`.

### 5. Transient Impact / Propagator Framework

Paper sections:

- Section 6.2, fixed permanent impact model.
- Section 6.4, transient impact framework.
- Section 6.5, history-dependent permanent impact.

Core formulas:

- Permanent impact random walk:
  - `r_n = epsilon_n * f(v_n; Omega_n) + eta_n`.
- Long-memory incompatibility:
  - If `C_tau ~ tau**(-gamma)` and impact is permanent, returns become predictable/super-diffusive.
- Transient propagator:
  - `m_n = sum_{k < n} G_0(n-k) * epsilon_k + eta`.
- Diffusive constraint:
  - If `G_0(lag) ~ lag**(-beta)`, critical `beta_c = (1 - gamma) / 2`.
- Response:
  - `R_lag = G_0(lag) + sum_{0 < j < lag} G_0(lag-j) C_j + sum_{j > 0} (G_0(lag+j) - G_0(j)) C_j`.
- History-dependent permanent impact:
  - `r_n = eta_n + theta * (epsilon_n - epsilon_hat_n)`.
  - `epsilon_hat_n = E_n[epsilon_{n+1} | I]`.
- Linear AR predictor:
  - `epsilon_hat_n = sum_{i=1..K} a_i * epsilon_{n-i}`.
- Propagator equivalence:
  - `theta * a_i = G(i+1) - G(i)`.
  - `G(i) = theta * (1 - sum_{j=1..i-1} a_j)`.

Implement:

```python
def critical_impact_decay(gamma: float) -> float:
    ...

def power_law_propagator(lags: npt.ArrayLike, *, gamma: float, amplitude: float = 1.0) -> np.ndarray:
    ...

def response_from_propagator(
    G_0: npt.ArrayLike,
    C_lag: npt.ArrayLike,
    *,
    max_response_lag: int,
) -> np.ndarray:
    ...

def propagator_from_ar_coefficients(theta: float, a: npt.ArrayLike) -> np.ndarray:
    ...
```

Validation:

- Unit test `critical_impact_decay(0.6) == 0.2`.
- Unit test AR(1) propagator relation against hand-computed small arrays.
- Synthetic response test with finite lags and explicit hand-computed expected output.
- Paper benchmark fixture includes response amplification factor `lambda approximately 2` from `lag=1` to `lag=1000` for France Telecom 2002 and MRR expected `1.2-1.4`.

Implementation warning:

- The full infinite-sum response equations are easy to get numerically wrong at truncation boundaries. Start with finite-array contracts and document truncation. Do not expose a public API until tests pin the exact indexing convention.

### 6. Spread / Impact Phase Diagram

Paper sections:

- Section 7.1, basic economics of spread and impact.
- Section 7.2, MRR model with bid-ask spread.
- Section 7.3, limit versus market orders phase diagram.

Core formulas:

- Market-maker gain on trade:
  - `G_L(n, n+lag) = v_n * epsilon_n * ((m_n + epsilon_n*S_n/2) - m_{n+lag})`.
- Average market-maker gain without extra costs:
  - `E[G_L](lag) = v * (E[S/2] - R_lag)`.
- Market-order gain:
  - `G_M = v_n * (r(n,n+lag) - S_n/2)`.
- MRR spread:
  - `S = 2 * (theta + phi) = 2 * lambda * R_1 + 2 * phi`.
  - `lambda = 1 / (1 - rho)`.
- Phase diagram coordinates:
  - `x = E[v * R_1(v)] / E[v]`.
  - `y = E[v * S] / E[v]`.
- Market-order zero-gain line:
  - `y = 2 * lambda * x`.
- Copy-cat bound:
  - `y = 2 * (lambda - 1) * x`.
- Market-making bound:
  - `y <= 2 / (1 - C_1) * x`.

Implement:

```python
def spread_impact_coordinates(
    trades: pd.DataFrame,
    quotes: pd.DataFrame,
    *,
    lag_events: int,
) -> SpreadImpactPoint:
    ...

def phase_diagram_bounds(C_1: float, lambda_: float) -> PhaseDiagramBounds:
    ...

def classify_spread_impact_region(point: SpreadImpactPoint, bounds: PhaseDiagramBounds) -> PhaseRegion:
    ...
```

Validation:

- Unit tests on hand-computed arrays.
- Paper benchmark fixture includes:
  - PSE 68 stocks 2002: fitted slope `2.86`, average theoretical `2/(1-C1) approximately 2.64`, `R2=0.90`.
  - NYSE 155 stocks 2005: fitted slope `3.3`, theoretical `2.78`, intercept `1.3 bp`, `R2=0.87`.

Important:

- Do not use this as an alpha signal. It is an execution/liquidity diagnostic.
- If only bar data exists, do not compute this. The required inputs are signed trades and contemporaneous spreads.

### 7. Spread Shock Recovery / Liquidity Crisis Metric

Paper section:

- Section 7.4, spread dynamics after a temporary liquidity crisis.

Core formula:

- `G(tau | Delta) = E[S_{t+tau} | S_t - S_{t-1} = Delta] - E[S_t]`.

Implement:

```python
def conditional_spread_recovery(
    quotes: pd.DataFrame,
    *,
    delta_bins: Sequence[float],
    max_lag: int,
    lag_unit: Literal["events", "seconds"],
) -> dict[str, pd.DataFrame]:
    ...

def fit_spread_recovery_decay(recovery_curve: pd.DataFrame, *, min_lag: int, max_lag: int) -> PowerLawFit:
    ...
```

Validation:

- Synthetic spread process with known power-law or exponential recovery.
- Paper benchmark fixture includes decay exponent range `0.4-0.5` for AZN/LSE spread openings.

Product use:

- Add an execution risk feature later: avoid passive placement or tighten participation after a spread shock until recovery decays below a threshold.

### 8. Liquidity Versus Volatility

Paper sections:

- Section 8.1, liquidity and large price changes.
- Section 8.2, volume versus liquidity fluctuations.
- Section 8.3, spread versus volatility.
- Section 8.4, market cap effects.

Core formulas:

- Transaction-time volatility identity:
  - `sigma_1**2 = E[r_{i,i+1}**2]`.
- Impact-volatility relation:
  - `sigma_1**2 = A * R_1**2 + Sigma**2`.
- Spread-volatility relation:
  - `E[S] = C * sigma_1`.
- Unit-time volatility:
  - `sigma = sigma_1 * sqrt(f)`.
- Market-cap spread scaling:
  - `S ~ sigma_1 ~ M**(-omega)`.

Implement:

```python
def volatility_per_trade(midpoints: npt.ArrayLike) -> float:
    ...

def fit_impact_volatility_relation(points: pd.DataFrame) -> LinearFit:
    ...

def fit_spread_volatility_relation(points: pd.DataFrame) -> LinearFit:
    ...

def liquidity_gap_return_attribution(order_book_events: pd.DataFrame) -> GapImpactAttribution:
    ...
```

Validation:

- Unit tests on small midpoint arrays.
- Synthetic cross-sectional fit with known slope/intercept.
- Paper benchmark fixture includes:
  - `sigma_1**2` versus `R_1**2`: PSE 68 stocks, `A approximately 10.9`, intercept approximately zero, `R2=0.96`.
  - `E[S]` versus `sigma_1`: PSE 68 stocks, slope `1.58`, `R2=0.96`.
  - Gap/liquidity validation: approximately `85%` of nonzero-impact trades have volume equal to best volume and `97%` generate a price change equal to the first gap, per paper prose.

Cannot validate without external data:

- Individual-transaction gap attribution.
- Shuffled-return/transaction-time experiments in Figure `lacifig`.

### 9. Order Book Placement, Shape, And Simulation

Paper sections:

- Section 9.1, heavy tails in order placement and average book shape.
- Section 9.2, volume at best prices and Glosten-Sandas critique.
- Section 9.3, statistical models of order flow and order books.

Core formulas:

- Limit order placement distance:
  - `rho(Delta) ~ 1 / Delta**(1 + mu)`.
- Zero-intelligence spread equation:
  - `E[S] = (mu_market / rho_limit) * F(nu_cancel / mu_market)`.
  - `F(u) approximately 0.28 + 1.86 * u**(3/4)`.
- Average book shape approximation:
  - For exponential `rho(u)`, `Phi_st(Delta) = Phi_0 * alpha * beta / (alpha - beta) * (exp(-beta*Delta) - exp(-alpha*Delta))`.
- Power-law book shape integral appears in Equation `final`.

Implement:

```python
def fit_limit_order_placement_tail(order_events: pd.DataFrame) -> TailFit:
    ...

def zero_intelligence_spread(mu_market: float, rho_limit: float, nu_cancel: float) -> float:
    ...

def average_book_shape_exponential(
    delta: npt.ArrayLike,
    *,
    alpha: float,
    beta: float,
    phi_0: float = 1.0,
) -> np.ndarray:
    ...

def simulate_zero_intelligence_order_book(...): ...
```

Validation:

- Unit test zero-intelligence spread formula against hand-computed values.
- Unit test average book shape is zero at zero, positive inside, and decays at large delta for valid parameters.
- Paper benchmark fixture includes placement exponents:
  - LSE `mu=1.5`.
  - Paris Bourse `mu=0.6`.
  - Mike-Farmer LSE Student degrees `1.3`, corresponding to `mu=1.3`.

Cannot validate without external data:

- Actual LSE/PSE average book shape.
- Student placement distribution fit.
- Cancellation-rate dependence on queue size, imbalance, and distance.

### 10. Optimal Execution And Adaptive Orders

Paper section:

- Section 10, impact and optimized execution strategies.

Core formulas:

- Own-impact model:
  - `p(t') - p_0(t') = P(0) * integral_0^t' phi(t) * G_0(t' - t) * ln(v) dt`.
- Continuous power-law impact kernel:
  - `G_0(t - t') = g_0 * S / (f**beta * |t' - t|**beta)`.
- Cost functional:
  - `0.5 * integral_0^T integral_0^T phi(t) * G_0(|t - t'|) * phi(t') dt dt'`.
- Constraint:
  - `integral_0^T phi(t) * v dt = V`.
- Euler equation:
  - `integral_0^T G_0(|t - t'|) * phi(t') dt' = z`.
- Exponential-impact pedagogical solution:
  - `phi*(t) = V / (1 + alpha*T/2) * [delta(t) + delta(T-t) + alpha/2]`.
- Power-law approximate solution:
  - `phi*(t) approximately V * Gamma(2*beta) / (T**(2*beta - 1) * Gamma(beta)**2) * t**(beta - 1) * (T - t)**(beta - 1)`.

Implement first:

```python
def normalized_power_law_u_schedule(
    *,
    n_slices: int,
    beta: float,
    endpoint_epsilon: float | None = None,
) -> np.ndarray:
    ...

def allocate_integer_child_quantities(
    *,
    total_quantity: int,
    weights: npt.ArrayLike,
    lot_size: int = 1,
) -> np.ndarray:
    ...

def plan_static_execution_schedule(
    *,
    side: Literal["buy", "sell"],
    total_quantity: int,
    start_ts_ms: int,
    end_ts_ms: int,
    n_slices: int,
    beta: float,
    lot_size: int = 1,
) -> list[ChildOrderInstruction]:
    ...
```

Then adaptive planner:

```python
def plan_adaptive_child_order(
    *,
    parent: ParentOrderState,
    market_state: ExecutionMarketState,
    schedule_state: ScheduleState,
    config: AdaptiveExecutionConfig,
) -> ChildOrderInstruction:
    ...
```

Bar-only version for the current repo:

```python
def plan_adaptive_child_order_from_bars(
    *,
    parent: ParentOrderState,
    latest_bar: BarExecutionMarketState,
    schedule_state: ScheduleState,
    config: AdaptiveExecutionConfig,
) -> ChildOrderInstruction:
    ...
```

Suggested dataclasses:

```python
@dataclass(frozen=True)
class ParentOrderIntent:
    symbol: str
    side: Literal["buy", "sell"]
    total_quantity: int
    start_ts_ms: int
    end_ts_ms: int
    max_participation_rate: float | None
    min_child_quantity: int = 1
    lot_size: int = 1

@dataclass(frozen=True)
class ExecutionMarketState:
    ts_ms: int
    bid: Decimal | None
    ask: Decimal | None
    last: Decimal | None
    midpoint: Decimal | None
    spread: Decimal | None
    recent_volume: int | None
    realized_volatility: float | None
    tick_size: Decimal

@dataclass(frozen=True)
class BarExecutionMarketState:
    ts_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    modeled_spread: Decimal | None
    modeled_realized_volatility: float | None
    tick_size: Decimal

@dataclass(frozen=True)
class ChildOrderInstruction:
    parent_id: str
    child_id: str
    ts_ms: int
    symbol: str
    side: Literal["buy", "sell"]
    quantity: int
    order_type: Literal["limit", "marketable_limit", "market"]
    limit_price: Decimal | None
    urgency: float
    reason_codes: tuple[str, ...]
```

Adaptive cost objective:

```text
expected_cost =
  spread_crossing_cost
  + temporary_impact_cost
  + timing_risk_cost
  + missed_fill_penalty
  + participation_penalty
```

Recommended Phase 1 behavior:

- Start from static U-shaped target cumulative schedule.
- At each decision time, compute target filled quantity by now.
- Compute schedule error: `target_filled - actual_filled`.
- In the current bar-only repo, use `modeled_spread` and `modeled_realized_volatility`; set all quote-derived fields to `model_status="modeled"` or `model_status="unavailable"`.
- If ahead and modeled spread is wide, rest passively or skip the slice.
- If behind and modeled spread is normal/tight, use a marketable-limit-style simulated child instruction.
- If deadline is close, raise urgency and allow crossing in the simulator.
- For small liquid orders such as 100 SPY shares over two hours, expect most cost savings to come from spread avoidance, not market impact reduction.
- Always cap child size by max participation if bar volume exists.
- If no modeled spread exists, produce a plan but mark `liquidity_state="unknown"` and avoid claiming minimized cost.

Validation:

- Static schedule:
  - weights sum to 1 at `atol=1e-12, rtol=0`.
  - symmetric weights.
  - for `beta < 1`, endpoint weights exceed middle weights.
  - integer allocation sums exactly to total quantity.
  - `total_quantity=100`, `n_slices=12`, `lot_size=1` produces 100 total shares.
- Adaptive planner:
  - behind schedule increases urgency.
  - wide modeled spread lowers aggression when not close to deadline.
  - near deadline chooses marketable limit or market depending config.
  - `max_participation_rate` caps quantity.
  - no quote data is expected in the first implementation; no modeled spread returns conservative instruction with reason code.

Do not implement live brokerage routing in this PR family. Emit instructions only.

## Current Fixture Added By This Design PR

Fixture path:

```text
PythonDataService/tests/fixtures/golden/microstructure/BFL-2008-MICRO-001/v1/
  input.json
  output.json
  attribution.md
```

Fixture intent:

- Capture paper-reported empirical validation benchmarks from the vendored TeX.
- Serve as the literature benchmark target inventory for future tests.
- Prevent "I vaguely remember the paper said..." implementation drift.

Fixture limitations:

- It is not a raw-data equivalence fixture.
- It does not prove our code can reproduce LSE/PSE/NYSE results.
- It must not be used as the only validation for implementations that claim empirical equivalence.

When Opus implements Phase 1 math, add:

```text
PythonDataService/tests/research/microstructure/test_bfl_paper_benchmarks.py
```

The test should load `output.json`, assert all required benchmark IDs exist, assert numeric field shapes/ranges, and use it as documentation. Formula-level tests must use separate synthetic or hand-computed fixtures.

## Paper-Reported Validation Studies To Preserve

These are already in `BFL-2008-MICRO-001/output.json`. Use this table when implementing tests and docs.

| Fixture benchmark id | Paper section | Dataset/context | Reported result | Implementation use |
|---|---|---|---|---|
| `order_flow_long_memory_lse_hurst` | 4.1 | LSE panel | `H approximately 0.7`, `gamma approximately 0.6` | sanity benchmark only |
| `order_flow_gamma_alpha_link` | 4.3 | LSE hidden-order/order-flow theory comparison | measured `gamma=0.57`, predicted `gamma=0.59` | sanity benchmark for `alpha=gamma+1` |
| `block_trade_half_cubic_tail` | 4.5 | LSE off-book/block trades | cumulative tail exponent near `1.5` | tail-fit target range |
| `individual_trade_impact_lse` | 5.1 | LSE large-cap stocks | `psi approximately 0.3`; small-volume `0.5`, large-volume `0.2` | impact model defaults/ranges |
| `selective_liquidity_taking_lse` | 6.1, 8.1 | LSE/AZN and related studies | `87%` and `97%` opposite-best penetration statements; later `85%` and `97%` gap statements | not testable without order book |
| `response_amplification_france_telecom` | 6.6 | France Telecom 2002 | `R_lag` grows by factor about `2` from lag 1 to 1000; MRR predicts `1.2-1.4` | propagator sanity |
| `spread_impact_pse` | 7.3.3 | PSE 68 stocks, 2002 | slope `2.86`, theory `2.64`, `R2=0.90` | phase-diagram benchmark |
| `spread_impact_nyse` | 7.3.3 | NYSE 155 stocks, 2005 | slope `3.3`, theory `2.78`, intercept `1.3 bp`, `R2=0.87` | specialist-market benchmark |
| `spread_recovery_azn` | 7.4 | AZN LSE spread shocks | power-law decay exponent `0.4-0.5` | spread-shock target |
| `impact_volatility_pse` | 8.3 | PSE 68 stocks, 2002 | `A approximately 10.9`, intercept near zero, `R2=0.96` | liquidity-volatility benchmark |
| `spread_volatility_pse` | 8.3 | PSE 68 stocks, 2002 | slope `1.58`, `R2=0.96` | spread-vol benchmark |
| `limit_order_placement_tails` | 9.1 | LSE/PSE/Mike-Farmer | `mu=1.5`, `mu=0.6`, Student df `1.3` | order-book placement target |
| `hidden_order_vaglica` | 11.1 | Spanish Stock Exchange brokerage data | hidden-order tails and scaling exponents | future hidden-order module |

## Implementation Phases

### Phase 0: Source And Design

Status: this PR.

Artifacts:

- Vendored arXiv source archive.
- Reference note.
- Opus-facing implementation design.
- Paper-reported benchmark fixture.

No production math should be added in Phase 0.

### Phase 1: Optimal Schedule Math

Goal:

- Implement static optimal execution schedule from Section 10.
- This is the most practical feature for the user's "100 shares over two hours" question.

Files:

- `PythonDataService/app/engine/execution/optimal_schedule.py`
- `PythonDataService/tests/engine/execution/test_optimal_schedule.py`
- `docs/references/microstructure-optimal-execution.md`
- Update `docs/math-sources-of-truth.md`.

Required functions:

- `normalized_power_law_u_schedule`.
- `allocate_integer_child_quantities`.
- `plan_static_execution_schedule`.

Acceptance:

- Unit tests pass.
- No new dependencies.
- All math provenance blocks present.
- Explicit tolerance on all float checks.

### Phase 2: Adaptive Order Planner

Goal:

- Convert static schedule into broker-agnostic child order instructions.

Files:

- `PythonDataService/app/engine/execution/adaptive_order.py`
- `PythonDataService/app/engine/execution/execution_cost.py`
- `PythonDataService/tests/engine/execution/test_adaptive_order.py`
- `docs/references/microstructure-adaptive-execution.md`

Required behavior:

- Static schedule as target.
- Bar-volume-aware aggression.
- Modeled-spread-aware aggression when no bid/ask exists.
- Deadline pressure.
- Fill-rate feedback.
- Max participation cap.
- Reason codes.

Acceptance:

- Deterministic tests for small `total_quantity=100`.
- Edge cases: no modeled spread, one slice, quantity less than slices, odd lot, wide modeled spread, near deadline.
- No live brokerage side effects.

### Phase 3: Spread And Impact Cost Model

Goal:

- Replace/augment the current internal spread model with paper-cited spread/impact components.

Files:

- `PythonDataService/app/engine/execution/execution_cost.py`
- Possibly extend `PythonDataService/app/engine/edge/spread_model.py`, but avoid breaking existing edge callers.
- `PythonDataService/tests/engine/execution/test_execution_cost.py`
- `docs/references/microstructure-market-impact.md`

Required components:

- Fixed spread crossing cost.
- Concave impact `k * signed_qty_abs**psi`.
- Participation-scaled impact.
- Optional transient decay kernel for schedule simulation.

Validation:

- Hand-computed formula tests.
- Paper benchmark ranges for `psi`.

Migration warning:

- `spread_model.py` currently cites Madhavan-Smidt and uses a square-root ADV-style model. Do not silently rewrite its behavior for existing callers. Either add new functions or introduce a config flag with tests.

### Phase 4: Microstructure Diagnostics

Goal:

- Deferred until Class B tick/quote data exists. Implement only formula-level helpers and synthetic tests if needed before then.

Files:

- `PythonDataService/app/research/microstructure/order_flow.py`
- `impact.py`
- `spread_impact.py`
- `liquidity.py`
- tests under `PythonDataService/tests/research/microstructure/`

Validation:

- Synthetic fixtures first.
- Paper benchmark fixture loaded as non-gating literature inventory.
- Raw-data empirical fixtures only if data is obtained.

### Phase 5: Order Book And Liquidity Simulation

Goal:

- Implement zero-intelligence and low-intelligence simulator primitives.

Files:

- `PythonDataService/app/research/microstructure/order_book.py`
- `PythonDataService/tests/research/microstructure/test_order_book.py`

Validation:

- Formula-level tests for zero-intelligence spread equation.
- Synthetic event-stream fixture.
- Do not claim to reproduce Mike-Farmer simulations without calibrated order event data.

### Phase 6: API Surface

Goal:

- Expose read-only execution planner and diagnostics through FastAPI.

Potential endpoints:

```text
POST /api/execution/optimal-schedule
POST /api/execution/adaptive-order/next-child
POST /api/research/microstructure/order-flow-memory       # future: requires tick/trade signs
POST /api/research/microstructure/impact-curve            # future: requires trades + quotes
POST /api/research/microstructure/spread-recovery         # future: requires quote series
```

Rules:

- Pydantic v2 schemas.
- Async routes.
- snake_case response fields.
- Boundary timestamps as `int64 ms UTC`.
- Routers are transport only.

### Phase 7: Backend/Frontend Passthrough And UI

Only after Python contracts are stable.

Backend:

- Thin GraphQL passthrough only.
- No arithmetic.

Frontend:

- Visualizes schedule, expected cost decomposition, and child order instructions.
- No strategy/execution math.

## What We Can Implement Now

With no new external data:

- Static optimal execution schedule.
- Integer child-order allocation.
- Broker-agnostic adaptive order decision logic using bars, observed fills, modeled spread, modeled volatility, and bar volume.
- Execution cost model formulas with all market-microstructure components marked `model_status="modeled"` or `model_status="unavailable"`.
- Baseline comparison against TWAP/VWAP-style bar schedules.
- Paper-reported benchmark fixture parser/test.

With synthetic-only data, for formula development but not empirical claims:

- Synthetic long-memory order-flow simulator.
- Synthetic impact/propagator tests.
- Spread/impact formula helpers.
- Zero-intelligence spread equation.

With top-of-book quotes and trades:

- Trade-sign autocorrelation.
- `gamma` and `H` estimates.
- Individual and aggregate impact curves.
- Spread/impact phase diagram.
- Spread shock recovery.
- Volatility per trade and spread-volatility relation.

With level-2 order book events:

- Virtual impact.
- Gap distribution and gap-driven return attribution.
- Limit-order placement distance tail.
- Average book shape.
- Order-book simulator calibration.

With broker/member identity data:

- Same-member versus different-member order-flow autocorrelation.
- Hidden-order identification.
- Strategy/ecology clustering.

## What We Cannot Honestly Implement Or Validate Yet

- We do not have tick-level trade, quote, or order-book data today; all microstructure empirical studies are future/data-gated.
- We cannot reproduce the authors' LSE/PSE/NYSE figures from the arXiv source alone. The raw data is not included.
- We cannot infer true market order signs from OHLCV bars.
- We cannot measure bid-ask spread from bars unless bid/ask data is supplied or a model is assumed.
- We cannot compute virtual impact without order-book depth.
- We cannot detect hidden orders without broker/member/institution identifiers or a strong proxy dataset.
- We cannot guarantee that an adaptive order minimizes realized cost. We can minimize a model-based expected cost under stated assumptions and compare against baselines.
- We cannot route live orders without a separate broker integration and operational safeguards.

## Detailed Adaptive Execution Design

### Parent Order Inputs

Minimum request:

```json
{
  "symbol": "SPY",
  "side": "buy",
  "total_quantity": 100,
  "start_ts_ms": 1715005800000,
  "end_ts_ms": 1715013000000,
  "n_slices": 12,
  "beta": 0.25,
  "max_participation_rate": 0.05,
  "lot_size": 1,
  "allow_market_orders": false
}
```

Important defaults:

- `beta`: default from impact decay assumption. If no calibrated `gamma`, choose conservative `beta=0.25` and document it as a modeling default, not empirical truth.
- `n_slices`: default one decision every 10 minutes for a two-hour horizon.
- `allow_market_orders`: false by default. Use marketable limit first.
- `max_participation_rate`: optional. If recent volume is absent, cap by absolute child quantity only.

### Static Schedule Construction

Use the paper's power-law U-shape as the initial target.

For discrete slices:

1. Map slices to centers in `(0, 1)`, avoiding exact endpoints unless explicit block-at-open/close behavior is desired.
2. Compute unnormalized weight `w_i = t_i**(beta - 1) * (1 - t_i)**(beta - 1)`.
3. Normalize `w / sum(w)`.
4. Convert to integer child quantities with deterministic largest-remainder allocation.
5. Enforce `sum(q_i) == total_quantity`.

Potential issue:

- The continuous density diverges at endpoints for `beta < 1`. Discrete implementation must use slice centers or an `endpoint_epsilon`. Do not evaluate at exactly `0` or `T`.

### Adaptive Overlay

State variables:

- `elapsed_fraction`.
- `target_cumulative_quantity`.
- `actual_filled_quantity`.
- `remaining_quantity`.
- `remaining_time_fraction`.
- `schedule_error = target_cumulative - actual_filled`.
- `spread_bps`.
- `spread_zscore` if recent spread history exists.
- `realized_volatility`.
- `recent_volume`.
- `fill_rate` for prior child orders.

Urgency:

```text
base_urgency = clamp(schedule_error / max(remaining_quantity, 1), 0, 1)
deadline_urgency = 1 - remaining_time_fraction
spread_relief = negative adjustment when spread_zscore is high
fill_shortfall = positive adjustment when passive orders are not filling
urgency = clamp(weighted_sum(...), 0, 1)
```

Order style:

- Low urgency, normal/tight spread: passive limit at bid for buy, ask for sell.
- Medium urgency: midpoint or one-tick-improved limit if supported by venue assumptions.
- High urgency: marketable limit at ask for buy, bid for sell.
- Final slice or severe shortfall: marketable limit. Market order only if config allows.

Limit price:

For buy:

- Passive: `bid`.
- Midpoint: `floor_to_tick((bid + ask) / 2)`.
- Marketable limit: `ask` or `ask + n_ticks * tick_size`.

For sell:

- Passive: `ask`.
- Midpoint: `ceil_to_tick((bid + ask) / 2)`.
- Marketable limit: `bid` or `bid - n_ticks * tick_size`.

All Decimal price arithmetic must be explicit. Do not mix float and Decimal.

### Cost Attribution

Return expected costs even if rough:

```text
expected_spread_cost
expected_impact_cost
expected_timing_risk_cost
expected_missed_fill_penalty
expected_total_cost
```

Each component must carry `model_status`:

- `observed`: computed from supplied bid/ask/trade data.
- `modeled`: computed from configured model.
- `unavailable`: cannot compute from inputs.

Do not hide unavailable components by setting them to zero.

## Test Strategy

### Formula Tests

- Strict float where formulas are deterministic: `atol=1e-12, rtol=0` where stable.
- Use explicit tolerances everywhere.
- Preserve paper indexing in tests.

### Synthetic Behavioral Tests

- Long-memory simulation tolerances are behavioral because finite samples are noisy.
- Seed every RNG.
- State tolerance rationale in test comments and docs reference.

### Paper Benchmark Tests

- Load `BFL-2008-MICRO-001`.
- Assert expected benchmark IDs and fields exist.
- Assert numeric values match fixture exactly, since the fixture is the extracted source of truth.
- Do not assert that our computed diagnostics match paper benchmarks unless using the same raw data.

### Future Raw-Data Fixture Tests

If we obtain raw LSE/PSE/NYSE/Spanish datasets:

- Create dataset-specific fixture directories under `PythonDataService/tests/fixtures/golden/microstructure/<ID>/`.
- Store input data in Arrow/Parquet where possible.
- Store reference outputs generated by one-time scripts.
- Include attribution with data license, date range, symbols, timezone, event-time convention, trade-sign classification method, and exact command.
- Add reconciliation docs under `docs/references/reconciliations/`.

## Documentation Requirements Per Phase

Every implementation PR must update:

- `docs/math-sources-of-truth.md`: new math concepts.
- `docs/architecture/engine-authority-map.md`: new execution/research owner.
- `docs/references/<concept>.md`: paper sections, formulas, assumptions, deviations, validation.
- The module docstring with provenance block.

Do not bury caveats only in PR descriptions. Put caveats in docs and module docstrings.

## Suggested Math Registry Rows

Add these only when the implementation files exist:

| Concept | Canonical | Reference | Validated against | Status |
|---|---|---|---|---|
| Optimal execution U-shaped schedule | `PythonDataService/app/engine/execution/optimal_schedule.py` | BFL 2008 Section 10 | `PythonDataService/tests/engine/execution/test_optimal_schedule.py` | canonical |
| Adaptive child-order planner | `PythonDataService/app/engine/execution/adaptive_order.py` | BFL 2008 Section 10 plus internal execution policy | `PythonDataService/tests/engine/execution/test_adaptive_order.py` | canonical |
| Concave market impact cost | `PythonDataService/app/engine/execution/execution_cost.py` | BFL 2008 Sections 5.1, 6.1 | `PythonDataService/tests/engine/execution/test_execution_cost.py` | canonical |
| Order-flow long-memory diagnostics | `PythonDataService/app/research/microstructure/order_flow.py` | BFL 2008 Section 4 | `PythonDataService/tests/research/microstructure/test_order_flow.py` | canonical |
| Spread-impact phase diagram | `PythonDataService/app/research/microstructure/spread_impact.py` | BFL 2008 Section 7.3 | `PythonDataService/tests/research/microstructure/test_spread_impact.py` | canonical |
| Spread shock recovery | `PythonDataService/app/research/microstructure/liquidity.py` | BFL 2008 Section 7.4 | `PythonDataService/tests/research/microstructure/test_liquidity.py` | canonical |
| Zero-intelligence spread equation | `PythonDataService/app/research/microstructure/order_book.py` | BFL 2008 Section 9.3.1 | `PythonDataService/tests/research/microstructure/test_order_book.py` | canonical |

## PR Sequencing

Recommended PRs:

1. This design PR.
2. Optimal schedule formula and tests.
3. Adaptive order planner and cost attribution.
4. Concave impact and spread model integration.
5. Tick/quote microstructure schemas and order-flow diagnostics.
6. Impact and spread-impact diagnostics.
7. Order-book simulator primitives.
8. FastAPI endpoints.
9. Backend/Frontend passthrough and UI.

Do not combine all phases into one massive implementation PR. The design is large; implementation should be reviewable.

## Specific Instructions To Opus

When implementing:

1. Start with Phase 1. Do not jump directly to order-book simulation.
2. Before writing code, open `references/arxiv-0809.0822v1/source/arXiv-0809.0822v1.tar.gz` and inspect `handbook20.tex` around the relevant section.
3. Use the exact section labels and equations cited here in docstrings.
4. Keep the first PR pure Python math and tests.
5. Do not add dependencies unless a built-in/scipy/numpy implementation is materially worse. The repo already has numpy/scipy/pandas.
6. If adding an optimizer for adaptive execution, implement an explicit deterministic heuristic first; do not introduce convex optimization dependencies in Phase 1 or 2.
7. Surface all data limitations in docs. Do not let a bar-only input path pretend to be a trade/quote study.
8. Use `Decimal` for prices and integer quantities in order instructions.
9. Use `float64` for research statistics and fittings.
10. Run at least:
    - `ruff check PythonDataService/app/ PythonDataService/tests/`
    - the targeted pytest files
    - broader Python tests if implementation touches shared execution engine behavior

## Open Questions For Later Human Decision

These should not block Phase 1.

- Which live or historical source will provide top-of-book quotes and trades for empirical microstructure validation?
- Do we want IBKR paper trading to consume adaptive child order instructions, or should routing remain outside learn-ai?
- What risk preference should default adaptive execution use for small retail orders?
- Should adaptive execution optimize for arrival price, midpoint, VWAP, or implementation shortfall versus decision price?
- Should execution planning be allowed to leave a residual unfilled quantity if spreads are pathological, or must it always complete?
- What compliance/safety rails are required before any live brokerage integration?

## Final Acceptance Definition

This paper is successfully integrated when:

- The source is vendored and cited.
- The paper-reported benchmarks are preserved as a fixture.
- Static optimal execution is implemented with strict formula tests.
- Adaptive execution produces deterministic child order instructions and cost attribution.
- Spread/impact and liquidity diagnostics reject insufficient data rather than fabricating results.
- Every mathematical concept is in the registry and has provenance.
- Raw-data validation is added if and only if licensed/raw datasets are obtained.
