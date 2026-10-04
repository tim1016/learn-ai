import { describe, expect, it } from 'vitest';

import type { CoverageCell } from './coverage-board';
import { sessionChecks, sessionVerdict } from './session-checks';

function cell(overrides: Partial<CoverageCell>): CoverageCell {
  return { tradingDateMs: 1, status: 'complete', artifactId: 7, rebuildReason: null, ...overrides };
}

function details(target: CoverageCell, mode: 'polygon_split_adjusted' | 'raw'): string[] {
  return sessionChecks(target, mode).map((check) => `${check.label}: ${check.detail} (${check.tone})`);
}

describe('sessionChecks', () => {
  it('names the missing adjustment record as the one thing stopping a run', () => {
    const target = cell({ status: 'needs_rebuild', rebuildReason: 'adjustment_record_missing' });

    expect(details(target, 'polygon_split_adjusted')).toEqual([
      'Price file: On disk (ok)',
      'Adjustment record: Missing (bad)',
      'Catalog entry: Complete (ok)',
    ]);
    expect(sessionVerdict(target)).toEqual({
      readable: false,
      title: "A run can't read this session",
      remedy: 'A backfill fetches the session again and writes its adjustment record.',
    });
  });

  it('tells a record that is out of date from one that is missing', () => {
    const target = cell({ status: 'needs_rebuild', rebuildReason: 'adjustment_record_rejected' });

    expect(details(target, 'polygon_split_adjusted')[1]).toBe(
      'Adjustment record: Out of date, or does not match the price file (bad)',
    );
  });

  it('asks for no adjustment record in a view that has none', () => {
    expect(details(cell({}), 'raw')).toEqual([
      'Price file: On disk (ok)',
      'Adjustment record: Not needed in this view (muted)',
      'Catalog entry: Complete (ok)',
    ]);
    expect(sessionVerdict(cell({})).readable).toBe(true);
  });

  it('reports a session with nothing on disk without blaming the adjustment record', () => {
    const target = cell({ status: 'missing', artifactId: null });

    expect(details(target, 'polygon_split_adjusted')).toEqual([
      'Price file: Not on disk (bad)',
      'Adjustment record: Written with the price file (muted)',
      'Catalog entry: None (bad)',
    ]);
    expect(sessionVerdict(target).remedy).toBe('A backfill fetches it.');
  });

  it('carries a failed catalog entry through under its own name', () => {
    const target = cell({ status: 'failed' });

    expect(details(target, 'polygon_split_adjusted')[2]).toBe('Catalog entry: Failed (bad)');
  });
});
