import type { ChartSpec } from './charts/golden-search-chart-spec';
import { tooltipFrame, tooltipHtml, type ChartTheme, type TooltipRow } from './charts/golden-search-chart-theme';
import type { ChartOption } from './charts/golden-search-echarts';
import { ratioText, signedPercentText, signedUsdText } from './golden-search-display';
import type { CandidateRef, MeasuredChanceComparison } from './golden-search.types';

/**
 * The development chance charts (#3011): the paired difference interval and
 * the share strip beside it. Every number is the server's — counts, shares,
 * percentiles, the label and the sentences are stored with the evidence; the
 * browser only formats them for an axis, a tooltip or a table. A reading the
 * study never recorded reads "not measured", never zero, and a tie is drawn
 * as a tie, never as improvement.
 */

const WHOLE = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 });
const SHARE = new Intl.NumberFormat('en-US', { style: 'percent', maximumFractionDigits: 1 });

/** A raw count as the panel words it. */
export function countText(value: number): string {
  return WHOLE.format(value);
}

/** A stored share as the panel words it (presentation only; labels come from counts). */
export function shareText(value: number): string {
  return SHARE.format(value);
}

/** How one objective unit reads on an axis, in a tooltip and in a table cell. */
export function objectiveTexts(measure: string): { readonly axis: (value: number) => string; readonly cell: (value: number | null) => string } {
  if (measure === 'total_return_pct') return { axis: (value) => signedPercentText(value), cell: signedPercentText };
  if (measure === 'net_profit') return { axis: (value) => signedUsdText(value), cell: signedUsdText };
  return { axis: (value) => ratioText(value), cell: ratioText };
}

/** The share strip's labelled segments, in draw order; the widths are the stored shares. */
export interface ShareSegment {
  readonly key: 'better' | 'tied' | 'worse' | 'undefined';
  readonly label: string;
  readonly count: number;
  readonly share: number;
}

export function shareSegments(comparison: MeasuredChanceComparison): readonly ShareSegment[] {
  return [
    { key: 'better', label: 'Better', count: comparison.counts.better, share: comparison.shares['better'] },
    { key: 'tied', label: 'Tied', count: comparison.counts.tied, share: comparison.shares['tied'] },
    { key: 'worse', label: 'Worse', count: comparison.counts.worse, share: comparison.shares['worse'] },
    { key: 'undefined', label: 'Undefined', count: comparison.counts.undefined, share: comparison.shares['undefined'] },
  ];
}

const DIFFERENCE_ROW = 'Difference from the current settings';

/**
 * The value-axis domain that keeps every stored percentile, its zero reference and nothing else inside the
 * plot (#3027, #3028): custom-series data does not feed autoscaling, so the axis is bounded explicitly from
 * the stored values. Entirely negative, cross-zero, entirely positive and all-tie/degenerate ranges all stay
 * visible; a zero-span domain gets a small pad so a tie still has a plot to sit in. ``includeZero`` is off
 * for quantities whose zero is not a reference (a rehearsal curve that never crosses zero is not flattened
 * to include it), and an out-of-range recorded marker is included in the values so the axis reaches it.
 */
export function valueAxisDomain(values: readonly (number | null)[], { includeZero = true }: { includeZero?: boolean } = {}): { min: number; max: number } {
  const finite = values.filter((value): value is number => value !== null && Number.isFinite(value));
  if (includeZero) finite.push(0);
  const low = Math.min(...finite);
  const high = Math.max(...finite);
  const span = high - low;
  const pad = span > 0 ? span * 0.05 : Math.max(Math.abs(low) * 0.05, 0.02);
  return { min: low - pad, max: high + pad };
}

/**
 * The primary chart: a horizontal 5th–95th interval of the resampled
 * differences with the median marked, against a zero (no-improvement)
 * reference. It is labelled the middle 90% of resampled differences — never a
 * confidence interval — and an interval that crosses zero visibly differs
 * from one entirely on one side.
 */
