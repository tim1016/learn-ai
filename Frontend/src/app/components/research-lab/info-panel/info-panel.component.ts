import { Component, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { AccordionModule } from 'primeng/accordion';
import { DividerModule } from 'primeng/divider';
import { KatexDirective } from '../../../shared/katex.directive';

interface FeatureDoc {
  name: string;
  formulaLatex: string;
  variablesLatex: string[];
  interpretation: string;
  implementationNote: string;
  window: number;
  category: string;
  tradingInsight: string;
}

interface TestDoc {
  name: string;
  description: string;
  formulaLatex: string;
  hypothesisLatex?: string;
  interpretation: string;
  threshold: string;
  whyItMatters: string;
}

interface MicrostructureDriver {
  title: string;
  explanation: string;
  formulaLatex?: string;
  formulaVariables?: string[];
}

interface MicrostructureDoc {
  featureName: string;
  hypothesis: string;
  drivers: MicrostructureDriver[];
  expectedDecay: string[];
  failureRegimes: string[];
  academicReferences: string[];
  checklist: { question: string; answer: string }[];
}

interface JustificationCriterion {
  name: string;
  description: string;
}

@Component({
  selector: 'app-info-panel',
  standalone: true,
  imports: [CommonModule, AccordionModule, DividerModule, KatexDirective],
  templateUrl: './info-panel.component.html',
  styleUrls: ['./info-panel.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class InfoPanelComponent {
  targetDoc = {
    name: '15-Minute Forward Log Return',
    formulaLatex: String.raw`R_{15}(t) = \ln\!\left(\frac{P_{t+15}}{P_t}\right)`,
    variablesLatex: [
      String.raw`P_t` + ' = close price at bar ' + String.raw`t`,
      String.raw`P_{t+15}` + ' = close price 15 bars ahead',
    ],
    whyLogReturns: [
      'Additive across time periods — log returns for consecutive intervals sum to the total return, simplifying multi-period analysis',
      'Approximately normally distributed — enables standard statistical tests (t-test, confidence intervals) without transformation',
      'Symmetric treatment of gains and losses — a +10% move and \u221210% move have equal magnitude in log space, preventing upward bias',
    ],
    crossDayNote:
      'Cross-day boundaries are masked with NaN — forward returns never span midnight to prevent overnight gap contamination.',
    horizonNote:
      '15-minute horizon balances signal detection with practical execution. Shorter horizons capture more noise; longer horizons introduce confounding from macro events and competing signals.',
  };

  methodology = {
    icExplanation: 'The Information Coefficient (IC) framework, pioneered by Grinold (1989) and central to the Fundamental Law of Active Management, measures the correlation between predicted and realized cross-sectional returns. In our implementation, we compute daily Spearman rank correlations between feature values and subsequent 15-minute log returns, then average across trading days to obtain the Mean IC.',
    icirExplanation: 'The IC Information Ratio (ICIR = Mean IC / Std(IC)) measures signal consistency. An IC that varies wildly across days is unreliable even if the mean is high. ICIR > 0.5 is considered strong by industry standards (Qian, Hua & Sorensen, 2007).',
    whySpearman: 'Spearman rank correlation is preferred over Pearson because it captures monotonic (not just linear) relationships, is robust to outliers, and does not require normally distributed features. This is critical for technical indicators which often have skewed or heavy-tailed distributions.',
    alphaDecay: 'Alpha factors lose predictive power over time as more market participants discover and trade on the same signals. Regularly re-running validation experiments helps detect alpha decay \u2014 a declining trend in mean IC across experiments is a warning sign.',
  };

  features: FeatureDoc[] = [
    {
      name: '5-Minute Momentum',
      formulaLatex: String.raw`M_5(t) = \frac{P_t - P_{t-5}}{P_{t-5}}`,
      variablesLatex: [
        String.raw`P_t` + ' = close price at bar t',
      ],
      interpretation:
        'Measures short-term price velocity. Positive values indicate upward momentum; negative values indicate selling pressure. Tests whether recent trend predicts near-term returns.',
      tradingInsight: 'Momentum is one of the most well-documented anomalies in finance (Jegadeesh & Titman, 1993). At the intraday level, short-term momentum often reflects institutional order flow \u2014 large orders are typically broken into smaller chunks executed over minutes.',
      implementationNote: 'pandas pct_change(periods=5)',
      window: 5,
      category: 'Momentum',
    },
    {
      name: 'RSI (14)',
      formulaLatex: String.raw`\text{RSI} = 100 - \frac{100}{1 + RS}, \quad RS = \frac{\overline{\text{gain}}_{14}}{\overline{\text{loss}}_{14}}`,
      variablesLatex: [
        String.raw`\overline{\text{gain}}_{14}` + ' = mean of positive close-to-close changes over 14 bars',
        String.raw`\overline{\text{loss}}_{14}` + ' = mean of negative close-to-close changes over 14 bars',
      ],
      interpretation:
        'Bounded oscillator (0\u2013100). RSI > 70 suggests overbought conditions, RSI < 30 suggests oversold. We test whether extreme RSI readings predict mean-reverting forward returns.',
      tradingInsight: 'RSI is a mean-reversion signal \u2014 it profits when prices snap back from extremes. At intraday frequencies, RSI captures short-term exhaustion of buying or selling pressure. The 14-bar window balances sensitivity with noise resistance.',
      implementationNote: 'pandas-ta rsi(length=14)',
      window: 14,
      category: 'Mean Reversion',
    },
    {
      name: 'Realized Volatility (30)',
      formulaLatex: String.raw`\sigma_{30}(t) = \sqrt{ \frac{1}{29} \sum_{i=0}^{29} \left( r_{t-i} - \bar{r} \right)^2 }`,
      variablesLatex: [
        String.raw`r_t = \ln(P_t / P_{t-1})` + ' = log return at bar t',
        String.raw`\bar{r}` + ' = mean log return over the 30-bar window',
      ],
      interpretation:
        'Measures recent price dispersion. Higher realized volatility often precedes mean reversion or signals regime change. Low volatility can precede breakouts.',
      tradingInsight: 'Volatility clustering (Mandelbrot, 1963) means high-vol periods tend to follow high-vol periods. This makes realized volatility predictive of future risk levels. Strategies that condition on volatility \u2014 scaling position sizes or timing entries \u2014 often show improved risk-adjusted returns.',
      implementationNote: 'Rolling std of log returns (window=30)',
      window: 30,
      category: 'Volatility',
    },
    {
      name: 'Volume Z-Score',
      formulaLatex: String.raw`Z_V(t) = \frac{V_t - \mu_{20}}{\sigma_{20}}`,
      variablesLatex: [
        String.raw`V_t` + ' = volume at bar t',
        String.raw`\mu_{20}` + ' = rolling 20-bar mean volume',
        String.raw`\sigma_{20}` + ' = rolling 20-bar standard deviation of volume',
      ],
      interpretation:
        'Standardized volume relative to recent history. Z > 2 indicates unusually high activity. Volume spikes often accompany or precede significant price moves.',
      tradingInsight: 'Volume is the "fuel" of price moves. Unusually high volume validates the direction of a price movement. Blume, Easley & O\'Hara (1994) showed that volume carries information about future price changes that price alone does not capture, making it an orthogonal predictor.',
      implementationNote: 'Rolling z-score normalization (window=20)',
      window: 20,
      category: 'Volume',
    },
    {
      name: 'MACD Signal Line',
      formulaLatex: String.raw`\text{MACD} = \text{EMA}_{12} - \text{EMA}_{26}, \quad \text{Signal} = \text{EMA}_9(\text{MACD})`,
      variablesLatex: [
        String.raw`\text{EMA}_n` + ' = exponential moving average with span n',
      ],
      interpretation:
        'Trend-following momentum indicator. MACD crossing above the signal line is bullish; crossing below is bearish. We test the signal line value as a continuous predictor of forward returns.',
      tradingInsight: 'MACD is a dual-timeframe momentum measure \u2014 it captures the convergence/divergence between short-term and medium-term trends. At intraday scale, MACD crossovers often coincide with shifts in order flow direction, making them useful for timing entries.',
      implementationNote: 'pandas-ta macd(fast=12, slow=26, signal=9)',
      window: 26,
      category: 'Trend',
    },
  ];

  tests: TestDoc[] = [
    {
      name: 'Information Coefficient (IC)',
      description:
        'The gold standard for evaluating alpha factors. Computes Spearman rank correlation between feature values and forward returns for each day, then averages across days.',
      formulaLatex: String.raw`\text{IC}_d = \rho_{\text{Spearman}}(X_d, R_d), \quad \overline{\text{IC}} = \frac{1}{D}\sum_{d=1}^{D} \text{IC}_d`,
      hypothesisLatex: String.raw`t = \frac{\overline{\text{IC}}}{\text{SE}(\text{IC})}, \quad \text{reject } H_0 \text{ if } |t| > 1.65`,
      interpretation:
        'Mean IC > 0.03 with t-stat > 1.65 (p < 0.10) suggests a meaningful predictive signal. Higher IC magnitude indicates stronger predictive power.',
      threshold: '|Mean IC| > 0.03, t-stat > 1.65',
      whyItMatters: 'An IC of 0.05 means the feature explains roughly 0.25% of return variance \u2014 this sounds small but compounds significantly across thousands of trades. Grinold\'s Fundamental Law: IR \u2248 IC \u00D7 \u221A(breadth), so even modest IC with high trading frequency yields substantial information ratios.',
    },
    {
      name: 'ADF Stationarity Test',
      description:
        'The Augmented Dickey-Fuller test checks whether the feature time series has a unit root (non-stationary). Non-stationary features have unstable statistical properties, making them unreliable predictors.',
      formulaLatex: String.raw`\Delta y_t = \alpha + \beta t + \gamma y_{t-1} + \sum_{i=1}^{p} \delta_i \Delta y_{t-i} + \epsilon_t`,
      hypothesisLatex: String.raw`H_0\!: \gamma = 0 \text{ (unit root)} \quad H_1\!: \gamma < 0 \text{ (stationary)}`,
      interpretation:
        'Reject H\u2080 (p < 0.05) \u2192 feature is stationary \u2192 safe to use as predictor. Complemented by KPSS test (which has the opposite null hypothesis) for robust stationarity confirmation.',
      threshold: 'ADF p < 0.05 and KPSS p > 0.05',
      whyItMatters: 'A non-stationary feature (like a random walk) produces spurious correlations \u2014 you might see high IC in-sample that disappears out-of-sample. Stationarity ensures the feature\'s relationship with returns is stable over time. The dual ADF + KPSS test provides robust confirmation.',
    },
    {
      name: 'Quantile Monotonicity',
      description:
        'Sorts all observations into 5 equal-sized bins by feature value, then computes the mean forward return in each bin. Checks whether returns increase (or decrease) monotonically across bins.',
      formulaLatex: String.raw`\mathbb{E}[R \mid Q_k] \text{ for } k = 1, \ldots, 5`,
      hypothesisLatex: String.raw`\text{monotonic if } \mathbb{E}[R|Q_1] \leq \mathbb{E}[R|Q_2] \leq \cdots \leq \mathbb{E}[R|Q_5]`,
      interpretation:
        'Monotonic quantile returns confirm a dose-response relationship \u2014 more signal produces more return. This is the strongest form of predictive evidence, ruling out non-linear artifacts.',
      threshold: 'Monotonicity ratio \u2265 75%',
      whyItMatters: 'A feature might show high mean IC due to one extreme quantile driving the correlation, while middle quantiles show no pattern. Monotonicity ensures the feature\'s predictive power is spread across its entire range \u2014 this is what separates robust signals from fragile ones.',
    },
  ];

  bestPractices = [
    {
      title: 'Never trust IC alone',
      detail: 'Always pair mean IC with rolling IC analysis and quantile breakdowns. A feature with IC = 0.08 that fluctuates between \u22120.2 and +0.3 is less reliable than IC = 0.04 that stays consistently between 0.02 and 0.06.',
    },
    {
      title: 'Watch for alpha decay',
      detail: 'Run the same feature at regular intervals. A declining trend in mean IC over time signals that the market is adapting to your signal. Consider shorter windows or new feature combinations.',
    },
    {
      title: 'Stationarity is non-negotiable',
      detail: 'Never deploy a non-stationary feature in production. Apply differencing, z-scoring, or log transforms to achieve stationarity before testing predictive power.',
    },
    {
      title: 'Quantiles reveal the full picture',
      detail: 'The quantile bar chart is your "report card." Monotonic returns across Q1 to Q5 confirm a genuine signal. If only extreme quantiles show significance, the feature may be useful only as a binary signal (top/bottom decile).',
    },
    {
      title: 'Multiple testing adjustment',
      detail: 'Testing many features inflates the chance of false positives. If you test 5 features, expect ~0.5 to pass by chance at p = 0.10. Apply Bonferroni or Holm corrections for rigorous validation.',
    },
  ];

  microstructureDocs: MicrostructureDoc[] = [
    {
      featureName: '5-Minute Momentum',
      hypothesis:
        'Short-term returns exhibit positive autocorrelation driven by institutional order flow persistence and behavioral underreaction to new information.',
      drivers: [
        {
          title: 'Order Flow Persistence',
          explanation:
            'Large institutions slice orders using execution algorithms (VWAP, TWAP), creating sustained directional pressure over short horizons. This relates to Kyle\'s (1985) model of price impact and informed trading.',
          formulaLatex: String.raw`P_{t+1} = P_t + \lambda \cdot q_t`,
          formulaVariables: [
            String.raw`q_t` + ' = signed order flow at time t',
            String.raw`\lambda` + ' = price impact coefficient',
          ],
        },
        {
          title: 'Behavioral Underreaction',
          explanation:
            'Traders don\'t fully update beliefs instantly. News is incorporated gradually into prices, creating a short-term drift. This is the classic momentum explanation from Jegadeesh & Titman (1993) \u2014 while their original work was at monthly frequency, the principle scales to intraday horizons.',
        },
      ],
      expectedDecay: [
        'Strong at 5\u201330 minutes',
        'Weakens beyond 1 hour',
        'Reverses intraday if liquidity providers fade moves',
      ],
      failureRegimes: [
        'Mean-reverting markets (range-bound, low trend)',
        'High-frequency liquidity shocks',
        'Macro news events (FOMC, CPI) that reset price levels instantly',
      ],
      academicReferences: [
        'Kyle, A.S. (1985) \u2014 Continuous Auctions and Insider Trading',
        'Jegadeesh, N. & Titman, S. (1993) \u2014 Returns to Buying Winners and Selling Losers',
      ],
      checklist: [
        {
          question: 'What inefficiency does this exploit?',
          answer:
            'Autocorrelated order flow from institutional execution algorithms',
        },
        {
          question: 'Who is the counterparty?',
          answer:
            'Slow-reacting traders and passive liquidity providers who haven\'t yet adjusted quotes',
        },
        {
          question: 'Why does it persist?',
          answer:
            'Institutions must break large orders into smaller pieces \u2014 a structural constraint that creates predictable flow',
        },
        {
          question: 'What kills it?',
          answer:
            'Faster execution algorithms, mean-reversion strategies that aggressively fade moves, or sudden regime shifts (macro events)',
        },
        {
          question: 'Expected decay profile?',
          answer:
            'Peak predictive power at 5\u201315 minutes, decays by 30\u201360 minutes, often reverses intraday',
        },
      ],
    },
    {
      featureName: 'RSI (14)',
      hypothesis:
        'Short-term overbought/oversold states revert as market makers manage inventory and retail traders anchor to psychological thresholds.',
      drivers: [
        {
          title: 'Retail Anchoring to Thresholds',
          explanation:
            'Retail traders anchor to overbought (>70) and oversold (<30) thresholds, creating self-reinforcing order flow at extremes. This behavioral pattern provides a predictable counterparty for mean-reversion strategies.',
        },
        {
          title: 'Market Maker Inventory Management',
          explanation:
            'Market makers hedge inventory imbalances and push price back to neutral. When RSI reaches extremes, it signals short-term liquidity exhaustion \u2014 aggressive buyers/sellers have likely exhausted their immediate demand.',
        },
        {
          title: 'Regime Sensitivity',
          explanation:
            'RSI captures continuation in trending regimes but reversal in range-bound regimes. The validation layer should test conditional IC (e.g., IC given high volatility) because RSI is inherently regime-sensitive.',
          formulaLatex: String.raw`\text{RSI} = 100 - \frac{100}{1 + RS}`,
        },
      ],
      expectedDecay: [
        'Mean reversion strongest at 5\u201320 minutes after extreme readings',
        'In trending regimes, RSI extremes may persist \u2014 signal reverses',
        'Effectiveness depends heavily on volatility regime',
      ],
      failureRegimes: [
        'Strong trending markets (RSI stays pinned at extremes)',
        'News-driven momentum (fundamentals override technical exhaustion)',
        'Low volatility drift (price moves slowly past thresholds without reverting)',
      ],
      academicReferences: [
        'Wilder, J.W. (1978) \u2014 New Concepts in Technical Trading Systems',
        'Barberis, N. & Thaler, R. (2003) \u2014 A Survey of Behavioral Finance',
      ],
      checklist: [
        {
          question: 'What inefficiency does this exploit?',
          answer:
            'Mean reversion from liquidity exhaustion at price extremes',
        },
        {
          question: 'Who is the counterparty?',
          answer:
            'Aggressive buyers/sellers who have overextended, plus late-to-the-trend retail traders',
        },
        {
          question: 'Why does it persist?',
          answer:
            'Market makers structurally provide liquidity and manage inventory toward neutral \u2014 extreme RSI indicates they will push back',
        },
        {
          question: 'What kills it?',
          answer:
            'Fundamental catalysts that justify new price levels (earnings, M&A), or strong trend regimes where momentum dominates reversion',
        },
        {
          question: 'Expected decay profile?',
          answer:
            'Immediate 5\u201320 minute reversion window; signal degrades quickly as new information arrives',
        },
      ],
    },
    {
      featureName: 'Realized Volatility (30)',
      hypothesis:
        'Volatility clusters and predicts future volatility. High realized volatility signals either mean reversion (liquidity premium) or regime shift (continuation).',
      drivers: [
        {
          title: 'Volatility Clustering',
          explanation:
            'Volatility clustering is driven by information arrival clustering, liquidity withdrawal during stress, and risk management deleveraging cycles. This aligns with the ARCH/GARCH literature pioneered by Robert Engle (1982).',
          formulaLatex: String.raw`RV_t = \sqrt{\sum_{i=1}^{n} r_i^2}`,
          formulaVariables: [
            String.raw`r_i` + ' = intrabar log return',
            'n = number of bars in window (30)',
          ],
        },
        {
          title: 'Liquidity Premium Mechanism',
          explanation:
            'High volatility causes liquidity providers to demand a premium (wider spreads, less depth). This creates short-term mean reversion as prices overshoot and snap back when liquidity normalizes.',
        },
        {
          title: 'Regime Classification',
          explanation:
            'Realized volatility acts as both a regime classifier and a signal strength modulator. Volatility breakouts indicate regime shifts (continuation), while sustained high-vol periods often precede mean reversion.',
        },
      ],
      expectedDecay: [
        'Volatility forecasting is strongest at matching horizons (30-bar RV predicts next 30-bar vol)',
        'Return prediction via volatility is secondary and regime-dependent',
        'Useful primarily as a conditioning variable for other signals',
      ],
      failureRegimes: [
        'Flash crashes / liquidity vacuums (volatility spikes without predictive pattern)',
        'Earnings / macro events that create one-time vol spikes',
        'Low-vol grind markets where RV provides no differentiation',
      ],
      academicReferences: [
        'Engle, R. (1982) \u2014 Autoregressive Conditional Heteroscedasticity (ARCH)',
        'Mandelbrot, B. (1963) \u2014 The Variation of Certain Speculative Prices',
        'Andersen, T. & Bollerslev, T. (1998) \u2014 Answering the Skeptics: Realized Volatility',
      ],
      checklist: [
        {
          question: 'What inefficiency does this exploit?',
          answer:
            'Volatility persistence \u2014 markets systematically underprice future volatility during calm periods and overprice it during stress',
        },
        {
          question: 'Who is the counterparty?',
          answer:
            'Traders who assume constant volatility, and option sellers who don\'t adjust quickly enough',
        },
        {
          question: 'Why does it persist?',
          answer:
            'Structural: risk management deleveraging is mechanical, liquidity withdrawal is rational self-preservation \u2014 both create predictable volatility dynamics',
        },
        {
          question: 'What kills it?',
          answer:
            'Exogenous shocks that break the clustering pattern (black swan events), or structural regime changes',
        },
        {
          question: 'Expected decay profile?',
          answer:
            'Volatility prediction: strong at matching horizons; return prediction: weak, best used as a conditioning variable',
        },
      ],
    },
    {
      featureName: 'Volume Z-Score',
      hypothesis:
        'Abnormal volume indicates informed trading or liquidity shocks. Volume carries information about future price changes that price alone does not capture.',
      drivers: [
        {
          title: 'Informed Trading Detection',
          explanation:
            'According to Kyle\'s price impact theory, price changes are proportional to signed volume. Abnormally high volume may signal informed traders acting on private information, institutional execution waves, or broad news reactions.',
          formulaLatex: String.raw`\Delta P \propto \text{Signed Volume}`,
        },
        {
          title: 'Volume-Direction Interaction',
          explanation:
            'Volume alone predicts volatility expansion. Combined with direction, it becomes a stronger signal: high volume + continuation suggests informed flow, while high volume + reversal suggests exhaustion.',
        },
      ],
      expectedDecay: [
        'Strongest predictive power in the 5\u201330 minute window after volume spike',
        'Volume spikes from informed trading predict continuation',
        'Volume spikes from exhaustion predict reversal',
      ],
      failureRegimes: [
        'Passive index rebalancing (volume spike with no information content)',
        'ETF arbitrage flows (mechanical, not directional)',
        'Closing auction distortions (end-of-day volume artifacts)',
        'Stock-split or corporate action days',
      ],
      academicReferences: [
        'Kyle, A.S. (1985) \u2014 Continuous Auctions and Insider Trading',
        'Blume, L., Easley, D. & O\'Hara, M. (1994) \u2014 Market Statistics and Technical Analysis: The Role of Volume',
        'Karpoff, J. (1987) \u2014 The Relation Between Price Changes and Trading Volume',
      ],
      checklist: [
        {
          question: 'What inefficiency does this exploit?',
          answer:
            'Information leakage through abnormal volume before price fully adjusts',
        },
        {
          question: 'Who is the counterparty?',
          answer:
            'Uninformed traders who don\'t monitor volume patterns, and passive participants',
        },
        {
          question: 'Why does it persist?',
          answer:
            'Informed traders must transact to profit from their information \u2014 volume is an unavoidable footprint of this activity',
        },
        {
          question: 'What kills it?',
          answer:
            'Mechanical/non-informational volume (index rebalancing, ETF arb, closing auctions) that dilutes the signal-to-noise ratio',
        },
        {
          question: 'Expected decay profile?',
          answer:
            '5\u201330 minutes for directional prediction; volume\'s volatility-forecasting power persists longer',
        },
      ],
    },
    {
      featureName: 'MACD Signal',
      hypothesis:
        'MACD captures medium-term trend acceleration by filtering micro-noise from the momentum signal. It represents the convergence/divergence between short-term and medium-term order imbalance.',
      drivers: [
        {
          title: 'Filtered Momentum',
          explanation:
            'MACD is the difference between a fast EMA (12) and slow EMA (26), further smoothed by a signal line EMA (9). This dual-timeframe structure captures trend strength persistence while filtering out micro-noise.',
          formulaLatex: String.raw`\text{MACD} = \text{EMA}_{12} - \text{EMA}_{26}`,
        },
        {
          title: 'Institutional Flow Detection',
          explanation:
            'At intraday scale, MACD crossovers often coincide with shifts in order flow direction. When institutional participation dominates and trend-following funds are active, MACD reliably tracks the medium-term trend.',
        },
      ],
      expectedDecay: [
        'Works best over 15\u201360 minute horizons (the "sweet spot" between its fast and slow windows)',
        'Less effective at very short horizons (where raw momentum is stronger)',
        'Signal persists longer than raw momentum due to smoothing',
      ],
      failureRegimes: [
        'Range-bound markets (generates frequent whipsaw signals)',
        'Mean-reversion dominated regimes (liquidity providers dominate trend followers)',
        'Choppy, low-conviction markets with no clear institutional direction',
      ],
      academicReferences: [
        'Appel, G. (1979) \u2014 The Moving Average Convergence Divergence Method',
        'Brock, W., Lakonishok, J. & LeBaron, B. (1992) \u2014 Simple Technical Trading Rules and the Stochastic Properties of Stock Returns',
      ],
      checklist: [
        {
          question: 'What inefficiency does this exploit?',
          answer:
            'Trend persistence from institutional order flow operating across multiple timeframes',
        },
        {
          question: 'Who is the counterparty?',
          answer:
            'Counter-trend traders and mean-reversion strategies that fade moves too early',
        },
        {
          question: 'Why does it persist?',
          answer:
            'Institutional allocation decisions and trend-following mandates create multi-timeframe momentum that single-horizon measures miss',
        },
        {
          question: 'What kills it?',
          answer:
            'Range-bound markets where MACD oscillates around zero, and sudden reversals from macro catalysts',
        },
        {
          question: 'Expected decay profile?',
          answer:
            'Peak effectiveness at 15\u201360 minutes; persists longer than raw momentum; degrades in choppy regimes',
        },
      ],
    },
  ];

  justificationCriteria: JustificationCriterion[] = [
    {
      name: 'Statistical Strength',
      description:
        'Mean IC magnitude, t-statistic significance, and quantile monotonicity. Higher IC with strong t-stat earns higher scores.',
    },
    {
      name: 'Stability',
      description:
        'Consistency of IC across time periods (ICIR). A feature with steady IC = 0.04 scores higher than volatile IC = 0.08.',
    },
    {
      name: 'Economic Clarity',
      description:
        'Clear identification of the exploited inefficiency, counterparty, and persistence mechanism. Hand-wavy explanations score low.',
    },
    {
      name: 'Regime Robustness',
      description:
        'Performance across different market conditions (trending, mean-reverting, high/low volatility). Features that only work in one regime score lower.',
    },
  ];

  deepInsight = {
    statistical:
      'Statistical validation answers: "Is this real in data?"',
    economic:
      'Economic interpretation answers: "Will this survive live trading?"',
    conclusion:
      'Funds shut down signals not because IC disappears \u2014 but because the economic rationale collapses. A feature with IC = 0.015 but strong economic logic is better than IC = 0.03 with no story.',
  };
}
