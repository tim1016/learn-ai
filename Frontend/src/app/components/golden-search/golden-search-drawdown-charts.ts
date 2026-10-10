import type { ChartSpec } from './charts/golden-search-chart-spec';
import { NOT_RECORDED, tooltipFrame, tooltipHtml, type ChartTheme, type TooltipRow } from './charts/golden-search-chart-theme';
import type { ChartOption } from './charts/golden-search-echarts';
import { percentText } from './golden-search-display';
import { shareText } from './golden-search-chance-charts';
import type { CandidateRef, MeasuredChanceDrawdown } from './golden-search.types';

/**
 * The session-close drawdown chart (#3013): the resampled range from zero to
 * the 95th percentile with its median, beside the recorded close-only fall,
 * the recorded engine per-bar fall and the frozen ceiling, all on one
 * percentage scale. Every number is the server's; the browser formats and
 * draws. The engine figure missing reads "not recorded", never zero, and
 * the panel carries no capital or sizing advice.
 */

const ROW = 'Resampled session-close fall';

export function drawdownSpec(candidate: CandidateRef, drawdown: MeasuredChanceDrawdown, featured: string | null): ChartSpec {
  const p95 = drawdown.percentiles['p95'];
  const p50 = drawdown.percentiles['p50'];
  const ceiling = drawdown.basis['thresholds']['ceiling'] as number;
  const hasRange = drawdown.percentiles['finite'] > 0 && p95 !== null && p50 !== null;
  return {
    label: `${candidate.label} drawdown range`,
    summary: `Resampled session-close falls reach ${percentText(p95)} at the 95th percentile; the recorded close-only fall is ${percentText(
      drawdown.recorded_close_only,
    )} and the engine's bar-by-bar figure ${drawdown.recorded_engine_per_bar === null ? 'is not recorded' : `is ${percentText(drawdown.recorded_engine_per_bar)}`}.`,
    featured,
    option: (theme) => drawdownOption(drawdown, hasRange, theme),
    table: {
      caption: `${candidate.label} session-close drawdown: resampled range, recorded falls and the frozen ceiling`,
      columns: ['Quantity', 'Value'],
      rows: drawdownTableRows(drawdown, ceiling),
    },
  };
}

function drawdownTableRows(drawdown: MeasuredChanceDrawdown, ceiling: number): { key: string; cells: string[] }[] {
  const rows = [
    { key: 'p50', cells: ['Resampled fall, median', drawdown.percentiles['p50'] === null ? NOT_RECORDED : percentText(drawdown.percentiles['p50'] as number)] },
    { key: 'p95', cells: ['Resampled fall, 95th percentile', drawdown.percentiles['p95'] === null ? NOT_RECORDED : percentText(drawdown.percentiles['p95'] as number)] },
    { key: 'finite', cells: ['Resamples behind the range', `${Number(drawdown.percentiles['finite']).toLocaleString('en-US')} of 10,000`] },
    { key: 'close', cells: ['Recorded fall at session closes', percentText(drawdown.recorded_close_only)] },
    { key: 'engine', cells: ["Recorded fall, engine's every bar", drawdown.recorded_engine_per_bar === null ? NOT_RECORDED : percentText(drawdown.recorded_engine_per_bar)] },
    { key: 'ceiling', cells: ['Frozen ceiling', percentText(ceiling)] },
    { key: 'beyond', cells: ['Resamples strictly beyond the ceiling', `${drawdown.counts['beyond_ceiling'].toLocaleString('en-US')} (${shareText(drawdown.shares['beyond_ceiling'])})`] },
    { key: 'undefined', cells: ['Resamples with an undefined fall', `${drawdown.counts['undefined'].toLocaleString('en-US')} (${shareText(drawdown.shares['undefined'])})`] },
  ];
  return rows;
}

