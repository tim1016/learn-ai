import type { CustomSeriesOption } from 'echarts/charts';

import { formatTimestampDisplay } from '../../shared/timestamp';
import type { ChartSpec } from './charts/golden-search-chart-spec';
import { dataIndexOf, tooltipFrame, tooltipHtml, type ChartTheme, type TooltipRow } from './charts/golden-search-chart-theme';
import type { ChartOption } from './charts/golden-search-echarts';
import type { CoverageMonth, MinimumWindow, PlanCharts, PlanKnob, PlanWindow } from './golden-search.types';

/**
 * The Plan step's charts (#2821): every window the frozen plan evaluates
 * (V2), each knob's searched range in its legal domain (V3), each stage's
 * planned against used engine runs (V4), each window's trade minimum and
 * its arithmetic (V5), and the lake's coverage of the data span month by
 * month (V6). Every value is the server's, from the protocol and receipt the
 * study froze at lock.
 */

const MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'] as const;
const THREE_DECIMALS = new Intl.NumberFormat('en-US', { maximumFractionDigits: 3 });

const axisText = (theme: ChartTheme) => ({ color: theme.textSecondary, fontSize: 11 });

function dateEt(ms: number): string {
  return formatTimestampDisplay(ms, { mode: 'date-et' });
}

/** A half-open window as its first and last ET dates. */
export function windowDates(window: Pick<PlanWindow, 'start_ms' | 'end_ms'>): string {
  return `${dateEt(window.start_ms)} to ${dateEt(window.end_ms - 1)}`;
}

function sessionsText(count: number): string {
  return count === 1 ? '1 session' : `${count} sessions`;
}

// ---------------------------------------------------------------- V2 window map

const KIND_NOTES: Readonly<Record<PlanWindow['kind'], string>> = {
  run_up: 'Data read only to warm the indicators up; nothing is judged on it.',
  development: 'The search and the candidates’ evidence: used for choosing.',
  recent: 'The recent fit searches only these months.',
  training: 'This fold searches again on these months only.',
  test: 'This fold tests its winner here; a single fold has no trade minimum of its own.',
  forward: 'Every fold’s test together: the verdict’s trade minimum applies here.',
  final: 'Opened once, at the end, for the chosen candidate and the current settings.',
};

/** Every window as a bar on one time axis, a row each. */
export function windowMapSpec(charts: PlanCharts): ChartSpec {
  const windows = charts.windows;
  return {
    label: 'Window map',
    summary: `${windows.length} windows the frozen plan evaluates, from the run-up to the final test, each with its sessions and trade minimum.`,
    featured: null,
    option: (theme) => windowMapOption(windows, theme),
    table: {
      caption: 'Every window the plan evaluates (ET); — marks a window with no trade minimum of its own',
      columns: ['Window', 'Dates', 'Sessions', 'Trade minimum'],
      rows: windows.map((window) => ({ key: window.key, cells: [window.label, windowDates(window), String(window.sessions), window.minimum_trades === null ? '—' : String(window.minimum_trades)] })),
    },
  };
}

function kindColor(kind: PlanWindow['kind'], theme: ChartTheme): string {
  if (kind === 'final') return theme.warn;
  if (kind === 'test' || kind === 'forward') return theme.stepAbove;
  if (kind === 'run_up') return theme.tooFew;
  return theme.textSecondary;
}

/** A window as a bar from its start to its end on its row. */
function windowBar(outline: string): NonNullable<CustomSeriesOption['renderItem']> {
  return (_params, api) => {
    const start = api.coord([api.value(0), api.value(2)]);
    const end = api.coord([api.value(1), api.value(2)]);
    const size = api.size?.([0, 1]);
    const height = (Array.isArray(size) ? size[1] : 20) * 0.55;
    return { type: 'rect', shape: { x: start[0], y: start[1] - height / 2, width: Math.max(1, end[0] - start[0]), height }, style: api.style(), emphasis: { style: { stroke: outline, lineWidth: 2 } } };
  };
}