export function chanceComparisonSpec(candidate: CandidateRef, comparison: MeasuredChanceComparison, featured: string | null): ChartSpec {
  const texts = objectiveTexts(comparison.basis.objective.measure);
  const difference = comparison.difference_range;
  const hasRange = difference.finite > 0 && difference.p5 !== null && difference.p50 !== null && difference.p95 !== null;
  return {
    label: `${candidate.label} resampled against the current settings`,
    summary: `${SHARE.format(comparison.at_least_as_good.share)} of ${WHOLE.format(comparison.basis.replications)} paired resamples were at least as good (${WHOLE.format(
      comparison.counts.better,
    )} better, ${WHOLE.format(comparison.counts.tied)} tied, ${WHOLE.format(comparison.counts.worse)} worse).`,
    featured,
    option: (theme) => chanceOption(comparison, hasRange, texts, theme),
    table: {
      caption: `${candidate.label} against the current settings over ${WHOLE.format(comparison.basis.replications)} paired resamples of the development sessions`,
      columns: ['Quantity', 'Value'],
      rows: chanceTableRows(comparison, texts),
    },
  };
}

function chanceTableRows(comparison: MeasuredChanceComparison, texts: { readonly cell: (value: number | null) => string }): { key: string; cells: string[] }[] {
  const difference = comparison.difference_range;
  const candidate = comparison.candidate_range;
  const rows = [
    { key: 'as-good', cells: ['Paths at least as good', `${WHOLE.format(comparison.at_least_as_good.count)} of ${WHOLE.format(comparison.basis.replications)} (${SHARE.format(comparison.at_least_as_good.share)})`] },
    { key: 'better', cells: ['Paths strictly better', `${WHOLE.format(comparison.counts.better)} (${SHARE.format(comparison.shares['better'])})`] },
    { key: 'tied', cells: ['Paths tied', `${WHOLE.format(comparison.counts.tied)} (${SHARE.format(comparison.shares['tied'])})`] },
    { key: 'worse', cells: ['Paths strictly worse', `${WHOLE.format(comparison.counts.worse)} (${SHARE.format(comparison.shares['worse'])})`] },
    { key: 'undefined', cells: ['Paths undefined', `${WHOLE.format(comparison.counts.undefined)} (${SHARE.format(comparison.shares['undefined'])})`] },
  ];
  if (difference.finite > 0 && difference.p5 !== null && difference.p50 !== null && difference.p95 !== null) {
    rows.push(
      { key: 'diff-p5', cells: ['Difference, 5th percentile', texts.cell(difference.p5)] },
      { key: 'diff-p50', cells: ['Difference, median', texts.cell(difference.p50)] },
      { key: 'diff-p95', cells: ['Difference, 95th percentile', texts.cell(difference.p95)] },
      { key: 'diff-finite', cells: ['Differences behind the range', `${WHOLE.format(difference.finite)} of ${WHOLE.format(comparison.basis.replications)}`] },
    );
  } else {
    rows.push({ key: 'diff-none', cells: ['Difference range', 'No resample produced a defined difference'] });
  }
  if (candidate.finite > 0 && candidate.p5 !== null && candidate.p50 !== null && candidate.p95 !== null) {
    rows.push(
      { key: 'cand-p5', cells: [`${comparison.basis.objective.label}, 5th percentile`, texts.cell(candidate.p5)] },
      { key: 'cand-p50', cells: [`${comparison.basis.objective.label}, median`, texts.cell(candidate.p50)] },
      { key: 'cand-p95', cells: [`${comparison.basis.objective.label}, 95th percentile`, texts.cell(candidate.p95)] },
    );
  }
  return rows;
}

