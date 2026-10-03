import type { CustomSeriesOption } from 'echarts/charts';

import { formatTimestampDisplay } from '../../shared/timestamp';
import type { ChartSpec } from './charts/golden-search-chart-spec';
import { axisText, dataIndexOf, NOT_RECORDED, seriesIndexOf, signedPercentOrNotRecorded, tooltipFrame, tooltipHtml, type ChartTheme, type TooltipRow } from './charts/golden-search-chart-theme';
import type { ChartOption } from './charts/golden-search-echarts';
import { entryText, fullPointEntries, ratioText, signedPercentText, wholePercentText } from './golden-search-display';
import type { FoldChart, KnobDrift, LinkedFoldReturn, StrategyCapability, TestOverTimeCharts } from './golden-search.types';

/**
 * The Test over time charts (#2821): each fold's training and test windows
 * (V12), the search procedure's linked test return beside the frozen
 * incumbent's (V13), each fold's training and test Sharpe with the retention
 * the verdict reads (V14), each searched knob's winning value fold by fold
 * (V15), each fold's test return against the incumbent's (V16) and its test
 * trades, with the total against the forward minimum (V17). Every value is
 * the server's; a fold that did not run or failed shows no number, never zero.
 * Everything here is out-of-sample for the procedure: each fold's winner was
 * chosen on its training window only.
 */

const OUT_OF_SAMPLE = 'Each fold’s test window is out-of-sample for the winner chosen on its training window.';
const IN_PROGRESS = 'Testing over time has no verdict yet: this counts only the folds completed so far.';
const PROCEDURE = 'Search procedure';
const INCUMBENT = 'Current settings';
const STATUS_TEXT: Readonly<Record<FoldChart['status'], string>> = {
  planned: 'Planned, not run yet',
  pending: 'Not run yet',
  completed: 'Completed',
  failed: 'Failed',
};

function foldName(fold: { readonly fold_index: number }): string {
  return `Fold ${fold.fold_index + 1}`;
}

function dateEt(ms: number): string {
  return formatTimestampDisplay(ms, { mode: 'date-et' });
}

/** A half-open window as its first and last ET dates. */
function windowText(startMs: number, endMs: number): string {
  return `${dateEt(startMs)} to ${dateEt(endMs - 1)}`;
}

function ratioOrMissing(value: number | null): string {
  return value === null ? NOT_RECORDED : ratioText(value);
}

/** Retention in whole percent, as the verdict panel shows it; a completed fold without one says why. */
function retentionText(fold: FoldChart): string {
  return fold.retention === null ? (fold.status === 'completed' ? 'not defined' : NOT_RECORDED) : wholePercentText(fold.retention);
}


// ---------------------------------------------------------------- V12 fold timeline

/** Each fold as a row: its training window, then its test window coloured by how the fold ended. */
export function foldTimelineSpec(charts: TestOverTimeCharts, capability: StrategyCapability | null): ChartSpec {
  const winnerText = (fold: FoldChart) => (fold.winner === null ? 'no winner' : fullPointEntries(fold.winner, capability).map(entryText).join(' · '));
  const folds = charts.folds;
  const ran = folds.filter((fold) => fold.status === 'completed').length;
  const summary = charts.planned
    ? `${folds.length} planned folds, each a training window followed by a test window; testing over time has not run.`
    : `${folds.length} folds, each a training window followed by a test window; ${ran} completed.`;
  return {
    label: 'Fold timeline',
    summary,
    featured: null,
    option: (theme) => timelineOption(folds, capability, theme),
    table: {
      caption: 'Every fold’s windows (ET) and results; — marks a value not recorded',
      columns: ['Fold', 'Training', 'Test', 'Status', 'Winner', 'Training Sharpe', 'Test Sharpe', 'Retention', 'Test return', 'Test trades'],
      rows: folds.map((fold) => ({
        key: String(fold.fold_index),
        cells: [
          foldName(fold),
          windowText(fold.train_start_ms, fold.train_end_ms),
          windowText(fold.test_start_ms, fold.test_end_ms),
          fold.failure_reason === null ? STATUS_TEXT[fold.status] : `${STATUS_TEXT[fold.status]}: ${fold.failure_reason}`,
          winnerText(fold),
          ratioText(fold.train_sharpe),
          ratioText(fold.test_sharpe),
          retentionText(fold),
          signedPercentText(fold.test_return),
          fold.test_trades === null ? '—' : String(fold.test_trades),
        ],
      })),
    },
  };
}