function windowMapOption(windows: readonly PlanWindow[], theme: ChartTheme): ChartOption {
  return {
    grid: { left: 140, right: 16, top: 8, bottom: 28 },
    tooltip: {
      ...tooltipFrame(theme),
      trigger: 'item',
      formatter: (params: unknown) => {
        const window = windows[dataIndexOf(params) ?? -1];
        if (window === undefined) return '';
        const rows: TooltipRow[] = [
          { label: 'Dates (ET)', values: [windowDates(window)] },
          { label: 'Sessions', values: [String(window.sessions)] },
          { label: 'Trade minimum', values: [window.minimum_trades === null ? 'none of its own' : String(window.minimum_trades)] },
        ];
        return tooltipHtml({ title: window.label, columns: ['Value'], rows, notes: [KIND_NOTES[window.kind], 'Sessions are the trading calendar’s; each window’s last day is shown.'] }, theme);
      },
    },
    xAxis: { type: 'time', axisLine: { lineStyle: { color: theme.axis } }, axisLabel: { ...axisText(theme), hideOverlap: true, formatter: (value: number) => dateEt(value) }, splitLine: { lineStyle: { color: theme.gridLine } } },
    yAxis: { type: 'category', data: windows.map((window) => window.label), inverse: true, axisLine: { lineStyle: { color: theme.axis } }, axisTick: { show: false }, axisLabel: { ...axisText(theme), width: 130, overflow: 'truncate' } },
    series: [
      {
        id: 'windows:plan',
        name: 'Windows',
        type: 'custom',
        renderItem: windowBar(theme.text),
        encode: { x: [0, 1], y: 2 },
        data: windows.map((window, i) => ({ value: [window.start_ms, window.end_ms, i], itemStyle: { color: kindColor(window.kind, theme) } })),
      },
    ],
  };
}

/** The window map's height: a row per window. */
export function windowMapHeight(count: number): number {
  return 44 + 26 * Math.max(1, count);
}

// ---------------------------------------------------------------- V3 search space

/** Each knob's searched range as a band in its legal domain, the current settings marked; held knobs a single mark. */
export function searchSpaceSpec(charts: PlanCharts): ChartSpec {
  const knobs = charts.search_space;
  const searched = knobs.filter((knob) => knob.searched).map((knob) => knob.label);
  const zoom = knobs.some((knob) => knob.start_position !== null);
  return {
    label: 'Search space',
    summary: `${searched.length === 0 ? 'No knob is searched' : `${searched.join(', ')} searched`}; every knob drawn in its legal range, the current settings marked${zoom ? ', and where Zoom starts' : ''}.`,
    featured: null,
    option: (theme) => searchSpaceOption(knobs, theme),
    table: {
      caption: `Every knob in search order: its range, step, values and importance, the current settings${zoom ? ' and where Zoom starts' : ''}`,
      columns: ['Knob', 'Range', 'Step', 'Values', 'Importance', 'Current', ...(zoom ? ['Zoom starts at'] : []), 'Legal range'],
      rows: knobs.map((knob) => ({
        key: knob.name,
        cells: [
          knob.label,
          knob.searched ? `${knob.low} to ${knob.high}` : `held at ${knob.low}`,
          knob.step === null ? '—' : String(knob.step),
          knob.values === null ? 'not valid' : String(knob.values),
          knob.importance === null ? '—' : String(knob.importance),
          String(knob.current),
          ...(zoom ? [knob.start === null ? '—' : String(knob.start)] : []),
          `${knob.domain_low} to ${knob.domain_high}`,
        ],
      })),
    },
  };
}

