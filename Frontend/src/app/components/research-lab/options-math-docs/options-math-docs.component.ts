import { Component, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { AccordionModule } from 'primeng/accordion';
import { DividerModule } from 'primeng/divider';
import { KatexDirective } from '../../../shared/katex.directive';

interface SymbolEntry {
  symbolLatex: string;
  name: string;
  definition: string;
}

interface FormulaDoc {
  label: string;
  formulaLatex: string;
  note: string;
  variablesLatex?: string[];
}

interface GreekDoc {
  name: string;
  symbol: string;
  callFormulaLatex: string;
  putFormulaLatex: string;
  interpretation: string;
  codeNote: string;
}

interface FilterRule {
  stage: string;
  filter: string;
  threshold: string;
}

interface PipelineStep {
  label: string;
  detail: string;
}

interface UpgradeDoc {
  id: number;
  title: string;
  impact: string;
  effort: string;
  problem: string;
  currentFormulaLatex?: string;
  correctedFormulaLatex?: string;
  explanation: string;
  phase: string;
}

@Component({
  selector: 'app-options-math-docs',
  standalone: true,
  imports: [CommonModule, AccordionModule, DividerModule, KatexDirective],
  templateUrl: './options-math-docs.component.html',
  styleUrls: ['./options-math-docs.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class OptionsMathDocsComponent {

  // ─── Symbol Glossary ───────────────────────────────────────
  symbolGlossary: SymbolEntry[] = [
    { symbolLatex: 'S', name: 'Spot Price', definition: 'Current underlying (stock) price' },
    { symbolLatex: 'K', name: 'Strike Price', definition: 'Option strike price' },
    { symbolLatex: 'r', name: 'Risk-Free Rate', definition: 'Annualized risk-free rate from FRED Treasury curve, interpolated to option DTE (fallback: 0.043)' },
    { symbolLatex: String.raw`\sigma`, name: 'Implied Volatility', definition: 'Annualized implied volatility (decimal, e.g. 0.30 = 30%)' },
    { symbolLatex: 'T', name: 'Time to Expiry', definition: 'Time to expiration in years (DTE / 365)' },
    { symbolLatex: String.raw`\Phi(x)`, name: 'Normal CDF', definition: 'Standard normal cumulative distribution function' },
    { symbolLatex: String.raw`\phi(x)`, name: 'Normal PDF', definition: 'Standard normal probability density function' },
    { symbolLatex: 'C', name: 'Call Price', definition: 'European call option price under Black-Scholes' },
    { symbolLatex: 'P', name: 'Put Price', definition: 'European put option price under Black-Scholes' },
    { symbolLatex: String.raw`\nu`, name: 'Vega', definition: 'Option price sensitivity to 1pp change in IV' },
    { symbolLatex: String.raw`\text{IV}_{30d}`, name: '30-Day IV', definition: 'Constant-maturity 30-day implied volatility (interpolated)' },
    { symbolLatex: String.raw`\text{RV}_N`, name: 'Realized Vol', definition: 'Annualized realized volatility over N-day window' },
    { symbolLatex: String.raw`\text{IC}`, name: 'Information Coefficient', definition: 'Spearman rank correlation between feature and a specified forward target (directional, volatility, or absolute return)' },
    { symbolLatex: String.raw`\text{VP}`, name: 'Volatility Premium', definition: 'IV − RV: difference in volatility space (not variance). See note below.' },
  ];
  symbolNote = 'All volatilities are annualized decimals. All rates are annualized decimals. Time is measured in years unless stated otherwise. The feature labeled "VRP" in the codebase is technically a volatility premium (IV − RV), not a true variance risk premium (IV² − RV²). See the Volatility Premium section for details.';

  // ─── Normal Distribution ───────────────────────────────────
  normalPdf: FormulaDoc = {
    label: 'Standard Normal PDF',
    formulaLatex: String.raw`\phi(x) = \frac{1}{\sqrt{2\pi}} e^{-x^2 / 2}`,
    note: 'The bell curve density function. Used internally by all Greek calculations involving N\'(d₁).',
  };

  normalCdf: FormulaDoc = {
    label: 'Standard Normal CDF',
    formulaLatex: String.raw`\Phi(x) = \int_{-\infty}^{x} \phi(u)\, du`,
    note: 'Computed via Abramowitz & Stegun rational approximation (eq. 26.2.17) with error < 7.5×10⁻⁸. Edge clamping: x < −8 → 0, x > 8 → 1.',
  };

  // ─── Black-Scholes Core ────────────────────────────────────
  d1Formula: FormulaDoc = {
    label: 'd₁ — Moneyness-Adjusted Distance',
    formulaLatex: String.raw`d_1 = \frac{\ln(S / K) + \left(r + \tfrac{1}{2}\sigma^2\right) T}{\sigma \sqrt{T}}`,
    note: 'Measures how far in-the-money the option is, adjusted for drift and volatility. The (r + ½σ²) term reflects the expected growth rate of the stock under the risk-neutral measure.',
    variablesLatex: [
      String.raw`S = \text{underlying spot price}`,
      String.raw`K = \text{option strike price}`,
      String.raw`r = \text{risk-free rate (annualized)}`,
      String.raw`\sigma = \text{implied volatility (annualized)}`,
      String.raw`T = \text{DTE} / 365`,
    ],
  };

  d2Formula: FormulaDoc = {
    label: 'd₂ — Risk-Neutral Exercise Probability',
    formulaLatex: String.raw`d_2 = d_1 - \sigma\sqrt{T}`,
    note: 'Φ(d₂) gives the risk-neutral probability that a call finishes in-the-money. The σ√T shift from d₁ accounts for the volatility of the forward distribution.',
  };

  callPriceFormula: FormulaDoc = {
    label: 'European Call Price',
    formulaLatex: String.raw`C = S \cdot \Phi(d_1) - K \cdot e^{-rT} \cdot \Phi(d_2)`,
    note: 'The first term is the expected asset value conditional on exercise (delta-weighted). The second term is the present value of the strike payment weighted by exercise probability.',
  };

  putPriceFormula: FormulaDoc = {
    label: 'European Put Price',
    formulaLatex: String.raw`P = K \cdot e^{-rT} \cdot \Phi(-d_2) - S \cdot \Phi(-d_1)`,
    note: 'Derived from put-call parity: P = C − S + K·e^(−rT). Φ(−d₂) is the probability that the put finishes in-the-money.',
  };

  bsGuards = [
    'σ ≤ 0, T ≤ 0, S ≤ 0, or K ≤ 0 → returns 0',
    'At expiration (T ≤ 0): Call → max(S − K, 0), Put → max(K − S, 0)',
  ];

  // ─── Greeks ────────────────────────────────────────────────
  greeks: GreekDoc[] = [
    {
      name: 'Delta',
      symbol: String.raw`\Delta`,
      callFormulaLatex: String.raw`\Delta_{\text{call}} = \Phi(d_1)`,
      putFormulaLatex: String.raw`\Delta_{\text{put}} = \Phi(d_1) - 1`,
      interpretation: 'Rate of change of option price with respect to the underlying. Call delta ranges [0, 1]; put delta ranges [−1, 0]. At-the-money options have |Δ| ≈ 0.5.',
      codeNote: 'At expiration: Call → 1 if S > K else 0. Put → −1 if S < K else 0.',
    },
    {
      name: 'Gamma',
      symbol: String.raw`\Gamma`,
      callFormulaLatex: String.raw`\Gamma = \frac{\phi(d_1)}{S \cdot \sigma \cdot \sqrt{T}}`,
      putFormulaLatex: String.raw`\text{(same for calls and puts)}`,
      interpretation: 'Rate of change of delta with respect to the underlying. Peaks at-the-money and near expiration. Measures convexity exposure — how quickly delta changes.',
      codeNote: 'Identical for calls and puts at the same strike/expiry.',
    },
    {
      name: 'Theta',
      symbol: String.raw`\Theta`,
      callFormulaLatex: String.raw`\Theta_{\text{call}} = \frac{1}{365}\left(-\frac{S \cdot \phi(d_1) \cdot \sigma}{2\sqrt{T}} - r \cdot K \cdot e^{-rT} \cdot \Phi(d_2)\right)`,
      putFormulaLatex: String.raw`\Theta_{\text{put}} = \frac{1}{365}\left(-\frac{S \cdot \phi(d_1) \cdot \sigma}{2\sqrt{T}} + r \cdot K \cdot e^{-rT} \cdot \Phi(-d_2)\right)`,
      interpretation: 'Time decay per calendar day. Always negative for long options — the option loses value as time passes. Accelerates near expiration (the "theta burn" curve).',
      codeNote: 'Divided by 365 for per-calendar-day decay (not per-year or per-trading-day).',
    },
    {
      name: 'Vega',
      symbol: String.raw`\mathcal{V}`,
      callFormulaLatex: String.raw`\mathcal{V} = \frac{S \cdot \phi(d_1) \cdot \sqrt{T}}{100}`,
      putFormulaLatex: String.raw`\text{(same for calls and puts)}`,
      interpretation: 'Sensitivity to a 1 percentage-point change in IV. Highest ATM and for longer-dated options. Divided by 100 so output is PnL per 1pp IV move (e.g. 30% → 31%).',
      codeNote: 'Identical for calls and puts at the same strike/expiry.',
    },
    {
      name: 'Rho',
      symbol: String.raw`\rho`,
      callFormulaLatex: String.raw`\rho_{\text{call}} = \frac{K \cdot T \cdot e^{-rT} \cdot \Phi(d_2)}{100}`,
      putFormulaLatex: String.raw`\rho_{\text{put}} = \frac{-K \cdot T \cdot e^{-rT} \cdot \Phi(-d_2)}{100}`,
      interpretation: 'Sensitivity to a 1pp change in the risk-free rate. Calls have positive rho (benefit from rate increases); puts have negative rho. More significant for longer-dated options.',
      codeNote: 'Divided by 100 for per-1pp rate move interpretation.',
    },
  ];

  // ─── Strategy Greeks ───────────────────────────────────────
  strategyGreekFormula: FormulaDoc = {
    label: 'Strategy-Level Greeks',
    formulaLatex: String.raw`G_{\text{total}}(S, T) = \sum_{i} \text{sign}_i \times \text{qty}_i \times G(S, K_i, r, \sigma_i, T)`,
    note: 'Where sign = +1 for long, −1 for short. Each leg uses its own IV (σᵢ), correctly handling IV skew across different strikes.',
    variablesLatex: [
      String.raw`\text{sign}_i = +1 \text{ (long)}, -1 \text{ (short)}`,
      String.raw`\text{qty}_i = \text{number of contracts for leg } i`,
      String.raw`G = \text{any Greek function (Delta, Gamma, Theta, Vega, Rho)}`,
    ],
  };

  // ─── IV Solver ─────────────────────────────────────────────
  ivSolverIntro = 'The IV solver inverts the Black-Scholes formula — given a market price, it finds the volatility σ that makes BS(σ) = market price. This is a root-finding problem solved in two stages.';

  brennerFormula: FormulaDoc = {
    label: 'Stage 1: Initial Guess (Brenner-Subrahmanyam)',
    formulaLatex: String.raw`\sigma_0 = \sqrt{\frac{2\pi}{T}} \cdot \frac{\text{price}}{S} \qquad \text{clamped to } [0.15, 3.0]`,
    note: 'An analytical approximation for ATM options. Provides a good starting point for Newton-Raphson iteration.',
  };

  newtonFormula: FormulaDoc = {
    label: 'Stage 1: Newton-Raphson Iteration',
    formulaLatex: String.raw`\sigma_{n+1} = \sigma_n - \frac{\text{BS}(\sigma_n) - \text{price}_{\text{market}}}{\nu(\sigma_n)}`,
    note: 'Converges quadratically near the root. Uses vega as the derivative. Typically converges in 3-5 iterations.',
    variablesLatex: [
      String.raw`\nu(\sigma) = S \cdot \sqrt{T} \cdot \phi(d_1) \quad \text{(vega — the BS price sensitivity to } \sigma \text{)}`,
    ],
  };

  brentFormula: FormulaDoc = {
    label: 'Stage 2: Brent Bisection (Fallback)',
    formulaLatex: String.raw`\text{brentq}\big(\sigma \mapsto \text{BS}(\sigma) - \text{price}_{\text{market}},\; 0.01,\; 5.0\big)`,
    note: 'If Newton-Raphson diverges or fails to converge, falls back to scipy.optimize.brentq — guaranteed to find the root within [0.01, 5.0] if one exists.',
  };

  ivSolverGuards = [
    'T < 7/365 → reject (too close to expiry for reliable IV)',
    'price ≤ 0 → reject',
    'price < intrinsic − ε → reject (arbitrage violation)',
    'Final σ ∉ [0.05, 3.0] → reject (implausible volatility)',
  ];

  // ─── 30-Day IV Construction ────────────────────────────────
  ivConstructionIntro = 'The system constructs a constant-maturity 30-day IV series by finding two option expiries that bracket the 30-day mark, solving IV for each, and interpolating to the target maturity.';

  previousInterpolation: FormulaDoc = {
    label: 'Linear-in-Volatility Interpolation (Replaced)',
    formulaLatex: String.raw`\text{IV}_{30d} = w_{\text{low}} \cdot \sigma_{\text{low}} + w_{\text{high}} \cdot \sigma_{\text{high}}`,
    note: 'Previously used simple linear interpolation in vol space. Introduced downward bias when the term structure has curvature (Jensen\'s inequality). Replaced by variance-time interpolation.',
    variablesLatex: [
      String.raw`w_{\text{low}} = \frac{\text{DTE}_{\text{high}} - 30}{\text{DTE}_{\text{high}} - \text{DTE}_{\text{low}}}`,
      String.raw`w_{\text{high}} = \frac{30 - \text{DTE}_{\text{low}}}{\text{DTE}_{\text{high}} - \text{DTE}_{\text{low}}}`,
    ],
  };

  varianceInterpolation: FormulaDoc = {
    label: 'Variance-Time Interpolation (Active)',
    formulaLatex: String.raw`\sigma_{30} = \sqrt{\frac{w_{\text{low}} \cdot \sigma_{\text{low}}^2 \cdot T_{\text{low}} + w_{\text{high}} \cdot \sigma_{\text{high}}^2 \cdot T_{\text{high}}}{T_{30}}}`,
    note: 'Industry standard for constructing constant-maturity vol surfaces. Interpolates in total variance (σ²T) space, then extracts σ. Eliminates the systematic downward bias from linear-in-vol interpolation.',
    variablesLatex: [
      String.raw`T_x = \text{DTE}_x / 365 \quad \text{(time in years)}`,
      String.raw`T_{30} = 30 / 365`,
      String.raw`w_{\text{low}}, w_{\text{high}} = \text{same linear weights as above}`,
    ],
  };

  jensenInequality: FormulaDoc = {
    label: 'Why Variance Interpolation? (Jensen\'s Inequality)',
    formulaLatex: String.raw`\sqrt{w_1 x_1 + w_2 x_2} \geq w_1 \sqrt{x_1} + w_2 \sqrt{x_2}`,
    note: 'Since √x is concave, linear-in-vol (right side) systematically underestimates the true value (left side). Interpolating in variance (x = σ²T) and then taking the square root gives the correct result.',
  };

  singleBracketFallback: FormulaDoc = {
    label: 'Single-Bracket Fallback (Removed)',
    formulaLatex: String.raw`\text{IV}_{30d} \approx \text{IV}_{\text{obs}} \cdot \sqrt{\frac{30}{\text{DTE}_{\text{obs}}}}`,
    note: 'Removed. Previously assumed volatility scales as √T — requires flat term structure, no skew shift, and IID returns (all empirically false). Now returns None, relying on forward-fill (limit 2 business days) for missing days.',
  };

  bracketWindow = {
    current: 'DTE window: 20–45 days',
    upgraded: 'Narrowed from 14–60 (completed)',
    rationale: 'Window of 20–45 ensures max |DTE − 30| ≤ 15, preventing asymmetric interpolation where one bracket dominates. Both weights stay in [0.25, 0.75] range.',
  };

  // ─── Price Source Hierarchy ────────────────────────────────
  priceHierarchy = [
    { rank: '1', source: 'Midpoint', condition: 'bid > 0, ask > 0, (ask − bid)/mid ≤ 15%, mid ≥ $0.05' },
    { rank: '2', source: 'VWAP', condition: 'vw field available, vwap ≥ $0.05' },
    { rank: '3', source: 'Close', condition: 'volume ≥ 50, close within [bid, ask] if available, close ≥ $0.05' },
    { rank: '4', source: 'Reject', condition: 'None of the above — stale or edge prints dropped' },
  ];

  // ─── Research Features ─────────────────────────────────────
  featureFormulas: FormulaDoc[] = [
    {
      label: 'IV Rank (Rolling Percentile)',
      formulaLatex: String.raw`\text{IV\_rank}_N = \frac{\text{IV}_{30d} - \min_{N}(\text{IV}_{30d})}{\max_{N}(\text{IV}_{30d}) - \min_{N}(\text{IV}_{30d})}`,
      note: 'Measures current IV relative to its N-day range (N ∈ {60, 252}). Values near 1.0 indicate elevated IV; near 0.0 indicates depressed IV. Useful for mean-reversion strategies.',
      variablesLatex: [
        String.raw`N = \text{lookback window in trading days (60 or 252)}`,
        String.raw`\text{Range: } [0, 1]`,
      ],
    },
    {
      label: 'Log Skew',
      formulaLatex: String.raw`\text{log\_skew} = \ln\left(\frac{\text{IV}_{\text{put}}}{\text{IV}_{\text{call}}}\right)`,
      note: 'Positive values indicate elevated put demand (fear/hedging). Negative values suggest call demand or complacency. Log transformation ensures symmetry around zero.',
      variablesLatex: [
        String.raw`\text{IV}_{\text{put}} = \text{30-day IV from 5\% OTM put}`,
        String.raw`\text{IV}_{\text{call}} = \text{30-day IV from 5\% OTM call}`,
      ],
    },
    {
      label: 'Volatility Premium (Signal Mode)',
      formulaLatex: String.raw`\text{VP}_5 = \text{IV}_{30d} - \text{RV}_5^{\text{trailing}}`,
      note: 'Difference between implied and trailing realized volatility in vol space. Positive VP means options are "expensive" relative to recent realized moves. Note: the codebase labels this "VRP" but it is technically a volatility premium (IV − RV), not a true variance risk premium (IV² − RV²). Variance scales linearly in time; volatility does not. A true VRP would compute IV² − RV² to operate in variance space.',
      variablesLatex: [
        String.raw`\text{RV}_5^{\text{trailing}} = \text{std}(\ln r_1, \ldots, \ln r_5) \cdot \sqrt{252}`,
        String.raw`\text{where } \ln r_i = \ln(\text{close}_i / \text{close}_{i-1}) \text{ are daily log returns}`,
        String.raw`\text{rolling}(5).\text{std}() \text{ gives daily-scale } \sigma \text{, then } \times\sqrt{252} \text{ annualizes}`,
      ],
    },
    {
      label: 'Volatility Premium (Research Mode)',
      formulaLatex: String.raw`\text{VP}_5^{\text{fwd}} = \text{IV}_{30d} - \text{RV}_5^{\text{forward}}`,
      note: 'Uses forward-looking realized vol — only valid for backtesting. NEVER used in live signal generation. Named vrp_5_forward explicitly to prevent look-ahead bias leakage.',
      variablesLatex: [
        String.raw`\text{RV}_5^{\text{forward}} = \text{std}(\ln r_{t+1}, \ldots, \ln r_{t+5}) \cdot \sqrt{252}`,
      ],
    },
  ];

  // ─── Risk-Free Rate ────────────────────────────────────────
  riskFreeRateDoc = {
    problem: 'IV sensitivity to rate: ∂σ/∂r ≈ −(∂C/∂r) / vega. For a 60-DTE ATM option, ~50bps rate error → ~0.3-0.5 vol point IV error. Now using FRED-sourced dynamic rates per trading day.',
    currentLatex: String.raw`r(t, \text{DTE}) = \text{interpolate}\big(\text{FRED tenors: DTB4WK, DTB3, DTB6, DTB1YR}\big)`,
    upgradedLatex: String.raw`\text{Tenors: 4wk (28d), 3mo (91d), 6mo (182d), 1yr (365d) — linear interpolation to DTE}`,
    sensitivityLatex: String.raw`\frac{\partial \sigma}{\partial r} \approx -\frac{K \cdot T \cdot e^{-rT} \cdot \Phi(d_2)}{\nu}`,
    fallback: 'If FRED API is unavailable, falls back to r = 0.043 with a warning log. 24-hour cache prevents repeated API calls. Never fails the IV build due to rate fetch failure.',
  };

  // ─── Delta-Based Strikes ───────────────────────────────────
  deltaFormula: FormulaDoc = {
    label: 'Black-Scholes Delta (Active — for Skew Strike Selection)',
    formulaLatex: String.raw`\Delta = \begin{cases} \Phi(d_1) & \text{call} \\ \Phi(d_1) - 1 & \text{put} \end{cases}`,
    note: 'OTM skew strikes are now selected by 25Δ — the put/call with BS delta closest to ±0.25. Uses default IV (0.25) and FRED risk-free rate for delta estimation at contract discovery time. Falls back to 5% OTM offset when DTE is unavailable.',
  };

  deltaCircularity = 'Note: delta estimation at contract discovery time uses a default IV (0.25) rather than solved IV, since IV solving happens after contract selection. This single-pass approach is sufficient because delta is relatively insensitive to IV for 25Δ-range strikes at 20–45 DTE.';

  // ─── Synthetic Forward ─────────────────────────────────────
  syntheticForward: FormulaDoc = {
    label: 'Synthetic Forward (Active — ATM IV from Call/Put Average)',
    formulaLatex: String.raw`\sigma_{\text{ATM}} = \frac{\sigma_{\text{call}}(K_{\text{ATM}}) + \sigma_{\text{put}}(K_{\text{ATM}})}{2}`,
    note: 'ATM IV is now the average of call and put IV at the strike closest to spot. Both ATM call and ATM put contracts are fetched per bracket expiry. Falls back to call-only when ATM put is unavailable.',
    variablesLatex: [
      String.raw`K_{\text{ATM}} = \arg\min_K |K - S| \quad \text{(closest strike to spot)}`,
      String.raw`\sigma_{\text{call}}, \sigma_{\text{put}} = \text{implied vols from BS solver}`,
      String.raw`\text{Fallback: } \sigma_{\text{ATM}} = \sigma_{\text{call}} \text{ if put unavailable}`,
    ],
  };

  // ─── Strategy Engine ───────────────────────────────────────
  strategyPnl: FormulaDoc = {
    label: 'Per-Leg P&L',
    formulaLatex: String.raw`\text{PnL}_i = \begin{cases} (V_i - \text{premium}_i) \times \text{qty}_i & \text{long} \\ (\text{premium}_i - V_i) \times \text{qty}_i & \text{short} \end{cases}`,
    note: 'Where Vᵢ = BS price at current market conditions. Each leg uses its own IV from the market snapshot — no shared vol assumption.',
  };

  totalPnl: FormulaDoc = {
    label: 'Total Strategy P&L',
    formulaLatex: String.raw`\text{PnL}_{\text{total}}(S, T) = \sum_{i} \text{PnL}_i`,
    note: 'Sum of all leg P&Ls. At expiration, Vᵢ reduces to intrinsic value: max(S−K, 0) for calls, max(K−S, 0) for puts.',
  };

  popFormula: FormulaDoc = {
    label: 'Probability of Profit (POP)',
    formulaLatex: String.raw`\text{POP} = P(S_T > \text{breakeven}) = 1 - \Phi\!\left(\frac{\ln(B/S) - (r - \tfrac{1}{2}\sigma^2)T}{\sigma\sqrt{T}}\right)`,
    note: 'Risk-neutral probability that the strategy is profitable at expiration. Uses the lognormal terminal distribution under GBM. For strategies with multiple breakevens, computed at each boundary.',
    variablesLatex: [
      String.raw`B = \text{breakeven price}`,
      String.raw`\text{Lognormal CDF: } P(S_T < x) = \Phi\!\left(\frac{\ln(x/S) - (r - \tfrac{1}{2}\sigma^2)T}{\sigma\sqrt{T}}\right)`,
    ],
  };

  evFormula: FormulaDoc = {
    label: 'Expected Value (EV)',
    formulaLatex: String.raw`\text{EV} = \int_0^{\infty} \text{PnL}(S) \cdot f_{\text{LN}}(S)\, dS`,
    note: 'Numerical integration over 1000 points covering 99.9% of the lognormal distribution. Gives the probability-weighted average payoff of the strategy.',
  };

  breakevenFormula: FormulaDoc = {
    label: 'Breakeven Interpolation',
    formulaLatex: String.raw`B = p_1 + (p_2 - p_1) \cdot \frac{|\text{PnL}_1|}{|\text{PnL}_1| + |\text{PnL}_2|}`,
    note: 'Linear interpolation between adjacent grid points where the expiration P&L crosses zero. Computed from intrinsic values (not BS-priced curve).',
  };

  // ─── Statistical Framework ─────────────────────────────────
  icFormula: FormulaDoc = {
    label: 'Information Coefficient (Target-Specific)',
    formulaLatex: String.raw`\text{IC}_{\text{target}} = \rho_s\!\left(\text{feature}_t,\; r^{\text{target}}_{t \to t+h}\right)`,
    note: 'Spearman rank correlation between feature values and a specific forward target. IC is always computed against one target type per run — the target must be specified. Rank-based so robust to outliers and non-linear relationships. Range: [−1, 1].',
    variablesLatex: [
      String.raw`\text{IC}_{\text{dir}} = \rho_s(\text{feature}_t,\; r^{\text{dir}}_{t \to t+1}) \quad \text{(directional predictor)}`,
      String.raw`\text{IC}_{\text{vol}} = \rho_s(\text{feature}_t,\; \text{RV}^{\text{fwd}}_5) \quad \text{(volatility predictor)}`,
      String.raw`\text{IC}_{\text{abs}} = \rho_s(\text{feature}_t,\; |r^{\text{dir}}_{t \to t+1}|) \quad \text{(magnitude predictor)}`,
      String.raw`\rho_s = 1 - \frac{6 \sum d_i^2}{n(n^2 - 1)} \quad \text{(Spearman rank correlation)}`,
    ],
  };

  neweyWestFormula: FormulaDoc = {
    label: 'Newey-West HAC Standard Error',
    formulaLatex: String.raw`\hat{V}_{\text{NW}} = \hat{\gamma}_0 + 2\sum_{k=1}^{L} \left(1 - \frac{k}{L+1}\right) \hat{\gamma}_k \qquad \text{SE}_{\text{NW}} = \sqrt{\frac{\hat{V}_{\text{NW}}}{n}}`,
    note: 'Two-step computation: (1) Bartlett-kernel weighted HAC variance estimate V̂_NW, then (2) standard error of the mean via SE = √(V̂/n). The t-statistic is t = mean(IC) / SE_NW. Accounts for serial correlation in daily IC values that inflates naive standard errors.',
    variablesLatex: [
      String.raw`\hat{\gamma}_0 = \frac{1}{n}\sum_{i=1}^n (x_i - \bar{x})^2 \quad \text{(sample variance)}`,
      String.raw`\hat{\gamma}_k = \frac{1}{n}\sum_{i=k+1}^n (x_i - \bar{x})(x_{i-k} - \bar{x}) \quad \text{(autocovariance at lag } k \text{)}`,
      String.raw`L = \max\!\big(\lfloor 4(n/100)^{2/9} \rfloor,\; L_{\min}\big) \quad \text{(Andrews 1991 auto-bandwidth, min lag enforced)}`,
      String.raw`t_{\text{NW}} = \bar{x} / \text{SE}_{\text{NW}} \quad \text{(HAC-corrected t-statistic)}`,
    ],
  };

  effectiveSampleSize: FormulaDoc = {
    label: 'Effective Sample Size',
    formulaLatex: String.raw`n_{\text{eff}} = \frac{n}{1 + 2 \sum_{k=1}^{K} \rho_k}`,
    note: 'When observations are autocorrelated, the effective number of independent data points is less than n. The denominator inflates with positive autocorrelation, shrinking n_eff.',
    variablesLatex: [
      String.raw`\rho_k = \text{autocorrelation at lag } k`,
      String.raw`K = \text{truncation lag (first } k \text{ where } |\rho_k| < 0.05 \text{)}`,
    ],
  };

  validationGate = {
    intro: 'Every feature must pass all four tests to be considered a validated predictor. IC is always computed against a single specified target type (directional, volatility, or absolute return).',
    criteria: [
      { test: 'Mean IC', rule: '|mean IC| ≥ 0.03 AND |t_NW| ≥ 2.0', rationale: 'Both effect size and statistical significance required. A fixed IC threshold alone is insufficient — IC = 0.03 is insignificant with n = 200 but highly significant with n = 2000. The t-stat accounts for sample size.' },
      { test: 'Significance', rule: 'p_NW < 0.05', rationale: 'Newey-West adjusted p-value ensures IC is distinguishable from zero after accounting for autocorrelation.' },
      { test: 'Stationarity', rule: 'ADF rejects unit root AND KPSS fails to reject stationarity', rationale: 'Dual test: ADF alone has low power; KPSS alone has size distortion. Both must agree to rule out spurious correlation.' },
      { test: 'Monotonicity', rule: 'Quantile mean returns are monotonic', rationale: 'Feature is sorted into Q1–Q5 quintiles. Mean forward return of each quintile must be monotonically increasing (or decreasing): mean(Q5) > mean(Q4) > ... > mean(Q1). Equivalently, Spearman(quintile_index, mean_return) > 0.' },
    ],
  };

  validationCaveats = [
    'When testing multiple features (IV rank, skew, VRP, etc.), p-values should be corrected for multiple comparisons using Benjamini-Hochberg FDR. This is NOT currently implemented — all p-values are reported as-is, which inflates Type I error.',
    'The IC threshold of 0.03 is a heuristic for daily equity signals. For different frequencies or asset classes, the appropriate threshold may differ.',
    'Train/test split uses chronological 70/30 division (first 70% = train, last 30% = test). No walk-forward rolling validation is currently implemented.',
  ];

  // ─── Forward Targets ───────────────────────────────────────
  forwardTargets: FormulaDoc[] = [
    {
      label: 'Directional',
      formulaLatex: String.raw`r_{\text{dir}} = \ln\!\left(\frac{\text{close}_{t+1}}{\text{close}_t}\right)`,
      note: 'One-day forward log return. Log returns are additive across time and approximately symmetric.',
    },
    {
      label: 'Volatility',
      formulaLatex: String.raw`r_{\text{vol}} = \text{std}(\ln \text{returns}, 5d\;\text{forward}) \cdot \sqrt{252}`,
      note: 'Forward 5-day realized volatility, annualized. Used for volatility-predicting features like IV rank.',
    },
    {
      label: 'Absolute Return',
      formulaLatex: String.raw`r_{\text{abs}} = \left|\ln\!\left(\frac{\text{close}_{t+1}}{\text{close}_t}\right)\right|`,
      note: 'Magnitude of the directional return, ignoring sign. Useful for features that predict "something will happen" without directional bias.',
    },
  ];

  // ─── Quality Flags (Upgrade 7) ─────────────────────────────
  qualityFlags = [
    { flag: 'High', rule: 'volume ≥ 50 AND OI ≥ 100 AND spread ≤ 10%', action: 'Full confidence in derived IV' },
    { flag: 'Medium', rule: 'volume ≥ 10 AND OI ≥ 25', action: 'Usable but with caution' },
    { flag: 'Low', rule: 'Everything else with valid IV', action: 'Kept in data, flagged for downstream filtering' },
  ];

  qualityNote = 'Current system hard-drops low-quality data points. During stress events, liquidity dries up — exactly when IV spikes are most informative. Dropping these creates survivorship bias. Upgrade 7 replaces hard filters with soft quality flags.';

  // ─── Data Pipeline ─────────────────────────────────────────
  pipelineSteps: PipelineStep[] = [
    { label: 'Contract Discovery', detail: 'Per trading day: identify 2 bracket expiries (20–45 DTE around 30d). ATM strike = argmin|K − S| for both calls and puts. OTM skew strikes selected by 25Δ (BS delta), with 5% offset fallback. Note: ATM strike availability is not guaranteed — system selects closest available.' },
    { label: 'IV Derivation', detail: 'Fetch stock daily bars. Prefetch all option bars (5 threads). Per-day: solve IV via BS solver with FRED-sourced risk-free rate → variance-time interpolation to 30d. ATM IV = average of call and put IV (synthetic forward). Quality flags assigned per day.' },
    { label: 'Gap Filling', detail: 'Forward-fill ≤ 2 business day gaps. Longer gaps reported as missing.' },
    { label: 'Diagnostics', detail: 'Validate: missing% ≤ 15, valid days ≥ 30, discontinuities ≤ 10%. Flag day-over-day changes > 50%.' },
    { label: 'Feature Engineering', detail: 'Compute IV rank, log skew, VRP (vrp_5 trailing for signals, vrp_5_forward for research) from the 30-day IV series.' },
    { label: 'Research Runner', detail: 'IC analysis with unified Andrews (1991) bandwidth for Newey-West and N_eff. ADF/KPSS stationarity, quantile analysis, regime robustness.' },
    { label: 'Persistence', detail: '.NET ResearchService checks cache, proxies to Python, persists results to PostgreSQL.' },
  ];

  // ─── Filter Rules ──────────────────────────────────────────
  filterRules: FilterRule[] = [
    { stage: 'Contract Finder', filter: 'Min volume', threshold: '50' },
    { stage: 'Contract Finder', filter: 'Min open interest', threshold: '100' },
    { stage: 'Contract Finder', filter: 'Max spread ratio', threshold: '10% of mid' },
    { stage: 'Contract Finder', filter: 'OTM skew selection', threshold: '25Δ (BS delta), fallback: 5% from ATM' },
    { stage: 'Contract Finder', filter: 'Strike search range', threshold: '±15% of ATM' },
    { stage: 'Bracket Search', filter: 'DTE window', threshold: '20–45 days' },
    { stage: 'BS Solver', filter: 'Min DTE', threshold: '7 days' },
    { stage: 'BS Solver', filter: 'Risk-free rate', threshold: 'FRED daily (4wk–1yr), fallback 0.043' },
    { stage: 'BS Solver', filter: 'Initial σ clamp', threshold: '[0.15, 3.0]' },
    { stage: 'BS Solver', filter: 'Final σ acceptance', threshold: '[0.05, 3.0]' },
    { stage: 'IV Builder', filter: 'Price hierarchy', threshold: 'Mid (≤15% spread) → VWAP → Close (in range) → Reject' },
    { stage: 'IV Builder', filter: 'Quality flags', threshold: 'high / medium / low / missing (no hard drops)' },
    { stage: 'IV Builder', filter: 'Forward-fill limit', threshold: '2 business days' },
    { stage: 'Diagnostics', filter: 'Max missing data', threshold: '15%' },
    { stage: 'Diagnostics', filter: 'Min valid IV days', threshold: '30' },
    { stage: 'Diagnostics', filter: 'Max discontinuities', threshold: '10% of total' },
  ];

  // ─── Upgrade Roadmap ───────────────────────────────────────
  upgrades: UpgradeDoc[] = [
    {
      id: 1, title: 'Variance Interpolation', impact: 'HIGH', effort: 'LOW',
      problem: 'Linear-in-vol interpolation introduces downward bias (Jensen\'s inequality).',
      currentFormulaLatex: String.raw`\sigma_{30} = w_1 \sigma_1 + w_2 \sigma_2`,
      correctedFormulaLatex: String.raw`\sigma_{30} = \sqrt{\frac{w_1 \sigma_1^2 T_1 + w_2 \sigma_2^2 T_2}{T_{30}}}`,
      explanation: 'Interpolate in total variance (σ²T) space — the industry standard for constant-maturity vol surfaces.',
      phase: 'Phase 1 ✓',
    },
    {
      id: 2, title: 'Drop √T Fallback', impact: 'MEDIUM', effort: 'TRIVIAL',
      problem: 'Single-bracket √T normalization assumes flat term structure and IID returns.',
      currentFormulaLatex: String.raw`\text{IV}_{30} \approx \text{IV}_{\text{obs}} \cdot \sqrt{30 / \text{DTE}}`,
      explanation: 'Returns None when only one bracket exists. Forward-fill (limit 2 days) covers short gaps.',
      phase: 'Phase 1 ✓',
    },
    {
      id: 3, title: 'Narrow Bracket Window', impact: 'MEDIUM', effort: 'TRIVIAL',
      problem: 'Previous 14–60 DTE window allowed extreme asymmetry in interpolation weights.',
      explanation: 'Tightened to 20–45 DTE, ensuring max |DTE − 30| ≤ 15. Both weights stay in [0.25, 0.75].',
      phase: 'Phase 1 ✓',
    },
    {
      id: 4, title: 'Dynamic Risk-Free Rate', impact: 'HIGH', effort: 'MEDIUM',
      problem: 'Previously hardcoded r = 0.043. A 50bps error → 0.3-0.5 vol point systematic IV bias.',
      currentFormulaLatex: String.raw`r = 0.043`,
      correctedFormulaLatex: String.raw`r = \text{FRED}(\text{DTB4WK}, \text{DTB3}, \text{DTB6}, \text{DTB1YR})`,
      explanation: 'Fetches daily Treasury rates from FRED API per trading day. Interpolates across 4 tenors (4wk, 3mo, 6mo, 1yr) to match option DTE. 24h cache with 0.043 fallback if FRED unavailable.',
      phase: 'Phase 2 ✓',
    },
    {
      id: 5, title: 'Delta-Based Skew Strikes', impact: 'HIGH', effort: 'HIGH',
      problem: 'Previously used fixed 5% OTM offset — skew not comparable across underlyings or time.',
      currentFormulaLatex: String.raw`K_{\text{put}} = S \times 0.95`,
      correctedFormulaLatex: String.raw`|\Delta(K_{\text{put}})| \approx 0.25`,
      explanation: 'Selects 25Δ put and 25Δ call using BS delta at each candidate strike. Falls back to 5% offset when DTE is unavailable. Cross-sectionally correct across underlyings.',
      phase: 'Phase 4 ✓',
    },
    {
      id: 6, title: 'Synthetic Forward for ATM', impact: 'MEDIUM', effort: 'MEDIUM',
      problem: 'Previously used call-only ATM IV. Stock close ≠ forward price (dividends, borrow cost).',
      correctedFormulaLatex: String.raw`\text{IV}_{\text{ATM}} = \frac{\text{IV}_{\text{call}} + \text{IV}_{\text{put}}}{2}`,
      explanation: 'ATM IV now averages call and put IV at the strike closest to spot. Both ATM call and ATM put contracts are fetched per bracket expiry. Falls back to call-only when put is unavailable.',
      phase: 'Phase 3 ✓',
    },
    {
      id: 7, title: 'Quality Flags (Not Hard Drops)', impact: 'MEDIUM', effort: 'LOW',
      problem: 'Previously hard-dropped IV outside [0.05, 3.0] — created survivorship bias during vol spikes.',
      explanation: 'Replaced with soft quality flags: "high" (midpoint, valid range), "medium" (close/VWAP price), "low" (outside IV range), "missing". All data kept — downstream decides filtering.',
      phase: 'Phase 3 ✓',
    },
    {
      id: 8, title: 'Strict Price Hierarchy', impact: 'LOW', effort: 'TRIVIAL',
      problem: 'Previously accepted close at bid/ask edge — not representative of fair value.',
      explanation: 'Midpoint (spread/mid ≤ 15%) → VWAP → Close (within bid-ask range, volume ≥ 50) → Reject. Eliminates stale prints and edge fills.',
      phase: 'Phase 2 ✓',
    },
    {
      id: 9, title: 'Newey-West Auto-Lag', impact: 'MEDIUM', effort: 'LOW',
      problem: 'NW and N_eff previously used different lag formulas — inconsistent bandwidth selection.',
      correctedFormulaLatex: String.raw`L = \max\big(\lfloor 4 \cdot (n/100)^{2/9} \rfloor,\; L_{\min}\big)`,
      explanation: 'Unified lag selection: both Newey-West and effective sample size now use Andrews (1991) bandwidth with shared _andrews_lag() function. min_lag floor preserved for daily options data.',
      phase: 'Phase 4 ✓',
    },
    {
      id: 10, title: 'Forward RV Namespace Isolation', impact: 'LOW', effort: 'LOW',
      problem: 'Previously vrp_5 used forward-looking RV in research mode — risk of look-ahead bias leak.',
      explanation: 'Signal mode: vrp_5 (trailing RV only). Research mode: vrp_5_forward (forward RV). Runtime guards prevent cross-mode access — vrp_5 in research mode and vrp_5_forward in signal mode both raise ValueError.',
      phase: 'Phase 3 ✓',
    },
  ];

  // ─── Known Simplifications ─────────────────────────────────
  simplifications = [
    { title: 'No Dividends', detail: 'BS assumes no dividends. For dividend-paying underlyings, slightly overprices calls and underprices puts.' },
    { title: 'European-Style Only', detail: 'BS assumes European exercise. American equity options can be exercised early, which BS doesn\'t model.' },
    { title: 'Per-Leg IV', detail: 'Each leg uses its own IV from the market snapshot. Skew now uses 25Δ strikes (delta-based selection), but no full vol surface interpolation or smile modeling between strikes.' },
    { title: 'Calendar-Day Theta', detail: 'Theta ÷ 365 (calendar days), not 252 (trading days). Slightly understates weekday decay.' },
    { title: 'Risk-Free Rate Granularity', detail: 'FRED rates are fetched per trading day with 4 tenors (4wk–1yr). Intraday rate changes and ultra-short tenors (<4wk) are not captured. Falls back to r = 0.043 if FRED is unavailable.' },
    { title: 'Daily Bar IV Input', detail: 'IV is derived from daily OHLCV bars, not intraday snapshots. Options spreads widen near close and midpoints are unstable at end-of-day. Professional pipelines typically use intraday snapshots (e.g. 15:45 NBBO midpoint). This adds noise to derived IV.' },
    { title: 'Forward-Fill Introduces Bias', detail: 'Missing IV days are forward-filled (limit 2 business days). Since IV changes daily, this carries stale values forward. A better approach would be to interpolate between bracket expiries without time-filling, or mark gaps as truly missing.' },
    { title: 'No Cross-Sectional Normalization', detail: 'Features are z-scored time-series wise (train-period mean/std), NOT cross-sectionally per observation date. Without cross-sectional normalization, regime shifts can contaminate signals when comparing across tickers.' },
    { title: 'No Multiple Hypothesis Correction', detail: 'When testing multiple features (IV rank, skew, VRP variants), p-values are reported individually without Benjamini-Hochberg FDR correction. This inflates the probability of false discoveries.' },
    { title: 'No Universe Definition', detail: 'The system does not define a fixed investment universe (e.g. S&P 500, top 500 by options volume). Without a defined universe, IC values are not comparable across runs and may be biased by ticker selection.' },
    { title: 'Vol Premium vs Variance Premium', detail: 'The "VRP" feature computes IV − RV (volatility space), not IV² − RV² (variance space). True variance risk premium operates in variance space where quantities scale linearly with time. The current metric is a volatility premium.' },
    { title: 'DTE Floor at 7 Days', detail: 'Options with DTE < 7 are rejected from IV solving. This can break interpolation when bracket expiries approach the boundary. A floor of 10 days or excluding weeklies entirely would be more conservative.' },
  ];

  // ─── References ────────────────────────────────────────────
  references = [
    { citation: 'Black, F. & Scholes, M. (1973). The Pricing of Options and Corporate Liabilities. Journal of Political Economy.', relevance: 'Foundation for all option pricing and IV computation in this system.' },
    { citation: 'Brenner, M. & Subrahmanyam, M. (1988). A Simple Formula to Compute the Implied Standard Deviation.', relevance: 'Initial guess for Newton-Raphson IV solver (σ₀ approximation).' },
    { citation: 'Abramowitz, M. & Stegun, I. (1964). Handbook of Mathematical Functions. Eq. 26.2.17.', relevance: 'Rational approximation for standard normal CDF with error < 7.5×10⁻⁸.' },
    { citation: 'Newey, W. & West, K. (1987). A Simple, Positive Semi-definite, Heteroskedasticity and Autocorrelation Consistent Covariance Matrix.', relevance: 'HAC standard errors for IC significance testing.' },
    { citation: 'Andrews, D. (1991). Heteroskedasticity and Autocorrelation Consistent Covariance Matrix Estimation.', relevance: 'Automatic bandwidth (lag) selection rule for Newey-West estimator.' },
    { citation: 'Lo, A. (2002). The Statistics of Sharpe Ratios. Financial Analysts Journal.', relevance: 'Statistical significance framework for evaluating signal Sharpe ratios.' },
    { citation: 'CBOE (2019). VIX Methodology: Variance interpolation in total variance space.', relevance: 'Industry standard for constant-maturity volatility index construction.' },
  ];
}
