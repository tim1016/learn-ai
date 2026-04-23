import { Component, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterModule } from '@angular/router';
import { Accordion, AccordionContent, AccordionHeader, AccordionPanel } from 'primeng/accordion';
import { KatexDirective } from '../../../shared/katex.directive';
import { PageHeaderComponent } from '../../../shared/page-header/page-header.component';

interface IndicatorDoc {
  name: string;
  formulaLatex: string;
  description: string;
  parameters: { name: string; default: string; description: string }[];
  columns: string[];
}

interface ParameterDoc {
  name: string;
  description: string;
  defaultValue: string;
  range: string;
}

@Component({
  selector: 'app-indicator-docs',
  standalone: true,
  imports: [CommonModule, RouterModule, Accordion, AccordionContent, AccordionHeader, AccordionPanel, KatexDirective, PageHeaderComponent],
  templateUrl: './indicator-docs.component.html',
  styleUrls: ['./indicator-docs.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class IndicatorDocsComponent {

  globalParameters: ParameterDoc[] = [
    { name: 'Ticker', description: 'Asset symbol to fetch data for', defaultValue: 'SPY', range: 'Any US equity' },
    { name: 'From Date', description: 'Start of the historical range', defaultValue: '—', range: 'Within 2-year Polygon limit' },
    { name: 'To Date', description: 'End of the historical range', defaultValue: '—', range: 'Within 2-year Polygon limit' },
    { name: 'Multiplier', description: 'Bar size multiplier', defaultValue: '1', range: '1+' },
    { name: 'Timespan', description: 'Bar resolution', defaultValue: 'minute', range: 'minute, hour, day' },
  ];

  indicators: IndicatorDoc[] = [
    {
      name: 'Exponential Moving Average (EMA)',
      formulaLatex: '\\text{EMA}_t = \\alpha \\cdot C_t + (1 - \\alpha) \\cdot \\text{EMA}_{t-1}, \\quad \\alpha = \\frac{2}{n + 1}',
      description: 'The EMA gives more weight to recent prices, making it more responsive to new information than a Simple Moving Average. Shorter periods (5, 10) track price closely for short-term momentum; longer periods (100, 200) identify major trend direction. The table calculates EMAs for 8 periods simultaneously, allowing crossover and multi-timeframe analysis.',
      parameters: [
        { name: 'EMA Periods', default: '[5, 10, 20, 30, 40, 50, 100, 200]', description: 'List of lookback periods to calculate' },
      ],
      columns: ['ema_5', 'ema_10', 'ema_20', 'ema_30', 'ema_40', 'ema_50', 'ema_100', 'ema_200'],
    },
    {
      name: 'Bollinger Bands',
      formulaLatex: '\\text{Basis} = \\text{SMA}(C, n), \\quad \\text{Upper} = \\text{Basis} + k \\cdot \\sigma_n, \\quad \\text{Lower} = \\text{Basis} - k \\cdot \\sigma_n',
      description: 'Bollinger Bands measure volatility by placing bands k standard deviations above and below a moving average. When price touches the upper band, the asset may be overbought; when it touches the lower band, oversold. Band width (upper minus lower) is a direct measure of current volatility — narrow bands ("squeeze") often precede large moves.',
      parameters: [
        { name: 'BB Length', default: '20', description: 'SMA lookback period for the basis line' },
        { name: 'BB Std', default: '2.0', description: 'Number of standard deviations for band width' },
      ],
      columns: ['bb_basis', 'bb_upper', 'bb_lower'],
    },
    {
      name: 'Supertrend',
      formulaLatex: '\\text{ATR}(n) = \\text{RMA}(\\text{TR}, n), \\quad \\text{Up} = \\frac{H + L}{2} + m \\cdot \\text{ATR}(n), \\quad \\text{Down} = \\frac{H + L}{2} - m \\cdot \\text{ATR}(n)',
      description: 'Supertrend is a trend-following indicator built on Average True Range (ATR). It plots a single line that flips between support (below price in uptrend) and resistance (above price in downtrend). When price closes above the upper band, direction flips to bullish (1); when it closes below the lower band, direction flips to bearish (-1). The output is split into two columns: supertrend_up (populated during uptrends) and supertrend_down (during downtrends).',
      parameters: [
        { name: 'Supertrend Length', default: '10', description: 'ATR lookback period' },
        { name: 'Supertrend Multiplier', default: '3.0', description: 'ATR multiplier for band distance' },
      ],
      columns: ['supertrend_up', 'supertrend_down'],
    },
    {
      name: 'Relative Strength Index (RSI)',
      formulaLatex: '\\text{RSI}(n) = 100 - \\frac{100}{1 + \\frac{\\text{AvgGain}(n)}{\\text{AvgLoss}(n)}}',
      description: 'RSI measures the speed and magnitude of recent price changes to evaluate overbought (>70) or oversold (<30) conditions. AvgGain and AvgLoss use Wilder\'s smoothing method (exponential moving average with alpha = 1/n). An RSI Moving Average (SMA of RSI values) is also calculated to identify RSI trend direction and generate crossover signals.',
      parameters: [
        { name: 'RSI Length', default: '14', description: 'Lookback period for RSI calculation' },
        { name: 'RSI MA Length', default: '14', description: 'SMA period applied to RSI values' },
      ],
      columns: ['rsi', 'rsi_ma'],
    },
    {
      name: 'MACD (Moving Average Convergence Divergence)',
      formulaLatex: '\\text{MACD} = \\text{EMA}(C, f) - \\text{EMA}(C, s), \\quad \\text{Signal} = \\text{EMA}(\\text{MACD}, g), \\quad \\text{Hist} = \\text{MACD} - \\text{Signal}',
      description: 'MACD captures the relationship between two EMAs. When the MACD line crosses above the signal line, it suggests bullish momentum; below suggests bearish. The histogram visualizes the distance between MACD and signal — rising histogram bars indicate strengthening momentum. Default parameters (12, 26, 9) are the most widely used and match TradingView defaults.',
      parameters: [
        { name: 'MACD Fast', default: '12', description: 'Fast EMA period (f)' },
        { name: 'MACD Slow', default: '26', description: 'Slow EMA period (s)' },
        { name: 'MACD Signal', default: '9', description: 'Signal line EMA period (g)' },
      ],
      columns: ['macd', 'macd_signal', 'macd_histogram'],
    },
    {
      name: 'Average Directional Index (ADX)',
      formulaLatex: '\\text{ADX}(n) = \\text{RMA}\\left(\\frac{|+\\text{DI} - (-\\text{DI})|}{+\\text{DI} + (-\\text{DI})} \\times 100,\\; n\\right)',
      description: 'ADX measures trend strength regardless of direction. Values above 25 indicate a strong trend; below 20 suggest a range-bound market. +DI and -DI (Directional Indicators) are derived from comparing consecutive highs and lows. ADX itself only measures how strong the trend is — combine with +DI/-DI or price direction to determine bullish vs bearish.',
      parameters: [
        { name: 'ADX Length', default: '14', description: 'Smoothing period for DI and ADX' },
      ],
      columns: ['adx'],
    },
  ];

  comparisonThresholds = [
    { className: 'match-exact', range: '< 0.001%', meaning: 'Exact match — values are effectively identical' },
    { className: 'match-close', range: '< 0.01%', meaning: 'Close match — negligible floating-point difference' },
    { className: 'match-ok', range: '< 0.1%', meaning: 'Acceptable — minor difference, likely rounding' },
    { className: 'match-bad', range: '> 0.1%', meaning: 'Significant divergence — investigate calculation difference' },
  ];

  divergenceCauses = [
    { cause: 'Polygon 07:00 ET bar contamination', detail: 'Polygon includes late-reported settlement trades in minute aggregates around 07:00-07:02 ET, inflating close prices by $4-6 on some bars. TradingView filters these out. This is the primary divergence source — it poisons all downstream EMAs, with longer periods recovering more slowly. Polygon volume at 07:00 ET can be 9-14x higher than TradingView\'s for the same minute.' },
    { cause: 'OHLCV source data feed', detail: 'Polygon provides consolidated tape data while TradingView uses Cboe BZX composite feeds. Even outside the 07:00 window, close prices may differ by $0.01 due to different trade-condition filters and last-sale rules.' },
    { cause: 'Warm-up period', detail: 'The system fetches 5x the max lookback period as warm-up bars before the requested window. For EMA-200, this means ~1000 extra bars are fetched and discarded. Without warm-up, recursive indicators produce unstable values. Investigation confirmed warm-up depth is sufficient — EMAs match TradingView within 0.0001% at the start of the requested window.' },
    { cause: 'Smoothing method', detail: 'pandas-ta uses Wilder\'s smoothing (RMA) for RSI and ATR by default, matching TradingView Pine Script. ADX uses tvmode=True for TradingView compatibility. EMA uses presma=True (SMA seed) and adjust=False (recursive formula), both matching TradingView exactly.' },
    { cause: 'Floating-point precision', detail: 'Values are rounded to 6 decimal places. Differences below 1e-6 are expected and should appear as match-exact or match-close.' },
    { cause: 'Supertrend direction flip', detail: 'The exact bar where Supertrend flips direction can differ by 1 bar due to close-vs-next-open comparison timing.' },
  ];

  pandasTaCategories = [
    { name: 'Overlap (36)', examples: 'SMA, EMA, DEMA, TEMA, WMA, HMA, KAMA, Bollinger Bands, Supertrend, Ichimoku, VWAP, Parabolic SAR, ALMA, ZLMA' },
    { name: 'Momentum (43)', examples: 'RSI, MACD, Stochastic, CCI, Williams %R, ROC, MFI, TSI, KDJ, Fisher, Squeeze, Coppock, Inertia' },
    { name: 'Volatility (16)', examples: 'ATR, NATR, Bollinger Bands, Keltner Channel, Donchian, True Range, Chandelier Exit, RVI' },
    { name: 'Volume (19)', examples: 'OBV, AD, CMF, MFI, VWAP, VWMA, EFI, KVO, PVT, NVI, PVI' },
    { name: 'Trend (20)', examples: 'ADX, Aroon, PSAR, Choppiness, Vortex, AlphaTrend, ZigZag, DPO, VHF' },
    { name: 'Statistics (10)', examples: 'Standard Deviation, Variance, Z-Score, Skew, Kurtosis, Entropy, MAD' },
    { name: 'Performance (2)', examples: 'Log Return, Percent Return' },
  ];
}
