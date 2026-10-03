import { formatTimestampDisplay } from '../../shared/timestamp';
import type { ChartSpec } from './charts/golden-search-chart-spec';
import { dataIndexOf, tooltipFrame, tooltipHtml, type ChartTheme, type TooltipRow } from './charts/golden-search-chart-theme';
import type { ChartOption } from './charts/golden-search-echarts';
import { DEVELOPMENT_NOTE, percentText, signedCentsText, signedUsdText } from './golden-search-display';
import type { ConcentrationCurve, EvidenceCandidate, MeasuredConcentration } from './golden-search.types';

/**
 * The Compare evidence's concentration charts (#2821, #2815): the running
 * share of net profit with the trades best first (V26), and the development
 * net profit with and without its best month and its best trades (V27).
 * Every value is the server's, and the stored measure's status, not the
 * drawing, is what the decision summary reports. The measure informs; it
 * gates nothing.
 */

const RULE = 'Concern at $0 or less.';
const NET_OF_COMMISSION = 'Each trade counts its net profit: its P&L less its entry and exit commission.';
/** How many removed trades a tooltip lists before it says how many more there are. */
const LISTED_TRADES = 3;
const WHOLE_PERCENT = new Intl.NumberFormat('en-US', { style: 'percent', maximumFractionDigits: 0 });
const AXIS_USD = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', notation: 'compact', maximumFractionDigits: 1 });

function tradesText(count: number): string {
  return count === 1 ? '1 trade' : `${count} trades`;
}

// ---------------------------------------------------------------- V26 concentration curve

/** The curve as the server drew it, which has a point for every trade (`curve.reason` is null). */
export function concentrationCurveSpec(candidate: EvidenceCandidate, curve: ConcentrationCurve): ChartSpec {
  const points = curve.points;
  const total = points.at(-1)?.trades ?? 0;
  const count = curve.best_count ?? 0;
  const best = points[count];
  return {
    label: `${candidate.label} profit concentration`,
    summary: `The best ${tradesText(count)} of ${total} make ${percentText(best?.share_of_profit)} of its development net profit.`,
    featured: candidate.key,
    option: (theme) => curveOption(candidate, curve, theme),
    table: {
      caption: `${candidate.label}'s development trades, best first, with their running net profit`,
      columns: ['Trades counted', 'Share of trades', 'Running net profit', 'Share of net profit'],
      rows: points.map((point) => ({
        key: String(point.trades),
        cells: [String(point.trades), percentText(point.share_of_trades), signedUsdText(point.net_profit), percentText(point.share_of_profit)],
      })),
    },
  };
}

/** The hovered point of the curve itself, not the marked best-trades dot. */
function curveIndexOf(params: unknown): number | null {
  const entries: unknown[] = Array.isArray(params) ? params : [params];
  return dataIndexOf(entries.find((entry) => typeof entry === 'object' && entry !== null && 'seriesId' in entry && String(entry.seriesId).startsWith('curve:')) ?? null);
}

