import { describe, expect, it } from 'vitest';

import { FAKE_CHART_THEME } from './testing/fake-charts';
import { chanceDrawdown } from './testing/fixtures';
import { drawdownSpec } from './golden-search-drawdown-charts';
import type { CandidateRef } from './golden-search.types';

const ALL_PERIOD: CandidateRef = { key: 'all_period', label: 'All-period fit' };

/** Falsifiability: draw the range as a confidence interval, hide the ceiling, zero a missing engine figure, or promise one-in-twenty depth, and a spec here names it. */

describe('the drawdown chart spec', () => {
  it('draws the default drawdown range with the frozen ceiling on a zero-based axis, never promising one path in twenty', () => {
    const spec = drawdownSpec(ALL_PERIOD, chanceDrawdown(), 'all_period');
    const markOption = spec.option(FAKE_CHART_THEME) as { series: { markLine?: { data: { xAxis: number }[] } }[] };
    const marks = markOption.series[0]?.markLine?.data ?? [];
    expect.soft(marks, 'marks the frozen ceiling on the axis beside the range').toContainEqual({ xAxis: 0.2 });
    const axisOption = spec.option(FAKE_CHART_THEME) as { xAxis: { min: number } };
    expect.soft(axisOption.xAxis.min, 'scales from zero in percent for every candidate').toBe(0);
    expect.soft(JSON.stringify(spec.option(FAKE_CHART_THEME)), 'never promises one path in twenty at the percentile depth').not.toContain('confidence');
  });

  it('carries the recorded falls, the counts and the ceiling in its table, with not recorded for a missing engine figure', () => {
    const spec = drawdownSpec(ALL_PERIOD, chanceDrawdown({ recorded_engine_per_bar: null }), 'all_period');
    const cells = spec.table.rows.map((row) => row.cells.join(' ')).join('\n');
    expect(cells).toContain('15.0%');
    expect(cells).toContain('15.8%');
    expect(cells).toContain('not recorded');
    expect(cells).toContain('20.0%');
    expect(cells).toContain('10,000');
  });

  it('states the resample count the reading was drawn over, not a fixed 10,000', () => {
    const base = chanceDrawdown();
    const spec = drawdownSpec(ALL_PERIOD, chanceDrawdown({ basis: { ...base.basis, replications: 2_000 }, percentiles: { p50: 0.092, p95: 0.158, finite: 1_980 } }), 'all_period');
    const finite = spec.table.rows.find((row) => row.key === 'finite')?.cells.join(' ') ?? '';
    expect(finite).toContain('1,980 of 2,000');
    expect(finite).not.toContain('10,000');
  });
});
