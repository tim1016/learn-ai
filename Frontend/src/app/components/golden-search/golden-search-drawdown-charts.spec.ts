import { describe, expect, it } from 'vitest';

import { FAKE_CHART_THEME } from './testing/fake-charts';
import { chanceDrawdown } from './testing/fixtures';
import { drawdownSpec } from './golden-search-drawdown-charts';
import type { CandidateRef } from './golden-search.types';

const ALL_PERIOD: CandidateRef = { key: 'all_period', label: 'All-period fit' };

/** Falsifiability: draw the range as a confidence interval, hide the ceiling, zero a missing engine figure, or promise one-in-twenty depth, and a spec here names it. */

describe('the drawdown chart spec', () => {
  it('marks the frozen ceiling on the axis beside the range', () => {
    const spec = drawdownSpec(ALL_PERIOD, chanceDrawdown(), 'all_period');
    const option = spec.option(FAKE_CHART_THEME) as { series: { markLine?: { data: { xAxis: number }[] } }[] };
    const marks = option.series[0]?.markLine?.data ?? [];
    expect(marks).toContainEqual({ xAxis: 0.2 });
  });

  it('scales from zero in percent for every candidate', () => {
    const spec = drawdownSpec(ALL_PERIOD, chanceDrawdown(), 'all_period');
    const option = spec.option(FAKE_CHART_THEME) as { xAxis: { min: number } };
    expect(option.xAxis.min).toBe(0);
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

  it('never promises one path in twenty at the percentile depth', () => {
    const spec = drawdownSpec(ALL_PERIOD, chanceDrawdown(), 'all_period');
    expect(JSON.stringify(spec.option(FAKE_CHART_THEME))).not.toContain('confidence');
  });
});
