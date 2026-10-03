import { formatTimestampDisplay } from '../../shared/timestamp';
import type { ChartSpec } from './charts/golden-search-chart-spec';
import { dataIndexOf, NOT_RECORDED, seriesIndexOf, tooltipFrame, tooltipHtml, type ChartTheme, type TooltipRow } from './charts/golden-search-chart-theme';
import type { ChartOption } from './charts/golden-search-echarts';
import type { CandidateRow } from './golden-search-compare';
import { knobsByName, metricTexts, percentText, ratioText, signedPercentText } from './golden-search-display';
import type { CandidateKey, CumulativeReturnPoint, EvidenceCandidate, EvidenceCellStatus, Metrics, NeighborRow, StrategyCapability, StressResult } from './golden-search.types';

/**
 * The Compare charts beside the equity chart (#2821): each candidate card's
 * small return line (V18), the candidates side by side (V21), the neighbor
 * tornado (V22) and the cost stress ladder (V23). Every plotted value is the
 * server's — returns, changes from the candidate or the base run, trades per
 * trading year, the stress tally. A value the study did not record is left
 * out of the drawing (`'-'`) and reads "not recorded", never zero.
 */

const DEVELOPMENT = 'Development data, used for choosing.';

export const NEIGHBOR_STATUS: Readonly<Record<EvidenceCellStatus, string>> = {
  center: 'This candidate',
  tested: 'Tested',
  failed: 'Run failed',
  invalid: 'Invalid',
  outside_domain: 'Outside the legal range',
  untested: 'Not tested',
};

const ONE_DECIMAL = new Intl.NumberFormat('en-US', { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const WHOLE = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 });
/** An axis tick as a short signed percent (`+4%`, `-2.5%`), so ticks do not crowd. */
const AXIS_PERCENT = new Intl.NumberFormat('en-US', { style: 'percent', maximumFractionDigits: 1, signDisplay: 'exceptZero' });

/** A category label that also says, on a second line, when its row loses money. */
function lossLabel(losing: (index: number) => boolean): (value: string, index: number) => string {
  return (value, index) => (losing(index) ? `${value}\n{loss|loses money}` : value);
}

// ---------------------------------------------------------------- V18 card line

/** One candidate's development return as a small line on its card; the equity chart below lists every value. */
export function sparklineSpec(key: CandidateKey, label: string, points: readonly CumulativeReturnPoint[]): ChartSpec {
  const dates = points.map((point) => formatTimestampDisplay(point.ms, { mode: 'date-et' }));
  const values = points.map((point) => point.value);
  return {
    label: `${label} development cumulative return`,
    summary: `Ends at ${signedPercentText(values.at(-1))}.`,
    featured: key,
    option: (theme) => {
      const color = theme.candidates[key];
      return {
        grid: { left: 2, right: 2, top: 4, bottom: 4 },
        tooltip: {
          ...tooltipFrame(theme),
          trigger: 'axis',
          formatter: (params: unknown) => {
            const index = dataIndexOf(params);
            if (index === null) return '';
            const rows = [{ label, swatch: { color, dashed: false }, values: [signedPercentText(values[index])] }];
            return tooltipHtml({ title: `${dates[index]} · at the session close`, columns: ['Return'], rows, notes: [DEVELOPMENT] }, theme);
          },
        },
        xAxis: { type: 'category', data: dates, show: false, boundaryGap: false },
        // The zero line stays in view, so a line above it is a gain.
        yAxis: { type: 'value', show: false, min: (extent: { min: number }) => Math.min(extent.min, 0), max: (extent: { max: number }) => Math.max(extent.max, 0) },
        series: [
          {
            id: `equity:${key}`,
            type: 'line',
            data: [...values],
            showSymbol: false,
            lineStyle: { color, width: 1.5 },
            itemStyle: { color },
            areaStyle: { color, opacity: 0.12 },
            markLine: { silent: true, symbol: 'none', label: { show: false }, lineStyle: { color: theme.axis, type: 'solid', width: 1 }, data: [{ yAxis: 0 }] },
          },
        ],
      };
    },
    table: {
      caption: `${label} development cumulative return at each session close (ET)`,
      columns: ['Session', 'Return'],
      rows: points.map((point, i) => ({ key: String(point.ms), cells: [dates[i], signedPercentText(point.value)] })),
    },
  };
}

