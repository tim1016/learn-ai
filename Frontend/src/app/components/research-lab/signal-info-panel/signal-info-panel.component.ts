import { Component, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { AccordionModule } from 'primeng/accordion';
import { DividerModule } from 'primeng/divider';
import { KatexDirective } from '../../../shared/katex.directive';

interface StepDoc {
  name: string;
  formulaLatex: string;
  variablesLatex: string[];
  interpretation: string;
  whyItMatters: string;
  stationarityAssumption?: string;
}

interface BacktestMetricDoc {
  name: string;
  formulaLatex: string;
  interpretation: string;
}

interface GraduationCriterionDoc {
  name: string;
  threshold: string;
  formulaLatex?: string;
  rationale: string;
  justification: string;
  failureAdvice: string;
}

interface StatusLabelDoc {
  label: string;
  condition: string;
  meaning: string;
  color: string;
}

interface SymbolEntry {
  symbolLatex: string;
  name: string;
  definition: string;
}

interface DataAssumption {
  label: string;
  value: string;
}

interface AuditCheck {
  category: string;
  checks: string[];
}

interface ReportingPrecision {
  metric: string;
  decimals: string;
  example: string;
}

interface ReferenceDoc {
  citation: string;
  relevance: string;
}

@Component({
  selector: 'app-signal-info-panel',
  standalone: true,
  imports: [CommonModule, AccordionModule, DividerModule, KatexDirective],
  templateUrl: './signal-info-panel.component.html',
  styleUrls: ['./signal-info-panel.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class SignalInfoPanelComponent {
  // ─── 0. Research Protocol ─────────────────────────────────
  researchProtocol = {
    intro:
      'Every signal analysis follows a strict 5-phase protocol. No backtest is run before a hypothesis is documented. No feature graduates without passing all five phases in order. This protocol prevents post-hoc narrative fitting and data-mining bias.',
    phases: [
      {
        number: 1,
        title: 'Formulate a Structural Hypothesis',
        summary: 'Document the market mechanism, counterparty, and regime expectations before any computation.',
        requirement: 'No backtest may begin until a hypothesis is written. This prevents running dozens of features and inventing stories for whichever one passed.',
      },
      {
        number: 2,
        title: 'Construct the Tradable Signal',
        summary: 'Transform the raw feature through Z-score standardization, threshold filtering, and regime gating \u2014 all using train-only statistics.',
        requirement: 'Every statistic (\u03BC, \u03C3, \u03B8) is computed on training data only and frozen before OOS evaluation. No re-optimization during test windows.',
      },
      {
        number: 3,
        title: 'Walk-Forward Validation',
        summary: 'Simulate live deployment with rolling 3-month train / 1-month test windows and frozen parameters.',
        requirement: 'Single backtests are insufficient. Validation requires rolling OOS evaluation across multiple time periods.',
      },
      {
        number: 4,
        title: 'Pass the 5-Point Graduation System',
        summary: 'Evaluate the signal against five criteria: Net Sharpe, Max Drawdown, OOS consistency, regime coverage, and parameter stability.',
        requirement: 'Graduation is denied if any critical criterion fails. Status is determined by the weakest link.',
      },
      {
        number: 5,
        title: 'Internal Audit Checklist',
        summary: 'Verify statistical integrity, execution integrity, and reporting integrity before finalizing.',
        requirement: 'If any audit condition fails, the feature cannot graduate regardless of backtest results.',
      },
    ],
  };

  hypothesisRequirements = [
    {
      label: 'Market Mechanism',
      question: 'Why should this feature predict future returns?',
      examples: 'Liquidity shocks, inventory rebalancing, behavioral overreaction, information asymmetry, institutional order flow.',
    },
    {
      label: 'Counterparty Identification',
      question: 'Who is systematically losing money on the other side of this trade?',
      examples: 'Late-to-trend retail traders, passive market makers absorbing flow, stop-loss cascades from overleveraged positions.',
    },
    {
      label: 'Regime Expectation',
      question: 'Under what conditions should the signal perform well, degrade, or fail entirely?',
      examples: 'Works in Low Vol + Sideways (mean-reversion); fails during trending regimes or macro events.',
    },
  ];

  hypothesisExamples = [
    {
      strategy: 'Mean-Reversion / Overextension (Contrarian)',
      hypothesis:
        'RSI (Relative Strength Index) Divergence exhibits negative correlation to future returns at a 15-minute horizon due to liquidity exhaustion after rapid localized buying/selling. The signal exploits short-term mean reversion with late-to-trend retail momentum traders on the other side. Expected to work best in Low Volatility + Sideways conditions and fail in High Volatility or strong Trending regimes.',
    },
    {
      strategy: 'Momentum / Trend Continuation',
      hypothesis:
        'VWAP (Volume Weighted Average Price) Crossover exhibits positive correlation to future returns at a 1-hour horizon due to institutional accumulation/distribution algorithms executing VWAP-pegged orders. The signal exploits intraday trend persistence with passive limit-order providers (market makers) on the other side. Expected to work best in Normal Volatility + Trending conditions and fail in Sideways/Choppy regimes.',
    },
    {
      strategy: 'Volatility Breakout',
      hypothesis:
        'Bollinger Band Expansion exhibits positive correlation to future returns at a 30-minute horizon due to sudden price discovery following periods of volatility compression. The signal exploits volatility clustering and breakout momentum with mean-reversion traders getting stopped out on the other side. Expected to work best in Transitioning from Low to High Volatility conditions and fail in Low Volatility + Sideways regimes.',
    },
  ];

  // ─── 1. Pipeline Overview ──────────────────────────────────
  pipelineOverview = {
    intro:
      'The Signal Engine converts a validated predictive feature into a tradable signal through a rigorous, multi-stage pipeline. Each stage is designed to prevent lookahead bias, account for transaction costs, and validate performance out-of-sample.',
    stages: [
      'Raw Feature \u2192 compute technical indicator on OHLCV bars',
      'Z-Score Standardization \u2192 normalize using train-period statistics only',
      'Threshold Filter \u2192 convert to binary position signal',
      'Regime Gate \u2192 suppress trades in hostile market environments',
      'Backtest \u2192 compute cost-adjusted returns with no lookahead',
      'Walk-Forward Validation \u2192 rolling OOS evaluation with frozen parameters',
      'Graduation \u2192 multi-criteria assessment and grade assignment',
    ],
    principles: [
      {
        title: 'No Lookahead',
        detail:
          'All statistics (\u03BC, \u03C3, threshold) are computed exclusively on training data. Position at time t uses signal from time t\u22121. This mirrors live deployment where the future is unknown.',
      },
      {
        title: 'Train-Only Statistics',
        detail:
          'Z-score parameters are frozen from the training period and applied unchanged to test data. This prevents test-set contamination of the normalization.',
      },
      {
        title: 'Frozen Parameters in OOS',
        detail:
          'The optimal threshold selected in-sample is locked for the out-of-sample evaluation. No re-optimization is allowed during testing.',
      },
    ],
  };

  // ─── 2. Symbol Glossary (§2.1) ─────────────────────────────
  symbolGlossary: SymbolEntry[] = [
    {
      symbolLatex: String.raw`f_t`,
      name: 'Feature',
      definition: 'Raw feature value at bar t (e.g., 5-minute momentum)',
    },
    {
      symbolLatex: String.raw`z_t`,
      name: 'Z-Score',
      definition:
        'Standardized feature value: (f_t \u2212 \u03BC_train) / \u03C3_train',
    },
    {
      symbolLatex: String.raw`\text{signal}_t`,
      name: 'Pre-gate Signal',
      definition:
        'Threshold-filtered output before regime gating: sign(z_t) \u00D7 \uD835\uDFD9[|z_t| > \u03B8]',
    },
    {
      symbolLatex: String.raw`w_t`,
      name: 'Weight / Position',
      definition:
        'Final portfolio weight: w_t = signal_t \u00D7 gate_t. Values: +1 (long), 0 (flat), \u22121 (short)',
    },
    {
      symbolLatex: String.raw`r_t`,
      name: 'Return',
      definition: '15-minute forward log return at bar t',
    },
    {
      symbolLatex: String.raw`\theta`,
      name: 'Threshold',
      definition:
        'Activation threshold in z-score units (tested: 0.5, 1.0, 1.5, 2.0)',
    },
    {
      symbolLatex: String.raw`c`,
      name: 'Cost',
      definition: 'Transaction cost in fractional units (bps / 10,000)',
    },
    {
      symbolLatex: String.raw`\mu_{\text{train}}`,
      name: 'Train Mean',
      definition:
        'Mean of feature values computed on training period only. Frozen for OOS.',
    },
    {
      symbolLatex: String.raw`\sigma_{\text{train}}`,
      name: 'Train Std',
      definition:
        'Standard deviation of feature values computed on training period only. Frozen for OOS.',
    },
    {
      symbolLatex: String.raw`SR`,
      name: 'Sharpe Ratio',
      definition: 'Annualized risk-adjusted return: mean(r) / std(r) \u00D7 \u221A(N_bars/year)',
    },
    {
      symbolLatex: String.raw`N_{\text{eff}}`,
      name: 'Effective N',
      definition:
        'Autocorrelation-adjusted sample size. Always \u2264 raw N.',
    },
  ];

  symbolNote =
    'Each symbol has exactly one meaning throughout this documentation. Mapping: signal_t is the pre-gate output (threshold-filtered z-score), w_t is the final position (signal_t \u00D7 gate_t), and "trade" refers to a position change event (|w_t \u2212 w_{t\u22121}| > 0). "Weight" and "position" are synonymous and always refer to w_t.';

  // ─── 3. Data Assumptions (§1.1) ────────────────────────────
  dataAssumptions: DataAssumption[] = [
    { label: 'Data Source', value: 'Polygon.io (Starter plan \u2014 2-year max history, 15-minute delayed)' },
    { label: 'Session Definition', value: 'Regular Trading Hours (RTH) only: 09:30\u201316:00 ET. Pre-market and after-hours bars excluded.' },
    { label: 'Bar Alignment', value: '1-minute OHLCV bars aligned to exchange timestamps. No synthetic bars generated for missing intervals.' },
    { label: 'Corporate Action Handling', value: 'Polygon provides split-adjusted and dividend-adjusted close prices by default. No additional adjustment applied.' },
    { label: 'Missing Bar Handling', value: 'Missing bars are left as gaps. Features requiring lookback skip NaN values via pandas dropna(). Forward returns across gaps are masked as NaN.' },
    { label: 'Outlier Treatment', value: 'No outlier removal applied. Z-score standardization naturally attenuates the impact of extreme values. Threshold filtering further limits exposure to outlier-driven signals.' },
    { label: 'Timezone Reference', value: 'All timestamps in UTC (milliseconds since epoch). Converted to US/Eastern for date-boundary detection and session filtering.' },
    { label: 'Cross-Day Boundary', value: 'Forward returns are masked with NaN at day boundaries. No return measurement spans midnight. This prevents overnight gap contamination.' },
  ];

  // ─── 4. Formal Signal Definition (§1.2) ────────────────────
  formalSignalDef = {
    featureFormula: {
      label: 'Raw Feature',
      formulaLatex: String.raw`f_t = \frac{P_t - P_{t-5}}{P_{t-5}}`,
      note: 'Default feature: 5-minute momentum (pct_change over 5 bars). Other features use their respective formulas from the Feature Runner documentation.',
    },
    lookback: '5 bars (configurable per feature)',
    returnHorizon: '15 bars (15 minutes at 1-minute frequency)',
    returnFormula: {
      label: 'Forward Return',
      formulaLatex: String.raw`r_t = \ln\!\left(\frac{P_{t+15}}{P_t}\right)`,
      note: 'Close-to-close log return. Masked with NaN at day boundaries.',
    },
    icDefinition: {
      label: 'Information Coefficient',
      formulaLatex: String.raw`\text{IC}_d = \rho_{\text{Spearman}}(f_d,\; r_d)`,
      note: 'Daily Spearman rank correlation between feature values and forward returns. Mean IC is the average across all valid trading days.',
      formalSpec: [
        { label: 'Correlation type', value: 'Spearman rank correlation (not Pearson linear). Rank-based to be robust to outliers and nonlinear relationships.' },
        { label: 'Return type', value: 'Log return: r_t = ln(C_{t+15} / C_t). Close-to-close, 15-bar horizon.' },
        { label: 'Horizon', value: 'Daily aggregation. One IC value per calendar day, computed over all intraday bars on that day.' },
        { label: 'Sample window', value: 'All intraday bars on a single trading day. Minimum 5 bars per day required; days with fewer bars or zero variance in feature/return are excluded.' },
        { label: 'Aggregation', value: 'Mean IC = arithmetic mean across all valid daily IC values. No weighting by number of bars per day.' },
      ],
    },
    signalChain: [
      { step: 'Compute feature', formulaLatex: String.raw`f_t = \text{feature}(P_{t-k}, \ldots, P_t)` },
      { step: 'Standardize', formulaLatex: String.raw`z_t = (f_t - \mu_{\text{train}}) / \sigma_{\text{train}}` },
      { step: 'Flip sign (if IC < 0)', formulaLatex: String.raw`z_t \leftarrow -z_t` },
      { step: 'Threshold filter', formulaLatex: String.raw`\text{signal}_t = \text{sign}(z_t) \cdot \mathbb{1}[|z_t| > \theta]` },
      { step: 'Regime gate', formulaLatex: String.raw`w_t = \text{signal}_t \cdot \text{gate}_t` },
    ],
  };

  // ─── 5. Execution Timeline (§1.3) ──────────────────────────
  executionTimeline = {
    steps: [
      { time: 't (bar close)', action: 'Observe close price P_t. Compute feature f_t, z-score z_t, and weight w_t.' },
      { time: 't \u2192 t+1', action: 'Decision delay: weight w_t is computed but NOT yet active.' },
      { time: 't+1 (next bar open)', action: 'Enter position w_t. This is the earliest executable moment after signal generation.' },
      { time: 't+1 \u2192 t+N', action: 'Hold position. Position remains w_t until a new signal changes it.' },
      { time: 't+N (exit)', action: 'Position changes to w_{t+N} based on new signal. Exit occurs at next bar open.' },
    ],
    clarifications: [
      { label: 'Entry bar', value: 'Bar t+1 (the bar after signal computation). In backtest: w_{t\u22121} earns r_t.' },
      { label: 'Exit bar', value: 'Bar where w_t changes from previous w_{t\u22121}. No explicit exit logic \u2014 exits occur when signal reverses or goes flat.' },
      { label: 'Return measurement', value: 'r_t = ln(P_{t+15} / P_t). Earned by position w_{t\u22121}. The 1-bar delay is the no-lookahead enforcement.' },
      { label: 'Cost timing', value: 'Transaction cost charged at the moment of position change: cost_t = c \u00D7 |w_t \u2212 w_{t\u22121}|. Applied to the same bar as the position change.' },
    ],
    noContradictionNote:
      'The return measurement window (15 bars forward from close) and the execution delay (1 bar) are intentionally distinct. The feature uses close prices; execution assumes next-bar entry. There is no contradiction: the 15-bar forward return is the target we predict, while the 1-bar delay is the execution constraint.',
    returnBindingFormula: {
      label: 'Precise Return Binding',
      formulaLatex: String.raw`\text{P\&L}_t = w_{t-1} \times \ln\!\left(\frac{C_{t+15}}{C_t}\right) - c \times |w_t - w_{t-1}|`,
      note: 'The forward return target r_t is a fixed 15-bar close-to-close measurement. It is NOT measured from entry to exit. Each bar\'s return is earned by the position established one bar prior (w_{t\u22121}). This is per-bar attribution, not trade-level P&L. Holding duration is variable \u2014 the position persists until the next signal change.',
      timestamps: [
        { label: 'Entry timestamp', value: 'Bar t+1 open \u2014 when position w_t becomes active (after signal computed at bar t close)' },
        { label: 'Return earned at bar t', value: 'ln(C_{t+15} / C_t) \u2014 close-to-close over exactly 15 bars' },
        { label: 'Holding duration', value: 'Variable. Position w_t persists until w_{t+k} \u2260 w_t for some k \u2265 1. Each bar independently earns its 15-bar forward return.' },
        { label: 'Exit timestamp', value: 'Bar where signal changes: w_{t+k} \u2260 w_{t+k\u22121}. Exit at next bar open after new signal.' },
      ],
    },
  };

  // ─── 6. Signal Construction Steps ──────────────────────────
  zScoreDoc: StepDoc = {
    name: 'Z-Score Standardization',
    formulaLatex: String.raw`z_t = \frac{f_t - \mu_{\text{train}}}{\sigma_{\text{train}}}`,
    variablesLatex: [
      String.raw`f_t` + ' = raw feature value at bar t',
      String.raw`\mu_{\text{train}}` +
        ' = mean of feature values computed on training period only',
      String.raw`\sigma_{\text{train}}` +
        ' = standard deviation of feature values computed on training period only',
    ],
    interpretation:
      'Standardization transforms the raw feature into units of standard deviation from its training-period mean. This ensures that threshold values (e.g., 1.0\u03C3) have consistent meaning across different features and time periods. A z-score of +2.0 means the current feature value is 2 standard deviations above the training-period average.',
    whyItMatters:
      'Using train-only statistics is critical to prevent lookahead bias. In live trading, you would only know the historical distribution \u2014 you cannot compute statistics using future data. This also ensures that the signal remains calibrated: if the feature\u2019s distribution shifts out-of-sample, the z-scores will reflect this drift rather than hiding it through re-normalization.',
    stationarityAssumption:
      'Implicit assumption: the feature distribution is approximately stationary between the training and test windows. If \u03BC or \u03C3 shift materially between train and OOS, z-scores will be mis-calibrated \u2014 a z = 2.0 in the test window may not represent the same tail probability as z = 2.0 in the train window. The Research Lab\'s Feature Runner validates stationarity via ADF and KPSS tests (p < 0.05) before signal generation. However, the signal engine itself does not re-validate \u2014 it trusts that inputs have passed stationarity screening.',
  };

  parameterStorage = {
    muStorage: '\u03BC_train is computed once per walk-forward fold from the training window. Stored as a float in WalkForwardWindow.mu. In full-sample mode, computed from the 70% train split.',
    sigmaStorage: '\u03C3_train is computed alongside \u03BC_train from the same training window. Stored in WalkForwardWindow.sigma. If \u03C3 < 1e\u207B\u00B9\u2070, the fold is skipped (degenerate feature).',
    thetaStorage: '\u03B8 is selected per fold by argmax of net Sharpe over {0.5, 1.0, 1.5, 2.0} on the training set at default cost (2 bps). Stored in WalkForwardWindow.best_threshold.',
    freezeEnforcement: 'The OOS code path receives frozen (\u03BC, \u03C3, \u03B8) as read-only values. Z-scoring uses these directly: z = (f \u2212 \u03BC) / \u03C3. No recomputation or adaptation occurs during the test window. This is enforced architecturally \u2014 the walk-forward loop computes train stats before entering the OOS evaluation block.',
  };

  signFlipDoc = {
    explanation:
      'When a feature has negative Information Coefficient (IC), it means higher feature values predict lower returns. Rather than building short-only strategies, we flip the z-score sign so that the signal becomes: "go long when the feature is unusually low (oversold), go short when unusually high (overbought)." This converts a mean-reversion feature into a consistently-signed signal.',
    formulaLatex: String.raw`z_t^{\text{flipped}} = -z_t \quad \text{(applied when IC} < 0\text{)}`,
    example:
      'For momentum_5m with negative IC: a large positive momentum reading (price surging up) gets flipped to a strong negative z-score, producing a short signal \u2014 betting on mean reversion back down.',
  };

  thresholdDoc: StepDoc = {
    name: 'Threshold Filtering',
    formulaLatex: String.raw`w_t = \begin{cases} +1 & \text{if } z_t > \theta \\ -1 & \text{if } z_t < -\theta \\ 0 & \text{otherwise} \end{cases}`,
    variablesLatex: [
      String.raw`z_t` + ' = standardized signal value at bar t',
      String.raw`\theta` +
        ' = activation threshold (tested: 0.5\u03C3, 1.0\u03C3, 1.5\u03C3, 2.0\u03C3)',
    ],
    interpretation:
      'The threshold filter converts the continuous z-score into a discrete weight: +1 (long), \u22121 (short), or 0 (flat). Only z-scores exceeding the threshold in absolute value generate a position. This acts as a conviction filter \u2014 only trading when the signal is strong enough.',
    whyItMatters:
      'Higher thresholds mean fewer but higher-conviction trades. Lower thresholds mean more frequent trading with weaker signals. The optimal threshold is selected in-sample by maximizing net Sharpe (after transaction costs), so the cost of frequent trading is directly penalized. The grid tests multiple thresholds (0.5, 1.0, 1.5, 2.0) to map the full trade-off surface.',
  };

  regimeDoc = {
    overview:
      'Signals rarely work in all market conditions. Regime gating identifies the current market environment and suppresses trades when conditions are hostile. The gate is a binary mask: 1 (trade) or 0 (no trade).',
    gatingFormulaLatex: String.raw`\text{gate}_t = \mathbb{1}[\text{vol regime} = \text{Low}] \cdot \mathbb{1}[\text{trend regime} = \text{Sideways}]`,
    volClassification: {
      name: 'Volatility Regime Classification',
      formulaLatex: String.raw`\sigma_d = \text{std}\!\left(\ln\frac{P_i}{P_{i-1}}\right) \text{ over intraday bars on day } d`,
      interpretation:
        'Daily realized volatility is computed from intraday log returns, then classified into terciles across the observation period.',
      regimes: [
        { label: 'Low Vol', rule: 'Below 33rd percentile of daily realized volatility' },
        { label: 'Normal Vol', rule: 'Between 33rd and 67th percentile' },
        { label: 'High Vol', rule: 'Above 67th percentile' },
      ],
    },
    trendClassification: {
      name: 'Trend Regime Classification',
      formulaLatex: String.raw`\text{slope}_d = \frac{MA_{d} - MA_{d-5}}{5}, \quad MA_d = \frac{1}{20}\sum_{i=d-19}^{d} P_i^{\text{close}}`,
      interpretation:
        'The 20-day moving average slope is computed as the 5-day difference of the MA, then compared against a dynamic threshold (50% of median absolute slope).',
      regimes: [
        { label: 'Trending Up', rule: 'MA slope > threshold (positive drift)' },
        { label: 'Sideways', rule: 'MA slope within \u00B1threshold (no clear direction)' },
        { label: 'Trending Down', rule: 'MA slope < \u2212threshold (negative drift)' },
      ],
    },
    percentileScope:
      'Percentile cutoffs are computed on the full daily dataset (all days in the fold), NOT on training-period data only and NOT on a rolling window. This is a deliberate design choice. Regime percentiles are computed on the full observation period available at classification time. In the walk-forward loop, daily regime labels are computed on all bars in the current fold (train + test). This is acceptable because regime classification uses only information available up to day d (closing prices through day d) and does not use forward returns. The classification is causal: each day\u2019s regime label depends only on its own and prior prices.',
    percentileCaveat:
      'The tercile boundaries (33rd/67th percentile of daily realized vol) are computed across all days in the fold. This means OOS days contribute to the percentile calculation. For strict purity, percentiles could be computed on training days only and applied to test days. The current design trades this small lookahead in regime classification for better statistical stability of the tercile boundaries.',
    whyGating:
      'Most mean-reversion signals fail during strong trends (prices don\u2019t revert) or high volatility (noise overwhelms signal). By restricting trading to Low Vol + Sideways conditions, the engine avoids the most common failure modes. This is a conservative gate \u2014 it reduces the number of trades but dramatically improves the quality of those trades.',
    caveat:
      'The regime grid has 3 \u00D7 3 = 9 combinations, but only Low Vol + Sideways passes the gate. This means the signal is inactive roughly 80-90% of the time. This is a feature, not a bug \u2014 it means you only deploy capital when conditions favor the strategy.',
  };

  // ─── 7. Backtesting Framework ──────────────────────────────
  backtestSteps = [
    {
      name: 'Position Sizing',
      formulaLatex: String.raw`w_t = \text{clip}\!\left(\text{signal}_t \cdot \text{gate}_t,\; -1,\; +1\right)`,
      explanation:
        'Final position is signal_t \u00D7 gate_t, clipped to [\u22121, +1]. Since signal_t is already binary (\u00B11 or 0) and gate_t is binary (0 or 1), clipping is a safety guarantee. Positions are: +1 (fully long), \u22121 (fully short), or 0 (flat). No fractional sizing or leverage.',
      caveat:
        'Binary sizing means the backtest cannot capture strategies that scale position size with conviction. This is intentional \u2014 it provides a conservative baseline.',
    },
    {
      name: 'Return Computation (No Lookahead)',
      formulaLatex: String.raw`r_t^{\text{net}} = w_{t-1} \cdot r_t - c \cdot |w_t - w_{t-1}|`,
      explanation:
        'The return at time t uses the position from time t\u22121 (previous bar\u2019s signal) multiplied by the actual return at time t. This is the critical no-lookahead constraint: you cannot trade on information you haven\u2019t seen yet. Transaction costs are proportional to position change (turnover).',
      caveat:
        'The cost model assumes fixed basis points per unit of turnover. Real-world costs include spread, slippage, and market impact \u2014 all of which increase with trade size and decrease with liquidity.',
    },
    {
      name: 'Transaction Cost Model',
      formulaLatex: String.raw`\text{cost}_t = c \cdot |w_t - w_{t-1}|, \quad c = \frac{\text{bps}}{10{,}000}`,
      explanation:
        'Costs are modeled as a fixed number of basis points per unit of position change. The grid tests multiple cost assumptions (1, 2, 3, 5 bps) to show how sensitive performance is to execution quality. Default analysis uses 2 bps.',
      caveat:
        'In reality, costs vary with market conditions, order size, and execution speed. The fixed-cost model is a simplification but provides a useful sensitivity analysis across the cost grid.',
    },
  ];

  backtestMetrics: BacktestMetricDoc[] = [
    {
      name: 'Annualized Sharpe Ratio',
      formulaLatex: String.raw`SR = \frac{\bar{r}}{\hat{\sigma}} \cdot \sqrt{N_{\text{bars/year}}}, \quad N_{\text{bars/year}} = 390 \times 252 = 98{,}280`,
      interpretation:
        'Risk-adjusted return annualized to compare across timeframes. Computed separately for gross (before costs) and net (after costs) returns. The gap between gross and net Sharpe reveals the cost drag. Values above 0.75 are considered investable; above 1.5 is strong.',
    },
    {
      name: 'Maximum Drawdown',
      formulaLatex: String.raw`\text{MDD} = \max_{t}\!\left(\text{peak}_t - \text{cumret}_t\right), \quad \text{peak}_t = \max_{s \leq t} \text{cumret}_s`,
      interpretation:
        'The largest peak-to-trough decline in cumulative net returns. Measures worst-case capital loss. A strategy with high Sharpe but deep drawdowns may be psychologically or financially untenable. Threshold: < 15%.',
    },
    {
      name: 'Annualized Turnover',
      formulaLatex: String.raw`\tau = \overline{|w_t - w_{t-1}|} \cdot N_{\text{bars/year}}`,
      interpretation:
        'Average position change per bar, scaled to annual frequency. High turnover amplifies transaction costs and makes the strategy sensitive to execution quality. A Sharpe of 2.0 with 50x annual turnover will be destroyed by real-world slippage.',
    },
    {
      name: 'Win Rate & Avg Win/Loss Ratio',
      formulaLatex: String.raw`\text{WR} = \frac{|\{r_t^{\text{net}} > 0\}|}{|\{r_t^{\text{net}} \neq 0\}|}, \quad \text{WL} = \frac{\overline{r^+}}{|\overline{r^-}|}`,
      interpretation:
        'Win rate alone is misleading \u2014 a strategy can win 90% of the time but lose catastrophically on the other 10%. The win/loss ratio provides the complementary view. Together they characterize the return distribution\u2019s shape.',
    },
  ];

  turnoverBoundary = {
    scope: 'Turnover is computed on intraday bars only. Overnight resets are not counted because positions do not carry overnight \u2014 cross-day boundaries are masked with NaN returns.',
    flatToFlat: 'Flat (0) \u2192 Long (+1) = 1.0. Long (+1) \u2192 Flat (0) = 1.0. Flat (0) \u2192 Short (\u22121) = 1.0. Long (+1) \u2192 Short (\u22121) = 2.0. Flat \u2192 Flat = 0.',
    directionality: 'This is one-way turnover (absolute position change per bar), NOT round-trip. A long\u2192short reversal counts as 2.0 units of one-way turnover, not 1.0 round-trip. The cost is c \u00D7 |w_t \u2212 w_{t\u22121}|, so a long\u2192short transition costs 2c.',
    formula: 'In words: turnover at bar t = absolute change in position = |w_t \u2212 w_{t\u22121}|. Annualized by multiplying mean per-bar turnover by bars per year (390 \u00D7 252 = 98,280).',
  };

  executionAssumptions = [
    { label: 'Signal Timestamp', value: 'Bar close price' },
    { label: 'Execution', value: 'Next bar open (1-bar delay)' },
    { label: 'Return Measurement', value: 'Close-to-close 15-minute forward log return' },
    { label: 'Transaction Cost Model', value: 'Fixed bps per unit turnover' },
    { label: 'Position Sizing', value: 'Binary: +1 / 0 / \u22121' },
    { label: 'Max Leverage', value: '1x (no leverage)' },
    { label: 'Slippage Model', value: 'Not modeled (known limitation)' },
    { label: 'Overnight Positions', value: 'None (intraday only, cross-day masked)' },
  ];

  // ─── 8. Walk-Forward Validation ────────────────────────────
  walkForwardDoc = {
    overview:
      'In-sample backtests are optimistic because parameters (threshold, cost assumption) are selected with the benefit of hindsight. Walk-forward validation simulates what would happen if you selected parameters on historical data and deployed them on unseen future data.',
    methodology: [
      'Divide data into rolling windows: 3-month train + 1-month test',
      'Shift forward by 1 month and repeat',
      'For each fold: fit (\u03BC, \u03C3) from train data only',
      'Select optimal threshold on train by maximizing net Sharpe',
      'Freeze all parameters (\u03BC, \u03C3, \u03B8) and apply unchanged to test',
      'Record out-of-sample (OOS) metrics on each test window',
      'Aggregate across all folds for final assessment',
    ],
    thresholdSelectionLatex: String.raw`\theta^* = \arg\max_{\theta \in \{0.5, 1.0, 1.5, 2.0\}} SR_{\text{net}}^{\text{train}}(\theta)`,
    thresholdNote:
      'The optimal threshold is selected to maximize net Sharpe in the training period at the default cost assumption (2 bps). This threshold is then frozen and applied to the test period without modification.',
    aggregateMetrics: [
      'Mean OOS Sharpe \u2014 central tendency of out-of-sample performance',
      'Median OOS Sharpe \u2014 robust to outlier folds',
      'Std OOS Sharpe \u2014 consistency across periods',
      'Best / Worst Window Sharpe \u2014 range of outcomes',
      '% Windows with Positive Sharpe \u2014 reliability measure',
      '% Windows Profitable \u2014 how often the strategy makes money OOS',
    ],
    equityCurveNote:
      'The combined OOS equity curve concatenates all test-period returns chronologically. Since test periods never overlap with training periods, this curve represents genuine out-of-sample performance. It is the most honest view of how the signal would have performed in practice.',
  };

  walkForwardWindowLogic = {
    pseudoCode: [
      'months = group_by_calendar_month(unique_dates)',
      'train_months = 3, test_months = 1',
      'for start_idx in range(0, len(months) - 4 + 1):',
      '    train = months[start_idx : start_idx + 3]',
      '    test  = months[start_idx + 3 : start_idx + 4]',
      '    fold_bars = bars[train_start_date .. test_end_date]',
      '    if len(fold_bars) < 500: skip fold',
      '    \u03BC, \u03C3 = fit(feature[train_mask])',
      '    \u03B8* = select_best_threshold(train, cost=2bps)',
      '    oos_metrics = backtest(test, frozen \u03BC, \u03C3, \u03B8*)',
      '    record(fold_index, oos_metrics)',
    ],
    overlapPolicy: 'Training windows overlap between consecutive folds by 2 months (months 2-3 of fold k become months 1-2 of fold k+1). Test windows never overlap. This ensures every calendar month appears as OOS exactly once.',
    minimumTradeRule: 'No minimum trade count is enforced per fold. If a fold produces zero trades (e.g., regime gate blocks all bars), it is still recorded with Sharpe = 0 and trade count = 0. Zero-trade folds are visible in the Walk-Forward Windows table.',
    foldConstruction: 'Folds are constructed from calendar month boundaries, not fixed bar counts. Month boundaries are determined by the first and last trading date in each calendar month. This ensures folds align with natural market cycles rather than arbitrary bar offsets.',
  };

  alphaDecayDoc = {
    formulaLatex: String.raw`\beta = \frac{\sum_{i=1}^{F} (i - \bar{i})(SR_i^{\text{OOS}} - \overline{SR})}{\sum_{i=1}^{F} (i - \bar{i})^2}`,
    variablesLatex: [
      String.raw`SR_i^{\text{OOS}}` + ' = out-of-sample Sharpe ratio for fold i',
      String.raw`F` + ' = total number of walk-forward folds',
      String.raw`\beta` + ' = linear regression slope of OOS Sharpe over time',
    ],
    interpretation:
      'A negative slope indicates that the signal\u2019s out-of-sample performance is deteriorating over time \u2014 a hallmark of alpha decay. This occurs as more market participants discover and trade on the same inefficiency, gradually eliminating the edge.',
    threshold:
      'If \u03B2 < \u22120.1, alpha decay is flagged and the signal status is set to "Degrading." This doesn\u2019t mean the signal is useless \u2014 it means its half-life is finite and deployment should be time-limited.',
  };

  // ─── 9. Effective Sample Size & Autocorrelation ────────────
  effectiveSampleDoc = {
    problem:
      'Financial return series exhibit autocorrelation \u2014 consecutive returns are not independent. A Sharpe ratio computed on 10,000 bars with high autocorrelation may have the same statistical power as one computed on only 2,000 independent observations. Ignoring this inflates confidence and leads to false discoveries.',
    formulaLatex: String.raw`N_{\text{eff}} = \frac{N}{1 + 2\sum_{k=1}^{K} \rho_k}`,
    variablesLatex: [
      String.raw`N` + ' = raw number of observations',
      String.raw`\rho_k` + ' = autocorrelation at lag k (biased estimator: \u03C1_k = (1/N) \u03A3 (r_t \u2212 \u03BC)(r_{t\u2212k} \u2212 \u03BC) / var)',
      String.raw`K` + ' = maximum lag (see truncation rule below)',
      String.raw`N_{\text{eff}}` + ' = effective (independent) sample size',
    ],
    independentBetsLatex: String.raw`\text{Independent Bets} = \lfloor N_{\text{eff}} \rfloor`,
    whyItMatters:
      'The effective sample size determines the true confidence interval around performance estimates. With N_eff = 2,000 instead of raw N = 10,000, your standard error is 2.2x larger than you\u2019d naively expect. This matters for determining whether a Sharpe ratio is statistically distinguishable from zero.',
  };

  autocorrelationTruncation = {
    rule: 'Truncation occurs at the first lag k such that \u03C1_k < 0.05 (signed, not absolute). Only lags 1 through K are summed, where K = min(\u230A\u221AN\u230B, \u230AN/3\u230B).',
    signedOrAbsolute: 'Signed value (\u03C1_k), not |\u03C1_k|. If \u03C1_k = \u22120.03, summation stops because \u22120.03 < 0.05. Negative autocorrelation (mean-reversion in returns) is not accumulated into the penalty.',
    consecutiveRequirement: 'No consecutive-lag requirement. The summation breaks at the first lag below the 0.05 threshold, even if subsequent lags might exceed 0.05 (which could indicate seasonal patterns, not monotone autocorrelation decay).',
    maxLagCap: 'Maximum lag is capped at min(\u230A\u221AN\u230B, \u230AN/3\u230B). For N = 10,000 bars, max lag = min(100, 3333) = 100. This prevents computing unreliable high-lag autocorrelations from limited data.',
    denominatorFloor: 'The denominator (1 + 2\u03A3\u03C1_k) is floored at 1.0. If the sum produces a value below 1, N_eff = N (no reduction). This prevents negative effective N from alternating-sign autocorrelations.',
  };

  // ─── 10. Statistical Significance (§1.6) ───────────────────
  statSignificance = {
    tStatFormula: {
      label: 'Sharpe Ratio t-statistic',
      formulaLatex: String.raw`t = \frac{SR}{\sqrt{1/N_{\text{eff}}}} = SR \cdot \sqrt{N_{\text{eff}}}`,
      note: 'Uses effective sample size, not raw N. This is the Lo (2002) adjustment. A Sharpe of 0.8 with N_eff = 2,000 yields t = 0.8 \u00D7 \u221A2000 = 35.8 (significant). But with N_eff = 50, t = 0.8 \u00D7 \u221A50 = 5.7 (still significant but with wide CI).',
    },
    confidenceInterval: {
      label: 'Confidence Interval for Sharpe Ratio',
      formulaLatex: String.raw`SR \pm z_{\alpha/2} \cdot \sqrt{\frac{1 + SR^2/2}{N_{\text{eff}}}}`,
      note: 'The variance of the Sharpe estimator depends on both sample size and the Sharpe itself (higher Sharpe \u2192 wider CI). At 95% confidence, z = 1.96. From Lo (2002): "The usual practice of ignoring sampling variation in computing Sharpe ratios is dangerously misleading."',
    },
    practicalNote:
      'The signal engine does not currently display t-statistics or confidence intervals in the report UI. These formulas are provided for manual verification. The effective sample size (N_eff) and independent bets count in the report provide the inputs needed for these calculations.',
  };

  // ─── 11. Graduation System ─────────────────────────────────
  graduationIntro =
    'Thresholds are calibrated to represent minimum economically viable signal quality after assumed 2 bps cost and no leverage. Each threshold is sourced from one of three categories: (1) economic viability \u2014 minimum return to justify operational costs, (2) industry heuristic \u2014 standards from quantitative portfolio management literature, or (3) empirical calibration \u2014 thresholds fitted by observing genuine vs. overfit signals in controlled experiments. The source category is documented per criterion below.';

  graduationCriteria: GraduationCriterionDoc[] = [
    {
      name: 'Net Sharpe Ratio',
      threshold: '> 0.75',
      formulaLatex: String.raw`SR_{\text{net}} > 0.75`,
      rationale:
        'The minimum risk-adjusted return after transaction costs. A Sharpe below 0.75 is generally not investable when accounting for implementation costs, model risk, and the opportunity cost of capital.',
      justification:
        'Industry heuristic calibrated to practical deployment. A Sharpe of 0.75 after costs implies roughly 7.5% annualized return at 10% volatility. Below this, the signal is not worth the operational complexity of deployment. This threshold aligns with institutional allocator minimums documented in Qian, Hua & Sorensen (2007).',
      failureAdvice:
        'Consider stronger feature transformations, longer lookback periods, or features with higher raw IC. The signal may also benefit from combining with complementary features (future enhancement).',
    },
    {
      name: 'Maximum Drawdown',
      threshold: '< 15%',
      formulaLatex: String.raw`\text{MDD} < 0.15`,
      rationale:
        'Capital preservation constraint. A 15% drawdown is the maximum most allocators will tolerate before reducing or eliminating an allocation. Deeper drawdowns create compounding recovery problems.',
      justification:
        'Economic viability threshold. A 15% drawdown requires 17.6% gain to recover \u2014 manageable. A 30% drawdown requires 42.9% \u2014 often fatal for a single-signal strategy. The 15% level is widely used in systematic trading as the "pain threshold" for single-strategy allocations.',
      failureAdvice:
        'Consider tighter thresholds (trade only on extreme signals), shorter holding periods, or more restrictive regime gating. Position sizing limits may also help.',
    },
    {
      name: 'OOS Windows Positive Sharpe',
      threshold: '> 60%',
      formulaLatex: String.raw`\frac{|\{SR_i^{\text{OOS}} > 0\}|}{F} > 0.60`,
      rationale:
        'The signal must demonstrate positive risk-adjusted returns in the majority of out-of-sample test periods. A signal that works brilliantly in one period but fails in three others is not reliable.',
      justification:
        'Empirical calibration. With F walk-forward folds, a random signal (Sharpe = 0) would produce ~50% positive OOS windows by chance. The 60% threshold requires the signal to beat chance consistently. With 10 folds, 60% = 6/10 positive, giving a binomial p-value of 0.17 against the null \u2014 modest but combined with other criteria provides robustness.',
      failureAdvice:
        'This suggests the signal is unstable across time periods. Investigate whether performance concentrates in specific regimes. Consider whether the signal has a limited effective lifespan.',
    },
    {
      name: 'Regime Coverage',
      threshold: '\u2265 4 of 6 regimes',
      rationale:
        'The signal must be tested across diverse market conditions (3 vol regimes \u00D7 2+ trend regimes). Insufficient coverage means conclusions may not generalize to unseen environments.',
      justification:
        'Industry heuristic for generalizability. 6 regimes = 3 vol \u00D7 (Trending + Sideways, grouped into 2 effective categories). Requiring 4/6 ensures the signal has been observed in at least 2 vol regimes and 2 trend regimes. This prevents a signal from graduating based solely on a single favorable market condition.',
      failureAdvice:
        'Need more historical data spanning different market conditions. Consider extending the analysis period or testing on additional tickers that experienced different regime exposures.',
    },
    {
      name: 'Parameter Stability',
      threshold: 'Score > 0.50',
      formulaLatex: String.raw`S = 1 - \frac{\sigma(SR_\theta)}{|\mu(SR_\theta)|}`,
      rationale:
        'Performance should not be critically dependent on the exact threshold chosen. If Sharpe is 1.5 at \u03B8 = 1.0 but 0.2 at \u03B8 = 0.5 and 1.5, the result is likely an artifact of threshold optimization rather than genuine signal quality.',
      justification:
        'Empirical calibration. A stability score of 0.5 means the standard deviation of Sharpe across thresholds is at most 50% of the mean. This is a coefficient-of-variation test: CV < 0.5 indicates the signal is reasonably robust to threshold choice. The threshold was calibrated by observing that genuine signals typically show CV < 0.3 (score > 0.7) while overfit signals show CV > 1.0 (score < 0).',
      failureAdvice:
        'High sensitivity to threshold suggests overfitting. The signal may only work in a narrow parameter regime. Consider whether the underlying feature has genuine predictive content or if the "sweet spot" is a statistical fluke.',
    },
  ];

  gradingScale = [
    { grade: 'A', criteria: '5/5 passed', meaning: 'Production-ready signal' },
    { grade: 'B', criteria: '4/5 passed', meaning: 'Strong signal with one weakness' },
    { grade: 'C', criteria: '3/5 passed', meaning: 'Moderate signal, needs improvement' },
    { grade: 'D', criteria: '2/5 passed', meaning: 'Weak signal, significant issues' },
    { grade: 'F', criteria: '0\u20131/5 passed', meaning: 'Failed \u2014 signal not viable' },
  ];

  statusLabels: StatusLabelDoc[] = [
    {
      label: 'Exploratory',
      condition: 'N_eff < 1,000 or fewer than 3 walk-forward folds or not enough data',
      meaning: 'Insufficient data to make a reliable assessment. Results are preliminary and should not inform allocation decisions.',
      color: 'info',
    },
    {
      label: 'Degrading',
      condition: 'OOS Sharpe trend slope \u03B2 < \u22120.1',
      meaning: 'Alpha decay detected \u2014 out-of-sample performance is declining over time. Deploy with caution and monitor closely. The signal has a finite lifespan.',
      color: 'danger',
    },
    {
      label: 'Conditional Alpha',
      condition: 'All 5 criteria passed, but not classified as Robust',
      meaning: 'The signal passes all graduation criteria but has some sensitivity. It may require regime gating or careful parameter selection to perform well.',
      color: 'warn',
    },
    {
      label: 'Robust Alpha',
      condition: 'All passed + Stable parameters + \u226570% windows positive Sharpe',
      meaning: 'The strongest classification. The signal passes all criteria, is stable across parameters, and works reliably in the majority of time periods. Best candidate for deployment.',
      color: 'success',
    },
  ];

  stabilityDoc = {
    formulaLatex: String.raw`S = 1 - \frac{\sigma(SR_\theta)}{|\mu(SR_\theta)|}`,
    variablesLatex: [
      String.raw`SR_\theta` + ' = net Sharpe ratio at threshold \u03B8 (at default cost)',
      String.raw`\sigma(SR_\theta)` + ' = standard deviation of Sharpe across tested thresholds',
      String.raw`|\mu(SR_\theta)|` + ' = absolute mean of Sharpe across tested thresholds',
    ],
    labels: [
      { label: 'Stable', range: 'Score \u2265 0.70', meaning: 'Performance is consistent across threshold choices' },
      { label: 'Sensitive', range: '0.40 \u2264 Score < 0.70', meaning: 'Moderate dependence on threshold \u2014 some caution needed' },
      { label: 'Fragile', range: 'Score < 0.40', meaning: 'Performance varies dramatically with threshold \u2014 likely overfit' },
    ],
  };

  // ─── 12. IS/OOS Reporting Standards (§3.1\u20133.4) ────────────
  reportingStandards = {
    separation: [
      'In-Sample Metrics: Computed on the 70/30 train split (backtest grid). Labeled as "Backtest Grid" in the report.',
      'Out-of-Sample Metrics: Computed on walk-forward test windows with frozen parameters. Labeled as "Walk-Forward Summary" and "Walk-Forward Windows" in the report.',
      'Combined Metrics: The graduation criteria use a mix \u2014 Net Sharpe from IS (criterion 1-2), OOS window stats (criterion 3). Each criterion\'s source (IS vs OOS) is documented in the criterion description.',
    ],
    minimumTradeDisclosure: [
      'The Walk-Forward Windows table shows trade count per OOS fold (oos_total_trades column).',
      'The Signal Diagnostics section shows % time active (non-zero position) across the full dataset.',
      'Zero-trade folds appear with Sharpe = 0.00, Return = 0.0000, and Trades = 0. They are NOT hidden.',
      'The Execution Assumptions panel shows total trades for the best in-sample configuration.',
    ],
    distributionStability: [
      'Signal Diagnostics reports: signal mean, signal std (IS computed on full z-score series).',
      '% filtered by threshold: fraction of bars zeroed by threshold filter (IS).',
      '% gated by regime: fraction of bars zeroed by regime gate (IS, after threshold).',
      'Walk-Forward windows provide per-fold OOS Sharpe, return, and trade count \u2014 these serve as the OOS distribution snapshot.',
      'The IS/OOS gap is visible by comparing Backtest Grid Sharpe (IS) to Mean OOS Sharpe (walk-forward).',
    ],
  };

  reportingPrecision: ReportingPrecision[] = [
    { metric: 'Sharpe Ratio', decimals: '2', example: '0.84' },
    { metric: 'Maximum Drawdown', decimals: '2 (as fraction)', example: '0.12' },
    { metric: 'Turnover', decimals: '1', example: '5.3x' },
    { metric: 'Percentages', decimals: '1', example: '67.0%' },
    { metric: 'Returns', decimals: '4', example: '0.0023' },
    { metric: 'Thresholds', decimals: '1', example: '1.0' },
    { metric: 'Z-Score Statistics', decimals: '4', example: '0.0012' },
    { metric: 'Bar Counts', decimals: '0 (integer)', example: '4,230' },
  ];

  // ─── 13. Hypothesis & Failure Interpretation (§4.1\u20134.2) ──
  hypothesisTemplate = {
    instruction: 'No backtest is run before a hypothesis is documented. Every signal analysis must begin with a clear structural hypothesis. This prevents post-hoc narrative fitting and ensures research intent is explicit.',
    template: 'Hypothesis: [Feature Name] exhibits [positive/negative] correlation to future returns at a [Time Horizon] horizon due to [Structural Mechanism / Market Microstructure]. The signal exploits [Specific Inefficiency] with [Counterparty] on the other side. Expected to work best in [Optimal Regimes] and fail in [Hostile Regimes].',
  };

  failureInterpretation = {
    instruction: 'Every report includes a graduation summary with pass/fail status, failure reasons, and the primary driver. The auto-generated research log provides a 1-2 sentence interpretation at the bottom of each report.',
    sections: [
      { label: 'Why it passed/failed', description: 'Each graduation criterion reports Pass/Marginal/Fail with the measured value vs threshold. Failed criteria include a specific failure_reason explaining the shortfall.' },
      { label: 'Primary failure driver', description: 'The graduation summary identifies the most impactful failure. For example: "Failed: Maximum Drawdown; OOS Windows Positive Sharpe" \u2014 listed in order of severity.' },
      { label: 'Secondary weaknesses', description: 'Criteria marked "Marginal" (value between fail and pass thresholds) are highlighted even if technically passing. These indicate areas of concern.' },
      { label: 'Suggested next step', description: 'Each failure criterion includes failureAdvice pointing to the research direction (e.g., "Consider tighter thresholds" or "Need more data"). These are research suggestions, not automatic changes.' },
    ],
  };

  // ─── 14. Versioning (§4.3) ─────────────────────────────────
  versioning = {
    fields: [
      { label: 'Pipeline Version', value: '1.0 \u2014 Single-feature, single-asset, binary position, fixed cost' },
      { label: 'Signal Engine Version', value: '1.0 \u2014 Z-score + threshold + regime gate' },
      { label: 'Backtest Engine Version', value: '1.0 \u2014 No-lookahead, fixed bps cost model' },
      { label: 'Walk-Forward Version', value: '1.0 \u2014 3-month train / 1-month test, monthly rolling' },
      { label: 'Graduation Version', value: '1.0 \u2014 5 criteria, A\u2013F grading, 4 status labels' },
      { label: 'Documentation Date', value: '2026-02-27 \u2014 date this documentation was last updated' },
      { label: 'Data Snapshot', value: 'Per-run \u2014 the report header shows the exact date range of OHLCV data used for each analysis' },
    ],
    note: 'Version numbers increment when criteria thresholds change, new pipeline stages are added, or existing computation logic is modified. The report header includes the analysis date range and ticker, ensuring each run is traceable to a specific data snapshot.',
  };

  // ─── 15. Internal Audit Checklist (§5) ─────────────────────
  auditChecklist: AuditCheck[] = [
    {
      category: 'Data Integrity',
      checks: [
        'No train/test data leakage: z-score \u03BC and \u03C3 computed on train only',
        'Forward returns masked at day boundaries (no overnight contamination)',
        'Bars sorted by timestamp before any computation',
      ],
    },
    {
      category: 'Parameter Freezing',
      checks: [
        'Parameters (\u03BC, \u03C3, \u03B8) frozen before OOS evaluation begins',
        'OOS code path does not recompute or update any train parameters',
        'Walk-forward window uses frozen threshold from train, not re-optimized on test',
      ],
    },
    {
      category: 'Execution Integrity',
      checks: [
        'Return uses w_{t\u22121} \u00D7 r_t (previous-bar position earns current return)',
        'Transaction costs applied to |w_t \u2212 w_{t\u22121}| (position change, not position)',
        'All metrics computed on net returns (after costs), not gross',
        'Execution timing consistent: signal at close, entry at next bar',
      ],
    },
    {
      category: 'Reporting Completeness',
      checks: [
        'Effective N reported alongside raw N',
        'Trade counts reported for IS (backtest grid) and OOS (per fold)',
        'Zero-trade folds visible in walk-forward table',
        'IS and OOS metrics clearly separated in report',
        'Regime coverage grid shows count per cell',
      ],
    },
    {
      category: 'Statistical Validity',
      checks: [
        'Sharpe computed with ddof=1 (sample standard deviation)',
        'Autocorrelation truncation rule applied for N_eff',
        'No negative N_eff (denominator floored at 1.0)',
        'Walk-forward folds have no test-period overlap',
      ],
    },
  ];

  // ─── 16. Interpreting Results ──────────────────────────────
  bestPractices = [
    {
      title: 'High Sharpe + High Turnover = Untradeable',
      detail:
        'A Sharpe of 2.0 with 50x annual turnover will be destroyed by real-world execution costs not modeled here (slippage, market impact, crossing the spread). Always check the turnover grid alongside the Sharpe grid. A lower Sharpe with manageable turnover is preferable.',
    },
    {
      title: 'Regime Gating is a Feature, Not a Bug',
      detail:
        'If your signal only works in Low Vol + Sideways, that\u2019s useful information \u2014 it means you should NOT deploy during high-vol or trending regimes. The gate reduces trade frequency but dramatically improves trade quality. Think of it as an intelligent filter, not a limitation.',
    },
    {
      title: 'In-Sample vs OOS Gap',
      detail:
        'A large gap between the backtest grid Sharpe and walk-forward OOS Sharpe is a red flag for overfitting. The backtest grid uses train-period statistics on 70% of data; walk-forward uses rolling frozen parameters. Trust the OOS numbers \u2014 they are your best estimate of live performance.',
    },
    {
      title: 'Alpha Decay is the Normal State',
      detail:
        'Most signals decay over time as markets adapt. A declining OOS Sharpe trend is not failure \u2014 it\u2019s information about signal half-life. Use it to plan deployment horizon: if the slope suggests 6 months of viability, plan to rotate to new signals before that horizon.',
    },
    {
      title: 'Parameter Stability > Peak Performance',
      detail:
        'A signal with Sharpe = 0.8 across all thresholds is more deployable than Sharpe = 1.5 at one threshold and 0.2 at others. The stable signal will perform similarly regardless of exact parameter choice; the fragile one requires getting the parameter exactly right \u2014 which you can\u2019t guarantee in live trading.',
    },
    {
      title: 'Effective N Determines Confidence',
      detail:
        'Don\u2019t trust raw bar counts. A 10,000-bar sample with high autocorrelation may yield only 2,000 effective observations. Check the Effective Sample Size section of the report before drawing conclusions. If N_eff is below 1,000, the signal is still "Exploratory" regardless of its Sharpe.',
    },
  ];

  // ─── 17. Known Limitations ─────────────────────────────────
  limitations = [
    {
      title: 'No Slippage or Market Impact Model',
      detail:
        'Real execution involves spread crossing, queue priority, and price impact from your own orders. These costs scale with order size and inversely with liquidity. The fixed bps model underestimates costs for larger positions or illiquid instruments.',
    },
    {
      title: 'Binary Position Sizing Only',
      detail:
        'Positions are +1, 0, or \u22121. No continuous sizing based on signal strength. This prevents the backtest from capturing strategies that bet more on stronger signals, but also avoids leverage-amplified results that wouldn\u2019t survive real deployment.',
    },
    {
      title: 'Fixed Cost Model',
      detail:
        'Transaction costs are modeled as a constant rate per unit of turnover. In reality, costs vary with time of day, market volatility, order size, and execution venue. The cost grid partially addresses this by testing multiple cost assumptions.',
    },
    {
      title: 'Single-Asset Testing',
      detail:
        'The engine tests one ticker at a time. Portfolio effects (diversification, correlation, cross-asset hedging) are not captured. A signal that looks marginal on one asset may be valuable in a diversified portfolio.',
    },
    {
      title: 'Conservative Regime Gate',
      detail:
        'Only Low Vol + Sideways passes the gate, which may filter out too aggressively. Some signals may work well in Normal Vol or mild trending environments. Future versions could optimize the gate itself.',
    },
    {
      title: 'No Overnight Positions',
      detail:
        'Cross-day boundaries are masked \u2014 no positions carry overnight. This eliminates overnight gap risk but also misses signals that predict opening moves or multi-day trends.',
    },
  ];

  // ─── 18. Academic References ───────────────────────────────
  references: ReferenceDoc[] = [
    {
      citation: 'Grinold, R. (1989) \u2014 The Fundamental Law of Active Management',
      relevance: 'Foundation for IR \u2248 IC \u00D7 \u221ABreadth. Explains why even modest IC compounds across many trades.',
    },
    {
      citation: 'Qian, E., Hua, R. & Sorensen, E. (2007) \u2014 Quantitative Equity Portfolio Management',
      relevance: 'Industry-standard reference for IC analysis, factor evaluation, and portfolio construction. ICIR > 0.5 benchmark originates here. Sharpe > 0.75 threshold calibration source.',
    },
    {
      citation: 'Harvey, C., Liu, Y. & Zhu, H. (2016) \u2014 ...and the Cross-Section of Expected Returns',
      relevance: 'Documents multiple testing bias in factor discovery. Testing many features inflates false positives \u2014 t-stat thresholds should be adjusted upward.',
    },
    {
      citation: 'Bailey, D. & Lopez de Prado, M. (2014) \u2014 The Deflated Sharpe Ratio',
      relevance: 'Adjusts Sharpe ratio for multiple testing and non-normal returns. Sharpe ratios from backtests are systematically inflated.',
    },
    {
      citation: 'White, H. (2000) \u2014 A Reality Check for Data Snooping',
      relevance: 'Bootstrap method for testing whether the best strategy from a set is genuinely profitable or just the luckiest of many trials.',
    },
    {
      citation: 'Lo, A. (2002) \u2014 The Statistics of Sharpe Ratios',
      relevance: 'Derives the sampling distribution and confidence intervals for Sharpe ratios. Shows that Sharpe estimates from short samples are very noisy. Source for t-stat and CI formulas used in this documentation.',
    },
  ];
}
