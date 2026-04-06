import { Component, ChangeDetectionStrategy, ElementRef, inject, viewChildren, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterModule } from '@angular/router';
import { Accordion, AccordionContent, AccordionHeader, AccordionPanel } from 'primeng/accordion';
import { KatexDirective } from '../../../shared/katex.directive';

interface IndicatorDoc {
  name: string;
  displayName: string;
  formulaLatex: string;
  description: string;
  library: string;
  outputColumns: string[];
  defaultParams: string;
  interpretation: string[];
  recommendedTimeframes: string;
  dataNotes: string[];
  relatedIndicators: string[];
  panelType: 'overlay' | 'sub-panel';
}

@Component({
  selector: 'app-data-lab-docs',
  standalone: true,
  imports: [CommonModule, RouterModule, Accordion, AccordionContent, AccordionHeader, AccordionPanel, KatexDirective],
  templateUrl: './data-lab-docs.component.html',
  styleUrls: ['./data-lab-docs.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class DataLabDocsComponent {
  private el = inject(ElementRef);

  allIndicators: IndicatorDoc[] = [
    // ═══════════════════════════════════════════════════════════
    //  OVERLAY INDICATORS
    // ═══════════════════════════════════════════════════════════
    {
      name: 'ema',
      displayName: 'Exponential Moving Average (EMA)',
      formulaLatex: '\\text{EMA}_t = \\alpha \\cdot C_t + (1 - \\alpha) \\cdot \\text{EMA}_{t-1}, \\quad \\alpha = \\frac{2}{n + 1}',
      description: 'Gives more weight to recent prices. Default setup calculates 8 EMAs (5, 10, 20, 30, 40, 50, 100, 200) for multi-timeframe analysis. Each EMA instance adds one column.',
      library: 'pandas-ta (ta.ema)',
      outputColumns: ['ema_{length}'],
      defaultParams: 'length = 5, 10, 20, 30, 40, 50, 100, 200',
      interpretation: [
        'Price above EMA → bullish bias',
        'Price below EMA → bearish bias',
        'EMA crossovers (fast vs slow) generate trend signals',
        'Multiple EMAs form a "ribbon" — fanning = strong trend, converging = consolidation',
      ],
      recommendedTimeframes: '1m–1D+ (all timeframes)',
      dataNotes: [
        'Requires `length` warmup bars before producing valid values',
        'Sensitive to missing candles — EMA drifts if gaps exist',
      ],
      relatedIndicators: ['sma', 'dema', 'tema'],
      panelType: 'overlay',
    },
    {
      name: 'sma',
      displayName: 'Simple Moving Average (SMA)',
      formulaLatex: '\\text{SMA}_t(n)=\\frac{1}{n}\\sum_{i=0}^{n-1}C_{t-i}',
      description: 'The arithmetic mean of the last n closing prices. One of the most widely used trend filters.',
      library: 'pandas-ta (ta.sma)',
      outputColumns: ['sma_{length}'],
      defaultParams: 'length = 20',
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
      relatedIndicators: ['ema', 'wma', 'dema'],
      panelType: 'overlay',
    },
    {
      name: 'dema',
      displayName: 'Double Exponential Moving Average (DEMA)',
      formulaLatex: '\\text{DEMA}=2\\cdot EMA(n)-EMA(EMA(n))',
      description: 'Reduces lag compared to EMA by subtracting a smoothed EMA of EMA.',
      library: 'pandas-ta (ta.dema)',
      outputColumns: ['dema_{length}'],
      defaultParams: 'length = 20',
      interpretation: [
        'Same usage as EMA (trend following)',
        'More responsive than SMA/EMA',
        'Helps reduce late entries in trends',
      ],
      recommendedTimeframes: '5m–1D (excellent on 4h–1D)',
      dataNotes: [
        'Warmup is larger than EMA due to nested smoothing',
      ],
      relatedIndicators: ['ema', 'tema', 'zlma'],
      panelType: 'overlay',
    },
    {
      name: 'tema',
      displayName: 'Triple Exponential Moving Average (TEMA)',
      formulaLatex: '\\text{TEMA}=3\\cdot EMA(n)-3\\cdot EMA(EMA(n))+EMA(EMA(EMA(n)))',
      description: 'Further reduces lag compared to DEMA while still smoothing price.',
      library: 'pandas-ta (ta.tema)',
      outputColumns: ['tema_{length}'],
      defaultParams: 'length = 20',
      interpretation: [
        'Trend-following moving average',
        'Better for catching early reversals',
        'Can be slightly more reactive / noisier than DEMA',
      ],
      recommendedTimeframes: '15m–1D+ (very strong on daily)',
      dataNotes: [
        'Requires 3x warmup of base EMA length due to triple nesting',
      ],
      relatedIndicators: ['ema', 'dema', 'zlma'],
      panelType: 'overlay',
    },
    {
      name: 'wma',
      displayName: 'Weighted Moving Average (WMA)',
      formulaLatex: '\\text{WMA}_t(n)=\\frac{\\sum_{i=0}^{n-1}(n-i)\\cdot C_{t-i}}{\\sum_{k=1}^{n}k}',
      description: 'Applies larger weights to recent bars, making it more responsive than SMA.',
      library: 'pandas-ta (ta.wma)',
      outputColumns: ['wma_{length}'],
      defaultParams: 'length = 20',
      interpretation: [
        'Faster trend identification than SMA',
        'Useful for short-term trend tracking',
        'Works well for crossover systems',
      ],
      recommendedTimeframes: '1m–4h (strong on intraday)',
      dataNotes: [
        'Requires `length` warmup bars',
      ],
      relatedIndicators: ['sma', 'ema', 'hma'],
      panelType: 'overlay',
    },
    {
      name: 'hma',
      displayName: 'Hull Moving Average (HMA)',
      formulaLatex: '\\text{HMA}(n)=WMA(2\\cdot WMA(n/2)-WMA(n),\\sqrt{n})',
      description: 'Reduces lag while keeping smoothness, often producing visually clean trend lines.',
      library: 'pandas-ta (ta.hma)',
      outputColumns: ['hma_{length}'],
      defaultParams: 'length = 55',
      interpretation: [
        'Excellent trend filter',
        'Often used as "signal line" for trend changes',
        'Commonly used in algorithmic trend systems',
      ],
      recommendedTimeframes: '4h–1D (excellent); can be choppy on 5m–1h',
      dataNotes: [
        'Warmup depends on sqrt(length) inner WMA',
      ],
      relatedIndicators: ['wma', 'ema', 'kama'],
      panelType: 'overlay',
    },
    {
      name: 'kama',
      displayName: 'Kaufman Adaptive Moving Average (KAMA)',
      formulaLatex: 'ER=\\frac{|C_t-C_{t-n}|}{\\sum_{i=1}^{n}|C_{t-i}-C_{t-i-1}|}, \\quad SC=(ER\\cdot(fastSC-slowSC)+slowSC)^2, \\quad KAMA_t = KAMA_{t-1} + SC\\cdot(C_t-KAMA_{t-1})',
      description: 'Adapts smoothing based on market efficiency: smooth during choppy markets, fast during trending markets.',
      library: 'pandas-ta (ta.kama)',
      outputColumns: ['kama_{length}'],
      defaultParams: 'length = 10',
      interpretation: [
        'Excellent adaptive trend line',
        'Reduces false signals during sideways conditions',
        'Works well as a regime detector',
      ],
      recommendedTimeframes: 'All timeframes (especially good for noisy intraday)',
      dataNotes: [
        'Depends heavily on consistent candle spacing',
      ],
      relatedIndicators: ['ema', 'alma', 'zlma'],
      panelType: 'overlay',
    },
    {
      name: 'zlma',
      displayName: 'Zero Lag Moving Average (ZLMA)',
      formulaLatex: 'ZLMA = EMA(2C_t - C_{t-lag}), \\quad lag=\\frac{n-1}{2}',
      description: 'Attempts to reduce lag by compensating for the inherent delay in moving averages.',
      library: 'pandas-ta (ta.zlma)',
      outputColumns: ['zlma_{length}'],
      defaultParams: 'length = 20',
      interpretation: [
        'Faster trend signals than standard EMA',
        'Higher false positives in sideways markets',
        'Useful for short-term trend detection',
      ],
      recommendedTimeframes: '1m–1h (strong on intraday)',
      dataNotes: [
        'Requires `length` warmup bars',
      ],
      relatedIndicators: ['ema', 'dema', 'tema'],
      panelType: 'overlay',
    },
    {
      name: 'rma',
      displayName: 'Running Moving Average / Wilder Smoothing (RMA)',
      formulaLatex: 'RMA_t = \\frac{(n-1)\\cdot RMA_{t-1}+C_t}{n}',
      description: 'Wilder\'s smoothing method, used internally by ATR, RSI, and ADX.',
      library: 'pandas-ta (ta.rma)',
      outputColumns: ['rma_{length}'],
      defaultParams: 'length = 14',
      interpretation: [
        'A smoothing baseline for volatility and momentum indicators',
        'Slightly slower than EMA with equivalent period',
      ],
      recommendedTimeframes: 'All timeframes',
      dataNotes: [
        'Requires `length` warmup bars',
      ],
      relatedIndicators: ['ema', 'atr', 'rsi'],
      panelType: 'overlay',
    },
    {
      name: 'alma',
      displayName: 'Arnaud Legoux Moving Average (ALMA)',
      formulaLatex: '\\text{ALMA}(n) = \\sum_{i=0}^{n-1} w_i \\cdot C_{t-i}, \\quad w_i = e^{-\\frac{(i - m)^2}{2s^2}}',
      description: 'Uses a Gaussian distribution weighting function for smoothing. Very smooth and low-lag.',
      library: 'pandas-ta (ta.alma)',
      outputColumns: ['alma_{length}'],
      defaultParams: 'length = 20, offset = 0.85, sigma = 6',
      interpretation: [
        'Very smooth and low-lag moving average',
        'Useful for signal extraction in medium-term trend systems',
        'Offset controls responsiveness, sigma controls smoothness',
      ],
      recommendedTimeframes: '15m–1D+ (excellent on daily)',
      dataNotes: [
        'Requires `length` warmup bars',
      ],
      relatedIndicators: ['ema', 'kama', 'hma'],
      panelType: 'overlay',
    },
    {
      name: 'bbands',
      displayName: 'Bollinger Bands',
      formulaLatex: '\\text{Mid} = \\text{SMA}(C, n), \\quad \\text{Upper} = \\text{Mid} + k\\sigma_n, \\quad \\text{Lower} = \\text{Mid} - k\\sigma_n',
      description: 'Measures volatility with bands k standard deviations around an SMA. Outputs 5 columns: lower band, mid (basis), upper band, bandwidth, and %B (percent B).',
      library: 'pandas-ta (ta.bbands)',
      outputColumns: ['bbl_{n}_{k}', 'bbm_{n}_{k}', 'bbu_{n}_{k}', 'bbb_{n}_{k}', 'bbp_{n}_{k}'],
      defaultParams: 'length = 20, std = 2.0',
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
      relatedIndicators: ['kc', 'squeeze', 'sma'],
      panelType: 'overlay',
    },
    {
      name: 'supertrend',
      displayName: 'Supertrend',
      formulaLatex: '\\text{Up} = \\frac{H+L}{2} + m \\cdot \\text{ATR}(n), \\quad \\text{Down} = \\frac{H+L}{2} - m \\cdot \\text{ATR}(n)',
      description: 'Trend-following indicator based on ATR. Flips between support (uptrend) and resistance (downtrend). Outputs 4 columns: trend value, direction (1/-1), long (support), short (resistance). ATR uses RMA smoothing.',
      library: 'pandas-ta (ta.supertrend)',
      outputColumns: ['supert_{n}_{m}', 'supertd_{n}_{m}', 'supertl_{n}_{m}', 'superts_{n}_{m}'],
      defaultParams: 'length = 10, multiplier = 3.0',
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
      relatedIndicators: ['atr', 'psar', 'ema'],
      panelType: 'overlay',
    },
    {
      name: 'vwap',
      displayName: 'Volume Weighted Average Price (VWAP)',
      formulaLatex: 'VWAP = \\frac{\\sum (TP \\cdot V)}{\\sum V}, \\quad TP = \\frac{H+L+C}{3}',
      description: 'Represents the "fair value" price weighted by volume. Must reset at session boundary (daily).',
      library: 'pandas-ta (ta.vwap)',
      outputColumns: ['vwap'],
      defaultParams: 'Session-based reset (implicit)',
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
      relatedIndicators: ['ad', 'cmf', 'mfi'],
      panelType: 'overlay',
    },
    {
      name: 'psar',
      displayName: 'Parabolic SAR',
      formulaLatex: '\\text{SAR}_{t+1} = \\text{SAR}_t + \\text{AF} \\cdot (\\text{EP} - \\text{SAR}_t)',
      description: 'Trailing stop-and-reverse system. Dots above price = downtrend, below = uptrend. AF accelerates toward the extreme point (EP).',
      library: 'pandas-ta (ta.psar)',
      outputColumns: ['psarl_{af}_{max}', 'psars_{af}_{max}', 'psaraf_{af}_{max}', 'psarr_{af}_{max}'],
      defaultParams: 'af0 = 0.02, af = 0.02, max_af = 0.2',
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
      relatedIndicators: ['supertrend', 'atr', 'adx'],
      panelType: 'overlay',
    },
    {
      name: 'kc',
      displayName: 'Keltner Channel',
      formulaLatex: '\\text{Mid} = EMA(C, n), \\quad \\text{Upper} = Mid + k\\cdot ATR(n), \\quad \\text{Lower} = Mid - k\\cdot ATR(n)',
      description: 'Volatility bands based on ATR, often smoother than Bollinger Bands.',
      library: 'pandas-ta (ta.kc)',
      outputColumns: ['kcl_{n}_{k}', 'kcb_{n}_{k}', 'kcu_{n}_{k}'],
      defaultParams: 'length = 20, scalar = 1.5',
      interpretation: [
        'Break above upper band → strong bullish expansion',
        'Inside channel → neutral consolidation',
        'Channel slope indicates trend direction',
      ],
      recommendedTimeframes: '5m–1D (excellent trend channel on 4h–1D)',
      dataNotes: [
        'Requires `length` warmup for both EMA and ATR',
      ],
      relatedIndicators: ['bbands', 'atr', 'squeeze'],
      panelType: 'overlay',
    },
    {
      name: 'donchian',
      displayName: 'Donchian Channel',
      formulaLatex: '\\text{Upper} = \\max(H_{t-n+1},...,H_t), \\quad \\text{Lower} = \\min(L_{t-n+1},...,L_t), \\quad \\text{Mid} = \\frac{Upper + Lower}{2}',
      description: 'Represents the highest high and lowest low over a rolling window. Used in Turtle Trading systems.',
      library: 'pandas-ta (ta.donchian)',
      outputColumns: ['dcl_{n}', 'dcm_{n}', 'dcu_{n}'],
      defaultParams: 'length = 20',
      interpretation: [
        'Breakout above upper channel → trend continuation / new trend',
        'Breakout below lower channel → bearish breakout',
        'Common in Turtle Trading systems',
      ],
      recommendedTimeframes: '1h–1W (excellent breakout indicator on 4h–1D)',
      dataNotes: [
        'Requires `length` warmup bars',
      ],
      relatedIndicators: ['kc', 'bbands', 'atr'],
      panelType: 'overlay',
    },

    // ═══════════════════════════════════════════════════════════
    //  SUB-PANEL INDICATORS
    // ═══════════════════════════════════════════════════════════
    {
      name: 'macd',
      displayName: 'MACD (Moving Average Convergence Divergence)',
      formulaLatex: '\\text{MACD} = \\text{EMA}(C, f) - \\text{EMA}(C, s), \\quad \\text{Signal} = \\text{EMA}(\\text{MACD}, g), \\quad \\text{Hist} = \\text{MACD} - \\text{Signal}',
      description: 'Captures the relationship between two EMAs. Outputs 3 columns: MACD line, histogram, and signal line. Rising histogram = strengthening momentum.',
      library: 'pandas-ta (ta.macd)',
      outputColumns: ['macd_{f}_{s}_{g}', 'macdh_{f}_{s}_{g}', 'macds_{f}_{s}_{g}'],
      defaultParams: 'fast = 12, slow = 26, signal = 9',
      interpretation: [
        'MACD crossing above signal → bullish',
        'MACD crossing below signal → bearish',
        'Histogram rising → momentum increasing',
        'Divergence between price and MACD → potential reversal',
      ],
      recommendedTimeframes: '15m–1D (excellent on 1h–4h)',
      dataNotes: [
        'Requires `slow` warmup bars before producing valid values',
      ],
      relatedIndicators: ['ema', 'rsi', 'tsi'],
      panelType: 'sub-panel',
    },
    {
      name: 'rsi',
      displayName: 'Relative Strength Index (RSI)',
      formulaLatex: '\\text{RSI} = 100 - \\frac{100}{1 + \\frac{\\text{AvgGain}(n)}{\\text{AvgLoss}(n)}}',
      description: 'Measures speed and magnitude of price changes. Uses Wilder\'s RMA smoothing by default. Values above 70 = overbought, below 30 = oversold.',
      library: 'pandas-ta (ta.rsi, mamode="rma")',
      outputColumns: ['rsi_{n}'],
      defaultParams: 'length = 14',
      interpretation: [
        'RSI > 70 → overbought (potential reversal or strong uptrend)',
        'RSI < 30 → oversold (potential reversal or strong downtrend)',
        'RSI divergence from price signals weakening momentum',
        'Centerline (50) crossover used as trend filter',
      ],
      recommendedTimeframes: '5m–1D (excellent on all common timeframes)',
      dataNotes: [
        'Requires `length` warmup bars (Wilder smoothing)',
      ],
      relatedIndicators: ['stochrsi', 'mfi', 'cci'],
      panelType: 'sub-panel',
    },
    {
      name: 'adx',
      displayName: 'Average Directional Index (ADX)',
      formulaLatex: '\\text{ADX} = \\text{RMA}\\!\\left(\\frac{|{+DI} - {-DI}|}{+DI + {-DI}} \\times 100,\\; n\\right)',
      description: 'Measures trend strength (not direction). Outputs ADX, +DI, and -DI. Uses tvmode=True for TradingView compatibility. ADX > 25 = strong trend.',
      library: 'pandas-ta (ta.adx, tvmode=True)',
      outputColumns: ['adx_{n}', 'dmp_{n}', 'dmn_{n}'],
      defaultParams: 'length = 14',
      interpretation: [
        'ADX > 25 → strong trend',
        'ADX < 20 → weak/no trend (range-bound)',
        '+DI above -DI → bullish pressure',
        '-DI above +DI → bearish pressure',
      ],
      recommendedTimeframes: '1h–1D (excellent for trend strength confirmation)',
      dataNotes: [
        'Requires ~2x `length` warmup bars due to nested smoothing',
      ],
      relatedIndicators: ['atr', 'rsi', 'supertrend'],
      panelType: 'sub-panel',
    },
    {
      name: 'atr',
      displayName: 'Average True Range (ATR)',
      formulaLatex: '\\text{TR} = \\max(H-L,\\; |H - C_{t-1}|,\\; |L - C_{t-1}|), \\quad \\text{ATR} = \\text{RMA}(\\text{TR}, n)',
      description: 'Measures volatility using the true range of each bar. ATR is the RMA-smoothed average of true range. Used internally by Supertrend and other indicators.',
      library: 'pandas-ta (ta.atr)',
      outputColumns: ['atr_{n}'],
      defaultParams: 'length = 14',
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
      relatedIndicators: ['natr', 'supertrend', 'kc'],
      panelType: 'sub-panel',
    },
    {
      name: 'stoch',
      displayName: 'Stochastic Oscillator',
      formulaLatex: '\\%K = \\frac{C - L_n}{H_n - L_n} \\times 100, \\quad \\%D = \\text{SMA}(\\%K, d)',
      description: 'Measures where the close sits in the recent high-low range. %K is the fast line, %D is the signal. Outputs 2 columns.',
      library: 'pandas-ta (ta.stoch)',
      outputColumns: ['stochk_{k}_{d}', 'stochd_{k}_{d}'],
      defaultParams: 'k = 14, d = 3',
      interpretation: [
        'K/D above 80 → overbought',
        'K/D below 20 → oversold',
        '%K crossing above %D → bullish signal',
        '%K crossing below %D → bearish signal',
      ],
      recommendedTimeframes: '5m–4h (strong intraday oscillator)',
      dataNotes: [
        'Requires `k` warmup bars',
      ],
      relatedIndicators: ['stochrsi', 'rsi', 'willr'],
      panelType: 'sub-panel',
    },
    {
      name: 'stochrsi',
      displayName: 'Stochastic RSI',
      formulaLatex: '\\text{StochRSI}=\\frac{RSI-\\min(RSI)}{\\max(RSI)-\\min(RSI)}',
      description: 'Applies a stochastic oscillator calculation to RSI, producing a faster oscillator. Outputs K and D lines.',
      library: 'pandas-ta (ta.stochrsi)',
      outputColumns: ['stochrsi_k_{n}', 'stochrsi_d_{n}'],
      defaultParams: 'length = 14, rsi_length = 14, k = 3, d = 3',
      interpretation: [
        'Values near 0 → oversold',
        'Values near 1 → overbought',
        'K/D crossovers generate signals',
        'Faster than regular RSI — more signals but more noise',
      ],
      recommendedTimeframes: '1m–4h (very strong but high signal frequency on lower timeframes)',
      dataNotes: [
        'Requires `length + rsi_length` warmup bars',
      ],
      relatedIndicators: ['rsi', 'stoch', 'willr'],
      panelType: 'sub-panel',
    },
    {
      name: 'obv',
      displayName: 'On Balance Volume (OBV)',
      formulaLatex: '\\text{OBV}_t = \\text{OBV}_{t-1} + \\begin{cases} V_t & C_t > C_{t-1} \\\\ -V_t & C_t < C_{t-1} \\\\ 0 & \\text{otherwise} \\end{cases}',
      description: 'Cumulative volume indicator. Rising OBV confirms uptrend; divergence between OBV and price signals potential reversal. No configurable parameters.',
      library: 'pandas-ta (ta.obv)',
      outputColumns: ['obv'],
      defaultParams: 'None',
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
      relatedIndicators: ['ad', 'cmf', 'mfi'],
      panelType: 'sub-panel',
    },
    {
      name: 'cci',
      displayName: 'Commodity Channel Index (CCI)',
      formulaLatex: '\\text{CCI} = \\frac{\\text{TP} - \\text{SMA}(\\text{TP}, n)}{0.015 \\cdot \\text{MAD}(\\text{TP}, n)}, \\quad \\text{TP} = \\frac{H+L+C}{3}',
      description: 'Measures deviation from the statistical mean. Values above +100 = overbought, below -100 = oversold.',
      library: 'pandas-ta (ta.cci)',
      outputColumns: ['cci_{n}'],
      defaultParams: 'length = 14',
      interpretation: [
        'CCI > +100 → overbought / strong bullish momentum',
        'CCI < -100 → oversold / strong bearish momentum',
        'Zero-line crossover used as trend filter',
        'Divergence with price signals weakening momentum',
      ],
      recommendedTimeframes: '15m–1D (strong on 1h–4h)',
      dataNotes: [
        'Requires `length` warmup bars',
      ],
      relatedIndicators: ['rsi', 'stoch', 'mfi'],
      panelType: 'sub-panel',
    },
    {
      name: 'willr',
      displayName: 'Williams %R',
      formulaLatex: '\\%R = -100\\cdot\\frac{HH(n)-C}{HH(n)-LL(n)}',
      description: 'Momentum oscillator measuring close location relative to recent high-low range.',
      library: 'pandas-ta (ta.willr)',
      outputColumns: ['willr_{n}'],
      defaultParams: 'length = 14',
      interpretation: [
        '-20 to 0 → overbought',
        '-80 to -100 → oversold',
        'Useful for mean reversion and turning points',
        'Essentially the inverse of the Stochastic oscillator',
      ],
      recommendedTimeframes: '1m–4h (strong intraday)',
      dataNotes: [
        'Requires `length` warmup bars',
      ],
      relatedIndicators: ['stoch', 'stochrsi', 'rsi'],
      panelType: 'sub-panel',
    },
    {
      name: 'roc',
      displayName: 'Rate of Change (ROC)',
      formulaLatex: '\\text{ROC}=\\frac{C_t-C_{t-n}}{C_{t-n}}\\times 100',
      description: 'Measures the percentage change from n bars ago.',
      library: 'pandas-ta (ta.roc)',
      outputColumns: ['roc_{n}'],
      defaultParams: 'length = 12',
      interpretation: [
        'ROC > 0 → bullish momentum',
        'ROC < 0 → bearish momentum',
        'ROC crossing above 0 often signals trend initiation',
      ],
      recommendedTimeframes: '15m–1D (all timeframes, particularly useful on 1h)',
      dataNotes: [
        'Requires `length` warmup bars',
      ],
      relatedIndicators: ['mom', 'rsi', 'tsi'],
      panelType: 'sub-panel',
    },
    {
      name: 'mom',
      displayName: 'Momentum (MOM)',
      formulaLatex: '\\text{MOM}=C_t-C_{t-n}',
      description: 'Absolute price change over n periods. Measures raw speed of movement.',
      library: 'pandas-ta (ta.mom)',
      outputColumns: ['mom_{n}'],
      defaultParams: 'length = 10',
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
      relatedIndicators: ['roc', 'rsi', 'tsi'],
      panelType: 'sub-panel',
    },
    {
      name: 'natr',
      displayName: 'Normalized ATR (NATR)',
      formulaLatex: '\\text{NATR}=\\frac{ATR(n)}{C_t}\\times 100',
      description: 'Expresses ATR as a percentage of price, making volatility comparable across instruments.',
      library: 'pandas-ta (ta.natr)',
      outputColumns: ['natr_{n}'],
      defaultParams: 'length = 14',
      interpretation: [
        'Rising NATR → volatility expansion',
        'Useful for risk sizing across different-priced instruments',
        'Can be used as a regime detector (high vol vs low vol)',
      ],
      recommendedTimeframes: 'All timeframes (very useful intraday for risk control)',
      dataNotes: [
        'Requires `length` warmup bars',
      ],
      relatedIndicators: ['atr', 'bbands', 'kc'],
      panelType: 'sub-panel',
    },
    {
      name: 'ad',
      displayName: 'Accumulation/Distribution Line (AD)',
      formulaLatex: 'CLV=\\frac{(C-L)-(H-C)}{H-L}, \\quad AD_t=AD_{t-1} + CLV\\cdot V',
      description: 'Estimates buying vs selling pressure using price location within the bar combined with volume.',
      library: 'pandas-ta (ta.ad)',
      outputColumns: ['ad'],
      defaultParams: 'None (cumulative)',
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
      relatedIndicators: ['obv', 'cmf', 'mfi'],
      panelType: 'sub-panel',
    },
    {
      name: 'cmf',
      displayName: 'Chaikin Money Flow (CMF)',
      formulaLatex: 'CMF(n)=\\frac{\\sum_{i=0}^{n-1}(CLV_i\\cdot V_i)}{\\sum_{i=0}^{n-1}V_i}',
      description: 'Measures accumulation/distribution over a rolling window.',
      library: 'pandas-ta (ta.cmf)',
      outputColumns: ['cmf_{n}'],
      defaultParams: 'length = 20',
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
      relatedIndicators: ['ad', 'obv', 'mfi'],
      panelType: 'sub-panel',
    },
    {
      name: 'mfi',
      displayName: 'Money Flow Index (MFI)',
      formulaLatex: 'TP=\\frac{H+L+C}{3}, \\quad MF=TP\\cdot V, \\quad MFI = 100 - \\frac{100}{1 + \\frac{\\sum MF^+}{\\sum MF^-}}',
      description: 'RSI-like oscillator but volume-weighted. Often called "volume RSI".',
      library: 'pandas-ta (ta.mfi)',
      outputColumns: ['mfi_{n}'],
      defaultParams: 'length = 14',
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
      relatedIndicators: ['rsi', 'cmf', 'obv'],
      panelType: 'sub-panel',
    },
    {
      name: 'tsi',
      displayName: 'True Strength Index (TSI)',
      formulaLatex: 'TSI = 100\\cdot \\frac{EMA(EMA(MOM, r), s)}{EMA(EMA(|MOM|, r), s)}',
      description: 'Smooth momentum oscillator designed to reduce noise and highlight trend momentum. Outputs TSI line and signal line.',
      library: 'pandas-ta (ta.tsi)',
      outputColumns: ['tsi_{fast}_{slow}_{signal}', 'tsis_{fast}_{slow}_{signal}'],
      defaultParams: 'fast = 13, slow = 25, signal = 13',
      interpretation: [
        'TSI > 0 → bullish momentum',
        'TSI < 0 → bearish momentum',
        'Signal line crossovers are common entry triggers',
        'Divergence with price indicates weakening trend',
      ],
      recommendedTimeframes: '1h–1D (excellent; moderate on 5m–15m)',
      dataNotes: [
        'Requires `slow + signal` warmup bars due to double smoothing',
      ],
      relatedIndicators: ['macd', 'rsi', 'roc'],
      panelType: 'sub-panel',
    },
    {
      name: 'fisher',
      displayName: 'Fisher Transform',
      formulaLatex: 'x=0.33\\cdot 2\\cdot\\left(\\frac{C-\\min(C)}{\\max(C)-\\min(C)}-0.5\\right)+0.67x_{t-1}, \\quad Fisher = 0.5\\cdot \\ln\\left(\\frac{1+x}{1-x}\\right)',
      description: 'Converts price into a near-Gaussian distribution, amplifying turning points.',
      library: 'pandas-ta (ta.fisher)',
      outputColumns: ['fisher_{n}', 'fishers_{n}'],
      defaultParams: 'length = 9',
      interpretation: [
        'Fisher crossing above signal → bullish reversal',
        'Fisher crossing below signal → bearish reversal',
        'Often used for cycle detection',
        'Sharp peaks/troughs indicate strong turning points',
      ],
      recommendedTimeframes: '15m–4h (strong); too noisy on 1m–5m',
      dataNotes: [
        'Requires `length` warmup bars',
      ],
      relatedIndicators: ['stoch', 'rsi', 'willr'],
      panelType: 'sub-panel',
    },
    {
      name: 'squeeze',
      displayName: 'Volatility Squeeze',
      formulaLatex: 'BB_{upper} < KC_{upper} \\;\\text{and}\\; BB_{lower} > KC_{lower} \\Rightarrow \\text{Squeeze ON}',
      description: 'Detects when Bollinger Bands contract inside Keltner Channels — a volatility compression phase that often precedes breakouts.',
      library: 'pandas-ta (ta.squeeze)',
      outputColumns: ['squeeze_{n}'],
      defaultParams: 'length = 20',
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
      relatedIndicators: ['bbands', 'kc', 'atr'],
      panelType: 'sub-panel',
    },
  ];

  overlayIndicators = this.allIndicators.filter(i => i.panelType === 'overlay');
  subPanelIndicators = this.allIndicators.filter(i => i.panelType === 'sub-panel');

  csvBaseColumns = [
    { column: 'unix_ts', type: 'int', description: 'Unix timestamp in milliseconds (UTC)' },
    { column: 'iso_time', type: 'string', description: 'ISO 8601 datetime string (UTC)' },
    { column: 'open', type: 'float', description: 'Opening price of the minute bar' },
    { column: 'high', type: 'float', description: 'Highest price during the minute bar' },
    { column: 'low', type: 'float', description: 'Lowest price during the minute bar' },
    { column: 'close', type: 'float', description: 'Closing price of the minute bar' },
    { column: 'volume', type: 'float', description: 'Shares traded during the minute bar' },
    { column: 'vwap', type: 'float', description: 'Volume-weighted average price' },
    { column: 'transactions', type: 'int', description: 'Number of transactions' },
  ];

  validationNotes = [
    'All float values are rounded to 6 decimal places for consistency',
    'Empty cells represent NaN — indicator warm-up period or insufficient data',
    'Timestamps represent the start of each 1-minute bar (bar-open convention)',
    'Data is de-duplicated by timestamp and sorted chronologically',
    'Polygon returns consolidated tape data (not exchange-specific)',
    'Date range is chunked into ~111-day windows to stay within 50,000-bar API limit',
    'The metadata JSON file describes every column, its source, library, and parameters',
  ];

  dataCaveats = [
    { icon: 'pi-clock', label: 'Warmup Bars', text: 'Most indicators require `length` bars before producing valid values. Earlier rows will be NaN.' },
    { icon: 'pi-exclamation-triangle', label: 'Missing Bars', text: 'EMA-like indicators drift if missing candles exist in the data.' },
    { icon: 'pi-replay', label: 'Session Reset', text: 'VWAP must reset at session boundary (daily). Multi-day VWAP is not standard.' },
    { icon: 'pi-chart-bar', label: 'Volume Dependency', text: 'VWAP, AD, CMF, MFI, OBV require reliable volume data. Zero-volume bars make these indicators unreliable.' },
    { icon: 'pi-sync', label: 'Resampling', text: 'Ensure OHLCV resample logic is consistent with TradingView (especially volume aggregation).' },
    { icon: 'pi-moon', label: 'RTH vs Extended', text: 'Volume-based indicators behave very differently in extended hours due to thin volume.' },
  ];

  hasCriticalCaveat(ind: IndicatorDoc): boolean {
    return ind.dataNotes.some(n =>
      n.includes('Volume-dependent') ||
      n.includes('session boundary') ||
      n.includes('drift heavily') ||
      n.includes('extended hours')
    );
  }

  getDisplayName(name: string): string {
    const ind = this.allIndicators.find(i => i.name === name);
    return ind?.displayName ?? name.toUpperCase();
  }

  scrollToIndicator(name: string): void {
    const el = this.el.nativeElement.querySelector(`#ind-${name}`);
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }
}