// ---------------------------------------------------------------- V21 side by side

interface Measure {
  readonly label: string;
  /** What the measure is, for the hover. */
  readonly note: string;
  readonly value: (row: CandidateRow) => number | null;
  /** A value as its cells read it. */
  readonly text: (value: number | null) => string;
  /** An axis tick, when it reads shorter than a cell. */
  readonly tick?: (value: number) => string;
  /** A candidate's cell when it says more than the value (the stress tally's scheduled runs). */
  readonly detail?: (row: CandidateRow) => string;
  /** Drawn reversed so that further right is better. */
  readonly inverse?: boolean;
  readonly min?: number;
  readonly max?: (rows: readonly CandidateRow[]) => number;
}

function completed(row: CandidateRow): Metrics | null {
  const metrics = row.candidate.development_metrics;
  return metrics?.status === 'completed' ? metrics : null;
}

function stressDetail(row: CandidateRow): string {
  const tally = row.candidate.stress_tally;
  if (tally.recorded === 0) return '—';
  const recorded = tally.recorded === tally.scenarios ? '' : ` (${tally.recorded} recorded)`;
  return `${tally.in_profit} of ${tally.scenarios}${recorded}`;
}

/** A candidate's value on `measure` as its cell reads it. */
function cellText(measure: Measure, row: CandidateRow): string {
  return measure.detail?.(row) ?? measure.text(measure.value(row));
}

const MEASURES: readonly Measure[] = [
  {
    label: 'Net return',
    note: 'Net return after costs, as a share of starting capital.',
    value: (row) => completed(row)?.total_return_pct ?? null,
    text: signedPercentText,
  },
  {
    label: 'Sharpe',
    note: 'Return for each unit of day-to-day swing; higher is steadier.',
    value: (row) => completed(row)?.sharpe_ratio ?? null,
    text: ratioText,
  },
  {
    label: 'Worst fall',
    note: 'The deepest fall from a peak on the engine’s bar-by-bar equity. Drawn reversed: a smaller fall sits further right.',
    value: (row) => completed(row)?.max_drawdown_pct ?? null,
    text: percentText,
    inverse: true,
  },
  {
    label: 'Trades a year',
    note: 'Development trades per trading year, counted on the exchange calendar.',
    value: (row) => row.candidate.trades_per_year,
    text: (value) => (value === null ? '—' : ONE_DECIMAL.format(value)),
    tick: (value) => WHOLE.format(value),
  },
  {
    label: 'Win rate',
    note: 'The share of trades that made money.',
    value: (row) => completed(row)?.win_rate ?? null,
    text: percentText,
  },
  {
    label: 'Stress runs in profit',
    note: 'Reruns under harsher costs and fills that still made money, of the scenarios the plan scheduled.',
    // No stress run recorded is a missing value, never zero runs in profit.
    value: (row) => (row.candidate.stress_tally.recorded === 0 ? null : row.candidate.stress_tally.in_profit),
    text: (value) => (value === null ? '—' : String(value)),
    detail: stressDetail,
    min: 0,
    max: (rows) => Math.max(1, ...rows.map((row) => row.candidate.stress_tally.scenarios)),
  },
];

const SIDE_LABEL = 'Candidates on six development measures';

/**
 * The candidates side by side (#2821, V21): one row per measure, each on its
 * own scale, a dot per candidate; further right is better on every row. The
 * selected candidate's dots are larger. Series ids are
 * `selected|other:<candidate>:<measure>`.
 */
