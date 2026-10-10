import type { ChartSpec } from './charts/golden-search-chart-spec';
import { NOT_RECORDED, tooltipFrame, tooltipHtml, type ChartTheme, type TooltipRow } from './charts/golden-search-chart-theme';
import type { ChartOption } from './charts/golden-search-echarts';
import { countText, objectiveTexts, shareText } from './golden-search-chance-charts';
import { ratioText, signedPercentText, signedUsdText } from './golden-search-display';
import type { CandidateRef, MeasuredChanceCarryOver, MeasuredChanceComparison, MeasuredChanceFitToFit } from './golden-search.types';

/**
 * The final-test chance charts (#3014): the middle-90% paired difference
 * against zero, the fit's resampled objective range against break-even, and
 * the reproduced 1× rehearsal's percentile curve with the recorded final
 * result marked. Every number is the server's — counts, shares, ranges, the
 * rank and every sentence; the browser only formats and draws. The frozen
 * "meets the rules" verdict stays visually separate from these
 * model-support labels, the rank never truncates to the plotted 1–99 range,
 * and the percentile curve is never joined into a synthetic equity path.
 */

const DIFFERENCE_ROW = 'Difference from the current settings';
const RANGE_ROW = 'Resampled result, middle 90%';

// eslint-disable-next-line @typescript-eslint/no-explicit-any -- echarts' series union through ComposeOption cannot be indexed here.
type FinalSeries = any;

function rangeBand(counts: { p5: number | null; p50: number | null; p95: number | null }, row: string, theme: ChartTheme): FinalSeries[] {
  const has = counts.p5 !== null && counts.p50 !== null && counts.p95 !== null;
  return [
    {
      id: `final:range:${row}`,
      name: row,
      type: 'custom',
      data: has ? [0] : [],
      // eslint-disable-next-line @typescript-eslint/no-explicit-any -- echarts' custom-series graphic children are a union the limited ComposeOption types cannot name.
      renderItem: (_params: unknown, api: { coord: (data: number[]) => number[] }): any => {
        const lowX = api.coord([counts.p5 as number])[0];
        const highX = api.coord([counts.p95 as number])[0];
        const medianX = api.coord([counts.p50 as number])[0];
        const top = api.coord([0])[1] - 13;
        return {
          type: 'group',
          children: [
            { type: 'rect', shape: { x: Math.min(lowX, highX), y: top + 6, width: Math.max(1, Math.abs(highX - lowX)), height: 14 }, style: { fill: theme.candidates.all_period, opacity: 0.28 } },
            { type: 'rect', shape: { x: medianX - 1.5, y: top + 2, width: 3, height: 22 }, style: { fill: theme.text } },
          ],
        };
      },
      z: 2,
      markLine: {
        silent: true,
        symbol: 'none',
        lineStyle: { color: theme.text, type: 'dashed', width: 1 },
        label: { show: true, position: 'end', color: theme.textSecondary, fontSize: 10, formatter: () => (row === DIFFERENCE_ROW ? 'tie' : 'break-even') },
        data: [{ xAxis: 0 }],
      },
    },
  ];
}

function intervalOption(rows: readonly { row: string; range: { p5: number | null; p50: number | null; p95: number | null }; note: string }[], texts: { axis: (value: number) => string }, theme: ChartTheme): ChartOption {
  const values = rows.flatMap(({ range }) => [range.p5, range.p50, range.p95].filter((value): value is number => value !== null));
  const min = Math.min(0, ...values);
  const max = Math.max(0, ...values);
  return {
    grid: { left: 168, right: 76, top: 14 + rows.length * 14, bottom: 34 },
    tooltip: {
      ...tooltipFrame(theme),
      trigger: 'item',
      formatter: () => {
        const tooltipRows: TooltipRow[] = rows.map(({ row, range }) => ({
          label: row,
          values: [range.p5 === null ? NOT_RECORDED : `${texts.axis(range.p5 as number)} to ${texts.axis(range.p95 as number)}, median ${texts.axis(range.p50 as number)}`],
        }));
        return tooltipHtml({ title: 'Middle 90% of resamples', columns: ['Range'], rows: tooltipRows, notes: rows.map(({ note }) => note) }, theme);
      },
    },
    xAxis: {
      type: 'value',
      min: min * 1.1,
      max: max === 0 ? 1 : max * 1.1,
      axisLabel: { color: theme.textSecondary, fontSize: 11, hideOverlap: true, formatter: (value: number) => texts.axis(value) },
      splitLine: { lineStyle: { color: theme.gridLine } },
    },
    yAxis: {
      type: 'category',
      data: rows.map(({ row }) => row),
      axisLine: { lineStyle: { color: theme.axis } },
      axisTick: { show: false },
      axisLabel: { color: theme.text, fontSize: 12, width: 160, overflow: 'truncate' },
    },
    series: rows.flatMap(({ row, range }) => rangeBand(range, row, theme)),
  };
}

