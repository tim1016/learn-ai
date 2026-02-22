# Black-Scholes Implementation & Current P&L Curve

Reference document for verifying the math in `Frontend/src/app/utils/black-scholes.ts` and the chart data pipeline in `options-strategy-lab.component.ts`.

---

## 1. Normal Distribution Primitives

### Standard Normal PDF

$$\phi(x) = \frac{1}{\sqrt{2\pi}} e^{-x^2 / 2}$$

**Code** (`normPdf`):
```
normPdf(x) = exp(-x² / 2) / sqrt(2π)
```

### Standard Normal CDF

$$\Phi(x) = \int_{-\infty}^{x} \phi(u)\, du$$

**Code** (`normCdf`): Uses the Abramowitz & Stegun rational approximation (equation 26.2.17) with error < 7.5×10⁻⁸:

```
Constants: a1=0.254829592, a2=-0.284496736, a3=1.421413741, a4=-1.453152027, a5=1.061405429, p=0.3275911

t = 1 / (1 + p·|x|)
y = 1 - (((((a5·t + a4)·t + a3)·t + a2)·t + a1) · t · exp(-x²/2))
Φ(x) = 0.5 · (1 + sign(x) · y)
```

**Edge clamping**: x < -8 → 0, x > 8 → 1.

---

## 2. Black-Scholes d₁ and d₂

### Formulae

$$d_1 = \frac{\ln(S / K) + (r + \frac{1}{2}\sigma^2) \cdot t}{\sigma \sqrt{t}}$$

$$d_2 = d_1 - \sigma\sqrt{t}$$

Where:
- **S** = underlying (spot) price
- **K** = strike price
- **r** = risk-free interest rate (annualized, decimal)
- **σ** = implied volatility (annualized, decimal, e.g. 0.30 = 30%)
- **t** = time to expiration (in years, i.e. `DTE / 365`)

**Code** (`bsD1`, `bsD2`):
```
bsD1(S, K, r, σ, t) = (ln(S/K) + (r + 0.5·σ²)·t) / (σ·√t)
bsD2(S, K, r, σ, t) = bsD1(S, K, r, σ, t) - σ·√t
```

**Guard**: Returns 0 if σ ≤ 0, t ≤ 0, S ≤ 0, or K ≤ 0.

---

## 3. Option Pricing

### Black-Scholes European Call

$$C = S \cdot \Phi(d_1) - K \cdot e^{-rT} \cdot \Phi(d_2)$$

### Black-Scholes European Put

$$P = K \cdot e^{-rT} \cdot \Phi(-d_2) - S \cdot \Phi(-d_1)$$

**Code** (`bsPrice`):
```
discount = exp(-r·t)

Call: S·Φ(d₁) - K·discount·Φ(d₂)
Put:  K·discount·Φ(-d₂) - S·Φ(-d₁)
```

**At expiration (t ≤ 0)**: Returns intrinsic value:
```
Call: max(S - K, 0)
Put:  max(K - S, 0)
```

---

## 4. Greeks

All Greeks are computed per single option (unsigned, before position/quantity adjustments).

### Delta — ∂V/∂S

$$\Delta_{\text{call}} = \Phi(d_1)$$

$$\Delta_{\text{put}} = \Phi(d_1) - 1$$

**Code** (`bsDelta`):
```
Call: Φ(d₁)
Put:  Φ(d₁) - 1
```

**At expiration**: Call → 1 if S > K else 0. Put → -1 if S < K else 0.

### Gamma — ∂²V/∂S²

$$\Gamma = \frac{\phi(d_1)}{S \cdot \sigma \cdot \sqrt{t}}$$

**Code** (`bsGamma`):
```
Γ = φ(d₁) / (S · σ · √t)
```

Same for calls and puts.

### Theta — ∂V/∂t (per calendar day)

$$\Theta_{\text{call}} = \frac{1}{365}\left(-\frac{S \cdot \phi(d_1) \cdot \sigma}{2\sqrt{t}} - r \cdot K \cdot e^{-rt} \cdot \Phi(d_2)\right)$$

