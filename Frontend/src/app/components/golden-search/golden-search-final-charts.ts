import { formatTimestampDisplay } from '../../shared/timestamp';
import type { ChartSpec } from './charts/golden-search-chart-spec';
import { dataIndexOf, NOT_RECORDED, seriesIndexOf, signedPercentOrNotRecorded, tooltipFrame, tooltipHtml, type ChartTheme, type TooltipRow } from './charts/golden-search-chart-theme';
import type { ChartOption } from './charts/golden-search-echarts';
import { percentText, ratioText, signedPercentText } from './golden-search-display';
import type { CandidateKey, CumulativeReturnPoint, FinalMeasure, MonthlyResult } from './golden-search.types';

/**
 * The Final decision charts (#2821): each measure on the development period
 * beside the final test, for the candidate and the current settings (V33),
 * their returns through the final test (V34), and the final test month by
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
  readonly monthly: readonly MonthlyResult[];
}

const FINAL = 'The final test: one look at data no step chose on.';
const MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'] as const;
const ONE_DECIMAL = new Intl.NumberFormat('en-US', { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const ROW = 64;

const axisText = (theme: ChartTheme) => ({ color: theme.textSecondary, fontSize: 11 });

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
      rows: measures.map((measure, i) => ({
        key: measure.key,
        cells: [
          measure.label,
          ...runs.flatMap((run) => {
            const own = run.comparison[i];
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
    { key: 'development', name: 'Development', color: theme.textSecondary },
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

/** Each run's cumulative return through the final test, at every session close. */
export function finalEquitySpec(runs: readonly FinalRun[]): ChartSpec {
  const sessions = [...new Set(runs.flatMap((run) => run.returns.map((point) => point.ms)))].sort((a, b) => a - b);
  const dates = sessions.map((ms) => formatTimestampDisplay(ms, { mode: 'date-et' }));
  const aligned = runs.map((run) => {
    const byMs = new Map(run.returns.map((point) => [point.ms, point.value]));
    return sessions.map((ms) => byMs.get(ms) ?? null);
  });
  const ends = runs.map((run, i) => `${run.label} ${signedPercentOrNotRecorded(aligned[i].filter((value): value is number => value !== null).at(-1) ?? null)}`);
  return {
    label: 'Final-test equity',
    summary: `Cumulative return through the final test: ${ends.join(', ')}.`,
    featured: runs[0]?.key ?? null,
    option: (theme) => equityOption(runs, aligned, dates, theme),
    table: {
      caption: 'Cumulative return at each session close of the final test (ET); — marks a session a run did not record',
      columns: ['Session', ...runs.map((run) => run.label)],
      rows: sessions.map((ms, i) => ({ key: String(ms), cells: [dates[i], ...aligned.map((values) => signedPercentText(values[i]))] })),
    },
  };
}

function equityOption(runs: readonly FinalRun[], aligned: readonly (readonly (number | null)[])[], dates: readonly string[], theme: ChartTheme): ChartOption {
  return {
    grid: { left: 52, right: 72, top: 12, bottom: 28 },
    tooltip: {
      ...tooltipFrame(theme),
      trigger: 'axis',
      formatter: (params: unknown) => {
        const index = dataIndexOf(params);
        if (index === null) return '';
        const rows: TooltipRow[] = runs.map((run, i) => ({
          label: run.label,
          values: [signedPercentOrNotRecorded(aligned[i][index] ?? null)],
          swatch: { color: theme.candidates[run.key], dashed: run.key === 'incumbent' },
        }));
        return tooltipHtml({ title: `${dates[index]} · at the session close`, columns: ['Return'], rows, notes: [FINAL] }, theme);
      },
    },
    xAxis: { type: 'category', data: [...dates], boundaryGap: false, axisLine: { lineStyle: { color: theme.axis } }, axisLabel: { ...axisText(theme), hideOverlap: true } },
    yAxis: { type: 'value', splitNumber: 4, axisLabel: { ...axisText(theme), formatter: (value: number) => signedPercentText(value) }, splitLine: { lineStyle: { color: theme.gridLine } } },
    series: runs.map((run, i) => {
      const color = theme.candidates[run.key];
      return {
        id: `equity:${run.key}`,
        name: run.label,
        type: 'line' as const,
        data: aligned[i].map((value) => value ?? '-'),
        showSymbol: false,
        connectNulls: false,
        lineStyle: { color, width: 2, type: run.key === 'incumbent' ? ('dashed' as const) : ('solid' as const) },
        itemStyle: { color },
        endLabel: { show: true, color, fontSize: 11, formatter: () => signedPercentOrNotRecorded(aligned[i].filter((value): value is number => value !== null).at(-1) ?? null) },
        emphasis: { focus: 'series' as const, blurScope: 'global' as const },
      };
    }),
  };
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
      rows: months.map((month) => ({ key: String(month.month_start_ms), cells: [`${MONTH_NAMES[month.month - 1]} ${month.year}`, ...runs.map((run) => signedPercentText(returnOf(run, month)))] })),
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
        return tooltipHtml({ title: `${MONTH_NAMES[month.month - 1]} ${month.year}`, columns: ['Net return'], rows, notes: [`The month from ${from} (ET).`, FINAL] }, theme);
      },
    },
    xAxis: { type: 'category', data: months.map((month) => `${MONTH_NAMES[month.month - 1]} ’${String(month.year).slice(-2)}`), axisLine: { lineStyle: { color: theme.axis } }, axisTick: { show: false }, axisLabel: axisText(theme) },
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