/** A completed fold's test bar takes its return's colour (neutral at 0%); a failed one is outlined in the loss colour; one not run is grey. */
function testStyle(fold: FoldChart, theme: ChartTheme): { color: string; borderColor?: string; borderWidth?: number } {
  if (fold.status === 'failed') return { color: 'transparent', borderColor: theme.loss, borderWidth: 2 };
  if (fold.status !== 'completed') return { color: theme.tooFew };
  if (fold.test_return === null) return { color: 'transparent', borderColor: theme.textSecondary, borderWidth: 1 };
  return { color: fold.test_return > 0 ? theme.gain : fold.test_return < 0 ? theme.loss : theme.textSecondary };
}

/** A fold's row name, with its test return beside it so colour is never the only cue. */
function foldRowLabel(fold: FoldChart): string {
  return fold.test_return === null ? foldName(fold) : `${foldName(fold)} ${signedPercentText(fold.test_return)}`;
}

/** A window as a bar from its start to its end on the fold's row, outlined when a walkthrough or hover lights it up. */
function windowBar(outline: string): NonNullable<CustomSeriesOption['renderItem']> {
  return (_params, api) => {
    const start = api.coord([api.value(0), api.value(2)]);
    const end = api.coord([api.value(1), api.value(2)]);
    const size = api.size?.([0, 1]);
    const height = (Array.isArray(size) ? size[1] : 20) * 0.5;
    return {
      type: 'rect',
      shape: { x: start[0], y: start[1] - height / 2, width: Math.max(1, end[0] - start[0]), height },
      style: api.style(),
      emphasis: { style: { stroke: outline, lineWidth: 2 } },
    };
  };
}

function timelineOption(folds: readonly FoldChart[], capability: StrategyCapability | null, theme: ChartTheme): ChartOption {
  const windows = [
    { group: 'training', name: 'Training window', bars: folds.map((fold) => ({ value: [fold.train_start_ms, fold.train_end_ms, fold.fold_index], itemStyle: { color: theme.textSecondary, opacity: 0.45 } })) },
    { group: 'test', name: 'Test window', bars: folds.map((fold) => ({ value: [fold.test_start_ms, fold.test_end_ms, fold.fold_index], itemStyle: testStyle(fold, theme) })) },
  ];
  return {
    grid: { left: 56, right: 16, top: 8, bottom: 28 },
    tooltip: {
      ...tooltipFrame(theme),
      trigger: 'item',
      formatter: (params: unknown) => {
        const fold = folds[dataIndexOf(params) ?? -1];
        return fold === undefined ? '' : foldTooltip(fold, capability, theme);
      },
    },
    xAxis: { type: 'time', axisLine: { lineStyle: { color: theme.axis } }, axisLabel: { ...axisText(theme), hideOverlap: true, formatter: (value: number) => dateEt(value) }, splitLine: { lineStyle: { color: theme.gridLine } } },
    yAxis: { type: 'category', data: folds.map(foldRowLabel), inverse: true, axisLine: { lineStyle: { color: theme.axis } }, axisTick: { show: false }, axisLabel: axisText(theme) },
    series: windows.map((window) => ({
      id: `${window.group}:folds`,
      name: window.name,
      type: 'custom' as const,
      renderItem: windowBar(theme.text),
      encode: { x: [0, 1], y: 2 },
      data: window.bars,
    })),
  };
}

function foldTooltip(fold: FoldChart, capability: StrategyCapability | null, theme: ChartTheme): string {
  const rows: TooltipRow[] = [
    { label: 'Training', values: [windowText(fold.train_start_ms, fold.train_end_ms)] },
    { label: 'Test', values: [windowText(fold.test_start_ms, fold.test_end_ms)] },
    { label: 'Training Sharpe', values: [ratioOrMissing(fold.train_sharpe)] },
    { label: 'Test Sharpe', values: [ratioOrMissing(fold.test_sharpe)] },
    { label: 'Retention', values: [retentionText(fold)] },
    { label: 'Test return', values: [signedPercentOrNotRecorded(fold.test_return)] },
    { label: 'Test trades', values: [fold.test_trades === null ? NOT_RECORDED : String(fold.test_trades)] },
  ];
  const reason = fold.failure_reason === null ? [] : [fold.failure_reason];
  const winner = fold.winner === null ? [] : [`Winner: ${fullPointEntries(fold.winner, capability).map(entryText).join(' · ')}.`];
  return tooltipHtml({ title: `${foldName(fold)} · ${STATUS_TEXT[fold.status]}`, columns: ['Value'], rows, notes: [...reason, ...winner, 'Dates are Eastern; each window’s last day is shown.', OUT_OF_SAMPLE] }, theme);
}