$$\Theta_{\text{put}} = \frac{1}{365}\left(-\frac{S \cdot \phi(d_1) \cdot \sigma}{2\sqrt{t}} + r \cdot K \cdot e^{-rt} \cdot \Phi(-d_2)\right)$$

**Code** (`bsTheta`):
```
common = -S · φ(d₁) · σ / (2·√t)

Call: (common - r·K·exp(-r·t)·Φ(d₂))  / 365
Put:  (common + r·K·exp(-r·t)·Φ(-d₂)) / 365
```

**Note**: Divided by 365 to give per-calendar-day decay (not per-year).

### Vega — ∂V/∂σ (per 1 percentage-point IV move)

$$\mathcal{V} = \frac{S \cdot \phi(d_1) \cdot \sqrt{t}}{100}$$

**Code** (`bsVega`):
```
V = S · φ(d₁) · √t / 100
```

Same for calls and puts. Divided by 100 so the output is the P&L change for a 1pp IV move (e.g. 30% → 31%).

### Rho — ∂V/∂r (per 1 percentage-point rate move)

$$\rho_{\text{call}} = \frac{K \cdot t \cdot e^{-rt} \cdot \Phi(d_2)}{100}$$

$$\rho_{\text{put}} = \frac{-K \cdot t \cdot e^{-rt} \cdot \Phi(-d_2)}{100}$$

**Code** (`bsRho`):
```
Call:  K·t·exp(-r·t)·Φ(d₂)  / 100
Put:  -K·t·exp(-r·t)·Φ(-d₂) / 100
```

Divided by 100 for per-1pp rate move.

---

## 5. Lognormal CDF — P(S_T < x)

Under risk-neutral GBM, the terminal price S_T is lognormally distributed. The probability that S_T < x is:

$$P(S_T < x) = \Phi\left(\frac{\ln(x/S) - (r - \frac{1}{2}\sigma^2) \cdot t}{\sigma\sqrt{t}}\right)$$

**Code** (`lognormalCdf`):
```
d = (ln(x/S) - (r - 0.5·σ²)·t) / (σ·√t)
P(S_T < x) = Φ(d)
```

**Note**: This uses `(r - 0.5σ²)` (the drift of ln S_T), not `(r + 0.5σ²)` like d₁. This is the d₂-style term for the terminal distribution.

---

## 6. Strategy-Level P&L Composition

### Per-Leg P&L

For each leg with parameters (K_i, σ_i, type_i, position_i, premium_i, qty_i):

$$V_i(S, t) = \text{bsPrice}(S, K_i, r, \sigma_i, t, \text{type}_i)$$

$$\text{PnL}_i = \begin{cases} (V_i - \text{premium}_i) \times \text{qty}_i & \text{if long} \\ (\text{premium}_i - V_i) \times \text{qty}_i & \text{if short} \end{cases}$$

### Total Strategy P&L

$$\text{PnL}_{\text{total}}(S, t) = \sum_{i} \text{PnL}_i$$

**Code** (`strategyPnlAtPrice`):
```
For each leg:
  value = bsPrice(S, leg.strike, r, leg.iv, t, leg.optionType)
  pnl = (long ? value - premium : premium - value) × quantity
total = Σ pnl
```

**Key detail**: Each leg uses its **own IV** (σ_i), not a shared IV. This means the strategy correctly handles IV skew across different strikes.

### Strategy-Level Greeks

$$G_{\text{total}}(S, t) = \sum_{i} \text{sign}_i \times \text{qty}_i \times G(S, K_i, r, \sigma_i, t)$$

Where sign = +1 for long, -1 for short, and G is any Greek function.

---

## 7. Chart Data Pipeline

### Price Grid (X-Axis)

