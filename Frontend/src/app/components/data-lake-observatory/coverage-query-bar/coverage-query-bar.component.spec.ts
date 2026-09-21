import { fireEvent, render, screen } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it, vi } from 'vitest';

import {
  CoverageQueryBarComponent,
  type ObservatoryQuery,
} from './coverage-query-bar.component';
import {
  fakeEnsureCoverage,
  fakeVendorCatalog,
  provideFakeEnsureCoverage,
  provideFakeVendorCatalog,
} from '../../../shared/symbol-catalog/testing/fake-symbol-catalog';
import {
  fakeTickerCatalog,
  provideFakeTickerCatalog,
} from '../../../shared/ticker-catalog/testing/fake-ticker-catalog';

const INITIAL: ObservatoryQuery = {
  symbolsText: 'SPY',
  startTradingDate: '2026-05-18',
  endTradingDate: '2026-05-22',
  dataType: 'trade',
  priceAdjustmentMode: 'raw',
};

/** Held lake rows for the joined catalog the card adapts. */
const PICKER_POOL = [
  { symbol: 'SPY', name: 'SPDR S&P 500', firstHeld: '2024-01-02', lastHeld: '2026-09-18' },
  { symbol: 'AAPL', name: 'Apple Inc.', firstHeld: '2024-01-02', lastHeld: '2026-09-18' },
];

async function renderBar(
  initial: ObservatoryQuery = INITIAL,
  options: { maxTradingRangeDays?: number } = {},
) {
  const applied = vi.fn();
  const view = await render(CoverageQueryBarComponent, {
    componentInputs: {
      initial,
      maxSymbolLength: 20,
      maxTradingRangeDays: options.maxTradingRangeDays ?? 1830,
    },
    providers: [
      provideFakeTickerCatalog(fakeTickerCatalog(PICKER_POOL)),
      provideFakeVendorCatalog(fakeVendorCatalog()),
      provideFakeEnsureCoverage(fakeEnsureCoverage()),
    ],
  });
  view.fixture.componentInstance.applied.subscribe(applied);
  return { ...view, applied };
}

/** Adds a symbol through the card's search, as an operator does. */
async function addSymbol(view: { fixture: { detectChanges: () => void } }, query: string): Promise<void> {
  fireEvent.input(screen.getByLabelText('Search to add a ticker'), { target: { value: query } });
  const option = await screen.findByRole('option', { name: new RegExp(query, 'i') });
  fireEvent.click(option);
  view.fixture.detectChanges();
}

describe('CoverageQueryBarComponent', () => {
  it('does not re-query while the operator is still searching — a chip is not a query', async () => {
    const view = await renderBar();

    fireEvent.input(screen.getByLabelText('Search to add a ticker'), { target: { value: 'AAP' } });

    expect(view.applied).not.toHaveBeenCalled();
  });

  it('emits the chip list once applied — the free-text symbols input is gone', async () => {
    const { applied, fixture } = await renderBar();

    // No free-text symbols field survives the conversion; chips are the
    // editable form of the query's symbolsText.
    expect(screen.queryByLabelText('Symbols')).toBeNull();

    await addSymbol({ fixture }, 'AAPL');
    fireEvent.click(screen.getByRole('button', { name: 'Load coverage' }));

    expect(applied).toHaveBeenCalledWith(
      expect.objectContaining({ symbolsText: 'SPY, AAPL', startTradingDate: '2026-05-18' }),
    );
  });

  it('carries the chosen data type and adjustment mode', async () => {
    const { applied } = await renderBar();

    fireEvent.change(screen.getByLabelText('Data type'), { target: { value: 'quote' } });
    fireEvent.change(screen.getByLabelText('Price adjustment'), {
      target: { value: 'lean_adjusted' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Load coverage' }));

    expect(applied).toHaveBeenCalledWith(
      expect.objectContaining({ dataType: 'quote', priceAdjustmentMode: 'lean_adjusted' }),
    );
  });

  it('applies a data type or price adjustment change immediately, without Load coverage', async () => {
    // A <select> has no half-typed state the way free-text symbols do, so it
    // does not wait on the "Load coverage" click. Without this, the backfill
    // panel below — seeded from the applied query, not the draft — would
    // silently keep submitting the previously-applied mode while this
    // dropdown already shows the new one.
    const { applied } = await renderBar();

    fireEvent.change(screen.getByLabelText('Price adjustment'), {
      target: { value: 'polygon_split_adjusted' },
    });

    expect(applied).toHaveBeenCalledWith(
      expect.objectContaining({ priceAdjustmentMode: 'polygon_split_adjusted' }),
    );
  });

  it('does not apply a select change while the rest of the draft is invalid', async () => {
    const { applied } = await renderBar({ ...INITIAL, symbolsText: '' });

    fireEvent.change(screen.getByLabelText('Price adjustment'), {
      target: { value: 'polygon_split_adjusted' },
    });

    expect(applied).not.toHaveBeenCalled();
  });

  it('renders the adjustment vocabulary as operator language, not raw codes', async () => {
    await renderBar();

    expect(screen.getByRole('option', { name: 'Polygon Split Adjusted' })).toBeTruthy();
  });

  it('names an unstorable seed symbol instead of sending it', async () => {
    // The card cannot mint junk; only a hand-edited URL query can — the
    // boundary the invalid note now guards.
    const { applied } = await renderBar({ ...INITIAL, symbolsText: '9x' });

    expect(screen.getByText('Not a storable symbol: 9x')).toBeTruthy();
    expect((screen.getByRole('button', { name: 'Load coverage' }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Load coverage' }));
    expect(applied).not.toHaveBeenCalled();
  });

  it('refuses an inverted window', async () => {
    const { applied } = await renderBar();

    fireEvent.input(screen.getByLabelText('Start trading date'), {
      target: { value: '2026-05-30' },
    });

    expect(screen.getByText('The start date is after the end date.')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Load coverage' }));
    expect(applied).not.toHaveBeenCalled();
  });

  it('accepts a window exactly at the range cap', async () => {
    // 1830 inclusive days from 2026-01-01 is 2031-01-04.
    const { applied } = await renderBar({
      ...INITIAL,
      startTradingDate: '2026-01-01',
      endTradingDate: '2031-01-04',
    });

    fireEvent.click(screen.getByRole('button', { name: 'Load coverage' }));

    expect(applied).toHaveBeenCalled();
  });

  it('blocks a window one day past the cap instead of spending a 422 on it', async () => {
    const { applied } = await renderBar({
      ...INITIAL,
      startTradingDate: '2026-01-01',
      endTradingDate: '2031-01-05',
    });

    expect(
      screen.getByText('That window is 1831 days; the data plane accepts at most 1830.'),
    ).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Load coverage' }));
    expect(applied).not.toHaveBeenCalled();
  });

  it('honours a cap the data plane lowered', async () => {
    const { applied } = await renderBar(
      { ...INITIAL, startTradingDate: '2026-05-01', endTradingDate: '2026-05-31' },
      { maxTradingRangeDays: 30 },
    );

    expect(
      screen.getByText('That window is 31 days; the data plane accepts at most 30.'),
    ).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Load coverage' }));
    expect(applied).not.toHaveBeenCalled();
  });

  it('passes AXE', async () => {
    await renderBar();

    const results = await axe.run(document.body, {
      rules: { 'color-contrast': { enabled: false }, region: { enabled: false } },
    });

    expect(results.violations).toEqual([]);
  });
});