function searchSpaceOption(knobs: readonly PlanKnob[], theme: ChartTheme): ChartOption {
  return {
    grid: { left: 130, right: 24, top: 8, bottom: 28 },
    tooltip: {
      ...tooltipFrame(theme),
      trigger: 'item',
      formatter: (params: unknown) => {
        const knob = knobs[dataIndexOf(params) ?? -1];
        if (knob === undefined) return '';
        const rows: TooltipRow[] = knob.searched
          ? [
              { label: 'Searched', values: [`${knob.low} to ${knob.high}`] },
              { label: 'Step', values: [knob.step === null ? '—' : String(knob.step)] },
              { label: 'Values', values: [knob.values === null ? 'not valid' : String(knob.values)] },
              { label: 'Importance', values: [knob.importance === null ? '—' : String(knob.importance)] },
            ]
          : [{ label: 'Held at', values: [String(knob.low)] }];
        const marks: TooltipRow[] = [
          { label: 'Current settings', values: [String(knob.current)] },
          ...(knob.start === null ? [] : [{ label: 'Zoom starts at', values: [String(knob.start)] }]),
        ];
        const notes = [`Legal range ${knob.domain_low} to ${knob.domain_high}. Higher importance is searched first.`];
        if (knob.start !== null && knob.searched) notes.push('Zoom keeps its starting value in every round, so it can end there even outside the band.');
        return tooltipHtml({ title: `${knob.label} (${knob.unit})`, columns: ['Value'], rows: [...rows, ...marks], notes }, theme);
      },
    },
    xAxis: { type: 'value', min: 0, max: 1, splitNumber: 2, axisLine: { lineStyle: { color: theme.axis } }, axisLabel: { ...axisText(theme), formatter: (value: number) => (value === 0 ? 'legal low' : value === 1 ? 'legal high' : '') }, splitLine: { lineStyle: { color: theme.gridLine } } },
    yAxis: { type: 'category', data: knobs.map((knob) => knob.label), inverse: true, axisLine: { lineStyle: { color: theme.axis } }, axisTick: { show: false }, axisLabel: { ...axisText(theme), width: 120, overflow: 'truncate' } },
    series: [
      {
        id: 'range:space',
        name: 'Searched range',
        type: 'custom',
        renderItem: windowBar(theme.text),
        encode: { x: [0, 1], y: 2 },
        data: knobs.map((knob, i) => ({
          value: [knob.low_position ?? 0, knob.high_position ?? 1, i],
          itemStyle: { color: knob.searched ? theme.candidates.all_period : theme.tooFew, opacity: knob.searched ? 0.55 : 1 },
        })),
      },
      {
        id: 'current:space',
        name: 'Current settings',
        type: 'scatter',
        symbol: 'diamond',
        symbolSize: 11,
        data: knobs.map((knob, i) => [knob.current_position ?? '-', i]),
        itemStyle: { color: theme.text },
        emphasis: { focus: 'series', blurScope: 'global' },
      },
      ...(knobs.some((knob) => knob.start_position !== null)
        ? [
            {
              id: 'start:space',
              name: 'Zoom starts',
              type: 'scatter' as const,
              symbol: 'emptyCircle',
              symbolSize: 13,
              data: knobs.map((knob, i) => [knob.start_position ?? '-', i]),
              itemStyle: { color: theme.text, borderWidth: 1.5 },
              emphasis: { focus: 'series' as const, blurScope: 'global' as const },
            },
          ]
        : []),
    ],
  };
}

/** The search space's height: a row per knob. */
export function searchSpaceHeight(count: number): number {
  return 40 + 30 * Math.max(1, count);
}

// ---------------------------------------------------------------- V4 workload

/** Each stage's planned engine runs against those it has used so far. */
export function workloadSpec(charts: PlanCharts): ChartSpec {
  const load = charts.workload;
  return {
    label: 'Workload',
    summary: `Up to ${load.planned_total} engine runs planned across ${load.stages.length} stages, within a cap of ${load.cap}; ${load.consumed} used so far.`,
    featured: null,
    option: (theme) => workloadOption(charts, theme),
    table: {
      caption: 'Engine runs per stage: planned at most and used so far',
      columns: ['Stage', 'Planned at most', 'Used so far'],
      rows: load.stages.map((stage) => ({ key: stage.stage, cells: [stage.label, String(stage.planned), String(stage.used)] })),
    },
  };
}

