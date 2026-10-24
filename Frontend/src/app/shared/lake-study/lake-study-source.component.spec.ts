import { render, screen, within } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { formatTimestampDisplay } from '../timestamp/timestamp-display';
import { LakeStudySourceComponent } from './lake-study-source.component';
import type { LakeCoverage, LakeStudyDataSource, LakeStudyRefusal } from './lake-study';

const MONDAY = Date.UTC(2024, 2, 4, 5, 0);
const FRIDAY = Date.UTC(2024, 2, 8, 5, 0);
const WEDNESDAY_OPEN = Date.UTC(2024, 2, 6, 14, 30);

function coverage(over: Partial<LakeCoverage['window']> = {}): LakeCoverage {
  return {
    window: {
      expectedSessions: 5, reused: 2, added: 3, missing: 0, failed: 0,
      building: 0, corrupt: 0, unavailableFromProvider: 0, ...over,
    },
    warmup: { expectedSessions: 0, reused: 0, added: 0, missing: 0, failed: 0, building: 0, corrupt: 0, unavailableFromProvider: 0 },
    artifacts: { fetched: 3, reused: 2, refreshed: 0 },
  };
}

const SOURCE: LakeStudyDataSource = {
  provider: 'polygon', symbol: 'SPY', priceAdjustmentMode: 'polygon_split_adjusted', sessionPolicy: 'extended',
  windowStartMs: MONDAY, windowEndMs: FRIDAY, effectiveWindow: { startMs: MONDAY, endMs: FRIDAY },
  warmupSessions: 0, barMinutes: 5, aggregation: 'minute_fold_et_wall_clock_no_fill',
  identityDigest: 'a'.repeat(64), calculationVersion: 'feature-study/1', snapshotDigest: 'b'.repeat(64),
  coverage: coverage(), barsUsed: 1920, cache: { hit: false, cachedAtMs: null },
};

const REFUSAL: LakeStudyRefusal = {
  code: 'data_unavailable_from_provider',
  reason: 'The lake cannot admit this window: 1 of 5 sessions cannot be supplied by the provider: 2024-03-06 (provider_no_data).',
  sessions: [{ group: 'window', tradingDateMs: WEDNESDAY_OPEN, outcome: 'unavailable_from_provider', reason: 'provider_no_data', failureReason: 'provider_no_data' }],
  sessionsTotal: 1,
  coverage: coverage({ reused: 0, added: 4, failed: 1, unavailableFromProvider: 1 }),
};

describe('LakeStudySourceComponent', () => {
  it('shows the data-source line of a completed study: the lake, the window, the bars and the counts', async () => {
    await render(LakeStudySourceComponent, { inputs: { source: SOURCE } });

    const summary = screen.getByText('Data source').closest('summary');
    expect(summary?.textContent).toContain('Data lake, Polygon');
    expect(summary?.textContent).toContain('5-minute bars, folded from 1-minute bars');
    const start = formatTimestampDisplay(MONDAY, { mode: 'date-et' });
    const end = formatTimestampDisplay(FRIDAY, { mode: 'date-et' });
    expect(summary?.textContent?.replace(/\s+/g, ' ')).toContain(`${start} → ${end}`);
    expect(screen.getByText('3 added · 2 reused · 0 missing')).toBeTruthy();
    expect(screen.queryByText(/No result/)).toBeNull();
  });

  it('says "not recorded" for a result that carries no data source, rather than guessing one', async () => {
    await render(LakeStudySourceComponent, { inputs: { source: null } });

    expect(screen.getByText('not recorded')).toBeTruthy();
  });

  it('shows a refused study as no result, with the reason, the counts admission reached and the session it could not make ready', async () => {
    await render(LakeStudySourceComponent, {
      inputs: { source: { ...SOURCE, snapshotDigest: null, effectiveWindow: null, coverage: REFUSAL.coverage }, refusal: REFUSAL },
    });

    expect(screen.getByRole('alert').textContent).toContain('No result');
    expect(screen.getByText(REFUSAL.reason)).toBeTruthy();
    expect(screen.getByText(/5 sessions · 0 reused · 4 added · 0 missing · 1 failed \(1 unavailable from the provider\)/)).toBeTruthy();
    const session = screen.getByText('provider_no_data').closest('li');
    expect(session?.textContent).toContain(formatTimestampDisplay(WEDNESDAY_OPEN, { mode: 'date-et', granularity: 'date' }));
    expect(within(session as HTMLElement).getByText('unavailable from the provider')).toBeTruthy();
    // The line below it still names where the study looked, and what was added.
    expect(screen.getByText('Data source')).toBeTruthy();
    expect(screen.getByText('4 added · 0 reused · 0 missing')).toBeTruthy();
  });

  it('counts the sessions it does not list', async () => {
    await render(LakeStudySourceComponent, { inputs: { source: null, refusal: { ...REFUSAL, sessionsTotal: 31 } } });

    expect(screen.getByText('and 30 more.')).toBeTruthy();
  });

  it('shows a refusal made before anything was read with no data-source line at all', async () => {
    await render(LakeStudySourceComponent, {
      inputs: { source: null, refusal: { code: 'resolution_unsupported', reason: '60-minute bars do not divide the 15-minute target.', sessions: [], sessionsTotal: 0, coverage: null } },
    });

    expect(screen.getByText('60-minute bars do not divide the 15-minute target.')).toBeTruthy();
    expect(screen.queryByText('Data source')).toBeNull();
    expect(screen.queryByText(/sessions ·/)).toBeNull();
  });
});