// ---------------------------------------------------------------- V13 linked test return

function linkedValues(points: readonly LinkedFoldReturn[]): (number | '-')[] {
  return points.map((point) => point.linked_return ?? '-');
}

function gapText(points: readonly LinkedFoldReturn[], index: number): string {
  const point = points[index];
  if (point === undefined) return NOT_RECORDED;
  if (point.linked_return !== null) return signedPercentText(point.linked_return);
  return point.fold_missing ? 'fold missing' : 'line broken by an earlier missing fold';
}

/** Growth linked across the test folds, the procedure's against the incumbent's on the same windows. */
export function linkedReturnSpec(charts: TestOverTimeCharts): ChartSpec {
  const linked = charts.linked;
  const incumbent = charts.incumbent_linked;
  return {
    label: 'Linked test return',
    summary: `The search procedure's test returns linked across ${linked.length} folds end at ${gapText(linked, linked.length - 1)}; current settings on the same test windows end at ${gapText(incumbent, incumbent.length - 1)}.`,
    featured: null,
    option: (theme) => linkedOption(charts, theme),
    table: {
      caption: 'Linked test return by fold; a missing fold breaks the line',
      columns: ['Fold', 'Test ends (ET)', PROCEDURE, INCUMBENT],
      rows: linked.map((point, i) => ({ key: String(point.fold_index), cells: [foldName(point), dateEt(point.test_end_ms - 1), gapText(linked, i), gapText(incumbent, i)] })),
    },
  };
}

function linkedOption(charts: TestOverTimeCharts, theme: ChartTheme): ChartOption {
  const lines = [
    { id: 'procedure:linked', name: PROCEDURE, points: charts.linked, color: theme.candidates.all_period, dashed: false },
    { id: 'incumbent:linked', name: INCUMBENT, points: charts.incumbent_linked, color: theme.candidates.incumbent, dashed: true },
  ];
  return {
    grid: { left: 52, right: 72, top: 12, bottom: 28 },
    tooltip: {
      ...tooltipFrame(theme),
      trigger: 'axis',
      formatter: (params: unknown) => {
        const index = dataIndexOf(params);
        const point = charts.linked[index ?? -1];
        if (index === null || point === undefined) return '';
        const rows: TooltipRow[] = lines.map((line) => ({ label: line.name, values: [gapText(line.points, index)], swatch: { color: line.color, dashed: line.dashed } }));
        const notes = ['Each fold starts flat with fresh capital; the returns are linked, so a missing fold breaks the line.', 'This is the procedure’s record, not any one candidate’s.', OUT_OF_SAMPLE];
        return tooltipHtml({ title: `${foldName(point)} · test ends ${dateEt(point.test_end_ms - 1)} (ET)`, columns: ['Linked return'], rows, notes }, theme);
      },
    },
    xAxis: { type: 'category', data: charts.linked.map(foldName), boundaryGap: false, axisLine: { lineStyle: { color: theme.axis } }, axisLabel: axisText(theme) },
    yAxis: { type: 'value', splitNumber: 4, axisLabel: { ...axisText(theme), formatter: (value: number) => signedPercentText(value) }, splitLine: { lineStyle: { color: theme.gridLine } } },
    series: lines.map((line) => ({
      id: line.id,
      name: line.name,
      type: 'line' as const,
      data: linkedValues(line.points),
      connectNulls: false,
      symbolSize: 6,
      lineStyle: { color: line.color, width: 2, type: line.dashed ? ('dashed' as const) : ('solid' as const) },
      itemStyle: { color: line.color },
      endLabel: { show: true, color: line.color, fontSize: 11, formatter: () => gapText(line.points, line.points.length - 1) },
      emphasis: { focus: 'series' as const, blurScope: 'global' as const },
    })),
  };
}

// ---------------------------------------------------------------- V14 training vs test Sharpe

