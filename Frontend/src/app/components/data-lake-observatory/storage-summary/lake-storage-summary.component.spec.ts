import { render, screen } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it } from 'vitest';

import type { StorageSummaryResponse } from '../lib/data-lake.types';
import { LakeStorageSummaryComponent } from './lake-storage-summary.component';

/** 09:30 America/New_York on the given May 2026 date, as int64 ms UTC. */
function sessionOpenMs(day: number): number {
  return Date.UTC(2026, 4, day, 13, 30);
}

const POPULATED: StorageSummaryResponse = {
  market: 'usa',
  kinds: [
    {
      artifact_kind: 'minute_trade',
      resolution: 'minute',
      artifact_count: 3,
      total_bytes: 3_145_728,
    },
    { artifact_kind: 'map_file', resolution: null, artifact_count: 1, total_bytes: 1_048_576 },
  ],
  symbols: [
    {
      symbol: 'SPY',
      first_trading_date_ms: sessionOpenMs(18),
      last_trading_date_ms: sessionOpenMs(20),
      artifact_count: 3,
    },
  ],
};

async function renderSummary(summary: StorageSummaryResponse) {
  return render(LakeStorageSummaryComponent, { componentInputs: { summary } });
}

describe('LakeStorageSummaryComponent', () => {
  it('says an empty catalog is empty rather than showing zeroed tables', async () => {
    await renderSummary({ market: 'usa', kinds: [], symbols: [] });

    expect(screen.getByText(/The catalog holds no artifacts yet/)).toBeTruthy();
    expect(screen.queryByRole('table')).toBeNull();
  });

  it('totals artifacts and bytes across every kind', async () => {
    await renderSummary(POPULATED);

    expect(screen.getByText('4')).toBeTruthy();
    expect(screen.getByText('4.00 MB')).toBeTruthy();
    expect(screen.getByText('3.00 MB')).toBeTruthy();
    expect(screen.getByText('1.00 MB')).toBeTruthy();
  });

  it('renders artifact kinds as operator language, not raw codes', async () => {
    await renderSummary(POPULATED);

    expect(screen.getByText('Minute Trade')).toBeTruthy();
    expect(screen.getByText('Map File')).toBeTruthy();
  });

  it('anchors each coverage span to its ET trading date', async () => {
    const { container } = await renderSummary(POPULATED);

    const dates = Array.from(container.querySelectorAll('[data-timestamp-mode]'));
    expect(dates.every((node) => node.getAttribute('data-timestamp-mode') === 'date-et')).toBe(true);
    expect(screen.getByText('2026-05-18')).toBeTruthy();
    expect(screen.getByText('2026-05-20')).toBeTruthy();
  });

  it('passes AXE', async () => {
    await renderSummary(POPULATED);

    const results = await axe.run(document.body, {
      rules: { 'color-contrast': { enabled: false }, region: { enabled: false } },
    });

    expect(results.violations).toEqual([]);
  });
});