export function sideBySideSpec(rows: readonly CandidateRow[], featured: CandidateKey): ChartSpec {
  const names = rows.map((row) => row.candidate.label);
  return {
    label: SIDE_LABEL,
    summary: `Net return, Sharpe, worst fall, trades a year, win rate and stress runs in profit for ${names.join(', ')}, each measure on its own scale; further right is better.`,
    featured,
    option: (theme) => sideBySideOption(rows, featured, theme),
    table: {
      caption: `${SIDE_LABEL}; — marks a value the study did not record`,
      columns: ['Measure', ...names],
      rows: MEASURES.map((measure) => ({ key: measure.label, cells: [measure.label, ...rows.map((row) => cellText(measure, row))] })),
    },
  };
}

const ROW_PITCH = 46;

function sideBySideOption(rows: readonly CandidateRow[], featured: CandidateKey, theme: ChartTheme): ChartOption {
  const axisLabel = { color: theme.textSecondary, fontSize: 10 };
  // Series run candidate by candidate, one per measure, so series i plots measure i mod the measure count.
  const measureOf = (series: number | null): Measure | undefined => (series === null ? undefined : MEASURES[series % MEASURES.length]);
  return {
    grid: MEASURES.map((_, i) => ({ left: 128, right: 28, top: 10 + i * ROW_PITCH, height: 14 })),
    tooltip: {
      ...tooltipFrame(theme),
      trigger: 'item',
      formatter: (params: unknown) => {
        const measure = measureOf(seriesIndexOf(params));
        if (measure === undefined) return '';
        const lines: TooltipRow[] = rows.map((row) => {
          const value = measure.value(row) === null ? NOT_RECORDED : cellText(measure, row);
          return { label: row.candidate.label, swatch: { color: theme.candidates[row.key], dashed: false }, values: [value] };
        });
        return tooltipHtml({ title: measure.label, columns: ['Value'], rows: lines, notes: [measure.note, DEVELOPMENT] }, theme);
      },
    },
    xAxis: MEASURES.map((measure, i) => ({
      type: 'value' as const,
      gridIndex: i,
      scale: true,
      inverse: measure.inverse ?? false,
      min: measure.min,
      max: measure.max?.(rows),
      splitNumber: 2,
      axisLine: { lineStyle: { color: theme.axis } },
      axisTick: { show: false },
      splitLine: { show: false },
      axisLabel: { ...axisLabel, hideOverlap: true, formatter: (value: number) => measure.tick?.(value) ?? measure.text(value) },
    })),
    yAxis: MEASURES.map((measure, i) => ({
      type: 'category' as const,
      gridIndex: i,
      data: [measure.label],
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: theme.text, fontSize: 12, width: 120, overflow: 'truncate' as const },
    })),
    series: rows.flatMap((row) =>
      MEASURES.map((measure, i) => {
        const value = measure.value(row);
        const selected = row.key === featured;
        return {
          id: `${selected ? 'selected' : 'other'}:${row.key}:${i}`,
          name: row.candidate.label,
          type: 'scatter' as const,
          xAxisIndex: i,
          yAxisIndex: i,
          data: value === null ? [] : [[value, 0]],
          symbolSize: selected ? 14 : 10,
          z: selected ? 3 : 2,
          itemStyle: { color: theme.candidates[row.key], borderColor: theme.tooltipBackground, borderWidth: 1.5 },
          emphasis: { focus: 'series' as const, blurScope: 'global' as const, scale: 1.3 },
        };
      }),
    ),
  };
}

// ---------------------------------------------------------------- V22 neighbor tornado

const TORNADO_LABEL = 'Change in development net return one step either side of the candidate, per knob';

interface KnobSides {
  readonly knob: string;
  readonly label: string;
  readonly center: NeighborRow | null;
  readonly below: NeighborRow | null;
  readonly above: NeighborRow | null;
}

/** A tested neighbor (its run completed) whose net profit is below zero. */
function loses(row: NeighborRow | null): boolean {
  const net = row?.status === 'tested' ? row.metrics?.net_profit : null;
  return net !== null && net !== undefined && net < 0;
}

