import { formatTimestampDisplay } from '../../shared/timestamp';
import type { ChartSpec } from './charts/golden-search-chart-spec';
import { axisText, dataIndexOf, monthAxisText, monthText, NOT_RECORDED, seriesIndexOf, signedPercentOrNotRecorded, tooltipFrame, tooltipHtml, type ChartTheme, type TooltipRow } from './charts/golden-search-chart-theme';
import type { ChartOption } from './charts/golden-search-echarts';
import { percentText, ratioText, signedPercentText } from './golden-search-display';
import { equityChartSpec, type EquityPeriod } from './golden-search-equity-chart';
import type { CandidateKey, CumulativeReturnPoint, DrawdownPoint, FinalMeasure, MonthlyResult } from './golden-search.types';

/**
 * The Final decision charts (#2821): each measure on the development period
 * beside the final test, for the candidate and the current settings (V33),
 * their returns and falls from peak through the final test (V34), and the final test month by
 * month (V36). Every value is the server's; the final test is the one
 * out-of-sample look, so a run that failed or did not record shows nothing,
 * never zero.
 */

/** One run of the final test: the candidate or the current settings. */
export interface FinalRun {
  readonly key: CandidateKey;
  readonly label: string;
  readonly comparison: readonly FinalMeasure[];
  readonly returns: readonly CumulativeReturnPoint[];
  readonly falls: readonly DrawdownPoint[];
  readonly monthly: readonly MonthlyResult[];
}

