import { formatTimestampDisplay } from '../../shared/timestamp';
import { dataIndexOf, tooltipFrame, type ChartTheme } from './charts/golden-search-chart-theme';
import type { ChartOption } from './charts/golden-search-echarts';
import { percentText, signedPercentText } from './golden-search-display';

/**
 * The two-part session chart every equity view shares (#2821, #2930): each
 * line's cumulative return on top and its fall from its own peak beneath, on
 * one axis of session closes, with the worst-fall limit dashed on the fall
 * axis. A caller decides which lines to draw and how each looks, and writes
 * its own hover and table; the grids, axes, limit line and gaps are here.
 */

/** One drawn line: its values at each session of the shared axis, and how it looks. */
export interface SessionLine {
  /** The series ids are `equity:<key>` and `fall:<key>`. */
  readonly key: string;
  readonly name: string;
  readonly returns: readonly (number | null)[];
  readonly falls: readonly (number | null)[];
  readonly color: string;
  readonly width: number;
  /** Left out for a full-strength line. */
  readonly opacity?: number;
  readonly dashed: boolean;
  /** Drawn above the lines with a lower number. */
  readonly z: number;
  /** Whether the line carries its last value at the right edge. */
  readonly endLabel: boolean;
  /** Whether the fall line is filled: the line the chart is about. */
  readonly filled: boolean;
  /** A background line: the pointer passes over it, so a crowd of them never dims the lines that matter. */
  readonly quiet?: boolean;
}

/** The session closes any of the lists holds, in order, and each as an ET date. */
export function sessionAxis(closes: readonly (readonly number[])[]): { readonly sessions: number[]; readonly dates: string[] } {
  const sessions = [...new Set(closes.flat())].sort((a, b) => a - b);
  return { sessions, dates: sessions.map((ms) => formatTimestampDisplay(ms, { mode: 'date-et' })) };
}

/** Each session's value, or null where this line recorded none. */
export function alignToSessions(sessions: readonly number[], closes: readonly number[], values: readonly (number | null)[]): (number | null)[] {
  const byMs = new Map(closes.map((ms, i) => [ms, values[i]] as const));
  return sessions.map((ms) => byMs.get(ms) ?? null);
}

/** A run's curve as a read returns it: its values from `first_session` on, along that read's session closes. */
export interface SessionCurve {
  readonly first_session: number;
  readonly cumulative_return: readonly (number | null)[];
  readonly drawdown: readonly (number | null)[] | null;
}

/** One curve with the session closes of the read that returned it. */
export interface ReadCurve {
  readonly sessions: readonly number[];
  readonly curve: SessionCurve;
}

/**
 * The curves on one axis: its sessions as ET dates, and each curve's return
 * and fall at each. The axis holds the sessions a drawn curve scored and no
 * other, so it does not depend on which curves were read together. Curves of
 * one read already share its sessions; curves of several reads are joined by
 * instant. A curve with no fall recorded has none at any session.
 */
export function onOneAxis(curves: readonly ReadCurve[]): { readonly dates: string[]; readonly lines: { readonly returns: (number | null)[]; readonly falls: (number | null)[] }[] } {
  const closes = curves.map(({ sessions, curve }) => sessions.slice(curve.first_session, curve.first_session + curve.cumulative_return.length));
  const { sessions, dates } = sessionAxis(curves.map(({ curve }, i) => closes[i].filter((_, at) => curve.cumulative_return[at] !== null)));
  return {
    dates,
    lines: curves.map(({ curve }, i) => ({
      returns: alignToSessions(sessions, closes[i], curve.cumulative_return),
      falls: curve.drawdown === null ? sessions.map(() => null) : alignToSessions(sessions, closes[i], curve.drawdown),
    })),
  };
}

/** The indexes of a line's first and last recorded sessions; null for a line that recorded none. */
export function recordedSpan(values: readonly (number | null)[]): readonly [number, number] | null {
  const first = values.findIndex((value) => value !== null);
  if (first < 0) return null;
  let last = values.length - 1;
  while (values[last] === null) last--;
  return [first, last];
}

export function lastValue(values: readonly (number | null)[]): number | null {
  for (let i = values.length - 1; i >= 0; i--) {
    const value = values[i];
    if (value !== null) return value;
  }
  return null;
}

/** The drawn option: `hover` writes the tooltip for the session at an index. The limit is marked on the first line. */
export function sessionCurvesOption(lines: readonly SessionLine[], dates: readonly string[], ceiling: number, hover: (index: number) => string, theme: ChartTheme): ChartOption {
  const axisLabel = { color: theme.textSecondary, fontSize: 11 };
  const axisLine = { lineStyle: { color: theme.axis } };
  const splitLine = { lineStyle: { color: theme.gridLine } };
  // Room at the right edge only when a line prints its last value there.
  const right = lines.some((line) => line.endLabel) ? 76 : 16;
  return {
    grid: [
      { left: 56, right, top: 12, height: '54%' },
      { left: 56, right, top: '66%', bottom: 28 },
    ],
    axisPointer: { link: [{ xAxisIndex: 'all' }], lineStyle: { color: theme.textSecondary } },
    tooltip: {
      ...tooltipFrame(theme),
      trigger: 'axis',
      formatter: (params: unknown) => {
        const index = dataIndexOf(params);
        return index === null ? '' : hover(index);
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
    series: lines.flatMap((line, index) => {
      const color = line.color;
      const lineStyle = { color, width: line.width, type: line.dashed ? ('dashed' as const) : ('solid' as const), ...(line.opacity === undefined ? {} : { opacity: line.opacity }) };
      const emphasis = line.quiet ? { disabled: true } : { focus: 'series' as const, blurScope: 'global' as const, lineStyle: { width: 'bolder' as const } };
      const common = { type: 'line' as const, name: line.name, showSymbol: false, connectNulls: false, z: line.z, lineStyle, itemStyle: { color }, emphasis, ...(line.quiet ? { silent: true } : {}) };
      return [
        { ...common, id: `equity:${line.key}`, xAxisIndex: 0, yAxisIndex: 0, data: plotted(line.returns), endLabel: { show: line.endLabel, color, fontSize: 11, formatter: () => signedPercentText(lastValue(line.returns)) } },
        {
          ...common,
          id: `fall:${line.key}`,
          xAxisIndex: 1,
          yAxisIndex: 1,
          data: plotted(line.falls),
          ...(line.filled ? { areaStyle: { color, opacity: 0.15 } } : {}),
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

/** ECharts' empty point is `'-'`: a session a line did not record leaves a gap, never a zero. */
function plotted(values: readonly (number | null)[]): (number | '-')[] {
  return values.map((value) => value ?? '-');
}
