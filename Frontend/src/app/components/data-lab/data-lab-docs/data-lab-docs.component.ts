import { Component, ChangeDetectionStrategy } from '@angular/core';
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

  defaultIndicators: IndicatorDoc[] = [
    {
      name: 'ema',
      displayName: 'Exponential Moving Average (EMA)',
      formulaLatex: '\\text{EMA}_t = \\alpha \\cdot C_t + (1 - \\alpha) \\cdot \\text{EMA}_{t-1}, \\quad \\alpha = \\frac{2}{n + 1}',
      description: 'Gives more weight to recent prices. Default setup calculates 8 EMAs (5, 10, 20, 30, 40, 50, 100, 200) for multi-timeframe analysis. Each EMA instance adds one column.',
      library: 'pandas-ta (ta.ema)',
      outputColumns: ['ema_{length}'],
      defaultParams: 'length = 5, 10, 20, 30, 40, 50, 100, 200',
    },
    {
      name: 'bbands',
      displayName: 'Bollinger Bands',
      formulaLatex: '\\text{Mid} = \\text{SMA}(C, n), \\quad \\text{Upper} = \\text{Mid} + k\\sigma_n, \\quad \\text{Lower} = \\text{Mid} - k\\sigma_n',
      description: 'Measures volatility with bands k standard deviations around an SMA. Outputs 5 columns: lower band, mid (basis), upper band, bandwidth, and %B (percent B).',
      library: 'pandas-ta (ta.bbands)',
      outputColumns: ['bbl_{n}_{k}', 'bbm_{n}_{k}', 'bbu_{n}_{k}', 'bbb_{n}_{k}', 'bbp_{n}_{k}'],
      defaultParams: 'length = 20, std = 2.0',
    },
    {
      name: 'supertrend',
      displayName: 'Supertrend',
      formulaLatex: '\\text{Up} = \\frac{H+L}{2} + m \\cdot \\text{ATR}(n), \\quad \\text{Down} = \\frac{H+L}{2} - m \\cdot \\text{ATR}(n)',
      description: 'Trend-following indicator based on ATR. Flips between support (uptrend) and resistance (downtrend). Outputs 4 columns: trend value, direction (1/-1), long (support), short (resistance). ATR uses RMA smoothing.',
      library: 'pandas-ta (ta.supertrend)',
      outputColumns: ['supert_{n}_{m}', 'supertd_{n}_{m}', 'supertl_{n}_{m}', 'superts_{n}_{m}'],
      defaultParams: 'length = 10, multiplier = 3.0',
    },
    {
      name: 'macd',
      displayName: 'MACD (Moving Average Convergence Divergence)',
      formulaLatex: '\\text{MACD} = \\text{EMA}(C, f) - \\text{EMA}(C, s), \\quad \\text{Signal} = \\text{EMA}(\\text{MACD}, g), \\quad \\text{Hist} = \\text{MACD} - \\text{Signal}',
      description: 'Captures the relationship between two EMAs. Outputs 3 columns: MACD line, histogram, and signal line. Rising histogram = strengthening momentum.',
      library: 'pandas-ta (ta.macd)',
      outputColumns: ['macd_{f}_{s}_{g}', 'macdh_{f}_{s}_{g}', 'macds_{f}_{s}_{g}'],
      defaultParams: 'fast = 12, slow = 26, signal = 9',
    },
  ];

  additionalIndicators: IndicatorDoc[] = [
    {
      name: 'rsi',
      displayName: 'Relative Strength Index (RSI)',
      formulaLatex: '\\text{RSI} = 100 - \\frac{100}{1 + \\frac{\\text{AvgGain}(n)}{\\text{AvgLoss}(n)}}',
      description: 'Measures speed and magnitude of price changes. Uses Wilder\'s RMA smoothing by default. Values above 70 = overbought, below 30 = oversold.',
      library: 'pandas-ta (ta.rsi, mamode="rma")',
      outputColumns: ['rsi_{n}'],
      defaultParams: 'length = 14',
    },
    {
      name: 'adx',
      displayName: 'Average Directional Index (ADX)',
      formulaLatex: '\\text{ADX} = \\text{RMA}\\!\\left(\\frac{|{+DI} - {-DI}|}{+DI + {-DI}} \\times 100,\\; n\\right)',
      description: 'Measures trend strength (not direction). Outputs ADX, +DI, and -DI. Uses tvmode=True for TradingView compatibility. ADX > 25 = strong trend.',
      library: 'pandas-ta (ta.adx, tvmode=True)',
      outputColumns: ['adx_{n}', 'dmp_{n}', 'dmn_{n}'],
      defaultParams: 'length = 14',
    },
    {
      name: 'atr',
      displayName: 'Average True Range (ATR)',
      formulaLatex: '\\text{TR} = \\max(H-L,\\; |H - C_{t-1}|,\\; |L - C_{t-1}|), \\quad \\text{ATR} = \\text{RMA}(\\text{TR}, n)',
      description: 'Measures volatility using the true range of each bar. ATR is the RMA-smoothed average of true range. Used internally by Supertrend and other indicators.',
      library: 'pandas-ta (ta.atr)',
      outputColumns: ['atr_{n}'],
      defaultParams: 'length = 14',
    },
    {
      name: 'stoch',
      displayName: 'Stochastic Oscillator',
      formulaLatex: '\\%K = \\frac{C - L_n}{H_n - L_n} \\times 100, \\quad \\%D = \\text{SMA}(\\%K, d)',
      description: 'Measures where the close sits in the recent high-low range. %K is the fast line, %D is the signal. Outputs 2 columns.',
      library: 'pandas-ta (ta.stoch)',
      outputColumns: ['stochk_{k}_{d}', 'stochd_{k}_{d}'],
      defaultParams: 'k = 14, d = 3',
    },
    {
      name: 'obv',
      displayName: 'On Balance Volume (OBV)',
      formulaLatex: '\\text{OBV}_t = \\text{OBV}_{t-1} + \\begin{cases} V_t & C_t > C_{t-1} \\\\ -V_t & C_t < C_{t-1} \\\\ 0 & \\text{otherwise} \\end{cases}',
      description: 'Cumulative volume indicator. Rising OBV confirms uptrend; divergence between OBV and price signals potential reversal. No configurable parameters.',
      library: 'pandas-ta (ta.obv)',
      outputColumns: ['obv'],
      defaultParams: 'None',
    },
    {
      name: 'cci',
      displayName: 'Commodity Channel Index (CCI)',
      formulaLatex: '\\text{CCI} = \\frac{\\text{TP} - \\text{SMA}(\\text{TP}, n)}{0.015 \\cdot \\text{MAD}(\\text{TP}, n)}, \\quad \\text{TP} = \\frac{H+L+C}{3}',
      description: 'Measures deviation from the statistical mean. Values above +100 = overbought, below -100 = oversold.',
      library: 'pandas-ta (ta.cci)',
      outputColumns: ['cci_{n}'],
      defaultParams: 'length = 14',
    },
    {
      name: 'psar',
      displayName: 'Parabolic SAR',
      formulaLatex: '\\text{SAR}_{t+1} = \\text{SAR}_t + \\text{AF} \\cdot (\\text{EP} - \\text{SAR}_t)',
      description: 'Trailing stop-and-reverse system. Dots above price = downtrend, below = uptrend. AF accelerates toward the extreme point (EP).',
      library: 'pandas-ta (ta.psar)',
      outputColumns: ['psarl_{af}_{max}', 'psars_{af}_{max}', 'psaraf_{af}_{max}', 'psarr_{af}_{max}'],
      defaultParams: 'af0 = 0.02, af = 0.02, max_af = 0.2',
    },
  ];

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
}