function sideText(row: NeighborRow | null): string {
  if (row === null) return NOT_RECORDED;
  if ((row.status === 'tested' || row.status === 'center') && row.metrics?.status === 'completed') return signedPercentText(row.metrics.total_return_pct);
  // The center row keeps status `center` even when the candidate's own run failed.
  return NEIGHBOR_STATUS[row.metrics?.status === 'failed' ? 'failed' : row.status];
}

/**
 * The neighbor tornado (#2821, V22): for each searched knob, the change in
 * net return one step below (lighter) and one step above (darker) the
 * candidate's value, every other knob held. Bars left of 0 lose return;
 * a neighbor that loses money is drawn in the loss colour and says so.
 * A step that was not tested has no bar; the hover and the table say why.
 */
export function tornadoSpec(candidate: EvidenceCandidate, capability: StrategyCapability | null): ChartSpec {
  const knobs = knobsByName(capability);
  const sides: KnobSides[] = candidate.neighbors.map((hood) => ({
    knob: hood.knob,
    label: knobs.get(hood.knob)?.label ?? hood.knob,
    center: hood.rows.find((row) => row.step === 0) ?? null,
    below: hood.rows.find((row) => row.step === -1) ?? null,
    above: hood.rows.find((row) => row.step === 1) ?? null,
  }));
  const losing = sides.filter((side) => loses(side.below) || loses(side.above)).map((side) => side.label);
  return {
    label: TORNADO_LABEL,
    summary: `${candidate.label} with ${sides.length === 1 ? 'its one knob' : `each of its ${sides.length} knobs`} moved one step either side; ${losing.length > 0 ? `a step on ${losing.join(', ')} loses money` : 'no tested step loses money'}.`,
    featured: null,
    option: (theme) => tornadoOption(sides, candidate, theme),
    table: {
      caption: `${candidate.label}: nearby settings, one knob moved one step either side, everything else held; — marks a value not recorded`,
      columns: ['Knob', 'Step', 'Value', 'Status', 'Net return', 'Change from candidate', 'Worst fall', 'Trades', 'Sharpe'],
      rows: sides.flatMap((side) =>
        [side.below, side.center, side.above]
          .filter((row): row is NeighborRow => row !== null)
          .map((row) => {
            const figures = metricTexts(row.metrics);
            const reason = row.reason ?? figures.failure;
            return {
              key: `${side.knob}|${row.step}`,
              cells: [
                side.label,
                row.step === 0 ? 'Candidate' : row.step < 0 ? 'One below' : 'One above',
                String(row.value),
                reason === null ? NEIGHBOR_STATUS[row.status] : `${NEIGHBOR_STATUS[row.status]} — ${reason}`,
                figures.netReturn,
                signedPercentText(row.return_change),
                figures.worstFall,
                figures.trades,
                figures.sharpe,
              ],
            };
          }),
      ),
    },
  };
}

function tornadoOption(sides: readonly KnobSides[], candidate: EvidenceCandidate, theme: ChartTheme): ChartOption {
  const axisLabel = { color: theme.textSecondary, fontSize: 11 };
  const bar = (group: 'below' | 'above', pick: (side: KnobSides) => NeighborRow | null, name: string, color: string) => ({
    id: `${group}:steps`,
    name,
    type: 'bar' as const,
    data: sides.map((side) => pick(side)?.return_change ?? '-'),
    barMaxWidth: 12,
    barGap: '25%',
    itemStyle: { color: (params: { dataIndex: number }) => (loses(pick(sides[params.dataIndex])) ? theme.loss : color) },
    emphasis: { focus: 'series' as const, blurScope: 'global' as const },
  });
  const below = bar('below', (side) => side.below, 'One step below', theme.stepBelow);
  return {
    grid: { left: 112, right: 24, top: 28, bottom: 40 },
    tooltip: {
      ...tooltipFrame(theme),
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: (params: unknown) => {
        const side = sides[dataIndexOf(params) ?? -1];
        return side === undefined ? '' : tornadoTooltip(side, theme);
      },
    },
    xAxis: {
      type: 'value',
      name: `Change from ${candidate.label} (${metricTexts(candidate.development_metrics).netReturn})`,
      nameLocation: 'middle',
      nameGap: 26,
      nameTextStyle: { color: theme.textSecondary, fontSize: 11 },
      splitNumber: 4,
      axisLabel: { ...axisLabel, hideOverlap: true, formatter: (value: number) => AXIS_PERCENT.format(value) },
      splitLine: { lineStyle: { color: theme.gridLine } },
    },
    yAxis: {
      type: 'category',
      inverse: true,
      data: sides.map((side) => side.label),
      axisLine: { lineStyle: { color: theme.axis } },
      axisTick: { show: false },
      axisLabel: {
        color: theme.text,
        fontSize: 12,
        width: 104,
        overflow: 'truncate',
        formatter: lossLabel((index) => loses(sides[index].below) || loses(sides[index].above)),
        rich: { loss: { color: theme.loss, fontSize: 10 } },
      },
    },
    series: [
      {
        ...below,
        markLine: { silent: true, symbol: 'none', label: { show: false }, lineStyle: { color: theme.text, type: 'dashed', width: 1 }, data: [{ xAxis: 0 }] },
      },
      bar('above', (side) => side.above, 'One step above', theme.stepAbove),
    ],
  };
}

