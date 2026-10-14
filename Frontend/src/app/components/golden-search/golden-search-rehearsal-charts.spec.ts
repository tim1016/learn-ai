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
    const falling = chanceRehearsal({
      rungs: [1, 2, 3, 4].map((k) =>
        rehearsalRung(k, {
          passes: { pass_joint: 5000 - k * 400, pass_positive_net: 9000, pass_drawdown: 8000, pass_exits: 8500, pass_objective: 7000 },
          shares: { joint: (5000 - k * 400) / 10_000, positive_net: 0.9, drawdown: 0.8, exits: 0.85, objective: 0.7 },
        }),
      ),
    });
    const option = rehearsalSpec(ALL_PERIOD, falling, 'all_period').option(FAKE_CHART_THEME) as { series: { id: string; data: number[] }[] };
    expect(option.series.find((item) => item.id === 'rehearsal:joint')?.data).toEqual([46, 42, 38, 34]);
    const series = rehearsalSeries(falling);
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
});
