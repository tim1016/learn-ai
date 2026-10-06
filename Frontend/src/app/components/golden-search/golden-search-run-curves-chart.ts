import { formatTimestampDisplay } from '../../shared/timestamp';
import type { ChartSpec } from './charts/golden-search-chart-spec';
import { dataIndexOf, NOT_RECORDED, signedPercentOrNotRecorded, tooltipFrame, tooltipHtml, type ChartTheme } from './charts/golden-search-chart-theme';
import type { ChartOption } from './charts/golden-search-echarts';
import { percentText, signedPercentText } from './golden-search-display';
import type { RunCurve } from './golden-search.types';

/** One run to draw: what the All runs list calls it, and its curve as the server sent it. */
export interface RunLine {
  readonly key: string;
  readonly label: string;
  readonly curve: RunCurve;
}

interface AlignedRun {
  readonly line: RunLine;
  readonly returns: readonly (number | null)[];
  readonly falls: readonly (number | null)[];
}

/** A hover lists this many runs, best first; past it the lines are a crowd, read by ticking fewer. */
const HOVER_ROWS = 12;
/** Each line carries its end value at the right edge only while they do not pile up. */
const END_LABELS = 8;

/**
 * The runs the owner ticked, drawn together (#2930): each run's cumulative
 * return on top and its fall from its own peak beneath, on one session axis.
 * Runs over different windows share the axis and each spans only its own
 * sessions; a session a run did not score stays empty, never zero. Every
 * value is the server's.
 */
export function runCurvesSpec(lines: readonly RunLine[], ceiling: number): ChartSpec {
  const sessions = [...new Set(lines.flatMap((line) => line.curve.close_ms))].sort((a, b) => a - b);
  const dates = sessions.map((ms) => formatTimestampDisplay(ms, { mode: 'date-et' }));
  const aligned: AlignedRun[] = lines.map((line) => ({
    line,
    returns: align(sessions, line.curve.close_ms, line.curve.cumulative_return),
    falls: line.curve.drawdown === null ? sessions.map(() => null) : align(sessions, line.curve.close_ms, line.curve.drawdown),
  }));
  return {
    label: 'Run cumulative return and fall from peak',
    summary: `${lines.length} run${lines.length === 1 ? '' : 's'} drawn at every session close; beneath, each run's fall from its own peak.`,
    featured: lines[0]?.key ?? null,
    option: (theme) => option(aligned, dates, ceiling, theme),
    table: {
      caption: 'Each drawn run: where it ended and its deepest fall at a session close; — marks a value the run did not record',
      columns: ['Run', 'First session', 'Last session', 'Ended at', 'Deepest fall'],
      rows: aligned.map(({ line, returns, falls }) => ({
        key: line.key,
        cells: [line.label, dateText(line.curve.close_ms.at(0)), dateText(line.curve.close_ms.at(-1)), signedPercentText(lastValue(returns)), signedPercentText(lowest(falls))],
      })),
    },
  };
}