function drawdownOption(drawdown: MeasuredChanceDrawdown, hasRange: boolean, theme: ChartTheme): ChartOption {
  const p95 = drawdown.percentiles['p95'];
  const p50 = drawdown.percentiles['p50'];
  const ceiling = drawdown.basis['thresholds']['ceiling'] as number;
  const max = Math.max(hasRange ? (p95 as number) : 0, drawdown.recorded_close_only, drawdown.recorded_engine_per_bar ?? 0, ceiling) * 1.15;
  return {
    grid: { left: 148, right: 76, top: 30, bottom: 34 },
    tooltip: {
      ...tooltipFrame(theme),
      trigger: 'item',
      formatter: () => {
        const rows: TooltipRow[] = [
          { label: '95th percentile', values: [p95 === null ? NOT_RECORDED : percentText(p95)] },
          { label: 'Median', values: [p50 === null ? NOT_RECORDED : percentText(p50)] },
          { label: 'Recorded, session closes', values: [percentText(drawdown.recorded_close_only)] },
          { label: "Recorded, engine's every bar", values: [drawdown.recorded_engine_per_bar === null ? NOT_RECORDED : percentText(drawdown.recorded_engine_per_bar)] },
          { label: 'Frozen ceiling', values: [percentText(ceiling)] },
        ];
        return tooltipHtml({ title: ROW, columns: ['Fall'], rows, notes: ['Session closes, resampled development outcomes, unchanged recorded sizing and costs.'] }, theme);
      },
    },
    xAxis: {
      type: 'value',
      min: 0,
      max: max,
      axisLabel: { color: theme.textSecondary, fontSize: 11, hideOverlap: true, formatter: (value: number) => percentText(value) },
      splitLine: { lineStyle: { color: theme.gridLine } },
    },
    yAxis: {
      type: 'category',
      data: [ROW],
      axisLine: { lineStyle: { color: theme.axis } },
      axisTick: { show: false },
      axisLabel: { color: theme.text, fontSize: 12, width: 140, overflow: 'truncate' },
    },
    series: [
      {
        id: 'drawdown:range',
        name: 'Resampled range',
        type: 'custom',
        // One datum always draws: without a range the band collapses to zero width, but the recorded markers and the ceiling stay visible.
        data: [0],
        // eslint-disable-next-line @typescript-eslint/no-explicit-any -- echarts' custom-series graphic children are a union the limited ComposeOption types cannot name.
        renderItem: (_params: unknown, api: { coord: (data: number[]) => number[] }): any => {
          const zeroX = api.coord([0])[0];
          const p95X = api.coord([hasRange ? (p95 as number) : 0])[0];
          const medianX = api.coord([hasRange ? (p50 as number) : 0])[0];
          const closeX = api.coord([drawdown.recorded_close_only])[0];
          const engineX = api.coord([drawdown.recorded_engine_per_bar ?? 0])[0];
          const top = api.coord([0])[1] - 15;
          const height = 30;
          const band = hasRange
            ? [{ type: 'rect', shape: { x: zeroX, y: top + 6, width: Math.max(1, p95X - zeroX), height: height - 12 }, style: { fill: theme.candidates.all_period, opacity: 0.28 } }]
            : [];
          return {
            type: 'group',
            children: [
              ...band,
              ...(hasRange ? [{ type: 'rect', shape: { x: medianX - 1.5, y: top + 2, width: 3, height: height - 4 }, style: { fill: theme.text } }] : []),
              { type: 'circle', shape: { cx: closeX, cy: top + height / 2, r: 5 }, style: { fill: 'transparent', stroke: theme.development, lineWidth: 2 } },
              ...(drawdown.recorded_engine_per_bar === null
                ? []
                : [{ type: 'rect', shape: { x: engineX - 1.5, y: top + 8, width: 3, height: height - 16 }, style: { fill: theme.stressed } }]),
            ],
          };
        },
        z: 2,
        markLine: {
          silent: true,
          symbol: 'none',
          lineStyle: { color: theme.warn, type: 'dashed', width: 1.5 },
          label: { show: true, position: 'end', color: theme.warn, fontSize: 10, formatter: () => 'ceiling' },
          data: [{ xAxis: ceiling }],
        },
      },
    ],
  };
}