```
low  = spot × (1 - rangePct)      // e.g. spot × 0.80 for ±20%
high = spot × (1 + rangePct)      // e.g. spot × 1.20

Base grid:  401 uniform points from low to high, rounded to 2 decimals
Dense zone: Per unique enabled strike, 201 extra points in [strike - 0.02×spot, strike + 0.02×spot]
Dedup:      Set<number> → sorted array
```

Total points: ~500–800 depending on number of strikes and overlap.

### Expiration P&L (Green Line)

Pure intrinsic value, no Black-Scholes:

$$\text{PnL}_{\text{exp}}(S) = \sum_i \text{sign}_i \times \text{qty}_i \times (\text{intrinsic}_i(S) - \text{premium}_i)$$

Where:
```
intrinsic_call(S) = max(S - K, 0)
intrinsic_put(S)  = max(K - S, 0)
sign = +1 for long, -1 for short (applied to the full (intrinsic - premium) term)
```

This is piecewise linear — always has sharp corners at each strike.

### Current P&L (Blue Dashed Line)

$$\text{PnL}_{\text{current}}(S) = \text{strategyPnlAtPrice}(\text{legs}, S, t, r)$$

Where:

```
daysToExpiry = max((expirationDate_16:00 - now) / 86400000, 0)   // fractional days
t = daysToExpiry / 365                                            // years
```

**Fractional time**: `daysToExpiry` uses real-time precision (hours/minutes, not rounded to integer days). Expiration is anchored to 4:00 PM (market close) on the expiration date. This matches how platforms like TradingView price options — as t → 0 the curve smoothly converges to the expiration hockey-stick rather than snapping abruptly.

When t = 0, the current P&L curve is hidden entirely (returns empty array) since it would be identical to the expiration P&L.

### What-If Scenario Curves

Same as Current P&L, but with adjusted parameters:

```
adjusted_dte = max(dte - scenario.timeDeltaDays, 0)
adjusted_t   = adjusted_dte / 365
adjusted_iv  = leg.iv + scenario.ivShift   (clamped to ≥ 0.01)
```

### Greek Curve (Right Y-Axis)

$$G(S) = \text{strategyGreekAtPrice}(\text{legs}, S, t, r, \text{selectedGreek})$$

Uses the same real `t` as Current P&L.

---

## 8. Breakeven Calculation

Linear interpolation between adjacent grid points where P&L crosses zero:

```
For consecutive points (p1, p2) where sign(pnl) changes:
  ratio = |p1.pnl| / (|p1.pnl| + |p2.pnl|)
  breakeven = p1.price + (p2.price - p1.price) × ratio
```

This is computed from the **expiration P&L** curve (intrinsic values), not the BS-priced curve.

---

## 9. Variable Reference

| Symbol | Code variable | Description | Units |
|--------|--------------|-------------|-------|
| S | `spot`, `underlyingPrice` | Current underlying price | $ |
| K | `strike`, `leg.strike` | Option strike price | $ |
| r | `riskFreeRate()` | Risk-free rate (default 0.043) | decimal |
| σ | `iv`, `leg.iv`, `sigma` | Implied volatility | decimal (0.30 = 30%) |
| t | `timeToExpiry()` | Time to expiration (fractional) | years (DTE/365) |
| qty | `leg.quantity` | Number of contracts | integer |
| premium | `leg.premium` | Entry price paid/received per contract | $ |

---

## 10. Known Simplifications

1. **No dividends**: The standard BS model assumes no dividends. For dividend-paying underlyings, the model slightly overprices calls and underprices puts.
2. **European-style only**: BS assumes European exercise. American options (e.g. equity options) can be exercised early, which BS doesn't account for.
3. **Per-leg IV**: Each leg uses its own IV from the market snapshot. There is no vol surface interpolation or IV smile modeling.
4. **Calendar-day theta**: Theta is divided by 365 (calendar days), not 252 (trading days). This is a common convention but slightly understates weekday decay.
5. **Chart x-axis**: Uses `type: 'linear'` (not category) so the adaptive grid's non-uniform spacing renders at correct proportional distances. Data is passed as `{x, y}` point objects.
