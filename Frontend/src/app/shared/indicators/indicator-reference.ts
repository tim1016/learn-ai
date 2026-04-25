/**
 * Indicator Reference — single source of truth for UI-facing indicator metadata.
 *
 * Layering note (per CLAUDE.md rule 5: "Python owns all math"):
 *   - Calculation truth lives in `PythonDataService` (pandas-ta).
 *   - Param contract truth (names, types, min/max, defaults) lives in
 *     `PythonDataService/app/services/dataset_service.py:INDICATOR_CONFIGS`.
 *   - This file is the **UI documentation** truth — names, formulas,
 *     narrative, recommended timeframes, caveats, related indicators.
 *
 * The `params` array below mirrors INDICATOR_CONFIGS for offline rendering of
 * defaults and bounds. Keep them in sync when adding/removing parameters.
 */

export type IndicatorPanelType = 'overlay' | 'sub-panel';
export type IndicatorCategory = 'trend' | 'momentum' | 'volatility' | 'volume';
export type IndicatorParamType = 'int' | 'float';

export interface IndicatorParam {
  name: string;
  type: IndicatorParamType;
  default: number;
  min: number;
  max: number;
  /** Short, user-facing description for the param. */
  description: string;
}

export interface IndicatorReferenceEntry {
  /** Internal key matching pandas-ta + backend (e.g. 'ema'). */
  key: string;
  /** Human-readable display name, e.g. "Exponential Moving Average (EMA)". */
  displayName: string;
  /** Trading category — used for chip color and category grouping. */
  category: IndicatorCategory;
  /** Where this indicator renders on a chart. */
  panelType: IndicatorPanelType;

  // ── Math ──────────────────────────────────────────────────
  formulaLatex: string;
  library: string;
  outputColumns: string[];
  /** One-line summary of default params, e.g. "length = 14". */
  defaultParams: string;
  /** Param contract — mirrors PythonDataService INDICATOR_CONFIGS. */
  params: IndicatorParam[];

  // ── Narrative ─────────────────────────────────────────────
  description: string;
  quickWhy: string;
  quickAnalogy: string;
  quickImpact: string;

  // ── Usage guidance ────────────────────────────────────────
  interpretation: string[];
  recommendedTimeframes: string;
  dataNotes: string[];
  timeframeBehavior: string;
  relatedIndicators: string[];

  // ── Provenance / learning ─────────────────────────────────
  professionalRef: string;
  checkQuestion?: string;
  checkAnswer?: string;
}

// Param contracts (pulled from PythonDataService INDICATOR_CONFIGS).
const P_LENGTH_500 = (def: number, desc = 'Lookback period'): IndicatorParam[] => [
  { name: 'length', type: 'int', default: def, min: 1, max: 500, description: desc },
];
const P_LENGTH_100 = (def: number, desc = 'Lookback period'): IndicatorParam[] => [
  { name: 'length', type: 'int', default: def, min: 1, max: 100, description: desc },
];

