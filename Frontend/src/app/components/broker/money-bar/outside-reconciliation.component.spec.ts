import { render, screen } from '@testing-library/angular';
import { expect, it } from 'vitest';

import { OutsideReconciliationComponent } from './outside-reconciliation.component';

it('keeps an unresolved population visible with the backend next action', async () => {
  await render(OutsideReconciliationComponent, { inputs: { rows: [{
    symbol: 'BTC/USD', state: 'unresolved', evidence_ids: [],
    detail: 'The quantity fees do not balance the recorded fills.',
    next_action: 'Compare the complete fill and crypto-fee records at Alpaca.',
  }] } });
  expect(screen.getByRole('status').textContent).toContain('Outside fills unresolved.');
  expect(screen.getByRole('status').textContent).toContain('Compare the complete fill and crypto-fee records at Alpaca.');
  expect(screen.queryByText('Outside fills reconciled.')).toBeNull();
});
