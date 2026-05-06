import { Component, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterModule } from '@angular/router';
import { Accordion, AccordionContent, AccordionHeader, AccordionPanel } from 'primeng/accordion';
import { KatexDirective } from '../../shared/katex.directive';
import { PageHeaderComponent } from '../../shared/page-header/page-header.component';

interface IndicatorDoc {
  name: string;
  formulaLatex: string;
  conditionLatex: string;
  description: string;
}

interface ParameterDoc {
  name: string;
  description: string;
  defaultValue: string;
  range: string;
}

interface PitfallDoc {
  title: string;
  description: string;
  mitigation: string;
}

@Component({
  selector: 'app-strategy-docs',
  standalone: true,
  imports: [CommonModule, RouterModule, Accordion, AccordionContent, AccordionHeader, AccordionPanel, KatexDirective, PageHeaderComponent],
  templateUrl: './strategy-docs.component.html',
  styleUrls: ['./strategy-docs.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class StrategyDocsComponent {

  // ── Parameters ──
  parameters: ParameterDoc[] = [
    { name: 'Ticker', description: 'Asset symbol to trade', defaultValue: 'SPY', range: 'Any US equity' },
    { name: 'Backtest Range', description: 'Historical period for the backtest', defaultValue: '2d', range: '1d – 2y (Polygon limit)' },
    { name: 'Bar Timeframe', description: 'Candle resolution (multiplier × timespan)', defaultValue: '5 min', range: '1m / 5m / 15m / 1h' },
    { name: 'RSI Length', description: 'Lookback period for RSI calculation', defaultValue: '14', range: '2 – 50' },
    { name: 'RSI Low', description: 'Lower bound of the neutral RSI regime', defaultValue: '40', range: '1 – 99' },
    { name: 'RSI High', description: 'Upper bound of the neutral RSI regime', defaultValue: '60', range: '1 – 99' },
    { name: 'Fast MA', description: 'Short-term Simple Moving Average period', defaultValue: '20', range: '2 – 200' },
    { name: 'Slow MA', description: 'Long-term Simple Moving Average period', defaultValue: '50', range: '5 – 500' },
    { name: 'Stochastic %K', description: 'Stochastic Oscillator lookback period', defaultValue: '14', range: '2 – 50' },
    { name: 'Stochastic %D', description: 'Signal line smoothing period (SMA of %K)', defaultValue: '3', range: '1 – 20' },
    { name: 'Exit Time', description: 'Minutes before market close to force exit', defaultValue: '15', range: '1 – 60' },
  ];

  // ── Indicators ──
  indicators: IndicatorDoc[] = [
    {
      name: 'Relative Strength Index (RSI)',
      formulaLatex: '\\text{RSI}(n) = 100 - \\frac{100}{1 + \\frac{\\text{AvgGain}(n)}{\\text{AvgLoss}(n)}}',
      conditionLatex: '\\text{RSI}_{\\text{low}} \\leq \\text{RSI}(n) \\leq \\text{RSI}_{\\text{high}}',
      description: 'Filters entries to occur only within a neutral momentum regime. This prevents buying into overbought conditions or catching falling knives in oversold territory. The default 40–60 band is conservative; widening to 40–70 captures more signals during moderate trends.',
    },
    {
      name: 'Moving Average Trend Filter',
      formulaLatex: '\\text{SMA}(n) = \\frac{1}{n} \\sum_{i=0}^{n-1} C_{t-i}',
      conditionLatex: '\\text{SMA}(\\text{fast}) > \\text{SMA}(\\text{slow}) \\;\\wedge\\; C_t > \\text{SMA}(\\text{fast})',
      description: 'Two conditions: (1) Fast MA above Slow MA confirms a bullish trend regime. (2) Price above Fast MA confirms the asset is currently in a pullback-recovery, not in a deep pullback. This is a state-based filter, not a crossover event, which greatly increases signal frequency.',
    },
    {
      name: 'Stochastic Oscillator',
      formulaLatex: '\\%K = \\frac{C_t - L_n}{H_n - L_n} \\times 100, \\qquad \\%D = \\text{SMA}(\\%K, d)',
      conditionLatex: '\\%K > \\%D',
      description: 'Measures where the current close sits relative to the recent high-low range. When %K > %D, short-term momentum is accelerating upward. This is a state condition (not a crossover requirement), so it triggers whenever bullish momentum is present rather than only at the moment of crossing.',
    },
  ];

  // ── Entry/Exit ──
  entryConditions = [
    { latex: '\\text{RSI}_{\\text{low}} \\leq \\text{RSI}(n) \\leq \\text{RSI}_{\\text{high}}', label: 'RSI in neutral regime' },
    { latex: '\\text{SMA}(\\text{fast}) > \\text{SMA}(\\text{slow})', label: 'Bullish trend confirmed' },
    { latex: 'C_t > \\text{SMA}(\\text{fast})', label: 'Price above fast MA' },
    { latex: '\\%K > \\%D', label: 'Bullish stochastic momentum' },
  ];

  // ── Pitfalls ──
  pitfalls: PitfallDoc[] = [
    {
      title: 'Consecutive signal firing',
      description: 'Since all conditions are state-based (not crossover-based), the strategy can trigger every bar during sustained trends. Without a position limit, this would create unlimited entries.',
      mitigation: 'The implementation enforces maximum 1 position at a time. A new entry is only allowed after the previous position has been closed.',
    },
    {
      title: 'RSI band may suppress strong moves',
      description: 'The default 40–60 band is very tight. Many of the strongest momentum moves occur when RSI is 60–75, so the strategy may filter out the best trades.',
      mitigation: 'Consider widening the RSI High parameter to 70. This captures moderate trends while still avoiding extreme overbought entries.',
    },
    {
      title: 'Indicator redundancy',
      description: 'RSI, Stochastic, and Moving Averages all measure variants of momentum/trend. Stacking three similar filters creates over-filtering and reduces trade count.',
      mitigation: 'The state-based approach (vs. crossover) mitigates this significantly. Each indicator contributes a distinct signal: RSI = regime, MA = trend, Stochastic = short-term momentum.',
    },
    {
      title: 'End-of-day forced exit cuts winners',
      description: 'Exiting 15 minutes before close means winning intraday trades that could continue are closed prematurely.',
      mitigation: 'This is the intentional design for an intraday-only strategy. No overnight risk is the explicit goal. For swing trading, remove the exit time parameter.',
    },
    {
      title: 'Timeframe sensitivity',
      description: 'Indicator behavior changes dramatically across timeframes. On 1-minute bars, noise dominates. On 15-minute bars, signals are smoother but fewer.',
      mitigation: 'Recommended sweet spot is 5-minute bars. Always compare performance across multiple timeframes to avoid overfitting to one.',
    },
    {
      title: 'No stop loss / take profit',
      description: 'The strategy relies entirely on time-based exit. A sharp adverse move during the day has no risk control mechanism.',
      mitigation: 'Future improvement: add stop loss (1 ATR) and take profit (2 ATR) parameters. Currently, the EOD exit is the only protection.',
    },
  ];

  // ── Timeframe behaviors ──
  timeframeBehaviors = [
    { timeframe: '1 min', behavior: 'Microstructure noise dominates. Many false signals.', recommendation: 'Not recommended' },
    { timeframe: '5 min', behavior: 'Good balance of signal quality and frequency.', recommendation: 'Recommended starting point' },
    { timeframe: '15 min', behavior: 'Session-level momentum. Fewer but higher-quality signals.', recommendation: 'Good for less active trading' },
    { timeframe: '1 hour', behavior: 'Too few signals for intraday with EOD exit.', recommendation: 'Use only with swing exit rules' },
  ];
}
