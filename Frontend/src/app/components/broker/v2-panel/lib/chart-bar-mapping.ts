import type { UTCTimestamp } from 'lightweight-charts';
import type { ChartBar } from './broker-v2-panel.types';

/**
 * Map a millisecond UTC ChartBar to lightweight-charts candle data.
 *
 * Canonical implementation — every chart surface plotting a `ChartBar`
 * (the full `DualPaneChartComponent` market tape and the gallery's
 * `BotTileComponent` thin tile) imports this rather than re-deriving the
 * OHLC-string-to-number conversion and the seconds-truncated `time`.
 */
export function toCandle(bar: ChartBar): {
  time: UTCTimestamp;
  open: number;
  high: number;
  low: number;
  close: number;
} {
  return {
    time: Math.floor(bar.start_ms / 1000) as UTCTimestamp,
    open: Number(bar.open),
    high: Number(bar.high),
    low: Number(bar.low),
    close: Number(bar.close),
  };
}