/** Each fold's training and test Sharpe side by side, labelled with the retention the verdict reads. */
export function sharpeRetentionSpec(charts: TestOverTimeCharts): ChartSpec {
  const median = charts.median_retention === null ? 'no median retention' : `a median retention of ${wholePercentText(charts.median_retention)}`;
  return {
    label: 'Training against test Sharpe',
    summary: `Each fold's training and test Sharpe, with ${median}; the verdict asks for ${wholePercentText(charts.retention_threshold)} or more.`,
    featured: null,
    option: (theme) => sharpeOption(charts, theme),
    table: {
      caption: 'Training and test Sharpe by fold, and the retention between them',
      columns: ['Fold', 'Training Sharpe', 'Test Sharpe', 'Retention'],
      rows: charts.folds.map((fold) => ({ key: String(fold.fold_index), cells: [foldName(fold), ratioText(fold.train_sharpe), ratioText(fold.test_sharpe), retentionText(fold)] })),
    },
  };
}

function sharpeOption(charts: TestOverTimeCharts, theme: ChartTheme): ChartOption {
  const folds = charts.folds;
  const bars = [
    { group: 'training', name: 'Training Sharpe', values: folds.map((fold) => fold.train_sharpe ?? '-'), color: theme.textSecondary },
    { group: 'test', name: 'Test Sharpe', values: folds.map((fold) => fold.test_sharpe ?? '-'), color: theme.candidates.all_period },
  ];
  return {
    grid: { left: 40, right: 12, top: 24, bottom: 28 },
    tooltip: {
      ...tooltipFrame(theme),
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: (params: unknown) => {
        const fold = folds[dataIndexOf(params) ?? -1];
        if (fold === undefined) return '';
        const rows: TooltipRow[] = [
          { label: 'Training Sharpe', values: [ratioOrMissing(fold.train_sharpe)] },
          { label: 'Test Sharpe', values: [ratioOrMissing(fold.test_sharpe)] },
          { label: 'Retention', values: [retentionText(fold)] },
        ];
        const rule = `Retention is the test Sharpe over the training Sharpe, defined only when the training Sharpe is above 0. The verdict reads the median over folds against ${wholePercentText(charts.retention_threshold)}.`;
        return tooltipHtml({ title: `${foldName(fold)} · ${STATUS_TEXT[fold.status]}`, columns: ['Value'], rows, notes: [rule, OUT_OF_SAMPLE] }, theme);
      },
    },
    xAxis: { type: 'category', data: folds.map(foldName), axisLine: { lineStyle: { color: theme.axis } }, axisTick: { show: false }, axisLabel: axisText(theme) },
    yAxis: { type: 'value', splitNumber: 3, axisLabel: axisText(theme), splitLine: { lineStyle: { color: theme.gridLine } } },
    series: bars.map((bar) => ({
      id: `${bar.group}:sharpe`,
      name: bar.name,
      type: 'bar' as const,
      data: bar.values,
      barMaxWidth: 16,
      itemStyle: { color: bar.color },
      emphasis: { focus: 'series' as const, blurScope: 'global' as const },
      ...(bar.group === 'test'
        ? { label: { show: true, position: 'top' as const, color: theme.text, fontSize: 10, formatter: (params: { dataIndex: number }) => keepsText(folds[params.dataIndex]) } }
        : {}),
    })),
  };
}

function keepsText(fold: FoldChart | undefined): string {
  return fold?.retention === null || fold?.retention === undefined ? '' : `keeps ${wholePercentText(fold.retention)}`;
}

// ---------------------------------------------------------------- V15 parameter drift

const DRIFT_ROW = 92;

/** Each searched knob's winning value fold by fold, on its own scale, against the all-period winner and the current settings. */
export function parameterDriftSpec(charts: TestOverTimeCharts): ChartSpec {
  const knobs = charts.drift;
  return {
    label: 'Parameter drift',
    summary: `The winning value of each of ${knobs.length} searched knobs in each fold, each on its own range, against the all-period winner and the current settings.`,
    featured: null,
    option: (theme) => driftOption(charts, theme),
    table: {
      caption: 'Each searched knob’s winning value by fold; — marks a fold without a winner',
      columns: ['Knob', ...charts.folds.map(foldName), 'All-period winner', INCUMBENT],
      rows: knobs.map((knob) => ({
        key: knob.name,
        cells: [knob.label, ...knob.folds.map((value) => (value === null ? '—' : String(value))), knob.all_period === null ? '—' : String(knob.all_period), String(knob.current)],
      })),
    },
  };
}

/** Every value a knob's row draws: the fold winners and the all-period winner. */
function present(knob: KnobDrift): number[] {
  return [...knob.folds, knob.all_period].filter((value): value is number => value !== null);
}