function tornadoTooltip(side: KnobSides, theme: ChartTheme): string {
  const row = (label: string, color: string | null, item: NeighborRow | null): TooltipRow => ({
    label,
    ...(color === null ? {} : { swatch: { color, dashed: false } }),
    values: [item === null ? '—' : String(item.value), sideText(item), item === null || item.step === 0 ? '—' : signedPercentText(item.return_change)],
  });
  const reasons = [side.below, side.above].filter((item): item is NeighborRow => item !== null && item.reason !== null).map((item) => `${item.step < 0 ? 'One below' : 'One above'}: ${item.reason}`);
  return tooltipHtml(
    {
      title: `${side.label} · one step either side`,
      columns: ['Value', 'Net return', 'Change'],
      rows: [row('One step below', theme.stepBelow, side.below), row('Candidate', null, side.center), row('One step above', theme.stepAbove, side.above)],
      notes: [...reasons, 'Every other knob is held at the candidate’s value.', DEVELOPMENT],
    },
    theme,
  );
}

// ---------------------------------------------------------------- V23 cost stress ladder

const STRESS_LABEL = 'Development net return under each cost stress';

interface Rung {
  readonly key: string;
  readonly label: string;
  readonly metrics: Metrics | null;
  /** Null for the base run itself. */
  readonly change: number | null;
  readonly base: boolean;
}

function rungReturn(rung: Rung): number | null {
  return rung.metrics?.status === 'completed' ? rung.metrics.total_return_pct : null;
}

function rungLoses(rung: Rung): boolean {
  return rung.metrics?.status === 'completed' && rung.metrics.net_profit !== null && rung.metrics.net_profit < 0;
}

/**
 * The cost stress ladder (#2821, V23): the candidate's development net
 * return at the study's own costs, then rerun under each predeclared
 * stress. A stress that turns the result into a loss is drawn in the loss
 * colour and says so; a run that failed has no bar.
 */
export function stressSpec(candidate: EvidenceCandidate): ChartSpec {
  const rungs: Rung[] = [
    { key: 'base', label: 'Base costs', metrics: candidate.development_metrics, change: null, base: true },
    ...candidate.stress.map((result: StressResult) => ({ key: result.scenario, label: result.label, metrics: result.metrics, change: result.return_change, base: false })),
  ];
  return {
    label: STRESS_LABEL,
    summary: `${candidate.label}: ${rungs.map((rung) => `${rung.label} ${rungText(rung)}`).join('; ')}.`,
    featured: null,
    option: (theme) => stressOption(rungs, candidate.key, theme),
    table: {
      caption: `${candidate.label} rerun in the engine under each predeclared cost scenario; — marks a value not recorded`,
      columns: ['Scenario', 'Net return', 'Change from base', 'Worst fall', 'Trades', 'Sharpe'],
      rows: rungs.map((rung) => {
        const figures = metricTexts(rung.metrics);
        const label = figures.failure === null ? rung.label : `${rung.label} — run failed: ${figures.failure}`;
        return { key: rung.key, cells: [label, figures.netReturn, rung.base ? '—' : signedPercentText(rung.change), figures.worstFall, figures.trades, figures.sharpe] };
      }),
    },
  };
}

