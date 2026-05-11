/* Static overlay/sub-panel classification per indicator name.
 *
 * The backend catalog (`/api/dataset/available`) doesn't expose this — it's a
 * UI-only fact about where the indicator renders. Overlay indicators draw on
 * the price chart (EMA, BBands, VWAP); sub-panel indicators draw in their own
 * pane below (RSI, MACD, ATR). Anything not listed defaults to 'sub'. */

export type IndicatorPane = 'overlay' | 'sub';

const OVERLAY_INDICATORS = new Set<string>([
  'ema', 'sma', 'dema', 'tema', 'wma', 'hma', 'kama', 'vwma',
  'vwap',
  'bbands', 'keltner', 'donchian',
  'supertrend', 'psar', 'ichimoku',
]);

export function paneFor(indicatorName: string): IndicatorPane {
  return OVERLAY_INDICATORS.has(indicatorName) ? 'overlay' : 'sub';
}