function chanceOption(
  comparison: MeasuredChanceComparison,
  hasRange: boolean,
  texts: { readonly axis: (value: number) => string },
  theme: ChartTheme,
): ChartOption {
  const difference = comparison.difference_range;
  const p5 = difference.p5;
  const p50 = difference.p50;
  const p95 = difference.p95;
  return {
    grid: { left: 148, right: 76, top: 26, bottom: 30 },
    tooltip: {
      ...tooltipFrame(theme),
      trigger: 'item',
      formatter: () => {
        const rows: TooltipRow[] = hasRange
          ? [
              { label: '5th percentile', swatch: { color: theme.candidates.all_period, dashed: false }, values: [texts.axis(p5 as number)] },
              { label: 'Median', swatch: { color: theme.text, dashed: false }, values: [texts.axis(p50 as number)] },
              { label: '95th percentile', swatch: { color: theme.candidates.all_period, dashed: false }, values: [texts.axis(p95 as number)] },
            ]
          : [{ label: 'Difference range', values: ['No resample produced a defined difference'] }];
        rows.push({ label: 'Zero line', values: ['Where the two settings tie'] });
        return tooltipHtml({ title: DIFFERENCE_ROW, columns: ['Value'], rows, notes: ['Middle 90% of resampled differences — not a confidence interval.'] }, theme);
      },
    },
    xAxis: {
      type: 'value',
      name: comparison.basis.objective.units,
      nameLocation: 'middle',
      nameGap: 22,
      nameTextStyle: { color: theme.textSecondary, fontSize: 11 },
      splitNumber: 4,
      // Bounded from the stored percentiles and zero (#3027): autoscaling saw only the custom series'
      // placeholder datum and squeezed a -30%..+10% interval into a misleading sliver outside the plot.
      ...valueAxisDomain([p5, p50, p95]),
      axisLabel: { color: theme.textSecondary, fontSize: 11, hideOverlap: true, formatter: (value: number) => texts.axis(value) },
      splitLine: { lineStyle: { color: theme.gridLine } },
    },
    yAxis: {
      type: 'category',
      data: [DIFFERENCE_ROW],
      axisLine: { lineStyle: { color: theme.axis } },
      axisTick: { show: false },
      axisLabel: { color: theme.text, fontSize: 12, width: 140, overflow: 'truncate' },
    },
    series: [
      {
        id: 'chance:difference',
        name: 'Middle 90% of resampled differences',
        type: 'custom',
        // #3027: every percentile contributes to the visible axis extent.
        dimensions: ['low', 'high', 'median', 'row'],
        encode: { x: [0, 1, 2], y: 3 },
        data: hasRange ? [[p5, p95, p50, 0]] : [],
        renderItem: (_params: unknown, api: { coord: (data: number[]) => number[] }) => {
          const lowX = api.coord([p5 as number, 0])[0] as number;
          const highX = api.coord([p95 as number, 0])[0] as number;
          const medianX = api.coord([p50 as number, 0])[0] as number;
          const bandTop = (api.coord([0, 0])[1] as number) - 13;
          const bandHeight = 26;
          return {
            type: 'group',
            children: [
              { type: 'rect', shape: { x: Math.min(lowX, highX), y: bandTop, width: Math.max(1, Math.abs(highX - lowX)), height: bandHeight }, style: { fill: theme.candidates.all_period, opacity: 0.28 } },
              { type: 'rect', shape: { x: medianX - 1.5, y: bandTop - 4, width: 3, height: bandHeight + 8 }, style: { fill: theme.text } },
            ],
          };
        },
        z: 2,
        markLine: {
          silent: true,
          symbol: 'none',
          lineStyle: { color: theme.text, type: 'dashed', width: 1 },
          label: { show: true, position: 'end', color: theme.textSecondary, fontSize: 10, formatter: () => 'tie' },
          data: [{ xAxis: 0 }],
        },
      },
    ],
  };
}

/** The strip's accessible sentence — the same numbers the chart draws, as text. */
export function shareStripText(comparison: MeasuredChanceComparison): string {
  const total = comparison.basis.replications;
  return `Share of ${WHOLE.format(total)} paired resamples: better ${SHARE.format(comparison.shares['better'])}, tied ${SHARE.format(
    comparison.shares['tied'],
  )}, worse ${SHARE.format(comparison.shares['worse'])}, undefined ${SHARE.format(comparison.shares['undefined'])}.`;
}