/** The difference and objective-range interval chart: both bands against zero, in the objective's units. */
export function finalIntervalsSpec(candidate: CandidateRef, comparison: MeasuredChanceComparison, featured: string | null): ChartSpec {
  const texts = objectiveTexts(comparison.basis.objective.measure);
  return {
    label: `${candidate.label} final uncertainty`,
    summary: `Middle 90% of resampled differences from the current settings and of the fit's own resampled result, in ${comparison.basis.objective.units}.`,
    featured,
    option: (theme) =>
      intervalOption(
        [
          { row: DIFFERENCE_ROW, range: comparison.difference_range, note: 'Candidate minus incumbent on every paired resample; zero is a tie.' },
          { row: RANGE_ROW, range: comparison.candidate_range, note: "The fit's own resampled result; zero is break-even." },
        ],
        texts,
        theme,
      ),
    table: {
      caption: `${candidate.label} final uncertainty over ${countText(comparison.basis.replications)} paired resamples of the final sessions`,
      columns: ['Quantity', 'Value'],
      rows: [
        { key: 'as-good', cells: ['Paths at least as good', `${countText(comparison.at_least_as_good.count)} of ${countText(comparison.basis.replications)} (${shareText(comparison.at_least_as_good.share)})`] },
        { key: 'counts', cells: ['Better / tied / worse / undefined', `${countText(comparison.counts.better)} / ${countText(comparison.counts.tied)} / ${countText(comparison.counts.worse)} / ${countText(comparison.counts.undefined)}`] },
        { key: 'diff', cells: ['Difference, 5th / median / 95th', comparison.difference_range.p5 === null ? NOT_RECORDED : `${texts.axis(comparison.difference_range.p5 as number)} / ${texts.axis(comparison.difference_range.p50 as number)} / ${texts.axis(comparison.difference_range.p95 as number)}`] },
        { key: 'range', cells: ['Own result, 5th / median / 95th', comparison.candidate_range.p5 === null ? NOT_RECORDED : `${texts.axis(comparison.candidate_range.p5 as number)} / ${texts.axis(comparison.candidate_range.p50 as number)} / ${texts.axis(comparison.candidate_range.p95 as number)}`] },
      ],
    },
  };
}

/** The reproduced 1× rehearsal's percentile curve with the recorded final result marked at its actual value. */
export function carryOverSpec(candidate: CandidateRef, carry: MeasuredChanceCarryOver, featured: string | null): ChartSpec {
  const texts = { axis: (value: number) => (Math.abs(value) >= 1000 ? signedUsdText(value) : Math.abs(value) <= 5 ? signedPercentText(value) : ratioText(value)) };
  const percentiles = Array.from({ length: 99 }, (_, index) => index + 1);
  return {
    label: `${candidate.label} result among its rehearsals`,
    summary: `The reproduced 1× rehearsal's objective at each percentile, with the recorded final result marked at its actual value; the rank counts actual paths, never percentile bins.`,
    featured,
    option: (theme) => ({
      grid: { left: 72, right: 96, top: 26, bottom: 46 },
      tooltip: {
        ...tooltipFrame(theme),
        trigger: 'axis',
        formatter: () =>
          tooltipHtml(
            {
              title: 'Rehearsal objective by percentile',
              columns: ['Value'],
              rows: [{ label: 'Rehearsal curve', swatch: { color: theme.candidates.all_period, dashed: false }, values: ['hover a percentile'] }],
              notes: ['The dashed line is the recorded final result at its actual value; the rank counts actual paths, never percentile bins.'],
            },
            theme,
          ),
      },
      xAxis: {
        type: 'value',
        min: 1,
        max: 99,
        name: 'percentile',
        nameLocation: 'middle',
        nameGap: 28,
        nameTextStyle: { color: theme.textSecondary, fontSize: 11 },
        axisLabel: { color: theme.textSecondary, fontSize: 11 },
        splitLine: { show: false },
      },
      yAxis: {
        type: 'value',
        scale: true,
        axisLabel: { color: theme.textSecondary, fontSize: 11, hideOverlap: true, formatter: (value: number) => texts.axis(value) },
        splitLine: { lineStyle: { color: theme.gridLine } },
      },
      series: [
        {
          id: 'carry:curve',
          name: 'Rehearsal objective',
          type: 'line',
          data: percentiles.map((p) => [p, carry.curve.p[p - 1] ?? null]),
          showSymbol: false,
          lineStyle: { color: theme.candidates.all_period, width: 2 },
          emphasis: { focus: 'series' },
          markLine: {
            silent: true,
            symbol: 'none',
            lineStyle: { color: theme.text, type: 'dashed', width: 1.5 },
            label: { show: true, position: 'end', color: theme.text, fontSize: 10, formatter: () => 'recorded final result' },
            data: [{ yAxis: carry.recorded_objective }],
          },
        },
      ],
    }),
    table: {
      caption: `${candidate.label} recorded final result among its own 1× development rehearsals`,
      columns: ['Quantity', 'Value'],
      rows: [
        { key: 'recorded', cells: ['Recorded final objective', texts.axis(carry.recorded_objective)] },
        { key: 'rank', cells: ['Rank among rehearsals', `${countText(carry.below)} of ${countText(carry.finite)} defined paths strictly below (${Math.round(carry.rank * 1000) / 10}%)`] },
        { key: 'undefined', cells: ['Undefined rehearsal paths', countText(carry.undefined)] },
        { key: 'label', cells: ['Reading', carry.sentence] },
      ],
    },
  };
}

/** The fit-to-fit sentence's short row text, from the stored reading. */
export function fitToFitText(fit_to_fit: MeasuredChanceFitToFit): string {
  return `${fit_to_fit.sentence} ${fit_to_fit.selection_note}`;
}
