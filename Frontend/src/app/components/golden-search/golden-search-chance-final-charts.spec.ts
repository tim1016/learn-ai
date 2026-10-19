import { describe, expect, it } from 'vitest';

import { FAKE_CHART_THEME } from './testing/fake-charts';
import { chanceCarryOver, chanceComparison, chanceProfitability } from './testing/fixtures';
import { carryOverSpec, finalIntervalsSpec, finalProfitSpec } from './golden-search-chance-final-charts';
import type { CandidateRef } from './golden-search.types';

const ALL_PERIOD: CandidateRef = { key: 'all_period', label: 'All-period fit' };

/** Falsifiability: truncate a rank to the plotted 99 percentiles, relabel the bands as confidence intervals, join the percentiles into a path, or hide the recorded marker, and a spec here names it. */

describe('the final uncertainty chart specs', () => {
  it('keeps the objective zero separate from break-even profit, labelled for its own measure (#3026)', () => {
    const spec = finalIntervalsSpec(ALL_PERIOD, chanceComparison(), 'all_period');
    const option = spec.option(FAKE_CHART_THEME) as { series: { markLine?: { data: { xAxis: number }[]; label?: { formatter?: () => string } } }[] };
    const marks = option.series.flatMap((series) => (series.markLine?.data ?? []).map(() => series.markLine?.label?.formatter?.() ?? ''));
    expect(marks).toContain('tie');
    expect(marks).toContain('zero return');  // the fixture objective is total return; its zero is the objective's, never money's
    expect(marks).not.toContain('break-even');
    expect(JSON.stringify(option)).not.toContain('confidence');
    const sharpe = chanceComparison({ basis: { ...chanceComparison().basis, objective: { measure: 'sharpe_ratio', label: 'Sharpe ratio', units: 'annualized ratio of session returns (× √252)' } } });
    const sharpeOption = finalIntervalsSpec(ALL_PERIOD, sharpe, 'all_period').option(FAKE_CHART_THEME) as { series: { markLine?: { label?: { formatter?: () => string } } }[] };
    const sharpeMarks = sharpeOption.series.map((series) => series.markLine?.label?.formatter?.() ?? '');
    expect(sharpeMarks).toContain('zero Sharpe');
    expect(sharpeMarks).not.toContain('break-even');
  });

  it('draws the compounded-net range as its own chart in percent units against actual break-even (#3026)', () => {
    const profitability = chanceProfitability();
    const spec = finalProfitSpec(ALL_PERIOD, profitability as Parameters<typeof finalProfitSpec>[1], 'all_period');
    const option = spec.option(FAKE_CHART_THEME) as { series: { markLine?: { data: { xAxis: number }[]; label?: { formatter?: () => string } } }[] };
    const marks = option.series.flatMap((series) => (series.markLine?.data ?? []).map(() => series.markLine?.label?.formatter?.() ?? ''));
    expect(marks).toEqual(['break-even']);
    expect(spec.summary).toContain('compounded net return');
    expect(spec.table.rows.map((row) => row.cells.join(' ')).join('\n')).toContain('+1.2% / +8.1% / +16.4%');
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

  it('says no path was defined instead of printing a zero rank over zero paths', () => {
    const carry = chanceCarryOver({ rank: 0, below: 0, finite: 0, undefined: 10_000, label: 'no_defined_paths', curve: { p: [], finite: 0 } });
    const rank = carryOverSpec(ALL_PERIOD, carry, 'all_period').table.rows.find((row) => row.key === 'rank')?.cells.join(' ') ?? '';
    expect(rank).toContain('No rehearsal path was defined');
    expect(rank).not.toContain('0 of 0');
    expect(rank).not.toContain('0%');
  });

  it('formats the curve by the recorded objective measure, never by a magnitude guess', () => {
    const base = chanceCarryOver();
    const carry = chanceCarryOver({
      basis: { ...base.basis, objective: { measure: 'net_profit', label: 'net profit', units: 'US dollars' } },
      recorded_objective: 512.5,
      curve: { p: base.curve.p.map((value) => value * 10_000), finite: base.curve.finite },
    });
    const spec = carryOverSpec(ALL_PERIOD, carry, 'all_period');
    const recorded = spec.table.rows.find((row) => row.key === 'recorded')?.cells.join(' ') ?? '';
    expect(recorded).toContain('+$513');
    expect(recorded).not.toContain('%');
  });
});
