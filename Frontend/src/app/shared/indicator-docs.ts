/**
 * Indicator Quick-Info documentation data.
 *
 * Each entry provides user-friendly content (analogy, why, impact)
 * plus parameter-level descriptions. This data powers the rich
 * tooltip overlay and can be reused anywhere indicators appear.
 *
 * Content sourced from the "35 Indicators for Modern Trading"
 * technical documentation suite.
 */

export interface IndicatorParamDoc {
  name: string;
  description: string;
}

export interface IndicatorQuickInfo {
  /** Internal key matching INDICATOR_META / backend (e.g. 'ema') */
  key: string;
  /** Human-readable display name */
  displayName: string;
  /** One-line "why use this?" */
  why: string;
  /** Plain-English analogy */
  analogy: string;
  /** Practical impact for the trader */
  impact: string;
  /** 'overlay' or 'sub-panel' */
  panelType: 'overlay' | 'sub-panel';
  /** Parameter documentation (keyed by param name) */
  params: IndicatorParamDoc[];
}

// ─────────────────────────────────────────────────────────────
// Complete documentation for all 35 indicators
// ─────────────────────────────────────────────────────────────

export const INDICATOR_QUICK_INFO: Record<string, IndicatorQuickInfo> = {

  // ═══════════════════════════════════════════════════════════
  //  PART I — OVERLAY INDICATORS
  // ═══════════════════════════════════════════════════════════

  ema: {
    key: 'ema',
    displayName: 'Exponential Moving Average (EMA)',
    why: 'Find the "current heartbeat" of a stock. Reacts faster to recent news than a standard average.',
    analogy: 'Driving a car and looking mostly at the 50 feet immediately behind you — it tells you about the turn you just made, not the road from five miles ago.',
    impact: 'Spots trend changes early. If price stays above this line, the trend\'s "pulse" is healthy.',
    panelType: 'overlay',
    params: [
      { name: 'length', description: 'Lookback period. Shorter = faster reaction (e.g. 10), longer = smoother trend (e.g. 200).' },
    ],
  },

  sma: {
    key: 'sma',
    displayName: 'Simple Moving Average (SMA)',
    why: 'Find the "big picture" average. Treats every day in its window as equally important.',
    analogy: 'The average temperature of a city. One heatwave doesn\'t change the climate. SMA shows you the "climate" of the stock.',
    impact: 'Acts as a major floor or ceiling that big banks and institutions watch closely.',
    panelType: 'overlay',
    params: [
      { name: 'length', description: 'Number of bars averaged. Common values: 20 (short-term), 50 (medium), 200 (long-term institutional level).' },
    ],
  },

  dema: {
    key: 'dema',
    displayName: 'Double Exponential Moving Average (DEMA)',
    why: 'For speed. Removes the "lag" that makes other averages feel slow and outdated.',
    analogy: 'If a standard average is a heavy bus, DEMA is a motorcycle that whips around corners instantly.',
    impact: 'Entry and exit signals arrive much earlier than with traditional moving averages.',
    panelType: 'overlay',
    params: [
      { name: 'length', description: 'Lookback period. Lower = more responsive but noisier.' },
    ],
  },

  tema: {
    key: 'tema',
    displayName: 'Triple Exponential Moving Average (TEMA)',
    why: 'Ultra-fast confirmation. The most sensitive version of DEMA.',
    analogy: 'A fighter jet following the price so closely it almost touches the candles.',
    impact: 'Best for hyper-volatile markets where direction changes in the blink of an eye.',
    panelType: 'overlay',
    params: [
      { name: 'length', description: 'Lookback period. Keep above 8 on lower timeframes to avoid excessive noise.' },
    ],
  },

  wma: {
    key: 'wma',
    displayName: 'Weighted Moving Average (WMA)',
    why: 'Ensures today\'s news is more important than last week\'s.',
    analogy: 'Like a "Trending Now" list — what people said 5 minutes ago matters more than what was popular yesterday.',
    impact: 'Stays closer to the price than an SMA, making it a better tracker for active traders.',
    panelType: 'overlay',
    params: [
      { name: 'length', description: 'Window size. Recent bars get linearly higher weight.' },
    ],
  },

  hma: {
    key: 'hma',
    displayName: 'Hull Moving Average (HMA)',
    why: 'Be smooth and fast at the same time — usually you have to pick one.',
    analogy: 'A silk ribbon flowing in the wind. It follows the wind (price) perfectly without any sharp, jagged edges.',
    impact: 'Clear "Go / No-Go" signals; the line changes color when the trend shifts.',
    panelType: 'overlay',
    params: [
      { name: 'length', description: 'Lookback period. Internally uses WMA(n/2) and WMA(n). Higher = smoother.' },
    ],
  },

  kama: {
    key: 'kama',
    displayName: 'Kaufman Adaptive Moving Average (KAMA)',
    why: 'An average with its own "intelligence." Speeds up in trends and slows down during chop.',
    analogy: 'Adaptive cruise control that speeds up on the highway but slows down in heavy city traffic.',
    impact: 'Helps you avoid getting fooled by small, meaningless price jumps.',
    panelType: 'overlay',
    params: [
      { name: 'length', description: 'Efficiency ratio lookback. Controls how far back to measure "signal vs. noise."' },
    ],
  },

  zlma: {
    key: 'zlma',
    displayName: 'Zero Lag Moving Average (ZLMA)',
    why: '"Predict" the price by removing the delay found in all other averages.',
    analogy: 'A forecast telling you where the storm will be in 10 minutes, rather than where it was 10 minutes ago.',
    impact: 'Crosses the price earlier than any other average, giving you a head start.',
    panelType: 'overlay',
    params: [
      { name: 'length', description: 'Base EMA period. The lag compensation uses half this value.' },
    ],
  },

  rma: {
    key: 'rma',
    displayName: 'Wilder\'s Moving Average (RMA)',
    why: 'The stable foundation used to calculate other famous tools like RSI.',
    analogy: 'The foundation of a house. You don\'t see it, but everything else relies on it being stable and slow-moving.',
    impact: 'Provides a very smooth, long-term view of "Fair Value."',
    panelType: 'overlay',
    params: [
      { name: 'length', description: 'Smoothing period. Uses alpha = 1/n, making it slower than a standard EMA of the same length.' },
    ],
  },

  alma: {
    key: 'alma',
    displayName: 'Arnaud Legoux Moving Average (ALMA)',
    why: 'The smoothest line without the "overshoot" common in fast averages.',
    analogy: 'A cinematographer using a steadicam — even if the cameraman is running, the footage looks perfectly smooth.',
    impact: 'The most trustworthy line for identifying a steady trend.',
    panelType: 'overlay',
    params: [
      { name: 'length', description: 'Window size for the Gaussian-weighted average.' },
      { name: 'sigma', description: 'Width of the Gaussian curve. Higher = wider bell, smoother result. Default: 6.' },
      { name: 'offset', description: 'Bias factor (0 to 1). 0.85 = right-biased (more weight on recent bars). Default: 0.85.' },
    ],
  },

  bbands: {
    key: 'bbands',
    displayName: 'Bollinger Bands (BBANDS)',
    why: 'See the market\'s "Mood Swing" — how far price is stretching from normal.',
    analogy: 'A rubber band. The further you stretch it, the more likely it is to snap back to the center.',
    impact: 'When the bands get tight (a "Squeeze"), a massive move is usually coming soon.',
    panelType: 'overlay',
    params: [
      { name: 'length', description: 'SMA lookback for the middle band. Default: 20.' },
      { name: 'std', description: 'Number of standard deviations for the upper/lower bands. Default: 2.0. Higher = wider bands.' },
    ],
  },

  supertrend: {
    key: 'supertrend',
    displayName: 'Supertrend',
    why: 'A simple "Stoplight" for trades — exactly when to be "In" or "Out."',
    analogy: 'A trail marker on a hike. As long as you stay on the green path, you\'re safe. Red means you\'ve gone off-trail.',
    impact: 'Automatically provides a level for your stop-loss.',
    panelType: 'overlay',
    params: [
      { name: 'length', description: 'ATR lookback period. Higher = slower, fewer flips. Default: 10.' },
      { name: 'multiplier', description: 'ATR multiplier for band distance. Higher = wider bands, fewer whipsaws. Default: 3.0.' },
    ],
  },

  vwap: {
    key: 'vwap',
    displayName: 'Volume Weighted Average Price (VWAP)',
    why: 'Find the "True Cost" today by combining price and how many people were buying.',
    analogy: 'If 1 person buys coffee for $10 and 100 people buy it for $2, the "real" average isn\'t $6 — it\'s $2. VWAP shows that $2 price.',
    impact: '"Big Money" uses this. Buying below this line is usually a "good deal" for the day.',
    panelType: 'overlay',
    params: [],
  },

  psar: {
    key: 'psar',
    displayName: 'Parabolic SAR (PSAR)',
    why: 'Your "Security Guard." Follows price up and tells you the second the trend is over.',
    analogy: 'A dog on a leash that gets shorter as you get closer to home. Eventually you hit the end and must stop.',
    impact: 'Tells you exactly where to exit a trade to protect your profits.',
    panelType: 'overlay',
    params: [
      { name: 'af0', description: 'Initial acceleration factor. Default: 0.02. Controls how quickly the SAR tightens.' },
      { name: 'af', description: 'Acceleration factor step increment. Default: 0.02.' },
      { name: 'max_af', description: 'Maximum acceleration factor cap. Default: 0.2.' },
    ],
  },

  kc: {
    key: 'kc',
    displayName: 'Keltner Channel (KC)',
    why: 'A calmer version of Bollinger Bands — a tunnel that doesn\'t overreact to every spike.',
    analogy: 'A riverbed. Price stays within the banks most of the time. If it overflows, something big is happening.',
    impact: 'Best for finding "Bargain Entries" when price dips to the middle during an uptrend.',
    panelType: 'overlay',
    params: [
      { name: 'length', description: 'EMA lookback for the middle line. Default: 20.' },
      { name: 'scalar', description: 'ATR multiplier for the channel width. Default: 1.5.' },
    ],
  },

  donchian: {
    key: 'donchian',
    displayName: 'Donchian Channel',
    why: 'Shows the "Ultimate High" and "Ultimate Low" of the last few days.',
    analogy: 'A sports record book showing the all-time high score for the current "game" (period).',
    impact: 'Helps find breakouts — when price breaks a record high, it often keeps going.',
    panelType: 'overlay',
    params: [
      { name: 'lower_length', description: 'Lookback for the lowest low. Default: 20.' },
      { name: 'upper_length', description: 'Lookback for the highest high. Default: 20.' },
    ],
  },

  // ═══════════════════════════════════════════════════════════
  //  PART II — SUB-PANEL INDICATORS
  // ═══════════════════════════════════════════════════════════

  macd: {
    key: 'macd',
    displayName: 'MACD (Moving Average Convergence Divergence)',
    why: 'Feel the "momentum" behind a move.',
    analogy: 'A snowball rolling down a hill. MACD tells you if it\'s getting bigger and faster (strong trend) or starting to melt (weak trend).',
    impact: 'When the two lines cross, it\'s like a "Starting Pistol" for a new move.',
    panelType: 'sub-panel',
    params: [
      { name: 'fast', description: 'Fast EMA period. Default: 12. Shorter = more sensitive.' },
      { name: 'slow', description: 'Slow EMA period. Default: 26. The MACD line = fast EMA minus slow EMA.' },
      { name: 'signal', description: 'Signal line EMA period. Default: 9. Smooths the MACD line for crossover detection.' },
    ],
  },

  rsi: {
    key: 'rsi',
    displayName: 'Relative Strength Index (RSI)',
    why: 'See if a stock is "Overstretched" or exhausted.',
    analogy: 'Pulling a rubber band to the "70" mark — very tight and wants to snap back (Overbought). At "30," it\'s stretched the other way (Oversold).',
    impact: 'Tells you when it\'s "Dangerous to Buy" because everyone else already bought.',
    panelType: 'sub-panel',
    params: [
      { name: 'length', description: 'Lookback period. Default: 14. Shorter = more sensitive, longer = smoother.' },
    ],
  },

  adx: {
    key: 'adx',
    displayName: 'Average Directional Index (ADX)',
    why: 'See if a trend is "Real" or just meaningless zig-zagging.',
    analogy: 'A wind speed meter. It doesn\'t care if the wind blows North or South — just how hard it\'s blowing.',
    impact: 'Saves you from trading in "boring" markets where nothing is happening.',
    panelType: 'sub-panel',
    params: [
      { name: 'length', description: 'Smoothing period. Default: 14. Above 25 = strong trend, below 20 = range-bound.' },
    ],
  },

  atr: {
    key: 'atr',
    displayName: 'Average True Range (ATR)',
    why: 'Measure "Volatility" — how much a stock jumps around.',
    analogy: 'The daily range of a forest fire. Does it move 1 mile a day or 10 miles? Helps you know how much space to give your trade.',
    impact: 'Helps set "smart" stop-losses so you don\'t get kicked out by a normal daily wiggle.',
    panelType: 'sub-panel',
    params: [
      { name: 'length', description: 'Smoothing period for the True Range average. Default: 14.' },
    ],
  },

  stoch: {
    key: 'stoch',
    displayName: 'Stochastic Oscillator',
    why: 'See where today\'s price sits compared to recent highs and lows.',
    analogy: 'A tide gauge at the beach. Is the water currently at the high-tide mark or the low-tide mark?',
    impact: 'When the "High Tide" turns back, it\'s often a signal to sell.',
    panelType: 'sub-panel',
    params: [
      { name: 'k', description: '%K lookback period. Default: 14. Measures close relative to the high-low range.' },
      { name: 'd', description: '%D smoothing period. Default: 3. SMA of %K — the "signal" line.' },
    ],
  },

  stochrsi: {
    key: 'stochrsi',
    displayName: 'Stochastic RSI',
    why: 'An ultra-sensitive version of RSI — finds "Extremes of the Extremes."',
    analogy: 'A microscope looking at the RSI. It reveals tiny shifts the normal eye misses.',
    impact: 'Great for finding the "Exact Top" or "Exact Bottom" of a quick move.',
    panelType: 'sub-panel',
    params: [
      { name: 'length', description: 'RSI period. Default: 14.' },
      { name: 'rsi_length', description: 'Stochastic lookback applied to the RSI values. Default: 14.' },
      { name: 'k', description: '%K smoothing. Default: 3.' },
      { name: 'd', description: '%D smoothing. Default: 3.' },
    ],
  },

  cci: {
    key: 'cci',
    displayName: 'Commodity Channel Index (CCI)',
    why: 'See when a stock acts "Weird" compared to its history.',
    analogy: 'A student\'s grades. If they usually get 80s and suddenly get a 20, CCI screams that something is wrong.',
    impact: 'Spots "crashes" or "moonshots" before they become obvious.',
    panelType: 'sub-panel',
    params: [
      { name: 'length', description: 'Lookback for the typical price mean. Default: 14. Above +100 = overbought, below -100 = oversold.' },
    ],
  },

  willr: {
    key: 'willr',
    displayName: 'Williams %R',
    why: 'A "flipped" version of the Stochastic focusing strictly on highs.',
    analogy: 'A thermometer measuring how far you are below the boiling point.',
    impact: 'Excellent for catching "failed rallies" when price tries to hit a new high but fails.',
    panelType: 'sub-panel',
    params: [
      { name: 'length', description: 'Lookback period. Default: 14. Scale: -100 to 0. Near 0 = overbought, near -100 = oversold.' },
    ],
  },

  roc: {
    key: 'roc',
    displayName: 'Rate of Change (ROC)',
    why: 'Measure the "Speed Limit" of a stock.',
    analogy: 'A car\'s speedometer. Is the stock going 10 mph or 100 mph?',
    impact: 'If a stock goes 100 mph and slows to 80, ROC drops, warning you the "race" is ending.',
    panelType: 'sub-panel',
    params: [
      { name: 'length', description: 'Number of bars to compare. Percentage change from n bars ago to now.' },
    ],
  },

  mom: {
    key: 'mom',
    displayName: 'Momentum (MOM)',
    why: 'Similar to ROC, but measures "Total Distance" rather than percentage.',
    analogy: 'Measuring how many feet a sprinter ran in 10 seconds.',
    impact: 'Shows the pure "Force" of the buyers.',
    panelType: 'sub-panel',
    params: [
      { name: 'length', description: 'Number of bars to compare. Raw price difference (not percentage).' },
    ],
  },

  natr: {
    key: 'natr',
    displayName: 'Normalized ATR (NATR)',
    why: 'Compare volatility of two different stocks (e.g. Apple vs. a penny stock).',
    analogy: 'Comparing a tall man to a short man. A 1-foot jump for a short man is huge; for a tall man, it\'s a small hop. NATR adjusts for "height."',
    impact: 'Tells you which stocks in your list are the "craziest" today.',
    panelType: 'sub-panel',
    params: [
      { name: 'length', description: 'ATR lookback period. Result expressed as a percentage of price.' },
    ],
  },

  ad: {
    key: 'ad',
    displayName: 'Accumulation/Distribution Line (AD)',
    why: 'See what the "Big Fish" (institutions) are doing behind the scenes.',
    analogy: 'A bank account for the stock. If the price is flat but this line goes up, money is being "deposited" into the stock.',
    impact: 'Warns you of a breakout before it happens.',
    panelType: 'sub-panel',
    params: [],
  },

  cmf: {
    key: 'cmf',
    displayName: 'Chaikin Money Flow (CMF)',
    why: 'See if the "Big Fish" are still buying over several weeks.',
    analogy: 'A popularity contest lasting 21 days. Shows if people are consistently voting "Yes" with their money.',
    impact: 'If price goes up but CMF stays below zero, the move is a "Fakeout."',
    panelType: 'sub-panel',
    params: [
      { name: 'length', description: 'Lookback period. Default: 20. Averages Money Flow Volume over this window.' },
    ],
  },

  mfi: {
    key: 'mfi',
    displayName: 'Money Flow Index (MFI)',
    why: 'RSI but with volume included — measures how much money actually moves price.',
    analogy: 'A concert. RSI shows how loud the music is; MFI shows how many people are in the stadium cheering.',
    impact: 'Much harder to "fake" than standard RSI.',
    panelType: 'sub-panel',
    params: [
      { name: 'length', description: 'Lookback period. Default: 14. Above 80 = overbought, below 20 = oversold.' },
    ],
  },

  tsi: {
    key: 'tsi',
    displayName: 'True Strength Index (TSI)',
    why: 'Find the "Cleanest" possible trend without jagged price wiggles.',
    analogy: 'Noise-cancelling headphones. Blocks the background chatter so you only hear the main music of the stock.',
    impact: 'Stays bullish/bearish longer, helping you avoid "Trading Too Much."',
    panelType: 'sub-panel',
    params: [
      { name: 'fast', description: 'Fast smoothing period. Default: 13.' },
      { name: 'slow', description: 'Slow smoothing period. Default: 25.' },
    ],
  },

  fisher: {
    key: 'fisher',
    displayName: 'Fisher Transform',
    why: 'Make "reversals" look like sharp spikes so you can\'t miss them.',
    analogy: 'A flat landscape where mountains (reversals) are hidden. Fisher turns them into giant, neon-lit towers.',
    impact: 'Earliest warnings that a trend is about to end.',
    panelType: 'sub-panel',
    params: [
      { name: 'length', description: 'Lookback period. Default: 9. Normalizes price into a Gaussian distribution.' },
    ],
  },

  squeeze: {
    key: 'squeeze',
    displayName: 'Volatility Squeeze',
    why: 'Find the "Calm Before the Storm."',
    analogy: 'A coiled spring. The tighter you compress it (the Squeeze), the further it flies when released (the breakout).',
    impact: 'Tells you when the market is "resting" before a huge explosion.',
    panelType: 'sub-panel',
    params: [
      { name: 'bb_length', description: 'Bollinger Band period. Default: 20.' },
      { name: 'bb_std', description: 'Bollinger Band standard deviations. Default: 2.0.' },
      { name: 'kc_length', description: 'Keltner Channel period. Default: 20.' },
      { name: 'kc_scalar', description: 'Keltner Channel ATR multiplier. Default: 1.5.' },
    ],
  },

  aroon: {
    key: 'aroon',
    displayName: 'Aroon',
    why: 'See if the stock is setting "New Records" or just repeating old history.',
    analogy: 'A "Time Since Last Win" counter. If they haven\'t won (set a new high) in a long time, the trend is dying.',
    impact: 'Tells you exactly when a "New King" (new trend) takes the throne.',
    panelType: 'sub-panel',
    params: [
      { name: 'length', description: 'Lookback period. Default: 25. Measures how long since the highest high / lowest low.' },
    ],
  },

  obv: {
    key: 'obv',
    displayName: 'On-Balance Volume (OBV)',
    why: 'The "Underground Truth" — tracks if smart money is actually showing up.',
    analogy: 'A turnstile at a stadium. It doesn\'t matter if ticket prices go up or down; it just counts how many people walked in.',
    impact: 'Price dropping but OBV rising? Smart money is "Buying the Dip."',
    panelType: 'sub-panel',
    params: [],
  },
};
