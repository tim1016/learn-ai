import { formatTimestampDisplay } from '../../shared/timestamp';
import type { ChartSpec } from './charts/golden-search-chart-spec';
import { tooltipFrame, tooltipHtml, type ChartTheme } from './charts/golden-search-chart-theme';
import type { ChartOption } from './charts/golden-search-echarts';
import { percentText, signedPercentText } from './golden-search-display';
import type { CandidateKey, CumulativeReturnPoint, DrawdownPoint } from './golden-search.types';

/** One candidate's development detail run as the server sent it; both lists are empty when no run is recorded. */
export interface EquityLine {
  readonly key: CandidateKey;
  readonly label: string;
  readonly returns: readonly CumulativeReturnPoint[];
  readonly falls: readonly DrawdownPoint[];
}

interface AlignedLine {
  readonly line: EquityLine;
  readonly recorded: boolean;
  readonly returns: readonly (number | null)[];
  readonly falls: readonly (number | null)[];
}

const NOT_RECORDED = 'not recorded';
const LABEL = 'Development cumulative return and fall from peak';

/**
 * The Compare equity chart (#2821, V19): every candidate's cumulative
 * development return on top and its fall from its own peak beneath, on one
 * session axis. Both series are the server's, one value per session close;
 * a session a run did not record stays empty, never zero. The worst-fall
 * limit is drawn on the fall axis for reference: the rules judge the
 * engine's bar-by-bar fall, which can be deeper than these session closes.
 */
export function equityChartSpec(lines: readonly EquityLine[], featured: CandidateKey, ceiling: number): ChartSpec {
  const sessions = [...new Set(lines.flatMap((line) => [...line.returns.map((p) => p.ms), ...line.falls.map((p) => p.ms)]))].sort((a, b) => a - b);
  const dates = sessions.map((ms) => formatTimestampDisplay(ms, { mode: 'date-et' }));
  const aligned: AlignedLine[] = lines.map((line) => ({
    line,
    recorded: line.returns.length > 0,
    returns: align(sessions, line.returns.map((p) => [p.ms, p.value])),
    falls: align(sessions, line.falls.map((p) => [p.ms, p.drawdown])),
  }));
  const ends = aligned.map(({ line, recorded, returns }) => `${line.label} ${recorded ? signedPercentText(lastValue(returns)) : NOT_RECORDED}`);
  return {
    label: LABEL,
    summary: `Ends at ${ends.join(', ')}; beneath, each line's fall from its own peak at every session close.`,
    featured,
    option: (theme) => option(aligned, dates, featured, ceiling, theme),
    table: {
      caption: `${LABEL} at each session close (ET); — marks a value the run did not record`,
      columns: ['Session', ...lines.flatMap((line) => [`${line.label} return`, `${line.label} fall from peak`])],
      rows: sessions.map((ms, i) => ({
        key: String(ms),
        cells: [dates[i], ...aligned.flatMap((a) => [signedPercentText(a.returns[i]), signedPercentText(a.falls[i])])],
      })),
    },
  };
}

function option(aligned: readonly AlignedLine[], dates: readonly string[], featured: CandidateKey, ceiling: number, theme: ChartTheme): ChartOption {
  const axisLabel = { color: theme.textSecondary, fontSize: 11 };
  const axisLine = { lineStyle: { color: theme.axis } };
  const splitLine = { lineStyle: { color: theme.gridLine } };
  const drawn = aligned.filter((a) => a.recorded);
  const limitOn = drawn[0]?.line.key;
  return {
    grid: [
      { left: 56, right: 76, top: 12, height: '54%' },
      { left: 56, right: 76, top: '66%', bottom: 28 },
    ],
    axisPointer: { link: [{ xAxisIndex: 'all' }], lineStyle: { color: theme.textSecondary } },
    tooltip: {
      ...tooltipFrame(theme),
      trigger: 'axis',
      formatter: (params: unknown) => {
        const index = dataIndexOf(params);
        return index === null ? '' : tooltip(aligned, dates[index], index, ceiling, theme);
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
    series: drawn.flatMap(({ line, returns, falls }) => {
      const color = theme.candidates[line.key];
      const isFeatured = line.key === featured;
      const lineStyle = { color, width: isFeatured ? 2.5 : 1.5, type: line.key === 'incumbent' ? ('dashed' as const) : ('solid' as const) };
      const common = { type: 'line' as const, name: line.label, showSymbol: false, connectNulls: false, z: isFeatured ? 3 : 2, lineStyle, itemStyle: { color }, emphasis: { focus: 'series' as const, blurScope: 'global' as const, lineStyle: { width: 'bolder' as const } } };
      return [
        { ...common, id: `equity:${line.key}`, xAxisIndex: 0, yAxisIndex: 0, data: plotted(returns), endLabel: { show: true, color, fontSize: 11, formatter: () => signedPercentText(lastValue(returns)) } },
        {
          ...common,
          id: `fall:${line.key}`,
          xAxisIndex: 1,
          yAxisIndex: 1,
          data: plotted(falls),
          ...(isFeatured ? { areaStyle: { color, opacity: 0.15 } } : {}),
          ...(line.key === limitOn
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

function tooltip(aligned: readonly AlignedLine[], date: string, index: number, ceiling: number, theme: ChartTheme): string {
  return tooltipHtml(
    {
      title: `${date} · at the session close`,
      columns: ['Return', 'Fall from peak'],
      rows: aligned.map(({ line, recorded, returns, falls }) => ({
        label: line.label,
        swatch: { color: theme.candidates[line.key], dashed: line.key === 'incumbent' },
        values: recorded ? [orNotRecorded(returns[index]), orNotRecorded(falls[index])] : [NOT_RECORDED, NOT_RECORDED],
      })),
      notes: [
        `Worst-fall limit ${percentText(ceiling)}. The rules judge each run's bar-by-bar fall, which can be deeper than this session-close line.`,
        'Development data, used for choosing.',
      ],
    },
    theme,
  );
}

/** ECharts' empty point is `'-'`: a session the run did not record leaves a gap, never a zero. */
function plotted(values: readonly (number | null)[]): (number | '-')[] {
  return values.map((value) => value ?? '-');
}

function orNotRecorded(value: number | null): string {
  return value === null ? NOT_RECORDED : signedPercentText(value);
}

/** Each session's value, or null where this run recorded none. */
function align(sessions: readonly number[], points: readonly (readonly [number, number])[]): (number | null)[] {
  const byMs = new Map(points);
  return sessions.map((ms) => byMs.get(ms) ?? null);
}

function lastValue(values: readonly (number | null)[]): number | null {
  for (let i = values.length - 1; i >= 0; i--) {
    const value = values[i];
    if (value !== null) return value;
  }
  return null;
}

/** The hovered session's index from an axis tooltip's parameters (an array) or an item tooltip's (one object). */
function dataIndexOf(params: unknown): number | null {
  const first: unknown = Array.isArray(params) ? params[0] : params;
  if (typeof first !== 'object' || first === null || !('dataIndex' in first)) return null;
  const index = first.dataIndex;
  return typeof index === 'number' ? index : null;
}