export const INDICATOR_REFERENCE: Record<string, IndicatorReferenceEntry> = {
  // ═══════════════════════════════════════════════════════════
  //  OVERLAY INDICATORS — TREND
  // ═══════════════════════════════════════════════════════════
  ema: {
    key: 'ema',
    displayName: 'Exponential Moving Average (EMA)',
    category: 'trend',
    panelType: 'overlay',
    formulaLatex: '\\text{EMA}_t = \\alpha \\cdot C_t + (1 - \\alpha) \\cdot \\text{EMA}_{t-1}, \\quad \\alpha = \\frac{2}{n + 1}',
    library: 'pandas-ta (ta.ema)',
    outputColumns: ['ema_{length}'],
    defaultParams: 'length = 5, 10, 20, 30, 40, 50, 100, 200',
    params: P_LENGTH_500(10, 'Lookback period. Shorter = faster reaction (e.g. 10), longer = smoother trend (e.g. 200).'),
    description: 'Gives more weight to recent prices. Default setup calculates 8 EMAs (5, 10, 20, 30, 40, 50, 100, 200) for multi-timeframe analysis. Each EMA instance adds one column.',
    quickWhy: 'Find the "current heartbeat" of a stock. Reacts faster to recent news than a standard average.',
    quickAnalogy: 'Driving a car and looking mostly at the 50 feet behind you — it tells you about the turn you just made, not the road from five miles ago.',
    quickImpact: 'Spots trend changes early. If price stays above this line, the trend\'s "pulse" is healthy.',
    interpretation: [
      'Price above EMA — bullish bias',
      'Price below EMA — bearish bias',
      'EMA crossovers (fast vs slow) generate trend signals',
      'Multiple EMAs form a "ribbon" — fanning = strong trend, converging = consolidation',
    ],
    recommendedTimeframes: '1m–1D+ (all timeframes)',
    dataNotes: [
      'Requires `length` warmup bars before producing valid values',
      'Sensitive to missing candles — EMA drifts if gaps exist',
    ],
    timeframeBehavior: 'Hyper-sensitive on 1m/5m charts. Due to the 15-minute delay, a "cross" on the 1m chart represents a move that finished 15 minutes ago. Most reliable on Daily/Weekly charts.',
    relatedIndicators: ['sma', 'dema', 'tema'],
    professionalRef: 'Robert Goodell Brown (1956) and P.N. Haurlan (1960s)',
    checkQuestion: 'If a stock suddenly crashes today, which line will drop faster: the SMA or the EMA?',
    checkAnswer: 'The EMA',
  },

  sma: {
    key: 'sma',
    displayName: 'Simple Moving Average (SMA)',
    category: 'trend',
    panelType: 'overlay',
    formulaLatex: '\\text{SMA}_t(n)=\\frac{1}{n}\\sum_{i=0}^{n-1}C_{t-i}',
    library: 'pandas-ta (ta.sma)',
    outputColumns: ['sma_{length}'],
    defaultParams: 'length = 20',
    params: P_LENGTH_500(20, 'Number of bars averaged. Common values: 20 (short-term), 50 (medium), 200 (long-term institutional level).'),
    description: 'The arithmetic mean of the last n closing prices. One of the most widely used trend filters.',
    quickWhy: 'Find the "big picture" average. Treats every day in its window as equally important.',
    quickAnalogy: 'The average temperature of a city. One heatwave doesn\'t change the climate. SMA shows you the "climate" of the stock.',
    quickImpact: 'Acts as a major floor or ceiling that big banks and institutions watch closely.',
    interpretation: [
      'Price above SMA → bullish bias',
      'Price below SMA → bearish bias',
      'SMA slope indicates trend strength',
      'SMA crossovers (fast vs slow) generate trend signals',
    ],
    recommendedTimeframes: '1h–1D+ (excellent long-term trend filter)',
    dataNotes: [
      'Requires `length` warmup bars',
      'Sensitive to missing candles / gaps',
    ],
    timeframeBehavior: 'Inherently lagging. 15-minute delay is irrelevant for the 200-day SMA but makes the 5-period SMA useless for real-time scalping.',
    relatedIndicators: ['ema', 'wma', 'dema'],
    professionalRef: 'Richard Donchian (1930s) and John J. Murphy',
    checkQuestion: 'For a one-year trend, would you use a 5-day SMA or a 200-day SMA?',
    checkAnswer: '200-day SMA',
  },

  dema: {
    key: 'dema',
    displayName: 'Double Exponential Moving Average (DEMA)',
    category: 'trend',
    panelType: 'overlay',
    formulaLatex: '\\text{DEMA}=2\\cdot EMA(n)-EMA(EMA(n))',
    library: 'pandas-ta (ta.dema)',
    outputColumns: ['dema_{length}'],
    defaultParams: 'length = 20',
    params: P_LENGTH_500(10, 'Lookback period. Lower = more responsive but noisier.'),
    description: 'Reduces lag compared to EMA by subtracting a smoothed EMA of EMA.',
    quickWhy: 'For speed. Removes the "lag" that makes other averages feel slow and outdated.',
    quickAnalogy: 'If a standard average is a heavy bus, DEMA is a motorcycle that whips around corners instantly.',
    quickImpact: 'Entry and exit signals arrive much earlier than with traditional moving averages.',
    interpretation: [
      'Same usage as EMA (trend following)',
      'More responsive than SMA/EMA',
      'Helps reduce late entries in trends',
    ],
    recommendedTimeframes: '5m–1D (excellent on 4h–1D)',
    dataNotes: [
      'Warmup is larger than EMA due to nested smoothing',
    ],
    timeframeBehavior: 'High responsiveness but prone to "false starts" on 1m/5m charts because of the 15-minute feed delay.',
    relatedIndicators: ['ema', 'tema', 'zlma'],
    professionalRef: 'Patrick Mulloy (1994)',
    checkQuestion: 'Why would a trader use DEMA instead of SMA?',
    checkAnswer: 'To catch reversals faster',
  },

  tema: {
    key: 'tema',
    displayName: 'Triple Exponential Moving Average (TEMA)',
    category: 'trend',
    panelType: 'overlay',
    formulaLatex: '\\text{TEMA}=3\\cdot EMA(n)-3\\cdot EMA(EMA(n))+EMA(EMA(EMA(n)))',
    library: 'pandas-ta (ta.tema)',
    outputColumns: ['tema_{length}'],
    defaultParams: 'length = 20',
    params: P_LENGTH_500(10, 'Lookback period. Keep above 8 on lower timeframes to avoid excessive noise.'),
    description: 'Further reduces lag compared to DEMA while still smoothing price.',
    quickWhy: 'Ultra-fast confirmation. The most sensitive version of DEMA.',
    quickAnalogy: 'A fighter jet following the price so closely it almost touches the candles.',
    quickImpact: 'Best for hyper-volatile markets where direction changes in the blink of an eye.',
    interpretation: [
      'Trend-following moving average',
      'Better for catching early reversals',
      'Can be slightly more reactive / noisier than DEMA',
    ],
    recommendedTimeframes: '15m–1D+ (very strong on daily)',
    dataNotes: [
      'Requires 3x warmup of base EMA length due to triple nesting',
    ],
    timeframeBehavior: 'On scalping timeframes, the 15-minute delay makes TEMA dangerous; the "spike" you see is already 15 minutes old.',
    relatedIndicators: ['ema', 'dema', 'zlma'],
    professionalRef: 'Patrick Mulloy (1994)',
    checkQuestion: 'If the TEMA starts pointing down, does that mean the trend is definitely over?',
    checkAnswer: 'Not necessarily — it is so sensitive it might just be a small hiccup',
  },

  wma: {
    key: 'wma',
    displayName: 'Weighted Moving Average (WMA)',
    category: 'trend',
    panelType: 'overlay',
    formulaLatex: '\\text{WMA}_t(n)=\\frac{\\sum_{i=0}^{n-1}(n-i)\\cdot C_{t-i}}{\\sum_{k=1}^{n}k}',
    library: 'pandas-ta (ta.wma)',
    outputColumns: ['wma_{length}'],
    defaultParams: 'length = 20',
    params: P_LENGTH_500(10, 'Window size. Recent bars get linearly higher weight.'),
    description: 'Applies larger weights to recent bars, making it more responsive than SMA.',
    quickWhy: 'Ensures today\'s news is more important than last week\'s.',
    quickAnalogy: 'Like a "Trending Now" list — what people said 5 minutes ago matters more than yesterday.',
    quickImpact: 'Stays closer to the price than an SMA, making it a better tracker for active traders.',
    interpretation: [
      'Faster trend identification than SMA',
      'Useful for short-term trend tracking',
      'Works well for crossover systems',
    ],
    recommendedTimeframes: '1m–4h (strong on intraday)',
    dataNotes: ['Requires `length` warmup bars'],
    timeframeBehavior: 'Heavy impact of 15-minute delay since the highest weight is on a candle that is actually 15 minutes old.',
    relatedIndicators: ['sma', 'ema', 'hma'],
    professionalRef: 'Popularized by Steven Achelis',
  },

  hma: {
    key: 'hma',
    displayName: 'Hull Moving Average (HMA)',
    category: 'trend',
    panelType: 'overlay',
    formulaLatex: '\\text{HMA}(n)=WMA(2\\cdot WMA(n/2)-WMA(n),\\sqrt{n})',
    library: 'pandas-ta (ta.hma)',
    outputColumns: ['hma_{length}'],
    defaultParams: 'length = 55',
    params: P_LENGTH_500(9, 'Lookback period. Internally uses WMA(n/2) and WMA(n). Higher = smoother.'),
    description: 'Reduces lag while keeping smoothness, often producing visually clean trend lines.',
    quickWhy: 'Be smooth and fast at the same time — usually you have to pick one.',
    quickAnalogy: 'A silk ribbon flowing in the wind. It follows the wind (price) perfectly without any sharp, jagged edges.',
    quickImpact: 'Clear "Go / No-Go" signals; the line changes color when the trend shifts.',
    interpretation: [
      'Excellent trend filter',
      'Often used as "signal line" for trend changes',
      'Commonly used in algorithmic trend systems',
    ],
    recommendedTimeframes: '4h–1D (excellent); can be choppy on 5m–1h',
    dataNotes: ['Warmup depends on sqrt(length) inner WMA'],
    timeframeBehavior: '"Beautiful" but deceptive with 15-minute delays. It may look like a smooth uptrend while a crash is happening in the "blind spot."',
    relatedIndicators: ['wma', 'ema', 'kama'],
    professionalRef: 'Alan Hull (2005)',
  },

  kama: {
    key: 'kama',
    displayName: 'Kaufman Adaptive Moving Average (KAMA)',
    category: 'trend',
    panelType: 'overlay',
    formulaLatex: 'ER=\\frac{|C_t-C_{t-n}|}{\\sum_{i=1}^{n}|C_{t-i}-C_{t-i-1}|}, \\quad SC=(ER\\cdot(fastSC-slowSC)+slowSC)^2, \\quad KAMA_t = KAMA_{t-1} + SC\\cdot(C_t-KAMA_{t-1})',
    library: 'pandas-ta (ta.kama)',
    outputColumns: ['kama_{length}'],
    defaultParams: 'length = 10',
    params: P_LENGTH_500(10, 'Efficiency ratio lookback. Controls how far back to measure "signal vs. noise."'),
    description: 'Adapts smoothing based on market efficiency: smooth during choppy markets, fast during trending markets.',
    quickWhy: 'An average with its own "intelligence." Speeds up in trends, slows down during chop.',
    quickAnalogy: 'Adaptive cruise control that speeds up on the highway but slows down in heavy city traffic.',
    quickImpact: 'Helps you avoid getting fooled by small, meaningless price jumps.',
    interpretation: [
      'Excellent adaptive trend line',
      'Reduces false signals during sideways conditions',
      'Works well as a regime detector',
    ],
    recommendedTimeframes: 'All timeframes (especially good for noisy intraday)',
    dataNotes: ['Depends heavily on consistent candle spacing'],
    timeframeBehavior: 'Excellent for daily charts. The 15-minute delay is less problematic as it filters for "efficiency" over time.',
    relatedIndicators: ['ema', 'alma', 'zlma'],
    professionalRef: 'Perry Kaufman (1995)',
  },

  zlma: {
    key: 'zlma',
    displayName: 'Zero Lag Moving Average (ZLMA)',
    category: 'trend',
    panelType: 'overlay',
    formulaLatex: 'ZLMA = EMA(2C_t - C_{t-lag}), \\quad lag=\\frac{n-1}{2}',
    library: 'pandas-ta (ta.zlma)',
    outputColumns: ['zlma_{length}'],
    defaultParams: 'length = 20',
    params: P_LENGTH_500(10, 'Base EMA period. The lag compensation uses half this value.'),
    description: 'Attempts to reduce lag by compensating for the inherent delay in moving averages.',
    quickWhy: '"Predict" the price by removing the delay found in all other averages.',
    quickAnalogy: 'A forecast telling you where the storm will be in 10 minutes, rather than where it was 10 minutes ago.',
    quickImpact: 'Crosses the price earlier than any other average, giving you a head start.',
    interpretation: [
      'Faster trend signals than standard EMA',
      'Higher false positives in sideways markets',
      'Useful for short-term trend detection',
    ],
    recommendedTimeframes: '1m–1h (strong on intraday)',
    dataNotes: ['Requires `length` warmup bars'],
    timeframeBehavior: 'Hyper-reactive. Very risky with a 15-minute delay because the "projection" is based on stale momentum data.',
    relatedIndicators: ['ema', 'dema', 'tema'],
    professionalRef: 'John Ehlers and Ric Way',
  },

  rma: {
    key: 'rma',
    displayName: 'Wilder\'s Moving Average (RMA)',
    category: 'trend',
    panelType: 'overlay',
    formulaLatex: 'RMA_t = \\frac{(n-1)\\cdot RMA_{t-1}+C_t}{n}',
    library: 'pandas-ta (ta.rma)',
    outputColumns: ['rma_{length}'],
    defaultParams: 'length = 14',
    params: P_LENGTH_500(10, 'Smoothing period. Uses alpha = 1/n, making it slower than a standard EMA of the same length.'),
    description: 'Wilder\'s smoothing method, used internally by ATR, RSI, and ADX.',
    quickWhy: 'The stable foundation used to calculate other famous tools like RSI.',
    quickAnalogy: 'The foundation of a house. You don\'t see it, but everything else relies on it being stable and slow-moving.',
    quickImpact: 'Provides a very smooth, long-term view of "Fair Value."',
    interpretation: [
      'A smoothing baseline for volatility and momentum indicators',
      'Slightly slower than EMA with equivalent period',
    ],
    recommendedTimeframes: 'All timeframes',
    dataNotes: ['Requires `length` warmup bars'],
    timeframeBehavior: 'Minimal impact from 15-minute delay due to heavy smoothing.',
    relatedIndicators: ['ema', 'atr', 'rsi'],
    professionalRef: 'J. Welles Wilder Jr. (1978)',
  },

  alma: {
    key: 'alma',
    displayName: 'Arnaud Legoux Moving Average (ALMA)',
    category: 'trend',
    panelType: 'overlay',
    formulaLatex: '\\text{ALMA}(n) = \\sum_{i=0}^{n-1} w_i \\cdot C_{t-i}, \\quad w_i = e^{-\\frac{(i - m)^2}{2s^2}}',
    library: 'pandas-ta (ta.alma)',
    outputColumns: ['alma_{length}'],
    defaultParams: 'length = 20, offset = 0.85, sigma = 6',
    params: [
      { name: 'length', type: 'int', default: 10, min: 1, max: 500, description: 'Window size for the Gaussian-weighted average.' },
    ],
    description: 'Uses a Gaussian distribution weighting function for smoothing. Very smooth and low-lag.',
    quickWhy: 'The smoothest line without the "overshoot" common in fast averages.',
    quickAnalogy: 'A cinematographer using a steadicam — even if the cameraman is running, the footage looks perfectly smooth.',
    quickImpact: 'The most trustworthy line for identifying a steady trend.',
    interpretation: [
      'Very smooth and low-lag moving average',
      'Useful for signal extraction in medium-term trend systems',
      'Offset controls responsiveness, sigma controls smoothness',
    ],
    recommendedTimeframes: '15m–1D+ (excellent on daily)',
    dataNotes: ['Requires `length` warmup bars'],
    timeframeBehavior: 'Highly reliable for 15-minute charts; filters delayed noise better than EMAs.',
    relatedIndicators: ['ema', 'kama', 'hma'],
    professionalRef: 'Arnaud Legoux and Dimitrios Kouzis-Loukas (2009)',
  },

  supertrend: {
    key: 'supertrend',
    displayName: 'Supertrend',
    category: 'trend',
    panelType: 'overlay',
    formulaLatex: '\\text{Up} = \\frac{H+L}{2} + m \\cdot \\text{ATR}(n), \\quad \\text{Down} = \\frac{H+L}{2} - m \\cdot \\text{ATR}(n)',
    library: 'pandas-ta (ta.supertrend)',
    outputColumns: ['supert_{n}_{m}', 'supertd_{n}_{m}', 'supertl_{n}_{m}', 'superts_{n}_{m}'],
    defaultParams: 'length = 10, multiplier = 3.0',
    params: [
      { name: 'length', type: 'int', default: 10, min: 1, max: 100, description: 'ATR lookback period. Higher = slower, fewer flips.' },
      { name: 'multiplier', type: 'float', default: 3.0, min: 0.5, max: 10.0, description: 'ATR multiplier for band distance. Higher = wider bands, fewer whipsaws.' },
    ],
    description: 'Trend-following indicator based on ATR. Flips between support (uptrend) and resistance (downtrend). Outputs 4 columns: trend value, direction (1/-1), long (support), short (resistance). ATR uses RMA smoothing.',
    quickWhy: 'A simple "Stoplight" for trades — exactly when to be "In" or "Out."',
    quickAnalogy: 'A trail marker on a hike. Green path = safe. Red = you\'ve gone off-trail.',
    quickImpact: 'Automatically provides a level for your stop-loss.',
    interpretation: [
      'Direction = 1 → uptrend (price above support line)',
      'Direction = -1 → downtrend (price below resistance line)',
      'Trend flips are common entry/exit signals',
      'Higher multiplier = fewer whipsaws but later entries',
    ],
    recommendedTimeframes: '15m–1D (excellent on 1h–4h)',
    dataNotes: [
      'First few bars may show NaN while ATR warms up',
      'Direction flips can produce gaps in the trend line',
    ],
    timeframeBehavior: 'Effective on 15m and Daily charts. The 15-minute delay makes signals reactive; price may have already moved 1% before the color flips.',
    relatedIndicators: ['atr', 'psar', 'ema'],
    professionalRef: 'Olivier Seban',
  },

  psar: {
    key: 'psar',
    displayName: 'Parabolic SAR',
    category: 'trend',
    panelType: 'overlay',
    formulaLatex: '\\text{SAR}_{t+1} = \\text{SAR}_t + \\text{AF} \\cdot (\\text{EP} - \\text{SAR}_t)',
    library: 'pandas-ta (ta.psar)',
    outputColumns: ['psarl_{af}_{max}', 'psars_{af}_{max}', 'psaraf_{af}_{max}', 'psarr_{af}_{max}'],
    defaultParams: 'af0 = 0.02, af = 0.02, max_af = 0.2',
    params: [
      { name: 'af0', type: 'float', default: 0.02, min: 0.001, max: 0.1, description: 'Initial acceleration factor. Controls how quickly the SAR tightens.' },
      { name: 'af', type: 'float', default: 0.02, min: 0.001, max: 0.1, description: 'Acceleration factor step increment.' },
      { name: 'max_af', type: 'float', default: 0.2, min: 0.05, max: 1.0, description: 'Maximum acceleration factor cap.' },
    ],
    description: 'Trailing stop-and-reverse system. Dots above price = downtrend, below = uptrend. AF accelerates toward the extreme point (EP).',
    quickWhy: 'Your "Trailing Security Guard." Follows price up and tells you the second the trend is over.',
    quickAnalogy: 'A dog on a leash that gets shorter as you get closer to home. Eventually you hit the end and must stop.',
    quickImpact: 'Tells you exactly where to exit a trade to protect your profits.',
    interpretation: [
      'Dots below price → uptrend (use as trailing stop)',
      'Dots above price → downtrend (use as trailing stop)',
      'Reversal when price crosses SAR dots',
      'Best in trending markets — frequent whipsaws in sideways',
    ],
    recommendedTimeframes: '15m–1D (works best in trending conditions)',
    dataNotes: [
      'First bar may be NaN',
      'Sensitive to gap openings',
    ],
    timeframeBehavior: 'Dangerous with 15-minute delay; the real price may hit your stop level 15 minutes before the app shows it.',
    relatedIndicators: ['supertrend', 'atr', 'adx'],
    professionalRef: 'J. Welles Wilder Jr. (1978)',
  },

  donchian: {
    key: 'donchian',
    displayName: 'Donchian Channel',
    category: 'trend',
    panelType: 'overlay',
    formulaLatex: '\\text{Upper} = \\max(H_{t-n+1},...,H_t), \\quad \\text{Lower} = \\min(L_{t-n+1},...,L_t), \\quad \\text{Mid} = \\frac{Upper + Lower}{2}',
    library: 'pandas-ta (ta.donchian)',
    outputColumns: ['dcl_{n}', 'dcm_{n}', 'dcu_{n}'],
    defaultParams: 'length = 20',
    params: [
      { name: 'lower_length', type: 'int', default: 20, min: 1, max: 200, description: 'Lookback for the lowest low.' },
      { name: 'upper_length', type: 'int', default: 20, min: 1, max: 200, description: 'Lookback for the highest high.' },
    ],
    description: 'Represents the highest high and lowest low over a rolling window. Used in Turtle Trading systems.',
    quickWhy: 'Shows the "Ultimate High" and "Ultimate Low" of the last few days.',
    quickAnalogy: 'A record book showing the all-time high score for the current "game" (period).',
    quickImpact: 'Helps find breakouts — when price breaks a record high, it often keeps going.',
    interpretation: [
      'Breakout above upper channel → trend continuation / new trend',
      'Breakout below lower channel → bearish breakout',
      'Common in Turtle Trading systems',
    ],
    recommendedTimeframes: '1h–1W (excellent breakout indicator on 4h–1D)',
    dataNotes: ['Requires `length` warmup bars'],
    timeframeBehavior: 'Very robust. 15-minute delay is less of an issue because a 20-day high is unlikely to change every minute.',
    relatedIndicators: ['kc', 'bbands', 'atr'],
    professionalRef: 'Richard Donchian',
  },

  // ═══════════════════════════════════════════════════════════
  //  OVERLAY INDICATORS — VOLATILITY
  // ═══════════════════════════════════════════════════════════
  bbands: {
    key: 'bbands',
    displayName: 'Bollinger Bands',
    category: 'volatility',
    panelType: 'overlay',
    formulaLatex: '\\text{Mid} = \\text{SMA}(C, n), \\quad \\text{Upper} = \\text{Mid} + k\\sigma_n, \\quad \\text{Lower} = \\text{Mid} - k\\sigma_n',
    library: 'pandas-ta (ta.bbands)',
    outputColumns: ['bbl_{n}_{k}', 'bbm_{n}_{k}', 'bbu_{n}_{k}', 'bbb_{n}_{k}', 'bbp_{n}_{k}'],
    defaultParams: 'length = 20, std = 2.0',
    params: [
      { name: 'length', type: 'int', default: 20, min: 1, max: 200, description: 'SMA lookback for the middle band.' },
      { name: 'std', type: 'float', default: 2.0, min: 0.1, max: 5.0, description: 'Number of standard deviations for the upper/lower bands. Higher = wider bands.' },
    ],
    description: 'Measures volatility with bands k standard deviations around an SMA. Outputs 5 columns: lower band, mid (basis), upper band, bandwidth, and %B (percent B).',
    quickWhy: 'See the market\'s "Mood Swing" — how far price is stretching from normal.',
    quickAnalogy: 'A rubber band. The further you stretch it, the more likely it is to snap back to the center.',
    quickImpact: 'When the bands get tight (a "Squeeze"), a massive move is usually coming soon.',
    interpretation: [
      'Price touching upper band → potential overbought / strong uptrend',
      'Price touching lower band → potential oversold / strong downtrend',
      'Band squeeze (narrow bandwidth) → volatility contraction, breakout pending',
      '%B > 1 = above upper band, %B < 0 = below lower band',
    ],
    recommendedTimeframes: '5m–1D (excellent on 1h–4h)',
    dataNotes: [
      'Requires `length` warmup bars',
      'Bandwidth and %B are useful for quantitative systems',
    ],
    timeframeBehavior: '15-minute delay is critical; if a breakout occurs in real-time, the app won\'t show it for 15 minutes.',
    relatedIndicators: ['kc', 'squeeze', 'sma'],
    professionalRef: 'John Bollinger (1983)',
    checkQuestion: 'If price hits the top band and stays there, is the market "too expensive" or "very strong"?',
    checkAnswer: 'Usually very strong momentum',
  },

  kc: {
    key: 'kc',
    displayName: 'Keltner Channel',
    category: 'volatility',
    panelType: 'overlay',
    formulaLatex: '\\text{Mid} = EMA(C, n), \\quad \\text{Upper} = Mid + k\\cdot ATR(n), \\quad \\text{Lower} = Mid - k\\cdot ATR(n)',
    library: 'pandas-ta (ta.kc)',
    outputColumns: ['kcl_{n}_{k}', 'kcb_{n}_{k}', 'kcu_{n}_{k}'],
    defaultParams: 'length = 20, scalar = 1.5',
    params: [
      { name: 'length', type: 'int', default: 20, min: 1, max: 200, description: 'EMA lookback for the middle line.' },
      { name: 'scalar', type: 'float', default: 1.5, min: 0.5, max: 5.0, description: 'ATR multiplier for the channel width.' },
    ],
    description: 'Volatility bands based on ATR, often smoother than Bollinger Bands.',
    quickWhy: 'A calmer version of Bollinger Bands — a tunnel that doesn\'t overreact to every spike.',
    quickAnalogy: 'A riverbed. Price stays within the banks most of the time. If it overflows, something big is happening.',
    quickImpact: 'Best for finding "Bargain Entries" when price dips to the middle during an uptrend.',
    interpretation: [
      'Break above upper band → strong bullish expansion',
      'Inside channel → neutral consolidation',
      'Channel slope indicates trend direction',
    ],
    recommendedTimeframes: '5m–1D (excellent trend channel on 4h–1D)',
    dataNotes: ['Requires `length` warmup for both EMA and ATR'],
    timeframeBehavior: 'Best on 15m charts. Delay makes bands appear "tighter" than reality during spikes.',
    relatedIndicators: ['bbands', 'atr', 'squeeze'],
    professionalRef: 'Chester Keltner (1960); revised by Linda Raschke',
  },

  // ═══════════════════════════════════════════════════════════
  //  OVERLAY INDICATORS — VOLUME
  // ═══════════════════════════════════════════════════════════
  vwap: {
    key: 'vwap',
    displayName: 'Volume Weighted Average Price (VWAP)',
    category: 'volume',
    panelType: 'overlay',
    formulaLatex: 'VWAP = \\frac{\\sum (TP \\cdot V)}{\\sum V}, \\quad TP = \\frac{H+L+C}{3}',
    library: 'pandas-ta (ta.vwap)',
    outputColumns: ['vwap'],
    defaultParams: 'Session-based reset (implicit)',
    params: [],
    description: 'Represents the "fair value" price weighted by volume. Must reset at session boundary (daily).',
    quickWhy: 'Find the "True Cost" today by combining price and how many people were buying.',
    quickAnalogy: 'If 1 person buys coffee for $10 and 100 people buy it for $2, the "real" average isn\'t $6 — it\'s $2.',
    quickImpact: '"Big Money" uses this. Buying below this line is usually a "good deal" for the day.',
    interpretation: [
      'Price above VWAP → bullish intraday bias',
      'Price below VWAP → bearish intraday bias',
      'Common mean-reversion anchor for institutional traders',
    ],
    recommendedTimeframes: 'Intraday only (1m–30m best). Not meaningful for multi-day unless anchored.',
    dataNotes: [
      'Must reset at session boundary (daily)',
      'Very sensitive to missing volume candles',
      'Volume-dependent — unreliable with zero-volume bars',
      'Behaves differently in extended hours vs RTH',
    ],
    timeframeBehavior: 'Only valid for intraday. The 15-minute delay is a severe handicap as it ignores the most recent high-volume action.',
    relatedIndicators: ['ad', 'cmf', 'mfi'],
    professionalRef: 'Berkowitz, Logue, and Noser (1988)',
  },

  // ═══════════════════════════════════════════════════════════
  //  SUB-PANEL — MOMENTUM
  // ═══════════════════════════════════════════════════════════
  macd: {
    key: 'macd',
    displayName: 'MACD (Moving Average Convergence Divergence)',
    category: 'momentum',
    panelType: 'sub-panel',
    formulaLatex: '\\text{MACD} = \\text{EMA}(C, f) - \\text{EMA}(C, s), \\quad \\text{Signal} = \\text{EMA}(\\text{MACD}, g), \\quad \\text{Hist} = \\text{MACD} - \\text{Signal}',
    library: 'pandas-ta (ta.macd)',
    outputColumns: ['macd_{f}_{s}_{g}', 'macdh_{f}_{s}_{g}', 'macds_{f}_{s}_{g}'],
    defaultParams: 'fast = 12, slow = 26, signal = 9',
    params: [
      { name: 'fast', type: 'int', default: 12, min: 1, max: 100, description: 'Fast EMA period. Shorter = more sensitive.' },
      { name: 'slow', type: 'int', default: 26, min: 1, max: 200, description: 'Slow EMA period. The MACD line = fast EMA minus slow EMA.' },
      { name: 'signal', type: 'int', default: 9, min: 1, max: 50, description: 'Signal line EMA period. Smooths the MACD line for crossover detection.' },
    ],
    description: 'Captures the relationship between two EMAs. Outputs 3 columns: MACD line, histogram, and signal line. Rising histogram = strengthening momentum.',
    quickWhy: 'Feel the "momentum" behind a move.',
    quickAnalogy: 'A snowball rolling down a hill. MACD tells you if it\'s getting bigger and faster or starting to melt.',
    quickImpact: 'When the two lines cross, it\'s like a "Starting Pistol" for a new move.',
    interpretation: [
      'MACD crossing above signal → bullish',
      'MACD crossing below signal → bearish',
      'Histogram rising → momentum increasing',
      'Divergence between price and MACD → potential reversal',
    ],
    recommendedTimeframes: '15m–1D (excellent on 1h–4h)',
    dataNotes: ['Requires `slow` warmup bars before producing valid values'],
    timeframeBehavior: 'Delayed Histogram can show "Momentum Loss" while the real market is exploding.',
    relatedIndicators: ['ema', 'rsi', 'tsi'],
    professionalRef: 'Gerald Appel (1970s)',
    checkQuestion: 'If MACD bars are shrinking while price is rising, is the move getting stronger or weaker?',
    checkAnswer: 'Weaker — this is a warning',
  },

  rsi: {
    key: 'rsi',
    displayName: 'Relative Strength Index (RSI)',
    category: 'momentum',
    panelType: 'sub-panel',
    formulaLatex: '\\text{RSI} = 100 - \\frac{100}{1 + \\frac{\\text{AvgGain}(n)}{\\text{AvgLoss}(n)}}',
    library: 'pandas-ta (ta.rsi, mamode="rma")',
    outputColumns: ['rsi_{n}'],
    defaultParams: 'length = 14',
    params: P_LENGTH_100(14, 'Lookback period. Shorter = more sensitive, longer = smoother.'),
    description: 'Measures speed and magnitude of price changes. Uses Wilder\'s RMA smoothing by default. Values above 70 = overbought, below 30 = oversold.',
    quickWhy: 'See if a stock is "Overstretched" or exhausted.',
    quickAnalogy: 'Pulling a rubber band to the "70" mark — very tight, wants to snap back (Overbought). At "30," stretched the other way (Oversold).',
    quickImpact: 'Tells you when it\'s "Dangerous to Buy" because everyone else already bought.',
    interpretation: [
      'RSI > 70 → overbought (potential reversal or strong uptrend)',
      'RSI < 30 → oversold (potential reversal or strong downtrend)',
      'RSI divergence from price signals weakening momentum',
      'Centerline (50) crossover used as trend filter',
    ],
    recommendedTimeframes: '5m–1D (excellent on all common timeframes)',
    dataNotes: ['Requires `length` warmup bars (Wilder smoothing)'],
    timeframeBehavior: '15-minute delay is a major risk; RSI might show "35" while real price has already hit "20" and bounced.',
    relatedIndicators: ['stochrsi', 'mfi', 'cci'],
    professionalRef: 'J. Welles Wilder Jr. (1978)',
    checkQuestion: 'If RSI is at 85, is it a good time to start a long-term investment?',
    checkAnswer: 'Probably not — wait for a dip',
  },

  stoch: {
    key: 'stoch',
    displayName: 'Stochastic Oscillator',
    category: 'momentum',
    panelType: 'sub-panel',
    formulaLatex: '\\%K = \\frac{C - L_n}{H_n - L_n} \\times 100, \\quad \\%D = \\text{SMA}(\\%K, d)',
    library: 'pandas-ta (ta.stoch)',
    outputColumns: ['stochk_{k}_{d}', 'stochd_{k}_{d}'],
    defaultParams: 'k = 14, d = 3',
    params: [
      { name: 'k', type: 'int', default: 14, min: 1, max: 100, description: '%K lookback period. Measures close relative to the high-low range.' },
      { name: 'd', type: 'int', default: 3, min: 1, max: 50, description: '%D smoothing period. SMA of %K — the "signal" line.' },
    ],
    description: 'Measures where the close sits in the recent high-low range. %K is the fast line, %D is the signal. Outputs 2 columns.',
    quickWhy: 'See where today\'s price sits compared to recent highs and lows.',
    quickAnalogy: 'A tide gauge at the beach. Is the water at the high-tide mark or low-tide mark?',
    quickImpact: 'When the "High Tide" turns back, it\'s often a signal to sell.',
    interpretation: [
      'K/D above 80 → overbought',
      'K/D below 20 → oversold',
      '%K crossing above %D → bullish signal',
      '%K crossing below %D → bearish signal',
    ],
    recommendedTimeframes: '5m–4h (strong intraday oscillator)',
    dataNotes: ['Requires `k` warmup bars'],
    timeframeBehavior: 'Prone to whipsaws on 1m charts. 15-minute delay can cause you to miss a crossover entirely.',
    relatedIndicators: ['stochrsi', 'rsi', 'willr'],
    professionalRef: 'George Lane (1950s)',
  },

  stochrsi: {
    key: 'stochrsi',
    displayName: 'Stochastic RSI',
    category: 'momentum',
    panelType: 'sub-panel',
    formulaLatex: '\\text{StochRSI}=\\frac{RSI-\\min(RSI)}{\\max(RSI)-\\min(RSI)}',
    library: 'pandas-ta (ta.stochrsi)',
    outputColumns: ['stochrsi_k_{n}', 'stochrsi_d_{n}'],
    defaultParams: 'length = 14, rsi_length = 14, k = 3, d = 3',
    params: P_LENGTH_100(14, 'RSI lookback period.'),
    description: 'Applies a stochastic oscillator calculation to RSI, producing a faster oscillator. Outputs K and D lines.',
    quickWhy: 'An ultra-sensitive version of RSI — finds "Extremes of the Extremes."',
    quickAnalogy: 'A microscope looking at the RSI. It reveals tiny shifts the normal eye misses.',
    quickImpact: 'Great for finding the "Exact Top" or "Exact Bottom" of a quick move.',
    interpretation: [
      'Values near 0 → oversold',
      'Values near 1 → overbought',
      'K/D crossovers generate signals',
      'Faster than regular RSI — more signals but more noise',
    ],
    recommendedTimeframes: '1m–4h (very strong but high signal frequency on lower timeframes)',
    dataNotes: ['Requires `length + rsi_length` warmup bars'],
    timeframeBehavior: 'So sensitive it spends most of its time at 0 or 100. The 15-minute delay is its "Achilles Heel."',
    relatedIndicators: ['rsi', 'stoch', 'willr'],
    professionalRef: 'Tushar Chande and Stanley Kroll (1994)',
  },

  cci: {
    key: 'cci',
    displayName: 'Commodity Channel Index (CCI)',
    category: 'momentum',
    panelType: 'sub-panel',
    formulaLatex: '\\text{CCI} = \\frac{\\text{TP} - \\text{SMA}(\\text{TP}, n)}{0.015 \\cdot \\text{MAD}(\\text{TP}, n)}, \\quad \\text{TP} = \\frac{H+L+C}{3}',
    library: 'pandas-ta (ta.cci)',
    outputColumns: ['cci_{n}'],
    defaultParams: 'length = 14',
    params: P_LENGTH_100(14, 'Lookback for the typical price mean. Above +100 = overbought, below -100 = oversold.'),
    description: 'Measures deviation from the statistical mean. Values above +100 = overbought, below -100 = oversold.',
    quickWhy: 'See when a stock acts "Weird" compared to its history.',
    quickAnalogy: 'A student\'s grades. If they usually get 80s and suddenly get a 20, CCI screams that something is wrong.',
    quickImpact: 'Spots "crashes" or "moonshots" before they become obvious.',
    interpretation: [
      'CCI > +100 → overbought / strong bullish momentum',
      'CCI < -100 → oversold / strong bearish momentum',
      'Zero-line crossover used as trend filter',
      'Divergence with price signals weakening momentum',
    ],
    recommendedTimeframes: '15m–1D (strong on 1h–4h)',
    dataNotes: ['Requires `length` warmup bars'],
    timeframeBehavior: 'Effective for Mean Reversion on Hourly charts.',
    relatedIndicators: ['rsi', 'stoch', 'mfi'],
    professionalRef: 'Donald Lambert (1980)',
  },

  willr: {
    key: 'willr',
    displayName: 'Williams %R',
    category: 'momentum',
    panelType: 'sub-panel',
    formulaLatex: '\\%R = -100\\cdot\\frac{HH(n)-C}{HH(n)-LL(n)}',
    library: 'pandas-ta (ta.willr)',
    outputColumns: ['willr_{n}'],
    defaultParams: 'length = 14',
    params: P_LENGTH_100(14, 'Lookback period. Scale: -100 to 0. Near 0 = overbought, near -100 = oversold.'),
    description: 'Momentum oscillator measuring close location relative to recent high-low range.',
    quickWhy: 'A "flipped" version of the Stochastic focusing strictly on highs.',
    quickAnalogy: 'A thermometer measuring how far you are below the boiling point.',
    quickImpact: 'Excellent for catching "failed rallies" when price tries to hit a new high but fails.',
    interpretation: [
      '-20 to 0 → overbought',
      '-80 to -100 → oversold',
      'Useful for mean reversion and turning points',
      'Essentially the inverse of the Stochastic oscillator',
    ],
    recommendedTimeframes: '1m–4h (strong intraday)',
    dataNotes: ['Requires `length` warmup bars'],
    timeframeBehavior: '15-minute delay often masks the "Overbought" signal until the drop has started.',
    relatedIndicators: ['stoch', 'stochrsi', 'rsi'],
    professionalRef: 'Larry Williams',
  },

  roc: {
    key: 'roc',
    displayName: 'Rate of Change (ROC)',
    category: 'momentum',
    panelType: 'sub-panel',
    formulaLatex: '\\text{ROC}=\\frac{C_t-C_{t-n}}{C_{t-n}}\\times 100',
    library: 'pandas-ta (ta.roc)',
    outputColumns: ['roc_{n}'],
    defaultParams: 'length = 12',
    params: P_LENGTH_100(10, 'Number of bars to compare. Percentage change from n bars ago to now.'),
    description: 'Measures the percentage change from n bars ago.',
    quickWhy: 'Measure the "Speed Limit" of a stock.',
    quickAnalogy: 'A car\'s speedometer. Is the stock going 10 mph or 100 mph?',
    quickImpact: 'If the stock slows from 100 to 80 mph, ROC drops, warning the "race" is ending.',
    interpretation: [
      'ROC > 0 → bullish momentum',
      'ROC < 0 → bearish momentum',
      'ROC crossing above 0 often signals trend initiation',
    ],
    recommendedTimeframes: '15m–1D (all timeframes, particularly useful on 1h)',
    dataNotes: ['Requires `length` warmup bars'],
    timeframeBehavior: '15-minute delay makes "Current Speed" essentially a historical record.',
    relatedIndicators: ['mom', 'rsi', 'tsi'],
    professionalRef: 'Classical momentum theory',
  },

  mom: {
    key: 'mom',
    displayName: 'Momentum (MOM)',
    category: 'momentum',
    panelType: 'sub-panel',
    formulaLatex: '\\text{MOM}=C_t-C_{t-n}',
    library: 'pandas-ta (ta.mom)',
    outputColumns: ['mom_{n}'],
    defaultParams: 'length = 10',
    params: P_LENGTH_100(10, 'Number of bars to compare. Raw price difference (not percentage).'),
    description: 'Absolute price change over n periods. Measures raw speed of movement.',
    quickWhy: 'Similar to ROC, but measures "Total Distance" rather than percentage.',
    quickAnalogy: 'Measuring how many feet a sprinter ran in 10 seconds.',
    quickImpact: 'Shows the pure "Force" of the buyers.',
    interpretation: [
      'Positive MOM → upward momentum',
      'Negative MOM → downward momentum',
      'Works best with smoothing or normalization for signal clarity',
    ],
    recommendedTimeframes: 'All timeframes (best paired with other confirmation)',
    dataNotes: [
      'Requires `length` warmup bars',
      'Not normalized — absolute value depends on price level',
    ],
    timeframeBehavior: 'Results in "Lagged Velocity" due to feed delay.',
    relatedIndicators: ['roc', 'rsi', 'tsi'],
    professionalRef: 'Traditional TA theory',
  },

  tsi: {
    key: 'tsi',
    displayName: 'True Strength Index (TSI)',
    category: 'momentum',
    panelType: 'sub-panel',
    formulaLatex: 'TSI = 100\\cdot \\frac{EMA(EMA(MOM, r), s)}{EMA(EMA(|MOM|, r), s)}',
    library: 'pandas-ta (ta.tsi)',
    outputColumns: ['tsi_{fast}_{slow}_{signal}', 'tsis_{fast}_{slow}_{signal}'],
    defaultParams: 'fast = 13, slow = 25, signal = 13',
    params: [
      { name: 'fast', type: 'int', default: 13, min: 1, max: 100, description: 'Fast smoothing period.' },
      { name: 'slow', type: 'int', default: 25, min: 1, max: 200, description: 'Slow smoothing period.' },
    ],
    description: 'Smooth momentum oscillator designed to reduce noise and highlight trend momentum. Outputs TSI line and signal line.',
    quickWhy: 'Find the "Cleanest" possible trend without jagged price wiggles.',
    quickAnalogy: 'Noise-cancelling headphones. Blocks the background chatter so you only hear the main music of the stock.',
    quickImpact: 'Stays bullish/bearish longer, helping you avoid "Trading Too Much."',
    interpretation: [
      'TSI > 0 → bullish momentum',
      'TSI < 0 → bearish momentum',
      'Signal line crossovers are common entry triggers',
      'Divergence with price indicates weakening trend',
    ],
    recommendedTimeframes: '1h–1D (excellent; moderate on 5m–15m)',
    dataNotes: ['Requires `slow + signal` warmup bars due to double smoothing'],
    timeframeBehavior: 'Significant impact from delay on double-smoothed values.',
    relatedIndicators: ['macd', 'rsi', 'roc'],
    professionalRef: 'William Blau (1995)',
  },

  fisher: {
    key: 'fisher',
    displayName: 'Fisher Transform',
    category: 'momentum',
    panelType: 'sub-panel',
    formulaLatex: 'x=0.33\\cdot 2\\cdot\\left(\\frac{C-\\min(C)}{\\max(C)-\\min(C)}-0.5\\right)+0.67x_{t-1}, \\quad Fisher = 0.5\\cdot \\ln\\left(\\frac{1+x}{1-x}\\right)',
    library: 'pandas-ta (ta.fisher)',
    outputColumns: ['fisher_{n}', 'fishers_{n}'],
    defaultParams: 'length = 9',
    params: P_LENGTH_100(9, 'Lookback period. Normalizes price into a Gaussian distribution.'),
    description: 'Converts price into a near-Gaussian distribution, amplifying turning points.',
    quickWhy: 'Make "reversals" look like sharp spikes so you can\'t miss them.',
    quickAnalogy: 'A flat landscape where mountains (reversals) are hidden. Fisher turns them into giant, neon-lit towers.',
    quickImpact: 'Earliest warnings that a trend is about to end.',
    interpretation: [
      'Fisher crossing above signal → bullish reversal',
      'Fisher crossing below signal → bearish reversal',
      'Often used for cycle detection',
      'Sharp peaks/troughs indicate strong turning points',
    ],
    recommendedTimeframes: '15m–4h (strong); too noisy on 1m–5m',
    dataNotes: ['Requires `length` warmup bars'],
    timeframeBehavior: 'Dangerous with 15-minute delay as it is designed for "instant" turning points.',
    relatedIndicators: ['stoch', 'rsi', 'willr'],
    professionalRef: 'John Ehlers (2004)',
  },

  squeeze: {
    key: 'squeeze',
    displayName: 'Volatility Squeeze',
    category: 'momentum',
    panelType: 'sub-panel',
    formulaLatex: 'BB_{upper} < KC_{upper} \\;\\text{and}\\; BB_{lower} > KC_{lower} \\Rightarrow \\text{Squeeze ON}',
    library: 'pandas-ta (ta.squeeze)',
    outputColumns: ['squeeze_{n}'],
    defaultParams: 'length = 20',
    params: [
      { name: 'bb_length', type: 'int', default: 20, min: 1, max: 200, description: 'Bollinger Band period.' },
      { name: 'kc_length', type: 'int', default: 20, min: 1, max: 200, description: 'Keltner Channel period.' },
    ],
    description: 'Detects when Bollinger Bands contract inside Keltner Channels — a volatility compression phase that often precedes breakouts.',
    quickWhy: 'Find the "Calm Before the Storm."',
    quickAnalogy: 'A coiled spring. The tighter you compress it, the further it flies when released (the breakout).',
    quickImpact: 'Tells you when the market is "resting" before a huge explosion.',
    interpretation: [
      'Squeeze ON → compression phase (low volatility)',
      'Squeeze OFF → expansion phase (breakout imminent)',
      'Best used with volume confirmation',
      'Direction of breakout determined by momentum at release',
    ],
    recommendedTimeframes: '15m–1D (excellent breakout detector on 4h–1D)',
    dataNotes: [
      'Requires stable volatility estimation',
      'Sensitive to missing candles and bad resampling',
    ],
    timeframeBehavior: 'Delay means you miss the first 15 minutes of the "Explosion."',
    relatedIndicators: ['bbands', 'kc', 'atr'],
    professionalRef: 'John Carter',
  },

  aroon: {
    key: 'aroon',
    displayName: 'Aroon',
    category: 'trend',
    panelType: 'sub-panel',
    formulaLatex: '\\text{AroonUp}=\\frac{n-\\text{barsSince}(HH(n))}{n}\\cdot 100, \\quad \\text{AroonDown}=\\frac{n-\\text{barsSince}(LL(n))}{n}\\cdot 100',
    library: 'pandas-ta (ta.aroon)',
    outputColumns: ['aroonu_{n}', 'aroond_{n}', 'aroonosc_{n}'],
    defaultParams: 'length = 25',
    params: P_LENGTH_100(25, 'Lookback period. Measures how long since the highest high / lowest low.'),
    description: 'Measures how long since the highest high and lowest low within a window. Indicates trend changes.',
    quickWhy: 'See if the stock is setting "New Records" or just repeating old history.',
    quickAnalogy: 'A "Time Since Last Win" counter. If they haven\'t won (set a new high) in a long time, the trend is dying.',
    quickImpact: 'Tells you exactly when a "New King" (new trend) takes the throne.',
    interpretation: [
      'AroonUp > 70 and AroonDown < 30 → strong uptrend',
      'AroonDown > 70 and AroonUp < 30 → strong downtrend',
      'Aroon Oscillator crossing zero signals trend reversal',
    ],
    recommendedTimeframes: '1h–1D (works on most timeframes)',
    dataNotes: ['Requires `length` warmup bars'],
    timeframeBehavior: 'Slow indicator; 15-minute delay has limited impact.',
    relatedIndicators: ['adx', 'donchian'],
    professionalRef: 'Tushar Chande (1995)',
  },

  // ═══════════════════════════════════════════════════════════
  //  SUB-PANEL — TREND STRENGTH
  // ═══════════════════════════════════════════════════════════
  adx: {
    key: 'adx',
    displayName: 'Average Directional Index (ADX)',
    category: 'trend',
    panelType: 'sub-panel',
    formulaLatex: '\\text{ADX} = \\text{RMA}\\!\\left(\\frac{|{+DI} - {-DI}|}{+DI + {-DI}} \\times 100,\\; n\\right)',
    library: 'pandas-ta (ta.adx, tvmode=True)',
    outputColumns: ['adx_{n}', 'dmp_{n}', 'dmn_{n}'],
    defaultParams: 'length = 14',
    params: P_LENGTH_100(14, 'Smoothing period. Above 25 = strong trend, below 20 = range-bound.'),
    description: 'Measures trend strength (not direction). Outputs ADX, +DI, and -DI. Uses tvmode=True for TradingView compatibility. ADX > 25 = strong trend.',
    quickWhy: 'See if a trend is "Real" or just meaningless zig-zagging.',
    quickAnalogy: 'A wind speed meter. Doesn\'t care if wind blows North or South — just how hard it\'s blowing.',
    quickImpact: 'Saves you from trading in "boring" markets where nothing is happening.',
    interpretation: [
      'ADX > 25 → strong trend',
      'ADX < 20 → weak/no trend (range-bound)',
      '+DI above -DI → bullish pressure',
      '-DI above +DI → bearish pressure',
    ],
    recommendedTimeframes: '1h–1D (excellent for trend strength confirmation)',
    dataNotes: ['Requires ~2x `length` warmup bars due to nested smoothing'],
    timeframeBehavior: '"Slow" indicator; 15-minute delay is negligible.',
    relatedIndicators: ['atr', 'rsi', 'supertrend'],
    professionalRef: 'J. Welles Wilder Jr. (1978)',
  },

  // ═══════════════════════════════════════════════════════════
  //  SUB-PANEL — VOLATILITY
  // ═══════════════════════════════════════════════════════════
  atr: {
    key: 'atr',
    displayName: 'Average True Range (ATR)',
    category: 'volatility',
    panelType: 'sub-panel',
    formulaLatex: '\\text{TR} = \\max(H-L,\\; |H - C_{t-1}|,\\; |L - C_{t-1}|), \\quad \\text{ATR} = \\text{RMA}(\\text{TR}, n)',
    library: 'pandas-ta (ta.atr)',
    outputColumns: ['atr_{n}'],
    defaultParams: 'length = 14',
    params: P_LENGTH_100(14, 'Smoothing period for the True Range average.'),
    description: 'Measures volatility using the true range of each bar. ATR is the RMA-smoothed average of true range. Used internally by Supertrend and other indicators.',
    quickWhy: 'Measure "Volatility" — how much a stock jumps around.',
    quickAnalogy: 'The daily range of a forest fire. Does it move 1 mile a day or 10 miles? Helps you know how much space to give your trade.',
    quickImpact: 'Helps set "smart" stop-losses so you don\'t get kicked out by a normal daily wiggle.',
    interpretation: [
      'Rising ATR → volatility expansion',
      'Falling ATR → volatility contraction',
      'Common for position sizing (risk per trade = ATR × multiplier)',
      'Used as input to Supertrend, Keltner Channels',
    ],
    recommendedTimeframes: 'All timeframes',
    dataNotes: [
      'Requires `length` warmup bars',
      'Sensitive to gap openings (true range includes previous close)',
    ],
    timeframeBehavior: 'Delay means ATR is based on "Stale Volatility."',
    relatedIndicators: ['natr', 'supertrend', 'kc'],
    professionalRef: 'J. Welles Wilder Jr. (1978)',
  },

  natr: {
    key: 'natr',
    displayName: 'Normalized ATR (NATR)',
    category: 'volatility',
    panelType: 'sub-panel',
    formulaLatex: '\\text{NATR}=\\frac{ATR(n)}{C_t}\\times 100',
    library: 'pandas-ta (ta.natr)',
    outputColumns: ['natr_{n}'],
    defaultParams: 'length = 14',
    params: P_LENGTH_100(14, 'ATR lookback period. Result expressed as a percentage of price.'),
    description: 'Expresses ATR as a percentage of price, making volatility comparable across instruments.',
    quickWhy: 'Compare volatility of two different stocks (e.g. Apple vs. a penny stock).',
    quickAnalogy: 'Comparing a tall man to a short man. A 1-foot jump for a short man is huge; for a tall man, it\'s a small hop.',
    quickImpact: 'Tells you which stocks in your list are the "craziest" today.',
    interpretation: [
      'Rising NATR → volatility expansion',
      'Useful for risk sizing across different-priced instruments',
      'Can be used as a regime detector (high vol vs low vol)',
    ],
    recommendedTimeframes: 'All timeframes (very useful intraday for risk control)',
    dataNotes: ['Requires `length` warmup bars'],
    timeframeBehavior: 'Delay impacts both components (ATR and Price).',
    relatedIndicators: ['atr', 'bbands', 'kc'],
    professionalRef: 'John Forman (2006)',
  },

  // ═══════════════════════════════════════════════════════════
  //  SUB-PANEL — VOLUME
  // ═══════════════════════════════════════════════════════════
  obv: {
    key: 'obv',
    displayName: 'On-Balance Volume (OBV)',
    category: 'volume',
    panelType: 'sub-panel',
    formulaLatex: '\\text{OBV}_t = \\text{OBV}_{t-1} + \\begin{cases} V_t & C_t > C_{t-1} \\\\ -V_t & C_t < C_{t-1} \\\\ 0 & \\text{otherwise} \\end{cases}',
    library: 'pandas-ta (ta.obv)',
    outputColumns: ['obv'],
    defaultParams: 'None',
    params: [],
    description: 'Cumulative volume indicator. Rising OBV confirms uptrend; divergence between OBV and price signals potential reversal. No configurable parameters.',
    quickWhy: 'The "Underground Truth" — tracks if smart money is actually showing up.',
    quickAnalogy: 'A turnstile at a stadium. Doesn\'t matter if ticket prices go up or down; it just counts how many people walked in.',
    quickImpact: 'Price dropping but OBV rising? Smart money is "Buying the Dip."',
    interpretation: [
      'Rising OBV with rising price → healthy uptrend confirmed',
      'Falling OBV while price rises → bearish divergence',
      'OBV breakout before price breakout → leading signal',
    ],
    recommendedTimeframes: '1h–1D+ (noisy on very short timeframes)',
    dataNotes: [
      'Cumulative — can drift heavily over long history',
      'Volume-dependent — unreliable with zero-volume bars',
      'Behaves differently in extended hours vs RTH',
    ],
    timeframeBehavior: 'Non-impactful 15-minute delay for this long-term cumulative line.',
    relatedIndicators: ['ad', 'cmf', 'mfi'],
    professionalRef: 'Joseph Granville (1963)',
  },

  ad: {
    key: 'ad',
    displayName: 'Accumulation/Distribution Line (AD)',
    category: 'volume',
    panelType: 'sub-panel',
    formulaLatex: 'CLV=\\frac{(C-L)-(H-C)}{H-L}, \\quad AD_t=AD_{t-1} + CLV\\cdot V',
    library: 'pandas-ta (ta.ad)',
    outputColumns: ['ad'],
    defaultParams: 'None (cumulative)',
    params: [],
    description: 'Estimates buying vs selling pressure using price location within the bar combined with volume.',
    quickWhy: 'See what the "Big Fish" (institutions) are doing behind the scenes.',
    quickAnalogy: 'A bank account for the stock. If price is flat but this line goes up, money is being "deposited."',
    quickImpact: 'Warns you of a breakout before it happens.',
    interpretation: [
      'Rising AD with rising price → healthy uptrend',
      'Falling AD while price rises → bearish divergence',
      'Strong divergence tool for spotting distribution',
    ],
    recommendedTimeframes: '4h–1W (noisy on intraday, excellent on daily+)',
    dataNotes: [
      'Cumulative — can drift heavily over long history',
      'Volume-dependent — unreliable with zero-volume bars',
      'Behaves differently in extended hours vs RTH',
    ],
    timeframeBehavior: '15-minute delay is a non-factor for this long-term cumulative line.',
    relatedIndicators: ['obv', 'cmf', 'mfi'],
    professionalRef: 'Marc Chaikin',
  },

  cmf: {
    key: 'cmf',
    displayName: 'Chaikin Money Flow (CMF)',
    category: 'volume',
    panelType: 'sub-panel',
    formulaLatex: 'CMF(n)=\\frac{\\sum_{i=0}^{n-1}(CLV_i\\cdot V_i)}{\\sum_{i=0}^{n-1}V_i}',
    library: 'pandas-ta (ta.cmf)',
    outputColumns: ['cmf_{n}'],
    defaultParams: 'length = 20',
    params: P_LENGTH_100(20, 'Lookback period. Averages Money Flow Volume over this window.'),
    description: 'Measures accumulation/distribution over a rolling window.',
    quickWhy: 'See if the "Big Fish" are still buying over several weeks.',
    quickAnalogy: 'A popularity contest lasting 21 days. Shows if people are consistently voting "Yes" with their money.',
    quickImpact: 'If price goes up but CMF stays below zero, the move is a "Fakeout."',
    interpretation: [
      'CMF > 0 → accumulation (bullish)',
      'CMF < 0 → distribution (bearish)',
      'Strong divergence indicator vs price',
    ],
    recommendedTimeframes: '1D+ (best on daily; works but noisy intraday)',
    dataNotes: [
      'Requires `length` warmup bars',
      'Volume-dependent — unreliable with zero-volume bars',
    ],
    timeframeBehavior: 'Volume "confirmation" is based on stale data.',
    relatedIndicators: ['ad', 'obv', 'mfi'],
    professionalRef: 'Marc Chaikin',
  },

  mfi: {
    key: 'mfi',
    displayName: 'Money Flow Index (MFI)',
    category: 'volume',
    panelType: 'sub-panel',
    formulaLatex: 'TP=\\frac{H+L+C}{3}, \\quad MF=TP\\cdot V, \\quad MFI = 100 - \\frac{100}{1 + \\frac{\\sum MF^+}{\\sum MF^-}}',
    library: 'pandas-ta (ta.mfi)',
    outputColumns: ['mfi_{n}'],
    defaultParams: 'length = 14',
    params: P_LENGTH_100(14, 'Lookback period. Above 80 = overbought, below 20 = oversold.'),
    description: 'RSI-like oscillator but volume-weighted. Often called "volume RSI".',
    quickWhy: 'RSI but with volume included — measures how much money actually moves price.',
    quickAnalogy: 'A concert. RSI shows how loud the music is; MFI shows how many people are in the stadium cheering.',
    quickImpact: 'Much harder to "fake" than standard RSI.',
    interpretation: [
      'MFI > 80 → overbought',
      'MFI < 20 → oversold',
      'Divergence with price signals potential reversal',
      'More reliable than RSI when volume data is clean',
    ],
    recommendedTimeframes: '4h–1W (noisy intraday; excellent on daily)',
    dataNotes: [
      'Requires `length` warmup bars',
      'Volume-dependent — unreliable with zero-volume bars',
      'Behaves differently in extended hours vs RTH',
    ],
    timeframeBehavior: 'Feed delay impacts the volume input, which is critical here.',
    relatedIndicators: ['rsi', 'cmf', 'obv'],
    professionalRef: 'Gene Quong and Avrum Soudack',
  },
};

