import {
  createChart as createLightweightChart,
  type ChartOptions,
  type DeepPartial,
  type IChartApi,
  type Time,
  TickMarkType,
  type UTCTimestamp,
} from 'lightweight-charts';

/**
 * Create a Lightweight Charts instance with application-wide policy applied.
 *
 * TradingView permits the built-in logo to be disabled because the application
 * publishes the required attribution at `/legal/notices`. Keeping the policy
 * here prevents individual chart surfaces from drifting.
 */
export function createAppChart(
  container: string | HTMLElement,
  options: DeepPartial<ChartOptions> = {},
  chartFactory: typeof createLightweightChart = createLightweightChart,
): IChartApi {
  return chartFactory(container, {
    ...options,
    layout: {
      ...options.layout,
      attributionLogo: false,
    },
  });
}

function formatAxisLabel(
  date: Date,
  timeZone: string | undefined,
  options: Intl.DateTimeFormatOptions,
): string {
  return new Intl.DateTimeFormat('en-US', { ...options, timeZone }).format(date);
}

/**
 * Format a Lightweight Charts axis tick at the UI boundary.
 *
 * The library supplies seconds UTC, derived from the source's int64-ms UTC
 * timestamp. Converting it to a display string here keeps timezone handling
 * out of chart data and leaves the source timestamp unchanged.
 */
export function formatChartAxisTick(
  time: Time | number,
  timeZone: string | undefined,
  tickMarkType: TickMarkType,
): string {
  if (typeof time !== 'number') return String(time);

  const date = new Date(time * 1_000);
  switch (tickMarkType) {
    case TickMarkType.Year:
      return formatAxisLabel(date, timeZone, { year: 'numeric' });
    case TickMarkType.Month:
      return formatAxisLabel(date, timeZone, { month: 'short', year: 'numeric' });
    case TickMarkType.DayOfMonth:
      return formatAxisLabel(date, timeZone, { month: 'short', day: 'numeric' });
    case TickMarkType.Time:
      return formatAxisLabel(date, timeZone, {
        hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
      });
    case TickMarkType.TimeWithSeconds:
      return formatAxisLabel(date, timeZone, {
        hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
      });
    default:
      return '';
  }
}

/**
 * Compatibility adapter that preserves the existing UTC axis behavior for
 * charts that have not opted into an explicit display zone yet.
 */
export function formatTickMark(
  time: UTCTimestamp,
  tickMarkType: TickMarkType,
  _locale: string
): string {
  return formatChartAxisTick(time, 'UTC', tickMarkType);
}