const FINAL = 'The final test: one look at data no step chose on.';
const ONE_DECIMAL = new Intl.NumberFormat('en-US', { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const ROW = 64;


function measureText(measure: FinalMeasure, value: number | null): string {
  if (value === null) return NOT_RECORDED;
  if (measure.key === 'annualized_return') return signedPercentText(value);
  if (measure.key === 'max_drawdown_pct') return percentText(value);
  if (measure.key === 'trades_per_year') return ONE_DECIMAL.format(value);
  return ratioText(value);
}

function changeText(measure: FinalMeasure): string {
  if (measure.change === null) return NOT_RECORDED;
  if (measure.key === 'annualized_return' || measure.key === 'max_drawdown_pct') return signedPercentText(measure.change);
  const sign = measure.change > 0 ? '+' : '';
  return measure.key === 'trades_per_year' ? `${sign}${ONE_DECIMAL.format(measure.change)}` : `${sign}${ratioText(measure.change)}`;
}

/** A table cell: the value, or — when the run did not record it. */
function cellText(text: string): string {
  return text === NOT_RECORDED ? '—' : text;
}

// ---------------------------------------------------------------- V33 development vs final

/** Each measure on its own scale: the development value beside the final test's, for each run. */
export function finalComparisonSpec(runs: readonly FinalRun[]): ChartSpec {
  const measures = runs[0]?.comparison ?? [];
  return {
    label: 'Development against final',
    summary: `${measures.map((measure) => measure.label).join(', ')} on the development period beside the final test, for ${runs.map((run) => run.label).join(' and ')}.`,
    featured: null,
    option: (theme) => comparisonOption(runs, theme),
    table: {
      caption: 'Each measure on the development period and the final test; — marks a value not recorded',
      columns: ['Measure', ...runs.flatMap((run) => [`${run.label}, development`, `${run.label}, final`, `${run.label}, change`])],
      rows: measures.map((measure) => ({
        key: measure.key,
        cells: [
          measure.label,
          ...runs.flatMap((run) => {
            const own = run.comparison.find((item) => item.key === measure.key);
            return own === undefined ? ['—', '—', '—'] : [cellText(measureText(own, own.development)), cellText(measureText(own, own.final)), cellText(changeText(own))];
          }),
        ],
      })),
    },
  };
}

/** The comparison's height: a row per measure. */
export function comparisonHeight(count: number): number {
  return 24 + ROW * Math.max(1, count);
}

function comparisonOption(runs: readonly FinalRun[], theme: ChartTheme): ChartOption {
  const measures = runs[0]?.comparison ?? [];
  const periods = [
    { key: 'development', name: 'Development', color: theme.development },
    { key: 'final', name: 'Final test', color: theme.candidates.all_period },
  ] as const;
  return {
    grid: measures.map((_, i) => ({ left: 130, right: 64, top: 16 + i * ROW, height: ROW - 28 })),
    tooltip: {
      ...tooltipFrame(theme),
      trigger: 'item',
      formatter: (params: unknown) => {
        const series = seriesIndexOf(params) ?? -1;
        const measure = measures[Math.floor(series / periods.length)];
        const run = runs[dataIndexOf(params) ?? -1];
        const own = run?.comparison.find((item) => item.key === measure?.key);
        if (run === undefined || own === undefined) return '';
        const rows: TooltipRow[] = [
          { label: 'Development', values: [measureText(own, own.development)] },
          { label: 'Final test', values: [measureText(own, own.final)] },
          { label: 'Change', values: [changeText(own)] },
        ];
        const note = own.key === 'annualized_return' || own.key === 'trades_per_year' ? 'Each window over its own trading years, so periods of different lengths compare.' : 'As the engine reported it.';
        return tooltipHtml({ title: `${run.label} · ${own.label}`, columns: ['Value'], rows, notes: [note, FINAL] }, theme);
      },
    },
    xAxis: measures.map((measure, i) => ({
      type: 'value' as const,
      gridIndex: i,
      splitNumber: 2,
      axisLine: { lineStyle: { color: theme.axis } },
      axisLabel: { ...axisText(theme), formatter: (value: number) => measureText(measure, value) },
      splitLine: { lineStyle: { color: theme.gridLine } },
    })),
    yAxis: measures.map((measure, i) => ({
      type: 'category' as const,
      gridIndex: i,
      data: runs.map((run) => run.label),
      inverse: true,
      name: measure.label,
      nameLocation: 'end' as const,
      nameTextStyle: { ...axisText(theme), align: 'left' as const },
      axisLine: { lineStyle: { color: theme.axis } },
      axisTick: { show: false },
      axisLabel: { ...axisText(theme), width: 120, overflow: 'truncate' as const },
    })),
    series: measures.flatMap((measure, i) =>
      periods.map((period) => ({
        id: `${period.key}:${measure.key}`,
        name: `${period.name} · ${measure.label}`,
        type: 'bar' as const,
        xAxisIndex: i,
        yAxisIndex: i,
        data: runs.map((run) => run.comparison.find((item) => item.key === measure.key)?.[period.key] ?? '-'),
        barMaxWidth: 8,
        itemStyle: { color: period.color },
        emphasis: { focus: 'series' as const, blurScope: 'global' as const },
      })),
    ),
  };
}

// ---------------------------------------------------------------- V34 final-year equity

/** The final test on the shared equity chart: each run's return through it, and its fall from its own peak beneath. */
export const FINAL_EQUITY: EquityPeriod = { label: 'Final-test cumulative return and fall from peak', note: FINAL };

/** Each run's cumulative return and fall from peak through the final test, against the worst-fall limit the final test is judged by. */
export function finalEquitySpec(runs: readonly FinalRun[], ceiling: number): ChartSpec {
  return equityChartSpec(
    runs.map((run) => ({ key: run.key, label: run.label, returns: run.returns, falls: run.falls })),
    runs[0]?.key ?? 'incumbent',
    ceiling,
    FINAL_EQUITY,
  );
}

// ---------------------------------------------------------------- V36 final-year months

/** Each final-test month's return for each run, side by side. */
export function finalMonthsSpec(runs: readonly FinalRun[]): ChartSpec {
  const months = [...new Map(runs.flatMap((run) => run.monthly.map((month) => [month.month_start_ms, month] as const))).values()].sort((a, b) => a.month_start_ms - b.month_start_ms);
  const returnOf = (run: FinalRun, month: MonthlyResult) => run.monthly.find((item) => item.month_start_ms === month.month_start_ms)?.return_fraction ?? null;
  return {
    label: 'Final-test months',
    summary: `Each of ${months.length} final-test months: the return of ${runs.map((run) => run.label).join(' and ')}.`,
    featured: null,
    option: (theme) => monthsOption(runs, months, returnOf, theme),
    table: {
      caption: 'Net return by final-test month (month from, ET); — marks a month a run did not record',
      columns: ['Month', ...runs.map((run) => run.label)],
      rows: months.map((month) => ({ key: String(month.month_start_ms), cells: [monthText(month), ...runs.map((run) => signedPercentText(returnOf(run, month)))] })),
    },
  };
}

function monthsOption(runs: readonly FinalRun[], months: readonly MonthlyResult[], returnOf: (run: FinalRun, month: MonthlyResult) => number | null, theme: ChartTheme): ChartOption {
  return {
    grid: { left: 52, right: 12, top: 12, bottom: 28 },
    tooltip: {
      ...tooltipFrame(theme),
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: (params: unknown) => {
        const month = months[dataIndexOf(params) ?? -1];
        if (month === undefined) return '';
        const rows: TooltipRow[] = runs.map((run) => ({ label: run.label, values: [signedPercentOrNotRecorded(returnOf(run, month))], swatch: { color: theme.candidates[run.key], dashed: false } }));
        const from = formatTimestampDisplay(month.month_start_ms, { mode: 'date-et' });
        return tooltipHtml({ title: monthText(month), columns: ['Net return'], rows, notes: [`The month from ${from} (ET).`, FINAL] }, theme);
      },
    },
    xAxis: { type: 'category', data: months.map((month) => monthAxisText(month)), axisLine: { lineStyle: { color: theme.axis } }, axisTick: { show: false }, axisLabel: axisText(theme) },
    yAxis: { type: 'value', splitNumber: 4, axisLabel: { ...axisText(theme), formatter: (value: number) => signedPercentText(value) }, splitLine: { lineStyle: { color: theme.gridLine } } },
    series: runs.map((run) => ({
      id: `months:${run.key}`,
      name: run.label,
      type: 'bar' as const,
      data: months.map((month) => returnOf(run, month) ?? '-'),
      barMaxWidth: 16,
      itemStyle: { color: theme.candidates[run.key] },
      emphasis: { focus: 'series' as const, blurScope: 'global' as const },
    })),
  };
}