function workloadOption(charts: PlanCharts, theme: ChartTheme): ChartOption {
  const stages = charts.workload.stages;
  const bars = [
    { group: 'planned', name: 'Planned at most', values: stages.map((stage) => stage.planned), color: theme.tooFew },
    { group: 'used', name: 'Used so far', values: stages.map((stage) => stage.used), color: theme.candidates.all_period },
  ];
  return {
    grid: { left: 150, right: 24, top: 8, bottom: 28 },
    tooltip: {
      ...tooltipFrame(theme),
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: (params: unknown) => {
        const stage = stages[dataIndexOf(params) ?? -1];
        if (stage === undefined) return '';
        const rows: TooltipRow[] = [
          { label: 'Planned at most', values: [String(stage.planned)] },
          { label: 'Used so far', values: [String(stage.used)] },
        ];
        const note = `Planned at most is an upper bound, not a stop. The study’s cap is ${charts.workload.cap} engine runs; ${charts.workload.consumed} used so far. A cached answer reuses a run without spending the cap.`;
        return tooltipHtml({ title: stage.label, columns: ['Engine runs'], rows, notes: [note] }, theme);
      },
    },
    xAxis: { type: 'value', minInterval: 1, splitNumber: 3, axisLine: { lineStyle: { color: theme.axis } }, axisLabel: axisText(theme), splitLine: { lineStyle: { color: theme.gridLine } } },
    yAxis: { type: 'category', data: stages.map((stage) => stage.label), inverse: true, axisLine: { lineStyle: { color: theme.axis } }, axisTick: { show: false }, axisLabel: { ...axisText(theme), width: 140, overflow: 'truncate' } },
    series: bars.map((bar) => ({
      id: `${bar.group}:workload`,
      name: bar.name,
      type: 'bar' as const,
      data: bar.values,
      barMaxWidth: 10,
      itemStyle: { color: bar.color },
      emphasis: { focus: 'series' as const, blurScope: 'global' as const },
    })),
  };
}

/** The workload's height: a row per stage. */
export function workloadHeight(count: number): number {
  return 40 + 30 * Math.max(1, count);
}

// ---------------------------------------------------------------- V5 trade minimums

function yearTerms(window: MinimumWindow): string {
  return window.years.map((year) => `${year.year}: ${year.selected_sessions} of ${year.year_sessions} sessions`).join('; ');
}

/** Each window's trade minimum; under an expected trade frequency, with the trading years behind it. */
export function minimumsSpec(charts: PlanCharts): ChartSpec {
  const minimums = charts.minimums;
  const rate = minimums.expected_trades_per_year;
  return {
    label: 'Trade minimums',
    summary:
      rate === null
        ? 'A fixed-floor plan: one minimum for every selection window and one for the final test.'
        : `Each window's trade minimum from an expected ${rate} trades a year, rounded up.`,
    featured: null,
    option: (theme) => minimumsOption(charts, theme),
    table: {
      caption: rate === null ? 'The plan’s two fixed trade minimums' : `Each window’s trade minimum at ${rate} trades a trading year`,
      columns: ['Window', 'Trading years', 'Trade minimum', 'Sessions by year'],
      rows: minimums.windows.map((window) => ({
        key: window.key,
        cells: [window.label, window.trading_years === null ? '—' : THREE_DECIMALS.format(window.trading_years), window.minimum_trades === null ? '—' : String(window.minimum_trades), window.years.length === 0 ? '—' : yearTerms(window)],
      })),
    },
  };
}

function minimumsOption(charts: PlanCharts, theme: ChartTheme): ChartOption {
  const minimums = charts.minimums;
  const rate = minimums.expected_trades_per_year;
  return {
    grid: { left: 140, right: 36, top: 8, bottom: 28 },
    tooltip: {
      ...tooltipFrame(theme),
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: (params: unknown) => {
        const window = minimums.windows[dataIndexOf(params) ?? -1];
        if (window === undefined) return '';
        const rows: TooltipRow[] = [
          { label: 'Trade minimum', values: [window.minimum_trades === null ? '—' : String(window.minimum_trades)] },
          ...(window.trading_years === null ? [] : [{ label: 'Trading years', values: [THREE_DECIMALS.format(window.trading_years)] }]),
          ...window.years.map((year) => ({ label: String(year.year), values: [`${year.selected_sessions} of ${year.year_sessions} sessions`] })),
        ];
        const note =
          rate === null
            ? 'A fixed floor, set in the plan.'
            : `Minimum = ${rate} × trading years, rounded up; a trading year is the year’s sessions in the window over all its sessions.`;
        return tooltipHtml({ title: window.label, columns: ['Value'], rows, notes: [note] }, theme);
      },
    },
    xAxis: { type: 'value', minInterval: 1, splitNumber: 3, axisLine: { lineStyle: { color: theme.axis } }, axisLabel: axisText(theme), splitLine: { lineStyle: { color: theme.gridLine } } },
    yAxis: { type: 'category', data: minimums.windows.map((window) => window.label), inverse: true, axisLine: { lineStyle: { color: theme.axis } }, axisTick: { show: false }, axisLabel: { ...axisText(theme), width: 130, overflow: 'truncate' } },
    series: [
      {
        id: 'minimum:windows',
        name: 'Trade minimum',
        type: 'bar',
        data: minimums.windows.map((window) => window.minimum_trades ?? '-'),
        barMaxWidth: 12,
        itemStyle: { color: theme.candidates.all_period },
        label: { show: true, position: 'right', color: theme.text, fontSize: 10, formatter: (params: { dataIndex: number }) => String(minimums.windows[params.dataIndex]?.minimum_trades ?? '') },
        emphasis: { focus: 'series', blurScope: 'global' },
      },
    ],
  };
}