function curveOption(candidate: EvidenceCandidate, curve: ConcentrationCurve, theme: ChartTheme): ChartOption {
  const points = curve.points;
  const total = points.at(-1)?.trades ?? 0;
  const count = curve.best_count ?? 0;
  const best = points[count];
  const color = theme.candidates[candidate.key];
  const axisLabel = { color: theme.textSecondary, fontSize: 11, hideOverlap: true, formatter: (value: number) => WHOLE_PERCENT.format(value) };
  return {
    grid: { left: 48, right: 20, top: 20, bottom: 44 },
    tooltip: {
      ...tooltipFrame(theme),
      trigger: 'axis',
      formatter: (params: unknown) => {
        const point = points[curveIndexOf(params) ?? -1];
        if (point === undefined) return '';
        const rows: TooltipRow[] = [
          { label: 'Share of trades', values: [percentText(point.share_of_trades)] },
          { label: 'Running net profit', values: [signedUsdText(point.net_profit)] },
          { label: 'Share of net profit', values: [percentText(point.share_of_profit)] },
        ];
        const marked = point.trades === count ? ['The best 5% of trades, rounded up.'] : [];
        const title = point.trades === 0 ? 'Before any trade' : `The best ${tradesText(point.trades)} of ${total}`;
        return tooltipHtml({ title, columns: ['Value'], rows, notes: [...marked, NET_OF_COMMISSION, DEVELOPMENT_NOTE] }, theme);
      },
    },
    xAxis: {
      type: 'value',
      min: 0,
      max: 1,
      splitNumber: 4,
      name: 'Share of trades, best first',
      nameLocation: 'middle',
      nameGap: 26,
      nameTextStyle: { color: theme.textSecondary, fontSize: 11 },
      axisLine: { lineStyle: { color: theme.axis } },
      axisLabel,
      splitLine: { show: false },
    },
    yAxis: { type: 'value', min: 0, splitNumber: 4, axisLabel, splitLine: { lineStyle: { color: theme.gridLine } } },
    series: [
      {
        id: `curve:${candidate.key}`,
        name: candidate.label,
        type: 'line',
        data: points.map((point) => [point.share_of_trades, point.share_of_profit]),
        showSymbol: false,
        lineStyle: { color, width: 2 },
        itemStyle: { color },
        areaStyle: { color, opacity: 0.12 },
        emphasis: { focus: 'series', blurScope: 'global' },
        markLine: {
          silent: true,
          symbol: 'none',
          label: { formatter: 'all net profit', position: 'insideEndBottom', color: theme.textSecondary, fontSize: 11 },
          lineStyle: { color: theme.textSecondary, type: 'dashed', width: 1 },
          data: [{ yAxis: 1 }],
        },
      },
      {
        id: `best:${candidate.key}`,
        name: 'Best 5% of trades',
        type: 'scatter',
        data: best === undefined ? [] : [[best.share_of_trades, best.share_of_profit]],
        symbolSize: 9,
        itemStyle: { color: theme.text },
        label: {
          show: true,
          position: 'right',
          color: theme.text,
          fontSize: 11,
          formatter: () => `best ${tradesText(count)}: ${percentText(best?.share_of_profit)}`,
        },
        emphasis: { focus: 'series', blurScope: 'global' },
      },
    ],
  };
}

// ---------------------------------------------------------------- V27 without its best

interface Bar {
  readonly key: 'all' | 'month' | 'trades';
  readonly label: string;
  readonly value: number;
}

/**
 * A measured result with and without its best month and best trades. Its
 * dollars are to the cent: the rule is judged at $0, and a whole-dollar
 * "$0" would hide which side of it a result falls.
 */
export function withoutBestSpec(candidate: EvidenceCandidate, measure: MeasuredConcentration): ChartSpec {
  const bars = barsOf(measure);
  const monthFrom = formatTimestampDisplay(measure.best_month.month_start_ms, { mode: 'date-et' });
  return {
    label: `${candidate.label} without its best`,
    summary: `${candidate.label}: ${bars.map((bar) => `${bar.label} ${signedCentsText(bar.value)}`).join('; ')}.`,
    featured: null,
    option: (theme) => withoutBestOption(candidate, measure, bars, theme),
    table: {
      caption: `${candidate.label}'s development net profit with and without its best; — marks a value not recorded`,
      columns: ['Result', 'Net profit', 'Taken out'],
      rows: [
        { key: 'all', cells: ['All trades', signedCentsText(measure.net_profit), '—'] },
        { key: 'month', cells: [`${bars[1].label} (month from ${monthFrom}, ET)`, signedCentsText(measure.without_best_month), signedCentsText(measure.best_month.net_profit)] },
        { key: 'trades', cells: [bars[2].label, signedCentsText(measure.without_best_trades), signedCentsText(measure.best_trades_net_profit)] },
      ],
    },
  };
}

function barsOf(measure: MeasuredConcentration): Bar[] {
  return [
    { key: 'all', label: 'All trades', value: measure.net_profit },
    { key: 'month', label: 'Without its best month', value: measure.without_best_month },
    { key: 'trades', label: `Without its best ${tradesText(measure.best_trades.length)}`, value: measure.without_best_trades },
  ];
}

function profitable(bar: Bar): boolean {
  return bar.value > 0;
}

