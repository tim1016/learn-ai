import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { FillTableComponent } from './fill-table.component';

describe('FillTableComponent', () => {
  it('renders timestamped buy and sell fills from the chart projection', async () => {
    await render(FillTableComponent, {
      inputs: {
        label: 'All fills today',
        fills: [
          { filled_at_ms: 1_753_800_000_000, side: 'buy', quantity: 2, price: 625.1, order_ref: 'buy-1', event_key: 'exec-buy-1' },
          { filled_at_ms: 1_753_800_005_000, side: 'sell', quantity: 1, price: 626.2, order_ref: 'sell-1', event_key: 'exec-sell-1' },
        ],
      },
    });

    expect(screen.getByRole('table', { name: 'All fills today' })).toBeTruthy();
    expect(screen.getByText('Buy')).toBeTruthy();
    expect(screen.getByText('Sell')).toBeTruthy();
    expect(screen.getByText('$625.10')).toBeTruthy();
    expect(screen.getByText('$626.20')).toBeTruthy();
    expect(screen.queryByRole('columnheader', { name: 'Asset' })).toBeNull();
  });

  it('names each fill’s asset for a bot that trades something other than what it listens to (#2912)', async () => {
    await render(FillTableComponent, {
      inputs: {
        label: 'Fills this run',
        showAsset: true,
        fills: [
          { filled_at_ms: 1_753_800_000_000, side: 'buy', quantity: 1, price: 512.4, symbol: 'QQQ', order_ref: 'buy-1', event_key: 'exec-buy-1' },
        ],
      },
    });

    expect(screen.getByRole('columnheader', { name: 'Asset' })).toBeTruthy();
    expect(screen.getByText('QQQ')).toBeTruthy();
    expect(screen.getByText('$512.40')).toBeTruthy();
  });

  it('says how a simulated fill was priced, and spells out a price that may not be its own moment’s (#2912)', async () => {
    const fill = { filled_at_ms: 1_753_800_000_000, quantity: 1, price: 512.4 };
    await render(FillTableComponent, {
      inputs: {
        label: 'Fills this run',
        fills: [
          {
            ...fill, side: 'buy', order_ref: 'buy-1', event_key: 'exec-buy-1',
            simulated: { kind: 'bar_close', label: 'Simulated', sentence: 'Filled at its minute’s close.', caution: false },
          },
          {
            ...fill, side: 'sell', order_ref: 'sell-1', event_key: 'exec-sell-1',
            simulated: {
              kind: 'older_bar_close', label: 'Simulated · price 3 minutes old',
              sentence: 'It used a price that was 3 minutes old.', price_age_ms: 180_000, caution: true,
            },
          },
          { ...fill, side: 'sell', order_ref: 'sell-2', event_key: 'exec-sell-2' },
        ],
      },
    });

    // Every simulated fill names its rule; one Alpaca made says nothing.
    expect(screen.getByText('Simulated').getAttribute('title')).toBe('Filled at its minute’s close.');
    expect(screen.getAllByText(/Simulated/)).toHaveLength(2);
    // Only the cautioned one spells its sentence out, where it reads without a pointer.
    expect(screen.getByText('It used a price that was 3 minutes old.').closest('td')?.getAttribute('colspan')).toBe('4');
    // The other's sentence is there for a screen reader, and not as a second row.
    expect(screen.getByText('Filled at its minute’s close.').closest('tr')?.className).toContain('fill-table__row');
  });
});