function rungText(rung: Rung): string {
  if (rung.metrics === null) return NOT_RECORDED;
  if (rung.metrics.status === 'failed') return 'run failed';
  return signedPercentText(rung.metrics.total_return_pct);
}

function stressOption(rungs: readonly Rung[], key: CandidateKey, theme: ChartTheme): ChartOption {
  const axisLabel = { color: theme.textSecondary, fontSize: 11 };
  const label = {
    show: true,
    position: 'right' as const,
    color: theme.text,
    fontSize: 11,
    formatter: (params: { dataIndex: number }) => rungText(rungs[params.dataIndex]),
  };
  const colour = (fallback: string) => (params: { dataIndex: number }) => (rungLoses(rungs[params.dataIndex]) ? theme.loss : fallback);
  return {
    grid: { left: 124, right: 64, top: 8, bottom: 28 },
    tooltip: {
      ...tooltipFrame(theme),
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: (params: unknown) => {
        const rung = rungs[dataIndexOf(params) ?? -1];
        return rung === undefined ? '' : stressTooltip(rung, theme);
      },
    },
    xAxis: {
      type: 'value',
      splitNumber: 4,
      axisLabel: { ...axisLabel, hideOverlap: true, formatter: (value: number) => AXIS_PERCENT.format(value) },
      splitLine: { lineStyle: { color: theme.gridLine } },
    },
    yAxis: {
      type: 'category',
      inverse: true,
      data: rungs.map((rung) => rung.label),
      axisLine: { lineStyle: { color: theme.axis } },
      axisTick: { show: false },
      axisLabel: {
        color: theme.text,
        fontSize: 11,
        width: 116,
        overflow: 'truncate',
        formatter: lossLabel((index) => rungLoses(rungs[index])),
        rich: { loss: { color: theme.loss, fontSize: 10 } },
      },
    },
    series: [
      {
        id: 'base:costs',
        name: 'Base costs',
        type: 'bar',
        data: rungs.map((rung) => (rung.base ? (rungReturn(rung) ?? '-') : '-')),
        barMaxWidth: 18,
        barGap: '-100%',
        itemStyle: { color: colour(theme.candidates[key]) },
        label,
        emphasis: { focus: 'series', blurScope: 'global' },
      },
      {
        id: 'stress:costs',
        name: 'Stressed',
        type: 'bar',
        data: rungs.map((rung) => (rung.base ? '-' : (rungReturn(rung) ?? '-'))),
        barMaxWidth: 18,
        itemStyle: { color: colour(theme.stressed) },
        label,
        emphasis: { focus: 'series', blurScope: 'global' },
      },
    ],
  };
}

function stressTooltip(rung: Rung, theme: ChartTheme): string {
  const figures = metricTexts(rung.metrics);
  const rows: TooltipRow[] = [
    { label: 'Net return', values: [rung.metrics === null ? NOT_RECORDED : figures.netReturn] },
    ...(rung.base ? [] : [{ label: 'Change from base', values: [rung.change === null ? NOT_RECORDED : signedPercentText(rung.change)] }]),
    { label: 'Worst fall', values: [figures.worstFall] },
    { label: 'Trades', values: [figures.trades] },
    { label: 'Sharpe', values: [figures.sharpe] },
  ];
  const what = rung.base ? 'The study’s own costs and fills.' : 'The same settings rerun in the engine with this scenario’s costs and fills.';
  const failed = figures.failure === null ? [] : [`The run failed: ${figures.failure}`];
  return tooltipHtml({ title: rung.label, columns: ['Value'], rows, notes: [...failed, what, DEVELOPMENT] }, theme);
}