/** The chart's height for `count` knobs: a row each. */
export function driftHeight(count: number): number {
  return 40 + DRIFT_ROW * Math.max(count, 1);
}

function driftOption(charts: TestOverTimeCharts, theme: ChartTheme): ChartOption {
  const knobs = charts.drift;
  const folds = charts.folds.map(foldName);
  const reference = (knob: KnobDrift) => [
    // Labels above and below their lines, so close values never print over each other.
    ...(knob.all_period === null
      ? []
      : [{ yAxis: knob.all_period, name: 'all-period', lineStyle: { color: theme.candidates.all_period, type: 'dashed' as const }, label: { position: 'insideEndTop' as const } }]),
    { yAxis: knob.current, name: 'current', lineStyle: { color: theme.candidates.incumbent, type: 'dotted' as const }, label: { position: 'insideEndBottom' as const } },
  ];
  return {
    grid: knobs.map((_, i) => ({ left: 56, right: 24, top: 32 + i * DRIFT_ROW, height: DRIFT_ROW - 48 })),
    tooltip: {
      ...tooltipFrame(theme),
      trigger: 'item',
      formatter: (params: unknown) => {
        const knob = knobs[seriesIndexOf(params) ?? -1];
        const index = dataIndexOf(params);
        if (knob === undefined || index === null) return '';
        const value = knob.folds[index];
        const rows: TooltipRow[] = [
          { label: `${folds[index]} winner`, values: [value === null ? 'no winner' : String(value)] },
          { label: 'All-period winner', values: [knob.all_period === null ? NOT_RECORDED : String(knob.all_period)], swatch: { color: theme.candidates.all_period, dashed: true } },
          { label: INCUMBENT, values: [String(knob.current)], swatch: { color: theme.candidates.incumbent, dashed: true } },
        ];
        return tooltipHtml({ title: `${knob.label} (${knob.unit})`, columns: ['Value'], rows, notes: [`Searched from ${knob.low} to ${knob.high}. Each fold searched again from the same starting point on its own training window.`] }, theme);
      },
    },
    xAxis: knobs.map((_, i) => ({
      type: 'category' as const,
      gridIndex: i,
      data: folds,
      axisLine: { lineStyle: { color: theme.axis } },
      axisTick: { show: false },
      axisLabel: { ...axisText(theme), show: i === knobs.length - 1 },
    })),
    yAxis: knobs.map((knob, i) => ({
      type: 'value' as const,
      gridIndex: i,
      // Every value drawn stays on the axis, even one outside the searched range (a kept starting value, say).
      min: Math.min(knob.low, knob.current, ...present(knob)),
      max: Math.max(knob.high, knob.current, ...present(knob)),
      splitNumber: 2,
      name: knob.label,
      nameLocation: 'end' as const,
      nameTextStyle: { ...axisText(theme), align: 'left' as const },
      axisLabel: axisText(theme),
      splitLine: { lineStyle: { color: theme.gridLine } },
    })),
    series: knobs.map((knob, i) => ({
      id: `winners:${knob.name}`,
      name: knob.label,
      type: 'line' as const,
      xAxisIndex: i,
      yAxisIndex: i,
      data: knob.folds.map((value) => value ?? '-'),
      connectNulls: false,
      symbolSize: 7,
      lineStyle: { color: theme.text, width: 1.5 },
      itemStyle: { color: theme.text },
      emphasis: { focus: 'series' as const, blurScope: 'global' as const },
      markLine: {
        silent: true,
        symbol: 'none',
        label: { color: theme.textSecondary, fontSize: 10, formatter: (params: { name?: string }) => params.name ?? '' },
        data: reference(knob),
      },
      // The searched range as a band.
      markArea: { silent: true, itemStyle: { color: theme.gridLine, opacity: 0.5 }, data: [[{ yAxis: knob.low }, { yAxis: knob.high }]] },
    })),
  };
}

// ---------------------------------------------------------------- V16 test return per fold

/** The incumbent's return on a fold's test window, or that its run failed. */
function incumbentText(fold: FoldChart): string {
  return fold.incumbent_failure === null ? signedPercentOrNotRecorded(fold.incumbent_return) : `run failed: ${fold.incumbent_failure}`;
}