/** All entries as a sorted array (alphabetical by key). */
export const INDICATOR_REFERENCE_LIST: readonly IndicatorReferenceEntry[] = Object.freeze(
  Object.values(INDICATOR_REFERENCE).sort((a, b) => a.key.localeCompare(b.key))
);

/** Look up by key; returns null when undocumented. */
export function getIndicatorReference(key: string): IndicatorReferenceEntry | null {
  return INDICATOR_REFERENCE[key] ?? null;
}

/**
 * Category metadata used by the UI for chip color and label.
 *
 * `color` and `colorSoft` are CSS custom-property references resolved at
 * render time. The actual hex values live in `Frontend/src/app/styles/_tokens.scss`
 * under `--ind-cat-{category}` / `--ind-cat-{category}-soft` so a design
 * pass can re-tune the palette without touching component code.
 */
export const CATEGORY_META: Record<IndicatorCategory, { label: string; color: string; colorSoft: string }> = {
  trend:      { label: 'Trend',      color: 'var(--ind-cat-trend)',      colorSoft: 'var(--ind-cat-trend-soft)' },
  momentum:   { label: 'Momentum',   color: 'var(--ind-cat-momentum)',   colorSoft: 'var(--ind-cat-momentum-soft)' },
  volatility: { label: 'Volatility', color: 'var(--ind-cat-volatility)', colorSoft: 'var(--ind-cat-volatility-soft)' },
  volume:     { label: 'Volume',     color: 'var(--ind-cat-volume)',     colorSoft: 'var(--ind-cat-volume-soft)' },
};

