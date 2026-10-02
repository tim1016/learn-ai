import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, it, expect } from 'vitest';
import { RunFillsListComponent } from './run-fills-list.component';
import type { ChartFillMarker } from '../lib/broker-v2-panel.types';

const BUY_FILL: ChartFillMarker = {
  filled_at_ms: 1_753_800_000_000,
  side: 'buy',
  quantity: 100,
  price: 512.3,
  order_ref: 'ord-buy-001',
  event_key: 'exec-buy-001',
};

function fills(count: number): ChartFillMarker[] {
  return Array.from({ length: count }, (_, index) => ({
    ...BUY_FILL,
    filled_at_ms: BUY_FILL.filled_at_ms + index * 5_000,
    price: 500 + index,
    order_ref: `ord-${index}`,
    event_key: `exec-${index}`,
  }));
}

describe('RunFillsListComponent (#2794)', () => {
  it('says when the run has no fills', async () => {
    await render(RunFillsListComponent, { inputs: { fills: [], fillCount: 0 } });

    expect(screen.getByRole('status').textContent?.trim()).toBe('No fills this run.');
  });

  it('bounds the inline list to the newest fills and opens every one it was sent', async () => {
    await render(RunFillsListComponent, { inputs: { fills: fills(6), fillCount: 6 } });

    const inline = screen.getByRole('table', { name: 'Fills this run' });
    expect(inline.querySelectorAll('tbody tr')).toHaveLength(4);
    expect(inline.textContent).not.toContain('$501.00');
    expect(inline.textContent).toContain('$505.00');
    fireEvent.click(screen.getByRole('button', { name: 'View all 6 fills' }));
    expect((await screen.findByRole('table', { name: 'All fills this run' })).querySelectorAll('tbody tr')).toHaveLength(6);
  });

  it('says when the backend sent only the run’s newest fills', async () => {
    await render(RunFillsListComponent, { inputs: { fills: fills(50), fillCount: 73 } });

    fireEvent.click(screen.getByRole('button', { name: 'View the newest 50 of 73 fills' }));
    expect(await screen.findByText("The newest 50 of this run's 73 fills.")).toBeTruthy();
  });
});