/** Each fold's test return for the procedure's winner and for the current settings on the same window. */
export function foldReturnsSpec(charts: TestOverTimeCharts): ChartSpec {
  return {
    label: 'Test return per fold',
    summary: `Each fold's test return for the winner its training chose, beside the current settings on the same test window, across ${charts.folds.length} folds.`,
    featured: null,
    option: (theme) => returnsOption(charts, theme),
    table: {
      caption: 'Test return by fold for the search procedure and the current settings; — marks a value not recorded',
      columns: ['Fold', PROCEDURE, INCUMBENT, 'Difference'],
      rows: charts.folds.map((fold) => ({
        key: String(fold.fold_index),
        cells: [foldName(fold), signedPercentText(fold.test_return), fold.incumbent_failure === null ? signedPercentText(fold.incumbent_return) : 'run failed', signedPercentText(fold.return_difference)],
      })),
    },
  };
}

function returnsOption(charts: TestOverTimeCharts, theme: ChartTheme): ChartOption {
  const folds = charts.folds;
  const bars = [
    { group: 'procedure', name: PROCEDURE, values: folds.map((fold) => fold.test_return ?? '-'), color: theme.candidates.all_period },
    { group: 'incumbent', name: INCUMBENT, values: folds.map((fold) => fold.incumbent_return ?? '-'), color: theme.candidates.incumbent },
  ];
  return {
    grid: { left: 52, right: 12, top: 12, bottom: 28 },
    tooltip: {
      ...tooltipFrame(theme),
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: (params: unknown) => {
        const fold = folds[dataIndexOf(params) ?? -1];
        if (fold === undefined) return '';
        const rows: TooltipRow[] = [
          { label: PROCEDURE, values: [signedPercentOrNotRecorded(fold.test_return)], swatch: { color: theme.candidates.all_period, dashed: false } },
          { label: INCUMBENT, values: [incumbentText(fold)], swatch: { color: theme.candidates.incumbent, dashed: false } },
          { label: 'Difference', values: [signedPercentOrNotRecorded(fold.return_difference)] },
        ];
        return tooltipHtml({ title: `${foldName(fold)} · test ${windowText(fold.test_start_ms, fold.test_end_ms)} (ET)`, columns: ['Test return'], rows, notes: ['Each return is on the fold’s fresh starting capital.', OUT_OF_SAMPLE] }, theme);
      },
    },
    xAxis: { type: 'category', data: folds.map(foldName), axisLine: { lineStyle: { color: theme.axis } }, axisTick: { show: false }, axisLabel: axisText(theme) },
    yAxis: { type: 'value', splitNumber: 4, axisLabel: { ...axisText(theme), formatter: (value: number) => signedPercentText(value) }, splitLine: { lineStyle: { color: theme.gridLine } } },
    series: bars.map((bar) => ({
      id: `${bar.group}:returns`,
      name: bar.name,
      type: 'bar' as const,
      data: bar.values,
      barMaxWidth: 16,
      itemStyle: { color: bar.color },
      emphasis: { focus: 'series' as const, blurScope: 'global' as const },
    })),
  };
}

// ---------------------------------------------------------------- V17 test activity per fold

const ALL_FOLDS = 'All folds';

/** Each fold's test trades, and beside them the total against the trades the forward tests must reach together. */
export function foldActivitySpec(charts: TestOverTimeCharts): ChartSpec {
  const total = charts.test_trades_total;
  const minimum = charts.forward_minimum;
  const against = minimum === null ? '' : ` against a minimum of ${minimum}`;
  const together = charts.in_progress ? `so far ${total ?? NOT_RECORDED}, before the verdict` : `together ${total ?? NOT_RECORDED}${against}`;
  return {
    label: 'Test activity per fold',
    summary: `Each fold's test trades; ${together}.`,
    featured: null,
    option: (theme) => activityOption(charts, theme),
    table: {
      caption: 'Test trades by fold, and the total over completed folds',
      columns: ['Fold', 'Test trades', `${INCUMBENT}, same test`],
      rows: [
        ...charts.folds.map((fold) => ({
          key: String(fold.fold_index),
          cells: [foldName(fold), fold.test_trades === null ? '—' : String(fold.test_trades), fold.incumbent_failure === null ? (fold.incumbent_trades === null ? '—' : String(fold.incumbent_trades)) : 'run failed'],
        })),
        { key: 'total', cells: [charts.in_progress ? `${ALL_FOLDS} so far, before the verdict` : `${ALL_FOLDS}${minimum === null ? '' : ` (minimum ${minimum})`}`, total === null ? '—' : String(total), '—'] },
      ],
    },
  };
}

