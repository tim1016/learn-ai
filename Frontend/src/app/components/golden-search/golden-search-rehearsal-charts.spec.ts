import { describe, expect, it } from 'vitest';

import { FAKE_CHART_THEME } from './testing/fake-charts';
import { chanceRehearsal, rehearsalRung } from './testing/fixtures';
import { rehearsalSeries, rehearsalSpec } from './golden-search-rehearsal-charts';
import type { CandidateRef } from './golden-search.types';

const ALL_PERIOD: CandidateRef = { key: 'all_period', label: 'All-period fit' };

/** Falsifiability: drop the target line, relabel the drawdown rule without its session-close basis, draw a falling rung as rising, or turn the x axis into dates, and a spec here names it. */

describe('the final-test rehearsal chart spec', () => {
  it('draws the joint line with the owner-heuristic target marked on it', () => {
    const spec = rehearsalSpec(ALL_PERIOD, chanceRehearsal(), 'all_period');
    const option = spec.option(FAKE_CHART_THEME) as { series: { markLine?: { data: { yAxis: number }[] } }[] };
    const joint = option.series.find((series) => series.markLine !== undefined);
    expect(joint).toBeDefined();
    expect(joint?.markLine?.data).toContainEqual({ yAxis: 80 });
  });

  it('scales y from 0 to 100 percent for every fit and rung, and never invents dates on x', () => {
    const spec = rehearsalSpec(ALL_PERIOD, chanceRehearsal(), 'all_period');
    const option = spec.option(FAKE_CHART_THEME) as { yAxis: { min: number; max: number }; xAxis: { type: string; data: string[] } };
    expect(option.yAxis.min).toBe(0);
    expect(option.yAxis.max).toBe(100);
    expect(option.xAxis.type).toBe('category');
    expect(option.xAxis.data).toEqual(['21', '42', '63', '84']);
  });

  it('draws a falling rung at its actual value and keeps every rule the plan models', () => {
    const rehearsal = chanceRehearsal();
    expect(rehearsal.rungs.map((rung) => rung.shares['joint'])).toEqual([0.612, 0.748, 0.815, 0.839]);
    const series = rehearsalSeries(rehearsal);
    expect(series.map((item) => item.key)).toEqual(['joint', 'positive_net', 'drawdown', 'exits', 'objective']);
    expect(series.find((item) => item.key === 'drawdown')?.label).toContain('at session closes');
  });

  it('omits the positive-net series entirely when the plan does not require it', () => {
    const rehearsal = chanceRehearsal({ active_rules: ['drawdown', 'exits', 'objective'] });
    const series = rehearsalSeries(rehearsal);
    expect(series.map((item) => item.key)).toEqual(['joint', 'drawdown', 'exits', 'objective']);
    expect(JSON.stringify(rehearsalSpec(ALL_PERIOD, rehearsal, 'all_period').option(FAKE_CHART_THEME))).not.toContain('Positive net');
  });

  it("carries each rung's session count and trade floor beside every share", () => {
    const spec = rehearsalSpec(ALL_PERIOD, chanceRehearsal(), 'all_period');
    const rows = spec.table.rows.map((row) => row.cells.join(' '));
    expect(rows[0]).toContain('1× 21 9 61.2%');
    expect(rows[3]).toContain('4× 84 36 83.9%');
  });

  it("never lets the joint share exceed a single rule's share on the same rung", () => {
    const rung = rehearsalRung(2);
    expect(rung.passes['pass_joint']).toBeLessThanOrEqual(rung.passes['pass_drawdown']);
    expect(rung.shares['joint']).toBeLessThanOrEqual(rung.shares['objective']);
  });
});