// ─────────────────────────────────────────────────────────────
//  Backwards-compatibility shim
// ─────────────────────────────────────────────────────────────
//
//  The old shared/indicator-docs.ts exposed `INDICATOR_QUICK_INFO` shaped as
//  `{ displayName, why, analogy, impact, panelType, params: [{name,description}] }`.
//  We re-export the same shape from the consolidated reference so that any
//  consumer not yet migrated keeps working. New code should import
//  `INDICATOR_REFERENCE` directly.

export interface IndicatorParamDoc {
  name: string;
  description: string;
}

export interface IndicatorQuickInfo {
  key: string;
  displayName: string;
  why: string;
  analogy: string;
  impact: string;
  panelType: IndicatorPanelType;
  params: IndicatorParamDoc[];
}

/** @deprecated Use {@link INDICATOR_REFERENCE} or {@link getIndicatorReference}. */
export const INDICATOR_QUICK_INFO: Record<string, IndicatorQuickInfo> = Object.freeze(
  Object.fromEntries(
    Object.values(INDICATOR_REFERENCE).map((e) => [
      e.key,
      {
        key: e.key,
        displayName: e.displayName,
        why: e.quickWhy,
        analogy: e.quickAnalogy,
        impact: e.quickImpact,
        panelType: e.panelType,
        params: e.params.map((p) => ({ name: p.name, description: p.description })),
      } satisfies IndicatorQuickInfo,
    ])
  )
);
