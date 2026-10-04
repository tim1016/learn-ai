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
    ...overrides,
  };
}

const GLD_REBUILD: RebuildRequest = { symbol: 'GLD', from: '2024-08-06', to: '2026-09-18' };
const ROWS: readonly SymbolReadinessRow[] = [
  row({ symbol: 'GLD', state: 'unreadable', runReadyCount: 0, rebuildableCount: 532, rebuild: GLD_REBUILD }),
  // Rebuilt already; what is left is older than the provider's history.
  row({ symbol: 'META', state: 'partial', sessionCount: 71, runReadyCount: 63, unrebuildableCount: 8 }),
  row({ symbol: 'SPY', sessionCount: 1252, runReadyCount: 1252 }),
];

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
