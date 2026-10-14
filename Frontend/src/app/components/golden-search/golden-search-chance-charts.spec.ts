import { describe, expect, it } from 'vitest';

import { FAKE_CHART_THEME } from './testing/fake-charts';
import { chanceComparison } from './testing/fixtures';
import { chanceComparisonSpec, objectiveTexts, shareSegments, shareStripText } from './golden-search-chance-charts';
import type { CandidateRef } from './golden-search.types';

/** Falsifiability: rename a strip segment, drop the zero reference, relabel the middle 90%, or let a tie read as improvement on one side only, and a spec here names it. */

const ALL_PERIOD: CandidateRef = { key: 'all_period', label: 'All-period fit' };

describe('the chance comparison chart spec', () => {
  it('draws a zero reference where the two settings tie, labelled as a tie', () => {
    const spec = chanceComparisonSpec(ALL_PERIOD, chanceComparison(), 'all_period');
    const option = spec.option(FAKE_CHART_THEME) as { series: { markLine?: { data: { xAxis: number }[] } }[] };
    const marks = option.series[0]?.markLine?.data ?? [];
    expect(marks).toContainEqual({ xAxis: 0 });
  });

  it('labels the interval the middle 90% of resampled differences, never a confidence interval', () => {
    const spec = chanceComparisonSpec(ALL_PERIOD, chanceComparison(), 'all_period');
    expect(spec.label).toBe('All-period fit resampled against the current settings');
    expect(JSON.stringify(spec.option(FAKE_CHART_THEME))).toContain('Middle 90% of resampled differences');
    expect(JSON.stringify(spec.option(FAKE_CHART_THEME))).not.toContain('confidence');
  });

  it('carries the raw counts behind every rounded share, ties and undefined included', () => {
    const comparison = chanceComparison({
      counts: { better: 0, tied: 9_430, worse: 0, undefined: 570 },
      shares: { better: 0, tied: 0.943, worse: 0, undefined: 0.057 },
      at_least_as_good: { count: 9_430, share: 0.943 },
    });
    const spec = chanceComparisonSpec(ALL_PERIOD, comparison, 'all_period');
    const cells = spec.table.rows.map((row) => row.cells.join(' '));
    expect(cells.join('\n')).toContain('9,430');
    expect(cells.join('\n')).toContain('570');
    expect(spec.summary).toContain('0 better');
    expect(spec.summary).toContain('9,430 tied');
  });

  it('says plainly when no resample produced a defined difference', () => {
    const spec = chanceComparisonSpec(ALL_PERIOD, chanceComparison({ difference_range: { p5: null, p50: null, p95: null, finite: 0 } }), 'all_period');
    expect(spec.table.rows.map((row) => row.cells.join(' ')).join('\n')).toContain('No resample produced a defined difference');
  });

  it('formats each objective in its own units', () => {
    expect(objectiveTexts('total_return_pct').cell(0.123)).toBe('+12.3%');
    expect(objectiveTexts('net_profit').cell(1234.5)).toBe('+$1,235');
    expect(objectiveTexts('sharpe_ratio').cell(1.234)).toBe('1.23');
  });

  it('orders the share strip better, tied, worse, undefined with its accessible sentence', () => {
    const comparison = chanceComparison();
    expect(shareSegments(comparison).map((segment) => segment.key)).toEqual(['better', 'tied', 'worse', 'undefined']);
    expect(shareStripText(comparison)).toContain('better 93.5%');
    expect(shareStripText(comparison)).toContain('undefined 0%');
  });
});