function withoutBestOption(candidate: EvidenceCandidate, measure: MeasuredConcentration, bars: readonly Bar[], theme: ChartTheme): ChartOption {
  const colorOf = (bar: Bar) => (!profitable(bar) ? theme.loss : bar.key === 'all' ? theme.candidates[candidate.key] : theme.withoutBest);
  // Each bar is its own series, so a walkthrough step can light up one of them; they share one row each.
  const series = bars.map((bar) => ({
    id: `${bar.key}:result`,
    name: bar.label,
    type: 'bar' as const,
    data: bars.map((other) => (other.key === bar.key ? other.value : '-')),
    barMaxWidth: 22,
    barGap: '-100%',
    itemStyle: { color: colorOf(bar) },
    // Right of the bar's end, which for a loss is $0, so the value never runs into the row's name.
    label: { show: true, position: 'right' as const, color: theme.text, fontSize: 11, formatter: () => signedCentsText(bar.value) },
    emphasis: { focus: 'series' as const, blurScope: 'global' as const },
  }));
  return {
    grid: { left: 150, right: 72, top: 8, bottom: 28 },
    tooltip: {
      ...tooltipFrame(theme),
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: (params: unknown) => {
        const bar = bars[dataIndexOf(params) ?? -1];
        return bar === undefined ? '' : withoutBestTooltip(bar, measure, theme);
      },
    },
    xAxis: {
      type: 'value',
      // A value axis keeps $0 in its rounded scale, so a bar is always drawn from $0.
      splitNumber: 3,
      axisLabel: { color: theme.textSecondary, fontSize: 11, hideOverlap: true, formatter: (value: number) => AXIS_USD.format(value) },
      splitLine: { lineStyle: { color: theme.gridLine } },
    },
    yAxis: {
      type: 'category',
      inverse: true,
      data: bars.map((bar) => bar.label),
      axisLine: { lineStyle: { color: theme.axis } },
      axisTick: { show: false },
      axisLabel: {
        color: theme.text,
        fontSize: 11,
        width: 140,
        overflow: 'truncate',
        formatter: (value: string, index: number) => (profitable(bars[index]) ? value : `${value}\n{loss|not profitable}`),
        rich: { loss: { color: theme.loss, fontSize: 10 } },
      },
    },
    series,
  };
}

function withoutBestTooltip(bar: Bar, measure: MeasuredConcentration, theme: ChartTheme): string {
  const net = { label: 'Net profit', values: [signedCentsText(bar.value)] };
  if (bar.key === 'all') {
    const rows: TooltipRow[] = [net, { label: 'Trades', values: [String(measure.trades)] }];
    return tooltipHtml({ title: bar.label, columns: ['Value'], rows, notes: ['The development run’s net profit, every trade included.', DEVELOPMENT_NOTE] }, theme);
  }
  if (bar.key === 'month') {
    const month = measure.best_month;
    const rows: TooltipRow[] = [net, { label: 'The best month made', values: [signedCentsText(month.net_profit)] }];
    const which = `The best month is the one from ${formatTimestampDisplay(month.month_start_ms, { mode: 'date-et' })} (ET).`;
    return tooltipHtml({ title: bar.label, columns: ['Value'], rows, notes: [which, RULE, DEVELOPMENT_NOTE] }, theme);
  }
  const removed = measure.best_trades;
  const rows: TooltipRow[] = [
    net,
    { label: 'Those trades made', values: [signedCentsText(measure.best_trades_net_profit)] },
    ...removed.slice(0, LISTED_TRADES).map((trade) => ({ label: `Entered ${formatTimestampDisplay(trade.entry_ms, { mode: 'local' })}`, values: [signedCentsText(trade.net_profit)] })),
  ];
  const more = removed.length > LISTED_TRADES ? [`${removed.length - LISTED_TRADES} more of the best trades are not listed.`] : [];
  const which = `The best 5% of its ${measure.trades} trades, rounded up, each net of its entry and exit commission.`;
  return tooltipHtml({ title: bar.label, columns: ['Value'], rows, notes: [...more, which, RULE, DEVELOPMENT_NOTE] }, theme);
}
