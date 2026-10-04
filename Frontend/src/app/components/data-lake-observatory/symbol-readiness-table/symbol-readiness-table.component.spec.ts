import { fireEvent, render, screen, within } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import type { RebuildRequest, SymbolReadinessRow } from '../lib/lake-readiness';
import { SymbolReadinessTableComponent } from './symbol-readiness-table.component';

/** 09:30 America/New_York on an EDT date, as int64 ms UTC. */
function sessionOpenMs(year: number, month: number, day: number): number {
  return Date.UTC(year, month - 1, day, 13, 30);
}

function row(overrides: Partial<SymbolReadinessRow> & { symbol: string }): SymbolReadinessRow {
  return {
    state: 'ready',
    sessionCount: 532,
    runReadyCount: 532,
    rebuildableCount: 0,
    unrebuildableCount: 0,
    firstTradingDateMs: sessionOpenMs(2024, 8, 6),
    lastTradingDateMs: sessionOpenMs(2026, 9, 18),
    rebuild: null,
    gapSessionCount: 0,
    gapCount: 0,
    unfillableGapCount: 0,
    fill: null,
    ...overrides,
  };
}

const GLD_REBUILD: RebuildRequest = {
  symbol: 'GLD',
  from: '2024-08-06',
  to: '2026-09-18',
  includeQuotes: false,
};
const ROWS: readonly SymbolReadinessRow[] = [
  row({ symbol: 'GLD', state: 'unreadable', runReadyCount: 0, rebuildableCount: 532, rebuild: GLD_REBUILD }),
  // Rebuilt already; what is left is older than the provider's history.
  row({ symbol: 'META', state: 'partial', sessionCount: 71, runReadyCount: 63, unrebuildableCount: 8 }),
  row({ symbol: 'SPY', sessionCount: 1252, runReadyCount: 1252 }),
];

const QQQ_FILL: RebuildRequest = { symbol: 'QQQ', from: '2021-11-01', to: '2024-09-03', includeQuotes: false };
/** Held in two stretches: every held session is readable, and 724 between them were never fetched. */
const QQQ = row({
  symbol: 'QQQ',
  sessionCount: 531,
  runReadyCount: 531,
  gapSessionCount: 724,
  gapCount: 1,
  unfillableGapCount: 10,
  fill: QQQ_FILL,
});

function tableRow(symbol: string): HTMLElement {
  return screen.getByRole('button', { name: `Show ${symbol} sessions` }).closest('tr') as HTMLElement;
}

describe('SymbolReadinessTableComponent', () => {
  it('answers whether a run can read each symbol, and offers a rebuild only where one can help', async () => {
    await render(SymbolReadinessTableComponent, { componentInputs: { rows: ROWS } });

    expect(within(tableRow('GLD')).getByText('No · needs rebuild')).toBeTruthy();
    expect(within(tableRow('GLD')).getByRole('button', { name: 'Rebuild GLD' })).toBeTruthy();

    // No button promises a fix a backfill cannot deliver.
    expect(within(tableRow('META')).getByText('Partly · too old to rebuild')).toBeTruthy();
    expect(within(tableRow('META')).getByText("8 older than the provider's history")).toBeTruthy();
    expect(within(tableRow('META')).queryByRole('button', { name: 'Rebuild META' })).toBeNull();

    expect(within(tableRow('SPY')).getByText('Yes')).toBeTruthy();
  });

  it("hands the symbol's own window to a rebuild, and holds it while a backfill runs", async () => {
    const view = await render(SymbolReadinessTableComponent, { componentInputs: { rows: ROWS } });
    const requests: RebuildRequest[] = [];
    view.fixture.componentInstance.rebuild.subscribe((request) => requests.push(request));

    fireEvent.click(screen.getByRole('button', { name: 'Rebuild GLD' }));
    expect(requests).toEqual([GLD_REBUILD]);

    view.fixture.componentRef.setInput('busy', true);
    view.detectChanges();
    expect((screen.getByRole('button', { name: 'Rebuild GLD' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('flags a symbol with sessions missing between its first and last, and offers to fill the gap', async () => {
    const view = await render(SymbolReadinessTableComponent, { componentInputs: { rows: [QQQ, ...ROWS] } });
    const requests: RebuildRequest[] = [];
    view.fixture.componentInstance.rebuild.subscribe((request) => requests.push(request));

    // Every held session is readable, so "Yes" would hide the hole a run falls into.
    const qqq = tableRow('QQQ');
    expect(within(qqq).getByText('Has a gap')).toBeTruthy();
    expect(within(qqq).queryByText('Yes')).toBeNull();
    expect(qqq.textContent.replace(/\s+/g, ' ')).toContain(
      '724 sessions missing between its first and last · 10 too old to fetch',
    );

    fireEvent.click(within(qqq).getByRole('button', { name: 'Fill the gap in QQQ' }));
    expect(requests).toEqual([QQQ_FILL]);

    // A symbol with a gap is not "Ready"; the gaps filter shows it alone.
    expect(screen.getByRole('button', { name: 'Ready 1' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Has gaps 1' }));
    expect(screen.getByRole('button', { name: 'Show QQQ sessions' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Show SPY sessions' })).toBeNull();
  });

  it('filters to the symbols that need a rebuild', async () => {
    await render(SymbolReadinessTableComponent, { componentInputs: { rows: ROWS } });

    fireEvent.click(screen.getByRole('button', { name: 'Needs rebuild 1' }));

    expect(screen.queryByRole('button', { name: 'Show SPY sessions' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Show GLD sessions' })).toBeTruthy();
  });

  it('picks the symbol whose sessions the page shows', async () => {
    const view = await render(SymbolReadinessTableComponent, { componentInputs: { rows: ROWS } });
    const picked: string[] = [];
    view.fixture.componentInstance.symbolSelected.subscribe((symbol) => picked.push(symbol));

    fireEvent.click(screen.getByRole('button', { name: 'Show SPY sessions' }));

    expect(picked).toEqual(['SPY']);
  });
});
