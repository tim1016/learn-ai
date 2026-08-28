import { fireEvent, render, screen } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it, vi } from 'vitest';

import {
  CoverageQueryBarComponent,
  type ObservatoryQuery,
} from './coverage-query-bar.component';

const INITIAL: ObservatoryQuery = {
  symbolsText: 'SPY',
  startTradingDate: '2026-05-18',
  endTradingDate: '2026-05-22',
  dataType: 'trade',
  priceAdjustmentMode: 'raw',
};

async function renderBar(initial: ObservatoryQuery = INITIAL) {
  const applied = vi.fn();
  const view = await render(CoverageQueryBarComponent, {
    componentInputs: { initial, maxSymbolLength: 20 },
  });
  view.fixture.componentInstance.applied.subscribe(applied);
  return { ...view, applied };
}

describe('CoverageQueryBarComponent', () => {
  it('does not re-query while the operator is still typing', async () => {
    const { applied } = await renderBar();

    fireEvent.input(screen.getByLabelText('Symbols'), { target: { value: 'AAP' } });

    expect(applied).not.toHaveBeenCalled();
  });

  it('emits the canonical symbol list once applied', async () => {
    const { applied } = await renderBar();

    fireEvent.input(screen.getByLabelText('Symbols'), { target: { value: 'aapl, spy aapl' } });
    fireEvent.click(screen.getByRole('button', { name: 'Load coverage' }));

    expect(applied).toHaveBeenCalledWith(
      expect.objectContaining({ symbolsText: 'AAPL, SPY', startTradingDate: '2026-05-18' }),
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

  it('renders the adjustment vocabulary as operator language, not raw codes', async () => {
    await renderBar();

    expect(screen.getByRole('option', { name: 'Polygon Split Adjusted' })).toBeTruthy();
  });

  it('names an unstorable symbol instead of sending it', async () => {
    const { applied } = await renderBar();

    fireEvent.input(screen.getByLabelText('Symbols'), { target: { value: '9x' } });

    expect(screen.getByText('Not a storable symbol: 9x')).toBeTruthy();
    expect((screen.getByRole('button', { name: 'Load coverage' }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Load coverage' }));
    expect(applied).not.toHaveBeenCalled();
  });

  it('refuses an inverted window', async () => {
    await renderBar();

    fireEvent.input(screen.getByLabelText('Start trading date'), {
      target: { value: '2026-05-30' },
    });

    expect(screen.getByText('Pick a start date on or before the end date.')).toBeTruthy();
  });

  it('passes AXE', async () => {
    await renderBar();

    const results = await axe.run(document.body, {
      rules: { 'color-contrast': { enabled: false }, region: { enabled: false } },
    });

    expect(results.violations).toEqual([]);
  });
});
