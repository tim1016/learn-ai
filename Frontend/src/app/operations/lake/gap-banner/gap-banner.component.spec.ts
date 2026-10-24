import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import type { FillRow, RebuildRequest } from '../lib/lake-readiness';
import { GapBannerComponent } from './gap-banner.component';

function gapRow(symbol: string, gapSessionCount: number): FillRow {
  return {
    symbol,
    state: 'ready',
    sessionCount: 531,
    runReadyCount: 531,
    rebuildableCount: 0,
    unrebuildableCount: 0,
    firstTradingDateMs: Date.UTC(2021, 9, 5, 13, 30),
    lastTradingDateMs: Date.UTC(2026, 9, 2, 13, 30),
    rebuild: null,
    gapSessionCount,
    gapCount: 1,
    unfillableGapCount: 0,
    fill: { symbol, from: '2021-10-18', to: '2024-09-03', includeQuotes: false },
  };
}

describe('GapBannerComponent', () => {
  it('names the one symbol with a gap and fills it', async () => {
    const view = await render(GapBannerComponent, { componentInputs: { rows: [gapRow('QQQ', 1)] } });
    const filled: (readonly RebuildRequest[])[] = [];
    view.fixture.componentInstance.fill.subscribe((requests) => filled.push(requests));

    expect(screen.getByRole('heading', { name: '1 symbol has sessions missing between its first and last' })).toBeTruthy();
    expect(view.container.textContent.replace(/\s+/g, ' ')).toContain('1 session, 2021-10-18 to 2024-09-03');

    fireEvent.click(screen.getByRole('button', { name: 'Fill the gap in QQQ' }));
    expect(filled).toEqual([[{ symbol: 'QQQ', from: '2021-10-18', to: '2024-09-03', includeQuotes: false }]]);
  });

  it('names five symbols, counts the rest, and still fills every one', async () => {
    const symbols = ['AAA', 'BBB', 'CCC', 'DDD', 'EEE', 'FFF', 'GGG'];
    const view = await render(GapBannerComponent, { componentInputs: { rows: symbols.map((symbol) => gapRow(symbol, 3)) } });
    const filled: (readonly RebuildRequest[])[] = [];
    view.fixture.componentInstance.fill.subscribe((requests) => filled.push(requests));

    expect(view.container.querySelectorAll('app-asset-identity').length).toBe(5);
    expect(screen.getByText('and 2 more, listed under "Has gaps" below')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Fill the gaps in all 7 symbols' }));
    expect(filled[0].map((request) => request.symbol)).toEqual(symbols);
  });

  it('holds its button while a backfill runs', async () => {
    await render(GapBannerComponent, { componentInputs: { rows: [gapRow('QQQ', 723)], busy: true } });

    expect((screen.getByRole('button', { name: 'Fill the gap in QQQ' }) as HTMLButtonElement).disabled).toBe(true);
  });
});