function activityOption(charts: TestOverTimeCharts, theme: ChartTheme): ChartOption {
  const folds = charts.folds;
  const total = charts.test_trades_total;
  const minimum = charts.forward_minimum;
  // The verdict's own trade-floor fact, from the server.
  const short = charts.below_minimum === true;
  return {
    grid: [
      { left: 40, right: '34%', top: 20, bottom: 28 },
      { left: '74%', right: 12, top: 20, bottom: 28 },
    ],
    tooltip: {
      ...tooltipFrame(theme),
      trigger: 'item',
      formatter: (params: unknown) => {
        if (seriesIndexOf(params) === 1) {
          const rows: TooltipRow[] = [
            { label: 'Test trades', values: [total === null ? NOT_RECORDED : String(total)] },
            { label: 'Forward minimum', values: [minimum === null ? NOT_RECORDED : String(minimum)] },
          ];
          const note = 'The verdict counts the winners’ test trades over completed folds and needs at least the minimum across all forward tests; no fold has a minimum of its own.';
          return tooltipHtml({ title: charts.in_progress ? `${ALL_FOLDS} so far` : ALL_FOLDS, columns: ['Count'], rows, notes: [...(charts.in_progress ? [IN_PROGRESS] : []), note, OUT_OF_SAMPLE] }, theme);
        }
        const fold = folds[dataIndexOf(params) ?? -1];
        if (fold === undefined) return '';
        const rows: TooltipRow[] = [
          { label: 'Test trades', values: [fold.test_trades === null ? NOT_RECORDED : String(fold.test_trades)] },
          { label: `${INCUMBENT}, same test`, values: [fold.incumbent_failure === null ? (fold.incumbent_trades === null ? NOT_RECORDED : String(fold.incumbent_trades)) : `run failed: ${fold.incumbent_failure}`] },
        ];
        return tooltipHtml({ title: `${foldName(fold)} · ${STATUS_TEXT[fold.status]}`, columns: ['Trades'], rows, notes: [OUT_OF_SAMPLE] }, theme);
      },
    },
    xAxis: [
      { type: 'category', gridIndex: 0, data: folds.map(foldName), axisLine: { lineStyle: { color: theme.axis } }, axisTick: { show: false }, axisLabel: axisText(theme) },
      { type: 'category', gridIndex: 1, data: [ALL_FOLDS], axisLine: { lineStyle: { color: theme.axis } }, axisTick: { show: false }, axisLabel: axisText(theme) },
    ],
    yAxis: [
      { type: 'value', gridIndex: 0, minInterval: 1, splitNumber: 3, axisLabel: axisText(theme), splitLine: { lineStyle: { color: theme.gridLine } } },
      { type: 'value', gridIndex: 1, minInterval: 1, splitNumber: 3, max: (extent: { max: number }) => Math.max(extent.max, minimum ?? 0), axisLabel: axisText(theme), splitLine: { lineStyle: { color: theme.gridLine } } },
    ],
    series: [
      {
        id: 'folds:trades',
        name: 'Test trades',
        type: 'bar',
        xAxisIndex: 0,
        yAxisIndex: 0,
        data: folds.map((fold) => fold.test_trades ?? '-'),
        barMaxWidth: 22,
        itemStyle: { color: theme.candidates.all_period },
        emphasis: { focus: 'series', blurScope: 'global' },
      },
      {
        id: 'total:trades',
        name: ALL_FOLDS,
        type: 'bar',
        xAxisIndex: 1,
        yAxisIndex: 1,
        data: [total ?? '-'],
        barMaxWidth: 28,
        itemStyle: { color: short ? theme.loss : theme.candidates.all_period },
        label: { show: total !== null, position: 'top', color: theme.text, fontSize: 11, formatter: () => (short ? `${total} · below the minimum` : charts.in_progress ? `${total} so far` : String(total)) },
        emphasis: { focus: 'series', blurScope: 'global' },
        ...(minimum === null
          ? {}
          : {
              markLine: {
                silent: true,
                symbol: 'none',
                lineStyle: { color: theme.warn, type: 'dashed', width: 1 },
                label: { color: theme.warn, fontSize: 10, position: 'insideStartTop', formatter: `minimum ${minimum}` },
                data: [{ yAxis: minimum }],
              },
            }),
      },
    ],
  };
}