/** The minimums' height: a row per window. */
export function minimumsHeight(count: number): number {
  return 40 + 26 * Math.max(1, count);
}

// ---------------------------------------------------------------- V6 data coverage

const COVERAGE_PARTS = [
  { key: 'complete', name: 'Complete' },
  { key: 'fetching', name: 'Still fetching' },
  { key: 'stale', name: 'Stale' },
  { key: 'failed', name: 'Failed' },
  { key: 'missing', name: 'Missing' },
] as const;

function monthName(month: CoverageMonth): string {
  return `${MONTH_NAMES[month.month - 1]} ${month.year}`;
}

/** Each month of the data span: its sessions split by what the lake holds for them. */
export function coverageSpec(months: readonly CoverageMonth[]): ChartSpec {
  const first = months[0];
  const last = months.at(-1);
  return {
    label: 'Data coverage',
    summary: `The lake's minute bars for ${months.length} months, ${first === undefined || last === undefined ? 'none' : `${monthName(first)} to ${monthName(last)}`}: each month's sessions, complete or not.`,
    featured: null,
    option: (theme) => coverageOption(months, theme),
    table: {
      caption: 'Sessions per month by lake status',
      columns: ['Month', 'Sessions', ...COVERAGE_PARTS.map((part) => part.name)],
      rows: months.map((month) => ({ key: String(month.month_start_ms), cells: [monthName(month), String(month.sessions), ...COVERAGE_PARTS.map((part) => String(month[part.key]))] })),
    },
  };
}

function coverageOption(months: readonly CoverageMonth[], theme: ChartTheme): ChartOption {
  const colors: Readonly<Record<(typeof COVERAGE_PARTS)[number]['key'], string>> = { complete: theme.gain, fetching: theme.warn, stale: theme.withoutBest, failed: theme.loss, missing: theme.tooFew };
  return {
    grid: { left: 40, right: 12, top: 12, bottom: 28 },
    tooltip: {
      ...tooltipFrame(theme),
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: (params: unknown) => {
        const month = months[dataIndexOf(params) ?? -1];
        if (month === undefined) return '';
        const rows: TooltipRow[] = [{ label: 'Sessions', values: [String(month.sessions)] }, ...COVERAGE_PARTS.map((part) => ({ label: part.name, values: [String(month[part.key])] }))];
        const notes = ['The lake’s coverage now; the study ran on the data snapshot frozen at lock.', 'Sessions are the trading calendar’s, so weekends and holidays never count as missing.'];
        return tooltipHtml({ title: `${monthName(month)} · ${sessionsText(month.sessions)}`, columns: ['Sessions'], rows, notes }, theme);
      },
    },
    xAxis: { type: 'category', data: months.map((month) => `${MONTH_NAMES[month.month - 1]} ’${String(month.year).slice(-2)}`), axisLine: { lineStyle: { color: theme.axis } }, axisTick: { show: false }, axisLabel: { ...axisText(theme), hideOverlap: true } },
    yAxis: { type: 'value', minInterval: 1, splitNumber: 3, axisLabel: axisText(theme), splitLine: { lineStyle: { color: theme.gridLine } } },
    series: COVERAGE_PARTS.map((part) => ({
      id: `${part.key}:coverage`,
      name: part.name,
      type: 'bar' as const,
      stack: 'sessions',
      data: months.map((month) => month[part.key]),
      barMaxWidth: 14,
      itemStyle: { color: colors[part.key] },
      emphasis: { focus: 'series' as const, blurScope: 'global' as const },
    })),
  };
}
