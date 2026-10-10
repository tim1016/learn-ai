import { describe, expect, it } from 'vitest';

import { FAKE_CHART_THEME } from './testing/fake-charts';
import { chanceCarryOver, chanceComparison, chanceFitToFit } from './testing/fixtures';
import { carryOverSpec, fitToFitText, finalIntervalsSpec } from './golden-search-chance-final-charts';
import type { CandidateRef } from './golden-search.types';

const ALL_PERIOD: CandidateRef = { key: 'all_period', label: 'All-period fit' };

/** Falsifiability: truncate a rank to the plotted 99 percentiles, relabel the bands as confidence intervals, join the percentiles into a path, or hide the recorded marker, and a spec here names it. */

describe('the final uncertainty chart specs', () => {
  it('draws both bands against a zero reference labelled tie and break-even', () => {
    const spec = finalIntervalsSpec(ALL_PERIOD, chanceComparison(), 'all_period');
    const option = spec.option(FAKE_CHART_THEME) as { series: { markLine?: { data: { xAxis: number }[]; label?: { formatter?: () => string } } }[] };
    const marks = option.series.flatMap((series) => (series.markLine?.data ?? []).map(() => series.markLine?.label?.formatter?.() ?? ''));
    expect(marks).toContain('tie');
    expect(marks).toContain('break-even');
    expect(JSON.stringify(option)).not.toContain('confidence');
  });

  it('marks the recorded final result at its actual value on the percentile curve', () => {
    const carry = chanceCarryOver();
    const spec = carryOverSpec(ALL_PERIOD, carry, 'all_period');
    const option = spec.option(FAKE_CHART_THEME) as { series: { markLine?: { data: { yAxis: number }[] } }[] };
    const marks = option.series.flatMap((series) => series.markLine?.data ?? []);
    expect(marks).toContainEqual({ yAxis: carry.recorded_objective });
    expect(spec.table.rows.map((row) => row.cells.join(' ')).join('\n')).toContain('6,221 of 10,000');
  });

  it('never truncates the rank to the plotted 1-99 range', () => {
    const carry = chanceCarryOver({ rank: 1.0, below: 10_000, label: 'above_most' });
    const spec = carryOverSpec(ALL_PERIOD, carry, 'all_period');
    expect(spec.table.rows.map((row) => row.cells.join(' ')).join('\n')).toContain('10,000 of 10,000');
    expect(spec.summary).toContain('never percentile bins');
  });

  it('carries the fit-to-fit sentence with its post-selection caveat', () => {
    const text = fitToFitText(chanceFitToFit());
    expect(text).toContain('68.4% of paths');
    expect(text).toContain('not adjusted for that choice');
  });
});