function option(aligned: readonly AlignedRun[], dates: readonly string[], ceiling: number, theme: ChartTheme): ChartOption {
  const axisLabel = { color: theme.textSecondary, fontSize: 11 };
  const axisLine = { lineStyle: { color: theme.axis } };
  const splitLine = { lineStyle: { color: theme.gridLine } };
  const colorOf = (index: number): string => theme.series[index % theme.series.length];
  const crowded = aligned.length > END_LABELS;
  return {
    grid: [
      { left: 56, right: crowded ? 16 : 76, top: 12, height: '54%' },
      { left: 56, right: crowded ? 16 : 76, top: '66%', bottom: 28 },
    ],
    axisPointer: { link: [{ xAxisIndex: 'all' }], lineStyle: { color: theme.textSecondary } },
    tooltip: {
      ...tooltipFrame(theme),
      trigger: 'axis',
      formatter: (params: unknown) => {
        const index = dataIndexOf(params);
        return index === null ? '' : tooltip(aligned, dates[index], index, ceiling, colorOf, theme);
      },
    },
    xAxis: [
      { type: 'category', gridIndex: 0, data: [...dates], boundaryGap: false, axisLine, axisTick: { show: false }, axisLabel: { show: false } },
      { type: 'category', gridIndex: 1, data: [...dates], boundaryGap: false, axisLine, axisLabel: { ...axisLabel, hideOverlap: true } },
    ],
    yAxis: [
      { type: 'value', gridIndex: 0, splitNumber: 4, axisLabel: { ...axisLabel, formatter: (value: number) => signedPercentText(value) }, splitLine },
      {
        type: 'value',
        gridIndex: 1,
        max: 0,
        // Room for the limit line when every fall stays above it.
        min: (extent: { min: number }) => Math.min(extent.min, -ceiling),
        splitNumber: 2,
        axisLabel: { ...axisLabel, formatter: (value: number) => signedPercentText(value) },
        splitLine,
      },
    ],
    series: aligned.flatMap(({ line, returns, falls }, index) => {
      const color = colorOf(index);
      const common = {
        type: 'line' as const,
        name: line.label,
        showSymbol: false,
        connectNulls: false,
        lineStyle: { color, width: crowded ? 1 : 1.75, opacity: crowded ? 0.7 : 1 },
        itemStyle: { color },
        emphasis: { focus: 'series' as const, blurScope: 'global' as const, lineStyle: { width: 'bolder' as const } },
      };
      return [
        { ...common, id: `equity:${line.key}`, xAxisIndex: 0, yAxisIndex: 0, data: plotted(returns), endLabel: { show: !crowded, color, fontSize: 11, formatter: () => signedPercentText(lastValue(returns)) } },
        {
          ...common,
          id: `fall:${line.key}`,
          xAxisIndex: 1,
          yAxisIndex: 1,
          data: plotted(falls),
          ...(index === 0
            ? {
                markLine: {
                  silent: true,
                  symbol: 'none',
                  lineStyle: { color: theme.warn, type: 'dashed' as const, width: 1 },
                  label: { color: theme.warn, fontSize: 11, position: 'insideStartTop' as const, formatter: `${percentText(ceiling)} limit` },
                  data: [{ yAxis: -ceiling }],
                },
              }
            : {}),
        },
      ];
    }),
  };
}

function tooltip(aligned: readonly AlignedRun[], date: string, index: number, ceiling: number, colorOf: (index: number) => string, theme: ChartTheme): string {
  // The runs that scored this session, best return first.
  const scored = aligned.map((run, at) => ({ run, at })).filter(({ run }) => run.returns[index] !== null);
  scored.sort((a, b) => (b.run.returns[index] ?? 0) - (a.run.returns[index] ?? 0));
  const shown = scored.slice(0, HOVER_ROWS);
  const notes = [`Worst-fall limit ${percentText(ceiling)}. The rules judge each run’s bar-by-bar fall, which can be deeper than this session-close line.`];
  if (scored.length > shown.length) notes.unshift(`The ${shown.length} best of ${scored.length} runs at this session. Tick fewer runs to read them all.`);
  if (scored.length < aligned.length) notes.push(`${aligned.length - scored.length} drawn run${aligned.length - scored.length === 1 ? '' : 's'} did not score this session.`);
  return tooltipHtml(
    {
      title: `${date} · at the session close`,
      columns: ['Return', 'Fall from peak'],
      rows: shown.map(({ run, at }) => ({
        label: run.line.label,
        swatch: { color: colorOf(at), dashed: false },
        values: [signedPercentOrNotRecorded(run.returns[index]), run.line.curve.drawdown === null ? NOT_RECORDED : signedPercentOrNotRecorded(run.falls[index])],
      })),
      notes,
    },
    theme,
  );
}

function dateText(ms: number | undefined): string {
  return ms === undefined ? '—' : formatTimestampDisplay(ms, { mode: 'date-et' });
}

/** ECharts' empty point is `'-'`: a session the run did not score leaves a gap, never a zero. */
function plotted(values: readonly (number | null)[]): (number | '-')[] {
  return values.map((value) => value ?? '-');
}

/** Each session's value, or null where this run scored none. */
function align(sessions: readonly number[], closes: readonly number[], values: readonly number[]): (number | null)[] {
  const byMs = new Map(closes.map((ms, i) => [ms, values[i]] as const));
  return sessions.map((ms) => byMs.get(ms) ?? null);
}

function lastValue(values: readonly (number | null)[]): number | null {
  for (let i = values.length - 1; i >= 0; i--) {
    const value = values[i];
    if (value !== null) return value;
  }
  return null;
}

function lowest(values: readonly (number | null)[]): number | null {
  const recorded = values.filter((value): value is number => value !== null);
  return recorded.length === 0 ? null : Math.min(...recorded);
}
